"""Once-only native resume/contact requests. All page/contact data stays local."""
import asyncio,time,uuid,re
from . import boss_chat
from .sourcing import SourcingError,TERMINAL
from .action_gateway import ActionLedger,ActionBlocked

BUTTONS={'ask_resume':'求简历','exchange_phone':'换电话','exchange_wechat':'换微信'}
FIELDS={'ask_resume':'resume','exchange_phone':'phone','exchange_wechat':'wechat'}
INSPECT_JS=r'''(()=>{
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const path=e=>{const a=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){a.unshift('#'+CSS.escape(e.id));break;}const s=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];a.unshift(e.tagName.toLowerCase()+(s.length>1?':nth-of-type('+(s.indexOf(e)+1)+')':''));}return a.join(' > ')};
 const buttons={};
 for(const [kind,label] of Object.entries({ask_resume:'求简历',exchange_phone:'换电话',exchange_wechat:'换微信'})){
  const matches=Array.from(document.querySelectorAll('button,a,[role="button"],[class*="btn"]')).filter(e=>visible(e)&&(e.innerText||'').trim()===label&&!e.closest('.message-item,.item-message,.geek-item,[role="dialog"],.dialog,.modal'));
  if(matches.length===1){const e=matches[0];buttons[kind]={selector:path(e),enabled:!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&!/(^|[\s-])disabled([\s-]|$)/.test(e.className||'')};}
 }
 const cards=Array.from(document.querySelectorAll('.message-item,.item-message')).filter(e=>!e.querySelector('.message-item,.item-message')).filter(e=>!e.querySelector('.text-content,.message-text')||e.querySelector('.card,.exchange-card,.phone-card,.wechat-card,.resume-card')).map(e=>({id:e.getAttribute('data-mid')||e.getAttribute('data-msgid')||e.id||'',text:(e.innerText||'').trim().slice(0,1000),out:!!e.querySelector('.item-myself')||/(^|\s)(sent|self|mine|right|item-myself)(\s|$)/.test((e.className||'')+' '+(e.parentElement?.className||'')),incoming:!!e.querySelector('.item-friend')||/(^|\s)(received|other|left|item-friend)(\s|$)/.test((e.className||'')+' '+(e.parentElement?.className||''))}));
 const dialogs=Array.from(document.querySelectorAll('[role="dialog"],.dialog,.modal,.dialog-wrap,.dialog-container')).filter(e=>visible(e)&&!e.querySelector('[role="dialog"],.dialog,.modal,.dialog-wrap,.dialog-container'));
 const confirmations={};
 for(const [kind,word] of Object.entries({ask_resume:'简历',exchange_phone:'电话',exchange_wechat:'微信'})){
  const matches=dialogs.filter(e=>(e.innerText||'').includes(word)&&!/购买|支付|充值|开通|消耗|扣除/.test(e.innerText||''));
  if(matches.length===1){const buttons=Array.from(matches[0].querySelectorAll('button,a,[role="button"],[class*="btn"]')).filter(e=>visible(e)&&!e.disabled&&/^(确定|确认|发送|发送请求|确认交换|立即交换|交换电话|交换微信|索要简历|求简历)$/.test((e.innerText||'').trim()));if(buttons.length===1)confirmations[kind]={selector:path(buttons[0])};}
 }
 return {buttons,cards,confirmations};
})()'''

async def inspect(session):return await boss_chat.evaluate(session,INSPECT_JS)

def receipt_kind(text):
 if re.search(r'(?:请求|申请|希望|想|邀请|交换|索要).{0,15}(?:电话|手机)|(?:电话|手机).{0,12}(?:请求|申请|交换)',text):return 'exchange_phone'
 if re.search(r'(?:请求|申请|希望|想|邀请|交换|索要).{0,15}微信|微信.{0,12}(?:请求|申请|交换)',text):return 'exchange_wechat'
 if re.search(r'(?:请求|申请|希望|想|邀请|索要).{0,15}(?:发送|提供|获取|查看)?.{0,5}简历',text):return 'ask_resume'
 return None

