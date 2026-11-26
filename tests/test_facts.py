import re
from cloakup.facts import draft_anonymous_job, effective_job, ANCHORS, jd_statements
from cloakup.recruiting_ai import checked_brief
from cloakup.sourcing import CaseStore
from cloakup.job_config import view_config, save_config


def _case(**extra):
    # 11-25 17:05 FDE交付工程师：绑定后脱敏岗位事实为空，管理员希望由系统按 JD 生成一版再核对。
    plan = {'greeting': '您好', 'keywords': ['甲'], 'follow_up_questions': [],
            'requirements': [{'kind': 'required', 'requirement': '能独立完成需求分析与方案设计', 'source': 'JD'},
                             {'kind': 'preferred', 'requirement': '有产品落地案例', 'source': 'JD'},
                             {'kind': 'clarify', 'requirement': '是否需要出差', 'source': 'JD'},
                             {'kind': 'required', 'requirement': '熟悉某某科技有限公司的产品', 'source': 'JD'},
                             {'kind': 'required', 'requirement': '期望薪资15-18K', 'source': 'JD'}]}
    case = {'id': 'c1', 'config_revision': 1, 'anonymous_job': None, 'search_plan': plan,
            'job': {'label': '方案工程师', 'revision': 1,
                    'snapshot': {'title': '方案工程师', 'location': '武汉东西湖区某某科技有限公司1001', 'requirements': 'JD 原文',
                                 'platform_fields': ['3-5年', '本科', '15k', '18k', '12个月']}}}
    case.update(extra)
    return case


def test_draft_builds_lines_from_plan_location_and_platform_fields():
    draft = draft_anonymous_job(_case())
    assert draft['role'] == '方案工程师'
    assert draft['requirements'] == ['工作地点：武汉东西湖区', '经验要求：3-5年', '学历要求：本科',
                                     '能独立完成需求分析与方案设计', '加分项：有产品落地案例']
    # 含公司标识、薪资金额的条目不进草稿；待澄清项不当作事实。
    assert draft['dropped'] == 2
    assert not any(re.search(r'\d+\s*[kK]|有限公司|出差', line) for line in draft['requirements'])
    checked_brief({'job': {'role': draft['role'], 'requirements': draft['requirements']},
                   'candidate': {k: '' for k in ('experience', 'availability', 'visible_evidence', 'coverage')}})


def test_draft_types_anchored_lines_so_they_survive_as_confirmed_facts():
    draft = draft_anonymous_job(_case())
    kinds = {(f['kind'], f['value']) for f in draft['facts']}
    assert ('work_arrangement', '工作地点：武汉东西湖区') in kinds
    assert ('experience', '经验要求：3-5年') in kinds and ('education', '学历要求：本科') in kinds
    assert all(f['status'] == 'confirmed' for f in draft['facts'])
    # 有类型锚点的每一行都有对应的类型事实，否则保存后 effective_job 会把它丢掉。
    anchored = [line for line in draft['requirements'] if any(re.search(p, line) for p in ANCHORS.values())]
    assert anchored and all(line in {f['value'] for f in draft['facts']} for line in anchored)


def test_no_draft_when_facts_exist_or_nothing_to_draft_from():
    assert draft_anonymous_job(_case(anonymous_job={'role': '方案工程师', 'requirements': ['已有']})) is None
    assert draft_anonymous_job(_case(search_plan=None, job={'label': '方案工程师', 'snapshot': {}})) is None


def test_draft_without_city_pattern_or_fields_still_uses_plan():
    case = _case();case['job']['snapshot'].update(location='', platform_fields=None)
    draft = draft_anonymous_job(case)
    assert draft['requirements'] == ['能独立完成需求分析与方案设计', '加分项：有产品落地案例']


def test_saving_the_draft_keeps_every_line_effective(tmp_path):
    s = CaseStore(tmp_path);c = s.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j1')
    s.bind_job(c, {'title': '方案工程师', 'requirements': 'JD 原文', 'location': '武汉东西湖区某某科技有限公司1001', 'platform_fields': ['3-5年', '本科']})
    c['search_plan'] = _case()['search_plan'];c['greeting_draft'] = '您好，方便聊一下吗？';s.save(c)
    draft = draft_anonymous_job(c)
    cfg = view_config(c);rev = cfg.pop('revision')
    cfg.update(anonymous_job={'role': draft['role'], 'requirements': draft['requirements']}, facts=draft['facts'])
    save_config(s, c, {'expected_revision': rev, 'config': cfg})
    assert effective_job(c)['requirements'] and set(effective_job(c)['requirements']) == set(draft['requirements'])
    assert draft_anonymous_job(c) is None


