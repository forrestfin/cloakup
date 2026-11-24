"""执行浏览器页面健康检查与逐级自愈（11-24：沟通页、推荐页多次卡在“加载中”，任务被停）。"""
from types import SimpleNamespace as NS

import pytest

from cloakup import page_health as ph

CHAT = 'https://www.zhipin.com/web/chat/index'
RECOMMEND = 'https://www.zhipin.com/web/chat/recommend'


def frame(**kw):
    base = {'text_len': 3000, 'loading': False, 'chat_rows': 0, 'editor_count': 0, 'has_draft': False,
            'login': False, 'challenge': False, 'recommend_tab': False}
    return {**base, **kw}


@pytest.mark.parametrize('kind,url,frames,reason,admin', [
    ('chat', CHAT, [frame(chat_rows=20)], None, False),
    ('chat', CHAT, [frame(text_len=20, loading=True)], 'loading_stuck', False),
    ('chat', CHAT, [frame(text_len=40)], 'content_missing', False),
    ('chat', CHAT, [frame(chat_rows=20, login=True)], 'login_required', True),
    ('chat', CHAT, [frame(chat_rows=20, challenge=True)], 'verification_required', True),
    ('chat', 'https://www.zhipin.com/web/user/', [frame()], 'login_required', True),
    ('recommend', RECOMMEND, [frame(), frame(recommend_tab=True)], None, False),
    ('recommend', RECOMMEND, [frame(), frame(text_len=10, loading=True)], 'loading_stuck', False),
    ('recommend', RECOMMEND, [frame()], 'content_missing', False),
    ('chat', RECOMMEND, [frame(recommend_tab=True)], 'wrong_page', False),
])
def test_verdict(kind, url, frames, reason, admin):
    v = ph.verdict(kind, url, frames)
    assert v['reason'] == reason and v['healthy'] == (reason is None) and v['needs_admin'] == admin


def test_draft_is_reported_but_does_not_make_a_loaded_page_unhealthy():
    v = ph.verdict('chat', CHAT, [frame(chat_rows=5, editor_count=1, has_draft=True)])
    assert v['healthy'] and v['has_draft']


class Browser:
    """伪造会话：按导航次数给出每次检查看到的页面状态。"""
    def __init__(self, states_after_nav, *, reset_states=None, url=CHAT):
        self.navs = 0
        self.states = states_after_nav
        self.reset_states = reset_states
        self.page = NS(url=url, is_closed=lambda: False)
        self.reset = 0
        self.context = NS(pages=[self.page, NS(url='about:blank', is_closed=lambda: False, close=self._closed)], new_page=self._new_page)
        self.closed = []

    async def _closed(self):
        self.closed.append('blank')

    async def _new_page(self):
        self.reset += 1
        page = NS(url='about:blank', is_closed=lambda: False)
        return page

    async def navigate(self, url):
        self.navs += 1
        self.page.url = url


@pytest.fixture
def world(monkeypatch):
    logged = []
    async def no_sleep(*a): pass
    monkeypatch.setattr(ph.asyncio, 'sleep', no_sleep)
    monkeypatch.setattr(ph, 'log', lambda category, **f: logged.append(f))
    monkeypatch.setattr(ph, 'SETTLE_READS', 1)
    def install(browser):
        async def read(session, kind):
            if browser.reset and browser.reset_states is not None:
                return browser.page.url, browser.reset_states
            return browser.page.url, browser.states[min(browser.navs, len(browser.states) - 1)]
        monkeypatch.setattr(ph, 'read_frames', read)
    return NS(logged=logged, install=install)


async def test_healthy_page_is_left_alone(world):
    b = Browser([[frame(chat_rows=9)]]);world.install(b)
    result = await ph.heal(b, 'chat')
    assert result['healthy'] and b.navs == 0


