"""Account environment and independent membership controls; no real writes."""
import json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'));a='a'*32;b='b'*32;owner='c'*32
s['definitions']=[{'id':i,'revision':1,'config':{'name':name,'scenario':'寻访','website':'https://www.zhipin.com/','engine':'boss_sourcing'}} for i,name in [(a,'岗位一'),(b,'岗位二')]]
s['account_execution']={'id':'boss:default','label':'BOSS · 当前招聘账号','owner_run_id':owner,'active_case_id':None,'status':'running','browser_url':'http://127.0.0.1:6080/vnc.html','members':[{'id':a,'status':'running'},{'id':b,'status':'running'}]}
s['runs']=[{'id':owner,'status':'running','created':0,'mode':'boss_autopilot','params':{},'account_runner':True}];s['busy']=owner
writes=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if r.request.url==base+'/api/state':r.fulfill(json=s)
  elif r.request.method=='POST':
   body=r.request.post_data_json;writes.append((r.request.url,body))
   if '/api/account-tasks/'+a in r.request.url:s['account_execution']['members'][0]['status']='paused'
   r.fulfill(json={})
  else:r.continue_()
 page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle')
 page.locator('[data-account-task="'+a+'"]').click()
 expect(page.locator('[data-account-task="'+a+'"]').get_by_text('继续')).to_be_visible(timeout=6000)
 expect(page.locator('[data-account-task="'+b+'"]').get_by_text('暂停')).to_be_visible()
 assert writes[0][0].endswith('/api/account-tasks/'+a)
 page.locator('.sidebar [data-nav=environments]').click();page.locator('[data-account-takeover]').click()
 assert writes[-1][0].endswith('/api/runs/'+owner+'/action') and writes[-1][1]=={'action':'takeover'}
 assert not errors,errors
 browser.close();print('independent account task controls and manual takeover: passed (writes intercepted)')
