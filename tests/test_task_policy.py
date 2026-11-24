from datetime import datetime
from zoneinfo import ZoneInfo
import copy
import pytest
from cloakup.task_policy import DEFAULT_SCHEDULE,checked_schedule,window_status
from cloakup.storage import Database
from cloakup.sourcing import CaseStore
from cloakup.platform import TaskStore
from cloakup.account_execution import AccountExecution


def timestamp(text):return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def test_work_window_lunch_weekend_and_end_boundary():
 cfg=checked_schedule({})
 assert window_status(cfg,timestamp('2026-11-16T09:00:00'))['allowed']
 assert not window_status(cfg,timestamp('2026-11-16T12:00:00'))['allowed']
 assert window_status(cfg,timestamp('2026-11-16T12:00:00'))['next_at']==timestamp('2026-11-16T13:30:00')
 assert not window_status(cfg,timestamp('2026-11-16T18:00:00'))['allowed']
 assert window_status(cfg,timestamp('2026-11-21T10:00:00'))['next_at']==timestamp('2026-11-23T09:00:00')
 with pytest.raises(ValueError):checked_schedule({'windows':[{'start':'09:00','end':'12:00'},{'start':'11:00','end':'13:00'}]})
 with pytest.raises(ValueError):checked_schedule({'weekdays':[True]})


def setup(tmp_path):
 db=Database(tmp_path);cases=CaseStore(tmp_path,db);store=TaskStore(db);ids=[]
 for label in ['甲岗位','乙岗位','丙岗位']:
  c=cases.create('https://www.zhipin.com/web/chat/job-list');cases.bind_job(c,{'title':label,'requirements':label+'事实'});cases.set_autonomy(c,True,source='test');ids.append(c['id'])
 def task(case_ids,**extra):return store.save({'config':{'name':'多岗位寻访','scenario':'寻访','website':'https://www.zhipin.com/','goal':'收取简历','engine':'boss_sourcing','case_ids':case_ids,**extra}})
 return db,cases,store,ids,task


def test_task_policy_isolated_ordered_and_case_facts_retained(tmp_path):
 db,cases,store,ids,task=setup(tmp_path)
 first=task(ids[:2],execution={'nudge_unread_hours':72,'daily_new_limit':4},greeting='您好，方便交流吗？')
 # 不同场景才是另一个任务；同一网站与场景只能有一个任务。
 second=task(ids[2:],scenario='另一场景',execution={'nudge_unread_hours':12})
 assert first['config']['case_ids']==ids[:2]
 for i,cid in enumerate(ids[:2]):
  c=cases.load(cid);assert c['execution']['nudge_unread_hours']==72 and c['autonomy']['daily_new_limit']==4
  assert c['job']['snapshot']['requirements']==['甲岗位事实','乙岗位事实'][i]
  assert c['desired_greeting']=='您好，方便交流吗？'
 assert cases.load(ids[2])['execution']['nudge_unread_hours']==12
 first['config']['execution']['nudge_unread_hours']=48
 store.save({'expected_revision':first['revision'],'config':first['config']},first['id'])
 assert cases.load(ids[1])['execution']['nudge_unread_hours']==48
 assert cases.load(ids[2])['execution']['nudge_unread_hours']==12


def test_same_website_and_scenario_allows_only_one_task(tmp_path):
 # 一个网站 + 一个场景只对应一个任务；新岗位并入该任务，不另建。
 db,cases,store,ids,task=setup(tmp_path)
 first=task(ids[:1])
 with pytest.raises(ValueError,match='不另建任务'):task(ids[1:2])
 # 同一任务自己保存（新增岗位）不受影响。
 cfg=copy.deepcopy(first['config']);cfg['case_ids']=ids[:2]
 assert store.save({'expected_revision':first['revision'],'config':cfg},first['id'])['config']['case_ids']==ids[:2]


def test_imported_jobs_join_the_scope_task_and_published_task_reports_them(tmp_path):
 db,cases,store,ids,task=setup(tmp_path)
 scope_task=task(ids[:1],scenario='候选人寻访与跟进')
 # 未发布的任务：新岗位直接并入，不新建任务。
 assert store.import_cases([cases.load(ids[1])])==[]
 assert store.get(scope_task['id'])['config']['case_ids']==ids[:2] and len(store.list())==1
 # 已启用验证后发布的任务不能直接修改：新岗位返回为未并入，交由草案发布流程处理。
 db.put('agent_release',scope_task['id'],{'id':scope_task['id'],'revision':1,'version_id':'v1','validation':'passed'})
 assert store.import_cases([cases.load(ids[2])])==[ids[2]]
 assert ids[2] not in store.get(scope_task['id'])['config']['case_ids'] and len(store.list())==1


