"""Harness decision loop over a deterministic, locally verified action gateway.

Only approved job facts and finite state cross the model boundary. Native SDK
file/shell/browser tools stay disabled; a proposal never authorizes execution.
"""
import asyncio,hashlib,json,random,re,time,uuid
from . import boss_chat
from .autonomy import intent_labels,local_brief,validate_reply,day_key
from .answering import answer_question,UNCERTAIN,SENSITIVE
from .sourcing import SourcingError,TERMINAL
from .recruiting_ai import APPROVED_HOST
from .pacing import jitter
from urllib.parse import urlparse

VERSION=13
SYSTEM='''你是当前自动化任务的持续决策器。每轮只使用当前岗位的 JD 事实与独立寻访配置，不从通用计划推断其他岗位的首聊、关键词和推荐条件。筛选配置不是可对外承诺的事实。输入只有管理员复核的脱敏岗位事实、参考计划、有限职业标签与真实操作结果摘要，不是候选人原话。
根据目标和当前状态，从本轮available_actions中选择一个下一步动作，可调整顺序。候选人正常回复即优先推进简历：先简短回答有依据的问题，再尽快使用求简历按钮；缺少事实时确认收到并生成待办，之后仍推进简历收集，不先多轮背景追问；单候选人的资料收集条件仅为取得简历，任务继续寻访其他合适候选人，不因单人资料到达停止，电话和微信非必需，默认不主动索要，取得简历后停止资料索取；已取得的资料不再索要，不连续催促。
plan是边界和上下文，不是固定步骤。上一步未得到回执不能推断成功，更不能重发。旧消息回执不明时不推断送达；若available_actions提供后续明确意向对应的独立平台交换申请，可继续推进该动作，禁止重发旧消息。观察不足时等待或交管理员；不能新增事实、扩大权限、改变收件人、绕过暂停或网站校验。
本机已经校验会话绑定、授权和可用动作，不要因为看不到原始页面就假设尚未核验。has_unhandled_reply为真且answer_route为answer时，应优先处理可回答的问题或确有必要的资料读取，不应无理由等待。没有新输入通常等待；仅当本机提供nudge动作时可低频追聊，先审查上下文，选择温和提醒或有依据的澄清，不重复原话。连续3次无回应停止主动追聊，暂停和未知回执仍优先。缺事实则交管理员。
优先使用ask_resume、exchange_phone、exchange_wechat平台申请能力；不要用文字替代可用的按钮，不同时索要电话和微信。申请等待中不得催促或另换渠道索要。
只选择提供的动作。fact_index仅可从optional_fact_indices选择；answer、identity、stop、handoff、wait、escalate、accept_resume、read_online_resume必须为-1。理由解释本轮选择，不声称执行已完成。
只输出action、reason、fact_index三个字段的JSON。'''


def latest_incoming(person):
    from .boss_resume import is_permission_text
    return next((m for m in reversed(person.get('inbox',[])) if not m.get('platform_control') and not is_permission_text(m.get('text',''))),None)


def _resume_card(message):
    text=message.get('text') or ''
    return bool(message.get('resume') or message.get('attachment') or '点击预览附件简历' in text)


def reply_target(person):
    """简历附件紧跟在问题后面时，仍回应那个问题。附件本身不当成最后一句。"""
    incoming=latest_incoming(person)
    if not incoming or not _resume_card(incoming):
        return incoming
    from .boss_resume import is_permission_text
    earlier=[m for m in person.get('inbox',[]) if m is not incoming and not m.get('platform_control') and not is_permission_text(m.get('text','')) and not _resume_card(m)]
    if not earlier:
        return incoming
    question=earlier[-1]
    answered=any(m.get('status') in ('sent','sending','delivery_unknown') and (m.get('decision') or {}).get('reply_id')==question.get('external_id') and not (m.get('decision') or {}).get('pending_ack') for m in person.get('outbox',[]))
    return incoming if answered else question


def plan_context(manager,case_id):
    members=manager.account_execution.members()
    task=next((m['id'] for m in members if case_id in m['case_ids'] and m['status']!='stopped'),None)
    if not task:
        task=next((t['id'] for t in manager.definitions.list() if case_id in t['config'].get('case_ids',[])),None)
    record=next((p for p in manager.task_plan_records() if p.get('task_id')==task and not p.get('stale') and p.get('origin') in ('llm','validated_release')),None)
    return {'id':record['id'],'plan':record['plan']} if record else None


def effective_answer(case,person):
    from .conversation_semantics import current
    semantic=current(case,person)
    if semantic:
        return {**semantic['answer'],'policy_version':'harness-context-v1','config_revision':case.get('config_revision',0)}
    from .autopilot import can_revisit_question
    incoming=reply_target(person)
    pending=next((m for m in person.get('inbox',[]) if m['external_id']==person.get('pending_question_id')),None)
    question=pending if pending and can_revisit_question(case,person) else incoming
    return answer_question(case,question['text'],person=person) if question else {'action':'continue','source':'conversation','fact_indices':[]}


