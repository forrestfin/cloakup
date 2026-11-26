"""11-25 工作台“今天”首页：任务状态人话、各岗位今日数字（与日报、额度同口径）、待办前 5 条、最近动态。"""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cloakup import daily_report as dr
from cloakup import job_quota, today
from cloakup.autonomy import day_key
from cloakup.sourcing import CaseStore
from cloakup.storage import Database

TZ = ZoneInfo('Asia/Shanghai')
DAY = date(2026, 11, 25)
SCHEDULE = {'enabled': True, 'timezone': 'Asia/Shanghai', 'weekdays': [0, 1, 2, 3, 4, 5, 6],
            'windows': [{'start': '09:00', 'end': '12:00'}, {'start': '13:30', 'end': '23:30'}],
            'work_minutes': 50, 'rest_min_minutes': 5, 'rest_max_minutes': 10}
NAME = '张三丰'


def at(clock, day=DAY):
    h, m = map(int, clock.split(':'))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).timestamp()


NOW = at('15:00')
YESTERDAY = DAY - timedelta(days=1)


class Env:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path)
        self.store = CaseStore(tmp_path, self.db)
        self.states = {}

    def case(self, title):
        case = self.store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=' + title)
        self.store.bind_job(case, {'title': title, 'requirements': '职业要求'})
        self.store.save(case)
        return case

    def person(self, case, pid, *, inbox=(), outbox=(), history=None, status='contacted'):
        person = self.store.add_candidate(case, pid, 'https://www.zhipin.com/web/chat/index', {'summary': '职业资料', 'display_name': NAME})
        person['inbox'] = [{'external_id': pid + str(i), 'text': '你好', 'observed_at': t} for i, t in enumerate(inbox)]
        person['outbox'] = [dict(m) for m in outbox]
        person['history'] = history if history is not None else []
        person['status'] = status
        return person

    def task(self, case_ids, *, status='running', history=None, limit=10, tid='t' * 32, engine='boss_sourcing'):
        cfg = {'name': '寻访任务', 'engine': engine, 'case_ids': list(case_ids), 'execution': {'daily_new_limit': limit, 'poll_seconds': 60, 'max_messages_per_candidate_day': 3}, 'schedule': SCHEDULE}
        self.db.put('task', tid, {'id': tid, 'revision': 1, 'config': cfg})
        self.db.put('account_task', tid, {'id': tid, 'case_ids': list(case_ids), 'status': status,
                                          'history': history or [{'at': at('08:00', YESTERDAY), 'status': 'running', 'actor': 'admin', 'reason': None}]})
        return tid

    def availability(self, cid):
        return self.states.get(cid, {'allowed': True, 'state': 'open', 'reason': '', 'next_at': None})

    def build(self, now=NOW):
        cases = self.store.list()
        return today.build(self.db, self.store, cases, self.db.list('account_task'), self.availability, now=now)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _job(result, cid):
    return next(j for t in result['tasks'] for j in t['jobs'] if j['case_id'] == cid)


def test_numbers_match_daily_report_columns(env):
    case = env.case('交付顾问')
    env.person(case, 'p1', inbox=[at('10:00'), at('14:00')], outbox=[
        {'id': 'm1', 'intent': 'first_contact', 'status': 'sent', 'claimed_at': at('09:20'), 'sent_at': at('09:20')},
        {'id': 'm2', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('10:10'), 'sent_at': at('10:10')}])
    case['audit'] = [{'at': at('11:00'), 'kind': 'resume_file_archived'}]
    env.store.save(case);env.task([case['id']])
    job = _job(env.build(), case['id'])
    start, end = dr.day_bounds(DAY, dr.report_timezone())
    row = dr.outcomes([env.store.load(case['id'])], start, end)[0][0]
    assert (job['incoming'], job['replies_sent'], job['resumes'], job['first_contacts_sent']) == (row['replies'], row['messages_sent'], row['resumes_archived'], row['first_contacts'])
    assert (job['incoming'], job['replies_sent'], job['resumes'], job['first_contacts_sent']) == (2, 1, 1, 1)
    assert dr._outcomes is dr.outcomes


