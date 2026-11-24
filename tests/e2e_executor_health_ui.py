"""Readiness and task diagnostics UI; every request uses a synthetic state."""
import copy,json,threading,urllib.request
from pathlib import Path
from functools import partial
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from playwright.sync_api import sync_playwright,expect
s=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state'));t=copy.deepcopy(next(t for t in s['definitions'] if t['config']['engine']=='boss_sourcing'));tid='b'*32;t.update(id=tid);t['config'].update(name='执行器验收任务',case_ids=[])
owner={'id':'c'*32,'status':'running','account_runner':True,'params':{},'active_case_id':None,'created':1}
s.update(definitions=[t],cases=[],runs=[owner],busy=owner['id'],attention=[],executor_checks=[],readiness={'id':'d'*32,'status':'passed','fresh':True,'checks':[{'key':'executor','title':'执行器完整链路','status':'passed','detail':'已验证合成页面'}],'executor':{'steps':[{'name':'原生输入','status':'passed','duration_ms':10},{'name':'结果回读与单次执行核验','status':'passed','duration_ms':10}]}})
s['account_execution'].update(owner_run_id=owner['id'],members=[{'id':tid,'status':'paused'}]);s['runtime_status'].update(selection_recovery=[])
class Quiet(SimpleHTTPRequestHandler):
 def log_message(self,*a):pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(Path('cloakup/web').resolve())));threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
try:
 with sync_playwright() as p:
  b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page();errors=[];calls=[];page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   if r.request.url==base+'/api/state':r.fulfill(json=s)
   elif r.request.url.endswith('/browser-diagnostics'):
    calls.append(r.request.method);report={'id':'e'*32,'task_id':tid,'checked_at':1,'mode':'page_read_only','probes':[{'name':'guarded_recovery_dom','ok':True,'duration_ms':10}]};s['executor_checks']=[report];r.fulfill(json=report)
   elif r.request.method!='GET':r.abort()
   elif r.request.url.startswith(base+'/'):r.continue_()
   else:r.abort()
  page.route('**/*',route);page.goto(base+'/?view=readiness',wait_until='networkidle')
  expect(page.get_by_role('button',name='快速测试',exact=True)).to_be_enabled()
  expect(page.get_by_role('heading',name='隔离执行器核验明细',exact=True)).to_be_visible()
  expect(page.get_by_role('heading',name='任务执行器核验',exact=True)).to_be_visible()
  page.get_by_role('button',name='检查当前页面',exact=True).click();expect(page.get_by_text('隔离读取与保护检查',exact=True)).to_be_visible();assert calls==['GET']
  s['account_execution']['members'][0]['status']='running';page.evaluate('refresh(true)')
  expect(page.get_by_role('button',name='快速测试',exact=True)).to_be_disabled();expect(page.get_by_role('button',name='检查当前页面',exact=True)).to_be_disabled()
  assert not errors,errors
  b.close();print('executor readiness steps, persisted task diagnostics, idle-only checks: passed; real writes: 0')
finally:server.shutdown()
