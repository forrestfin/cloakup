"""每日执行报告：出现了哪些异常、能否自动修复，以及操作效率上可以优化的地方。

只读本地记录（运行事件、任务状态历史、岗位审计与候选人消息、待办、飞书通知），
不访问网站、不调用模型、不改任何业务状态。

前一天的报告在次日凌晨由后台监控生成；服务停机错过的日期，下次启动后补生成，
最多回补 BACKFILL_DAYS 天。
"""
from __future__ import annotations

import os
import re
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KIND = 'daily_report'
VERSION = 2
BACKFILL_DAYS = 14
# 次日过了这个时间才生成前一天的报告，让跨零点仍在写入的记录落盘。
READY_AFTER = timedelta(minutes=10)
CHECK_INTERVAL = 600
IDLE_GAP_MINUTES = 30
QUICK_PASS_SECONDS = 15 * 60


def report_timezone():
    return ZoneInfo(os.environ.get('CLOAKUP_REPORT_TZ') or 'Asia/Shanghai')


# 类别 -> (名称, 能否自动修复, 处理方式)。auto：执行器自己处理；partial：多数自动，个别需人；manual：必须人工。
CATEGORIES = {
    'selection': ('会话选择恢复', 'auto', '单条会话失败自动重试，用完次数只放下这一条；只有页面级问题才暂停任务'),
    'isolation': ('隔离会话暂缓新开聊', 'partial', '到期只读复核通过后自动解除；长期复核不过需管理员核对原会话或允许开聊例外'),
    'outreach': ('岗位推荐页暂无可联系的人', 'auto', '当天不再为该岗位首聊，剩余额度按权重让给其他岗位；次日自动恢复'),
    'outreach_entry': ('开聊入口暂不可用', 'auto', '推荐页没加载出来时先重开一次；仍不行只暂缓开聊约 15 分钟，收件照常'),
    'page_read': ('沟通页读取超时', 'auto', '按收件间隔重读；连续 3 次才停止本轮'),
    'profile_changed': ('推荐资料两次打开不一致', 'auto', '放下该候选人、作废未发送草稿，下一批继续'),
    'release_changed': ('发布版本与任务绑定不一致', 'partial', '岗位问答生效时自动换绑；其他发布需暂停并恢复任务'),
    'release': ('部分岗位验收失效', 'manual', '只停失效岗位的发送，其他岗位照常；需在 Agent 工作空间重新验收该岗位'),
    'license': ('浏览器授权校验失败', 'auto', '有限次随机退避重试；持续失败需检查许可证与网络'),
    'capacity': ('浏览器名额占用', 'auto', '自动等待名额释放；长时间占用需关闭其他环境的浏览器'),
    'greeting': ('首聊话术核验未通过', 'manual', '核对该岗位话术设置；只影响该岗位新开聊'),
    'login': ('登录失效', 'manual', '在执行浏览器重新登录'),
    'verification': ('平台要求验证', 'manual', '在执行浏览器完成验证'),
    'draft': ('发现未发送草稿', 'manual', '检查并处理草稿后继续'),
    'delivery_unknown': ('送达结果未确认', 'manual', '设计上不自动重发；等待回执同步或人工核对'),
    'decision_held': ('模型决策暂缓', 'auto', '退避后重试；持续失败检查模型网关'),
    'notification': ('飞书通知未送达', 'partial', '可重试错误自动重试；结果未知不重发，需人工核对'),
    'task_error': ('任务异常停止', 'manual', '查看错误记录后恢复任务'),
    'job_change': ('岗位变更生效', 'auto', '加入、撤下、移除岗位与调整权重、总额在执行器安全点生效；加入的岗位须先通过本岗位隔离验收，未生效时生成待办'),
    'browser_errand': ('浏览器代办（读取、绑定 BOSS 岗位）', 'partial', '执行器在执行时段内的安全点一轮做一条，做完回沟通页；单条失败只标记这一条并生成待办，掉登录或平台校验照常停止'),
    'page_health': ('BOSS 页面打不开', 'partial', '执行中自动刷新、重开标签页、从左侧菜单进入；因此暂停的任务在执行时段内按间隔再试（每次暂停最多 6 次、每天每任务最多自动恢复 3 次），页面确认安全后自动恢复并先只读核验原会话；有草稿、掉登录、验证或发布未验收需管理员处理'),
    'service_restart': ('服务重启', 'partial', '重启后自动核验环境并恢复原先运行的任务；核验不通过时保持暂停并写明原因'),
}
FIXABILITY = {'auto': '可自动修复', 'partial': '部分可自动修复', 'manual': '需人工处理'}