def reconcile(store,case,person,observed):
 changed=False
 for value in person.get('contacts',[]):
  field='phone' if re.fullmatch(r'1[3-9]\d{9}',value) else None
  if field and not any(c['value']==value for c in person.setdefault('contact_records',[])):
   person['contact_records'].append({'kind':field,'value':value,'source':'conversation_text','at':time.time()});changed=True
 for card in observed.get('cards',[]):
  text=card.get('text','');kind=receipt_kind(text)
  # Existing outbound request cards are imported to prevent repeat requests.
  if kind and card.get('out') and not any(a.get('kind')==kind for a in person.get('platform_actions',[])):
   person.setdefault('platform_actions',[]).append({'id':uuid.uuid4().hex,'kind':kind,'status':'pending','at':time.time(),'source':'platform_history','evidence':text,'receipt_id':card.get('id')});changed=True
  # Never treat our own phone number / WeChat in an outgoing card as theirs.
  if card.get('incoming'):
   values=[]
   values += [('phone',v) for v in re.findall(r'(?:手机号|手机|电话)\s*(?:[:：]\s*|\s+)(1[3-9]\d{9})(?!\d)',text)]
   values += [('wechat',v) for v in re.findall(r'(?:微信账号|微信号?)\s*(?:[:：]\s*|\s+)([A-Za-z][A-Za-z0-9_-]{4,19})(?![A-Za-z0-9_-])',text)]
   for field,value in values:
    if value not in person.setdefault('contacts',[]):person['contacts'].append(value);changed=True
    if not any(c['value']==value for c in person.setdefault('contact_records',[])):
     person['contact_records'].append({'kind':field,'value':value,'source':'platform_incoming_card','receipt_id':card.get('id'),'at':time.time()});changed=True
 for action in person.get('platform_actions',[]):
  if action.get('kind') not in BUTTONS:continue
  kind=action['kind'];field=FIELDS[kind]
  obtained=bool(person.get('resumes')) if field=='resume' else any(c['kind']==field for c in person.get('contact_records',[]))
  matching=[c for c in observed.get('cards',[]) if c.get('out') and receipt_kind(c.get('text',''))==kind]
  if obtained and action['status']!='obtained':action.update(status='obtained',verified_at=time.time(),evidence='实际附件简历已到达' if field=='resume' else '已读取候选人实际联系方式');changed=True
  elif matching and re.search(r'已拒绝|对方拒绝',matching[-1]['text']) and action['status']!='declined':
   action.update(status='declined',verified_at=time.time(),evidence=matching[-1]['text']);changed=True
  elif matching and action['status'] in ('claimed','awaiting_verification'):
   action.update(status='pending',verified_at=time.time(),evidence=matching[-1]['text'],receipt_id=matching[-1].get('id'));changed=True
  if action.get('operation_id') and action['status'] in ('pending','obtained','declined'):
   ledger=ActionLedger(store.db);op=ledger.get(action['operation_id'])
   if op['state'] in ('executing','unknown'):ledger.transition(op['id'],'verified',evidence=action['evidence'],verification='platform_request_card');changed=True
 if changed:store.audit(case,'platform_request_synced',candidate_id=person['id'],reason='申请回执与实际资料分别核验，申请不计作获取成功')


def available(case,person,observed):
 if not case.get('execution',{}).get('request_resume',True):return {}
 if not person.get('inbox') or person.get('status') in TERMINAL:return {}
 from .autonomy import intent_labels,day_key
 if set(intent_labels(person['inbox'][-1]['text']))&{'decline','later'}:return {}
 count=sum(1 for m in person.get('outbox',[]) if m.get('claimed_at') and day_key(m['claimed_at'])==day_key())+sum(1 for a in person.get('platform_actions',[]) if a.get('at') and day_key(a['at'])==day_key())
 if count>=case.get('autonomy',{}).get('max_messages_per_candidate_day',6):return {}
 if any(a.get('kind') in BUTTONS for a in person.get('platform_actions',[])):
  # One outstanding channel at a time; never switch channels to bypass refusal.
  if any(a.get('kind') in BUTTONS and a['status']!='obtained' for a in person['platform_actions']):return {}
 result={}
 for kind in BUTTONS:
  if kind!='ask_resume' and (not case.get('execution',{}).get('request_contacts',False) or person.get('resumes')):continue
  if any(a.get('kind')==kind for a in person.get('platform_actions',[])):continue
  if kind=='ask_resume' and person.get('resumes'):continue
  if kind!='ask_resume' and person.get('contacts'):continue
  if observed.get('buttons',{}).get(kind,{}).get('enabled'):result[kind]='通过平台'+BUTTONS[kind]+'按钮发起一次申请；等待同意，实际资料到达才计入结果'
 return result

