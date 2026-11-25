"""执行浏览器页面健康检查与逐级自愈。

11-24 沟通页、推荐页多次卡在“加载中，请稍候”：网址正确但内容不出来，执行器每轮只报“未就绪”，
开聊入口找不到控件时还把整个任务停了。这里统一处理：

- 检查只读：页面是否卡在加载、关键内容是否出来、是否掉登录／出现平台验证、编辑框里有没有草稿。
- 自愈逐级：刷新当前页（最多 RELOAD_ATTEMPTS 次）→ 重置标签页（关闭多余空白页、新开一个干净标签）
  → 从菜单进入（11-24 20:40 沟通页卡在启动页，刷新、重开都无效，从推荐牛人页点左侧菜单“消息”才恢复）：
  先打开另一个 BOSS 页面，再原生点击左侧菜单里可见、唯一、未被遮挡的那一项。
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

# 从另一页进入时点的左侧菜单文字（按顺序取第一个出现的）；“沟通”是旧版菜单名。
MENU_LABELS = {'chat': ('消息', '沟通'), 'recommend': ('推荐牛人',)}
# 只认左侧导航：元素右边缘在视口左侧 LEFT_NAV 范围内；弹窗、对话框里的同名文字一律不算。
MENU_DIALOGS = '[role="dialog"],[aria-modal="true"],[class*="dialog"],[class*="popup"],[class*="modal"],[class*="popover"],[class*="tooltip"]'
MENU_SKIP = '.geek-item,.chat-conversation,.message-item,.chat-message-list,.candidate-card-wrap,[data-geekid],button,input,textarea,[contenteditable="true"]'
# 主页面左侧导航里文字恰为菜单名的可见元素（不在候选人区域、按钮、输入框或弹窗里）：给出中心点和命中测试结果，用于原生点击。
MENU_JS = r'''((labels,skip,dialogs)=>{const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const left=e=>e.getBoundingClientRect().right<=Math.min(360,window.innerWidth*0.35);
 for(const label of labels){
  const nodes=Array.from(document.querySelectorAll('a,li,div,span')).filter(e=>vis(e)&&left(e)&&(e.innerText||'').trim()===label&&!e.closest(skip)&&!e.closest(dialogs));
  const leaves=nodes.filter(e=>!nodes.some(n=>n!==e&&e.contains(n)));
  if(!leaves.length)continue;
  return leaves.map(e=>{const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);
   return {label,x,y,hit:!!h&&(h===e||e.contains(h))};});}
 return [];})'''


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


async def _menu_targets(session, kind):
    import json
    from .dom_reader import read_dom
    expression = '(' + MENU_JS + ')(' + json.dumps(MENU_LABELS[kind], ensure_ascii=False) + ',' + json.dumps(MENU_SKIP) + ',' + json.dumps(MENU_DIALOGS) + ')'
    items = await read_dom(session.page, expression, timeout=6)
    return items if isinstance(items, list) else []


def _stopped(reason):
    return {'healthy': False, 'reason': reason, 'needs_admin': reason in ADMIN_REASONS, 'has_draft': reason == 'draft_present'}


async def _menu_entry(session, kind):
    """先打开另一个 BOSS 页面，再原生点击左侧菜单进入目标页。菜单不唯一、被遮挡或位置变了就不点。"""
    other = 'recommend' if kind == 'chat' else 'chat'
    await asyncio.sleep(jitter(4))
    await session.navigate(PAGES[other][0])
    side = await _settle(session, other)
    if side['needs_admin']:
        return side
    if side['has_draft']:
        return _stopped('draft_present')
    items = await _menu_targets(session, kind)
    if not items:
        return _stopped('menu_missing')
    if len(items) != 1:
        return _stopped('menu_not_unique')
    target = items[0]
    if not target.get('hit'):
        return _stopped('menu_obscured')
    await asyncio.sleep(jitter(1.5, .2))
    async def click():
        # 点击前再核一次：仍是同一个唯一、未遮挡的菜单项，位置没有移动。
        again = await _menu_targets(session, kind)
        if len(again) != 1 or not again[0].get('hit') or abs(again[0]['x'] - target['x']) > 3 or abs(again[0]['y'] - target['y']) > 3:
            raise ValueError('菜单位置已变化，未点击')
        await session.page.mouse.click(target['x'], target['y'])
    await session._act('click', click)
    return await _settle(session, kind)


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
                                       'cause': state.get('cause'), 'steps': state.get('steps', []),
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


async def heal(session, kind, *, force=False):
    """确保 kind 对应的页面可用；必要时逐级自愈。返回检查结果（含 healed_by、exhausted）。

    force：暂停期间的定时自愈与环境检查自己控制间隔，不受执行中的冷却限制。"""
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
    if not force and time.time() - (getattr(session, '_page_health_exhausted_at', None) or 0) < COOLDOWN_SECONDS:
        return {**state, 'reason': 'cooling_down', 'cause': state['reason']}
    cause = state['reason']
    steps = []    # 实际做过的自愈步骤，写进结果和记录，管理员待办与环境检查据此说明“已做”
    for attempt in range(1, RELOAD_ATTEMPTS + 1):
        log('state', type='page_health', kind=kind, step='reload', attempt=attempt, reason=cause)
        steps.append('reload')
        await asyncio.sleep(jitter(4))
        await session.navigate(url)
        state = await _settle(session, kind)
        if state['healthy']:
            log('state', type='page_health', kind=kind, step='healed', healed_by='reload', reason=cause)
            _record(session, kind, state, 'healed')
            return {**state, 'healed_by': 'reload', 'steps': steps}
        if state['needs_admin']:
            state = {**state, 'steps': steps}
            _record(session, kind, state, 'needs_admin')
            return state
    log('state', type='page_health', kind=kind, step='tab_reset', reason=state['reason'])
    steps.append('tab_reset')
    try:
        await _reset_tabs(session, url)
        state = await _settle(session, kind)
    except Exception as exc:
        state = {'healthy': False, 'reason': 'tab_reset_failed', 'needs_admin': False, 'has_draft': False, 'error': type(exc).__name__}
    if state['healthy']:
        log('state', type='page_health', kind=kind, step='healed', healed_by='tab_reset', reason=cause)
        _record(session, kind, state, 'healed')
        return {**state, 'healed_by': 'tab_reset', 'steps': steps}
    if state['has_draft'] and not state['needs_admin']:
        state = {**state, 'reason': 'draft_present', 'needs_admin': True}
    if state['needs_admin']:
        state = {**state, 'steps': steps}
        _record(session, kind, state, 'needs_admin')
        return state
    log('state', type='page_health', kind=kind, step='menu_entry', reason=state['reason'])
    steps.append('menu_entry')
    try:
        state = await _menu_entry(session, kind)
    except Exception as exc:
        state = {'healthy': False, 'reason': 'menu_entry_failed', 'needs_admin': False, 'has_draft': False, 'error': type(exc).__name__}
    if state['healthy']:
        log('state', type='page_health', kind=kind, step='healed', healed_by='menu_entry', reason=cause)
        _record(session, kind, state, 'healed')
        return {**state, 'healed_by': 'menu_entry', 'steps': steps}
    if state['needs_admin']:
        log('state', type='page_health', kind=kind, step='needs_admin', reason=state['reason'])
        state = {**state, 'steps': steps}
        _record(session, kind, state, 'needs_admin')
        return state
    session._page_health_exhausted_at = time.time()
    state = {**state, 'exhausted': True, 'cause': cause, 'steps': steps, 'screenshot': await _screenshot(session)}
    log('state', type='page_health', kind=kind, step='exhausted', reason=state['reason'], cause=cause)
    _record(session, kind, state, 'exhausted')
    return state


def kind_of(url):
    path = urlparse(url or '').path
    return next((kind for kind, (_, p) in PAGES.items() if path == p), None)


NAMES = {'chat': '沟通页', 'recommend': '推荐牛人页'}
HEALED_BY = {'reload': '刷新', 'tab_reset': '重新打开标签页', 'menu_entry': '从左侧菜单进入'}
REASON_TEXT = {'loading_stuck': '一直停在“加载中”', 'content_missing': '内容没出来（停在启动页或空白）', 'wrong_page': '没有停在该页',
               'login_required': '登录已失效', 'verification_required': '平台要求验证', 'draft_present': '输入框里有没发出去的文字',
               'unreadable': '页面读不到', 'tab_reset_failed': '重新打开标签页失败', 'menu_missing': '另一页上找不到左侧菜单入口',
               'menu_not_unique': '左侧菜单入口不唯一，没有点', 'timed_out': '检查超时（超过时间预算），已停止检查', 'cooling_down': '刚自动处理过，冷却中', 'menu_obscured': '左侧菜单入口被遮挡，没有点', 'menu_entry_failed': '从菜单进入失败'}


PAGE_CHECK_SECONDS = 120   # 环境检查每页的时间预算：两页加回到沟通页仍在环境检查 300 秒上限内


async def check_pages(session):
    """环境检查用：沟通页、推荐牛人页各检查一次，不正常就逐级自愈，最后回到沟通页。

    不预先跳转：heal 先读当前页（有草稿、掉登录、验证就停在原页），需要时由它自己打开目标页。每页有时间预算，超时即停止。"""
    results = []
    for kind in ('chat', 'recommend'):
        try:
            state = await asyncio.wait_for(heal(session, kind, force=True), PAGE_CHECK_SECONDS)
        except asyncio.TimeoutError:
            log('state', type='page_health', kind=kind, step='check_timed_out', seconds=PAGE_CHECK_SECONDS)
            results.append({'kind': kind, 'healthy': False, 'reason': 'timed_out', 'needs_admin': False, 'has_draft': False})
            return results
        results.append({'kind': kind, **state})
        if state.get('needs_admin') or state.get('has_draft'):
            return results
    if kind_of(getattr(session.page, 'url', '')) != 'chat':
        await session.navigate(CHAT_URL)
    return results


def done_steps(steps):
    """把实际做过的自愈步骤写成“自动刷新 2 次、重新打开标签页 1 次……”；没有记录时返回空串。"""
    steps = list(steps or [])
    parts = [text + ' ' + str(steps.count(step)) + ' 次' for step, text in (('reload', '刷新'), ('tab_reset', '重新打开标签页'), ('menu_entry', '从左侧菜单重新进入')) if step in steps]
    return '自动' + '、'.join(parts) if parts else ''


def describe(results):
    parts = []
    for r in results:
        name = NAMES[r['kind']]
        if r.get('healthy') and r.get('has_draft'):
            parts.append(name + '正常，但输入框里有没发出去的文字；为了不丢掉它，没有离开该页，未检查其他页面')
        elif r.get('healthy'):
            parts.append(name + '正常' + ('（' + HEALED_BY[r['healed_by']] + '后恢复）' if r.get('healed_by') in HEALED_BY else ''))
        elif r.get('needs_admin'):
            parts.append(name + '不正常：' + REASON_TEXT.get(r.get('reason'), str(r.get('reason'))) + '；需要管理员在执行浏览器处理，系统没有刷新页面')
        else:
            done = '已' + (done_steps(r.get('steps')) or '自动处理') + '，仍未恢复' if r.get('exhausted') else '自动处理未完成'
            stuck = REASON_TEXT.get(r.get('cause') or r.get('reason'), str(r.get('cause') or r.get('reason')))
            last = '' if not r.get('cause') or r.get('cause') == r.get('reason') else '（最后一步：' + REASON_TEXT.get(r.get('reason'), str(r.get('reason'))) + '）'
            parts.append(name + '不正常：' + stuck + '；' + done + last + '。请打开执行浏览器（http://127.0.0.1:6080）查看')
    return '；'.join(parts)