async def test_stuck_page_is_reloaded_until_healthy(world):
    b = Browser([[frame(text_len=10, loading=True)], [frame(text_len=10, loading=True)], [frame(chat_rows=9)]]);world.install(b)
    result = await ph.heal(b, 'chat')
    assert result['healthy'] and result['healed_by'] == 'reload' and b.navs == 2
    assert [f['step'] for f in world.logged if f.get('type') == 'page_health'][-1] == 'healed'


async def test_reloads_failing_escalate_to_a_tab_reset(world):
    b = Browser([[frame(text_len=10, loading=True)]], reset_states=[frame(chat_rows=9)]);world.install(b)
    result = await ph.heal(b, 'chat')
    assert result['healthy'] and result['healed_by'] == 'tab_reset' and b.reset == 1 and b.closed == ['blank']


async def test_everything_failing_is_reported_as_exhausted(world):
    b = Browser([[frame(text_len=10, loading=True)]], reset_states=[frame(text_len=10, loading=True)]);world.install(b)
    result = await ph.heal(b, 'chat')
    assert not result['healthy'] and result['exhausted'] and not result['needs_admin']


@pytest.mark.parametrize('state,reason', [(frame(text_len=10, loading=True, has_draft=True), 'draft_present'),
                                          (frame(login=True), 'login_required'),
                                          (frame(challenge=True), 'verification_required')])
async def test_never_refresh_over_a_draft_login_or_verification(world, state, reason):
    b = Browser([[state]]);world.install(b)
    result = await ph.heal(b, 'chat')
    assert not result['healthy'] and result['reason'] == reason and result['needs_admin'] and b.navs == 0 and b.reset == 0


async def test_heavy_healing_has_a_cooldown(world):
    b = Browser([[frame(text_len=10, loading=True)]], reset_states=[frame(text_len=10, loading=True)]);world.install(b)
    await ph.heal(b, 'chat')
    navs = b.navs
    result = await ph.heal(b, 'chat')
    assert not result['healthy'] and result['reason'] == 'cooling_down' and b.navs == navs


async def test_a_page_still_loading_normally_is_not_refreshed(world, monkeypatch):
    monkeypatch.setattr(ph, 'SETTLE_READS', 3)
    reads = iter([[frame(text_len=10, loading=True)], [frame(text_len=10, loading=True)], [frame(chat_rows=9)]])
    b = Browser([[frame()]])
    async def read(session, kind):return b.page.url, next(reads)
    monkeypatch.setattr(ph, 'read_frames', read)
    assert (await ph.heal(b, 'chat'))['healthy'] and b.navs == 0


async def test_recovery_after_an_admin_problem_is_recorded(world, monkeypatch):
    saved = {}
    b = Browser([[frame(chat_rows=5, editor_count=1, has_draft=True, text_len=10, loading=True)]])
    b.manager = NS(db=NS(put=lambda kind, ident, doc: saved.update({ident: doc})))
    world.install(b)
    assert (await ph.heal(b, 'chat'))['reason'] == 'draft_present' and saved['boss']['needs_admin']
    b.states = [[frame(chat_rows=5)]]
    assert (await ph.heal(b, 'chat'))['healthy'] and saved['boss']['healthy']


async def test_unloadable_recommend_page_defers_outreach_not_the_task(monkeypatch):
    from cloakup import boss_recommend as br
    from cloakup.sourcing import SourcingError
    async def heal(session, kind):return {'healthy': False, 'reason': 'loading_stuck', 'exhausted': True}
    async def navigate(url):pass
    monkeypatch.setattr(ph, 'heal', heal)
    session = NS(max_elements=0, navigate=navigate)
    with pytest.raises(SourcingError, match='推荐牛人页多次刷新和重置后仍未正常加载') as caught:
        await br.open_list(session, {'job': {'label': '岗位'}, 'recommend_filters': []})
    assert not any(t in str(caught.value) for t in ('登录', '校验', '验证', '过于频繁', '账号存在异常'))
