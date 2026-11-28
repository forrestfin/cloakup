import asyncio
import time
from types import SimpleNamespace as NS
import pytest
from cloakup.account_execution import AccountExecution
from cloakup.storage import Database
from cloakup.workbench import Workbench
from test_agent_workspace import setup,two_jobs,publish_validated,edit_release  # noqa: F401  setup 是 fixture


def test_independent_pause_persistence_and_truthful_live_owner(tmp_path):
 a=AccountExecution(Database(tmp_path))
 a.set_task('first',['one'],'running');a.set_task('second',['two'],'running')
 a.set_task('first',['one'],'paused')
 assert not a.can_send('one') and a.can_send('two')
 assert a.case_ids()==['two']
 restored=AccountExecution(Database(tmp_path))
 assert restored.members()[0]['history']
 assert restored.view(None,False)['status']=='idle'
 assert restored.view({'id':'owner','status':'running','active_case_id':'two'},True)['active_case_id']=='two'


def test_two_tasks_join_one_executor_and_pause_does_not_stop_other(tmp_path,monkeypatch):
 m=Workbench(tmp_path);started=[];gate=None
 async def execute(job,recipe):
  started.append(job['id']);job['status']='running'
  try:await asyncio.Event().wait()
  finally:job['status']='cancelled'
 monkeypatch.setattr(m,'execute',execute)
 async def generate(key):return {}
 monkeypatch.setattr(m,'generate_definition_plan',generate)
 async def scenario():
  ids=[]
  for name in ['岗位一','岗位二']:
   c=m.cases.create('https://www.zhipin.com/web/chat/job-list');m.cases.bind_job(c,{'title':name,'requirements':'招聘'})
   m.cases.set_autonomy(c,True,source='test')
   # 同一网站的不同场景才是不同任务；同一场景只能一个任务。
   d=m.definitions.save({'config':{'name':name,'schedule':{'enabled':False},'website':'https://www.zhipin.com/','scenario':'寻访-'+name,'goal':'取得简历','engine':'boss_sourcing','sourcing_mode':'quota','case_ids':[c['id']]}})
   ids.append((d['id'],c['id']))
  r1=await m.start_account_task(ids[0][0]);await asyncio.sleep(0)
  r2=await m.start_account_task(ids[1][0]);assert r1['id']==r2['id']
  await m.account_task_action(ids[0][0],{'action':'pause'})
  assert not m.account_execution.can_send(ids[0][1]) and m.account_execution.can_send(ids[1][1])
  assert len(started)==1
  await m.account_task_action(ids[0][0],{'action':'resume'})
  assert m.account_execution.can_send(ids[0][1])
  await m.action(r1['id'],{'action':'takeover'})
  assert m.jobs[r1['id']]['manual_takeover'] and m.jobs[r1['id']]['pause_requested']
 try:m.call(scenario())
 finally:m.close()


async def test_unseen_resume_offer_skips_outreach_until_inbox_returns(tmp_path,monkeypatch):
 from cloakup import autopilot as ap
 a=AccountExecution(Database(tmp_path));a.set_task('task',['case'],'running')
 case={'id':'case','autonomy':{'enabled':True,'poll_seconds':120},'execution':{'new_contacts':True},'candidates':{}}
 contacts=[];events=[]
 async def checkpoint(job):pass
 async def sync(manager,job,session,loaded):
  job.setdefault('inbound_resume_cases',[]).append(loaded['id']);return 1
 async def contact(*args):contacts.append(args[-1]['id'])
 class EndPass(Exception):pass
 async def sleep(seconds):raise EndPass
 monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap,'first_contact',contact);monkeypatch.setattr(ap.asyncio,'sleep',sleep)
 manager=NS(account_execution=a,cases=NS(load=lambda cid:case),definitions=NS(get=lambda tid:{'config':{'target_resumes':2}}),checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:events.append(args[-1]))
 job={'params':{'new_contacts':True}}
 with pytest.raises(EndPass):await ap.run_account_executor(manager,job,object())
 assert contacts==[] and any('同意接收' in text for text in events)


