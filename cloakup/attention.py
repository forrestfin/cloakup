"""Persistent operator inbox; reminders never authorize candidate sends."""
import copy,hashlib,time,re
from .autonomy import intent_labels,day_key
from .faq import question_topic
from .answering import answer_question

# 自动恢复失败原因（任务状态历史里的 auto_resume_failed:<code>）→ 给管理员的处理说明。
AUTO_RESUME_FAILURES={
    'release_changed':'执行代码或模型更新后，已发布版本的验收签名不再匹配，按发布规则不能直接启动。请在 Agent 工作空间重新建立草案、完成隔离验收并发布，然后恢复任务。',
    'release_not_validated':'该任务的发布版本尚未通过完整隔离验收。请在 Agent 工作空间验证并发布，然后恢复任务。',
    'release_diverged':'生效版本除常见问题外与已验收版本不一致。请重新建立草案、验证并发布，然后恢复任务。',
    'environment_check_failed':'服务重启后的环境检查未通过。请在执行环境 → 环境检查查看失败项，处理后恢复任务。',
}

def answered(person,reply_id):
    """这条来信已有实质回答送达（以回执为准）。"""
    from .decision_runtime import answer_sent
    return answer_sent(person,reply_id,statuses=('sent',))

# 让管理员在执行浏览器里点击、输入时，说明怎样拿到控制权（noVNC 默认只读）。
BROWSER_CONTROL='打开执行浏览器 http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale&view_only=0 操作（如果画面只能看不能点，在 noVNC 左侧设置里关掉 View Only）'

# 会话被隔离超过这么久仍打不开，才提醒管理员（之前一直由系统自动重试）。
ISOLATED_TODO_SECONDS = 24 * 3600
# 岗位变更排队超过这么久仍未到执行器安全点才提醒；同一岗位当天推荐页入口失败达到这么多次才提醒。
JOB_CHANGE_STUCK_SECONDS = 30 * 60
OUTREACH_BLOCKED_DEFERRALS = 3
SCREENING_UNAVAILABLE_FAILURES = 3

def order(t):
    """待办排序：未解决在前，登录／验证等优先，同类按创建时间新到旧（待办中心与首页共用）。"""
    return (t['status']=='resolved',{'login':0,'verification':0,'delivery':0,'capacity':1,'execution':1,'execution_recovery':1,'question':2,'handoff':3}.get(t['kind'],4),-t['created_at'])

def _own_draft_note(db):
    """11-27 10:43：输入框里是系统自己点发送前中止、又没删掉的文字时，说清是谁的会话、可以直接删。24 小时内有效。"""
    try:draft=db.get('system_draft','boss')
    except FileNotFoundError:return ''
    if time.time()-(draft.get('at') or 0)>24*3600:return ''
    text=(draft.get('text') or '')[:200]
    # 只凭时间判断不了框里现在是谁的字：写出原文，让管理员比对；不是这段就按人工草稿处理。
    return ('系统给'+(draft.get('receiver') or '一位候选人')+'（'+(draft.get('job_label') or '岗位')+'）准备过一段回复，点发送前中止、删除失败：「'+text+'」。'
            '如果输入框里正是这段，那是系统自己没发出的文字，没有发给候选人，确认后删除即可继续，下一轮系统会按最新对话重新回复；'
            '如果不是这段文字，就是人工草稿，请按下面的说明处理。')


