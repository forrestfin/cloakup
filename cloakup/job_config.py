"""Versioned local job configuration; never overwrites the platform JD."""
import copy
import time

from .sourcing import SourcingError
from .boss import PLAN_SCHEMA
from .boss_recommend import checked_filters
from .recruiting_ai import checked_brief
import jsonschema

from .followup import DEFAULTS as FOLLOWUP_DEFAULTS
from .resume_closure import MESSAGE as RESUME_RECEIPT_MESSAGE

DEFAULT_EXECUTION = {**FOLLOWUP_DEFAULTS,'resume_receipt_message':RESUME_RECEIPT_MESSAGE,'new_contacts':True,'follow_up':True,'request_resume':True,'request_contacts':False,'accept_contact_requests':True,'accept_resume_requests':True,
 'decision_max_turns':3,'poll_seconds':120,'daily_new_limit':20,'max_messages_per_candidate_day':6,
                     'outreach_terms':[]}
EXECUTION_SCHEMA = {'type':'object','additionalProperties':False,'required':list(DEFAULT_EXECUTION),
 'properties':{'resume_receipt_message':{'type':'string','minLength':1,'maxLength':240},**{k:{'type':'boolean'} for k in ('new_contacts','follow_up','request_resume','request_contacts','accept_resume_requests','accept_contact_requests','nudge_enabled')},
 **{k:{'type':'integer','minimum':low,'maximum':high} for k,low,high in [('nudge_unread_hours',1,168),('nudge_read_hours',1,168),('nudge_max_attempts',1,3),('nudge_start_hour',0,23),('nudge_end_hour',1,24)]},
 'decision_max_turns':{'type':'integer','minimum':1,'maximum':4},
 'poll_seconds':{'type':'integer','minimum':30,'maximum':3600},
 'daily_new_limit':{'type':'integer','minimum':0,'maximum':20},
 'max_messages_per_candidate_day':{'type':'integer','minimum':1,'maximum':20},
 'outreach_terms':{'type':'array','maxItems':30,'items':{'type':'string','minLength':1,'maxLength':40}}}}


def view_config(case):
 execution={**DEFAULT_EXECUTION,**case.get('execution',{})}
 for k in ('poll_seconds','daily_new_limit','max_messages_per_candidate_day'):
  execution[k]=case.get('autonomy',{}).get(k,execution[k])
 return {'revision':case.get('config_revision',0),
         'jd':copy.deepcopy((case['job']['snapshot'] or {}).get('requirements','')),
         'plan':copy.deepcopy(case.get('search_plan')),
         'anonymous_job':copy.deepcopy(case.get('anonymous_job')),
         'greeting':case.get('desired_greeting',case.get('platform_greeting_text',case.get('greeting_draft',''))),
         'faq':copy.deepcopy(case.get('faq',[])),
         'facts':__import__('cloakup.facts',fromlist=['facts_for']).facts_for(case),
         'filters':list(case.get('recommend_filters',[])),'execution':execution}


def locked_task(db,case_id,releases=None,tasks=None,account_tasks=None):
 """已纳入有发布版本的任务时返回该任务（岗位只能在任务里改）；releases（发布 id 集合）、tasks、account_tasks 可传入已读的整表，避免逐岗位重复读取。"""
 releases={r['id'] for r in db.list('agent_release')} if releases is None else releases
 if not releases:return None
 from .task_policy import effective_task
 task=effective_task(db,case_id,tasks,account_tasks)
 return {'id':task['id'],'name':task['config'].get('name')} if task and task['id'] in releases else None


def locked_tasks(db,case_ids):
 """状态轮询用：发布、任务、账号任务三张表各只读一次，逐岗位给出 locked_task 的结果。"""
 releases={r['id'] for r in db.list('agent_release')}
 if not releases:return {cid:None for cid in case_ids}
 tasks,account_tasks=db.list('task'),db.list('account_task')
 return {cid:locked_task(db,cid,releases,tasks,account_tasks) for cid in case_ids}


