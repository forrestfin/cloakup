from types import SimpleNamespace as NS
import pytest

from cloakup import boss_chat as bc
from cloakup.sourcing import SourcingError


async def test_chat_requires_actual_chat_menu_before_dom_access():
 class World:
  async def evaluate(self,*a):raise AssertionError('must not read wrong page')
 for url in ['https://www.zhipin.com/web/user/','https://attacker.test/web/chat/index','https://www.zhipin.com/web/chat/recommend']:
  with pytest.raises(SourcingError):await bc.evaluate(NS(page=NS(url=url,_stealth_world=World())),'x')


async def test_unknown_or_empty_history_fails_closed(monkeypatch):
 async def evaluate(*a):return {'valid':True,'messages':[]}
 monkeypatch.setattr(bc,'evaluate',evaluate)
 with pytest.raises(SourcingError):await bc.read_conversation(object(),'12-0','甲','岗位')


async def test_message_dedup_preserves_identical_messages_with_distinct_ids(monkeypatch):
 async def evaluate(*a):return {'valid':True,'messages':[
  {'platform_id':'m1','direction':'in','text':'好的','timestamp':'','attachment':False},
  {'platform_id':'m2','direction':'in','text':'好的','timestamp':'','attachment':False}]}
 monkeypatch.setattr(bc,'evaluate',evaluate)
 snap=await bc.read_conversation(object(),'12-0','甲','岗位')
 assert [m['external_id'] for m in snap['messages']]==['m1','m2']


async def test_intervening_reply_prevents_typing_and_send(monkeypatch):
 actions=[]
 async def read(*a,**kw):return {'fingerprint':'new','editor_count':1}
 monkeypatch.setattr(bc,'read_conversation',read)
 class Session:
  async def click(self,*a):actions.append('click')
  async def type_text(self,*a,**kw):actions.append('type')
 with pytest.raises(bc.SendAborted) as caught:
  await bc.send_once(Session(),{'chat_id':'12-0','receiver':'甲','job_title':'岗位'},'正文',{'fingerprint':'old'})
 assert not actions and caught.value.reason=='changed_before_typing' and caught.value.typed is False


@pytest.mark.asyncio
async def test_read_uses_existing_verified_chat_tab_after_focus_changes():
    from types import SimpleNamespace as NS
    from cloakup import boss_chat
    calls=[]
    async def evaluate(expression):calls.append(expression);return ['actual rows']
    chat=NS(url='https://www.zhipin.com/web/chat/index',is_closed=lambda:False,_stealth_world=NS(evaluate=evaluate))
    blank=NS(url='about:blank',is_closed=lambda:False)
    async def observe(*a,**kw):return NS(url=chat.url,elements=[],raw={})
    session=NS(page=blank,context=NS(pages=[blank,chat]),observe=observe)
    assert await boss_chat.evaluate(session,'read only')==['actual rows']
    assert session.page is chat and calls==['1','read only']


@pytest.mark.parametrize('after',['target_row_missing','header_name_unverified'])
async def test_post_send_receipt_scrolls_list_back_when_row_left_virtual_list(monkeypatch,after):
 # 发送后会话跳到顶部被移出 DOM：滚回顶部再核对回执；其他身份问题仍按原方式抛出。
 before={'fingerprint':'f','messages':[],'editor_count':1}
 reads=iter([{**before,'valid':True,'messages':[]},{**before,'valid':True,'editor_value':'正文'},
             {'valid':False,'reason_code':after},
             {'valid':True,'fingerprint':'g','messages':[{'external_id':'m9','direction':'out','text':'正文','status':'delivered'}]}])
 async def read(session,chat_id,receiver,job_title,**kw):
  data=next(reads)
  if kw.get('required',True) and not data.get('valid'):raise bc.ChatSelectionUnavailable('未核实',stage='verify_identity',reason=data['reason_code'])
  return data
 scrolls=[];clicks=[]
 async def evaluate(session,expr):scrolls.append(expr)
 async def observe(*a,**kw):return NS(elements=[NS(label='发送',raw={},index=3,tag='button')])
 async def sleep(_):pass
 class Editor:
  async def count(self):return 1
  async def inner_text(self):return ''
  async def fill(self,*a,**kw):pass
 async def act(kind,fn):await fn()
 async def click(index):clicks.append(index)
 monkeypatch.setattr(bc,'read_conversation',read);monkeypatch.setattr(bc,'evaluate',evaluate)
 monkeypatch.setattr(bc,'_observation',observe);monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 session=NS(page=NS(locator=lambda s:Editor()),_act=act,click=click)
 binding={'chat_id':'12-0','receiver':'甲','job_title':'岗位'}
 if after=='target_row_missing':
  receipt=await bc.send_once(session,binding,'正文',before)
  assert receipt['message_id']=='m9' and clicks==[3] and len(scrolls)==1 and '"top"' in scrolls[0]
 else:
  reads=iter([{**before,'valid':True,'messages':[]},{**before,'valid':True,'editor_value':'正文'},{'valid':False,'reason_code':after},{'valid':False,'reason_code':after}])
  with pytest.raises(bc.ChatSelectionUnavailable):await bc.send_once(session,binding,'正文',before)
  assert clicks==[3] and not scrolls


