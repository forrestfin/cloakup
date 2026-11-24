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
