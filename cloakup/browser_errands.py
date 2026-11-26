"""浏览器代办（Task 7，11-25）：管理员登记的 BOSS 页面读写之外的小差事——读取已发布岗位列表、绑定岗位。

记录 browser_errand：kind=job_list|job_bind，status=pending|running|done|failed。
- 执行器运行中（含待机）：只在每轮开始的安全点、任务执行时段内做，一轮最多一条，持有浏览器诊断锁；做完回沟通页。
- 执行器未运行：直接起一次 BOSS 操作（mode=boss_errand，与 boss_bind 相同的浏览器占用规则），把排队的代办依次做完。
- 单条失败只标记这一条并生成待办（重试或管理员放弃后关闭）；登录失效、平台校验是页面级问题，照常停止。
读到的岗位列表只存本机（boss_job_list），不发给模型；绑定沿用现有 boss_bind 逻辑（读 JD、建岗位档案、生成计划草稿）。
"""
import asyncio
import os
import time
import uuid

from . import boss_jobs
from .boss_recommend import JOB_ID
from .logbook import log
from .sourcing import SourcingError

KIND = 'browser_errand'
LIST = 'boss_job_list'
LIST_ID = 'boss'
KINDS = {'job_list': '读取 BOSS 岗位列表', 'job_bind': '绑定岗位'}
STEPS = {'job_list': '读取推荐页岗位下拉与职位管理列表', 'job_bind': '读取岗位 JD 并生成计划草稿'}
OPEN = ('pending', 'running')
MAX_BIND = 20
STUCK_SECONDS = 30 * 60
# 没有执行器时一次运行最多做几个绑定（每个都要读 JD、调模型）；剩下的留给执行器或下一次请求，免得长时间占着浏览器挡住任务启动。
DIRECT_BINDS = 3
RUN_ACTIVE = {'queued', 'running', 'pausing', 'paused', 'waiting', 'compiling', 'cancelling', 'closing'}
DRAFT = '沟通页有未发送的草稿，未离开页面；请管理员在执行浏览器处理草稿后重试'
INTERRUPTED = '上次执行被中断（服务重启或运行已结束），未完成；可在岗位导入页重试'


def latest(db):
    try:return db.get(LIST, LIST_ID)
    except FileNotFoundError:return None


def label(record):
    return KINDS[record['kind']] + ('「' + record['title'] + '」' if record.get('title') else '')


def scope(record):
    """重试判定：同一类代办（绑定按岗位）。"""
    return (record['kind'], record.get('encrypt_id'))


def _open(db):
    return sorted((r for r in db.list(KIND) if r['status'] in OPEN), key=lambda r: r['at'])


def _queue(db):
    return [r for r in _open(db) if r['status'] == 'pending']


def _complete(case):
    """绑定做完：读到 JD（snapshot）且计划草稿已生成（search_plan）。只建了档的半成品不算，重试时接着用它。"""
    return bool(case['job'].get('snapshot') and case.get('search_plan'))


def _identified(manager):
    """岗位档案按 encryptId 索引（含半成品）。"""
    from .boss_recommend import job_identity
    found = {}
    for case in manager.cases.list():
        try:eid = job_identity(case)
        except Exception:continue
        if eid not in found or _complete(case):found[eid] = case
    return found


def _bound(manager):
    """已绑定岗位：encryptId -> 绑定做完的岗位档案。"""
    return {eid: case for eid, case in _identified(manager).items() if _complete(case)}


def settle(manager):
    """停在 running、但所属运行已不在进行中的代办（服务重启、运行结束）：标为失败并生成待办，可重试。"""
    jobs = getattr(manager, 'jobs', None) or {};moved = []
    for record in manager.db.list(KIND):
        if record['status'] != 'running' or (jobs.get(record.get('run_id')) or {}).get('status') in RUN_ACTIVE:continue
        moved.append(_finish(manager, record, 'failed', actor='system', error=INTERRUPTED))
    return moved


