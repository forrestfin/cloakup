"""运行中的寻访任务增量变更岗位：加入、撤下、恢复、移除、调权重、改每日首聊总额。

闭环：管理员发起 -> 校验 -> （仅加入）只用新岗位的必需场景跑一次隔离验收 -> 排队 ->
执行器在每轮开始的安全点（未持有任何岗位数据、不在恢复核验轮次）基于当前生效版本重新组装，
同一事务写入发布包、生效版本、任务、受影响岗位与运行中任务的绑定 -> 新版本按岗位逐项判定验收。

加入的岗位快照在发起时生成一次并随验收保存，应用时原样并入，不从岗位现状重新派生；
已有岗位的快照从当前生效版本复制。验收时的运行时签名、任务摘要或新岗位快照摘要与应用时不一致，
变更失败并生成待办，不覆盖更新的版本。撤下只停新首聊，收件、回复、追聊、收简历照常。
"""
import copy
import time
import uuid

from . import job_members
from .sourcing import TERMINAL

KIND='job_change'
ACTIONS={'add':'加入岗位','withdraw':'撤下岗位','resume':'恢复首聊','remove':'移除岗位','weights':'调整岗位权重','limit':'调整每日首聊总额'}
OPEN=('testing','pending')
UNCONFIGURED='请先确认该岗位的问候语、事实和寻访条件'


def _current(manager,task_id):
    release=manager.agent_workspace.bootstrap(task_id)
    base=manager.db.get('agent_bundle',release['version_id'])
    return release,base


def _members(bundle):
    cfg=copy.deepcopy(bundle['config']);return job_members.normalize(cfg)


def _label(manager,cid):
    try:return manager.cases.load(cid)['job'].get('label') or cid
    except (FileNotFoundError,KeyError):return cid


def _open_conversations(case):
    return any(p.get('status') not in TERMINAL and p.get('conversation') and not p.get('alias_of') for p in case['candidates'].values())


def _weight(value):
    if type(value) is not int or not 1<=value<=10:raise ValueError('岗位权重须为1至10的整数')
    return value


def _check_addable(manager,task_id,members,cid):
    """加入前提：岗位已绑定 JD、管理员已保存问候语与寻访配置，不属于其他未停止任务，全账号无同名岗位。"""
    try:case=manager.cases.load(cid)
    except (FileNotFoundError,KeyError,TypeError) as exc:raise ValueError('岗位不存在') from exc
    if cid in [m['case_id'] for m in members]:raise ValueError('岗位已在任务中')
    if len(members)>=20:raise ValueError('一个任务最多20个岗位')
    if not case['job'].get('snapshot') or case.get('job_sourcing') is None or not case.get('config_revision',0)>0:raise ValueError(UNCONFIGURED)
    if not case.get('autonomy',{}).get('enabled'):raise ValueError('请先为该岗位启用自动化')
    others=[m for m in manager.account_execution.members() if m['id']!=task_id and m['status']!='stopped']
    if any(cid in m['case_ids'] for m in others):raise ValueError('岗位已由其他任务管理，请先停止原任务')
    ids=list(dict.fromkeys([c for m in others for c in m['case_ids']]+[m['case_id'] for m in members]))
    if case['job'].get('label') in {_label(manager,c) for c in ids}:raise ValueError('账号下已有同名岗位，收件无法可靠分流，请先区分名称')
    return case