def capabilities(case,person,snapshot,controls):
    """Action admission, never model-selected safeguards."""
    from .autopilot import can_revisit_question
    ex=case.get('execution',{});incoming=reply_target(person)
    labels=intent_labels(incoming['text']) if incoming else []
    fresh=bool(incoming and (incoming['external_id']!=person.get('handled_reply_id') or can_revisit_question(case,person)))
    answer=effective_answer(case,person)
    semantic=answer.get('semantic_route')
    if semantic:
        labels=[label for label in labels if label not in ('decline','later','identity')]
    if semantic in ('decline','later','identity'):labels=[semantic]
    if incoming and re.search(r'不要再联系|别再联系|请勿联系|勿扰|停止联系',incoming['text']):labels=['decline']
    actions={'wait':'等待状态变化，保持会话；不发送消息'}
    unknown=any(m['status'] in ('sending','delivery_unknown') for m in person.get('outbox',[]))
    if person['status'] in TERMINAL:return actions,answer,labels
    if 'decline' in labels:
        if fresh:actions['stop']='记录拒绝并停止联系'
        return actions,answer,labels
    from .boss_contact_consent import available as contact_permissions
    actions.update(contact_permissions(case,person,controls))
    if any(k in actions for k in ('accept_wechat','accept_phone')):return actions,answer,labels
    if 'later' in labels:return actions,answer,labels
    if person.get('resumes') and incoming and (fresh or person.get('pending_question_id')==incoming['external_id']) and answer['action'] in ('answer','defer'):
        if not unknown and ex.get('follow_up',True):
            answered_ack=any(m.get('decision',{}).get('pending_ack') and m['decision'].get('reply_id')==incoming['external_id'] and m['status'] in ('sent','sending','delivery_unknown') for m in person.get('outbox',[]))
            if answer['action']=='answer':actions['answer']='先回答候选人最后的问题，不用收到简历的感谢语代替回答'
            elif person.get('pending_question_id')!=incoming['external_id']:
                actions['escalate']='最后的问题缺少事实，转管理员，不能用结束语掩盖未答问题'
                if not answered_ack:actions['acknowledge_pending']='先确认收到问题并等待管理员补充，不结束会话'
            elif not answered_ack:actions['acknowledge_pending']='先确认收到问题并等待管理员补充，不结束会话'
        return actions,answer,labels
    if person.get('resumes') and person.get('pending_question_id'):return actions,answer,labels
    if person.get('resumes'):
        from .resume_closure import pending
        from .receipt_policy import continuation
        from .resume_closure import render
        if (not unknown or 'acknowledge_resume' in continuation(person,snapshot)) and ex.get('follow_up',True) and pending(person) and render(case,person,snapshot):
            actions['acknowledge_resume']='结合候选人最后一轮语言自然收尾，先接住对方意图；不机械重复收到简历，同份只一次，不作未经确认的承诺';return actions,answer,labels
        if person.get('handoff'):return actions,answer,labels
    if 'later' in labels:return actions,answer,labels
    if person.get('handoff'):return actions,answer,labels
    if person.get('resumes'):
        actions['handoff']='简历已取得，转交顾问；联系方式可选，不再索要';return actions,answer,labels
    if unknown:
        from .receipt_policy import continuation
        from .boss_requests import available
        admitted=available(case,person,controls)
        actions.update({k:admitted[k] for k in continuation(person,snapshot) if k in admitted})
        return actions,answer,labels
    if fresh and 'identity' in labels:
        if ex.get('follow_up',True):actions['identity']='如实回应明确的自动化身份询问'
        return actions,answer,labels
    consent=len(controls.get('requests',[]))==1 and not person.get('resumes') and not any(a.get('kind')=='accept_resume' for a in person.get('platform_actions',[]))
    if consent and ex.get('accept_resume_requests',True):actions['accept_resume']='同意候选人主动发送附件简历，核验后才更新状态'
    if not fresh and answer.get('semantic_route')=='clarify':return actions,answer,labels
    if fresh and not case.get('execution',{}).get('request_resume',True) and not person.get('online_resume_checked_at') and not person.get('online_resume'):
        actions['read_online_resume']='读取平台可见职业资料，仅保存在本机'
    from .followup import evaluate as followup_evaluate
    if not fresh and followup_evaluate(case,person)['state']=='exhausted':return actions,answer,labels
    from .boss_requests import available
    native=available(case,person,controls) if ex.get('follow_up',True) and answer.get('resume_readiness')!='understand_first' and (answer['action']=='continue' or (not fresh and (not person.get('pending_question_id') or any(m.get('decision',{}).get('pending_ack') and m['decision'].get('reply_id')==person.get('pending_question_id') and m['status']=='sent' for m in person.get('outbox',[]))))) else {}
    count=sum(1 for m in person.get('outbox',[]) if m.get('claimed_at') and day_key(m['claimed_at'])==day_key())+sum(1 for a in person.get('platform_actions',[]) if a.get('at') and day_key(a['at'])==day_key())
    if count>=case.get('autonomy',{}).get('max_messages_per_candidate_day',6):native={}
    actions.update(native)
    if ex.get('follow_up',True) and incoming and answer['action']=='defer' and person.get('pending_question_id')==incoming['external_id'] and not any(m.get('decision',{}).get('pending_ack') and m['decision'].get('reply_id')==incoming['external_id'] and m['status'] in ('sending','delivery_unknown','sent') for m in person.get('outbox',[])) and count<case.get('autonomy',{}).get('max_messages_per_candidate_day',6):actions['acknowledge_pending']='确认收到待核实问题，不编造答案、不承诺时间；每个问题只回应一次，管理员待办保持未解决'
    if not fresh and ex.get('follow_up',True):
        from .followup import evaluate
        followup=evaluate(case,person)
        if followup['due']:
            for variant in followup['variants']:
                if variant=='resume_after_interest':actions['nudge_'+variant]='候选人已有正向回复但尚未给出简历：可温和文字索要一次简历，不重复点击平台申请'
                else:actions['nudge_'+variant]='依据送达或已读状态和上下文进行低频追聊：'+variant+'；受对话体验底线约束，无回应后停止'
    if not fresh or not ex.get('follow_up',True):return actions,answer,labels
    count=sum(1 for m in person.get('outbox',[]) if m.get('claimed_at') and day_key(m['claimed_at'])==day_key())
    if count>=case.get('autonomy',{}).get('max_messages_per_candidate_day',6):return actions,answer,labels
    if answer['action']=='defer':
        actions['escalate']='缺少完整确认事实，创建管理员待办，补充和重规划后再回复'
    elif answer['action']=='answer':
        actions['answer']='使用已通过本机事实校验的完整答案回复，不新增条件'
    else:
        resume=not person.get('resumes') and ex.get('request_resume',True)
        contact=ex.get('request_contacts',False) and not person.get('contacts')
        if native:return actions,answer,labels
        blocked_request=next((a for a in reversed(person.get('platform_actions',[])) if a.get('kind')=='ask_resume' and a['status'] in ('claimed','awaiting_verification','pending')),None)
        if resume and blocked_request and 'interest' in labels and incoming.get('observed_at',0)>=blocked_request.get('at',0) and not any(m['status'] in ('sending','delivery_unknown') for m in person.get('outbox',[])):
            actions['invite_resume']='候选人新正向回复后发送一次文字简历邀请；不重复点击已有平台申请，不按冷追聊等待';return actions,answer,labels
        if any(a.get('kind') in ('ask_resume','exchange_phone','exchange_wechat') for a in person.get('platform_actions',[])):return actions,answer,labels
        if resume:actions['request_resume']='礼貌索取尚未取得的简历'
        if contact:actions['request_contact']='礼貌索取尚未取得的电话或微信'
        if resume and contact:actions['request_both']='一次索取尚未取得的简历与联系方式'
    return actions,answer,labels


