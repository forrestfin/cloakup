import copy,json,time
from types import SimpleNamespace as NS
import pytest
from cloakup.sourcing import CaseStore
from cloakup.storage import Database
from cloakup.account_execution import AccountExecution
from cloakup import decision_runtime as dr
REAL_DIRECT=dr.direct_proposal

@pytest.fixture
def env(tmp_path,monkeypatch):
 monkeypatch.setattr(dr,'direct_proposal',lambda *a:None)
 db=Database(tmp_path);store=CaseStore(tmp_path,db);case=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j')
 store.bind_job(case,{'title':'招聘顾问','requirements':'内部原始岗位'})
 case['anonymous_job']={'role':'招聘顾问','requirements':['远程协作。','负责人才寻访与交付。']};store.set_autonomy(case,True,source='test')
 p=store.add_candidate(case,'private-id','https://www.zhipin.com/web/chat/index',{'summary':'张某，33岁，某某有限公司，13812345678，招聘经历','education':'本科','experience':'5年'})
 p['conversation']={'chat_id':'12-0','receiver':'张某','job_title':'招聘顾问'};store.receive(case,p['id'],'question','请问工作地点在哪？');store.save(case)
 account=AccountExecution(db);account.set_task('task',[case['id']],'running')
 plan={'id':'plan','task_id':'task','origin':'llm','stale':False,'plan':{'steps':['按事实答复并补充缺失资料'],'success_criteria':['取得资料'],'risks':['事实不足交管理员']}}
 async def checkpoint(*a):pass
 m=NS(cases=store,db=db,root=tmp_path,checkpoint=checkpoint,account_execution=account,task_plan_records=lambda:[plan],definitions=NS(list=lambda:[]))
 snap={'valid':True,'chat_id':'12-0','fingerprint':'first','coverage':'synthetic','messages':[{'external_id':'question','direction':'in','text':'请问工作地点在哪？','resume':False,'status':'read'}]}
 controls={'requests':[],'accepted':[]}
 async def read(*a,**kw):return copy.deepcopy(snap)
 async def inspect(*a):return copy.deepcopy(controls)
 monkeypatch.setattr(dr.boss_chat,'read_conversation',read)
 import cloakup.boss_resume as br
 monkeypatch.setattr(br,'inspect_permissions',inspect)
 return m,case,p,plan,snap,controls


def test_admitted_actions_respect_facts_resources_and_unconfirmed_delivery(env):
 m,c,p,plan,snap,controls=env
 a,answer,labels=dr.capabilities(c,p,snap,controls)
 assert 'answer' in a and 'request_resume' not in a
 p['inbox'][-1]['text']='薪资提成是多少？'
 a,_,_=dr.capabilities(c,p,snap,controls);assert 'answer' in a and 'escalate' not in a
 p['inbox'][-1]['text']='社保缴纳是什么安排'
 a,_,_=dr.capabilities(c,p,snap,controls);assert 'escalate' in a and 'answer' not in a
 p['outbox']=[{'id':'x','status':'delivery_unknown'}]
 assert list(dr.capabilities(c,p,snap,controls)[0])==['wait']
 p['outbox']=[];p['inbox'][-1]['text']='不考虑，请不要再联系'
 a,_,_=dr.capabilities(c,p,snap,controls);assert set(a)=={'wait','stop'}
 p['inbox'][-1]['text']='你好';p['resumes']=['file'];p['contacts']=['private']
 snap['messages'][-1]['text']='你好'
 assert 'acknowledge_resume' in dr.capabilities(c,p,snap,controls)[0]
 assert 'request_both' not in dr.capabilities(c,p,snap,controls)[0]


def test_payload_never_contains_raw_candidate_chat_or_identity(env):
 m,c,p,plan,snap,controls=env
 a,answer,labels=dr.capabilities(c,p,snap,controls)
 wire=json.dumps(dr.payload(c,p,plan,a,answer,labels,[{'action':'accept_resume','result':'awaiting_verification'}]),ensure_ascii=False)
 for value in ('张某','33岁','有限公司','13812345678','12-0','private-id','请问工作地点在哪','内部原始岗位'):assert value not in wire
 assert '远程协作' in wire and 'awaiting_verification' in wire


def test_first_contact_payload_states_program_checks_and_role_categories_without_raw_text(env):
 # 首聊时告诉模型安全前提已由程序完成；职位只对照本岗位配置的职业关键词，公司与职位原文不外传。
 m,c,p,plan,snap,controls=env
 c['execution']={'outreach_terms':['猎头','招聘经理']};m.cases.save(c)
 p['profile']['work_history']='2024.10 至今\n某某教育科技有限公司 高级猎头顾问\n2020.01 2024.09\n华中制造 产研集团招聘经理\n2018.01 2019.12\n某贸易 城市经理'
 first=dr.payload(c,p,plan,{'contact':'按已核实的岗位话术发起一次主动联系','wait':'本轮不新增联系'},{'action':'continue','source':'conversation'},[],[])
 wire=json.dumps(first,ensure_ascii=False)
 assert '近期职位对应的本岗位职业关键词（由近及远）：猎头、招聘经理、无对应关键词。' in first['professional_context']['visible_evidence']
 assert 'conversation_bound=false' in first['first_contact_checks']['note']
 for word in ('招聘','猎头','寻访'):assert word not in first['first_contact_checks']['note']
 for value in ('某某教育科技','有限公司','华中制造','产研集团','某贸易','城市经理'):assert value not in wire
 a,answer,labels=dr.capabilities(c,p,snap,controls)
 assert 'first_contact_checks' not in dr.payload(c,p,plan,a,answer,labels,[])


