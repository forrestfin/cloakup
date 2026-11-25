from types import SimpleNamespace as NS

import pytest

from cloakup import autopilot as ap, boss_chat
from cloakup.account_execution import AccountExecution
from cloakup.attention import AttentionStore
from cloakup.selection_recovery import public_records
from cloakup.storage import Database
from cloakup.workbench import Workbench

pytestmark = pytest.mark.usefixtures('no_jitter')


@pytest.mark.parametrize('mode', ['recovers', 'exhausted', 'draft', 'restart', 'unverified'])
async def test_bounded_recovery_then_resume_or_pause_only_affected_task(tmp_path, monkeypatch, mode):
    clock=[1000.0]; attempts=[]; repairs=[]; contacts=[]; events=[]
    monkeypatch.setattr(ap.time,'time',lambda:clock[0])
    db=Database(tmp_path); account=AccountExecution(db)
    account.set_task('task',['case'],'running')
    account.set_task('other-task',['other-case'],'running')
    cases={cid:{'id':cid,'job':{'label':cid},'autonomy':{'enabled':True,'poll_seconds':120},'candidates':{}} for cid in ('case','other-case')}
    class Finished(Exception):pass
    async def checkpoint(job):pass
    async def sleep(seconds):
        clock[0]+=seconds
        if next(m for m in account.members() if m['id']=='other-task')['status']=='paused':raise AssertionError('unaffected task must continue')
        if next(m for m in account.members() if m['id']=='task')['status']=='paused':raise Finished()
    async def sync(manager,job,session,case):
        if case['id']=='other-case':return 0
        attempts.append(clock[0])
        if mode=='recovers' and len(attempts)==2:
            assert job['params']['read_only'] is True
            return 1
        if mode=='unverified' and len(attempts)>3:
            raise Finished()  # 这一条已放下，后续普通轮次照常进行
        if mode=='unverified' and len(attempts)>1:
            from cloakup.sourcing import SourcingError
            raise SourcingError('打开后的收件人与岗位未核实')
        error=boss_chat.ChatSelectionUnavailable('会话选择超时',stage='click',reason='timeout',exception_type='TimeoutError')
        error.chat_id='123-0'
        raise error
    async def repair(session):
        repairs.append(clock[0])
        if mode=='draft':return {'status':'blocked','reason':'draft_present'}
        return {'status':'recovered'}
    async def contact(manager,job,session,case):
        contacts.append(case['id'])
        if case['id']=='case':raise Finished()
    monkeypatch.setattr(ap.asyncio,'sleep',sleep)
    monkeypatch.setattr(ap,'sync_and_follow',sync)
    monkeypatch.setattr(ap,'first_contact',contact)
    monkeypatch.setattr(boss_chat,'recover_chat_selection',repair)
    job={'id':'run','created':1000,'status':'running','params':{'new_contacts':True},'account_runner':True}
    if mode=='restart':
        job['selection_recovery']={'case':{'incident_id':'preserved','task_id':'task','status':'retrying','attempts':2,'next_check_at':1000,'updated_at':900,'failures':['123-0'],'reason':'等待恢复'}}
    manager=NS(account_execution=account,cases=NS(load=lambda cid:cases[cid],save=lambda c:cases.__setitem__(c['id'],c)),jobs={'run':job},
        definitions=NS(get=lambda tid:{'config':{'sourcing_mode':'continuous'}}),
        checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:events.append(args[-1]))
    with pytest.raises(Finished):await ap.run_account_executor(manager,job,object())
    state=job['selection_recovery']['case']
    todos=AttentionStore(db).reconcile([job],list(cases.values()))
    if mode=='recovers':
        assert attempts==[1000,1120] and repairs==[1120]
        assert state['status']=='recovered' and not todos
        assert 'case' in contacts
    elif mode=='unverified':
        # 恢复后原会话仍对不上：异常没带会话编号，也按原失败的那一条计次，只放下这一条，不停整单。
        assert state['status']=='deferred' and cases['case']['deferred_chat_ids']==['123-0']
        assert attempts[:3]==[1000,1120,1360]
        assert next(m for m in account.members() if m['id']=='task')['status']=='running'
    else:
        assert state['status']=='blocked'
        assert 'case' not in contacts
        assert next(m for m in account.members() if m['id']=='task')['status']=='paused'
        if mode=='draft':
            assert len(todos)==1 and todos[0]['task_id']=='task' and todos[0]['kind']=='execution_recovery'
        else:
            assert not any(t['status']!='resolved' for t in todos)
        if mode=='exhausted':assert attempts==[1000,1120,1360] and repairs==[1120,1360]
        if mode=='draft':assert attempts==[1000] and repairs==[1120]
        if mode=='restart':assert state['incident_id']=='preserved' and state['attempts']==3 and attempts==[1000]
    assert next(m for m in account.members() if m['id']=='other-task')['status']=='running'


