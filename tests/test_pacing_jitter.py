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


def test_daily_new_target_equals_admin_limit(monkeypatch):
    # 11-25 管理员：每天要把首聊额度用完；当天目标等于配置上限，不再按 75%～100% 打折。
    for day in range(30):
        monkeypatch.setattr(ap, 'day_key', lambda now=None, d=day: '2026-11-%02d' % (d + 1))
        for job in ('a', 'b'):
            assert ap.daily_new_target({'id': job, 'autonomy': {'daily_new_limit': 20}}) == 20
    assert ap.daily_new_target({'id': 'x', 'autonomy': {'daily_new_limit': 0}}) == 0