def test_liepin_sourcing_is_its_own_task_and_keeps_boss_jobs_apart(tmp_path):
 db,cases,store,ids,task=setup(tmp_path)
 boss=task(ids[:1],scenario='候选人寻访与跟进')
 liepin=cases.create('https://www.liepin.com/job/example')
 cases.bind_job(liepin,{'title':'猎聘岗位','requirements':'猎聘岗位事实'})
 assert liepin['platform']=='liepin'
 assert store.import_cases([cases.load(liepin['id'])])==[]
 tasks=store.list()
 assert len(tasks)==2
 copied=next(t for t in tasks if t['id']!=boss['id'])
 assert copied['config']['engine']=='liepin_sourcing' and copied['config']['case_ids']==[liepin['id']]
 assert liepin['id'] not in store.get(boss['id'])['config']['case_ids']
 mixed=copy.deepcopy(store.get(boss['id'])['config']);mixed['case_ids']=[*mixed['case_ids'],liepin['id']]
 with pytest.raises(ValueError,match='另一个寻访场景'):
  store.save({'expected_revision':boss['revision'],'config':mixed},boss['id'])
 with pytest.raises(ValueError,match='寻访适配器'):
  store.save({'config':{'name':'网页','scenario':'采集','website':'https://www.zhipin.com/','goal':'采集公开文字','engine':'web'}})


def test_global_legacy_record_does_not_infect_new_case(tmp_path):
 db=Database(tmp_path);db.put('followup_policy','default',{'policy':{'nudge_unread_hours':99}})
 c=CaseStore(tmp_path,db).create('https://www.zhipin.com/web/chat/job-list')
 assert c['execution']['nudge_unread_hours']!=99


def test_break_at_cycle_boundary_and_schedule_gate(tmp_path):
 db,cases,store,ids,task=setup(tmp_path);t=task(ids[:2]);a=AccountExecution(db);a.set_task(t['id'],ids[:2],'running')
 start=timestamp('2026-11-16T09:00:00');assert a.availability(ids[0],start)['allowed']
 # 连续工作时长为配置 50 分钟的 0.75～1.25 倍随机值，63 分钟时必然进入休息。
 a.begin_work(t['id'],start)
 assert 0.75<=next(m for m in a.members() if m['id']==t['id'])['work_factor']<=1.25
 a.finish_cycle(t['id'],start+63*60)
 state=a.availability(ids[1],start+63*60)
 assert not state['allowed'] and state['state']=='resting'
 assert start+68*60<=state['next_at']<=start+73*60
 assert not a.availability(ids[0],timestamp('2026-11-16T20:00:00'))['allowed']
 a.begin_work(t['id'],state['next_at']+1)
 assert a.availability(ids[1],state['next_at']+1)['allowed']


async def test_offhours_account_executor_keeps_session_without_page_work(tmp_path,monkeypatch):
 from types import SimpleNamespace as NS
 from cloakup import autopilot as ap
 db,cases,store,ids,task=setup(tmp_path);t=task(ids[:2]);a=AccountExecution(db);a.set_task(t['id'],ids[:2],'running')
 monkeypatch.setattr('cloakup.account_execution.time.time',lambda:timestamp('2026-11-16T22:00:00'))
 class EndIdle(Exception):pass
 async def sleep(n):raise EndIdle
 async def checkpoint(job):pass
 async def sync(*args):raise AssertionError('offhours must not operate browser')
 monkeypatch.setattr(ap.asyncio,'sleep',sleep);monkeypatch.setattr(ap,'sync_and_follow',sync)
 m=NS(account_execution=a,cases=cases,definitions=store,checkpoint=checkpoint,persist=lambda j:None,event=lambda *a:None)
 job={'params':{}}
 with pytest.raises(EndIdle):await ap.run_account_executor(m,job,object())
 assert job['active_case_id'] is None and job['autopilot_stage']=='outside_window'
 assert a.members()[0]['status']=='running'


async def test_multiple_jobs_traversed_in_configured_order(tmp_path,monkeypatch):
 from types import SimpleNamespace as NS
 from cloakup import autopilot as ap
 db,cases,store,ids,task=setup(tmp_path);ordered=[ids[1],ids[0]];t=task(ordered,schedule={'enabled':False});a=AccountExecution(db);a.set_task(t['id'],ordered,'running');seen=[]
 class EndCycle(Exception):pass
 async def sleep(n):raise EndCycle
 async def checkpoint(job):pass
 async def sync(m,j,s,c):seen.append(c['id']);return 1
 monkeypatch.setattr(ap.asyncio,'sleep',sleep);monkeypatch.setattr(ap,'sync_and_follow',sync)
 m=NS(account_execution=a,cases=cases,definitions=store,checkpoint=checkpoint,persist=lambda j:None,event=lambda *a:None)
 with pytest.raises(EndCycle):await ap.run_account_executor(m,{'params':{'new_contacts':False}},object())
 assert seen==ordered