# (类别, 结果, 匹配)。匹配为事件 kind 或消息片段，按顺序取第一条命中的规则。
EVENT_RULES = [
    ('selection', 'auto_recovered', ('msg', '会话选择已恢复')),
    ('selection', 'isolated', ('msg', '已先放下')),
    ('selection', 'paused', ('re', r'恢复核验 \d+/\d+')),
    ('page_health', 'auto_recovered', ('msg', '系统已自动恢复任务')),
    ('page_health', 'retry', ('msg', '沟通页仍打不开')),
    ('page_health', 'manual', ('msg', '暂停期间检查沟通页')),
    ('page_health', 'manual', ('msg', '任务未能自动恢复')),
    ('page_health', 'manual', ('msg', '页面恢复后也无法自动恢复任务')),
    ('page_health', 'stopped', ('msg', '沟通页自动恢复已停止自动重试')),
    ('isolation', 'outreach_held', ('msg', '岗位有未核实的隔离会话')),
    ('outreach_entry', 'retry', ('msg', '主动开聊入口暂不可用')),
    ('outreach', 'job_exhausted', ('msg', '额度让给其他岗位')),
    ('page_read', 'stopped', ('msg', '连续 3 次读取超时')),
    ('page_read', 'retry', ('msg', '沟通页面读取暂未完成')),
    ('profile_changed', 'skipped', ('msg', '资料在核对时发生变化')),
    ('release_changed', 'waiting', ('msg', '发布版本已变化')),
    ('release', 'job_not_validated', ('msg', '部分岗位验收已失效')),
    ('license', 'retry', ('msg', '浏览器授权校验暂未通过')),
    ('capacity', 'waiting', ('kind', 'browser_capacity')),
    ('capacity', 'auto_recovered', ('kind', 'browser_ready')),
    ('greeting', 'outreach_held', ('msg', '仅暂停本岗位新增开聊')),
    ('login', 'manual', ('kind', 'access_required')),
    ('login', 'auto_recovered', ('kind', 'access_restored')),
    ('verification', 'manual', ('msg', '平台要求验证')),
    ('draft', 'manual', ('msg', '发现未发送的草稿')),
    ('job_change', 'applied', ('msg', '岗位变更已在安全点生效')),
    ('browser_errand', 'done', ('msg', '浏览器代办已完成')),
    ('browser_errand', 'failed', ('msg', '浏览器代办未完成')),
    ('task_error', 'stopped', ('kind', 'error')),
]


def _classify(event):
    message = str(event.get('message', ''))
    for category, outcome, (how, value) in EVENT_RULES:
        if how == 'kind' and event.get('kind') == value:
            return category, outcome
        if how == 'msg' and value in message:
            return category, outcome
        if how == 're' and re.search(value, message):
            # “恢复核验 n/3”：带“已暂停任务／任务已暂停”是暂停，否则是重试中。
            return category, 'paused' if ('已暂停任务' in message or '任务已暂停' in message) else 'retry'
    return None


def day_bounds(day: date, tz=None):
    tz = tz or report_timezone()
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _clock(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime('%H:%M')


def _minutes(seconds):
    return round(seconds / 60)


def _window_minutes(db, day, tz):
    """当天计划执行时段（分钟），取运行中的任务配置；没有配置时按全天不限。"""
    from .task_policy import DEFAULT_SCHEDULE
    schedules = []
    for task in db.list('task'):
        schedule = task.get('config', {}).get('schedule')
        if schedule:
            schedules.append(schedule)
    cfg = schedules[0] if schedules else DEFAULT_SCHEDULE
    if not cfg.get('enabled', True):
        return 24 * 60, []
    if day.weekday() not in cfg['weekdays']:
        return 0, []
    spans = []
    for window in cfg['windows']:
        sh, sm = map(int, window['start'].split(':'))
        eh, em = map(int, window['end'].split(':'))
        start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz).timestamp()
        end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz).timestamp()
        spans.append((start, end))
    return sum(_minutes(e - s) for s, e in spans), spans


def _events(db, start, end):
    """当天所有运行的事件，按时间排序。只取账号执行器与寻访相关的运行。"""
    rows = []
    for run in db.list('run'):
        if run.get('created', 0) >= end:
            continue
        if run.get('mode') not in ('boss_autopilot', 'boss_contact', 'boss_recommend') and not run.get('account_runner'):
            continue
        for event in db.trace(run['id']):
            if start <= event.get('at', 0) < end:
                rows.append({**event, 'run_id': run['id']})
    rows.sort(key=lambda e: e['at'])
    return rows


