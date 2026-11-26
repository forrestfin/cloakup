"""11-25 Task 7：读取/绑定 BOSS 岗位的浏览器代办。执行器运行中只在安全点、执行时段内做，一轮一条；失败只标记这一条。"""
import asyncio
import time
from types import SimpleNamespace as NS

import pytest

from cloakup import boss_jobs as bj
from cloakup import browser_errands as be
from cloakup.account_execution import AccountExecution
from cloakup.attention import AttentionStore
from cloakup.platform import TaskStore
from cloakup.sourcing import CaseStore, SourcingError
from cloakup.storage import Database

ID_A='abcDEF123456~~';ID_B='xyzXYZ987654__';ID_C='qwePOI555666--'
CLOSED={'enabled':True,'timezone':'Asia/Shanghai','weekdays':[],'windows':[{'start':'09:00','end':'10:00'}]}


class Session:
    def __init__(self):self.navigations=[];self.page=NS(url=bj.CHAT_URL,frames=[])
    async def navigate(self,url):self.navigations.append(url);self.page.url=url


@pytest.fixture
def env(tmp_path,monkeypatch):
    db=Database(tmp_path);cases=CaseStore(tmp_path,db);store=TaskStore(db);account=AccountExecution(db)
    bound=cases.create(bj.job_url(ID_B),'交付顾问');cases.bind_job(bound,{'title':'交付顾问','requirements':'职业要求'});bound['search_plan']={'greeting':'您好'};cases.save(bound)
    other=cases.create(bj.job_url(ID_C),'运营专员');cases.bind_job(other,{'title':'运营专员','requirements':'职业要求'});other['search_plan']={'greeting':'您好'};cases.save(other)
    task=store.save({'config':{'name':'寻访任务','website':'https://www.zhipin.com/','scenario':'寻访','goal':'取得简历','engine':'boss_sourcing','case_ids':[bound['id']],'schedule':{'enabled':False}}})
    # 同一网站只能有一个寻访任务；另一个任务直接写库，只用来标注“由其他任务管理”。
    task2={**task,'id':'2'*32,'config':{**task['config'],'name':'另一任务','case_ids':[other['id']]}};db.put('task',task2['id'],task2)
    account.set_task(task['id'],[bound['id']],'running');account.set_task(task2['id'],[other['id']],'paused')
    state={'busy':None,'created':[],'events':[],'binds':[],'bind_error':None,'read_error':None,'stub_error':None,'chat':{'healthy':True,'reason':None,'has_draft':False}}
    async def create(body):state['created'].append(body);return {'id':'r'*32}
    async def bind_boss_job(job,session,url,case_id=None,encrypt_id=None):
        state['binds'].append((url,case_id))
        if state['bind_error']:raise state['bind_error']
        case=cases.load(case_id) if case_id else cases.create(url,'数据顾问')
        cases.bind_job(case,{'title':'数据顾问','requirements':'职业要求'})
        if state['stub_error']:raise state['stub_error']  # 已建档、计划草稿未生成
        case=cases.load(case['id']);case['search_plan']={'greeting':'您好'};cases.save(case)
        return {'case_id':case['id'],'job_title':'数据顾问','summary':'岗位已绑定'}
    manager=NS(root=tmp_path,db=db,cases=cases,definitions=store,account_execution=account,busy=lambda:state['busy'],create=create,jobs={},
               bind_boss_job=bind_boss_job,event=lambda job,kind,message:state['events'].append(message),persist=lambda job:None)
    async def read_published(session):
        if state['read_error']:raise state['read_error']
        await session.navigate(bj.CHAT_URL)
        return {'jobs':[{'encrypt_id':ID_A,'title':'数据顾问','detail':'上海','status':'open','status_text':'开放中','recommendable':True,'option_text':'数据顾问 上海'},
                        {'encrypt_id':ID_B,'title':'交付顾问','detail':'北京','status':'open','status_text':'开放中','recommendable':True,'option_text':'交付顾问 北京'},
                        {'encrypt_id':ID_C,'title':'运营专员','detail':'杭州','status':'open','status_text':'开放中','recommendable':True,'option_text':'运营专员 杭州'},
                        {'encrypt_id':None,'title':'测试岗','detail':'深圳','status':'open','status_text':'开放中','recommendable':False,'option_text':None}],
                'read_at':time.time(),'truncated':False,'option_count':3,'row_count':4}
    async def guard(session):return None
    async def check(session,kind):assert kind=='chat';state.setdefault('checks',0);state['checks']+=1;return state['chat']
    from cloakup import page_health
    monkeypatch.setattr(bj,'read_published',read_published);monkeypatch.setattr(bj,'_guard',guard);monkeypatch.setattr(page_health,'check',check)
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-key')
    return NS(m=manager,state=state,task=task,task2=task2,bound=bound,other=other,db=db)


