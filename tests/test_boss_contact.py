import asyncio
from types import SimpleNamespace as NS
import pytest
import cloakup.boss_contact as bc
from cloakup.sourcing import CaseStore, SourcingError


@pytest.fixture
def setup_case(tmp_path):
    store=CaseStore(tmp_path)
    case=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=job-1')
    store.bind_job(case,{'title':'交付顾问','requirements':'五年经验'})
    c=store.add_candidate(case,'candidate-1','https://www.zhipin.com/web/chat/search',{'display_name':'候选人 A','availability':'在职','expect_id':'expect-1','job_id':'job-1'})
    m=store.draft(case,c['id'],'first_contact','您好，方便了解高端猎聘交付方向吗？如暂不考虑，不再打扰。')
    job={'params':{'case_id':case['id'],'candidate_id':c['id'],'message_id':m['id']}}
    return store,case,c,m,job


class Manager:
    def __init__(self,store,answer=True,on_question=None): self.cases=store;self.answer=answer;self.on_question=on_question;self.questions=[]
    def event(self,*a): pass
    def persist(self,*a): pass
    async def question(self,job,kind,prompt):
        self.questions.append(prompt)
        if self.on_question:self.on_question()
        return self.answer


def adapter(monkeypatch,store,case,c,m,*,receipt=True,cancel=False,preflight_error=False):
    calls=[]
    class Adapter:
        async def prepare(self,case,candidate,message):
            calls.append('prepare')
            if preflight_error:raise SourcingError('目标不匹配')
            return {'expect_id':'expect-1','job_id':'job-1','receiver_label':'隐名'}
        async def configure(self,title,text):
            calls.append(('configure',title,text))
            assert store.load(case['id'])['candidates'][c['id']]['outbox'][0]['status']=='approved'
            return {'saved':True}
        async def candidate(self,*a): return {'expect_id':'expect-1','job_id':'job-1','receiver_label':'隐名'}
        async def contact_once(self,proof):
            calls.append('contact')
            assert store.load(case['id'])['candidates'][c['id']]['outbox'][0]['status']=='sending'
            if cancel:raise asyncio.CancelledError()
        async def receipt(self,*a):return {'evidence':'真实会话核验'} if receipt else None
    monkeypatch.setattr(bc,'BossFirstContact',lambda session:Adapter())
    return calls


async def test_decline_has_no_platform_writes_or_delivery(setup_case,monkeypatch):
    store,case,c,m,job=setup_case;calls=adapter(monkeypatch,store,case,c,m)
    result=await bc.run_first_contact(Manager(store,False),job,object())
    assert result['status']=='cancelled' and calls==['prepare']
    assert store.load(case['id'])['candidates'][c['id']]['outbox'][0]['status']=='draft'


async def test_approved_exact_message_is_claimed_before_contact(setup_case,monkeypatch):
    store,case,c,m,job=setup_case;calls=adapter(monkeypatch,store,case,c,m)
    manager=Manager(store)
    result=await bc.run_first_contact(manager,job,object())
    assert result['status']=='succeeded'
    assert calls==['prepare',('configure','交付顾问',m['text']),'contact']
    assert m['text'] in manager.questions[0] and '仅向' in manager.questions[0]
    latest=store.load(case['id'])['candidates'][c['id']]
    assert latest['status']=='waiting_reply' and latest['outbox'][0]['status']=='sent'


async def test_new_reply_during_review_prevents_platform_write(setup_case,monkeypatch):
    store,case,c,m,job=setup_case;calls=adapter(monkeypatch,store,case,c,m)
    manager=Manager(store,on_question=lambda:store.receive(case,c['id'],'reply-1','暂不考虑'))
    with pytest.raises(SourcingError):await bc.run_first_contact(manager,job,object())
    assert calls==['prepare']


@pytest.mark.parametrize('cancel',[False,True])
async def test_uncertain_delivery_never_becomes_sent_or_auto_retried(setup_case,monkeypatch,cancel):
    store,case,c,m,job=setup_case;calls=adapter(monkeypatch,store,case,c,m,receipt=False,cancel=cancel)
    if cancel:
        with pytest.raises(asyncio.CancelledError):await bc.run_first_contact(Manager(store),job,object())
    else:
        assert (await bc.run_first_contact(Manager(store),job,object()))['status']=='failed'
    latest=store.load(case['id']);candidate=latest['candidates'][c['id']]
    assert candidate['status']=='delivery_unknown'
    assert candidate['outbox'][0]['status']=='delivery_unknown'
    assert calls.count('contact')==1
    with pytest.raises(SourcingError):store.claim_send(latest,c['id'],m['id'])


