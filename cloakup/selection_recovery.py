"""Bounded recovery for selecting a conversation, never for sending a message."""
import asyncio
import os
import time
import uuid

from .pacing import jitter

MAX_ATTEMPTS = 3
# 右边详情没画出来、列表里暂时找不到、这一行的姓名或岗位对不上，都只属于这一条会话。
# 不能和登录、验证码、草稿混在一起，也不能因此暂停整个任务。
ISOLATABLE_REASONS = (
    'header_name_unverified', 'header_job_unverified', 'header_identity_unverified', 'detail_not_open',
    'identity_ambiguous', 'target_missing', 'target_row_missing', 'list_identity_mismatch',
    # 恢复轮次里原会话仍未核实（含收件人与岗位对不上）：同样只属于原失败的那一条。
    'target_unverified',
)
BLOCK_REASONS = {
    'draft_present': '发现未发送的草稿，已保留原内容，请管理员检查后继续',
    'login_required': '登录已失效，请管理员在执行浏览器重新登录后继续',
    'verification_required': '平台要求验证，请管理员处理后继续',
    'page_ambiguous': '存在多个沟通标签页，无法安全确定操作目标，请管理员检查',
    'page_missing': '没有可核验的沟通标签页，请管理员检查执行浏览器',
    'page_changed': '沟通页面已变化，请管理员检查后继续',
    'editor_unavailable': '无法核验编辑器是否存在草稿，已停止自动恢复，请管理员检查',
}


# 11-24 20:40 沟通页卡在启动页：系统因“无法核验编辑器”（页面打不开）暂停的任务，暂停期间在执行时段内定时自愈，
# 页面确认安全后由系统恢复。前 3 次约每 PAGE_RETRY_SECONDS 秒（不超过 PAGE_RETRY_MAX），之后约每 PAGE_BACKOFF_SECONDS 秒
# （不超过 PAGE_BACKOFF_MAX）；同一次暂停最多 PAGE_MAX_ATTEMPTS 次，每天最多 PAGE_DAILY_ATTEMPTS 次；
# 同一任务每天最多自动恢复 PAGE_DAILY_RESUMES 次，防止反复暂停、恢复。用完即停，由管理员处理。
# 管理员暂停、登录、验证、草稿等原因的暂停一律不自动恢复。
PAGE_BLOCK_REASON = 'selection_blocked:editor_unavailable'
PAGE_RETRY_SECONDS = 600
PAGE_RETRY_MAX = 900
PAGE_BACKOFF_SECONDS = 1800
PAGE_BACKOFF_MAX = 2700
PAGE_FAST_ATTEMPTS = 3
PAGE_MAX_ATTEMPTS = 6
PAGE_DAILY_ATTEMPTS = 12
PAGE_DAILY_RESUMES = 3
PAGE_STUCK_REASONS = ('loading_stuck', 'content_missing')
# 暂停期间自愈时发现、必须管理员处理的页面问题（给管理员的说明）。
PAGE_PROBLEMS = {'draft_present': '发现输入框里有没发出去的文字', 'login_required': '登录已失效', 'verification_required': '平台要求验证'}
# 停止自动重试的原因（给管理员的说明）。
PAGE_CAPS = {'incident': '本次暂停已自动尝试 ' + str(PAGE_MAX_ATTEMPTS) + ' 次仍未恢复',
             'daily_attempts': '今天已自动尝试 ' + str(PAGE_DAILY_ATTEMPTS) + ' 次',
             'daily_resumes': '该任务今天已因页面问题自动恢复 ' + str(PAGE_DAILY_RESUMES) + ' 次又被暂停'}


def page_blocked(member):
    """任务当前是否正处于“系统因页面打不开而暂停”。"""
    last = (member.get('history') or [{}])[-1]
    return member.get('status') == 'paused' and last.get('actor') == 'system' and last.get('reason') == PAGE_BLOCK_REASON


