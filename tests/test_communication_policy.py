from cloakup.communication_policy import COMMUNICATION_POLICY
from cloakup.agent import WebAgent
import inspect
from cloakup import autonomy,boss,recruiting_ai


def test_global_policy_covers_all_generation_entrypoints():
    assert '全部网站、场景和岗位' in COMMUNICATION_POLICY
    assert '如实说明' in COMMUNICATION_POLICY
    for fn in [WebAgent.__init__,autonomy.compose_reply,boss.prepare_plan,recruiting_ai.analyze_anonymous_brief]:
        assert 'COMMUNICATION_POLICY' in inspect.getsource(fn)