async def test_account_pass_prioritizes_all_inboxes_and_paused_job_is_read_only(tmp_path,monkeypatch):
 from cloakup import autopilot as ap
 a=AccountExecution(Database(tmp_path));a.set_task('task-one',['one'],'paused');a.set_task('task-two',['two'],'running')
 cases={cid:{'id':cid,'autonomy':{'enabled':True},'candidates':{}} for cid in ['one','two']}
 calls=[]
 async def checkpoint(job):pass
 async def sync(manager,job,session,case):calls.append((case['id'],job['params']['read_only']));return 1
 class EndPass(Exception):pass
 async def sleep(seconds):raise EndPass
 monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap.asyncio,'sleep',sleep)
 manager=NS(account_execution=a,cases=NS(load=lambda cid:cases[cid]),definitions=NS(get=lambda tid:{'config':{'target_resumes':2}}),checkpoint=checkpoint,persist=lambda job:None)
 job={'params':{'new_contacts':False}}
 with pytest.raises(EndPass):await ap.run_account_executor(manager,job,object())
 assert calls==[('two',False)]
 assert job['active_case_id'] is None


async def test_all_paused_executor_has_heartbeat_but_no_browser_work(tmp_path,monkeypatch):
 from cloakup import autopilot as ap
 a=AccountExecution(Database(tmp_path));a.set_task('one',['case'],'paused')
 class EndIdle(Exception):pass
 async def sleep(n):raise EndIdle
 async def checkpoint(job):pass
 async def sync(*args):raise AssertionError('paused task must not read browser')
 monkeypatch.setattr(ap.asyncio,'sleep',sleep);monkeypatch.setattr(ap,'sync_and_follow',sync)
 manager=NS(account_execution=a,checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:None)
 job={'params':{}}
 with pytest.raises(EndIdle):await ap.run_account_executor(manager,job,object())
 assert job['autopilot_stage']=='standby' and job['active_case_id'] is None and job['next_check_at'] is None
 assert a.view({'id':'owner','status':'running'},True)['status']=='standby'


def test_completed_goal_cannot_start_until_target_increased(tmp_path,monkeypatch):
 m=Workbench(tmp_path)
 async def scenario():
  c=m.cases.create('https://www.zhipin.com/web/chat/job-list');m.cases.bind_job(c,{'title':'岗位','requirements':'招聘'})
  m.cases.set_autonomy(c,True,source='test');p=m.cases.add_candidate(c,'person','https://www.zhipin.com/web/chat/index',{'summary':'职业经历'});m.cases.attach_resume(c,p['id'],'platform-message:file')
  d=m.definitions.save({'config':{'name':'任务','website':'https://www.zhipin.com/','scenario':'寻访','goal':'取得简历','engine':'boss_sourcing','sourcing_mode':'quota','case_ids':[c['id']]}})
  m.account_execution.set_task(d['id'],[c['id']],'completed')
  history=list(m.account_execution.members()[0]['history'])
  with pytest.raises(ValueError,match='请先调整目标人数'):
   await m.start_account_task(d['id'])
  assert m.account_execution.members()[0]['status']=='completed' and m.account_execution.members()[0]['history']==history
  d['config']['target_resumes']=2;m.definitions.save({'expected_revision':d['revision'],'config':d['config']},d['id'])
  async def create(*a):return {'id':'new-owner'}
  m.jobs['new-owner']={'id':'new-owner','status':'succeeded'};monkeypatch.setattr(m,'create',create)
  await m.start_account_task(d['id']);assert m.account_execution.members()[0]['status']=='running'
 try:m.call(scenario())
 finally:m.close()


async def test_continuous_task_remains_running_after_any_resume_count(tmp_path,monkeypatch):
 from cloakup import autopilot as ap
 a=AccountExecution(Database(tmp_path));a.set_task('continuous',['case'],'running')
 case={'id':'case','autonomy':{'enabled':True},'execution':{},'candidates':{'p':{'id':'p','resumes':['file']}}}
 async def checkpoint(*a):pass
 async def sync(*a):return 1
 class EndPass(Exception):pass
 async def sleep(*a):raise EndPass
 monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap.asyncio,'sleep',sleep)
 manager=NS(account_execution=a,cases=NS(load=lambda cid:case),definitions=NS(get=lambda tid:{'config':{'sourcing_mode':'continuous','target_resumes':1}}),checkpoint=checkpoint,persist=lambda job:None)
 with pytest.raises(EndPass):await ap.run_account_executor(manager,{'params':{'new_contacts':False}},object())
 assert a.members()[0]['status']=='running'