def request(manager,task_id,body):
    """校验并登记一条岗位变更；加入先启动新岗位的隔离验收，其余直接排队。执行器未运行时立即生效。"""
    db=manager.db;action=body.get('action')
    if action not in ACTIONS:raise ValueError('不支持的岗位变更')
    task=manager.definitions.get(task_id)
    from .scenarios import is_sourcing
    if not is_sourcing(task['config'].get('engine')):raise ValueError('仅寻访任务可以变更岗位')
    release,base=_current(manager,task_id);members=_members(base['bundle'])
    member_ids=[m['case_id'] for m in members]
    cid=body.get('case_id') if action not in ('weights','limit') else None
    if action not in ('weights','limit') and (not isinstance(cid,str) or not cid):raise ValueError('请选择岗位')
    if cid and any(r['task_id']==task_id and r.get('case_id')==cid and r['status'] in OPEN for r in db.list(KIND)):raise ValueError('该岗位已有变更等待生效，请等待完成')
    member=next((m for m in members if m['case_id']==cid),None)
    payload={}
    if action=='add':
        payload={'weight':_weight(body.get('weight',1))}
        case=_check_addable(manager,task_id,members,cid)
    elif action in ('withdraw','resume','remove') and not member:raise ValueError('岗位不在任务中')
    elif action=='withdraw' and member['outreach']!='active':raise ValueError('该岗位已撤下')
    elif action=='resume' and member['outreach']!='withdrawn':raise ValueError('该岗位已在参与首聊')
    elif action=='remove':
        if len(members)<=1:raise ValueError('任务至少保留一个岗位')
        if _open_conversations(manager.cases.load(cid)):raise ValueError('该岗位还有进行中的会话，请先撤下，等会话结束后再移除')
    elif action=='weights':
        weights=body.get('weights')
        if not isinstance(weights,dict) or not weights:raise ValueError('请填写岗位权重')
        if set(weights)-set(member_ids):raise ValueError('岗位不在任务中')
        payload={'weights':{k:_weight(v) for k,v in weights.items()}}
    elif action=='limit':
        limit=body.get('limit')
        if type(limit) is not int or not 0<=limit<=20:raise ValueError('每日首聊总额须为0至20的整数')
        payload={'limit':limit}
    now=time.time()
    record={'id':uuid.uuid4().hex,'task_id':task_id,'action':action,'case_id':cid,'payload':payload,'base_version_id':release['version_id'],'status':'pending','at':now}
    if action=='add':
        # 以生效版本派生：追加新岗位快照（只在此生成一次）与成员，只跑该岗位的必需场景。
        from .agent_workspace import source,source_digest
        bundle=copy.deepcopy(base['bundle'])
        draft=copy.deepcopy(bundle);draft['sources'].append(source(case));draft['config']['case_ids']=[*member_ids,cid]
        draft['config']['members']=[*members,{'case_id':cid,'weight':payload['weight'],'outreach':'active'}]
        normalized=manager.agent_workspace.normalize(task_id,draft)  # 与整单草案同一套校验（事实、FAQ、寻访配置、任务归属）
        bundle['sources'].append(next(i for i in normalized['sources'] if i['case_id']==cid))
        bundle['config']['members']=copy.deepcopy(draft['config']['members']);bundle['config'].pop('case_ids',None);job_members.normalize(bundle['config'])
        test=manager.agent_workspace.start_job_test(task_id,bundle,cid,job_change_id=record['id'])
        record.update(status='testing',test_id=test['id'],source_digest=source_digest(case))
        db.put(KIND,record['id'],record)
        worker=manager.agent_workspace.workers.get(test['id'])
        if worker is not None:worker.add_done_callback(lambda _task:_after_test(manager,record['id']))
        return db.get(KIND,record['id'])
    db.put(KIND,record['id'],record)
    # 没有活动运行时没有其他写入方，可以立即生效；否则由执行器在下一轮开始时应用。
    if not manager.busy():apply_pending(manager)
    return db.get(KIND,record['id'])


def _after_test(manager,ident):
    try:
        settle(manager)
        if not manager.busy():apply_pending(manager)
    except Exception:
        from .logbook import log
        log('error',type='job_change',step='settle_failed',job_change_id=ident)


def settle(manager):
    """验收结束的加入请求：通过转 pending，未通过或被中断转 failed；仍在运行的保持 testing。"""
    from .agent_workspace import incomplete_validation
    db=manager.db;moved=0
    for record in db.list(KIND):
        if record['status']!='testing':continue
        try:test=db.get('agent_test',record['test_id'])
        except FileNotFoundError:test={'status':'interrupted'}
        if test['status'] in ('queued','running'):continue
        gap=incomplete_validation(test,test['bundle'],[record['case_id']]) if test['status']=='passed' else None
        if test['status']=='passed' and not gap:record.update(status='pending',validated_at=time.time())
        else:
            outcomes=(test.get('result') or {}).get('outcomes') or []
            reason=gap or test.get('error') or f"新岗位隔离验收未通过（{sum(bool(o.get('passed')) for o in outcomes)}/{len(outcomes)} 项通过）"
            _fail(record,reason)
        db.put(KIND,record['id'],record);moved+=1
    return moved


def _fail(record,reason):
    record.update(status='failed',error=str(reason)[:400] or 'failed',finished_at=time.time())
    from .logbook import log
    log('state',type='job_change',task_id=record['task_id'],case_id=record.get('case_id'),action=record['action'],step='failed',actor='admin',reason=record['error'])


