import copy,json
from types import SimpleNamespace as NS
import pytest
from cloakup.agent import WebAgent
from cloakup.config import AgentConfig
from cloakup.web_harness import WebHarness
from test_agent import FakeBrowser,FakeClient,RAW

PLAN={'steps':['Read page and act based on feedback'],'success_criteria':['Observed result'],'risks':['No unverified claims']}
class Engine:
 def __init__(self,actions):self.actions=list(actions);self.calls=[];self.last_trace={'runtime':'deepseek-harness','session_id':'native-test','event_count':1}
 async def json(self,**kw):
  self.calls.append(json.loads(kw['content']))
  return self.actions.pop(0)

def make(actions,**cfg):
 engine=Engine(actions);client=FakeClient([]);driver=WebHarness(client,'.',engine=engine)
 agent=WebAgent(AgentConfig(trace_dir=None,**cfg),client=client,harness=driver)
 browser=FakeBrowser();agent.browser=agent.executor.browser=browser
 return agent,browser,engine,client

def action(tool,args,evidence=None):return {'reason':'Based on observed page','tool':tool,'arguments':args,'evidence':evidence or []}

async def test_harness_plans_then_decides_with_tool_feedback_and_masks_secrets():
 a,b,e,c=make([PLAN,action('type_text',{'element_id':0,'text':'{{secret:PW}}'}),action('done',{'success':True,'summary':'observed','result':{'title':'Welcome'}},['Welcome'])],secrets={'PW':'hunter2'})
 r=await a.run('Read page',start_url='https://shop.test/',output_schema={'type':'object','required':['title'],'properties':{'title':{'type':'string'}}})
 assert r.success and len(e.calls)==3 and ('type',0,'hunter2',False) in b.calls
 assert 'hunter2' not in json.dumps(e.calls) and not c.requests
 assert 'tool_result' in json.dumps(e.calls[-1]) and e.calls[-1]['reference_plan']==PLAN
 assert all('image' not in json.dumps(x) for x in e.calls)

async def test_invalid_tool_never_reaches_executor_and_has_no_direct_api_fallback():
 a,b,e,c=make([PLAN,action('shell',{'command':'rm'})])
 r=await a.run('read')
 assert not r.success and not b.calls and not c.requests

async def test_completion_needs_observed_evidence_and_schema():
 a,b,e,c=make([PLAN,action('done',{'success':True,'summary':'invented','result':{'title':'fake'}},['not present in observation']),action('done',{'success':False,'summary':'cannot verify'})])
 r=await a.run('read')
 assert not r.success and len(e.calls)==3
 assert '没有可核实' in json.dumps(e.calls[-1],ensure_ascii=False)

async def test_changed_observation_invalidates_indexed_action():
 a,b,e,c=make([PLAN,action('click',{'element_id':2}),action('done',{'success':False,'summary':'changed'})])
 observe=b.observe;index=0
 async def changing(screenshot=True):
  nonlocal index
  index+=1;obs=await observe(screenshot)
  if index>=2:
   obs.raw=copy.deepcopy(obs.raw);obs.raw['title']='Updated page'
  return obs
 b.observe=changing
 r=await a.run('read')
 assert not r.success and not b.calls
 assert '旧决策未执行' in json.dumps(e.calls[-1],ensure_ascii=False)

async def test_unknown_submit_result_stops_without_repeating(tmp_path):
 a,b,e,c=make([PLAN,action('type_text',{'element_id':0,'text':'query','submit':True})]);a.cfg.trace_dir=str(tmp_path)
 calls=[]
 async def fail(*args,**kw):calls.append(1);raise TimeoutError('ambiguous submit')
 b.type_text=fail
 r=await a.run('read')
 assert not r.success and len(calls)==1 and len(e.calls)==2
 trace=next(tmp_path.rglob('trace.jsonl')).read_text();assert 'action_claim' in trace and 'receipt_unknown' in trace

@pytest.mark.parametrize('url',['https://www.zhipin.com/web/chat','https://outside.test/'])
async def test_private_or_outside_site_is_not_sent_to_harness(url):
 a,b,e,c=make([],allowed_domains=['shop.test']);original=b.observe
 async def outside(*args):
  obs=await original();obs.url=url;return obs
 b.observe=outside
 with pytest.raises(ValueError):await a.run('read')
 assert not e.calls


def test_system_prompt_does_not_conflict_with_native_template_variables():
 from cloakup.agent import SYSTEM_PROMPT
 assert '{{secret:NAME}}' not in SYSTEM_PROMPT
 assert 'secret:NAME' in SYSTEM_PROMPT


async def test_known_search_routine_is_a_single_business_capability():
 a,b,e,c=make([PLAN,action('search_and_read',{'search_input_id':0,'text':'Poetry'}),action('done',{'success':True,'summary':'read'},['Welcome'])])
 result=await a.run('Search Poetry and read the result')
 assert result.success and ('type',0,'Poetry',True) in b.calls
 assert len(e.calls)==3


async def test_browser_write_without_business_receipt_stays_unknown():
 from cloakup.storage import Database
 from cloakup.action_gateway import ActionLedger
 a,b,e,c=make([PLAN,action('click',{'element_id':1})])
 async def approve(*a):return True
 a.guard._confirm=approve
 result=await a.run('Perform the authorized action')
 assert not result.success
 rows=a.operation_ledger.list();assert len(rows)==1 and rows[0]['state']=='unknown'
 assert [h['state'] for h in rows[0]['payload']['history']]==['prepared','claimed','executing','unknown']