async def test_paused_recovery_does_not_touch_browser(tmp_path,monkeypatch):
    account=AccountExecution(Database(tmp_path));account.set_task('task',['case'],'paused')
    class Finished(Exception):pass
    async def stop(*a):raise Finished()
    async def checkpoint(*a):pass
    async def repair(*a):raise AssertionError('paused task cannot manipulate page')
    monkeypatch.setattr(ap.asyncio,'sleep',stop);monkeypatch.setattr(boss_chat,'recover_chat_selection',repair)
    job={'id':'run','params':{},'selection_recovery':{'case':{'status':'blocked','reason':'blocked','updated_at':1}}}
    manager=NS(account_execution=account,checkpoint=checkpoint,persist=lambda j:None,event=lambda *a:None)
    with pytest.raises(Finished):await ap.run_account_executor(manager,job,object())
    assert job['autopilot_stage']=='selection_blocked'


def test_recovery_runtime_is_truthful_and_does_not_expose_internal_identifiers():
    job={'id':'run','status':'running','account_runner':True,'autopilot_stage':'selection_blocked','created':1,
         'selection_recovery':{'case':{'status':'blocked','task_id':'task','attempts':3,'max_attempts':3,'updated_at':3,'reason':'请管理员检查','failures':['private-chat-id']}}}
    manager=NS(busy=lambda:job,jobs={'run':job},active_session=None,boss_session=None,
        browser_alive=lambda s:False,service_instance_id='service',monitor_heartbeat=1,
        account_execution=NS(members=lambda:[{'status':'paused'}]))
    status=Workbench.runtime_status(manager)
    assert status['task_state']=='任务受阻 · 等待管理员' and status['needs_attention']
    assert 'private-chat-id' not in str(status)
    newer={'id':'new','selection_recovery':{'case':{**job['selection_recovery']['case'],'status':'recovered','updated_at':4}}}
    assert public_records([job,newer])[0]['status']=='recovered'


def test_recovery_inheritance_keeps_budget_and_latest_verified_state():
    previous={'id':'previous','selection_recovery':{'case':{'incident_id':'same','status':'blocked','attempts':3,'updated_at':1}}}
    retained={'id':'retained'}
    manager=NS(jobs={'previous':previous,'retained':retained})
    Workbench.inherit_selection_recovery(manager,retained,['case'])
    assert retained['selection_recovery']['case']['attempts']==3
    retained['selection_recovery']['case'].update(status='recovered',updated_at=2)
    assert previous['selection_recovery']['case']['status']=='blocked'  # deep copy
    joined={'id':'joined'};manager.jobs['joined']=joined
    Workbench.inherit_selection_recovery(manager,joined,['case'])
    assert joined['selection_recovery']['case']['status']=='recovered'


def test_another_chat_does_not_inherit_the_shared_attempt_budget(tmp_path):
    from cloakup.selection_recovery import failure
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'running')
    saved=[]
    class Cases:
        def load(self,cid):return {'id':cid,'deferred_chat_ids':['old-0']}
        def save(self,case):saved.append(case)
    job={'id':'run','selection_recovery':{'case':{'incident_id':'same','task_id':'task','status':'retrying','attempts':2,'failures':['317-0','519-0'],'updated_at':1}}}
    manager=NS(account_execution=account,cases=Cases(),persist=lambda job:None,event=lambda *args:None)
    for _ in range(3):
        exc=boss_chat.ChatSelectionUnavailable('详情头部未匹配目标岗位',stage='verify_identity',reason='header_job_unverified')
        exc.chat_id='519-0'
        record=failure(manager,job,{'id':'case'},exc)
    assert record['status']=='retrying' and '519-0' not in record['failures'] and record['failures']==['317-0']
    assert saved[-1]['deferred_chat_ids']==['old-0','519-0']
    assert next(m for m in account.members() if m['id']=='task')['status']=='running'


