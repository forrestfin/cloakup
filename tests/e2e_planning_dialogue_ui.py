"""Exercise planning conversation and answer feedback; all writes intercepted."""
import copy,json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'));t=next(t for t in s['definitions'] if t['config']['engine']=='boss_sourcing');cid=t['config']['case_ids'][0];writes=[]
s['planning_dialogues']=[];s['planning_requests']=[]
s['attention']=[{'id':'a'*32,'kind':'question','status':'pending','title':'候选人问题需顾问确认','detail':'请补充已确认信息','created_at':1794800000,'history':[],'case_id':cid,'candidate_id':'synthetic','job_label':'测试岗位','question':'请问结算周期是多久？','reply_id':'q'}]
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if r.request.url==base+'/api/state':r.fulfill(json=s)
  elif r.request.method=='POST':
   data=r.request.post_data_json;writes.append((r.request.url,data));r.fulfill(json={'id':'request'})
  else:r.continue_()
 page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle');page.locator('[data-task-workspace="'+t['id']+'"]').first.click();page.locator('[data-workspace-tab=planning]').click()
 expect(page.get_by_role('heading',name='规划对话',exact=True)).to_be_visible()
 form=page.locator('[data-planning-chat]');form.locator('[name=message]').fill('先处理待答问题，避免重复索要简历');form.locator('[name=reviewed]').check();form.get_by_role('button',name='发送并更新计划').click();page.wait_for_timeout(500)
 assert writes[-1][0].endswith('/planning-message') and writes[-1][1]['reviewed']
 page.screenshot(path='/tmp/cloakup-planning-dialogue.png',full_page=True)
 page.locator('.sidebar [data-nav=attention]').click();page.locator('summary').filter(has_text='补充确认答案并重规划').click()
 form=page.locator('[data-answer-todo]');form.locator('[name=message]').fill('合作结算周期');form.locator('[name=answer]').fill('按月结算。');form.locator('[name=reviewed]').check();form.get_by_role('button',name='保存答案并重规划').click();page.wait_for_timeout(500)
 assert writes[-1][0].endswith('/answer') and writes[-1][1]['answer']=='按月结算。'
 assert writes[-1][1]['message']!='请问结算周期是多久？'
 s['planning_requests']=[{'id':'f'*32,'task_id':t['id'],'status':'failed','error':'网关暂不可用'}];page.locator('.sidebar [data-nav=tasks]').click();page.locator('[data-task-workspace="'+t['id']+'"]').first.click();page.locator('[data-workspace-tab=planning]').click();page.wait_for_timeout(1500)
 page.get_by_role('button',name='重试规划',exact=True).click();page.wait_for_timeout(300);assert writes[-1][0].endswith('/retry')
 assert not errors,errors;b.close();print('planning chat + reviewed answer feedback + retry UI: passed (all writes intercepted)')