def executor(**extra):return {'id':'e'*32,'account_runner':True,'status':'running',**extra}


async def _read(env):
    env.state['busy']=executor()
    await be.request_refresh(env.m)
    return await be.at_safe_point(env.m,env.state['busy'],Session())


async def test_refresh_runs_directly_when_no_executor(env):
    record=await be.request_refresh(env.m)
    assert record['kind']=='job_list' and record['status']=='pending'
    assert env.state['created']==[{'mode':'boss_errand','params':{}}]


async def test_refresh_waits_for_the_executor_safe_point(env):
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    assert record['status']=='pending' and env.state['created']==[]
    again=await be.request_refresh(env.m)
    assert again['id']==record['id']  # 同一读取只排一条


async def test_refresh_is_refused_while_another_browser_operation_runs(env):
    env.state['busy']={'id':'x'*32,'mode':'boss_contact','status':'running'}
    with pytest.raises(ValueError,match='浏览器'):await be.request_refresh(env.m)
    assert env.db.list(be.KIND)==[]


async def test_safe_point_runs_one_errand_per_cycle_and_returns_to_chat(env):
    env.state['busy']=executor()
    await be.request_refresh(env.m);await be.request_refresh(env.m)
    done=await be.at_safe_point(env.m,env.state['busy'],Session())
    assert [r['status'] for r in done]==['done'] and be.latest(env.db)['jobs'][0]['encrypt_id']==ID_A
    assert any('浏览器代办已完成' in e for e in env.state['events'])


async def test_safe_point_does_nothing_outside_the_execution_window(env):
    for t in (env.task,env.task2):
        current=env.db.get('task',t['id']);env.db.put('task',t['id'],{**current,'config':{**current['config'],'schedule':CLOSED}})
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    assert await be.at_safe_point(env.m,env.state['busy'],Session())==[]
    assert env.db.get(be.KIND,record['id'])['status']=='pending'


async def test_safe_point_skips_during_manual_takeover_or_a_held_browser_lock(env):
    env.state['busy']=executor(manual_takeover=True)
    await be.request_refresh(env.m)
    assert await be.at_safe_point(env.m,env.state['busy'],Session())==[]
    env.m._browser_diagnostic_lock=asyncio.Lock()
    async with env.m._browser_diagnostic_lock:
        assert await be.at_safe_point(env.m,executor(),Session())==[]


async def test_bind_accepts_only_recommendable_unbound_ids_from_the_latest_read(env):
    with pytest.raises(ValueError,match='刷新'):await be.request_bind(env.m,{'encrypt_ids':[ID_A]})
    await _read(env)
    with pytest.raises(ValueError,match='已绑定'):await be.request_bind(env.m,{'encrypt_ids':[ID_B]})
    with pytest.raises(ValueError,match='最近一次'):await be.request_bind(env.m,{'encrypt_ids':['zzzzzz999999']})
    with pytest.raises(ValueError):await be.request_bind(env.m,{'encrypt_ids':[]})
    with pytest.raises(ValueError):await be.request_bind(env.m,{'encrypt_ids':['bad"id']})
    records=await be.request_bind(env.m,{'encrypt_ids':[ID_A]})
    assert [r['kind'] for r in records]==['job_bind'] and records[0]['job_url']==bj.job_url(ID_A) and records[0]['title']=='数据顾问'
    with pytest.raises(ValueError,match='等待执行'):await be.request_bind(env.m,{'encrypt_ids':[ID_A]})