def page_stuck(health, paused_at=None):
    """最近一次页面检查是否显示页面卡住（卡在加载、内容缺失或自愈用尽）。计划的第二个条件：编辑器读不到不一定是页面卡住。
    给了暂停时间时，只认暂停前 15 分钟以内的检查结论，旧的“用尽”记录不能让无关的暂停被自动处理。"""
    if paused_at is not None and (health or {}).get('at', 0) < paused_at - 900:return False
    return bool(health) and not health.get('healthy') and (bool(health.get('exhausted')) or health.get('reason') in PAGE_STUCK_REASONS)


def page_resumes_today(member, day):
    from .autonomy import day_key
    return sum(1 for h in member.get('history') or [] if h.get('reason') == 'page_recovered' and h.get('status') == 'running' and day_key(h.get('at')) == day)


# 一条会话刚被隔离时，先暂缓新开聊，给只读复核留出时间；超过这个时长仍未核实，
# 只把这一条留在隔离里（并生成管理员待办），不再拦整个岗位的新开聊。
OUTREACH_HOLD_SECONDS = 2 * 3600


def outreach_blockers(case, now=None):
    """仍在拦新开聊的隔离会话：管理员未设例外、且隔离不足 OUTREACH_HOLD_SECONDS。没有隔离时间的旧记录视为已超时。"""
    now = time.time() if now is None else now
    since = case.get('deferred_chat_since') or {}
    allowed = set(case.get('outreach_isolation_exceptions') or [])
    return [chat_id for chat_id in case.get('deferred_chat_ids') or []
            if chat_id not in allowed and now - since.get(chat_id, 0) < OUTREACH_HOLD_SECONDS]

def unresolved(job, case_id):
    record = job.get('selection_recovery', {}).get(case_id)
    return record if record and record['status'] not in ('recovered','deferred') else None


def _chat_attempt(prior, chat_id):
    """额度按会话计。另一条会话的失败不能把这一条直接推到第 3 次。"""
    counts = dict(prior.get('chat_attempts') or {})
    if not chat_id:
        return counts, min(prior.get('attempts', 0) + 1, MAX_ATTEMPTS)
    if chat_id in counts:
        base = counts[chat_id]
    elif prior.get('failures') == [chat_id]:
        base = prior.get('attempts', 0)
    else:
        base = 0
    counts[chat_id] = min(base + 1, MAX_ATTEMPTS)
    return counts, counts[chat_id]


def _attributed_chat(prior, exc, block_reason):
    """异常没带会话编号时，按本次恢复正在核验的那一条会话计次。

    恢复轮次核验的就是 prior['failures'] 里的会话；“恢复后仍未核验目标会话”这类异常由外层包装，
    丢了编号。不归到那一条，次数就会记到整单上，第 3 次直接暂停整个任务。
    登录、验证码、草稿等页面级阻塞不做归属。
    """
    chat_id = getattr(exc, 'chat_id', None)
    if chat_id or block_reason or exc is None:
        return chat_id
    if (getattr(exc, 'diagnostics', None) or {}).get('reason') not in ISOLATABLE_REASONS:
        return None
    known = [item for item in prior.get('failures') or [] if item]
    if not known:
        return None
    last = prior.get('last_failed_chat_id')
    return last if last in known else known[-1]