def direct_proposal(actions,answer,labels,person):
    action=None;reason=''
    if 'stop' in actions:action='stop';reason='明确拒绝直接停止，无需模型判断'
    elif 'accept_wechat' in actions:action='accept_wechat';reason='候选人主动申请交换微信，按授权同意，不因已收简历或交接而忽略'
    elif 'accept_phone' in actions:action='accept_phone';reason='候选人主动申请交换电话，按授权同意并核验实际号码'
    elif 'acknowledge_resume' in actions:action='acknowledge_resume';reason='简历已收到，依据最后一条真实回复自然回应，不使用统一结束语'
    elif 'later' in labels:action='wait';reason='候选人表示稍后，程序停止推进'
    elif 'accept_resume' in actions:action='accept_resume';reason='候选人主动申请且已授权，程序接收并核验'
    elif 'answer' in actions:action='answer';reason='执行 Harness 已理解上下文并通过来源校验的回复' if answer.get('source')=='harness_context' else '已确认事实完整覆盖问题，直接答复'
    elif 'identity' in actions:action='identity';reason='按全局规则如实回应身份询问'
    elif 'invite_resume' in actions:action='invite_resume';reason='收到新的正向回复，平台申请已有占位，立即用一次文字邀请推进简历，不重复点击'
    elif 'acknowledge_pending' in actions:action='acknowledge_pending';reason='问题已转管理员且尚未确认收到，先发送一次流程回应'
    elif 'escalate' in actions:action='escalate';reason='Harness 已识别具体问题和缺失事实，程序创建待办' if answer.get('source')=='harness_context' else '事实不足直接生成待办，不交给模型猜测'
    elif 'handoff' in actions:action='handoff';reason='资料齐备，按目标交接'
    elif 'nudge_clarify_previous' in actions:action='nudge_clarify_previous';reason='此前对这条问题的回答与当前已确认口径不一致，程序按现口径补充回复一次'
    elif 'nudge_resume_after_interest' in actions:action='nudge_resume_after_interest';reason='候选人已有正向回复且尚未给出简历，程序按约定时机文字索要一次简历'
    elif set(actions)=={'wait'}:action='wait';reason='没有可执行的新业务动作'
    if action:return {'action':action,'reason':reason,'fact_index':-1}
    return None


def state_key(case,person,snapshot,plan,controls):
    data=[VERSION,case.get('config_revision'),case['job']['revision'],case.get('autonomy',{}).get('revision'),
          person.get('context_revision'),snapshot['fingerprint'],plan['id'] if plan else None,
          person.get('handled_reply_id'),person.get('pending_question_id'),bool(person.get('resumes')),bool(person.get('contacts')),
          person.get('online_resume_checked_at'),person.get('harness_feedback_pending',False),[(a['id'],a['status']) for a in person.get('platform_actions',[])],
          len(controls.get('requests',[])),[(m['id'],m['status']) for m in person.get('outbox',[])]]
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def payload(case,person,plan,actions,answer,labels,feedback):
    brief=local_brief(case,person)
    from .autopilot import can_revisit_question
    incoming=latest_incoming(person)
    fresh=bool(incoming and (incoming['external_id']!=person.get('handled_reply_id') or can_revisit_question(case,person)))
    optional=[i for i,f in enumerate(brief['job']['requirements']) if not UNCERTAIN.search(f) and not SENSITIVE.search(f)]
    content={'version':VERSION,'goal':'持续寻访合适候选人，单候选人取得简历后交接并继续下一位，不设累计简历上限；电话和微信是可选资料，不影响目标完成；尊重拒绝和稍后意愿',
            'reference_plan':plan['plan'] if plan else None,'job':brief['job'],'professional_context':brief['candidate'],
            'observation':{'has_unhandled_reply':fresh,'conversation_bound':bool(person.get('conversation')),'authorization_enabled':bool(case.get('autonomy',{}).get('enabled')),'candidate_question_detected':answer['action'] in ('answer','defer'),'intents':labels,'has_resume':bool(person.get('resumes')),'has_contact':bool(person.get('contacts')),
                           'has_online_profile':bool(person.get('online_resume',{}).get('sections')),'platform_requests':[{'kind':a['kind'],'status':a['status']} for a in person.get('platform_actions',[])],
                           'followup':{k:v for k,v in __import__('cloakup.followup',fromlist=['evaluate']).evaluate(case,person).items() if k!='anchor'},
                           'answer_route':answer['action'],'answer_source':answer['source'],
                           'has_pending_question':bool(person.get('pending_question_id'))},
            'available_actions':actions,'optional_fact_indices':optional,'previous_results':feedback[-4:]}
    if set(actions)=={'contact','wait'}:
        # 首聊前的安全前提已由程序完成；未告知时模型会把“尚未联系”的正常状态误判为未核验而等待。
        content['first_contact_checks']={
            'verified_by_program':['岗位首聊话术已与平台设置核对一致','候选人卡片显示可打招呼，本机无该候选人的会话或发送记录（去重已完成）',
                                   '推荐卡片与详情已只读核对','自动沟通授权、岗位暂停状态与发送额度已由程序校验'],
            'note':'conversation_bound=false、followup.state=no_verified_send 是尚未联系候选人的正常状态，不代表风险或未核验。'
                   '本轮只依据 professional_context 与 job.requirements 判断初步匹配度：首聊只是了解意向，不作录用决定，'
                    '有限标签无法证明业绩属正常；有与 job.requirements 或本岗位职业关键词相关的经历线索时应联系，明显无关时再等待。'}
    return content