async def test_bind_runs_through_the_existing_binding_and_marks_the_errand_done(env):
    await _read(env)
    await be.request_bind(env.m,{'encrypt_ids':[ID_A]})
    session=Session()
    done=await be.at_safe_point(env.m,env.state['busy'],session)
    assert env.state['binds']==[(bj.job_url(ID_A),None)] and done[0]['status']=='done' and done[0]['result']['case_id']
    assert session.navigations[-1]==bj.CHAT_URL
    view=be.view(env.m)
    assert next(j for j in view['jobs'] if j['encrypt_id']==ID_A)['bound_case_id']==done[0]['result']['case_id']


async def test_failure_marks_only_that_errand_and_opens_a_todo_that_retry_closes(env):
    await _read(env)
    env.state['bind_error']=SourcingError('岗位名称或完整 JD 未读到')
    first=(await be.request_bind(env.m,{'encrypt_ids':[ID_A]}))[0]
    await be.request_refresh(env.m)
    done=await be.at_safe_point(env.m,env.state['busy'],Session())
    assert done[0]['status']=='failed' and 'JD 未读到' in done[0]['error']
    assert [r['status'] for r in env.db.list(be.KIND) if r['kind']=='job_list' and r['status']!='done']==['pending']
    assert any('浏览器代办未完成' in e for e in env.state['events'])
    todos=[t for t in AttentionStore(env.db).reconcile([],env.m.cases.list()) if t['kind']=='browser_errand']
    assert len(todos)==1 and '数据顾问' in todos[0]['detail'] and 'JD 未读到' in todos[0]['detail'] and todos[0]['status']=='pending'
    env.state['bind_error']=None
    retry=(await be.request_bind(env.m,{'encrypt_ids':[ID_A]}))[0]
    assert retry['id']!=first['id']
    todo=next(t for t in AttentionStore(env.db).reconcile([],env.m.cases.list()) if t['kind']=='browser_errand')
    assert todo['status']=='resolved'


async def test_page_level_problem_fails_the_errand_and_stops_the_executor(env):
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    env.state['read_error']=SourcingError('BOSS 已显示登录表单，请在专用窗口重新登录后继续；未读取岗位')
    with pytest.raises(SourcingError,match='登录'):await be.at_safe_point(env.m,env.state['busy'],Session())
    assert env.db.get(be.KIND,record['id'])['status']=='failed'


async def test_direct_run_processes_every_pending_errand(env):
    env.state['busy']=None
    await be.request_refresh(env.m)
    env.state['busy']={'id':'r'*32,'mode':'boss_errand','status':'running'}
    result=await be.run_direct(env.m,env.state['busy'],Session())
    assert result['status']=='succeeded' and be.latest(env.db)
    await be.request_bind(env.m,{'encrypt_ids':[ID_A]})  # 代办运行中：排队，由同一运行接着做
    result=await be.run_direct(env.m,env.state['busy'],Session())
    assert result['status']=='succeeded' and all(r['status']=='done' for r in env.db.list(be.KIND))


async def test_view_annotates_bound_task_ownership_and_unrecommendable_rows(env):
    await _read(env)
    view=be.view(env.m,env.task['id'])
    jobs={j['title']:j for j in view['jobs']}
    assert jobs['交付顾问']['bound_case_id']==env.bound['id'] and jobs['交付顾问']['in_task'] and not jobs['交付顾问']['other_task']
    assert jobs['运营专员']['other_task'] and not jobs['运营专员']['in_task'] and not jobs['运营专员']['bindable']
    assert jobs['数据顾问']['bindable'] and not jobs['测试岗']['bindable'] and jobs['测试岗']['recommendable'] is False
    assert view['read_at'] and view['pending']==[]


