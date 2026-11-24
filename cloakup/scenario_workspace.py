"""Scenario-owned policies, isolated conversations and explicit task application.

Templates never contain production job snapshots or candidate conversations.
Legacy task bundles remain internal publication boundaries, not user workspaces.
"""
import asyncio,copy,json,time,uuid
from urllib.parse import urlparse
import jsonschema
from .agent_workspace import digest,administrator_text,simulate_bundle
from .conversation_semantics import SYSTEM,consent
from .harness_runtime import get_harness
from .llm import LLM
from .recruiting_ai import APPROVED_HOST
from .job_config import DEFAULT_EXECUTION,EXECUTION_SCHEMA
from .task_policy import DEFAULT_SCHEDULE

COMMON_KEYS={'new_contacts','follow_up','request_resume','request_contacts','accept_resume_requests','accept_contact_requests','nudge_enabled','nudge_unread_hours','nudge_read_hours','nudge_max_attempts','resume_receipt_message'}
POLICY_KEYS={'goal','instructions','greeting_template','planning_prompt','conversation_prompt','execution','plan'}
DEFAULT_POLICY={
 'goal':'持续寻访，尽可能多地取得符合当前岗位要求的候选人简历；电话和微信非必需。',
 'instructions':'先回应候选人最后一句的真实意图，再自然推进下一步。依据当前岗位 JD 和已确认事实回答，不编造薪酬、地点或合作条件。候选人表现出兴趣后及时邀请发送简历，但尊重先了解再决定的意愿，不重复催促。收到简历后结合最后一句自然确认和感谢。明确拒绝后停止联系，追聊达到上限仍无回应则停止主动追聊。缺少重要事实时生成管理员待办。',
 'greeting_template':'您好，想和您交流{{岗位名称}}这个机会，方便了解一下吗？',
 'planning_prompt':'为当前网站场景制定通用推进策略。岗位名称、JD、地点、薪酬、FAQ 等在任务执行时注入，规划中以占位符表示，不写入某个岗位的具体事实。',
 'conversation_prompt':SYSTEM,
 'execution':{k:copy.deepcopy(DEFAULT_EXECUTION[k]) for k in COMMON_KEYS},
 'plan':{'steps':['读取当前任务绑定岗位的 JD 与已确认事实','优先处理来信，核验候选人意图并据事实回答','在适当时机邀请发送简历，避免重复索取','收到简历后自然回应并归档；需要管理员的信息生成待办'],
         'success_criteria':['持续取得与当前岗位相关的真实简历','对外回复有事实依据且可追溯','尊重拒绝、稍后沟通和追聊边界'],
         'risks':['缺少岗位事实时不编造','页面或身份未核验时不执行业务动作','任务参数与权限由程序约束']}}
DEFAULT_TEST_FACTS={'role':'示例岗位','location':'工作地点待确认','duties':'负责人才寻访、沟通与交付','requirements':'具有相关工作经验，具体要求以测试事实为准','compensation':'薪酬结构需进一步确认，不承诺具体金额','faq':''}
FACT_FIELDS=set(DEFAULT_TEST_FACTS)


def website_key(website):
 p=urlparse(website)
 if p.scheme not in ('http','https') or not p.hostname:raise ValueError('请选择有效的网站')
 return p.scheme+'://'+p.netloc.lower()


def scenario_key(config):return digest([website_key(config['website']),config['scenario'],config['engine']])[:32]