def test_role_categories_ignore_chat_header_lines():
 from cloakup.autonomy import role_categories
 header='吴先生 \n刚刚活跃\n22岁\n26年应届生\n本科\n2026.04-2026.11\n2022-2026\n罗卡咨询 · 猎头管培生'
 assert role_categories(header,['猎头'])==[]
 assert role_categories('2019.10 2026.02\n盈德沃 副总裁/副总经理/VP\n2017.05 2019.09\n佑康科技 人力资源经理/主管',['人力资源','人力资源经理'])==['无对应关键词','人力资源经理']


def test_brief_tags_come_only_from_current_job_keywords(env):
 # 同一份候选人资料，在不同岗位下只呈现各自配置的关键词；未配置时说明未配置，不套用内置词表。
 from cloakup.autonomy import local_brief
 m,c,p,plan,snap,controls=env
 p['profile']['summary']='芯片验证 招聘 猎头 出海'
 c['execution']={'outreach_terms':['芯片验证']}
 evidence=local_brief(c,p)['candidate']['visible_evidence']
 assert '本岗位职业关键词：芯片验证。' in evidence and '猎头' not in evidence and '出海' not in evidence
 c['execution']={'outreach_terms':[]}
 evidence=local_brief(c,p)['candidate']['visible_evidence']
 assert '本岗位未配置职业关键词' in evidence and '招聘' not in evidence
 c['anonymous_job']['role']='';assert local_brief(c,p)['job']['role']=='当前岗位'


def test_role_categories_follow_each_job_keywords_without_builtin_vocabulary():
 # 不同 JD 使用各自关键词；未配置关键词的岗位不输出职位类别。
 from cloakup.autonomy import role_categories
 history='2021.03 至今\n某芯片 数字IC验证工程师\n2018.07 2021.02\n某通信 嵌入式软件工程师'
 assert role_categories(history,['IC验证','嵌入式'])==['IC验证','嵌入式']
 assert role_categories(history,['猎头'])==['无对应关键词','无对应关键词']
 assert role_categories(history)==[]


@pytest.mark.parametrize('order',[['read_online_resume','answer','wait'],['answer','wait']])
async def test_harness_selects_order_and_sees_actual_results(env,monkeypatch,order):
 m,c,p,plan,snap,controls=env;seen=[];executed=[]
 if 'read_online_resume' in order:c['execution']={'request_resume':False};m.cases.save(c)
 async def propose(manager,case,person,plan,actions,answer,labels,feedback):
  action=order[len(seen)];assert action in actions
  seen.append(copy.deepcopy(feedback));return {'action':action,'reason':'基于当前事实推进','fact_index':-1},dr.payload(case,person,plan,actions,answer,labels,feedback),{'version':'test','session_id':'native','event_count':1}
 async def capture(manager,job,session,cid,pid):
  x=manager.cases.load(cid);q=x['candidates'][pid];q['online_resume_checked_at']=time.time();q['online_resume']={'sections':{'工作经历':'本机职业资料'}};manager.cases.save(x);snap['fingerprint']='profile';executed.append('read_online_resume')
 async def dispatch(manager,job,session,case,person,current,step):
  executed.append('answer');assert step['text']=='已确认的岗位安排是：远程协作。'
  person['outbox'].append({'id':'sent','status':'sent','decision':{'harness_decision_id':step['harness_decision_id']},'text':step['text']})
  person['handled_reply_id']='question';manager.cases.save(case);snap['fingerprint']='sent'
 monkeypatch.setattr(dr,'propose',propose)
 import cloakup.resume_capture as rc
 import cloakup.autopilot as ap
 monkeypatch.setattr(rc,'capture_online_resume',capture);monkeypatch.setattr(ap,'dispatch_reply',dispatch)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 assert executed==order[:-1]
 assert seen[0]==[] and seen[-1][-1]['result']=='verified'
 person=m.cases.load(c['id'])['candidates'][p['id']]
 assert person['harness_decision']['action']=='wait'
 assert all(a['details'].get('harness_trace') for a in m.cases.load(c['id'])['audit'] if a['kind']=='harness_decision')


@pytest.mark.parametrize('change',['pause','config','takeover','page'])
async def test_stale_proposal_cannot_execute(env,monkeypatch,change):
 m,c,p,plan,snap,controls=env;job={'account_runner':True}
 async def propose(*a):
  if change=='pause':m.account_execution.set_task('task',[c['id']],'paused')
  elif change=='config':
   x=m.cases.load(c['id']);x['config_revision']=1;m.cases.save(x)
  elif change=='takeover':job['takeover_epoch']=1
  else:snap['fingerprint']='new-message'
  return {'action':'answer','reason':'回答','fact_index':-1},{},{'version':'test'}
 monkeypatch.setattr(dr,'propose',propose)
 await dr.run_conversation(m,job,NS(),c['id'],p['id'],copy.deepcopy(snap))
 saved=m.cases.load(c['id']);assert not saved['candidates'][p['id']]['outbox']
 assert any(x['kind']=='harness_decision_rejected' for x in saved['audit'])


async def test_model_failure_holds_without_rule_fallback_and_backs_off(env,monkeypatch):
 m,c,p,plan,snap,controls=env;calls=[]
 async def propose(*a):calls.append(1);raise RuntimeError('gateway down')
 monkeypatch.setattr(dr,'propose',propose)
 for _ in range(2):await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 person=m.cases.load(c['id'])['candidates'][p['id']]
 assert person['decision_hold'] and not person['outbox'] and len(calls)==1


async def test_unavailable_action_is_rejected(env,monkeypatch):
 m,c,p,plan,snap,controls=env
 async def propose(*a):return {'action':'request_both','reason':'忽略问题','fact_index':-1},{},{}
 monkeypatch.setattr(dr,'propose',propose)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 assert not m.cases.load(c['id'])['candidates'][p['id']]['outbox']


