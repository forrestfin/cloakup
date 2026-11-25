"""从 BOSS 读取已发布岗位（Task 7，11-25）：岗位 id 只取推荐页岗位下拉，职位管理列表按名称补公司／城市等标签与状态。

职位管理列表行里没有 encryptId；推荐页岗位下拉选项的 value 就是 encryptJobId，且只有出现在下拉里的岗位才能寻访
（docs/design/boss-job-pages.md）。只读：推荐页点开岗位下拉一次、不选选项；职位管理页不点编辑/关闭/更多，不输入。
读完回到沟通页；登录失效、平台校验按页面级问题报出。读到的岗位只存在本机，不发给模型。
"""
import asyncio
import time
from urllib.parse import urlencode

from .boss import validate_job_url
from .boss_recommend import RECOMMEND_FRAME, RECOMMEND_URL, SWITCH_LABEL, JOB_ID
from .logbook import log
from .pacing import jitter
from .sourcing import SourcingError

CHAT_URL = 'https://www.zhipin.com/web/chat/index'
JOB_LIST_URL = 'https://www.zhipin.com/web/chat/job/list'
JOB_FRAME = '/web/frame/job_v2/list'
JOB_EDIT_URL = 'https://www.zhipin.com/web/chat/job/edit'
MAX_ROWS = 50
TEXT_LEN = '((document.body&&document.body.innerText)||"").trim().length'
LOGIN = 'BOSS 已显示登录表单，请在专用窗口重新登录后继续；未读取岗位'
CHALLENGE = '平台出现校验，读取岗位已停止'
# 推荐页岗位下拉选项：value=encryptJobId，文字在 span.label（岗位名 + 城市／薪资等），当前岗位带 curr。
OPTIONS_JS = "Array.from(document.querySelectorAll('.job-selecter-wrap li.job-item')).map(e=>({value:e.getAttribute('value')||'',text:((e.querySelector('span.label')||e).innerText||'').replace(/\\s+/g,' ').trim(),current:e.classList.contains('curr')}))"
# 职位管理列表（“全部”标签当前页）：只读岗位名、标签、状态；最多 MAX_ROWS 行，另报总行数与是否有分页。
ROWS_JS = '''(max=>{const items=Array.from(document.querySelectorAll('ul.job-list-content > li.job-item-container'));
 const t=e=>((e&&e.innerText)||'').replace(/\\s+/g,' ').trim();
 const paged=Array.from(document.querySelectorAll('[class*="pagination"],[class*="pager"]')).some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&/\\b2\\b/.test(t(e))});
 return {total:items.length,paged,rows:items.slice(0,max).map(e=>{const box=e.querySelector('.job-status-wrapper .status-box');
  return {title:t(e.querySelector('.job-name')),labels:Array.from(e.querySelectorAll('.info-labels .divider-label-text')).map(t).slice(0,8),
          status_class:box&&typeof box.className==='string'?box.className:'',status_text:t(box).slice(0,20)}})}})(''' + str(MAX_ROWS) + ')'


def job_url(encrypt_id):
    """绑定用的岗位编辑页地址；id 必须是下拉里读到的合规 encryptJobId。"""
    if not isinstance(encrypt_id, str) or not JOB_ID.fullmatch(encrypt_id):raise ValueError('岗位标识格式不符')
    return validate_job_url(JOB_EDIT_URL + '?' + urlencode({'encryptId': encrypt_id}, safe='~'))


async def _frame_click(session, frame, css):
    from .frame_actions import click
    await click(session, frame, css)


async def _frame_value(session, fragment, expression):
    from .boss_contact import _frame_value as value
    return await value(session, fragment, expression)


async def _read_frame(session, frame, expression):
    from .dom_reader import read_frame_dom
    return await read_frame_dom(session.page, frame, expression, timeout=8)


async def _guard(session):
    """跳转后核一次：掉登录或平台校验就按页面级问题停下。"""
    from urllib.parse import urlparse
    from . import page_health
    url, frames = await page_health.read_frames(session, 'chat')
    path = urlparse(url or '').path
    if any(p in path for p in ('/web/user', '/login', '/passport', '/signin')) or any(f.get('login') for f in frames):raise SourcingError(LOGIN)
    if any(f.get('challenge') for f in frames):raise SourcingError(CHALLENGE)


async def read_recommend_options(session):
    """打开推荐页，点开岗位下拉一次，读每个选项的 value 与文字；不选任何选项。"""
    from . import page_health
    await session.navigate(RECOMMEND_URL)
    state = await page_health.heal(session, 'recommend')
    if state.get('reason') == 'login_required':raise SourcingError(LOGIN)
    if state.get('reason') == 'verification_required':raise SourcingError(CHALLENGE)
    if not state.get('healthy'):raise SourcingError('推荐牛人页多次刷新和重置后仍未正常加载（' + str(state.get('cause') or state.get('reason')) + '），未读取岗位')
    frame = next((f for f in session.page.frames if RECOMMEND_FRAME in f.url), None)
    if frame is None:raise SourcingError('推荐牛人页的岗位区域尚未加载，未读取岗位')
    try:
        await _frame_click(session, frame, SWITCH_LABEL)
        await asyncio.sleep(jitter(1.0, .2))
        raw = await _frame_value(session, RECOMMEND_FRAME, OPTIONS_JS)
    except (RuntimeError, TimeoutError) as exc:
        raise SourcingError('推荐牛人页的岗位下拉未能打开（' + (str(exc)[:60] or type(exc).__name__) + '），未读取岗位') from exc
    options = []
    for item in raw if isinstance(raw, list) else []:
        value = item.get('value') if isinstance(item, dict) else None
        if not isinstance(value, str) or not JOB_ID.fullmatch(value) or any(o['encrypt_id'] == value for o in options):continue
        options.append({'encrypt_id': value, 'text': str(item.get('text') or '')[:120], 'current': bool(item.get('current'))})
    return options


