"""One authenticated BOSS account owns one serialized browser executor.

Membership is durable; process/run ownership is live and never inferred from a
saved 'running' flag. Other sites keep their existing isolated task profiles.
"""
import time

BOSS_ENV = 'boss:default'

class AccountExecution:
    def __init__(self,db):self.db=db
    def members(self):return self.db.list('account_task')
    def set_task(self,task_id,case_ids,status,*,actor='system',reason=None):
        """actor：admin（工作台操作）或 system（执行器自己）；reason 写进状态历史，日报和日志据此说明暂停原因。"""
        if status not in ('running','paused','stopped','completed'):raise ValueError('任务状态不合法')
        prior=next((m for m in self.members() if m['id']==task_id),{})
        item={**prior,'id':task_id,'environment_id':BOSS_ENV,'case_ids':list(case_ids),'status':status,'updated_at':time.time()}
        if status=='running':
            try:item['release_id']=self.db.get('agent_release',task_id)['version_id']
            except FileNotFoundError:pass
        item['history']=[*prior.get('history',[]),{'at':time.time(),'status':status,'actor':actor,'reason':reason}]
        self.db.put('account_task',task_id,item)
        from .logbook import log
        log('state',type='task_status',task_id=task_id,case_ids=list(case_ids),before=prior.get('status'),after=status,
            actor=actor,reason=reason,release_id=item.get('release_id'))
        return item
    def case_ids(self):return list(dict.fromkeys(cid for m in self.members() if m['status']=='running' for cid in m['case_ids']))
    def availability(self,cid,now=None):
        from .task_policy import window_status
        now=time.time() if now is None else now
        if any(cid in r.get('case_ids',[]) and r['status'] in ('queued','running','failed') for r in self.db.list('planning_request')):return {'allowed':False,'state':'planning','reason':'等待任务规划完成','next_at':None}
        member=next((m for m in self.members() if m['status']=='running' and cid in m['case_ids']),None)
        if not member:return {'allowed':False,'state':'paused','reason':'任务已暂停','next_at':None}
        if member.get('release_id'):
            try:release=self.db.get('agent_release',member['id'])
            except FileNotFoundError:release=None
            if release and release['version_id']!=member['release_id']:return {'allowed':False,'state':'release_changed','reason':'发布版本已变化，请暂停并恢复任务以绑定新版本','next_at':None}
            # 绑定的就是生效版本：再逐岗位核对验收仍有效（代码、模型、任务设置或该岗位事实变了即失效），只停失效的岗位。
            from .agent_workspace import validated_cases
            if release and cid not in validated_cases(self.db,member['id']):return {'allowed':False,'state':'job_not_validated','reason':'该岗位验收已失效，只停这个岗位','next_at':None}
        try:task=self.db.get('task',member['id'])
        except FileNotFoundError:return {'allowed':True,'state':'open','next_at':None}
        # Legacy definitions are upgraded when saved; new tasks default to work hours.
        if 'schedule' not in task['config']:return {'allowed':True,'state':'open','next_at':None}
        state=window_status(task['config']['schedule'],now)
        if not state['allowed']:return state
        if member.get('rest_until',0)>now:return {'allowed':False,'state':'resting','reason':'执行器休息中，保留登录资料','next_at':member['rest_until']}
        return state
    def can_send(self,cid):
        state=self.availability(cid)
        return bool(state and state['allowed'])
    def begin_work(self,task_id,now=None):
        now=time.time() if now is None else now
        member=next((m for m in self.members() if m['id']==task_id),None)
        if not member:return
        # A lunch/night/manual pause already supplies rest; do not immediately
        # schedule another break from yesterday's work timer on resumption.
        rested=member.get('last_cycle_at') and now-member['last_cycle_at']>=600
        if not member.get('work_started_at') or rested or member.get('rest_until',0) and member['rest_until']<=now:
            import random
            # 每段连续工作时长按配置值的 0.75～1.25 倍随机，避免每段都恰好工作同样分钟数。
            member.update(work_started_at=now,rest_until=0,work_factor=random.uniform(0.75,1.25));self.db.put('account_task',task_id,member)
    def finish_cycle(self,task_id,now=None):
        import random
        now=time.time() if now is None else now
        try:task=self.db.get('task',task_id)
        except FileNotFoundError:return
        cfg=task['config'].get('schedule')
        if not cfg or not cfg.get('enabled',True):return
        member=next((m for m in self.members() if m['id']==task_id),None)
        if member and member.get('work_started_at') and now-member['work_started_at']>=cfg['work_minutes']*60*member.get('work_factor',1):
            member.update(rest_until=now+random.uniform(cfg['rest_min_minutes'],cfg['rest_max_minutes'])*60,work_started_at=None)
        if member:
            member['last_cycle_at']=now
            self.db.put('account_task',task_id,member)
    def view(self,owner,browser_alive):
        return {'id':BOSS_ENV,'label':'BOSS · 当前招聘账号','owner_run_id':owner.get('id') if owner else None,
                'active_case_id':owner.get('active_case_id') if owner else None,
                'executor_heartbeat_at':owner.get('executor_heartbeat_at') if owner else None,
                'last_business_action_at':owner.get('events',[])[-1]['at'] if owner and owner.get('events') else None,
                'session_policy':'保留窗口与登录资料；恢复前核验，过期转登录待办',
                'status':'standby' if owner and not owner.get('manual_takeover') and not any(m['status']=='running' for m in self.members()) else 'manual' if owner and owner.get('manual_takeover') else 'running' if owner and owner.get('status') in ('running','waiting') else 'idle',
                'browser_alive':browser_alive,'browser_url':'http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale',
                'members':[{**m,'availability':self.availability(m['case_ids'][0]) if m['case_ids'] else None} for m in self.members()]}