async def test_standby_executor_runs_an_errand_at_its_safe_point(env,monkeypatch):
    from cloakup import autopilot as ap
    env.m.account_execution.set_task(env.task['id'],[env.bound['id']],'paused')
    class EndIdle(Exception):pass
    async def sleep(n):raise EndIdle
    async def checkpoint(job):pass
    monkeypatch.setattr(ap.asyncio,'sleep',sleep);env.m.checkpoint=checkpoint
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    with pytest.raises(EndIdle):await ap.run_account_executor(env.m,{'id':'e'*32,'params':{}},Session())
    assert env.db.get(be.KIND,record['id'])['status']=='done'


async def test_running_executor_does_one_errand_before_the_inbox_pass(env,monkeypatch):
    from cloakup import autopilot as ap
    order=[]
    class EndPass(Exception):pass
    async def sleep(n):raise EndPass
    async def checkpoint(job):pass
    async def health(session):order.append('health')
    async def sync(manager,job,session,case):order.append('sync');return 1
    async def read_published(session):order.append('errand');raise SourcingError('职位管理列表未加载出来，未读取岗位')
    monkeypatch.setattr(ap.asyncio,'sleep',sleep);monkeypatch.setattr(ap,'check_page_health',health);monkeypatch.setattr(ap,'sync_and_follow',sync)
    monkeypatch.setattr(bj,'read_published',read_published);env.m.checkpoint=checkpoint
    monkeypatch.setattr(env.m.account_execution,'can_send',lambda cid:True)
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    with pytest.raises(EndPass):await ap.run_account_executor(env.m,{'id':'e'*32,'params':{'new_contacts':False}},Session())
    # 非页面级失败只标记这一条，收件照常
    assert order[:3]==['errand','health','sync'] and env.db.get(be.KIND,record['id'])['status']=='failed'


async def test_errand_waiting_too_long_opens_a_todo_that_closes_once_done(env):
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    env.db.put(be.KIND,record['id'],{**record,'at':time.time()-be.STUCK_SECONDS-5})
    assert any(t['kind']=='browser_errand_stuck' for t in AttentionStore(env.db).reconcile([],env.m.cases.list()))
    await be.at_safe_point(env.m,env.state['busy'],Session())
    assert next(t for t in AttentionStore(env.db).reconcile([],env.m.cases.list()) if t['kind']=='browser_errand_stuck')['status']=='resolved'


DRAFT={'healthy':True,'reason':None,'has_draft':True}


async def test_draft_on_chat_page_blocks_errand_in_standby(env,monkeypatch):
    # 11-25 审阅：任务因草稿暂停时管理员点刷新——代办不能先跳转把沟通页未发送的草稿冲掉（约束 5）。
    from cloakup import autopilot as ap
    env.m.account_execution.set_task(env.task['id'],[env.bound['id']],'paused')
    class EndIdle(Exception):pass
    async def sleep(n):raise EndIdle
    async def checkpoint(job):pass
    monkeypatch.setattr(ap.asyncio,'sleep',sleep);env.m.checkpoint=checkpoint
    env.state['busy']=executor();env.state['chat']=DRAFT
    record=await be.request_refresh(env.m);session=Session()
    with pytest.raises(EndIdle):await ap.run_account_executor(env.m,{'id':'e'*32,'params':{}},session)
    saved=env.db.get(be.KIND,record['id'])
    assert session.navigations==[] and saved['status']=='failed' and '草稿' in saved['error']


async def test_draft_on_chat_page_blocks_errand_in_running_cycle(env,monkeypatch):
    # 11-25 审阅：运行中同样先只读检查沟通页，有草稿就不离开页面，收件照常。
    from cloakup import autopilot as ap
    class EndPass(Exception):pass
    async def sleep(n):raise EndPass
    async def checkpoint(job):pass
    async def health(session):pass
    async def sync(manager,job,session,case):return 1
    monkeypatch.setattr(ap.asyncio,'sleep',sleep);monkeypatch.setattr(ap,'check_page_health',health);monkeypatch.setattr(ap,'sync_and_follow',sync)
    monkeypatch.setattr(env.m.account_execution,'can_send',lambda cid:True);env.m.checkpoint=checkpoint
    env.state['busy']=executor();env.state['chat']=DRAFT
    record=await be.request_refresh(env.m);session=Session()
    with pytest.raises(EndPass):await ap.run_account_executor(env.m,{'id':'e'*32,'params':{'new_contacts':False}},session)
    assert session.navigations==[] and env.db.get(be.KIND,record['id'])['status']=='failed'


