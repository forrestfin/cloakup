"""执行日报：只读本地记录，归类异常与自动修复情况，给出操作分析；次日生成、缺失补生成。"""
from datetime import date, datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from cloakup import daily_report as dr
from cloakup.sourcing import CaseStore
from cloakup.storage import Database

TZ = ZoneInfo('Asia/Shanghai')
DAY = date(2026, 11, 20)  # 周五，默认执行时段 09:00–12:00、13:30–18:00


def at(clock, day=DAY):
    h, m = map(int, clock.split(':'))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).timestamp()


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path)
    cases = CaseStore(tmp_path, db)
    case = cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j')
    cases.bind_job(case, {'title': '交付顾问', 'requirements': '职业要求'})
    person = cases.add_candidate(case, 'p1', 'https://www.zhipin.com/web/chat/index', {'summary': '职业资料'})
    person['outbox'] = [
        {'id': 'm1', 'intent': 'first_contact', 'status': 'sent', 'claimed_at': at('09:20'), 'sent_at': at('09:20')},
        {'id': 'm2', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('10:10'), 'sent_at': at('10:10')},
        {'id': 'm3', 'intent': 'request_resume', 'status': 'delivery_unknown', 'claimed_at': at('15:00')},
    ]
    person['inbox'] = [{'external_id': 'a', 'text': '你好', 'observed_at': at('10:00')},
                       {'external_id': 'b', 'text': '还在吗', 'observed_at': at('17:50')}]
    case['audit'] = [{'at': at('11:00'), 'kind': 'resume_file_archived'},
                     {'at': at('15:00'), 'kind': 'delivery_unknown'}]
    cases.save(case)
    run = {'id': 'r' * 32, 'mode': 'boss_autopilot', 'account_runner': True, 'created': at('08:55'), 'events': []}
    db.put('run', run['id'], run)

    def event(clock, message, kind='step'):
        db.append(run['id'], {'at': at(clock), 'kind': kind, 'message': message})
    event('09:05', '开始主动开聊批次，最多提交 2 次')
    event('09:21', '主动开聊批次结束，已提交 1 次，返回收件检查')
    event('09:40', '候选人 暂不联系，继续查看下一张推荐卡片')
    event('10:05', '会话选择暂未完成，将按退避间隔安全重读；暂停该岗位新增开聊（恢复核验 1/3）')
    event('10:06', '会话核验仍未通过，已暂停任务，请检查执行浏览器与具体失败环节（恢复核验 3/3）')
    event('10:30', '单条会话暂时打不开，已先放下这一条并继续其他会话；稍后只读复核（恢复核验 3/3）')
    event('10:40', '岗位有未核实的隔离会话，暂缓新增开聊')
    event('11:55', '本轮已检查 3 条当前加载会话；没有新回复则等待，不连续追聊。')
    event('13:40', '张三 的资料在核对时发生变化，未打招呼；先放下这一位，返回收件检查')
    event('14:00', '发布版本已变化，任务暂停发送；请暂停并恢复任务以绑定新版本')
    event('15:30', '开始主动开聊批次，最多提交 1 次')
    db.put('account_task', 'task', {'id': 'task', 'case_ids': [case['id']], 'status': 'running', 'history': [
        {'at': at('08:50'), 'status': 'running'}, {'at': at('10:06'), 'status': 'paused'}, {'at': at('10:25'), 'status': 'running'}]})
    manager = NS(root=tmp_path, db=db, cases=cases)
    return manager, case


def test_report_classifies_anomalies_and_whether_they_self_heal(env):
    manager, case = env
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 3600 * 2)
    rows = {r['category']: r for r in report['anomalies']}
    assert set(rows) >= {'selection', 'isolation', 'profile_changed', 'release_changed', 'delivery_unknown'}
    assert rows['selection']['outcomes'] == {'retry': 1, 'paused': 1, 'isolated': 1}
    assert rows['selection']['fixability'] == 'auto' and rows['delivery_unknown']['fixability'] == 'manual'
    # 10:06 暂停，10:25 手动恢复后 10:30 立刻只放下单条：标记为“恢复后立刻通过”。
    pause, = report['pauses']
    assert pause['cause'] == 'selection' and pause['minutes'] == 19 and pause['quick_pass_after_resume']
    assert rows['selection']['pauses'] == 1 and rows['selection']['quick_pass_after_resume'] == 1
    assert report['anomalies'][0]['category'] == 'selection'  # 造成暂停的排在最前