def _cause_from_history(entry):
    """状态历史里记录了暂停原因时直接采用（2026-11-23 起记录）；旧记录返回 None，按事件推断。"""
    reason = entry.get('reason') or ''
    if entry.get('actor') == 'admin':
        return 'admin'
    if reason.startswith('selection_blocked'):
        return 'selection'
    if reason.startswith('executor_error'):
        return 'task_error'
    if reason == 'service_restart' or reason.startswith('auto_resume_failed'):
        return 'service_restart'
    return None


def _pauses(db, start, end, events, now):
    """任务状态历史里当天的暂停区间，并推断原因与恢复后是否立刻通过。"""
    items = []
    for member in db.list('account_task'):
        history = sorted(member.get('history', []), key=lambda h: h.get('at', 0))
        for index, entry in enumerate(history):
            at = entry.get('at', 0)
            if entry.get('status') != 'paused' or not start <= at < end:
                continue
            resumed_entry = next((h for h in history[index + 1:] if h.get('status') == 'running'), None)
            resumed = resumed_entry['at'] if resumed_entry else None
            cause = _cause_from_history(entry)
            for event in ([] if cause else reversed(events)):
                if event['at'] > at + 5:
                    continue
                if event['at'] < at - 120:
                    break
                found = _classify(event)
                if found and found[1] in ('paused', 'stopped', 'manual', 'waiting'):
                    cause = found[0]
                    break
            quick_pass = False
            if resumed:
                for event in events:
                    if resumed <= event['at'] <= resumed + QUICK_PASS_SECONDS:
                        found = _classify(event)
                        if found and found[0] == cause and found[1] in ('auto_recovered', 'isolated'):
                            quick_pass = True
                            break
            items.append({'task_id': member['id'], 'paused_at': at, 'resumed_at': resumed,
                          'actor': entry.get('actor'), 'reason': entry.get('reason'),
                          'resumed_by': resumed_entry.get('actor') if resumed_entry else None,
                          'minutes': _minutes((resumed or min(end, now)) - at),
                          'cause': cause, 'quick_pass_after_resume': quick_pass})
    items.sort(key=lambda p: p['paused_at'])
    return items


def _held_minutes(events, category, release_markers, end, now):
    """某类“暂缓”从第一次出现到被解除（出现解除事件或当天结束）累计的分钟数。"""
    total = 0
    since = None
    for event in events:
        found = _classify(event)
        if found and found[0] == category and since is None:
            since = event['at']
        elif since is not None and any(marker in str(event.get('message', '')) for marker in release_markers):
            total += event['at'] - since
            since = None
    if since is not None:
        total += min(end, now) - since
    return _minutes(total)


def outcomes(cases, start, end):
    """按岗位统计当天结果，以及来信到我方下一条消息的响应时长。"""
    per_job = []
    latencies = []
    unanswered = 0
    for case in cases:
        row = {'job': case.get('job', {}).get('label', ''), 'first_contacts': 0, 'messages_sent': 0,
               'replies': 0, 'resumes_archived': 0, 'delivery_unknown': 0, 'opt_out': 0, 'handoff': 0}
        for person in case.get('candidates', {}).values():
            if person.get('alias_of'):
                continue
            outbox = person.get('outbox', [])
            for message in outbox:
                sent = message.get('sent_at') or message.get('observed_at')
                claimed = message.get('claimed_at') or message.get('created_at', 0)
                if message.get('status') == 'sent' and sent and start <= sent < end:
                    key = 'first_contacts' if message.get('intent') == 'first_contact' else 'messages_sent'
                    row[key] += 1
                if message.get('status') == 'delivery_unknown' and start <= claimed < end:
                    row['delivery_unknown'] += 1
            for incoming in person.get('inbox', []):
                at = incoming.get('observed_at', 0)
                if not start <= at < end:
                    continue
                row['replies'] += 1
                answers = [m.get('claimed_at') or m.get('sent_at') for m in outbox
                           if (m.get('claimed_at') or m.get('sent_at') or 0) >= at and m.get('status') in ('sent', 'delivery_unknown', 'sending')]
                if answers:
                    latencies.append(min(answers) - at)
                else:
                    unanswered += 1
        for entry in case.get('audit', []):
            if not start <= entry.get('at', 0) < end:
                continue
            kind = entry.get('kind')
            if kind == 'resume_file_archived':
                row['resumes_archived'] += 1
            elif kind == 'candidate_opt_out':
                row['opt_out'] += 1
            elif kind == 'consultant_handoff':
                row['handoff'] += 1
        per_job.append(row)
    summary = None
    if latencies:
        ordered = sorted(latencies)
        summary = {'count': len(ordered), 'median_minutes': _minutes(statistics.median(ordered)),
                   'p90_minutes': _minutes(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]),
                   'max_minutes': _minutes(ordered[-1])}
    return per_job, summary, unanswered


