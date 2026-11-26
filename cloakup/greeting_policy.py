"""Task/job-scoped greeting readiness; settings failures never stop the inbox."""
import asyncio,hashlib,re,time
from .sourcing import SourcingError
from .pacing import jitter
from .boss_contact import GREETING_FRAME,GREETING_URL,_click,_find,_frame_value,valid_message,BossFirstContact

class GreetingUnavailable(SourcingError):
 def __init__(self,reason,state=None):
  super().__init__(reason);self.state=state or {}

def render(template,case):
 if not isinstance(template,str):raise ValueError('首聊模板必须是文字')
 text=template.replace('{{岗位名称}}',case['job']['label']).strip()
 if re.search(r'\{\{.*?\}\}|｛｛.*?｝｝',text):raise ValueError('首聊话术仅支持 {{岗位名称}}；请修正未解析参数')
 if text:valid_message(text)
 return text

def key(task_id,case_id):return hashlib.sha256((task_id+':'+case_id).encode()).hexdigest()[:32]
def stamp(text):return hashlib.sha256(text.encode()).hexdigest()

READ_ROWS=r'''(()=>{const path=e=>{const a=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){a.unshift('#'+CSS.escape(e.id));break;}const siblings=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];a.unshift(e.tagName.toLowerCase()+(siblings.length>1?':nth-of-type('+(siblings.indexOf(e)+1)+')':''));}return a.join(' > ')};const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};return Array.from(document.querySelectorAll('.job-content')).filter(visible).map(e=>{const controls=Array.from(e.querySelectorAll('button,a,i,span,[role=button]')).filter(visible);const edit=controls.filter(n=>/^(编辑|修改)$/.test((n.innerText||'').trim())||/^(编辑|修改)$/.test(n.getAttribute('title')||n.getAttribute('aria-label')||'')||n.matches('.icon-edit,.edit-icon,.edit-btn'));return {title:e.querySelector('.job-name span')?.innerText.trim(),text:e.querySelector('.job-greeting')?.innerText.trim(),edit_path:edit.filter(n=>!edit.some(o=>o!==n&&n.contains(o))).length===1?path(edit.filter(n=>!edit.some(o=>o!==n&&n.contains(o)))[0]):null,control_labels:controls.map(n=>(n.innerText||n.getAttribute('title')||n.getAttribute('aria-label')||'').trim()).filter(Boolean).slice(0,20)};});})()'''

async def read_settings(session):
 if any(GREETING_FRAME in f.url for f in getattr(session.page,'frames',[])):
  opened=await _frame_value(session,GREETING_FRAME,"Array.from(document.querySelectorAll('textarea')).some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0})")
  if opened:raise GreetingUnavailable('话术编辑窗口仍打开，已保留内容，请管理员核对')
 session._greeting_stage='打开岗位话术设置'
 from urllib.parse import urlparse
 if urlparse(session.page.url).path!=urlparse(GREETING_URL).path:
  await session.navigate(GREETING_URL)
 session._greeting_stage='选择岗位设置页签'
 frame=None
 for _ in range(12):
  frames=[f for f in session.page.frames if GREETING_FRAME in f.url]
  if len(frames)==1:frame=frames[0];break
  if len(frames)>1:raise GreetingUnavailable('岗位设置内嵌页面不唯一；未修改')
  await asyncio.sleep(.3)
 if frame is None:raise GreetingUnavailable('岗位设置页面尚未加载；未修改')
 # 内嵌页出现后内容还要一会儿才画出来（11-24 16:23 导航后 7 秒就判失败）：等岗位列表或页签出现。
 from .frame_actions import text_control,click as frame_click
 ready=False;tab_path=None
 for _ in range(12):
  ready=await _frame_value(session,GREETING_FRAME,"!!document.querySelector('.job-content .job-name span')")
  if ready:break
  tab_path=await text_control(session.page,frame,'按职位设置招呼语')
  if tab_path:break
  await asyncio.sleep(.5)
 if not ready:
  if not tab_path:raise GreetingUnavailable('无法唯一核实按岗位设置页签；未修改')
  await frame_click(session,frame,tab_path)
 session._greeting_stage='读取启用状态'
 enabled=await _frame_value(session,GREETING_FRAME,"!!document.querySelector('.greeting-header .ui-switch.ui-switch-checked')")
 session._greeting_stage='读取岗位话术与编辑控件'
 # 岗位列表晚于页签出现：读到 0 行就当“本岗位没有话术”会误走新增流程（11-24 15:44／15:57），先等列表出现。
 for _ in range(12):
  rows=await _frame_value(session,GREETING_FRAME,READ_ROWS)
  if rows:break
  await asyncio.sleep(.5)
 drafts=await _frame_value(session,GREETING_FRAME,"Array.from(document.querySelectorAll('textarea')).some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0})")
 return {'enabled':bool(enabled),'rows':rows,'open_editor':bool(drafts)}

