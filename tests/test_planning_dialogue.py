import asyncio,copy,json
from types import SimpleNamespace as NS
import pytest
from cloakup.storage import Database
from cloakup.sourcing import CaseStore
from cloakup.platform import TaskStore
from cloakup.account_execution import AccountExecution
from cloakup.attention import AttentionStore
from cloakup.planning_dialogue import PlanningDialogue
from cloakup.task_planning import generate_boss_plan,planning_signature

@pytest.fixture
def env(tmp_path,monkeypatch):
 from datetime import datetime
 from zoneinfo import ZoneInfo
 # Planning/send tests run at a known business-hour instant. Separate policy
 # tests exercise lunch, weekends, off-hours and break boundaries.
 fixed_now=datetime(2026,11,16,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
 availability=AccountExecution.availability
 monkeypatch.setattr(AccountExecution,'availability',lambda self,cid,now=None:availability(self,cid,fixed_now if now is None else now))
 db=Database(tmp_path);cases=CaseStore(tmp_path,db);defs=TaskStore(db)
 c=cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=job')
 cases.bind_job(c,{'title':'招聘顾问','requirements':'实际招聘经历，远程协作'})
 c['anonymous_job']={'role':'招聘顾问','requirements':['远程协作。']};c['desired_greeting']='您好，欢迎沟通';cases.set_autonomy(c,True,source='test')
 p=cases.add_candidate(c,'p','https://www.zhipin.com/web/chat/index',{'summary':'职业资料'})
 p['conversation']={'chat_id':'12-0','receiver':'甲','job_title':'招聘顾问'}
 cases.receive(c,p['id'],'q','结算周期是多久？');p['pending_question_id']='q';p['handled_reply_id']='q';cases.save(c)
 t=defs.save({'config':{'name':'寻访','website':'https://www.zhipin.com/','scenario':'寻访','goal':'取得资料','engine':'boss_sourcing','case_ids':[c['id']],'schedule':{'enabled':True,'timezone':'Asia/Shanghai','weekdays':[0,1,2,3,4],'windows':[{'start':'09:00','end':'12:00'},{'start':'13:30','end':'18:00'}]}}})
 m=NS(db=db,cases=cases,definitions=defs,account_execution=AccountExecution(db));service=PlanningDialogue(m);m.planning=service
 return m,service,c,p,t

async def finish(service):
 await asyncio.gather(*list(service.workers.values()))

async def test_answer_replans_before_release_and_preserves_pause(env):
 m,s,c,p,t=env;m.account_execution.set_task(t['id'],[c['id']],'paused');seen=[]
 todo=next(x for x in AttentionStore(m.db).reconcile([],[c]) if x['kind']=='question')
 async def generate(key,conversation=False):
  assert conversation and not m.account_execution.can_send(c['id'])
  d=s.definition(key);seen.append(d['planning_context']);return {'id':'newplan','reply':'已纳入确认的结算周期，并保留资料获取目标。'}
 m.generate_definition_plan=generate
 req=await s.submit(t['id'],{'reviewed':True,'message':'候选人询问结算周期','answer':'按月结算。'},todo)
 await finish(s)
 assert m.db.get('planning_request',req['id'])['status']=='succeeded'
 saved=m.cases.load(c['id']);assert saved['faq'][0]['answer']=='按月结算。'
 assert saved['candidates'][p['id']]['planning_answer_ready']['plan_id']=='newplan'
 assert seen[0]['messages'][0]['content'].endswith('答案：按月结算。')
 assert '甲' not in json.dumps(seen,ensure_ascii=False)
 assert m.account_execution.members()[0]['status']=='paused'
 assert len(s.definition(t['id'])['planning_context']['messages'])==2
 assert next(x for x in AttentionStore(m.db).reconcile([],[saved]) if x['id']==todo['id'])['status']!='resolved'

async def test_failure_is_durable_retry_does_not_duplicate_faq_or_turn(env):
 m,s,c,p,t=env;m.account_execution.set_task(t['id'],[c['id']],'running')
 todo=next(x for x in AttentionStore(m.db).reconcile([],[c]) if x['kind']=='question')
 calls=[]
 async def generate(key,conversation=False):
  calls.append(key)
  if len(calls)==1:raise ValueError('网关不可用')
  return {'id':'plan','reply':'已更新'}
 m.generate_definition_plan=generate
 req=await s.submit(t['id'],{'reviewed':True,'message':'结算周期','answer':'按月结算。'},todo);await finish(s)
 assert m.db.get('planning_request',req['id'])['status']=='failed'
 assert not m.account_execution.can_send(c['id'])
 await s.retry(req['id']);await finish(s)
 assert m.account_execution.can_send(c['id'])
 assert len(m.cases.load(c['id'])['faq'])==1
 assert len(s.definition(t['id'])['planning_context']['messages'])==2

async def test_chat_history_passes_to_harness_without_raw_candidate(env):
 m,s,c,p,t=env;wire=[]
 class Model:
  client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))
  async def json(self,**kw):
   wire.append(json.loads(kw['content']))
   return {'reply':'先处理待答问题，再获取缺失资料。','plan':{'steps':['先回复已有事实支持的问题'],'success_criteria':['取得资料'],'risks':['事实不足转待办']}}
 d=copy.deepcopy(t);d['planning_context']={'revision':2,'messages':[{'role':'user','content':'先回复问题'},{'role':'assistant','content':'已调整顺序'},{'role':'user','content':'不要重复索取简历'}]}
 result=await generate_boss_plan(Model(),d,[c],conversation=True)
 assert len(wire[0]['planning_conversation'])==3
 assert '结算周期是多久' not in json.dumps(wire,ensure_ascii=False)
 assert result['reply'] and result['planning_context_revision']==2
 d['planning_context']['revision']=3
 assert result['signature']!=planning_signature(d,[c])