async def propose(manager,case,person,plan,actions,answer,labels,feedback):
    from .harness_runtime import get_harness
    from .llm import LLM
    llm=LLM(manager.model_client())
    if urlparse(str(llm.client.raw.base_url)).hostname!=APPROVED_HOST:raise SourcingError('决策网关不在已授权范围')
    engine=get_harness(llm,manager.root,"boss-decisions")
    content=payload(case,person,plan,actions,answer,labels,feedback)
    schema={'type':'object','additionalProperties':False,'required':['action','reason','fact_index'],'properties':{
        'action':{'enum':list(actions)},'reason':{'type':'string','minLength':1,'maxLength':600},'fact_index':{'type':'integer','enum':[-1]+content['optional_fact_indices']}}}
    schema['oneOf']=[{'properties':{'action':{'const':a},'fact_index':{'enum':[-1]+content['optional_fact_indices'] if a.startswith('request_') else [-1]}}} for a in actions]
    try:result=await engine.json(system=SYSTEM,content=json.dumps(content,ensure_ascii=False),schema=schema)
    except Exception as exc:
        exc.harness_trace=engine.last_trace
        raise
    if result['action'] not in actions:
        error=SourcingError('Harness提出了未授权动作');error.harness_trace=engine.last_trace;raise error
    if result['fact_index']!=-1 and (not result['action'].startswith('request_') or result['fact_index'] not in content['optional_fact_indices']):
        error=SourcingError('Harness引用的事实不在本轮允许范围');error.harness_trace=engine.last_trace;raise error
    return result,content,engine.last_trace


def reply_step(case,person,action,answer,labels,index):
    from .facts import effective_job
    confirmed=(effective_job(case) or {'requirements':[]})['requirements']
    texts={'request_resume':'方便发一份最新版简历吗？便于结合您的经历进一步沟通。',
           'request_contact':'方便通过平台交换电话或微信吗？便于进一步沟通具体安排。',
           'request_both':'方便发一份最新版简历，并通过平台交换电话或微信吗？便于结合您的经历进一步沟通。'}
    if action=='answer':text=answer['text']
    elif action=='acknowledge_resume':
        # Dispatch renders against the current bound snapshot, after rechecking.
        from .resume_closure import render
        closing=render(case,person,{'messages':person.get('history',[])})
        text=closing['text'] if closing else '谢谢您。'
    elif action=='invite_resume':text='好的，方便发一份最新版简历吗？我先了解一下您的经历。'
    elif action=='identity':text='这条回复由 AI 根据当前招聘账号授权生成，岗位信息以已确认内容为准；需要人工核实的事项会交由账号负责人处理。'
    else:
        text=('简历已收到，谢谢。' if person.get('resumes') else '谢谢回复。')
        if index!=-1:text+=confirmed[index].rstrip('。')+'。'
        text+=texts[action]
    validate_reply(text,confirmed,[index] if index!=-1 else [])
    invitation=next((a['id'] for a in reversed(person.get('platform_actions',[])) if a.get('kind')=='ask_resume' and a['status'] in ('claimed','awaiting_verification','pending')),None) if action=='invite_resume' else None
    return {'semantic_state_key':person.get('conversation_semantics',{}).get('state_key') if action=='answer' and answer.get('source')=='harness_context' else None,'resume_receipt_ack':action=='acknowledge_resume','resume_invite_for_action':invitation,'action':'send','intent':'request_resume' if action in ('request_resume','request_both','invite_resume') else 'follow_up','text':text,'labels':labels,'fact_indices':[index] if index!=-1 else []}


