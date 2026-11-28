"""One persistent recruiter workflow: sync history -> decide -> send -> receipt.

Policy authorization replaces per-message questions, while identity, context,
quota and delivery checks remain mandatory. Ambiguous delivery is never retried.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import random
import re
import time

from . import boss_chat
from .autonomy import day_key, intent_labels, local_brief, next_step, compose_reply, trace_hash
from .boss_recommend import (open_list, open_profile, card_fingerprint, job_identity, ProfileChanged,
                             BossRecommendContact, RECOMMEND_URL, close_profile, verify_list, read_cards)
from .llm import LLM
from .pacing import jitter, lognormal, stable_fraction
from .logbook import log
from .sourcing import SourcingError, TERMINAL
from . import job_members, job_quota
from .job_quota import attempted_on
from .task_policy import effective_task


_UNSET=object()


@contextlib.contextmanager
def recovery_pass(params,active):
    """会话选择恢复后的核验轮次。

    目标会话尚未重新核实，本轮只读：不回复、不推进；唯一例外是同意接收候选人主动
    发起的简历（inbound_resume_accept）。无论正常结束还是抛错，都把两个参数恢复原值，
    避免只读或接收许可泄漏到后续轮次。
    """
    if not active:
        yield
        return
    saved={k:params.get(k,_UNSET) for k in ('read_only','inbound_resume_accept')}
    params.update(read_only=True,inbound_resume_accept=True)
    try:
        yield
    finally:
        for k,v in saved.items():
            if v is _UNSET:params.pop(k,None)
            else:params[k]=v


def _resume_offer_pending(person):
    """候选人已主动申请发送附件简历，尚未同意，也还没有简历文件。

    平台接收卡记为 platform_control，只写入 history，不进入 inbox。
    """
    if not person or person.get('alias_of') or person.get('resumes'):return False
    if any(a.get('kind')=='accept_resume' for a in person.get('platform_actions',[])):return False
    from .boss_resume import is_resume_permission_text
    texts=[m.get('text','') for m in person.get('inbox',[])]
    texts+=[m.get('text','') for m in person.get('history',[]) if m.get('direction')=='in']
    return any(is_resume_permission_text(text) for text in texts)


def _row_resume_offer(person,row):
    if person and (person.get('resumes') or any(a.get('kind')=='accept_resume' for a in person.get('platform_actions',[]))):return False
    if _resume_offer_pending(person):return True
    return bool(re.search(r'对方想发送(?:加密)?附件简历', row.get('text') or ''))


def bind_chat(case, row):
    matches = [c for c in case['candidates'].values() if not c.get('alias_of') and c.get('conversation',{}).get('chat_id')==row['chat_id']]
    if len(matches)==1:return matches[0]
    if matches:raise SourcingError('同一会话关联了多份候选档案')
    return None


def professional_identity_matches(profile, identity):
    """Local reconciliation only: name plus multiple exact dated role records."""
    import re
    label=profile.get('platform_label','')
    receiver=identity.get('receiver','')
    if not label or not receiver:return False
    name_matches=label==receiver or (label.endswith(('女士','先生')) and receiver.startswith(label[:-2]) and len(label[:-2])>=1)
    header=identity.get('header','')
    records=re.findall(r'(20\d{2}\.\d{2})\s+(至今|20\d{2}\.\d{2})\n([^\n]+)',profile.get('work_history',''))
    matched=0
    for start,end,work in records:
        company,sep,role=work.rpartition(' ')
        if sep and start in header and end in header and company in header and role in header:
            matched+=1
    # The actual chat header shows only the two most recent jobs. Requiring
    # all three card records makes a valid recipient impossible to reconcile.
    return name_matches and matched>=2


def adopt_platform_identity(store, case, candidate, snapshot):
    """同一 chat_id 上，平台姓名和岗位与本地不一致时，改本地绑定。不拿当前打开的另一条会话来顶替。"""
    binding = candidate.get('conversation') or {}
    receiver = (snapshot.get('receiver') or '').strip()
    job_title = (snapshot.get('job_title') or '').strip()
    chat_id = (snapshot.get('chat_id') or '').strip()
    if not snapshot.get('valid') or not receiver or not job_title or not chat_id or chat_id != binding.get('chat_id'):
        return candidate
    if binding.get('receiver') == receiver and binding.get('job_title') == job_title:
        return candidate
    previous = {'receiver': binding.get('receiver'), 'job_title': binding.get('job_title')}
    candidate['conversation'] = {**binding, 'chat_id': chat_id, 'receiver': receiver, 'job_title': job_title}
    candidate.setdefault('profile', {})['platform_label'] = receiver
    store.audit(case, 'platform_identity_adopted', candidate_id=candidate['id'], previous=previous,
                binding=candidate['conversation'], reason='同一会话的平台姓名或岗位与本地不一致，已按平台更新')
    return store.candidate(store.load(case['id']), candidate['id'])


def import_chat_identity(store,case,identity):
    bound=bind_chat(case,identity)
    if bound:return bound
    matches=[c for c in case['candidates'].values() if not c.get('alias_of') and professional_identity_matches(c['profile'],identity)]
    if len(matches)>1:raise SourcingError('已有会话与多份资料相似，停止自动关联')
    if matches:
        c=matches[0]
    else:
        cid='boss-chat:'+identity['chat_id']
        c=store.add_candidate(case,cid,boss_chat.CHAT_URL,{
            'display_name':'候选人 '+str(len(case['candidates'])+1),
            'platform_label':identity['receiver'],'source_channel':'conversation',
            'summary':'从沟通菜单读取的已有会话；职业经历待进一步核实',
            'work_history':identity['header'],'coverage':'当前沟通页已加载的历史'})
    c['conversation']={k:identity[k] for k in ('chat_id','receiver','job_title')}
    store.audit(case,'conversation_bound',candidate_id=c['id'],binding=c['conversation'],
                basis='同一打开会话的真实收件人、岗位与平台会话标识；既有推荐档案另核对姓名和多条带日期履历')
    return c


def reconcile_delivery(store, case, candidate, snapshot, binding):
    """Read-only receipt matching; a supplied binding must already be verified."""
    # Reconcile a previously ambiguous send from the real bound chat. This
    # only reads delivered messages; it never authorizes or repeats a click.
    for draft in candidate['outbox']:
        if draft['status']!='delivery_unknown':continue
        before_ids=draft.get('pre_send_history_ids')
        if before_ids is None:
            if draft['intent']!='first_contact':continue
            before_ids=[]
        matches=[m for m in snapshot['messages'] if m['direction']=='out' and m['text']==draft['text']
                 and m['status'] in ('delivered','read') and m['external_id'] not in before_ids]
        if len(matches)==1:
            store.confirm_delivery(case,candidate['id'],draft['id'],{
                'evidence':'在已核实收件人及岗位的真实沟通菜单中只读核对待查消息，正文一致且显示送达或已读',
                'conversation':binding,'message_id':matches[0]['external_id'],'delivery_status':matches[0]['status'],'status_evidence':matches[0].get('status_evidence'),
                'url':boss_chat.CHAT_URL,'at':time.time(),'reconciled':True})
    for draft in candidate['outbox']:
        if draft['status']=='delivery_unknown':
            from .receipt_policy import recovery_status,continuation
            draft['verification']={**recovery_status(draft),'continuation_actions':continuation(candidate,snapshot),'at':time.time(),'state':'not_confirmed',
                'reason':'当前加载的真实会话中尚无唯一新增送达消息；不等于发送失败',
                'coverage':snapshot.get('coverage','当前加载消息')}
    store.save(case)


def can_revisit_question(case,candidate):
    from .answering import answer_question
    incoming=candidate.get('inbox',[])
    question=next((m for m in incoming if m['external_id']==candidate.get('pending_question_id')),None)
    if not question or answer_question(case,question['text'])['action']!='answer':return False
    if incoming[-1]!=question and not incoming[-1].get('resume') and '点击预览附件简历' not in incoming[-1]['text']:return False
    if candidate.get('planning_answer_ready',{}).get('reply_id')==question['external_id']:return True
    return any(m['status']=='sent' and m.get('decision',{}).get('reply_id')==question['external_id'] and
               (m.get('decision',{}).get('reason')=='需顾问核实' or m.get('decision',{}).get('answer_route',{}).get('action')=='defer') for m in candidate.get('outbox',[]))


def revisit_question(store,case,candidate,snapshot):
    if not can_revisit_question(case,candidate):return
    outgoing=[m for m in snapshot['messages'] if m['direction']=='out' and not m.get('platform_control')]
    ready=candidate.get('planning_answer_ready',{}).get('reply_id')==candidate.get('pending_question_id')
    if not outgoing and not ready:return
    latest=outgoing[-1] if outgoing else None
    deferred=[m for m in candidate['outbox'] if m['status']=='sent' and m.get('decision',{}).get('reply_id')==candidate['pending_question_id']]
    if not deferred and not ready:return
    # Only resume the exact deferred turn. A later manual message wins.
    ids=[m['external_id'] for m in snapshot['messages']]
    before_question=bool(ready and latest and candidate['pending_question_id'] in ids and ids.index(latest['external_id'])<ids.index(candidate['pending_question_id']))
    automated=before_question or latest is None or any(m['status']=='sent' and m.get('approval_actor')=='automation' and m.get('receipt',{}).get('message_id')==latest['external_id'] for m in candidate['outbox'])
    if latest and (not deferred or deferred[-1].get('receipt',{}).get('message_id')!=latest['external_id']) and not automated:
        candidate.pop('pending_question_id',None);store.save(case);return
    candidate.pop('handled_reply_id',None)
    store.audit(case,'answer_reopened',candidate_id=candidate['id'],reply_id=candidate['pending_question_id'],reason='旧问题已获得明确岗位事实依据，且尚无后续人工回复，重新进入答复')


def sync_snapshot(store, case, candidate, snapshot):
    if not snapshot.get('valid') or snapshot['chat_id']!=candidate['conversation']['chat_id']:
        raise SourcingError('历史快照会话绑定不一致')
    for sent in candidate.get('outbox',[]):
        receipt=sent.get('receipt') or {}
        if sent['status']!='sent':continue
        matches=[m for m in snapshot['messages'] if m['direction']=='out' and m['external_id']==receipt.get('message_id')]
        if len(matches)==1 and matches[0]['status'] in ('delivered','read'):
            observed=matches[0]
            if receipt.get('delivery_status')!='read':receipt['delivery_status']=observed['status']
            if observed['status']=='read':receipt.setdefault('read_at',time.time())
            receipt['status_evidence']=observed.get('status_evidence');sent['receipt']=receipt
    previous_fingerprint=candidate.get('conversation_fingerprint')
    history=candidate.setdefault('history',[]);known={m['external_id'] for m in history}
    for msg in snapshot['messages']:
        if msg['direction']=='in' and msg.get('resume'):
            store.attach_resume(case,candidate['id'],'platform-message:'+msg['external_id'])
        if msg['external_id'] in known:
            previous=next(m for m in history if m['external_id']==msg['external_id'])
            if previous.get('status')!='read' and msg.get('status') in ('delivered','read'):
                previous['status']=msg['status'];previous['status_evidence']=msg.get('status_evidence')
            continue
        history.append({**copy.deepcopy(msg),'observed_at':time.time()});known.add(msg['external_id'])
        if msg['direction']=='in' and msg.get('resume') and not msg['text']:
            store.receive(case,candidate['id'],msg['external_id'],'附件简历已送达')
        if msg['direction']=='in' and msg['text'] and not msg.get('platform_control'):
            store.receive(case,candidate['id'],msg['external_id'],msg['text'])
        if msg['direction']=='in' and msg.get('resume'):
            store.attach_resume(case,candidate['id'],'platform-message:'+msg['external_id'])
        if msg['direction']=='in' and 'contact' in intent_labels(msg['text']):
            # Store actual contact data locally, not in the model brief.
            import re
            found=re.findall(r'(?<!\d)1[3-9]\d{9}(?!\d)|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',msg['text'])
            found+=re.findall(r'(?:微信|vx|v信)\s*[:：]\s*([A-Za-z][A-Za-z0-9_-]{4,})',msg['text'],re.I)
            for value in found:
                if value not in candidate.setdefault('contacts',[]):candidate['contacts'].append(value)
    candidate['history_coverage']=snapshot['coverage'];candidate['last_sync_at']=time.time()
    candidate['conversation_fingerprint']=snapshot['fingerprint']
    reconcile_delivery(store,case,candidate,snapshot,candidate['conversation'])
    # A later platform outbound message (including manual replies) already
    # handled earlier inbound history. Never answer that old turn again.
    conversational=[m for m in snapshot['messages'] if m['direction'] in ('in','out') and not m.get('platform_control')]
    if conversational and conversational[-1]['direction']=='out' and candidate['inbox']:
        last_id=candidate['inbox'][-1]['external_id']
        if any(m['external_id']==last_id for m in conversational):
            candidate['handled_reply_id']=last_id
    if conversational and conversational[-1]['direction']=='out' and candidate['status']=='profile_collected':
        candidate['status']='waiting_reply'
    if previous_fingerprint!=snapshot['fingerprint']:
        store.audit(case,'history_synced',candidate_id=candidate['id'],snapshot_sha256=snapshot['fingerprint'],
                    loaded_messages=len(snapshot['messages']),coverage=snapshot['coverage'])
    else:store.save(case)


def daily_attempts(store, intent=None, day=None):
    day=day or day_key()
    return sum(attempted_on(m,day,intent) for case in store.list() for c in case['candidates'].values() for m in c['outbox'])


# 点发送前中止的原因（boss_chat.SendAborted.reason）→ 管理员看得懂的说法
ABORT_TEXT={'reply_during_typing':'输入期间收到新消息','text_mismatch':'输入后的正文不一致','send_button_not_unique':'发送按钮不唯一',
            'not_authorized':'发送前授权已停止','changed_before_typing':'读取后会话有变化','existing_draft':'输入框已有人工草稿','editor_unavailable':'找不到唯一输入框',
            'reread_failed':'输入后读取页面失败'}


async def dispatch_reply(manager,job,session,case,candidate,snapshot,step):
    if not case.get('execution',{}).get('follow_up',True):return
    if step['action']=='wait' or (not candidate.get('inbox') and not step.get('nudge') and not step.get('resume_receipt_ack')):
        return
    if job.get('account_runner') and not manager.account_execution.can_send(case['id']):return
    from .decision_runtime import reply_target
    incoming=reply_target(candidate)
    if not incoming and not step.get('nudge') and not step.get('resume_receipt_ack'):return
    store=manager.cases;cid=candidate['id'];reply_id=incoming['external_id'] if incoming else 'initial'
    if any(m['status'] in ('sending','delivery_unknown') for m in candidate['outbox']):
        from .receipt_policy import continuation
        if not step.get('resume_receipt_ack') or 'acknowledge_resume' not in continuation(candidate,snapshot):return
    if candidate.get('status') in TERMINAL:return
    today=day_key()
    count=sum(1 for m in candidate['outbox'] if m.get('claimed_at') and day_key(m['claimed_at'])==today)
    if count>=case['autonomy']['max_messages_per_candidate_day']:return
    if step['action']=='wait':return
    if step['action']=='stop':
        store._invalidate(candidate);candidate['status']='do_not_contact';candidate['handled_reply_id']=reply_id
        store.audit(case,'candidate_opt_out',candidate_id=cid,reason=step['reason']);return
    if step['action']=='handoff':
        candidate['handoff']={'reason':step['reason'],'at':time.time()};candidate['handled_reply_id']=reply_id
        store.audit(case,'consultant_handoff',candidate_id=cid,reason=step['reason']);return
    incoming_text=incoming['text'] if incoming else ''
    pending=next((m for m in candidate['inbox'] if m['external_id']==candidate.get('pending_question_id')),None)
    if pending and can_revisit_question(case,candidate):incoming_text=pending['text']
    from .decision_runtime import effective_answer
    answer=effective_answer(case,candidate)
    if step.get('semantic_state_key'):
        from .conversation_semantics import current
        semantic=current(case,candidate)
        if not semantic or semantic['state_key']!=step['semantic_state_key'] or step.get('text')!=semantic['answer'].get('text'):
            raise SourcingError('上下文或事实已变化，旧语义回复未发送')
    nudge_state=None;nudge_hash=None
    if step.get('nudge'):
        from .followup import evaluate,render
        nudge_state=evaluate(case,candidate)
        if not nudge_state['due']:return
        text,nudge_hash=render(case,candidate,step.get('nudge_variant'),nudge_state)
        from .autonomy import validate_reply
        validate_reply(text,[],[])
        step={**step,'text':text,'labels':[]}
    closure=None
    if step.get('resume_receipt_ack'):
        from .resume_closure import pending,render
        closure=pending(candidate)
        if not closure:return
        closing=render(case,candidate,snapshot)
        if not closing:return
        step={**step,'text':closing['text'],'labels':[]}
        from .autonomy import validate_reply
        validate_reply(step['text'],[],[])
    if step.get('pending_ack'):
        if answer['action']!='defer' or candidate.get('pending_question_id')!=reply_id:return
        from .faq import question_topic
        topic=question_topic(incoming_text)
        subject={'compensation':'薪资福利','cooperation':'合作安排','location':'工作安排'}.get(topic,'具体安排')
        step={**step,'text':answer['text'] if answer.get('source')=='harness_context' else '收到，您关注的'+subject+'我核实清楚后再具体回复您。','labels':[]}
        if any(m.get('decision',{}).get('pending_ack') and m['decision'].get('reply_id')==reply_id and m['status'] in ('sending','delivery_unknown','sent') for m in candidate['outbox']):return
    if step.get('labels') and 'identity' not in step['labels']:
        if answer['action']=='answer':
            step={**step,'action':'send','intent':'follow_up','text':answer['text'],'faq_question':answer.get('faq_question')}
        elif answer['action']=='defer':
            candidate['pending_question_id']=reply_id;candidate['handled_reply_id']=reply_id
            store.audit(case,'answer_waiting_admin',candidate_id=cid,reply_id=reply_id,reason=answer['reason'])
            return
    if step.get('intent')=='request_resume' and not case.get('execution',{}).get('request_resume',True):return
    revision=candidate['context_revision'];job_revision=case['job']['revision']
    policy_revision=case.get('autonomy',{}).get('revision')
    model_trace=None
    if step['action']=='compose':
        brief=local_brief(case,candidate)
        own=[m['text'] for m in candidate['outbox'] if m.get('approval_actor')=='automation' and m['status']=='sent' and not m.get('decision',{}).get('faq_question')]
        data=await compose_reply(LLM(manager.model_client()),brief,step,own)
        if not case.get('execution',{}).get('request_resume',True) and (data.get('question')=='resume' or '简历' in data['text']):return
        model_input=data.pop('model_input')
        text=data['text'];decision={'labels':step['labels'],'fact_indices':data['fact_indices'],
             'anonymous_input_sha256':trace_hash(model_input),'reply_id':reply_id,'policy':'JD facts; no unverified commitments'}
        model_trace={'decision':decision,'model_input':model_input,'output':data}
    else:
        text=step['text'];decision={'labels':step['labels'],'reason':answer['reason'] if answer['action'] in ('answer','defer') else '已确认流程话术','reply_id':reply_id,'faq_question':step.get('faq_question'),'answer_route':answer}
    if step.get('resume_receipt_ack'):decision.update(resume_receipt_ack=True,resume_receipt_key=closure['key'],resume_receipt_refs=closure['refs'],resume_receipt_hashes=closure['hashes'],resume_closure_context=closing['context'],reason=closing['reason'])
    if step.get('resume_invite_for_action'):decision['resume_invite_for_action']=step['resume_invite_for_action']
    if step.get('nudge'):decision.update(nudge=True,nudge_anchor=nudge_state['anchor'],nudge_number=nudge_state['attempts']+1,nudge_variant=step['nudge_variant'],nudge_correction_hash=nudge_hash)
    if step.get('pending_ack'):decision['pending_ack']=True
    if step.get('program_decision_id'):decision['program_decision_id']=step['program_decision_id']
    if step.get('harness_decision_id'):
        decision['harness_decision_id']=step['harness_decision_id']
        decision['fact_indices']=step.get('fact_indices',[])
    if any(m['status']=='sent' and m.get('decision',{}).get('reply_id')==reply_id and m['text']==text for m in candidate['outbox']):
        candidate['handled_reply_id']=reply_id
        store.audit(case,'reply_already_delivered',candidate_id=cid,reply_id=reply_id,reason='相同回复已有送达证据，不重复发送')
        return
    latest=store.load(case['id']);fresh=store.candidate(latest,cid)
    if not latest.get('autonomy',{}).get('enabled') or latest['autonomy']['revision']!=policy_revision:
        return
    if fresh['context_revision']!=revision or latest['job']['revision']!=job_revision:
        raise SourcingError('生成期间岗位或候选信息变化，本轮未发送')
    if step.get('semantic_state_key'):
        from .conversation_semantics import current
        semantic=current(latest,fresh)
        if not semantic or semantic['state_key']!=step['semantic_state_key']:
            raise SourcingError('发送前语义依据已失效，未发送旧回复')
    if model_trace:
        store.audit(latest,'model_decision',candidate_id=cid,**model_trace)
    if step.get('resume_receipt_ack'):
        store.audit(latest,'resume_closure_composed',candidate_id=cid,reply_id=reply_id,context=closing['context'],reason=closing['reason'],text=text)
    store.audit(latest,'answer_decision',candidate_id=cid,reply_id=reply_id,**answer)
    message=store.draft(latest,cid,step['intent'],text)
    store.authorize_automation(latest,cid,message['id'],decision=decision)
    # Read/think delay, bounded and interruptible. The context is checked again
    # immediately before every send; neither randomness nor a timer authorizes it.
    await manager.checkpoint(job)
    await asyncio.sleep(random.uniform(3,7))
    current=await boss_chat.read_conversation(session,**fresh['conversation'])
    latest=store.load(case['id']);fresh=store.candidate(latest,cid)
    if current['fingerprint']!=snapshot['fingerprint']:
        store._invalidate(fresh);store.save(latest);return
    if step.get('nudge'):
        from .followup import evaluate
        if not evaluate(latest,fresh)['due']:
            store._invalidate(fresh);store.save(latest);return
    store._message(fresh,message['id'])['pre_send_history_ids']=[m['external_id'] for m in snapshot['messages']]
    store.save(latest)
    if job.get('account_runner') and not manager.account_execution.can_send(case['id']):
        store._invalidate(fresh);store.save(latest);return
    store.claim_send(latest,cid,message['id'],run_id=job.get('id'),observed_snapshot=current if step.get('resume_receipt_ack') else None)
    def still_authorized():
        if job.get('account_runner') and not manager.account_execution.can_send(case['id']):raise SourcingError('岗位任务已暂停，未点击发送')
        current=store.load(case['id']);m=store._message(store.candidate(current,cid),message['id'])
        if (not current.get('autonomy',{}).get('enabled') or m['status']!='sending'
                or current['autonomy']['revision']!=m.get('policy_revision')):
            raise SourcingError('发送前自动沟通授权已停止，未点击发送')
    try:
        receipt=await boss_chat.send_once(session,fresh['conversation'],text,current,pre_send=still_authorized)
    except asyncio.CancelledError:
        latest=store.load(case['id']);fresh=store.candidate(latest,cid)
        store._message(fresh,message['id'])['status']='delivery_unknown';fresh['status']='delivery_unknown'
        store.audit(latest,'delivery_unknown',candidate_id=cid,message_id=message['id'],note='执行被中断，不自动重发')
        raise
    except boss_chat.SendAborted as exc:
        # 11-27 10:28 赵清山：点发送前中止，消息一定没发出。记“未发送”而不是回执未知；
        # 删不掉自己打的字时记下来，页面检查暂停时待办能说清这是系统自己的文字。
        latest=store.load(case['id'])
        store.abort_send(latest,cid,message['id'],exc.reason)
        if exc.reason in ('existing_draft','editor_unavailable'):
            # 人工草稿或输入框异常不会自己消失：这位候选人冷却一段时间再决策，免得每轮认领又中止。
            person=store.candidate(latest,cid)
            person['decision_wait_until']=time.time()+jitter(1800);person['decision_wait_revision']=latest.get('config_revision',0)
        store.save(latest)
        note=ABORT_TEXT.get(exc.reason,exc.reason)
        if exc.typed and not exc.cleared:
            conv=fresh.get('conversation') or {}
            manager.db.put('system_draft','boss',{'id':'boss','chat_id':conv.get('chat_id'),'receiver':conv.get('receiver'),'job_label':case['job']['label'],
                'case_id':case['id'],'candidate_id':cid,'text':text,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'at':time.time()})
        from .logbook import log
        log('action',type='send_aborted',case_id=case['id'],candidate_id=cid,message_id=message['id'],reason=exc.reason,typed=exc.typed,cleared=exc.cleared)
        manager.event(job,'step','本次未点击发送：'+note+('；已删除未发送的文字' if exc.cleared else '；输入框里的文字未能删除，任务会在页面检查时暂停' if exc.typed else ''))
        return
    except Exception as exc:
        receipt=None;manager.event(job,'step','本次消息回执未核实：'+type(exc).__name__+('：'+str(exc)[:120] if str(exc) else ''))
    latest=store.load(case['id']);fresh=store.candidate(latest,cid)
    if receipt:
        store.confirm_delivery(latest,cid,message['id'],receipt)
        fresh['handled_reply_id']=reply_id
        if step.get('needs_confirmation'):fresh['pending_question_id']=reply_id
        elif answer['action']=='answer' and not step.get('nudge') and not step.get('resume_receipt_ack'):
            fresh.pop('pending_question_id',None);fresh.pop('planning_answer_ready',None)
        store.save(latest)
    else:
        store._message(fresh,message['id'])['status']='delivery_unknown';fresh['status']='delivery_unknown'
        store.audit(latest,'delivery_unknown',candidate_id=cid,message_id=message['id'],note='不会重试发送')
        manager.event(job,'step','该会话回执待核实，将随收件只读核验；不重发，继续处理其他会话')


def resume_collection_due(case,person):
    if not person or person['status'] in TERMINAL or person.get('handoff') or person.get('resumes') or not person.get('inbox') or not case.get('execution',{}).get('request_resume',True):return False
    if set(intent_labels(person['inbox'][-1]['text']))&{'decline','later'}:return False
    from .followup import evaluate
    if evaluate(case,person)['state']=='exhausted':return False
    if any(m['status'] in ('sending','delivery_unknown') for m in person.get('outbox',[])):return False
    pending_request=next((a for a in reversed(person.get('platform_actions',[])) if a.get('kind')=='ask_resume' and a['status'] in ('claimed','awaiting_verification','pending')),None)
    reply=person['inbox'][-1]
    if pending_request and reply['external_id']!=person.get('handled_reply_id') and 'interest' in intent_labels(reply['text']) and reply.get('observed_at',0)>=pending_request.get('at',0) and not any(m['status'] in ('sending','delivery_unknown') for m in person.get('outbox',[])):return True
    if any(a.get('kind') in ('ask_resume','exchange_phone','exchange_wechat') and a['status']!='obtained' for a in person.get('platform_actions',[])):return False
    return True


# 连续几次翻到列表最底部仍找不到，才认定会话已不在 BOSS 沟通列表里。
MISSING_AFTER = 3


async def recheck_deferred_chat(manager,job,session,case,row):
    """只读复核一条隔离会话：核实同一会话身份与消息列表后解除隔离。

    沟通页没打开任何会话时本来就没有编辑框，不能把“没有编辑框”当成页面异常跳过
    （11-21 起 5 条隔离会话一次都没复核成功）。草稿、登录、平台验证仍然不切换会话。
    每次结果都写 state 日志，失败次数记在 deferred_chat_failures。
    """
    from .logbook import log
    store=manager.cases;chat_id=row['chat_id']
    await manager.checkpoint(job)
    fresh=store.load(case['id'])
    fresh['deferred_chat_check_at']=time.time()+jitter(900)
    store.save(fresh)
    page=boss_chat._selection_page(session)
    guard=await boss_chat._bound_read(page,boss_chat.RECOVERY_GUARD_JS)
    blocked=next((key for key in ('has_draft','login','challenge') if guard.get(key)),None)
    if blocked:
        log('state',type='deferred_chat_check',case_id=case['id'],chat_id=chat_id,result='skipped',reason=blocked)
        return None  # 页面级问题，没有复核
    try:
        bound=bind_chat(fresh,row)
        if bound:
            snap=await boss_chat.open_conversation(session,**bound['conversation'])
            fresh=store.load(case['id'])
            person=bind_chat(fresh,row)
            if person:adopt_platform_identity(store,fresh,person,snap)
        else:
            identity=await boss_chat.inspect_row(session,row,fresh['job']['label'])
            await boss_chat.read_conversation(session,identity['chat_id'],identity['receiver'],identity['job_title'])
    except boss_chat.ChatSelectionUnavailable as exc:
        fresh=store.load(case['id'])
        failures=fresh.setdefault('deferred_chat_failures',{})
        failures[chat_id]=failures.get(chat_id,0)+1
        diagnostics=exc.diagnostics or {}
        log('state',type='deferred_chat_check',case_id=case['id'],chat_id=chat_id,result='failed',
            reason=diagnostics.get('reason'),list_end=diagnostics.get('list_end'),failures=failures[chat_id],header_jobs=diagnostics.get('header_jobs'))
        if diagnostics.get('list_end'):
            # 已翻到列表最底部仍找不到：连续 MISSING_AFTER 次就认定会话已不在 BOSS 沟通列表里，
            # 不再反复滚动查找；它若重新出现在列表里，收件同步会自动恢复。
            gone=fresh.setdefault('deferred_chat_gone',{})
            gone[chat_id]=gone.get(chat_id,0)+1
            if gone[chat_id]>=MISSING_AFTER:
                fresh['deferred_chat_ids'].remove(chat_id)
                fresh['missing_chat_ids']=list(dict.fromkeys([*fresh.get('missing_chat_ids',[]),chat_id]))
                fresh.setdefault('missing_chat_since',{})[chat_id]=time.time()
                for key in ('deferred_chat_gone','deferred_chat_failures','deferred_chat_since'):fresh.get(key,{}).pop(chat_id,None)
                fresh['outreach_isolation_exceptions']=[item for item in fresh.get('outreach_isolation_exceptions',[]) if item!=chat_id]
                store.audit(fresh,'chat_missing_from_list',chat_id=chat_id,reason='多次翻到沟通列表最底部仍找不到这条会话，停止自动查找')
                log('state',type='deferred_chat_check',case_id=case['id'],chat_id=chat_id,result='missing')
                return False
        store.save(fresh)
        return False
    fresh=store.load(case['id'])
    fresh['deferred_chat_ids'].remove(chat_id)
    fresh['outreach_isolation_exceptions']=[item for item in fresh.get('outreach_isolation_exceptions',[]) if item!=chat_id]
    fresh.get('deferred_chat_failures',{}).pop(chat_id,None)
    fresh.get('deferred_chat_since',{}).pop(chat_id,None)
    store.audit(fresh,'deferred_chat_verified',reason='同一会话身份与消息列表已通过只读复核')
    log('state',type='deferred_chat_check',case_id=case['id'],chat_id=chat_id,result='verified')
    record=job.get('selection_recovery',{}).get(case['id'],{})
    if record.get('status')=='deferred' and record.get('last_failed_chat_id')==chat_id:
        record.update(status='recovered',updated_at=time.time(),reason='隔离会话已通过只读复核')
        manager.persist(job)
    return True


def _row_hash(row):
    """会话列表行的摘要哈希：去掉时间字样，只在内容或未读变化时改变。"""
    preview=re.sub(r'\b\d{1,2}:\d{2}\b|刚刚|\d+分钟前','',row.get('text',''))
    return hashlib.sha256(json.dumps([preview,row.get('unread',False)],ensure_ascii=False).encode()).hexdigest()


def withdrawn_case(manager,case_id):
    """该岗位在未停止的任务里已撤下首聊（outreach=withdrawn）。"""
    from .job_members import normalize
    execution=getattr(manager,'account_execution',None)
    if not execution:return False
    for member in execution.members():
        if member.get('status') in ('stopped','completed') or case_id not in member.get('case_ids',[]):continue
        try:cfg=copy.deepcopy(manager.db.get('task',member['id'])['config']);entry=next((m for m in normalize(cfg) if m['case_id']==case_id),None)
        except (FileNotFoundError,KeyError,ValueError):continue
        if entry and entry['outreach']=='withdrawn':return True
    return False


# 列表摘要里我方消息带送达状态标记（候选人消息没有），据此判断最后一条是不是我方发的。
OWN_PREVIEW=re.compile(r'\[(送达|已读|未读)\]')


async def _recheck_withdrawn_chat(manager,job,session,case,row):
    """撤下岗位的放下会话：摘要文字变了、且最后一条是候选人发的，才复核一次（第一次看到时有未读也复核）。
    摘要只看文字不看未读：打开会话会清掉未读，管理员手动回复只改成我方消息，都不算候选人新消息。
    复核仍认不出就记入 withdrawn_unreadable，由待办交给人工；复核通过则照常恢复。"""
    store=manager.cases;chat_id=row['chat_id']
    preview=re.sub(r'\b\d{1,2}:\d{2}\b|刚刚|\d+分钟前','',row.get('text',''))
    row_hash=hashlib.sha256(preview.encode()).hexdigest()
    fresh=store.load(case['id']);previews=fresh.setdefault('deferred_chat_preview',{})
    if previews.get(chat_id)==row_hash:return
    first=chat_id not in previews;old=previews.get(chat_id)
    previews[chat_id]=row_hash;store.save(fresh)
    if OWN_PREVIEW.search(preview) or (first and not row.get('unread')):return
    ok=await recheck_deferred_chat(manager,job,session,store.load(case['id']),row)
    fresh=store.load(case['id'])
    if ok is None:
        # 页面有草稿、掉登录或验证，没真正复核：摘要退回原值，下次再试。
        fresh.setdefault('deferred_chat_preview',{})[chat_id]=old;store.save(fresh);return
    if ok:
        fresh.get('withdrawn_unreadable',{}).pop(chat_id,None);fresh.get('deferred_chat_preview',{}).pop(chat_id,None)
    elif chat_id in fresh.get('deferred_chat_ids',[]):
        person=bind_chat(fresh,row)
        receiver=((person or {}).get('conversation',{}).get('receiver') or row.get('receiver') or '')[:100]
        fresh.setdefault('withdrawn_unreadable',{})[chat_id]={'at':time.time(),'receiver':receiver}
        from .logbook import log
        log('state',type='withdrawn_chat_unreadable',case_id=case['id'],chat_id=chat_id)
    store.save(fresh)


async def sync_and_follow(manager,job,session,case):
    store=manager.cases
    from .selection_recovery import unresolved
    pending_chat_ids=set((unresolved(job,case['id']) or {}).get('failures',[]))
    verified=set()
    from .boss_resume import is_permission_text
    resume_offers=[c.get('conversation',{}).get('chat_id') for c in case['candidates'].values() if _resume_offer_pending(c)]
    rows=await boss_chat.open_menu(session)
    # 待核验的原会话和简历申请一样，可能被虚拟列表滚出可视区。先滚出来再判断缺不缺。
    reveal=[]
    # 隔离会话到了复核时间也要滚出来；滚不出来就永远等不到复核，岗位会一直暂缓新开聊。
    # 已撤下岗位的放下会话不按时复核（11-27 旧 QE：约 7 条旧会话认不出，每约 15 分钟复核一次、次次失败），只在列表摘要变化时复核。
    withdrawn=withdrawn_case(manager,case['id'])
    deferred_due=case.get('deferred_chat_ids',[]) if time.time()>=case.get('deferred_chat_check_at',0) and not withdrawn else []
    for chat_id in [*resume_offers, *pending_chat_ids, *deferred_due]:
        if chat_id and chat_id not in reveal:reveal.append(chat_id)
    rows=await boss_chat.include_chat_rows(session,rows,reveal)
    if not rows and any(p.get('conversation') for p in case['candidates'].values() if not p.get('alias_of')):raise SourcingError('已有绑定会话但沟通列表未就绪，等待恢复；暂不新增开聊')
    def priority(row):
        person=bind_chat(case,row)
        from .resume_closure import pending as closure_pending
        from .boss_contact_consent import needs_sync
        needs=bool(person and (needs_sync(case,person) or closure_pending(person) or resume_collection_due(case,person) or person.get('pending_question_id') or can_revisit_question(case,person) or any(is_permission_text(m.get('text','')) for m in person.get('inbox',[])) and not any(a.get('kind')=='accept_resume' for a in person.get('platform_actions',[]))))
        # 候选人主动申请发简历优先于未读和追聊；原会话核验仍先做，避免点到未核实的会话。
        offer=_row_resume_offer(person,row)
        return (row['chat_id'] not in pending_chat_ids,not offer,not row.get('unread',False),not needs)
    rows=sorted(rows,key=priority)
    deferred_row=None
    for row in rows:
        if row['chat_id'] in case.get('missing_chat_ids',[]):
            # 之前找不到的会话又出现在列表里：恢复正常处理。
            fresh=store.load(case['id'])
            fresh['missing_chat_ids']=[item for item in fresh.get('missing_chat_ids',[]) if item!=row['chat_id']]
            fresh.get('missing_chat_since',{}).pop(row['chat_id'],None)
            store.audit(fresh,'chat_back_in_list',chat_id=row['chat_id'],reason='会话重新出现在沟通列表里，恢复正常处理')
            case=store.load(case['id'])
        if row['chat_id'] in case.get('deferred_chat_ids',[]):
            if withdrawn:
                await _recheck_withdrawn_chat(manager,job,session,case,row)
            elif time.time() >= case.get('deferred_chat_check_at',0):
                await recheck_deferred_chat(manager,job,session,case,row)
            continue
        await manager.checkpoint(job)
        case=store.load(case['id']);c=bind_chat(case,row)
        row_hash=_row_hash(row)
        from .followup import evaluate as nudge_evaluate
        nudge_status=nudge_evaluate(case,c) if c else {}
        nudge_due=bool(c and (nudge_status.get('due') or (nudge_status.get('state')=='exhausted' and c.get('followup_state',{}).get('state')!='exhausted')))
        resume_due=resume_collection_due(case,c)
        from .resume_closure import pending as closure_pending
        receipt_due=bool(c and c.get('resumes') and closure_pending(c))
        from .boss_contact_consent import needs_sync
        contact_pending=needs_sync(case,c)
        pending_ack=bool(c and c.get('pending_question_id') and c.get('inbox') and c['inbox'][-1]['external_id']==c['pending_question_id'] and not any(m['status'] in ('sending','delivery_unknown') or (m.get('decision',{}).get('pending_ack') and m['decision'].get('reply_id')==c['pending_question_id'] and m['status']=='sent') for m in c['outbox']))
        from .resume_archive import archive_pending as needs_archive
        archive_pending=needs_archive(c)
        from .decision_runtime import reply_target
        from .answering import answer_question
        hidden=reply_target(c) if c else None
        hidden_question=bool(hidden and hidden.get('external_id')!=c.get('handled_reply_id') and answer_question(case,hidden.get('text',''),person=c)['action'] in ('answer','defer') and hidden.get('external_id')!=(c.get('inbox') or [{}])[-1].get('external_id'))
        pending_reply=bool(c and c['status'] not in TERMINAL and c.get('inbox') and (next_step(c)['action']!='wait' or can_revisit_question(case,c) or c.get('harness_feedback_pending') or hidden_question) and (not any(m['status'] in ('sending','delivery_unknown') for m in c['outbox']) or any(m.get('verification',{}).get('continuation_actions') for m in c['outbox'])))
        consent_pending=bool(c and any(is_permission_text(m.get('text','')) for m in c.get('inbox',[])) and not any(a.get('kind')=='accept_resume' for a in c.get('platform_actions',[])) and not c.get('resumes'))
        cooling=bool(c and c.get('decision_wait_until',0)>time.time()
                     and c.get('decision_wait_revision')==case.get('config_revision',0))
        # 复查间隔按“候选人+上次同步时刻”取稳定随机值（4～8 分钟），避免整齐的 5 分钟周期。
        delivery_due=bool(c and time.time()-c.get('last_sync_at',0)>=240+240*stable_fraction(c['id'],c.get('last_sync_at',0))
                          and any(m['status']=='delivery_unknown' for m in c['outbox']))
        reasons=[label for label,trigger in (
            ('原会话核验',row['chat_id'] in pending_chat_ids),
            ('新未读',row.get('unread',False) and (not c or c.get('list_preview_hash')!=row_hash)),
            ('建立同步基线',not c or not c.get('list_preview_hash')),
            ('列表摘要变化',c and c.get('list_preview_hash') and c['list_preview_hash']!=row_hash),
            ('追聊到期',nudge_due),('附件待归档',archive_pending),
            ('同意接收简历',_resume_offer_pending(c)),
            ('资料或回执待核验',contact_pending or receipt_due or resume_due or consent_pending or delivery_due),
            ('来信待处理',(pending_reply or pending_ack) and not cooling),
        ) if trigger]
        if not reasons:continue
        try:
            if not c:
                # Import only rows whose text unambiguously includes this job.
                if case['job']['label'] not in row['text']:continue
                identity=await boss_chat.inspect_row(session,row,case['job']['label'])
                case=store.load(case['id'])
                c=import_chat_identity(store,case,identity)
            snap=await boss_chat.open_conversation(session,**c['conversation'])
        except boss_chat.ChatSelectionUnavailable as exc:
            store.audit(case,'conversation_visit',chat_id=row['chat_id'],
                        triggers=reasons,result='identity_unverified')
            exc.chat_id=row['chat_id']
            exc.receiver=(row.get('receiver') or (c or {}).get('conversation',{}).get('receiver') or '')[:100]
            # 登录、验证码、草稿、页面丢失仍立刻停止。一条会话暂时对不上或找不到时，先处理其余已核验会话，最后再把这条交给恢复额度。
            from .selection_recovery import ISOLATABLE_REASONS
            if (exc.diagnostics or {}).get('reason') in ISOLATABLE_REASONS:
                deferred_row=exc
                continue
            raise
        verified.add(row['chat_id'])
        case=store.load(case['id']);c=store.candidate(case,c['id'])
        c=adopt_platform_identity(store,case,c,snap)
        case=store.load(case['id']);c=store.candidate(case,c['id'])
        store.audit(case,'conversation_visit',candidate_id=c['id'],chat_id=row['chat_id'],
                    triggers=reasons,result='verified')
        if any(m['direction']=='unknown' and m['text'] and not m.get('platform_control') for m in snap['messages']):
            from .decision_runtime import hold
            hold(manager,case,c,'平台出现无法核实方向的新消息，请在执行浏览器核对后重新决策；未发送')
            continue
        sync_snapshot(store,case,c,snap)
        from .followup import evaluate as nudge_evaluate
        followup_state=nudge_evaluate(case,c)
        old_followup=c.get('followup_state',{})
        c['followup_state']=followup_state
        if followup_state['state']=='exhausted' and old_followup.get('state')!='exhausted':store.audit(case,'followup_exhausted',candidate_id=c['id'],reason='连续追聊达到上限且仍无回应，停止主动追聊；新来信可恢复沟通')
        else:store.save(case)
        from .resume_archive import capture
        await capture(manager,job,session,case['id'],c['id'],snap)
        case=store.load(case['id']);c=store.candidate(case,c['id'])
        identity={**c['conversation'],'header':snap.get('header','')}
        pending=[p for p in case['candidates'].values() if p['id']!=c['id'] and not p.get('conversation') and
                 any(m['status']=='delivery_unknown' for m in p.get('outbox',[])) and professional_identity_matches(p['profile'],identity)]
        if len(pending)==1:
            reconcile_delivery(store,case,pending[0],snap,c['conversation'])
        if not job['params'].get('read_only'):
            from .decision_runtime import run_conversation
            await run_conversation(manager,job,session,case['id'],c['id'],snap)
        elif job['params'].get('inbound_resume_accept') and _resume_offer_pending(c):
            # 会话已经核验。同意接收是执行边界内的候选人主动沟通，恢复轮次的只读限制不挡住这一步，也不顺带发送其他消息。
            from .boss_resume import accept_resume_request
            await accept_resume_request(manager,job,session,case,c,snap)
            snap=await boss_chat.read_conversation(session,**c['conversation'])
            fresh=store.load(case['id']);sync_snapshot(store,fresh,store.candidate(fresh,c['id']),snap)
            await capture(manager,job,session,case['id'],c['id'],snap)
        latest=store.load(case['id']);fresh=store.candidate(latest,c['id'])
        fresh['list_preview_hash']=row_hash;store.save(latest)
    if deferred_row:
        raise deferred_row
    if any(chat_id and chat_id not in verified for chat_id in resume_offers):
        job.setdefault('inbound_resume_cases',[])
        if case['id'] not in job['inbound_resume_cases']:job['inbound_resume_cases'].append(case['id'])
    missing=pending_chat_ids-verified
    if missing:
        exc=boss_chat.ChatSelectionUnavailable('待恢复会话仍未从当前列表核验',stage='locate',reason='target_missing')
        exc.chat_id=next(iter(missing))
        raise exc
    return len(verified)


def daily_new_target(case):
    """当天实际主动联系上限：等于管理员配置值（11-25 管理员：每天要把首聊额度用完，不再按 75%～100% 打折）。"""
    return max(0,case['autonomy']['daily_new_limit'])


# 每批评估人数上限的随机区间；每批单独抽取，避免固定次数的规律。
FIRST_CONTACT_REVIEWS_PER_BATCH = (3, 8)


async def _set_aside_changed_profile(manager,job,session,case_id,cid,exc):
    """打招呼之前发现资料变了：作废这一位的未发送草稿，冷却一段时间，关掉弹层，结束本批。

    招呼还没点出去，没有写入平台，也不会改联系其他人；任务继续，下一批重新进推荐列表。
    """
    store=manager.cases
    case=store.load(case_id);c=case['candidates'].get(cid)
    name=(c or {}).get('profile',{}).get('display_name','候选人')
    # 还没建档的人（第一次打开资料就出问题）没有候选人记录可写冷却，记在本次会话里，下一批跳过这一位。
    until=time.time()+jitter(1800)
    if not isinstance(getattr(session,'_outreach_set_aside',None),dict):session._outreach_set_aside={}
    session._outreach_set_aside[cid.removeprefix('boss-geek:')]=until
    if c:
        store._invalidate(c)
        c['decision_wait_until']=until
        store.audit(case,'profile_changed_before_contact',candidate_id=cid,reason=str(exc)[:200])
    session._boss_recommend_detail=None
    try:
        await close_profile(session)
    except Exception:
        pass
    manager.event(job,'step',f'{name} 的资料在核对时发生变化，未打招呼；先放下这一位，返回收件检查')
    return False


async def first_contact(manager,job,session,case):
    """开一次首聊。返回 'contacted'（已提交）、'no_candidates'（推荐页已打开，本次没有可联系的卡片：空列表或全部被已联系、筛选排除）、
    'screened_out'（本批至少打开 1 位，且打开的全部被首聊前初筛判为不符合必须项或初筛暂不可用）、
    'views_exhausted'（今天打开资料已到上限，本岗位今天不再审阅推荐）
    或 'blocked'（额度、开关、话术、评估上限、资料变化、模型暂缓等其他原因）。"""
    store=manager.cases
    if not case.get('autonomy',{}).get('enabled'):return 'blocked'
    policy_revision=case['autonomy']['revision']
    task=effective_task(manager.db,case['id']) if getattr(manager,'db',None) is not None else None
    # 寻访任务：账号上限、任务当天目标与岗位权重份额共同决定；无任务（旧命令行）时按岗位自己的上限
    quota_task=task if task and 'execution' in task['config'] else None
    if quota_task:
        if not job_quota.allowed(store,task,case['id'],day_key()):return 'blocked'
    elif daily_attempts(store,'first_contact')>=daily_new_target(case):return 'blocked'

    def views_exhausted():
        # 每账号每天打开资料有上限（账号合计，且按岗位权重分份额），到上限本岗位当天不再审阅推荐。
        noted=job.get('profile_view_cap_noted') if isinstance(job.get('profile_view_cap_noted'),dict) else {}
        today=list(noted.get(day_key()) or [])
        if case['id'] not in today:
            job['profile_view_cap_noted']={day_key():today+[case['id']]}
            log('state',type='profile_view_cap',case_id=case['id'],task_id=quota_task.get('id'),actor='executor',reason='daily_profile_view_cap')
            manager.event(job,'step','今天打开资料已到上限，本岗位今天不再审阅推荐')
        return 'views_exhausted'
    # 打开资料额度与推荐卡片无关：到上限就不再进入推荐页；本次调用内按打开次数递减，不逐张重算。
    views_left=job_quota.views_remaining(store,quota_task,case['id'],day_key()) if quota_task else None
    if views_left is not None and views_left<=0:return views_exhausted()
    # Start every automatic batch with a fresh read. A proof inherited from a
    # previous task may have only seconds left even though it is still valid.
    session._boss_greeting=None
    from .greeting_policy import ensure as ensure_greeting
    if not await ensure_greeting(manager,job,session,case):return 'blocked'
    case=store.load(case['id'])
    pool=await open_list(session,case)
    reviewed=rejected=failed=0;index=0;review_limit=random.randint(*FIRST_CONTACT_REVIEWS_PER_BATCH)
    from . import screening
    from .job_sourcing import must_haves_revision

    async def move_on(case,card):
        """放下当前这一位：停留片刻后关弹层，在当前列表接着看；确认不了就重新进入推荐列表。返回 (pool,index)。"""
        # 像人一样在资料上停留一段右偏分布的随机时长再离开，避免“打开即关闭”的固定节奏。
        await asyncio.sleep(lognormal(7,0.5,2.5,25))
        await manager.checkpoint(job)
        # 优先像真人一样关闭弹层、在当前列表接着往下看，避免每跳过一位都整页重载并重新筛选；
        # 只有确认弹层已关闭且仍是本岗位普通推荐时才继续，否则退回重新进入推荐列表。
        fresh_cards=None
        if await close_profile(session):
            try:
                await verify_list(session,job_identity(case),case['job']['label'])
                fresh_cards=await read_cards(session)
            except SourcingError:
                fresh_cards=None
        if fresh_cards:
            ids=[x['geek_id'] for x in fresh_cards]
            manager.event(job,'step','已关闭资料弹层，在当前推荐列表继续')
            return fresh_cards,ids.index(card['geek_id'])+1 if card['geek_id'] in ids else 0
        manager.event(job,'step','未确认资料弹层已关闭，重新进入推荐列表')
        return await open_list(session,case),0

    def screening_record(**fields):
        """执行器记录上当天各岗位初筛连续失败次数（只保留当天）；成功时更新 last_ok_at 并清零。"""
        day=day_key();daily=job.get('screening_failures');counts=daily.get(day) if isinstance(daily,dict) else None
        counts=counts if isinstance(counts,dict) else {}
        entry=counts.get(case['id']) if isinstance(counts.get(case['id']),dict) else {'n':0}
        entry={**entry,**fields}
        if 'last_failed_at' in fields:entry['n']=int(entry.get('n') or 0)+1
        if 'last_ok_at' in fields:entry['n']=0
        job['screening_failures']={day:{**counts,case['id']:entry}}
        if hasattr(manager,'persist'):manager.persist(job)

    def screened_out():
        return reviewed>0 and rejected+failed==reviewed
    while index<len(pool):
        card=pool[index];index+=1
        await manager.checkpoint(job)
        case=store.load(case['id']);cid='boss-geek:'+card['geek_id']
        c=case['candidates'].get(cid)
        if any(card.get('platform_label','').startswith(h['prefix']) for h in case.get('identity_holds',[]) if h.get('prefix')):continue
        if card.get('contact_label')!='打招呼' or '暂不考虑' in card.get('availability',''):continue
        if c and (c['status'] in TERMINAL or c.get('conversation') or any(m['status'] in ('sending','sent','delivery_unknown') for m in c['outbox'])):continue
        terms=case.get('execution',{}).get('outreach_terms',[])
        if terms and not any(t in (card.get('summary','')+' '+card.get('work_history','')) for t in terms):continue
        # 刚被判定暂不联系或决策暂缓的候选人，冷却期内不再打开详情，直接看下一张卡片。
        if c and max(c.get('decision_wait_until',0),c.get('decision_hold',{}).get('retry_at',0))>time.time():continue
        if (getattr(session,'_outreach_set_aside',None) or {}).get(card['geek_id'],0)>time.time():continue
        # 按当前必须项版本已判为不符合的人不再打开资料；必须项改动后版本变化，会重新初筛。
        must=(case.get('job_sourcing') or {}).get('must_haves') or []
        if must and c and (c.get('screening') or {}).get('revision')==must_haves_revision(must) and c['screening'].get('outcome')=='reject':continue
        # 每批最多评估若干位，避免连续跳过时长时间浏览详情、反复调用模型。
        if reviewed>=review_limit:return 'screened_out' if screened_out() else 'blocked'
        if views_left is not None:
            if views_left<=0:return views_exhausted()
            views_left-=1
        reviewed+=1
        try:
            await open_profile(session,card['geek_id'],job_identity(case),case['job']['label'])
        except ProfileChanged as exc:
            await _set_aside_changed_profile(manager,job,session,case['id'],cid,exc);return 'blocked'
        case=store.load(case['id']);c=case['candidates'].get(cid)
        if not case.get('autonomy',{}).get('enabled') or case['autonomy']['revision']!=policy_revision:return 'blocked'
        if c and c['status'] in TERMINAL:return 'blocked'
        profile={**card,'display_name':c['profile'].get('display_name') if c else '候选人 '+str(len(case['candidates'])+1),
                 'job_id':job_identity(case),'source_channel':'recommend','review_card_sha256':card_fingerprint(card)}
        c=store.add_candidate(case,cid,RECOMMEND_URL,profile)
        previous_review=c.get('profile_reviewed_at');c['profile_reviewed_at']=time.time();store.save(case)
        if any(m['status'] in ('draft','approved') for m in c['outbox']):
            store._invalidate(c)
            store.audit(case,'draft_superseded',candidate_id=cid,reason='新的只读核对替换尚未发送的旧草稿')
        must=(case.get('job_sourcing') or {}).get('must_haves') or []
        if must:
            # 首聊前初筛：用 open_profile 已读到的经历概览，脱敏后逐条对照必须项；任何失败只放下这一位。
            overview=(getattr(session,'_boss_recommend_detail',None) or {}).get('overview') or ''
            name=c['profile'].get('display_name','候选人')
            try:
                result=await screening.screen(manager,case,c,overview)
            except Exception as exc:
                reason=str(exc)[:60] if isinstance(exc,screening.ScreeningUnavailable) else '初筛出错：'+type(exc).__name__
                failed+=1
                if views_left is not None:views_left+=1
                case=store.load(case['id']);c=store.candidate(case,cid)
                c['decision_wait_until']=time.time()+jitter(1800)
                # 初筛没做成不算这一位今天的审阅，模型故障不消耗打开资料额度。
                if previous_review is None:c.pop('profile_reviewed_at',None)
                else:c['profile_reviewed_at']=previous_review
                store.save(case)
                screening_record(last_failed_at=time.time(),reason=reason)
                log('state',type='precontact_screening_unavailable',case_id=case['id'],candidate_id=cid,actor='executor',reason=reason)
                manager.event(job,'step',f'初筛暂不可用（{reason}），先放下这一位')
                pool,index=await move_on(case,card);continue
            screening_record(last_ok_at=time.time())
            case=store.load(case['id']);c=store.candidate(case,cid);c['screening']=result
            if result['outcome']=='reject':
                rejected+=1
                store.audit(case,'precontact_screening',candidate_id=cid,outcome='reject',items=result['items'])
                texts={str(v['id']):v['text'] for v in must}
                miss=next((i for i in result['items'] if i['verdict']=='unmet'),{})
                unmet=texts.get(miss.get('id'),'') or miss.get('evidence','')
                manager.event(job,'step',f'{name} 不符合本岗位必须项'+(f'（{unmet}）' if unmet else '')+'，不开聊，继续查看下一张推荐卡片')
                pool,index=await move_on(case,card);continue
            store.save(case)
        # 11-26 管理员决定“照写不拦”：首聊记录用本任务的问候语；BOSS 实际发出的是该岗位现有招呼语（写入成功时二者一致）。
        from .greeting_policy import render as render_greeting
        text=render_greeting(case['desired_greeting'],case) if case.get('desired_greeting') else case.get('platform_greeting_text')
        if not text:raise SourcingError('任务尚未配置首聊话术')
        from .decision_runtime import initial_contact_decision
        harness_id=await initial_contact_decision(manager,job,case,c)
        # False：仅跳过这一位（模型选择暂不联系）；None：全局条件不满足，结束本批。
        if harness_id is False:
            # 被跳过者的资料弹层仍打开，直接打开下一位会被收件人一致性校验拦截，须先关闭弹层；
            # 被跳过者处于冷却期，重新遍历时会在上方被过滤。
            manager.event(job,'step',c['profile'].get('display_name','候选人')+' 暂不联系，继续查看下一张推荐卡片')
            pool,index=await move_on(case,card)
            continue
        if not harness_id:return 'blocked'
        case=store.load(case['id']);c=store.candidate(case,cid)
        if not case.get('autonomy',{}).get('enabled') or case['autonomy']['revision']!=policy_revision:return 'blocked'
        message=store.draft(case,cid,'first_contact',text)
        decision={'reason':'普通推荐卡片；仅依据本岗位要求与职业关键词相关的职业记录发起了解，不作录用决定',
                  'card_sha256':card_fingerprint(card),'job_revision':case['job']['revision'],'harness_decision_id':harness_id}
        store.authorize_automation(case,cid,message['id'],decision=decision)
        adapter=BossRecommendContact(session,require_greeting_proof=False)
        try:
            proof=await adapter.prepare(case,c,message)
            await adapter.configure(case['job']['label'],text)
            await adapter.candidate(proof['geek_id'],proof['job_id'])
        except ProfileChanged as exc:
            # 还没占用发送，草稿未写入平台。
            await _set_aside_changed_profile(manager,job,session,case['id'],cid,exc);return 'blocked'
        case=store.load(case['id']);store._message(store.candidate(case,cid),message['id'])['pre_send_history_ids']=[]
        store.save(case);store.claim_send(case,cid,message['id'],run_id=job.get('id'))
        try:
            submission=await adapter.contact_once(proof)
        except BaseException:
            current=store.load(case['id']);target=store.candidate(current,cid)
            store._message(target,message['id'])['status']='delivery_unknown';target['status']='delivery_unknown'
            store.audit(current,'delivery_unknown',candidate_id=cid,message_id=message['id'],note='开聊过程被中断，不重试')
            raise
        case=store.load(case['id']);c=store.candidate(case,cid)
        if not submission.get('click_completed'):
            store._message(c,message['id'])['status']='delivery_unknown';c['status']='delivery_unknown'
            store.audit(case,'delivery_unknown',candidate_id=cid,message_id=message['id'],note='开聊点击未正常返回，不重试')
            manager.event(job,'step','首聊点击未正常完成，结束本批并返回收件检查')
            return 'blocked'
        c['harness_memory']=[{'action':'contact','result':'submitted'}]
        store.confirm_delivery(case,cid,message['id'],{'evidence':'目标岗位和候选人已核验，打招呼点击正常返回',
                              'delivery_status':'submitted'},submission_only=True)
        return 'contacted'
    return 'screened_out' if screened_out() else 'no_candidates'


async def run_autopilot(manager,job,session):
    case_id=job['params']['case_id'];cycle=0
    while True:
        await manager.checkpoint(job)
        case=manager.cases.load(case_id)
        from .task_policy import window_status
        schedule=case.get('task_policy',{}).get('schedule')
        if schedule:
            availability=window_status(schedule)
            if not availability['allowed']:
                job.update(autopilot_stage='outside_window',active_case_id=None,next_check_at=availability['next_at'])
                manager.persist(job)
                await asyncio.sleep(1)
                continue
        if not job['params'].get('read_only') and not case.get('autonomy',{}).get('enabled'):
            return {'status':'succeeded','summary':'自动沟通已停止；浏览器和记录已保留。'}
        cycle+=1;job['autopilot_cycle']=cycle;job['autopilot_stage']='sync_history';manager.persist(job)
        job['active_case_id']=case_id
        try:
            count=await sync_and_follow(manager,job,session,case)
            case=manager.cases.load(case_id)
            if job['params'].get('new_contacts') and case.get('execution',{}).get('new_contacts',True) and not job['params'].get('read_only'):
                job['autopilot_stage']='first_contact';manager.persist(job)
                await first_contact(manager,job,session,case)
        except SourcingError:
            current_schedule=manager.cases.load(case_id).get('task_policy',{}).get('schedule')
            if current_schedule and not window_status(current_schedule)['allowed']:continue
            raise
        manager.event(job,'step',f'本轮已检查 {count} 条当前加载会话；没有新回复则等待，不连续追聊。')
        if not job['params'].get('continuous'):
            return {'status':'succeeded','summary':'沟通菜单同步与本轮推进完成；可查看候选人记录和完整审计。'}
        # 轮询间隔以配置值为中位数随机抖动，避免每轮准点回到会话页。
        interval=round(jitter(case.get('autonomy',{}).get('poll_seconds',120)))
        job.update(autopilot_stage='waiting_next_cycle',next_check_at=time.time()+interval)
        manager.persist(job)
        # Short chunks let pause/stop take effect without waiting for a full poll.
        for _ in range(interval):
            await asyncio.sleep(1);await manager.checkpoint(job)
            if not manager.cases.load(case_id).get('autonomy',{}).get('enabled'):
                return {'status':'succeeded','summary':'自动沟通已停止；窗口及会话记录已保留。'}


_run_single = run_autopilot


async def check_page_health(session):
    page=getattr(session,'page',None)
    if page is None:return None
    from . import page_health
    kind=page_health.kind_of(getattr(page,'url',''))
    if not kind:return None
    try:return await page_health.heal(session,kind)
    except asyncio.CancelledError:raise
    except Exception as exc:
        from .logbook import log
        log('state',type='page_health',kind=kind,step='check_failed',reason=type(exc).__name__)
        return None


async def _chat_guard(session):
    """自愈后再用恢复核验的同一份只读检查确认沟通页：没有未发送的草稿、没有登录或验证、会话列表或编辑器读得到。返回问题原因或 None。"""
    page=getattr(session,'page',None)
    if not boss_chat.is_chat_page(page):return 'page_changed'
    try:state=await boss_chat._settled_guard_state(page)
    except asyncio.CancelledError:raise
    except Exception:return 'editor_unavailable'
    if not isinstance(state,dict):return 'editor_unavailable'
    if state.get('login'):return 'login_required'
    if state.get('challenge'):return 'verification_required'
    if state.get('has_draft'):return 'draft_present'
    # 与 recover_chat_selection 一致：没读到草稿字段、或列表和编辑器都没有，视为无法核验编辑器。
    if 'has_draft' not in state or (not state.get('editor_count') and not state.get('chat_rows')):return 'editor_unavailable'
    return None


def _page_retry_delay(attempts):
    from .selection_recovery import PAGE_RETRY_SECONDS,PAGE_RETRY_MAX,PAGE_BACKOFF_SECONDS,PAGE_BACKOFF_MAX,PAGE_FAST_ATTEMPTS
    if attempts<PAGE_FAST_ATTEMPTS:return min(jitter(PAGE_RETRY_SECONDS),PAGE_RETRY_MAX)
    return min(jitter(PAGE_BACKOFF_SECONDS),PAGE_BACKOFF_MAX)


async def heal_paused_tasks_safely(manager,job,session):
    """执行器每轮都会调自动恢复：这里的任何意外只记日志，不能让执行器整个停掉（取消照常向上传）。"""
    try:await heal_paused_tasks(manager,job,session)
    except asyncio.CancelledError:raise
    except Exception as exc:log('state',type='page_recovery',step='error',run_id=(job or {}).get('id'),error_type=type(exc).__name__,error=str(exc)[:200])


async def heal_paused_tasks(manager,job,session):
    """11-24 20:40 沟通页卡在启动页：系统因页面打不开暂停的任务，按间隔自愈；页面确认安全后由系统恢复任务。

    待机和运行中的安全点都会调用。只在执行时段内、暂停前页面检查确实卡住时操作；先查刷新修不好的恢复前置条件；
    次数有上限，用完即停并交给管理员。整个自愈、复核和恢复期间持有浏览器诊断锁。恢复后原会话由 resume_verification 只读复核，不发送任何消息。"""
    from .selection_recovery import page_blocked,page_stuck,page_resumes_today,PAGE_PROBLEMS,PAGE_CAPS,PAGE_MAX_ATTEMPTS,PAGE_DAILY_ATTEMPTS,PAGE_DAILY_RESUMES
    from .task_policy import window_status
    from . import page_health
    account=manager.account_execution;db=getattr(account,'db',None);now=time.time()
    members=[m for m in account.members() if page_blocked(m)]
    if db is None:return None
    try:doc=db.get('page_recovery','boss')
    except FileNotFoundError:doc={}
    daily={'day':doc.get('day'),'day_attempts':doc.get('day_attempts',0)}
    if not members:
        if doc.get('task_ids'):db.put('page_recovery','boss',{'id':'boss','task_ids':[],'updated_at':now,**daily})
        return None
    ids=sorted(m['id'] for m in members);paused_at=max(m['history'][-1]['at'] for m in members)
    def save(**fields):
        doc.update(updated_at=time.time(),**fields);db.put('page_recovery','boss',doc)
    def announce(result,message):
        if doc.get('announced')==result:return
        save(announced=result);manager.event(job,'step',message)
    if doc.get('task_ids')!=ids or doc.get('paused_at')!=paused_at:
        try:health=db.get('page_health','boss')
        except FileNotFoundError:health=None
        # 刚暂停时页面自愈刚用尽，先隔一个间隔再试；记下暂停前页面检查的结论和已做过的步骤。
        doc={'id':'boss','task_ids':ids,'paused_at':paused_at,'attempts':0,'next_at':paused_at+_page_retry_delay(0),
             'page_stuck':page_stuck(health,paused_at),'steps':list((health or {}).get('steps') or []),**daily}
        save()
    if not doc.get('page_stuck') or doc.get('result')=='capped':return None
    def window(member,at):
        try:schedule=db.get('task',member['id'])['config'].get('schedule')
        except (FileNotFoundError,KeyError):schedule=None
        return window_status(schedule,at) if schedule else {'allowed':True,'next_at':None}
    states={m['id']:window(m,now) for m in members}
    ready=[m for m in members if states[m['id']]['allowed']]
    if not ready:
        until=min((s['next_at'] for s in states.values() if s.get('next_at')),default=None)
        if doc.get('outside_window_until')!=until:save(outside_window_until=until)
        return None
    if doc.get('outside_window_until'):save(outside_window_until=None)
    if now<doc.get('next_at',0):return None
    # 刷新页面修不好的前置条件（发布验收、环境核验等）会让恢复被拒绝：不去碰页面，交给管理员。
    blocker=getattr(manager,'resume_blocker',None)
    blockers={m['id']:blocker(m['id']) if blocker else None for m in ready}
    healable=[m for m in ready if not blockers[m['id']]]
    if not healable:
        reason=next(iter(blockers.values()))
        save(result='precondition',reason=reason,next_at=now+_page_retry_delay(doc.get('attempts',0)))
        log('state',type='page_recovery',step='precondition',task_ids=ids,reason=reason)
        announce('precondition','沟通页暂停期间未自动检查：任务恢复会被拒绝（'+str(reason)+'），页面恢复后也无法自动恢复任务；请管理员处理后手动恢复')
        return None
    from .autonomy import day_key
    day=day_key(now)
    if doc.get('day')!=day:doc.update(day=day,day_attempts=0)
    healable=[m for m in healable if page_resumes_today(m,day)<PAGE_DAILY_RESUMES]
    cap=('daily_resumes' if not healable else 'incident' if doc.get('attempts',0)>=PAGE_MAX_ATTEMPTS
         else 'daily_attempts' if doc.get('day_attempts',0)>=PAGE_DAILY_ATTEMPTS else None)
    if cap:
        save(result='capped',cap=cap,reason=PAGE_CAPS[cap])
        log('state',type='page_recovery',step='capped',task_ids=ids,cap=cap,attempts=doc.get('attempts'))
        announce('capped','沟通页自动恢复已停止自动重试：'+PAGE_CAPS[cap]+'；任务保持暂停，请管理员检查执行浏览器后手动恢复')
        return None
    # 环境检查或执行器诊断正在用这个浏览器：这次不碰页面。自己开始后整个过程持有同一把锁。
    lock=getattr(manager,'_browser_diagnostic_lock',None)
    if lock is None:lock=manager._browser_diagnostic_lock=asyncio.Lock()
    preflight=getattr(manager,'preflight_task',None)
    if lock.locked() or (preflight is not None and not preflight.done()):return None
    # 运行中的执行器每轮也会自愈页面：它刚用尽（冷却中）就先不再跑一遍，等冷却过了再试，避免短时间内反复刷新。
    if time.time()-(getattr(session,'_page_health_exhausted_at',None) or 0)<page_health.COOLDOWN_SECONDS:return None
    async with lock:
        save(attempts=doc.get('attempts',0)+1,day_attempts=doc.get('day_attempts',0)+1,last_at=now)
        log('state',type='page_recovery',step='attempt',attempt=doc['attempts'],task_ids=ids)
        try:state=await page_health.heal(session,'chat',force=True)
        except asyncio.CancelledError:raise
        except Exception as exc:state={'healthy':False,'reason':'heal_failed','needs_admin':False,'has_draft':False,'error':type(exc).__name__}
        problem=None if state.get('healthy') and not state.get('has_draft') else state.get('reason') or 'unhealthy'
        if state.get('has_draft'):problem='draft_present'
        if problem is None:problem=await _chat_guard(session)
        now=time.time()
        if problem:
            result='needs_admin' if problem in PAGE_PROBLEMS else 'unhealthy'
            save(result=result,reason=problem,next_at=now+_page_retry_delay(doc['attempts']))
            log('state',type='page_recovery',step='still_blocked',attempt=doc['attempts'],reason=problem,task_ids=ids,next_at=doc['next_at'])
            announce(result,'暂停期间检查沟通页：'+PAGE_PROBLEMS[problem]+'；处理前系统不自动恢复任务，请管理员在执行浏览器处理；处理后下次检查页面正常会自动恢复，也可手动恢复' if result=='needs_admin'
                     else '沟通页仍打不开，任务保持暂停；执行时段内按间隔自动再试（最多 '+str(PAGE_MAX_ATTEMPTS)+' 次），页面恢复且确认没有草稿后自动恢复任务')
            return state
        resumed=[];failed={}
        # 自愈期间管理员可能已手动恢复或暂停、也可能走出了执行时段：恢复前逐项重新核对。
        still={m['id'] for m in account.members() if page_blocked(m)}
        for member in healable:
            if member['id'] not in still or not window(member,time.time())['allowed']:continue
            reason=blocker(member['id']) if blocker else None
            if reason:failed[member['id']]=reason;continue
            try:
                # 与管理员恢复走同一入口（发布版本、环境核验等前置检查不变），只是记为系统恢复。
                await manager.start_account_task(member['id'],actor='system',reason='page_recovered')
                resumed.append(member['id'])
            except asyncio.CancelledError:raise
            except Exception as exc:failed[member['id']]=str(exc)[:200] if isinstance(exc,ValueError) else type(exc).__name__
        retrying=doc['attempts']<PAGE_MAX_ATTEMPTS
        save(result='resume_failed' if failed else 'resumed' if resumed else 'unhealthy',reason='；'.join(failed.values()) or None,next_at=now+_page_retry_delay(doc['attempts']))
        log('state',type='page_recovery',step='resumed',attempt=doc['attempts'],task_ids=resumed,failed=failed,healed_by=state.get('healed_by'))
        if resumed:manager.event(job,'step','BOSS 沟通页已恢复正常，系统已自动恢复任务；原会话先只读核验，未发送任何消息')
        if failed:
            announce('resume_failed','沟通页已恢复正常，但任务未能自动恢复（'+'；'.join(failed.values())+'）；'
                     +('系统会在下次检查时再试（已试 '+str(doc['attempts'])+'/'+str(PAGE_MAX_ATTEMPTS)+' 次），也可在任务页手动恢复' if retrying else '已停止自动重试，请管理员在任务页手动恢复'))
        return state


def _outreach_ready(manager,job,cid,held,due):
    """该岗位本轮能否开首聊批次：未被放下、可发送、无隔离会话、话术核验已到期、自动化与开聊开关打开。只影响本轮，不记作当天无人可联系。"""
    if cid in held or not manager.account_execution.can_send(cid):return False
    case=manager.cases.load(cid)
    blockers=job.setdefault('outreach_blockers',{})
    from .selection_recovery import outreach_blockers
    if outreach_blockers(case):
        reason='岗位有未核实的隔离会话（隔离不足 2 小时），暂缓新增开聊'
        if blockers.get(cid)!=reason:
            blockers[cid]=reason
            manager.persist(job)
            manager.event(job,'step',reason)
        return False
    blockers.pop(cid,None)
    from .greeting_policy import key as greeting_key
    try:readiness=(manager.db if hasattr(manager,'db') else manager.account_execution.db).get('greeting_readiness',greeting_key(case.get('task_policy',{}).get('task_id','legacy'),cid))
    except FileNotFoundError:readiness={}
    if readiness and (readiness.get('next_check_at')==0 or readiness.get('task_revision')!=case.get('task_policy',{}).get('revision')):due.pop(cid,None)
    if due.get(cid,0)>time.time():return False
    return bool(case.get('autonomy',{}).get('enabled')) and bool(case.get('execution',{}).get('new_contacts',True))


def _pick_outreach_case(manager,member,ready,drained,day):
    """选该任务本轮开首聊批次的岗位：只在参与首聊（未撤下）的岗位中按当天额度与权重选；账号满 20 或任务达到当天目标时不选。
    没有每日总额设置的旧任务按成员顺序取第一个可开聊的岗位，额度由 first_contact 入口按岗位自己的上限复核。"""
    cfg=copy.deepcopy(manager.definitions.get(member['id'])['config'])
    try:weights={cid:w for cid,w in job_members.weights(cfg).items() if cid in member['case_ids']}
    except ValueError:
        if 'execution' in cfg:return None
        weights={cid:1 for cid in member['case_ids']}
    waiting={cid for cid in weights if cid not in drained and not ready(cid)};skip=waiting|set(drained)
    if 'execution' not in cfg:return next((cid for cid in weights if cid not in skip),None)
    store=manager.cases
    if job_quota.account_exhausted(store,day):return None
    usage=job_quota.usage_by_case(store,job_quota.counted_case_ids(cfg,day),day);limit=cfg['execution'].get('daily_new_limit',0)
    cid=job_quota.pick_case(member['id'],limit,weights,usage,skip,day)
    if cid is None:return None
    # 只有推荐页当天没人可联系的岗位让出额度；暂时不能开聊（放下、隔离、话术核验未到期等）的岗位还有份额时，不把它的额度给已用完份额的岗位。
    shares=job_quota.allocate(job_quota.task_target(member['id'],limit,day),weights)
    if usage.get(cid,0)>=shares[cid] and any(usage.get(k,0)<shares[k] for k in waiting):return None
    return cid


async def run_account_executor(manager,job,session):
    """Inbox first for each account job; outreach is a separate due queue."""
    from .boss_contact import ObservationUnavailable
    from .selection_recovery import failure as selection_failure, recovered as selection_recovered, unresolved, resume_verification, preserve_scene
    from .browser_errands import at_safe_point as run_browser_errands
    due={};sync_due={};held={};read_failures={};cycle=0
    for cid,record in job.get('selection_recovery',{}).items():
        if record['status'] not in ('recovered','deferred'):
            sync_due[cid]=record.get('next_check_at') or 0
            held[cid]={'status':'held','summary':record['reason'],'at':record['updated_at']}
    while True:
        await manager.checkpoint(job)
        # 每轮开始尚未持有任何岗位数据，是写入待办答案（岗位常见问题）的安全点，避免整份岗位数据互相覆盖。
        from .faq_answers import apply_pending as apply_faq_answers
        if apply_faq_answers(manager):manager.event(job,'step','管理员确认的岗位问答已加入常见问题，等待答复的候选人将按新事实重新理解')
        # 同一安全点应用岗位增量变更（加入、撤下、权重、总额）；恢复核验轮次只读，不在此处。
        from .job_changes import apply_pending as apply_job_changes,describe as describe_job_change
        for change in apply_job_changes(manager,job['params']):manager.event(job,'step','岗位变更已在安全点生效：'+describe_job_change(manager,change))
        for member in manager.account_execution.members():
            if member['status']!='running':continue
            definition=manager.definitions.get(member['id'])
            goal=definition['config'].get('target_resumes',1)
            people=[c for cid in member['case_ids'] for c in manager.cases.load(cid)['candidates'].values() if not c.get('alias_of')]
            if definition['config'].get('sourcing_mode','continuous')=='quota' and sum(bool(c.get('resumes')) for c in people)>=goal:
                manager.account_execution.set_task(member['id'],member['case_ids'],'completed',reason='resume_target_reached')
                manager.event(job,'step','岗位任务已达到简历目标（联系方式非必需），停止该任务新增动作')
        resume_verification(manager,job)
        ids=manager.account_execution.case_ids()
        if not ids:
            if any(r['id']!=job['id'] and r.get('status')=='queued' for r in getattr(manager,'jobs',{}).values()):
                job['release_requested']=True
                manager.event(job,'step','全部任务已暂停，自动释放席位给排队任务；本地资料保留')
                return {'status':'succeeded','summary':'已释放待机浏览器席位，排队任务接续；任务暂停状态保留'}
            idle_stage='selection_blocked' if any(r['status']=='blocked' for r in job.get('selection_recovery',{}).values()) else 'standby'
            if job.get('autopilot_stage')!=idle_stage:
                job.update(autopilot_stage=idle_stage,active_case_id=None,next_check_at=None)
                manager.event(job,'step','所有任务已暂停或结束，执行器待机；保留浏览器，不操作网站')
                manager.persist(job)
            # 管理员登记的浏览器代办（读取、绑定 BOSS 岗位）：执行时段内在此安全点做一条，做完回沟通页。
            await run_browser_errands(manager,job,session)
            # 只有“系统因页面打不开而暂停”的任务会在执行时段内定时自愈，其他暂停不碰网站。
            await heal_paused_tasks_safely(manager,job,session)
            await asyncio.sleep(1)
            continue
        available=[cid for cid in ids if manager.account_execution.can_send(cid)]
        if not available:
            states=[manager.account_execution.availability(cid) for cid in ids]
            next_at=min((s['next_at'] for s in states if s and s.get('next_at')),default=None)
            stage=('release_changed' if any(s and s.get('state')=='release_changed' for s in states) else
                   'job_not_validated' if any(s and s.get('state')=='job_not_validated' for s in states) else
                   'resting' if any(s and s.get('state')=='resting' for s in states) else 'outside_window' if any(s and s.get('state')=='outside_window' for s in states) else 'waiting_policy')
            if job.get('autopilot_stage')!=stage:
                job.update(autopilot_stage=stage,active_case_id=None,next_check_at=next_at)
                # 版本变化不会自己好：必须明确告诉管理员，不能和“等待执行时段”混在一起。
                manager.event(job,'step','发布版本已变化，任务暂停发送；请暂停并恢复任务以绑定新版本' if stage=='release_changed'
                              else '部分岗位验收已失效，这些岗位暂停发送，其他岗位继续' if stage=='job_not_validated'
                              else '任务等待允许的执行时段或规划完成；保留登录资料，不操作网站')
                manager.persist(job)
            await asyncio.sleep(1)
            continue
        cycle+=1;job['autopilot_cycle']=cycle
        job['case_results']=held.copy();job['inbound_resume_cases']=[]
        # 安全点（未持有任何会话）：做一条浏览器代办（读取、绑定 BOSS 岗位），做完回沟通页再做页面检查；页面级问题照常停止。
        await run_browser_errands(manager,job,session)
        # 每轮开始前对当前 BOSS 页做一次只读健康检查；卡在加载等情况先自愈，掉登录、验证交给后续读取按原流程处理。
        await check_page_health(session)
        # 安全点：其他任务在运行时，系统因页面打不开而暂停的任务同样按间隔自愈、恢复（规则与待机时相同）。
        await heal_paused_tasks_safely(manager,job,session)
        # Paused memberships perform no browser operations, including reads.
        for cid in ids:
            await manager.checkpoint(job)
            if not manager.account_execution.can_send(cid):continue
            if sync_due.get(cid,0)>time.time():continue
            member=next((m for m in manager.account_execution.members() if m['status']=='running' and cid in m['case_ids']),None)
            if member:manager.account_execution.begin_work(member['id'])
            case=manager.cases.load(cid);job['active_case_id']=cid
            enabled=manager.account_execution.can_send(cid) and case.get('autonomy',{}).get('enabled')
            job['params']['read_only']=not enabled
            job['params'].pop('inbound_resume_accept',None)
            try:
                recovery=unresolved(job,cid)
                if recovery:
                    job.update(autopilot_stage='selection_retry',next_check_at=None)
                    manager.persist(job)
                    repair=await boss_chat.recover_chat_selection(session)
                    if repair['status']=='blocked':
                        record=selection_failure(manager,job,case,block_reason=repair['reason'])
                        await preserve_scene(manager,job,record,session)
                        held[cid]=job['case_results'][cid]
                        continue
                    # Recovery of the menu is not proof of the target conversation.
                    # Only a complete verified pass below clears this incident.
                    await manager.checkpoint(job)
                    if not manager.account_execution.can_send(cid):continue
                job['autopilot_stage']='sync_history';manager.persist(job)
                # 恢复核验轮次只读；候选人主动申请发简历并已核验会话时，同意接收仍在执行边界内。
                with recovery_pass(job['params'],bool(recovery)):
                    count=await sync_and_follow(manager,job,session,case)
                if recovery and count<1:
                    raise boss_chat.ChatSelectionUnavailable('恢复后仍缺少实际会话核验证据',stage='recover_verify',reason='target_unverified')
                selection_recovered(manager,job,cid)
                job['params']['read_only']=not enabled
                sync_due[cid]=time.time()+jitter(20 if cid in job.get('inbound_resume_cases',[]) else case.get('autonomy',{}).get('poll_seconds',120))
                held.pop(cid,None)
                read_failures.pop(cid,None)
                job.get('read_recovery',{}).pop(cid,None)
                if hasattr(manager,'verify_account_plan'):
                    for member in manager.account_execution.members():
                        if cid in member['case_ids']:manager.verify_account_plan(member['id'],cid,job['id'])
                job['case_results'][cid]={'status':'running' if enabled else 'paused','loaded_conversations':count,'at':time.time()}
            except boss_chat.ChatSelectionUnavailable as exc:
                if not manager.account_execution.can_send(cid):continue
                record=selection_failure(manager,job,case,exc)
                await preserve_scene(manager,job,record,session)
                held[cid]=job['case_results'][cid]
                sync_due[cid]=record.get('next_check_at') or 0
            except ObservationUnavailable as exc:
                attempts=read_failures.get(cid,0)+1
                read_failures[cid]=attempts
                retry_at=time.time()+jitter(case.get('autonomy',{}).get('poll_seconds',120))
                sync_due[cid]=retry_at
                info={'status':'held','summary':str(exc),'attempts':attempts,'max_attempts':3,
                      'next_check_at':retry_at if attempts<3 else None,'at':time.time()}
                held[cid]=job['case_results'][cid]=info
                job.setdefault('read_recovery',{})[cid]=info.copy()
                manager.persist(job)
                if attempts>=3:
                    raise SourcingError('沟通页面连续 3 次读取超时，已停止本轮执行。请查看执行浏览器并处理任务异常待办后重新启动；不会重复发送已有消息。') from exc
                manager.event(job,'step',f'沟通页面读取暂未完成（{attempts}/3），按任务收件间隔等待后重读；期间不新增开聊')
            except SourcingError as exc:
                if not manager.account_execution.can_send(cid):
                    job['case_results'][cid]={'status':'waiting','summary':'到达任务执行边界，等待下一允许时段','at':time.time()}
                    continue
                if any(t in str(exc) for t in ('登录','校验','过于频繁','账号存在异常')):raise
                if unresolved(job,cid):
                    # A menu reload followed by an unverifiable target is still
                    # the same failed recovery, not a fresh unlimited loop.
                    failure=boss_chat.ChatSelectionUnavailable('恢复后仍未核验目标会话',stage='recover_verify',reason='target_unverified',exception_type=type(exc).__name__)
                    record=selection_failure(manager,job,case,failure)
                    await preserve_scene(manager,job,record,session)
                    held[cid]=job['case_results'][cid]
                    sync_due[cid]=record.get('next_check_at') or 0
                    continue
                job['case_results'][cid]={'status':'held','summary':str(exc),'at':time.time()}
                held[cid]=job['case_results'][cid]
                sync_due[cid]=time.time()+jitter(case.get('autonomy',{}).get('poll_seconds',120))
                manager.event(job,'step','岗位读取待下轮核验：'+str(exc))
        job['params']['read_only']=False
        # 还有没打开的简历接收申请时，先回到收件，本轮不新增开聊。
        if job.get('inbound_resume_cases'):
            manager.event(job,'step','有候选人正在申请发送简历，先完成同意接收，本轮不新增开聊')
        elif job['params'].get('new_contacts'):
            # 每个运行中的任务每轮至多开一批：按当天额度与岗位权重选岗位；推荐页今天没人可联系的岗位当天让出额度，次日恢复。
            day=day_key();drained=list((job.get('outreach_exhausted') or {}).get(day) or [])
            job['outreach_exhausted']={day:drained} if drained else {}
            for member in manager.account_execution.members():
                if not job['params'].get('new_contacts'):break
                if member['status']!='running':continue
                cid=_pick_outreach_case(manager,member,lambda c:_outreach_ready(manager,job,c,held,due),drained,day)
                if cid is None:continue
                case=manager.cases.load(cid)
                job['active_case_id']=cid;job['autopilot_stage']='first_contact'
                from .greeting_policy import GreetingUnavailable,block as block_greeting
                from .boss_recommend import GreetingVerificationError
                confirmed=0;outcome=None
                # 每批提交次数在 1～3 次间随机，避免每轮固定连续打招呼 3 次。
                contact_attempts=random.randint(1,3)
                manager.event(job,'step',f'开始主动开聊批次，最多提交 {contact_attempts} 次')
                try:
                    for _ in range(contact_attempts):
                        if not job['params'].get('new_contacts') or not manager.account_execution.can_send(cid):
                            break
                        await manager.checkpoint(job)
                        outcome=await first_contact(manager,job,session,manager.cases.load(cid))
                        if outcome!='contacted':
                            break
                        confirmed+=1
                except (GreetingUnavailable,GreetingVerificationError) as exc:
                    block_greeting(manager,job,case,str(exc));due[cid]=time.time()+jitter(600)
                except SourcingError as exc:
                    if manager.account_execution.can_send(cid):
                        # 登录、平台验证、频繁、账号异常是页面级问题，仍停止任务等人处理；
                        # 其他开聊入口问题（推荐页没加载出来、控件找不到）只暂缓开聊，收件和回复照常。
                        if any(t in str(exc) for t in ('登录','校验','验证','过于频繁','账号存在异常')):raise
                        due[cid]=time.time()+jitter(900)
                        from .logbook import log
                        log('state',type='outreach_deferred',case_id=cid,reason=str(exc)[:200])
                        # 当天该岗位开聊入口失败次数（attention 累计到 3 次生成待办）；只保留当天，次日清零。
                        daily=job.get('outreach_deferrals');counts=daily.get(day) if isinstance(daily,dict) else None
                        counts=counts if isinstance(counts,dict) else {}
                        job['outreach_deferrals']={day:{**counts,cid:counts.get(cid,0)+1}};manager.persist(job)
                        manager.event(job,'step','主动开聊入口暂不可用（'+str(exc)[:60]+'），先继续处理收件，约 15 分钟后重试开聊')
                finally:
                    manager.event(job,'step',f'主动开聊批次结束，已提交 {confirmed} 次，返回收件检查')
                    if confirmed:
                        for inbox_cid in available:
                            if inbox_cid not in held:sync_due[inbox_cid]=0
                if outcome=='no_candidates' and not confirmed:
                    drained.append(cid);job['outreach_exhausted']={day:drained};manager.persist(job)
                    from .logbook import log
                    log('state',type='outreach_exhausted',case_id=cid,task_id=member['id'],actor='executor',reason='no_candidates')
                    manager.event(job,'step','该岗位今天推荐页暂无可联系的人，额度让给其他岗位')
                if outcome=='views_exhausted':
                    # 今天打开资料已到上限（first_contact 已发事件）：本岗位当天不再进入推荐页，剩余首聊额度让给其他岗位，次日恢复。
                    drained.append(cid);job['outreach_exhausted']={day:drained};manager.persist(job)
                    from .logbook import log
                    log('state',type='outreach_exhausted',case_id=cid,task_id=member['id'],actor='executor',reason='profile_view_cap')
                if outcome=='screened_out':
                    # 本批打开的人都不符合必须项或初筛暂不可用：先让其他岗位开聊，约 30 分钟后再看；不算今天没人可联系，不让出当天额度。
                    due[cid]=max(due.get(cid,0),time.time()+jitter(1800))
                    from .logbook import log
                    log('state',type='outreach_screened_out',case_id=cid,task_id=member['id'],actor='executor',reason='all_reviewed_rejected_or_unscreened')
                    manager.event(job,'step','该岗位本批审阅的人不符合必须项或初筛暂不可用，先让其他岗位开聊，约 30 分钟后再看')
                due.setdefault(cid,0)
                due[cid]=max(due[cid],time.time()+jitter(30))
        for member in manager.account_execution.members():
            if member['status']=='running':manager.account_execution.finish_cycle(member['id'])
        active_ids=manager.account_execution.case_ids()
        selecting=any(unresolved(job,cid) for cid in active_ids)
        blocked=not active_ids and any(r['status']=='blocked' for r in job.get('selection_recovery',{}).values())
        recovering=bool(available) and all(cid in read_failures for cid in available)
        # 账号执行器的空闲间隔随机抖动，避免每 30 秒整齐醒来一次。
        idle=round(jitter(30))
        next_at=None if blocked else min((sync_due[cid] for cid in available if cid in sync_due),default=time.time()+idle) if recovering or selecting else time.time()+idle
        job.update(active_case_id=None,autopilot_stage='selection_blocked' if blocked else 'selection_retry' if selecting else 'read_retry' if recovering else 'waiting_next_cycle',next_check_at=next_at)
        manager.persist(job)
        for _ in range(idle):
            await asyncio.sleep(1);await manager.checkpoint(job)


async def run_autopilot(manager, job, session):
    if job.get('account_runner'):return await run_account_executor(manager,job,session)
    ids=job['params'].get('case_ids')
    if not ids:return await _run_single(manager,job,session)
    original=dict(job['params']);due={cid:0 for cid in ids};held=set()
    job['case_results']={}
    while True:
        runnable=0
        for cid in ids:
            if cid in held:continue
            case=manager.cases.load(cid)
            if not original.get('read_only') and not case.get('autonomy',{}).get('enabled'):continue
            runnable+=1
            if due[cid]>time.time():continue
            job['active_case_id']=cid
            job['params']={**original,'case_id':cid,'continuous':False}
            try:
                result=await _run_single(manager,job,session)
                job['case_results'][cid]={**result,'at':time.time(),'config_revision':case.get('config_revision',0)}
            except SourcingError as exc:
                job['case_results'][cid]={'status':'held','summary':str(exc),'at':time.time()};held.add(cid)
                manager.cases.audit(manager.cases.load(cid),'batch_case_held',reason=str(exc),run_id=job['id'])
                if any(t in str(exc) for t in ('登录','校验','过于频繁','账号存在异常')):raise
            finally:
                job['params']=original;manager.persist(job)
            due[cid]=time.time()+jitter(manager.cases.load(cid).get('autonomy',{}).get('poll_seconds',120))
        if not original.get('continuous') or not runnable or len(held)==len(ids):
            return {'status':'succeeded' if not held else 'failed','summary':f'已处理 {len(ids)} 个岗位；{len(held)} 个岗位待处理异常，详情见分岗位结果。'}
        job.update(autopilot_stage='waiting_next_cycle',next_check_at=min(due[cid] for cid in ids if cid not in held))
        manager.persist(job)
        await asyncio.sleep(1);await manager.checkpoint(job)