async def test_review_and_concurrency_are_required(env):
 m,s,c,p,t=env
 with pytest.raises(ValueError,match='复核'):await s.submit(t['id'],{'message':'先回复问题'})
 gate=asyncio.Event()
 async def generate(*a,**kw):await gate.wait();return {'id':'plan','reply':'已更新'}
 m.generate_definition_plan=generate
 await s.submit(t['id'],{'message':'先回复问题','reviewed':True})
 with pytest.raises(ValueError,match='已有'):await s.submit(t['id'],{'message':'下一轮','reviewed':True})
 gate.set();await finish(s)
 assert len(s.definition(t['id'])['planning_context']['messages'])==2

async def test_unknown_answer_waits_for_admin_without_sending(env):
 from cloakup.autopilot import dispatch_reply
 from cloakup.autonomy import next_step
 m,s,c,p,t=env
 m.cases=cstore=m.cases
 p.pop('handled_reply_id',None)
 await dispatch_reply(m,{},object(),c,p,{},next_step(p))
 saved=cstore.load(c['id'])['candidates'][p['id']]
 assert saved['pending_question_id']=='q' and not saved['outbox']

async def test_confirmed_answer_reopens_after_plan_and_resolves_only_on_receipt(env,monkeypatch):
 from cloakup import autopilot
 from cloakup.autonomy import next_step
 m,s,c,p,t=env;m.account_execution.set_task(t['id'],[c['id']],'running')
 todo=next(x for x in AttentionStore(m.db).reconcile([],[c]) if x['kind']=='question')
 async def generate(*a,**kw):return {'id':'plan','reply':'已纳入确认答案'}
 m.generate_definition_plan=generate
 await s.submit(t['id'],{'reviewed':True,'message':'结算周期','answer':'按月结算。'},todo);await finish(s)
 c=m.cases.load(c['id']);p=c['candidates'][p['id']]
 snap={'messages':[{'external_id':'q','direction':'in','text':'结算周期是多久？'}],'fingerprint':'same'}
 autopilot.revisit_question(m.cases,c,p,snap)
 assert 'handled_reply_id' not in p
 async def read(*a,**kw):return snap
 async def send(*a,**kw):return {'evidence':'synthetic verified delivery','message_id':'out','delivery_status':'delivered'}
 async def checkpoint(*a):pass
 async def sleep(*a):pass
 m.checkpoint=checkpoint
 monkeypatch.setattr(autopilot.boss_chat,'read_conversation',read);monkeypatch.setattr(autopilot.boss_chat,'send_once',send);monkeypatch.setattr(autopilot.asyncio,'sleep',sleep)
 await autopilot.dispatch_reply(m,{'account_runner':True},object(),c,p,snap,next_step(p))
 c=m.cases.load(c['id']);p=c['candidates'][p['id']]
 assert p['outbox'][-1]['text']=='按月结算。' and p['outbox'][-1]['status']=='sent'
 assert 'pending_question_id' not in p
 assert next(x for x in AttentionStore(m.db).reconcile([],[c]) if x['id']==todo['id'])['status']=='resolved'
