import json
from types import SimpleNamespace as NS

import pytest

from cloakup import job_sourcing as js
from cloakup import screening as sc

# 2026-11-27 首聊前初筛：必须项 fixture 可以写岗位词，代码与提示词不行（约束 8）。
MH = [{'id':'mh_a','text':'有 SQE／供应商质量相关工作经历','source':'client_feedback'},
      {'id':'mh_b','text':'第一学历统招大专及以上','source':'client_feedback','note':'客户原话：不要非统招'}]
OVERVIEW = '工作经历\n2018-2024 某制造公司 质量工程师'


class FakeLLM:
    def __init__(self, result=None, error=None, host='ark.cn-beijing.volces.com'):
        self.client = NS(raw=NS(base_url='https://%s/api/v3' % host))
        self.model = 'test-model'
        self.result = result
        self.error = error
        self.prompts = []

    async def json(self, *, system, content, schema, effort='low', structured=True):
        self.prompts.append(content)
        if self.error:
            raise self.error
        return self.result


def fake_llm_with_host(host):
    return FakeLLM({'items':[]}, host=host)


def case_with(items):
    return {'id':'case1','job_sourcing':{'must_haves':items}}


def person_named(name):
    return {'id':'p1','profile':{'display_name':name,'experience':'6年','education':'本科',
                                 'summary':'负责供应商质量','work_history':'2018.07 至今\n某制造公司 SQE'}}


person = person_named('李四')
m = NS()


async def test_unmet_rejects():
    llm = FakeLLM({'items':[{'id':'mh_a','verdict':'unmet','evidence':'只做过来料检验'},{'id':'mh_b','verdict':'met','evidence':'2012-2015 统招大专'}]})
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=llm)
    assert r['outcome']=='reject' and r['revision']==js.must_haves_revision(MH)
    assert r['model']=='test-model' and r['unclear_ids']==[]


async def test_all_unclear_passes():
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_a','verdict':'unclear','evidence':'资料未提及'},{'id':'mh_b','verdict':'unclear','evidence':'资料未提及'}]}))
    assert r['outcome']=='pass' and r['unclear_ids']==['mh_a','mh_b']


async def test_missing_item_counts_as_unclear():
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_a','verdict':'met','evidence':'SQE 3 年'}]}))
    assert r['outcome']=='pass' and r['unclear_ids']==['mh_b']


async def test_unknown_ids_ignored():
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_x','verdict':'unmet','evidence':'-'}]}))
    assert r['outcome']=='pass' and {i['id'] for i in r['items']}=={'mh_a','mh_b'}


def test_payload_has_no_identity():
    p = sc.build_payload(MH, person_named('张三'), '张三 28岁 手机13800138000 微信 abc123\n工作经历 某公司 SQE')
    text = json.dumps(p, ensure_ascii=False)
    assert '张三' not in text and '13800138000' not in text and '28岁' not in text and 'abc123' not in text and 'SQE' in text
    assert 'note' not in text and '客户原话' not in text
    assert p['must_haves']==[{'id':'mh_a','text':MH[0]['text']},{'id':'mh_b','text':MH[1]['text']}]
    assert '6年' in p['profile'] and '某制造公司' in p['profile']


def test_payload_cut_at_newline_so_no_phone_fragment_survives():
    # 先截断再脱敏会让跨截断点的手机号残留半截；截断落在换行处就不会。
    head = 'x' * (sc.MAX_PROFILE_CHARS - 300)
    p = sc.build_payload(MH, {'profile':{}}, head + '\n' + 'y' * 294 + '13800138000\n尾部')  # 硬切会留下“13800”
    assert '13800' not in p['profile'] and p['profile'].endswith('x') and len(p['profile']) <= sc.MAX_PROFILE_CHARS


def test_payload_single_line_cut_at_punctuation():
    # 没有换行时退到最后一个空白或标点处截断，跨 6000 的手机号不留任何数字片段。
    head = 'x' * (sc.MAX_PROFILE_CHARS - 6) + '，'
    p = sc.build_payload(MH, {'profile':{}}, head + '13800138000 尾部')
    assert not any(c.isdigit() for c in p['profile']) and p['profile'].endswith('x')