_outcomes = outcomes


CONVERSATION_ISSUES = {
    'unanswered_streak': '对方未回复时我方已连续主动发了 3 条及以上',
    'night_proactive': '在 9–20 点以外主动发消息（不是回复对方来信）',
    'short_gap': '对方未回复时，两条主动消息间隔不足 48 小时',
    'unread_chase': '上一条对方还没读，又追发了一条',
    'after_decline': '对方表示拒绝或暂不考虑之后，我方仍主动发消息',
    'off_topic': '提到联系方式，但对方从未问过联系方式',
}


def _conversation_quality(cases, start, end, tz):
    """逐个候选人检查当天我方主动消息是否让人不适。只看当天有我方消息的对话。"""
    from .autonomy import intent_labels
    issues = []
    checked = 0
    for case in cases:
        for person in case.get('candidates', {}).values():
            if person.get('alias_of'):
                continue
            outs = sorted([(m.get('sent_at') or m.get('claimed_at'), m) for m in person.get('outbox', [])
                           if m.get('status') in ('sent', 'delivery_unknown', 'sending') and (m.get('sent_at') or m.get('claimed_at'))],
                          key=lambda x: x[0])
            if not any(start <= at < end for at, _ in outs):
                continue
            checked += 1
            ins = sorted([(m.get('observed_at') or 0, m) for m in person.get('inbox', [])
                          if not m.get('platform_control')], key=lambda x: x[0])
            timeline = sorted([(at, 'out', m) for at, m in outs] + [(at, 'in', m) for at, m in ins], key=lambda x: x[0])
            found = {}
            streak = 0
            previous = None
            declined = False
            asked_contact = False
            for at, direction, message in timeline:
                text = message.get('text') or ''
                if direction == 'in':
                    streak = 0
                    labels = set(intent_labels(text))
                    declined = bool(labels & {'decline', 'later'})
                    asked_contact = asked_contact or any(word in text for word in ('联系方式', '电话', '微信', '手机'))
                    previous = (at, 'in', message)
                    continue
                streak += 1
                today = start <= at < end
                proactive = not previous or previous[1] == 'out' or at - previous[0] > 3600
                if today:
                    if streak >= 3:
                        found['unanswered_streak'] = streak
                    hour = datetime.fromtimestamp(at, tz).hour
                    if proactive and not 9 <= hour < 20:
                        found.setdefault('night_proactive', _clock(at, tz))
                    if previous and previous[1] == 'out':
                        if at - previous[0] < 48 * 3600:
                            found.setdefault('short_gap', f"{round((at - previous[0]) / 3600)} 小时")
                        if (previous[2].get('receipt') or {}).get('delivery_status') != 'read':
                            found.setdefault('unread_chase', _clock(at, tz))
                    if declined and proactive:
                        found.setdefault('after_decline', _clock(at, tz))
                    if '联系方式' in text and not asked_contact:
                        found.setdefault('off_topic', _clock(at, tz))
                previous = (at, 'out', message)
            if found:
                issues.append({'job': case.get('job', {}).get('label', ''), 'candidate_id': person.get('id'),
                               'name': (person.get('profile') or {}).get('display_name') or person.get('id'),
                               'issues': found})
    counts = {kind: sum(kind in item['issues'] for item in issues) for kind in CONVERSATION_ISSUES}
    return {'checked': checked, 'flagged': len(issues), 'counts': counts, 'issues': issues}


def _audit_anomalies(cases, start, end):
    counts = {'delivery_unknown': 0, 'decision_held': 0, 'isolation_verified': 0, 'isolated': 0, 'profile_changed': 0}
    for case in cases:
        for entry in case.get('audit', []):
            if not start <= entry.get('at', 0) < end:
                continue
            kind = entry.get('kind')
            if kind == 'delivery_unknown':
                counts['delivery_unknown'] += 1
            elif kind == 'harness_decision_held':
                counts['decision_held'] += 1
            elif kind == 'deferred_chat_verified':
                counts['isolation_verified'] += 1
            elif kind == 'failed_chat_deferred':
                counts['isolated'] += 1
    return counts