def test_report_measures_outcomes_latency_and_idle_time(env):
    manager, case = env
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 3600 * 2)
    totals = report['outcomes']['totals']
    assert totals == {'first_contacts': 1, 'messages_sent': 1, 'replies': 2, 'resumes_archived': 1,
                      'delivery_unknown': 1, 'opt_out': 0, 'handoff': 0}
    assert report['outcomes']['reply_latency']['median_minutes'] == 10
    assert report['outcomes']['unanswered_replies'] == 1
    ops = report['operations']
    assert ops['batches'] == {'started': 2, 'submitted': 1, 'skipped_after_review': 1}
    assert ops['outreach_held_minutes']['isolation'] == 290  # 10:40 → 15:30 下一次开聊批次
    gaps = {(g['from_clock'], g['to_clock']) for g in ops['idle_gaps']}
    assert ('14:00', '15:30') in gaps and ('15:30', '18:00') in gaps
    assert report['window_minutes'] == 450 and report['complete']


def test_suggestions_and_markdown_cover_the_three_questions(env):
    manager, case = env
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 3600 * 2)
    text = '\n'.join(report['suggestions'])
    assert '恢复后立刻通过' in text and '隔离会话' in text and '送达未确认' in text
    assert '没有任何执行记录' in text and '仍未处理' in text and '发布版本不一致' in text
    markdown = dr.render_markdown(report)
    assert '## 异常与自动修复' in markdown and '## 操作分析' in markdown and '## 建议' in markdown
    assert '| 会话选择恢复 |' in markdown and '恢复后立刻通过' in markdown


def test_generate_persists_json_and_markdown(env):
    manager, case = env
    report = dr.generate(manager, DAY, now=at('23:00') + 3600 * 2)
    stored = manager.db.get(dr.KIND, '2026-11-20')
    assert stored['markdown'] == report['markdown']
    assert (manager.root / 'reports' / '2026-11-20.md').read_text(encoding='utf-8') == report['markdown']
    assert dr.listing(manager.db)[0]['day'] == '2026-11-20'


def test_ensure_backfills_missed_days_after_midnight_only(env, monkeypatch):
    manager, case = env
    monkeypatch.setenv('CLOAKUP_REPORT_TZ', 'Asia/Shanghai')
    # 当天 23:00：前一天之前没有运行记录，当天尚未结束，不生成。
    assert dr.ensure(manager, now=at('23:00')) == []
    # 次日 00:05 仍在缓冲期内；00:20 生成前一天。
    assert dr.ensure(manager, now=at('00:05', date(2026, 11, 21))) == []
    assert dr.ensure(manager, now=at('00:20', date(2026, 11, 21))) == ['2026-11-20']
    assert dr.ensure(manager, now=at('09:00', date(2026, 11, 21))) == []
    # 停机两天后恢复：缺失的日期一起补上，已有的完整报告不重算。
    assert dr.ensure(manager, now=at('08:00', date(2026, 11, 24))) == ['2026-11-21', '2026-11-22', '2026-11-23']


def test_incomplete_report_is_regenerated_once_the_day_ends(env, monkeypatch):
    manager, case = env
    monkeypatch.setenv('CLOAKUP_REPORT_TZ', 'Asia/Shanghai')
    assert not dr.generate(manager, DAY, now=at('12:00'))['complete']
    assert dr.ensure(manager, now=at('00:20', date(2026, 11, 21))) == ['2026-11-20']
    assert manager.db.get(dr.KIND, '2026-11-20')['complete']


def test_report_does_not_touch_business_state(env):
    manager, case = env
    before = manager.cases.load(case['id'])
    runs = manager.db.list('run')
    members = manager.db.list('account_task')
    dr.generate(manager, DAY, now=at('23:00') + 3600 * 2)
    assert manager.cases.load(case['id']) == before
    assert manager.db.list('run') == runs and manager.db.list('account_task') == members


async def test_workbench_checks_reports_at_most_every_interval(env, monkeypatch):
    from cloakup.workbench import Workbench
    manager, case = env
    calls = []
    monkeypatch.setattr(dr, 'ensure', lambda m: calls.append(m) or ['2026-11-20'])
    assert await Workbench.ensure_daily_reports(manager) == ['2026-11-20']
    assert await Workbench.ensure_daily_reports(manager) == []
    assert len(calls) == 1