def test_quota_columns_come_from_task_quota(env):
    case = env.case('交付顾问')
    env.person(case, 'p1', outbox=[{'id': 'm1', 'intent': 'first_contact', 'status': 'sent', 'claimed_at': at('09:20'), 'sent_at': at('09:20')}])
    env.person(case, 'p2', outbox=[{'id': 'm2', 'intent': 'first_contact', 'status': 'delivery_unknown', 'claimed_at': at('09:30')}])
    env.store.save(case);tid = env.task([case['id']], limit=7)
    job = _job(env.build(), case['id'])
    quota = job_quota.task_quota(env.store, env.db.get('task', tid), day_key(NOW))
    assert (job['quota_used'], job['quota_share']) == (quota['usage'][case['id']], quota['shares'][case['id']]) == (2, 7)
    assert job['first_contacts_sent'] == 1


def test_counts_only_today_across_midnight(env):
    case = env.case('交付顾问')
    env.person(case, 'p1', inbox=[at('23:59', YESTERDAY), at('00:01')], outbox=[
        {'id': 'm1', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('23:59', YESTERDAY), 'sent_at': at('23:59', YESTERDAY)},
        {'id': 'm2', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('00:01'), 'sent_at': at('00:01')}])
    env.store.save(case);env.task([case['id']])
    result = env.build()
    job = _job(result, case['id'])
    assert result['day'] == '2026-11-25'
    assert (job['incoming'], job['replies_sent']) == (1, 1)
    assert all(a['at'] >= at('00:00') for a in result['activity']) and len(result['activity']) == 1


def test_greetings_and_unanswered(env):
    case = env.case('交付顾问')
    env.person(case, 'p1', inbox=[at('10:00')], history=[{'direction': 'in', 'text': '你好'}])
    env.person(case, 'p2', inbox=[at('11:00')], history=[{'direction': 'in', 'text': '你好'}, {'direction': 'out', 'text': '您好'}],
               outbox=[{'id': 'm1', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('11:05'), 'sent_at': at('11:05')}])
    env.person(case, 'p3', inbox=[at('10:00', YESTERDAY)], history=[{'direction': 'in', 'text': '你好'}])
    env.person(case, 'p4', inbox=[at('12:00')], history=[{'direction': 'out', 'text': '您好'}, {'direction': 'in', 'text': '你好'}],
               outbox=[{'id': 'm2', 'intent': 'first_contact', 'status': 'sent', 'claimed_at': at('09:00', YESTERDAY), 'sent_at': at('09:00', YESTERDAY)}])
    env.store.save(case);env.task([case['id']])
    job = _job(env.build(), case['id'])
    assert (job['greetings'], job['greetings_unanswered']) == (2, 1)


def test_waiting_now_counts_conversations_not_messages(env):
    case = env.case('交付顾问')
    env.person(case, 'p1', inbox=[at('10:00'), at('10:01'), at('10:02')], history=[{'direction': 'in', 'text': 'a'}] * 3)
    env.person(case, 'p2', inbox=[at('10:00')], status='closed')
    env.person(case, 'p3', inbox=[at('10:00')], outbox=[{'id': 'm1', 'intent': 'answer', 'status': 'delivery_unknown', 'claimed_at': at('10:05')}])
    env.store.save(case);env.task([case['id']])
    assert _job(env.build(), case['id'])['waiting_now'] == 1


