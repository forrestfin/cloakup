"""Local source UI acceptance. Synthetic state and every write intercepted."""
import json,threading,urllib.request
from pathlib import Path
from functools import partial
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from playwright.sync_api import sync_playwright,expect
s=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state'));t=next(t for t in s['definitions'] if t['config']['engine']=='boss_sourcing');c=next(c for c in s['cases'] if c['id']==t['config']['case_ids'][0]);pid=next(iter(c['candidates']));p=c['candidates'][pid];p['profile']['platform_label']='测试候选人'
c['editable_config']['execution']['decision_max_turns']=3
c['audit']=[{'id':'d'*32,'at':1794800000,'kind':'harness_decision','candidate_id':pid,'hash':'1'*64,'details':{'decision_id':'test-decision','cycle_id':'test-cycle','turn':1,'plan_id':'p'*32,'proposal':{'action':'answer','reason':'当前问题已有完整的确认事实，先回答问题。','fact_index':-1},'model_input':{'observation':{'has_unhandled_reply':True},'available_actions':{'answer':'确认答案','wait':'等待'},'previous_results':[]},'harness_trace':{'version':'0.2.2rc2','session_id':'test-native'}}}, {'id':'e'*32,'at':1794800001,'kind':'harness_tool_result','candidate_id':pid,'hash':'2'*64,'details':{'decision_id':'test-decision','action':'answer','result':'verified'}}]
s['attention']=[{'id':'a'*32,'kind':'decision','status':'pending','title':'Harness 决策待恢复','detail':'测试网关异常','created_at':1794800000,'history':[],'case_id':c['id'],'candidate_id':pid,'job_label':'测试岗位'}]
s['planning_runtime']['scope']='任务规划与寻访持续决策';writes=[]
class Quiet(SimpleHTTPRequestHandler):
 def log_message(self,*a):pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(Path(__file__).resolve().parents[1]/'cloakup/web')));threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
try:
 with sync_playwright() as pw:
  b=pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page(viewport={'width':1440,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   if '/api/state' in r.request.url:r.fulfill(json=s)
   elif r.request.method=='POST':writes.append((r.request.url,r.request.post_data_json));r.fulfill(json={'ok':True,'revision':2})
   else:r.continue_()
  page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle');page.locator('[data-task-workspace="'+t['id']+'"]').first.click()
  expect(page.get_by_role('heading',name='持续决策',exact=True)).to_be_visible()
  page.locator('.audit-item summary').filter(has_text='按确认事实回答').first.click();expect(page.get_by_text('当前问题已有完整的确认事实，先回答问题。',exact=True)).to_be_visible();expect(page.get_by_text('测试候选人 · 已有回执核实',exact=True)).to_be_visible()
  page.screenshot(path='/tmp/cloakup-decision-runtime-ui.png',full_page=True)
  page.locator('[data-workspace-tab=strategy]').click();page.locator('[data-job-tab=execution]').click();expect(page.locator('[name=decision_max_turns]')).to_have_value('3')
  page.locator('[name=decision_max_turns]').fill('2');page.get_by_role('button',name='保存岗位策略',exact=True).click();page.wait_for_timeout(300);assert writes[-1][1]['config']['execution']['decision_max_turns']==2
  page.locator('.sidebar [data-nav=attention]').click();page.get_by_role('button',name='重新观察并决策',exact=True).click();page.wait_for_timeout(300);assert writes[-1][0].endswith('/decision-retry') and writes[-1][1]['candidate_id']==pid
  page.locator('.sidebar [data-nav=settings]').click();expect(page.get_by_text('通用网站执行器已通过 Harness 持续选择动作并核验工具结果。',exact=False)).to_be_visible()
  assert not errors,errors;b.close();print('decision trace, configurable turn budget, recovery UI: passed (synthetic state, writes intercepted)')
finally:server.shutdown();server.server_close()
