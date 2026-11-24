"""Accept candidate-initiated phone/WeChat permission, independently of outreach."""
import asyncio,time,uuid,re
from . import boss_chat
from .sourcing import SourcingError,TERMINAL
from .action_gateway import ActionLedger,ActionBlocked,digest

INSPECT_JS=r'''(()=>{
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const path=e=>{const a=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){a.unshift('#'+CSS.escape(e.id));break;}const s=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];a.unshift(e.tagName.toLowerCase()+(s.length>1?':nth-of-type('+(s.indexOf(e)+1)+')':''));}return a.join(' > ')};
 const found=[];
 for(const leaf of Array.from(document.querySelectorAll('body *')).filter(e=>!e.children.length&&visible(e)&&/我想要?和您交换(?:微信|电话)[，,]?\s*您是否同意/.test(e.innerText||''))){
  let e=leaf;
  for(let i=0;e&&i<6;i++,e=e.parentElement){
   const text=(e.innerText||'').trim();if(text.length>260)break;
   const enabled=b=>visible(b)&&!b.disabled&&b.getAttribute('aria-disabled')!=='true'&&!/(^|[\s-])disabled([\s-]|$)/.test(b.className||'');
   const buttons=Array.from(e.querySelectorAll('button,a,[role="button"],[class*="btn"]')).filter(enabled);
   const agree=buttons.filter(b=>(b.innerText||'').trim()==='同意'),refuse=buttons.filter(b=>(b.innerText||'').trim()==='拒绝');
   if(agree.length===1&&refuse.length===1){found.push({kind:/交换微信/.test(text)?'accept_wechat':'accept_phone',text,selector:path(agree[0]),request_id:e.getAttribute('data-mid')||e.getAttribute('data-msgid')||e.id||'',history:!!e.closest('.message-item,.item-message')});break;}
  }
 }
 const requests=[];
 for(const kind of ['accept_wechat','accept_phone']){const matches=Array.from(new Map(found.filter(r=>r.kind===kind).map(r=>[r.selector,r])).values()),footer=matches.filter(r=>!r.history);const chosen=footer.length?footer:matches;if(chosen.length===1)requests.push(chosen[0]);}
 const accepted=Array.from(document.querySelectorAll('.message-item,.item-message')).filter(e=>/已同意|已交换/.test(e.innerText||'')&&/微信|电话/.test(e.innerText||'')).map(e=>({text:(e.innerText||'').trim().slice(0,500),id:e.getAttribute('data-mid')||e.getAttribute('data-msgid')||e.id||''}));
 return {contact_requests:requests,contact_accepted:accepted};
})()'''


def needs_sync(case,person,now=None):
 if not person or person['status'] in TERMINAL or not case.get('execution',{}).get('accept_contact_requests',True):return False
 now=time.time() if now is None else now
 for word,kind,field in [('微信','accept_wechat','wechat'),('电话','accept_phone','phone')]:
  if any(c['kind']==field for c in person.get('contact_records',[])):continue
  if not any(re.search(r'我想要?和您交换'+word+r'[，,]?\s*您是否同意',m.get('text','')) for m in person.get('inbox',[])+person.get('history',[])):continue
  previous=[a for a in person.get('platform_actions',[]) if a.get('kind')==kind]
  if previous and previous[-1]['status']=='obtained':continue
  if previous and previous[-1]['status'] in ('claimed','awaiting_verification','accepted') and now-person.get('last_sync_at',0)<300:continue
  return True
 return False

def available(case,person,controls):
 if not case.get('execution',{}).get('accept_contact_requests',True) or person['status'] in TERMINAL:return {}
 result={}
 for request in controls.get('contact_requests',[]):
  kind=request['kind'];field='wechat' if kind=='accept_wechat' else 'phone'
  if any(c['kind']==field for c in person.get('contact_records',[])):continue
  if any(a.get('kind')==kind and a['status']!='not_executed' for a in person.get('platform_actions',[])):continue
  result[kind]='同意候选人主动交换'+('微信' if field=='wechat' else '电话')+'，仅点击一次；实际号码读取后才记为已获取'
 return result


