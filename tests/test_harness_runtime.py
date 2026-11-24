import json
from types import SimpleNamespace as NS
import pytest
from cloakup.harness_runtime import HarnessPlanner
from cloakup.prompts import planning_prompt,PLAN_CONTRACT
from cloakup.communication_policy import COMMUNICATION_POLICY


async def test_official_harness_turn_uses_existing_client_with_no_tools(tmp_path):
 seen=[]
 class Client:
  async def create(self,**kw):
   seen.append(kw)
   return NS(content=[NS(type='text',text='{"ok":true}')],stop_reason='end_turn',usage=NS(input_tokens=1,output_tokens=2))
 engine=HarnessPlanner(NS(client=Client()),tmp_path)
 result=await engine.json(system='只输出JSON，不使用工具。{{岗位名称}} 和 {{role}} 均为业务文字占位符。',content='返回ok=true',schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False})
 assert result=={'ok':True} and len(seen)==1 and 'tools' not in seen[0]
 assert '｛｛岗位名称｝｝' in json.dumps(seen[0]['system'],ensure_ascii=False)
 trace=engine.last_trace
 assert trace['runtime']=='deepseek-harness' and trace['finish_reason']=='completed' and trace['event_count']>0
 assert (tmp_path/'harness'/'calls'/trace['session_id']/'cloakup-trace.json').is_file()


def test_editable_prompt_is_composed_with_fixed_policy_and_contract():
 prompt=planning_prompt({'planning_prompt':'优先确认远程协作安排，再索取尚缺资料。'})
 assert COMMUNICATION_POLICY in prompt and PLAN_CONTRACT in prompt
 assert '优先确认远程协作安排' in prompt

async def test_harness_corrects_bad_json_in_same_native_session(tmp_path):
 seen=[]
 class Client:
  async def create(self,**kw):
   seen.append(kw)
   text='{"ok":' if len(seen)==1 else '{"ok":true}'
   return NS(content=[NS(type='text',text=text)],stop_reason='end_turn',usage=NS(input_tokens=1,output_tokens=2))
 engine=HarnessPlanner(NS(client=Client()),tmp_path)
 assert await engine.json(system='只输出JSON',content='返回ok',schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}})=={'ok':True}
 assert engine.last_trace['correction_rounds']==1 and engine.last_trace['gateway_requests']==2
 assert len(seen[1]['messages'])>len(seen[0]['messages'])


async def test_runtime_process_reused_but_logical_sessions_isolated(tmp_path):
 seen=[]
 class Client:
  async def create(self,**kw):
   seen.append(kw['messages']);return NS(content=[NS(type='text',text='{"ok":true}')],stop_reason='end_turn',usage=NS(input_tokens=1,output_tokens=2))
 engine=HarnessPlanner(NS(client=Client()),tmp_path,persistent=True)
 schema={'type':'object','required':['ok'],'properties':{'ok':{'const':True}}}
 try:
  await engine.json(system='JSON only',content='FIRST_TASK_ONLY',schema=schema);first=engine.last_trace.copy()
  await engine.json(system='JSON only',content='SECOND_TASK_ONLY',schema=schema);second=engine.last_trace.copy()
  assert second['runtime_reused'] and second['runtime_starts']==1
  assert first['session_id']!=second['session_id'] and first['runtime_home']==second['runtime_home']
  assert 'FIRST_TASK_ONLY' not in json.dumps(seen[-1])
 finally:await engine.aclose()
