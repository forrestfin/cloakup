"""Explicit, local, read-only probes for an existing task browser.

Never type, save, download resumes or return candidate text. Only the
greeting-settings and job-page probes navigate; the job-page probe also opens
and closes the recommend job dropdown once without choosing an option.
"""
import asyncio
from functools import wraps
import time
import uuid
from urllib.parse import urlparse
from .sourcing import SourcingError

# 头部结构探针：只返回标签、类名、可见性与“是否等于选中行姓名”等布尔证据，不返回候选人原文。
HEADER_STRUCTURE_PROBE = r"""(()=>{
 const ZW=/[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]/g;
 const norm=v=>(v||'').replace(/\s+/g,'').replace(ZW,'').replace(/^[：:]+|[：:]+$/g,'');
 const selected=Array.from(document.querySelectorAll('.geek-item.selected[data-id]:not(.group-item)'));
 const rowName=selected.length===1?(selected[0].querySelector('.geek-name')?.innerText||''):'';
 const kinds=s=>{const c={length:s.length,cjk:0,ascii:0,private_use:0,zero_width:0,other:0};for(const ch of s){const p=ch.codePointAt(0);if(/[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]/.test(ch))c.zero_width++;else if(p>=0xe000&&p<=0xf8ff)c.private_use++;else if(/[\u4e00-\u9fff]/.test(ch))c.cjk++;else if(p<128)c.ascii++;else c.other++;}return c};
 const leaves=Array.from(document.querySelectorAll('#container *')).filter(e=>!e.children.length&&/沟通职位/.test(e.innerText||''));
 let header=null;
 for(const leaf of leaves){for(let e=leaf,i=0;e&&i<9;e=e.parentElement,i++){
  if(e.matches('.geek-item')||e.querySelector('.geek-item,.message-item,.item-message,[contenteditable="true"],textarea'))break;
  if((e.innerText||'').includes('在线简历')){header=e;break;}
 } if(header)break;}
 const out={selected_rows:selected.length,row_name:kinds(rowName),row_name_markup:selected.length===1?(selected[0].querySelector('.geek-name')?.children.length??null):null,header_found:!!header,nodes:[],truncated:false};
 if(!header||!rowName)return out;
 const target=norm(rowName);
 const walk=(node,depth)=>{
  if(out.nodes.length>=160){out.truncated=true;return;}
  if(node.nodeType===3){const raw=node.textContent||'';if(!raw.trim())return;const r=document.createRange();r.selectNodeContents(node);
   out.nodes.push({depth,type:'text',...kinds(raw),rendered:r.getClientRects().length>0,equals_name:norm(raw)===target,contains_name:norm(raw).includes(target)});return;}
  if(node.nodeType!==1)return;
  const e=node,st=getComputedStyle(e),rect=e.getBoundingClientRect(),text=typeof e.innerText==='string'?e.innerText:null;
  out.nodes.push({depth,type:'element',tag:e.tagName.toLowerCase(),classes:(typeof e.className==='string'?e.className:'').slice(0,80),
   attrs:Array.from(e.attributes).map(a=>a.name).filter(n=>n!=='class'&&n!=='style').slice(0,8),children:e.children.length,
   shadow:!!e.shadowRoot,display:st.display,visibility:st.visibility,font:st.fontFamily.slice(0,60),w:Math.round(rect.width),h:Math.round(rect.height),
   pseudo_before:getComputedStyle(e,'::before').content!=='none',
   text_equals_name:text!==null&&norm(text)===target,text_contains_name:text!==null&&norm(text).includes(target)});
  if(depth<10)for(const c of e.childNodes)walk(c,depth+1);
 };
 walk(header,0);
 return out;
})()"""

PROBE = """(()=>({selection_markers:Array.from(document.querySelectorAll('.geek-item[data-id]:not(.group-item)')).map(e=>({classes:e.className,aria_selected:e.getAttribute('aria-selected'),data_selected:e.getAttribute('data-selected')})),ready:document.readyState,rows:document.querySelectorAll('.geek-item[data-id]:not(.group-item)').length,editors:document.querySelectorAll('[contenteditable="true"],textarea').length,has_draft:Array.from(document.querySelectorAll('[contenteditable="true"],textarea')).some(e=>(e.value||e.innerText||'').trim().length>0)}))()"""