@pytest.mark.parametrize('visible',[True,False])
async def test_select_row_centers_and_never_forces_obscured_click(monkeypatch,visible):
 calls=[]
 class Locator:
  async def count(self):return 1
  async def click(self,**kw):calls.append(kw)
 async def evaluate(page,expr):
  if 'elementFromPoint' in expr:return {'x':120,'y':40} if visible else None
  assert 'block:"center"' in expr
 async def act(kind,fn):return await fn()
 monkeypatch.setattr(bc,'_bound_read',evaluate)
 session=NS(page=NS(url=bc.CHAT_URL,locator=lambda selector:Locator()),_act=act)
 if visible:
  await bc.select_chat_row(session,'12-0')
  assert calls==[{'position':{'x':120,'y':40},'timeout':5000}]
 else:
  with pytest.raises(SourcingError,match='被遮挡'):await bc.select_chat_row(session,'12-0')
  assert not calls


@pytest.mark.parametrize('counts,found',[([0,0,1],True),([0]+[0]*bc.LIST_REVEAL_STEPS,False),([2],False)])
async def test_virtualized_row_is_revealed_by_scrolling_list_only(monkeypatch,counts,found):
 # 虚拟列表把目标行移出 DOM 时只滚动列表查找；重复行仍直接拒绝。
 seen=list(counts);counts=iter(counts);clicked=[];scrolls=[]
 class Locator:
  async def count(self):return next(counts)
  async def click(self,**kw):clicked.append(kw)
 async def evaluate(page,expr):
  if expr.startswith('((mode)'):
   scrolls.append('top' if '"top"' in expr else 'next');return {'moved':True,'end':False}
  if 'elementFromPoint' in expr:return {'x':10,'y':10}
 async def act(kind,fn):return await fn()
 async def sleep(_):pass
 monkeypatch.setattr(bc,'_bound_read',evaluate);monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 session=NS(page=NS(url=bc.CHAT_URL,locator=lambda s:Locator()),_act=act)
 if found:
  await bc.select_chat_row(session,'12-0')
  assert scrolls==['top','next'] and clicked==[{'position':{'x':10,'y':10},'timeout':5000}]
 else:
  with pytest.raises(bc.ChatSelectionUnavailable) as caught:await bc.select_chat_row(session,'12-0')
  assert caught.value.diagnostics['reason']=='row_not_unique' and not clicked
  assert len(scrolls)==(0 if 2 in seen else bc.LIST_REVEAL_STEPS)


async def test_point_is_read_after_pacing_and_waits_for_stable_geometry(monkeypatch):
 phase=[];points=iter([{'x':10,'y':10,'top':100},{'x':10,'y':10,'top':60},{'x':10,'y':10,'top':60}]);clicked=[]
 class Locator:
  async def count(self):return 1
  async def click(self,**kw):clicked.append(kw)
 async def evaluate(session,expr):
  assert phase==['paced']
  if 'elementFromPoint' in expr:return next(points)
 async def act(kind,fn):phase.append('paced');return await fn()
 monkeypatch.setattr(bc,'_bound_read',evaluate)
 await bc.select_chat_row(NS(page=NS(url=bc.CHAT_URL,locator=lambda s:Locator()),_act=act),'12-0')
 assert clicked[0]['position']=={'x':10,'y':10}


async def test_selection_timeout_becomes_recoverable_read_error(monkeypatch):
 class Locator:
  async def count(self):return 1
  async def click(self,**kw):raise TimeoutError('outside viewport; private candidate DOM')
 async def evaluate(session,expr):return {'x':10,'y':10} if 'elementFromPoint' in expr else None
 async def act(kind,fn):return await fn()
 monkeypatch.setattr(bc,'_bound_read',evaluate)
 with pytest.raises(bc.ChatSelectionUnavailable,match='未执行发送') as caught:
  await bc.select_chat_row(NS(page=NS(url=bc.CHAT_URL,locator=lambda s:Locator()),_act=act),'12-0')
 assert caught.value.diagnostics=={'stage':'click','reason':'outside_viewport','exception_type':'TimeoutError'}
 assert 'private candidate' not in str(caught.value) and 'private candidate' not in str(caught.value.diagnostics)


async def test_locator_and_geometry_bind_to_chat_page_after_pacing(monkeypatch):
 phase=[];calls=[]
 class Locator:
  async def count(self):return 1
  async def click(self,**kwargs):calls.append('click')
 def locator(selector):
  assert phase==['paced'];calls.append('locator');return Locator()
 chat=NS(url=bc.CHAT_URL,locator=locator)
 other=NS(url='about:blank',locator=lambda selector:pytest.fail('old page locator'))
 async def read(page,expr):
  assert page is chat and phase==['paced']
  return {'x':10,'y':10} if 'elementFromPoint' in expr else None
 async def act(kind,fn):phase.append('paced');await fn()
 monkeypatch.setattr(bc,'_bound_read',read)
 session=NS(page=other,_boss_chat_page=chat,_act=act)
 await bc.select_chat_row(session,'12-0')
 assert calls==['locator','click'] and session.page is chat