async def run_conversation(manager,job,session,case_id,candidate_id,snapshot):
    from .autopilot import sync_snapshot,revisit_question,dispatch_reply
    from .boss_resume import inspect_permissions,reconcile_permission,accept_resume_request
    cycle=uuid.uuid4().hex;feedback=[];seen=set();tool_attempts=set()
    initial=manager.cases.load(case_id)
    history=manager.cases.candidate(initial,candidate_id).get('harness_memory',[])
    turns=initial.get('execution',{}).get('decision_max_turns',3)
    for turn in range(min(4,max(1,turns))):
        await manager.checkpoint(job)
        case=manager.cases.load(case_id);person=manager.cases.candidate(case,candidate_id)
        if not case.get('autonomy',{}).get('enabled') or (job.get('account_runner') and not manager.account_execution.can_send(case_id)):return
        controls=await inspect_permissions(session)
        case=manager.cases.load(case_id);person=manager.cases.candidate(case,candidate_id)
        reconcile_permission(manager.cases,case,person,snapshot,controls)
        from .boss_requests import reconcile as reconcile_requests
        reconcile_requests(manager.cases,case,person,controls)
        from .boss_contact_consent import reconcile as reconcile_contact_permissions
        reconcile_contact_permissions(manager.cases,case,person,controls)
        # Platform permission cards use the verified UI, independent of dialogue inference.
        if len(controls.get('requests',[]))==1 and not person.get('resumes') and not any(
            a.get('kind')=='accept_resume' for a in person.get('platform_actions',[])
        ) and case.get('execution',{}).get('accept_resume_requests',True):
            await accept_resume_request(manager,job,session,case,person,snapshot)
            snapshot=await boss_chat.read_conversation(session,**person['conversation'])
            fresh=manager.cases.load(case_id)
            sync_snapshot(manager.cases,fresh,manager.cases.candidate(fresh,candidate_id),snapshot)
            from .resume_archive import capture
            await capture(manager,job,session,case_id,candidate_id,snapshot)
            return
        elif case.get('execution',{}).get('accept_resume_requests',True) and not person.get('resumes') and not any(a.get('kind')=='accept_resume' for a in person.get('platform_actions',[])):
            from .autopilot import _resume_offer_pending
            if _resume_offer_pending(person):
                hint=str((controls.get('debug') or []))[:160]
                manager.event(job,'step','已打开简历接收申请，页面上没有唯一的同意按钮，本轮未点击 文案%s个 申请%s个 %s'%(controls.get('phrase_nodes'),len(controls.get('requests') or []),hint))
        # Understanding precedes semantic action admission. Model errors or
        # privacy blocks never become a fabricated "missing job fact" reply.
        from .conversation_semantics import ensure,consent,current as semantic_current,PrivacyReviewRequired
        restore_owed_answer(manager.cases,case,person)
        incoming=reply_target(person)
        # 隐私暂缓只挡模型理解与模型回复；不需要理解对话的程序事务（收到简历的致谢）照常执行。
        privacy_hold=False
        semantic_required=bool(consent(manager) and incoming and person['status'] not in TERMINAL and
            not any(m['status'] in ('sending','delivery_unknown') for m in person.get('outbox',[])) and
            (incoming['external_id']!=person.get('handled_reply_id') or person.get('pending_question_id') or person.get('harness_feedback_pending')))
        if semantic_required:
            semantic_plan=plan_context(manager,case_id)
            semantic_epoch=job.get('takeover_epoch',0)
            from .conversation_semantics import fingerprint as semantic_fingerprint
            semantic_key=semantic_fingerprint(case,person)
            held=person.get('decision_hold',{})
            semantic=None
            if held.get('retry_at',0)>time.time() and held.get('config_revision')==case.get('config_revision',0):
                if not is_privacy_hold(held):return
                privacy_hold=True
            else:
                try:semantic=await ensure(manager,case,person,semantic_plan)
                except asyncio.CancelledError:raise
                except PrivacyReviewRequired:
                    hold(manager,case,person,PRIVACY_HOLD_REASON,kind='privacy')
                    privacy_hold=True
                except Exception as exc:
                    hold(manager,case,person,'Harness 对话理解未完成或回复未通过依据校验：'+type(exc).__name__+'；未生成岗位事实缺失回复',exc=exc);return
            await manager.checkpoint(job)
            case=manager.cases.load(case_id);person=manager.cases.candidate(case,candidate_id)
            if semantic_fingerprint(case,person)!=semantic_key or job.get('takeover_epoch',0)!=semantic_epoch or job.get('manual_takeover') or not case.get('autonomy',{}).get('enabled') or job.get('account_runner') and not manager.account_execution.can_send(case_id):return
            if semantic and not semantic_current(case,person):
                person['conversation_semantics']=semantic
                person.pop('decision_hold',None)
                manager.cases.audit(case,'harness_conversation_understood',candidate_id=candidate_id,**semantic)
            if semantic and semantic['answer']['action']!='defer' and person.get('pending_question_id'):
                person.pop('pending_question_id',None);person.pop('planning_answer_ready',None)
                # “确认收到、等待核实”不是回答：只有真正的回答（含发送中、回执未知）才算已答，避免重复发送。
                if incoming and not answer_sent(person,incoming['external_id']):
                    person.pop('handled_reply_id',None)
                manager.cases.save(case)
        revisit_question(manager.cases,case,person,snapshot)
        plan=plan_context(manager,case_id)
        actions,answer,labels=capabilities(case,person,snapshot,controls)
        actions={k:v for k,v in actions.items() if k not in tool_attempts}
        if privacy_hold:
            actions={k:v for k,v in actions.items() if k in PRIVACY_HOLD_ACTIONS or k=='wait'}
            if set(actions)<={'wait'}:return
        direct=direct_proposal(actions,answer,labels,person)
        if not direct and not plan:
            hold(manager,case,person,'缺少当前有效的 Harness 任务计划，请更新规划后继续');return
        guarded_plan=None if direct else plan
        key=state_key(case,person,snapshot,guarded_plan,controls)
        if key in seen and not feedback:return
        seen.add(key)
        cache=person.get('harness_decision',{})
        if not direct and not feedback and cache.get('state_key')==key and cache.get('next_review_at',0)>time.time():return
        if len(actions)==1 and not feedback and not person.get('harness_feedback_pending'):return
        pending_hold=person.get('decision_hold',{})
        if not direct and pending_hold.get('retry_at',0)>time.time() and pending_hold.get('config_revision')==case.get('config_revision',0):return
        epoch=job.get('takeover_epoch',0)
        if direct:
            decision=direct;trace=None
            content={'route':'program','available_actions':actions,'answer_source':answer['source']}
            if answer.get('source')=='harness_context':
                semantic=semantic_current(case,person)
                content.update(semantic_state_key=semantic['state_key'] if semantic else None,semantic_answer=answer)
        else:
            manager.cases.audit(case,'harness_observation',candidate_id=candidate_id,cycle_id=cycle,turn=turn+1,state_key=key,model_input=payload(case,person,plan,actions,answer,labels,history+feedback))
            job['autopilot_stage']='harness_deciding'
            if hasattr(manager,'event'):manager.event(job,'decision','Harness正在根据当前状态选择下一步')
            try:
                decision,content,trace=await propose(manager,case,person,plan,actions,answer,labels,history+feedback)
            except asyncio.CancelledError:raise
            except Exception as exc:
                hold(manager,case,person,'Harness决策未完成：'+type(exc).__name__,getattr(exc,'harness_trace',None),exc=exc);return
        # No model output is executed against a stale observation or authority.
        await manager.checkpoint(job)
        fresh=manager.cases.load(case_id);person=manager.cases.candidate(fresh,candidate_id)
        current=await boss_chat.read_conversation(session,**person['conversation'])
        current_controls=await inspect_permissions(session)
        fresh=manager.cases.load(case_id);person=manager.cases.candidate(fresh,candidate_id)
        current_plan=plan_context(manager,case_id)
        if job.get('takeover_epoch',0)!=epoch or job.get('manual_takeover') or not fresh.get('autonomy',{}).get('enabled') or (job.get('account_runner') and not manager.account_execution.can_send(case_id)) or state_key(fresh,person,current,None if direct else current_plan,current_controls)!=key:
            manager.cases.audit(fresh,'harness_decision_rejected',candidate_id=candidate_id,cycle_id=cycle,reason='观察、配置、计划或操作权已变化，未执行旧决策',harness_trace=trace,proposal=decision,model_input=content)
            return
        allowed,answer,labels=capabilities(fresh,person,current,current_controls)
        if decision['action'] not in allowed:
            manager.cases.audit(fresh,'harness_decision_rejected',candidate_id=candidate_id,cycle_id=cycle,reason='动作不再可用');return
        action=decision['action'];ident=uuid.uuid4().hex
        job['autopilot_stage']='harness_executing'
        if hasattr(manager,'event'):manager.event(job,'decision',('程序直接处理：' if direct else 'Harness已选择动作：')+action)
        if not privacy_hold:person.pop('decision_hold',None)
        person['last_decision']={'actor':'program' if direct else 'harness','id':ident,'action':action,'reason':decision['reason'],'at':time.time()}
        if not direct:person['harness_decision']={'id':ident,'cycle_id':cycle,'action':action,'reason':decision['reason'],'state_key':key,'next_review_at':time.time()+jitter(300),'at':time.time()}
        manager.cases.audit(fresh,'program_decision' if direct else 'harness_decision',candidate_id=candidate_id,decision_id=ident,cycle_id=cycle,turn=turn+1,model_input=content,proposal=decision,harness_trace=trace,plan_id=plan['id'] if plan else None)
        if action=='wait':
            if direct and 'later' in labels and latest_incoming(person):person['handled_reply_id']=latest_incoming(person)['external_id']
            if not direct and latest_incoming(person) and latest_incoming(person)['external_id']!=person.get('handled_reply_id'):
                person['harness_wait_count']=person.get('harness_wait_count',0)+1
                if person['harness_wait_count']>=2:
                    hold(manager,fresh,person,'Harness连续选择等待但仍有未处理来信，请检查任务规划或请求重新决策',trace)
                    return
            person['harness_feedback_pending']=False
            person['decision_wait_until']=time.time()+jitter(300);person['decision_wait_revision']=fresh.get('config_revision',0);manager.cases.save(fresh);return
        person['harness_wait_count']=0
        if action in tool_attempts:return
        tool_attempts.add(action)
        try:
            if action in ('stop','handoff','escalate'):
                incoming=reply_target(person)
                if action=='stop':
                    manager.cases._invalidate(person);person['status']='do_not_contact'
                    manager.cases.audit(fresh,'candidate_opt_out',candidate_id=candidate_id,reason='尊重候选人拒绝')
                elif action=='handoff':
                    person['handoff']={'reason':'已取得简历，转顾问接续；联系方式非必需','at':time.time()}
                    manager.cases.audit(fresh,'consultant_handoff',candidate_id=candidate_id,reason=person['handoff']['reason'])
                else:
                    person['pending_question_id']=incoming['external_id']
                    manager.cases.audit(fresh,'answer_waiting_admin',candidate_id=candidate_id,reply_id=incoming['external_id'],reason=answer['reason'])
                if incoming:person['handled_reply_id']=incoming['external_id']
                manager.cases.save(fresh);result='recorded'
            elif action.startswith('nudge_'):
                step={'action':'send','intent':'follow_up','nudge':True,'nudge_variant':action.removeprefix('nudge_'),'labels':[],'harness_decision_id':ident}
                await dispatch_reply(manager,job,session,fresh,person,current,step)
                saved=manager.cases.candidate(manager.cases.load(case_id),candidate_id)
                msg=next((m for m in reversed(saved['outbox']) if m.get('decision',{}).get('harness_decision_id')==ident),None)
                result='verified' if msg and msg['status']=='sent' else 'awaiting_verification' if msg and msg['status'] in ('sending','delivery_unknown') else 'not_executed'
            elif action=='acknowledge_pending':
                pending_target=reply_target(person)
                if pending_target and person.get('pending_question_id')!=pending_target['external_id']:
                    person['pending_question_id']=pending_target['external_id']
                    manager.cases.save(fresh)
                step={'action':'send','intent':'follow_up','pending_ack':True,'needs_confirmation':True,'labels':[],'program_decision_id':ident}
                await dispatch_reply(manager,job,session,fresh,person,current,step)
                saved=manager.cases.candidate(manager.cases.load(case_id),candidate_id)
                ack=next((m for m in reversed(saved['outbox']) if m.get('decision',{}).get('program_decision_id')==ident),None)
                result='verified' if ack and ack['status']=='sent' else 'awaiting_verification' if ack and ack['status'] in ('sending','delivery_unknown') else 'not_executed'
            elif action=='accept_resume':
                await accept_resume_request(manager,job,session,fresh,person,current)
                saved=manager.cases.candidate(manager.cases.load(case_id),candidate_id)
                result='verified' if any(a['status']=='accepted' for a in saved.get('platform_actions',[])) else 'awaiting_verification'
            elif action in ('accept_wechat','accept_phone'):
                from .boss_contact_consent import execute
                result=await execute(manager,job,session,fresh,person,action)
            elif action in ('ask_resume','exchange_phone','exchange_wechat'):
                from .boss_requests import execute
                result=await execute(manager,job,session,fresh,person,action)
            elif action=='read_online_resume':
                from .resume_capture import capture_online_resume
                await capture_online_resume(manager,job,session,case_id,candidate_id)
                saved=manager.cases.candidate(manager.cases.load(case_id),candidate_id)
                result='observed' if saved.get('online_resume',{}).get('sections') else 'not_available'
            else:
                step=reply_step(fresh,person,action,answer,labels,decision['fact_index'])
                step['program_decision_id' if direct else 'harness_decision_id']=ident
                await dispatch_reply(manager,job,session,fresh,person,current,step)
                saved=manager.cases.candidate(manager.cases.load(case_id),candidate_id)
                message=next((m for m in reversed(saved['outbox']) if (m.get('decision',{}).get('harness_decision_id') or m.get('decision',{}).get('program_decision_id'))==ident),None)
                result='verified' if message and message['status']=='sent' else 'awaiting_verification' if message and message['status'] in ('sending','delivery_unknown') else 'not_executed'
        except asyncio.CancelledError:raise
        except SourcingError as exc:
            if any(t in str(exc) for t in ('登录','校验','过于频繁','账号存在异常')):raise
            result='blocked'
        except Exception:
            result='not_verified'
        fresh=manager.cases.load(case_id);person=manager.cases.candidate(fresh,candidate_id)
        person['harness_feedback_pending']=True
        person['harness_memory']=(history+feedback+[{'action':action,'result':result}])[-4:]
        manager.cases.audit(fresh,'harness_tool_result',candidate_id=candidate_id,decision_id=ident,cycle_id=cycle,action=action,result=result)
        feedback.append({'action':action,'result':result})
        if action in ('stop','handoff'):return
        if not fresh.get('autonomy',{}).get('enabled') or (job.get('account_runner') and not manager.account_execution.can_send(case_id)):return
        snapshot=await boss_chat.read_conversation(session,**person['conversation'])
        fresh=manager.cases.load(case_id);person=manager.cases.candidate(fresh,candidate_id)
        sync_snapshot(manager.cases,fresh,person,snapshot)
        # The attachment can first appear in this post-action snapshot.
        # Archive it before handing control back to the account scheduler.
        if any(m.get('resume') and m['direction']=='in' and m.get('resume_selector') for m in snapshot['messages']):
            from .resume_archive import capture
            await capture(manager,job,session,case_id,candidate_id,snapshot)
    fresh=manager.cases.load(case_id)
    manager.cases.audit(fresh,'harness_cycle_yielded',candidate_id=candidate_id,cycle_id=cycle,reason='达到本轮决策步数，交还账号调度器，下轮基于新状态继续')


