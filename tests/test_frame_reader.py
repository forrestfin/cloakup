from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import asyncio
import pytest
from cloakup.dom_reader import read_frame_dom

async def test_frame_read_binds_unique_document_and_detaches():
 url='https://www.zhipin.com/web/frame/info/set/greeting'
 cdp=NS(send=AsyncMock(side_effect=[{'frameTree':{'frame':{'id':'main','url':'parent'},'childFrames':[{'frame':{'id':'child','url':url}}]}},{'executionContextId':7},{'result':{'value':{'ready':True}}}]),detach=AsyncMock())
 page=NS(is_closed=lambda:False,context=NS(new_cdp_session=AsyncMock(return_value=cdp)))
 assert await read_frame_dom(page,NS(url=url),'document.readyState')=={'ready':True}
 args=cdp.send.await_args_list
 assert args[1].args[1]['frameId']=='child' and args[2].args[1]['contextId']==7
 assert args[2].args[1]['returnByValue'] and not args[2].args[1]['awaitPromise']
 cdp.detach.assert_awaited_once()

async def test_duplicate_frame_url_is_not_guessed():
 cdp=NS(send=AsyncMock(return_value={'frameTree':{'frame':{'id':'one','url':'same'},'childFrames':[{'frame':{'id':'two','url':'same'}}]}}),detach=AsyncMock())
 page=NS(is_closed=lambda:False,context=NS(new_cdp_session=AsyncMock(return_value=cdp)))
 with pytest.raises(RuntimeError,match='frame_not_unique'):await read_frame_dom(page,NS(url='same'),'1')
 assert cdp.send.await_count==1;cdp.detach.assert_awaited_once()

async def test_stalled_frame_channel_is_bounded_and_detached():
 async def wait(*a,**kw):await asyncio.Event().wait()
 cdp=NS(send=wait,detach=AsyncMock());page=NS(is_closed=lambda:False,context=NS(new_cdp_session=AsyncMock(return_value=cdp)))
 with pytest.raises(asyncio.TimeoutError):await read_frame_dom(page,NS(url='same'),'1',timeout=.01)
 cdp.detach.assert_awaited_once()
