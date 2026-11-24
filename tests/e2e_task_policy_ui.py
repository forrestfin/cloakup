"""Task policy UI acceptance against local source; all writes stay in memory."""
import copy,json,threading,urllib.request
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright,expect

state=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state'))
task=next(t for t in state['definitions'] if t['config']['engine']=='boss_sourcing')
first=copy.deepcopy(next(c for c in state['cases'] if c['id']==task['config']['case_ids'][0]))
first['id']='a'*32;first['job']['label']='测试岗位甲';first['candidates']={};first['audit']=[]
second=copy.deepcopy(first);second['id']='b'*32;second['job']['label']='测试岗位乙'
state['cases']=[first,second];state['definitions']=[task];state['attention']=[];state['runs']=[];state['busy']=None
state['task_plans']=[];state['planning_requests']=[];state['planning_dialogues']=[]
state['notifications']={'configured':False,'records':[]};state['account_execution']['members']=[];state['account_execution']['owner_run_id']=None
cfg=task['config'];cfg.update(name='高端岗位 · 寻访',case_ids=[first['id'],second['id']],sourcing_mode='continuous',
 goal='在工作窗口内持续寻访，尽可能取得更多合适候选人的简历，电话和微信非必需。',
 execution={**first['editable_config']['execution'],'nudge_unread_hours':24,'nudge_read_hours':48,'nudge_max_attempts':3},
 schedule={'enabled':True,'timezone':'Asia/Shanghai','weekdays':[0,1,2,3,4],'windows':[{'start':'09:00','end':'12:00'},{'start':'13:30','end':'18:00'}],'work_minutes':50,'rest_min_minutes':5,'rest_max_minutes':10},
 notifications={'enabled':False,'resume_chat_id':'','todo_user_id':''},greeting='您好，方便了解一下目前的机会吗？',recommend_filters=['本科'])
generic=copy.deepcopy(task);generic['id']='c'*32
generic['config']={'name':'图书采集 · 时间窗口验收','website':'https://books.toscrape.com/','scenario':'图书信息采集',
 'goal':'采集当前目录的图书名称与价格。','engine':'web','params':{},'output_schema':{'type':'object'},'max_steps':20,
 'plan':None,'schedule':copy.deepcopy(cfg['schedule'])}
state['definitions'].append(generic)
writes=[];errors=[]
class Quiet(SimpleHTTPRequestHandler):
 def log_message(self,*a):pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(Path(__file__).resolve().parents[1]/'cloakup/web')))
threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
try:
 with sync_playwright() as pw:
  browser=pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
  page=browser.new_page(viewport={'width':1440,'height':1050});page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   if r.request.url==base+'/api/state':r.fulfill(json=state)
   elif r.request.method=='POST':
    writes.append((r.request.url,r.request.post_data_json));data=r.request.post_data_json
    target=next((t for t in state['definitions'] if r.request.url==base+'/api/tasks/'+t['id']),None)
    if target:
     target['config']=data['config'];target['revision']+=1;r.fulfill(json=target)
    else:r.fulfill(json={'ok':True})
   elif r.request.url.startswith(base+'/'):r.continue_()
   else:r.abort()
  page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle')
  page.locator('[data-task-workspace="'+task['id']+'"]').first.click()
  expect(page.get_by_text('关联岗位：测试岗位甲（权重1）、测试岗位乙（权重1） · 按权重分配首聊',exact=True)).to_be_visible()
  expect(page.get_by_text('目标：持续寻访，尽可能多地取得合适候选人的简历（电话和微信非必需）',exact=True)).to_be_visible()
  page.locator('[data-task-edit="'+task['id']+'"]').first.click()
  expect(page.get_by_role('heading',name='任务共用策略',exact=True)).to_be_visible()
  expect(page.get_by_role('heading',name='执行时间与休息',exact=True)).to_be_visible()
  expect(page.get_by_role('heading',name='飞书交付与提醒',exact=True)).to_be_visible()
  page.locator('[name=shared_nudge_unread_hours]').fill('36')
  page.locator('[name=shared_nudge_read_hours]').fill('72')
  page.locator('[name=shared_request_contacts]').check()
  page.locator('[name=shared_filters][value="硕士"]').check()
  page.locator('[name=schedule_weekdays][value="5"]').check()
  page.locator('[name=schedule_work_minutes]').fill('40')
  page.locator('[name=schedule_rest_min_minutes]').fill('6')
  page.locator('[name=schedule_rest_max_minutes]').fill('12')
  page.locator('[name=schedule_windows]').fill('09:30-12:00\n14:00-17:30')
  page.locator('[name=notifications_enabled]').check()
  page.locator('[name=resume_chat_id]').fill('oc_test_resume_group')
  page.locator('[name=todo_user_id]').fill('ou_test_administrator')
  page.locator('[data-job-order]').filter(has_text='测试岗位乙').get_by_role('button',name='上移岗位',exact=True).click()
  assert page.locator('[data-job-order] input[name=case_ids]').evaluate_all('(els)=>els.map(e=>e.value)')==[second['id'],first['id']]
  # Changing mode re-renders the form, as does live state refresh. All edits
  # including checkbox-only fields and explicit ordering must survive.
  page.locator('#task-sourcing-mode').select_option('quota')
  page.locator('#task-sourcing-mode').select_option('continuous')
  expect(page.locator('[name=shared_nudge_unread_hours]')).to_have_value('36')
  expect(page.locator('[name=schedule_work_minutes]')).to_have_value('40')
  expect(page.locator('[name=schedule_rest_min_minutes]')).to_have_value('6')
  expect(page.locator('[name=schedule_weekdays][value="5"]')).to_be_checked()
  expect(page.locator('[name=shared_filters][value="硕士"]')).to_be_checked()
  expect(page.locator('[name=notifications_enabled]')).to_be_checked()
  assert page.locator('[data-job-order] input[name=case_ids]').evaluate_all('(els)=>els.map(e=>e.value)')==[second['id'],first['id']]
  page.get_by_role('button',name='保存任务',exact=True).first.click();page.wait_for_timeout(300)
  assert len(writes)==1,writes
  body=writes[0][1]['config'];assert body['case_ids']==[second['id'],first['id']]
  assert body['execution']['nudge_unread_hours']==36 and body['execution']['nudge_read_hours']==72
  assert body['execution']['request_contacts'] is True and body['execution']['nudge_max_attempts']==3
  assert body['schedule']['weekdays']==[0,1,2,3,4,5]
  assert body['schedule']['windows']==[{'start':'09:30','end':'12:00'},{'start':'14:00','end':'17:30'}]
  assert body['schedule']['work_minutes']==40 and body['schedule']['rest_min_minutes']==6 and body['schedule']['rest_max_minutes']==12
  assert body['recommend_filters']==['本科','硕士']
  assert body['notifications']=={'enabled':True,'resume_chat_id':'oc_test_resume_group','todo_user_id':'ou_test_administrator'}
  page.goto(base+'/?view=tasks',wait_until='networkidle')
  page.locator('[data-task-workspace="'+task['id']+'"]').first.click()
  expect(page.get_by_text('关联岗位：测试岗位乙（权重1）、测试岗位甲（权重1） · 按权重分配首聊',exact=True)).to_be_visible()
  expect(page.get_by_text('追聊：本任务启用 · 最多 3 次无回应停止 · 飞书通知：等待机器人凭据',exact=True)).to_be_visible()
  expect(page.get_by_text('执行窗口：周一、周二、周三、周四、周五、周六 09:30–12:00 / 14:00–17:30（上海）',exact=True)).to_be_visible()
  page.goto(base+'/?view=tasks',wait_until='networkidle')
  page.locator('[data-task-workspace="'+generic['id']+'"]').first.click()
  expect(page.get_by_text(generic['config']['goal'],exact=True)).to_be_visible()
  page.locator('[data-task-edit="'+generic['id']+'"]').first.click()
  expect(page.get_by_role('heading',name='执行时间',exact=True)).to_be_visible()
  expect(page.get_by_role('heading',name='任务共用策略',exact=True)).to_have_count(0)
  expect(page.locator('[name=schedule_work_minutes]')).not_to_be_visible()
  page.locator('[name=schedule_weekdays][value="0"]').uncheck()
  page.locator('[name=schedule_weekdays][value="6"]').check()
  page.locator('[name=schedule_windows]').fill('10:00-16:30')
  page.get_by_role('button',name='保存任务',exact=True).first.click();page.wait_for_timeout(300)
  assert len(writes)==2,writes
  generic_body=writes[1][1]['config']
  assert generic_body['engine']=='web'
  assert generic_body['schedule']['weekdays']==[1,2,3,4,6]
  assert generic_body['schedule']['windows']==[{'start':'10:00','end':'16:30'}]
  assert generic_body['schedule']['timezone']=='Asia/Shanghai' and generic_body['schedule']['enabled'] is True
  assert 'execution' not in generic_body and 'notifications' not in generic_body
  page.locator('.sidebar [data-nav=settings]').click()
  assert page.locator('#followup-policy-form').count()==0
  assert '15分钟升级待办' not in page.locator('body').inner_text()
  assert not errors,errors
  browser.close();print('task strategy, BOSS/web schedules, notification status, ordered jobs and re-render persistence: passed (synthetic state; writes intercepted)')
finally:server.shutdown();server.server_close()