def test_single_missing_chat_is_set_aside_without_pausing_the_task(tmp_path):
    from cloakup.selection_recovery import failure
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'running')
    saved=[]
    class Cases:
        def load(self,cid):return {'id':cid}
        def save(self,case):saved.append(case)
    exc=boss_chat.ChatSelectionUnavailable('待恢复会话仍未从当前列表核验',stage='locate',reason='target_missing')
    exc.chat_id='103-0'
    job={'id':'run','selection_recovery':{'case':{'incident_id':'same','task_id':'task','status':'retrying','attempts':2,'failures':['103-0'],'updated_at':1}}}
    manager=NS(account_execution=account,cases=Cases(),persist=lambda job:None,event=lambda *args:None)
    record=failure(manager,job,{'id':'case'},exc)
    assert record['status']=='deferred' and saved[0]['deferred_chat_ids']==['103-0']
    assert next(m for m in account.members() if m['id']=='task')['status']=='running'


def test_list_identity_mismatch_is_set_aside_without_pausing_the_task(tmp_path):
    from cloakup.selection_recovery import failure
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'running')
    saved=[]
    class Cases:
        def load(self,cid):return {'id':cid}
        def save(self,case):saved.append(case)
    exc=boss_chat.ChatSelectionUnavailable('目标会话的候选人与岗位信息变化，未发送',stage='list_identity',reason='list_identity_mismatch')
    exc.chat_id='789-0'
    job={'id':'run','selection_recovery':{'case':{'incident_id':'same','task_id':'task','status':'retrying','attempts':2,'failures':['789-0'],'updated_at':1}}}
    manager=NS(account_execution=account,cases=Cases(),persist=lambda job:None,event=lambda *args:None)
    record=failure(manager,job,{'id':'case'},exc)
    assert record['status']=='deferred' and saved[0]['deferred_chat_ids']==['789-0']
    assert next(m for m in account.members() if m['id']=='task')['status']=='running'


def test_resumed_block_is_pending_verification_without_budget_reset(tmp_path):
    from cloakup.selection_recovery import resume_verification
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'paused')
    job={'id':'run','selection_recovery':{'case':{'task_id':'task','incident_id':'same','status':'blocked','attempts':3,'updated_at':1}}}
    writes=[];manager=NS(account_execution=account,persist=lambda j:writes.append(True))
    assert not resume_verification(manager,job)
    account.set_task('task',['case'],'running')
    assert resume_verification(manager,job)
    record=job['selection_recovery']['case']
    assert record['status']=='retrying' and record['attempts']==3 and record['incident_id']=='same'
    assert not resume_verification(manager,job)
    assert len(writes)==1


@pytest.mark.parametrize('initial', [{'read_only': False}, {'read_only': True, 'inbound_resume_accept': True}, {}])
def test_recovery_pass_forces_read_only_and_restores_on_error(initial):
    params = dict(initial)
    class Boom(Exception): pass
    with pytest.raises(Boom):
        with ap.recovery_pass(params, True):
            assert params['read_only'] is True and params['inbound_resume_accept'] is True
            raise Boom()
    assert params == initial


def test_recovery_pass_inactive_leaves_params_untouched():
    params = {'read_only': False}
    with ap.recovery_pass(params, False):
        assert params == {'read_only': False}
    assert params == {'read_only': False}


def _failure_env(tmp_path,prior):
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'running')
    store={'case':{'id':'case'}}
    cases=NS(load=lambda cid:dict(store[cid]),save=lambda c:store.__setitem__(c['id'],c))
    job={'id':'run','selection_recovery':{'case':{'incident_id':'same','task_id':'task','status':'retrying','updated_at':1,**prior}}}
    manager=NS(account_execution=account,cases=cases,persist=lambda job:None,event=lambda *args:None)
    return manager,job,account,store


def test_third_failure_without_chat_id_sets_aside_the_chat_under_recovery(tmp_path):
    """14:50/14:51、18:20、20:09：第三次失败没带会话编号，旧逻辑把整单暂停。"""
    from cloakup.selection_recovery import failure
    manager,job,account,store=_failure_env(tmp_path,{'attempts':2,'failures':['456-0'],'last_failed_chat_id':'456-0','receiver':'甲'})
    exc=boss_chat.ChatSelectionUnavailable('恢复后仍未核验目标会话',stage='recover_verify',reason='target_unverified')
    record=failure(manager,job,{'id':'case'},exc)
    assert record['status']=='deferred' and store['case']['deferred_chat_ids']==['456-0']
    assert record['receiver']=='甲'
    assert next(m for m in account.members() if m['id']=='task')['status']=='running'


