"""Durable action state and API/browser routing independent of model output."""
import hashlib,json,time
from dataclasses import dataclass
from sqlalchemy import select,update,delete
from sqlalchemy.exc import IntegrityError
from .storage import operations,action_leases
from .logbook import log

UNRESOLVED=('claimed','executing','unknown')
class ActionBlocked(ValueError):pass

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()

class ActionLedger:
 def __init__(self,db):self.db=db
 def get(self,ident):
  with self.db.engine.connect() as c:
   row=c.execute(select(operations).where(operations.c.id==ident)).mappings().first()
   if not row:raise FileNotFoundError('动作不存在')
   return {**dict(row),'payload':dict(row['payload'])}
 def list(self,task_id=None):
  query=select(operations).order_by(operations.c.updated_at.desc())
  if task_id:query=query.where(operations.c.task_id==task_id)
  with self.db.engine.connect() as c:return [dict(r) for r in c.execute(query).mappings()]
 def claim(self,*,task_id,resource,capability,identity,run_id=None,expectation=None):
  ident=digest([task_id,resource,capability,identity])
  payload={'identity_hash':digest(identity),'expectation':expectation,'created_at':time.time(),'history':[{'state':'prepared','at':time.time()},{'state':'claimed','at':time.time()}]}
  try:
   with self.db.engine.begin() as c:
    c.execute(action_leases.insert().values(id=digest([task_id,resource]),operation_id=ident))
    row=c.execute(select(operations.c.state,operations.c.payload).where(operations.c.id==ident)).mappings().first()
    old=row['state'] if row else None
    if row:payload['history']=row['payload'].get('history',[])+payload['history']
    if old is None:c.execute(operations.insert().values(id=ident,task_id=task_id,resource=resource,capability=capability,run_id=run_id,state='prepared',payload=payload,updated_at=time.time()))
    elif old!='failed':raise ActionBlocked('动作已经执行或待核实，不会重复执行')
    result=c.execute(update(operations).where(operations.c.id==ident,operations.c.state.in_(('prepared','failed'))).values(state='claimed',updated_at=time.time(),payload=payload))
    if result.rowcount!=1:raise ActionBlocked('动作已经被其他执行器占位')
  except IntegrityError as exc:
   log('action',operation_id=ident,task_id=task_id,run_id=run_id,resource=resource,capability=capability,state='blocked',reason='lease_held')
   raise ActionBlocked('该对象有执行中或结果未知的动作，先核验，不能重试') from exc
  except ActionBlocked as exc:
   log('action',operation_id=ident,task_id=task_id,run_id=run_id,resource=resource,capability=capability,state='blocked',reason=str(exc))
   raise
  log('action',operation_id=ident,task_id=task_id,run_id=run_id,resource=resource,capability=capability,state='claimed',expectation=expectation)
  return ident
 def transition(self,ident,state,**details):
  if state not in ('executing','verified','unknown','failed'):raise ValueError('动作状态不合法')
  allowed={'executing':('claimed',),'verified':('executing','unknown'),'unknown':('claimed','executing'),'failed':('claimed','executing','unknown')}
  if state=='verified' and not details.get('evidence'):raise ValueError('完成动作必须有核验依据')
  with self.db.engine.begin() as c:
   query=select(operations).where(operations.c.id==ident)
   if self.db.backend=='postgresql':query=query.with_for_update()
   old=c.execute(query).mappings().first()
   if not old or old['state'] not in allowed[state]:raise ActionBlocked('动作状态已变化，拒绝覆盖')
   payload={**old['payload'],**details,'history':old['payload'].get('history',[])+[{'state':state,'at':time.time(),**details}]}
   result=c.execute(update(operations).where(operations.c.id==ident,operations.c.state==old['state']).values(state=state,payload=payload,updated_at=time.time()))
   if result.rowcount!=1:raise ActionBlocked('动作状态已变化，拒绝覆盖')
   if state in ('verified','failed'):c.execute(delete(action_leases).where(action_leases.c.operation_id==ident))
  created=old['payload'].get('created_at')
  log('action',operation_id=ident,task_id=old['task_id'],run_id=old['run_id'],resource=old['resource'],capability=old['capability'],
      state=state,before=old['state'],elapsed_ms=int((time.time()-created)*1000) if created else None,
      details={k:v for k,v in details.items() if k in ('reason','evidence','error','stage','receipt','status')})
 def isolate_platform_requests(self):
  """Move unresolved native request locks; never change outcome or release dedup."""
  for operation in self.list():
   if operation['capability'] not in ('ask_resume','exchange_phone','exchange_wechat') or operation['state'] not in UNRESOLVED:continue
   old_resource=operation['resource']
   if old_resource.endswith(':platform-request'):continue
   base=old_resource.removesuffix(':contact-request');resource=base+':platform-request'
   with self.db.engine.begin() as c:
    query=select(operations).where(operations.c.id==operation['id'])
    if self.db.backend=='postgresql':query=query.with_for_update()
    current=c.execute(query).mappings().first()
    if current['resource']!=old_resource:continue
    payload={**current['payload'],'history':current['payload'].get('history',[])+[{'event':'resource_scope_isolated','at':time.time(),'old_resource':old_resource,'resource':resource,'reason':'平台申请未知仍禁止重复，正常问题答复独立校验'}]}
    c.execute(delete(action_leases).where(action_leases.c.operation_id==operation['id']))
    c.execute(action_leases.insert().values(id=digest([operation['task_id'],resource]),operation_id=operation['id']))
    c.execute(update(operations).where(operations.c.id==operation['id']).values(resource=resource,payload=payload,updated_at=time.time()))
 def recover(self,run_id):
  for operation in self.list():
   if operation['run_id']==run_id and operation['state'] in ('claimed','executing'):
    self.transition(operation['id'],'failed' if operation['state']=='claimed' else 'unknown',reason='占位后未进入执行阶段' if operation['state']=='claimed' else '服务重启，执行结果未核实')

@dataclass
class CapabilityAdapter:
 name:str
 transport:str
 available:object
 execute:object
 verify:object=None

class CapabilityRegistry:
 def __init__(self):self.routes={};self.schemas={};self.read_only=set()
 def register(self,capability,adapter,schema=None,read_only=False):
  if adapter.transport not in ('api','browser','human'):raise ValueError('未知传输方式')
  self.routes.setdefault(capability,[]).append(adapter)
  if schema:self.schemas[capability]=schema
  if read_only:self.read_only.add(capability)
 def choose(self,capability,context):
  if context.get('authorized') is not True:raise ActionBlocked('缺少操作授权')
  if context.get('assert_authorized'):context['assert_authorized']()
  options=sorted(self.routes.get(capability,[]),key=lambda a:{'api':0,'browser':1,'human':2}[a.transport])
  return next((a for a in options if a.available(context) is True),None)
 async def run(self,capability,args,context):
  adapter=self.choose(capability,context)
  if not adapter:raise ActionBlocked('当前没有已授权且可用的能力适配器')
  # A failed write never silently falls back to another transport.
  if context.get('assert_authorized'):context['assert_authorized']()
  result=await adapter.execute(capability,args,context)
  if adapter.verify and not result.is_error:
   receipt=await adapter.verify(context,args,result)
   result.detail.update(receipt_verified=bool(receipt.get('verified')),receipt_evidence=receipt.get('evidence'))
  result.detail['transport']=adapter.transport;result.detail['adapter']=adapter.name
  return result
