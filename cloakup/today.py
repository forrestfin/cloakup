"""工作台“今天”首页：只读计算任务状态（人话）、各岗位今日数字、未解决待办与最近动态。

与日报共有的数字直接用 `daily_report.outcomes`，首聊额度用 `job_quota.task_quota`；不写任何记录，不读日志文件。
动态里不写候选人姓名，一律称“一位候选人”。
"""
from __future__ import annotations

import copy
import time
from datetime import datetime

from . import daily_report, job_members, job_quota, screening
from .attention import order as todo_order
from .autonomy import day_key
from .scenarios import is_sourcing
from .sourcing import TERMINAL
from .task_policy import DEFAULT_SCHEDULE, window_status

LABELS={'running':'运行中','outside_window':'等待执行时段','resting':'休息中','paused_admin':'暂停中','paused_system':'暂停中','blocked':'受阻'}
NUMBERS=('quota_used','quota_share','first_contacts_sent','incoming','replies_sent','resumes','greetings','greetings_unanswered','waiting_now','profiles_reviewed','screened_out')
ANSWERING=('sending','sent','delivery_unknown')  # 已发出或可能已发出的我方消息，算作“已回”
AUDIT_TEXT={'resume_file_archived':'收到一份简历','candidate_opt_out':'候选人表示不考虑','consultant_handoff':'转顾问接续'}
TODO_LIMIT,ACTIVITY_LIMIT,DETAIL_LIMIT=5,10,120
PAUSE_KINDS=('browser_health','execution','execution_recovery','login','verification','task_not_validated')  # 会暂停任务的待办；系统暂停的原因只取这几类
ACCOUNT_KINDS=('login','verification')  # 按执行器 run_id 记录、没有 task_id；账号只有一个执行浏览器，对每个任务都算


def _clock(ts,tz):return datetime.fromtimestamp(ts,tz).strftime('%H:%M')


def _people(case):return [p for p in case.get('candidates',{}).values() if not p.get('alias_of')]


def _greetings(case,start,end):
    """今天新出现的、候选人先开口的会话数，以及其中我方还没出过声的。"""
    total=silent=0
    for p in _people(case):
        history=p.get('history') or [];inbox=p.get('inbox') or []
        if not history or history[0].get('direction')!='in' or not inbox:continue
        if not start<=min(m.get('observed_at',0) for m in inbox)<end:continue
        total+=1
        if not any(h.get('direction')=='out' for h in history) and not any(m.get('status') in ANSWERING for m in p.get('outbox',[])):silent+=1
    return total,silent


def _waiting(case):
    """此刻待回的会话数：最后一条是候选人来信、其后我方没有发出中／已发出／回执不明的消息、且不在终止状态。"""
    count=0
    for p in _people(case):
        inbox=p.get('inbox') or []
        if p.get('status') in TERMINAL or not inbox:continue
        if (p.get('history') or [{}])[-1].get('direction')=='out':continue
        last=max(m.get('observed_at',0) for m in inbox)
        if any(m.get('status') in ANSWERING and (m.get('claimed_at') or m.get('sent_at') or m.get('created_at') or 0)>=last for m in p.get('outbox',[])):continue
        count+=1
    return count


def _status(member,jobs,availability,todos,tz):
    """成员状态 + 各岗位 availability → 五种人话；不显示内部代号。"""
    if member.get('status')!='running':
        last=(member.get('history') or [{}])[-1]
        if last.get('actor')=='admin':return 'paused_admin','管理员 '+_clock(last['at'],tz)+' 手动暂停' if last.get('at') else '管理员手动暂停',None
        todo=next((t for t in todos if t.get('kind') in PAUSE_KINDS and (t.get('task_id')==member['id'] or t.get('kind') in ACCOUNT_KINDS)),None)
        if not todo:return 'paused_system','系统暂停',None
        detail=todo.get('title','')+('：'+todo['detail'] if todo.get('detail') else '')
        if len(detail)>DETAIL_LIMIT:detail=detail[:DETAIL_LIMIT-1]+'…'
        retry=todo.get('next_check_at')
        return 'paused_system',detail+('，'+_clock(retry,tz)+' 自动再试' if retry else ''),retry or None
    states=[availability(cid) or {} for cid in jobs]
    hard=next((s for s in states if s.get('state') in ('release_changed','planning')),None)
    if hard:return 'blocked',hard.get('reason',''),None
    if states and all(s.get('state')=='job_not_validated' for s in states):return 'blocked',states[0].get('reason',''),None
    first=next((s for s in states if s.get('state')!='job_not_validated'),{})
    state,next_at=first.get('state'),first.get('next_at')
    if state=='outside_window':return 'outside_window',('下一段 '+_clock(next_at,tz)+' 开始' if next_at else ''),next_at
    if state=='resting':return 'resting',(_clock(next_at,tz)+' 继续' if next_at else ''),next_at
    return 'running','',None


def _anonymous(text,todo,by_id):
    """首页待办里把关联候选人的显示名换成“一位候选人”；待办中心存的原文不动。"""
    person=(by_id.get(todo.get('case_id')) or {}).get('candidates',{}).get(todo.get('candidate_id')) or {}
    name=(person.get('profile') or {}).get('display_name')
    return text.replace(name,'一位候选人') if name else text


