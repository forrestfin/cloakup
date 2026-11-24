from copy import deepcopy
from types import SimpleNamespace as NS

from cloakup.browser import BrowserSession
from cloakup.config import BrowserConfig, PacingConfig
from cloakup.locate import describe, locate
from tests.test_agent import RAW


async def test_iframe_observation_routes_actions_to_correct_frame():
    main = object()
    child_impl = object()
    calls = []
    root_raw = deepcopy(RAW)
    root_raw['elements'] = [deepcopy(RAW['elements'][2])]
    root_raw['total'] = 1
    child_raw = deepcopy(root_raw)
    child_raw['elements'][0]['label'] = '岗位详情'
    child_raw['text'] = '真实岗位要求'
    child = NS(url='https://app.test/frame/jobs?id=variable', _impl_obj=child_impl,
               locator=lambda css: NS(first=('iframe', css)))
    async def evaluate(frame, expression):
        assert frame is child_impl
        calls.append(frame)
        if 'includes(' in expression:
            return True
        return deepcopy(child_raw)
    async def main_evaluate(expression):
        if 'includes(' in expression:
            return False
        return deepcopy(root_raw)
    async def wait(*a, **kw): pass
    world = NS(evaluate=main_evaluate, _human=NS(worlds=NS(evaluate=evaluate)))
    page = NS(url="https://app.test/",_stealth_world=world, main_frame=main, frames=[main, child],
              wait_for_load_state=wait, locator=lambda css: NS(first=('main', css)))
    session = BrowserSession(BrowserConfig(), PacingConfig())
    session.page = page
    obs = await session.observe(False)
    assert len(obs.elements) == 2
    assert '真实岗位要求' in obs.render()
    assert session.element(0)[0][0] == 'main'
    assert session.element(1)[0][0] == 'iframe'
    target = describe(obs.elements[1])
    assert target['frame_url_contains'] == '/frame/jobs'
    assert 'variable' not in target['frame_url_contains']
    assert locate(target, obs)[0].element.index == 1
    assert locate({**target, 'frame_url_contains': '/wrong-frame'}, obs)[0] is None
    assert await session.text_contains('真实岗位要求')
