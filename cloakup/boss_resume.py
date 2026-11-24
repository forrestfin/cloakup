"""Accept a candidate-initiated resume permission once, with local receipts.

An accepted permission is never counted as an obtained resume.
"""
import asyncio,hashlib,json,re,time,uuid
from . import boss_chat
from .sourcing import SourcingError,TERMINAL

REQUEST_TEXT='对方想发送附件简历给您'
CONTROLS_JS=r'''(()=>{
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const path=e=>{const parts=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){parts.unshift('#'+CSS.escape(e.id));break;}const tag=e.tagName.toLowerCase(),siblings=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];parts.unshift(tag+(siblings.length>1?':nth-of-type('+(siblings.indexOf(e)+1)+')':''));}return parts.join(' > ')};
 const norm=t=>(t||'').replace(/\s+/g,'');
 const phrase=/对方想发送(?:加密)?附件简历给您/;
 const choice=root=>{const labeled=Array.from(root.querySelectorAll('*')).filter(visible).filter(b=>/^(同意|拒绝)$/.test(norm(b.innerText)));const leaves=labeled.filter(b=>!labeled.some(n=>n!==b&&b.contains(n)));return {agree:leaves.filter(b=>norm(b.innerText)==='同意'),refuse:leaves.filter(b=>norm(b.innerText)==='拒绝')};};
 // 整句经常被图标或高亮拆开，没有叶子节点能单独拼出「对方想发送附件简历」。
 // 先找包含整句的最小元素，再上溯到消息卡片里的同意、拒绝。
 const holders=Array.from(document.querySelectorAll('body *')).filter(e=>visible(e)&&phrase.test(norm(e.innerText)));
 const starts=holders.filter(e=>!holders.some(n=>n!==e&&e.contains(n)));
 const raw=[];
 for(const leaf of starts){
  // 左侧会话列表不算。正文卡片即使套在列表结构里，也要能点到同意。
  const inThread=leaf.closest('.message-item,.item-message');
  if(leaf.closest('.geek-item')&&!inThread)continue;
  const scopes=[];
  // 消息卡片本身不再按 200 字截断：正文加按钮经常超过这个长度，截断后就找不到同意。
  if(inThread)scopes.push(inThread);
  let e=leaf;
  for(let i=0;e&&i<8&&e!==inThread;i++,e=e.parentElement){scopes.push(e);if(!inThread&&((e.innerText||'').trim().length>200))break;}
  for(const scope of scopes){
   const text=(scope.innerText||'').trim();if(!text||(!inThread&&text.length>200))continue;
   const found=choice(scope);
   if(found.agree.length===1&&found.refuse.length===1){raw.push({kind:'resume_permission',text,el:found.agree[0],request_id:scope.getAttribute('data-id')||scope.id||''});break;}
  }
 }
 // 同一句申请会在列表和正文里各出现一次，正文里也可能有一份不可见副本。只点当前屏幕上、会话正文里最靠下的那一个同意。
 const onScreen=el=>{const r=el.getBoundingClientRect();return r.width>1&&r.height>1&&r.bottom>0&&r.right>0&&r.top<window.innerHeight&&r.left<window.innerWidth;};
 let chosen=raw.filter(r=>onScreen(r.el));
 const inMsg=chosen.filter(r=>r.el.closest('.message-item,.item-message'));
 if(inMsg.length)chosen=inMsg;
 if(chosen.length>1&&chosen.every(r=>norm(r.text)===norm(chosen[0].text))){
  chosen=[chosen.slice().sort((a,b)=>a.el.getBoundingClientRect().top-b.el.getBoundingClientRect().top).pop()];
 }
 const unique=Array.from(new Map(chosen.map(r=>[path(r.el),{kind:r.kind,text:r.text,selector:path(r.el),request_id:r.request_id}])).values());
 const accepted=Array.from(document.querySelectorAll('.message-item,.item-message')).filter(e=>/已同意/.test(e.innerText||'')&&/附件简历|发送简历|接收简历/.test(e.innerText||'')).map(e=>({text:(e.innerText||'').trim().slice(0,300),id:e.getAttribute('data-mid')||e.getAttribute('data-msgid')||e.id||''}));
 const debug=unique.length===1?[]:starts.slice(0,2).map(leaf=>{const box=leaf.closest('.message-item,.item-message')||leaf.parentElement||leaf;const bits=Array.from(box.querySelectorAll('*')).filter(e=>/同意|拒绝/.test(e.innerText||'')&&norm(e.innerText).length<=8).slice(0,8).map(e=>e.tagName+'|'+(typeof e.className==='string'?e.className:'').slice(0,30)+'|'+norm(e.innerText));return {inThread:!!leaf.closest('.message-item,.item-message'),tag:leaf.tagName,len:norm(leaf.innerText).length,bits};});
 return {requests:unique,accepted,debug,phrase_nodes:starts.length};
})()'''


def is_permission_text(text):return bool(re.fullmatch(r'(?:对方想发送(?:加密)?附件简历给您[，,]?\s*您是否同意|我想要?和您交换(?:微信|电话)[，,]?\s*您是否同意)\s*(?:拒绝\s*同意)?',text.strip()))