def _exclusive(fn):
    @wraps(fn)
    async def run(manager,*args,**kwargs):
        preflight=getattr(manager,'preflight_task',None)
        if preflight and not preflight.done():raise ValueError('环境与执行器核验进行中，请等待完成')
        account=getattr(manager,'account_execution',None)
        if account and any(m['status']=='running' for m in account.members()):
            raise ValueError('请先暂停账号任务，避免诊断与业务页面操作并发')
        lock=getattr(manager,'_browser_diagnostic_lock',None)
        if lock is None:
            lock=manager._browser_diagnostic_lock=asyncio.Lock()
        if lock.locked():raise ValueError('已有浏览器诊断正在执行，请等待完成')
        async with lock:
            result=await fn(manager,*args,**kwargs)
            result.update(id=uuid.uuid4().hex,checked_at=time.time(),browser_instance_id=getattr(getattr(manager,'boss_session',None),'instance_id',None),mode={'inspect':'page_read_only','greeting_settings':'greeting_settings_read_only','job_pages':'job_pages_read_only'}.get(fn.__name__,'existing_chat_selection_only'))
            if getattr(manager,'db',None):
                from .platform import environment_signature
                result['signature']=environment_signature()
                manager.db.put('executor_check',result['id'],result)
            return result
    return run


@_exclusive
async def inspect(manager, task_id):
    definition=manager.definitions.get(task_id)
    if definition['config']['engine']!='boss_sourcing':
        raise ValueError('当前诊断仅支持 BOSS 任务已有浏览器')
    session=manager.boss_session
    if not manager.browser_alive(session):
        return {'task_id':task_id,'browser_alive':False,'probes':[]}
    pages=list(session.context.pages)
    matches=[p for p in pages if urlparse(p.url).path.rstrip('/')=='/web/chat/index' and urlparse(p.url).hostname=='www.zhipin.com']
    report={'task_id':task_id,'browser_alive':True,'page_count':len(pages),'chat_page_count':len(matches),'probes':[]}
    if len(matches)!=1:
        report['reason']='chat_page_missing' if not matches else 'chat_page_ambiguous'
        return report
    page=matches[0]
    async def probe(name,fn,timeout=6):
        started=time.monotonic()
        try:
            value=await asyncio.wait_for(fn(),timeout)
            result={'name':name,'ok':True,'value':value}
        except Exception as exc:
            result={'name':name,'ok':False,'error_type':type(exc).__name__}
        result['duration_ms']=round((time.monotonic()-started)*1000)
        report['probes'].append(result)
        return result
    cdp=None
    async def connect():
        nonlocal cdp
        cdp=await session.context.new_cdp_session(page)
        return True
    try:
        connection=await probe('cdp_connection',connect)
        if connection['ok']:
            await probe('frame_tree',lambda:cdp.send('Page.getFrameTree'))
            # Keep the frame tree private: IDs and URLs are unnecessary for operators.
            report['probes'][-1].pop('value',None)
            async def main_read():
                result=await cdp.send('Runtime.evaluate',{'expression':PROBE,'returnByValue':True,'awaitPromise':False,'timeout':3000})
                if result.get('exceptionDetails'):raise RuntimeError('page_probe_failed')
                return result.get('result',{}).get('value')
            await probe('main_world_dom',main_read)
        from .boss_chat import _bound_read, RECOVERY_GUARD_JS,IDENTITY_READER_JS
        await probe('guarded_recovery_dom',lambda:_bound_read(page,RECOVERY_GUARD_JS),timeout=40)
        for cid in definition['config'].get('case_ids',[]):
            title=manager.cases.load(cid)['job']['label']
            import json
            expression='(()=>{const r=('+IDENTITY_READER_JS+')('+json.dumps(title)+');return {valid:r.valid,reason:r.reason_code,evidence:r.evidence};})()'
            await probe('conversation_identity',lambda:_bound_read(page,expression))  # noqa: B023 -- 立即 await，不会延迟绑定
        await probe('header_structure',lambda:_bound_read(page,HEADER_STRUCTURE_PROBE))
        world=getattr(page,'_stealth_world',None)
        if world:
            first=await probe('cloak_world_dom',lambda:world.evaluate(PROBE))
            if not first['ok']:
                world.invalidate()
                await probe('cloak_world_recreated_dom',lambda:world.evaluate(PROBE))
    finally:
        if cdp:
            try:await asyncio.wait_for(cdp.detach(),2)
            except Exception:pass
    return report


