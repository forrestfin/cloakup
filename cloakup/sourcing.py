"""Durable recruitment case state, independent of any site's page selectors.

A case binds an existing job to candidate evidence, messages and human decisions.
Browser dispatch must claim an approved draft before clicking Send and record a
verified receipt afterwards. Ambiguous delivery is never retried automatically.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

INTENTS = {"first_contact", "follow_up", "request_resume"}
TERMINAL = {"recommended", "closed", "do_not_contact"}


class SourcingError(ValueError):
    pass


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CaseStore:
    def __init__(self, root: Path, database=None):
        from .storage import Database
        self.db = database or Database(root)
        self.root = root / "runs/sourcing"
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob("*.json"):
            try:
                self.db.get('case', path.stem)
            except FileNotFoundError:
                self.db.put('case', path.stem, json.loads(path.read_text()))
        for case in self.db.list('case'):
            self.load(case['id'], recover=True)
        from .action_gateway import ActionLedger
        ledger=ActionLedger(self.db)
        ledger.isolate_platform_requests()
        for operation in ledger.list():
            if operation['capability']!='send_message' or operation['state'] not in ('claimed','executing'):continue
            ledger.recover(operation['run_id'])
            current=ledger.get(operation['id'])
            if current['state']!='unknown':continue
            refs=current['payload'].get('expectation') or {}
            try:
                case=self.load(refs['case_id']);person=self.candidate(case,refs['candidate_id']);message=self._message(person,refs['message_id'])
            except (KeyError,FileNotFoundError,SourcingError):continue
            message.update(operation_id=current['id'],operation_state='unknown',status='delivery_unknown');person['status']='delivery_unknown'
            self.audit(case,'delivery_unknown',candidate_id=person['id'],message_id=message['id'],note='从统一动作账本恢复未核实发送，禁止重发')


    def _path(self, case_id: str) -> Path:
        if not isinstance(case_id, str) or not re.fullmatch(r"[a-f0-9]{32}", case_id):
            raise SourcingError("寻访任务标识不合法")
        return self.root / f"{case_id}.json"

    def load(self, case_id: str, *, recover: bool = False) -> dict[str, Any]:
        self._path(case_id)  # validate identifier before database lookup
        case = self.db.get("case", case_id)
        # A process may have exited after clicking Send but before seeing receipt.
        changed = False
        for candidate in case["candidates"].values():
            for message in candidate["outbox"]:
                if recover and message["status"] == "sending":
                    message["status"] = "delivery_unknown"
                    candidate["status"] = "delivery_unknown"
                    changed = True
        if changed:
            self.save(case)
        from .task_policy import effective_task,overlay
        task=effective_task(self.db,case_id)
        if task:
            overlay(case,task)
            try:
                release=self.db.get('agent_release',task['id'])
                bundle=self.db.get('agent_bundle',release['version_id'])['bundle']
                snapshot=next((s for s in bundle['sources'] if s['case_id']==case_id),None)
                if snapshot:
                    case.update(anonymous_job=copy.deepcopy(snapshot['anonymous_job']),confirmed_facts=copy.deepcopy(snapshot['facts']),faq=copy.deepcopy(snapshot['faq']),
                                conversation_prompt=bundle['conversation_prompt'],agent_release_id=release['version_id'])
                    if 'sourcing' in snapshot:case['job_sourcing']=copy.deepcopy(snapshot['sourcing'])
                    overlay(case,task)
            except FileNotFoundError:pass
        return case

    def save(self, case: dict[str, Any]):
        from .action_gateway import ActionLedger
        for person in case.get('candidates',{}).values():
            for message in person.get('outbox',[]):
                if message.get('operation_id') and message['status']=='delivery_unknown' and message.get('operation_state')!='unknown':
                    ledger=ActionLedger(self.db);state=ledger.get(message['operation_id'])['state']
                    if state in ('claimed','executing'):ledger.transition(message['operation_id'],'unknown',reason='会话发送结果待核实')
                    message['operation_state']='unknown'
        case["updated_at"] = time.time()
        self.db.put("case", case["id"], case)
        path = self._path(case["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(case, ensure_ascii=False, indent=2))
        tmp.replace(path)

    def audit(self, case, kind, *, candidate_id=None, **details):
        """Append a durable hash-linked event (local trace, not model input)."""
        trail = case.setdefault('audit', [])
        entry = {'id': uuid.uuid4().hex, 'at': time.time(), 'kind': kind,
                 'candidate_id': candidate_id, 'details': copy.deepcopy(details),
                 'previous_hash': trail[-1]['hash'] if trail else None}
        entry['hash'] = _fingerprint(entry)
        trail.append(entry)
        self.save(case)
        return entry

    def set_autonomy(self, case, enabled, *, source):
        if type(enabled) is not bool or not source:
            raise SourcingError('自动沟通授权参数不合法')
        old = case.get('autonomy', {})
        case['autonomy'] = {'enabled': enabled, 'revision': old.get('revision', 0) + 1,
                            'source': source, 'at': time.time(), 'daily_new_limit': old.get('daily_new_limit',20),
                            'max_messages_per_candidate_day': old.get('max_messages_per_candidate_day',6), 'poll_seconds': old.get('poll_seconds',120)}
        case['send_policy'] = 'autonomous' if enabled else 'human_approval'
        for c in case['candidates'].values():
            for message in c['outbox']:
                if message.get('approval_actor') == 'automation' and message['status'] == 'approved':
                    message.update(status='stale', approved_fingerprint=None)
        self.audit(case, 'autonomy_changed', enabled=enabled, source=source,
                   policy_revision=case['autonomy']['revision'])

    def authorize_automation(self, case, candidate_id, message_id, *, decision):
        policy = case.get('autonomy', {})
        if not policy.get('enabled') or case.get('send_policy') != 'autonomous':
            raise SourcingError('岗位未启用自动沟通')
        candidate = self.candidate(case, candidate_id)
        message = self._message(candidate, message_id)
        if (message['status'] != 'draft' or not decision
                or message['job_revision'] != case['job']['revision']
                or message['context_revision'] != candidate['context_revision']):
            raise SourcingError('自动沟通的消息或上下文已变化')
        if candidate['status'] in TERMINAL or (not decision.get('resume_receipt_ack') and any(m['status'] in ('sending','delivery_unknown') for m in candidate['outbox'])):
            raise SourcingError('候选人已停止推进或有发送结果待核实')
        message.update(status='approved', approval_actor='automation',
                       policy_revision=policy['revision'],
                       approved_fingerprint=_fingerprint(self._payload(message)),
                       authorized_at=time.time(), decision=copy.deepcopy(decision))
        self.audit(case, 'message_authorized', candidate_id=candidate_id,
                   message_id=message_id, actor='automation', decision=decision,
                   policy_revision=policy['revision'])

    def list(self):
        return sorted([self.load(c['id']) for c in self.db.list("case")],
                      key=lambda case: case["updated_at"], reverse=True)

    def create(self, job_url: str, label: str = "待绑定岗位") -> dict[str, Any]:
        parsed = urlparse(job_url)
        from .scenarios import scenario_for_host
        spec = scenario_for_host(parsed.hostname)
        if parsed.scheme != "https" or spec is None or parsed.username or parsed.password:
            raise SourcingError("请输入已登记寻访网站上的 HTTPS 岗位页面地址")
        case = {"id": uuid.uuid4().hex, "platform": spec["platform"], "created_at": time.time(),
                "job": {"url": job_url, "label": label, "snapshot": None, "revision": 0},
                "stage": "job_binding", "greeting_draft": "", "search_plan": None,
                "candidates": {}, "events": [], "send_policy": "human_approval"}
        from .followup import DEFAULTS
        case['execution']=copy.deepcopy(DEFAULTS)
        self.save(case)
        return case

    def bind_job(self, case, snapshot):
        if not isinstance(snapshot, dict) or not snapshot.get("title") or not snapshot.get("requirements"):
            raise SourcingError("岗位需包含正式名称与要求，缺失时需补充读取")
        if snapshot == case["job"]["snapshot"]:
            return
        case["job"].update(snapshot=copy.deepcopy(snapshot), label=snapshot["title"],
                           revision=case["job"]["revision"] + 1)
        case["stage"] = "greeting_draft"
        for candidate in case["candidates"].values():
            self._invalidate(candidate)
        self.save(case)

    def candidate(self, case, candidate_id):
        try:
            return case["candidates"][candidate_id]
        except KeyError as exc:
            raise SourcingError("候选人不存在") from exc

    def add_candidate(self, case, candidate_id, source_url, profile):
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise SourcingError("候选人必须有可去重的稳定标识")
        candidate = case["candidates"].get(candidate_id)
        if candidate:
            if profile != candidate["profile"]:
                candidate["profile"] = copy.deepcopy(profile)
                self._invalidate(candidate)
        else:
            candidate = {"id": candidate_id, "source_url": source_url,
                         "profile": copy.deepcopy(profile), "context_revision": 1,
                         "status": "profile_collected", "evidence": [], "inbox": [],
                         "outbox": [], "resumes": [], "decision": None}
            case["candidates"][candidate_id] = candidate
        self.save(case)
        return candidate

    @staticmethod
    def _invalidate(candidate):
        candidate["context_revision"] += 1
        for draft in candidate["outbox"]:
            if draft["status"] in ("draft", "approved"):
                draft.update(status="stale", approved_fingerprint=None)

    def draft(self, case, candidate_id, intent, text):
        candidate = self.candidate(case, candidate_id)
        if candidate["status"] in TERMINAL:
            raise SourcingError("该候选人已结束推进，不能创建沟通草稿")
        if not case["job"]["snapshot"]:
            raise SourcingError("请先确认绑定岗位")
        if intent not in INTENTS or not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise SourcingError("沟通意图或正文不合法")
        from .communication_policy import check_compensation_disclosure
        check_compensation_disclosure(text)
        if intent == "first_contact" and any(m["intent"] == intent and m["status"] in ("sending", "sent", "delivery_unknown") for m in candidate["outbox"]):
            raise SourcingError("已有首次联系记录，请先核实会话，不能重复开聊")
        draft = {"id": uuid.uuid4().hex, "candidate_id": candidate_id,
                 "job_revision": case["job"]["revision"],
                 "context_revision": candidate["context_revision"], "intent": intent,
                 "text": text.strip(), "status": "draft", "approved_fingerprint": None,
                 "created_at": time.time(), "receipt": None}
        candidate["outbox"].append(draft)
        self.save(case)
        return draft

    @staticmethod
    def _message(candidate, message_id):
        message = next((m for m in candidate["outbox"] if m["id"] == message_id), None)
        if message is None:
            raise SourcingError("未找到该消息草稿")
        return message

    @staticmethod
    def _payload(message):
        return {k: message[k] for k in ("id", "candidate_id", "job_revision", "context_revision", "intent", "text")}

    def approve(self, case, candidate_id, message_id, displayed_fingerprint):
        candidate = self.candidate(case, candidate_id)
        message = self._message(candidate, message_id)
        fingerprint = _fingerprint(self._payload(message))
        if message["status"] != "draft" or displayed_fingerprint != fingerprint:
            raise SourcingError("草稿已变化或已处理，请重新查看后确认")
        if candidate["status"] in TERMINAL:
            raise SourcingError("该候选人已结束推进")
        message.update(status="approved", approved_fingerprint=fingerprint, approved_at=time.time())
        self.save(case)

    def preview(self, case, candidate_id, message_id):
        message = self._message(self.candidate(case, candidate_id), message_id)
        return {**copy.deepcopy(message), "fingerprint": _fingerprint(self._payload(message))}

    def claim_send(self, case, candidate_id, message_id, run_id=None, observed_snapshot=None):
        candidate = self.candidate(case, candidate_id)
        message = self._message(candidate, message_id)
        from .communication_policy import check_compensation_disclosure
        check_compensation_disclosure(message['text'])
        if candidate["status"] in TERMINAL or message["status"] != "approved":
            raise SourcingError("仅能发送已由顾问确认、且尚未发送的消息")
        if message.get('approval_actor') == 'automation' and (
                not case.get('autonomy', {}).get('enabled')
                or message.get('policy_revision') != case['autonomy']['revision']):
            raise SourcingError('自动沟通授权已停止或变化')
        if message["job_revision"] != case["job"]["revision"] or message["context_revision"] != candidate["context_revision"] or message["approved_fingerprint"] != _fingerprint(self._payload(message)):
            raise SourcingError("岗位、会话或消息内容已变化，需要重新起草并确认")
        from .receipt_policy import continuation
        receipt_continuation=bool(message.get('approval_actor')=='automation' and message.get('decision',{}).get('resume_receipt_ack') and observed_snapshot and 'acknowledge_resume' in continuation(candidate,observed_snapshot))
        if not receipt_continuation and any(m["id"] != message_id and m["status"] in ("sending", "delivery_unknown") for m in candidate["outbox"]):
            raise SourcingError("有发送状态待核实的消息，需先核实，不能重复发送")
        if message["intent"] == "first_contact" and any(m["id"] != message_id and m["intent"] == "first_contact" and m["status"] == "sent" for m in candidate["outbox"]):
            raise SourcingError("已联系过该候选人，不能重复开聊")
        from .action_gateway import ActionLedger
        ledger=ActionLedger(self.db)
        operation=ledger.claim(task_id='case:'+case['id'],resource='candidate:'+candidate_id+(':resume-receipt' if receipt_continuation else ''),capability='send_message',identity={'intent':message['intent'],'reply_id':message.get('decision',{}).get('reply_id'),'text_hash':hashlib.sha256(message['text'].encode()).hexdigest(),**({'resume_receipt_key':message['decision']['resume_receipt_key']} if message.get('decision',{}).get('resume_receipt_key') else {})},run_id=run_id or message_id,expectation={'case_id':case['id'],'candidate_id':candidate_id,'message_id':message_id,'job_revision':case['job']['revision']})
        ledger.transition(operation,'executing')
        message.update(operation_id=operation,operation_state='executing',status="sending", claimed_at=time.time())
        candidate["status"] = "sending"
        self.audit(case, 'send_claimed', candidate_id=candidate_id, message_id=message_id,
                   text_sha256=hashlib.sha256(message['text'].encode()).hexdigest())
        return copy.deepcopy(self._payload(message))

    def confirm_delivery(self, case, candidate_id, message_id, receipt, *, submission_only=False):
        candidate = self.candidate(case, candidate_id)
        message = self._message(candidate, message_id)
        if message["status"] not in ("sending", "delivery_unknown") or not isinstance(receipt, dict) or not receipt.get("evidence"):
            raise SourcingError("需在真实会话中核实发送结果并提供证据")
        if submission_only and (message['intent']!='first_contact' or message['status']!='sending' or receipt.get('delivery_status')!='submitted'):
            raise SourcingError('仅当前已执行的首聊可记录为提交')
        if message.get('operation_id'):
            from .action_gateway import ActionLedger
            ledger=ActionLedger(self.db)
            if ledger.get(message['operation_id'])['state']!='verified':ledger.transition(message['operation_id'],'verified',evidence=receipt['evidence'],verification='browser_submission' if submission_only else 'platform_receipt')
            message['operation_state']='verified'
        message.update(status="sent", receipt=copy.deepcopy(receipt), sent_at=time.time())
        candidate["status"] = "resume_requested" if message["intent"] == "request_resume" else "waiting_reply"
        self.audit(case, 'first_contact_submitted' if submission_only else 'delivery_verified', candidate_id=candidate_id, message_id=message_id, receipt=receipt)

    def record_existing_contact(self, case, candidate_id, external_id, text, receipt):
        """Record an observed platform message without inventing send approval.

        The caller must match the opened conversation to this candidate and job.
        This imports evidence only; it must never invoke a browser send action.
        """
        candidate = self.candidate(case, candidate_id)
        if (not isinstance(external_id, str) or not external_id.strip()
                or not isinstance(text, str) or not text.strip()
                or not isinstance(receipt, dict)
                or receipt.get('candidate_id') != candidate_id
                or receipt.get('job_revision') != case['job']['revision']
                or not receipt.get('evidence') or not receipt.get('identity_evidence')
                or receipt.get('delivery_status') not in ('delivered', 'read')):
            raise SourcingError('已有联系需核对收件人、岗位、正文与送达证据')
        previous = next((m for m in candidate['outbox'] if m.get('external_id') == external_id), None)
        if previous:
            if previous['text'] != text.strip():
                raise SourcingError('同一已有消息的正文不一致，需重新核实')
            return previous
        if any(m['status'] in ('sending', 'delivery_unknown', 'sent')
               and m['intent'] == 'first_contact' for m in candidate['outbox']):
            raise SourcingError('已有首聊发送记录，需核对原记录，不能另建一条')
        self._invalidate(candidate)
        message = {'id': uuid.uuid4().hex, 'candidate_id': candidate_id,
                   'job_revision': case['job']['revision'],
                   'context_revision': candidate['context_revision'],
                   'intent': 'first_contact', 'text': text.strip(), 'status': 'sent',
                   'origin': 'external_observed', 'external_id': external_id,
                   'approved_fingerprint': None, 'created_at': time.time(),
                   'receipt': copy.deepcopy(receipt), 'observed_at': time.time()}
        candidate['outbox'].append(message)
        if candidate['status'] not in TERMINAL:
            candidate['status'] = 'replied' if candidate['inbox'] else 'waiting_reply'
        self.save(case)
        return message

    def receive(self, case, candidate_id, external_id, text):
        candidate = self.candidate(case, candidate_id)
        if not external_id or not isinstance(text, str):
            raise SourcingError("回复需要稳定消息标识及正文")
        if any(m["external_id"] == external_id for m in candidate["inbox"]):
            return False
        candidate["inbox"].append({"external_id": external_id, "text": text, "observed_at": time.time()})
        self._invalidate(candidate)
        if candidate["status"] not in TERMINAL:
            candidate["status"] = "replied"
        self.save(case)
        return True

    def attach_resume(self, case, candidate_id, document_ref):
        candidate = self.candidate(case, candidate_id)
        if not isinstance(document_ref, str) or not document_ref.strip():
            raise SourcingError("请提供已收到简历的文件引用")
        if document_ref not in candidate["resumes"]:
            candidate["resumes"].append(document_ref)
            self._invalidate(candidate)
        if candidate["status"] not in TERMINAL:
            candidate["status"] = "resume_received"
        self.save(case)

    def decide(self, case, candidate_id, decision, note):
        if decision not in TERMINAL or not isinstance(note, str) or not note.strip():
            raise SourcingError("顾问需选择推进结果并填写说明")
        candidate = self.candidate(case, candidate_id)
        if any(m["status"] == "sending" for m in candidate["outbox"]):
            raise SourcingError("请等待当前发送完成")
        self._invalidate(candidate)
        candidate.update(status=decision, decision={"value": decision, "note": note, "actor": "consultant", "at": time.time()})
        self.save(case)