def view(manager, task_id=None):
    """岗位选择器的只读视图：最近一次读取 + 标注已绑定、在本任务／其他任务、平台上尚未可推荐、绑定排队中。"""
    settle(manager)
    db = manager.db;doc = latest(db) or {};bound = _bound(manager)
    owners = {}
    for member in manager.account_execution.members():
        if member.get('status') == 'stopped':continue
        for cid in member.get('case_ids', []):owners.setdefault(cid, []).append(member['id'])
    errands = _open(db);binding = {r.get('encrypt_id') for r in errands if r['kind'] == 'job_bind'}
    jobs = []
    for item in doc.get('jobs', []):
        eid = item.get('encrypt_id');case = bound.get(eid) if eid else None
        tasks = owners.get(case['id'], []) if case else []
        jobs.append({**item, 'bound_case_id': case['id'] if case else None, 'bound_label': case['job'].get('label') if case else None, 'task_ids': tasks,
                     'in_task': bool(task_id) and task_id in tasks, 'other_task': any(t != task_id for t in tasks), 'binding': bool(eid) and eid in binding,
                     'bindable': bool(eid and item.get('recommendable') and not case and eid not in binding)})
    recent = sorted(db.list(KIND), key=lambda r: r['at'], reverse=True)[:10]
    brief = lambda r: {k: r.get(k) for k in ('id', 'kind', 'title', 'encrypt_id', 'status', 'error', 'at', 'finished_at')}
    return {'read_at': doc.get('read_at'), 'truncated': bool(doc.get('truncated')), 'jobs': jobs, 'pending': [brief(r) for r in errands],
            'recent': [brief(r) for r in recent]}


def _route(manager):
    """谁来做：direct（没有浏览器操作在跑）、executor（账号执行器在安全点做）、queued（代办运行正在做）；其他浏览器操作在跑时拒绝。"""
    owner = manager.busy()
    if owner is None:
        if not window_open(manager):raise ValueError('当前不在任务执行时段，不操作网站；请在执行时段内再读取或绑定岗位')
        return 'direct'
    if owner.get('account_runner'):return 'executor'
    if owner.get('mode') == 'boss_errand':return 'queued'
    raise ValueError('浏览器正被其他操作占用，等它结束后再读取或绑定岗位')


async def _dispatch(manager, route, records):
    if route != 'direct':return
    try:await manager.create({'mode': 'boss_errand', 'params': {}})
    except ValueError as exc:
        for record in records:
            if manager.db.get(KIND, record['id'])['status'] == 'pending':_finish(manager, record, 'failed', error='未能开始浏览器操作：' + str(exc)[:200], actor='admin')
        raise


def _new(manager, kind, **fields):
    record = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'pending', 'at': time.time(), 'actor': 'admin', **fields}
    manager.db.put(KIND, record['id'], record)
    log('state', type='browser_errand', step='requested', errand_id=record['id'], kind=kind, actor='admin', reason=label(record))
    return record


async def request_refresh(manager):
    """登记一次岗位列表读取；已有排队的读取就复用。没有浏览器操作在跑时立即开始。"""
    route = _route(manager)
    record = next((r for r in _open(manager.db) if r['kind'] == 'job_list'), None) or _new(manager, 'job_list')
    await _dispatch(manager, route, [record])
    return manager.db.get(KIND, record['id'])