@_exclusive
async def verify_target(manager, task_id):
    """Explicit diagnostic capability: select one existing bound chat, never send.

    Requires all account tasks paused/stopped and no manual takeover. Uses the
    same humanized selection adapter as production, with an action allowlist.
    """
    from . import boss_chat
    definition=manager.definitions.get(task_id)
    if definition['config']['engine']!='boss_sourcing':raise ValueError('不支持该任务类型')
    def permitted():
        if any(m['status'] not in ('paused','stopped','completed') for m in manager.account_execution.members()):
            raise ValueError('诊断选择会话前，请暂停该账号全部任务')
        owner=manager.busy()
        if owner and (not owner.get('account_runner') or owner.get('manual_takeover')):
            raise ValueError('浏览器正被其他操作或管理员接管')
    permitted()
    session=manager.boss_session
    if not manager.browser_alive(session):raise ValueError('没有可诊断的现有浏览器')
    pages=[p for p in session.context.pages if boss_chat.is_chat_page(p)]
    if len(pages)!=1:raise ValueError('无法唯一确定沟通页面')
    page=pages[0]
    async def read_probe(expression, stage):
        try:
            return await asyncio.wait_for(boss_chat._bound_read(page,expression),timeout=12)
        except TimeoutError:
            return {'task_id':task_id,'mode':'existing_chat_selection_only',
                    'verified':False,'stage':stage,'reason':'page_read_timeout',
                    'sent_messages':0}

    target=None
    for cid in definition['config'].get('case_ids',[]):
        case=manager.cases.load(cid)
        records=[r.get('selection_recovery',{}).get(cid,{}) for r in manager.jobs.values()]
        record=max(records,key=lambda r:r.get('updated_at',0),default={})
        failed=set(record.get('failures',[]))
        if not failed and record.get('status')=='recovered':
            failed={r for prior in records if prior.get('incident_id')==record.get('incident_id') for r in prior.get('failures',[])}
            if record.get('last_verified_chat_id'):failed={record['last_verified_chat_id']}
        for person in case['candidates'].values():
            binding=person.get('conversation') or {}
            if binding.get('chat_id') in failed:
                target=(cid,binding);break
        if target:break
        if not target and failed:
            # The original failure may have occurred before importing/binding a person.
            rows=await read_probe(boss_chat.ROWS_JS,'read_conversation_list')
            if isinstance(rows,dict):return rows
            last=record.get('last_failed_chat_id')
            candidates=[r for r in rows if r['chat_id'] in failed and (not last or r['chat_id']==last) and r.get('receiver') and r.get('job_title')==case['job']['label']]
            if len(candidates)==1:
                row=candidates[0];target=(cid,{'chat_id':row['chat_id'],'receiver':row['receiver'],'job_title':case['job']['label']});break
    if not target:raise ValueError('没有可唯一核验的原失败会话')
    cid,binding=target
    class DiagnosticSession:
        def __init__(self):self.page=page;self.context=session.context
        async def _act(self,kind,fn):
            if kind!='click':raise ValueError('诊断仅允许选中已有会话')
            permitted()
            # Keep native CloakBrowser pacing/selection. The production task
            # remains paused; no send/type/navigation capability is exposed.
            async def checked():
                permitted()
                guard=await boss_chat._bound_read(page,boss_chat.RECOVERY_GUARD_JS)
                if guard.get('has_draft') or guard.get('login') or guard.get('challenge'):
                    raise ValueError('存在草稿、登录要求或平台校验，停止诊断选择')
                return await fn()
            await session.pacer.before_action(kind)
            try:await checked()
            finally:session.pacer.mark_action()
            await asyncio.sleep(.6)
    adapter=DiagnosticSession()
    started=time.monotonic()
    rows=await read_probe(boss_chat.ROWS_JS,'read_conversation_list')
    if isinstance(rows,dict):return rows
    matching=[r for r in rows if r['chat_id']==binding['chat_id']]
    result={'task_id':task_id,'case_id':cid,'mode':'existing_chat_selection_only','target_row_count':len(matching),'sent_messages':0}
    if len(matching)!=1:
        return {**result,'verified':False,'stage':'locate','reason':'target_row_missing' if not matching else 'target_row_ambiguous'}
    row=matching[0]
    if binding['receiver'] not in row['text'] or binding['job_title'] not in row['text']:
        return {**result,'verified':False,'stage':'list_identity','reason':'target_identity_changed'}
    guard=await read_probe(boss_chat.RECOVERY_GUARD_JS,'read_page_guard')
    if guard.get('reason')=='page_read_timeout':return guard
    blocked=next((reason for key,reason in [('has_draft','draft_present'),('login','login_required'),('challenge','verification_required')] if guard.get(key)),None)
    if blocked:return {**result,'verified':False,'stage':'guard','reason':blocked}
    try:
        initial=await boss_chat.read_conversation(adapter,**binding,required=False,page=page)
        result['selection_performed']=not initial.get('valid')
        if result['selection_performed']:await boss_chat.select_chat_row(adapter,binding['chat_id'])
        snapshot=None
        for _ in range(6):
            snapshot=await boss_chat.read_conversation(adapter,**binding,required=False,page=page)
            if snapshot.get('valid') and snapshot.get('messages'):break
            await asyncio.sleep(.6)
        result.update(verified=bool(snapshot and snapshot.get('valid') and snapshot.get('messages') and snapshot.get('editor_count')==1),
                      stage='snapshot',reason=snapshot.get('reason_code') or snapshot.get('reason') if snapshot else 'snapshot_missing',
                      message_count=len(snapshot.get('messages',[])) if snapshot else 0,editor_count=snapshot.get('editor_count') if snapshot else None)
    except Exception as exc:
        result.update(verified=False,stage=getattr(exc,'diagnostics',{}).get('stage','selection'),
                      reason=getattr(exc,'diagnostics',{}).get('reason','selection_failed'),error_type=type(exc).__name__)
    result['duration_ms']=round((time.monotonic()-started)*1000)
    if result.get('verified'):
        owner=manager.busy()
        record=(owner or {}).get('selection_recovery',{}).get(cid)
        if record and set(record.get('failures',[]))=={binding['chat_id']}:
            from .selection_recovery import recovered
            record['last_verified_chat_id']=binding['chat_id']
            recovered(manager,owner,cid)
            owner.setdefault('case_results',{})[cid]={'status':'paused','summary':'原目标会话已核验；任务保持暂停','at':time.time()}
            manager.persist(owner)
            result['incident_resolved']=True
    return result

