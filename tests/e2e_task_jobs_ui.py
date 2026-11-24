"""Task job area UI: per-job weights, quota and job changes; every API write is intercepted (no real business actions)."""
import copy,json,threading,urllib.request
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright,expect

state=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state'))
task=copy.deepcopy(next(t for t in state['definitions'] if t['config']['engine']=='boss_sourcing'))
base_case=copy.deepcopy(next(c for c in state['cases'] if c['id'] in task['config']['case_ids']))
tid='b'*32;A,B,C,D,E,F,G,H=('1'*32,'2'*32,'3'*32,'4'*32,'5'*32,'6'*32,'7'*31+'0','9'*31+'0')
labels={A:'测试岗位甲',B:'测试岗位乙',C:'测试岗位丙',D:'测试岗位丁',E:'测试岗位戊',F:'测试岗位己',G:'测试岗位庚',H:'测试岗位辛'}
cases=[]
for cid,label in labels.items():
 case=copy.deepcopy(base_case);case.update(id=cid,candidates={},audit=[]);case['job']['label']=label;cases.append(case)
members=[{'case_id':A,'weight':3,'outreach':'active'},{'case_id':B,'weight':1,'outreach':'withdrawn'},{'case_id':C,'weight':1,'outreach':'active'}]
task.update(id=tid,revision=4);task['config'].update(name='多岗位验收任务',case_ids=[A,B,C],members=members,notifications={'enabled':False,'resume_chat_id':'','todo_user_id':''})
task['config'].setdefault('execution',{})['daily_new_limit']=16
overview={'validation':'passed','shared':True,'quota':{'limit':16,'target':14,'used':5,'account_used':7,'account_cap':20},
 'jobs':[{'case_id':A,'label':labels[A],'weight':3,'outreach':'active','share':11,'used':4,'open_conversations':3,'validated':True},
         {'case_id':B,'label':labels[B],'weight':1,'outreach':'withdrawn','share':0,'used':1,'open_conversations':2,'validated':True},
         {'case_id':C,'label':labels[C],'weight':1,'outreach':'active','share':3,'used':0,'open_conversations':0,'validated':False}],
 'changes':[{'id':'7'*32,'action':'add','case_id':D,'label':labels[D],'status':'testing','error':None,'payload':{'weight':2},'at':1794877600},
            {'id':'8'*32,'action':'add','case_id':E,'label':labels[E],'status':'failed','error':'新岗位隔离验收未通过（7/9 项通过）','payload':{'weight':1},'at':1794877500},
            {'id':'9'*32,'action':'weights','case_id':None,'label':'','status':'applied','error':None,'payload':{'weights':{A:3}},'at':1794877400},
            # 庚：加入失败且管理员已放弃（无未处理待办）；辛：加入后又被移除。两者都应能再次选择加入
            {'id':'6'*31+'1','action':'add','case_id':G,'label':labels[G],'status':'failed','error':'验收未通过','payload':{'weight':1},'at':1794877300},
            {'id':'6'*31+'2','action':'remove','case_id':H,'label':labels[H],'status':'applied','error':None,'payload':{},'at':1794877250},
            {'id':'6'*31+'3','action':'add','case_id':H,'label':labels[H],'status':'applied','error':None,'payload':{'weight':1},'at':1794877200}]}
draft={'id':'c'*32,'task_id':tid,'revision':1,'status':'draft','signature':'s','bundle':{'config':task['config'],'sources':[]}}
todo=lambda ident,kind,title,**refs:{'id':ident,'kind':kind,'title':title,'detail':title+'（测试）','status':'pending','created_at':1794877600,'history':[],'job_label':labels.get(refs.get('case_id'),''),**refs}
attention=[todo('d'*32,'job_not_validated','岗位验收已失效',task_id=tid,case_id=C),todo('e'*32,'job_outreach_blocked','岗位首聊入口反复失败',case_id=A),
           todo('f'*32,'job_change','岗位变更未生效',task_id=tid,case_id=E,job_change_id='8'*32),todo('a'*32,'job_removable','岗位可以移除',task_id=tid,case_id=B)]
