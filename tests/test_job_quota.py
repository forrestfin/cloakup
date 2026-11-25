import time
from types import SimpleNamespace as NS

from cloakup import job_quota as jq
from cloakup.autonomy import day_key


def _store(cases):
    return NS(list=lambda: cases)


def _case(cid, claims=0, intent='first_contact', **extra):
    now = time.time()
    outbox = [{'intent': intent, 'status': 'sent', 'claimed_at': now} for _ in range(claims)]
    return {'id': cid, 'candidates': {'p': {'outbox': outbox}}, **extra}


def _task(members, limit=20, tid='t1'):
    return {'id': tid, 'config': {'members': members, 'case_ids': [m['case_id'] for m in members], 'execution': {'daily_new_limit': limit}}}


def test_task_target_uses_the_whole_limit_every_day():
    # 11-25 17:4x 管理员：每天要把首聊额度用完（原为上限的 75%～100%，当天上限 20 只开了 15）。
    assert all(jq.task_target('t%d' % i, 20, '2026-11-%02d' % d) == 20 for i in range(20) for d in range(1, 29))
    assert jq.task_target('t1', 0, '2026-11-24') == 0 and jq.task_target('t1', 1, '2026-11-24') == 1 and jq.task_target('t1', 7, '2026-11-24') == 7


def test_allocate_by_weight_largest_remainder():
    assert jq.allocate(10, {'a': 3, 'b': 1}) == {'a': 8, 'b': 2}
    first = jq.allocate(7, {'a': 1, 'b': 1, 'c': 1})
    assert sum(first.values()) == 7 and sorted(first.values()) == [2, 2, 3] and first == jq.allocate(7, {'c': 1, 'b': 1, 'a': 1})
    assert jq.allocate(0, {'a': 1}) == {'a': 0} and jq.allocate(5, {}) == {}


def _limit_for(target, day='2026-11-24', tid='t1'):
    # 找一个当天目标恰为 target 的上限，便于按份额断言
    return next(n for n in range(1, 21) if jq.task_target(tid, n, day) == target)


def test_pick_prefers_lowest_share_ratio():
    day = '2026-11-24';limit = _limit_for(10, day)
    assert jq.allocate(10, {'a': 3, 'b': 1}) == {'a': 8, 'b': 2}
    # 比值 a=4/8, b=1/2 相等时取权重大的 a；b 比值更低时选 b
    assert jq.pick_case('t1', limit, {'a': 3, 'b': 1}, {'a': 4, 'b': 1}, set(), day) == 'a'
    assert jq.pick_case('t1', limit, {'a': 3, 'b': 1}, {'a': 5, 'b': 1}, set(), day) == 'b'
    assert jq.pick_case('t1', limit, {'a': 3, 'b': 1}, {'a': 1, 'b': 1}, set(), day) == 'a'


def test_exhausted_job_yields_share():
    day = '2026-11-24';limit = _limit_for(10, day)
    # b 当天无人可联系，a 份额已满但任务未达目标：b 让出的额度给 a
    assert jq.pick_case('t1', limit, {'a': 3, 'b': 1}, {'a': 8, 'b': 0}, {'b'}, day) == 'a'
    assert jq.pick_case('t1', limit, {'a': 3, 'b': 1}, {'a': 8, 'b': 0}, {'a', 'b'}, day) is None


def test_task_target_reached_returns_none():
    day = '2026-11-24';limit = _limit_for(10, day)
    assert jq.pick_case('t1', limit, {'a': 3, 'b': 1}, {'a': 8, 'b': 2}, set(), day) is None
    assert jq.pick_case('t1', 0, {'a': 1}, {}, set(), day) is None


def test_withdraw_and_readd_same_day_keeps_usage():
    # 当天 b 已占位 2 次后撤下再加回：任务合计仍算上 b 的占位，不会因撤下而重新发满额度
    day = day_key();cases = [_case('a', 3), _case('b', 2)];store = _store(cases)
    limit = 20;target = jq.task_target('t1', limit, day)
    cases[0]['candidates']['p']['outbox'] += [{'intent': 'first_contact', 'status': 'sent', 'claimed_at': time.time()} for _ in range(target - 5)]
    usage = jq.usage_by_case(store, ['a', 'b'], day)
    assert usage == {'a': target - 2, 'b': 2} and sum(usage.values()) == target
    withdrawn = _task([{'case_id': 'a', 'weight': 1, 'outreach': 'active'}, {'case_id': 'b', 'weight': 1, 'outreach': 'withdrawn'}])
    readded = _task([{'case_id': 'a', 'weight': 1, 'outreach': 'active'}, {'case_id': 'b', 'weight': 1, 'outreach': 'active'}])
    for task in (withdrawn, readded):
        assert jq.usage_by_case(store, task['config']['case_ids'], day) == usage
        assert jq.pick_case('t1', limit, {'a': 1}, usage, set(), day) is None
        assert not jq.allowed(store, task, 'a', day) and not jq.allowed(store, task, 'b', day)
    # 合计未满时，撤下的岗位不被选中，加回后才参与
    cases[0]['candidates']['p']['outbox'].pop()
    assert jq.allowed(store, withdrawn, 'a', day) and not jq.allowed(store, withdrawn, 'b', day)
    assert jq.allowed(store, readded, 'b', day)


