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
