"""Operations and administrator inbox UI; all writes intercepted."""
import json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'))
s['attention']=[{'id':'a'*32,'kind':'question','title':'候选人问题需顾问确认','detail':'需要核实岗位条件','created_at':1794700000,'status':'pending','history':[{'at':1794700000,'action':'created'}]}]
s['busy']=None;writes=[]
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 ctx=b.new_context(viewport={'width':1440,'height':1000})
 def route(r):
  if r.request.url==base+'/api/state':r.fulfill(json=s);return
  if r.request.method=='POST':
   assert '/api/attention/' in r.request.url
   v=r.request.post_data_json;writes.append(v);s['attention'][0].update(**v);r.fulfill(json=s['attention'][0]);return
  assert r.request.url.startswith(base);r.continue_()
 ctx.route('**/*',route);page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto(base,wait_until='networkidle');expect(page.get_by_role('heading',name='工作台',exact=True)).to_be_visible()
 expect(page.get_by_role('heading',name='任务进度',exact=True)).to_be_visible();page.locator('.sidebar [data-nav=environments]').click()
 expect(page.locator('.runtime-panel')).to_be_visible()
 assert page.locator('.topbar [data-open-browser]').count()==0
 assert page.locator('.runtime-panel [data-open-browser]').count()==1
 page.locator('[data-nav=attention]').first.click();expect(page.get_by_role('heading',name='待办中心',exact=True)).to_be_visible()
 page.locator('textarea[name=note]').fill('顾问已确认并接续处理')
 page.get_by_role('button',name='记录处理完成',exact=True).click()
 expect(page.get_by_role('heading',name='暂无待处理事项',exact=True)).to_be_visible()
 assert writes==[{'status':'resolved','note':'顾问已确认并接续处理'}]
 page.locator('#todo-filter').select_option('resolved');expect(page.get_by_text('顾问已确认并接续处理',exact=True)).to_be_visible()
 for width in [1024,760,390,320]:
  page.set_viewport_size({'width':width,'height':900})
  for nav in ['operations','attention']:
   page.locator('.sidebar [data-nav='+nav+']').first.click()
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,nav)
 assert not errors,errors;b.close()
 print(json.dumps({'operations':'passed','todo_resolution':'passed','responsive':'passed','page_errors':errors}))