async def test_direct_run_checks_the_chat_page_before_leaving_it(env):
    # 11-25 审阅：没有执行器时同样先查草稿；读不出页面就留着排队，不跳转。
    await be.request_refresh(env.m)
    env.state['chat']={'healthy':False,'reason':'unreadable','has_draft':False}
    session=Session();job={'id':'r'*32,'mode':'boss_errand'}
    await be.run_direct(env.m,job,session)
    assert session.navigations==[] and [r['status'] for r in env.db.list(be.KIND)]==['pending']
    env.state['chat']=DRAFT
    await be.run_direct(env.m,job,session)
    assert session.navigations==[] and [r['status'] for r in env.db.list(be.KIND)]==['failed']


async def test_login_seen_before_leaving_chat_stays_page_level(env):
    # 11-25 审阅：起跳前发现掉登录，按页面级问题停止，不跳转。
    env.state['busy']=executor();env.state['chat']={'healthy':False,'reason':'login_required','has_draft':False}
    record=await be.request_refresh(env.m);session=Session()
    with pytest.raises(SourcingError,match='登录'):await be.at_safe_point(env.m,env.state['busy'],session)
    assert session.navigations==[] and env.db.get(be.KIND,record['id'])['status']=='failed'


async def test_half_finished_bind_can_be_retried_and_resumes_the_stub(env):
    # 11-25 审阅：建档后、计划草稿前失败（JD 不全、模型超时）留下的半成品不算已绑定；重试接着用这份档案，不另建。
    await _read(env)
    env.state['stub_error']=SourcingError('模型调用超时')
    await be.request_bind(env.m,{'encrypt_ids':[ID_A]})
    done=await be.at_safe_point(env.m,env.state['busy'],Session())
    assert done[0]['status']=='failed'
    stub=next(c for c in env.m.cases.list() if c['job']['url']==bj.job_url(ID_A))
    assert next(j for j in be.view(env.m)['jobs'] if j['encrypt_id']==ID_A)['bindable']
    env.state['stub_error']=None
    await be.request_bind(env.m,{'encrypt_ids':[ID_A]})
    done=await be.at_safe_point(env.m,env.state['busy'],Session())
    assert done[0]['status']=='done' and done[0]['result']['case_id']==stub['id'] and env.state['binds'][-1]==(bj.job_url(ID_A),stub['id'])
    assert sum(c['job']['url']==bj.job_url(ID_A) for c in env.m.cases.list())==1
    assert not [t for t in AttentionStore(env.db).reconcile([],env.m.cases.list()) if t['kind']=='browser_errand' and t['status']!='resolved']


async def _five_binds(env,monkeypatch):
    await _read(env);env.state['busy']=None
    jobs=be.latest(env.db)
    extra=[{'encrypt_id':'more%08dxx'%i,'title':'岗位'+str(i),'detail':None,'status':'open','status_text':'','recommendable':True,'option_text':'岗位'+str(i)} for i in range(5)]
    env.db.put(be.LIST,be.LIST_ID,{**jobs,'jobs':jobs['jobs']+extra})
    await be.request_bind(env.m,{'encrypt_ids':[j['encrypt_id'] for j in extra]})


async def test_direct_run_is_capped_and_only_takes_records_present_at_start(env,monkeypatch):
    # 11-25 审阅：没有执行器时一次运行最多做 3 个绑定，剩下的留给执行器或下一次请求，不长时间占着浏览器。
    await _five_binds(env,monkeypatch)
    job={'id':'r'*32,'mode':'boss_errand'};env.state['busy']=job
    result=await be.run_direct(env.m,job,Session())
    statuses=[r['status'] for r in env.db.list(be.KIND) if r['kind']=='job_bind']
    assert statuses.count('done')==be.DIRECT_BINDS and statuses.count('pending')==5-be.DIRECT_BINDS and '排队' in result['summary']