def _notifications(db, start, end):
    failed = [n for n in db.list('task_notification')
              if start <= n.get('updated_at', n.get('created_at', 0)) < end and n.get('status') in ('failed', 'unknown')]
    return {'failed': sum(n['status'] == 'failed' for n in failed), 'unknown': sum(n['status'] == 'unknown' for n in failed),
            'kinds': sorted({n.get('kind', '') for n in failed})}


def _todos(db, start, end):
    created = {}
    resolved = 0
    open_at_end = 0
    for todo in db.list('attention'):
        at = todo.get('created_at', 0)
        if start <= at < end:
            created[todo.get('kind', 'other')] = created.get(todo.get('kind', 'other'), 0) + 1
        done = todo.get('resolved_at') or (todo.get('updated_at') if todo.get('status') == 'resolved' else None)
        if done and start <= done < end:
            resolved += 1
        if at < end and (todo.get('status') != 'resolved' or (done and done >= end)):
            open_at_end += 1
    return {'created': created, 'resolved': resolved, 'open_at_end': open_at_end}


def _idle_gaps(events, spans, pauses, now):
    """计划时段内超过 IDLE_GAP_MINUTES 没有任何执行记录、且不是暂停或休息造成的空档。"""
    gaps = []
    for span_start, span_end in spans:
        if span_start >= now:
            continue
        points = [span_start] + [e['at'] for e in events if span_start <= e['at'] < span_end] + [min(span_end, now)]
        for previous, current in zip(points, points[1:]):
            if current - previous < IDLE_GAP_MINUTES * 60:
                continue
            paused = any(p['paused_at'] <= previous and (p['resumed_at'] or float('inf')) >= current for p in pauses)
            if paused:
                continue
            last = next((e for e in reversed(events) if e['at'] <= previous), None)
            context = _classify(last) if last else None
            gaps.append({'from': previous, 'to': current, 'minutes': _minutes(current - previous),
                         'after': CATEGORIES[context[0]][0] if context else (str(last.get('message', ''))[:60] if last else '时段开始')})
    return gaps


def _anomaly_table(events, audit_counts, notifications, pauses, end):
    table = {}

    def bump(category, outcome, at=None, amount=1):
        row = table.setdefault(category, {'count': 0, 'outcomes': {}, 'first_at': None, 'last_at': None})
        row['count'] += amount
        row['outcomes'][outcome] = row['outcomes'].get(outcome, 0) + amount
        if at:
            row['first_at'] = at if row['first_at'] is None else min(row['first_at'], at)
            row['last_at'] = at if row['last_at'] is None else max(row['last_at'], at)

    for event in events:
        found = _classify(event)
        if found:
            bump(found[0], found[1], event['at'])
    for pause in pauses:
        # 只由状态历史记录的暂停（如服务重启）也要出现在异常表里。
        if pause['cause'] in CATEGORIES and pause['cause'] not in table:
            bump(pause['cause'], 'paused', pause['paused_at'])
    if audit_counts['delivery_unknown']:
        bump('delivery_unknown', 'kept_unknown', amount=audit_counts['delivery_unknown'])
    if audit_counts['decision_held']:
        bump('decision_held', 'retry', amount=audit_counts['decision_held'])
    if audit_counts['isolation_verified']:
        bump('isolation', 'auto_recovered', amount=audit_counts['isolation_verified'])
    if notifications['failed'] or notifications['unknown']:
        bump('notification', 'failed', amount=notifications['failed'] + notifications['unknown'])
    rows = []
    for category, row in table.items():
        name, fixability, handling = CATEGORIES[category]
        caused = [p for p in pauses if p['cause'] == category]  # 'admin' 不属于任何异常类别
        rows.append({'category': category, 'name': name, 'fixability': fixability, 'handling': handling,
                     'count': row['count'], 'outcomes': row['outcomes'], 'first_at': row['first_at'], 'last_at': row['last_at'],
                     'pauses': len(caused), 'paused_minutes': sum(p['minutes'] for p in caused),
                     'quick_pass_after_resume': sum(p['quick_pass_after_resume'] for p in caused)})
    order = {'manual': 0, 'partial': 1, 'auto': 2}
    rows.sort(key=lambda r: (-r['pauses'], order[r['fixability']], -r['count']))
    return rows


def _batches(events):
    started = submitted = skipped = 0
    for event in events:
        message = str(event.get('message', ''))
        if message.startswith('开始主动开聊批次'):
            started += 1
        found = re.search(r'主动开聊批次结束，已提交 (\d+) 次', message)
        if found:
            submitted += int(found.group(1))
        if '暂不联系' in message:
            skipped += 1
    return {'started': started, 'submitted': submitted, 'skipped_after_review': skipped}