@pytest.mark.parametrize('states,state,label', [
    ([{'state': 'open'}], 'running', '运行中'),
    ([{'state': 'outside_window', 'next_at': at('13:30'), 'reason': '当前不在任务执行时段'}], 'outside_window', '等待执行时段'),
    ([{'state': 'resting', 'next_at': at('15:08'), 'reason': '执行器休息中'}], 'resting', '休息中'),
    ([{'state': 'release_changed', 'reason': '发布版本已变化，请暂停并恢复任务以绑定新版本'}, {'state': 'open'}], 'blocked', '受阻'),
    ([{'state': 'job_not_validated', 'reason': '该岗位验收已失效，只停这个岗位'}] * 2, 'blocked', '受阻'),
    ([{'state': 'planning', 'reason': '等待任务规划完成'}, {'state': 'open'}], 'blocked', '受阻'),
    ([{'state': 'job_not_validated', 'reason': '该岗位验收已失效，只停这个岗位'}, {'state': 'open'}], 'running', '运行中'),
])
def test_status_mapping(env, states, state, label):
    cases = [env.case('岗位%d' % i) for i in range(len(states))]
    for case, s in zip(cases, states):
        env.states[case['id']] = {'allowed': s['state'] == 'open', 'reason': s.get('reason', ''), 'next_at': s.get('next_at'), **s}
    env.task([c['id'] for c in cases])
    task = env.build()['tasks'][0]
    assert (task['state'], task['label']) == (state, label)
    assert task['windows'] == '09:00–12:00、13:30–23:30' and task['in_window'] is True and task['name'] == '寻访任务'
    if state == 'outside_window':assert '13:30' in task['detail'] and task['next_at'] == at('13:30')
    if state == 'resting':assert '15:08' in task['detail']
    if state == 'blocked':assert task['detail'] == states[0]['reason']


def test_admin_pause_says_who_and_when(env):
    case = env.case('交付顾问')
    env.task([case['id']], status='paused', history=[{'at': at('08:00'), 'status': 'running', 'actor': 'admin', 'reason': None},
                                                     {'at': at('14:12'), 'status': 'paused', 'actor': 'admin', 'reason': None}])
    task = env.build()['tasks'][0]
    assert (task['state'], task['label']) == ('paused_admin', '暂停中')
    assert '管理员' in task['detail'] and '14:12' in task['detail']


def test_system_pause_uses_todo_wording(env):
    case = env.case('交付顾问')
    tid = env.task([case['id']], status='paused', history=[{'at': at('14:12'), 'status': 'paused', 'actor': 'system', 'reason': 'selection_blocked:editor_unavailable'}])
    env.db.put('attention', 'a1', {'id': 'a1', 'kind': 'execution_recovery', 'title': '会话选择恢复未通过，任务已暂停', 'detail': '请检查执行浏览器', 'status': 'pending', 'task_id': tid, 'created_at': at('14:12')})
    env.db.put('attention', 'a0', {'id': 'a0', 'kind': 'execution', 'title': '旧的已处理', 'detail': '', 'status': 'resolved', 'task_id': tid, 'created_at': at('14:13')})
    task = env.build()['tasks'][0]
    assert (task['state'], task['label']) == ('paused_system', '暂停中')
    assert task['detail'] == '会话选择恢复未通过，任务已暂停：请检查执行浏览器' and task['next_at'] is None
    assert re.search(r'[a-z_]{6,}', task['detail']) is None