def test_empty_profile_raises():
    with pytest.raises(sc.ScreeningUnavailable): sc.build_payload(MH, {'profile':{}}, '   ')


async def test_model_error_and_bad_host_raise():
    with pytest.raises(sc.ScreeningUnavailable): await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM(error=RuntimeError()))
    with pytest.raises(sc.ScreeningUnavailable): await sc.screen(m, case_with(MH), person, OVERVIEW, llm=fake_llm_with_host('evil.example.com'))


async def test_bad_host_sends_nothing():
    llm = fake_llm_with_host('evil.example.com')
    with pytest.raises(sc.ScreeningUnavailable): await sc.screen(m, case_with(MH), person, OVERVIEW, llm=llm)
    assert llm.prompts==[]


async def test_bad_schema_raises():
    with pytest.raises(sc.ScreeningUnavailable):
        await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_a','verdict':'maybe','evidence':'-'}]}))


async def test_evidence_with_phone_rejected():
    with pytest.raises(sc.ScreeningUnavailable):
        await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_a','verdict':'met','evidence':'电话13800138000'}]}))


async def test_screen_logs_model_call(monkeypatch):
    calls = []
    monkeypatch.setattr(sc, 'log', lambda category, **fields: calls.append((category, fields)))
    await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[]}))
    assert calls==[('model', {'type':'precontact_screening','case_id':'case1','candidate_id':'p1','outcome':'pass','items':2})]


async def test_split_feedback_returns_checked_drafts():
    llm = FakeLLM({'items':[{'text':'有 SQE／供应商质量相关工作经历'},{'text':'第一学历统招大专及以上'}]})
    out = await sc.split_feedback(m, '客户说必须做过 SQE，第一学历要统招大专', [], llm=llm)
    assert [i['source'] for i in out]==['client_feedback']*2 and out[0]['note'].startswith('客户说')
    assert all(i['id'].startswith('mh_') for i in out)


async def test_split_feedback_skips_existing_and_errors_raise():
    existing = js.checked_must_haves([MH[1]])
    out = await sc.split_feedback(m, '客户反馈', existing, llm=FakeLLM({'items':[{'text':'第一学历统招大专及以上'},{'text':'有海外工作经历'}]}))
    assert [i['text'] for i in out]==['有海外工作经历']
    with pytest.raises(sc.ScreeningUnavailable): await sc.split_feedback(m, '客户反馈', [], llm=FakeLLM(error=RuntimeError()))


async def test_split_feedback_drops_blank_model_texts():
    out = await sc.split_feedback(m, '客户反馈', [], llm=FakeLLM({'items':[{'text':'  '},{'text':'有海外工作经历'}]}))
    assert [i['text'] for i in out]==['有海外工作经历']
    with pytest.raises(sc.ScreeningUnavailable): await sc.split_feedback(m, '客户反馈', [], llm=FakeLLM({'items':[{'text':' '}]}))
    with pytest.raises(sc.ScreeningUnavailable): await sc.split_feedback(m, '客户反馈', [], llm=FakeLLM({'items':[]}))


def test_no_job_words_in_prompt():  # 约束 8：初筛、拆分、打分追加说明与首聊复核说明都不写岗位词
    import inspect
    from cloakup import decision_runtime, resume_scoring
    src = inspect.getsource(decision_runtime)
    note = src[src.index("content['first_contact_checks']"):src.index('return content', src.index("content['first_contact_checks']"))]
    assert not any(w in prompt for prompt in (sc.SYSTEM, sc.SPLIT_SYSTEM, resume_scoring.MUST_HAVE_SYSTEM, note) for w in ('SQE','QE','质量','供应商'))


