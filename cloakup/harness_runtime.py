"""Official SDK runtime: pooled process, isolated logical sessions, text bridge."""
import asyncio,json,secrets,threading,time,uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import jsonschema
from .client import default_model
from .llm import _first_json
SDK_VERSION='0.2.2rc2'
PATCH=Path(__file__).with_name('harness')/'planner.patch.yml'

class HarnessPlanner:
 def __init__(self,llm,root,persistent=False):
  self.client=llm.client;self.model=getattr(llm,'model',None) or default_model()
  self.root=Path(root);self.last_trace=None;self.persistent=persistent;self._runtimes={};self._lock=asyncio.Lock();self.starts=0
 async def _open(self,system):
  import hashlib
  key=hashlib.sha256(system.encode()).hexdigest();loop=asyncio.get_running_loop()
  if key in self._runtimes and self._runtimes[key]['loop'] is loop:return self._runtimes[key],True
  if key in self._runtimes:await self._close(self._runtimes.pop(key))
  if len(self._runtimes)>=2:
   old=self._runtimes.pop(next(iter(self._runtimes)));await self._close(old)
  token=secrets.token_urlsafe(32);wire=[];pending=set();upstream=self.client;model=self.model
  home=self.root/'harness'/'runtimes'/uuid.uuid4().hex;workspace=home/'workspace';workspace.mkdir(parents=True,exist_ok=True)
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_POST(self):
    if self.headers.get('x-api-key')!=token or self.path!='/v1/messages':self.send_error(403);return
    future=None
    try:
     size=int(self.headers.get('Content-Length','0'))
     if not 0<size<=1048576:raise ValueError('request too large')
     body=json.loads(self.rfile.read(size))
     if body.get('tools'):raise ValueError('native execution tools disabled')
     messages=body.get('messages',[])
     for message in messages:
      if message.get('role') not in ('user','assistant') or not isinstance(message.get('content'),list) or any(b.get('type')!='text' for b in message['content']):raise ValueError('text blocks only')
     entry={'at':time.time(),'message_count':len(messages),'tools':0};wire.append(entry)
     future=asyncio.run_coroutine_threadsafe(upstream.create(model=model,max_tokens=min(body.get('max_tokens',4096),8192),system=body.get('system',''),messages=messages),loop)
     pending.add(future)
     response=future.result(timeout=100);pending.discard(future);text=''.join(b.text for b in response.content if b.type=='text');usage=getattr(response,'usage',None)
     if getattr(response,'stop_reason','end_turn') not in ('end_turn','stop_sequence'):raise ValueError('incomplete output')
     entry.update(input_tokens=getattr(usage,'input_tokens',0) or 0,output_tokens=getattr(usage,'output_tokens',0) or 0)
     self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
     events=[('message_start',{'type':'message_start','message':{'id':'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant','model':model,'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':entry['input_tokens'],'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':entry['output_tokens']}}),('message_stop',{'type':'message_stop'})]
     for event,data in events:self.wfile.write(('event: '+event+'\ndata: '+json.dumps(data,ensure_ascii=False)+'\n\n').encode())
     self.wfile.flush()
    except Exception:
     if future:
      pending.discard(future)
      if not future.done():future.cancel()
     self.send_error(502,'Harness gateway request failed')
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
  def construct():
   from deepseek_harness import DeepSeekHarness
   return DeepSeekHarness(dsh_home=str(home),cwd=str(workspace),profile='sdk-minimal',patches=(str(PATCH.resolve()),),
    # Application placeholders are literal prose, never DSH prompt variables.
    env={'DSH_SYSTEM_PROMPT':system.replace('{{','｛｛').replace('}}','｝｝'),'HOME':str(home),'ANTHROPIC_API_KEY':'','ANTHROPIC_AUTH_TOKEN':'','CLOAKBROWSER_LICENSE_KEY':'','DATABASE_URL':'','DSH_PRIMARY_RUNTIME':''},
    base_url='http://127.0.0.1:'+str(server.server_port),api_key=token,provider='deepseek-official',model=model,reasoning_effort='off',max_tokens=4096,initialize_timeout_seconds=30,request_timeout_seconds=30)
  try:harness=await asyncio.to_thread(construct)
  except BaseException:await asyncio.to_thread(server.shutdown);server.server_close();raise
  rt={'harness':harness,'server':server,'wire':wire,'loop':loop,'home':str(home),'pending':pending};self._runtimes[key]=rt;self.starts+=1
  return rt,False
 async def _close(self,runtime):
  for future in list(runtime.get('pending',[])):future.cancel()
  try:await asyncio.to_thread(runtime['harness'].close)
  finally:await asyncio.to_thread(runtime['server'].shutdown);runtime['server'].server_close()
 async def aclose(self):
  runtimes=list(self._runtimes.values());self._runtimes.clear()
  for runtime in runtimes:await self._close(runtime)
 async def json(self,*,system,content,schema,**options):
  if not isinstance(content,str):raise ValueError('Harness只接受已授权文字')
  async with self._lock:
   started=time.monotonic()
   ident=uuid.uuid4().hex;home=self.root/'harness'/'calls'/ident;home.mkdir(parents=True,exist_ok=True)
   self.last_trace={'runtime':'deepseek-harness','version':SDK_VERSION,'session_id':ident,'home':str(home),'tool_access':'disabled','finish_reason':'running'}
   rt=None
   try:
    rt,reused=await self._open(system);offset=len(rt['wire'])
    def run():
     prompt=content+'\n只输出符合Schema的完整JSON：\n'+json.dumps(schema,ensure_ascii=False);count=0
     for correction in range(3):
      result=rt['harness'].run(prompt,session_id=ident);count+=len(result.events)
      if result.finish_reason!='completed':raise RuntimeError('Harness未正常完成')
      try:data=_first_json(result.final_response);jsonschema.validate(data,schema);return data,result,count,correction
      except (ValueError,jsonschema.ValidationError) as exc:
       if correction==2:raise ValueError('Harness连续三次未返回有效JSON') from exc
       prompt='上一轮格式未通过校验，请保持目标和事实，修正为完整JSON，字符串换行需转义。Schema：'+json.dumps(schema,ensure_ascii=False)
    data,result,count,correction=await asyncio.wait_for(asyncio.to_thread(run),120)
    calls=rt['wire'][offset:]
    self.last_trace.update(session_id=result.session_id,runtime_home=rt['home'],event_count=count,correction_rounds=correction,gateway_requests=len(calls),finish_reason='completed',runtime_reused=reused,runtime_starts=self.starts,input_tokens=sum(x.get('input_tokens',0) for x in calls),output_tokens=sum(x.get('output_tokens',0) for x in calls))
    return data
   except BaseException as exc:
    self.last_trace.update(finish_reason='failed',error=f'{type(exc).__name__}: {str(exc)[:300]}')
    await self.aclose();raise
   finally:
    self.last_trace['elapsed_ms']=int((time.monotonic()-started)*1000)
    if rt:self.last_trace.setdefault('gateway_requests',len(rt['wire'][offset:]))
    (home/'cloakup-trace.json').write_text(json.dumps(self.last_trace,ensure_ascii=False,indent=2))
    from .logbook import log
    log('model',type='harness_call',**{k:self.last_trace.get(k) for k in ('session_id','finish_reason','elapsed_ms','event_count','correction_rounds','gateway_requests','runtime_reused','error')})
    if not self.persistent:await self.aclose()


def get_harness(llm,root,scope):
 client=llm.client;cache=getattr(client,'_cloakup_harness_pool',None)
 if cache is None:cache={};client._cloakup_harness_pool=cache
 key=(str(Path(root).resolve()),scope,getattr(llm,'model',None) or default_model())
 if key not in cache:cache[key]=HarnessPlanner(llm,root);cache[key].persistent=True
 return cache[key]

async def close_harnesses(client):
 cache=getattr(client,'_cloakup_harness_pool',{})
 for engine in list(cache.values()):await engine.aclose()
 cache.clear()
