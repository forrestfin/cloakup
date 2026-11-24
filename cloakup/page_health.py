"""执行浏览器页面健康检查与逐级自愈。

11-24 沟通页、推荐页多次卡在“加载中，请稍候”：网址正确但内容不出来，执行器每轮只报“未就绪”，
开聊入口找不到控件时还把整个任务停了。这里统一处理：

- 检查只读：页面是否卡在加载、关键内容是否出来、是否掉登录／出现平台验证、编辑框里有没有草稿。
- 自愈逐级：刷新当前页（最多 RELOAD_ATTEMPTS 次）→ 重置标签页（关闭多余空白页、新开一个干净标签）。
- 绝不在有草稿、掉登录、平台验证时刷新；全程不点击业务控件、不发送。
- 全部失败后进入冷却，期间不再反复刷新；结果写入 state 日志和 page_health 记录，供管理员待办使用。
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse

from .logbook import log
from .pacing import jitter

CHAT_URL = 'https://www.zhipin.com/web/chat/index'
RECOMMEND_URL = 'https://www.zhipin.com/web/chat/recommend'
PAGES = {'chat': (CHAT_URL, '/web/chat/index'), 'recommend': (RECOMMEND_URL, '/web/chat/recommend')}
RELOAD_ATTEMPTS = 2
SETTLE_READS = 8          # 刷新后最多读几次等页面画好（每次间隔约 1.5 秒）
COOLDOWN_SECONDS = 600    # 全部自愈失败后，这段时间内不再刷新重置

HEALTH_JS = r'''(()=>{
 const t=((document.body&&document.body.innerText)||'').trim();
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const editors=Array.from(document.querySelectorAll('[contenteditable="true"],textarea'));
 return {text_len:t.length,loading:t.length<200&&t.includes('加载中'),
  chat_rows:document.querySelectorAll('.geek-item[data-id]:not(.group-item)').length,
  editor_count:editors.length,
  has_draft:editors.some(e=>(e.value||e.innerText||'').trim().length>0),
  recommend_tab:Array.from(document.querySelectorAll('li')).some(e=>(e.innerText||'').trim()==='推荐'),
  login:!!Array.from(document.querySelectorAll('input[type="password"]')).find(visible)||
   !!Array.from(document.querySelectorAll('button')).find(e=>visible(e)&&/^(登录\/注册|扫码登录|密码登录|短信登录)$/.test((e.innerText||'').trim())),
  challenge:/环境存在异常|操作过于频繁|账号存在异常|请完成安全验证|请完成验证|拖动滑块/.test(t)||
   !!Array.from(document.querySelectorAll('iframe[src*="captcha"],iframe[src*="verify"],[class*="captcha"]')).find(visible)};
})()'''

ADMIN_REASONS = {'login_required', 'verification_required', 'draft_present'}


def verdict(kind, url, frames):
    """根据各 frame 的只读状态判断页面是否可用。返回 healthy、reason、needs_admin、has_draft。"""
    path = urlparse(url or '').path
    has_draft = any(f.get('has_draft') for f in frames)
    def result(reason):
        return {'healthy': reason is None, 'reason': reason, 'needs_admin': reason in ADMIN_REASONS, 'has_draft': has_draft}
    if any(p in path for p in ('/web/user', '/login', '/passport', '/signin')) or any(f.get('login') for f in frames):
        return result('login_required')
    if any(f.get('challenge') for f in frames):
        return result('verification_required')
    if path != PAGES[kind][1]:
        return result('wrong_page')
    if any(f.get('loading') for f in frames):
        return result('loading_stuck')
    if kind == 'chat' and not any(f.get('chat_rows') or f.get('editor_count') for f in frames) and max((f.get('text_len', 0) for f in frames), default=0) < 500:
        return result('content_missing')
    if kind == 'recommend' and not any(f.get('recommend_tab') for f in frames):
        return result('content_missing')
    return result(None)


async def read_frames(session, kind):
    """读取主页面和同源内嵌页面（推荐页的内容在 iframe 里）的只读状态。"""
    from .dom_reader import read_dom
    page = session.page
    states = [await read_dom(page, HEALTH_JS, timeout=6)]
    human = getattr(getattr(page, '_stealth_world', None), '_human', None)
    if human is not None:
        for frame in list(getattr(page, 'frames', []))[:9]:
            if frame == page.main_frame or re.search(r'captcha|challenges\.cloudflare|turnstile|hcaptcha', frame.url, re.I):
                continue
            try:
                states.append(await asyncio.wait_for(human.worlds.evaluate(frame._impl_obj, HEALTH_JS), timeout=6))
            except Exception:
                continue
    return page.url, [s for s in states if isinstance(s, dict)]


async def check(session, kind):
    try:
        url, frames = await read_frames(session, kind)
    except Exception as exc:
        return {'healthy': False, 'reason': 'unreadable', 'needs_admin': False, 'has_draft': False, 'error': type(exc).__name__}
    return verdict(kind, url, frames)


async def _settle(session, kind):
    state = await check(session, kind)
    for _ in range(SETTLE_READS - 1):
        if state['healthy'] or state['needs_admin']:
            break
        await asyncio.sleep(1.5)
        state = await check(session, kind)
    return state


async def _reset_tabs(session, url):
    """关掉多余的空白标签，新开一个干净标签打开目标页；不重启浏览器、不清登录。"""
    context = session.context
    old = session.page
    for page in list(getattr(context, 'pages', [])):
        if page is not old and getattr(page, 'url', '') in ('about:blank', ''):
            try:
                await page.close()
            except Exception:
                pass
    session.page = await context.new_page()
    for attr in ('_boss_chat_page', '_boss_selection_page', '_boss_recommend_list', '_boss_recommend_detail'):
        if hasattr(session, attr):
            setattr(session, attr, None)
    await session.navigate(url)
    try:
        await old.close()
    except Exception:
        pass


def _record(session, kind, state, step):
    """保存最近一次自愈结果，管理员待办据此生成或解除。"""
    if not state['healthy']:
        session._page_health_flagged = True
    manager = getattr(session, 'manager', None)
    db = getattr(manager, 'db', None)
    if db is None:
        return
    try:
        db.put('page_health', 'boss', {'id': 'boss', 'kind': kind, 'step': step, 'healthy': state['healthy'],
                                       'reason': state.get('reason'), 'needs_admin': state.get('needs_admin', False),
                                       'exhausted': state.get('exhausted', False), 'screenshot': state.get('screenshot'),
                                       'at': time.time()})
    except Exception:
        pass


async def _screenshot(session):
    manager = getattr(session, 'manager', None)
    root = getattr(manager, 'root', None)
    if root is None or not hasattr(session.page, 'screenshot'):
        return None
    try:
        folder = root / 'runs' / 'page_health'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / time.strftime('%Y%m%d-%H%M%S.jpg')
        await session.page.screenshot(path=str(path), type='jpeg', quality=55, caret='initial', animations='allow')
        return str(path)
    except Exception:
        return None


async def heal(session, kind):
    """确保 kind 对应的页面可用；必要时逐级自愈。返回检查结果（含 healed_by、exhausted）。"""
    url = PAGES[kind][0]
    # 刚打开的页面可能还在正常加载：先等它画好再判断，免得把“正在加载”当成“卡住”去刷新。
    state = await _settle(session, kind)
    if state['healthy']:
        if getattr(session, '_page_health_flagged', False):
            session._page_health_flagged = False
            session._page_health_exhausted_at = None
            _record(session, kind, state, 'healthy')
        return state
    if state['has_draft'] and not state['needs_admin']:
        state = {**state, 'reason': 'draft_present', 'needs_admin': True}
    if state['needs_admin']:
        log('state', type='page_health', kind=kind, step='needs_admin', reason=state['reason'])
        _record(session, kind, state, 'needs_admin')
        return state
    if time.time() - (getattr(session, '_page_health_exhausted_at', None) or 0) < COOLDOWN_SECONDS:
        return {**state, 'reason': 'cooling_down', 'cause': state['reason']}
    cause = state['reason']
    for attempt in range(1, RELOAD_ATTEMPTS + 1):
        log('state', type='page_health', kind=kind, step='reload', attempt=attempt, reason=cause)
        await asyncio.sleep(jitter(4))
        await session.navigate(url)
        state = await _settle(session, kind)
        if state['healthy']:
            log('state', type='page_health', kind=kind, step='healed', healed_by='reload', reason=cause)
            _record(session, kind, state, 'healed')
            return {**state, 'healed_by': 'reload'}
        if state['needs_admin']:
            _record(session, kind, state, 'needs_admin')
            return state
    log('state', type='page_health', kind=kind, step='tab_reset', reason=state['reason'])
    try:
        await _reset_tabs(session, url)
        state = await _settle(session, kind)
    except Exception as exc:
        state = {'healthy': False, 'reason': 'tab_reset_failed', 'needs_admin': False, 'has_draft': False, 'error': type(exc).__name__}
    if state['healthy']:
        log('state', type='page_health', kind=kind, step='healed', healed_by='tab_reset', reason=cause)
        _record(session, kind, state, 'healed')
        return {**state, 'healed_by': 'tab_reset'}
    session._page_health_exhausted_at = time.time()
    state = {**state, 'exhausted': True, 'cause': cause, 'screenshot': await _screenshot(session)}
    log('state', type='page_health', kind=kind, step='exhausted', reason=state['reason'], cause=cause)
    _record(session, kind, state, 'exhausted')
    return state


def kind_of(url):
    path = urlparse(url or '').path
    return next((kind for kind, (_, p) in PAGES.items() if path == p), None)
