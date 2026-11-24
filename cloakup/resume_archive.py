"""Local original-file archive. Never send resume contents to a model."""
import asyncio,hashlib,html,io,re,time,uuid,zipfile
from pathlib import Path
from . import boss_chat
from .sourcing import SourcingError

MAX_BYTES=20*1024*1024
# 图片简历（如候选人在 BOSS 直接发来的 jpg/png）按原文件归档，文件头用于识别真实格式。
IMAGE_PNG=b'\x89PNG\r\n\x1a\n'
IMAGE_JPEG=b'\xff\xd8\xff'
RESUME_CONTENT_TYPES=('application/pdf','image/png','image/jpeg')

def save_file(store,case,person,source,data,filename):
 if not data or len(data)>MAX_BYTES:raise SourcingError('简历为空或超过20MB，未归档')
 if data.startswith(b'%PDF-'):ext='pdf';mime='application/pdf'
 elif data.startswith(b'PK'):
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   if 'word/document.xml' not in z.namelist():raise SourcingError('附件不是可识别的Word简历')
   if sum(i.file_size for i in z.infolist())>MAX_BYTES:raise SourcingError('Word解压体积过大')
  ext='docx';mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
 elif data.startswith(IMAGE_PNG):ext='png';mime='image/png'
 elif data.startswith(IMAGE_JPEG):ext='jpg';mime='image/jpeg'
 else:raise SourcingError('仅支持归档PDF、DOCX或图片原文件；未保存错误页或未知内容')
 digest=hashlib.sha256(data).hexdigest()
 old=next((f for f in person.get('resume_files',[]) if f.get('source_ref')==source and f.get('sha256')==digest),None)
 if old:return old
 ident=uuid.uuid4().hex;directory=store.root.parent.parent/'artifacts'/'resumes'/case['id'];directory.mkdir(parents=True,exist_ok=True)
 dest=directory/(ident+'.'+ext);tmp=dest.with_suffix('.tmp');tmp.write_bytes(data);tmp.replace(dest);dest.chmod(0o600)
 name=Path(filename.replace('\\','/')).name
 if not name.lower().endswith('.'+ext):name='简历.'+ext
 record={'id':ident,'case_id':case['id'],'candidate_id':person['id'],'source_ref':source,'filename':name,'extension':ext,'mime':mime,'size':len(data),'sha256':digest,'archived_at':time.time(),'status':'archived'}
 store.db.put('resume_file',ident,record)
 person.setdefault('resume_files',[]).append(record)
 person.setdefault('resume_archives',{})[source]={'status':'archived','file_id':ident,'at':time.time()}
 store.audit(case,'resume_file_archived',candidate_id=person['id'],file_id=ident,size=len(data),sha256=digest,model_disclosure=False)
 return record

def read_file(store,ident):
 if not re.fullmatch('[a-f0-9]{32}',ident):raise FileNotFoundError()
 record=store.db.get('resume_file',ident)
 case=store.load(record['case_id']);person=store.candidate(case,record['candidate_id'])
 if not any(f['id']==ident for f in person.get('resume_files',[])):raise FileNotFoundError()
 path=store.root.parent.parent/'artifacts'/'resumes'/case['id']/(ident+'.'+record['extension'])
 data=path.read_bytes()
 if hashlib.sha256(data).hexdigest()!=record['sha256']:raise SourcingError('归档文件校验失败')
 return record,data

def word_preview(data):
 from xml.etree import ElementTree as ET
 with zipfile.ZipFile(io.BytesIO(data)) as z:root=ET.fromstring(z.read('word/document.xml'))
 ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
 paragraphs=[''.join(t.text or '' for t in p.findall('.//w:t',ns)) for p in root.findall('.//w:p',ns)]
 return ('<!doctype html><meta charset="utf-8"><title>简历归档</title><main>'+''.join('<p>'+html.escape(p)+'</p>' for p in paragraphs)+'</main>').encode()

