from cloakup import autopilot as ap
from cloakup.pacing import jitter, stable_fraction


def test_jitter_is_bounded_and_not_periodic():
    # 调度间隔围绕配置值随机分布，不再整齐落在同一时长。
    values = [jitter(120) for _ in range(300)]
    assert all(72 <= v <= 216 for v in values) and len({round(v) for v in values}) > 30


def test_stable_fraction_is_repeatable_per_key_and_differs_across_keys():
    assert stable_fraction('a', 1) == stable_fraction('a', 1)
    assert len({stable_fraction('a', i) for i in range(50)}) == 50
    assert all(0 <= stable_fraction('k', i) < 1 for i in range(50))


def test_daily_new_target_stays_under_admin_cap_and_varies_by_job_and_day(monkeypatch):
    # 管理员配置值是封顶；不同日期、不同岗位的实际上限在 75%～100% 间变化，同一天内保持不变。
    targets = set()
    for day in range(30):
        monkeypatch.setattr(ap, 'day_key', lambda now=None, d=day: '2026-11-%02d' % (d + 1))
        for job in ('a', 'b'):
            case = {'id': job, 'autonomy': {'daily_new_limit': 20}}
            value = ap.daily_new_target(case)
            assert 15 <= value <= 20 and value == ap.daily_new_target(case)
            targets.add(value)
    assert len(targets) > 2
    assert ap.daily_new_target({'id': 'x', 'autonomy': {'daily_new_limit': 0}}) == 0
