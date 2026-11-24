"""Five destinations and task-owned business data; no real writes."""
import json,copy,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'));writes=[]
# A second local-only case proves task result and audit scoping.
first=s['cases'][0];other=copy.deepcopy(first);other['id']='e'*32;other['job']['label']='其他任务的岗位';other['candidates']={};other['audit']=[];s['cases'].append(other)
t=next(t for t in s['definitions'] if first['id'] in t['config'].get('case_ids',[]))
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=b.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if r.request.url==base+'/api/state':r.fulfill(json=s)
  elif r.request.method=='POST':writes.append(r.request.post_data_json);r.fulfill(json={})
  else:r.continue_()
 page.route('**/*',route);page.goto(base,wait_until='networkidle')
 expect(page.get_by_role('heading',name='工作台',exact=True)).to_be_visible()
 assert page.locator('.sidebar [data-nav]').count()==5
 page.locator('.sidebar [data-nav=tasks]').click();page.locator('[data-task-workspace="'+t['id']+'"]').first.click()
 for tab in ['overview','strategy','results','records']:
  page.locator('[data-workspace-tab='+tab+']').click()
  assert page.locator('h1').count()==1,(tab,page.locator('h1').all_text_contents())
  if tab=='strategy':expect(page.get_by_role('button',name='保存岗位策略',exact=True)).to_be_visible()
  if tab=='results':assert page.locator('#chat-job option').all_text_contents()==['全部岗位',first['job']['label']]
  if tab=='records':assert '其他任务的岗位' not in page.locator('#audit-job').inner_text()
 for nav in ['environments','attention','settings','operations']:
  page.locator('.sidebar [data-nav='+nav+']').click()
  if nav=='environments':page.locator('[data-environment-tab=health]').click();expect(page.get_by_role('button',name='快速测试',exact=True)).to_be_visible()
 for width in [1024,760,390,320]:
  page.set_viewport_size({'width':width,'height':900})
  for nav in ['operations','tasks','attention','environments','settings']:
   page.locator('.sidebar [data-nav='+nav+']').click()
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,nav)
 assert not errors,errors
 assert not writes,writes
 page.screenshot(path='/tmp/cloakup-workspace-home.png',full_page=True)
 b.close();print('five destinations, scoped task tabs, environment health, responsive layout: passed')