def test_build_does_not_reload_cases_but_matches_task_quota(env):
    # 11-25 评审：首页每次轮询不应让 task_quota 再用 store.list() 加载全部岗位两遍
    case = env.case('交付顾问');other = env.case('数据工程师')
    env.person(case, 'p1', outbox=[{'id': 'm1', 'intent': 'first_contact', 'status': 'sent', 'claimed_at': at('09:20'), 'sent_at': at('09:20')}])
    env.person(case, 'p2', outbox=[{'id': 'm2', 'intent': 'first_contact', 'status': 'delivery_unknown', 'claimed_at': at('09:30')}])
    env.person(other, 'p3', outbox=[{'id': 'm3', 'intent': 'first_contact', 'status': 'sent', 'claimed_at': at('09:40'), 'sent_at': at('09:40')}])
    env.store.save(case);env.store.save(other);tid = env.task([case['id'], other['id']], limit=7)
    cases = env.store.list()
    expected = job_quota.task_quota(env.store, env.db.get('task', tid), day_key(NOW))

    class NoList:
        def list(self):raise AssertionError('build() 不应重新加载岗位')
    result = today.build(env.db, NoList(), cases, env.db.list('account_task'), env.availability, now=NOW)
    jobs = result['tasks'][0]['jobs']
    assert [(j['quota_used'], j['quota_share']) for j in jobs] == [(expected['usage'][j['case_id']], expected['shares'][j['case_id']]) for j in jobs]
    assert [j['quota_used'] for j in jobs] == [2, 1]


def test_system_pause_ignores_unrelated_todo_and_shows_retry(env):
    # 11-25 评审：系统暂停原因只取会暂停任务的待办（标题＋说明），较新的问答类待办不算；会自动再试时写下次时间
    case = env.case('交付顾问')
    tid = env.task([case['id']], status='paused', history=[{'at': at('14:12'), 'status': 'paused', 'actor': 'system', 'reason': 'page_unavailable'}])
    env.db.put('attention', 'b', {'id': 'b', 'kind': 'browser_health', 'title': 'BOSS 沟通页打不开，任务已暂停', 'detail': '系统会定时重新打开沟通页', 'status': 'pending',
                                  'task_id': tid, 'created_at': at('14:12'), 'next_check_at': at('15:20')})
    env.db.put('attention', 'q', {'id': 'q', 'kind': 'question', 'title': '候选人问了岗位地点', 'detail': '请补答', 'status': 'pending', 'task_id': tid, 'created_at': at('14:30')})
    task = env.build()['tasks'][0]
    assert task['state'] == 'paused_system'
    assert task['detail'].startswith('BOSS 沟通页打不开，任务已暂停：系统会定时重新打开沟通页') and '15:20' in task['detail']
    assert '候选人问了岗位地点' not in task['detail'] and task['next_at'] == at('15:20')
    env.db.put('attention', 'b', {**env.db.get('attention', 'b'), 'detail': '长' * 300})
    assert len(env.build()['tasks'][0]['detail']) <= 140


def test_system_pause_without_todo_says_system(env):
    case = env.case('交付顾问')
    env.task([case['id']], status='paused', history=[{'at': at('14:12'), 'status': 'paused', 'actor': 'system', 'reason': 'page_unavailable'}])
    task = env.build()['tasks'][0]
    assert task['state'] == 'paused_system' and task['detail'] == '系统暂停'


def test_paused_task_still_counts_waiting_conversations(env):
    case = env.case('交付顾问')
    env.person(case, 'p1', inbox=[at('10:00')], history=[{'direction': 'in', 'text': 'a'}])
    env.store.save(case)
    env.task([case['id']], status='paused', history=[{'at': at('14:12'), 'status': 'paused', 'actor': 'admin', 'reason': None}])
    task = env.build()['tasks'][0]
    assert task['jobs'][0]['waiting_now'] == 1 and task['totals']['waiting_now'] == 1


def test_todos_top_five_unresolved_newest_first(env):
    case = env.case('交付顾问');env.task([case['id']])
    for i in range(7):
        env.db.put('attention', 'a%d' % i, {'id': 'a%d' % i, 'kind': 'question', 'title': '待办%d' % i, 'detail': '说明%d' % i, 'status': 'pending',
                                            'job_label': '交付顾问', 'created_at': at('10:%02d' % i)})
    env.db.put('attention', 'z', {'id': 'z', 'kind': 'question', 'title': '已解决', 'detail': '', 'status': 'resolved', 'created_at': at('11:00')})
    todos = env.build()['todos']
    assert [t['id'] for t in todos] == ['a6', 'a5', 'a4', 'a3', 'a2']
    assert set(todos[0]) == {'id', 'title', 'job_label', 'created_at', 'detail'} and todos[0]['job_label'] == '交付顾问'