def failure(manager, job, case, exc=None, *, block_reason=None):
    """Persist each attempt before yielding; same incident produces one alert."""
    cid = case['id']
    prior = unresolved(job, cid) or {}
    now = time.time()
    chat_id = _attributed_chat(prior, exc, block_reason)
    chat_attempts, attempts = _chat_attempt(prior, chat_id)
    reason_code = getattr(exc, 'diagnostics', {}).get('reason') if exc else None
    # 详情空白或列表里暂时没有这一条，用完的是这一条自己的次数，不暂停整个任务。
    chat_exhausted = bool(chat_id) and not block_reason and attempts >= MAX_ATTEMPTS and reason_code in ISOLATABLE_REASONS
    blocked = bool(block_reason) or (attempts >= MAX_ATTEMPTS and not chat_exhausted)
    member = next((m for m in manager.account_execution.members() if cid in m['case_ids'] and m['status'] != 'stopped'), None) if getattr(manager, 'account_execution', None) else None
    task_id = member['id'] if member else job.get('task_id')
    failures = list(prior.get('failures', []))
    if chat_id and chat_id not in failures:
        failures.append(chat_id)
    reason = BLOCK_REASONS.get(block_reason) if block_reason else None
    if not reason and getattr(exc,'diagnostics',{}).get('exception_type')=='TimeoutError' and getattr(exc,'diagnostics',{}).get('stage') in ('recover_guard','recover_verify'):
        reason='页面读取通道超时，已暂停任务；请检查执行浏览器与页面读取诊断，尚未发送消息' if blocked else '页面读取通道暂未响应，等待下一轮只读核验；期间不新增开聊'
    if not reason:
        detail={
            'target_row_missing':'当前沟通列表没有找到原目标会话',
            'row_not_unique':'目标会话在列表中不唯一',
            'list_identity_mismatch':'列表中的候选人与岗位信息和本地绑定不一致',
            'identity_ambiguous':'多条会话匹配当前详情，缺少唯一的选中会话证据',
            'selected_target_mismatch':'页面当前选中的会话与本次目标不一致',
            'detail_not_open':'右侧详情尚未打开',
            'header_job_unverified':'详情头部未匹配目标岗位',
            'header_name_unverified':'详情头部未匹配目标收件人',
            'header_identity_unverified':'会话已尝试打开，但详情头部的候选人与岗位尚未核实',
            'message_list_unavailable':'会话身份已核实，但真实消息列表尚未读取到',
            'outside_viewport':'目标会话行未进入可点击区域',
            'moving_or_obscured':'目标会话行移动或被其他界面遮挡',
        }.get(getattr(exc,'diagnostics',{}).get('reason'))
        if detail:reason=detail+('；任务已暂停，未发送消息' if blocked else '；等待下轮只读核验，未发送消息')
    reason = reason or ('会话核验仍未通过，已暂停任务，请检查执行浏览器与具体失败环节' if blocked else '会话选择暂未完成，将按退避间隔安全重读；暂停该岗位新增开聊')
    # 退避时长随机抖动，避免 120/240/360 秒这类整齐的线性重试节奏。
    delay = jitter(max(120, case.get('autonomy', {}).get('poll_seconds', 120)) * attempts)
    record = {**prior, 'incident_id': prior.get('incident_id') or uuid.uuid4().hex,
              'case_id': cid, 'task_id': task_id, 'status': 'blocked' if blocked else 'retrying',
              'attempts': attempts, 'max_attempts': MAX_ATTEMPTS,
              'next_check_at': None if blocked else now + delay,
              'reason': reason, 'failures': failures, 'chat_attempts': chat_attempts,
              'last_failed_chat_id':chat_id or prior.get('last_failed_chat_id'), 'updated_at': now,
              'receiver': (getattr(exc,'receiver','') or prior.get('receiver')) if chat_id else prior.get('receiver'),
              'diagnostics': getattr(exc, 'diagnostics', None) or {'stage': 'recover_guard', 'reason': block_reason, 'exception_type': None}}
    job.setdefault('selection_recovery', {})[cid] = record
    job.setdefault('case_results', {})[cid] = {'status': 'held', 'summary': reason, 'at': now}
    # 页面本身正常时，一条会话打不开或暂时对不上岗位，只放下这一条，不暂停整个任务。
    # 已经放下过别的会话，也不影响这一条继续单独放下。
    isolatable = chat_exhausted and member and hasattr(manager, 'cases')
    if isolatable:
        fresh = manager.cases.load(cid)
        kept = [item for item in fresh.get('deferred_chat_ids') or [] if item != chat_id]
        fresh['deferred_chat_ids'] = kept + [chat_id]
        fresh.setdefault('deferred_chat_since', {}).setdefault(chat_id, now)
        fresh['deferred_chat_check_at'] = now + jitter(900)
        manager.cases.save(fresh)
        chat_attempts.pop(chat_id, None)
        failures = [item for item in failures if item != chat_id]
        if failures:
            record.update(status='retrying', failures=failures, chat_attempts=chat_attempts,
                          attempts=max(chat_attempts.values()) if chat_attempts else 0,
                          next_check_at=now + delay,
                          reason='这条会话已先放下，继续核验其余未完成的会话；未暂停任务')
        else:
            record.update(status='deferred', failures=[], chat_attempts={}, next_check_at=None,
                          reason='单条会话暂时打不开，已先放下这一条并继续其他会话；稍后只读复核')
    if blocked and not isolatable and member:
        manager.account_execution.set_task(member['id'], member['case_ids'], 'paused',
                                           reason=f"selection_blocked:{block_reason or (record.get('diagnostics') or {}).get('reason') or 'unknown'}")
    from .logbook import log
    log('state', type='selection_failure', run_id=job.get('id'), case_id=cid, status=record.get('status'),
        attempts=record.get('attempts'), chat_id=chat_id, chat_attempts=record.get('chat_attempts'),
        failures=record.get('failures'), block_reason=block_reason, diagnostics=record.get('diagnostics'),
        exception=type(exc).__name__ if exc else None, detail=str(exc)[:500] if exc else None, reason=record.get('reason'))
    manager.persist(job)
    manager.event(job, 'step', record['reason'] + f'（恢复核验 {attempts}/{MAX_ATTEMPTS}）')
    return record