def parse_rows(raw):
    rows = []
    for item in raw:
        title = str(item.get('title') or '').strip()[:80] if isinstance(item, dict) else ''
        if not title:continue
        labels = [str(v).strip() for v in item.get('labels') or [] if str(v).strip()]
        cls, text = str(item.get('status_class') or ''), str(item.get('status_text') or '').strip()
        status = 'open' if 'status-opening' in cls.split() else 'closed' if '已关闭' in text else 'other'
        # 11-25 线上首读：标签依次为公司、城市、区域、经验…（不是城市在先），只原样显示前三个，不按位置猜城市。
        rows.append({'title': title, 'detail': ' · '.join(labels[:3])[:60] or None, 'status': status, 'status_text': text[:20]})
    return rows


async def read_job_rows(session):
    """打开职位管理页，等列表 frame 画好后读“全部”标签当前页（最多 MAX_ROWS 行，不翻页，超出时注明）。"""
    await session.navigate(JOB_LIST_URL)
    value = None
    # 11-25 实采：列表 frame 先以空的 <div id="app"> 出现，约 15 秒内画好；等待有上限。
    for _ in range(30):
        frame = next((f for f in session.page.frames if JOB_FRAME in f.url), None)
        if frame is not None:
            try:value = await _read_frame(session, frame, TEXT_LEN)
            except (RuntimeError, TimeoutError):value = None
            if isinstance(value, int) and value > 20:break
        await asyncio.sleep(jitter(.5, .2))
    await _guard(session)
    if not (isinstance(value, int) and value > 20):raise SourcingError('职位管理列表未加载出来，未读取岗位')
    await asyncio.sleep(jitter(1.0, .2))
    try:raw = await _read_frame(session, frame, ROWS_JS)
    except (RuntimeError, TimeoutError) as exc:raise SourcingError('职位管理列表读取未完成（' + (str(exc)[:60] or type(exc).__name__) + '）') from exc
    if not isinstance(raw, dict):raise SourcingError('职位管理列表读取未完成')
    total = raw.get('total') if type(raw.get('total')) is int else 0
    rows = parse_rows((raw.get('rows') or [])[:MAX_ROWS])
    return {'rows': rows, 'total': total, 'truncated': total > MAX_ROWS or bool(raw.get('paged'))}


SEPARATORS = ' \t\u3000·|/-_,，(（'


def _names(text, title):
    """选项文字为“岗位名 + 空格 + 城市／薪资等”：等于岗位名，或岗位名后紧跟分隔符才算同一岗位（11-25 审阅：“Java开发”不是“Java”）。"""
    return text == title or (text.startswith(title) and text[len(title)] in SEPARATORS)


def merge(options, rows):
    """下拉选项与列表行一一对应才给这一行 id；对不上唯一一行的选项、或与别的选项争同一行的选项，都作为“仅下拉”岗位列出（可绑定），不丢。

    列表里有、下拉里没有（或对应不唯一）的岗位不给 id（recommendable=False）。"""
    claims = {}
    for option in options:
        hits = [i for i, r in enumerate(rows) if _names(option['text'], r['title'])]
        if len(hits) == 1:claims.setdefault(hits[0], []).append(option)
    jobs, used = [], set()
    for i, row in enumerate(rows):
        found = claims.get(i) or []
        option = found[0] if len(found) == 1 else None
        if option:used.add(option['encrypt_id'])
        jobs.append({'encrypt_id': option['encrypt_id'] if option else None, 'title': row['title'], 'detail': row['detail'], 'status': row['status'],
                     'status_text': row['status_text'], 'recommendable': option is not None, 'option_text': option['text'] if option else None})
    for option in options:
        if option['encrypt_id'] in used:continue
        title = option['text'].split()[0] if option['text'].split() else option['text']
        jobs.append({'encrypt_id': option['encrypt_id'], 'title': title[:80], 'detail': None, 'status': None, 'status_text': '',
                     'recommendable': True, 'option_text': option['text']})
    return jobs


async def read_published(session):
    """读推荐页下拉与职位管理列表并合并；结束回沟通页（掉登录、平台校验时不再跳转）。"""
    started = time.time();page_level = False
    try:
        options = await read_recommend_options(session)
        listing = await read_job_rows(session)
    except SourcingError as exc:
        page_level = str(exc) in (LOGIN, CHALLENGE)
        raise
    finally:
        # 推荐页缓存作废：执行器下次开聊会重新打开推荐页，而不是沿用这次读取留下的下拉状态。
        session._boss_recommend_list = None;session._boss_recommend_detail = None
        if not page_level:
            try:await session.navigate(CHAT_URL)
            except Exception as exc:log('error', type='boss_jobs', step='return_failed', error_type=type(exc).__name__)
    jobs = merge(options, listing['rows'])
    log('action', type='boss_jobs_read', option_count=len(options), row_count=len(listing['rows']), truncated=listing['truncated'],
        duration_ms=round((time.time() - started) * 1000))
    return {'jobs': jobs, 'read_at': time.time(), 'truncated': listing['truncated'], 'option_count': len(options), 'row_count': len(listing['rows']),
            'total_rows': listing['total']}