@pytest.mark.parametrize('error,stops',[('未找到唯一的实际页面控件：推荐',False),('平台出现校验，自动沟通已停止',True),('BOSS 已显示登录表单，请在专用窗口重新登录后继续',True)])
async def test_outreach_entry_failure_does_not_stop_the_inbox(tmp_path,monkeypatch,error,stops):
 # 11-24 13:35：推荐页卡在“加载中”，开聊报“未找到唯一的实际页面控件：推荐”，整个任务被停，收件也跟着停了。
 from cloakup import autopilot as ap
 from cloakup.sourcing import SourcingError
 a=AccountExecution(Database(tmp_path));a.set_task('task',['case'],'running')
 case={'id':'case','autonomy':{'enabled':True,'poll_seconds':120},'execution':{'new_contacts':True},'candidates':{}}
 events=[]
 async def checkpoint(job):pass
 async def sync(manager,job,session,loaded):return 1
 async def contact(*args):raise SourcingError(error)
 class EndPass(Exception):pass
 async def sleep(seconds):raise EndPass
 monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap,'first_contact',contact);monkeypatch.setattr(ap.asyncio,'sleep',sleep)
 import cloakup.greeting_policy as gp
 monkeypatch.setattr(gp,'key',lambda *a:'k')
 manager=NS(account_execution=a,db=NS(get=lambda *a:{}),cases=NS(load=lambda cid:case),definitions=NS(get=lambda tid:{'config':{'target_resumes':2}}),checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:events.append(args[-1]))
 job={'params':{'new_contacts':True},'outreach_deferrals':{'2000-01-01':{'case':5}}}
 if stops:
  with pytest.raises(SourcingError):await ap.run_account_executor(manager,job,object())
 else:
  with pytest.raises(EndPass):await ap.run_account_executor(manager,job,object())
  assert any('开聊入口暂不可用' in text for text in events)
  # 11-24 Task 9：当天该岗位入口失败次数记在执行器记录上，attention 累计到 3 次生成待办；旧日期的计数在写入时清掉
  from cloakup.autonomy import day_key
  assert job['outreach_deferrals']=={day_key():{'case':1}}


async def test_each_cycle_checks_the_current_boss_page(monkeypatch):
 from cloakup import autopilot as ap, page_health
 seen=[]
 async def heal(session,kind):seen.append(kind);return {'healthy':True}
 monkeypatch.setattr(page_health,'heal',heal)
 await ap.check_page_health(NS(page=NS(url='https://www.zhipin.com/web/chat/index')))
 await ap.check_page_health(NS(page=NS(url='about:blank')))
 await ap.check_page_health(object())
 assert seen==['chat']


def test_unvalidated_job_stops_alone(setup):
 # 岗位 b 的事实在验收后变了：只停 b 的发送，a 照常；任务不暂停。
 lab,m,t,c=setup;other=two_jobs(lab,m,t,c);release,_=publish_validated(lab,t)
 edit_release(lab,t,lambda b:b['sources'][1]['facts'].append({'kind':'duties','value':'新职责','status':'confirmed'}))
 a=m.account_execution;a.set_task(t['id'],[c['id'],other['id']],'running')
 assert a.availability(c['id'])['state']!='job_not_validated' and a.availability(c['id'])['state']!='release_changed'
 assert a.availability(other['id'])=={'allowed':False,'state':'job_not_validated','reason':'该岗位验收已失效，只停这个岗位','next_at':None}
 m.db.put('agent_release',t['id'],{**m.db.get('agent_release',t['id']),'version_id':'other'})
 assert a.availability(c['id'])['state']=='release_changed'


async def test_lapsed_job_validation_is_reported_as_its_own_stage(tmp_path,monkeypatch):
 from cloakup import autopilot as ap
 a=AccountExecution(Database(tmp_path));a.set_task('task',['b'],'running')
 monkeypatch.setattr(a,'availability',lambda cid,now=None:{'allowed':False,'state':'job_not_validated','reason':'该岗位验收已失效，只停这个岗位','next_at':None})
 events=[]
 async def checkpoint(job):pass
 class EndPass(Exception):pass
 async def sleep(seconds):raise EndPass
 monkeypatch.setattr(ap.asyncio,'sleep',sleep)
 manager=NS(account_execution=a,cases=NS(load=lambda cid:{'id':cid,'autonomy':{'enabled':True},'candidates':{}}),definitions=NS(get=lambda tid:{'config':{'target_resumes':2}}),
            checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:events.append(args[-1]))
 job={'params':{'new_contacts':True}}
 with pytest.raises(EndPass):await ap.run_account_executor(manager,job,object())
 assert job['autopilot_stage']=='job_not_validated' and events==['部分岗位验收已失效，这些岗位暂停发送，其他岗位继续']