async def test_page_change_during_geometry_does_not_click_stale_locator(monkeypatch):
 class Locator:
  async def count(self):return 1
  async def click(self,**kwargs):pytest.fail('must not click after context change')
 page=NS(url=bc.CHAT_URL,locator=lambda selector:Locator())
 async def act(kind,fn):await fn()
 session=NS(page=page,_act=act)
 async def read(bound,expr):
  if 'elementFromPoint' in expr:
   session.page=NS(url='about:blank')
   return {'x':10,'y':10}
 monkeypatch.setattr(bc,'_bound_read',read)
 with pytest.raises(bc.ChatSelectionUnavailable) as caught:await bc.select_chat_row(session,'12-0')
 assert caught.value.diagnostics['reason']=='page_changed'


@pytest.mark.parametrize('matched',[True,False])
async def test_click_timeout_verifies_actual_selection_without_second_click(monkeypatch,matched):
 calls=[];page=NS(url=bc.CHAT_URL)
 async def menu(session):return [{'chat_id':'12-0','text':'甲 岗位'}]
 async def select(session,chat_id):
  calls.append('click')
  raise bc.ChatSelectionUnavailable('选择超时',stage='click',reason='timeout',exception_type='TimeoutError')
 async def read(session,chat_id,receiver,job_title,**kwargs):
  assert kwargs=={'required':False,'page':page};calls.append('read');return {'valid':matched and calls.count('read')>2}
 monkeypatch.setattr(bc,'open_menu',menu);monkeypatch.setattr(bc,'select_chat_row',select)
 monkeypatch.setattr(bc,'read_conversation',read)
 if matched:assert (await bc.open_conversation(NS(page=page),'12-0','甲','岗位'))['valid']
 else:
  with pytest.raises(bc.ChatSelectionUnavailable):await bc.open_conversation(NS(page=page),'12-0','甲','岗位')
 assert calls==['read','click']+['read']*(2 if matched else 3)


@pytest.mark.parametrize('pages,expected',[([[{'chat_id':'9-0','text':'乙 岗位'}],[{'chat_id':'12-0','text':'甲 岗位'}]],None),
                                           ([[{'chat_id':'9-0','text':'乙 岗位'}]]*(bc.LIST_REVEAL_STEPS+1),'target_row_missing')])
async def test_bound_chat_scrolled_out_of_virtual_list_is_found_by_scrolling(monkeypatch,pages,expected):
 # 已绑定会话被虚拟列表移出 DOM：滚动列表重新读取，找不到时仍按原因码拒绝且不点击。
 pages=iter(pages);calls=[];page=NS(url=bc.CHAT_URL)
 async def menu(session):return next(pages)
 async def evaluate(session,expr):
  if expr.startswith('((mode)'):calls.append('scroll');return {'moved':True,'end':False}
  assert expr==bc.ROWS_JS;return next(pages)
 async def read(session,chat_id,receiver,job_title,**kwargs):return {'valid':True}
 async def sleep(_):pass
 monkeypatch.setattr(bc,'open_menu',menu);monkeypatch.setattr(bc,'evaluate',evaluate);monkeypatch.setattr(bc,'read_conversation',read)
 monkeypatch.setattr(bc,'select_chat_row',lambda *a:pytest.fail('must not click'));monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 if expected is None:
  assert (await bc.open_conversation(NS(page=page),'12-0','甲','岗位'))['valid'] and calls==['scroll']
 else:
  with pytest.raises(bc.ChatSelectionUnavailable) as caught:await bc.open_conversation(NS(page=page),'12-0','甲','岗位')
  assert caught.value.diagnostics['reason']==expected and calls==['scroll']*bc.LIST_REVEAL_STEPS


async def test_inspect_unknown_chat_can_verify_click_timeout(monkeypatch):
 calls=[];page=NS(url=bc.CHAT_URL)
 async def select(*args):raise bc.ChatSelectionUnavailable('超时',stage='click',reason='timeout',exception_type='TimeoutError')
 async def identity(session,title,**kwargs):
  assert kwargs['page'] is page;calls.append('read');return {'chat_id':'12-0','receiver':'甲','job_title':title}
 monkeypatch.setattr(bc,'select_chat_row',select);monkeypatch.setattr(bc,'current_identity',identity)
 assert (await bc.inspect_row(NS(page=page),{'chat_id':'12-0'},'岗位'))['chat_id']=='12-0'
 assert calls==['read']


def recovery_session(states,*,url=bc.CHAT_URL):
 states=iter(states);calls=[]
 async def evaluate(expr):
  if expr=='1':return 1
  calls.append('guard');return next(states)
 async def goto(url,**kwargs):calls.append('reload')
 async def act(kind,fn):calls.append('paced');await fn()
 page=NS(url=url,_stealth_world=NS(evaluate=evaluate),goto=goto)
 async def observe(*args,**kwargs):
  calls.append('observe');return NS(url=page.url,elements=[],raw={})
 return NS(page=page,_act=act,observe=observe),calls


EMPTY_EDITOR={'editor_count':1,'has_draft':False,'chat_rows':2,'login':False,'challenge':False}


@pytest.mark.parametrize('override,reason',[
 ({'has_draft':True},'draft_present'),({'login':True},'login_required'),
 ({'challenge':True},'verification_required'),({'editor_count':0,'chat_rows':0},'editor_unavailable')])
async def test_recovery_does_not_reload_draft_login_or_risk(override,reason,monkeypatch):
 async def no_sleep(_):pass
 monkeypatch.setattr(bc.asyncio,'sleep',no_sleep)
 # 编辑器和列表都是 0 时先等页面画好；一直画不出来才判定无法核验。
 reads=1+bc.RENDER_WAIT_READS if reason=='editor_unavailable' else 1
 session,calls=recovery_session([{**EMPTY_EDITOR,**override}]*reads)
 result=await bc.recover_chat_selection(session)
 assert result['status']=='blocked' and result['reason']==reason and result['requires_admin']
 assert calls==['guard']*reads