@_exclusive
async def greeting_settings(manager,task_id):
    """Read current per-job settings; never opens an editor or saves anything."""
    from .greeting_policy import read_settings,render
    task=manager.definitions.get(task_id)
    if task['config']['engine']!='boss_sourcing':raise ValueError('仅支持 BOSS 岗位话术核验')
    session=manager.boss_session
    if not manager.browser_alive(session):raise ValueError('请先保留本任务浏览器会话')
    cases=[manager.cases.load(cid) for cid in task['config']['case_ids']]
    try:settings=await asyncio.wait_for(read_settings(session),60)
    except (asyncio.TimeoutError,SourcingError) as exc:
        reason=('岗位话术只读核验超时' if isinstance(exc,asyncio.TimeoutError) else str(exc))+'；阶段：'+getattr(session,'_greeting_stage','读取设置')+'；未修改设置'
        from .greeting_policy import block
        from .task_policy import overlay
        from types import SimpleNamespace
        for case in cases:
            scoped=overlay(case,task)
            proxy=SimpleNamespace(db=manager.db,event=lambda *args:None)
            block(proxy,{'task_id':task_id},scoped,reason)
        raise ValueError(reason)
    titles={c['job']['label'] for c in cases}
    return {'task_id':task_id,'platform_enabled':settings['enabled'],'open_editor':settings['open_editor'],'rows':[r for r in settings['rows'] if r.get('title') in titles],
            'expected':[{'case_id':c['id'],'title':c['job']['label'],'text':render(c.get('job_sourcing',{}).get('greeting',c.get('desired_greeting','')),c)} for c in cases],'business_writes':0,'settings_writes':0}


@_exclusive
async def job_pages(manager,task_id):
    """只读：职位管理页与推荐页岗位切换控件的结构；不选择选项、不修改设置。"""
    from .job_page_probe import read_job_pages
    return await read_job_pages(manager,task_id)
