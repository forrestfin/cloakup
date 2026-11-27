import io,zipfile
import pytest
from cloakup.sourcing import CaseStore,SourcingError
from cloakup.resume_archive import save_file,read_file,word_preview

def env(tmp_path):
 store=CaseStore(tmp_path);case=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j');p=store.add_candidate(case,'p','https://www.zhipin.com/web/chat/index',{'summary':'职业经历'})
 return store,case,p

def test_pdf_persisted_and_viewable_without_platform_session(tmp_path):
 store,c,p=env(tmp_path);data=b'%PDF-1.4\n test original file'
 f=save_file(store,c,p,'platform-message:file',data,'../../resume.pdf')
 assert f['filename']=='resume.pdf' and f['size']==len(data)
 assert save_file(store,c,p,'platform-message:file',data,'resume.pdf')['id']==f['id']
 restored=CaseStore(tmp_path);record,body=read_file(restored,f['id']);assert body==data and record['candidate_id']==p['id']
 assert restored.candidate(restored.load(c['id']),p['id'])['resume_archives']['platform-message:file']['status']=='archived'
 with pytest.raises(FileNotFoundError):read_file(restored,'../../etc/passwd')

def test_unknown_or_html_file_never_marked_archived(tmp_path):
 store,c,p=env(tmp_path)
 with pytest.raises(SourcingError):save_file(store,c,p,'ref',b'<html>login</html>','resume.pdf')
 assert not p.get('resume_files')