def answer_sent(person,reply_id,statuses=('sent','sending','delivery_unknown')):
    """这条来信已有真正的回答（不是“确认收到、等待核实”、追聊或收简历致谢）。"""
    for m in person.get('outbox',[]):
        d=m.get('decision') or {}
        if m.get('status') not in statuses or d.get('reply_id')!=reply_id:continue
        if d.get('nudge_variant')=='clarify_previous':return True
        if d.get('pending_ack') or d.get('nudge') or d.get('resume_receipt_ack'):continue
        if (d.get('answer_route') or {}).get('action')=='answer':return True
    return False


def owed_question(person):
    """最后一条来信若只发过“确认收到、等待核实”、还没有真正回答，返回这条来信。

    我方之后又发过其他消息（人工接续）时不算，避免盖过人工。
    """
    incoming=reply_target(person)
    if not incoming or person.get('handled_reply_id')!=incoming['external_id']:return None
    acks=[m for m in person.get('outbox',[]) if m.get('status')=='sent' and (m.get('decision') or {}).get('pending_ack') and m['decision'].get('reply_id')==incoming['external_id']]
    if not acks or answer_sent(person,incoming['external_id']):return None
    outgoing=[m for m in person.get('history',[]) if m.get('direction')=='out' and not m.get('platform_control')]
    if not outgoing or outgoing[-1].get('external_id')!=(acks[-1].get('receipt') or {}).get('message_id'):return None
    return incoming


