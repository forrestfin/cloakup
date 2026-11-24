import json
from types import SimpleNamespace as NS
import pytest
from cloakup.task_planning import generate_boss_plan,planning_signature
from cloakup.sourcing import SourcingError


async def test_plan_only_uses_approved_facts_and_records_task_ownership():
 seen=[]
 class Model:
  client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))
  async def json(self,**kw):
   seen.append(kw['content']);return {'steps':['核验登录与岗位入口','读取收件并按事实回复'],'success_criteria':['取得一份简历及联系方式'],'risks':['登录失效时交管理员处理']}
 d={'id':'task','revision':3,'config':{'target_resumes':1}}
 c={'id':'case','config_revision':5,'anonymous_job':{'role':'招聘顾问','requirements':['远程协作。']},'job':{'snapshot':{'requirements':'某某公司的原始JD'}},'candidates':{'private-name':{'phone':'13812345678'}}}
 result=await generate_boss_plan(Model(),d,[c])
 assert '不设累计数量上限' in result['plan']['success_criteria'][0]
 assert '电话和微信非必需' in result['plan']['success_criteria'][0]
 assert result['goal_enforced']
 assert result['task_id']=='task' and result['task_revision']==3 and result['verification']=='pending'
 assert 'private-name' not in seen[0] and '13812345678' not in seen[0] and '原始JD' not in seen[0]
 signature=result['signature'];c['config_revision']=6
 assert signature!=planning_signature(d,[c])


async def test_plan_requires_confirmed_facts_before_model_call():
 class Model:
  client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))
  async def json(self,**kw):raise AssertionError('must not call model')
 with pytest.raises(SourcingError,match='脱敏岗位事实'):
  await generate_boss_plan(Model(),{'config':{'target_resumes':1}},[{}])


def test_task_prompt_is_editable_and_versioned(tmp_path):
 from cloakup.storage import Database
 from cloakup.platform import TaskStore
 from cloakup.prompts import DEFAULT_PLANNING_PROMPT
 store=TaskStore(Database(tmp_path))
 t=store.save({'config':{'name':'寻访','website':'https://www.zhipin.com/','scenario':'候选人沟通','goal':'取得简历和联系方式','engine':'boss_sourcing','sourcing_mode':'quota','case_ids':['case']}})
 assert t['config']['planning_prompt']==DEFAULT_PLANNING_PROMPT
 cfg=dict(t['config']);cfg['planning_prompt']='先阅读已确认的远程安排，再规划资料获取。'
 new=store.save({'expected_revision':t['revision'],'config':cfg},t['id'])
 assert new['revision']==2 and new['config']['planning_prompt']==cfg['planning_prompt']
 assert planning_signature(t,[])!=planning_signature(new,[])
 cfg['planning_prompt']='联系候选人13812345678'
 with pytest.raises(ValueError,match='提示词'):
  store.save({'expected_revision':2,'config':cfg},t['id'])

async def test_plan_boundary_preserves_job_settings_locally_without_disclosing_raw_values():
 wire=[]
 class Model:
  client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))
  async def json(self,**kw):
   wire.append(kw);return {'steps':['加载当前岗位参数再寻访'],'success_criteria':['取得简历'],'risks':['不得串岗位']}
 d={'id':'task','revision':1,'config':{'execution':{'outreach_terms':['不应传送的共用词']}}}
 c={'id':'job','job':{'snapshot':{}},'anonymous_job':{'role':'示例岗位','requirements':['负责研发。']},'job_sourcing':{'greeting':'本机专属首聊','outreach_terms':['本机专属关键词'],'recommend_filters':['本科']}}
 await generate_boss_plan(Model(),d,[c]);data=json.loads(wire[0]['content'])
 assert 'outreach_terms' not in data['task_execution'] and '首聊' in data['job_order']
 assert all(text not in wire[0]['content'] for text in ['不应传送的共用词','本机专属首聊','本机专属关键词'])
 assert '{{岗位首聊}}' in wire[0]['system']
 original=planning_signature(d,[c]);c['job_sourcing']['greeting']='新首聊'
 assert planning_signature(d,[c])!=original