async def test_direct_run_leaves_late_requests_to_a_follow_up_run(env):
    # 11-25 审阅：运行快结束时登记的代办不能被丢下——运行结束后浏览器空闲就再起一次（同样有上限）。
    await _read(env);env.state['busy']=None
    await be.request_refresh(env.m)
    job={'id':'r'*32,'mode':'boss_errand'};env.state['busy']=job
    original=bj.read_published
    async def read_then_late_request(session):
        result=await original(session);await be.request_bind(env.m,{'encrypt_ids':[ID_A]});return result
    bj.read_published=read_then_late_request
    try:await be.run_direct(env.m,job,Session())
    finally:bj.read_published=original
    assert [r['status'] for r in sorted(env.db.list(be.KIND),key=lambda r:r['at'])]==['done','done','pending']
    env.state['busy']=None;env.state['created'].clear()
    await be.after_run(env.m,job)
    assert env.state['created']==[{'mode':'boss_errand','params':{}}]


async def test_run_that_never_reached_the_queue_does_not_start_another(env):
    # 11-26 09:33 现场：CloakBrowser 授权未通过，浏览器起不来，run_direct 没跑到、没有快照；
    # after_run 把所有排队代办都当成“新登记”，每 35 秒再起一次，一直重试授权。起不来的运行不续跑。
    await _read(env);env.state['busy']=None
    await be.request_refresh(env.m)
    job={'id':'r'*32,'mode':'boss_errand','status':'failed'};env.state['created'].clear()
    await be.after_run(env.m,job)
    assert env.state['created']==[]


async def test_direct_path_respects_the_execution_window(env):
    # 11-25 审阅：没有执行器时也只在执行时段内操作网站。
    for t in (env.task,env.task2):
        current=env.db.get('task',t['id']);env.db.put('task',t['id'],{**current,'config':{**current['config'],'schedule':CLOSED}})
    with pytest.raises(ValueError,match='执行时段'):await be.request_refresh(env.m)
    assert env.db.list(be.KIND)==[]


def test_window_needs_a_running_task_window_and_defaults_when_no_task(env):
    # 11-25 审阅：有运行中的任务时看运行中任务的时段；没有任务时按默认时段，而不是不限。
    running=env.db.get('task',env.task['id']);env.db.put('task',env.task['id'],{**running,'config':{**running['config'],'schedule':CLOSED}})
    assert not be.window_open(env.m)  # 暂停任务的时段（全天）不算
    for t in (env.task,env.task2):env.m.account_execution.set_task(t['id'],[],'stopped')
    from datetime import datetime
    from zoneinfo import ZoneInfo
    night=datetime(2026,11,25,3,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp();noon=datetime(2026,11,25,10,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    assert not be.window_open(env.m,night) and be.window_open(env.m,noon)


async def test_interrupted_running_errand_is_settled_and_gets_a_todo(env):
    # 11-25 审阅：服务重启或运行结束时停在 running 的代办，不能一直显示“绑定中”。
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    env.db.put(be.KIND,record['id'],{**record,'status':'running','run_id':'x'*32})
    todos=[t for t in AttentionStore(env.db).reconcile([],env.m.cases.list()) if t['kind']=='browser_errand']
    assert len(todos)==1 and '中断' in todos[0]['detail']
    be.settle(env.m)
    saved=env.db.get(be.KIND,record['id'])
    assert saved['status']=='failed' and '中断' in saved['error'] and be.view(env.m)['pending']==[]


async def test_stuck_todo_waits_quietly_outside_the_window(env):
    # 11-25 审阅：时段外排队是正常等待，不提醒；进入时段仍未执行才提醒，并说明时段内会执行。
    env.state['busy']=executor()
    record=await be.request_refresh(env.m)
    env.db.put(be.KIND,record['id'],{**record,'at':time.time()-be.STUCK_SECONDS-5})
    for t in (env.task,env.task2):
        current=env.db.get('task',t['id']);env.db.put('task',t['id'],{**current,'config':{**current['config'],'schedule':CLOSED}})
    assert not [t for t in AttentionStore(env.db).reconcile([],env.m.cases.list()) if t['kind']=='browser_errand_stuck']
