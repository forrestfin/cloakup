"""Visible BOSS 沟通-menu adapter. DOM reads stay local, sends use humanized UI.

Fail closed when a row/header/editor/message direction cannot be established.
No undocumented HTTP messaging endpoints, JS click, JS input or retry of sends.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlparse

from .boss_contact import _observation
from .sourcing import SourcingError

CHAT_URL = 'https://www.zhipin.com/web/chat/index'

# data-id is the platform's actual conversation key, distinct from geek_id.
ROWS_JS = r'''(()=>{const rows=Array.from(document.querySelectorAll('.geek-item[data-id]:not(.group-item)')).filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0}).filter(e=>/^[1-9]\d*-\d+$/.test(e.getAttribute('data-id')||''));return rows.map(e=>({chat_id:e.getAttribute('data-id'),text:(e.innerText||'').trim(),receiver:e.querySelector('.geek-name')?.innerText.trim(),job_title:e.querySelector('.source-job')?.innerText.trim(),class_name:e.className,unread:!!Array.from(e.querySelectorAll('[class*=badge],[class*=unread],[class*=red-dot]')).find(n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0})}));})()'''

# One identity contract for imports, reads and pre-send verification.
IDENTITY_READER_JS = r'''(title,chatId=null,expectedName=null)=>{
 // 零宽/软连字符不可见，不能让它们破坏整字段相等比较。
 const norm=v=>(v||'').replace(/\s+/g,'').replace(/[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]/g,'').replace(/^[：:]+|[：:]+$/g,'');
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
 // 元素实际渲染出的完整文字：只拼接有布局盒的文本节点，隐藏文字不计入。
 const renderedText=root=>{const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let s='';for(let n=w.nextNode();n;n=w.nextNode()){const p=n.parentElement;if(!p||getComputedStyle(p).visibility==='hidden')continue;const r=document.createRange();r.selectNodeContents(n);if(r.getClientRects().length)s+=n.textContent;}return norm(s)};
 const rows=Array.from(document.querySelectorAll('.geek-item[data-id]:not(.group-item)')).filter(visible).filter(e=>/^[1-9]\d*-\d+$/.test(e.getAttribute('data-id')||''));
 const selected=rows.filter(r=>r.classList.contains('selected'));
 const evidence={selected_rows:selected.length,visible_rows:rows.length,header_candidates:0,job_matched:0,name_matched:0,header_jobs:[],target_row_present:!chatId||rows.some(r=>r.getAttribute('data-id')===chatId)};
 if(selected.length>1)return {valid:false,reason_code:'selected_row_ambiguous',evidence};
 if(chatId&&selected.length===1&&selected[0].getAttribute('data-id')!==chatId)return {valid:false,reason_code:'selected_target_mismatch',evidence};
 const candidates=selected.length===1?selected:rows;
 const fields=root=>Array.from(root.querySelectorAll('*')).filter(visible).flatMap(e=>[...(!e.children.length?[e.innerText]:[]),...Array.from(e.childNodes).filter(n=>n.nodeType===3).map(n=>n.textContent)]).map(norm).filter(Boolean);
 const leaves=Array.from(document.querySelectorAll('#container *')).filter(visible).filter(e=>!e.children.length&&/沟通职位/.test(e.innerText||''));
 const headers=new Set(),matches=new Map();
 for(const leaf of leaves){for(let e=leaf,i=0;e&&i<9;e=e.parentElement,i++){
  if(e.matches('.geek-item')||e.querySelector('.geek-item,.message-item,.item-message,[contenteditable="true"],textarea'))break;
  if(!visible(e)||!(e.innerText||'').includes('在线简历'))continue;
  if(headers.has(e))continue;headers.add(e);evidence.header_candidates++;
  const values=fields(e),job=values.some(v=>v.replace(/^沟通职位[：:]?/,'')===norm(title));
  // 诊断用：头部“沟通职位”实际写的是什么（同一字段或紧随其后的字段），最多 3 条、每条 40 字。
  values.forEach((v,i)=>{if(!/^沟通职位/.test(v))return;const shown=(v.replace(/^沟通职位[：:]?/,'')||values[i+1]||'').slice(0,40);if(shown&&evidence.header_jobs.length<3&&!evidence.header_jobs.includes(shown))evidence.header_jobs.push(shown)});
  if(!job)continue;evidence.job_matched++;
  // A display name may be composed of multiple inline text spans.
  // Accept the entire name element only, never a substring of the header.
  const nameFields=Array.from(e.querySelectorAll('.geek-name,.geek-name-text,[class~="candidate-name"],b,strong')).filter(visible)
   .map(n=>norm(n.innerText));
  // 姓名可能拆成多段并带认证徽标，外层也不一定是固定类名：
  // 仍只接受“某个头部元素渲染出的完整文字＝姓名”，不做子串匹配。
  const wholeTexts=Array.from(e.querySelectorAll('*')).map(renderedText).filter(Boolean);
  for(const row of candidates){
   const id=row.getAttribute('data-id'),rowJob=norm(row.querySelector('.source-job')?.innerText);
   if(rowJob&&rowJob!==norm(title))continue;
   const name=(row.querySelector('.geek-name')?.innerText||((id===chatId)?expectedName:'')||'').trim();
   if(!name||(!values.includes(norm(name))&&!nameFields.includes(norm(name))&&!wholeTexts.includes(norm(name))))continue;
   if(expectedName&&norm(name)!==norm(expectedName))continue;
   evidence.name_matched++;
   matches.set(id,{chat_id:id,receiver:name,job_title:title,header:e.innerText,headerElement:e});
  }
 }}
 // 没有「沟通职位」头部时，右侧详情还没挂上，不能当成岗位名称不匹配。
 if(matches.size!==1)return {valid:false,reason_code:matches.size?'identity_ambiguous':!evidence.target_row_present?'target_row_missing':!evidence.header_candidates?'detail_not_open':!evidence.job_matched?'header_job_unverified':'header_name_unverified',evidence};
 const identity=Array.from(matches.values())[0];
 if(chatId&&identity.chat_id!==chatId)return {valid:false,reason_code:'selected_target_mismatch',evidence};
 return {valid:true,...identity,evidence};
}'''
IDENTITY_JS = r'''(()=>{const result=(IDENTITY_READER)(EXPECTED_TITLE);if(!result.valid)return null;delete result.headerElement;return result;})()'''.replace('IDENTITY_READER',IDENTITY_READER_JS)

# Status labels can be siblings of the bubble; never borrow a different message's label.
MESSAGE_STATUS_JS = r'''(e,body)=>{
 const visible=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0};
 const leafMessages=root=>Array.from(root.querySelectorAll('.message-item,.item-message')).filter(n=>!n.querySelector('.message-item,.item-message'));
 let root=e;
 for(let depth=0;root&&depth<3;depth++,root=root.parentElement){
  if(root===document.body||root.id==='container'||root.querySelector('.geek-item,[contenteditable="true"],textarea'))break;
  const messages=leafMessages(root);if(messages.some(n=>n!==e))break;
  const labels=Array.from(root.querySelectorAll('*')).filter(n=>!n.children.length&&visible(n)&&!(body&&(n===body||body.contains(n)))).map(n=>(n.innerText||'').trim());
  const error=labels.find(t=>/^(发送失败|重新发送|正在发送|网络异常)$/.test(t));
  if(error)return {status:'unknown',status_evidence:{label:error,scope:depth===0?'message':'single_message_wrapper'}};
  const label=labels.find(t=>t==='已读')||labels.find(t=>/^(已送达|送达)$/.test(t));
  if(label)return {status:label==='已读'?'read':'delivered',status_evidence:{label,scope:depth===0?'message':'single_message_wrapper'}};
 }
 return {status:'unverified'};
}''' 

# Candidate and recruiter sides must be explicit CSS states, never inferred
# from matching text against a locally generated draft.
SNAPSHOT_JS = r'''(()=>{
 const identity=(IDENTITY_READER)(EXPECTED_TITLE,CHAT_ID,EXPECTED_NAME);
 if(!identity.valid)return {valid:false,reason:'会话收件人或岗位未核实',reason_code:identity.reason_code,evidence:identity.evidence};
 const header=identity.headerElement,name=identity.receiver,title=identity.job_title;
 const cssPath=e=>{const a=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){a.unshift('#'+CSS.escape(e.id));break;}const siblings=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];a.unshift(e.tagName.toLowerCase()+(siblings.length>1?':nth-of-type('+(siblings.indexOf(e)+1)+')':''));}return a.join(' > ')};
 const messageStatus=STATUS_FUNCTION;
 const selector='.message-item, .item-message';
 const nodes=Array.from(document.querySelectorAll(selector)).filter(e=>!e.querySelector('.item-system,.message-dialog-center'));
 const messages=nodes.filter(e=>!nodes.some(n=>n!==e&&e.contains(n))).map(e=>{
   const cls=(e.className||'')+' '+(e.parentElement?.className||'');
   const out=!!e.querySelector('.item-myself')||/(^|\s)(sent|self|mine|right|item-myself)(\s|$)/.test(cls);
   const inc=!!e.querySelector('.item-friend')||/(^|\s)(received|other|left|item-friend)(\s|$)/.test(cls);
   const body=e.querySelector('.text-content')||e.querySelector('.message-text')||e.querySelector('.message-content')||e.querySelector('.text');
   const t=(body?.innerText||'').trim();
   const platformControl=/我想要?和您交换(?:微信|电话)|对方想发送(?:加密)?附件简历给您|已同意.{0,12}(?:发送|接收).{0,6}简历/.test(e.innerText||'');
   return {platform_id:e.getAttribute('data-mid')||e.getAttribute('data-msgid')||e.getAttribute('data-id')||e.id||'',
      platform_control:platformControl,direction:out&&!inc?'out':inc&&!out?'in':'unknown',text:t,
      ...messageStatus(e,body),
      timestamp:e.querySelector('time')?.getAttribute('datetime')||e.getAttribute('data-time')||'',
      resume_selector:cssPath(Array.from(e.querySelectorAll('*')).find(n=>!n.children.length&&(n.innerText||'').trim()==='点击预览附件简历')||e.querySelector('a[download],.resume-card,.file-card')),
      attachment:!!e.querySelector('a[download],.resume-card,.file-card'),
      resume:!platformControl&&(!!e.querySelector('.resume-card')||(/点击预览附件简历/.test(e.innerText||'')&&/\.(?:pdf|docx?|jpe?g|png)(?:\s|$)/i.test(e.innerText||''))||(/简历|resume|\bcv\b/i.test(e.innerText||'')&&!!e.querySelector('.file-card,a[download]')))};});
 const editors=Array.from(document.querySelectorAll('[contenteditable="true"],textarea')).filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.height});
 const sensitive=header.querySelectorAll('img').length;
 return {valid:true,chat_id:CHAT_ID,receiver:name,job_title:title,header:header.innerText,
         messages,editor_count:editors.length,editor_value:editors.length===1?(editors[0].value||editors[0].innerText||''):'',coverage:'当前沟通页已加载的消息；不代表平台全部历史'};
})()'''


def is_chat_page(page):
    try:
        parsed=urlparse(page.url)
        return not getattr(page,'is_closed',lambda:False)() and parsed.hostname=='www.zhipin.com' and parsed.path.rstrip('/')=='/web/chat/index'
    except (AttributeError,RuntimeError):return False


async def evaluate(session, expression):
    # Resolve at the read boundary: user navigation and site redirects can
    # invalidate a previously pinned Page without closing that Page object.
    pinned=getattr(session,'_boss_chat_page',None)
    if pinned is not None and is_chat_page(pinned):session.page=pinned
    elif not is_chat_page(session.page):
        alternatives=[p for p in getattr(getattr(session,'context',None),'pages',[]) if is_chat_page(p)]
        if len(alternatives)==1:session.page=alternatives[0]
        else:
            host=urlparse(session.page.url).hostname
            if host not in (None,'www.zhipin.com') or not hasattr(session,'navigate'):
                raise SourcingError('当前页面不在可恢复的沟通上下文，未执行读写')
            await session.navigate(CHAT_URL)
        await _observation(session)  # genuine login / challenge handoff, never a bypass
    if not is_chat_page(session.page):
        raise SourcingError('沟通页尚未就绪，未执行读写；请查看浏览器和登录待办')
    session._boss_chat_page=session.page
    return await _bound_read(session.page,expression)


async def _checked_menu(session):
    obs = await _observation(session)
    if obs.raw.get('challenge') or any(t in obs.raw.get('text','') for t in ('环境存在异常','操作过于频繁','账号存在异常')):
        raise SourcingError('平台出现校验，自动沟通已停止')
    session._boss_chat_page=session.page


async def open_menu(session):
    if urlparse(session.page.url).path != '/web/chat/index':
        await session.navigate(CHAT_URL)
    await _checked_menu(session)
    # 11-25 FDE交付工程师：上一轮打开老会话后虚拟列表停在下方，顶部的新来信行不在 DOM 里，新岗位的主动招呼一直没被读到。
    # 每次读列表前先把列表容器滚回顶部（只滚动，不点击）；在下方的已知会话仍由 include_chat_rows 滚出来。
    if await evaluate(session, LIST_SCROLL_JS.replace('MODE', json.dumps('top'))):
        await asyncio.sleep(.3)
    rows = await evaluate(session, ROWS_JS)
    if not rows:
        for _ in range(4):
            await asyncio.sleep(.4)
            rows=await evaluate(session,ROWS_JS)
            if rows:break
    if not rows:
        # 列表为空时先做页面健康检查：卡在“加载中”等情况逐级自愈（刷新 → 重置标签页），
        # 有草稿、掉登录、平台验证时不刷新，交给原有流程处理。
        from . import page_health
        state=await page_health.heal(session,'chat')
        if state.get('reason')=='login_required':raise SourcingError('BOSS 已显示登录表单，请在专用窗口重新登录后继续；未执行联系')
        if state.get('reason')=='verification_required':raise SourcingError('平台出现校验，自动沟通已停止')
        if state.get('healed_by'):
            await _checked_menu(session)
            for _ in range(10):
                rows=await evaluate(session,ROWS_JS)
                if rows:break
                await asyncio.sleep(.5)
    if not rows:
        # A retained 'unread' filter can hide a reply as soon as it is opened.
        # Reset only the message-list All filter, never a conversation editor.
        probe=r'''(()=>{const root=document.querySelector('.chat-message-filter-left');if(!root)return null;const leaves=Array.from(root.querySelectorAll('*')).filter(e=>!e.children.length&&(e.innerText||'').trim()==='全部');if(leaves.length!==1)return null;let e=leaves[0],parts=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){parts.unshift('#'+CSS.escape(e.id));break;}const same=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];parts.unshift(e.tagName.toLowerCase()+(same.length>1?':nth-of-type('+(same.indexOf(e)+1)+')':''));}return parts.join(' > ')})()'''
        selector=await evaluate(session,probe)
        if selector:
            async def reset_filter():
                loc=session.page.locator(selector)
                if await loc.count()!=1:raise SourcingError('沟通列表全部过滤入口不唯一')
                await loc.click(timeout=5000)
            await session._act('click',reset_filter)
            for _ in range(6):
                await asyncio.sleep(.5);rows=await evaluate(session,ROWS_JS)
                if rows:break
    ids = [r['chat_id'] for r in rows]
    if len(ids) != len(set(ids)):
        raise SourcingError('沟通列表会话标识不唯一')
    return rows


class ChatSelectionUnavailable(SourcingError):
    """Transient list layout problem; never a send failure."""

    def __init__(self,message,*,stage='selection',reason='unavailable',exception_type=None):
        super().__init__(message)
        self.diagnostics={'stage':stage,'reason':reason,'exception_type':exception_type}


def _note_header_jobs(error, evidence):
    """头部“沟通职位”实际显示的文字写进诊断，只取这个字段，便于查岗位名为什么对不上。"""
    jobs=[str(v)[:40] for v in ((evidence or {}).get('header_jobs') or [])][:3] if isinstance(evidence,dict) else []
    if jobs:error.diagnostics['header_jobs']=jobs


def _selection_page(session):
    """Resolve a page without navigation, before creating any bound locator."""
    pinned=getattr(session,'_boss_chat_page',None)
    if is_chat_page(pinned):page=pinned
    elif is_chat_page(session.page):page=session.page
    else:
        pages=[p for p in getattr(getattr(session,'context',None),'pages',[]) if is_chat_page(p)]
        if len(pages)!=1:
            raise ChatSelectionUnavailable('无法唯一核验沟通标签页，等待恢复；未执行发送',
                stage='resolve_page',reason='page_ambiguous' if pages else 'page_missing')
        page=pages[0]
    session.page=session._boss_chat_page=page
    return page


async def _bound_read(page,expression):
    from .dom_reader import read_dom
    def check():
        if not is_chat_page(page):
            raise ChatSelectionUnavailable('沟通标签页已变化，停止旧操作；未执行发送',stage='resolve_page',reason='page_changed')
    return await read_dom(page,expression,validate=check)


def _selection_timeout(stage,exc):
    # Classify known diagnostics without copying selectors, DOM or candidate text.
    text=str(exc).lower()
    reason=next((value for marker,value in (
        ('intercepts pointer events','obscured'),('not stable','moving'),
        ('outside of the viewport','outside_viewport'),('outside viewport','outside_viewport'),
        ('waiting for the page to respond','page_unresponsive')) if marker in text),'timeout')
    return ChatSelectionUnavailable('会话选择超时，下轮重新读取；未执行发送',
        stage=stage,reason=reason,exception_type=type(exc).__name__)


RECOVERY_GUARD_JS=r'''(()=>{
 const editors=Array.from(document.querySelectorAll('[contenteditable="true"],textarea'));
 const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const text=document.body?.innerText||'';
 return {editor_count:editors.length,
   has_draft:editors.some(e=>(e.value||e.innerText||'').trim().length>0),
   chat_rows:document.querySelectorAll('.geek-item[data-id]:not(.group-item)').length,
   login:!!Array.from(document.querySelectorAll('input[type="password"]')).find(visible)||
     !!Array.from(document.querySelectorAll('button')).find(e=>visible(e)&&/^(登录\/注册|扫码登录|密码登录|短信登录)$/.test((e.innerText||'').trim())),
   challenge:/环境存在异常|操作过于频繁|账号存在异常|请完成安全验证|请完成验证|拖动滑块/.test(text)||
     !!Array.from(document.querySelectorAll('iframe[src*="captcha"],iframe[src*="verify"],[class*="captcha"]')).find(visible)};
})()'''


# 浏览器刚启动或刚打开沟通页时，编辑器和会话列表都还没画出来（两者都是 0）。
# 这时不能当成“无法核验编辑器”直接暂停，先等页面画好再判断；有登录、验证或草稿则立刻返回。
RENDER_WAIT_READS = 8


def _unrendered(state):
    if not isinstance(state,dict):return True
    if state.get('login') or state.get('challenge') or state.get('has_draft'):return False
    return not state.get('editor_count') and not state.get('chat_rows')


async def _settled_guard_state(page):
    from .pacing import lognormal
    state=await _bound_read(page,RECOVERY_GUARD_JS)
    for _ in range(RENDER_WAIT_READS):
        if not _unrendered(state):break
        await asyncio.sleep(lognormal(2.0,0.4,0.8,5))
        state=await _bound_read(page,RECOVERY_GUARD_JS)
    return state


async def recover_chat_selection(session):
    """One guarded menu reload. Never discard drafts, bypass login or send."""
    def blocked(reason):return {'status':'blocked','reason':reason,'requires_admin':True,'page_reloaded':False}
    path=urlparse(getattr(session.page,'url','')).path.lower()
    if any(p in path for p in ('/web/user','/login','/passport','/signin')):return blocked('login_required')
    try:page=_selection_page(session)
    except ChatSelectionUnavailable as exc:
        # 浏览器刚启动时往往还没有沟通页。先打开沟通页再核验，不把整个任务停掉。
        if exc.diagnostics.get('reason')!='page_missing' or not hasattr(session,'navigate'):
            return blocked(exc.diagnostics['reason'])
        await session.navigate(CHAT_URL)
        try:page=_selection_page(session)
        except ChatSelectionUnavailable as again:return blocked(again.diagnostics['reason'])
    stage='recover_guard'
    async def guard():
        if session.page is not page or not is_chat_page(page):return blocked('page_changed')
        state=await _settled_guard_state(page)
        if session.page is not page or not is_chat_page(page):return blocked('page_changed')
        if not isinstance(state,dict):return blocked('editor_unavailable')
        if state.get('login'):return blocked('login_required')
        if state.get('challenge'):return blocked('verification_required')
        if state.get('has_draft'):return blocked('draft_present')
        if 'has_draft' not in state or (not state.get('editor_count') and not state.get('chat_rows')):
            return blocked('editor_unavailable')
        return None
    try:
        result=await guard()
        if result:return result
        reloaded=False
        async def reload_menu():
            nonlocal result,stage,reloaded
            # Recheck after pacing so a newly typed manual draft is retained.
            result=await guard()
            if result:return
            # 已经在沟通页就不要整页刷新。刷新会把右侧详情清成未选中的空白。
            if is_chat_page(page):return
            stage='recover_reload'
            reloaded=True
            await asyncio.wait_for(page.goto(CHAT_URL,wait_until='domcontentloaded',timeout=15000),timeout=20)
        await session._act('navigate',reload_menu)
        if result:return result
        stage='recover_verify'
        obs=await _observation(session)
        from .access import boss_login_required,boss_verification_required
        if boss_login_required(obs):return {**blocked('login_required'),'page_reloaded':reloaded}
        if boss_verification_required(obs):return {**blocked('verification_required'),'page_reloaded':reloaded}
        if session.page is not page or not is_chat_page(page):return {**blocked('page_changed'),'page_reloaded':reloaded}
        session._boss_chat_page=page
        return {'status':'recovered','page_reloaded':reloaded}
    except Exception as exc:
        if type(exc).__name__=='TimeoutError':raise _selection_timeout(stage,exc) from exc
        raise


# 滚动会话列表的可滚动容器：top 回到顶部，next 下移约一屏；返回是否到底。
# 找真正在滚的列表容器：不只看 overflow 样式（BOSS 的虚拟列表不一定声明 auto/scroll），
# 以设置 scrollTop 后是否真的移动为准；都不行时让最后（或第一）一行滚进视野，带动列表加载。
LIST_SCROLL_JS = r'''((mode)=>{const items=document.querySelectorAll('.geek-item[data-id]:not(.group-item)');if(!items.length)return null;for(let e=items[0].parentElement;e&&e!==document.body&&e!==document.documentElement;e=e.parentElement){if(e.scrollHeight<=e.clientHeight+1)continue;const before=e.scrollTop;e.scrollTop=mode==='top'?0:before+Math.max(1,Math.floor(e.clientHeight*0.8));if(e.scrollTop!==before||before>0||mode==='top')return {moved:e.scrollTop!==before,end:e.scrollTop+e.clientHeight>=e.scrollHeight-1,via:'container'};}const target=items[mode==='top'?0:items.length-1];target.scrollIntoView({block:mode==='top'?'start':'end',behavior:'instant'});return {moved:true,end:false,via:'into_view'};})(MODE)'''
LIST_REVEAL_STEPS = 40
# 滚到底后列表可能还在加载更早的会话：等一下再滚，最多这么多次，才算确认到底。
LIST_END_WAITS = 3


async def _reveal(scroll, found):
    """逐屏滚动虚拟列表直到 found() 为真；只滚动列表容器，不点击。

    返回 (是否找到, 是否确认已到底)。11-24：老会话在 12 屏以外，原来最多滚 12 步就放弃，
    5 条会话因此被隔离且一直复核不过。
    """
    from .logbook import log
    end_waits=0;via=None
    def done(hit,end,steps):
        log('state',type='list_reveal',found=hit,reached_end=end,steps=steps,via=via)
        return hit,end
    for step in range(LIST_REVEAL_STEPS):
        state=await scroll('top' if step==0 else 'next')
        if not state:return done(False,False,step)
        via=state.get('via',via)
        await asyncio.sleep(.3)
        if await found():return done(True,False,step+1)
        if state.get('end'):
            if end_waits>=LIST_END_WAITS:return done(False,True,step+1)
            end_waits+=1
            await asyncio.sleep(1.2)
    return done(False,False,LIST_REVEAL_STEPS)


async def include_chat_rows(session, rows, chat_ids):
    """把已知、但被虚拟列表滚出可视区的会话行并入本轮。只滚动查找，不点击。"""
    wanted=[chat_id for chat_id in chat_ids if chat_id and chat_id not in {row['chat_id'] for row in rows}]
    if not wanted:return rows
    found=list(rows);seen={row['chat_id'] for row in found}
    async def scroll(mode):return await evaluate(session,LIST_SCROLL_JS.replace('MODE',json.dumps(mode)))
    async def collect():
        for row in await evaluate(session,ROWS_JS):
            if row['chat_id'] not in seen:
                found.append(row);seen.add(row['chat_id'])
        return all(chat_id in seen for chat_id in wanted)
    await _reveal(scroll,collect)
    return found


async def select_chat_row(session, chat_id):
    """Reposition after pacing, wait for stable geometry, then normal click."""
    if not re.fullmatch(r'\d+-\d+', chat_id or ''):
        raise SourcingError('会话标识不合法')
    selector='.geek-item[data-id='+json.dumps(chat_id)+']:not(.group-item)'
    scroll='(()=>{const e=Array.from(document.querySelectorAll('+json.dumps(selector)+')).find(e=>{const r=e.getBoundingClientRect();return r.width&&r.height});if(e)e.scrollIntoView({block:"center",inline:"nearest",behavior:"instant"});})()'
    point_js='(()=>{const e=Array.from(document.querySelectorAll('+json.dumps(selector)+')).find(e=>{const r=e.getBoundingClientRect();return r.width&&r.height});if(!e)return null;const r=e.getBoundingClientRect();for(const fy of [0.5,0.7,0.3]){const x=r.left+r.width*0.5,y=r.top+r.height*fy;const hit=document.elementFromPoint(x,y);if(hit&&(hit===e||e.contains(hit)))return {x:r.width*0.5,y:r.height*fy,left:r.left,top:r.top,width:r.width,height:r.height};}return null;})()'
    stage='pacing'
    async def select():
        nonlocal stage
        stage='resolve_page'
        page=_selection_page(session)
        session._boss_selection_page=page
        # A locator belongs to its creation Page, even if session.page changes.
        # All geometry reads below use this exact Page and never retarget it.
        loc=page.locator(selector+':visible')
        stage='locate'
        count=await asyncio.wait_for(loc.count(),timeout=5)
        if count==0:
            # 会话列表是虚拟滚动：处理其他会话后列表被滚走，目标行会被移出 DOM。
            # 只滚动列表容器逐屏查找，不点击；多条同标识的行仍按不唯一处理。
            stage='reveal'
            async def scroll_list(mode):return await _bound_read(page,LIST_SCROLL_JS.replace('MODE',json.dumps(mode)))
            async def row_visible():
                nonlocal count
                count=await asyncio.wait_for(loc.count(),timeout=5)
                return bool(count)
            await _reveal(scroll_list,row_visible)
            stage='locate'
        if count!=1:
            raise ChatSelectionUnavailable('会话行暂不唯一，下轮重新读取',stage=stage,reason='row_not_unique')
        # DOM reads can succeed in a background tab while pointer events stall.
        stage='focus'
        if hasattr(page,"bring_to_front"):
            await asyncio.wait_for(page.bring_to_front(),timeout=5)
        # This runs AFTER the human pacing delay. A point calculated before
        # that delay can be stale when new messages reorder the list.
        stage='scroll'
        await _bound_read(page,scroll)
        stage='stability'
        previous=None;point=None
        for _ in range(8):
            current=await _bound_read(page,point_js)
            if current and previous and all(abs(current[k]-previous[k])<1 for k in current):
                point=current;break
            previous=current
            await asyncio.sleep(.15)
        if not point:raise ChatSelectionUnavailable('会话列表仍在移动或被遮挡，下轮重新读取',stage=stage,reason='moving_or_obscured')
        if session.page is not page or not is_chat_page(page):
            raise ChatSelectionUnavailable('沟通标签页已变化，停止旧操作；未执行发送',stage=stage,reason='page_changed')
        stage='click'
        await asyncio.wait_for(loc.click(position={k:point[k] for k in ('x','y')},timeout=5000),timeout=12)
        stage='settle'
    try:
        await session._act('click',select)
    except Exception as exc:
        if type(exc).__name__=='TimeoutError':
            raise _selection_timeout(stage,exc) from exc
        raise


async def verify_after_click_timeout(read, matches, error):
    """Allow the clicked pane to settle without repeating the click."""
    try:
        async with asyncio.timeout(5):
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(.6)
                result = await read()
                if matches(result):
                    return result
    except (TimeoutError, SourcingError):
        pass
    raise error


async def refresh_chat_list(session):
    """沟通列表和本地绑定对不上时，整页重新打开沟通页再读列表。不覆盖草稿，不绕过登录或验证。"""
    page=_selection_page(session)
    state=await _settled_guard_state(page)
    if not isinstance(state,dict):
        raise ChatSelectionUnavailable('无法核验编辑器是否存在草稿，未刷新页面',stage='recover_guard',reason='editor_unavailable')
    if state.get('login'):
        raise ChatSelectionUnavailable('登录已失效，未刷新页面',stage='recover_guard',reason='login_required')
    if state.get('challenge'):
        raise ChatSelectionUnavailable('平台要求验证，未刷新页面',stage='recover_guard',reason='verification_required')
    if state.get('has_draft'):
        raise ChatSelectionUnavailable('发现未发送的草稿，已保留原内容，未刷新页面',stage='recover_guard',reason='draft_present')
    from .pacing import lognormal
    async def reload():
        await asyncio.wait_for(page.goto(CHAT_URL,wait_until='domcontentloaded',timeout=15000),timeout=20)
        await asyncio.sleep(lognormal(1.6,0.45,0.7,5))
    await session._act('navigate',reload)
    session.page=session._boss_chat_page=page
    return await evaluate(session,ROWS_JS)


def _listed_identity(row):
    return ((row.get('receiver') or '').strip(), (row.get('job_title') or '').strip())


def _listed_identity_disagrees(row, receiver, job_title):
    """同一条会话的平台姓名或岗位，和本地绑定不是同一份。"""
    name, title = _listed_identity(row)
    if (name and name != receiver) or (title and title != job_title):
        return True
    text = row.get('text') or ''
    return receiver not in text or job_title not in text


async def _identity_from_platform_row(session, row, chat_id, receiver, job_title):
    """列表行和本地不一致时，先整页重读这一条。平台给出的姓名和岗位覆盖本地；读不到这一行则不改。"""
    if not _listed_identity_disagrees(row, receiver, job_title):
        name, title = _listed_identity(row)
        return (name, title) if name and title else (receiver, job_title)
    fresh = await refresh_chat_list(session)
    matches = [item for item in fresh if item['chat_id'] == chat_id]
    list_end = False
    if fresh and not matches:
        # 整页刷新后列表回到第一屏，老会话要往下滚才找得到（11-24 候选人 34）。
        async def scroll(mode):return await evaluate(session,LIST_SCROLL_JS.replace('MODE',json.dumps(mode)))
        async def present():
            nonlocal matches
            matches=[item for item in await evaluate(session,ROWS_JS) if item['chat_id']==chat_id]
            return bool(matches)
        _,list_end=await _reveal(scroll,present)
    if len(matches) != 1:
        exc = ChatSelectionUnavailable('当前列表没有唯一目标会话，未发送', stage='locate', reason='target_row_missing' if not matches else 'row_not_unique')
        exc.diagnostics['list_end'] = bool(list_end and not matches)
        raise exc
    row = matches[0]
    name, title = _listed_identity(row)
    if name and title:
        return name, title
    text = row.get('text') or ''
    if receiver not in text or job_title not in text:
        raise ChatSelectionUnavailable('目标会话的候选人与岗位信息变化，未发送', stage='list_identity', reason='list_identity_mismatch')
    return receiver, job_title


async def open_conversation(session, chat_id, receiver, job_title):
    if not re.fullmatch(r'\d+-\d+', chat_id or ''):
        raise SourcingError('会话标识不合法')
    rows = await open_menu(session)
    matches = [r for r in rows if r['chat_id'] == chat_id]
    list_end=False
    if rows and not matches:
        # 虚拟滚动列表只渲染可视范围附近的行：只滚动列表查找，重新读取后再判断唯一性。
        async def scroll(mode):return await evaluate(session,LIST_SCROLL_JS.replace('MODE',json.dumps(mode)))
        async def present():
            nonlocal matches
            matches=[r for r in await evaluate(session,ROWS_JS) if r['chat_id']==chat_id]
            return bool(matches)
        _,list_end=await _reveal(scroll,present)
    if len(matches)!=1:
        exc=ChatSelectionUnavailable('当前列表没有唯一目标会话，未发送',stage='locate',reason='target_row_missing' if not matches else 'row_not_unique')
        # 已翻到列表最底部仍找不到：这条会话多半已不在 BOSS 沟通列表里（超过 30 天、被删除等）。
        exc.diagnostics['list_end']=bool(list_end and not matches)
        raise exc
    receiver,job_title=await _identity_from_platform_row(session,matches[0],chat_id,receiver,job_title)
    # Do not click an already selected, fully verified conversation again.
    current=await read_conversation(session,chat_id,receiver,job_title,required=False,
        page=_selection_page(session))
    if current.get('valid'):return current
    try:
        await select_chat_row(session,chat_id)
    except ChatSelectionUnavailable as exc:
        if exc.diagnostics['stage']!='click' or exc.diagnostics['exception_type']!='TimeoutError':raise
        page=getattr(session,'_boss_selection_page',session.page)
        return await verify_after_click_timeout(
            lambda:read_conversation(session,chat_id,receiver,job_title,required=False,page=page),
            lambda snapshot:snapshot.get('valid'),exc)
    from .pacing import lognormal
    snapshot={}
    # 第一次点击经常落在列表上，但右侧详情仍是未选中的空白。同一条再点，直到详情挂上。
    for click_index in range(3):
        if click_index:
            await asyncio.sleep(lognormal(2.2, 0.45, 1.0, 6))
            try:
                await select_chat_row(session, chat_id)
            except ChatSelectionUnavailable as exc:
                if exc.diagnostics.get('stage')!='click' or exc.diagnostics.get('exception_type')!='TimeoutError':raise
        for _ in range(4 if click_index else 6):
            snapshot = await read_conversation(session, chat_id, receiver, job_title, required=False)
            if snapshot.get('valid'):
                return snapshot
            await asyncio.sleep(lognormal(0.8, 0.4, 0.35, 2.5) if click_index else .6)
        if snapshot.get('reason_code') not in ('detail_not_open','header_job_unverified'):
            break
    error=ChatSelectionUnavailable('打开后的会话头部身份未核实，未发送',stage='verify_identity',reason=snapshot.get('reason_code','header_identity_unverified'))
    _note_header_jobs(error,snapshot.get('evidence'))
    raise error


async def current_identity(session, job_title, *, page=None, chat_id=None):
    expression='(()=>{const r=('+IDENTITY_READER_JS+')('+json.dumps(job_title)+','+json.dumps(chat_id)+');delete r.headerElement;return r;})()'
    result=await _bound_read(page,expression) if page is not None else await evaluate(session,expression)
    session._boss_identity_probe={k:result.get(k) for k in ('reason_code','evidence')} if result else {}
    return result if result and result.get('valid') else None


async def inspect_row(session, row, job_title):
    """Read an unbound existing chat, then bind using its actual opened header."""
    if not re.fullmatch(r'\d+-\d+',row.get('chat_id','')):
        raise SourcingError('历史会话标识不合法')
    try:
        await select_chat_row(session,row['chat_id'])
    except ChatSelectionUnavailable as exc:
        if exc.diagnostics['stage']!='click' or exc.diagnostics['exception_type']!='TimeoutError':raise
        page=getattr(session,'_boss_selection_page',session.page)
        return await verify_after_click_timeout(
            lambda:current_identity(session,job_title,page=page,chat_id=row['chat_id']),
            lambda identity:identity and identity['chat_id']==row['chat_id'],exc)
    page=getattr(session,'_boss_selection_page',getattr(session,'page',None))
    for _ in range(6):
        identity=await current_identity(session,job_title,page=page,chat_id=row['chat_id'])
        if identity and identity['chat_id']==row['chat_id']:return identity
        await asyncio.sleep(.6)
    probe=getattr(session,'_boss_identity_probe',{})
    reason=probe.get('reason_code') or 'header_identity_unverified'
    error=ChatSelectionUnavailable('历史会话的收件人或岗位尚未核实，未发送',stage='verify_identity',reason=reason)
    error.diagnostics['identity_evidence']=probe
    _note_header_jobs(error,probe.get('evidence'))
    raise error


async def read_conversation(session, chat_id, receiver, job_title, *, required=True, page=None):
    expr = SNAPSHOT_JS.replace('IDENTITY_READER',IDENTITY_READER_JS).replace('STATUS_FUNCTION',MESSAGE_STATUS_JS).replace('CHAT_ID',json.dumps(chat_id)).replace('EXPECTED_NAME',json.dumps(receiver)).replace('EXPECTED_TITLE',json.dumps(job_title))
    data = await _bound_read(page,expr) if page is not None else await evaluate(session, expr)
    if required and not data.get('valid'):
        error=ChatSelectionUnavailable(data.get('reason','会话未核实'),stage='verify_identity',reason=data.get('reason_code','header_identity_unverified'))
        _note_header_jobs(error,data.get('evidence'))
        raise error
    if data.get('valid'):
        if not data.get('messages') and required:
            raise ChatSelectionUnavailable('尚未识别真实消息列表，停止自动沟通，需适配当前页面',stage='read_messages',reason='message_list_unavailable')
        occurrence = {}
        for msg in data['messages']:
            basis = json.dumps([chat_id,msg['direction'],msg['text'],msg['timestamp'],msg['attachment']],ensure_ascii=False)
            occurrence[basis] = occurrence.get(basis,0) + 1
            msg['external_id'] = msg['platform_id'] or 'observed:'+hashlib.sha256((basis+str(occurrence[basis])).encode()).hexdigest()
        data['fingerprint'] = hashlib.sha256(json.dumps(data['messages'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return data


class SendAborted(SourcingError):
    """点击“发送”之前中止：消息一定没发出去。typed=已打字，cleared=自己打的字已删掉。"""

    def __init__(self, message, *, reason, typed, cleared):
        super().__init__(message)
        self.reason, self.typed, self.cleared = reason, typed, cleared


async def _clear_own_text(session, binding, text, editor):
    """只删自己刚打、与正文逐字一致的文字；删不掉或内容不一致都返回 False，不抛异常。"""
    try:
        now = await read_conversation(session, binding['chat_id'], binding['receiver'], binding['job_title'], required=False)
        if now.get('editor_value') != text:return False
        # 任务刚暂停时 _act 会拒绝页面操作；删自己刚打的字走 _tidy（只在管理员接管时拒绝），否则留下的字恢复后又会暂停任务。
        act = getattr(session, '_tidy', None) or session._act
        await act('type', lambda: editor.fill('', timeout=20000))
        after = await read_conversation(session, binding['chat_id'], binding['receiver'], binding['job_title'], required=False)
        return after.get('editor_value', None) == ''
    except Exception:
        return False


async def send_once(session, binding, text, before, *, pre_send=None):
    now = await read_conversation(session, binding['chat_id'], binding['receiver'], binding['job_title'])
    if now['fingerprint'] != before['fingerprint'] or now['editor_count'] != 1:
        raise SendAborted('阅读后会话变化或输入框不唯一，未发送', reason='changed_before_typing', typed=False, cleared=False)
    if now.get('editor_value','').strip():
        raise SendAborted('输入框已有人工草稿，自动沟通未覆盖', reason='existing_draft', typed=False, cleared=False)
    obs = await _observation(session)
    editors = [e for e in obs.elements if e.raw.get('editable') or e.raw.get('contenteditable') or e.tag=='textarea' or e.raw.get('role')=='textbox']
    # Observation CSS must resolve to the single actual contenteditable input.
    if len(editors) != 1:
        # Some BOSS versions expose contenteditable with no ARIA role. Resolve
        # the DOM control directly but still use the humanized locator API.
        editor = session.page.locator('[contenteditable="true"]')
        if await editor.count() != 1:
            raise SendAborted('无法核实唯一会话输入框', reason='editor_unavailable', typed=False, cleared=False)
        value = await editor.inner_text()
        if value.strip():raise SendAborted('输入框已有人工草稿，自动沟通未覆盖', reason='existing_draft', typed=False, cleared=False)
        await session._act('type',lambda:editor.fill(text,timeout=20000))
    else:
        if editors[0].raw.get('value','').strip():raise SendAborted('输入框已有人工草稿，未覆盖', reason='existing_draft', typed=False, cleared=False)
        editor = session.element(editors[0].index)[0]
        await session.type_text(editors[0].index,text,clear=True,submit=False)

    async def abort(message, reason):
        # 11-27 10:28：中止时把自己打的字留在框里，15 分钟后被当成人工草稿，整单停了 5 小时。
        cleared = await _clear_own_text(session, binding, text, editor)
        raise SendAborted(message + ('；已删除未发送的文字' if cleared else '；未能删除输入框里的文字'), reason=reason, typed=True, cleared=cleared)
    # Recheck messages after typing; an intervening reply revokes this send.
    try:
        fresh = await read_conversation(session,binding['chat_id'],binding['receiver'],binding['job_title'])
    except Exception as exc:
        await abort('输入后读取会话失败（'+type(exc).__name__+'），未点击发送', 'reread_failed')
    if fresh['fingerprint'] != before['fingerprint']:
        await abort('输入期间收到新消息，停止本次发送', 'reply_during_typing')
    if fresh.get('editor_value') != text:
        await abort('输入后的正文与本次消息不一致，未点击发送', 'text_mismatch')
    try:
        obs = await _observation(session)
    except Exception as exc:
        await abort('输入后读取页面失败（'+type(exc).__name__+'），未点击发送', 'reread_failed')
    buttons = [e for e in obs.elements if e.label=='发送' and not e.raw.get('disabled') and not e.raw.get('frame_url')]
    if len(buttons) != 1:await abort('发送按钮不唯一', 'send_button_not_unique')
    # The caller durably claims before calling this method. No retry after click.
    if pre_send:
        try:pre_send()
        except SourcingError as exc:await abort(str(exc), 'not_authorized')
    await session.click(buttons[0].index)
    prior = {m['external_id'] for m in before['messages']}
    for _ in range(8):
        current=await read_conversation(session,binding['chat_id'],binding['receiver'],binding['job_title'],required=False)
        if not current.get('valid') and current.get('reason_code')=='target_row_missing':
            # 发送后该会话跳到列表顶部，列表停在下方时会被虚拟滚动移出 DOM；
            # 只把列表滚回顶部再读，不点击、不重发。
            await evaluate(session,LIST_SCROLL_JS.replace('MODE',json.dumps('top')))
            await asyncio.sleep(1)
            continue
        if not current.get('valid') or not current.get('messages'):
            current=await read_conversation(session,binding['chat_id'],binding['receiver'],binding['job_title'])
        found=[m for m in current['messages'] if m['external_id'] not in prior and m['direction']=='out' and m['text']==text and m['status'] in ('delivered','read')]
        if len(found)==1:
            return {'evidence':'同一会话出现新增出站消息，正文逐字一致且显示送达或已读',
                    'chat_id':binding['chat_id'],'message_id':found[0]['external_id'],'url':CHAT_URL,'at':time.time(),
                    'delivery_status':found[0]['status'],'status_evidence':found[0].get('status_evidence'),
                    'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
        await asyncio.sleep(1)
    return None