def _suggestions(report):
    tips = []
    for row in report['anomalies']:
        if row['quick_pass_after_resume']:
            tips.append(f"{row['name']}：{row['quick_pass_after_resume']} 次暂停在手动恢复后立刻通过，说明这类情况多数不需要人，可考虑纳入自动恢复。")
        if row['category'] == 'release_changed':
            tips.append('出现发布版本不一致：确认是否有运行中直接发布的情况；岗位问答以外的发布请在暂停后进行。')
    held = report['operations']['outreach_held_minutes']
    if held.get('isolation', 0) >= 60:
        tips.append(f"隔离会话让新开聊暂缓了约 {held['isolation']} 分钟：请在工作台核对隔离会话，确认无误可允许开聊例外。")
    if held.get('greeting', 0) >= 60:
        tips.append(f"首聊话术核验未通过，新开聊暂缓约 {held['greeting']} 分钟：请核对岗位话术设置。")
    gaps = report['operations']['idle_gaps']
    if gaps:
        longest = max(gaps, key=lambda g: g['minutes'])
        tips.append(f"执行时段内有 {len(gaps)} 段超过 {IDLE_GAP_MINUTES} 分钟没有任何执行记录，最长 {longest['minutes']} 分钟（{longest['from_clock']}–{longest['to_clock']}，之前是“{longest['after']}”），心跳正常但没有进展时需排查。")
    latency = report['outcomes']['reply_latency']
    if latency and latency['p90_minutes'] >= 60:
        tips.append(f"来信响应偏慢：90% 的来信在 {latency['p90_minutes']} 分钟内处理，最长 {latency['max_minutes']} 分钟；可检查收件是否被开聊批次或异常长时间占住。")
    if report['outcomes']['unanswered_replies']:
        tips.append(f"当天有 {report['outcomes']['unanswered_replies']} 条来信到日终仍未处理，第二天优先跟进。")
    totals = report['outcomes']['totals']
    if totals['delivery_unknown']:
        tips.append(f"有 {totals['delivery_unknown']} 条消息送达未确认：不会自动重发，请在沟通记录中核对。")
    if report['window_minutes'] and not totals['first_contacts'] and not any(held.values()) and report['operations']['batches']['started'] == 0:
        tips.append('执行时段内没有任何开聊批次，也没有记录到暂缓原因：请确认岗位是否开启新开聊、当天额度设置是否为 0。')
    conversation = report.get('conversation') or {}
    if conversation.get('flagged'):
        worst = ', '.join(f"{CONVERSATION_ISSUES[k]} {v} 人" for k, v in conversation['counts'].items() if v)
        tips.append(f"对话体验：{conversation['flagged']} 位候选人的对话有不适问题（{worst}）。请在工作台逐个核对，必要时人工接续或停止跟进。")
    if not report['event_count'] and report['window_minutes']:
        tips.append('执行时段内没有任何执行记录：确认服务与任务当天是否在运行。')
    return tips