async def test_recovery_waits_for_a_freshly_started_page_to_render(monkeypatch):
 # 15:09、15:27、15:44：重建后第一次恢复时编辑器和列表都还是 0，被直接判成无法核验而暂停。
 pauses=[]
 async def sleep(seconds):pauses.append(seconds)
 monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 blank={**EMPTY_EDITOR,'editor_count':0,'chat_rows':0}
 session,calls=recovery_session([blank,blank,EMPTY_EDITOR,EMPTY_EDITOR])
 assert await bc.recover_chat_selection(session)=={'status':'recovered','page_reloaded':False}
 assert calls==['guard','guard','guard','paced','guard','observe']
 assert len(pauses)==2 and all(0.8<=p<=5 for p in pauses)


async def test_recovery_opens_chat_when_the_browser_has_no_chat_tab(monkeypatch):
    calls=[]
    page=NS(url='about:blank')
    async def navigate(url):
        calls.append(url);page.url=bc.CHAT_URL
    session=NS(page=page,navigate=navigate,context=NS(pages=[]))
    def selection(sess):
        if bc.is_chat_page(sess.page):
            sess.page=sess._boss_chat_page=sess.page
            return sess.page
        raise bc.ChatSelectionUnavailable('无法唯一核验沟通标签页，等待恢复；未执行发送',stage='resolve_page',reason='page_missing')
    async def read(bound,expr):
        return {'editor_count':0,'has_draft':False,'chat_rows':0,'login':False,'challenge':False}
    monkeypatch.setattr(bc,'_selection_page',selection)
    monkeypatch.setattr(bc,'_bound_read',read)
    async def no_sleep(_):pass
    monkeypatch.setattr(bc.asyncio,'sleep',no_sleep)
    result=await bc.recover_chat_selection(session)
    assert calls==[bc.CHAT_URL] and result['reason']=='editor_unavailable'


async def test_recovery_checks_for_new_draft_after_pacing():
 session,calls=recovery_session([EMPTY_EDITOR,{**EMPTY_EDITOR,'has_draft':True}])
 assert (await bc.recover_chat_selection(session))['reason']=='draft_present'
 assert calls==['guard','paced','guard']


async def test_safe_recovery_keeps_the_open_chat_page():
 session,calls=recovery_session([EMPTY_EDITOR,EMPTY_EDITOR])
 assert await bc.recover_chat_selection(session)=={'status':'recovered','page_reloaded':False}
 assert calls==['guard','paced','guard','observe']


async def test_blank_detail_is_clicked_again_until_it_opens(monkeypatch):
    calls=[];page=NS(url=bc.CHAT_URL)
    async def menu(session):return [{'chat_id':'12-0','text':'甲 岗位'}]
    async def select(session,chat_id):calls.append('click')
    async def read(session,chat_id,receiver,job_title,**kwargs):
        calls.append('read')
        if calls.count('click')<2:return {'valid':False,'reason_code':'detail_not_open'}
        return {'valid':True}
    async def sleep(_):pass
    monkeypatch.setattr(bc,'open_menu',menu)
    monkeypatch.setattr(bc,'select_chat_row',select)
    monkeypatch.setattr(bc,'read_conversation',read)
    monkeypatch.setattr(bc.asyncio,'sleep',sleep)
    monkeypatch.setattr('cloakup.pacing.lognormal',lambda *args:0)
    assert (await bc.open_conversation(NS(page=page),'12-0','甲','岗位'))['valid']
    assert calls.count('click')==2


async def test_recovery_does_not_navigate_away_from_login():
 session,calls=recovery_session([],url='https://www.zhipin.com/web/user/')
 assert (await bc.recover_chat_selection(session))['reason']=='login_required'
 assert not calls


async def test_already_verified_conversation_does_not_click(monkeypatch):
 page=NS(url=bc.CHAT_URL)
 async def menu(session):return [{'chat_id':'12-0','text':'甲 岗位'}]
 async def read(*args,**kwargs):return {'valid':True}
 async def select(*args):raise AssertionError('already verified target must not be clicked again')
 monkeypatch.setattr(bc,'open_menu',menu)
 monkeypatch.setattr(bc,'read_conversation',read)
 monkeypatch.setattr(bc,'select_chat_row',select)
 assert (await bc.open_conversation(NS(page=page),'12-0','甲','岗位'))['valid']


async def test_list_identity_mismatch_refreshes_the_chat_page_then_rechecks(monkeypatch):
 calls=[];page=NS(url=bc.CHAT_URL)
 async def menu(session):
  calls.append('menu');return [{'chat_id':'12-0','text':'旧列表'}]
 async def refresh(session):
  calls.append('refresh');return [{'chat_id':'12-0','text':'甲 岗位'}]
 async def read(*args,**kwargs):
  calls.append('read');return {'valid':True}
 monkeypatch.setattr(bc,'open_menu',menu)
 monkeypatch.setattr(bc,'refresh_chat_list',refresh)
 monkeypatch.setattr(bc,'read_conversation',read)
 monkeypatch.setattr(bc,'select_chat_row',lambda *a:pytest.fail('刷新后已核对的会话不必再点'))
 assert (await bc.open_conversation(NS(page=page),'12-0','甲','岗位'))['valid']
 assert calls==['menu','refresh','read']


