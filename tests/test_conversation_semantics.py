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


def test_redact_respects_limit(example):
    # 11-27 联系前筛选：在线简历正文较长，需按 6000 字上限脱敏；默认 1600 不变。
    _,p=example;t='长'*3000
    assert len(cs.redact(t,p))<=1600
    assert cs.redact(t,p,limit=6000)==t


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


QE_JD='我们提供 1、8-12K＋绩效奖金，具体结构面议。 2、直接向品质负责人汇报，体系优化有实际决策空间。'


def _with_jd(case,jd=QE_JD,location='深圳宝安区深圳西部硅谷-B座六楼'):
    case['job']={'label':'qe工程师','revision':2,'snapshot':{'company':'君润人力','location':location,'requirements':jd}}
    return case


def test_catalog_offers_jd_statements_after_facts_and_faq(example):
    # 11-26 16:04 qe工程师 候选人问汇报对象：JD 有答案却转了顾问。
    case,_=example;catalog=cs.source_catalog(_with_jd(case))
    jd={k:v for k,v in catalog.items() if k.startswith('jd:')}
    assert '直接向品质负责人汇报，体系优化有实际决策空间。' in [v['text'] for v in jd.values()]
    assert all(v['kind']=='jd' and v['revision']==2 for v in jd.values())
    assert not any('12K' in v['text'] for v in catalog.values())
    assert list(catalog).index('faq:0')<min(list(catalog).index(k) for k in jd)


def test_answer_grounded_on_jd_statement_is_valid(example):
    case,p=example;_with_jd(case)
    msg={'external_id':'m2','direction':'in','text':'您好，这个职位汇报对象是谁'}
    p['history']=[msg];p['inbox']=[dict(msg)]
    data=cs.model_input(case,p,None)
    ident=next(k for k,v in data['sources'].items() if v['text'].startswith('直接向品质负责人汇报'))
    out=cs.validate(dict(route='answer',reason='JD 写明汇报对象',parts=[ident],grounding_ids=[ident],candidate_question='',missing_information=[],resume_readiness='ready'),data)
    assert out['action']=='answer' and out['text']=='直接向品质负责人汇报，体系优化有实际决策空间。'


def test_catalog_drops_jd_line_that_contradicts_confirmed_fact(example):
    case,_=example;_with_jd(case,jd='1、工作地点在武汉东西湖区。 2、3年以上质量工作经验，5年以上优先。',location='武汉东西湖区')
    case['confirmed_facts']=[{'id':'loc','kind':'work_arrangement','scope':'job','value':'工作地点：深圳宝安区','status':'confirmed'},
                             {'id':'exp','kind':'experience','scope':'job','value':'经验要求：3-5年','status':'confirmed'}]
    texts=[v['text'] for k,v in cs.source_catalog(case).items() if k.startswith('jd:')]
    assert not any('武汉' in t for t in texts)
    assert '3年以上质量工作经验，5年以上优先。' in texts


def test_catalog_without_jd_is_unchanged(example):
    case,_=example
    assert not any(k.startswith('jd:') for k in cs.source_catalog(case))
    case['job']={'revision':1,'snapshot':{'requirements':''}}
    assert not any(k.startswith('jd:') for k in cs.source_catalog(case))


def test_semantic_version_and_prompt_cover_jd():
    assert cs.VERSION==4
    assert 'JD' in cs.SYSTEM and '已确认事实' in cs.SYSTEM


def _confirmed(case,**kinds):
    case['confirmed_facts']=[{'id':k,'kind':k,'scope':'job','value':v,'status':'confirmed'} for k,v in kinds.items()]
    return [v['text'] for k,v in cs.source_catalog(case).items() if k.startswith('jd:')]


@pytest.mark.parametrize('line',['工作地点：武汉','办公地点：武汉光谷','base武汉东西湖区，常驻'])
def test_catalog_drops_jd_city_without_district_suffix(example,line):
    # 11-26 终审 C2：JD 城市不带“区／市”也要与已确认地点比对。
    case,_=example;_with_jd(case,jd='1、'+line+'。 2、工作地点在深圳宝安。',location='')
    texts=_confirmed(case,work_arrangement='工作地点：深圳宝安区')
    assert texts==[]


@pytest.mark.parametrize('confirmed,line,kept',[
    ({'experience':'经验要求：10年以上'},'1年以上相关工作经验',False),
    ({'experience':'经验要求：3-5年'},'经验不限',False),
    ({'education':'学历要求：本科'},'学历不限',False),
    ({'experience':'经验要求：3-5年'},'3年以上经验，熟悉ISO9001体系',True),
])
def test_catalog_compares_experience_and_education_as_whole_values(example,confirmed,line,kept):
    # 11-26 终审 I5：按整数和“不限”比对，不按子串。
    case,_=example;_with_jd(case,jd='1、'+line+'。',location='')
    assert ((line+'。') in _confirmed(case,**confirmed))==kept


def test_catalog_contradiction_rules_from_live_rehearsal(example):
    # 11-26 只读演练：高端已确认“至少五年”与 JD“5年”不矛盾；FDE 已确认“深圳、武汉、杭州”含武汉；远程岗位不报 BOSS 地址。
    case,_=example;_with_jd(case,jd='1、要求至少5年从业经验。 2、工作地点：武汉东西湖区。',location='')
    # 11-26 16:43 隔离验收 FDE known_fact：已确认“深圳、武汉、杭州”与 JD“武汉东西湖区”并存，模型判为口径不一致转人工；有已确认地点时 JD 地点句一律不进目录。
    assert _confirmed(case,experience='招聘或猎头从业经验至少五年',work_arrangement='工作地点：深圳、武汉、杭州')==['要求至少5年从业经验。']
    case,_=example;_with_jd(case,jd='1、负责人才寻访。',location='武汉东西湖区某路')
    assert _confirmed(case,work_arrangement='远程协作，地区不限，华中区域优先。')==['负责人才寻访。']


def test_jd_review_lists_usable_ids_and_blocked_reasons(example):
    # 11-26 管理员问“基于 JD 的岗位事实从哪看到”：页面需要逐句列出可引用（带来源编号）与被拦原因。
    case,_=example;_with_jd(case,jd='1、8-12K＋绩效奖金。 2、直接向品质负责人汇报。 3、客户为某某科技有限公司。 4、工作地点在武汉。',location='')
    case['confirmed_facts']=[{'id':'loc','kind':'work_arrangement','scope':'job','value':'工作地点：深圳宝安区','status':'confirmed'}]
    review=cs.jd_review(case)
    usable=[r for r in review if r['id']]
    assert [(r['id'],r['text']) for r in usable]==[(k,v['text']) for k,v in cs.source_catalog(case).items() if k.startswith('jd:')]
    assert ('直接向品质负责人汇报。',None) in [(r['text'],r['reason']) for r in usable]
    blocked={r['text']:r['reason'] for r in review if not r['id']}
    assert blocked=={'8-12K＋绩效奖金':'含薪酬数字','客户为某某科技有限公司':'含公司或机构名','工作地点在武汉。':'已有确认地点，以已确认事实为准'}