def save_config(store,case,body):
 if locked_task(store.db,case['id']):
  raise SourcingError('该岗位已纳入验证后发布，请在关联任务的岗位资料与寻访配置中修改并验证，不能直接覆盖生效事实')
 if set(body)!={'expected_revision','config'}:raise SourcingError('配置请求字段不合法')
 if type(body['expected_revision']) is not int or body['expected_revision']!=case.get('config_revision',0):
  raise SourcingError('配置已被其他页面修改，请重新加载后再保存')
 for c in case['candidates'].values():
  if any(m['status']=='sending' for m in c['outbox']):raise SourcingError('有消息发送中，请待回执核实后保存')
 cfg=copy.deepcopy(body['config'])
 if isinstance(cfg.get('execution'),dict):
  cfg['execution'].setdefault('request_contacts',False)
  cfg['execution'].setdefault('accept_contact_requests',True)
  cfg['execution'].setdefault('resume_receipt_message',RESUME_RECEIPT_MESSAGE)
  for key,value in FOLLOWUP_DEFAULTS.items():cfg['execution'].setdefault(key,value)
  from .followup import limits
  cfg['execution'].update(limits(cfg['execution']))
 cfg.setdefault('facts',__import__('cloakup.facts',fromlist=['facts_for']).facts_for(case))
 cfg.setdefault('faq',copy.deepcopy(case.get('faq',[])))
 from .faq import checked_faq
 checked_faq(cfg['faq'])
 if set(cfg)!={'facts','faq','jd','plan','anonymous_job','greeting','filters','execution'}:raise SourcingError('配置字段缺失或包含未知字段')
 if not isinstance(cfg['jd'],str) or not 1<=len(cfg['jd'].strip())<=20000:raise SourcingError('JD 必须为非空文字，最多两万字')
 try:
  if cfg['plan'] is not None:jsonschema.validate(cfg['plan'],PLAN_SCHEMA)
  jsonschema.validate(cfg['execution'],EXECUTION_SCHEMA)
  if cfg['execution']['nudge_start_hour']>=cfg['execution']['nudge_end_hour']:raise SourcingError('追聊开始时间必须早于结束时间')
  from .autonomy import validate_reply
  validate_reply(cfg['execution']['resume_receipt_message'],[],[])
  checked_filters(cfg['filters'])
  if cfg['anonymous_job'] is not None:
   checked_brief({'job':cfg['anonymous_job'],'candidate':{k:'' for k in ('experience','availability','visible_evidence','coverage')}})
 except (jsonschema.ValidationError,TypeError) as e:raise SourcingError('配置格式不合法：'+str(e).splitlines()[0]) from e
 if not isinstance(cfg['greeting'],str) or not 1<=len(cfg['greeting'].strip())<=100:raise SourcingError('首聊话术必须为1至100字')
 from .facts import checked_facts
 previous_facts={f['id']:f for f in __import__('cloakup.facts',fromlist=['facts_for']).facts_for(case)}
 cfg['facts']=checked_facts(cfg['facts'],case.get('config_revision',0)+1)
 for fact in cfg['facts']:
  prior=previous_facts.get(fact['id'])
  if prior and all(fact[k]==prior[k] for k in ('kind','value','status')):fact.update(source=prior['source'],revision=prior['revision'],**({'anonymous_index':prior['anonymous_index']} if 'anonymous_index' in prior else {}))
 old=view_config(case)
 if case.get('task_policy') and {k:v for k,v in cfg['execution'].items() if k!='outreach_terms'}!={k:v for k,v in old['execution'].items() if k!='outreach_terms'}:
  raise SourcingError('执行与追聊规则由所属任务管理，请在任务执行设置中修改')
 if cfg['jd']!=old['jd'] and cfg['anonymous_job']==old['anonymous_job']:
  # Raw local JD never silently replaces the gateway-approved anonymous facts.
  cfg['anonymous_job']=None
  if cfg['facts']==old['facts']:
   for fact in cfg['facts']:fact['status']='unconfirmed'
  if cfg['faq']==old['faq']:
   for faq in cfg['faq']:faq['enabled']=False
 case.setdefault('source_job_snapshot',copy.deepcopy(case['job']['snapshot']))
 case.setdefault('config_history',[]).append({'revision':old['revision'],'at':time.time(),'config':old})
 case['config_revision']=old['revision']+1
 case['job']['snapshot']['requirements']=cfg['jd'];case['job']['revision']+=1
 case['confirmed_facts']=cfg['facts']
 case.update(search_plan=cfg['plan'],anonymous_job=cfg['anonymous_job'],desired_greeting=cfg['greeting'].strip(),
             recommend_filters=cfg['filters'],execution=cfg['execution'],faq=cfg['faq'],job_sourcing={'greeting':cfg['greeting'].strip(),'outreach_terms':list(cfg['execution']['outreach_terms']),'recommend_filters':list(cfg['filters']),'must_haves':copy.deepcopy((case.get('job_sourcing') or {}).get('must_haves') or [])})
 policy=case.get('autonomy',{})
 policy.update(revision=policy.get('revision',0)+1,**{k:cfg['execution'][k] for k in ('poll_seconds','daily_new_limit','max_messages_per_candidate_day')})
 case['autonomy']=policy
 for c in case['candidates'].values():
  store._invalidate(c)
  from .faq import match_faq
  incoming=c.get('inbox',[])
  if incoming and c.get('pending_question_id')==incoming[-1]['external_id'] and match_faq(case,incoming[-1]['text']):
   c.pop('handled_reply_id',None)
 store.audit(case,'job_config_changed',config_revision=case['config_revision'],job_revision=case['job']['revision'],
             before=old,after=view_config(case),platform_write=False)
 return view_config(case)