async def test_report_failure_never_breaks_the_monitor(env, monkeypatch):
    from cloakup.workbench import Workbench
    manager, case = env
    def broken(m): raise RuntimeError('boom')
    monkeypatch.setattr(dr, 'ensure', broken)
    assert await Workbench.ensure_daily_reports(manager) == []


def test_cli_prints_the_report(env, capsys):
    from cloakup.cli import main
    manager, case = env
    main(['report', '--root', str(manager.root), '--day', '2026-11-20'])
    out = capsys.readouterr().out
    assert out.startswith('# 执行日报 2026-11-20') and '## 建议' in out


def test_recorded_pause_actor_and_reason_replace_guessing(env):
    """“手动暂停或原因未记录”：状态历史记了 actor/reason 后，日报直接写明。"""
    manager, case = env
    manager.db.put('account_task', 'task', {'id': 'task', 'case_ids': [case['id']], 'status': 'running', 'history': [
        {'at': at('08:50'), 'status': 'running', 'actor': 'admin', 'reason': 'start_or_resume'},
        {'at': at('12:54'), 'status': 'paused', 'actor': 'admin', 'reason': 'manual'},
        {'at': at('12:55'), 'status': 'running', 'actor': 'admin', 'reason': 'start_or_resume'},
        {'at': at('16:02'), 'status': 'paused', 'actor': 'system', 'reason': 'executor_error:TimeoutError'},
        {'at': at('16:05'), 'status': 'running', 'actor': 'admin', 'reason': 'start_or_resume'}]})
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 7200)
    assert [(p['cause'], p['cause_name'], p['resumed_by']) for p in report['pauses']] == [
        ('admin', '管理员暂停', 'admin'), ('task_error', '任务异常停止（TimeoutError）', 'admin')]


def test_existing_report_file_is_rewritten_when_the_folder_moves(env, monkeypatch):
    manager, case = env
    monkeypatch.setenv('CLOAKUP_REPORT_TZ', 'Asia/Shanghai')
    dr.ensure(manager, now=at('00:20', date(2026, 11, 21)))
    path = manager.root / 'reports' / '2026-11-20.md'
    path.unlink()
    assert dr.ensure(manager, now=at('00:30', date(2026, 11, 21))) == []
    assert path.read_text(encoding='utf-8') == manager.db.get(dr.KIND, '2026-11-20')['markdown']


def _hu(env):
    """11-23 反馈：对方从未回复，首聊后 11-18 23:08、11-22 15:31、11-23 18:27 连续三次追聊。"""
    manager, case = env
    person = manager.cases.add_candidate(case, 'cand-a', 'https://www.zhipin.com/web/chat/index', {'summary': '资料', 'display_name': '候选人甲'})
    d18, d22, d23 = date(2026, 11, 18), date(2026, 11, 22), date(2026, 11, 23)
    person['outbox'] = [
        {'id': 'g', 'intent': 'first_contact', 'status': 'sent', 'sent_at': at('10:00', date(2026, 11, 16)), 'text': '岗位问候', 'receipt': {'delivery_status': 'read'}},
        {'id': 'n1', 'status': 'sent', 'sent_at': at('23:08', d18), 'text': '您好，想再确认一下您是否愿意了解这个机会', 'receipt': {'delivery_status': 'read'}},
        {'id': 'n2', 'status': 'sent', 'sent_at': at('15:31', d22), 'text': '这边先看简历即可，联系方式可以之后按您的意愿再交流。', 'receipt': {'delivery_status': 'delivered'}},
        {'id': 'n3', 'status': 'sent', 'sent_at': at('18:27', d23), 'text': '最后跟进一次', 'receipt': {'delivery_status': 'delivered'}}]
    person['inbox'] = []
    manager.cases.save(case)
    return manager, d18, d22, d23