def is_resume_permission_text(text):
 """候选人主动申请把附件简历发给我们，等待点击同意。"""
 return bool(text and re.fullmatch(r'对方想发送(?:加密)?附件简历给您[，,]?\s*您是否同意\s*(?:拒绝\s*同意)?',text.strip()))


async def inspect_permissions(session):
    from .boss_requests import inspect
    from .boss_contact_consent import INSPECT_JS
    return {**await boss_chat.evaluate(session,CONTROLS_JS),**await inspect(session),**await boss_chat.evaluate(session,INSPECT_JS)}


def reconcile_permission(store,case,candidate,snapshot,controls):
    for action in candidate.get('platform_actions',[]):
        if action['kind']!='accept_resume' or action['status'] not in ('claimed','awaiting_verification'):continue
        files=[m for m in snapshot['messages'] if m.get('resume') and m['direction']=='in' and m['external_id'] not in action['before_message_ids']]
        pending=bool(controls['requests'])
        if files or (not pending and controls.get('accepted') and not action.get('accepted_before')):
            action.update(status='accepted',verified_at=time.time(),evidence='同会话出现新增附件简历' if files else '同会话的平台简历申请显示已同意')
            store.audit(case,'resume_permission_verified',candidate_id=candidate['id'],action_id=action['id'],evidence=action['evidence'])


async def accept_resume_request(manager,job,session,case,candidate,snapshot):
    if candidate['status'] in TERMINAL or not case.get('autonomy',{}).get('enabled'):return
    if not case.get('execution',{}).get('accept_resume_requests',True):return
    if job.get('account_runner') and not manager.account_execution.can_send(case['id']):return
    from .autonomy import intent_labels
    if candidate.get('inbox') and 'decline' in intent_labels(candidate['inbox'][-1]['text']):return
    controls=await inspect_permissions(session)
    reconcile_permission(manager.cases,case,candidate,snapshot,controls)
    if candidate.get('resumes') or len(controls['requests'])!=1:return
    request=controls['requests'][0]
    key=hashlib.sha256(json.dumps([candidate['conversation']['chat_id'],request['kind'],request['request_id'],request['text']],ensure_ascii=False).encode()).hexdigest()
    if any(a.get('fingerprint')==key for a in candidate.get('platform_actions',[])):return
    await manager.checkpoint(job)
    fresh_snapshot=await boss_chat.read_conversation(session,**candidate['conversation'])
    fresh_controls=await inspect_permissions(session)
    if fresh_controls['requests']!=controls['requests']:return
    latest=manager.cases.load(case['id']);person=manager.cases.candidate(latest,candidate['id'])
    if not latest.get('autonomy',{}).get('enabled') or latest['autonomy']['revision']!=case['autonomy']['revision']:return
    if job.get('account_runner') and not manager.account_execution.can_send(case['id']):return
    action={'id':uuid.uuid4().hex,'kind':'accept_resume','status':'claimed','fingerprint':key,'at':time.time(),
            'before_message_ids':[m['external_id'] for m in fresh_snapshot['messages']],'accepted_before':bool(fresh_controls['accepted'])}
    person.setdefault('platform_actions',[]).append(action)
    manager.cases.audit(latest,'resume_permission_claimed',candidate_id=person['id'],action_id=action['id'],reason='候选人主动申请发送附件简历，仅同意接收一次')
    async def click():
        await boss_chat.read_conversation(session,**person['conversation'])
        current=await inspect_permissions(session)
        if current['requests']!=fresh_controls['requests']:raise SourcingError('简历接收申请已变化，未点击')
        check=manager.cases.load(case['id'])
        if not check.get('autonomy',{}).get('enabled') or check['autonomy']['revision']!=case['autonomy']['revision']:raise SourcingError('授权已变化，未同意接收')
        loc=session.page.locator(current['requests'][0]['selector'])
        if await loc.count()!=1:raise SourcingError('同意接收按钮不唯一')
        await asyncio.wait_for(loc.click(timeout=5000),timeout=12)
    try:
        await session._act('click',click)
        for _ in range(3):
            snap=await boss_chat.read_conversation(session,**person['conversation']);current=await inspect_permissions(session)
            latest=manager.cases.load(case['id']);person=manager.cases.candidate(latest,candidate['id'])
            action=next(a for a in person['platform_actions'] if a['id']==action['id'])
            reconcile_permission(manager.cases,latest,person,snap,current)
            if action['status']=='accepted':break
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        manager.event(job,'step','简历接收结果待核实：'+type(exc).__name__)
    finally:
        latest=manager.cases.load(case['id']);person=manager.cases.candidate(latest,candidate['id'])
        action=next(a for a in person['platform_actions'] if a['id']==action['id'])
        if action['status']=='claimed':
            action['status']='awaiting_verification'
            manager.cases.audit(latest,'resume_permission_unknown',candidate_id=person['id'],action_id=action['id'],reason='接收操作已占位，未核实前不重复点击，也不计作已收简历')