class AttentionStore:
    def __init__(self,db):self.db=db
    def reconcile(self,runs,cases):
        desired={}
        existing={t['id']:t for t in self.db.list('attention')}
        planning=self.db.list('planning_request')

        def add(key,kind,title,detail,**refs):
            ident=hashlib.sha256(key.encode()).hexdigest()[:32]
            desired[ident]={'id':ident,'kind':kind,'title':title,'detail':detail,**refs}
        # Only persist bounded recovery context. Internal conversation IDs and
        # locator diagnostics must not leak into operator or Feishu reminders.
        case_labels={c['id']:c.get('job',{}).get('label','') for c in cases}
        members=self.db.list('account_task')
        recovery_states={}
        for run in runs:
            for case_id,state in (run.get('selection_recovery') or {}).items():
                if not isinstance(state,dict):continue
                explicit=state.get('task_id') or run.get('task_id')
                owners=[explicit] if explicit else [m['id'] for m in members if case_id in m.get('case_ids',[]) and m.get('status') not in ('stopped','completed')]
                for owner in owners:
                    scope=(owner,case_id)
                    stamp=(state.get('updated_at') or run.get('created',0),run.get('created',0))
                    if scope not in recovery_states or stamp>recovery_states[scope][0]:
                        recovery_states[scope]=(stamp,run,state)
        for (task_id,case_id),(_,run,state) in recovery_states.items():
            episode=state.get('incident_id') or run['id']
            key='execution_recovery:'+task_id+':'+case_id+':'+str(episode)
            ident=hashlib.sha256(key.encode()).hexdigest()[:32]
            prior=existing.get(ident)
            still_open=bool(prior and prior['status']!='resolved')
            blocked=state.get('status')=='blocked'
            # 点选、超时、详情没画出来，执行器自己重试，不发给管理员。
            # 只有登录、验证码、未发送草稿、沟通页缺失才需要人处理。
            from .selection_recovery import ISOLATABLE_REASONS
            diagnostics=state.get('diagnostics') if isinstance(state.get('diagnostics'),dict) else {}
            browser_reasons=set(ISOLATABLE_REASONS)|{'timeout','target_unverified','page_changed','page_ambiguous','editor_unavailable','selected_target_mismatch','message_list_unavailable','outside_viewport','moving_or_obscured','row_not_unique','list_identity_mismatch'}
            if diagnostics.get('reason') in browser_reasons or state.get('status')=='deferred':
                continue
            if not (blocked or state.get('status')=='retrying' and still_open):continue
            member=next((m for m in members if m['id']==task_id),None)
            resumed=bool(member and member['status']=='running')
            detail=('任务已重新启用，旧异常仍待核验；进入允许的执行时段后先只读检查目标会话，成功后自动关闭待办。' if resumed else '自动恢复未能安全完成，已暂停关联任务。请打开任务执行浏览器，接管检查页面和会话列表，再恢复任务核验。' if blocked else '任务正在重新核验会话选择；核验成功后自动关闭此待办。')
            if blocked and not resumed and diagnostics.get('reason')=='draft_present':detail=_own_draft_note(self.db)+detail
            def count(value,default):return max(0,min(value,100)) if type(value) is int else default
            add(key,'execution_recovery','会话选择恢复需要管理员处理',detail,
                task_id=task_id,case_id=case_id,run_id=run['id'],job_label=case_labels.get(case_id,''),
                incident_id=episode,recovery_status=state.get('status'),
                recovery_attempts=count(state.get('attempts'),0),recovery_max_attempts=count(state.get('max_attempts'),3))
        # Losing a process snapshot is not evidence that a blocked selector was
        # repaired. Keep an existing incident until its recovery state clears.
        for ident,todo in existing.items():
            if todo.get('kind')=='execution_recovery' and todo['status']!='resolved' and (todo.get('task_id'),todo.get('case_id')) not in recovery_states:
                desired[ident]={k:v for k,v in todo.items() if k not in ('status','created_at','updated_at','history','note','auto_resolved')}
        # 服务重启后自动恢复没成功：任务保持暂停，必须有人处理，否则只在状态历史里留一行原因，没人看得到。
        # 任务恢复运行或被管理员改成其他状态后，这条待办自动解除。
        for member in members:
            last=(member.get('history') or [{}])[-1]
            reason=last.get('reason') or ''
            if member.get('status')!='paused' or not reason.startswith('auto_resume_failed:'):continue
            code=reason.split(':',1)[1]
            detail=AUTO_RESUME_FAILURES.get(code,'服务重启后自动恢复失败（'+code+'）。请查看任务详情和 data/logs 中的错误日志，处理后手动恢复任务。')
            add('auto_resume:'+member['id']+':'+str(last.get('at')),'execution','服务重启后任务未自动恢复',detail+'任务一直保持暂停，没有操作网站。',
                task_id=member['id'],failure_code=code,job_label='、'.join(filter(None,(case_labels.get(cid,'') for cid in member.get('case_ids',[])))))
        # 网页动作结果未知由执行器保留、不重试，也不发给管理员。
        greeting_states=[]
        from .greeting_policy import render,stamp
        for record in self.db.list('greeting_readiness'):
            try:task=self.db.get('task',record['task_id']);case=next(c for c in cases if c['id']==record['case_id'])
            except (FileNotFoundError,StopIteration):continue
            if record['case_id'] not in task['config'].get('case_ids',[]):continue
            # 已撤出首聊的岗位不再挂话术待办（11-26 FDE 撤出后仍显示“待核验”）。
            from .job_members import outreach_ids
            if record['case_id'] not in outreach_ids(task['config']):continue
            if stamp(render(case.get('job_sourcing',{}).get('greeting',case.get('desired_greeting','')),case))!=record.get('expected_hash'):continue
            greeting_states.append(record)
            if record['status']=='unverified':
                add('greeting:'+record['task_id']+':'+record['case_id']+':'+record['expected_hash'],'greeting','首聊话术未能写入 BOSS',record['reason']+'；首聊照常进行，候选人收到的是 BOSS 上该岗位现有的招呼语。系统约 10 分钟后会再试写入；如需立即一致，可在 BOSS 后台“按职位设置招呼语”里改成本任务的问候语，写入成功后此待办自动解除。',task_id=record['task_id'],case_id=record['case_id'],job_label=record.get('job_label',''),greeting_state_id=record['id'])
            elif record['status']=='blocked':
                add('greeting:'+record['task_id']+':'+record['case_id']+':'+record['expected_hash'],'greeting','本岗位首聊话术待核验',record['reason']+'；仅限制该岗位新增开聊，已有会话和其他岗位继续处理。',task_id=record['task_id'],case_id=record['case_id'],job_label=record.get('job_label',''),greeting_state_id=record['id'])
        active_cases={r.get('params',{}).get('case_id') for r in runs if r.get('mode')=='boss_autopilot' and r.get('status')=='running' and r.get('params',{}).get('case_id')}
        latest={}
        for r in sorted(runs,key=lambda r:r.get('created',0),reverse=True):
            latest.setdefault(r.get('task_id') or (r.get('mode','')+str(r.get('params',{}).get('case_id',''))),r['id'])
        for r in runs:
            key=r.get('task_id') or (r.get('mode','')+str(r.get('params',{}).get('case_id','')))
            q=r.get('interaction') or {}
            if q.get('kind') in ('browser_access','browser_capacity'):
                reason=q.get('reason','capacity')
                add('access:'+r['id']+':'+reason,reason,'需要重新登录' if reason=='login' else '浏览器需要处理',q.get('prompt',''),run_id=r['id'])
            elif q.get('kind') in ('reply','confirm'):
                add('operator:'+r['id']+':'+str(q.get('id','current')),'operator','任务需要管理员处理',q.get('prompt','请打开关联任务补充信息或确认操作'),run_id=r['id'])
            elif r.get('status')=='failed' and r.get('params',{}).get('case_id') in active_cases and r.get('mode') in ('boss_contact','boss_recommend') and any(t in (r.get('error') or '') for t in ('未进行联系','请选择该候选人当前有效的首聊草稿')):
                continue
            elif r.get('status')=='failed' and r.get('error') and latest.get(key)==r['id'] and '预算内未达到' not in r['error']:
                if '本地话术与平台话术不同' in r['error'] and any(g['case_id']==r.get('params',{}).get('case_id') for g in greeting_states):continue
                # 页面超时、目标关闭、草稿在核对中变化，执行器下轮自己重做，不发给管理员。
                if not any(mark in r['error'] for mark in ('登录','验证码','未配置','授权','凭据','输入框已有')):continue
                add('failure:'+r['id'],'execution','任务异常待处理',r['error'],run_id=r['id'])
        for request in planning:
            if request['status']=='failed':
                add('planning:'+request['id'],'planning','Harness 重规划未完成',request.get('error','请在任务规划对话中重试'),task_id=request['task_id'],planning_request_id=request['id'])
        # 岗位变更未生效：同一任务同一岗位（或同类任务级变更）有了更新的请求（重试）后自动解除；管理员标记放弃（记录处理完成）后保持关闭。
        changes=self.db.list('job_change')
        from .job_changes import ACTIONS
        def change_label(change):return ACTIONS.get(change['action'],'岗位变更')+('「'+case_labels.get(change.get('case_id'),'')+'」' if change.get('case_id') else '')
        for change in changes:
            if change['status']!='failed':continue
            scope=lambda r:(r['task_id'],r.get('case_id') or r['action'])
            if any(r['at']>change['at'] and scope(r)==scope(change) for r in changes):continue
            head='岗位「'+case_labels.get(change.get('case_id'),'')+'」加入未完成' if change['action']=='add' else change_label(change)+'未完成'
            add('job_change:'+change['id'],'job_change','岗位变更未生效',head+'：'+(change.get('error') or '原因未记录')+'。已做：未改动正在运行的任务。需要：'+('查看失败场景后重试或放弃。' if change['action']=='add' else '查看失败原因后重试或放弃。'),
                task_id=change['task_id'],case_id=change.get('case_id'),job_label=case_labels.get(change.get('case_id'),''),job_change_id=change['id'])
        # 岗位变更排队超过 30 分钟仍未到执行器安全点（任务在运行）：生效或任务不再运行后自动解除。
        running={m['id']:m for m in members if m.get('status')=='running'}
        for change in changes:
            if change['status']!='pending' or change['task_id'] not in running or time.time()-change['at']<JOB_CHANGE_STUCK_SECONDS:continue
            add('job_change_stuck:'+change['id'],'job_change_stuck','岗位变更等待超时','岗位变更等待执行器安全点超过 30 分钟：'+change_label(change)+'仍未生效。已做：变更保持排队，任务其他工作照常，没有向候选人发消息。需要：查看任务运行状态与执行器日志，确认执行器仍在推进；生效后此待办自动解除。',
                task_id=change['task_id'],case_id=change.get('case_id'),job_label=case_labels.get(change.get('case_id'),''),job_change_id=change['id'])
        # 浏览器代办（读取/绑定 BOSS 岗位）未完成：同类（绑定按岗位）有更新的请求（重试）后自动解除；管理员标记已处理（放弃）后保持关闭。
        # 停在 running 但所属运行已不在进行中（服务重启、运行结束）也算未完成（下次结算标为失败，沿用同一待办）。
        from .browser_errands import KIND as ERRAND_KIND,STEPS as ERRAND_STEPS,STUCK_SECONDS as ERRAND_STUCK,INTERRUPTED,RUN_ACTIVE,label as errand_label,scope as errand_scope,window_open_db
        errands=self.db.list(ERRAND_KIND);live_runs={r.get('id') for r in runs if r.get('status') in RUN_ACTIVE}
        for errand in errands:
            orphan=errand['status']=='running' and errand.get('run_id') not in live_runs
            if not (errand['status']=='failed' or orphan) or any(r['at']>errand['at'] and errand_scope(r)==errand_scope(errand) for r in errands):continue
            add('browser_errand:'+errand['id'],'browser_errand','BOSS 岗位读取或绑定未完成',errand_label(errand)+'未完成，卡在“'+ERRAND_STEPS[errand['kind']]+'”：'+((INTERRUPTED if orphan else errand.get('error')) or '原因未记录')+'。已做：只标记这一条代办，任务其他工作照常，没有向候选人发消息。需要：查看原因后在岗位导入页重试，或把此待办标记为已处理（放弃）。',
                errand_id=errand['id'],job_label=errand.get('title') or '')
        # 代办在执行时段内排队超过 30 分钟仍未执行（执行器被接管、已停止等）：执行后自动解除；时段外排队是正常等待，不提醒。
        if window_open_db(self.db):
            for errand in errands:
                if errand['status']!='pending' or time.time()-errand['at']<ERRAND_STUCK:continue
                add('browser_errand_stuck:'+errand['id'],'browser_errand_stuck','BOSS 岗位读取或绑定等待超时',errand_label(errand)+'在执行时段内排队超过 30 分钟仍未执行。已做：代办保持排队，执行时段内执行器会在安全点处理；没有执行器时，再点一次刷新或绑定会带起执行。需要：确认执行器在运行、没有被管理员接管，或在岗位导入页再点一次刷新；执行后此待办自动解除。',
                    errand_id=errand['id'],job_label=errand.get('title') or '')
        # 运行中且绑定当前生效版本的任务：验收已失效的岗位只停发送。版本不一致属于 release_changed，不在这里重复提醒。
        from . import agent_workspace
        for member in running.values():
            try:release=self.db.get('agent_release',member['id'])
            except FileNotFoundError:continue
            if not member.get('release_id') or release.get('version_id')!=member['release_id']:continue
            _,validated,shared=agent_workspace.validation_state(self.db,member['id'])
            if not shared:
                # 生效版本未通过完整验收，或代码、模型、任务设置在运行中变了：执行器对该任务全部岗位停发，提醒一条任务级待办。
                add('task_not_validated:'+member['id']+':'+release['version_id'],'task_not_validated','任务验收已全部失效',('该任务的验收与当前执行代码、模型或任务设置不再一致，任务内全部岗位已停止发送。已做：收件读取照常，没有向候选人发送任何消息。需要：在任务岗位区点“重新验收”（按当前生效版本跑一次完整隔离验收，无需发布、不暂停任务）；通过后自动恢复发送，此待办自动解除。' if release.get('validation')=='passed' else '该任务的生效版本未通过完整隔离验收，任务内全部岗位已停止发送。已做：收件读取照常，没有向候选人发送任何消息。需要：在 Agent 工作空间重新建立草案、完成隔离验收并发布；验收恢复后此待办自动解除。'),
                    task_id=member['id'],job_label='、'.join(filter(None,(case_labels.get(cid,'') for cid in member.get('case_ids',[])))))
                continue
            for cid in member.get('case_ids',[]):
                if cid in validated:continue
                add('job_not_validated:'+member['id']+':'+cid+':'+release['version_id'],'job_not_validated','岗位验收已失效','岗位「'+case_labels.get(cid,'')+'」验收已失效，已只停这个岗位的发送；需要重新验收该岗位。',
                    task_id=member['id'],case_id=cid,job_label=case_labels.get(cid,''))
        # 同一岗位当天推荐页入口（切换、核对）失败累计次数：各执行器记录相加（重启前后合计），按日期计，次日自然清零。
        today=day_key();deferrals={}
        for r in runs:
            daily=r.get('outreach_deferrals');counts=daily.get(today) if isinstance(daily,dict) else None
            for cid,n in (counts.items() if isinstance(counts,dict) else ()):
                if type(n) is int:deferrals[cid]=deferrals.get(cid,0)+n
        for cid,n in deferrals.items():
            if n<OUTREACH_BLOCKED_DEFERRALS:continue
            add('job_outreach_blocked:'+today+':'+cid,'job_outreach_blocked','岗位首聊入口反复失败','岗位「'+case_labels.get(cid,'')+'」今天推荐页切换或核对已失败 '+str(n)+' 次。已做：只停这个岗位的首聊（稍后自动重试），收件、回复与其他岗位照常。需要：打开执行浏览器查看推荐牛人页能否选到该岗位；当天累计次数次日清零，此待办次日自动解除。',
                case_id=cid,job_label=case_labels.get(cid,''))
        # 同一岗位当天首聊前初筛连续失败：执行器在成功时清零；各执行器记录合计（只加最近一次失败晚于全部成功的记录），之后有过成功或次日即自动解除。
        screening={};num=lambda v:v if type(v) in (int,float) else 0
        for r in runs:
            daily=r.get('screening_failures');counts=daily.get(today) if isinstance(daily,dict) else None
            for cid,entry in (counts.items() if isinstance(counts,dict) else ()):
                if not isinstance(entry,dict) or type(entry.get('n',0)) is not int:continue
                acc=screening.setdefault(cid,{'n':0,'last_failed_at':0,'last_ok_at':0,'entries':[]})
                acc['entries'].append(entry)
                if num(entry.get('last_failed_at'))>=acc['last_failed_at'] and isinstance(entry.get('reason'),str):acc['reason']=entry['reason'][:60]
                acc['last_failed_at']=max(acc['last_failed_at'],num(entry.get('last_failed_at')));acc['last_ok_at']=max(acc['last_ok_at'],num(entry.get('last_ok_at')))
        for cid,acc in screening.items():
            acc['n']=sum(e.get('n',0) for e in acc['entries'] if num(e.get('last_failed_at'))>acc['last_ok_at'])
            if acc['n']<SCREENING_UNAVAILABLE_FAILURES or acc['last_failed_at']<=acc['last_ok_at']:continue
            add('screening_unavailable:'+today+':'+cid,'screening_unavailable','岗位初筛暂不可用','岗位「'+case_labels.get(cid,'')+'」今天首聊前初筛已连续失败 '+str(acc['n'])+' 次'+(f'（最近一次：{acc["reason"]}）' if acc.get('reason') else '')+'。已做：失败的候选人先放下、稍后再看，其他工作照常，没有向候选人发消息。需要：查看模型网关是否可用、执行浏览器里推荐牛人详情能否打开；恢复后此待办自动解除。',
                case_id=cid,job_label=case_labels.get(cid,''))
        # 提示：已撤下首聊且没有进行中会话的岗位可以移除；移除后或恢复首聊、来了新会话时自动解除。
        from .job_changes import OPEN as CHANGE_OPEN,_open_conversations
        from .job_members import normalize as normalize_members
        by_id={c['id']:c for c in cases}
        for member in members:
            if member.get('status') in ('stopped','completed'):continue
            try:config=copy.deepcopy(self.db.get('task',member['id'])['config'])
            except (FileNotFoundError,KeyError):continue
            try:jobs=normalize_members(config)
            except ValueError:continue
            for entry in jobs:
                cid=entry['case_id']
                if entry['outreach']!='withdrawn' or cid not in by_id or _open_conversations(by_id[cid]):continue
                if any(r['task_id']==member['id'] and r.get('case_id')==cid and r['action']=='remove' and r['status'] in CHANGE_OPEN for r in changes):continue
                add('job_removable:'+member['id']+':'+cid,'job_removable','岗位可以移除','岗位「'+case_labels.get(cid,'')+'」已撤下首聊，目前没有进行中的会话。已做：仅提示，任务照常运行。需要：不再需要时可在任务岗位区移除该岗位；不处理也不影响其他岗位。',
                    task_id=member['id'],case_id=cid,job_label=case_labels.get(cid,''))
        # 执行浏览器页面：自动刷新、重置都没修好，或页面上有没发出去的文字（系统不会替你删或发）。
        try:health=self.db.get('page_health','boss')
        except FileNotFoundError:health=None
        # 11-24 20:40 沟通页卡在启动页：系统因页面打不开暂停的任务，按暂停期间自愈的真实状态写明会不会自动恢复。
        from .selection_recovery import page_blocked,page_stuck,PAGE_PROBLEMS,PAGE_MAX_ATTEMPTS
        from .page_health import done_steps
        page_members=[m for m in members if page_blocked(m)]
        if page_members:
            try:recovery=self.db.get('page_recovery','boss')
            except FileNotFoundError:recovery={}
            paused_at=max(m['history'][-1]['at'] for m in page_members)
            # 执行器还没为这次暂停建立记录时，按当前页面检查推断，不沿用旧的尝试次数与结果。
            if recovery.get('paused_at')!=paused_at:
                recovery={'attempts':0,'next_at':paused_at+600,'page_stuck':page_stuck(health,paused_at),'steps':(health or {}).get('steps') or []}
            # 暂停期间的自愈由运行中的账号执行器完成；执行器没在运行（例如服务重启后）就没有人重试，必须照实说。
            runner=any(r.get('account_runner') and r.get('status') in ('running','waiting') for r in runs)
            from datetime import datetime
            from .daily_report import report_timezone
            def clock(ts):return datetime.fromtimestamp(ts,report_timezone()).strftime('%H:%M') if ts else '稍后'
            labels='、'.join(filter(None,(case_labels.get(cid,'') for m in page_members for cid in m.get('case_ids',[]))))
            attempts=recovery.get('attempts',0)
            done=done_steps(recovery.get('steps')) or '自动处理页面'
            detail=('BOSS 沟通页打不开（停在启动页、“加载中”或内容读不到），系统无法确认输入框里有没有没发出去的文字，已于 '+clock(paused_at)+' 暂停任务'+('「'+labels+'」' if labels else '')+'，没有发送任何消息。'
                    '已做：'+done+('；暂停后又自动检查 '+str(attempts)+' 次' if attempts else '')+'。')
            manual='需要：'+BROWSER_CONTROL+'，确认沟通页正常（可从左侧菜单“消息”进入）后，在任务页手动恢复任务；恢复时会先检查页面并只读核对原会话，恢复后此待办自动解除。'
            result=recovery.get('result')
            if not runner:
                detail+='现在账号执行器没有在运行（例如服务刚重启），系统不会自动重试。'+manual
            elif not recovery.get('page_stuck'):
                detail+='暂停前的页面检查没有显示页面卡住（可能是编辑器本身读不到），这种情况系统不会自动重试。'+manual
            elif result=='capped':
                detail+=str(recovery.get('reason') or '自动尝试次数已用完')+'，系统已停止自动重试。'+manual
            elif result=='precondition':
                detail+='任务恢复会被拒绝（'+str(recovery.get('reason'))+'），刷新页面解决不了，所以页面恢复后也无法自动恢复任务，系统没有再去碰页面。需要：先按原因处理（例如重新验收发布、运行环境检查），再在任务页手动恢复。'
            elif result=='needs_admin':
                detail+=('暂停期间检查发现：'+PAGE_PROBLEMS.get(recovery.get('reason'),str(recovery.get('reason')))+'。在你处理之前系统不会自动恢复任务。'
                         '需要：'+BROWSER_CONTROL+'处理；处理好后，系统下次检查（约 '+clock(recovery.get('next_at'))+'）确认页面正常就会自动恢复，也可以直接在任务页手动恢复。恢复后此待办自动解除。')
            elif result=='resume_failed':
                detail+=('页面已恢复正常，但任务未能自动恢复（'+str(recovery.get('reason') or '原因未记录')+'）。'
                         +('系统会在下次检查时再试（约 '+clock(recovery.get('next_at'))+'，已试 '+str(attempts)+'/'+str(PAGE_MAX_ATTEMPTS)+' 次）；也可以按原因处理后在任务页手动恢复。' if attempts<PAGE_MAX_ATTEMPTS
                           else '自动尝试次数已用完，系统已停止自动重试。需要：按原因处理后在任务页手动恢复。')+'恢复后此待办自动解除。')
            else:
                when=('现在不在任务执行时段，系统不操作网站；进入时段后（约 '+clock(recovery.get('outside_window_until'))+'）自动再试。' if recovery.get('outside_window_until')
                      else '执行时段内前 3 次约每 10 分钟、之后约每 30 分钟自动再试，最多 '+str(PAGE_MAX_ATTEMPTS)+' 次（已试 '+str(attempts)+' 次，下次约 '+clock(recovery.get('next_at'))+'）。')
                detail+=(when+'页面恢复、并确认没有草稿、没有掉登录或验证后，系统会自动恢复任务并先只读核对原会话，此待办自动解除。'
                         '一般不用处理；如长时间未恢复，可以'+BROWSER_CONTROL+'，从左侧菜单“消息”进入沟通页，页面正常后系统会自动恢复。')
            add('browser_health:paused:'+','.join(sorted(m['id'] for m in page_members))+':'+str(paused_at),'browser_health','BOSS 沟通页打不开，任务已暂停',detail,
                task_id=page_members[0]['id'],job_label=labels,screenshot=(health or {}).get('screenshot'))
        elif health and not health.get('healthy') and time.time()-health.get('at',0)<24*3600:
            if health.get('reason')=='draft_present':
                add('browser_health:draft:'+str(int(health['at']//3600)),'browser_health','执行浏览器里有一段没发出去的文字',
                    _own_draft_note(self.db)+'BOSS 沟通页的输入框里有一段还没发送的文字。为了不丢掉它，系统不会刷新页面，也不会替你发送，这期间自动处理会受影响。'
                    '请'+BROWSER_CONTROL+'，看一下这段文字：需要就手动发出去，不需要就删掉。处理完系统会自动继续，此待办自动解除。',screenshot=health.get('screenshot'))
            elif health.get('exhausted'):
                running=any(m.get('status')=='running' for m in members)
                add('browser_health:exhausted:'+str(int(health['at']//3600)),'browser_health','执行浏览器的 BOSS 页面打不开',
                    'BOSS '+('沟通页' if health.get('kind')=='chat' else '推荐牛人页')+'一直停在“加载中”或内容出不来。系统已'+(done_steps(health.get('steps')) or '自动处理')+'，仍然不行；'
                    +('任务仍在运行，执行器约 10 分钟后会自动再试。收件、回复或开聊会暂时受影响。' if running else '目前没有运行中的任务，系统不会自动重试；恢复任务后执行器会先检查页面。')+
                    '请'+BROWSER_CONTROL+'，看一下画面：可以从左侧菜单“消息”进入沟通页；如果 BOSS 提示网络异常或要求重新登录，按页面提示处理；页面恢复正常后此待办自动解除。',screenshot=health.get('screenshot'))
        for case in cases:
            # 11-27 旧 QE：撤下岗位的旧会话认不出，候选人来了新消息系统也不会回，交给人工。每次新消息一条，管理员回复后可记录处理完成。
            for chat_id,info in (case.get('withdrawn_unreadable') or {}).items():
                if chat_id not in case.get('deferred_chat_ids',[]):continue  # 已恢复正常处理或不在列表里了
                label=case.get('job',{}).get('label','')
                add('withdrawn_chat:'+case['id']+':'+chat_id+':'+str(int(info.get('at') or 0)),'withdrawn_chat','旧岗位会话需人工回复',
                    (info.get('receiver') or '一位候选人')+'在已撤下的岗位「'+label+'」的会话里有新消息，但系统认不出这条旧会话（会话头部的沟通职位和岗位名对不上），不会自动回复。'
                    '已做：不再定时复核，只在这条会话再有新消息时复核一次。需要：在 BOSS 手动回复这位候选人，处理后填写说明并“记录处理完成”；系统复核通过时此待办自动关闭。',
                    case_id=case['id'],job_label=label)
        for case in cases:
            # 会话相关待办只发管理员能处理的，并用大白话写清楚“发生了什么、系统做了什么、你可以做什么”。
            def person_of(chat_id,case=case):
                return next((p for p in case.get('candidates',{}).values() if not p.get('alias_of') and (p.get('conversation') or {}).get('chat_id')==chat_id),None)
            def name_of(person):return (person or {}).get('profile',{}).get('display_name') or '一位候选人'
            for chat_id in case.get('missing_chat_ids') or []:
                person=person_of(chat_id)
                add('missing_chat:'+case['id']+':'+chat_id,'missing_chat',name_of(person)+' 的会话在 BOSS 上找不到了',
                    '系统多次把 BOSS 沟通列表翻到最底部，都找不到和这位候选人的会话。常见原因：会话超过 30 天被平台收起、被删除，或对方换了沟通岗位。'
                    '系统已停止自动查找，也不会再自动回复或追聊这位候选人；其他候选人和新开聊不受影响。'
                    '你可以：在 BOSS 上搜索这位候选人，还想继续沟通就由顾问手动联系；不需要的话，在这里写一句处理说明后关闭即可。会话如果重新出现在列表里，系统会自动恢复处理。',
                    case_id=case['id'],candidate_id=person['id'] if person else None,job_label=case.get('job',{}).get('label',''))
            since=case.get('deferred_chat_since') or {}
            for chat_id in case.get('deferred_chat_ids') or []:
                if time.time()-since.get(chat_id,0)<ISOLATED_TODO_SECONDS:continue
                person=person_of(chat_id)
                add('isolated_chat:'+case['id']+':'+chat_id,'isolated_chat',name_of(person)+' 的会话一天多都没能打开',
                    '系统一直在自动重试打开这条会话，但页面上显示的人或岗位和记录对不上，所以暂停了对这位候选人的自动回复和追聊；其他候选人和新开聊不受影响。'
                    '你可以：在 BOSS 上找到这位候选人的会话看一眼，如果对方改了名字或换了岗位，由顾问手动接续即可；确认不需要处理的话，写一句说明后关闭。系统之后能正常打开这条会话时会自动解除。',
                    case_id=case['id'],candidate_id=person['id'] if person else None,job_label=case.get('job',{}).get('label',''))
            for c in case.get('candidates',{}).values():
                if c.get('alias_of'):continue
                refs={'case_id':case['id'],'candidate_id':c['id'],'job_label':case['job']['label']}
                # 决策暂缓和平台回执核验由执行器自己重试，不发给管理员。
                # 知识库没有答案的问题在下面单独生成待办。
                incoming=c.get('inbox',[])
                # 11-26 周全起：简历归档失败只留在候选人记录里，管理员不知道这份简历没推到飞书群。
                for source,state in (c.get('resume_archives') or {}).items():
                    from .resume_archive import retryable
                    if state.get('status')=='failed' and not retryable(state):
                        add('resume_archive:'+case['id']+':'+c['id']+':'+source,'resume_archive','简历未能归档','已收到候选人的附件简历，但系统没能下载存档（'+str(state.get('error') or '原因未知')[:120]+'），因此也没有推送到飞书简历群。请在候选人详情的“简历归档”里点重新归档，系统会在下次拜访该会话时重新下载并推送；存档成功后此待办自动解除。',**refs)
                # 对话因隐私规则暂缓时，候选人来信不会自动回复：必须让管理员看到，由顾问在平台接续。
                from .decision_runtime import is_privacy_hold
                if is_privacy_hold(c.get('decision_hold')) and c.get('status') not in ('closed','do_not_contact'):
                    add('privacy_hold:'+case['id']+':'+c['id'],'decision','对话需要隐私复核后人工接续',
                        '这段对话里有系统无法可靠隐去的身份或机构信息，按隐私规则没有发给模型，候选人的来信暂时不会自动回复（收到简历的致谢仍会照常发送）。请在沟通记录中查看，由顾问在平台上回复；对话有新消息后系统会自动重新检查，恢复正常后此待办自动解除。',**refs)
                # 11-26 17:36 伍礼鹏：问题后面跟着简历卡片时，语义判断针对的是卡片前的问题（与回复用同一个 reply_target），不是最后一条。
                from .decision_runtime import reply_target,_resume_card
                target=reply_target(c)
                for last in incoming:
                    # 已对这条问题发出实质回答并确认送达：管理员补充的答案已经给到候选人，待办完成。
                    if answered(c,last['external_id']):continue
                    if _resume_card(last):continue
                    from .conversation_semantics import current as semantic_current
                    semantic=semantic_current(case,c)
                    if semantic and (not target or last['external_id']!=target.get('external_id') or semantic['answer']['action']!='defer'):continue
                    # 知识库答不上就立刻生成待办，不等模型分类，也不等浏览器把会话点开。
                    from .boss_resume import is_permission_text
                    text=last.get('text','').strip()
                    platform_card=last.get('platform_control') or is_permission_text(text) or bool(re.fullmatch(r'我想要?和您交换(?:微信|电话)[，,]?\s*您是否同意\s*(?:拒绝\s*同意)?',text))
                    routine_interest='对这份工作很感兴趣' in text and bool(re.search(r'深入沟通|进一步沟通',text)) and not bool(re.search(r'薪|待遇|提成|底薪|地点|地址|工作时间|合作|合同',text))
                    if platform_card or routine_interest:continue
                    labels=intent_labels(last.get('text',''))
                    feedback=next((r for r in reversed(planning) if r.get('case_id')==case['id'] and r.get('candidate_id')==c['id'] and r.get('reply_id')==last['external_id']),None)
                    waiting=feedback and (feedback['status']!='succeeded' or c.get('pending_question_id')==last['external_id'])
                    route=semantic['answer']['action'] if semantic else answer_question(case,last.get('text',''))['action']
                    if (waiting or route=='defer') and not set(labels)&{'decline','identity','later'} and c.get('status') not in ('closed','do_not_contact'):
                        add('question:'+case['id']+':'+c['id']+':'+last['external_id'],'question','候选人问题需顾问确认','请在沟通记录中查看原问题，补充已确认的岗位事实或由顾问接续；系统不会猜测具体条件。',planning_request_id=feedback['id'] if feedback else None,question=last.get('text',''),reply_id=last['external_id'],topic=question_topic(last.get('text','')),**refs)
                # 已取得简历后转顾问接续属于正常流程：简历已由飞书简历群交付，不再生成“待顾问接续”待办，也不推送给管理员。
                # 候选人上的 handoff 标记保留，用于阻止后续追聊；异常（发送失败、问题待答等）仍照常生成待办。
        for item in desired.values():
            if item['kind']=='question':
                item['related_count']=sum(1 for other in desired.values() if other['kind']=='question' and other['case_id']==item['case_id'] and other['topic']==item['topic'])
        now=time.time()
        for ident,item in desired.items():
            old=existing.get(ident)
            if not old:
                self.db.put('attention',ident,{**item,'status':'pending','created_at':now,'updated_at':now,'history':[{'at':now,'action':'created'}]})
            elif old.get('auto_resolved'):
                old.update(status='pending',auto_resolved=False,updated_at=now)
                old['history'].append({'at':now,'action':'reopened'})
                self.db.put('attention',ident,old)
            if old and not old.get('auto_resolved'):
                old.update(item)
                self.db.put('attention',ident,old)
        for ident,old in existing.items():
            if ident not in desired and old['status']!='resolved':
                old.update(status='resolved',auto_resolved=True,updated_at=now)
                retired_delivery=old.get('kind')=='delivery'
                note='消息回执转为后台会话同步，不再按时间生成异常待办；仅清理提醒，未改变发送状态或防重发记录。' if retired_delivery else None
                if retired_delivery:old.update(note=note,retired_reason='routine_delivery_observation')
                old['history'].append({'at':now,'action':'source_cleared',**({'note':note} if note else {})})
                self.db.put('attention',ident,old)
        return sorted(self.db.list('attention'),key=order)

    def update(self,ident,body):
        todo=self.db.get('attention',ident)
        status=body.get('status');note=body.get('note','')
        if status not in ('pending','in_progress','resolved') or not isinstance(note,str) or len(note)>4000:raise ValueError('待办状态或处理说明不合法')
        if status=='resolved' and todo['kind'] in ('login','verification','capacity','delivery','execution','execution_recovery','greeting','platform_action','decision','operator','planning','action','browser_health','job_change_stuck','job_not_validated','task_not_validated','job_outreach_blocked','screening_unavailable','browser_errand_stuck'):
            raise ValueError('请先在关联任务或平台核实并处理；该类待办由真实状态自动解除')
        if status=='resolved' and not note.strip():raise ValueError('请填写处理结果，便于追溯')
        todo.update(status=status,updated_at=time.time(),note=note)
        todo['history'].append({'at':time.time(),'action':status,'note':note})
        self.db.put('attention',ident,todo)
        return todo


def sourcing_overview(cases,runs):
    today=day_key();rows=[]
    for case in cases:
        people=[c for c in case['candidates'].values() if not c.get('alias_of')]
        attempts=[m for c in people for m in c.get('outbox',[]) if m.get('intent')=='first_contact' and m.get('claimed_at') and day_key(m['claimed_at'])==today]
        rows.append({'case_id':case['id'],'name':case['job']['label'],'enabled':case.get('autonomy',{}).get('enabled',False),
            'daily_limit':case.get('autonomy',{}).get('daily_new_limit',20),'today_claimed':len(attempts),
            'today_delivered':sum(m['status']=='sent' for m in attempts),'reply_count':sum(bool(c.get('inbox')) for c in people),
            'resume_count':sum(bool(c.get('resumes')) for c in people),'handoff_count':sum(bool(c.get('handoff')) for c in people),
            'execution':case.get('editable_config',{}).get('execution',case.get('execution',{}))})
    return {'date':today,'jobs':rows,'account_claimed':sum(r['today_claimed'] for r in rows),'scope':'本机已记录的尝试次数，回执未知也占用本机预算；平台可用余额未实时核实'}
