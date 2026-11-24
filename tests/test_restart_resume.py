"""服务重启：原先运行的任务如实标为暂停，再自动核验环境并恢复；失败时保持暂停并写明原因。"""
import asyncio
from types import SimpleNamespace as NS

import pytest

from cloakup.account_execution import AccountExecution
from cloakup.agent_workspace import StartBlocked
from cloakup.attention import AttentionStore
from cloakup.storage import Database
from cloakup.workbench import Workbench


def test_startup_marks_running_tasks_paused_with_reason(tmp_path, monkeypatch):
    monkeypatch.setenv('CLOAKUP_AUTO_RESUME', '0')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    AccountExecution(Database(tmp_path)).set_task('task', ['case'], 'running', actor='admin', reason='start_or_resume')
    AccountExecution(Database(tmp_path)).set_task('other', ['case2'], 'paused', actor='admin', reason='manual')
    manager = Workbench(tmp_path)
    try:
        members = {m['id']: m for m in manager.account_execution.members()}
        # 之前 11-23 18:50 重建后任务仍显示“运行”，实际没有执行器。
        assert members['task']['status'] == 'paused' and members['task']['history'][-1]['reason'] == 'service_restart'
        assert members['other']['history'][-1]['reason'] == 'manual'
        assert manager.restart_resume == [] and manager.restart_resume_future is None
    finally:
        manager.close()


def make(tmp_path, *, current=True, preflight_passes=True, start_error=None):
    account = AccountExecution(Database(tmp_path))
    account.set_task('task', ['case'], 'paused', reason='service_restart')
    calls = []
    readiness = {'executor_current': current}

    async def test_environment():
        calls.append('test_environment')
        readiness['executor_current'] = preflight_passes

    async def start_account_task(tid, *, actor, reason):
        calls.append(('start', tid, actor, reason))
        if start_error:
            raise start_error
        account.set_task(tid, ['case'], 'running', actor=actor, reason=reason)

    manager = NS(restart_resume=['task'], account_execution=account, latest_readiness=lambda: readiness,
                 test_environment=test_environment, preflight_task=None, start_account_task=start_account_task)
    return manager, account, calls


def status(account):
    member = account.members()[0]
    return member['status'], member['history'][-1]['reason']


def test_resumes_directly_when_the_environment_check_is_still_current(tmp_path):
    manager, account, calls = make(tmp_path)
    assert asyncio.run(Workbench.resume_after_restart(manager, delay=0)) == ['task']
    assert calls == [('start', 'task', 'system', 'auto_resume_after_restart')]
    assert status(account) == ('running', 'auto_resume_after_restart')


def test_runs_the_environment_check_first_when_the_executor_changed(tmp_path):
    manager, account, calls = make(tmp_path, current=False)
    assert asyncio.run(Workbench.resume_after_restart(manager, delay=0)) == ['task']
    assert calls[0] == 'test_environment' and status(account)[0] == 'running'


@pytest.mark.parametrize('kwargs,reason', [
    ({'current': False, 'preflight_passes': False}, 'auto_resume_failed:environment_check_failed'),
    # 11-24 09:26 重建后实际发生的情况：代码更新使发布签名失效。
    ({'start_error': StartBlocked('模型、执行代码或策略依据已变化', 'release_changed')}, 'auto_resume_failed:release_changed'),
    ({'start_error': RuntimeError('boom')}, 'auto_resume_failed:RuntimeError')])
def test_failure_keeps_the_task_paused_with_a_reason(tmp_path, kwargs, reason):
    manager, account, calls = make(tmp_path, **kwargs)
    assert asyncio.run(Workbench.resume_after_restart(manager, delay=0)) == []
    assert status(account) == ('paused', reason)


def test_admin_change_during_the_wait_is_respected(tmp_path):
    manager, account, calls = make(tmp_path)
    account.set_task('task', ['case'], 'paused', actor='admin', reason='manual')
    assert asyncio.run(Workbench.resume_after_restart(manager, delay=0)) == []
    assert calls == [] and status(account) == ('paused', 'manual')


def todos(db):
    return [t for t in AttentionStore(db).reconcile([], [{'id': 'case', 'job': {'label': '岗位A'}, 'candidates': {}}])
            if t['title'] == '服务重启后任务未自动恢复']


def test_failed_auto_resume_raises_an_admin_todo_until_the_task_runs_again(tmp_path):
    db = Database(tmp_path)
    account = AccountExecution(db)
    account.set_task('task', ['case'], 'paused', reason='auto_resume_failed:release_changed')
    [todo] = todos(db)
    assert todo['status'] == 'pending' and todo['kind'] == 'execution' and todo['task_id'] == 'task'
    assert todo['failure_code'] == 'release_changed' and todo['job_label'] == '岗位A'
    assert '重新建立草案' in todo['detail'] and '没有操作网站' in todo['detail']
    # 只能由真实状态解除，不能手动勾掉。
    with pytest.raises(ValueError):
        AttentionStore(db).update(todo['id'], {'status': 'resolved', 'note': 'x'})
    account.set_task('task', ['case'], 'running', actor='admin', reason='start_or_resume')
    [todo] = todos(db)
    assert todo['status'] == 'resolved' and todo['auto_resolved']


def test_unknown_failure_code_still_tells_the_admin_what_to_do(tmp_path):
    db = Database(tmp_path)
    AccountExecution(db).set_task('task', ['case'], 'paused', reason='auto_resume_failed:RuntimeError')
    [todo] = todos(db)
    assert 'RuntimeError' in todo['detail'] and '手动恢复' in todo['detail']


def test_ordinary_pauses_do_not_raise_the_todo(tmp_path):
    db = Database(tmp_path)
    AccountExecution(db).set_task('task', ['case'], 'paused', actor='admin', reason='manual')
    assert todos(db) == []
