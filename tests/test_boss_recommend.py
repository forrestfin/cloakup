"""Ordinary-recommendation boundaries, using observed DOM contracts."""
from types import SimpleNamespace as NS
import pytest
import cloakup.boss_recommend as br
from cloakup.sourcing import SourcingError


class Session:
    def __init__(self,query='jobid=job-1&status=0'):
        self.page=NS(frames=[NS(url='https://www.zhipin.com/web/frame/recommend/?'+query)])


@pytest.mark.parametrize('query',['jobid=another&status=0','jobid=job-1&status=3','jobid=job-1&status=1','jobid=job-1'])
async def test_wrong_job_or_paid_channel_stops_before_reading(monkeypatch,query):
    calls=[]
    async def value(*a):calls.append(a);return '交付顾问'
    monkeypatch.setattr(br,'_frame_value',value)
    with pytest.raises(SourcingError):await br.verify_list(Session(query),'job-1','交付顾问')
    assert not calls


async def test_correct_channel_still_requires_visible_job_title(monkeypatch):
    async def value(*a):return '另一个岗位'
    monkeypatch.setattr(br,'_frame_value',value)
    with pytest.raises(SourcingError):await br.verify_list(Session(),'job-1','交付顾问')


def test_filters_exclude_unrelated_or_sensitive_criteria():
    assert br.checked_filters(['本科','硕士','博士','5-10年','10年以上'])
    for values in (['女'],['25-30岁'],['本科','本科'],'本科',None):
        with pytest.raises(SourcingError):br.checked_filters(values)


async def test_existing_greeting_is_read_only_and_must_match_exactly(monkeypatch):
    actions=[]
    original=object()
    class Temporary:
        async def close(self):pass
    class Context:
        async def new_page(self):raise AssertionError('Settings must stay in the authenticated original tab')
    class S:
        page=original
        context=Context()
        async def navigate(self,url):actions.append(('navigate',url))
        async def observe(self,*args,**kwargs):return NS(url=br.GREETING_URL,elements=[])
    async def click(*a,**kw):actions.append(('read_tab',a[1]))
    async def value(s,f,expr):
        return True if 'ui-switch' in expr else [{'title':'交付顾问','text':'已批准正文'}]
    monkeypatch.setattr(br,'_click',click);monkeypatch.setattr(br,'_frame_value',value)
    s=S()
    assert (await br.existing_greeting(s,'交付顾问','已批准正文'))['action']=='reused'
    assert s.page is original
    with pytest.raises(SourcingError):await br.existing_greeting(s,'交付顾问','不同正文')
    assert s.page is original
    assert all(a[0] in ('navigate','read_tab') for a in actions)


async def test_saved_greeting_waits_for_settings_data_without_writing(monkeypatch):
    original=object();reads=[];clicks=[]
    class Temporary:
        async def close(self):pass
    class Context:
        async def new_page(self):raise AssertionError('Settings must stay in the authenticated original tab')
    class S:
        page=original
        context=Context()
        async def navigate(self,url):pass
        async def observe(self,*args,**kwargs):return NS(url=br.GREETING_URL,elements=[])
    async def click(*args,**kwargs):clicks.append(args[1])
    async def value(s,f,expr):
        if 'ui-switch' in expr:return True
        reads.append(True)
        return [] if len(reads)==1 else [{'title':'交付顾问','text':'已批准正文'}]
    monkeypatch.setattr(br,'_click',click);monkeypatch.setattr(br,'_frame_value',value)
    session=S()
    assert (await br.existing_greeting(session,'交付顾问','已批准正文'))['action']=='reused'
    assert len(reads)==2 and clicks==['按职位设置招呼语'] and session.page is original


async def test_missing_target_never_opens_another_candidate(monkeypatch):
    async def verify(*a):return 'verified'
    async def cards(*a):return [{'geek_id':'another','contact_label':'打招呼'}]
    monkeypatch.setattr(br,'verify_list',verify);monkeypatch.setattr(br,'read_cards',cards)
    with pytest.raises(SourcingError):await br.open_profile(object(),'wanted','job-1','交付顾问')


