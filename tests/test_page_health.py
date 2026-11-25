"""执行浏览器页面健康检查与逐级自愈（11-24：沟通页、推荐页多次卡在“加载中”，任务被停）。"""
from types import SimpleNamespace as NS

import asyncio

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


# 11-24 20:40 沟通页卡在启动页：刷新 ×2、重开标签页都无效；管理员从推荐牛人页点左侧菜单“消息”进入后恢复。
class MenuBrowser(Browser):
    """沟通页直接打开一直卡住，只有从另一页点菜单进入后才正常。"""
    def __init__(self, *, items, other=None, heal_by_menu=True):
        super().__init__([[frame(text_len=10, loading=True)]])
        self.items = items;self.other = other or [frame(recommend_tab=True)];self.heal_by_menu = heal_by_menu
        self.clicks = [];self.acts = [];self.visited = []
        self.page.mouse = NS(click=self._click)

    async def _new_page(self):
        self.reset += 1
        return NS(url='about:blank', is_closed=lambda: False, mouse=NS(click=self._click))

    async def navigate(self, url):
        self.visited.append(url);self.menu_entered = False
        await super().navigate(url)

    async def _click(self, x, y):
        self.clicks.append((x, y));self.page.url = CHAT;self.menu_entered = True

    async def _act(self, kind, fn):
        self.acts.append(kind);await fn()


@pytest.fixture
def menu_world(world, monkeypatch):
    def install(browser):
        async def read(session, kind):
            if browser.page.url == RECOMMEND:return browser.page.url, browser.other
            if getattr(browser, 'menu_entered', False) and browser.heal_by_menu:return browser.page.url, [frame(chat_rows=9, editor_count=1)]
            return browser.page.url, [frame(text_len=10, loading=True)]
        async def targets(session, kind):return browser.items
        monkeypatch.setattr(ph, 'read_frames', read);monkeypatch.setattr(ph, '_menu_targets', targets)
    return NS(logged=world.logged, install=install)


async def test_menu_entry_heals_a_chat_page_stuck_on_the_splash_screen(menu_world):
    # 11-24 20:40 沟通页卡在启动页
    b = MenuBrowser(items=[{'label': '消息', 'x': 40, 'y': 300, 'hit': True}]);menu_world.install(b)
    result = await ph.heal(b, 'chat')
    assert result['healthy'] and result['healed_by'] == 'menu_entry'
    assert b.reset == 1 and RECOMMEND in b.visited and b.clicks == [(40, 300)] and b.acts == ['click']
    steps = [f['step'] for f in menu_world.logged if f.get('type') == 'page_health']
    assert steps[-2:] == ['menu_entry', 'healed'] and menu_world.logged[-1]['healed_by'] == 'menu_entry'


@pytest.mark.parametrize('items,reason', [([], 'menu_missing'),
                                          ([{'label': '消息', 'x': 40, 'y': 300, 'hit': True}, {'label': '消息', 'x': 40, 'y': 600, 'hit': True}], 'menu_not_unique'),
                                          ([{'label': '消息', 'x': 40, 'y': 300, 'hit': False}], 'menu_obscured')])
async def test_menu_entry_never_clicks_an_ambiguous_or_covered_menu(menu_world, items, reason):
    # 11-24 20:40 沟通页卡在启动页：菜单不唯一或被遮挡时不点，判用尽
    b = MenuBrowser(items=items);menu_world.install(b)
    result = await ph.heal(b, 'chat')
    assert not result['healthy'] and result['exhausted'] and result['reason'] == reason and b.clicks == []


@pytest.mark.parametrize('other,reason', [([frame(login=True)], 'login_required'), ([frame(challenge=True)], 'verification_required')])
async def test_menu_entry_stops_when_the_other_page_needs_the_admin(menu_world, other, reason):
    # 11-24 20:40 沟通页卡在启动页：经过的另一页掉登录或出验证，交给管理员，不点菜单
    b = MenuBrowser(items=[{'label': '消息', 'x': 40, 'y': 300, 'hit': True}], other=other);menu_world.install(b)
    result = await ph.heal(b, 'chat')
    assert not result['healthy'] and result['needs_admin'] and result['reason'] == reason and b.clicks == []


async def test_menu_entry_failing_still_ends_exhausted(menu_world):
    b = MenuBrowser(items=[{'label': '消息', 'x': 40, 'y': 300, 'hit': True}], heal_by_menu=False);menu_world.install(b)
    result = await ph.heal(b, 'chat')
    assert not result['healthy'] and result['exhausted'] and b.clicks == [(40, 300)]