def test_word_preview_escapes_text_and_preserves_original(tmp_path):
 store,c,p=env(tmp_path);out=io.BytesIO()
 with zipfile.ZipFile(out,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>&lt;script&gt;hello&lt;/script&gt;</w:t></w:r></w:p></w:document>')
 data=out.getvalue();f=save_file(store,c,p,'ref',data,'resume.docx');assert read_file(store,f['id'])[1]==data
 preview=word_preview(data);assert b'&lt;script&gt;' in preview and b'<script>' not in preview

def test_archive_integrity_detects_changed_file(tmp_path):
 store,c,p=env(tmp_path);f=save_file(store,c,p,'ref',b'%PDF-1.4 original','resume.pdf')
 path=tmp_path/'artifacts'/'resumes'/c['id']/(f['id']+'.pdf');path.write_bytes(b'%PDF-1.4 altered')
 with pytest.raises(SourcingError):read_file(store,f['id'])

async def test_browser_pdf_response_is_archived_for_exact_received_attachment(tmp_path,monkeypatch):
 from types import SimpleNamespace as NS
 from cloakup import resume_archive as ar
 store,c,p=env(tmp_path);p['conversation']={'chat_id':'12-0','receiver':'甲','job_title':'岗位'};store.attach_resume(c,p['id'],'platform-message:received')
 snap={'valid':True,'chat_id':'12-0','messages':[{'external_id':'received','direction':'in','resume':True,'resume_selector':'#file','text':'resume.pdf\n点击预览附件简历'}]}
 class Emitter:
  def __init__(self):self.events={}
  def on(self,event,fn):self.events.setdefault(event,[]).append(fn)
  def remove_listener(self,event,fn):self.events[event].remove(fn)
 context=Emitter();page=Emitter();context.pages=[page]
 class Response:
  status=200;headers={'content-type':'application/pdf'};frame=NS(page=page)
  async def body(self):return b'%PDF-1.4 real browser response'
 class Locator:
  async def count(self):return 1
  async def click(self,**kw):
   for fn in context.events['response']:fn(Response())
 page.locator=lambda selector:Locator()
 class Session:
  async def _act(self,kind,fn):return await fn()
  async def press_key(self,key):pass
 session=Session();session.page=page;session.context=context
 async def checkpoint(*a):pass
 async def read(*a,**kw):return snap
 monkeypatch.setattr(ar.boss_chat,'read_conversation',read)
 await ar.capture(NS(cases=store,checkpoint=checkpoint),{'id':'run'},session,c['id'],p['id'],snap)
 saved=store.candidate(store.load(c['id']),p['id']);assert len(saved['resume_files'])==1
 assert read_file(store,saved['resume_files'][0]['id'])[1]==b'%PDF-1.4 real browser response'
 assert not context.events['response'] and not page.events['download']

def test_image_resume_archived_as_original_and_marked_unscored(tmp_path):
 import asyncio
 from cloakup import resume_scoring
 from cloakup.resume_archive import IMAGE_JPEG,IMAGE_PNG
 store,c,p=env(tmp_path)
 jpg=IMAGE_JPEG+b'\x00\x10JFIF test original';png=IMAGE_PNG+b'test original'
 f=save_file(store,c,p,'platform-message:image',jpg,'简历.jpg');assert f['extension']=='jpg' and f['mime']=='image/jpeg' and f['filename']=='简历.jpg'
 g=save_file(store,c,p,'platform-message:png',png,'resume.jpeg');assert g['extension']=='png' and g['filename']=='简历.png'
 assert read_file(store,f['id'])[1]==jpg
 record,data=read_file(store,f['id'])
 with pytest.raises(resume_scoring.ScoringUnavailable):resume_scoring.extract_text(record,data)
 assert not resume_scoring.ScoringUnavailable.__dict__.get('retryable',False)

def test_unknown_binary_still_rejected(tmp_path):
 store,c,p=env(tmp_path)
 with pytest.raises(SourcingError):save_file(store,c,p,'ref',b'GIF89a\x01\x00',"resume.gif")

async def test_browser_image_response_is_archived_for_received_attachment(tmp_path,monkeypatch):
 from types import SimpleNamespace as NS
 from cloakup import resume_archive as ar
 from cloakup.resume_archive import IMAGE_JPEG
 store,c,p=env(tmp_path);p['conversation']={'chat_id':'12-0','receiver':'甲','job_title':'岗位'};store.attach_resume(c,p['id'],'platform-message:received')
 snap={'valid':True,'chat_id':'12-0','messages':[{'external_id':'received','direction':'in','resume':True,'resume_selector':'#file','text':'简历.jpg\n点击预览附件简历'}]}
 class Emitter:
  def __init__(self):self.events={}
  def on(self,event,fn):self.events.setdefault(event,[]).append(fn)
  def remove_listener(self,event,fn):self.events[event].remove(fn)
 context=Emitter();page=Emitter();context.pages=[page]
 original_body=IMAGE_JPEG+b'\x00\x10JFIF browser image'
 class Response:
  status=200;headers={'content-type':'image/jpeg'};frame=NS(page=page)
  async def body(self):return original_body
 class Locator:
  async def count(self):return 1
  async def click(self,**kw):
   for fn in context.events['response']:fn(Response())
 page.locator=lambda selector:Locator()
 class Session:
  async def _act(self,kind,fn):return await fn()
  async def press_key(self,key):pass
 session=Session();session.page=page;session.context=context
 async def checkpoint(*a):pass
 async def read(*a,**kw):return snap
 monkeypatch.setattr(ar.boss_chat,'read_conversation',read)
 await ar.capture(NS(cases=store,checkpoint=checkpoint),{'id':'run'},session,c['id'],p['id'],snap)
 saved=store.candidate(store.load(c['id']),p['id']);assert len(saved['resume_files'])==1
 assert saved['resume_files'][0]['extension']=='jpg' and saved['resume_files'][0]['filename']=='简历.jpg'
 assert read_file(store,saved['resume_files'][0]['id'])[1]==original_body


class _Emitter:
 def __init__(self):self.pages=[]
 def on(self,*a):pass
 def remove_listener(self,*a):pass
 def is_closed(self):return False


def _interrupted_capture(tmp_path):
 import asyncio
 from types import SimpleNamespace as NS
 from cloakup.resume_archive import capture
 store,case,p=env(tmp_path);person=store.candidate(case,p['id']);person['conversation']={'chat_id':'1-0'};store.save(case)
 async def stop(job):raise asyncio.CancelledError()
 async def press(*a):pass
 page=_Emitter();session=NS(page=page,context=_Emitter(),press_key=press)
 manager=NS(cases=store,checkpoint=stop)
 snap={'messages':[{'external_id':'m1','direction':'in','resume':True,'resume_selector':'#x','text':'某某-个人简历.pdf\n点击预览附件简历'}]}
 with pytest.raises(asyncio.CancelledError):asyncio.run(capture(manager,{},session,case['id'],p['id'],snap))
 return store,case,p,manager,session,snap


def test_interrupted_archive_is_retried_on_next_visit(tmp_path):
 # 11-26 17:45 qe工程师 周全起：简历刚收到 4 秒，服务重建打断下载，被记为“失败”，之后拜访不再下载，也没推到飞书群。
 import asyncio
 from cloakup.resume_archive import capture
 store,case,p,manager,session,snap=_interrupted_capture(tmp_path)
 state=store.candidate(store.load(case['id']),p['id'])['resume_archives']['platform-message:m1']
 assert state['status']=='interrupted'
 calls=[]
 async def seen(job):calls.append(job);raise asyncio.CancelledError()
 manager.checkpoint=seen
 with pytest.raises(asyncio.CancelledError):asyncio.run(capture(manager,{},session,case['id'],p['id'],snap))
 assert calls, '中断的归档应在下次拜访重新下载'


def test_legacy_interrupted_failure_is_also_retried(tmp_path):
 # 11-26 修复前被记成 failed／“归档中断，可手动重新归档”的记录（周全起）也要自动重试。
 import asyncio
 from cloakup.resume_archive import capture
 store,case,p,manager,session,snap=_interrupted_capture(tmp_path)
 fresh=store.load(case['id']);store.candidate(fresh,p['id'])['resume_archives']['platform-message:m1']={'status':'failed','error':'归档中断，可手动重新归档','at':1};store.save(fresh)
 calls=[]
 async def seen(job):calls.append(job);raise asyncio.CancelledError()
 manager.checkpoint=seen
 with pytest.raises(asyncio.CancelledError):asyncio.run(capture(manager,{},session,case['id'],p['id'],snap))
 assert calls
 fresh=store.load(case['id']);store.candidate(fresh,p['id'])['resume_archives']['platform-message:m1']={'status':'failed','error':'未取得唯一PDF原文件','at':1};store.save(fresh)
 calls.clear();asyncio.run(capture(manager,{},session,case['id'],p['id'],snap))
 assert not calls, '真正失败的不自动重试，等管理员处理'


def test_executor_revisits_conversations_with_unfinished_archives():
 # 11-26 周全起：中断的归档要让执行器主动回到会话重试，不只等对话自然有新消息。
 from cloakup.resume_archive import archive_pending
 ref='platform-message:m1'
 person=lambda state:{'resumes':[ref],'resume_archives':{ref:state} if state is not None else {}}
 assert archive_pending(person(None)) and archive_pending(person({'status':'queued'})) and archive_pending(person({'status':'downloading'}))
 assert archive_pending(person({'status':'interrupted'})) and archive_pending(person({'status':'failed','error':'归档中断，可手动重新归档'}))
 assert not archive_pending(person({'status':'failed','error':'未取得唯一PDF原文件'})) and not archive_pending(person({'status':'archived'}))
 assert archive_pending({'resume_archive_requested':True}) and not archive_pending(None)