async def capture(manager,job,session,case_id,candidate_id,snapshot):
 store=manager.cases;case=store.load(case_id);person=store.candidate(case,candidate_id)
 states=person.setdefault('resume_archives',{})
 pending=[m for m in snapshot['messages'] if m.get('resume') and m['direction']=='in' and m.get('resume_selector') and states.get('platform-message:'+m['external_id'],{}).get('status') not in ('archived','failed')]
 if not pending:return
 message=pending[0];source='platform-message:'+message['external_id'];states[source]={'status':'downloading','at':time.time()};store.save(case)
 original=session.page;pages_before=list(session.context.pages);responses=[];tasks=[];downloads=[]
 async def collect(response):
  try:
   if response.status!=200 or not any(t in response.headers.get('content-type','').lower() for t in RESUME_CONTENT_TYPES):return
   if int(response.headers.get('content-length','0'))>MAX_BYTES:return
   page=response.frame.page
   if page is not original and page in pages_before:return
   content=await response.body()
   if (content.startswith(b'%PDF-') or content.startswith(IMAGE_PNG) or content.startswith(IMAGE_JPEG)) and len(content)<=MAX_BYTES:responses.append(content)
  except Exception:pass
 def on_response(response):tasks.append(asyncio.create_task(collect(response)))
 def on_download(download):downloads.append(download)
 def on_page(page):page.on('download',on_download)
 session.context.on('response',on_response);session.context.on('page',on_page);original.on('download',on_download)
 try:
  await manager.checkpoint(job)
  current=await boss_chat.read_conversation(session,**person['conversation'])
  target=next((m for m in current['messages'] if m['external_id']==message['external_id'] and m.get('resume') and m['direction']=='in'),None)
  if not target or not target.get('resume_selector'):raise SourcingError('当前会话无法唯一定位该附件')
  async def click():
   latest=await boss_chat.read_conversation(session,**person['conversation'])
   verified=next((m for m in latest['messages'] if m['external_id']==message['external_id'] and m.get('resume') and m['direction']=='in'),None)
   if not verified or verified.get('resume_selector')!=target['resume_selector']:raise SourcingError('附件入口已变化，未下载')
   loc=session.page.locator(target['resume_selector'])
   if await loc.count()!=1:raise SourcingError('简历附件入口不唯一')
   await loc.click(timeout=5000)
  await session._act('click',click)
  for _ in range(6):
   if downloads or responses:break
   await asyncio.sleep(.5)
  if not downloads and not responses:
   # Some previews expose a download button instead of returning PDF bytes.
   for preview in [p for p in session.context.pages if p not in pages_before]+[original]:
    expression=r'''(()=>{const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};const root=IS_NEW?document.body:document.querySelector('.resume-dialog,.resume-preview,.file-preview,[role="dialog"]');if(!root)return null;const matches=Array.from(root.querySelectorAll('button,a,[role="button"]')).filter(e=>visible(e)&&/^(下载|下载简历|下载附件|保存附件)$/.test((e.innerText||'').trim()));if(matches.length!==1)return null;let e=matches[0],a=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){a.unshift('#'+CSS.escape(e.id));break;}const s=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];a.unshift(e.tagName.toLowerCase()+(s.length>1?':nth-of-type('+(s.indexOf(e)+1)+')':''));}return a.join(' > ')})()'''.replace('IS_NEW','true' if preview is not original else 'false')
    selector=await preview._stealth_world.evaluate(expression)
    if selector:
     await manager.checkpoint(job)
     async def download_click():
      loc=preview.locator(selector)  # noqa: B023 -- 本轮内立即执行，随后 break
      if await loc.count()!=1:raise SourcingError('预览下载入口不唯一')
      await loc.click(timeout=5000)
     await session._act('click',download_click)
     break
   for _ in range(12):
    if downloads or responses:break
    await asyncio.sleep(.5)
  if tasks:
   try:await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),timeout=12)
   except asyncio.TimeoutError:raise SourcingError('简历文件响应超时')
  filename=(re.search(r'([^\n]+\.(?:pdf|docx|jpe?g|png))',message.get('text',''),re.I) or [None,'简历.pdf'])[1]
  if len(downloads)==1:
   download=downloads[0];path=await download.path()
   if not path or Path(path).stat().st_size>MAX_BYTES:raise SourcingError('下载未完成或文件过大')
   data=Path(path).read_bytes();filename=download.suggested_filename
  else:
   unique={hashlib.sha256(b).hexdigest():b for b in responses}
   if len(unique)!=1:raise SourcingError('未取得唯一PDF原文件；保留引用，等待重新归档')
   data=next(iter(unique.values()))
  case=store.load(case_id);person=store.candidate(case,candidate_id)
  person.pop('resume_archive_requested',None)
  save_file(store,case,person,source,data,filename)
 except asyncio.CancelledError:
  case=store.load(case_id);person=store.candidate(case,candidate_id);person.setdefault('resume_archives',{})[source]={'status':'failed','error':'归档中断，可手动重新归档','at':time.time()};store.save(case);raise
 except Exception as exc:
  case=store.load(case_id);person=store.candidate(case,candidate_id);person.pop('resume_archive_requested',None);person.setdefault('resume_archives',{})[source]={'status':'failed','error':str(exc)[:300],'at':time.time()};store.audit(case,'resume_archive_failed',candidate_id=candidate_id,reason=str(exc)[:300])
 finally:
  session.context.remove_listener('response',on_response);session.context.remove_listener('page',on_page)
  original.remove_listener('download',on_download)
  for task in tasks:
   if not task.done():task.cancel()
  for page in session.context.pages:
   if page not in pages_before:
    page.remove_listener('download',on_download)
    if not page.is_closed():await page.close()
  session.page=original
  try:await session.press_key('Escape')
  except Exception:pass
