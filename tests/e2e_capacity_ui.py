"""Official capacity and safe release action; all writes intercepted."""
import json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'))
s['browser_capacity']={'license':{'state':'ok','plan':'free','limit':1,'active':1,'available':0,'checked_at':1794800000},'local_running':0,'local_retained':1,'queued':1,'can_release_boss':True};writes=[]
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if r.request.url==base+'/api/state':r.fulfill(json=s)
  elif r.request.method=='POST':
   assert r.request.url.endswith('/api/browser-capacity');writes.append(r.request.post_data_json)
   if writes[-1]['action']=='release':s['browser_capacity']['can_release_boss']=False
   r.fulfill(json={'status':'releasing'})
  else:r.continue_()
 page.route('**/*',route);page.goto(base+'/?view=environments',wait_until='networkidle')
 expect(page.get_by_role('heading',name='浏览器实例容量',exact=True)).to_be_visible()
 expect(page.locator('.capacity-summary strong').first).to_have_text('1')
 page.get_by_role('button',name='刷新容量',exact=True).click();page.get_by_role('button',name='释放 BOSS 浏览器席位',exact=True).click()
 expect(page.get_by_role('button',name='释放 BOSS 浏览器席位',exact=True)).to_be_disabled()
 assert writes==[{'action':'refresh'},{'action':'release'}]
 for width in [1024,760,390,320]:
  page.set_viewport_size({'width':width,'height':900});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 assert not errors,errors;b.close();print('capacity counts / refresh / release / responsive: passed (writes intercepted)')