def reconcile(store,case,person,controls):
 changed=False
 for action in person.get('platform_actions',[]):
  if action.get('kind') not in ('accept_wechat','accept_phone'):continue
  field='wechat' if action['kind']=='accept_wechat' else 'phone';word='微信' if field=='wechat' else '电话'
  obtained=any(c['kind']==field for c in person.get('contact_records',[]))
  accepted=[r for r in controls.get('contact_accepted',[]) if word in r['text']]
  if obtained and action['status']!='obtained':action.update(status='obtained',verified_at=time.time(),evidence='同会话已读取候选人实际'+word);changed=True
  elif accepted and action['status'] in ('claimed','awaiting_verification'):
   new=[r for r in accepted if r not in action.get('accepted_before',[])]
   if new:action.update(status='accepted',verified_at=time.time(),evidence='平台显示已同意交换'+word);changed=True
  if action['status'] in ('accepted','obtained') and action.get('operation_id'):
   ledger=ActionLedger(store.db);op=ledger.get(action['operation_id'])
   if op['state'] in ('executing','unknown'):ledger.transition(op['id'],'verified',evidence=action['evidence'],verification='incoming_permission');changed=True
 if changed:store.audit(case,'contact_permission_verified',candidate_id=person['id'],reason='同意交换与实际联系方式分别核验，资料只保存本机')


async def execute(manager,job,session,case,person,kind):
 from .boss_resume import inspect_permissions
 from .boss_requests import reconcile as reconcile_contacts
 await manager.checkpoint(job)
 await boss_chat.read_conversation(session,**person['conversation'])
 controls=await inspect_permissions(session)
 latest=manager.cases.load(case['id']);person=manager.cases.candidate(latest,person['id'])
 reconcile_contacts(manager.cases,latest,person,controls);reconcile(manager.cases,latest,person,controls)
 if not latest.get('autonomy',{}).get('enabled') or latest['autonomy']['revision']!=case['autonomy']['revision']:return 'blocked'
 if job.get('account_runner') and not manager.account_execution.can_send(case['id']):return 'blocked'
 if kind not in available(latest,person,controls):return 'not_available'
 request=next(r for r in controls['contact_requests'] if r['kind']==kind)
 fingerprint=digest([person['conversation']['chat_id'],kind,request['request_id'],request['text']])
 ledger=ActionLedger(manager.cases.db)
 try:op=ledger.claim(task_id='case:'+case['id'],resource='candidate:'+person['id']+':incoming-permission',capability=kind,identity=fingerprint,run_id=job['id'],expectation={'case_id':case['id'],'candidate_id':person['id']})
 except ActionBlocked:return 'blocked'
 action={'id':uuid.uuid4().hex,'kind':kind,'fingerprint':fingerprint,'operation_id':op,'status':'claimed','at':time.time(),'accepted_before':controls.get('contact_accepted',[])}
 person.setdefault('platform_actions',[]).append(action);manager.cases.audit(latest,'contact_permission_claimed',candidate_id=person['id'],action_id=action['id'],request_kind=kind)
 started=False
 async def click():
  nonlocal started
  await boss_chat.read_conversation(session,**person['conversation'])
  check=manager.cases.load(case['id']);p=manager.cases.candidate(check,person['id'])
  if not check.get('autonomy',{}).get('enabled') or check['autonomy']['revision']!=case['autonomy']['revision'] or p['status'] in TERMINAL:raise SourcingError('同意前授权或候选人状态已变化')
  if job.get('account_runner') and not manager.account_execution.can_send(case['id']):raise SourcingError('岗位已暂停，未同意交换')
  current=await inspect_permissions(session)
  matches=[r for r in current.get('contact_requests',[]) if r==request]
  if len(matches)!=1:raise SourcingError('主动交换申请已变化，未同意')
  loc=session.page.locator(request['selector'])
  if await loc.count()!=1:raise SourcingError('交换同意按钮不唯一')
  ledger.transition(op,'executing');started=True
  await asyncio.wait_for(loc.click(timeout=5000),timeout=12)
 try:
  await session._act('click',click)
  for _ in range(3):
   await boss_chat.read_conversation(session,**person['conversation']);current=await inspect_permissions(session)
   latest=manager.cases.load(case['id']);p=manager.cases.candidate(latest,person['id'])
   reconcile_contacts(manager.cases,latest,p,current);reconcile(manager.cases,latest,p,current)
   a=next(a for a in p['platform_actions'] if a['id']==action['id'])
   if a['status'] in ('accepted','obtained'):return a['status']
   await asyncio.sleep(1)
 finally:
  latest=manager.cases.load(case['id']);p=manager.cases.candidate(latest,person['id']);a=next(a for a in p['platform_actions'] if a['id']==action['id'])
  if a['status']=='claimed':
   a['status']='awaiting_verification' if started else 'not_executed'
   ledger.transition(op,'unknown' if started else 'failed',reason='交换同意结果待核实，不重复点击' if started else '同意前校验未通过')
   manager.cases.audit(latest,'contact_permission_unknown',candidate_id=p['id'],action_id=a['id'])
 return 'awaiting_verification'