def apply_pending(manager,params=None):
    """执行器安全点调用：依次应用排队的变更，返回已生效的记录。单条失败只标记该条，不影响执行器。

    恢复核验轮次（autopilot.recovery_pass）只读，不应用任何变更。
    """
    db=getattr(manager,'db',None)
    if db is None or not hasattr(db,'list'):return []
    if params and params.get('inbound_resume_accept'):return []
    settle(manager)
    applied=[]
    for record in sorted((r for r in db.list(KIND) if r['status']=='pending'),key=lambda r:r['at']):
        try:applied.append(apply(manager,record))
        except Exception as exc:
            # 已提交的记录不再改写：只有提交前出错（记录仍为 pending）才标记失败。
            if db.get(KIND,record['id'])['status']!='pending':continue
            _fail(record,str(exc) or type(exc).__name__);db.put(KIND,record['id'],record)
    return applied


def apply(manager,record):
    """基于当前生效版本重新组装并原子提交：发布包、生效版本、任务、受影响岗位、运行中任务的绑定。"""
    from .agent_workspace import digest,incomplete_validation,runtime_signature,signature,source_digest,source_check_digest,task_digest
    from .autonomy import day_key
    from .task_policy import overlay
    db=manager.db;now=time.time();task_id=record['task_id'];action=record['action'];cid=record.get('case_id')
    release,base=_current(manager,task_id);task=manager.definitions.get(task_id)
    bundle=copy.deepcopy(base['bundle']);cfg=bundle['config'];members=job_members.normalize(copy.deepcopy(cfg))
    member=next((m for m in members if m['case_id']==cid),None)
    if action in ('withdraw','resume','remove') and not member:raise ValueError('岗位已不属于该任务的生效版本')
    touched=[]
    if action=='add':
        test=db.get('agent_test',record['test_id'])
        _check_addable(manager,task_id,members,cid)
        if source_digest(manager.cases.load(cid))!=record.get('source_digest'):raise ValueError('岗位资料在验收后被修改，请重新发起加入')
        item=copy.deepcopy(next(i for i in test['bundle']['sources'] if i['case_id']==cid))
        bundle['sources'].append(item);members.append({'case_id':cid,'weight':record['payload']['weight'],'outreach':'active'})
        touched=[cid]
    elif action=='withdraw':member['outreach']='withdrawn'
    elif action=='resume':member['outreach']='active'
    elif action=='remove':
        if len(members)<=1:raise ValueError('任务至少保留一个岗位')
        if _open_conversations(manager.cases.load(cid)):raise ValueError('该岗位还有进行中的会话，请先撤下，等会话结束后再移除')
        members.remove(member);bundle['sources']=[i for i in bundle['sources'] if i['case_id']!=cid]
    elif action=='weights':
        for m in members:
            if m['case_id'] in record['payload']['weights']:m['weight']=record['payload']['weights'][m['case_id']]
        if set(record['payload']['weights'])-{m['case_id'] for m in members}:raise ValueError('岗位已不属于该任务的生效版本')
    elif action=='limit':
        cfg.setdefault('execution',{})['daily_new_limit']=record['payload']['limit'];touched=[m['case_id'] for m in members]
    # 当天移除的岗位仍计入任务当天额度（job_quota.counted_case_ids）；只保留当天的记录，重新加入或仍在任务中的岗位按成员计入。
    today=day_key();removed=[e for e in cfg.get('removed_today') or [] if isinstance(e,dict) and e.get('day')==today and e.get('case_id')!=cid]
    if action=='remove':removed.append({'case_id':cid,'day':today})
    if removed:cfg['removed_today']=removed
    else:cfg.pop('removed_today',None)
    cfg['members']=members;cfg.pop('case_ids',None);job_members.normalize(cfg)
    if task_digest(bundle)!=task_digest(base['bundle']):raise ValueError('岗位变更不能改变任务级设置')
    if action=='add':
        # 新岗位的验收必须对应应用时的运行时、任务设置与这份岗位快照，并通过与发布相同的门槛。
        covered=test.get('covered') or {}
        if (covered.get('runtime')!=runtime_signature() or covered.get('task')!=task_digest(bundle) or (covered.get('sources') or {}).get(cid)!=source_check_digest(item)
                or test.get('signature')!=signature(test['bundle']) or incomplete_validation(test,test['bundle'],[cid])):
            raise ValueError('验收之后任务设置、执行代码或模型已变化，新岗位的验收不再有效，请重新发起加入')
    prepared=manager.definitions.save({'expected_revision':task['revision'],'config':copy.deepcopy(cfg)},task_id,validate_only=True)
    version_id=uuid.uuid4().hex
    version={'id':version_id,'task_id':task_id,'bundle':bundle,'digest':digest(bundle),'created_at':now,'origin':'job_change',
             'validation':base.get('validation'),'test_id':base.get('test_id'),'base_version_id':base['id'],'job_change_id':record['id'],
             **({'job_test_id':record['test_id']} if action=='add' else {})}
    next_release={**release,'version_id':version_id,'revision':release['revision']+1,'released_at':now,'task_revision':prepared['revision'],'job_change_id':record['id']}
    items=[('agent_bundle',version_id,version,0,True),
           ('agent_release',task_id,next_release,db.revision('agent_release',task_id),True),
           ('task',task_id,prepared,db.revision('task',task_id),True)]
    for c in touched:
        case=manager.cases.load(c);item=next(i for i in bundle['sources'] if i['case_id']==c)
        if any(m['status']=='sending' for p in case['candidates'].values() for m in p.get('outbox',[])):raise ValueError('该岗位仍有消息发送中，稍后重试')
        if c==cid and action=='add':
            case.update(anonymous_job=copy.deepcopy(item['anonymous_job']),confirmed_facts=copy.deepcopy(item['facts']),faq=copy.deepcopy(item['faq']),
                        conversation_prompt=bundle['conversation_prompt'],job_sourcing=copy.deepcopy(item['sourcing']))
            case.setdefault('autonomy',{})['revision']=case.get('autonomy',{}).get('revision',0)+1
            for p in case['candidates'].values():
                p.pop('decision_wait_until',None);p.pop('harness_decision',None);p.pop('conversation_semantics',None)
                for msg in p.get('outbox',[]):
                    if msg['status'] in ('draft','approved'):msg.update(status='stale',approved_fingerprint=None)
        overlay(case,prepared);case['agent_release_id']=version_id;case['updated_at']=now
        items.append(('case',c,case,db.revision('case',c),True))
    record={**record,'status':'applied','applied_at':now,'version_id':version_id}
    items.append((KIND,record['id'],record,db.revision(KIND,record['id']),False))
    # 运行中的任务绑在当前生效版本上：同一事务换绑到新版本并更新岗位范围，否则 availability 判定 release_changed。
    owner=next((m for m in db.list('account_task') if m['id']==task_id),None)
    if owner and owner['status']!='stopped':
        rebound={**owner,'case_ids':list(prepared['config']['case_ids'])}
        if owner.get('release_id')==release['version_id']:rebound['release_id']=version_id
        rebound['history']=[*owner.get('history',[]),{'at':now,'status':owner['status'],'release_id':rebound.get('release_id'),'actor':'admin','reason':'job_change:'+action}]
        items.append(('account_task',owner['id'],rebound,db.revision('account_task',owner['id']),True))
    db.put_batch(items)
    # 以下在提交之后：变更已生效，出错只记日志，绝不把已生效的记录改成失败（否则待办会误报“未写入”）。
    from .logbook import log
    try:_refresh_plan(manager,task_id,prepared,bundle,version_id,base)
    except Exception as exc:
        log('state',type='job_change',task_id=task_id,case_id=cid,action=action,step='post_commit_failed',actor='admin',
            reason='变更已生效，但重写参考计划失败：'+(str(exc)[:300] or type(exc).__name__),version_id=version_id,job_change_id=record['id'])
    log('state',type='job_change',task_id=task_id,case_id=cid,action=action,step='applied',actor='admin',reason='job_change:'+action,version_id=version_id,job_change_id=record['id'])
    return record


