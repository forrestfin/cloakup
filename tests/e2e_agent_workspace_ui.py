"""Synthetic UI contract: all API writes intercepted; no real business actions."""
import copy,json,threading,urllib.request
from pathlib import Path
from functools import partial
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from playwright.sync_api import sync_playwright,expect
from cloakup.conversation_semantics import SYSTEM

state=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state'))
task=copy.deepcopy(next(t for t in state['definitions'] if t['config']['engine']=='boss_sourcing'))
tid='b'*32;cid='a'*32;did='c'*32
case={'id':cid,'job':{'label':'验收岗位','revision':1,'snapshot':{'requirements':'负责人才寻访与交付。'}},'config_revision':1,'anonymous_job':{'role':'招聘顾问','requirements':['负责人才寻访与交付。']},'confirmed_facts':[],'faq':[],'candidates':{},'audit':[],'autonomy':{'enabled':True}}
task.update(id=tid,revision=2);task['config'].update(name='寻访验收任务',case_ids=[cid],notifications={'enabled':False,'resume_chat_id':'','todo_user_id':''})
source={'case_id':cid,'config_revision':1,'job_revision':1,'job':case['job'],'anonymous_job':case['anonymous_job'],'facts':[],'faq':[]}
bundle={'config':task['config'],'sources':[source],'plan':{'steps':['按事实推进'],'success_criteria':['取得简历'],'risks':['不编造事实']},'conversation_prompt':SYSTEM}
draft={'id':did,'task_id':tid,'revision':1,'status':'draft','base_task_revision':2,'base_release_revision':1,'signature':'tested-signature','bundle':bundle,'changes':[]}
planner={'id':'d'*32,'task_id':tid,'agent':'planner','draft_id':did,'revision':1,'messages':[{'role':'assistant','content':'规划端独立上下文'}]}
executor={'id':'e'*32,'task_id':tid,'agent':'executor','draft_id':did,'revision':1,'messages':[{'role':'assistant','content':'执行端独立上下文'}]}
release={'id':tid,'version_id':'f'*32,'revision':1,'validation':'legacy_unverified'}
state.update(definitions=[task],cases=[case],runs=[],attention=[],task_plans=[],planning_dialogues=[],planning_requests=[],busy=None,
 agent_workspace={'agents':{},'drafts':[draft],'releases':[release],'sessions':[planner,executor],'tests':[]})
state['runtime_status'].update(task_state='任务已暂停',browser_alive=False,needs_attention=False)
state['account_execution']['members']=[{'id':tid,'status':'paused','case_ids':[cid]}]
from cloakup.scenario_workspace import DEFAULT_POLICY,DEFAULT_TEST_FACTS
sid='7'*32
scenario={'id':sid,'name':'BOSS · 候选人寻访','website':'https://www.zhipin.com','scenario':task['config']['scenario'],'engine':'boss_sourcing','revision':1,'policy':copy.deepcopy(DEFAULT_POLICY),'updated_at':1}
ps={'id':'8'*32,'template_id':sid,'agent':'planner','mode':'discuss','revision':1,'title':'通用策略讨论','messages':[{'role':'assistant','content':'规划端独立上下文'}],'updated_at':1}
xs={'id':'6'*32,'template_id':sid,'agent':'executor','mode':'simulate','revision':1,'title':'候选人问答测试','messages':[{'role':'assistant','content':'执行端独立上下文'}],'facts':copy.deepcopy(DEFAULT_TEST_FACTS),'facts_revision':1,'updated_at':1}
state['scenario_workspace']={'templates':[scenario],'sessions':[ps,xs],'test_contexts':[{'id':sid,'revision':1,'facts':copy.deepcopy(DEFAULT_TEST_FACTS)}],'applications':[],'bindings':[],'versions':[]}
writes=[];errors=[]
class Quiet(SimpleHTTPRequestHandler):
 def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(Path(__file__).resolve().parents[1]/'cloakup/web')))
threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
try:
 with sync_playwright() as pw:
  browser=pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
  page=browser.new_page(viewport={'width':1600,'height':1050});page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   if r.request.url==base+'/api/state':r.fulfill(json=state)
   elif r.request.method=='POST':
    body=r.request.post_data_json;writes.append((r.request.url,body))
    if '/api/scenarios/' in r.request.url:
     action=r.request.url.rsplit('/',1)[-1];data=state['scenario_workspace']
     if action=='chat':
      session=next(s for s in data['sessions'] if s['id']==body['session_id']);assert session['revision']==body['expected_revision']
      session['messages'] += [{'role':'user','content':body['message']},{'role':'assistant','content':'您好，支持远程协作，方便发份简历吗？' if session['mode']=='simulate' else '通用策略已保存，尚未应用到任务。'}];session['revision']+=1;r.fulfill(json={'session':session,'template':scenario})
     elif action=='save':
      assert body['expected_revision']==scenario['revision'];scenario.update(policy=body['policy'],revision=scenario['revision']+1);r.fulfill(json=scenario)
     elif action=='context':
      ctx=data['test_contexts'][0];ctx.update(facts=body['facts'],revision=ctx['revision']+1);r.fulfill(json=ctx)
     elif action=='session':
      import uuid
      session={'id':uuid.uuid4().hex,'template_id':sid,'agent':body['agent'],'mode':body['mode'],'revision':1,'title':'新对话','messages':[],'updated_at':2}
      if body['mode']=='simulate':session.update(facts=data['test_contexts'][0]['facts'],facts_revision=data['test_contexts'][0]['revision'])
      data['sessions'].insert(0,session);r.fulfill(json=session)
     elif action=='apply':
      assert body['task_ids']==[tid];data['applications']=[{'id':'5'*32,'template_id':sid,'created_at':2,'status':'validating','results':[]}];r.fulfill(json=data['applications'][0])
     else:raise AssertionError(action)
    elif r.request.url.endswith('/draft'):
     if not body:r.fulfill(json=draft)
     else:draft.update(bundle=body['bundle'],revision=draft['revision']+1);r.fulfill(json=draft)
    elif r.request.url.endswith('/browser-diagnostics'):
     report={'task_id':tid,'mode':'existing_chat_selection_only','verified':False,'reason':'header_name_unverified','checked_at':1}
     state['executor_checks']=[report];r.fulfill(json=report)
    elif r.request.url.endswith('/test'):r.fulfill(json={'id':'1'*32,'status':'queued'})
    else:raise AssertionError('Unexpected write '+r.request.url)
   elif r.request.url.startswith(base+'/'):r.continue_()
   else:r.abort()
  page.route('**/*',route);  page.goto(base+'/?view=planner',wait_until='networkidle')
  expect(page.get_by_role('heading',name='规划端',exact=True)).to_be_visible()
  expect(page.get_by_role('heading',name='目标',exact=True)).to_be_visible()
  expect(page.locator('#scenario-message')).to_have_count(0)
  assert page.locator('#content').get_by_text('验收岗位',exact=False).count()==0
  assert page.locator('#agent-task,#agent-draft,#agent-chat-replay,#agent-replay-person').count()==0
  page.locator('#sc-instructions').fill('依据上下文自然回答，尊重候选人意愿。')
  page.wait_for_timeout(1100);assert scenario['policy']['instructions']=='依据上下文自然回答，尊重候选人意愿。'
  page.locator('[data-nav="executor"]').click()
  expect(page.get_by_role('heading',name='执行端',exact=True)).to_be_visible()
  expect(page.get_by_text('按已确认事实回复',exact=True)).to_be_visible()
  expect(page.locator('#scenario-message')).to_have_count(0)
  page.locator('#sc-request_contacts').check()
  page.wait_for_timeout(1100);assert scenario['policy']['execution']['request_contacts'] is True
  page.locator('[data-sc-panel="apply"]').click();page.locator('input[name=task_ids]').check();page.get_by_role('button',name='检查并应用',exact=True).click()
  expect(page.get_by_text('正在验证',exact=True)).to_be_visible()
  assert task['revision']==2
  page.wait_for_function('!ui.pending');page.locator('[data-sc-close]').click()
  page.set_viewport_size({'width':390,'height':844});page.locator('[data-nav="planner"]').click();expect(page.locator('#sc-goal')).to_be_visible()
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
  page.set_viewport_size({'width':1600,'height':1050});page.wait_for_function('!ui.pending')
  out=Path('runs/verification');out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(out/'scenario-planner-ui.png'))
  page.locator('[data-nav="executor"]').click();page.screenshot(path=str(out/'scenario-executor-ui.png'))
  page.evaluate("ui.workspaceTask='"+tid+"';ui.workspaceTab='settings';ui.view='tasks';render(true)")
  expect(page.locator('#task-parameters-form')).to_be_visible();expect(page.locator('#schedule-windows')).to_be_visible()
  page.locator('#schedule-windows').fill('10:00-12:00\n14:00-17:00');page.get_by_role('button',name='保存并验证',exact=True).click();page.wait_for_timeout(300)
  assert writes[-1][1]['mode']=='validate'
  assert draft['bundle']['config']['schedule']['windows'][0]['start']=='10:00'
  assert page.locator('[name=greeting],[name=outreach_terms],[name=recommend_filters]').count()==0
  page.wait_for_function('!ui.pending')
  page.evaluate("ui.taskFactsEditing='"+cid+"';ui.workspaceTab='strategy';render(true)")
  expect(page.locator('#tf-greeting')).to_be_visible()
  page.locator('#tf-greeting').fill('您好，欢迎了解人才寻访岗位。')
  page.locator('#tf-terms').fill('人才寻访\n招聘交付')
  page.locator('#tf-filter-0').check()
  page.locator('#tf-confirmed').check()
  page.locator('#task-facts-form button[type=submit]').click();page.wait_for_function('!ui.pending')
  assert 'sourcing' in draft['bundle']['sources'][0], {'writes':[(u,b.get('mode')) for u,b in writes], 'errors':errors, 'invalid':page.locator(':invalid').evaluate_all('(els)=>els.map(e=>({id:e.id,message:e.validationMessage}))'), 'toast':page.locator('#toast').inner_text()}
  profile=draft['bundle']['sources'][0]['sourcing']
  assert profile=={'greeting':'您好，欢迎了解人才寻访岗位。','outreach_terms':['人才寻访','招聘交付'],'recommend_filters':['本科']}
  page.evaluate("ui.view='readiness';render(true)")
  page.get_by_role('button',name='核验原目标会话（不发送）').click()
  expect(page.get_by_role('status').filter(has_text='核验未通过')).to_be_visible()
  assert [body for url,body in writes if url.endswith('/browser-diagnostics')]==[{'action':'verify_existing_target'}]
  assert not errors,errors
  print(json.dumps({'passed':True,'checks':['role isolation','no local sessions','multiturn reply simulation','test facts snapshots','autosave','explicit task apply','mobile layout','task-owned settings','job-owned sourcing configuration'],'intercepted_writes':len(writes),'real_writes':0}))
  browser.close()
finally:server.shutdown()
