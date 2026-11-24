"""Document-scoped isolated DOM reads; never retries a browser write."""
import asyncio
import time


async def read_dom(page,expression,*,validate=None,timeout=5):
    def check():
        if validate:validate()
        elif getattr(page,'is_closed',lambda:False)():raise RuntimeError('page_closed')
    check();world=page._stealth_world;key=(id(world),page.url)
    if hasattr(page,'on') and not getattr(page,'_cloakup_read_listener',False):
        def navigated(frame):
            if getattr(frame,'parent_frame',None) is None:page._cloakup_read_ready=None
        page.on('framenavigated',navigated);page._cloakup_read_listener=True
    async def warm():
        await asyncio.wait_for(world.evaluate('1'),timeout=12)
        check();page._cloakup_read_ready=(id(world),page.url)
    try:
        if getattr(page,'_cloakup_read_ready',None)!=key:await warm()
        check();return await asyncio.wait_for(world.evaluate(expression),timeout=timeout)
    except asyncio.TimeoutError:
        check()
        if not hasattr(world,'invalidate'):raise
        world.invalidate();page._cloakup_read_ready=None
        await warm();result=await asyncio.wait_for(world.evaluate(expression),timeout=timeout)
        page._cloakup_read_channel_recovered_at=time.time()
        return result

async def read_frame_dom(page,frame,expression,timeout=6):
    """Fresh document-bound CDP read for an observed iframe, no mutations/tools."""
    cdp=None;expected_url=frame.url
    async def read():
        nonlocal cdp
        if frame.url!=expected_url or page.is_closed():raise RuntimeError('frame_changed')
        cdp=await page.context.new_cdp_session(page)
        tree=await cdp.send('Page.getFrameTree')
        def find(node):
            out=[node['frame']['id']] if node['frame'].get('url')==expected_url else []
            for child in node.get('childFrames',[]):out+=find(child)
            return out
        ids=find(tree['frameTree'])
        if len(ids)!=1:raise RuntimeError('frame_not_unique')
        world=await cdp.send('Page.createIsolatedWorld',{'frameId':ids[0],'worldName':'cloakup-readonly-frame'})
        result=await cdp.send('Runtime.evaluate',{'expression':expression,'contextId':world['executionContextId'],'returnByValue':True,'awaitPromise':False,'timeout':int(timeout*1000)})
        if frame.url!=expected_url or result.get('exceptionDetails'):raise RuntimeError('frame_read_unverified')
        return result.get('result',{}).get('value')
    try:return await asyncio.wait_for(read(),timeout)
    finally:
        if cdp:
            try:await asyncio.wait_for(cdp.detach(),2)
            except Exception:pass