async def test_new_facts_after_review_stop_before_approval(monkeypatch):
    card={'geek_id':'g-1','platform_label':'隐名','summary':'新的经历'}
    async def greeting(*a):return {}
    async def profile(*a,**kw):return card
    monkeypatch.setattr(br,'existing_greeting',greeting);monkeypatch.setattr(br,'open_profile',profile)
    adapter=br.BossRecommendContact(object())
    case={'job':{'url':'https://www.zhipin.com/web/chat/job/edit?encryptId=job-1','label':'交付顾问'},'platform_greeting_text':'已批准正文'}
    candidate={'profile':{'geek_id':'g-1','review_card_sha256':br.card_fingerprint({**card,'summary':'原先经历'})}}
    with pytest.raises(SourcingError):await adapter.prepare(case,candidate,{'text':'已批准正文'})


async def test_open_reviewed_profile_reuses_the_same_page_and_checks_overview(monkeypatch):
    page=NS(url=br.RECOMMEND_URL)
    session=NS(page=page,_boss_recommend_detail={'page':page,'geek_id':'g-1','overview':'原经历概览'})
    async def verify(*a):return 'verified'
    async def cards(*a):return [{'geek_id':'g-1','platform_label':'隐名','contact_label':'打招呼'}]
    async def overview(*a):return '原经历概览'
    async def button(*a,**kw):return NS()
    monkeypatch.setattr(br,'verify_list',verify);monkeypatch.setattr(br,'read_cards',cards)
    monkeypatch.setattr(br,'profile_overview',overview);monkeypatch.setattr(br,'_main_button',button)
    assert (await br.open_profile(session,'g-1','job-1','交付顾问',reload_case={}))['geek_id']=='g-1'
    async def changed(*a):return '另一个人的经历概览'
    monkeypatch.setattr(br,'profile_overview',changed)
    with pytest.raises(SourcingError):await br.open_profile(session,'g-1','job-1','交付顾问',reload_case={})


@pytest.mark.parametrize('prior_analysis',[False,True])
async def test_review_keeps_profile_open_until_a_current_draft_exists(tmp_path,monkeypatch,prior_analysis):
    from cloakup.sourcing import CaseStore
    import cloakup.boss_contact as bc
    store=CaseStore(tmp_path)
    case=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=job-1')
    store.bind_job(case,{'title':'交付顾问','requirements':'五年招聘经验'})
    card={'geek_id':'g-1','platform_label':'隐名','summary':'招聘经历','contact_label':'打招呼'}
    prior_draft=None
    if prior_analysis:
        store.add_candidate(case,'boss-geek:g-1',br.RECOMMEND_URL,{
            **card,'display_name':'已审阅资料','job_id':'job-1',
            'review_card_sha256':br.card_fingerprint(card),'summary':'已复核的匿名事实'})
        prior_draft=store.draft(case,'boss-geek:g-1','first_contact','已审阅正文')
    questions=[];observations=[]
    class Manager:
        cases=store
        def persist(self,*a):pass
        async def question(self,job,kind,prompt):
            questions.append(job['recommend_stage'])
            if len(questions)==1:return 'g-1'
            if prior_draft:return prior_draft['id']
            latest=store.load(case['id'])
            return store.draft(latest,job['params']['candidate_id'],'first_contact','已审阅正文')['id']
    class Session:
        async def observe(self,*a,**kw):observations.append(True)
    async def profile(*a,**kw):return card
    async def contact(manager,job,session):
        latest=store.load(case['id'])
        c=store.candidate(latest,job['params']['candidate_id'])
        assert c['profile']['review_card_sha256']==br.card_fingerprint(card)
        assert store.preview(latest,c['id'],job['params']['message_id'])['text']=='已审阅正文'
        if prior_analysis:
            assert c['profile']['summary']=='已复核的匿名事实'
            assert c['outbox'][0]['status']=='draft'
        return {'status':'cancelled','summary':'等待真实逐条确认'}
    monkeypatch.setattr(br,'open_profile',profile);monkeypatch.setattr(bc,'run_first_contact',contact)
    job={'params':{'case_id':case['id']}}
    result=await br.run_recommend_review(Manager(),job,Session(),case,[card])
    assert questions==['choose_candidate','draft_preparation'] and observations==[True]
    assert result['status']=='cancelled' and job['recommend_stage']=='contact'


