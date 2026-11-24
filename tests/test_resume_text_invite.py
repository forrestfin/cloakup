import pytest

from cloakup import conversation_semantics as cs
from cloakup.sourcing import SourcingError


@pytest.fixture
def example():
    case={'id':'case','config_revision':2,'job':{'revision':1},'anonymous_job':{'role':'招聘顾问','requirements':['可远程协作。']},'faq':[]}
    person={'id':'private','profile':{'platform_label':'何某','summary':'何某'},'conversation':{'receiver':'何某','chat_id':'9-0'},
            'inbox':[{'external_id':'m1','text':'好的，可以细聊'}],'history':[{'external_id':'m1','direction':'in','text':'好的，可以细聊'}],
            'resumes':[],'outbox':[],'platform_actions':[]}
    return case,person


def positive_reply(**kw):
    # 候选人正向交流、没有新业务问题时，模型选择以简历邀请作为唯一下一步
    return dict(route='continue',reason='候选人表示可以细聊，无新业务问题，文字邀请简历',parts=['welcome','resume'],grounding_ids=[],
                candidate_question='',missing_information=[],resume_readiness='ready',**kw)


def test_pending_platform_resume_request_allows_one_text_invite(example):
    case,p=example
    p['platform_actions']=[{'id':'r1','kind':'ask_resume','status':'awaiting_verification','at':100}]
    data=cs.model_input(case,p,None)
    assert data['state']['resume_text_invite_open'] is True
    out=cs.validate(positive_reply(),data)
    assert out['text']==cs.PHRASES['welcome']+cs.PHRASES['resume']


def test_text_invite_allowed_again_after_previous_invite(example):
    # 放宽后：每条新的正向回复都可以再次文字索要简历，不受同一申请的限制
    case,p=example
    p['platform_actions']=[{'id':'r1','kind':'ask_resume','status':'awaiting_verification','at':100}]
    p['outbox']=[{'status':'sent','claimed_at':200,'text':cs.PHRASES['resume']}]
    data=cs.model_input(case,p,None)
    assert data['state']['resume_text_invite_open'] is True
    assert cs.validate(positive_reply(),data)['text']


def test_other_pending_applications_still_block_resume_text(example):
    case,p=example
    p['platform_actions']=[{'id':'w1','kind':'exchange_wechat','status':'pending','at':100}]
    data=cs.model_input(case,p,None)
    assert data['state']['resume_text_invite_open'] is False
    with pytest.raises(SourcingError):
        cs.validate(positive_reply(),data)


def test_no_pending_request_keeps_normal_resume_flow(example):
    case,p=example
    data=cs.model_input(case,p,None)
    assert data['state']['resume_text_invite_open'] is False
    assert cs.validate(positive_reply(),data)['text']