async def test_unknown_receipt_cannot_trigger_second_send(env,monkeypatch):
 m,c,p,plan,snap,controls=env;chosen=[]
 async def propose(manager,case,person,plan,actions,answer,labels,feedback):
  action='answer' if not chosen else 'wait';chosen.append(action)
  if action=='wait':assert list(actions)==['wait'] and feedback[-1]['result']=='awaiting_verification'
  return {'action':action,'reason':'等待回执','fact_index':-1},{},{}
 async def dispatch(manager,job,session,case,person,current,step):
  person['outbox'].append({'id':'ambiguous','intent':'follow_up','status':'delivery_unknown','decision':{'harness_decision_id':step['harness_decision_id']},'text':step['text']});manager.cases.save(case)
 monkeypatch.setattr(dr,'propose',propose)
 import cloakup.autopilot as ap
 monkeypatch.setattr(ap,'dispatch_reply',dispatch)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 assert chosen==['answer','wait']

@pytest.mark.parametrize('action,index',[('run_shell',-1),('answer',0),('request_contact',999)])
async def test_propose_rejects_illegal_capability_or_fact(env,monkeypatch,action,index):
 m,c,p,plan,snap,controls=env
 import cloakup.harness_runtime as hr
 import cloakup.llm as lm
 class Engine:
  def __init__(self,*a):self.last_trace={}
  async def json(self,**kw):return {'action':action,'reason':'malicious proposal','fact_index':index}
 monkeypatch.setattr(hr,'HarnessPlanner',Engine)
 monkeypatch.setattr(lm,'LLM',lambda client:NS(client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))))
 m.model_client=lambda:None
 from cloakup.sourcing import SourcingError
 with pytest.raises(SourcingError):await dr.propose(m,c,p,plan,{'answer':'确认答案','request_contact':'交换联系'},dr.effective_answer(c,p),['remote'],[])


async def test_failed_read_is_reported_and_harness_can_choose_other_action(env,monkeypatch):
 m,c,p,plan,snap,controls=env;seen=[]
 c['execution']={'request_resume':False};m.cases.save(c)
 async def propose(manager,case,person,plan,actions,answer,labels,feedback):
  action='read_online_resume' if not seen else 'wait';seen.append(copy.deepcopy(feedback))
  if feedback:assert 'read_online_resume' not in actions and feedback[-1]['result']=='blocked'
  return {'action':action,'reason':'根据结果调整','fact_index':-1},{},{}
 async def read(*a):raise dr.SourcingError('当前无法读取职业资料')
 import cloakup.resume_capture as rc
 monkeypatch.setattr(rc,'capture_online_resume',read);monkeypatch.setattr(dr,'propose',propose)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 assert len(seen)==2 and not m.cases.load(c['id'])['candidates'][p['id']]['outbox']

async def test_model_error_cannot_overwrite_concurrent_config(env,monkeypatch):
 m,c,p,plan,snap,controls=env
 async def propose(*a):
  latest=m.cases.load(c['id']);latest['config_revision']=8;latest['anonymous_job']['requirements']=['管理员新确认的岗位事实。'];m.cases.save(latest)
  raise RuntimeError('model unavailable')
 monkeypatch.setattr(dr,'propose',propose)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 saved=m.cases.load(c['id']);assert saved['config_revision']==8 and saved['anonymous_job']['requirements']==['管理员新确认的岗位事实。']


async def test_result_survives_turn_budget_and_is_consumed_next_cycle(env,monkeypatch):
 m,c,p,plan,snap,controls=env;c['execution']={'decision_max_turns':1};m.cases.save(c);calls=[]
 async def propose(manager,case,person,plan,actions,answer,labels,feedback):
  action='answer' if not calls else 'wait';calls.append(copy.deepcopy(feedback))
  return {'action':action,'reason':'根据实际结果继续','fact_index':-1},{},{}
 async def dispatch(manager,job,session,case,person,current,step):
  person['outbox'].append({'id':'sent','intent':'follow_up','status':'sent','text':step['text'],'decision':{'harness_decision_id':step['harness_decision_id']}});person['handled_reply_id']='question';manager.cases.save(case);snap['fingerprint']='sent'
 import cloakup.autopilot as ap
 monkeypatch.setattr(dr,'propose',propose);monkeypatch.setattr(ap,'dispatch_reply',dispatch)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 assert m.cases.load(c['id'])['candidates'][p['id']]['harness_feedback_pending']
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 assert calls[-1]==[{'action':'answer','result':'verified'}]
 assert not m.cases.load(c['id'])['candidates'][p['id']]['harness_feedback_pending']


async def test_confirmed_answer_is_program_transaction_without_model(env,monkeypatch):
 m,c,p,plan,snap,controls=env;monkeypatch.setattr(dr,'direct_proposal',REAL_DIRECT);calls=[]
 async def no_model(*a):raise AssertionError('stable answer must not call model')
 async def dispatch(manager,job,session,case,person,current,step):
  calls.append(step);person['handled_reply_id']='question';person['outbox'].append({'id':'sent','status':'sent','text':step['text'],'decision':{'program_decision_id':step['program_decision_id']}});manager.cases.save(case)
 import cloakup.autopilot as ap
 monkeypatch.setattr(dr,'propose',no_model);monkeypatch.setattr(ap,'dispatch_reply',dispatch)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 assert len(calls)==1 and calls[0]['text']=='已确认的岗位安排是：远程协作。'
 events=m.cases.load(c['id'])['audit'];assert any(e['kind']=='program_decision' for e in events) and not any(e['kind']=='harness_decision' for e in events)