async def test_platform_row_identity_replaces_the_local_name_and_job(monkeypatch):
 calls=[]
 async def menu(session):return [{'chat_id':'12-0','text':'平台姓名 平台岗位','receiver':'平台姓名','job_title':'平台岗位'}]
 async def refresh(session):return [{'chat_id':'12-0','text':'平台姓名 平台岗位','receiver':'平台姓名','job_title':'平台岗位'}]
 async def read(session,chat_id,receiver,job_title,**kwargs):
  calls.append((chat_id,receiver,job_title));return {'valid':True,'chat_id':chat_id,'receiver':receiver,'job_title':job_title}
 monkeypatch.setattr(bc,'open_menu',menu)
 monkeypatch.setattr(bc,'refresh_chat_list',refresh)
 monkeypatch.setattr(bc,'read_conversation',read)
 monkeypatch.setattr(bc,'select_chat_row',lambda *a:pytest.fail('平台身份已核对，不必再点'))
 snap=await bc.open_conversation(NS(page=NS(url=bc.CHAT_URL)),'12-0','旧姓名','旧岗位')
 assert calls==[('12-0','平台姓名','平台岗位')]
 assert snap['receiver']=='平台姓名' and snap['job_title']=='平台岗位'


async def test_missing_row_does_not_adopt_another_open_person(monkeypatch):
 async def menu(session):return [{'chat_id':'99-0','text':'另一个人 岗位','receiver':'另一个人','job_title':'岗位'}]
 async def evaluate(session,expr):
  if expr==bc.ROWS_JS:return [{'chat_id':'99-0','text':'另一个人 岗位','receiver':'另一个人','job_title':'岗位'}]
  return {'moved':False,'end':True}
 async def sleep(_):pass
 monkeypatch.setattr(bc,'open_menu',menu)
 monkeypatch.setattr(bc,'evaluate',evaluate)
 monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 monkeypatch.setattr(bc,'refresh_chat_list',lambda *a:pytest.fail('找不到目标行时不能整页刷新去套用别人'))
 monkeypatch.setattr(bc,'read_conversation',lambda *a,**k:pytest.fail('找不到目标行时不能读取当前打开的人'))
 with pytest.raises(bc.ChatSelectionUnavailable) as caught:
  await bc.open_conversation(NS(page=NS(url=bc.CHAT_URL)),'12-0','旧姓名','旧岗位')
 assert caught.value.diagnostics['reason']=='target_row_missing'


async def test_list_identity_still_mismatch_after_refresh_does_not_open(monkeypatch):
 page=NS(url=bc.CHAT_URL)
 async def menu(session):return [{'chat_id':'12-0','text':'旧列表'}]
 async def refresh(session):return [{'chat_id':'12-0','text':'仍然不对'}]
 monkeypatch.setattr(bc,'open_menu',menu)
 monkeypatch.setattr(bc,'refresh_chat_list',refresh)
 monkeypatch.setattr(bc,'select_chat_row',lambda *a:pytest.fail('对不上时不能点击'))
 with pytest.raises(bc.ChatSelectionUnavailable) as caught:
  await bc.open_conversation(NS(page=page),'12-0','甲','岗位')
 assert caught.value.diagnostics['reason']=='list_identity_mismatch'


@pytest.mark.parametrize('override,reason',[({'has_draft':True},'draft_present'),({'login':True},'login_required')])
async def test_refresh_chat_list_does_not_reload_draft_or_login(monkeypatch,override,reason):
 calls=[]
 async def bound(page,expr):return {**EMPTY_EDITOR,**override}
 async def act(kind,fn):
  calls.append('reload');await fn()
 monkeypatch.setattr(bc,'_bound_read',bound)
 page=NS(url=bc.CHAT_URL,goto=lambda *a,**k:pytest.fail('must not reload'))
 with pytest.raises(bc.ChatSelectionUnavailable) as caught:
  await bc.refresh_chat_list(NS(page=page,_act=act))
 assert caught.value.diagnostics['reason']==reason and calls==[]


async def test_refresh_chat_list_reloads_the_chat_page_once(monkeypatch):
 calls=[]
 async def goto(url,**kwargs):
  calls.append(url);page.url=url
 page=NS(url=bc.CHAT_URL,goto=goto)
 async def bound(page,expr):return dict(EMPTY_EDITOR)
 async def act(kind,fn):
  calls.append(kind);await fn()
 async def rows(session,expr):
  calls.append('rows');return [{'chat_id':'12-0','text':'甲 岗位'}]
 async def sleep(_):return None
 monkeypatch.setattr(bc,'_bound_read',bound)
 monkeypatch.setattr(bc,'evaluate',rows)
 monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 monkeypatch.setattr('cloakup.pacing.lognormal',lambda *args:0)
 fresh=await bc.refresh_chat_list(NS(page=page,_act=act))
 assert fresh==[{'chat_id':'12-0','text':'甲 岗位'}]
 assert calls==['navigate',bc.CHAT_URL,'rows']


