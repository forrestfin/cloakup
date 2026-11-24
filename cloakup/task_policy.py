"""Task-owned policies and business-hour gates; no browser keepalive actions."""
import copy,hashlib,json,re,time,uuid
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

DEFAULT_SCHEDULE={'enabled':True,'timezone':'Asia/Shanghai','weekdays':[0,1,2,3,4],
 'windows':[{'start':'09:00','end':'12:00'},{'start':'13:30','end':'18:00'}],
 'work_minutes':50,'rest_min_minutes':5,'rest_max_minutes':10}


def checked_schedule(value):
 if not isinstance(value,dict):raise ValueError('任务时间窗口必须为对象')
 cfg={**copy.deepcopy(DEFAULT_SCHEDULE),**value}
 if set(cfg)!=set(DEFAULT_SCHEDULE) or type(cfg['enabled']) is not bool:raise ValueError('任务时间窗口字段不合法')
 try:ZoneInfo(cfg['timezone'])
 except (ZoneInfoNotFoundError,TypeError):raise ValueError('请选择有效时区')
 days=cfg['weekdays']
 if not isinstance(days,list) or not days or any(type(d) is not int or not 0<=d<=6 for d in days) or len(set(days))!=len(days):raise ValueError('请选择有效工作日')
 if not isinstance(cfg['windows'],list) or not 1<=len(cfg['windows'])<=6:raise ValueError('请设置1至6个每日工作时间段')
 spans=[]
 for window in cfg['windows']:
  if not isinstance(window,dict) or set(window)!={'start','end'}:raise ValueError('时间段格式应为开始和结束时间')
  if any(not isinstance(v,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v) for v in window.values()) or window['start']>=window['end']:raise ValueError('时间段必须同日且开始早于结束')
  spans.append((window['start'],window['end']))
 spans.sort()
 if any(b[0]<a[1] for a,b in zip(spans,spans[1:])):raise ValueError('工作时间段不能重叠')
 for k,low,high in [('work_minutes',10,180),('rest_min_minutes',1,60),('rest_max_minutes',1,90)]:
  if type(cfg[k]) is not int or not low<=cfg[k]<=high:raise ValueError('连续工作与休息分钟数不合法')
 if cfg['rest_min_minutes']>cfg['rest_max_minutes']:raise ValueError('休息最短时间不能大于最长时间')
 cfg['windows']=[{'start':a,'end':b} for a,b in spans]
 return cfg


def normalize_config(db,cfg):
 """Normalize operational rules; legacy business fields remain compatibility only."""
 from .job_config import DEFAULT_EXECUTION,EXECUTION_SCHEMA
 from .boss_recommend import checked_filters
 from .autonomy import validate_reply
 import jsonschema
 cfg['execution']={**copy.deepcopy(DEFAULT_EXECUTION),**copy.deepcopy(cfg.get('execution',{}))}
 # 追聊有独立的白天时段（9–20 点），不跟随执行时段延到深夜；次数与间隔收敛到体验底线。
 from .followup import limits
 cfg['execution'].update(limits(cfg['execution']))
 if 'execution' not in cfg:raise ValueError('缺少执行策略')
 try:jsonschema.validate(cfg['execution'],EXECUTION_SCHEMA)
 except jsonschema.ValidationError as e:raise ValueError('任务执行策略格式不合法：'+e.message) from e
 if cfg['execution']['nudge_start_hour']>=cfg['execution']['nudge_end_hour']:raise ValueError('追聊开始时间必须早于结束时间')
 validate_reply(cfg['execution']['resume_receipt_message'],[],[])
 cfg['schedule']=checked_schedule(cfg.get('schedule',{}))
 cfg.setdefault('recommend_filters',[]);checked_filters(cfg['recommend_filters'])
 cfg.setdefault('greeting','')
 if not isinstance(cfg['greeting'],str) or len(cfg['greeting'])>100:raise ValueError('任务首聊话术最多100字')
 if cfg['greeting']:
  validate_reply(cfg['greeting'],[],[])
  from .greeting_policy import render
  for cid in cfg.get('case_ids',[]):
   render(cfg['greeting'],db.get('case',cid))
 return cfg


def effective_task(db,cid):
 from .scenarios import is_sourcing
 tasks=[t for t in db.list('task') if is_sourcing(t['config'].get('engine')) and cid in t['config'].get('case_ids',[])]
 members={m['id']:m for m in db.list('account_task') if m['status']!='stopped' and cid in m['case_ids']}
 tasks.sort(key=lambda t:(t['id'] in members,members.get(t['id'],{}).get('status')=='running',t.get('updated_at',0)),reverse=True)
 return tasks[0] if tasks else None


def overlay(case,task):
 cfg=task['config']
 if 'execution' not in cfg:return case
 from .job_sourcing import resolve,apply
 profile=resolve(case,cfg)
 case['execution']=copy.deepcopy(cfg['execution'])
 apply(case,profile)
 case['task_policy']={'task_id':task['id'],'revision':task['revision'],'schedule':copy.deepcopy(cfg.get('schedule',DEFAULT_SCHEDULE))}
 case.setdefault('autonomy',{}).update({k:case['execution'][k] for k in ('poll_seconds','daily_new_limit','max_messages_per_candidate_day')})
 return case


def apply_to_cases(db,task):
 for cid in task['config'].get('case_ids',[]):
  try:case=db.get('case',cid)
  except FileNotFoundError:continue
  before={k:copy.deepcopy(case.get(k)) for k in ('execution','recommend_filters','desired_greeting','task_policy')}
  overlay(case,task)
  if before=={k:case.get(k) for k in before}:continue
  case['config_revision']=case.get('config_revision',0)+1
  case.setdefault('autonomy',{})['revision']=case.get('autonomy',{}).get('revision',0)+1
  for p in case.get('candidates',{}).values():
   p.pop('decision_wait_until',None);p.pop('harness_decision',None)
   for m in p.get('outbox',[]):
    if m['status'] in ('draft','approved'):m.update(status='stale',approved_fingerprint=None)
  trail=case.setdefault('audit',[])
  entry={'id':uuid.uuid4().hex,'at':time.time(),'kind':'task_policy_changed','candidate_id':None,'details':{'task_id':task['id'],'task_revision':task['revision'],'reason':'执行策略和追聊规则由所属任务统一管理；岗位只维护事实差异'},'previous_hash':trail[-1]['hash'] if trail else None}
  entry['hash']=hashlib.sha256(json.dumps(entry,sort_keys=True,ensure_ascii=False).encode()).hexdigest();trail.append(entry)
  case['updated_at']=time.time();db.put('case',cid,case)


def window_status(schedule,now=None):
 cfg=schedule or DEFAULT_SCHEDULE;now=time.time() if now is None else now
 if not cfg.get('enabled',True):return {'allowed':True,'state':'open','next_at':None}
 local=datetime.fromtimestamp(now,ZoneInfo(cfg['timezone']));clock=local.strftime('%H:%M')
 if local.weekday() in cfg['weekdays'] and any(w['start']<=clock<w['end'] for w in cfg['windows']):return {'allowed':True,'state':'open','next_at':None}
 starts=[]
 for days in range(8):
  day=local+timedelta(days=days)
  if day.weekday() not in cfg['weekdays']:continue
  for w in cfg['windows']:
   h,m=map(int,w['start'].split(':'));start=day.replace(hour=h,minute=m,second=0,microsecond=0).timestamp()
   if start>now:starts.append(start)
 return {'allowed':False,'state':'outside_window','reason':'当前不在任务执行时段，保留登录资料，不操作网站','next_at':min(starts) if starts else None}
