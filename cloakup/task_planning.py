"""Task-owned plans; generation is distinct from observed page verification."""
import hashlib,json,time,uuid
from urllib.parse import urlparse
import jsonschema
from .platform import PLAN_SCHEMA
from .recruiting_ai import checked_brief,APPROVED_HOST
from .prompts import planning_prompt
from .sourcing import SourcingError


def planning_signature(definition,cases):
    data={'revision':definition['revision'],'config':definition['config'],
          'planning_context_revision':definition.get('planning_context',{}).get('revision',0),'runtime':'deepseek-harness@0.2.2rc2','prompt':planning_prompt(definition['config']),
          'cases':[{'id':c['id'],'revision':c.get('config_revision',0),'facts':c.get('anonymous_job'),'execution':c.get('execution',{}),'sourcing':c.get('job_sourcing')} for c in cases]}
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


async def generate_boss_plan(llm,definition,cases,conversation=False):
    if urlparse(str(llm.client.raw.base_url)).hostname!=APPROVED_HOST:
        raise SourcingError('计划生成仅允许使用已授权的脱敏文字网关')
    jobs=[]
    for c in cases:
        if not c.get('anonymous_job'):raise SourcingError('请先维护已确认的脱敏岗位事实，再生成计划')
        from .facts import effective_job
        brief={'job':effective_job(c),'candidate':{k:'' for k in ('experience','availability','visible_evidence','coverage')}}
        checked_brief(brief)
        jobs.append({'job':brief['job'],'execution':{k:c.get('execution',{}).get(k,k!='request_contacts') for k in ('new_contacts','follow_up','request_resume','request_contacts')}})
    payload={'goal':'按已确认岗位事实沟通，取得候选人简历；电话和微信非必需，默认不主动索要','sourcing_mode':definition['config'].get('sourcing_mode','continuous'),'target':definition['config'].get('target_resumes',1) if definition['config'].get('sourcing_mode','continuous')=='quota' else None,
             'task_execution':{k:v for k,v in definition['config'].get('execution',{}).items() if k!='outreach_terms'},'schedule':definition['config'].get('schedule',{}),'job_order':'按关联岗位顺序串行遍历，仅执行时间、额度、追聊等规则共用；每轮独立加载当前岗位 JD、事实、FAQ、首聊、职业关键词和推荐条件，不能跨岗位复用业务配置','jobs':jobs,'constraints':['同账号串行，来信优先','先查FAQ和已确认事实；依据不足生成待办','暂停停止页面动作，席位可释放','送达和已读是正常状态，不按未读时长生成异常待办；按所属任务追聊策略推进，连续达到上限无回应后停止主动追聊。真实发送证据仍须校验，不能重发结果未核实的原消息','实际收到简历后承接最后一轮语言自然回应，有问题先回答或转待办，表示稍后则等待；同一份只收尾一次，不承诺时间或结果','不新增未经核实的待遇或条件；只介绍薪酬结构和已确认的竞争力表述，不发送金额、范围、比例等量化待遇','正常回复后尽快索取简历，先回答可确认的问题；缺事实确认收到并转待办后仍推进求简历，不先多轮盘问','优先使用平台求简历按钮取得简历；电话和微信不影响完成，默认不主动索要；申请待同意或回执不明时不重复索要，不同时申请电话和微信；实际简历到达才计数']}
    if definition.get('previous_plan'):payload['previous_plan']=definition['previous_plan']
    context=definition.get('planning_context',{})
    if context.get('messages'):payload['planning_conversation']=context['messages']
    generic=definition['config'].get('engine')=='web'
    if generic:
        from .planning_dialogue import reviewed_text
        payload.update(goal=reviewed_text(definition['config']['goal']),website=urlparse(definition['config']['website']).hostname,output_schema=definition['config'].get('output_schema',{}))
        payload.pop('target',None)
    payload['job_configuration_boundary']='首聊、职业关键词和推荐条件保存在各岗位的版本快照中，由本机执行器按当前岗位加载；此处不传送未经脱敏复核的业务配置原文。通用计划使用占位符，不覆盖岗位配置。'
    system=planning_prompt(definition['config'])+'\n计划必须保持跨岗位可复用，以{{岗位JD}}、{{岗位首聊}}、{{职业关键词}}、{{推荐条件}}表示岗位参数；不把某个岗位业务配置推广到其他岗位。'
    bounded_plan=json.loads(json.dumps(PLAN_SCHEMA))
    for field in bounded_plan['properties'].values():
        field['maxItems']=12;field['items']['maxLength']=500
    schema=bounded_plan
    if conversation:
        system=system.replace('只输出steps、success_criteria、risks三个数组的JSON；每个最多12条，每条最多500字。','输出reply与plan两个字段的JSON。reply用中文回应本轮讨论，说明调整与缺失信息，不声称已经执行；plan包含steps、success_criteria、risks，每个最多12条，每条最多500字。')
        schema={'type':'object','additionalProperties':False,'required':['reply','plan'],'properties':{'reply':{'type':'string','minLength':1,'maxLength':2000},'plan':bounded_plan}}
    if generic:system+='\n当前为通用网站任务，按输入目标验收，不采用候选人数量条件。'

    plan=await llm.json(system=system,
        content=json.dumps(payload,ensure_ascii=False),schema=schema,structured=False,effort='low')
    jsonschema.validate(plan,schema)
    reply=plan['reply'] if conversation else None
    plan=plan['plan'] if conversation else plan
    jsonschema.validate(plan,PLAN_SCHEMA)
    if any(len(items)>12 or any(len(item)>500 for item in items) for items in plan.values()):raise SourcingError('计划过长，请重新生成')
    # Completion is a platform predicate, not a model's optional alternative.
    # Agreement to keep chatting on-platform is not an obtained contact detail.
    if not generic and definition['config'].get('sourcing_mode','continuous')=='quota':plan['success_criteria']=[f"取得 {definition['config']['target_resumes']} 位候选人的真实简历；电话和微信非必需，同意发送不计作已收到。"]+[s for s in plan['success_criteria'] if not any(word in s for word in ('联系方式','平台联系','平台沟通'))][:11]
    if not generic and definition['config'].get('sourcing_mode','continuous')=='continuous':
        plan['success_criteria']=['持续取得合适候选人的真实简历并归档，不设累计数量上限；电话和微信非必需。','遵守每日额度、沟通频率和平台限制；额度用完等待下一周期，工作窗口外或休息期间保留会话并停止业务动作；管理员可随时暂停或停止。']
    return {'id':uuid.uuid4().hex,'task_id':definition['id'],'task_revision':definition['revision'],
            'signature':planning_signature(definition,cases),'origin':'llm','verification':'pending','goal_enforced':True,
            'plan':plan,'at':time.time(),'observed_case_ids':[],
            'system_prompt':system,'model_input':payload,'prompt_sha256':hashlib.sha256(system.encode()).hexdigest(),
            'harness_trace':getattr(llm,'last_trace',None),'reply':reply,'planning_context_revision':context.get('revision',0)}