async def synchronize(session,title,text,before,guard,before_save):
 """Native UI only. Never toggle account settings, guess a job or repeat Save."""
 if not before['enabled']:raise GreetingUnavailable('平台按岗位话术未启用；未改变账号通用设置')
 if before.get('open_editor'):raise GreetingUnavailable('平台已有未关闭的编辑窗口，请管理员核对；未覆盖内容')
 matches=[r for r in before['rows'] if r['title']==title]
 if len(matches)>1:raise GreetingUnavailable('平台存在同名岗位话术，无法唯一定位；未修改')
 await guard()
 if not matches:
  await BossFirstContact(session).greeting_editor(title)
  await guard();await _click(session,title,frame=GREETING_FRAME)
 else:
  row=matches[0]
  if not row.get('edit_path'):raise GreetingUnavailable('未找到本岗位唯一的编辑控件，请在执行浏览器同步话术后重新核验')
  frame=next((f for f in session.page.frames if GREETING_FRAME in f.url),None)
  if frame is None:raise GreetingUnavailable('岗位设置页面已变化；未修改')
  from .frame_actions import click as frame_click
  await frame_click(session,frame,row['edit_path'])
 # Verify the opened editor still belongs to this one job before typing.
 selected=await _frame_value(session,GREETING_FRAME,"Array.from(document.querySelectorAll('.dialog-wrap .radio-group label')).filter(e=>e.querySelector('input[type=radio]')?.checked).map(e=>e.innerText.trim())")
 # 11-25 18:24 FDE交付工程师：新增窗口里岗位文字可能带城市、薪资（“岗位名 + 分隔 + 其他”），全等比较必失败。
 # 只认“等于岗位名，或岗位名后紧跟空白／分隔符”，并把看到的选中项写进原因，下次失败可直接对照。
 labels=[str(v).strip() for v in selected or []]
 same=lambda v:v==title or (v.startswith(title) and v[len(title)] in ' \t\n　·|/-_,，(（')
 if len(labels)!=1 or not same(labels[0]):raise GreetingUnavailable('编辑窗口未唯一核实当前岗位（选中 %d 个：%s）；未输入、未保存'%(len(labels),'、'.join(v.replace('\n',' ')[:40] for v in labels[:3]) or '无'))
 await guard();el,_=await _find(session,'请输入打招呼语',tag='textarea',frame=GREETING_FRAME)
 await session.type_text(el.index,text,clear=True,submit=False)
 el,_=await _find(session,'请输入打招呼语',tag='textarea',frame=GREETING_FRAME)
 if el.raw.get('value')!=text:raise GreetingUnavailable('输入后正文不一致，未保存')
 await guard();before_save();await _click(session,'保存',tag='button',frame=GREETING_FRAME)
 import asyncio
 for _ in range(6):
  opened=await _frame_value(session,GREETING_FRAME,"Array.from(document.querySelectorAll('textarea')).some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0})")
  if not opened:return
  await asyncio.sleep(.4)
 raise GreetingUnavailable('保存后编辑窗口仍打开，已保留内容；请核对保存结果')

