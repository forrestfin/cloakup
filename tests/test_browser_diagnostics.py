from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cloakup.browser_diagnostics import inspect


async def test_diagnostics_are_read_only_and_strip_frame_identifiers():
 cdp=NS(send=AsyncMock(side_effect=[{'frameTree':{'frame':{'id':'PRIVATE','url':'PRIVATE'}}},{'result':{'value':{'ready':'complete','rows':7,'editors':0}}}]),detach=AsyncMock())
 world=NS(evaluate=AsyncMock(return_value={'ready':'complete','rows':7}),invalidate=lambda:None)
 page=NS(url='https://www.zhipin.com/web/chat/index',_stealth_world=world)
 session=NS(context=NS(pages=[page],new_cdp_session=AsyncMock(return_value=cdp)))
 manager=NS(definitions=NS(get=lambda _: {'config':{'engine':'boss_sourcing'}}),boss_session=session,browser_alive=lambda _:True)
 result=await inspect(manager,'task')
 assert all(p['ok'] for p in result['probes'])
 assert 'PRIVATE' not in str(result)
 assert [c.args[0] for c in cdp.send.call_args_list]==['Page.getFrameTree','Runtime.evaluate']
 cdp.detach.assert_awaited_once()


async def test_diagnostics_recreate_only_failed_cloak_world():
 calls=[]
 world=NS(evaluate=AsyncMock(side_effect=[1,{'ready':'complete'},{'header_found':False},TimeoutError(),{'ready':'complete'}]),invalidate=lambda:calls.append('invalidate'))
 page=NS(url='https://www.zhipin.com/web/chat/index',_stealth_world=world)
 session=NS(context=NS(pages=[page],new_cdp_session=AsyncMock(side_effect=TimeoutError())))
 manager=NS(definitions=NS(get=lambda _: {'config':{'engine':'boss_sourcing'}}),boss_session=session,browser_alive=lambda _:True)
 result=await inspect(manager,'task')
 assert result['probes'][-1]['name']=='cloak_world_recreated_dom' and result['probes'][-1]['ok']
 assert calls==['invalidate']


async def test_ambiguous_browser_stays_untouched():
 page=NS(url='https://www.zhipin.com/web/chat/index')
 context=NS(pages=[page,page],new_cdp_session=AsyncMock())
 manager=NS(definitions=NS(get=lambda _: {'config':{'engine':'boss_sourcing'}}),boss_session=NS(context=context),browser_alive=lambda _:True)
 result=await inspect(manager,'task')
 assert result['reason']=='chat_page_ambiguous'
 context.new_cdp_session.assert_not_awaited()


async def test_selection_probe_uses_local_target_and_has_no_send_capability(monkeypatch):
 from cloakup.browser_diagnostics import verify_target
 from cloakup import boss_chat
 binding={'chat_id':'12-0','receiver':'候选人','job_title':'岗位'}
 page=NS(url=boss_chat.CHAT_URL)
 pacer=NS(before_action=AsyncMock(),mark_action=lambda:None)
 session=NS(context=NS(pages=[page]),pacer=pacer)
 manager=NS(definitions=NS(get=lambda _:{'config':{'engine':'boss_sourcing','case_ids':['case']}}),
  account_execution=NS(members=lambda:[{'status':'paused'}]),busy=lambda:{'account_runner':True},
  boss_session=session,browser_alive=lambda _:True,cases=NS(load=lambda _:{'candidates':{'person':{'conversation':binding}}}),
  jobs={'run':{'selection_recovery':{'case':{'failures':['12-0'],'updated_at':1}}}})
 async def read(page,expression):
  if expression==boss_chat.ROWS_JS:return [{'chat_id':'12-0','text':'候选人 岗位'}]
  return {'has_draft':False,'login':False,'challenge':False}
 async def select(adapter,chat_id):
  assert chat_id=='12-0' and not hasattr(adapter,'send_once') and not hasattr(adapter,'type_text')
  await adapter._act('click',AsyncMock())
 monkeypatch.setattr(boss_chat,'_bound_read',read)
 monkeypatch.setattr(boss_chat,'select_chat_row',select)
 monkeypatch.setattr(boss_chat,'read_conversation',AsyncMock(side_effect=[{'valid':False},{'valid':True,'messages':[{'text':'PRIVATE'}],'editor_count':1}]))
 result=await verify_target(manager,'task')
 assert result['verified'] and result['sent_messages']==0
 assert 'PRIVATE' not in str(result) and '候选人' not in str(result)
 pacer.before_action.assert_awaited_once_with('click')


async def test_selection_probe_rejects_running_tasks():
 import pytest
 from cloakup.browser_diagnostics import verify_target
 manager=NS(account_execution=NS(members=lambda:[{'status':'running'}]))
 with pytest.raises(ValueError,match='暂停'):await verify_target(manager,'task')


async def test_selection_probe_preserves_draft_and_does_not_clear_incident(monkeypatch):
 from cloakup.browser_diagnostics import verify_target
 from cloakup import boss_chat
 binding={'chat_id':'12-0','receiver':'候选人','job_title':'岗位'}
 owner={'account_runner':True,'selection_recovery':{'case':{'status':'blocked','failures':['12-0'],'updated_at':1}}}
 manager=NS(definitions=NS(get=lambda _:{'config':{'engine':'boss_sourcing','case_ids':['case']}}),
  account_execution=NS(members=lambda:[{'status':'paused'}]),busy=lambda:owner,
  boss_session=NS(context=NS(pages=[NS(url=boss_chat.CHAT_URL)])),browser_alive=lambda _:True,
  cases=NS(load=lambda _:{'candidates':{'person':{'conversation':binding}}}),jobs={'run':owner})
 async def read(page,expression):
  if expression==boss_chat.ROWS_JS:return [{'chat_id':'12-0','text':'候选人 岗位'}]
  return {'has_draft':True}
 select=AsyncMock();snapshot=AsyncMock()
 monkeypatch.setattr(boss_chat,'_bound_read',read);monkeypatch.setattr(boss_chat,'select_chat_row',select);monkeypatch.setattr(boss_chat,'read_conversation',snapshot)
 result=await verify_target(manager,'task')
 assert result['reason']=='draft_present' and not result['verified']
 select.assert_not_awaited();snapshot.assert_not_awaited()
 assert owner['selection_recovery']['case']['status']=='blocked'


async def test_environment_check_and_page_diagnostic_are_exclusive():
    import pytest
    manager=NS(preflight_task=NS(done=lambda:False))
    with pytest.raises(ValueError,match='环境与执行器核验'):await inspect(manager,'task')