def test_conversation_quality_flags_the_pushy_follow_ups(env):
    manager, d18, d22, d23 = _hu(env)
    def flagged(day):
        report = dr.build(manager.db, manager.cases.list(), day, tz=TZ, now=at('23:00', day) + 7200)
        return next(i for i in report['conversation']['issues'] if i['name'] == '候选人甲')['issues'], report
    issues, _ = flagged(d18)
    assert issues == {'night_proactive': '23:08'}
    issues, _ = flagged(d22)
    assert issues['off_topic'] == '15:31' and issues['unanswered_streak'] == 3
    issues, report = flagged(d23)
    assert issues['unanswered_streak'] == 4 and issues['short_gap'] == '27 小时' and issues['unread_chase'] == '18:27'
    markdown = dr.render_markdown(report)
    assert '## 对话体验' in markdown and '| 候选人甲 |' in markdown and '对话体验：' in '\n'.join(report['suggestions'])


def test_reply_to_a_candidate_message_is_not_flagged(env):
    manager, case = env
    person = manager.cases.add_candidate(case, 'ok', 'https://www.zhipin.com/web/chat/index', {'summary': '资料', 'display_name': '李先生'})
    person['outbox'] = [{'id': 'g', 'status': 'sent', 'sent_at': at('10:00'), 'text': '岗位问候', 'receipt': {'delivery_status': 'read'}},
                        {'id': 'a', 'status': 'sent', 'sent_at': at('21:10'), 'text': '可以的，方便时发份简历', 'receipt': {'delivery_status': 'read'}}]
    person['inbox'] = [{'external_id': 'x', 'text': '请问需要联系方式吗', 'observed_at': at('21:00')}]
    manager.cases.save(case)
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 7200)
    assert all(i['name'] != '李先生' for i in report['conversation']['issues'])


def test_new_outreach_and_isolation_event_texts_are_classified():
    from cloakup.daily_report import _classify
    assert _classify({'message':'主动开聊入口暂不可用（未找到唯一的实际页面控件：推荐），先继续处理收件，约 15 分钟后重试开聊'})==('outreach_entry','retry')
    assert _classify({'message':'岗位有未核实的隔离会话（隔离不足 2 小时），暂缓新增开聊'})==('isolation','outreach_held')


def test_job_validation_lapse_is_classified_and_reported(env):
    # 部分岗位验收失效：只停这些岗位的发送，不是任务暂停；日报能归类并渲染，不因未知类别出错。
    manager, case = env
    message = '部分岗位验收已失效，这些岗位暂停发送，其他岗位继续'
    assert dr._classify({'message': message}) == ('release', 'job_not_validated')
    manager.db.append('r' * 32, {'at': at('16:00'), 'kind': 'step', 'message': message})
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 7200)
    row = next(r for r in report['anomalies'] if r['category'] == 'release')
    assert row['outcomes'] == {'job_not_validated': 1} and row['fixability'] == 'manual' and row['pauses'] == 0
    assert '部分岗位验收失效' in dr.render_markdown(report)


def test_job_change_event_is_classified_and_reported(env):
    # 岗位增量变更在安全点生效：归入“岗位变更生效”，不算暂停，日报能渲染。
    manager, case = env
    message = '岗位变更已在安全点生效：加入岗位「数据顾问」'
    assert dr._classify({'message': message}) == ('job_change', 'applied')
    manager.db.append('r' * 32, {'at': at('16:00'), 'kind': 'step', 'message': message})
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 7200)
    row = next(r for r in report['anomalies'] if r['category'] == 'job_change')
    assert row['outcomes'] == {'applied': 1} and row['pauses'] == 0
    assert '岗位变更生效' in dr.render_markdown(report)


def test_job_exhausted_event_is_classified_and_reported(env):
    # 岗位推荐页当天没人可联系、额度让给其他岗位：归入首聊调度，不算暂停，日报能渲染。
    manager, case = env
    message = '该岗位今天推荐页暂无可联系的人，额度让给其他岗位'
    assert dr._classify({'message': message}) == ('outreach', 'job_exhausted')
    manager.db.append('r' * 32, {'at': at('16:00'), 'kind': 'step', 'message': message})
    report = dr.build(manager.db, manager.cases.list(), DAY, tz=TZ, now=at('23:00') + 7200)
    row = next(r for r in report['anomalies'] if r['category'] == 'outreach')
    assert row['outcomes'] == {'job_exhausted': 1} and row['fixability'] == 'auto' and row['pauses'] == 0
    assert '岗位推荐页暂无可联系的人' in dr.render_markdown(report)