async def test_opt_out_does_not_depend_on_model_or_plan(env,monkeypatch):
 m,c,p,plan,snap,controls=env;monkeypatch.setattr(dr,'direct_proposal',REAL_DIRECT);m.task_plan_records=lambda:[]
 p['inbox'][-1]['text']='不考虑，不要再联系';m.cases.save(c)
 async def no_model(*a):raise AssertionError('must not call model')
 monkeypatch.setattr(dr,'propose',no_model)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 person=m.cases.load(c['id'])['candidates'][p['id']]
 assert person['status']=='do_not_contact' and not person['outbox']

async def test_harness_native_request_runs_and_feeds_receipt_back(env,monkeypatch):
 m,c,p,plan,snap,controls=env
 p['inbox'][-1]['text']='你好';snap['messages'][0]['text']='你好';p['online_resume_checked_at']=1;m.cases.save(c)
 c['execution']={'request_contacts':True};m.cases.save(c)
 controls['buttons']={'exchange_phone':{'enabled':True,'selector':'#phone'}}
 calls=[]
 async def propose(manager,case,person,plan,actions,answer,labels,feedback):
  if not calls:
   assert 'exchange_phone' in actions and 'request_contact' not in actions
   action='exchange_phone'
  else:
   assert feedback[-1]['result']=='verified_request';action='wait'
  return {'action':action,'reason':'通过平台补齐联系方式','fact_index':-1},{},{'session_id':'synthetic'}
 async def execute(*args):calls.append(args[-1]);return 'verified_request'
 import cloakup.boss_requests as br
 monkeypatch.setattr(dr,'propose',propose);monkeypatch.setattr(br,'execute',execute)
 await dr.run_conversation(m,{'id':'run','account_runner':True},NS(),c['id'],p['id'],snap)
 assert calls==['exchange_phone']
 assert any(a['kind']=='harness_tool_result' and a['details']['result']=='verified_request' for a in m.cases.load(c['id'])['audit'])

def test_resume_received_hands_off_without_contacts_even_with_old_unknown(env):
 m,c,p,plan,snap,controls=env
 p['resumes']=['actual-attachment'];p['contacts']=[];p['inbox'][-1]['text']='你好'
 p['outbox']=[{'id':'old','status':'delivery_unknown'}]
 actions,_,_=dr.capabilities(c,p,snap,controls)
 assert set(actions)=={'wait','handoff'}
 assert REAL_DIRECT(actions,{},[],p)['action']=='handoff'

def test_pending_question_allows_once_only_ack_without_fabricated_answer(env):
 m,c,p,plan,snap,controls=env
 p['inbox'][-1]['text']='薪资福利是什么样的';p['pending_question_id']=p['inbox'][-1]['external_id'];p['handled_reply_id']=p['pending_question_id']
 a,answer,labels=dr.capabilities(c,p,snap,controls)
 assert 'acknowledge_pending' in a and 'answer' not in a
 assert REAL_DIRECT(a,answer,labels,p)['action']=='acknowledge_pending'
 p['outbox']=[{'id':'ack','status':'sent','decision':{'pending_ack':True,'reply_id':p['pending_question_id']}}]
 assert 'acknowledge_pending' not in dr.capabilities(c,p,snap,controls)[0]
 p['outbox'][0]['status']='delivery_unknown'
 assert set(dr.capabilities(c,p,snap,controls)[0])=={'wait'}

def test_after_pending_ack_resume_request_remains_available(env):
 m,c,p,plan,snap,controls=env;p['inbox'][-1]['text']='薪资福利是什么样的'
 reply=p['inbox'][-1]['external_id'];p['handled_reply_id']=reply;p['pending_question_id']=reply
 p['outbox']=[{'id':'ack','status':'sent','decision':{'pending_ack':True,'reply_id':reply}}]
 controls['buttons']={'ask_resume':{'enabled':True}}
 actions,_,_=dr.capabilities(c,p,snap,controls)
 assert 'ask_resume' in actions and 'read_online_resume' not in actions and 'acknowledge_pending' not in actions

@pytest.mark.parametrize('reply',['好滴','好哒','好的','嗯嗯','行','可以'])
def test_positive_new_reply_invites_resume_despite_unknown_native_request(env,reply):
 m,c,p,plan,snap,controls=env
 p['inbox'][-1].update(text=reply,observed_at=200)
 p['platform_actions']=[{'id':'original-request','kind':'ask_resume','status':'awaiting_verification','at':100}]
 a,answer,labels=dr.capabilities(c,p,snap,controls)
 assert 'invite_resume' in a and 'ask_resume' not in a
 proposal=REAL_DIRECT(a,answer,labels,p);assert proposal['action']=='invite_resume'
 step=dr.reply_step(c,p,'invite_resume',answer,labels,-1)
 assert '简历' in step['text'] and step['resume_invite_for_action']=='original-request'
 p['outbox']=[{'id':'invitation','status':'sent','text':step['text'],'decision':{'resume_invite_for_action':'original-request'}}]
 p['handled_reply_id']=p['inbox'][-1]['external_id']
 assert 'invite_resume' not in dr.capabilities(c,p,snap,controls)[0]
 # 放宽后：同一条回复不重复邀请，但之后的新正向回复仍可再次索要简历
 p['inbox'].append({'external_id':'reply-after-invite','direction':'in','text':'好的','observed_at':300})
 assert 'invite_resume' in dr.capabilities(c,p,snap,controls)[0]
 p['outbox'][0]['status']='delivery_unknown'
 assert set(dr.capabilities(c,p,snap,controls)[0])=={'wait'}