async def execute(manager,job,session,case,person,kind):
 if kind not in BUTTONS:raise SourcingError('未知资料申请动作')
 await manager.checkpoint(job)
 snapshot=await boss_chat.read_conversation(session,**person['conversation'])
 controls=await inspect(session)
 fresh=manager.cases.load(case['id']);person=manager.cases.candidate(fresh,person['id'])
 from .autonomy import intent_labels
 if not fresh.get('autonomy',{}).get('enabled') or fresh.get('config_revision')!=case.get('config_revision'):return 'blocked'
 if job.get('account_runner') and not manager.account_execution.can_send(case['id']):return 'blocked'
 if person.get('inbox') and any(x in intent_labels(person['inbox'][-1]['text']) for x in ('decline','later')):return 'blocked'
 reconcile(manager.cases,fresh,person,controls)
 if kind not in available(fresh,person,controls):return 'not_available'
 from .receipt_policy import continuation
 has_unknown=any(m['status'] in ('sending','delivery_unknown') for m in person.get('outbox',[]))
 if has_unknown and kind not in continuation(person,snapshot):return 'blocked'
 # The old send lease remains locked. A distinct, explicitly requested contact
 # exchange uses its own lease and can never authorize another text send.
 resource='candidate:'+person['id']+':platform-request'
 ledger=ActionLedger(manager.cases.db)
 try:op=ledger.claim(task_id='case:'+case['id'],resource=resource,capability=kind,identity=kind,run_id=job['id'],expectation={'case_id':case['id'],'candidate_id':person['id']})
 except ActionBlocked:return 'blocked'
 action={'id':uuid.uuid4().hex,'kind':kind,'status':'claimed','operation_id':op,'at':time.time()}
 person.setdefault('platform_actions',[]).append(action);manager.cases.audit(fresh,'platform_request_claimed',candidate_id=person['id'],action_id=action['id'],request_kind=kind)
 started=False
 async def click():
  nonlocal started
  current_snapshot=await boss_chat.read_conversation(session,**person['conversation'])
  latest=manager.cases.load(case['id']);p=manager.cases.candidate(latest,person['id'])
  if not latest.get('autonomy',{}).get('enabled') or latest.get('config_revision')!=case.get('config_revision') or p['status'] in TERMINAL:raise SourcingError('申请授权或状态已变化')
  if job.get('account_runner') and not manager.account_execution.can_send(case['id']):raise SourcingError('岗位已暂停')
  if any(m['status'] in ('sending','delivery_unknown') for m in p.get('outbox',[])) and kind not in continuation(p,current_snapshot):raise SourcingError('后续意向已变化，保留旧消息防重发保护')
  current=await inspect(session);button=current.get('buttons',{}).get(kind)
  if not button or not button['enabled']:raise SourcingError('申请按钮不可用')
  loc=session.page.locator(button['selector'])
  if await loc.count()!=1:raise SourcingError('申请按钮不唯一')
  ledger.transition(op,'executing');started=True
  await asyncio.wait_for(loc.click(timeout=5000),timeout=12)
 try:
  await session._act('click',click)
  after=await inspect(session)
  confirmation=after.get('confirmations',{}).get(kind)
  if confirmation:
   await manager.checkpoint(job)
   async def confirm():
    await boss_chat.read_conversation(session,**person['conversation'])
    latest=manager.cases.load(case['id'])
    if latest.get('config_revision')!=case.get('config_revision') or not latest.get('autonomy',{}).get('enabled'):raise SourcingError('确认前授权发生变化')
    if job.get('account_runner') and not manager.account_execution.can_send(case['id']):raise SourcingError('确认前任务已暂停')
    now=(await inspect(session)).get('confirmations',{}).get(kind)
    if now!=confirmation:raise SourcingError('申请确认框已变化')
    loc=session.page.locator(now['selector'])
    if await loc.count()!=1:raise SourcingError('申请确认按钮不唯一')
    await asyncio.wait_for(loc.click(timeout=5000),timeout=12)
   await session._act('click',confirm)
  for _ in range(3):
   await boss_chat.read_conversation(session,**person['conversation']);current=await inspect(session)
   latest=manager.cases.load(case['id']);p=manager.cases.candidate(latest,person['id']);reconcile(manager.cases,latest,p,current)
   a=next(a for a in p['platform_actions'] if a['id']==action['id'])
   if a['status'] in ('pending','obtained'):return 'verified_request' if a['status']=='pending' else 'obtained'
   await asyncio.sleep(1)
 finally:
  latest=manager.cases.load(case['id']);p=manager.cases.candidate(latest,person['id']);a=next(a for a in p['platform_actions'] if a['id']==action['id'])
  if a['status']=='claimed':
   a['status']='awaiting_verification' if started else 'not_executed'
   ledger.transition(op,'unknown' if started else 'failed',reason='申请结果未核实，禁止重复' if started else '点击前校验未通过')
   manager.cases.audit(latest,'platform_request_unknown' if started else 'platform_request_blocked',candidate_id=p['id'],action_id=a['id'])
 return 'awaiting_verification'
