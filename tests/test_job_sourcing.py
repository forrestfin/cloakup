import copy
import pytest
from test_agent_workspace import setup,passing_test
from cloakup.job_sourcing import checked,resolve
from cloakup.agent_workspace import case_for,source
from cloakup.task_policy import overlay
from cloakup.scenario_workspace import ScenarioWorkspace

@pytest.fixture
def jobs(setup):
 lab,m,t,first=setup
 first['job_sourcing']={'greeting':'您好，想交流招聘交付岗位。','outreach_terms':['招聘','人才交付'],'recommend_filters':['本科','5-10年']};m.cases.save(first)
 second=m.cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=engineering')
 m.cases.bind_job(second,{'title':'研发工程师','requirements':'负责分布式系统研发。'})
 second['anonymous_job']={'role':'研发工程师','requirements':['负责分布式系统研发。']};m.cases.set_autonomy(second,True,source='test')
 second['job_sourcing']={'greeting':'您好，想交流分布式研发岗位。','outreach_terms':['分布式','后端'],'recommend_filters':['硕士','10年以上']};m.cases.save(second)
 cfg=copy.deepcopy(t['config']);cfg['case_ids']=[first['id'],second['id']];cfg['greeting']='遗留任务话术';cfg['execution']['outreach_terms']=['旧的共用词'];cfg['recommend_filters']=['博士']
 t=m.definitions.save({'expected_revision':t['revision'],'config':cfg},t['id'])
 return lab,m,t,first,second

def test_task_overlay_never_overwrites_job_business_settings(jobs):
 lab,m,t,a,b=jobs
 for case in (a,b):
  live=m.cases.load(case['id']);own=case['job_sourcing']
  assert live['desired_greeting']==own['greeting'] and live['execution']['outreach_terms']==own['outreach_terms'] and live['recommend_filters']==own['recommend_filters']
  assert live['job']['snapshot']['requirements']==case['job']['snapshot']['requirements']
 cfg=copy.deepcopy(t['config']);cfg['execution']['daily_new_limit']=7;cfg['greeting']='另一个遗留任务值'
 t=m.definitions.save({'expected_revision':t['revision'],'config':cfg},t['id'])
 assert m.cases.load(a['id'])['desired_greeting']==a['job_sourcing']['greeting']
 assert m.cases.load(b['id'])['execution']['daily_new_limit']==7
 assert m.cases.load(b['id'])['execution']['outreach_terms']==['分布式','后端']

def test_job_profiles_and_jd_enter_each_execution_context_separately(jobs):
 lab,m,t,a,b=jobs;d=lab.draft(t['id'],{})
 for case in (a,b):
  runtime=case_for(d['bundle'],case['id'])
  assert runtime['job_sourcing']==case['job_sourcing']
  assert runtime['job']['snapshot']['requirements']==case['job']['snapshot']['requirements']
  assert runtime['desired_greeting']==case['job_sourcing']['greeting']
 assert case_for(d['bundle'],a['id'])['execution']['outreach_terms']!=case_for(d['bundle'],b['id'])['execution']['outreach_terms']

def test_publishing_one_job_changes_no_other_job(jobs):
 lab,m,t,a,b=jobs;d=lab.draft(t['id'],{});bundle=copy.deepcopy(d['bundle']);before_b=copy.deepcopy(next(s for s in bundle['sources'] if s['case_id']==b['id']))
 one=next(s for s in bundle['sources'] if s['case_id']==a['id']);one['sourcing']['greeting']='您好，交流高端人才交付的机会。';one['sourcing']['outreach_terms']=['高端招聘'];one['sourcing']['recommend_filters']=['本科']
 d=lab.draft(t['id'],{'draft_id':d['id'],'expected_revision':d['revision'],'bundle':bundle});test=passing_test(lab,d)
 lab.publish(t['id'],{'draft_id':d['id'],'expected_revision':d['revision'],'test_id':test['id']})
 assert m.cases.load(a['id'])['desired_greeting']==one['sourcing']['greeting']
 assert m.cases.load(b['id'])['job_sourcing']==before_b['sourcing']
 assert m.cases.load(b['id'])['job']['snapshot']==before_b['job']['snapshot']
 assert m.definitions.get(t['id'])['config']==t['config']