QE_JD=('岗位职责 1、全流程质量管控：负责来料（IQC）、制程（IPQC）、出货（OQC）三个环节的检验标准制定与执行监督。 '
       '任职要求 （一）硬性条件 1、大专及以上学历，电子、电气、材料、机械等理工科专业。 2、3年以上电子制造业质量工作经验，5年以上优先。 '
       '我们提供 1、8-12K＋绩效奖金，具体结构面议。 2、直接向品质负责人汇报，体系优化有实际决策空间。 3、服务头部消费电子／新能源客户，产品线稳定')


def _jd_case(jd=QE_JD,location='深圳宝安区深圳西部硅谷-B座六楼',company='君润人力'):
    return _case(job={'label':'qe工程师','revision':2,'snapshot':{'title':'qe工程师','company':company,'location':location,'requirements':jd,'platform_fields':[]}})


def test_jd_statements_split_single_line_numbered_jd():
    # 11-26 16:04 qe工程师 候选人问汇报对象：JD 写明“直接向品质负责人汇报”，系统却转了顾问。
    out=jd_statements(_jd_case())
    assert '直接向品质负责人汇报，体系优化有实际决策空间。' in out
    assert '服务头部消费电子／新能源客户，产品线稳定。' in out
    assert '3年以上电子制造业质量工作经验，5年以上优先。' in out
    assert not any(s.rstrip('。') in ('岗位职责','任职要求','硬性条件','我们提供') for s in out)
    assert not any(re.match(r'\d+、|（',s) for s in out)


def test_jd_statements_location_keeps_district_only():
    assert jd_statements(_jd_case())[0]=='工作地点：深圳宝安区。'
    assert jd_statements(_jd_case(location='武汉东西湖区中盈智源有限公司1001'))[0]=='工作地点：武汉东西湖区。'


def test_jd_statements_drop_money_company_and_address():
    jd='1、底薪15k起。 2、月度8-12K＋绩效奖金。 3、客户为某某科技有限公司。 4、隶属华东集团。 5、办公在B座六楼。 6、君润人力直招。 7、负责供应商审核。'
    out=jd_statements(_jd_case(jd=jd))
    assert out[1:]==['负责供应商审核。']


def test_jd_statements_empty_without_snapshot_and_capped_at_40():
    assert jd_statements({'job':{}})==[]
    many=' '.join(f'{i}、负责第{i}项检验工作' for i in range(1,61))
    assert len(jd_statements(_jd_case(jd=many,location='')))==40


import pytest


@pytest.mark.parametrize('line',['收入8000-12000','待遇8000-12000','月均收入可达一万五','月入过万','转正后上浮20%','股票期权0.1%','试用期工资打八折'])
def test_jd_statements_drop_pay_figures_without_unit(line):
    # 11-26 终审 C1：没有“薪”字或单位的收入数字也不能对外。
    assert jd_statements(_jd_case(jd='1、'+line+'。 2、负责供应商审核。',location=''))==['负责供应商审核。']


@pytest.mark.parametrize('line',['联系电话：0755-12345678','座机 020 3888 1234','QQ 123456789','联系人张经理','客户为华为、比亚迪等头部企业','甲方为宁德时代','ABC Technology Co., Ltd.'])
def test_jd_statements_drop_contacts_and_named_clients(line):
    # 11-26 终审 I1／I2：座机、QQ、联系人与点名的客户不能对外。
    assert jd_statements(_jd_case(jd='1、'+line+'。 2、负责供应商审核。',location=''))==['负责供应商审核。']


def test_jd_statements_split_dot_numbering_and_glued_headings():
    # 11-26 终审 I3／I4：“1.”“1)”编号与粘在句尾的小节标题。
    jd='岗位职责：1.负责来料检验 2.负责制程巡检 任职要求：1.大专以上学历 2)熟悉SPC工具 （一）硬性要求 4)沟通顺畅 我们提供 1、股票期权0.1%'
    assert jd_statements(_jd_case(jd=jd,location=''))==['负责来料检验。','负责制程巡检。','大专以上学历。','熟悉SPC工具。','沟通顺畅。']


def test_jd_statements_keep_standard_codes_followed_by_list_comma():
    # 11-26 只读预览 qe工程师：“持ISO9001、IATF16949内审员证书”被当成编号“9001、”切开。
    jd='加分项 1、持ISO9001、IATF16949内审员证书。 2、熟悉8D、FMEA。'
    assert jd_statements(_jd_case(jd=jd,location=''))==['持ISO9001、IATF16949内审员证书。','熟悉8D、FMEA。']


def test_jd_statements_keep_tool_names_and_exclamations():
    # 11-26 只读演练：qe“5Why”被当成“5W”（五万）拦掉；高端末句“联系！”被补成“联系！。”。
    jd='1、主导原因分析（5Why／鱼骨图），输出8D报告。 2、欢迎有交付能力的伙伴联系！'
    assert jd_statements(_jd_case(jd=jd,location=''))==['主导原因分析（5Why／鱼骨图），输出8D报告。','欢迎有交付能力的伙伴联系！']