async def test_bound_read_recreates_timed_out_world_without_navigation():
 from unittest.mock import AsyncMock
 invalidations=[]
 world=NS(evaluate=AsyncMock(side_effect=[TimeoutError(),1,{'valid':True}]),invalidate=lambda:invalidations.append(True))
 page=NS(url=bc.CHAT_URL,_stealth_world=world)
 assert await bc._bound_read(page,'probe')=={'valid':True}
 assert [c.args[0] for c in world.evaluate.call_args_list]==['1','1','probe']
 assert invalidations==[True] and page._cloakup_read_channel_recovered_at>0


async def test_bound_read_has_one_rebuild_budget():
 from unittest.mock import AsyncMock
 invalidations=[]
 world=NS(evaluate=AsyncMock(side_effect=[TimeoutError(),TimeoutError()]),invalidate=lambda:invalidations.append(True))
 with pytest.raises(TimeoutError):await bc._bound_read(NS(url=bc.CHAT_URL,_stealth_world=world),'probe')
 assert invalidations==[True] and world.evaluate.await_count==2


async def test_bound_read_stops_if_page_changes_after_rebuild():
 from unittest.mock import AsyncMock
 page=NS(url=bc.CHAT_URL)
 async def warm(expression):page.url='https://www.zhipin.com/web/user/';return 1
 page._stealth_world=NS(evaluate=AsyncMock(side_effect=[TimeoutError(),None]),invalidate=lambda:None)
 async def evaluate(expression):
  if expression!='1':raise TimeoutError()
  return await warm(expression)
 page._stealth_world.evaluate=evaluate
 with pytest.raises(bc.ChatSelectionUnavailable,match='页已变化'):await bc._bound_read(page,'probe')


async def test_read_world_warms_once_and_invalidates_on_document_navigation():
 from unittest.mock import AsyncMock
 callbacks={}
 world=NS(evaluate=AsyncMock(side_effect=lambda e:1 if e=='1' else {'valid':True}),invalidate=lambda:None)
 page=NS(url=bc.CHAT_URL,_stealth_world=world,on=lambda event,cb:callbacks.update({event:cb}))
 await bc._bound_read(page,'probe');await bc._bound_read(page,'probe')
 assert [c.args[0] for c in world.evaluate.call_args_list]==['1','probe','probe']
 callbacks['framenavigated'](NS(parent_frame=None))
 await bc._bound_read(page,'probe')
 assert [c.args[0] for c in world.evaluate.call_args_list][-2:]==['1','probe']

async def test_unbound_history_identity_failure_enters_bounded_recovery(monkeypatch):
 async def select(*a):pass
 async def identity(*a,**kw):return None
 async def sleep(*a):pass
 monkeypatch.setattr(bc,'select_chat_row',select);monkeypatch.setattr(bc,'current_identity',identity);monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 with pytest.raises(bc.ChatSelectionUnavailable) as err:await bc.inspect_row(NS(),{'chat_id':'12-0'},'岗位')
 assert err.value.diagnostics['stage']=='verify_identity'
 assert err.value.diagnostics['reason']=='header_identity_unverified'

async def test_history_read_remains_bound_to_clicked_page_and_preserves_ambiguity(monkeypatch):
 page=NS(url=bc.CHAT_URL)
 async def select(session,chat_id):
  session._boss_selection_page=page
  session.page=NS(url=bc.CHAT_URL)
 async def identity(session,title,**kwargs):
  assert kwargs=={'page':page,'chat_id':'12-0'}
  session._boss_identity_probe={'reason_code':'identity_ambiguous','evidence':{'name_matched':2}}
 async def sleep(*a):pass
 monkeypatch.setattr(bc,'select_chat_row',select)
 monkeypatch.setattr(bc,'current_identity',identity)
 monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 with pytest.raises(bc.ChatSelectionUnavailable) as err:
  await bc.inspect_row(NS(page=page),{'chat_id':'12-0'},'岗位')
 assert err.value.diagnostics['reason']=='identity_ambiguous'


def _menu_world(monkeypatch, heal_result, rows_after=None):
 """沟通页列表为空：交给 page_health 自愈，自愈成功后重读列表。"""
 calls=[]
 async def observation(session):return NS(raw={'text':''})
 async def evaluate(session,expr):
  if expr==bc.ROWS_JS:return (rows_after or []) if calls else []
  return None
 async def heal(session,kind):calls.append(kind);return heal_result
 async def no_sleep(*a):pass
 from cloakup import page_health
 monkeypatch.setattr(bc,'_observation',observation);monkeypatch.setattr(bc,'evaluate',evaluate)
 monkeypatch.setattr(page_health,'heal',heal);monkeypatch.setattr(bc.asyncio,'sleep',no_sleep)
 return NS(page=NS(url=bc.CHAT_URL)),calls


async def test_empty_menu_is_healed_and_read_again(monkeypatch):
 # 11-24 重建后沟通页一直停在“加载中”，执行器连续 3 轮只报“未就绪”。
 session,calls=_menu_world(monkeypatch,{'healthy':True,'healed_by':'reload'},[{'chat_id':'1-0'}])
 assert await bc.open_menu(session)==[{'chat_id':'1-0'}] and calls==['chat']


async def test_unhealed_menu_returns_empty_for_the_next_cycle(monkeypatch):
 session,calls=_menu_world(monkeypatch,{'healthy':False,'reason':'loading_stuck','exhausted':True})
 assert await bc.open_menu(session)==[] and calls==['chat']