async def request_bind(manager, body):
    """只接受最近一次读取里可推荐、未绑定、未在排队的岗位；每个岗位一条代办。"""
    ids = body.get('encrypt_ids') if isinstance(body, dict) else None
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BIND or len(set(map(str, ids))) != len(ids):raise ValueError('请选择1至20个不重复的岗位')
    if any(not isinstance(i, str) or not JOB_ID.fullmatch(i) for i in ids):raise ValueError('岗位标识格式不符')
    if not (os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')):
        raise ValueError('尚未配置模型凭据，绑定岗位需要生成计划草稿；请设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN 后重启工作台')
    route = _route(manager)
    doc = latest(manager.db)
    if not doc:raise ValueError('请先刷新岗位列表，再选择要绑定的岗位')
    jobs = {j['encrypt_id']: j for j in doc.get('jobs', []) if j.get('encrypt_id')}
    bound = _bound(manager);queued = {r.get('encrypt_id') for r in _open(manager.db) if r['kind'] == 'job_bind'}
    for eid in ids:
        job = jobs.get(eid)
        if not job:raise ValueError('岗位不在最近一次读取的列表里，请刷新后再选')
        if not job.get('recommendable'):raise ValueError('岗位「' + job['title'] + '」平台上还未生效为可推荐岗位，稍后刷新再试')
        if eid in bound:raise ValueError('岗位「' + job['title'] + '」已绑定，不能重复绑定')
        if eid in queued:raise ValueError('岗位「' + job['title'] + '」已有绑定等待执行')
    records = [_new(manager, 'job_bind', encrypt_id=eid, title=jobs[eid]['title'], job_url=boss_jobs.job_url(eid)) for eid in ids]
    await _dispatch(manager, route, records)
    return [manager.db.get(KIND, r['id']) for r in records]


def window_open_db(db, now=None):
    """执行时段：有运行中的任务时看运行中任务的时段；否则看暂停（未停止）任务的时段；都没有时按默认时段，不是不限。"""
    from .task_policy import window_status
    members = db.list('account_task')
    running = [m for m in members if m.get('status') == 'running']
    pool = running or [m for m in members if m.get('status') == 'paused']
    if not pool:return window_status(None, now)['allowed']
    for member in pool:
        try:schedule = db.get('task', member['id'])['config'].get('schedule')
        except (FileNotFoundError, KeyError):schedule = None
        if window_status(schedule, now)['allowed']:return True
    return False


def window_open(manager, now=None):
    return window_open_db(manager.db, now)


def _finish(manager, record, status, *, actor, error=None, result=None):
    record.update(status=status, finished_at=time.time(), error=error, result=result)
    manager.db.put(KIND, record['id'], record)
    if status == 'failed':log('state', type='browser_errand', step='failed', errand_id=record['id'], kind=record['kind'], actor=actor, reason=error)
    else:log('action', type='browser_errand', step='done', errand_id=record['id'], kind=record['kind'], actor=actor, reason=label(record))
    return record


async def _to_chat(session):
    try:await session.navigate(boss_jobs.CHAT_URL)
    except Exception as exc:log('error', type='browser_errand', step='return_failed', error_type=type(exc).__name__)


async def _chat_blocker(session):
    """离开沟通页之前只读检查一次（约束 5）：跳转会丢掉输入框里未发送的草稿。掉登录、平台校验按页面级问题报出。"""
    from . import page_health
    state = await page_health.check(session, 'chat')
    if state.get('reason') == 'login_required':raise SourcingError(boss_jobs.LOGIN)
    if state.get('reason') == 'verification_required':raise SourcingError(boss_jobs.CHALLENGE)
    if state.get('has_draft'):return 'draft'
    if state.get('reason') == 'unreadable':return 'unreadable'
    return None


async def _page_problem(session):
    """绑定失败后核一次页面：掉登录或平台校验才算页面级问题。"""
    try:await boss_jobs._guard(session)
    except SourcingError as exc:return exc
    except Exception:return None
    return None


async def _run_one(manager, job, session, record, actor):
    """做一条代办；沟通页读不出来时不动页面、留着排队（返回 None）。"""
    try:blocked = await _chat_blocker(session)
    except SourcingError as exc:
        _finish(manager, record, 'failed', actor=actor, error=str(exc))
        manager.event(job, 'step', '浏览器代办未完成：' + label(record) + '（' + str(exc)[:80] + '）；页面需要管理员处理，任务停止')
        raise
    if blocked == 'unreadable':
        log('state', type='browser_errand', step='deferred', errand_id=record['id'], kind=record['kind'], actor=actor, reason='chat_unreadable')
        return None
    if blocked == 'draft':
        _finish(manager, record, 'failed', actor=actor, error=DRAFT)
        manager.event(job, 'step', '浏览器代办未完成：' + label(record) + '（沟通页有未发送的草稿，未离开页面）；只标记这一条，任务其他工作照常')
        return record
    record.update(status='running', started_at=time.time(), run_id=job.get('id'))
    manager.db.put(KIND, record['id'], record)
    log('action', type='browser_errand', step='started', errand_id=record['id'], kind=record['kind'], actor=actor, reason=label(record))
    try:
        if record['kind'] == 'job_list':
            outcome = await boss_jobs.read_published(session)
            manager.db.put(LIST, LIST_ID, {'id': LIST_ID, **outcome, 'errand_id': record['id']})
            result = {'jobs': len(outcome['jobs']), 'recommendable': outcome['option_count'], 'truncated': outcome['truncated']}
            summary = '读到 ' + str(len(outcome['jobs'])) + ' 个岗位，其中 ' + str(outcome['option_count']) + ' 个可在推荐页寻访' + ('；列表只读了第一页' if outcome['truncated'] else '')
        else:
            existing = _identified(manager).get(record['encrypt_id'])
            if existing and _complete(existing):
                result = {'case_id': existing['id'], 'already_bound': True};summary = '该岗位已绑定，未重复绑定'
            else:
                # 上次只建了档（读 JD 或生成计划草稿时失败、被中断）：接着用这份档案，不另建；地址里的岗位标识由绑定入口在建档前核对。
                outcome = await manager.bind_boss_job(job, session, record['job_url'], case_id=existing['id'] if existing else None, encrypt_id=record['encrypt_id'])
                result = {'case_id': outcome['case_id'], 'job_title': outcome['job_title']};summary = outcome['summary']
            await _to_chat(session)
    except asyncio.CancelledError:
        # 被停止：放回排队，下次重做（绑定前会先查是否已绑定，不会重复建档）。
        record.update(status='pending');manager.db.put(KIND, record['id'], record)
        raise
    except Exception as exc:
        page = exc if isinstance(exc, SourcingError) and str(exc) in (boss_jobs.LOGIN, boss_jobs.CHALLENGE) else None
        if page is None and record['kind'] == 'job_bind':page = await _page_problem(session)
        error = str(page or exc)[:300] or type(exc).__name__
        _finish(manager, record, 'failed', actor=actor, error=error)
        manager.event(job, 'step', '浏览器代办未完成：' + label(record) + '（' + error[:80] + '）；' + ('页面需要管理员处理，任务停止' if page else '只标记这一条，任务其他工作照常'))
        if page:raise page from exc
        await _to_chat(session)
        return record
    _finish(manager, record, 'done', actor=actor, result=result)
    manager.event(job, 'step', '浏览器代办已完成：' + label(record) + '；' + summary)
    return record


async def run_pending(manager, job, session, *, limit=None, actor, ids=None, binds=None):
    """依次做排队的代办：limit 条为止；ids 只做这些记录；binds 限制绑定条数。沟通页读不出来就停，留着排队。"""
    done = [];bound = 0
    for record in _queue(manager.db):
        if limit is not None and len(done) >= limit:break
        if ids is not None and record['id'] not in ids:continue
        if record['kind'] == 'job_bind' and binds is not None:
            if bound >= binds:continue
            bound += 1
        if manager.db.get(KIND, record['id'])['status'] != 'pending':continue
        outcome = await _run_one(manager, job, session, record, actor)
        if outcome is None:break
        done.append(outcome)
    return done


async def at_safe_point(manager, job, session):
    """执行器每轮开始（含待机）调用：有代办、执行时段内、未被管理员接管、浏览器没有被诊断或环境检查占用时，做一条。"""
    db = getattr(manager, 'db', None)
    if db is None or not hasattr(db, 'list'):return []
    settle(manager)
    if not _queue(db):return []
    if job.get('manual_takeover') or job.get('pause_requested') or not window_open(manager):return []
    lock = getattr(manager, '_browser_diagnostic_lock', None)
    if lock is None:lock = manager._browser_diagnostic_lock = asyncio.Lock()
    preflight = getattr(manager, 'preflight_task', None)
    if lock.locked() or (preflight is not None and not preflight.done()):return []
    async with lock:
        return await run_pending(manager, job, session, limit=1, actor='executor')


async def run_direct(manager, job, session):
    """执行器未运行时的一次 BOSS 操作：只做开始时已排队的代办，且最多 DIRECT_BINDS 个绑定；时段外不操作网站。"""
    settle(manager)
    snapshot = [r['id'] for r in _queue(manager.db)];job['errand_snapshot'] = snapshot
    if not window_open(manager):
        return {'status': 'succeeded', 'summary': '当前不在任务执行时段，未操作网站；' + str(len(snapshot)) + ' 条代办保持排队。', 'errand_ids': []}
    done = await run_pending(manager, job, session, actor='admin', ids=set(snapshot), binds=DIRECT_BINDS)
    failed = [r for r in done if r['status'] == 'failed'];left = [r for r in _queue(manager.db) if r['id'] in snapshot]
    summary = '已处理 ' + str(len(done)) + ' 条浏览器代办' + ('，' + str(len(failed)) + ' 条未完成：' + '；'.join(label(r) + '（' + (r.get('error') or '')[:60] + '）' for r in failed) if failed else '，全部完成' if done else '')
    if left:summary += '；还有 ' + str(len(left)) + ' 条在排队，执行器运行时在安全点处理，或再点一次刷新继续'
    return {'status': 'failed' if failed else 'succeeded', 'summary': summary + '；平台内容未修改。', 'errand_ids': [r['id'] for r in done]}


async def after_run(manager, job):
    """代办运行结束后：运行期间新登记的代办（不在开始时的快照里）不能被丢下——浏览器空闲、在执行时段内就再起一次（同样有上限）。"""
    if 'errand_snapshot' not in job:
        # 浏览器没起来（如授权未通过），这次运行根本没碰代办：不续跑，否则会无上限地反复启动浏览器（11-26 现场）。
        log('state', type='browser_errand', step='follow_up_skipped', actor='system', reason='run_never_reached_queue');return None
    late = [r for r in _queue(manager.db) if r['id'] not in job['errand_snapshot']]
    if not late or manager.busy() or not window_open(manager):return None
    try:return await manager.create({'mode': 'boss_errand', 'params': {}})
    except ValueError as exc:
        log('state', type='browser_errand', step='follow_up_skipped', actor='system', reason=str(exc)[:200]);return None
