"""Login handoff tests: no external sites, model calls, or real messages."""
import asyncio
import copy
from types import SimpleNamespace as NS
import pytest
from cloakup.access import boss_login_required,boss_access_ready,can_recover_login
from cloakup.workbench import Workbench,WebSession
from cloakup.browser import BrowserSession
from cloakup.config import BrowserConfig,PacingConfig


def observation(path='/web/user/',labels=('登录/注册',),**raw):
    return NS(url='https://www.zhipin.com'+path,title='BOSS',
              elements=[NS(label=t,tag='button',raw={}) for t in labels],raw=raw,screenshot=None)


def test_access_needs_recruiter_page_not_claimed_login():
    assert boss_login_required(observation())
    assert not boss_access_ready(observation())
    assert not boss_access_ready(observation('/web/chat/index',()))
    assert boss_access_ready(observation('/web/chat/index',('沟通','推荐牛人')))
    assert not boss_access_ready(observation('/web/chat/index',('沟通',),challenge=['captcha']))
    assert not boss_access_ready(observation('/web/chat/index',('沟通',),text='账号存在异常'))
    assert not boss_access_ready(observation('/web/geek/',('沟通',)))


@pytest.fixture
def manager(tmp_path):
    m=Workbench(tmp_path)
    yield m
    m.close()


def job():
    return {'id':'a'*32,'mode':'automation_task','status':'running','phase':'exploration',
            'events':[],'task_snapshot':{'config':{'engine':'boss_sourcing'}}}


def test_handoff_keeps_task_and_verifies_button(manager,monkeypatch):
    value=[observation()]
    async def read(session): return value[0]
    monkeypatch.setattr(manager,'access_observation',read)
    async def scenario():
        j=job();manager.jobs[j['id']]=j
        waiting=asyncio.create_task(manager.wait_for_access(j,object(),'login'))
        manager.tasks[j['id']]=waiting
        await asyncio.sleep(0)
        assert j['status']=='waiting' and j['interaction']['kind']=='browser_access'
        assert manager.busy()['id']==j['id']
        interaction=j['interaction']['id']
        with pytest.raises(ValueError,match='尚未核验'):
            await manager.action(j['id'],{'action':'answer','interaction_id':interaction,'value':'已登录'})
        with pytest.raises(ValueError,match='尚未核验'):
            await manager.action(j['id'],{'action':'check_access','interaction_id':interaction})
        assert not waiting.done() and j['status']=='waiting'
        value[0]=observation('/web/chat/index',('沟通',))
        await manager.action(j['id'],{'action':'check_access','interaction_id':interaction})
        await waiting
        assert j['status']=='running' and j['interaction'] is None
        assert [e['kind'] for e in j['events']]==['access_required','access_restored']
        assert not manager.answers and not manager.access_sessions
        manager.tasks.pop(j['id'])
        j['status']='succeeded'
    manager.call(scenario())


def test_handoff_detects_login_automatically(manager,monkeypatch):
    async def read(session):return observation('/web/chat/index',('沟通',))
    monkeypatch.setattr(manager,'access_observation',read)
    async def scenario():
        j=job();manager.jobs[j['id']]=j
        await manager.wait_for_access(j,object(),'login')
        assert j['status']=='running' and j['interaction'] is None
        assert len(manager.db.trace(j['id']))==2
        j['status']='succeeded'
    manager.call(scenario())


def test_cancel_during_login_cleans_waiters(manager,monkeypatch):
    async def read(session):return observation()
    monkeypatch.setattr(manager,'access_observation',read)
    async def scenario():
        j=job();manager.jobs[j['id']]=j
        waiting=asyncio.create_task(manager.wait_for_access(j,object(),'login'))
        manager.tasks[j['id']]=waiting
        await asyncio.sleep(0)
        await manager.action(j['id'],{'action':'cancel'})
        with pytest.raises(asyncio.CancelledError):await waiting
        assert not manager.answers and not manager.access_sessions
        assert j['interaction'] is None and j['status']=='cancelling'
        manager.tasks.pop(j['id']);j['status']='cancelled'
    manager.call(scenario())


def test_observation_restores_target_and_invalidates_auth_caches(manager,monkeypatch):
    calls=[];observations=[observation(),observation('/web/chat/index',('沟通',))]
    async def read(self,screenshot,*args,**kwargs):
        calls.append(('observe',screenshot));return observations.pop(0)
    async def navigate(self,url):calls.append(('navigate',url));return ''
    async def handoff(j,session,reason):
        assert reason=='login';calls.append(('wait',j['id']))
    monkeypatch.setattr(BrowserSession,'observe',read)
    monkeypatch.setattr(BrowserSession,'navigate',navigate)
    monkeypatch.setattr(manager,'wait_for_access',handoff)
    j=job();session=WebSession(manager,j)
    session._boss_greeting={'old':True};session._boss_recommend_detail={'old':True}
    result=manager.call(session.observe(False))
    assert boss_access_ready(result)
    assert all(not c[1] for c in calls if c[0]=='observe')
    assert ('navigate','https://www.zhipin.com/web/chat/index') in calls
    assert session._boss_greeting is None and session._boss_recommend_detail is None