async def test_dispatch_reuses_verified_text_without_navigation_and_rejects_expiry():
    import time,hashlib
    text='已批准正文'
    proof={'job_title':'交付顾问','job_id':'job-1','text_sha256':hashlib.sha256(text.encode()).hexdigest(),
           'checked_monotonic':time.monotonic(),'checked_at':time.time(),'action':'reused'}
    session=NS(_boss_greeting=proof)
    adapter=br.BossRecommendContact(session)
    adapter.case={'job':{'url':'https://www.zhipin.com/web/chat/job/edit?encryptId=job-1','label':'交付顾问'}}
    assert (await adapter.configure('交付顾问',text))['text_sha256']==proof['text_sha256']
    with pytest.raises(SourcingError):await adapter.configure('交付顾问','不同正文')
    proof['checked_monotonic']=time.monotonic()-br.GREETING_TTL-1
    with pytest.raises(SourcingError):await adapter.configure('交付顾问',text)


async def test_review_retry_preserves_the_current_recommendation_list(monkeypatch):
    page=NS(url=br.RECOMMEND_URL)
    session=NS(page=page,_boss_recommend_list={'page':page,'job_id':'job-1','filters':['本科']})
    async def must_not_reload(*args):pytest.fail('Retry must not replace the recommendation list')
    async def verify(*args):return 'verified'
    async def cards(*args):return []
    monkeypatch.setattr(br,'open_list',must_not_reload);monkeypatch.setattr(br,'verify_list',verify);monkeypatch.setattr(br,'read_cards',cards)
    with pytest.raises(SourcingError,match='未找到已核验候选人'):
        await br.open_profile(session,'target','job-1','交付顾问',reload_case={'recommend_filters':['本科']})


@pytest.mark.parametrize('texts,closed',[(['推荐列表'],True),(['经历概览 …','推荐列表'],True),(['经历概览 …']*3,False)])
async def test_close_profile_confirms_dialog_is_gone_before_continuing(monkeypatch,texts,closed):
    # 按 Esc 关闭资料弹层；只有主页面不再出现“经历概览”才视为关闭，否则交由调用方重新进入列表。
    keys=[];reads=iter(texts)
    async def evaluate(expr):return next(reads)
    async def press(key):keys.append(key)
    async def no_sleep(seconds):pass
    session=NS(page=NS(_stealth_world=NS(evaluate=evaluate)),press_key=press,_boss_recommend_detail={'geek_id':'g'})
    monkeypatch.setattr(br.asyncio,'sleep',no_sleep)
    assert await br.close_profile(session) is closed
    assert keys==['Escape'] and (session._boss_recommend_detail is None)==closed


@pytest.mark.parametrize('buttons',[[],[('继续沟通','')],[('打招呼',''),('打招呼','')]])
async def test_profile_without_one_greet_button_sets_aside_only_this_candidate(monkeypatch,buttons):
    # 11-24 14:12／15:21／15:37：同一位的资料弹层没出现唯一的“打招呼”，整批开聊被暂缓 15 分钟后又从这一位开始，
    # 下午一个招呼都没发出去。弹层异常只属于这一位：放下这一位（ProfileChanged），并记下看到了哪些按钮。
    from cloakup.sourcing import SourcingError
    logged=[]
    obs=NS(url=br.RECOMMEND_URL,elements=[NS(tag='button',label=label,raw={'frame_url':frame}) for label,frame in buttons],raw={})
    async def observe(session):return obs
    async def no_sleep(*a):pass
    monkeypatch.setattr(br,'_observation',observe);monkeypatch.setattr(br.asyncio,'sleep',no_sleep)
    monkeypatch.setattr('cloakup.boss_contact._observation',observe);monkeypatch.setattr('cloakup.boss_contact.asyncio.sleep',no_sleep)
    monkeypatch.setattr(br,'log',lambda category,**f:logged.append(f))
    with pytest.raises(br.ProfileChanged,match='打招呼') as caught:
        await br._profile_button(NS(),'g-1')
    assert isinstance(caught.value,SourcingError)
    assert logged and logged[0]['type']=='profile_button_missing' and logged[0]['buttons']==[label for label,_ in buttons]


