import asyncio,copy,json,uuid
from types import SimpleNamespace as NS
import pytest
from test_agent_workspace import setup,passing_test
from cloakup.scenario_workspace import ScenarioWorkspace,DEFAULT_POLICY
from cloakup.task_policy import overlay

@pytest.fixture
def scene(setup):
 lab,m,t,c=setup
 s=ScenarioWorkspace(m);m.scenario_workspace=s
 return s,lab,m,t,c

def test_bootstrap_abstracts_jobs_without_changing_task(scene):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0]
 assert c['job']['label'] not in json.dumps(template,ensure_ascii=False)
 assert m.definitions.get(t['id'])==t
 assert not s.catalog()['bindings']
 assert set(template['policy']['execution'])==set(DEFAULT_POLICY['execution'])
 assert any(item['engine']=='liepin_sourcing' for item in s.catalog()['available'])

def test_save_cas_and_scope_guard(scene):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0];policy=copy.deepcopy(template['policy']);policy['instructions']='针对招聘顾问，先回复再索取简历'
 saved=s.save(template['id'],{'expected_revision':1,'policy':policy})
 assert saved['revision']==2 and '{{岗位名称}}' in saved['policy']['instructions']
 with pytest.raises(ValueError,match='修改'):s.save(template['id'],{'expected_revision':1,'policy':policy})
 policy['execution']['daily_new_limit']=20
 with pytest.raises(ValueError,match='通用'):s.save(template['id'],{'expected_revision':2,'policy':policy})
 assert m.definitions.get(t['id'])==t

def test_contexts_are_role_mode_and_fact_snapshot_isolated(scene):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0];ident=template['id']
 planner=s.session(ident,{'agent':'planner'});executor=s.session(ident,{'agent':'executor'})
 assert planner['mode']=='discuss' and 'facts' not in planner
 facts=copy.deepcopy(executor['facts']);facts['location']='支持远程办公'
 s.test_context(ident,{'expected_revision':1,'facts':facts})
 assert s.db.get('scenario_session',executor['id'])['facts']['location']!='支持远程办公'
 assert s.session(ident,{'agent':'executor'})['facts']['location']=='支持远程办公'
 with pytest.raises(ValueError):s.session(ident,{'agent':'planner','mode':'simulate'})

def test_composition_preserves_task_local_parameters_and_jobs(scene):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0];prior=lab.db.get('agent_bundle',lab.bootstrap(t['id'])['version_id'])['bundle'];bundle=s.compose(template,t)
 for key in ('name','schedule','notifications','case_ids','recommend_filters'):
  assert bundle['config'][key]==prior['config'][key]
 assert bundle['sources']==prior['sources']
 case=overlay(copy.deepcopy(c),{'id':t['id'],'revision':t['revision'],'config':bundle['config']})
 assert case['desired_greeting']==c.get('desired_greeting','') # Generic template cannot replace a job greeting.

async def test_discussion_sends_no_production_job_and_keeps_roles_isolated(scene,monkeypatch):
 import cloakup.scenario_workspace as mod
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0];ident=template['id'];seen=[]
 class Engine:
  client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/v3'));last_trace={'runtime':'deepseek-harness'}
  async def json(self,**kw):
   seen.append(json.loads(kw['content']));return {'reply':'已调整通用推进方式','policy_patch':{'instructions':'先理解上下文，再自然推进'}}
 monkeypatch.setattr(mod,'get_harness',lambda *a:Engine());monkeypatch.setattr(mod,'LLM',lambda v:v)
 p=s.session(ident,{'agent':'planner'});x=s.session(ident,{'agent':'executor','mode':'discuss'})
 for text in ['请先回答问题','再索取简历']:
  result=await s.chat(ident,{'session_id':p['id'],'expected_revision':p['revision'],'message':text});p=result['session']
 await s.chat(ident,{'session_id':x['id'],'expected_revision':1,'message':'讨论沟通原则'})
 assert len(p['messages'])==4 and len(seen[-1]['dialogue'])==1
 assert c['job']['label'] not in json.dumps(seen,ensure_ascii=False)
 assert t['id'] not in json.dumps(seen) and m.definitions.get(t['id'])==t
 assert s.db.revision('scenario_session',p['id'])==p['revision']

async def test_multiturn_simulation_uses_only_synthetic_facts_and_no_writes(scene,monkeypatch):
 import cloakup.scenario_workspace as mod
 s,lab,m,t,c=scene;ident=s.catalog()['templates'][0]['id'];seen=[]
 async def simulate(bundle,fixtures,*args):
  seen.append((bundle,fixtures));return {'outcomes':[{'actual_route':'continue','action':'ask_resume','answer':{'text':'您好，方便发份简历吗？','sources':[]}}],'real_writes':0}
 monkeypatch.setattr(mod,'simulate_bundle',simulate)
 session=s.session(ident,{'agent':'executor'})
 for text in ['可以聊聊吗','好的']:
  result=await s.chat(ident,{'session_id':session['id'],'expected_revision':session['revision'],'message':text});session=result['session']
 assert len(seen[-1][1][0]['messages'])==3 and result['real_writes']==0
 assert seen[0][0]['sources'][0]['case_id']=='scenario-synthetic'
 assert c['id'] not in json.dumps(seen) and c['job']['label'] not in json.dumps(seen,ensure_ascii=False)
 assert s.db.revision('scenario_session',session['id'])==3
 assert m.definitions.get(t['id'])==t

