import pytest


@pytest.fixture
def no_jitter(monkeypatch):
    """把调度抖动替换为原值，供校验退避/重试逻辑的用例使用；随机性本身另有用例覆盖。"""
    from cloakup import autopilot, decision_runtime, greeting_policy, pacing, platform, selection_recovery
    identity = lambda seconds, sigma=0.3: seconds
    for module in (pacing, autopilot, decision_runtime, greeting_policy, platform, selection_recovery):
        monkeypatch.setattr(module, 'jitter', identity)
