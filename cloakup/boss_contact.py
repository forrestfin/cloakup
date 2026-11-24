"""Deterministic first-contact preparation and dispatch; no model calls.

All interactions are derived from observed recruiter controls. Preparation only
reads pages. Dispatch requires a separately approved CaseStore message. It never
retries a possibly successful contact click.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

from .boss import validate_job_url
from .locate import locate
from .sourcing import SourcingError

GREETING_URL = 'https://www.zhipin.com/web/chat/set/greeting'
SEARCH_URL = 'https://www.zhipin.com/web/chat/search'
GREETING_FRAME = '/web/frame/info/set/greeting'


class ObservationUnavailable(SourcingError):
    """A page snapshot timed out; no browser action is retried by this error."""


def valid_message(text):
    from .communication_policy import check_compensation_disclosure
    check_compensation_disclosure(text)
    # The actual form displays a 100-character counter (HTML maxLength is 200).
    # Use a conservative 100-character cap; never truncate an approved message.
    if not isinstance(text, str) or not text.strip() or len(text) > 100:
        raise SourcingError('BOSS 首次开聊话术需为 1–100 字；请先修改草稿后重新确认')
    return text


async def _observation(session):
    try:
        obs = await session.observe(False, text_limit=18000)
    except TimeoutError as exc:
        # Keep this boundary around the read only. Click/send failures must not
        # become recoverable reads: their durable send claims still govern them.
        raise ObservationUnavailable('沟通页面读取超时，等待下轮重新读取；本次未继续执行后续动作') from exc
    if urlparse(obs.url).path.startswith('/web/user/') and any(
        e.tag == 'button' and e.label == '登录/注册' for e in obs.elements
    ):
        raise SourcingError('BOSS 已显示登录表单，请在专用窗口重新登录后继续；未执行联系')
    return obs


async def _find(session, label, *, tag=None, frame=None):
    for _ in range(5):
        obs = await _observation(session)
        target = {'label': label}
        if tag: target['tag'] = tag
        if frame: target['frame_url_contains'] = frame
        match, _ = locate(target, obs)
        # A fuzzy label match is inappropriate at a sending boundary.
        if match and match.element.label == label:
            return match.element, obs
        await asyncio.sleep(.5)
    raise SourcingError('未找到唯一的实际页面控件：' + label)


async def _click(session, label, **kwargs):
    el, _ = await _find(session, label, **kwargs)
    await session.click(el.index)


async def _main_button(session, label, attempts=5):
    for _ in range(attempts):
        obs = await _observation(session)
        matches = [e for e in obs.elements if e.tag == 'button' and e.label == label
                   and not e.raw.get('frame_url') and not e.raw.get('disabled')]
        if len(matches) == 1:
            return matches[0]
        await asyncio.sleep(.5)
    raise SourcingError('未找到唯一的目标资料按钮：' + label)


async def _frame_value(session, frame_fragment, expression):
    frame = next((f for f in session.page.frames if frame_fragment in f.url), None)
    if frame is None:
        raise ObservationUnavailable('岗位内嵌页面尚未加载；未读取或修改')
    from .dom_reader import read_frame_dom
    try:return await read_frame_dom(session.page,frame,expression)
    except (TimeoutError,RuntimeError) as exc:
        reason=str(exc) if str(exc) in ('frame_changed','frame_not_unique','frame_read_unverified') else type(exc).__name__
        raise ObservationUnavailable('岗位内嵌页面读取未完成（'+reason+'）；未继续保存或联系') from exc


class BossFirstContact:
    def __init__(self, session):
        self.s = session

    async def greeting_editor(self, title):
        await self.s.navigate(GREETING_URL)
        await _click(self.s, '按职位设置招呼语', frame=GREETING_FRAME)
        obs = await _observation(self.s)
        if title in obs.raw.get('text', ''):
            raise SourcingError('当前岗位已存在单独话术。为避免覆盖，请先核实原设置；本次没有修改。')
        enabled = await _frame_value(self.s, GREETING_FRAME,
            "!!document.querySelector('.greeting-header .ui-switch.ui-switch-checked')")
        if not enabled:
            raise SourcingError('平台自定义打招呼语尚未启用；本次没有切换账号设置')
        await _click(self.s, '添加打招呼语', frame=GREETING_FRAME)
        textarea, obs = await _find(self.s, '请输入打招呼语', tag='textarea', frame=GREETING_FRAME)
        if not any(e.label == title for e in obs.elements):
            raise SourcingError('话术表单中未找到绑定岗位')
        # Just opening the form is read-only. No row is selected or saved here.
        return {'title': title, 'limit': min(100, textarea.raw.get('max_length', 100))}

    async def candidate(self, expected_id, job_id):
        await self.s.navigate(SEARCH_URL)
        found = None
        for _ in range(5):
            obs = await _observation(self.s)
            for el in obs.elements:
                if el.tag != 'a' or '/web/frame/search/' not in el.raw.get('frame_url', ''):
                    continue
                owner = self.s._observed_frames.get(el.raw.get('frame_key'))
                if owner is None: continue
                expr = ('(()=>{const e=document.querySelector(' + json.dumps(el.css) + ');'
                        'return e?{expect:e.getAttribute("data-expect"),job:e.getAttribute("data-jid")}:null})()')
                ids = await self.s.page._stealth_world._human.worlds.evaluate(owner._impl_obj, expr)
                if ids and ids.get('expect') == expected_id:
                    if ids.get('job') != job_id:
                        raise SourcingError('候选卡片绑定岗位与本任务不一致，已停止')
                    found = el
                    break
            if found: break
            await asyncio.sleep(.7)
        if found is None:
            raise SourcingError('当前可见搜索结果中未找到目标候选人；本次不会联系其他人')
        receiver = found.label.split()[0]
        try:
            await self.s.click(found.index)
        except Exception:
            # Navigation can detach the locator's iframe after a successful
            # detail click. Read the result; never repeat a contact action.
            pass
        _, obs = await _find(self.s, '联系Ta', tag='button')
        return {'expect_id': expected_id, 'job_id': job_id, 'receiver_label': receiver,
                'page_url': obs.url}

    async def prepare(self, case, candidate, message):
        valid_message(message['text'])
        job_id = parse_qs(urlparse(validate_job_url(case['job']['url'])).query)['encryptId'][0]
        expected_id = candidate['profile'].get('expect_id')
        if not expected_id or candidate['profile'].get('job_id') != job_id:
            raise SourcingError('缺少可核验的候选人及岗位绑定，不能准备发送')
        await self.greeting_editor(case['job']['label'])
        proof = await self.candidate(expected_id, job_id)
        return {**proof, 'title': case['job']['label'],
                'text_sha256': hashlib.sha256(message['text'].encode()).hexdigest(),
                'scope': '仅当前岗位新增话术，并仅联系已核验的候选人一次'}

    async def configure(self, title, text):
        valid_message(text)
        await self.greeting_editor(title)
        await _click(self.s, title, frame=GREETING_FRAME)
        el, _ = await _find(self.s, '请输入打招呼语', tag='textarea', frame=GREETING_FRAME)
        await self.s.type_text(el.index, text, clear=True, submit=False)
        el, _ = await _find(self.s, '请输入打招呼语', tag='textarea', frame=GREETING_FRAME)
        if el.raw.get('value') != text:
            raise SourcingError('页面正文与已确认草稿不同，未保存、未发送')
        selected = await _frame_value(self.s, GREETING_FRAME,
            "Array.from(document.querySelectorAll('.dialog-wrap .radio-group label')).filter(e=>e.querySelector('input[type=radio]')?.checked).map(e=>e.innerText)")
        if len(selected) != 1 or title not in selected[0]:
            raise SourcingError('话术关联岗位未正确选中，未保存、未发送')
        await _click(self.s, '保存', tag='button', frame=GREETING_FRAME)
        for _ in range(6):
            obs = await _observation(self.s)
            dialogs = [e for e in obs.elements if e.tag == 'textarea' and GREETING_FRAME in e.raw.get('frame_url','')]
            if not dialogs and title in obs.raw.get('text','') and text in obs.raw.get('text',''):
                return {'job_title': title, 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
            await asyncio.sleep(.7)
        raise SourcingError('话术保存结果尚未核实；不会继续联系，请先核对平台设置')

    async def contact_once(self, proof):
        # Called only after CaseStore.claim_send has persisted its sending state.
        if proof.get('source_channel') == 'recommend':
            el = await _main_button(self.s, '打招呼')
        else:
            el, _ = await _find(self.s, '联系Ta', tag='button')
        try:
            await self.s.click(el.index)
        except Exception as exc:
            # A failed click may already have reached the site. Keep the reason
            # for diagnosis without turning it into permission to click again.
            return {'click_completed': False, 'error': f'{type(exc).__name__}: {str(exc)[:600]}'}
        return {'click_completed': True}

    async def receipt(self, proof, text):
        for _ in range(8):
            for frame in self.s.page.frames:
                # A profile, quota dialog, or greeting preview is not a sent
                # message, even when it contains both the name and draft text.
                if urlparse(frame.url).path != '/web/chat/index':
                    continue
                try:
                    visible = await self.s.page._stealth_world._human.worlds.evaluate(frame._impl_obj,
                        "document.body ? document.body.innerText : ''")
                except Exception:
                    continue
                if text in visible and proof['receiver_label'] in visible and not any(w in visible for w in ('发送失败','正在发送','重新发送','网络异常')):
                    return {'evidence': '联系目标由页面期望标识核验；联系后会话页面同时出现对应收件人与已确认正文',
                            'url': frame.url, 'at': time.time(), 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
            await asyncio.sleep(1)
        return None


async def run_first_contact(manager, job, session):
    """Prepare, ask for exact human approval, then perform a single dispatch."""
    params = job['params']
    store = manager.cases
    case = store.load(params['case_id'])
    candidate = store.candidate(case, params['candidate_id'])
    shown = store.preview(case, candidate['id'], params['message_id'])
    if shown['intent'] != 'first_contact':
        raise SourcingError('首次联系入口不能用于后续消息')
    if candidate['profile'].get('source_channel') == 'recommend':
        from .boss_recommend import BossRecommendContact
        adapter = BossRecommendContact(session)
    else:
        adapter = BossFirstContact(session)
    manager.event(job, 'step', '只读核对岗位话术入口和候选人标识')
    while True:
        try:
            proof = await adapter.prepare(case, candidate, shown)
            break
        except SourcingError as exc:
            if not job.get('preserve_preflight') or shown['status'] != 'draft':
                raise
            job['recommend_stage']='preflight_issue'
            job['preflight_diagnostics']=getattr(session,'last_greeting_check',None)
            manager.persist(job)
            answer=await manager.question(job,'reply',f'只读核对未完成，浏览器已保留，未联系候选人。{exc}')
            if answer!='retry':
                return {'status':'cancelled','summary':'已保留资料和草稿，本次未联系候选人。'}
            latest=store.load(case['id'])
            fresh=store.preview(latest,candidate['id'],shown['id'])
            if fresh['status']!='draft' or fresh['fingerprint']!=shown['fingerprint']:
                raise SourcingError('核对期间资料或草稿已变化，请重新审阅；未进行联系')
    if job.get('preserve_preflight'):
        job['recommend_stage']='contact'
    job['contact_review'] = {**proof, 'text': shown['text'], 'candidate_label': candidate['profile'].get('display_name', '候选人')}
    manager.persist(job)
    automatic=case.get('send_policy')=='autonomous' and case.get('autonomy',{}).get('enabled')
    if shown['status'] != 'approved' and not automatic:
        effect = ('确认后，沿用已逐字核实的本岗位现有首聊话术，仅向已核验的这位候选人发起一次普通推荐开聊。不会覆盖岗位话术。'
                  if proof.get('greeting_action') == 'reuse' else
                  '确认后，将为此岗位单独保存以上首聊话术，并仅向已核验的这位候选人发起一次联系。此岗位后续首聊也会使用该话术；账号通用话术不变。')
        prompt = (f"请审核本次操作：岗位「{case['job']['label']}」，候选人「{candidate['profile'].get('display_name','候选人')}」"
                  f"（平台显示 {proof['receiver_label']}，{candidate['profile'].get('availability','意愿待核实')}）。\n\n"
                  f"拟发送正文（{len(shown['text'])}字）：\n{shown['text']}\n\n"
                  f"{effect}拒绝则不保存、不联系。")
        confirmed = await manager.question(job, 'confirm', prompt)
        if not confirmed:
            return {'status': 'cancelled', 'summary': '顾问未确认，本次未保存话术、未联系候选人。'}
    # The exact draft displayed before the wait must still be current afterwards.
    case = store.load(case['id'])
    candidate = store.candidate(case, candidate['id'])
    fresh = store.preview(case, candidate['id'], shown['id'])
    if fresh['fingerprint'] != shown['fingerprint']:
        raise SourcingError('审核期间草稿、岗位或会话已变化，未进行平台写入')
    if fresh['status'] == 'draft':
        if automatic:
            store.authorize_automation(case,candidate['id'],shown['id'],decision={
                'reason':'用户已授权岗位自动首聊；收件人、岗位与正文已在本轮核对',
                'proof':proof})
        else:
            store.approve(case, candidate['id'], shown['id'], shown['fingerprint'])
    elif fresh['status'] != 'approved':
        raise SourcingError('消息已失效或已处理，本次不重复发送')
    manager.event(job, 'step', '正在按岗位授权核对首聊话术' if automatic else '顾问已确认，正在核对本岗位的首聊话术')
    config = await adapter.configure(case['job']['label'], shown['text'])
    job['platform_config'] = config
    manager.persist(job)
    proof = await adapter.candidate(proof['expect_id'], proof['job_id'])
    case = store.load(case['id'])
    candidate = store.candidate(case, candidate['id'])
    store.claim_send(case, candidate['id'], shown['id'])
    manager.event(job, 'step', '准备仅发起一次联系，随后核对会话结果')
    try:
        job['contact_attempt'] = await adapter.contact_once(proof)
        manager.persist(job)
        receipt = await adapter.receipt(proof, shown['text'])
    except asyncio.CancelledError:
        store._message(candidate, shown['id'])['status'] = 'delivery_unknown'
        candidate['status'] = 'delivery_unknown'
        store.save(case)
        raise
    except Exception:
        receipt = None
    if not receipt:
        # Preserve the actual post-click page locally before the browser closes.
        # In particular, quota dialogs and extra confirmation screens must not
        # disappear without evidence. This observation never goes to a model.
        try:
            await session.observe(True, text_limit=18000)
            manager.event(job, 'step', '发送回执未核实，已在本机留存当前页面；不会再次点击联系')
        except Exception:
            manager.event(job, 'step', '发送回执未核实，当前页面留存失败；不会再次点击联系')
        message = store._message(candidate, shown['id'])
        message['status'] = 'delivery_unknown'
        candidate['status'] = 'delivery_unknown'
        store.save(case)
        return {'status': 'failed', 'summary': '已尝试发起一次联系，但会话回执尚未核实。请人工检查；系统不会自动重发。'}
    store.confirm_delivery(case, candidate['id'], shown['id'], receipt)
    return {'status': 'succeeded', 'summary': '已仅向目标候选人发起联系，并在会话页面核验对应收件人与确认正文。等待候选人回复。',
            'receipt': receipt}
