from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from cloakup.executor_check import verify
from cloakup.observe import Element


def session():
 elements=[Element(i,tag,'#test'+str(i),label,20,20,{'tag':tag,'label':label}) for i,(tag,label) in enumerate([('input','执行器测试输入'),('button','确认执行器测试'),('button','重复测试按钮'),('button','重复测试按钮')])]
 obs=NS(title='Cloakup executor check',elements=elements)
 return NS(page=NS(url='data:text/html,test'),observe=AsyncMock(return_value=obs),type_text=AsyncMock(),click=AsyncMock())


async def test_executor_contract_requires_real_result_and_single_click(monkeypatch):
 s=session();read=AsyncMock(return_value={'value':'CLOAKUP_READY','count':1});monkeypatch.setattr('cloakup.executor_check.read_dom',read)
 result=await verify(s)
 assert result['status']=='passed' and result['business_writes']==0
 s.type_text.assert_awaited_once_with(0,'CLOAKUP_READY');s.click.assert_awaited_once_with(1)
 assert len(result['steps'])==6


@pytest.mark.parametrize('count',[0,2])
async def test_executor_contract_fails_for_no_effect_or_duplicate_effect(monkeypatch,count):
 s=session();monkeypatch.setattr('cloakup.executor_check.read_dom',AsyncMock(return_value={'value':'CLOAKUP_READY','count':count}))
 with pytest.raises(ValueError,match='结果回读'):await verify(s)
 s.click.assert_awaited_once()


async def test_executor_contract_refuses_business_pages():
 s=session();s.page.url='https://www.zhipin.com/web/chat/index'
 with pytest.raises(ValueError,match='隔离合成'):await verify(s)
 s.click.assert_not_awaited();s.type_text.assert_not_awaited()


async def test_native_action_timeout_keeps_partial_input_and_never_replays(monkeypatch):
 import asyncio
 from cloakup.browser import BrowserSession
 from cloakup.config import BrowserConfig,PacingConfig
 s=BrowserSession(BrowserConfig(),PacingConfig());s.page=NS(url='data:text/html,test');s._settle=AsyncMock()
 s.pacer=NS(before_action=AsyncMock(),mark_action=lambda:None)
 monkeypatch.setitem(__import__('cloakup.browser',fromlist=['ACTION_TIMEOUTS']).ACTION_TIMEOUTS,'type',.01)
 state={'calls':0,'draft':''}
 async def slow():
  state['calls']+=1;state['draft']='partial';await asyncio.sleep(1)
 with pytest.raises(TimeoutError):await s._act('type',slow)
 assert state=={'calls':1,'draft':'partial'} and s.last_action_timing['completed'] is False
 s._settle.assert_not_awaited()