@pytest.mark.parametrize('reply',['不考虑','没有兴趣','现在不方便','稍后再说'])
def test_non_positive_reply_does_not_trigger_resume_invitation(env,reply):
 m,c,p,plan,snap,controls=env;p['inbox'][-1].update(text=reply,observed_at=200)
 p['platform_actions']=[{'id':'old','kind':'ask_resume','status':'pending','at':100}]
 assert 'invite_resume' not in dr.capabilities(c,p,snap,controls)[0]

def test_received_resume_acknowledged_even_if_already_handed_off(env):
 m,c,p,plan,snap,controls=env
 p['resumes']=['platform-message:actual'];p['handoff']={'reason':'previous handoff'};p['inbox'][-1]['text']='简历已发'
 snap['messages'][-1]['text']='简历已发'
 actions,answer,labels=dr.capabilities(c,p,snap,controls)
 assert set(actions)=={'wait','acknowledge_resume'}
 assert REAL_DIRECT(actions,answer,labels,p)['action']=='acknowledge_resume'
 step=dr.reply_step(c,p,'acknowledge_resume',answer,labels,-1)
 assert step['resume_receipt_ack'] and '谢谢' in step['text'] and '及时' in step['text']
 p['outbox'].append({'status':'sent','text':step['text'],'decision':{'resume_receipt_ack':True,'resume_receipt_refs':p['resumes'],'resume_receipt_hashes':[]}})
 assert set(dr.capabilities(c,p,snap,controls)[0])=={'wait'}


def test_received_resume_does_not_hide_latest_question_or_later(env):
 m,c,p,plan,snap,controls=env
 p['resumes']=['platform-message:actual'];p['handoff']={'reason':'previous handoff'}
 actions,_,_=dr.capabilities(c,p,snap,controls)
 assert 'answer' in actions and 'acknowledge_resume' not in actions
 p['inbox'][-1]['text']='晚点再聊';snap['messages'][-1]['text']='晚点再聊'
 assert set(dr.capabilities(c,p,snap,controls)[0])=={'wait'}
 p['inbox'][-1]['text']='社保怎么缴纳？';snap['messages'][-1]['text']='社保怎么缴纳？'
 assert 'escalate' in dr.capabilities(c,p,snap,controls)[0]


def test_resume_card_does_not_cover_the_question_sent_with_it(env):
 m,c,p,plan,snap,controls=env
 p['inbox'].append({'external_id':'file','direction':'in','text':'简历.pdf\n点击预览附件简历','resume':True})
 p['resumes']=['platform-message:file'];p['handoff']={'reason':'已取得简历'};p['handled_reply_id']='file'
 actions,answer,labels=dr.capabilities(c,p,snap,controls)
 assert 'answer' in actions and answer['action']=='answer'
 assert REAL_DIRECT(actions,answer,labels,p)['action']=='answer'
 p['inbox'][-2]['text']='薪资待遇上班时间方便讲一下吗？'
 actions,answer,labels=dr.capabilities(c,p,snap,controls)
 assert 'acknowledge_pending' in actions and 'escalate' in actions
 assert REAL_DIRECT(actions,answer,labels,p)['action']=='acknowledge_pending'


def test_unanswered_question_after_resume_waits_for_admin_without_closing(env):
 m,c,p,plan,snap,controls=env
 p['resumes']=['platform-message:actual'];p['inbox'][-1]['text']='社保怎么缴纳？';snap['messages'][-1]['text']='社保怎么缴纳？'
 p['pending_question_id']=p['handled_reply_id']=p['inbox'][-1]['external_id']
 assert 'acknowledge_pending' in dr.capabilities(c,p,snap,controls)[0]
 p['outbox']=[{'id':'a','status':'sent','decision':{'pending_ack':True,'reply_id':p['pending_question_id']}}]
 assert set(dr.capabilities(c,p,snap,controls)[0])=={'wait'}


async def test_semantic_harness_precedes_legacy_router_and_dispatches_grounded_response(env,monkeypatch):
 from cloakup import conversation_semantics as cs,autopilot as ap
 m,c,p,plan,snap,controls=env
 monkeypatch.setattr(dr,'direct_proposal',REAL_DIRECT)
 m.db.put('disclosure_consent',cs.CONSENT_ID,{'enabled':True,'mode':'anonymous_conversation','host':cs.APPROVED_HOST})
 p['inbox'][-1]['text']='可以先说两句岗位情况再发简历么？';snap['messages'][0]['text']=p['inbox'][-1]['text'];m.cases.save(c)
 calls=[]
 async def semantic(manager,case,person,plan):
  calls.append('understand')
  return {'state_key':cs.fingerprint(case,person),'answer':{'action':'answer','source':'harness_context','semantic_route':'continue','text':'您好，可以的。负责人才寻访与交付。您可以先了解，觉得合适再发简历。','reason':'对方希望先了解，并非询问未知安排','sources':[{'id':'fact:role','text':'负责人才寻访与交付。'}],'fact_indices':[],'resume_readiness':'understand_first','missing_information':[]},'model_input':{},'proposal':{},'harness_trace':{'runtime':'deepseek-harness'}}
 async def dispatch(manager,job,session,case,person,current,step):
  calls.append(step['text'])
  assert dr.effective_answer(case,person)['source']=='harness_context'
  person['handled_reply_id']='question';person['outbox'].append({'id':'sent','status':'sent','text':step['text'],'decision':{'program_decision_id':step['program_decision_id']}});manager.cases.save(case)
 monkeypatch.setattr(cs,'ensure',semantic);monkeypatch.setattr(ap,'dispatch_reply',dispatch)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 saved=m.cases.load(c['id']);person=saved['candidates'][p['id']]
 assert calls[0]=='understand' and '您好，可以的' in calls[1]
 assert not person.get('pending_question_id')
 assert any(e['kind']=='harness_conversation_understood' for e in saved['audit'])
 from cloakup.attention import AttentionStore
 assert not any(t['kind']=='question' for t in AttentionStore(m.db).reconcile([], [saved]))