def test_activity_plain_words_without_names(env):
    case = env.case('交付顾问')
    outbox = [{'id': 'f', 'intent': 'first_contact', 'status': 'sent', 'claimed_at': at('09:20'), 'sent_at': at('09:20')},
              {'id': 'r', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('10:10'), 'sent_at': at('10:10')},
              {'id': 'y', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('10:10', YESTERDAY), 'sent_at': at('10:10', YESTERDAY)}]
    outbox += [{'id': 'x%d' % i, 'intent': 'answer', 'status': 'sent', 'claimed_at': at('08:%02d' % i), 'sent_at': at('08:%02d' % i)} for i in range(12)]
    env.person(case, 'p1', outbox=outbox)
    case['audit'] = [{'at': at('11:00'), 'kind': 'resume_file_archived', 'candidate_id': 'p1'}]
    env.store.save(case);env.task([case['id']])
    activity = env.build()['activity']
    assert len(activity) == 10 and [a['at'] for a in activity] == sorted((a['at'] for a in activity), reverse=True)
    assert [a['text'] for a in activity[:3]] == ['收到一份简历', '回复了一位候选人', '向一位候选人发出首聊']
    assert all(a['job_label'] == '交付顾问' for a in activity[:3])
    assert all(NAME not in a['text'] for a in activity) and all(a['at'] >= at('00:00') for a in activity)


def test_activity_task_pause_and_page_health(env):
    case = env.case('交付顾问')
    env.task([case['id']], history=[{'at': at('08:00'), 'status': 'running', 'actor': 'admin', 'reason': None},
                                    {'at': at('12:00'), 'status': 'paused', 'actor': 'admin', 'reason': None},
                                    {'at': at('13:00'), 'status': 'paused', 'actor': 'system', 'reason': 'x'},
                                    {'at': at('13:40'), 'status': 'running', 'actor': 'system', 'reason': None}])
    env.db.put('attention', 'b', {'id': 'b', 'kind': 'browser_health', 'title': '执行浏览器的 BOSS 页面打不开', 'detail': '', 'status': 'resolved',
                                  'created_at': at('14:00'), 'updated_at': at('14:20')})
    texts = [a['text'] for a in env.build()['activity']]
    assert texts == ['BOSS 页面已恢复', 'BOSS 页面打不开，系统正在自动重试', '任务恢复运行', '系统暂停任务', '管理员暂停任务', '任务开始运行']


def test_no_sourcing_task_returns_empty(env):
    case = env.case('交付顾问')
    env.task([case['id']], engine='web')
    result = env.build()
    assert result['tasks'] == [] and result['activity'] == [] and result['day'] == '2026-11-25'


def test_sourcing_task_without_member_record_is_skipped(env):
    case = env.case('交付顾问')
    env.task([case['id']])
    result = today.build(env.db, env.store, env.store.list(), [], env.availability, now=NOW)
    assert result['tasks'] == []


def test_todos_follow_attention_order_login_first(env):
    # 11-25 终审：首页待办按待办中心同一排序（登录／验证优先），较旧的登录待办不被 5 条较新的问题挤出
    case = env.case('交付顾问');env.task([case['id']])
    env.db.put('attention', 'login', {'id': 'login', 'kind': 'login', 'title': '需要重新登录', 'detail': '', 'status': 'pending', 'run_id': 'r1', 'created_at': at('08:00')})
    for i in range(5):
        env.db.put('attention', 'q%d' % i, {'id': 'q%d' % i, 'kind': 'question', 'title': '问题%d' % i, 'detail': '', 'status': 'pending', 'created_at': at('10:%02d' % i)})
    calls = []
    original = env.db.list
    def counting(kind):
        calls.append(kind);return original(kind)
    env.db.list = counting
    todos = env.build()['todos']
    assert [t['id'] for t in todos] == ['login', 'q4', 'q3', 'q2', 'q1']
    assert calls.count('attention') == 1