@pytest.mark.parametrize('reason,text',[('login_required','登录'),('verification_required','校验')])
async def test_login_or_verification_found_by_the_health_check_stops(monkeypatch,reason,text):
 session,_=_menu_world(monkeypatch,{'healthy':False,'reason':reason,'needs_admin':True})
 with pytest.raises(SourcingError,match=text):await bc.open_menu(session)


async def test_reveal_waits_for_more_rows_at_the_bottom_before_giving_up(monkeypatch):
 # 11-24：老会话在 12 屏以外；到底后列表还会加载更早的会话。
 async def sleep(_):pass
 monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 states=iter([{'end':False}]*3+[{'end':True},{'end':False},{'end':False}])
 seen=iter([False]*5+[True])
 async def scroll(mode):return next(states)
 async def found():return next(seen)
 assert await bc._reveal(scroll,found)==(True,False)
 async def at_end(mode):return {'end':True}
 async def never():return False
 calls=[]
 async def counting(mode):calls.append(mode);return {'end':True}
 assert await bc._reveal(counting,never)==(False,True) and len(calls)==bc.LIST_END_WAITS+1


async def test_missing_chat_after_reaching_the_bottom_is_marked(monkeypatch):
 async def menu(session):return [{'chat_id':'9-0','text':'乙 岗位'}]
 async def evaluate(session,expr):
  if expr.startswith('((mode)'):return {'moved':False,'end':True}
  return [{'chat_id':'9-0','text':'乙 岗位'}]
 async def sleep(_):pass
 monkeypatch.setattr(bc,'open_menu',menu);monkeypatch.setattr(bc,'evaluate',evaluate);monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 with pytest.raises(bc.ChatSelectionUnavailable) as caught:await bc.open_conversation(NS(page=NS(url=bc.CHAT_URL)),'12-0','甲','岗位')
 assert caught.value.diagnostics['reason']=='target_row_missing' and caught.value.diagnostics['list_end'] is True


async def test_identity_refresh_keeps_scrolling_for_an_old_chat(monkeypatch):
 # 11-24 候选人 34：列表身份不一致时整页刷新，刷新后只看第一屏，老会话永远找不到。
 async def refresh(session):return [{'chat_id':'9-0','text':'乙 岗位'}]
 pages=iter([[{'chat_id':'9-0','text':'乙 岗位'}],[{'chat_id':'12-0','text':'新名字 新岗位'}]])
 async def evaluate(session,expr):
  if expr.startswith('((mode)'):return {'moved':True,'end':False,'via':'container'}
  return next(pages)
 async def sleep(_):pass
 monkeypatch.setattr(bc,'refresh_chat_list',refresh);monkeypatch.setattr(bc,'evaluate',evaluate);monkeypatch.setattr(bc.asyncio,'sleep',sleep)
 monkeypatch.setattr(bc,'_listed_identity',lambda row:(None,None))
 row={'chat_id':'12-0','text':'别的 名字'}
 with pytest.raises(bc.ChatSelectionUnavailable) as caught:
  await bc._identity_from_platform_row(NS(),row,'12-0','甲','岗位')
 assert caught.value.diagnostics['reason']=='list_identity_mismatch'


async def test_menu_is_scrolled_to_the_top_before_rows_are_read(monkeypatch):
 # 11-25 17:39–18:00 FDE交付工程师加入后，多名候选人主动打招呼一直没被处理：上一岗位刚打开 11-22 的老会话，
 # 虚拟列表停在下方，只渲染老会话行；顶部的新来信行不在 DOM 里，新岗位每轮读到的都是别的岗位的行。
 state={'top':False};order=[]
 async def observation(session):return NS(raw={'text':''})
 async def evaluate(session,expr):
  if expr==bc.ROWS_JS:
   order.append('rows')
   return [{'chat_id':'1-0','text':'新候选人 新岗位','unread':True}] if state['top'] else [{'chat_id':'9-0','text':'老候选人 老岗位','unread':False}]
  if expr==bc.LIST_SCROLL_JS.replace('MODE','"top"'):
   order.append('top');state['top']=True;return {'moved':True,'end':False,'via':'container'}
  return None
 async def no_sleep(*a):pass
 monkeypatch.setattr(bc,'_observation',observation);monkeypatch.setattr(bc,'evaluate',evaluate);monkeypatch.setattr(bc.asyncio,'sleep',no_sleep)
 rows=await bc.open_menu(NS(page=NS(url=bc.CHAT_URL)))
 assert order[0]=='top' and [r['chat_id'] for r in rows]==['1-0']


def _abort_world(monkeypatch,editor_after,*,fresh_fp='g',clear_raises=False,pre_send=None):
 # 11-27 10:28 赵清山：打完字发现候选人刚回消息，按设计中止；自己打的字不能留在输入框里。
 before={'fingerprint':'f','messages':[],'editor_count':1}
 box={'value':''}
 reads=iter([{**before,'valid':True},{**before,'fingerprint':fresh_fp,'valid':True,'editor_value':editor_after}])
 async def read(session,chat_id,receiver,job_title,**kw):
  try:data=next(reads)
  except StopIteration:data={**before,'valid':True,'editor_value':box['value']}
  if 'editor_value' in data and data is not None and data.get('fingerprint')==fresh_fp:box['value']=data['editor_value']
  return data
 async def observe(*a,**kw):return NS(elements=[NS(label='发送',raw={},index=3,tag='button')])
 class Editor:
  async def count(self):return 1
  async def inner_text(self):return ''
  async def fill(self,value,**kw):
   if clear_raises and value=='':raise RuntimeError('fill failed')
   box['value']=value
 async def act(kind,fn):await fn()
 clicks=[]
 async def click(index):clicks.append(index)
 monkeypatch.setattr(bc,'read_conversation',read);monkeypatch.setattr(bc,'_observation',observe)
 session=NS(page=NS(locator=lambda s:Editor()),_act=act,click=click)
 return session,before,box,clicks