def _outreach_env(tmp_path,monkeypatch,tasks,rounds,outcome=lambda cid:'contacted'):
 # 首聊调度：tasks 为 [(任务编号,成员表,每日总额)]；first_contact 用替身，入口照常按任务额度复核，成功时在岗位上记一次当天占位。
 # 时钟由 sleep 推进，跑满 rounds 轮后结束；任务当天目标固定为总额，便于按份额断言。
 from cloakup import autopilot as ap, job_quota
 import cloakup.greeting_policy as gp
 a=AccountExecution(Database(tmp_path));defs={};cases={}
 for tid,members,limit in tasks:
  ids=[m['case_id'] for m in members];a.set_task(tid,ids,'running')
  defs[tid]={'id':tid,'config':{'members':members,'case_ids':ids,'execution':{'daily_new_limit':limit},'target_resumes':99}}
  for cid in ids:cases[cid]={'id':cid,'autonomy':{'enabled':True,'poll_seconds':120},'execution':{'new_contacts':True},'candidates':{}}
 store=NS(load=lambda cid:cases[cid],list=lambda:list(cases.values()))
 clock=[1_000_000.0];synced=[];contacts=[];events=[];attempts=[]
 monkeypatch.setattr(ap,'time',NS(time=lambda:clock[0]))
 monkeypatch.setattr(job_quota,'task_target',lambda tid,limit,day:limit)
 monkeypatch.setattr(ap,'random',NS(randint=lambda lo,hi:1))
 monkeypatch.setattr(gp,'key',lambda *a:'k')
 async def checkpoint(job):pass
 async def sync(manager,job,session,case):synced.append(case['id']);return 1
 async def contact(manager,job,session,case):
  attempts.append(case['id']);task=next(t for t in defs.values() if case['id'] in t['config']['case_ids'])
  if not job_quota.allowed(store,task,case['id'],ap.day_key()):return 'blocked'
  result=outcome(case['id'])
  if result=='contacted':
   contacts.append(case['id'])
   case['candidates']['p%d'%len(contacts)]={'outbox':[{'intent':'first_contact','status':'sent','claimed_at':time.time()}]}
  return result
 class EndPass(Exception):pass
 job={'params':{'new_contacts':True}}
 async def sleep(seconds):
  clock[0]+=1000
  if job.get('autopilot_cycle',0)>=rounds[0]:raise EndPass
 monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap,'first_contact',contact);monkeypatch.setattr(ap.asyncio,'sleep',sleep)
 manager=NS(account_execution=a,db=NS(get=lambda *a:{}),cases=store,definitions=NS(get=lambda tid:defs[tid]),checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:events.append(args[-1]))
 async def run():
  with pytest.raises(EndPass):await ap.run_account_executor(manager,job,object())
 return NS(job=job,run=run,synced=synced,contacts=contacts,attempts=attempts,events=events,cases=cases,rounds=rounds)


def _job(cid,weight=1,outreach='active'):
 return {'case_id':cid,'weight':weight,'outreach':outreach}


async def test_outreach_picks_job_by_weight_share(tmp_path,monkeypatch):
 # 权重 a=3、b=1，任务当天目标 8：份额 6:2；多轮后按份额分配，任务合计不超过目标。
 env=_outreach_env(tmp_path,monkeypatch,[('t1',[_job('a',3),_job('b',1)],8)],[20])
 await env.run()
 assert env.contacts.count('a')==6 and env.contacts.count('b')==2 and len(env.contacts)==8


async def test_exhausted_job_yields_to_others_until_next_day(tmp_path,monkeypatch):
 # a 推荐页今天没人可联系：当天不再尝试，额度让给 b，直到任务目标；次日 a 重新参与。
 from cloakup import autopilot as ap
 calls=[]
 def outcome(cid):calls.append(cid);return 'no_candidates' if cid=='a' else 'contacted'
 env=_outreach_env(tmp_path,monkeypatch,[('t1',[_job('a'),_job('b')],4)],[12],outcome)
 await env.run()
 today=ap.day_key()
 assert calls.count('a')==1 and env.contacts==['b']*4
 assert env.job['outreach_exhausted']=={today:['a']} and env.events.count('该岗位今天推荐页暂无可联系的人，额度让给其他岗位')==1
 monkeypatch.setattr(ap,'day_key',lambda now=None:'2099-01-01')
 env.rounds[0]+=2;del env.job['autopilot_cycle']
 calls.clear();await env.run()
 assert 'a' in calls and today not in env.job['outreach_exhausted']