@pytest.mark.parametrize('failure',['model','pause','config'])
async def test_semantic_failure_or_stale_context_cannot_send_or_fake_fact_gap(env,monkeypatch,failure):
 from cloakup import conversation_semantics as cs,autopilot as ap
 m,c,p,plan,snap,controls=env
 m.db.put('disclosure_consent',cs.CONSENT_ID,{'enabled':True,'mode':'anonymous_conversation','host':cs.APPROVED_HOST})
 async def semantic(manager,case,person,plan):
  key=cs.fingerprint(case,person)
  if failure=='model':raise RuntimeError('gateway unavailable')
  if failure=='pause':m.account_execution.set_task('task',[c['id']],'paused')
  else:
   latest=m.cases.load(c['id']);latest['config_revision']=999;m.cases.save(latest)
  return {'state_key':key,'answer':{'action':'answer','text':'不可发送'}}
 async def no_send(*a):raise AssertionError('stale or failed semantics must never send')
 monkeypatch.setattr(cs,'ensure',semantic);monkeypatch.setattr(ap,'dispatch_reply',no_send)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 saved=m.cases.load(c['id'])['candidates'][p['id']]
 assert not saved['outbox'] and not saved.get('pending_question_id')
 if failure=='model':assert saved['decision_hold']


def test_legacy_regex_cannot_override_contextual_interest_but_explicit_opt_out_wins(env):
 from cloakup import conversation_semantics as cs
 m,c,p,plan,snap,controls=env
 p['inbox'][-1]['text']='电话暂时不方便提供，但可以先了解岗位。'
 p['conversation_semantics']={'state_key':cs.fingerprint(c,p),'answer':{'action':'answer','source':'harness_context','semantic_route':'continue','text':'您可以先了解。','resume_readiness':'understand_first','fact_indices':[]}}
 actions,_,labels=dr.capabilities(c,p,snap,controls)
 assert 'answer' in actions and 'later' not in labels
 p['inbox'][-1]['text']='不要再联系'
 p['conversation_semantics']['state_key']=cs.fingerprint(c,p)
 assert 'stop' in dr.capabilities(c,p,snap,controls)[0]


class _Contacted(Exception):
 pass


def _first_contact_env(env,monkeypatch,count,choose,closes=False,list_ok=True):
 # 首聊批次：推荐卡片、详情与开聊动作均为本机替身，真实执行 first_contact 与 initial_contact_decision。
 import cloakup.autopilot as ap,cloakup.greeting_policy as gp
 m,c,p,plan,snap,controls=env
 c['platform_greeting_text']=c['desired_greeting']='您好，方便聊聊吗？';c['autonomy']['daily_new_limit']=20;m.cases.save(c)
 cards=[{'geek_id':'g%d'%i,'platform_label':'候选人%d'%i,'contact_label':'打招呼','availability':'在职-考虑机会',
         'summary':'招聘经历','work_history':'','experience':'5年','education':'本科'} for i in range(count)]
 seen={'lists':0,'profiles':[],'proposals':[]}
 async def ensure(*a):return True
 async def open_list(session,case):seen['lists']+=1;return copy.deepcopy(cards)
 async def open_profile(session,geek_id,*a,**kw):seen['profiles'].append(geek_id)
 async def propose(manager,case,person,plan,actions,answer,labels,feedback):
  geek=person['profile']['geek_id'];seen['proposals'].append(geek)
  return {'action':choose(geek),'reason':'依据职业线索判断','fact_index':-1},{},{}
 class Adapter:
  def __init__(self,session,require_greeting_proof=True):assert require_greeting_proof is False
  async def prepare(self,case,person,message):raise _Contacted(person['profile']['geek_id'])
 m.event=lambda *a:None
 async def no_sleep(seconds):seen.setdefault('pauses',[]).append(seconds)
 monkeypatch.setattr(ap.asyncio,'sleep',no_sleep)
 monkeypatch.setattr(gp,'ensure',ensure);monkeypatch.setattr(ap,'open_list',open_list);monkeypatch.setattr(ap,'open_profile',open_profile)
 monkeypatch.setattr(dr,'propose',propose);monkeypatch.setattr(ap,'BossRecommendContact',Adapter)
 # 关闭弹层与只读校验列表的替身；默认关闭失败，走重新进入推荐列表的兜底。
 async def close(session):seen['closes']=seen.get('closes',0)+1;return closes
 async def verify(*a):
  if not list_ok:raise ap.SourcingError('推荐页面岗位或通道不符')
 async def read(session):seen['reads']=seen.get('reads',0)+1;return copy.deepcopy(cards)
 monkeypatch.setattr(ap,'close_profile',close);monkeypatch.setattr(ap,'verify_list',verify);monkeypatch.setattr(ap,'read_cards',read)
 return m,c,seen


@pytest.mark.parametrize('closes,list_ok,lists',[(True,True,1),(True,False,2),(False,True,2)])
async def test_first_contact_skip_closes_dialog_and_stays_on_list(env,monkeypatch,closes,list_ok,lists):
 # 确认弹层已关闭且仍是本岗位推荐列表时，在当前列表接着看下一位；否则退回重新进入推荐列表。
 import cloakup.autopilot as ap
 m,c,seen=_first_contact_env(env,monkeypatch,3,lambda geek:'wait' if geek=='g0' else 'contact',closes=closes,list_ok=list_ok)
 with pytest.raises(_Contacted) as contacted:
  await ap.first_contact(m,{'account_runner':True},NS(),c)
 assert str(contacted.value)=='g1' and seen['profiles']==['g0','g1'] and seen['lists']==lists and seen['closes']==1