class ScenarioWorkspace:
 def __init__(self,manager):
  self.m=manager;self.db=manager.db;self.workers={};self.locks={}
  for record in self.db.list('scenario_apply'):
   if record['status'] in ('queued','validating','applying'):
    record.update(status='interrupted',finished_at=time.time(),error='服务重启中断应用；已应用任务以各任务记录为准，未应用任务保留原配置')
    self.db.put('scenario_apply',record['id'],record)
  self.bootstrap()

 def anchors(self):
  values={}
  for case in self.m.cases.list():
   job=case.get('job',{});snapshot=job.get('snapshot') or {}
   for value,token in [(job.get('label'),'{{岗位名称}}'),(snapshot.get('company'),'{{所属机构}}'),(snapshot.get('location'),'{{工作地点}}')]:
    if isinstance(value,str) and len(value.strip())>=2:values[value.strip()]=token
  return values

 def abstract(self,text):
  for value,token in sorted(self.anchors().items(),key=lambda p:-len(p[0])):text=text.replace(value,token)
  return text

 def checked_policy(self,policy):
  if not isinstance(policy,dict) or set(policy)!=POLICY_KEYS:raise ValueError('通用方案字段不完整')
  result=copy.deepcopy(policy)
  for key in POLICY_KEYS-{'execution','plan'}:
   value=result[key]
   limit=20000 if key=='conversation_prompt' else 8000
   if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError('通用策略内容为空或过长')
   result[key]=self.abstract(value.strip())
  if len(result['greeting_template'])>100:raise ValueError('通用首聊话术最多100字')
  execution=result['execution']
  if not isinstance(execution,dict) or set(execution)!=COMMON_KEYS:raise ValueError('模板只能包含通用执行规则，时间、额度、筛选与通知由任务管理')
  jsonschema.validate({**DEFAULT_EXECUTION,**execution},EXECUTION_SCHEMA)
  result['execution']['resume_receipt_message']=self.abstract(execution['resume_receipt_message'])
  plan=result['plan']
  if not isinstance(plan,dict) or set(plan)!={'steps','success_criteria','risks'}:raise ValueError('方案需要推进步骤、验收目标和风险边界')
  for key,items in plan.items():
   if not isinstance(items,list) or not 1<=len(items)<=30 or any(not isinstance(v,str) or not v.strip() or len(v)>1000 for v in items):raise ValueError('通用计划格式不合法')
   plan[key]=[self.abstract(v) for v in items]
  return result

 def bootstrap(self):
  for task in self.m.definitions.list():
   from .scenarios import is_sourcing, scenario_for_engine
   if not is_sourcing(task['config']['engine']):continue
   spec=scenario_for_engine(task['config']['engine'])
   ident=scenario_key(task['config'])
   try:self.db.get('scenario_template',ident);continue
   except FileNotFoundError:pass
   policy=copy.deepcopy(DEFAULT_POLICY)
   for key in COMMON_KEYS:
    if key in task['config'].get('execution',{}):policy['execution'][key]=copy.deepcopy(task['config']['execution'][key])
   now=time.time();record={'id':ident,'name':spec['label'],'website':website_key(task['config']['website']),'scenario':task['config']['scenario'],'engine':spec['engine'],
    'revision':1,'policy':self.checked_policy(policy),'created_at':now,'updated_at':now,'origin':'abstracted_common_settings'}
   self.db.put('scenario_template',ident,record,expected=0,history=True)
   self.db.put('scenario_test_context',ident,{'id':ident,'revision':1,'facts':copy.deepcopy(DEFAULT_TEST_FACTS)},expected=0)

 def catalog(self):
  from .scenarios import SCENARIOS
  self.bootstrap()
  sessions=[{k:v for k,v in s.items() if k not in ('traces',)} for s in self.db.list('scenario_session')]
  # 已登记但还没建立模板的场景也要出现在前端，不能只显示已经保存的模板。
  available=[{'engine':s['engine'],'label':s['label'],'website':s['default_website'],'scenario':s['default_scenario'],'platform':s['platform'],'page_adapter':s['page_adapter']} for s in SCENARIOS.values() if s['kind']=='sourcing']
  return {'templates':self.db.list('scenario_template'),'available':available,'sessions':sessions,'test_contexts':self.db.list('scenario_test_context'),
   'versions':self.db.list('scenario_version'),'applications':self.db.list('scenario_apply')[:30],'bindings':self.db.list('scenario_binding')}

 def create(self,body):
  from .scenarios import scenario_for_host
  website=body.get('website','https://www.zhipin.com')
  spec=scenario_for_host(urlparse(website).hostname)
  if spec is None:raise ValueError('请选择已登记的寻访场景网站，其他网站仍通过任务入口管理')
  config={'website':website,'scenario':body.get('scenario',spec['default_scenario']),'engine':spec['engine']}
  ident=uuid.uuid4().hex;now=time.time();name=str(body.get('name',spec['label'])).strip()
  if not name or len(name)>80:raise ValueError('模板名称需要1至80字')
  if not isinstance(config['scenario'],str) or not config['scenario'].strip() or len(config['scenario'])>200:raise ValueError('场景需要1至200字')
  # 场景模板与“网站 + 场景”一一对应：同一范围已有模板时直接使用它，不另建。
  if any((t['website'],t['scenario'])==(website_key(config['website']),config['scenario']) for t in self.db.list('scenario_template')):
   raise ValueError('该网站与场景已有场景模板，请直接使用它')
  record={'id':ident,'name':name,'website':website_key(config['website']),'scenario':config['scenario'],'engine':spec['engine'],'revision':1,'policy':copy.deepcopy(DEFAULT_POLICY),'created_at':now,'updated_at':now,'origin':'administrator'}
  self.db.put('scenario_template',ident,record,expected=0,history=True)
  self.db.put('scenario_test_context',ident,{'id':ident,'revision':1,'facts':copy.deepcopy(DEFAULT_TEST_FACTS)},expected=0)
  return record

 def save(self,ident,body):
  current=self.db.get('scenario_template',ident)
  if current['revision']!=body.get('expected_revision'):raise ValueError('通用方案已被其他操作修改，本地输入已保留，请重新载入后比较')
  policy=self.checked_policy(body.get('policy'))
  if policy==current['policy']:return current
  current.update(policy=policy,revision=current['revision']+1,updated_at=time.time())
  self.db.put('scenario_template',ident,current,expected=current['revision']-1,history=True)
  return current

 def test_context(self,ident,body):
  self.db.get('scenario_template',ident);current=self.db.get('scenario_test_context',ident)
  if body.get('expected_revision')!=current['revision']:raise ValueError('测试事实已变化，请刷新后重试')
  facts=body.get('facts')
  if not isinstance(facts,dict) or set(facts)!=FACT_FIELDS:raise ValueError('测试事实字段不完整')
  checked={}
  for key,value in facts.items():
   if not isinstance(value,str) or len(value)>(1500 if key=='faq' else 80 if key=='role' else 500):raise ValueError('测试岗位名称最多80字，单项事实最多500字，FAQ最多1500字')
   checked[key]=administrator_text(value) if value.strip() else ''
  current.update(facts=checked,revision=current['revision']+1)
  self.db.put('scenario_test_context',ident,current,expected=current['revision']-1,history=True)
  return current

 def session(self,ident,body):
  self.db.get('scenario_template',ident);agent=body.get('agent');mode=body.get('mode','discuss' if agent=='planner' else 'simulate')
  if agent not in ('planner','executor') or mode not in ('discuss','simulate') or agent=='planner' and mode!='discuss':raise ValueError('规划端只进行通用方案讨论，执行端可讨论策略或模拟回复')
  context=self.db.get('scenario_test_context',ident)
  record={'id':uuid.uuid4().hex,'template_id':ident,'agent':agent,'mode':mode,'revision':1,'title':'新对话','messages':[],'created_at':time.time(),'updated_at':time.time()}
  if mode=='simulate':record.update(facts=copy.deepcopy(context['facts']),facts_revision=context['revision'],has_resume=bool(body.get('has_resume',False)))
  self.db.put('scenario_session',record['id'],record,expected=0)
  return record

 async def chat(self,ident,body):
  sid=body.get('session_id');lock=self.locks.setdefault(sid,asyncio.Lock())
  async with lock:
   session=self.db.get('scenario_session',sid);template=self.db.get('scenario_template',ident)
   if session['template_id']!=ident or session['revision']!=body.get('expected_revision'):raise ValueError('对话已变化，请重新载入')
   if len(session['messages'])>=80:raise ValueError('本次对话已达40轮，请开始新对话')
   text=administrator_text(self.abstract(body.get('message','')))
   if not text.strip():raise ValueError('请输入消息')
   if session['mode']=='simulate':return await self._simulate(template,session,text)
   schema={'type':'object','additionalProperties':False,'required':['reply','policy_patch'],'properties':{'reply':{'type':'string','minLength':1,'maxLength':5000},'policy_patch':{'type':'object','additionalProperties':False,'properties':{k:{} for k in POLICY_KEYS}}}}
   system=('你是网站场景通用模板的'+('规划端 Agent' if session['agent']=='planner' else '执行端 Agent')+'。和管理员进行多轮对话，产出可用于多个任务和多个岗位的通用方案。'
    '首聊参考只用于管理员编辑岗位时借鉴，不覆盖岗位独立首聊、职业关键词和推荐条件。只讨论网站场景的目标、策略、规则、计划和提示词。不读取、不引用具体任务名称、岗位JD、岗位事实、候选人或本地会话。涉及岗位名称、地点、职责、薪酬等使用{{岗位名称}}、{{工作地点}}、{{岗位JD}}、{{已确认岗位事实}}等占位符。'
    'planning_prompt与conversation_prompt同样必须通用。execution只能修改现有通用规则，不修改额度、时间窗口、筛选条件或通知配置。'
    '需要澄清时提问并保持policy_patch为空。明确修改时给完整字段补丁，execution和plan如修改需返回完整对象。reply自然解释当前建议与变更，不说已应用到任务，不操作浏览器、不发消息、不发布。'
    '没有本地候选人会话选择功能。规划端不负责模拟候选人；执行端的候选人回复测试在独立模拟模式完成。')
   context={'website':template['website'],'scenario':template['scenario'],'policy':template['policy'],'dialogue':[{'role':m['role'],'content':m['content']} for m in session['messages'][-20:]]+[{'role':'user','content':text}]}
   engine=get_harness(LLM(self.m.model_client()),self.m.root/'scenario-sessions'/sid,session['agent'])
   if urlparse(str(engine.client.raw.base_url)).hostname!=APPROVED_HOST:raise ValueError('模型网关不在已授权范围')
   result=await engine.json(system=system,content=json.dumps(context,ensure_ascii=False),schema=schema);jsonschema.validate(result,schema)
   policy={**template['policy'],**result['policy_patch']};saved=self.save(ident,{'expected_revision':template['revision'],'policy':policy})
   self._append(session,text,self.abstract(result['reply']),saved['revision'],{'harness_trace':engine.last_trace,'model_input':context,'policy_patch':result['policy_patch']})
   return {'session':session,'template':saved}

 def _append(self,session,text,reply,revision,trace,explanation=None):
  prior=session['revision'];session['messages'] += [{'role':'user','content':text,'at':time.time()},{'role':'assistant','content':reply,'at':time.time(),'policy_revision':revision}]
  if explanation is not None:session['messages'][-1]['explanation']=explanation
  session['title']=session['title'] if prior>1 else text[:24];session['revision']+=1;session['updated_at']=time.time();session.setdefault('traces',[]).append(trace)
  self.db.put('scenario_session',session['id'],session,expected=prior,history=True)

 def synthetic_bundle(self,template,facts):
  from .facts import checked_facts
  from .faq import checked_faq
  values=[]
  for field,kind in [('location','work_arrangement'),('duties','duties'),('requirements','requirements'),('compensation','compensation')]:
   if facts.get(field):values.append({'kind':kind,'value':facts[field],'status':'confirmed'})
  faq=[]
  for line in facts.get('faq','').splitlines():
   if not line.strip():continue
   if '|' not in line:raise ValueError('测试常见问题按每行“问题|答案”填写')
   question,answer=line.split('|',1);faq.append({'question':question.strip(),'answer':answer.strip(),'variants':[],'enabled':True})
  cid='scenario-synthetic';role=facts.get('role') or '示例岗位';requirements=[v['value'] for v in values]
  item={'case_id':cid,'config_revision':1,'job_revision':1,'job':{'label':role,'revision':1,'snapshot':{'requirements':'\n'.join(requirements)}},'anonymous_job':{'role':role,'requirements':requirements},'facts':checked_facts(values,1),'faq':checked_faq(faq)}
  p=template['policy'];cfg={'engine':template['engine'],'case_ids':[cid],'goal':p['goal'],'execution':{**DEFAULT_EXECUTION,**p['execution']},'schedule':copy.deepcopy(DEFAULT_SCHEDULE),'greeting':p['greeting_template'],'recommend_filters':[]}
  item['sourcing']={'greeting':p['greeting_template'],'outreach_terms':[],'recommend_filters':[]}
  cfg['greeting']=''
  return {'config':cfg,'sources':[item],'plan':p['plan'],'conversation_prompt':p['conversation_prompt']+'\n场景通用策略：\n'+p['instructions']}

 async def _simulate(self,template,session,text):
  if not consent(self.m):raise ValueError('请先确认脱敏招聘对话的模型处理授权')
  bundle=self.synthetic_bundle(template,session['facts'])
  turns=[{'direction':'in' if m['role']=='user' else 'out','text':m['content']} for m in session['messages'][-10:]]+[{'direction':'in','text':text}]
  fixture={'id':'scenario:manual','case_id':'scenario-synthetic','messages':turns,'expected_routes':['continue','answer','defer','clarify','decline','later','identity'],'has_resume':session.get('has_resume',False),'pending_resume':False}
  result=await simulate_bundle(bundle,[fixture],self.m.model_client(),self.m.root/'scenario-simulation'/uuid.uuid4().hex,self.db.get('disclosure_consent','recruiting-conversation'))
  outcome=result['outcomes'][0]
  if outcome.get('error'):raise ValueError(outcome['error'])
  answer=outcome.get('answer',{});reply=answer.get('text') or {'stop':'本轮判断为停止主动联系，不再发送追聊消息。','wait':'本轮无需追加消息，等待对方进一步回复。','ask_resume':'方便发一份最新简历吗？我结合您的经历进一步了解。'}.get(outcome.get('action'),'本轮不发送消息，需先核实信息。')
  explanation={'intent':outcome.get('actual_route'),'action':outcome.get('action'),'missing_information':answer.get('missing_information',[]),'sources':[s.get('text','') for s in answer.get('sources',[])]}
  self._append(session,text,reply,template['revision'],{'outcome':outcome,'policy_revision':template['revision'],'facts_revision':session['facts_revision'],'real_writes':0},explanation)
  return {'session':session,'template':template,'simulation':True,'real_writes':0}

 def compatible(self,template,task):
  cfg=task['config'];return cfg['engine']==template['engine'] and website_key(cfg['website'])==template['website'] and cfg['scenario']==template['scenario']

 def compose(self,template,task):
  bundle=copy.deepcopy(self.db.get('agent_bundle',self.m.agent_workspace.bootstrap(task['id'])['version_id'])['bundle']);p=template['policy']
  bundle['config'].update(goal=p['goal'],planning_prompt=p['planning_prompt']+'\n通用策略：\n'+p['instructions'])
  bundle['config']['execution'].update(copy.deepcopy(p['execution']))
  bundle['plan']=copy.deepcopy(p['plan']);bundle['conversation_prompt']=p['conversation_prompt']+'\n场景通用策略：\n'+p['instructions']
  # Existing job business preferences are immutable under a generic template apply.
  return bundle

 def apply(self,ident,body):
  template=self.db.get('scenario_template',ident);ids=body.get('task_ids')
  if body.get('expected_revision')!=template['revision']:raise ValueError('方案已变化，请确认最新内容后应用')
  if not isinstance(ids,list) or not ids or len(ids)>20 or len(ids)!=len(set(ids)):raise ValueError('请选择1至20个不同任务')
  members={m['id']:m for m in self.m.account_execution.members()};owner=self.m.busy()
  for tid in ids:
   task=self.m.definitions.get(tid)
   if not self.compatible(template,task):raise ValueError('只能应用到同一网站与场景的任务')
   if members.get(tid,{}).get('status')=='running' or owner and (owner.get('active_case_id') in task['config']['case_ids'] or owner.get('task_id')==tid):raise ValueError('请先暂停所选任务并等待安全点，应用不会自动启动')
   self.compose(template,task)
  if any(set(ids)&set(j['task_ids']) and j['status'] in ('queued','validating','applying') for j in self.db.list('scenario_apply')):raise ValueError('所选任务已有应用操作正在进行')
  job={'id':uuid.uuid4().hex,'template_id':ident,'template_revision':template['revision'],'policy_hash':digest(template['policy']),'policy':copy.deepcopy(template['policy']),'task_ids':ids,'status':'queued','results':[],'created_at':time.time(),'configuration_scope':'common_policy_only'}
  self.db.put('scenario_apply',job['id'],job)
  self.workers[job['id']]=asyncio.create_task(self._apply(job['id']))
  return job

 async def _apply(self,ident):
  job=self.db.get('scenario_apply',ident);template={**self.db.get('scenario_template',job['template_id']),'policy':job['policy']};prepared=[]
  try:
   job['status']='validating';self.db.put('scenario_apply',ident,job)
   for tid in job['task_ids']:
    task=self.m.definitions.get(tid);draft=self.m.agent_workspace.draft(tid,{'bundle':self.compose(template,task)});test=self.m.agent_workspace.start_test(tid,{'draft_id':draft['id'],'mode':'validate'})
    row={'task_id':tid,'status':'validating','test_id':test['id']};job['results'].append(row);self.db.put('scenario_apply',ident,job)
    await self.m.agent_workspace.workers[test['id']]
    result=self.db.get('agent_test',test['id']);row.update(status='validated' if result['status']=='passed' else 'failed',summary={'passed':sum(bool(r.get('passed')) for r in result.get('result',{}).get('outcomes',[])),'total':len(result.get('result',{}).get('outcomes',[]))});self.db.put('scenario_apply',ident,job)
    if result['status']!='passed':raise ValueError('任务隔离校验未通过，生效配置保持原版本；请查看报告')
    prepared.append((tid,draft,test))
   for tid,draft,_test in prepared:
    task=self.m.definitions.get(tid)
    if task['revision']!=draft['base_task_revision']:raise ValueError('验证期间任务配置已变化，请重新应用')
    if any(m['id']==tid and m['status']=='running' for m in self.m.account_execution.members()):raise ValueError('验证期间任务已启动，请暂停后重新应用')
   version_id=digest([job['template_id'],job['policy_hash']])[:32]
   try:self.db.get('scenario_version',version_id)
   except FileNotFoundError:self.db.put('scenario_version',version_id,{'id':version_id,'template_id':job['template_id'],'policy_hash':job['policy_hash'],'policy':job['policy'],'created_at':time.time(),'template_revision':job['template_revision']},expected=0,history=True)
   job['status']='applying';self.db.put('scenario_apply',ident,job)
   for tid,draft,test in prepared:
    release=self.m.agent_workspace.publish(tid,{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']})
    self.db.put('scenario_binding',tid,{'id':tid,'template_id':job['template_id'],'version_id':version_id,'policy_hash':job['policy_hash'],'template_revision':job['template_revision'],'task_revision':release['task_revision'],'applied_at':time.time()})
    next(r for r in job['results'] if r['task_id']==tid).update(status='applied',task_revision=release['task_revision']);self.db.put('scenario_apply',ident,job)
   job.update(status='applied',version_id=version_id)
  except asyncio.CancelledError:job.update(status='interrupted',error='应用被中断，未应用任务保留原配置');raise
  except Exception as exc:job.update(status='partial' if any(r['status']=='applied' for r in job['results']) else 'failed',error=str(exc) if isinstance(exc,ValueError) else '应用未完成，请查看各任务验证记录')
  finally:job['finished_at']=time.time();self.db.put('scenario_apply',ident,job)