def test_failure_without_chat_id_or_known_target_still_counts_for_the_task(tmp_path):
    from cloakup.selection_recovery import failure
    manager,job,account,store=_failure_env(tmp_path,{'attempts':2,'failures':[]})
    exc=boss_chat.ChatSelectionUnavailable('恢复后仍未核验目标会话',stage='recover_verify',reason='target_unverified')
    record=failure(manager,job,{'id':'case'},exc)
    assert record['status']=='blocked' and 'deferred_chat_ids' not in store['case']
    assert next(m for m in account.members() if m['id']=='task')['status']=='paused'


@pytest.mark.parametrize('reason',['timeout','page_changed'])
def test_page_level_failure_is_not_attributed_to_a_chat(tmp_path,reason):
    from cloakup.selection_recovery import failure
    manager,job,account,store=_failure_env(tmp_path,{'attempts':2,'failures':['456-0'],'last_failed_chat_id':'456-0'})
    exc=boss_chat.ChatSelectionUnavailable('页面问题',stage='click',reason=reason)
    record=failure(manager,job,{'id':'case'},exc)
    assert record['status']=='blocked'
    assert next(m for m in account.members() if m['id']=='task')['status']=='paused'


def test_block_reason_is_never_attributed_to_a_chat(tmp_path):
    from cloakup.selection_recovery import failure
    manager,job,account,store=_failure_env(tmp_path,{'attempts':1,'failures':['456-0'],'last_failed_chat_id':'456-0'})
    record=failure(manager,job,{'id':'case'},block_reason='draft_present')
    assert record['status']=='blocked' and 'deferred_chat_ids' not in store['case']


def test_isolated_chat_records_since_and_blocks_outreach_only_for_a_while():
    # 11-24：隔离会话无限期拦住整个岗位的新开聊（11-23 拦了 869 分钟，11-24 整天没有开聊）。
    from cloakup.selection_recovery import outreach_blockers,OUTREACH_HOLD_SECONDS
    now=1_000_000
    case={'deferred_chat_ids':['a-0','b-0','c-0'],'deferred_chat_since':{'a-0':now-60,'b-0':now-OUTREACH_HOLD_SECONDS-1},
          'outreach_isolation_exceptions':[]}
    # 刚隔离的会拦；超过时限的不拦；没有记录时间的旧隔离视为已超时。
    assert outreach_blockers(case,now)==['a-0']
    case['outreach_isolation_exceptions']=['a-0']
    assert outreach_blockers(case,now)==[]


def test_failure_records_when_a_chat_was_isolated(tmp_path):
    from cloakup.selection_recovery import failure
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'running')
    store={'case':{'id':'case'}}
    class Cases:
        def load(self,cid):return dict(store[cid])
        def save(self,case):store[case['id']]=case
    job={'id':'run'}
    manager=NS(account_execution=account,cases=Cases(),persist=lambda job:None,event=lambda *args:None)
    for _ in range(3):
        exc=boss_chat.ChatSelectionUnavailable('没有唯一目标会话',stage='locate',reason='target_row_missing')
        exc.chat_id='456-0'
        failure(manager,job,{'id':'case'},exc)
    assert store['case']['deferred_chat_ids']==['456-0'] and store['case']['deferred_chat_since']['456-0']>0


# 11-24 20:40 沟通页卡在启动页：系统因“无法核验编辑器”暂停任务后执行器只待机，12 小时没有任何重试。
# 现在：只针对这一种暂停（且暂停前页面检查确实卡住），执行时段内按间隔自愈；页面恢复、确认无草稿／登录／验证后由系统恢复任务，原会话照旧只读复核。
CHAT_URL='https://www.zhipin.com/web/chat/index'
STUCK={'id':'boss','kind':'chat','healthy':False,'reason':'content_missing','exhausted':True,'steps':['reload','reload','tab_reset','menu_entry']}