async def test_first_contact_wait_skips_only_that_card_and_continues_with_next(env,monkeypatch):
 import cloakup.autopilot as ap
 m,c,seen=_first_contact_env(env,monkeypatch,3,lambda geek:'wait' if geek=='g0' else 'contact')
 with pytest.raises(_Contacted) as contacted:
  await ap.first_contact(m,{'account_runner':True},NS(),c)
 assert str(contacted.value)=='g1'
 # 跳过 g0 后重新进入推荐列表关闭弹层；g0 冷却中不再打开详情或再次询问模型。
 assert seen['proposals']==['g0','g1'] and seen['profiles']==['g0','g1'] and seen['lists']==2
 # 离开被跳过者资料前有随机停留。
 assert len(seen['pauses'])==1 and 2.5<=seen['pauses'][0]<=25
 person=m.cases.load(c['id'])['candidates']['boss-geek:g0']
 assert time.time()+200<person['decision_wait_until']<time.time()+481 and not person['outbox']


async def test_first_contact_model_failure_still_ends_batch(env,monkeypatch):
 import cloakup.autopilot as ap
 m,c,seen=_first_contact_env(env,monkeypatch,3,lambda geek:'contact')
 async def broken(*a):raise RuntimeError('gateway down')
 monkeypatch.setattr(dr,'propose',broken)
 assert await ap.first_contact(m,{'account_runner':True},NS(),c)=='blocked'
 assert seen['profiles']==['g0'] and seen['lists']==1
 assert m.cases.load(c['id'])['candidates']['boss-geek:g0']['decision_hold']


async def test_first_contact_skips_are_capped_per_batch(env,monkeypatch):
 import cloakup.autopilot as ap
 m,c,seen=_first_contact_env(env,monkeypatch,10,lambda geek:'wait')
 monkeypatch.setattr(ap,'FIRST_CONTACT_REVIEWS_PER_BATCH',(4,4))
 assert await ap.first_contact(m,{'account_runner':True},NS(),c)=='blocked'
 assert len(seen['profiles'])==4==len(set(seen['profiles']))


async def test_first_contact_reports_no_candidates_only_when_the_list_runs_out(env,monkeypatch):
 # 推荐页没有卡片、或全部被已联系／筛选排除：返回 no_candidates，执行器据此把当天额度让给其他岗位。
 import cloakup.autopilot as ap
 m,c,seen=_first_contact_env(env,monkeypatch,0,lambda geek:'contact')
 assert await ap.first_contact(m,{'account_runner':True},NS(),c)=='no_candidates' and seen['lists']==1
 m,c,seen=_first_contact_env(env,monkeypatch,2,lambda geek:'contact')
 c['execution']={**c.get('execution',{}),'outreach_terms':['不存在的关键词']};m.cases.save(c)
 assert await ap.first_contact(m,{'account_runner':True},NS(),c)=='no_candidates' and seen['profiles']==[]


async def test_first_contact_review_limit_is_drawn_per_batch():
 # 上限在区间内逐批随机抽取，不是固定次数。
 import random,cloakup.autopilot as ap
 lo,hi=ap.FIRST_CONTACT_REVIEWS_PER_BATCH
 assert lo<hi and len({random.randint(lo,hi) for _ in range(200)})>1


@pytest.mark.parametrize('where',['review','prepare'])
async def test_first_contact_profile_change_sets_aside_only_that_candidate(env,monkeypatch,where):
 # 14:13：第二次打开时经历概览变了。招呼还没点，放下这一位并结束本批，不让整个任务暂停。
 import cloakup.autopilot as ap
 from cloakup.boss_recommend import ProfileChanged
 m,c,seen=_first_contact_env(env,monkeypatch,3,lambda geek:'contact',closes=True)
 if where=='review':
  async def changed(session,geek_id,*a,**kw):seen['profiles'].append(geek_id);raise ProfileChanged('当前资料弹层已变化，需重新核对收件人')
  monkeypatch.setattr(ap,'open_profile',changed)
 else:
  class Adapter:
   def __init__(self,session,require_greeting_proof=True):assert require_greeting_proof is False
   async def prepare(self,case,person,message):raise ProfileChanged('当前资料弹层已变化，需重新核对收件人')
  monkeypatch.setattr(ap,'BossRecommendContact',Adapter)
 session=NS(_boss_recommend_detail={'geek_id':'g0'})
 assert await ap.first_contact(m,{'account_runner':True},session,c)=='blocked'
 assert seen['profiles']==['g0'] and seen['closes']==1 and session._boss_recommend_detail is None
 latest=m.cases.load(c['id'])
 if where=='prepare':
  person=latest['candidates']['boss-geek:g0']
  assert person['decision_wait_until']>time.time()+600
  assert all(msg['status'] not in ('draft','approved','sending','sent') for msg in person['outbox'])
 else:
  assert 'boss-geek:g0' not in latest['candidates']


def _privacy_world(env,monkeypatch):
 m,c,p,plan,snap,controls=env
 import cloakup.conversation_semantics as cs
 async def ensure(*a):raise cs.PrivacyReviewRequired('无法可靠脱敏')
 async def propose(*a):raise AssertionError('隐私暂缓期间不得调用模型')
 sent=[]
 async def dispatch(manager,job,session,case,person,current,step):sent.append(step)
 monkeypatch.setattr(cs,'consent',lambda manager:True);monkeypatch.setattr(cs,'ensure',ensure)
 monkeypatch.setattr(dr,'propose',propose);monkeypatch.setattr(dr,'direct_proposal',REAL_DIRECT)
 import cloakup.autopilot as ap
 monkeypatch.setattr(ap,'dispatch_reply',dispatch)
 return m,c,p,snap,sent