async def test_apply_validates_before_publishing_and_does_not_start(scene,monkeypatch):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0]
 def test_start(tid,body):
  d=s.db.get('agent_draft',body['draft_id']);r=passing_test(lab,d);r['id']=uuid.uuid4().hex;s.db.put('agent_test',r['id'],r)
  lab.workers[r['id']]=asyncio.create_task(asyncio.sleep(0));return r
 monkeypatch.setattr(lab,'start_test',test_start)
 job=s.apply(template['id'],{'expected_revision':1,'task_ids':[t['id']]});await s.workers[job['id']]
 done=s.db.get('scenario_apply',job['id'])
 assert done['status']=='applied',done
 assert s.db.get('scenario_binding',t['id'])['template_id']==template['id']
 assert m.account_execution.members()[0]['status']=='paused'
 assert m.definitions.get(t['id'])['config']['schedule']==t['config']['schedule']

async def test_failed_validation_keeps_live_config(scene,monkeypatch):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0]
 def test_start(tid,body):
  r={'id':uuid.uuid4().hex,'status':'failed','result':{'outcomes':[]}};s.db.put('agent_test',r['id'],r);lab.workers[r['id']]=asyncio.create_task(asyncio.sleep(0));return r
 monkeypatch.setattr(lab,'start_test',test_start)
 job=s.apply(template['id'],{'expected_revision':1,'task_ids':[t['id']]});await s.workers[job['id']]
 assert s.db.get('scenario_apply',job['id'])['status']=='failed'
 assert m.definitions.get(t['id'])==t and not s.catalog()['bindings']

def test_running_and_incompatible_tasks_cannot_apply(scene):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0]
 m.account_execution.set_task(t['id'],[c['id']],'running')
 with pytest.raises(ValueError,match='暂停'):s.apply(template['id'],{'expected_revision':1,'task_ids':[t['id']]})
 assert not s.catalog()['applications']

async def test_multiple_jobs_in_one_task_keep_own_sources(scene,monkeypatch):
 # 一个网站 + 一个场景只有一个任务；多个岗位作为任务参数并存，各自事实不串。
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0]
 second=m.cases.create('https://www.zhipin.com/web/chat/job-list');m.cases.bind_job(second,{'title':'人才顾问','requirements':'负责人才管理。'})
 second['anonymous_job']={'role':'人才顾问','requirements':['负责人才管理。']};m.cases.set_autonomy(second,True,source='test');m.cases.save(second)
 t=m.definitions.save({'expected_revision':t['revision'],'config':{**copy.deepcopy(t['config']),'case_ids':[c['id'],second['id']]}},t['id'])
 def start(tid,body):
  d=s.db.get('agent_draft',body['draft_id']);r=passing_test(lab,d);r['id']=uuid.uuid4().hex;s.db.put('agent_test',r['id'],r);lab.workers[r['id']]=asyncio.create_task(asyncio.sleep(0));return r
 monkeypatch.setattr(lab,'start_test',start)
 job=s.apply(template['id'],{'expected_revision':1,'task_ids':[t['id']]});await s.workers[job['id']]
 assert s.db.get('scenario_apply',job['id'])['status']=='applied'
 release=s.db.get('agent_release',t['id']);bundle=s.db.get('agent_bundle',release['version_id'])['bundle']
 assert m.definitions.get(t['id'])['config']['case_ids']==[c['id'],second['id']]
 assert {src['case_id']:src['job']['label'] for src in bundle['sources']}=={c['id']:c['job']['label'],second['id']:second['job']['label']}
 assert len(s.catalog()['versions'])==1 and len(s.catalog()['bindings'])==1


def test_new_job_in_draft_is_covered_by_source_cas(scene):
 s,lab,m,t,c=scene
 second=m.cases.create('https://www.zhipin.com/web/chat/job-list');m.cases.bind_job(second,{'title':'人才顾问','requirements':'负责人才管理。'})
 second['anonymous_job']={'role':'人才顾问','requirements':['负责人才管理。']};m.cases.save(second)
 from cloakup.agent_workspace import source
 d=lab.draft(t['id'],{});bundle=copy.deepcopy(d['bundle']);bundle['config']['case_ids'].append(second['id']);bundle['sources'].append(source(second))
 d=lab.draft(t['id'],{'draft_id':d['id'],'expected_revision':d['revision'],'bundle':bundle})
 assert second['id'] in d['base_source_digests']

async def test_failed_validation_keeps_the_single_task_unchanged(scene,monkeypatch):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0]
 def start(tid,body):
  d=s.db.get('agent_draft',body['draft_id']);r=passing_test(lab,d);r['id']=uuid.uuid4().hex;r['status']='failed'
  s.db.put('agent_test',r['id'],r);lab.workers[r['id']]=asyncio.create_task(asyncio.sleep(0));return r
 monkeypatch.setattr(lab,'start_test',start)
 job=s.apply(template['id'],{'expected_revision':1,'task_ids':[t['id']]});await s.workers[job['id']]
 assert s.db.get('scenario_apply',job['id'])['status']=='failed'
 assert m.definitions.get(t['id'])==t and not s.catalog()['bindings']

def test_simulation_uses_job_owned_profile_not_task_greeting(scene):
 s,lab,m,t,c=scene;template=s.catalog()['templates'][0]
 bundle=s.synthetic_bundle(template,{'role':'测试研发岗位','duties':'研发分布式系统'})
 profile=bundle['sources'][0]['sourcing']
 assert profile['greeting']==template['policy']['greeting_template']
 assert profile['outreach_terms']==[] and profile['recommend_filters']==[]
 assert bundle['config']['greeting']==''
 from cloakup.agent_workspace import case_for
 assert '测试研发岗位' in case_for(bundle,'scenario-synthetic')['desired_greeting']