def _page_blocked_world(tmp_path,monkeypatch,*,heal=None,guard=None,actor='system',reason='selection_blocked:editor_unavailable',schedule=None,clock=10_000.0,health=STUCK,blocker=None):
    from cloakup import page_health
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'running',actor='admin',reason='start_or_resume')
    now=[clock]
    monkeypatch.setattr(ap.time,'time',lambda:now[0])
    if health:db.put('page_health','boss',{**health,'at':clock-100})
    account.set_task('task',['case'],'paused',actor=actor,reason=reason)
    if schedule:db.put('task','task',{'id':'task','config':{'schedule':schedule}})
    events=[];heals=[];starts=[];logged=[]
    async def fake_heal(session,kind,*,force=False):
        heals.append((kind,force));return heal or {'healthy':True,'reason':None,'needs_admin':False,'has_draft':False}
    async def fake_guard(session):return guard
    async def start(task_id,*,actor,reason):
        starts.append((task_id,actor,reason))
        member=next(m for m in account.members() if m['id']==task_id)
        account.set_task(task_id,member['case_ids'],'running',actor=actor,reason=reason)
    monkeypatch.setattr(page_health,'heal',fake_heal);monkeypatch.setattr(ap,'_chat_guard',fake_guard)
    monkeypatch.setattr(ap,'log',lambda category,**f:logged.append(f),raising=False)
    manager=NS(account_execution=account,start_account_task=start,resume_blocker=lambda tid:blocker,persist=lambda j:None,event=lambda *a:events.append(a[-1]))
    job={'id':'run','params':{},'selection_recovery':{'case':{'status':'blocked','task_id':'task','reason':'无法核验编辑器是否存在草稿','updated_at':1,
         'failures':['123-0'],'diagnostics':{'stage':'recover_guard','reason':'editor_unavailable'}}}}
    session=NS(page=NS(url=CHAT_URL,is_closed=lambda:False))
    return NS(db=db,account=account,events=events,heals=heals,starts=starts,logged=logged,manager=manager,job=job,session=session,now=now)


async def _due(w,**fields):
    """先让执行器建立本次暂停的记录，再把下次尝试时间提前到现在。"""
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    doc=w.db.get('page_recovery','boss');doc.update(next_at=0,**fields);w.db.put('page_recovery','boss',doc)


async def test_page_blocked_task_is_resumed_by_the_system_after_the_page_heals(tmp_path,monkeypatch):
    # 11-24 20:40 沟通页卡在启动页
    from cloakup.selection_recovery import resume_verification
    w=_page_blocked_world(tmp_path,monkeypatch);await _due(w)
    assert w.heals==[]  # 刚暂停时先隔一个间隔
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.heals==[('chat',True)] and w.starts==[('task','system','page_recovered')]
    member=w.account.members()[0]
    assert member['status']=='running' and member['history'][-1]['actor']=='system' and member['history'][-1]['reason']=='page_recovered'
    assert any('系统已自动恢复任务' in e for e in w.events)
    assert any(f.get('type')=='page_recovery' and f.get('step')=='resumed' for f in w.logged)
    # 原会话照旧走只读复核，不直接清除异常
    assert resume_verification(w.manager,w.job) and w.job['selection_recovery']['case']['status']=='retrying'
    assert w.job['selection_recovery']['case']['resume_verification']


@pytest.mark.parametrize('case',['draft','login','verification','guard_draft','guard_unreadable','still_stuck'])
async def test_page_blocked_task_stays_paused_unless_the_page_is_truly_safe(tmp_path,monkeypatch,case):
    # 11-24 20:40 沟通页卡在启动页：有草稿、掉登录、平台验证、编辑器读不到、页面仍卡住，一律不自动恢复
    heal={'draft':{'healthy':False,'reason':'draft_present','needs_admin':True,'has_draft':True},
          'login':{'healthy':False,'reason':'login_required','needs_admin':True,'has_draft':False},
          'verification':{'healthy':False,'reason':'verification_required','needs_admin':True,'has_draft':False},
          'still_stuck':{'healthy':False,'reason':'loading_stuck','needs_admin':False,'has_draft':False,'exhausted':True}}.get(case)
    guard={'guard_draft':'draft_present','guard_unreadable':'editor_unavailable'}.get(case)
    w=_page_blocked_world(tmp_path,monkeypatch,heal=heal,guard=guard);await _due(w)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.heals==[('chat',True)] and w.starts==[] and w.account.members()[0]['status']=='paused'
    doc=w.db.get('page_recovery','boss')
    assert doc['attempts']==1 and 10_000.0<doc['next_at']<=10_000.0+900