def build(db, cases, day: date, *, tz=None, now=None):
    tz = tz or report_timezone()
    now = time.time() if now is None else now
    start, end = day_bounds(day, tz)
    events = _events(db, start, end)
    window_minutes, spans = _window_minutes(db, day, tz)
    pauses = _pauses(db, start, end, events, now)
    per_job, latency, unanswered = outcomes(cases, start, end)
    audit_counts = _audit_anomalies(cases, start, end)
    notifications = _notifications(db, start, end)
    anomalies = _anomaly_table(events, audit_counts, notifications, pauses, end)
    totals = {key: sum(row[key] for row in per_job) for key in
              ('first_contacts', 'messages_sent', 'replies', 'resumes_archived', 'delivery_unknown', 'opt_out', 'handoff')}
    gaps = _idle_gaps(events, spans, pauses, now)
    for gap in gaps:
        gap.update(from_clock=_clock(gap['from'], tz), to_clock=_clock(gap['to'], tz))
    active_minutes = len({int(e['at'] // 60) for e in events if any(s <= e['at'] < t for s, t in spans)})
    report = {
        'id': day.isoformat(), 'version': VERSION, 'day': day.isoformat(), 'timezone': str(tz),
        'generated_at': now, 'complete': now >= end,
        'window_minutes': window_minutes, 'event_count': len(events),
        'outcomes': {'per_job': per_job, 'totals': totals, 'reply_latency': latency, 'unanswered_replies': unanswered},
        'anomalies': anomalies,
        'pauses': [{**p, 'paused_clock': _clock(p['paused_at'], tz),
                    'resumed_clock': _clock(p['resumed_at'], tz) if p['resumed_at'] else None,
                    'cause_name': ('管理员暂停' + (f"（{p['reason']}）" if p['reason'] not in (None, 'manual') else '')) if p['cause'] == 'admin'
                    else CATEGORIES[p['cause']][0] + (f"（{p['reason'].split(':', 1)[1]}）" if p['reason'] and ':' in p['reason'] else '') if p['cause']
                    else '手动暂停或原因未记录'} for p in pauses],
        'todos': _todos(db, start, end),
        'notifications': notifications,
        'operations': {
            'active_minutes_in_window': active_minutes,
            'paused_minutes': sum(p['minutes'] for p in pauses),
            'outreach_held_minutes': {
                'isolation': _held_minutes(events, 'isolation', ('开始主动开聊批次', '隔离会话已通过只读复核'), end, now),
                'greeting': _held_minutes(events, 'greeting', ('开始主动开聊批次',), end, now),
                'release_changed': _held_minutes(events, 'release_changed', ('开始主动开聊批次', '本轮已检查'), end, now),
            },
            'idle_gaps': gaps,
            'batches': _batches(events),
        },
    }
    report['conversation'] = _conversation_quality(cases, start, end, tz)
    report['suggestions'] = _suggestions(report)
    return report


def render_markdown(report):
    lines = [f"# 执行日报 {report['day']}", '']
    if not report['complete']:
        lines += ['> 当天尚未结束，以下为截至生成时的数据。', '']
    totals = report['outcomes']['totals']
    manual = [r for r in report['anomalies'] if r['fixability'] == 'manual']
    paused = report['operations']['paused_minutes']
    lines += ['## 概览', '',
              f"- 主动开聊 {totals['first_contacts']} 次，其他消息 {totals['messages_sent']} 条，收到来信 {totals['replies']} 条，归档简历 {totals['resumes_archived']} 份",
              f"- 计划执行 {report['window_minutes']} 分钟；任务暂停累计 {paused} 分钟（{len(report['pauses'])} 次）；超过 {IDLE_GAP_MINUTES} 分钟无执行记录的空档累计 {sum(g['minutes'] for g in report['operations']['idle_gaps'])} 分钟",
              f"- 异常 {len(report['anomalies'])} 类，其中需人工处理 {len(manual)} 类；待办新增 {sum(report['todos']['created'].values())} 条，日终未处理 {report['todos']['open_at_end']} 条",
              '']
    lines += ['## 异常与自动修复', '']
    if report['anomalies']:
        lines += ['| 异常 | 次数 | 结果 | 能否自动修复 | 造成暂停 | 处理方式 |', '| --- | --- | --- | --- | --- | --- |']
        names = {'auto_recovered': '自动恢复', 'isolated': '只放下单条', 'paused': '暂停任务', 'retry': '重试中', 'stopped': '停止',
                 'outreach_held': '暂缓开聊', 'skipped': '放下该候选人', 'waiting': '等待', 'manual': '待人工', 'kept_unknown': '保留未知', 'failed': '失败', 'job_not_validated': '岗位停发'}
        for row in report['anomalies']:
            outcomes = '、'.join(f"{names.get(k, k)} {v}" for k, v in row['outcomes'].items())
            pause = f"{row['pauses']} 次 / {row['paused_minutes']} 分钟" if row['pauses'] else '无'
            lines.append(f"| {row['name']} | {row['count']} | {outcomes} | {FIXABILITY[row['fixability']]} | {pause} | {row['handling']} |")
    else:
        lines.append('当天没有记录到异常。')
    lines.append('')
    if report['pauses']:
        lines += ['### 暂停记录', '']
        for p in report['pauses']:
            resumed = p['resumed_clock'] or '当天未恢复'
            extra = '；恢复后立刻通过' if p['quick_pass_after_resume'] else ''
            lines.append(f"- {p['paused_clock']} → {resumed}（{p['minutes']} 分钟）：{p['cause_name']}{extra}")
        lines.append('')
    ops = report['operations']
    lines += ['## 操作分析', '',
              f"- 开聊批次 {ops['batches']['started']} 次，提交 {ops['batches']['submitted']} 次，审阅后暂不联系 {ops['batches']['skipped_after_review']} 位"]
    held = {k: v for k, v in ops['outreach_held_minutes'].items() if v}
    if held:
        labels = {'isolation': '隔离会话', 'greeting': '话术核验', 'release_changed': '版本不一致'}
        lines.append('- 新开聊暂缓：' + '，'.join(f"{labels[k]} {v} 分钟" for k, v in held.items()))
    latency = report['outcomes']['reply_latency']
    if latency:
        lines.append(f"- 来信响应：{latency['count']} 条，中位 {latency['median_minutes']} 分钟，90% 在 {latency['p90_minutes']} 分钟内，最长 {latency['max_minutes']} 分钟")
    if report['outcomes']['unanswered_replies']:
        lines.append(f"- 日终仍未处理的来信 {report['outcomes']['unanswered_replies']} 条")
    for gap in ops['idle_gaps']:
        lines.append(f"- 空档 {gap['from_clock']}–{gap['to_clock']}（{gap['minutes']} 分钟），之前：{gap['after']}")
    lines.append('')
    if report['outcomes']['per_job']:
        lines += ['### 分岗位', '', '| 岗位 | 开聊 | 其他消息 | 来信 | 简历 | 送达未确认 |', '| --- | --- | --- | --- | --- | --- |']
        for row in report['outcomes']['per_job']:
            lines.append(f"| {row['job']} | {row['first_contacts']} | {row['messages_sent']} | {row['replies']} | {row['resumes_archived']} | {row['delivery_unknown']} |")
        lines.append('')
    conversation = report.get('conversation') or {}
    lines += ['## 对话体验', '']
    if not conversation.get('checked'):
        lines.append('当天没有我方发出的消息。')
    elif not conversation.get('flagged'):
        lines.append(f"检查了当天有我方消息的 {conversation['checked']} 段对话，没有发现让人不适的情况。")
    else:
        lines.append(f"检查了当天有我方消息的 {conversation['checked']} 段对话，{conversation['flagged']} 段有问题：")
        lines.append('')
        lines += ['| 候选人 | 岗位 | 问题 |', '| --- | --- | --- |']
        for item in conversation['issues'][:30]:
            detail = '；'.join(f"{CONVERSATION_ISSUES[k]}（{v}）" for k, v in item['issues'].items())
            lines.append(f"| {item['name']} | {item['job']} | {detail} |")
    lines.append('')
    lines += ['## 建议', '']
    lines += [f'- {tip}' for tip in report['suggestions']] or ['- 当天没有需要调整的地方。']
    return '\n'.join(lines) + '\n'


def _write_file(root, report):
    path = Path(root) / 'reports' / f"{report['id']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.md.tmp')
    tmp.write_text(report['markdown'], encoding='utf-8')
    tmp.replace(path)


def generate(manager, day: date, *, now=None):
    report = build(manager.db, manager.cases.list(), day, now=now)
    report['markdown'] = render_markdown(report)
    manager.db.put(KIND, report['id'], report)
    _write_file(manager.root, report)
    return report


def _first_activity_day(db, tz):
    created = [run.get('created') for run in db.list('run') if run.get('created')]
    return datetime.fromtimestamp(min(created), tz).date() if created else None


def ensure(manager, *, now=None):
    """生成所有已结束、但还没有完整报告的日期（含停机错过的），最多回补 BACKFILL_DAYS 天。"""
    tz = report_timezone()
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, tz)
    latest = (local - READY_AFTER).date() - timedelta(days=1)
    first = _first_activity_day(manager.db, tz)
    if first is None or latest < first:
        return []
    existing = {r['id']: r for r in manager.db.list(KIND)}
    made = []
    day = max(first, latest - timedelta(days=BACKFILL_DAYS - 1))
    while day <= latest:
        prior = existing.get(day.isoformat())
        if not prior or not prior.get('complete') or prior.get('version') != VERSION:
            made.append(generate(manager, day, now=now)['id'])
        elif not (Path(manager.root) / 'reports' / f'{prior["id"]}.md').exists():
            # 报告目录换成本机挂载后，已有报告补写一份文件，不重算。
            _write_file(manager.root, prior)
        day += timedelta(days=1)
    return made


def listing(db):
    rows = []
    for report in db.list(KIND):
        totals = report['outcomes']['totals']
        rows.append({'day': report['day'], 'complete': report['complete'], 'generated_at': report['generated_at'],
                     'first_contacts': totals['first_contacts'], 'replies': totals['replies'],
                     'resumes_archived': totals['resumes_archived'],
                     'manual_anomalies': sum(r['fixability'] == 'manual' for r in report['anomalies']),
                     'pauses': len(report['pauses'])})
    return sorted(rows, key=lambda r: r['day'], reverse=True)