async def preserve_scene(manager, job, record, session):
    """Save the blocked viewport before any further browser operations."""
    if record.get('screenshot_id'):
        return
    directory = getattr(manager, 'data', None)
    page = getattr(session, 'page', None)
    if directory is None or page is None:
        return
    screenshot_id = uuid.uuid4().hex
    try:
        data = await asyncio.wait_for(page.screenshot(type='jpeg', timeout=2500), timeout=3)
        path = directory / job['id'] / f'incident-{screenshot_id}.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as file:
            file.write(data)
    except Exception:
        record['screenshot_failed'] = True
    else:
        record.update(screenshot_id=screenshot_id, screenshot_failed=False)
    manager.persist(job)


def recovered(manager, job, cid):
    record = unresolved(job, cid)
    if not record:
        return
    record.update(status='recovered', next_check_at=None, failures=[], updated_at=time.time(),
                  reason='已重新核验实际目标会话，恢复正常调度')
    manager.persist(job)
    manager.event(job, 'step', '会话选择已恢复：实际目标会话和岗位已重新核验')


def public_records(runs):
    """Latest incident state for the local operator."""
    latest = {}
    for run in runs:
        for cid, value in run.get('selection_recovery', {}).items():
            key = (value.get('task_id'), cid)
            if value.get('updated_at', 0) >= latest.get(key, {}).get('updated_at', -1):
                latest[key] = {k: value.get(k) for k in ('task_id', 'incident_id', 'status', 'attempts', 'max_attempts', 'next_check_at', 'reason', 'updated_at', 'screenshot_id', 'screenshot_failed', 'last_failed_chat_id', 'receiver', 'diagnostics')}
                latest[key].update(case_id=cid, run_id=run['id'])
    return list(latest.values())


def resume_verification(manager, job):
    """An explicit task resume requests verification, never clears an incident."""
    members = manager.account_execution.members()
    changed = False
    for cid, record in job.get('selection_recovery', {}).items():
        if record.get('status') != 'blocked':
            continue
        member = next((m for m in members if cid in m['case_ids'] and m['status'] == 'running'), None)
        if not member:
            continue
        record.update(status='retrying', next_check_at=None, updated_at=time.time(),
                      reason='任务已重新启用，等待允许的执行时段，只读核验原目标会话；核验成功后恢复沟通',
                      resume_verification=True)
        changed = True
    if changed:
        manager.persist(job)
    return changed
