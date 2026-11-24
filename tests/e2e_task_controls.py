"""Task controls use row-owned IDs; all mutations intercepted."""
import json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'))
s['definitions']=[{'id':'a'*32,'revision':1,'config':{'name':'测试任务','scenario':'测试场景','website':'https://example.com','engine':'web'}}]
s['runs']=[{'id':'b'*32,'task_id':'a'*32,'status':'running','params':{},'created':0}];s['busy']='b'*32;s['runtime_status']['task_id']='b'*32;s['runtime_status']['browser_alive']=True;writes=[]
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if r.request.url==base+'/api/state':r.fulfill(json=s)
  elif r.request.method=='POST':
   body=r.request.post_data_json;writes.append((r.request.url,body))
   if body.get('action')=='pause':s['runs'][0]['status']='paused'
   if body.get('action')=='resume':s['runs'][0]['status']='running'
   r.fulfill(json={'id':'c'*32})
  else:r.continue_()
 page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle')
 row=page.locator('tbody tr').first;row.get_by_role('button',name='暂停',exact=True).click()
 expect(row.get_by_role('button',name='继续',exact=True)).to_be_visible(timeout=6000)
 row.get_by_role('button',name='继续',exact=True).click()
 expect(row.get_by_role('button',name='暂停',exact=True)).to_be_visible(timeout=6000)
 assert all('/api/runs/'+'b'*32+'/action' in url for url,_ in writes)
 assert row.get_by_role('link',name='打开执行浏览器').get_attribute('href').startswith('http://127.0.0.1:6080/')
 s['runs'][0]['status']='succeeded';s['busy']=None
 expect(row.get_by_role('button',name='启动',exact=True)).to_be_visible(timeout=6000)
 row.get_by_role('button',name='启动',exact=True).click();page.wait_for_timeout(300)
 assert writes[-1][1]['task_id']=='a'*32
 assert not errors,errors
 b.close();print('task row pause/resume/start/browser: passed; real mutations intercepted')