def _member_activity(member,label,start,end):
    items=[];history=member.get('history') or []
    for i,h in enumerate(history):
        if not start<=h.get('at',0)<end:continue
        if h.get('status')=='paused':text='管理员暂停任务' if h.get('actor')=='admin' else '系统暂停任务'
        elif h.get('status')=='running':text='任务恢复运行' if i else '任务开始运行'
        else:continue
        items.append({'at':h['at'],'job_label':label,'text':text})
    return items


def build(db,store,cases,members,availability,now=None)->dict:
    now=time.time() if now is None else now;tz=daily_report.report_timezone()
    day=datetime.fromtimestamp(now,tz).date();start,end=daily_report.day_bounds(day,tz);quota_day=day_key(now)
    by_id={c['id']:c for c in cases};by_member={m['id']:m for m in members}
    all_todos=db.list('attention');open_todos=sorted((t for t in all_todos if t.get('status')!='resolved'),key=todo_order)
    # 额度口径与 job_quota.usage_by_case、autopilot.daily_attempts 相同，但只用调用方给的 cases 算一次，不再让 task_quota 重新加载全部岗位
    usage={c['id']:sum(job_quota.attempted_on(m,quota_day,'first_contact') for p in c.get('candidates',{}).values() for m in p.get('outbox',[])) for c in cases}
    account_used=sum(usage.values())
    tasks=[];activity=[]
    for task in db.list('task'):
        cfg=copy.deepcopy(task.get('config') or {})
        member=by_member.get(task['id'])
        if not is_sourcing(cfg.get('engine')) or not member:continue
        try:job_ids=[m['case_id'] for m in job_members.normalize(cfg)]
        except ValueError:job_ids=list(member.get('case_ids') or [])
        quota=job_quota.task_quota(store,task,quota_day,usage=usage,account_used=account_used)
        jobs=[]
        for cid in job_ids:
            case=by_id.get(cid)
            if not case:continue
            row=daily_report.outcomes([case],start,end)[0][0];greet,silent=_greetings(case,start,end);screened=screening.stats(case,start)
            jobs.append({'case_id':cid,'label':case.get('job',{}).get('label',''),'quota_used':quota['usage'].get(cid,0),'quota_share':quota['shares'].get(cid,0),
                'first_contacts_sent':row['first_contacts'],'incoming':row['replies'],'replies_sent':row['messages_sent'],'resumes':row['resumes_archived'],
                'greetings':greet,'greetings_unanswered':silent,'waiting_now':_waiting(case),
                'profiles_reviewed':screened['reviewed'],'screened_out':screened['rejected'],'must_haves':len((case.get('job_sourcing') or {}).get('must_haves') or [])})
            for p in _people(case):
                for m in p.get('outbox',[]):
                    if m.get('status')=='sent' and m.get('sent_at') and start<=m['sent_at']<end:
                        activity.append({'at':m['sent_at'],'job_label':jobs[-1]['label'],'text':'向一位候选人发出首聊' if m.get('intent')=='first_contact' else '追聊了一位候选人' if (m.get('decision') or {}).get('nudge') else '回复了一位候选人'})
            activity+=[{'at':e['at'],'job_label':jobs[-1]['label'],'text':AUDIT_TEXT[e.get('kind')]} for e in case.get('audit',[]) if e.get('kind') in AUDIT_TEXT and start<=e.get('at',0)<end]
        name=cfg.get('name','')
        activity+=_member_activity(member,name,start,end)
        state,detail,next_at=_status(member,job_ids,availability,open_todos,tz)
        schedule=cfg.get('schedule') or DEFAULT_SCHEDULE
        tasks.append({'id':task['id'],'name':name,'state':state,'label':LABELS[state],'detail':detail,'next_at':next_at,
            'windows':'、'.join(w['start']+'–'+w['end'] for w in schedule.get('windows',[])),'in_window':bool(window_status(schedule,now)['allowed']),
            'jobs':jobs,'totals':{k:sum(j[k] for j in jobs) for k in NUMBERS}})
    if tasks:
        for todo in all_todos:
            if todo.get('kind')!='browser_health':continue
            if start<=todo.get('created_at',0)<end:activity.append({'at':todo['created_at'],'job_label':'','text':'BOSS 页面打不开，系统正在自动重试'})
            done=todo.get('resolved_at') or (todo.get('updated_at') if todo.get('status')=='resolved' else None)
            if done and start<=done<end:activity.append({'at':done,'job_label':'','text':'BOSS 页面已恢复'})
    activity.sort(key=lambda a:-a['at'])
    return {'day':day.isoformat(),'tasks':tasks,
        'todos':[{'id':t['id'],'title':_anonymous(t.get('title',''),t,by_id),'job_label':t.get('job_label',''),'created_at':t.get('created_at'),'detail':_anonymous(t.get('detail',''),t,by_id)} for t in open_todos[:TODO_LIMIT]],
        'activity':activity[:ACTIVITY_LIMIT]}
