from types import SimpleNamespace as NS
from cloakup.workbench import Workbench


def test_browser_window_and_business_task_are_independent(tmp_path):
 m=Workbench(tmp_path)
 try:
  m.jobs['a'*32]={'id':'a'*32,'mode':'boss_autopilot','created':1,'status':'failed','error':'会话未核验'}
  m.jobs['b'*32]={'id':'b'*32,'mode':'browser_open','created':2,'status':'succeeded'}
  m.boss_session=NS(context=NS(pages=[]),page=NS(is_closed=lambda:False))
  r=m.runtime_status()
  assert r['application']=='online' and r['browser_alive']
  assert r['task_state']=='任务异常停止' and r['task_id']=='a'*32
  assert r['task_error']=='会话未核验' and r['needs_attention']
 finally:m.boss_session=None;m.close()


def test_browser_open_does_not_need_model_or_send_messages(tmp_path,monkeypatch):
 monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False);monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN',raising=False)
 m=Workbench(tmp_path)
 class Session:
  def __init__(self,*a):self.context=None;self.page=NS(url='https://www.zhipin.com/web/chat/index',is_closed=lambda:False)
  async def start(self):self.context=NS(pages=[self.page])
  async def close(self):self.context=None
 monkeypatch.setattr('cloakup.workbench.WebSession',Session)
 async def scenario():
  r=await m.create({'mode':'browser_open','params':{}})
  await m.tasks[r['id']]
  assert m.jobs[r['id']]['status']=='succeeded'
  assert m.jobs[r['id']]['browser_kept_open']
  assert m.client is None
  assert m.runtime_status()['task_state']=='暂无运行任务'
 try:m.call(scenario())
 finally:m.close()


def test_release_change_is_reported_instead_of_looking_busy(tmp_path):
 # 心跳正常、状态“运行”，但版本不匹配导致发送被关：必须显式提示，不能看起来还在工作。
 job={'id':'c'*32,'mode':'boss_autopilot','created':1,'status':'running','account_runner':True,'autopilot_stage':'release_changed','events':[]}
 manager=NS(busy=lambda:job,jobs={job['id']:job},active_session=None,boss_session=None,browser_alive=lambda s:False,
            service_instance_id='service',monitor_heartbeat=1,account_execution=NS(members=lambda:[{'status':'running'}]))
 status=Workbench.runtime_status(manager)
 assert status['task_state']=='发布版本已变化 · 需暂停并恢复任务' and status['needs_attention']
