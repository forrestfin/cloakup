"""Source UI with a synthetic generic task; all mutations intercepted."""
import copy,json,threading,urllib.request
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright,expect
s=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state'));generic=next((t for t in s['definitions'] if t['config']['engine']=='web'),None);assert generic
s=copy.deepcopy(s);t=next(t for t in s['definitions'] if t['id']==generic['id']);t['config']['planning_prompt']='Read the public catalogue and verify results.'
s['runs']=[{'id':'f'*32,'task_id':t['id'],'mode':'automation_task','status':'succeeded','created':1794800000,'task':t['config']['name'],'task_snapshot':t,'params':{},'events':[], 'harness_steps':[{'at':1794800000,'tool':'click','reason':'Read the Poetry category','result':'Navigated to category','phase':'implementation','harness_trace':{'session_id':'native-public-test','event_count':13}}]}]
writes=[]
class Quiet(SimpleHTTPRequestHandler):
 def log_message(self,*a):pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(Path(__file__).resolve().parents[1]/'cloakup/web')));threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
try:
 with sync_playwright() as p:
  b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   if '/api/state' in r.request.url:r.fulfill(json=s)
   elif r.request.method=='POST':writes.append(r.request.post_data_json);r.fulfill(json={'id':t['id'],'revision':2})
   else:r.continue_()
  page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle');page.locator('[data-task-workspace="'+t['id']+'"]').first.click()
  expect(page.get_by_role('heading',name='持续决策',exact=True)).to_be_visible();page.locator('.audit-item summary').filter(has_text='click').first.click();expect(page.get_by_text('Read the Poetry category',exact=True)).to_be_visible()
  page.screenshot(path='/tmp/cloakup-universal-harness-ui.png',full_page=True)
  page.get_by_role('button',name='编辑目标与参数',exact=True).click();page.locator('summary').filter(has_text='规划提示词（可编辑）').click();page.locator('#task-planning-prompt').fill('Read only public pages and verify each result.');page.get_by_role('button',name='保存任务',exact=True).first.click();page.wait_for_timeout(300);assert writes[-1]['config']['planning_prompt']=='Read only public pages and verify each result.'
  page.locator('.sidebar [data-nav=settings]').click();expect(page.get_by_text('通用网站执行器已通过 Harness 持续选择动作并核验工具结果。',exact=False)).to_be_visible();assert not errors,errors;b.close();print('generic task prompt editor + Harness decision trace + runtime UI: passed (writes intercepted)')
finally:server.shutdown();server.server_close()