def test_todos_hide_candidate_name_on_home_page(env):
    # 11-25 终审：会话类待办标题带候选人显示名；首页只写“一位候选人”，待办中心原样保留
    case = env.case('交付顾问');env.person(case, 'p7');env.store.save(case);env.task([case['id']])
    env.db.put('attention', 'iso', {'id': 'iso', 'kind': 'isolated_chat', 'title': NAME + ' 的会话一天多都没能打开', 'detail': '请在 BOSS 上找到' + NAME + '的会话',
                                    'status': 'pending', 'case_id': case['id'], 'candidate_id': 'p7', 'created_at': at('10:00')})
    env.db.put('attention', 'other', {'id': 'other', 'kind': 'question', 'title': NAME + '无关联', 'detail': '', 'status': 'pending', 'created_at': at('09:00')})
    result = env.build()
    todo = next(t for t in result['todos'] if t['id'] == 'iso')
    assert todo['title'] == '一位候选人 的会话一天多都没能打开' and todo['detail'] == '请在 BOSS 上找到一位候选人的会话'
    assert next(t for t in result['todos'] if t['id'] == 'other')['title'] == NAME + '无关联'
    assert NAME in env.db.get('attention', 'iso')['title']
    result['todos'] = [t for t in result['todos'] if t['id'] != 'other']
    assert NAME not in repr(result)


def test_activity_follow_up_says_followed(env):
    # 11-25 终审：只有 decision.nudge 标记的才是追聊；线上 intent=follow_up 也用于普通回复，仍算“回复”
    case = env.case('交付顾问')
    env.person(case, 'p1', outbox=[{'id': 'u', 'intent': 'follow_up', 'status': 'sent', 'claimed_at': at('10:00'), 'sent_at': at('10:00'), 'decision': {'nudge': True}},
                                   {'id': 'f', 'intent': 'follow_up', 'status': 'sent', 'claimed_at': at('10:30'), 'sent_at': at('10:30')},
                                   {'id': 'r', 'intent': 'answer', 'status': 'sent', 'claimed_at': at('11:00'), 'sent_at': at('11:00'), 'decision': {}}])
    env.store.save(case);env.task([case['id']])
    assert [a['text'] for a in env.build()['activity']] == ['回复了一位候选人', '回复了一位候选人', '追聊了一位候选人']


def test_system_pause_reason_from_task_validation_todo(env):
    # 11-25 终审：任务验收失效的待办也说明系统暂停原因
    case = env.case('交付顾问')
    tid = env.task([case['id']], status='paused', history=[{'at': at('14:12'), 'status': 'paused', 'actor': 'system', 'reason': 'x'}])
    env.db.put('attention', 'v', {'id': 'v', 'kind': 'task_not_validated', 'title': '任务验收已全部失效', 'detail': '', 'status': 'pending', 'task_id': tid, 'created_at': at('14:12')})
    assert env.build()['tasks'][0]['detail'] == '任务验收已全部失效'


def test_system_pause_reason_from_login_todo_keyed_by_run(env):
    # 11-25 终审：登录待办按执行器 run_id 记录、没有 task_id；账号只有一个浏览器，照样作为暂停原因
    case = env.case('交付顾问')
    env.task([case['id']], status='paused', history=[{'at': at('14:12'), 'status': 'paused', 'actor': 'system', 'reason': 'x'}])
    env.db.put('attention', 'l', {'id': 'l', 'kind': 'login', 'title': '需要重新登录', 'detail': '', 'status': 'pending', 'run_id': 'r1', 'created_at': at('14:12')})
    assert env.build()['tasks'][0]['detail'] == '需要重新登录'