# 11-25 13:31 只读诊断：推荐页岗位下拉为 .job-selecter-wrap 内 li.job-item，value=encryptJobId；frame URL 的 jobid 同一个 id。
class SwitchSession:
    def __init__(self,current='job-AAAAAA',reloads_to=None,option_count=1,cards_after=None):
        self.page=NS(frames=[NS(url='https://www.zhipin.com/web/frame/recommend/?jobid='+current+'&status=0')])
        self.clicks=[];self.reloads_to=reloads_to;self.option_count=option_count
        self.cards=['g-old'];self.cards_after=['g-new'] if cards_after is None else cards_after


@pytest.fixture
def switch_world(monkeypatch):
    async def frame_click(session,frame,css):
        session.clicks.append(css)
        if 'li.job-item' in css and session.reloads_to:
            session.page.frames=[NS(url='https://www.zhipin.com/web/frame/recommend/?jobid='+session.reloads_to+'&status=0')];session.cards=session.cards_after
    async def value(session,fragment,expr):
        if expr==br.CARD_IDS:return session.cards
        assert 'querySelectorAll' in expr and 'job-BBBBBB' in expr
        return session.option_count
    async def no_sleep(_):return None
    monkeypatch.setattr(br,'_frame_click',frame_click);monkeypatch.setattr(br,'_frame_value',value);monkeypatch.setattr(br.asyncio,'sleep',no_sleep)


async def test_switch_skipped_when_already_on_job(switch_world):
    s=SwitchSession(current='job-BBBBBB')
    assert await br.switch_job(s,'job-BBBBBB','岗位乙') is False and s.clicks==[]


async def test_switch_opens_dropdown_and_picks_option_by_job_id(switch_world):
    s=SwitchSession(reloads_to='job-BBBBBB')
    assert await br.switch_job(s,'job-BBBBBB','岗位乙') is True
    assert s.clicks==[br.SWITCH_LABEL,'.job-selecter-wrap li.job-item[value="job-BBBBBB"]']


@pytest.mark.parametrize('kw',[{'reloads_to':'job-AAAAAA'},{'reloads_to':None},{'reloads_to':'job-BBBBBB','cards_after':['g-old']},{'reloads_to':'job-BBBBBB','option_count':0},{'reloads_to':'job-BBBBBB','option_count':2}])
async def test_switch_failure_defers_only_that_job(switch_world,kw):
    s=SwitchSession(**kw)
    with pytest.raises(SourcingError,match='推荐页面未能切换到当前岗位') as caught:await br.switch_job(s,'job-BBBBBB','岗位乙')
    # 不含页面级关键词，autopilot 只暂缓这个岗位的首聊，任务不暂停
    assert not any(t in str(caught.value) for t in ('登录','校验','验证','过于频繁','账号存在异常'))
    if kw.get('option_count') in (0,2):assert s.clicks==[br.SWITCH_LABEL]


async def test_switch_rejects_unexpected_job_id_without_clicking(switch_world):
    s=SwitchSession()
    with pytest.raises(SourcingError,match='推荐页面未能切换到当前岗位'):await br.switch_job(s,'x"]),li[value','岗位乙')
    assert s.clicks==[]


async def test_switch_click_problem_becomes_job_deferral(switch_world,monkeypatch):
    async def blocked(session,frame,css):raise SourcingError('内嵌控件被遮挡或不在可见区域；未点击')
    monkeypatch.setattr(br,'_frame_click',blocked)
    with pytest.raises(SourcingError,match='推荐页面未能切换到当前岗位（内嵌控件被遮挡'):await br.switch_job(SwitchSession(),'job-BBBBBB','岗位乙')


async def test_open_list_switches_to_target_job(monkeypatch):
    from cloakup import page_health
    order=[]
    async def heal(session,kind):return {'healthy':True}
    async def navigate(url):order.append('navigate')
    async def click(session,label,**kw):order.append('tab:'+label)
    async def switch(session,job_id,title):order.append(('switch',job_id,title))
    async def verify(session,job_id,title):order.append(('verify',job_id))
    async def cards(session):return []
    monkeypatch.setattr(page_health,'heal',heal);monkeypatch.setattr(br,'_click',click);monkeypatch.setattr(br,'switch_job',switch)
    monkeypatch.setattr(br,'verify_list',verify);monkeypatch.setattr(br,'read_cards',cards)
    session=NS(max_elements=0,navigate=navigate,page=None)
    case={'job':{'label':'岗位乙','url':'https://www.zhipin.com/job_detail/x.html?encryptId=job-BBBBBB'},'recommend_filters':[]}
    monkeypatch.setattr(br,'job_identity',lambda c:'job-BBBBBB')
    await br.open_list(session,case)
    assert order[:4]==['navigate','tab:推荐',('switch','job-BBBBBB','岗位乙'),('verify','job-BBBBBB')]