def later_question(person,first,second):
    """两条来信 id 中较新的一条；不在收件里的视为无效。"""
    ids=[m.get('external_id') for m in person.get('inbox',[])]
    if first not in ids:return second if second in ids or not first else first
    if second not in ids:return first
    return first if ids.index(first)>ids.index(second) else second


def restore_owed_answer(store,case,person):
    """恢复“这条问题还欠候选人一个回答”的标记。

    11-22 候选人 30 问“双休还是单休”，先收到“确认清楚后再回复”；管理员 16:20 补了 FAQ，
    但重新理解时把那句确认当成了回答，清掉了标记，之后一直只选等待。
    11-24 16:10 同一位：管理员先后补了最新问题和几天前的旧问题，待答指针被改回旧问题，最新问题不再补答。
    条件：最后一条来信已发过确认收到、没有任何真正回答，且我方之后没有再发过其他消息（人工接续优先）；
    指针为空或指向更早的来信时才改，幂等。
    """
    if person.get('status') in TERMINAL:return False
    incoming=owed_question(person)
    if not incoming:return False
    current=person.get('pending_question_id')
    if current==incoming['external_id'] or (current and later_question(person,current,incoming['external_id'])==current):return False
    person['pending_question_id']=incoming['external_id']
    store.audit(case,'answer_owed_restored',candidate_id=person['id'],reply_id=incoming['external_id'],
                reason='此前只发了“确认收到、等待核实”，尚未真正回答；恢复待答标记，事实齐备后补答')
    return True


