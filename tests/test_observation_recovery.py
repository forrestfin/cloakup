import json
from types import SimpleNamespace as NS

import pytest

from cloakup import autopilot as ap, boss_contact as bc
from cloakup.account_execution import AccountExecution
from cloakup.attention import AttentionStore
from cloakup.sourcing import SourcingError
from cloakup.storage import Database
from cloakup.workbench import Workbench, _exception_frames

pytestmark = pytest.mark.usefixtures('no_jitter')


async def test_only_snapshot_timeouts_become_recoverable():
    error=TimeoutError()
    async def observe(*args,**kwargs):raise error
    with pytest.raises(bc.ObservationUnavailable) as caught:
        await bc._observation(NS(observe=observe))
    assert caught.value.__cause__ is error
    assert '页面读取超时' in str(caught.value)
    async def other_error(*args,**kwargs):raise ValueError('invalid snapshot')
    with pytest.raises(ValueError):await bc._observation(NS(observe=other_error))


async def test_click_timeout_is_not_reclassified_or_retried(monkeypatch):
    calls=[]
    async def find(*args,**kwargs):return NS(index=4),None
    async def click(index):
        calls.append(index)
        raise TimeoutError()
    monkeypatch.setattr(bc,'_find',find)
    with pytest.raises(TimeoutError):await bc._click(NS(click=click),'发送')
    assert calls==[4]


@pytest.mark.parametrize('failure_count',[1,3])
async def test_account_read_backoff_recovers_or_stops_after_three(tmp_path,monkeypatch,failure_count):
    clock=[1000.0];attempts=[];contacts=[];events=[]
    monkeypatch.setattr(ap.time,'time',lambda:clock[0])
    db=Database(tmp_path);account=AccountExecution(db)
    account.set_task('task',['case'],'running')
    case={'id':'case','autonomy':{'enabled':True,'poll_seconds':120},'candidates':{}}
    class Finished(Exception):pass
    async def checkpoint(job):pass
    async def sleep(seconds):clock[0]+=seconds
    async def sync(*args):
        attempts.append(clock[0])
        if len(attempts)<=failure_count:raise bc.ObservationUnavailable('沟通页面读取超时')
        return 1
    async def contact(*args):
        contacts.append(clock[0])
        raise Finished()
    monkeypatch.setattr(ap.asyncio,'sleep',sleep)
    monkeypatch.setattr(ap,'sync_and_follow',sync)
    monkeypatch.setattr(ap,'first_contact',contact)
    manager=NS(account_execution=account,cases=NS(load=lambda cid:case),
        definitions=NS(get=lambda tid:{'config':{'sourcing_mode':'continuous'}}),
        checkpoint=checkpoint,persist=lambda job:None,event=lambda *args:events.append(args[-1]))
    job={'id':'run','mode':'boss_autopilot','params':{'new_contacts':True}}
    if failure_count==1:
        with pytest.raises(Finished):await ap.run_account_executor(manager,job,object())
        assert contacts==[1120]
        assert job['read_recovery']=={}
        assert job['case_results']['case']['status']=='running'
    else:
        with pytest.raises(SourcingError,match='连续 3 次读取超时') as caught:
            await ap.run_account_executor(manager,job,object())
        assert not contacts
        assert job['read_recovery']['case']['next_check_at'] is None
        assert job['read_recovery']['case']['attempts']==3
        # Existing failure reconciliation makes exhausted recovery actionable.
        job.update(status='failed',error=str(caught.value))
        todos=AttentionStore(db).reconcile([job],[])
        assert not any(t['status']!='resolved' for t in todos)
    assert attempts==[1000+120*i for i in range(len(attempts))]


def test_error_trace_contains_locations_without_locals_or_source():
    secret='private-runtime-value-do-not-export'
    try:
        try:raise TimeoutError()
        except TimeoutError as exc:raise bc.ObservationUnavailable('snapshot unavailable') from exc
    except bc.ObservationUnavailable as exc:
        frames=_exception_frames(exc)
    assert frames and all(set(frame)=={'file','function','line'} for frame in frames)
    assert all(frame['file']=='test_observation_recovery.py' and frame['line']>0 for frame in frames)
    assert secret not in json.dumps(frames)


def test_read_retry_is_visible_without_slow_progress_warning():
    job={'id':'run','status':'running','autopilot_stage':'read_retry','events':[{'at':1}],
         'created':1,'next_check_at':500}
    manager=NS(busy=lambda:job,jobs={'run':job},active_session=None,boss_session=None,
        browser_alive=lambda session:False,service_instance_id='service',monitor_heartbeat=1)
    status=Workbench.runtime_status(manager)
    assert status['task_state']=='页面读取待重试'
    assert status['next_check_at']==500 and not status['slow_progress']