def _refresh_plan(manager,task_id,prepared,bundle,version_id,base):
    """任务修订号变了，参考计划签名随之过期；按新任务重写一条同内容计划，避免重启时因计划过期无法启动。"""
    from .task_planning import planning_signature
    definition=copy.deepcopy(prepared)
    definition['planning_context']=manager.planning.definition(task_id).get('planning_context',{})
    cases=[manager.cases.load(c) for c in prepared['config']['case_ids']]
    plan={'id':uuid.uuid4().hex,'task_id':task_id,'task_revision':prepared['revision'],'signature':planning_signature(definition,cases),'origin':'job_change',
          'plan':copy.deepcopy(bundle['plan']),'at':time.time(),'verification':'simulation_passed','release_id':version_id,'test_id':base.get('test_id')}
    manager.db.put('plan',plan['id'],plan,history=True)


def changes(manager,task_id):
    """任务的岗位变更记录，新的在前；同时结算已结束的验收。"""
    manager.definitions.get(task_id);settle(manager)
    return sorted((r for r in manager.db.list(KIND) if r['task_id']==task_id),key=lambda r:r['at'],reverse=True)


def describe(manager,record):
    """事件与待办用的中文说明：动作 +「岗位名」。"""
    label=ACTIONS[record['action']]
    if record.get('case_id'):label+='「'+_label(manager,record['case_id'])+'」'
    elif record['action']=='limit':label+='为 '+str(record['payload']['limit'])
    return label