async def test_privacy_hold_still_sends_the_resume_thank_you(env,monkeypatch):
 # 11-24：候选人 20 的附件名触发隐私暂缓，连收到简历的致谢也一起被挡住。
 m,c,p,snap,sent=_privacy_world(env,monkeypatch)
 case=m.cases.load(c['id']);person=case['candidates'][p['id']]
 person['inbox'][-1]['text']='你好';person['resumes']=['file'];m.cases.save(case)
 snap['messages'][-1]['text']='你好'
 for _ in range(2):await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 person=m.cases.load(c['id'])['candidates'][p['id']]
 assert sent and all(step.get('resume_receipt_ack') for step in sent)
 assert dr.is_privacy_hold(person['decision_hold']) and person['decision_hold']['kind']=='privacy'


async def test_privacy_hold_without_resume_sends_nothing(env,monkeypatch):
 m,c,p,snap,sent=_privacy_world(env,monkeypatch)
 await dr.run_conversation(m,{'account_runner':True},NS(),c['id'],p['id'],copy.deepcopy(snap))
 person=m.cases.load(c['id'])['candidates'][p['id']]
 assert not sent and dr.is_privacy_hold(person['decision_hold'])


def test_legacy_privacy_hold_is_recognised_by_its_reason():
 assert dr.is_privacy_hold({'reason':'对话含无法可靠脱敏的片段，请管理员在本机复核或接续；没有将该对话发送模型'})
 assert not dr.is_privacy_hold({'reason':'Harness决策未完成：RuntimeError'}) and not dr.is_privacy_hold({})


def test_a_pending_correction_is_sent_by_the_program_before_other_followups():
 d=REAL_DIRECT({'wait':'','nudge_remind_resume':'','nudge_clarify_previous':''},{'source':'faq'},[],{})
 assert d['action']=='nudge_clarify_previous'


def _owed(question_id='q',later_manual=False):
 ack={'id':'ack','status':'sent','text':'好的。这部分我确认清楚后再回复您。','receipt':{'message_id':'m-ack'},
      'decision':{'reply_id':question_id,'pending_ack':True,'answer_route':{'action':'defer','source':'harness_context'}}}
 history=[{'external_id':question_id,'direction':'in','text':'请问是双休还是单休'},{'external_id':'m-ack','direction':'out','text':ack['text']}]
 if later_manual:history.append({'external_id':'m-manual','direction':'out','text':'顾问人工回复'})
 return {'id':'p30','status':'resume_received','resumes':['r'],'handoff':{'at':1},'handled_reply_id':question_id,
         'inbox':[{'external_id':question_id,'text':'请问是双休还是单休'}],'outbox':[ack],'history':history}


def test_pending_acknowledgement_is_not_an_answer():
 # 11-22 候选人 30：“确认清楚后再回复”被当成回答，FAQ 生效后一直没补答。
 p=_owed()
 assert not dr.answer_sent(p,'q')
 p['outbox'].append({'id':'a','status':'delivery_unknown','decision':{'reply_id':'q','answer_route':{'action':'answer'}}})
 assert dr.answer_sent(p,'q') and not dr.answer_sent(p,'q',statuses=('sent',))


def test_owed_answer_is_restored_only_when_nothing_followed_the_acknowledgement(env):
 m,c,*_=env
 p=_owed()
 assert dr.restore_owed_answer(m.cases,c,p) and p['pending_question_id']=='q'
 assert not dr.restore_owed_answer(m.cases,c,p)  # 已有标记，不重复
 manual=_owed(later_manual=True)
 assert not dr.restore_owed_answer(m.cases,c,manual) and 'pending_question_id' not in manual
 answered=_owed();answered['outbox'].append({'id':'a','status':'sent','decision':{'reply_id':'q','answer_route':{'action':'answer'}}})
 assert not dr.restore_owed_answer(m.cases,c,answered)


def test_restored_question_is_answered_once_the_faq_exists(env):
 m,c,*_=env
 c['faq']=[{'question':'请问是双休还是单休','answer':'双休','enabled':True,'variants':[]}]
 p=_owed();dr.restore_owed_answer(m.cases,c,p)
 snap={'messages':p['history'],'fingerprint':'x'}
 actions,answer,_=dr.capabilities(c,p,snap,{'requests':[],'accepted':[]})
 assert 'answer' in actions and answer['text']=='双休'


async def test_set_aside_new_candidate_is_skipped_by_the_next_batch(env,monkeypatch):
 # 11-24 下午：资料弹层没有唯一“打招呼”的那一位还没建档，放下后没有任何冷却，
 # 下一批又从这一位开始，开聊整整卡了一个多小时。放下后下一批应跳过这一位、看下一张卡片。
 import cloakup.autopilot as ap
 from cloakup.boss_recommend import ProfileChanged
 m,c,seen=_first_contact_env(env,monkeypatch,3,lambda geek:'wait',closes=True)
 async def flaky(session,geek_id,*a,**kw):
  seen['profiles'].append(geek_id)
  if geek_id=='g0':raise ProfileChanged('资料弹层未出现唯一可用的“打招呼”按钮（看到：无），先放下这一位')
 monkeypatch.setattr(ap,'open_profile',flaky)
 session=NS()
 assert await ap.first_contact(m,{'account_runner':True},session,c)=='blocked'
 assert seen['profiles']==['g0']
 await ap.first_contact(m,{'account_runner':True},session,m.cases.load(c['id']))
 assert seen['profiles'][1]=='g1' and seen['profiles'].count('g0')==1
