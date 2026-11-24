import copy
import json
from types import SimpleNamespace as NS

import pytest

from cloakup import conversation_semantics as cs, decision_runtime as dr
from cloakup.sourcing import SourcingError


@pytest.fixture
def example():
    case={'id':'case','config_revision':2,'job':{'revision':1},'anonymous_job':{'role':'招聘顾问','requirements':['可远程协作。','负责人才寻访与交付。']},
          'faq':[{'enabled':True,'question':'薪酬结构','answer':'固定底薪加绩效工资和提成，整体方案合理、有竞争力。'}]}
    person={'id':'private','profile':{'platform_label':'张某','summary':'张某，某某有限公司'},'conversation':{'receiver':'张某','chat_id':'123-0'},
            'inbox':[{'external_id':'m1','text':'Boss，您好，可以聊聊吗？'}], 'history':[{'external_id':'m1','direction':'in','text':'Boss，您好，可以聊聊吗？'}],
            'resumes':[],'outbox':[],'platform_actions':[]}
    return case,person


def proposal(**kw):
    return dict(route='continue',reason='对方只是邀请交流，应正常承接并推进',parts=['welcome','introduce','fact:legacy-1-duties','resume'],grounding_ids=['fact:legacy-1-duties'],candidate_question='',missing_information=[],resume_readiness='ready',**kw)


def test_model_sees_ordered_anonymous_dialogue_faq_and_sources(example):
    case,p=example
    p['history']=[{'direction':'out','text':'可以远程协作。'},{'direction':'in','text':'那我人在外地也能做对吧？'}]
    p['inbox'][-1]['text']=p['history'][-1]['text']
    data=cs.model_input(case,p,None)
    assert data['conversation']==[{'role':'consultant','text':'可以远程协作。'},{'role':'candidate','text':'那我人在外地也能做对吧？'}]
    assert data['sources']['faq:0']['question']=='薪酬结构'
    wire=json.dumps(data,ensure_ascii=False)
    assert all(value not in wire for value in ('张某','123-0','某某有限公司','private'))


def test_no_conversation_or_attachment_is_disclosed_without_authorization(example):
    assert not cs.consent(NS())
    c,p=example
    p['history'].insert(0,{'direction':'in','text':'PRIVATE_RESUME_BODY','resume':True})
    assert 'PRIVATE_RESUME_BODY' not in json.dumps(cs.model_input(c,p,None))


def test_redaction_removes_identity_and_contacts(example):
    _,p=example
    text=cs.redact('我叫张某，22岁，女，微信:abc123456，电话13812345678，邮箱 a@b.cn，可以聊聊吗？',p)
    assert all(value not in text for value in ('张某','22岁','女','abc123456','13812345678','a@b.cn'))
    assert '可以聊聊吗' in text
    assert cs.redact('李女士介绍来的',p)=='[称呼]介绍来的'
    assert 'Michael' not in cs.redact('联系人 Michael',p)


def test_redaction_keeps_employer_school_city_and_never_refuses(example):
    # 2026-11-24 管理员确认：只隐去身份与联系方式；单位、学校、城市保留，脱敏不再整段拒绝。
    _,p=example
    for text in ('我在武汉，这个工作具体地点是在哪里？','我之前在腾讯做招聘，北京大学毕业','某某科技有限公司，社保怎么交？',
                 '我会 Python 和 Excel','email 尚未分类的 Latin entity','男女都可以吗'):
        clean=cs.redact(text,p)
        assert isinstance(clean,str) and clean
    assert cs.redact('我在武汉，这个工作具体地点是在哪里？',p)=='我在武汉，这个工作具体地点是在哪里？'
    assert '腾讯' in cs.redact('我之前在腾讯做招聘',p)
    assert len(cs.redact('长'*3000,p))==1600


@pytest.mark.parametrize('text',['汤女士简历.pdf\n点击预览附件简历','MAR-wuhan.doc\n点击预览附件简历','Zhang_San_CV.docx'])
def test_attachment_file_names_never_reach_the_model(example,text):
    # 11-24 候选人 20 的附件名 MAR-wuhan.doc 被当作未知英文实体整段拒绝。
    _,p=example
    clean=cs.redact(text,p)
    assert '[附件]' in clean and not any(part in clean for part in ('汤','MAR','wuhan','Zhang','.pdf','.doc'))


def test_harness_rendered_response_cannot_be_overwritten_by_legacy_question_route(example):
    case,p=example;p['inbox'][-1]['text']='您这边可以先聊两句不？'
    data=cs.model_input(case,p,None);answer=cs.validate(proposal(),data)
    p['conversation_semantics']={'state_key':cs.fingerprint(case,p),'answer':answer}
    result=dr.effective_answer(case,p)
    assert result['source']=='harness_context' and result['action']=='answer'
    assert result['text']=='您好，可以的。我先和您介绍一下这个岗位。负责人才寻访与交付。方便发一份最新简历吗？我结合您的经历和您进一步聊聊。'
    assert '核实' not in result['text']
    changed=copy.deepcopy(case);changed['faq'][0]['answer']='新口径'
    assert cs.current(changed,p) is None
    p['inbox'].append({'external_id':'new','text':'稍后聊'})
    assert cs.current(case,p) is None


@pytest.mark.parametrize('patch',[
    {'route':'defer','parts':['confirm_missing'],'grounding_ids':[]},
    {'parts':['confirm_missing'],'grounding_ids':[]},
    {'route':'answer','parts':['welcome'],'grounding_ids':[]},
    {'grounding_ids':['fact:nonexistent']},
    {'route':'clarify','parts':['clarify','resume'],'grounding_ids':[]},
])
def test_missing_facts_and_unsupported_claims_require_evidence(example,patch):
    case,p=example;value=proposal();value.update(patch)
    with pytest.raises(SourcingError):cs.validate(value,cs.model_input(case,p,None))


def test_mixed_question_can_answer_known_part_and_defer_specific_unknown(example):
    case,p=example;data=cs.model_input(case,p,None)
    value=proposal();value.update(route='defer',parts=['fact:legacy-0-work_arrangement','confirm_missing'],grounding_ids=['fact:legacy-0-work_arrangement'],candidate_question='地点和社保怎么安排？',missing_information=['社保缴纳安排'])
    result=cs.validate(value,data)
    assert result['text']=='可远程协作。这部分我确认清楚后再回复您。'
    assert result['missing_information']==['社保缴纳安排']


async def test_model_failure_has_no_template_fallback_and_input_uses_harness(example,monkeypatch,tmp_path):
    case,p=example
    import cloakup.harness_runtime as hr
    import cloakup.llm as lm
    seen=[]
    class Engine:
        last_trace={'runtime':'deepseek-harness','version':'test'}
        async def json(self,**kwargs):
            seen.append(json.loads(kwargs['content']))
            return proposal()
    monkeypatch.setattr(hr,'get_harness',lambda *a:Engine())
    monkeypatch.setattr(lm,'LLM',lambda client:NS(client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))))
    manager=NS(db=NS(get=lambda *a:{'enabled':True,'mode':'anonymous_conversation','host':cs.APPROVED_HOST}),root=tmp_path,model_client=lambda:None)
    result=await cs.ensure(manager,case,p,None)
    assert result['harness_trace']['runtime']=='deepseek-harness'
    assert seen[0]['conversation'][0]['text']=='Boss，您好，可以聊聊吗？'
    class Broken(Engine):
        async def json(self,**kwargs):raise RuntimeError('gateway down')
    monkeypatch.setattr(hr,'get_harness',lambda *a:Broken())
    with pytest.raises(RuntimeError):await cs.ensure(manager,case,p,None)