def test_account_cap_blocks_every_task():
    day = day_key();store = _store([_case('other', 20), _case('a', 0)])
    task = _task([{'case_id': 'a', 'weight': 1, 'outreach': 'active'}])
    assert jq.account_exhausted(store, day) and not jq.allowed(store, task, 'a', day)
    assert not jq.account_exhausted(_store([_case('other', 19)]), day)
    assert jq.allowed(_store([_case('other', 19), _case('a', 0)]), task, 'a', day)


def test_usage_counts_delivery_unknown_and_external_first_contacts():
    day = day_key();now = time.time()
    outbox = [{'intent': 'first_contact', 'status': 'delivery_unknown', 'claimed_at': now},
              {'intent': 'first_contact', 'status': 'sent', 'origin': 'external_observed', 'receipt': {'platform_time': day + ' 10:00'}},
              {'intent': 'first_contact', 'status': 'sent', 'claimed_at': now - 3 * 86400},
              {'intent': 'reply', 'status': 'sent', 'claimed_at': now}]
    store = _store([{'id': 'a', 'candidates': {'p': {'outbox': outbox}}}, _case('x', 4)])
    assert jq.usage_by_case(store, ['a', 'missing'], day) == {'a': 2, 'missing': 0}
    from cloakup import autopilot as ap
    assert ap.daily_attempts(store, 'first_contact', day) == 6


async def test_first_contact_entry_uses_task_quota(monkeypatch):
    # 属于寻访任务的岗位：入口按任务额度复核，未选中时不打开推荐页；无任务时仍按岗位自己的上限
    from cloakup import autopilot as ap
    case = _case('a', 0, autonomy={'enabled': True, 'revision': 1, 'daily_new_limit': 20})
    task = _task([{'case_id': 'a', 'weight': 1, 'outreach': 'active'}]);task['config']['engine'] = 'boss_sourcing'
    seen = []
    monkeypatch.setattr(ap.job_quota, 'allowed', lambda store, t, cid, day: seen.append((t['id'], cid)) or False)
    db = NS(list=lambda kind: [task] if kind == 'task' else [])
    manager = NS(cases=_store([case]), db=db)
    assert await ap.first_contact(manager, {}, NS(), case) == 'blocked' and seen == [('t1', 'a')]
    case['autonomy']['daily_new_limit'] = 0
    assert await ap.first_contact(NS(cases=_store([case]), db=NS(list=lambda kind: [])), {}, NS(), case) == 'blocked' and len(seen) == 1


def test_job_removed_today_still_counts_toward_task_total():
    # 当天 b 已占位 2 次后被彻底移除：任务合计仍算上 b 的占位，不会因移除而多发
    day = day_key();cases = [_case('a', 0), _case('b', 2)];store = _store(cases)
    # 目标低于账号每天 20 的硬上限，才能单独看任务合计（目标等于上限后 20 会同时撞上账号上限）
    limit = 12;target = jq.task_target('t1', limit, day)
    cases[0]['candidates']['p']['outbox'] += [{'intent': 'first_contact', 'status': 'sent', 'claimed_at': time.time()} for _ in range(target - 2)]
    task = _task([{'case_id': 'a', 'weight': 1, 'outreach': 'active'}]);task['config']['execution']['daily_new_limit'] = limit
    assert jq.allowed(store, task, 'a', day)
    task['config']['removed_today'] = [{'case_id': 'b', 'day': day}]
    assert jq.counted_case_ids(task['config'], day) == ['a', 'b']
    assert not jq.allowed(store, task, 'a', day)
    # 前一天移除的岗位不再计入
    task['config']['removed_today'] = [{'case_id': 'b', 'day': '2000-01-01'}]
    assert jq.allowed(store, task, 'a', day)


def test_task_quota_view_reports_target_shares_and_usage():
    # 界面只读视图：今日目标与算法同源，份额只分给参与首聊的岗位，已撤下岗位的占位仍计入任务已用，账号已用含其他任务
    day = day_key();store = _store([_case('a', 2), _case('b', 1), _case('c', 3), _case('other', 4)])
    task = _task([{'case_id': 'a', 'weight': 3, 'outreach': 'active'}, {'case_id': 'b', 'weight': 1, 'outreach': 'active'},
                  {'case_id': 'c', 'weight': 2, 'outreach': 'withdrawn'}], limit=12)
    view = jq.task_quota(store, task, day)
    target = jq.task_target('t1', 12, day)
    assert view == {'limit': 12, 'target': target, 'used': 6, 'account_used': 10, 'account_cap': 20,
                    'shares': jq.allocate(target, {'a': 3, 'b': 1}), 'usage': {'a': 2, 'b': 1, 'c': 3}}
    assert task['config']['members'][2]['outreach'] == 'withdrawn'  # 不改动传入的任务
    # 当天移除的岗位仍计入任务已用；没有配置总额时按 0（与入口复核一致）
    task['config']['removed_today'] = [{'case_id': 'other', 'day': day}];del task['config']['execution']['daily_new_limit']
    view = jq.task_quota(store, task, day)
    assert view['used'] == 10 and view['usage']['other'] == 4 and view['limit'] == 0 and view['target'] == 0 and view['shares'] == {'a': 0, 'b': 0}