def overview_context(db,cases,day=None):
    """各任务岗位视图共用的账号级读取（每次状态轮询只做一次）：变更记录、隔离验收状态、执行器任务状态、各岗位与账号当天首聊占位。"""
    from types import SimpleNamespace
    from .autonomy import day_key
    from .autopilot import daily_attempts
    from .job_quota import usage_by_case
    day=day or day_key();store=SimpleNamespace(list=lambda:cases)
    return {'day':day,'changes':db.list(KIND),'tests':{t['id']:t.get('status') for t in db.list('agent_test')},'members':{m['id']:m.get('status') for m in db.list('account_task')},
            'usage':usage_by_case(store,[c['id'] for c in cases],day),'account_used':daily_attempts(store,'first_contact',day)}


def overview(db,task,cases,context=None):
    """任务岗位区的只读视图（随 /api/state 轮询，不结算、不写任何记录）：额度、每个岗位的权重/首聊状态/份额/已用/进行中会话/验收，及最近的岗位变更。

    验收已结束但还未结算的加入请求（如服务重启中断）显示为 interrupted，不当作仍在验收。额度只对账号执行器里运行或暂停的任务计算；
    没有任务级执行设置的旧任务按各岗位自己的上限执行，只给账号已用。
    """
    from .agent_workspace import validation_state
    from .job_quota import ACCOUNT_DAILY_CAP,task_quota
    ctx=context or overview_context(db,cases)
    tid=task['id'];by_id={c['id']:c for c in cases}
    def label(cid):return (by_id.get(cid) or {}).get('job',{}).get('label') or cid
    try:release=db.get('agent_release',tid)
    except FileNotFoundError:release={}
    _,validated,shared=validation_state(db,tid)
    records=sorted((r for r in ctx['changes'] if r['task_id']==tid),key=lambda r:r['at'],reverse=True)[:20]
    def status(r):
        if r['status']=='testing' and ctx['tests'].get(r.get('test_id')) not in ('queued','running','passed'):return 'interrupted'
        return r['status']
    changes=[{'id':r['id'],'action':r['action'],'case_id':r.get('case_id'),'label':label(r['case_id']) if r.get('case_id') else '','status':status(r),
              'error':r.get('error'),'payload':r.get('payload') or {},'at':r['at']} for r in records]
    view={'validation':release.get('validation'),'shared':shared,'changes':changes,'jobs':[]}
    try:members=job_members.normalize(copy.deepcopy(task['config']))
    except ValueError as exc:return {**view,'error':str(exc)}
    shares=usage=None;quota=None
    if 'execution' not in task['config']:
        quota={'per_job':True,'account_used':ctx['account_used'],'account_cap':ACCOUNT_DAILY_CAP};usage=ctx['usage']
    elif ctx['members'].get(tid) in ('running','paused'):
        quota=task_quota(None,task,ctx['day'],usage=ctx['usage'],account_used=ctx['account_used']);shares,usage=quota.pop('shares'),quota.pop('usage')
    count=lambda case:sum(p.get('status') not in TERMINAL and bool(p.get('conversation')) and not p.get('alias_of') for p in case['candidates'].values())
    view['jobs']=[{'case_id':m['case_id'],'label':label(m['case_id']),'weight':m['weight'],'outreach':m['outreach'],'share':shares.get(m['case_id'],0) if shares is not None else None,
                   'used':usage.get(m['case_id'],0) if usage is not None else None,'open_conversations':count(by_id[m['case_id']]) if m['case_id'] in by_id else 0,
                   'validated':m['case_id'] in validated} for m in members]
    return {**view,'quota':quota} if quota else view