@pytest.mark.parametrize('actor,reason',[('admin','manual'),('system','selection_blocked:draft_present'),('system','selection_blocked:login_required'),('system','selection_blocked:verification_required'),('system','service_restart')])
async def test_other_pauses_are_never_healed_or_resumed(tmp_path,monkeypatch,actor,reason):
    # 11-24 20:40 沟通页卡在启动页：只处理“系统因页面打不开而暂停”这一种
    w=_page_blocked_world(tmp_path,monkeypatch,actor=actor,reason=reason)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    try:doc=w.db.get('page_recovery','boss')
    except FileNotFoundError:doc={}
    w.db.put('page_recovery','boss',{**doc,'id':'boss','next_at':0})
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.heals==[] and w.starts==[] and w.account.members()[0]['status']=='paused'


async def test_editor_pause_without_a_stuck_page_is_not_auto_healed(tmp_path,monkeypatch):
    # 复审：计划的第二个条件——暂停前最近一次页面检查确实是卡在加载、内容缺失或自愈用尽
    w=_page_blocked_world(tmp_path,monkeypatch,health={'id':'boss','kind':'chat','healthy':True,'reason':None})
    await _due(w)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.heals==[] and w.starts==[] and w.db.get('page_recovery','boss')['page_stuck'] is False