async def test_split_feedback_redacts_and_limits_feedback():  # 11-27 终审：管理员粘贴的客户原话也先脱敏再进模型
    llm = FakeLLM({'items':[{'text':'第一学历统招大专及以上'}]})
    text = '客户王先生说第一学历要统招，有问题打 13812345678'
    out = await sc.split_feedback(m, text, [], llm=llm)
    assert '13812345678' not in llm.prompts[0] and '王先生' not in llm.prompts[0] and '统招' in llm.prompts[0]
    assert out[0]['note'] == text  # 原话只留在本机草案
    with pytest.raises(ValueError, match='1000'): await sc.split_feedback(m, '客' * 1001, [], llm=llm)
    assert len(llm.prompts) == 1


def test_stats_counts_by_item():  # 11-27 Task 9：任务页“近 7 天初筛”统计行
    since = 1_000_000.0
    def outbox(at): return [{'id':'m','intent':'first_contact','status':'sent','claimed_at':at}]
    reject = {'revision':'r','at':since + 10,'outcome':'reject','unclear_ids':[],
              'items':[{'id':'mh_a','verdict':'unmet','evidence':'资料未提及'},{'id':'mh_b','verdict':'met','evidence':'本科'}]}
    case = {'candidates':{
        'p1':{'profile_reviewed_at':since + 5,'screening':reject},
        'p2':{'profile_reviewed_at':since + 6,'screening':{**reject,'at':since + 20}},
        'p3':{'profile_reviewed_at':since + 7,'screening':{'revision':'r','at':since + 30,'outcome':'pass','unclear_ids':['mh_b'],
              'items':[{'id':'mh_a','verdict':'met','evidence':'SQE'},{'id':'mh_b','verdict':'unclear','evidence':'资料未提及'}]},'outbox':outbox(since + 40)},
        'old':{'profile_reviewed_at':since - 5,'screening':{**reject,'at':since - 5},'outbox':outbox(since - 50)},
        'alias':{'alias_of':'p1','profile_reviewed_at':since + 5,'screening':reject}}}
    assert sc.stats(case, since) == {'reviewed':3,'rejected':2,'unclear':1,'contacted':1,'by_item':{'mh_a':2}}
    assert sc.stats({}, since) == {'reviewed':0,'rejected':0,'unclear':0,'contacted':0,'by_item':{}}


def test_stats_counts_external_observed_first_contacts_by_platform_day():  # 11-27 Task 9 复核：平台外观察到的首聊只有日期
    import time
    from cloakup.autonomy import day_key
    now = time.time()
    def seen(day): return {'id':'x','intent':'first_contact','origin':'external_observed','status':'sent','receipt':{'platform_time':day + ' 10:00'}}
    case = {'candidates':{'p1':{'outbox':[seen(day_key(now - 2 * 86400))]},'p2':{'outbox':[seen(day_key(now - 9 * 86400))]},
                          'p3':{'outbox':[{'id':'y','intent':'answer','origin':'external_observed','receipt':{'platform_time':day_key(now)}}]}}}
    assert sc.stats(case, now - 7 * 86400)['contacted'] == 1


def test_experience_absence_in_listed_history_is_unmet():  # 11-27 只读演练：QE 岗 9 份全文简历里 5 份全无相关经历却判“看不出”，管理员要求改严
    from cloakup import resume_scoring
    for prompt in (sc.SYSTEM, resume_scoring.MUST_HAVE_SYSTEM):
        assert sc.EXPERIENCE_RULE in prompt
    assert '没有任何与该项相关' in sc.EXPERIENCE_RULE and '仍判 unclear' in sc.EXPERIENCE_RULE


async def test_card_only_profile_is_set_aside():  # 11-27 只读演练：…i8FFE~ 只看卡片被判无 SQE，全文却写着做过；没取到推荐页详情就放下这一位，不调模型
    llm = FakeLLM({'items':[{'id':MH[0]['id'],'verdict':'unmet','evidence':'经历均为质检'}]})
    for overview in ('', '   ', None):
        with pytest.raises(sc.ScreeningUnavailable, match='推荐页详情'): await sc.screen(m, case_with(MH), person, overview, llm=llm)
    assert not llm.prompts