async def test_preflight_mismatch_cannot_approve_or_mutate(setup_case,monkeypatch):
    store,case,c,m,job=setup_case;calls=adapter(monkeypatch,store,case,c,m,preflight_error=True)
    manager=Manager(store)
    with pytest.raises(SourcingError):await bc.run_first_contact(manager,job,object())
    assert calls==['prepare'] and not manager.questions


async def test_existing_approval_is_not_requested_again(setup_case,monkeypatch):
    store,case,c,m,job=setup_case
    store.approve(case,c['id'],m['id'],store.preview(case,c['id'],m['id'])['fingerprint'])
    adapter(monkeypatch,store,case,c,m)
    manager=Manager(store)
    assert (await bc.run_first_contact(manager,job,object()))['status']=='succeeded'
    assert manager.questions==[]


def test_platform_message_length_is_checked_without_truncation():
    assert bc.valid_message('字'*100)=='字'*100
    for value in ('', ' '*10, '字'*101, None):
        with pytest.raises(SourcingError):bc.valid_message(value)


async def test_click_error_is_retained_without_a_second_click(monkeypatch):
    async def find(*args, **kwargs): return NS(index=9), None
    monkeypatch.setattr(bc, '_find', find)
    clicks=[]
    class Session:
        async def click(self, index):
            clicks.append(index)
            raise RuntimeError('target detached after click')
    result=await bc.BossFirstContact(Session()).contact_once({})
    assert clicks==[9]
    assert result['click_completed'] is False
    assert 'target detached' in result['error']


async def test_unknown_delivery_retains_local_page_before_closing(setup_case,monkeypatch):
    store,case,c,m,job=setup_case
    calls=adapter(monkeypatch,store,case,c,m,receipt=False)
    observations=[]
    class Session:
        async def observe(self,screenshot,text_limit): observations.append((screenshot,text_limit))
    result=await bc.run_first_contact(Manager(store),job,Session())
    assert result['status']=='failed' and calls.count('contact')==1
    assert observations==[(True,18000)]
    assert store.load(case['id'])['candidates'][c['id']]['status']=='delivery_unknown'


async def test_recommendation_preview_is_not_a_delivery_receipt(monkeypatch):
    evaluations=[]
    async def evaluate(*args):evaluations.append(args);return '隐名 已批准正文'
    async def sleep(*args):pass
    monkeypatch.setattr(bc.asyncio,'sleep',sleep)
    session=NS(page=NS(frames=[NS(url='https://www.zhipin.com/web/chat/recommend')],
        _stealth_world=NS(_human=NS(worlds=NS(evaluate=evaluate)))))
    assert await bc.BossFirstContact(session).receipt({'receiver_label':'隐名'},'已批准正文') is None
    assert not evaluations


async def test_login_form_is_reported_without_contact_or_screenshot():
    calls=[]
    class Session:
        async def observe(self,screenshot,**kwargs):
            calls.append(screenshot)
            return NS(url='https://www.zhipin.com/web/user/?ka=bticket',
                      elements=[NS(tag='button',label='登录/注册')])
    with pytest.raises(SourcingError,match='登录表单'):await bc._observation(Session())
    assert calls==[False]


async def test_read_only_issue_preserves_browser_and_retry_still_needs_send_confirmation(setup_case,monkeypatch):
    store,case,c,m,job=setup_case
    calls=[];questions=[]
    class Adapter:
        async def prepare(self,*args):
            calls.append('prepare')
            if len(calls)==1:raise SourcingError('iframe loading')
            return {'receiver_label':'隐名','expect_id':'expect-1','job_id':'job-1'}
        async def configure(self,*args):pytest.fail('No platform write before send confirmation')
    monkeypatch.setattr(bc,'BossFirstContact',lambda session:Adapter())
    manager=Manager(store)
    async def question(job,kind,prompt):
        questions.append(kind)
        return 'retry' if kind=='reply' else False
    manager.question=question
    job['preserve_preflight']=True
    result=await bc.run_first_contact(manager,job,object())
    assert calls==['prepare','prepare'] and questions==['reply','confirm']
    assert result['status']=='cancelled'
    assert store.load(case['id'])['candidates'][c['id']]['outbox'][0]['status']=='draft'


async def test_explicit_autonomous_policy_removes_only_per_message_confirmation(setup_case,monkeypatch):
    store,case,c,m,job=setup_case
    store.set_autonomy(case,True,source='user explicitly authorizes automatic greeting and follow-up')
    calls=adapter(monkeypatch,store,case,c,m)
    manager=Manager(store,False)
    result=await bc.run_first_contact(manager,job,object())
    assert result['status']=='succeeded' and manager.questions==[]
    message=store.load(case['id'])['candidates'][c['id']]['outbox'][0]
    assert message['approval_actor']=='automation' and message['status']=='sent'
    assert 'approved_at' not in message and calls.count('contact')==1