state.update(definitions=[task],cases=cases,runs=[],attention=attention,task_plans=[],planning_dialogues=[],planning_requests=[],busy=None,job_overview={tid:overview})
state['agent_workspace']={'agents':{},'drafts':[],'releases':[{'id':tid,'version_id':'f'*32,'revision':3,'validation':'passed'}],'sessions':[],'tests':[]}
state['notifications']={'configured':False,'records':[]}
state['account_execution'].update(owner_run_id=None,members=[{'id':tid,'status':'running','case_ids':[A,B,C]}])
state['runtime_status'].update(task_state='任务运行中',browser_alive=False,needs_attention=False)
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
    url=r.request.url;body=r.request.post_data_json;writes.append((url[len(base):],body))
    if url==base+'/api/tasks/'+tid+'/jobs':r.fulfill(json={'id':'0'*32,'task_id':tid,'status':'pending',**body})
    elif url.endswith('/agent-workspace/draft'):r.fulfill(json=draft)
    elif url.endswith('/agent-workspace/test'):r.fulfill(json={'id':'0'*32,'status':'queued'})
    else:raise AssertionError('Unexpected write '+url)
   elif r.request.url.startswith(base+'/'):r.continue_()
   else:r.abort()
  def last(n=1):page.wait_for_function('!ui.pending');return writes[-n:]
  page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle')
  page.locator('[data-task-workspace="'+tid+'"]').first.click()
  jobs=page.locator('#task-jobs');expect(jobs.get_by_role('heading',name='岗位与首聊额度',exact=True)).to_be_visible()
  # 额度：总额、今日目标、任务与账号已用
  expect(jobs.get_by_text('14',exact=True)).to_be_visible();expect(jobs.get_by_text('7 / 20',exact=True)).to_be_visible();expect(page.locator('#job-limit-'+tid)).to_have_value('16')
  row=lambda cid:page.locator('[data-job-row="'+cid+'"]')
  expect(row(A)).to_contain_text('参与首聊');expect(row(A)).to_contain_text('4 / 11')
  expect(row(B)).to_contain_text('已撤下（仍跟进 2 个会话）');expect(row(C)).to_contain_text('验收失效')
  expect(row(D)).to_contain_text('验收中');expect(row(E)).to_contain_text('加入失败');expect(row(E)).to_contain_text('7/9 项通过')
  # 移除：有进行中会话不可用；无会话可用
  expect(row(B).get_by_role('button',name='移除')).to_be_disabled();expect(row(A).get_by_role('button',name='移除')).to_be_disabled();expect(row(C).get_by_role('button',name='移除')).to_be_enabled()
  expect(row(A).get_by_role('button',name='撤下')).to_be_visible();expect(row(B).get_by_role('button',name='恢复首聊')).to_be_visible()
  # 权重 → weights 动作
  row(A).locator('input[data-job-weight]').fill('5');row(A).locator('input[data-job-weight]').dispatch_event('change')
  assert last()==[('/api/tasks/'+tid+'/jobs',{'action':'weights','weights':{A:5}})],writes
  expect(row(A).locator('input[data-job-weight]')).to_have_value('3')  # 排队期间显示生效值，不把未生效的输入当作现状
  row(A).locator('input[data-job-weight]').fill('11');row(A).locator('input[data-job-weight]').dispatch_event('change');page.wait_for_function('!ui.pending')
  assert len(writes)==1 and '权重' in page.locator('#toast').inner_text()
  # 撤下 / 恢复 / 移除
  row(A).get_by_role('button',name='撤下').click();assert last()==[('/api/tasks/'+tid+'/jobs',{'action':'withdraw','case_id':A})]
  row(B).get_by_role('button',name='恢复首聊').click();assert last()==[('/api/tasks/'+tid+'/jobs',{'action':'resume','case_id':B})]
  page.once('dialog',lambda d:d.accept());row(C).get_by_role('button',name='移除').click();assert last()==[('/api/tasks/'+tid+'/jobs',{'action':'remove','case_id':C})]
  # 加入失败可以重新加入（沿用原权重）
  row(E).get_by_role('button',name='重新加入').click();assert last()==[('/api/tasks/'+tid+'/jobs',{'action':'add','case_id':E,'weight':1})]
  # 总额
  page.locator('#job-limit-'+tid).fill('12');jobs.get_by_role('button',name='保存总额').click()
  assert last()==[('/api/tasks/'+tid+'/jobs',{'action':'limit','limit':12})]
  # 粘贴链接导入后的岗位：选择并加入（只列出不在任务、也没有进行中加入的岗位）
  options=page.locator('#job-add-'+tid+' select[name=case_id] option').evaluate_all('els=>els.map(e=>e.value).filter(Boolean)')
  assert options==[F,G,H],options
  expect(row(G)).to_have_count(0);expect(row(H)).to_have_count(0)
  page.locator('#job-add-'+tid+' select[name=case_id]').select_option(F);page.locator('#job-add-'+tid+' input[name=weight]').fill('2')
  page.locator('#job-add-'+tid).get_by_role('button',name='验收并加入').click()
  assert last()==[('/api/tasks/'+tid+'/jobs',{'action':'add','case_id':F,'weight':2})]
  expect(page.locator('#job-add-'+tid).get_by_role('button',name='粘贴链接导入新岗位')).to_be_visible()
  # 重新验收：用当前生效版本建草案并跑完整隔离验收，不发布
  row(C).get_by_role('button',name='重新验收').click()
  assert last(2)==[('/api/tasks/'+tid+'/agent-workspace/draft',{}),('/api/tasks/'+tid+'/agent-workspace/test',{'draft_id':draft['id'],'mode':'validate'})],writes[-2:]
  # 岗位级待办：不出现候选人沟通按钮，给出岗位区与重新验收
  page.evaluate("ui.view='attention';render(true)")
  for ident,kind in (('d'*32,'job_not_validated'),('e'*32,'job_outreach_blocked'),('f'*32,'job_change'),('a'*32,'job_removable')):
   page.locator('[data-select-todo="'+ident+'"]').click();detail=page.locator('.attention-detail')
   expect(detail.get_by_role('button',name='打开任务岗位区')).to_be_visible()
   assert detail.get_by_role('button',name='查看候选人沟通').count()==0 and detail.get_by_role('button',name='更新岗位事实').count()==0,kind
   assert (detail.get_by_role('button',name='重新验收').count()==1)==(kind=='job_not_validated'),kind
   assert (detail.get_by_role('button',name='记录处理完成').count()==1)==(kind in ('job_change','job_removable')),kind
  page.locator('[data-select-todo="'+'d'*32+'"]').click();n=len(writes)
  page.locator('.attention-detail').get_by_role('button',name='重新验收').click();page.wait_for_function('!ui.pending')
  assert [w[0] for w in writes[n:]]==['/api/tasks/'+tid+'/agent-workspace/draft','/api/tasks/'+tid+'/agent-workspace/test']
  page.locator('[data-select-todo="'+'e'*32+'"]').click();page.locator('.attention-detail').get_by_role('button',name='打开任务岗位区').click()
  expect(page.locator('#task-jobs')).to_be_visible();expect(row(A)).to_have_attribute('aria-current','true')
  # 视图带不出来时只提示原因，不报错
  state['job_overview']={tid:{'jobs':[],'changes':[],'error':'岗位视图暂时无法读取：boom'}};page.evaluate('refresh(true)')
  expect(page.locator('#task-jobs')).to_contain_text('岗位视图暂时无法读取：boom')
  # 权重/总额变更排队：受影响的岗位行与总额显示待生效值，输入暂不可改
  pending=copy.deepcopy(overview);pending['changes'][:0]=[{'id':'5'*31+'1','action':'weights','case_id':None,'label':'','status':'pending','error':None,'payload':{'weights':{A:5}},'at':1794877700},
   {'id':'5'*31+'2','action':'limit','case_id':None,'label':'','status':'pending','error':None,'payload':{'limit':10},'at':1794877700}]
  state['job_overview']={tid:pending};page.evaluate("ui.view='tasks';ui.workspaceTask='"+tid+"';ui.workspaceTab='overview';render(true);refresh(true)")
  expect(row(A)).to_contain_text('权重调整等待生效');expect(row(A)).to_contain_text('3 → 5');expect(row(A).locator('input[data-job-weight]')).to_be_disabled()
  expect(row(C).locator('input[data-job-weight]')).to_be_enabled();expect(page.locator('#task-jobs')).to_contain_text('总额调整为 10 等待生效');expect(jobs.get_by_role('button',name='保存总额')).to_be_disabled()
  # 有隔离验收在跑：验收有效的岗位照常显示参与首聊；重新验收与加入暂不可用并说明原因，不发出任何请求
  state['job_overview']={tid:overview};state['agent_workspace']['tests']=[{'id':'4'*31+'1','task_id':tid,'status':'running','mode':'validate','case_ids':[A,B,C],'summary':{'passed':0,'total':0}}];n=len(writes);page.evaluate('refresh(true)')
  expect(row(A)).to_contain_text('参与首聊');expect(row(C)).to_contain_text('验收中');expect(row(C).get_by_role('button',name='重新验收')).to_be_disabled()
  expect(page.locator('#job-add-'+tid).get_by_role('button',name='验收并加入')).to_be_disabled();expect(row(E).get_by_role('button',name='重新加入')).to_be_disabled()
  expect(page.locator('#task-jobs')).to_contain_text('加入岗位需等这次验收结束')
  page.evaluate("ui.view='attention';ui.todoSelection='"+'d'*32+"';render(true)");expect(page.locator('.attention-detail').get_by_role('button',name='重新验收')).to_be_disabled()
  assert len(writes)==n;state['agent_workspace']['tests']=[]
  page.evaluate("ui.view='tasks';render(true)")
  # 重启后验收已中断但尚未结算的加入：如实显示
  stale=copy.deepcopy(overview);stale['changes'][0]['status']='interrupted';state['job_overview']={tid:stale};page.evaluate('refresh(true)')
  expect(row(D)).to_contain_text('验收已中断')
  # 旧任务没有任务级执行设置：不显示任务总额与份额，说明按岗位上限
  legacy=copy.deepcopy(overview);legacy['quota']={'per_job':True,'account_used':7,'account_cap':20}
  for j in legacy['jobs']:j['share']=None
  state['job_overview']={tid:legacy};page.evaluate('refresh(true)')
  expect(page.locator('#task-jobs')).to_contain_text('按各岗位自己的每日上限');expect(page.locator('#job-limit-'+tid)).to_have_count(0);expect(jobs.get_by_text('7 / 20',exact=True)).to_be_visible()
  out=Path('runs/verification');out.mkdir(parents=True,exist_ok=True)
  state['job_overview']={tid:overview};page.evaluate('refresh(true)');expect(row(A)).to_be_visible();page.locator('#task-jobs').screenshot(path=str(out/'task-jobs-ui.png'))
  page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(200)
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
  assert not errors,errors
  print(json.dumps({'passed':True,'checks':['job rows and states','quota','weights action','withdraw/resume/remove','remove disabled with open conversations','re-add failed','limit','add from imported jobs','revalidate','job todos without candidate-chat buttons','overview error','abandoned or removed jobs can be added again','pending weights and limit','validation running blocks revalidate and add','interrupted add','legacy per-job limits'],'intercepted_writes':len(writes),'real_writes':0}))
  browser.close()
finally:server.shutdown()