def test_generic_template_application_preserves_every_job_profile(jobs):
 lab,m,t,a,b=jobs;s=ScenarioWorkspace(m);template=s.catalog()['templates'][0];template['policy']['greeting_template']='新的通用参考，不是岗位正文';bundle=s.compose(template,t)
 for case in (a,b):assert next(v for v in bundle['sources'] if v['case_id']==case['id'])['sourcing']==case['job_sourcing']
 assert bundle['config']['greeting']==t['config']['greeting']

def test_old_bundle_sources_upgrade_without_losing_effective_job_values(jobs):
 lab,m,t,a,b=jobs;d=lab.draft(t['id'],{});old=copy.deepcopy(d['bundle'])
 for value in old['sources']:value.pop('sourcing')
 normalized=lab.normalize(t['id'],old)
 assert normalized['sources'][0]['sourcing']==a['job_sourcing'] and normalized['sources'][1]['sourcing']==b['job_sourcing']

def test_profile_validation_does_not_accept_unknown_fields_or_filters():
 case={'job':{'label':'研发岗位'}};p={'greeting':'您好，{{岗位名称}}','outreach_terms':['研发','研发'],'recommend_filters':['本科']}
 assert checked(p,case)['outreach_terms']==['研发']
 for bad in ({**p,'shared':True},{**p,'recommend_filters':['未知条件']},{**p,'outreach_terms':[1]}):
  with pytest.raises(Exception):checked(bad,case)

@pytest.mark.parametrize('agent',['planner','executor'])
async def test_agent_job_patch_is_scoped_partial_and_only_changes_draft(jobs,monkeypatch,agent):
 import json
 from types import SimpleNamespace as NS
 import cloakup.agent_workspace as aw
 lab,m,t,a,b=jobs;d=lab.draft(t['id'],{});session=lab.session(t['id'],{'draft_id':d['id'],'agent':agent})
 seen=[]
 class Engine:
  client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'));last_trace={}
  async def json(self,**kw):
   data=json.loads(kw['content']);seen.append(data)
   assert 'greeting' not in data['task_config'] and 'recommend_filters' not in data['task_config']
   assert 'outreach_terms' not in data['task_config']['execution']
   assert len(data['facts'])==2 and all(v['job_configuration']['scope']=='per_job' for v in data['facts'])
   assert a['job_sourcing']['greeting'] not in kw['content']
   return {'reply':'仅调整指定岗位首聊，尚未发布。','config_patch':{},'source_patches':[{'case_id':a['id'],'sourcing':{'greeting':'您好，欢迎了解人才交付机会。'}}],'plan':None,'tool_request':None}
 monkeypatch.setattr(aw,'get_harness',lambda *args:Engine())
 await lab.chat(t['id'],session['id'],{'expected_revision':1,'message':'仅调整指定岗位首聊，不改其他字段'})
 updated=m.db.get('agent_draft',d['id'])['bundle'];one=next(s for s in updated['sources'] if s['case_id']==a['id']);two=next(s for s in updated['sources'] if s['case_id']==b['id'])
 assert one['sourcing']=={**a['job_sourcing'],'greeting':'您好，欢迎了解人才交付机会。'}
 assert two['sourcing']==b['job_sourcing'] and updated['config']==d['bundle']['config']
 assert m.cases.load(a['id'])['job_sourcing']==a['job_sourcing'] and len(seen)==1

async def test_agent_cannot_reintroduce_task_level_job_keywords(jobs,monkeypatch):
 from types import SimpleNamespace as NS
 import cloakup.agent_workspace as aw
 lab,m,t,a,b=jobs;d=lab.draft(t['id'],{});session=lab.session(t['id'],{'draft_id':d['id'],'agent':'planner'})
 class Engine:
  client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'));last_trace={}
  async def json(self,**kw):return {'reply':'修改任务关键词','config_patch':{'execution':{'outreach_terms':['错误共用词']}},'source_patches':[],'plan':None,'tool_request':None}
 monkeypatch.setattr(aw,'get_harness',lambda *args:Engine())
 with pytest.raises(ValueError,match='职业关键词属于岗位'):await lab.chat(t['id'],session['id'],{'expected_revision':1,'message':'解释岗位配置'})
 assert m.db.get('agent_draft',d['id'])==d