async def ensure(manager,job,session,case):
 from .boss_recommend import job_identity
 task_id=case.get('task_policy',{}).get('task_id') or job.get('task_id') or 'legacy'
 ident=key(task_id,case['id']);text=render(case.get('desired_greeting',''),case)
 if not text:raise GreetingUnavailable('任务尚未配置首聊话术')
 try:prior=manager.db.get('greeting_readiness',ident)
 except FileNotFoundError:prior={}
 revision=case.get('task_policy',{}).get('revision',0)
 # 11-26 管理员决定“照写不拦”：上次写入未成功且还没到重试时间，不再打开设置页，首聊照常（候选人收到 BOSS 上现有的招呼语）。
 if prior.get('status') in ('blocked','unverified') and prior.get('expected_hash')==stamp(text) and prior.get('task_revision')==revision and prior.get('next_check_at',0)>time.time():return True
 state={'id':ident,'task_id':task_id,'case_id':case['id'],'job_label':case['job']['label'],'job_id':job_identity(case),'task_revision':revision,'expected_text':text,'expected_hash':stamp(text),'status':'checking','checked_at':time.time(),'actual_text':None,'reason':'','next_check_at':None,'settings_write_attempted':False,'save_uncertain':prior.get('save_uncertain',False)}
 async def guard():
  await manager.checkpoint(job)
  current=manager.cases.load(case['id'])
  if hasattr(manager,'account_execution') and job.get('account_runner') and not manager.account_execution.can_send(case['id']):raise GreetingUnavailable('任务已暂停或不在执行窗口，未继续修改')
  if current.get('task_policy',{}).get('revision',0)!=revision or render(current.get('desired_greeting',''),current)!=text:raise GreetingUnavailable('任务话术配置已变化，未继续修改')
 try:
  manager.db.put('greeting_readiness',ident,state)
  await guard();before=await asyncio.wait_for(read_settings(session),60);matches=[r for r in before['rows'] if r.get('title')==case['job']['label']]
  if len(matches)==1:state['actual_text']=matches[0].get('text')
  state['platform_enabled']=before['enabled'];state['matching_rows']=len(matches)
  if not before['enabled']:raise GreetingUnavailable('平台按岗位话术未启用；不改变账号通用设置')
  if len(matches)>1:raise GreetingUnavailable('平台同名岗位话术不唯一；未修改')
  if not matches or state['actual_text']!=text:
   if prior.get('save_uncertain'):raise GreetingUnavailable('上次话术保存结果尚未核实，请管理员核对；不会重复保存')
   state['status']='syncing';state['settings_write_attempted']=True;manager.db.put('greeting_readiness',ident,state)
   def before_save():
    state['save_uncertain']=True;manager.db.put('greeting_readiness',ident,state)
   await synchronize(session,case['job']['label'],text,before,guard,before_save)
   after=await asyncio.wait_for(read_settings(session),60);matching=[r for r in after['rows'] if r.get('title')==case['job']['label']]
   other=lambda v:sorted((r.get('title') or '',r.get('text') or '') for r in v['rows'] if r.get('title')!=case['job']['label'])
   if not after['enabled'] or len(matching)!=1 or matching[0].get('text')!=text or other(after)!=other(before):raise GreetingUnavailable('保存后未核实本岗位正文或其他岗位设置变化，暂停本岗位新增开聊')
   state['actual_text']=text
  await guard();state.update(status='verified',reason='平台当前岗位话术与本任务参数化正文一致',next_check_at=None,save_uncertain=False,checked_at=time.time())
  manager.db.put('greeting_readiness',ident,state)
  latest=manager.cases.load(case['id']);latest.update(platform_greeting_text=text,platform_greeting_checked_at=time.time());manager.cases.save(latest)
  import hashlib
  session._boss_greeting={'job_title':case['job']['label'],'job_id':state['job_id'],'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'checked_at':time.time(),'checked_monotonic':time.monotonic(),'action':'verified'}
  return True
 except Exception as exc:
  if isinstance(exc, __import__('asyncio').CancelledError):raise
  # Account/login/risk controls retain their fatal safety boundary.
  if any(word in str(exc) for word in ('登录','账号存在异常','过于频繁','平台要求验证')):raise
  reason=str(exc) if isinstance(exc,(SourcingError,ValueError)) else '平台话术读取或同步暂未完成（'+getattr(session,'_greeting_stage','核验')+'）；未新增联系'
  # 写入或读回失败只记提示、约 10 分钟后再试写入，不拦首聊；登录、平台验证等页面级问题已在上面原样抛出。
  state.update(status='unverified',reason=reason,next_check_at=time.time()+jitter(600),checked_at=time.time())
  manager.db.put('greeting_readiness',ident,state)
  if prior.get('reason')!=reason or prior.get('expected_hash')!=state['expected_hash']:manager.event(job,'step','首聊话术未能写入 BOSS：'+reason+'；首聊照常进行，候选人会收到 BOSS 上该岗位现有的招呼语，约 10 分钟后再试写入')
  return True


def block(manager,job,case,reason):
 task_id=case.get('task_policy',{}).get('task_id') or job.get('task_id') or 'legacy';ident=key(task_id,case['id'])
 try:state=manager.db.get('greeting_readiness',ident)
 except FileNotFoundError:state={'id':ident,'task_id':task_id,'case_id':case['id'],'job_label':case['job']['label'],'expected_text':case.get('desired_greeting',''),'expected_hash':stamp(case.get('desired_greeting','')),'actual_text':None,'task_revision':case.get('task_policy',{}).get('revision',0)}
 changed=state.get('reason')!=reason or state.get('status')!='blocked'
 state.update(status='blocked',reason=reason,checked_at=time.time(),next_check_at=time.time()+jitter(600))
 manager.db.put('greeting_readiness',ident,state)
 if changed:manager.event(job,'step','仅暂停本岗位新增开聊：'+reason+'；已有会话和其他岗位继续处理')
 return state