async def test_forced_heal_skips_the_cooldown(world, monkeypatch):
    # 暂停期间的定时自愈自己控制间隔，不受执行中冷却限制
    b = Browser([[frame(text_len=10, loading=True)]], reset_states=[frame(text_len=10, loading=True)]);world.install(b)
    async def none(session, kind):return []
    monkeypatch.setattr(ph, '_menu_targets', none)
    await ph.heal(b, 'chat')
    navs = b.navs
    assert (await ph.heal(b, 'chat'))['reason'] == 'cooling_down' and b.navs == navs
    result = await ph.heal(b, 'chat', force=True)
    assert result['exhausted'] and b.navs > navs


async def test_environment_page_check_visits_both_pages_and_returns_to_chat(monkeypatch):
    # 11-24 20:40 沟通页卡在启动页：环境检查实际打开沟通页、推荐牛人页各一次（打开由 heal 自己完成），最后回到沟通页
    seen = []
    async def heal(session, kind, *, force=False):
        seen.append((kind, session.page.url, force));session.page.url = ph.PAGES[kind][0]
        return {'healthy': True, 'reason': None, 'needs_admin': False, 'has_draft': False, **({'healed_by': 'menu_entry'} if kind == 'recommend' else {})}
    navs = []
    async def navigate(url):navs.append(url);session.page.url = url
    session = NS(page=NS(url=CHAT), navigate=navigate)
    monkeypatch.setattr(ph, 'heal', heal)
    results = await ph.check_pages(session)
    assert seen == [('chat', CHAT, True), ('recommend', CHAT, True)] and navs == [CHAT]
    text = ph.describe(results)
    assert '沟通页正常' in text and '推荐牛人页正常' in text and '菜单进入' in text


async def test_environment_page_check_never_leaves_a_draft_or_login(monkeypatch):
    async def heal(session, kind, *, force=False):
        return {'healthy': True, 'reason': None, 'needs_admin': False, 'has_draft': True}
    navs = []
    async def navigate(url):navs.append(url)
    monkeypatch.setattr(ph, 'heal', heal)
    results = await ph.check_pages(NS(page=NS(url=CHAT), navigate=navigate))
    assert navs == [] and len(results) == 1 and '没发出去的文字' in ph.describe(results)


async def test_environment_page_check_from_another_page_with_a_draft_does_not_navigate(world):
    # 复审：环境检查开始时停在推荐页且有草稿，不能先跳去沟通页把草稿丢掉
    b = Browser([[frame(recommend_tab=True, editor_count=1, has_draft=True)]], url=RECOMMEND);world.install(b)
    results = await ph.check_pages(b)
    assert b.navs == 0 and b.reset == 0 and results[-1]['reason'] == 'draft_present' and results[-1]['needs_admin']


async def test_environment_page_check_has_a_time_budget_per_page(monkeypatch):
    # 复审：自愈最坏耗时可能超过环境检查 300 秒上限，每页有时间预算，超时干净地报告并停止
    async def slow(session, kind, *, force=False):await asyncio.Event().wait()
    monkeypatch.setattr(ph, 'heal', slow);monkeypatch.setattr(ph, 'PAGE_CHECK_SECONDS', 0.01)
    navs = []
    async def navigate(url):navs.append(url)
    results = await ph.check_pages(NS(page=NS(url=CHAT), navigate=navigate))
    assert [r['reason'] for r in results] == ['timed_out'] and navs == [] and '超时' in ph.describe(results)


def test_environment_page_check_describes_where_it_got_stuck_and_what_was_done():
    text = ph.describe([{'kind': 'chat', 'healthy': False, 'reason': 'menu_missing', 'cause': 'content_missing', 'exhausted': True, 'needs_admin': False, 'has_draft': False,
                         'steps': ['reload', 'reload', 'tab_reset', 'menu_entry']}])
    assert '沟通页不正常' in text and '刷新 2 次' in text and '重新打开标签页 1 次' in text and '菜单' in text
    # 只写实际做过的步骤
    text = ph.describe([{'kind': 'chat', 'healthy': False, 'reason': 'tab_reset_failed', 'cause': 'loading_stuck', 'exhausted': True, 'steps': ['reload', 'reload', 'tab_reset']}])
    assert '刷新 2 次' in text and '菜单' not in text


async def test_exhausted_record_lists_the_steps_actually_taken(world):
    saved = {}
    b = Browser([[frame(text_len=10, loading=True)]], reset_states=[frame(text_len=10, loading=True)])
    b.manager = NS(db=NS(put=lambda kind, ident, doc: saved.update({ident: doc})));world.install(b)
    result = await ph.heal(b, 'chat')
    assert result['steps'] == ['reload', 'reload', 'tab_reset', 'menu_entry'] and saved['boss']['steps'] == result['steps']
