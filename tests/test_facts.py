import re
from cloakup.facts import draft_anonymous_job, effective_job, ANCHORS
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
