"""结构化日志：分类落盘、隐藏凭据、保留期清理、写入失败不影响执行，以及各处埋点。"""
import json
import logging
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from cloakup import logbook
from cloakup.account_execution import AccountExecution
from cloakup.action_gateway import ActionBlocked, ActionLedger
from cloakup.storage import Database


def read(root, category):
    lines = []
    for path in sorted((root / 'logs').glob(f'{category}-*.jsonl')):
        lines += [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    return lines


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv('CLOAKUP_REPORT_TZ', 'Asia/Shanghai')
    logbook.configure(tmp_path)
    yield tmp_path
    logbook._book = None


def test_writes_one_jsonl_file_per_category_and_day(book):
    at = datetime(2026, 11, 23, 23, 59, 59, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    logbook.log('event', at=at, run_id='r', message='开始主动开聊批次')
    logbook.log('event', at=at + 2, run_id='r', message='次日第一条')
    logbook.log('nonsense', message='未知类别不写')
    assert [p.name for p in sorted((book / 'logs').iterdir())] == ['event-2026-11-23.jsonl', 'event-2026-11-24.jsonl']
    first = read(book, 'event')[0]
    assert first['category'] == 'event' and first['time'].startswith('2026-11-23T23:59:59') and first['run_id'] == 'r'


def test_credentials_never_reach_disk(book, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test-secret-value')
    monkeypatch.setenv('LARK_APP_SECRET', 'lark-secret-value')
    logbook.log('error', message='请求失败 sk-test-secret-value / lark-secret-value')
    text = (next((book / 'logs').iterdir())).read_text(encoding='utf-8')
    assert 'secret-value' not in text and text.count('[已隐藏]') == 2


def test_old_files_are_pruned_after_retention(book):
    folder = book / 'logs'
    folder.mkdir(exist_ok=True)
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    old = folder / f'event-{(today - timedelta(days=31)).isoformat()}.jsonl'
    kept = folder / f'state-{(today - timedelta(days=29)).isoformat()}.jsonl'
    other = folder / 'notes.jsonl'
    for path in (old, kept, other):
        path.write_text('{}\n')
    logbook.log('event', message='触发清理')
    assert not old.exists() and kept.exists() and other.exists()


def test_write_failure_never_breaks_execution(book, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(logbook.Logbook, 'write', broken)
    logbook.log('event', message='不会抛出')


def test_unconfigured_logbook_is_a_no_op(tmp_path):
    logbook._book = None
    logbook.log('event', message='没有配置时不写')
    assert not (tmp_path / 'logs').exists()


def test_cloakup_warnings_and_exceptions_go_to_the_error_log(book):
    logger = logging.getLogger('cloakup.test')
    try:
        raise ValueError('页面读取失败')
    except ValueError:
        logger.exception('执行日报生成失败')
    logger.info('普通信息不进错误日志')
    entry, = read(book, 'error')
    assert entry['level'] == 'ERROR' and entry['exception'] == 'ValueError' and entry['detail'] == '页面读取失败'
    assert entry['frames'][-1]['function'] == 'test_cloakup_warnings_and_exceptions_go_to_the_error_log'


def test_task_status_records_actor_and_reason(book, tmp_path):
    account = AccountExecution(Database(tmp_path))
    account.set_task('task', ['case'], 'running', actor='admin', reason='start_or_resume')
    account.set_task('task', ['case'], 'paused', reason='selection_blocked:draft_present')
    member = account.members()[0]
    assert [(h['status'], h['actor'], h['reason']) for h in member['history']] == [
        ('running', 'admin', 'start_or_resume'), ('paused', 'system', 'selection_blocked:draft_present')]
    entries = read(book, 'state')
    assert [(e['before'], e['after'], e['actor']) for e in entries] == [(None, 'running', 'admin'), ('running', 'paused', 'system')]


def test_action_ledger_transitions_are_logged(book, tmp_path):
    ledger = ActionLedger(Database(tmp_path))
    ident = ledger.claim(task_id='t', resource='case:chat', capability='send_message', identity={'m': 1}, run_id='r')
    ledger.transition(ident, 'executing')
    ledger.transition(ident, 'unknown', reason='回执未同步')
    with pytest.raises(ActionBlocked):
        ledger.claim(task_id='t', resource='case:chat', capability='send_message', identity={'m': 1}, run_id='r')
    states = [(e['state'], e.get('before')) for e in read(book, 'action')]
    assert states == [('claimed', None), ('executing', 'claimed'), ('unknown', 'executing'), ('blocked', None)]
    assert read(book, 'action')[2]['details'] == {'reason': '回执未同步'}


def test_workbench_logs_events_and_stage_changes(book, tmp_path):
    from cloakup.workbench import Workbench
    manager = Workbench(tmp_path)
    try:
        job = {'id': 'j' * 32, 'mode': 'boss_autopilot', 'status': 'running', 'events': [], 'account_runner': True,
               'autopilot_stage': 'sync_history', 'active_case_id': 'case', 'created': time.time()}
        manager.event(job, 'step', '开始主动开聊批次')
        job.update(autopilot_stage='waiting', next_check_at=time.time() + 600, active_case_id=None)
        manager.persist(job)
        manager.persist(job)
        event, = read(tmp_path, 'event')
        assert event['message'] == '开始主动开聊批次' and event['stage'] == 'sync_history' and event['case_id'] == 'case'
        stages = [(e['before_stage'], e['stage']) for e in read(tmp_path, 'state') if e['type'] == 'executor_stage']
        assert stages == [(None, 'sync_history'), ('sync_history', 'waiting')]
    finally:
        manager.close()


def test_decision_hold_logs_the_exception(book, tmp_path):
    from cloakup import decision_runtime as dr
    from cloakup.sourcing import CaseStore
    db = Database(tmp_path)
    cases = CaseStore(tmp_path, db)
    case = cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j')
    person = cases.add_candidate(case, 'p1', 'https://www.zhipin.com/web/chat/index', {'summary': '资料'})
    try:
        raise TimeoutError('gateway timed out')
    except TimeoutError as exc:
        dr.hold(NS(cases=cases), case, person, 'Harness决策未完成：TimeoutError', {'session_id': 's', 'elapsed_ms': 120000}, exc=exc)
    entry, = read(book, 'model')
    assert entry['type'] == 'decision_held' and entry['exception'] == 'TimeoutError' and entry['detail'] == 'gateway timed out'
    assert entry['trace']['elapsed_ms'] == 120000 and entry['candidate_id'] == person['id']