async def test_screened_out_job_yields_for_now_but_not_for_the_day(tmp_path,monkeypatch):
 # 11-27 首聊前初筛：a 本批审阅的人都不符合必须项，先让 b 开聊、约 30 分钟后再看 a；不算“今天没人可联系”，不让出当天额度。
 from cloakup import autopilot as ap,logbook
 logged=[];monkeypatch.setattr(logbook,'log',lambda *a,**kw:logged.append(kw))
 env=_outreach_env(tmp_path,monkeypatch,[('t1',[_job('a'),_job('b')],4)],[4],lambda cid:'screened_out' if cid=='a' else 'contacted')
 # 替身时钟每次 sleep 前进很多；把 30 分钟的让位放大，确认让位期间 a 不再被选中。
 waits=[];monkeypatch.setattr(ap,'jitter',lambda s,*a:waits.append(s) or (s*10**4 if s==1800 else s))
 await env.run()
 assert 1800 in waits and env.attempts.count('a')==1 and env.contacts==['b','b']
 assert not env.job.get('outreach_exhausted')
 assert env.events.count('该岗位本批审阅的人不符合必须项或初筛暂不可用，先让其他岗位开聊，约 30 分钟后再看')==1
 assert any(e.get('type')=='outreach_screened_out' and e.get('case_id')=='a' and e.get('task_id')=='t1' and e.get('actor')=='executor' and e.get('reason')=='all_reviewed_rejected_or_unscreened' for e in logged)


async def test_view_cap_drains_job_for_the_rest_of_the_day(tmp_path,monkeypatch):
 # 打开资料到当天上限：执行器当天不再为该岗位进入推荐页（不再调用首聊/打开列表），首聊额度让给其他岗位；次日恢复。
 from cloakup import autopilot as ap
 env=_outreach_env(tmp_path,monkeypatch,[('t1',[_job('a'),_job('b')],4)],[8],lambda cid:'views_exhausted' if cid=='a' else 'contacted')
 await env.run()
 assert env.attempts.count('a')==1 and env.contacts==['b']*4
 assert env.job['outreach_exhausted']=={ap.day_key():['a']}


async def test_withdrawn_job_still_syncs_inbox_but_no_outreach(tmp_path,monkeypatch):
 # 撤下的 b：收件照常同步与回复，从不进入首聊；额度全部给 a。
 env=_outreach_env(tmp_path,monkeypatch,[('t1',[_job('b',5,'withdrawn'),_job('a')],3)],[8])
 await env.run()
 assert 'b' in env.synced and 'b' not in env.attempts and env.contacts==['a']*3


async def test_no_round_ends_after_first_success(tmp_path,monkeypatch):
 # a 首聊成功后，同一轮里另一任务的 b 仍会被调度，不再“成功一次就结束本轮”。
 env=_outreach_env(tmp_path,monkeypatch,[('t1',[_job('a')],5),('t2',[_job('b')],5)],[1])
 await env.run()
 assert sorted(env.contacts)==['a','b']


async def test_temporarily_unavailable_job_keeps_its_share(tmp_path,monkeypatch):
 # a 暂时不能开聊（开关关闭、隔离、话术核验未到期等）不是“今天没人可联系”：b 用完自己的份额就停，不占 a 的额度。
 env=_outreach_env(tmp_path,monkeypatch,[('t1',[_job('a',3),_job('b',1)],8)],[10])
 env.cases['a']['execution']['new_contacts']=False
 await env.run()
 assert env.contacts==['b','b'] and 'a' not in env.attempts and not env.job.get('outreach_exhausted')


@pytest.mark.parametrize('seed',['oops',{'2000-01-01':'bad'},None])
async def test_outreach_deferral_counter_tolerates_malformed_record(tmp_path,monkeypatch,seed):
 # 复审 Minor 4：执行器记录上的计数被写坏（旧版本或手改）时不崩，按当天重新计数
 from cloakup import autopilot as ap
 from cloakup.sourcing import SourcingError
 from cloakup.autonomy import day_key
 a=AccountExecution(Database(tmp_path));a.set_task('task',['case'],'running')
 case={'id':'case','autonomy':{'enabled':True,'poll_seconds':120},'execution':{'new_contacts':True},'candidates':{}}
 async def checkpoint(job):pass
 async def sync(manager,job,session,loaded):return 1
 async def contact(*args):raise SourcingError('未找到唯一的实际页面控件：推荐')
 class EndPass(Exception):pass
 async def sleep(seconds):raise EndPass
 monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap,'first_contact',contact);monkeypatch.setattr(ap.asyncio,'sleep',sleep)
 import cloakup.greeting_policy as gp
 monkeypatch.setattr(gp,'key',lambda *a:'k')
 manager=NS(account_execution=a,db=NS(get=lambda *a:{}),cases=NS(load=lambda cid:case),definitions=NS(get=lambda tid:{'config':{'target_resumes':2}}),checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:None)
 job={'params':{'new_contacts':True},'outreach_deferrals':seed}
 with pytest.raises(EndPass):await ap.run_account_executor(manager,job,object())
 assert job['outreach_deferrals']=={day_key():{'case':1}}