PRIVACY_HOLD_REASON='对话含无法可靠脱敏的片段，请管理员在本机复核或接续；没有将该对话发送模型'
# 隐私暂缓期间仍可执行、且不依赖模型理解对话的程序事务。
PRIVACY_HOLD_ACTIONS={'acknowledge_resume'}


def is_privacy_hold(record):
    return bool(record) and (record.get('kind')=='privacy' or str(record.get('reason','')).startswith('对话含无法可靠脱敏'))


def hold(manager,case,person,reason,trace=None,exc=None,kind=None):
    ident=person['id'];case=manager.cases.load(case['id']);person=manager.cases.candidate(case,ident)
    from .logbook import exception as log_exception, log
    fields=dict(type='decision_held',case_id=case['id'],candidate_id=ident,reason=reason,
                trace={k:(trace or {}).get(k) for k in ('session_id','finish_reason','elapsed_ms','gateway_requests','correction_rounds','error')} if trace else None)
    if exc is not None:log_exception('model',exc,**fields)
    else:log('model',**fields)
    # 暂缓时长随机抖动，避免所有暂缓候选人都在整 5 分钟后被重新访问。
    retry_at=time.time()+jitter(300)
    person['decision_wait_until']=retry_at;person['decision_wait_revision']=case.get('config_revision',0)
    person['decision_hold']={'reason':reason,'at':time.time(),'retry_at':retry_at,'config_revision':case.get('config_revision',0),**({'kind':kind} if kind else {})}
    manager.cases.audit(case,'harness_decision_held',candidate_id=person['id'],reason=reason,harness_trace=trace)


async def initial_contact_decision(manager,job,case,person):
    """The model chooses whether to contact an already locally eligible card.

    返回决策编号表示联系；False 表示仅跳过这位候选人（模型选择等待或其冷却未到），
    调用方继续看下一张卡片；None 表示授权、计划、调用异常等全局条件不满足，应结束本批。
    """
    await manager.checkpoint(job)
    if person.get('decision_hold',{}).get('retry_at',0)>time.time():return False
    if person.get('decision_wait_until',0)>time.time():return False
    if not case.get('autonomy',{}).get('enabled') or (job.get('account_runner') and not manager.account_execution.can_send(case['id'])):return None
    plan=plan_context(manager,case['id'])
    if not plan:
        hold(manager,case,person,'缺少当前有效的任务计划，请重规划后继续');return None
    epoch=job.get('takeover_epoch',0);revision=(case['job']['revision'],case.get('config_revision',0),case['autonomy']['revision'],person['context_revision'])
    try:
        decision,content,trace=await propose(manager,case,person,plan,{'contact':'按已核实的岗位话术发起一次主动联系','wait':'本轮不新增联系'},
            {'action':'continue','source':'conversation'},[],[])
    except asyncio.CancelledError:raise
    except Exception as exc:
        hold(manager,case,person,'Harness首聊决策未完成：'+type(exc).__name__,getattr(exc,'harness_trace',None),exc=exc);return None
    await manager.checkpoint(job)
    fresh=manager.cases.load(case['id']);person=manager.cases.candidate(fresh,person['id']);current_plan=plan_context(manager,case['id'])
    if (fresh['job']['revision'],fresh.get('config_revision',0),fresh['autonomy']['revision'],person['context_revision'])!=revision or not fresh.get('autonomy',{}).get('enabled') or job.get('takeover_epoch',0)!=epoch or job.get('manual_takeover') or current_plan!=plan or (job.get('account_runner') and not manager.account_execution.can_send(case['id'])):return None
    ident=uuid.uuid4().hex;person.pop('decision_hold',None)
    manager.cases.audit(fresh,'harness_decision',candidate_id=person['id'],decision_id=ident,cycle_id=ident,turn=1,model_input=content,proposal=decision,harness_trace=trace,plan_id=plan['id'])
    if decision['action']!='contact':
        # 冷却时长随机化，避免被跳过者总在固定间隔后被重新打开详情。
        person['decision_wait_until']=time.time()+random.uniform(240,480);manager.cases.save(fresh);return False
    return ident
