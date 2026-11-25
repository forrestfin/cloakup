"""Read ordinary recommendations and open a verified profile; no model calls."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlparse

from .boss import validate_job_url
from .boss_contact import BossFirstContact, GREETING_URL, GREETING_FRAME, _click, _main_button, _frame_value, _observation, valid_message
from .logbook import log
from .pacing import jitter, lognormal
from .sourcing import SourcingError

RECOMMEND_URL = 'https://www.zhipin.com/web/chat/recommend'
RECOMMEND_FRAME = '/web/frame/recommend/'
ALLOWED_FILTERS = {'本科', '硕士', '博士', '5-10年', '10年以上'}
GREETING_TTL = 300
# 11-25 只读诊断：岗位下拉在推荐页 frame 的 .job-selecter-wrap，选项 li.job-item 的 value 即 encryptJobId（docs/design/boss-job-pages.md）
SWITCH_LABEL = '.job-selecter-wrap .ui-dropmenu-label'
JOB_ID = re.compile(r'[A-Za-z0-9_~-]{6,80}')
CARD_IDS = "Array.from(document.querySelectorAll('.candidate-card-wrap .card-inner[data-geekid]')).map(e=>e.getAttribute('data-geekid'))"

class GreetingVerificationError(SourcingError):pass


class ProfileChanged(SourcingError):
    """这一位候选人的资料在两次打开之间变了、不在列表里或已不可开聊。

    只属于这一位：还没点打招呼，放下这一位即可，不能让整个任务暂停。
    """


def job_identity(case):
    return parse_qs(urlparse(validate_job_url(case['job']['url'])).query)['encryptId'][0]


def checked_filters(values):
    if not isinstance(values, list) or any(not isinstance(v, str) or v not in ALLOWED_FILTERS for v in values) or len(set(values)) != len(values):
        raise SourcingError('普通推荐仅支持已核实的学历、工作经验条件')
    return values


async def verify_list(session, job_id, title):
    frame = next((f for f in session.page.frames if RECOMMEND_FRAME in f.url), None)
    if frame is None:
        raise SourcingError('普通推荐页面尚未加载，未执行联系')
    q = parse_qs(urlparse(frame.url).query, keep_blank_values=True)
    if q.get('jobid') != [job_id] or q.get('status') != ['0']:
        raise SourcingError('推荐页面岗位或通道不符；只允许当前岗位的普通推荐')
    text = await _frame_value(session, RECOMMEND_FRAME, "document.body.innerText")
    if title not in text:
        raise SourcingError('推荐页面未显示绑定岗位，已停止')
    return frame.url


async def _frame_click(session, frame, css):
    from .frame_actions import click
    await click(session, frame, css)


def _recommend_job(session):
    frame = next((f for f in session.page.frames if RECOMMEND_FRAME in f.url), None)
    return frame, parse_qs(urlparse(frame.url).query).get('jobid', [None])[0] if frame else None


async def switch_job(session, job_id, title):
    """推荐页切到本岗位：已是本岗位不点；否则展开岗位下拉，按 value=encryptJobId 点唯一选项，等 frame 的 jobid 变成目标。

    失败只抛开聊入口问题（不含登录/验证等页面级字样），上层只暂缓该岗位首聊，任务不暂停。"""
    frame, current = _recommend_job(session)
    if frame is not None and current == job_id:
        return False
    def failed(reason):
        log('state', type='recommend_switch_failed', job_id=job_id, reason=reason)
        return SourcingError('推荐页面未能切换到当前岗位（' + reason + '），已暂缓该岗位开聊')
    if frame is None:raise failed('推荐页面尚未加载')
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):raise failed('岗位标识格式不符')
    option = '.job-selecter-wrap li.job-item[value="' + job_id + '"]'
    try:
        # 切换前记下当前卡片：frame URL 可能先变、列表后刷新，不能拿旧岗位的推荐去联系
        before = await _frame_value(session, RECOMMEND_FRAME, CARD_IDS)
        await _frame_click(session, frame, SWITCH_LABEL)
        await asyncio.sleep(jitter(1.0, .2))
        count = await _frame_value(session, RECOMMEND_FRAME, 'document.querySelectorAll(' + json.dumps(option) + ').length')
        if count != 1:raise failed('岗位下拉里没有该岗位' if not count else '岗位下拉选项不唯一')
        frame, _ = _recommend_job(session)
        if frame is None:raise failed('推荐页面在展开岗位下拉后消失')
        await _frame_click(session, frame, option)
    except SourcingError as exc:
        if str(exc).startswith('推荐页面未能切换到当前岗位'):raise
        raise failed(str(exc)[:60]) from exc
    except (RuntimeError, TimeoutError) as exc:
        # frame 重载中读取或点击（frame_changed 等）也只算这个岗位的开聊入口问题
        raise failed(str(exc)[:60] or type(exc).__name__) from exc
    # 等 frame 到目标岗位且卡片列表换过；总等待有上限（约 15 秒）
    for _ in range(20):
        await asyncio.sleep(jitter(.75, .2))
        _, current = _recommend_job(session)
        if current != job_id:continue
        try:after = await _frame_value(session, RECOMMEND_FRAME, CARD_IDS)
        except (SourcingError, RuntimeError, TimeoutError):continue
        if not before or after != before:
            log('action', type='recommend_job_switched', job_id=job_id, actor='executor')
            return True
    raise failed('切换后页面仍不是该岗位' if current != job_id else '切换后推荐列表未刷新')


async def open_list(session, case):
    checked_filters(case.get('recommend_filters', []))
    # Larger local observations let semantic targets include the whole filter
    # menu. No observation from these pages is forwarded to an external model.
    session.max_elements = max(session.max_elements, 700)
    greeting=case.get('platform_greeting_text')
    if greeting and not current_greeting(session,case['job']['label'],greeting,job_identity(case)):
        proof=await existing_greeting(session,case['job']['label'],greeting)
        session._boss_greeting={**proof,'job_id':job_identity(case)}
    session._boss_recommend_detail = None
    session._boss_recommend_list = None
    await session.navigate(RECOMMEND_URL)
    # 推荐页偶尔卡在“加载中，请稍候”（11-24 13:35）：先做页面健康检查，逐级自愈（刷新 → 重置标签页）。
    # 仍不可用时报开聊入口问题，上层只暂缓开聊；掉登录、平台验证仍按页面级问题停止。
    from . import page_health
    state = await page_health.heal(session, 'recommend')
    if state.get('reason') == 'login_required':
        raise SourcingError('BOSS 已显示登录表单，请在专用窗口重新登录后继续；未执行联系')
    if state.get('reason') == 'verification_required':
        raise SourcingError('平台出现校验，自动沟通已停止')
    if not state.get('healthy'):
        raise SourcingError('推荐牛人页多次刷新和重置后仍未正常加载（' + str(state.get('cause') or state.get('reason')) + '）')
    await _click(session, '推荐', tag='li', frame=RECOMMEND_FRAME)
    # 多岗位任务：推荐页默认停在上次的岗位，先切到本岗位再核对（Task 8）
    await switch_job(session, job_identity(case), case['job']['label'])
    await verify_list(session, job_identity(case), case['job']['label'])
    filters = case.get('recommend_filters', [])
    if filters:
        await _click(session, '筛选', tag='div', frame=RECOMMEND_FRAME)
        # Only toggle an option when its actual DOM state is not selected.
        for label in filters:
            options = await _frame_value(session, RECOMMEND_FRAME,
                'Array.from(document.querySelectorAll(".option")).filter(e=>e.innerText.trim()===' + json.dumps(label) + ').map(e=>e.classList.contains("active"))')
            if len(options) != 1:
                raise SourcingError('岗位筛选项不唯一：' + label)
            if not options[0]:
                await _click(session, label, tag='div', frame=RECOMMEND_FRAME)
        selected = await _frame_value(session, RECOMMEND_FRAME,
            'Array.from(document.querySelectorAll(".option.active")).map(e=>e.innerText.trim())')
        # Availability/pay groups also use .option and default to "不限".
        # They must remain unlimited; compare the professional options alone.
        professional = [v for v in selected if v in ALLOWED_FILTERS]
        unexpected = [v for v in selected if v not in ALLOWED_FILTERS and v != '不限']
        if sorted(professional) != sorted(filters) or unexpected:
            await session.observe(True, text_limit=18000)
            raise SourcingError('页面筛选条件与本岗位参数不一致：' + json.dumps(selected, ensure_ascii=False))
        await _click(session, '确定', tag='div', frame=RECOMMEND_FRAME)
    await verify_list(session, job_identity(case), case['job']['label'])
    cards=await read_cards(session)
    session._boss_recommend_list={'page':session.page,'job_id':job_identity(case),'filters':list(filters)}
    return cards


async def read_cards(session):
    expr = r'''Array.from(document.querySelectorAll('.candidate-card-wrap .card-inner[data-geekid]')).map(e=>{
      const card=e.closest('.candidate-card-wrap');
      const base=Array.from(e.querySelectorAll('.base-info > span')).map(x=>x.innerText.trim());
      return {geek_id:e.getAttribute('data-geekid'), platform_label:e.querySelector('.name')?.innerText.trim(),
        experience:base.find(x=>/年/.test(x)&&!/岁|毕业|应届/.test(x))||'',
        education:base.find(x=>/^(本科|硕士|博士|大专|高中|中专.*|初中.*)$/.test(x))||'',
        availability:base.find(x=>/^(在职|离职)/.test(x))||'当前未展示',
        summary:e.querySelector('.geek-desc .content')?.innerText.trim()||'',
        expectation:e.querySelector('.expect-wrap .content')?.innerText.trim()||'',
        work_history:e.querySelector('.work-exps')?.innerText.trim()||'',
        contact_label:card.querySelector('.button-list button')?.innerText.trim()||''};
    })'''
    for _ in range(6):
        cards = await _frame_value(session, RECOMMEND_FRAME, expr)
        if cards:
            ids = [c['geek_id'] for c in cards]
            if any(not v for v in ids) or len(set(ids)) != len(ids):
                raise SourcingError('推荐卡片标识缺失或重复，不能用于联系')
            return cards
        await asyncio.sleep(.6)
    raise SourcingError('当前普通推荐未展示候选资料，请检查平台页面')


async def open_profile(session, geek_id, job_id, title, *, reload_case=None):
    cached = getattr(session, '_boss_recommend_detail', None)
    listing=getattr(session,'_boss_recommend_list',None)
    same_list=bool(listing and listing['page'] is session.page and listing['job_id']==job_id
                   and reload_case is not None and listing['filters']==reload_case.get('recommend_filters',[]))
    if reload_case is not None and not same_list and not (cached and cached['page'] is session.page and cached['geek_id'] == geek_id):
        await open_list(session, reload_case)
        cached = None
    await verify_list(session, job_id, title)
    cards = await read_cards(session)
    found = [c for c in cards if c['geek_id'] == geek_id]
    if len(found) != 1:
        raise ProfileChanged('当前普通推荐中未找到已核验候选人；不会改联系其他人')
    card = found[0]
    if card['contact_label'] != '打招呼':
        raise ProfileChanged('该候选人已有沟通或不可开聊；请先核对原会话')
    if cached:
        if cached['page'] is not session.page or cached['geek_id'] != geek_id:
            raise ProfileChanged('当前打开的资料与审核收件人不同')
        overview = await profile_overview(session)
        if overview != cached['overview']:
            raise ProfileChanged('当前资料弹层已变化，需重新核对收件人')
        await _profile_button(session, geek_id)
        return {**card, 'job_id':job_id, 'receiver_label':card['platform_label'],
                'page_url':session.page.url, 'source_channel':'recommend'}
    # Match the displayed name AND the data-geekid ancestor, never a list index.
    obs = await _observation(session)
    matches=[]
    for el in obs.elements:
        if el.tag != 'span' or el.label != card['platform_label'] or RECOMMEND_FRAME not in el.raw.get('frame_url',''):
            continue
        owner=session._observed_frames.get(el.raw.get('frame_key'))
        if owner is None: continue
        expr='document.querySelector('+json.dumps(el.css)+')?.closest("[data-geekid]")?.getAttribute("data-geekid")'
        actual=await session.page._stealth_world._human.worlds.evaluate(owner._impl_obj,expr)
        if actual==geek_id: matches.append(el)
    if len(matches)!=1:
        raise SourcingError('未找到唯一的目标资料入口，未执行联系')
    await session.click(matches[0].index)
    button=await _profile_button(session,geek_id)
    # The resume dialog contact button is in the main page; list buttons remain
    # in the iframe. An ambiguous or missing dialog must stop before approval.
    if button.raw.get('frame_url'):
        raise SourcingError('未核实目标资料弹层，未执行联系')
    session._boss_recommend_detail = {'page':session.page, 'geek_id':geek_id,
                                     'overview':await profile_overview(session)}
    return {**card, 'job_id':job_id, 'receiver_label':card['platform_label'],
            'page_url':session.page.url, 'source_channel':'recommend'}


async def _profile_button(session,geek_id):
    """资料弹层里唯一可用的“打招呼”按钮。

    11-24 下午同一位的弹层三次都没出现唯一的“打招呼”，整批开聊被一再暂缓。还没点击任何联系控件，
    这只属于这一位：记下看到了哪些按钮，按资料变化放下这一位，其他候选人照常开聊。
    """
    try:
        return await _main_button(session,'打招呼',attempts=8)
    except SourcingError as exc:
        try:
            obs=await _observation(session)
            seen=[e.label for e in obs.elements if e.tag=='button' and not e.raw.get('frame_url')
                  and any(w in (e.label or '') for w in ('招呼','沟通','聊'))][:8]
        except Exception:
            seen=None
        log('state',type='profile_button_missing',geek_id=geek_id,buttons=seen)
        raise ProfileChanged('资料弹层未出现唯一可用的“打招呼”按钮（看到：'+('、'.join(seen or []) or '无')+'），先放下这一位') from exc


async def profile_overview(session):
    text=await session.page._stealth_world.evaluate("document.body.innerText")
    if '经历概览' not in text:
        raise SourcingError('未核实实际资料弹层的经历概览')
    return text.split('经历概览',1)[1].strip()


async def close_profile(session):
    """像人一样按 Esc 关闭资料弹层并留在当前推荐列表；只有确认弹层已消失才返回 True。

    以“经历概览”是否仍在主页面作为弹层存在的依据（与 profile_overview 一致）。
    未能确认关闭时返回 False，由调用方改为重新进入推荐列表，不在不确定的页面上继续操作。
    """
    page=session.page
    await session.press_key('Escape')
    for _ in range(2):
        text=await page._stealth_world.evaluate("document.body.innerText")
        if '经历概览' not in text:
            session._boss_recommend_detail=None
            return True
        await asyncio.sleep(lognormal(0.8,0.4,0.4,2.0))
    return False


def card_fingerprint(card):
    return hashlib.sha256(json.dumps({k:card.get(k) for k in
        ('geek_id','platform_label','experience','education','availability','summary','expectation','work_history')},
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def current_greeting(session,title,text,job_id):
    proof=getattr(session,'_boss_greeting',None)
    if (proof and proof.get('job_title')==title and proof.get('job_id')==job_id
        and proof.get('text_sha256')==hashlib.sha256(text.encode()).hexdigest()
        and 0 <= time.monotonic()-proof.get('checked_monotonic',0) <= GREETING_TTL):
        return proof
    return None


async def existing_greeting(session,title,text):
    valid_message(text)
    # This platform can keep login in the original tab while a new tab is
    # redirected to login. Read settings in the original authenticated tab.
    enabled=False;rows=[]
    session._boss_recommend_detail=None
    session._boss_recommend_list=None
    try:
        await session.navigate(GREETING_URL)
        await _click(session,'按职位设置招呼语',frame=GREETING_FRAME)
        for attempt in range(6):
            try:
                enabled=await _frame_value(session,GREETING_FRAME,
                    "!!document.querySelector('.greeting-header .ui-switch.ui-switch-checked')")
                rows=await _frame_value(session,GREETING_FRAME,
                    "Array.from(document.querySelectorAll('.job-content')).map(e=>({title:e.querySelector('.job-name span')?.innerText.trim(),text:e.querySelector('.job-greeting')?.innerText.trim()}))")
            except SourcingError:
                if attempt==5:raise
                await _observation(session);await asyncio.sleep(.5);continue
            matches=[r for r in rows if r.get('title')==title]
            if enabled and len(matches)==1 and matches[0].get('text')==text:break
            if attempt<5:
                await _observation(session);await asyncio.sleep(.5)
    finally:
        session.last_greeting_check={
            'page_path':urlparse(getattr(session.page,'url','')).path,
            'frame_paths':[urlparse(f.url).path for f in getattr(session.page,'frames',[])],
            'enabled':enabled,'matching_rows':len([r for r in rows if r.get('title')==title])}
    matches=[r for r in rows if r.get('title')==title]
    if not enabled:raise GreetingVerificationError('现有岗位话术的启用状态未核实；本次不会覆盖或开聊')
    if len(matches)!=1:raise GreetingVerificationError('未找到唯一的本岗位已保存话术；本次不会覆盖或开聊')
    if matches[0].get('text')!=text:raise GreetingVerificationError('现有岗位话术与待审核正文不同；本次不会覆盖或开聊')
    return {'job_title':title,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'action':'reused','checked_at':time.time(),'checked_monotonic':time.monotonic()}


class BossRecommendContact(BossFirstContact):
    async def prepare(self,case,candidate,message):
        self.case=case
        self.expected_fingerprint=candidate['profile'].get('review_card_sha256')
        if not self.expected_fingerprint:
            raise SourcingError('缺少已审阅的普通推荐资料快照')
        if case.get('platform_greeting_text')!=message['text']:
            raise GreetingVerificationError('当前草稿不是本岗位已配置的话术；请先重新核对设置')
        proof=await self.candidate(candidate['profile']['geek_id'],job_identity(case))
        greeting=current_greeting(self.s,case['job']['label'],message['text'],job_identity(case))
        if not greeting:raise GreetingVerificationError('岗位话术核对已过期，请重新预览；未执行联系')
        return {**proof,'title':case['job']['label'],'greeting_action':'reuse',
                'text_sha256':hashlib.sha256(message['text'].encode()).hexdigest(),
                'greeting_checked_at':greeting['checked_at'],
                'scope':'复用已核实的岗位话术，只向当前候选人普通推荐开聊一次'}

    async def configure(self,title,text):
        proof=current_greeting(self.s,title,text,job_identity(self.case))
        if not proof:raise GreetingVerificationError('岗位话术核对已超过五分钟或正文变化，请重新预览；未执行联系')
        return dict(proof)

    async def candidate(self,geek_id,job_id):
        proof=await open_profile(self.s,geek_id,job_id,self.case['job']['label'],reload_case=self.case)
        if card_fingerprint(proof)!=self.expected_fingerprint:
            raise ProfileChanged('普通推荐资料自审阅后已变化；请重新审阅，未执行联系')
        return proof

    async def receipt(self,proof,text):
        # A greeting opens an embedded chat on this BOSS version. Verify in
        # the actual 沟通 menu instead of accepting a preview or clicking again.
        from . import boss_chat
        from .autopilot import professional_identity_matches
        rows=await boss_chat.open_menu(self.s)
        label=proof.get('platform_label') or proof.get('receiver_label','')
        prefix=label[:-2] if label.endswith(('女士','先生')) else label
        rows=[r for r in rows if r.get('job_title')==self.case['job']['label']
              and r.get('receiver','').startswith(prefix)]
        for row in rows:
            identity=await boss_chat.inspect_row(self.s,row,self.case['job']['label'])
            if not professional_identity_matches(proof,identity):continue
            binding={k:identity[k] for k in ('chat_id','receiver','job_title')}
            snapshot=await boss_chat.read_conversation(self.s,**binding)
            matches=[m for m in snapshot['messages'] if m['direction']=='out' and m['text']==text and m['status'] in ('delivered','read')]
            if len(matches)==1:
                return {'evidence':'在真实沟通菜单按收件人及多条带日期履历核对会话；出站正文一致且显示送达或已读',
                        'conversation':binding,'message_id':matches[0]['external_id'],
                        'url':self.s.page.url,'at':time.time(),
                        'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
        return None


async def run_recommend_review(manager,job,session,case,pool):
    """Keep one recommendation page alive from profile review to dispatch."""
    from .boss_contact import run_first_contact
    job['recommend_stage']='choose_candidate'
    job['recommend_options']=pool
    choice=await manager.question(job,'reply','普通推荐资料已读取。请选择一份资料只读审阅；此步不会联系对方。')
    card=next((c for c in pool if c['geek_id']==choice),None)
    if card is None:
        raise SourcingError('请选择本次已读取的候选资料，未执行联系')
    # 只做收件人核验：资料与已审阅卡片不一致时 open_profile 抛错。
    await open_profile(session,choice,job_identity(case),case['job']['label'])
    case=manager.cases.load(case['id'])
    candidate_id='boss-geek:'+choice
    existing=case['candidates'].get(candidate_id)
    label=existing['profile'].get('display_name') if existing else '候选人 '+str(len(case['candidates'])+1)
    profile={**card,'display_name':label,'job_id':job_identity(case),'source_channel':'recommend',
             'review_card_sha256':card_fingerprint(card),'coverage':'当前普通推荐卡片及已打开的资料；未取得附件简历',
             'analysis_origin':'本机读取；原始资料未发送给模型'}
    # Reopening an unchanged card must not replace reviewed AI facts with the
    # raw summary or invalidate an otherwise current draft.
    if not (existing and existing['profile'].get('review_card_sha256') == profile['review_card_sha256']
            and existing['profile'].get('job_id') == profile['job_id']):
        manager.cases.add_candidate(case,candidate_id,RECOMMEND_URL,profile)
    job['params']['candidate_id']=candidate_id
    job['recommend_stage']='draft_preparation'
    await session.observe(True,text_limit=18000)
    draft_id=await manager.question(job,'reply','资料已在本机保存，等待准备脱敏分析与首聊草稿。原始资料和截图不会发送给模型；所有消息仍需另行确认。')
    latest=manager.cases.load(case['id'])
    draft=manager.cases.preview(latest,candidate_id,draft_id)
    if draft['intent']!='first_contact' or draft['status'] not in ('draft','approved'):
        raise SourcingError('请选择该候选人当前有效的首聊草稿')
    job['params']['message_id']=draft_id
    job['recommend_stage']='contact'
    manager.persist(job)
    job['preserve_preflight']=True
    return await run_first_contact(manager,job,session)