async def test_reply_during_typing_clears_own_text_and_raises_aborted(monkeypatch):
 session,before,box,clicks=_abort_world(monkeypatch,'正文')
 with pytest.raises(bc.SendAborted) as caught:
  await bc.send_once(session,{'chat_id':'12-0','receiver':'甲','job_title':'岗位'},'正文',before)
 e=caught.value
 assert e.reason=='reply_during_typing' and e.typed and e.cleared and box['value']=='' and clicks==[]


async def test_abort_keeps_editor_when_text_differs(monkeypatch):
 session,before,box,clicks=_abort_world(monkeypatch,'正文\n')
 with pytest.raises(bc.SendAborted) as caught:
  await bc.send_once(session,{'chat_id':'12-0','receiver':'甲','job_title':'岗位'},'正文',before)
 assert caught.value.typed and caught.value.cleared is False and box['value']=='正文\n' and clicks==[]


async def test_abort_reports_not_cleared_when_clear_fails(monkeypatch):
 session,before,box,clicks=_abort_world(monkeypatch,'正文',clear_raises=True)
 with pytest.raises(bc.SendAborted) as caught:
  await bc.send_once(session,{'chat_id':'12-0','receiver':'甲','job_title':'岗位'},'正文',before)
 assert caught.value.cleared is False and box['value']=='正文' and clicks==[]


async def test_pre_send_refusal_clears_and_raises_aborted(monkeypatch):
 session,before,box,clicks=_abort_world(monkeypatch,'正文',fresh_fp='f')
 def refuse():raise SourcingError('岗位任务已暂停，未点击发送')
 with pytest.raises(bc.SendAborted) as caught:
  await bc.send_once(session,{'chat_id':'12-0','receiver':'甲','job_title':'岗位'},'正文',before,pre_send=refuse)
 assert caught.value.reason=='not_authorized' and caught.value.cleared and box['value']=='' and clicks==[]


async def test_reread_failure_after_typing_aborts_and_clears(monkeypatch):
 # 评审：打字后、点击前读取失败也一定没发出，同样删掉自己的字。
 session,before,box,clicks=_abort_world(monkeypatch,'正文',fresh_fp='f')
 calls=[]
 async def observe(*a,**kw):
  calls.append(1)
  if len(calls)>1:raise RuntimeError('observe failed')
  return NS(elements=[])
 monkeypatch.setattr(bc,'_observation',observe)
 class Editor:
  async def count(self):return 1
  async def inner_text(self):return ''
  async def fill(self,value,**kw):box['value']=value
 session.page=NS(locator=lambda s:Editor())
 with pytest.raises(bc.SendAborted) as caught:
  await bc.send_once(session,{'chat_id':'12-0','receiver':'甲','job_title':'岗位'},'正文',before)
 assert caught.value.reason=='reread_failed' and caught.value.cleared and clicks==[]


async def test_clear_uses_tidy_when_session_has_it(monkeypatch):
 # 评审：任务刚暂停时 _act 会拒绝操作；删自己的字走 _tidy（只在管理员接管时拒绝）。
 session,before,box,clicks=_abort_world(monkeypatch,'正文',fresh_fp='f')
 async def refuse_act(kind,fn):
  if box['value']:raise SourcingError('岗位任务已暂停，取消后续页面操作')
  await fn()
 tidy=[]
 async def tidy_act(kind,fn):tidy.append(kind);await fn()
 session._act=refuse_act;session._tidy=tidy_act
 def refuse():raise SourcingError('岗位任务已暂停，未点击发送')
 with pytest.raises(bc.SendAborted) as caught:
  await bc.send_once(session,{'chat_id':'12-0','receiver':'甲','job_title':'岗位'},'正文',before,pre_send=refuse)
 assert caught.value.cleared and tidy==['type'] and box['value']==''


def test_identity_reader_reports_header_job_field():
 # 11-27 旧 QE 岗位撤下后约 7 条旧会话都报“详情头部未匹配目标岗位”：诊断里要看得到头部实际写的沟通职位。
 assert 'header_jobs' in bc.IDENTITY_READER_JS


async def test_header_job_mismatch_reports_observed_job_field(monkeypatch):
 async def evaluate(*a):return {'valid':False,'reason':'会话收件人或岗位未核实','reason_code':'header_job_unverified',
                               'evidence':{'header_jobs':['QE工程师（已关闭）'],'header_candidates':1}}
 monkeypatch.setattr(bc,'evaluate',evaluate)
 with pytest.raises(bc.ChatSelectionUnavailable) as caught:await bc.read_conversation(object(),'12-0','甲','QE工程师')
 assert caught.value.diagnostics['header_jobs']==['QE工程师（已关闭）']