SCHEDULE={'enabled':True,'timezone':'Asia/Shanghai','weekdays':[0,1,2,3,4,5,6],'windows':[{'start':'20:00','end':'21:00'}]}
def _local(hour,minute):
    import datetime as dt
    return dt.datetime(2026,11,24,hour,minute,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()


async def test_page_blocked_task_is_not_touched_outside_the_execution_window(tmp_path,monkeypatch):
    # 11-24 20:40 沟通页卡在启动页：时段外不操作网站，进入时段再试
    w=_page_blocked_world(tmp_path,monkeypatch,schedule=SCHEDULE,clock=_local(23,50))
    await _due(w)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.heals==[] and w.starts==[]
    assert w.db.get('page_recovery','boss')['outside_window_until']>_local(23,50)


async def test_the_window_is_rechecked_right_before_resuming(tmp_path,monkeypatch):
    # 复审：自愈耗时期间走出了执行时段，就不再恢复任务
    from cloakup import page_health
    w=_page_blocked_world(tmp_path,monkeypatch,schedule=SCHEDULE,clock=_local(20,59))
    await _due(w)
    async def slow_heal(session,kind,*,force=False):
        w.heals.append(kind);w.now[0]=_local(21,2);return {'healthy':True,'reason':None,'needs_admin':False,'has_draft':False}
    monkeypatch.setattr(page_health,'heal',slow_heal)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.heals==['chat'] and w.starts==[] and w.account.members()[0]['status']=='paused'


async def test_page_retry_interval_uses_jitter_with_an_upper_bound_and_backs_off(tmp_path,monkeypatch):
    # 11-24 20:40 沟通页卡在启动页：前 3 次约每 10 分钟、之后约每 30 分钟，随机抖动但有上限；未到时间不碰页面
    seen=[]
    def huge(seconds,sigma=0.3):seen.append(seconds);return 10**6
    monkeypatch.setattr(ap,'jitter',huge)
    w=_page_blocked_world(tmp_path,monkeypatch,heal={'healthy':False,'reason':'loading_stuck','needs_admin':False,'has_draft':False})
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    doc=w.db.get('page_recovery','boss')
    assert w.heals==[] and seen==[600] and doc['next_at']==10_000.0+900
    gaps=[]
    for _ in range(4):
        doc=w.db.get('page_recovery','boss');doc['next_at']=0;w.db.put('page_recovery','boss',doc)
        await ap.heal_paused_tasks(w.manager,w.job,w.session)
        gaps.append(w.db.get('page_recovery','boss')['next_at']-10_000.0)
        await ap.heal_paused_tasks(w.manager,w.job,w.session)  # 未到时间：不碰页面
    assert len(w.heals)==4 and gaps==[900,900,2700,2700] and seen[1:]==[600,600,1800,1800]


async def test_attempts_are_capped_per_incident_and_admin_is_told(tmp_path,monkeypatch):
    # 复审：同一次暂停最多自动尝试 6 次，用完停下并告诉管理员
    w=_page_blocked_world(tmp_path,monkeypatch,heal={'healthy':False,'reason':'loading_stuck','needs_admin':False,'has_draft':False})
    await _due(w,attempts=6)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    await _due(w)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    doc=w.db.get('page_recovery','boss')
    assert w.heals==[] and doc['result']=='capped' and doc['cap']=='incident'
    assert sum('已停止自动重试' in e for e in w.events)==1


async def test_attempts_are_capped_per_day(tmp_path,monkeypatch):
    from cloakup.autonomy import day_key
    w=_page_blocked_world(tmp_path,monkeypatch)
    await _due(w,day=day_key(10_000.0),day_attempts=12)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    doc=w.db.get('page_recovery','boss')
    assert w.heals==[] and doc['result']=='capped' and doc['cap']=='daily_attempts'


async def test_automatic_resumes_are_capped_per_task_per_day(tmp_path,monkeypatch):
    # 复审：一天内因页面问题自动恢复 3 次又被暂停，就不再自动恢复，防止反复暂停、恢复
    db_world=_page_blocked_world(tmp_path,monkeypatch)
    member=db_world.account.members()[0]
    member['history']=member['history'][:-1]+[{'at':9_000.0+i,'status':s,'actor':'system','reason':r} for i,(s,r) in enumerate(
        [('running','page_recovered'),('paused','selection_blocked:editor_unavailable')]*3)]+[member['history'][-1]]
    db_world.db.put('account_task','task',member)
    await _due(db_world)
    await ap.heal_paused_tasks(db_world.manager,db_world.job,db_world.session)
    doc=db_world.db.get('page_recovery','boss')
    assert db_world.heals==[] and db_world.starts==[] and doc['result']=='capped' and doc['cap']=='daily_resumes'


async def test_unfixable_resume_preconditions_skip_the_heal(tmp_path,monkeypatch):
    # 复审：发布验收、环境核验这些刷新页面修不好的前置条件不满足，就不去碰页面
    w=_page_blocked_world(tmp_path,monkeypatch,blocker='当前环境与执行器尚未通过核验')
    await _due(w)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    await _due(w)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    doc=w.db.get('page_recovery','boss')
    assert w.heals==[] and doc['result']=='precondition' and '尚未通过核验' in doc['reason'] and doc['attempts']==0
    assert sum('页面恢复后也无法自动恢复任务' in e for e in w.events)==1


async def test_admin_change_during_healing_is_not_overridden(tmp_path,monkeypatch):
    # 11-24 20:40 沟通页卡在启动页：自愈期间管理员手动暂停（或恢复）了任务，系统不再改它的状态
    from cloakup import page_health
    w=_page_blocked_world(tmp_path,monkeypatch);await _due(w)
    async def heal(session,kind,*,force=False):
        w.account.set_task('task',['case'],'paused',actor='admin',reason='manual')
        return {'healthy':True,'reason':None,'needs_admin':False,'has_draft':False}
    monkeypatch.setattr(page_health,'heal',heal)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.starts==[] and w.account.members()[0]['history'][-1]['actor']=='admin'


async def test_paused_page_recovery_waits_for_a_running_environment_check(tmp_path,monkeypatch):
    # 11-24 20:40 沟通页卡在启动页：环境检查（也会打开 BOSS 页面）进行中时不同时自愈
    w=_page_blocked_world(tmp_path,monkeypatch);await _due(w)
    w.manager.preflight_task=NS(done=lambda:False)
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert w.heals==[] and w.starts==[]


async def test_the_browser_lock_is_held_for_the_whole_heal_and_resume(tmp_path,monkeypatch):
    # 复审：只在开头看锁不够，自愈要几分钟；整个自愈、复核和恢复期间都持有锁，诊断或环境检查不能同时驱动同一标签页
    import asyncio
    from cloakup import page_health
    w=_page_blocked_world(tmp_path,monkeypatch);await _due(w)
    held=[]
    async def heal(session,kind,*,force=False):
        held.append(w.manager._browser_diagnostic_lock.locked())
        await ap.heal_paused_tasks(w.manager,w.job,w.session)   # 期间再来一次（如另一路调用）：直接跳过
        return {'healthy':True,'reason':None,'needs_admin':False,'has_draft':False}
    async def start(task_id,*,actor,reason):
        held.append(w.manager._browser_diagnostic_lock.locked())
        w.account.set_task(task_id,['case'],'running',actor=actor,reason=reason)
    monkeypatch.setattr(page_health,'heal',heal);w.manager.start_account_task=start
    await ap.heal_paused_tasks(w.manager,w.job,w.session)
    assert held==[True,True] and not w.manager._browser_diagnostic_lock.locked()
    # 锁已被诊断占用时不碰页面
    w2=_page_blocked_world(tmp_path/'b',monkeypatch);await _due(w2)
    w2.manager._browser_diagnostic_lock=asyncio.Lock();await w2.manager._browser_diagnostic_lock.acquire()
    await ap.heal_paused_tasks(w2.manager,w2.job,w2.session)
    assert w2.heals==[] and w2.starts==[]


async def test_chat_guard_requires_an_explicit_draft_reading(monkeypatch):
    # 复审：读到的状态没有 has_draft 字段，与恢复核验一样视为无法核验编辑器
    async def state(page):return {'editor_count':1,'chat_rows':3}
    monkeypatch.setattr(boss_chat,'_settled_guard_state',state)
    assert await ap._chat_guard(NS(page=NS(url=CHAT_URL,is_closed=lambda:False)))=='editor_unavailable'
    async def good(page):return {'editor_count':1,'chat_rows':3,'has_draft':False}
    monkeypatch.setattr(boss_chat,'_settled_guard_state',good)
    assert await ap._chat_guard(NS(page=NS(url=CHAT_URL,is_closed=lambda:False))) is None


async def test_standby_executor_runs_the_paused_page_recovery(tmp_path,monkeypatch):
    # 11-24 20:40 沟通页卡在启动页：待机分支会调用定时自愈
    w=_page_blocked_world(tmp_path,monkeypatch)
    calls=[]
    async def recover(manager,job,session):calls.append(job['id'])
    class Finished(Exception):pass
    async def stop(*a):raise Finished()
    async def checkpoint(*a):pass
    monkeypatch.setattr(ap,'heal_paused_tasks',recover);monkeypatch.setattr(ap.asyncio,'sleep',stop)
    w.manager.checkpoint=checkpoint
    with pytest.raises(Finished):await ap.run_account_executor(w.manager,w.job,w.session)
    assert calls==['run'] and w.job['autopilot_stage']=='selection_blocked'


async def test_running_executor_also_retries_a_page_blocked_task(tmp_path,monkeypatch):
    # 复审：另一个任务还在运行时，系统因页面暂停的任务也要在安全点重试，而不是只在全部暂停时
    w=_page_blocked_world(tmp_path,monkeypatch)
    w.account.set_task('other',['other-case'],'running',actor='admin',reason='start_or_resume')
    calls=[];synced=[]
    async def recover(manager,job,session):calls.append(sorted(manager.account_execution.case_ids()))
    async def sync(manager,job,session,case):synced.append(case['id']);return 1
    class Finished(Exception):pass
    async def stop(*a):raise Finished()
    async def checkpoint(*a):pass
    monkeypatch.setattr(ap,'heal_paused_tasks',recover);monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap.asyncio,'sleep',stop)
    cases={'other-case':{'id':'other-case','autonomy':{'enabled':True,'poll_seconds':120},'candidates':{}}}
    w.manager.checkpoint=checkpoint;w.manager.cases=NS(load=lambda cid:cases[cid])
    w.manager.definitions=NS(get=lambda tid:{'config':{'sourcing_mode':'continuous'}})
    w.job['selection_recovery']={}
    with pytest.raises(Finished):await ap.run_account_executor(w.manager,w.job,object())
    assert calls==[['other-case']] and synced==['other-case']


def test_stale_page_record_does_not_make_an_unrelated_pause_eligible():
    # 11-24 20:40 沟通页卡在启动页：只认暂停前后 15 分钟内的页面检查结论，旧的“用尽”记录不算。
    from cloakup.selection_recovery import page_stuck
    stuck={'healthy':False,'exhausted':True,'reason':'loading_stuck','at':1000.0}
    assert page_stuck(stuck,paused_at=1500.0)
    assert not page_stuck(stuck,paused_at=1000.0+901+900)
    assert page_stuck(stuck)


async def test_page_recovery_error_never_stops_the_executor(monkeypatch):
    # 11-24 20:40 沟通页卡在启动页：运行中的执行器每轮都会调自动恢复，任何异常只记日志，不让执行器停掉。
    from cloakup import autopilot
    async def boom(*a,**k):raise TypeError('bad config')
    logged=[]
    monkeypatch.setattr(autopilot,'heal_paused_tasks',boom)
    monkeypatch.setattr(autopilot,'log',lambda *a,**k:logged.append(k))
    await autopilot.heal_paused_tasks_safely(object(),{'id':'run'},object())
    assert logged and logged[0].get('error_type')=='TypeError'