async def test_switch_waits_for_card_list_to_change_after_url(switch_world):
    # URL 先变成目标岗位、卡片还是旧岗位的：不能放行
    with pytest.raises(SourcingError,match='推荐列表未刷新'):await br.switch_job(SwitchSession(reloads_to='job-BBBBBB',cards_after=['g-old']),'job-BBBBBB','岗位乙')
    assert await br.switch_job(SwitchSession(reloads_to='job-BBBBBB',cards_after=[]),'job-BBBBBB','岗位乙') is True


@pytest.mark.parametrize('exc',[RuntimeError('frame_changed'),TimeoutError()])
async def test_switch_frame_reload_errors_defer_only_that_job(switch_world,monkeypatch,exc):
    async def broken(session,frame,css):raise exc
    monkeypatch.setattr(br,'_frame_click',broken)
    with pytest.raises(SourcingError,match='推荐页面未能切换到当前岗位') as caught:await br.switch_job(SwitchSession(),'job-BBBBBB','岗位乙')
    assert not any(t in str(caught.value) for t in ('登录','校验','验证','过于频繁','账号存在异常'))


async def test_switch_without_recommend_frame_defers(switch_world):
    s=SwitchSession();s.page.frames=[]
    with pytest.raises(SourcingError,match='推荐页面尚未加载'):await br.switch_job(s,'job-BBBBBB','岗位乙')
    assert s.clicks==[]


async def test_open_list_goes_on_when_the_platform_greeting_cannot_be_reread(monkeypatch):
    # 11-26 管理员决定“照写不拦”：复核 BOSS 岗位话术失败不再挡住推荐页。
    from cloakup import page_health
    async def heal(session,kind):return {'healthy':True}
    async def navigate(url):pass
    async def noop(*a,**kw):pass
    async def cards(session):return []
    async def unreadable(*a):raise br.GreetingVerificationError('未找到唯一的本岗位已保存话术；本次不会覆盖或开聊')
    monkeypatch.setattr(page_health,'heal',heal);monkeypatch.setattr(br,'_click',noop);monkeypatch.setattr(br,'switch_job',noop)
    monkeypatch.setattr(br,'verify_list',noop);monkeypatch.setattr(br,'read_cards',cards);monkeypatch.setattr(br,'existing_greeting',unreadable)
    monkeypatch.setattr(br,'job_identity',lambda c:'job-1')
    session=NS(max_elements=0,navigate=navigate,page=None,_boss_greeting=None)
    case={'job':{'label':'岗位甲','url':'https://www.zhipin.com/job_detail/x.html?encryptId=job-1'},'recommend_filters':[],'platform_greeting_text':'旧话术'}
    assert await br.open_list(session,case)==[] and session._boss_greeting is None


async def test_automatic_contact_does_not_require_a_greeting_proof(monkeypatch):
    # 11-26：自动首聊不再要求岗位话术核对凭证；BOSS 发出的是该岗位现有招呼语。人工审阅开聊仍要求凭证。
    card={'geek_id':'g-1','platform_label':'隐名','summary':'经历'}
    async def profile(*a,**kw):return card
    monkeypatch.setattr(br,'open_profile',profile)
    case={'job':{'url':'https://www.zhipin.com/web/chat/job/edit?encryptId=job-1','label':'交付顾问'}}
    candidate={'profile':{'geek_id':'g-1','review_card_sha256':br.card_fingerprint(card)}}
    strict=br.BossRecommendContact(NS(_boss_greeting=None))
    with pytest.raises(SourcingError):await strict.prepare(case,candidate,{'text':'任务问候语'})
    adapter=br.BossRecommendContact(NS(_boss_greeting=None),require_greeting_proof=False)
    proof=await adapter.prepare(case,candidate,{'text':'任务问候语'})
    assert proof['geek_id']=='g-1' and proof['greeting_action']=='platform'
    assert await adapter.configure('交付顾问','任务问候语')=={}