def test_legacy_login_failure_recovery_is_read_only_and_versioned(manager,monkeypatch):
    t=manager.definitions.save({'config':{'name':'Test','website':'https://example.com/','scenario':'Read','goal':'Read'}})
    j={**job(),'status':'failed','error':'SourcingError: BOSS 已显示登录表单','task_id':t['id'],'task_snapshot':copy.deepcopy(t),'params':{},'phases':[{'phase':'exploration'}]}
    manager.jobs[j['id']]=j
    assert can_recover_login(j)
    assert not can_recover_login({**j,'phase':'implementation'})
    assert not can_recover_login({**j,'phases':[{'phase':'implementation'}]})
    assert not can_recover_login({**j,'error':'身份待核实'})
    restarted=[]
    async def execute(item,recipe):
        restarted.append(item['id']);item['status']='succeeded';manager.tasks.pop(item['id'],None)
    monkeypatch.setattr(manager,'execute',execute)
    async def scenario():
        await manager.action(j['id'],{'action':'recover_login'})
        await manager.tasks[j['id']]
        assert restarted==[j['id']] and j['recoveries'][0]['phase']=='exploration'
        assert j['task_snapshot']['revision']==1
    manager.call(scenario())


def test_browser_capacity_wait_retries_without_replaying_business(manager,monkeypatch):
    class CloakBrowserLicenseError(Exception):pass
    calls=[]
    class Session:
        async def start(self):
            calls.append('start')
            if calls.count('start')==1:raise CloakBrowserLicenseError('session limit reached')
        async def close(self):calls.append('close')
    async def sleep(seconds):
        assert seconds==15
        j=manager.jobs['a'*32]
        assert j['status']=='waiting' and j['interaction']['kind']=='browser_capacity'
    monkeypatch.setattr('cloakup.workbench.asyncio.sleep',sleep)
    j=job();manager.jobs[j['id']]=j
    manager.call(manager.start_browser(j,Session()))
    assert calls==['start','close','start']
    assert j['status']=='running' and j['interaction'] is None
    assert 'capacity_wait' not in j
    assert [e['kind'] for e in j['events']]==['browser_capacity','browser_ready']
    j['status']='succeeded'


def test_invalid_license_is_not_retried_as_capacity(manager,monkeypatch):
    # 授权失败只做有限次退避重试，不进入名额等待；仍失败按原错误报出。
    from cloakup.workbench import BROWSER_LICENSE_RETRIES
    class CloakBrowserLicenseError(Exception):pass
    calls=[]
    class Session:
        async def start(self):calls.append('start');raise CloakBrowserLicenseError('invalid license')
        async def close(self):calls.append('close')
    async def sleep(seconds):assert 4<=seconds<=20
    monkeypatch.setattr('cloakup.workbench.asyncio.sleep',sleep)
    j=job();manager.jobs[j['id']]=j
    with pytest.raises(CloakBrowserLicenseError):manager.call(manager.start_browser(j,Session()))
    assert calls.count('start')==1+BROWSER_LICENSE_RETRIES
    assert 'browser_capacity' not in [e['kind'] for e in j['events']] and j.get('interaction') is None
    j['status']='failed'


def test_transient_license_check_at_startup_is_retried(manager,monkeypatch):
    # 16:02 重建容器后授权校验瞬间失败，许可证有效、名额空着，恢复后正常。
    class CloakBrowserLicenseError(Exception):pass
    calls=[]
    class Session:
        async def start(self):
            calls.append('start')
            if calls.count('start')==1:raise CloakBrowserLicenseError('license validation failed')
        async def close(self):calls.append('close')
    async def sleep(seconds):pass
    monkeypatch.setattr('cloakup.workbench.asyncio.sleep',sleep)
    j=job();manager.jobs[j['id']]=j
    manager.call(manager.start_browser(j,Session()))
    assert calls==['start','close','start'] and j['status']=='running'
    j['status']='succeeded'


def test_badge_counts_are_valid_recruiter_navigation():
    assert boss_access_ready(observation('/web/chat/index',('沟通 1',)))
    assert boss_access_ready(observation('/web/chat/index',('沟通12',)))
    assert not boss_access_ready(observation('/web/chat/index',('请沟通这个职位',)))


@pytest.mark.parametrize('authenticated', [True,False])
def test_access_selects_verified_tab_only_inside_owned_context(manager,monkeypatch,authenticated):
    login=NS(url='https://www.zhipin.com/web/user/',is_closed=lambda:False)
    other=NS(url='https://www.zhipin.com/web/chat/index',is_closed=lambda:False)
    outside=NS(url='https://example.com/',is_closed=lambda:False)
    session=NS(page=login,context=NS(pages=[login,other,outside]),last_obs='original',_observed_frames={'original':True})
    calls=[]
    async def observe(self,*a,**kw):
        calls.append(self.page)
        obs=observation('/web/chat/index',('沟通 1',)) if self.page is other and authenticated else observation()
        self.last_obs=obs;self._observed_frames={'changed':True};return obs
    monkeypatch.setattr(BrowserSession,'observe',observe)
    result=manager.call(manager.access_observation(session))
    assert boss_access_ready(result)==authenticated
    if authenticated:assert session.page is other
    else:assert session.page is login and session.last_obs=='original' and session._observed_frames=={'original':True}
    assert outside not in calls


def test_legacy_reply_triggers_real_check_after_login(manager,monkeypatch):
    async def observe(session):return observation('/web/chat/index',('沟通 1',))
    monkeypatch.setattr(manager,'access_observation',observe)
    async def scenario():
        j=job();manager.jobs[j['id']]=j
        task=asyncio.create_task(manager.wait_for_access(j,object(),'login'))
        await asyncio.sleep(0)
        await manager.action(j['id'],{'action':'answer','interaction_id':j['interaction']['id'],'value':'已登录'})
        await task
        assert j['status']=='running' and j['interaction'] is None
        j['status']='succeeded'
    manager.call(scenario())


def test_boss_popup_cannot_steal_conversation_focus(manager):
    session=WebSession(manager,job())
    original=object();session.page=original
    session._on_new_page(NS(url='about:blank'))
    assert session.page is original
