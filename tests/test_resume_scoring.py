import html
import io
import json
import zipfile
from types import SimpleNamespace as NS

import pytest

from cloakup import resume_scoring as rs
from cloakup import task_notifications as n
from cloakup.storage import Database
from cloakup.sourcing import CaseStore
from cloakup.resume_archive import save_file


def make_docx(lines):
    body = ''.join('<w:p><w:r><w:t>%s</w:t></w:r></w:p>' % html.escape(line) for line in lines)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>%s</w:body></w:document>' % body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('word/document.xml', xml)
    return buf.getvalue()


RESUME_LINES = [
    '张三',
    '电话：13800138000 邮箱：zhangsan@example.com',
    '身份证：110101199001011234 年龄：30岁 性别：男',
    '工作经历',
    '2019-2023 某科技有限公司 数据分析负责人，负责用户增长与运营分析项目',
    '教育背景 某大学 计算机专业 本科',
]


class Definitions:
    def __init__(self, db):
        self.db = db

    def list(self):
        return self.db.list('task')

    def get(self, key):
        return self.db.get('task', key)


class Client:
    def __init__(self):
        self.calls = []
        self.uploads = 0

    def configured(self):
        return True

    def authenticate(self):
        return 'memory-only-token'

    def upload(self, record, data):
        self.uploads += 1
        return 'file_key_private'

    def send(self, receiver, receiver_type, msg_type, content, stable_uuid):
        self.calls.append((receiver, receiver_type, msg_type, content, stable_uuid))
        return 'om_' + str(len(self.calls))


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.client = NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/v3'))
        self.model = 'test-model'
        self.result = result
        self.error = error
        self.prompts = []

    async def json(self, *, system, content, schema, effort='low', structured=True):
        self.prompts.append(content)
        if self.error:
            raise self.error
        return self.result


CONSENT = {'enabled': True, 'mode': 'anonymous_resume_scoring', 'host': 'ark.cn-beijing.volces.com'}


def person_fixture(cases, case):
    p = cases.add_candidate(case, 'person', 'https://www.zhipin.com/web/chat/index',
                            {'summary': '职业资料', 'platform_label': '测试候选人'})
    p['inbox'] = [{'text': '您好，我对这个机会感兴趣', 'external_id': 'in1', 'created_at': 1}]
    p['outbox'] = [{'text': '您好，方便发一份简历吗？', 'status': 'sent', 'created_at': 2, 'intent': 'request_resume'}]
    cases.save(case)
    return p


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path)
    cases = CaseStore(tmp_path, db)
    case = cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=test')
    case['anonymous_job'] = {'role': '测试岗位', 'requirements': ['需要有数据分析或运营分析经验']}
    cases.save(case)
    p = person_fixture(cases, case)
    record = save_file(cases, case, p, 'platform-message:file', make_docx(RESUME_LINES), '简历.docx')
    task = {'id': 'task1', 'config': {'name': '高端岗位 · 寻访', 'case_ids': [case['id']],
                                      'notifications': {'enabled': True, 'resume_chat_id': 'oc_group', 'todo_user_id': ''}}}
    db.put('task', task['id'], task)
    manager = NS(db=db, cases=cases, definitions=Definitions(db), jobs={}, _notification_client=Client())
    return manager, task, case, p, record


# ---------------------------------------------------------------- 脱敏与抽取

def test_redaction_removes_identity_but_keeps_employers_and_schools(env):
    m, task, case, p, record = env
    text = rs.extract_text(record, make_docx(RESUME_LINES))
    clean = rs.redact_resume(text, p)
    for secret in ('张三', '13800138000', 'zhangsan@example.com', '110101199001011234', '30岁'):
        assert secret not in clean
    assert '某科技有限公司' in clean and '某大学' in clean and '数据分析' in clean


def test_urls_and_contacts_are_removed_before_the_residual_check(env):
    m, task, case, p, record = env
    clean = rs.redact_resume('工作经历 作品集 https://example.test/profile 详情内容较多的一段描述文字', p)
    assert 'example.test' not in clean and '[已隐去联系方式]' in clean


def test_attachment_filename_is_omitted_and_does_not_require_review():
    person = {
        'profile': {'platform_label': '测试候选人'},
        'conversation': {'receiver': '测试候选人'},
        'inbox': [
            {'text': '可以了解，感谢', 'external_id': 'a', 'created_at': 1},
            {'text': 'MAR-wuhan.doc\n点击预览附件简历', 'external_id': 'b', 'created_at': 2},
        ],
        'outbox': [{'text': '您好，方便发一份简历吗？', 'status': 'sent', 'created_at': 3, 'intent': 'request_resume'}],
    }
    rows = rs.conversation_for_scoring(person)
    joined = '\n'.join(row['text'] for row in rows)
    assert 'wuhan' not in joined and '点击预览' not in joined
    assert any('可以了解' in row['text'] for row in rows)


def test_scoring_dialogue_keeps_employer_and_strips_phone():
    person = {
        'profile': {},
        'conversation': {},
        'inbox': [{'text': '我之前在某科技有限公司，电话13800138000', 'created_at': 1}],
        'outbox': [],
    }
    rows = rs.conversation_for_scoring(person)
    assert rows[0]['text'].count('某科技有限公司') == 1
    assert '13800138000' not in rows[0]['text']


def test_masked_name_that_cannot_be_removed_blocks_resume(env):
    m, task, case, p, record = env
    with pytest.raises(rs.ScoringUnavailable) as result:
        rs.redact_resume('工作经历 王*经理负责运营分析与用户增长项目 详情内容较多', p)
    assert not result.value.retryable and '未发送模型' in str(result.value)


def test_scanned_or_unsupported_file_is_reported_not_guessed(env):
    m, task, case, p, record = env
    with pytest.raises(rs.ScoringUnavailable, match='未抽取到足够文字'):
        rs.extract_text({'extension': 'docx'}, make_docx(['短']))
    with pytest.raises(rs.ScoringUnavailable, match='不支持'):
        rs.extract_text({'extension': 'txt'}, b'plain')


def test_blank_pdf_is_treated_as_unreadable_without_model_call(env):
    pypdf = pytest.importorskip('pypdf')
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(rs.ScoringUnavailable, match='未抽取到足够文字'):
        rs.extract_text({'extension': 'pdf'}, buf.getvalue())


# ---------------------------------------------------------------- 授权与模型边界

async def test_no_consent_means_no_model_call(env):
    m, task, case, p, record = env
    llm = FakeLLM(result={'score': 4, 'basis': 'x', 'comment': 'y'})
    with pytest.raises(rs.ScoringUnavailable, match='未授权'):
        await rs.evaluate(m, case, p, record, make_docx(RESUME_LINES), llm=llm)
    assert llm.prompts == []


async def test_recruiting_dialogue_consent_does_not_authorize_resume_scoring(env):
    m, task, case, p, record = env
    m.db.put('disclosure_consent', 'recruiting-conversation',
             {'enabled': True, 'mode': 'anonymous_conversation', 'host': 'ark.cn-beijing.volces.com'})
    with pytest.raises(rs.ScoringUnavailable, match='未授权'):
        await rs.evaluate(m, case, p, record, make_docx(RESUME_LINES), llm=FakeLLM(result={}))


async def test_wrong_gateway_blocks_before_sending(env):
    m, task, case, p, record = env
    m.db.put('disclosure_consent', rs.CONSENT_ID, CONSENT)
    llm = FakeLLM(result={'score': 4, 'basis': 'x', 'comment': 'y'})
    llm.client.raw.base_url = 'https://other.example.test'
    with pytest.raises(rs.ScoringUnavailable, match='不同'):
        await rs.evaluate(m, case, p, record, make_docx(RESUME_LINES), llm=llm)
    assert llm.prompts == []


async def test_missing_requirements_never_falls_back_to_other_jobs(env):
    m, task, case, p, record = env
    m.db.put('disclosure_consent', rs.CONSENT_ID, CONSENT)
    case['anonymous_job'] = None
    with pytest.raises(rs.ScoringUnavailable, match='未配置脱敏要求'):
        await rs.evaluate(m, case, p, record, make_docx(RESUME_LINES), llm=FakeLLM(result={}))


async def test_scoring_sends_only_redacted_content_and_returns_comment(env):
    m, task, case, p, record = env
    m.db.put('disclosure_consent', rs.CONSENT_ID, CONSENT)
    llm = FakeLLM(result={'score': 4, 'basis': '有数据分析经历', 'comment': '匹配数据分析要求，缺少运营团队管理经历。'})
    result = await rs.evaluate(m, case, p, record, make_docx(RESUME_LINES), llm=llm)
    assert result['status'] == 'ok' and result['score'] == 4 and result['comment'].startswith('匹配')
    sent = llm.prompts[0]
    for secret in ('张三', '13800138000', 'zhangsan@example.com', '110101199001011234'):
        assert secret not in sent
    payload = json.loads(sent)
    assert payload['job_requirements'] == ['需要有数据分析或运营分析经验']
    assert '某科技有限公司' in payload['resume_text']


async def test_model_output_with_identifier_is_not_pushed(env):
    m, task, case, p, record = env
    m.db.put('disclosure_consent', rs.CONSENT_ID, CONSENT)
    llm = FakeLLM(result={'score': 5, 'basis': '符合', 'comment': '可联系 13900139000'})
    with pytest.raises(rs.ScoringUnavailable, match='已拦截'):
        await rs.evaluate(m, case, p, record, make_docx(RESUME_LINES), llm=llm)


# ---------------------------------------------------------------- 通知集成

@pytest.fixture
def scored(env, monkeypatch):
    """替换评分函数，统计调用次数；不触发真实模型。"""
    m, task, case, p, record = env
    m.db.put('disclosure_consent', rs.CONSENT_ID, CONSENT)
    state = {'calls': 0, 'results': []}

    async def fake_evaluate(manager, case_, person, record_, data, llm=None):
        state['calls'] += 1
        outcome = state['results'].pop(0) if state['results'] else {
            'status': 'ok', 'score': 4, 'basis': '有相关经历', 'comment': '匹配数据分析要求。', 'model': 'test', 'at': 1}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(rs, 'evaluate', fake_evaluate)
    return m, state


async def test_notification_carries_score_and_scores_each_file_once(scored):
    m, state = scored
    for _ in range(4):
        await n.tick(m)
    texts = [c[3]['text'] for c in m._notification_client.calls if c[2] == 'text' and '收到简历' in c[3]['text']]
    assert len(texts) == 1 and '推荐评分：4/5 · 有相关经历' in texts[0] and '匹配数据分析要求' in texts[0]
    assert state['calls'] == 1
    assert m.db.get(n.KIND, next(r['id'] for r in m.db.list(n.KIND) if r['kind'] == 'resume'))['status'] == 'sent'


async def test_temporary_scoring_failure_retries_then_sends_once(scored):
    m, state = scored
    state['results'] = [rs.ScoringUnavailable('模型暂不可用', retryable=True),
                        rs.ScoringUnavailable('模型暂不可用', retryable=True)]
    await n.tick(m)
    row = next(r for r in m.db.list(n.KIND) if r['kind'] == 'resume')
    assert row['status'] == 'retry' and row['evaluation_attempts'] == 1
    assert not m._notification_client.calls
    row['next_attempt_at'] = 0
    n._save(m.db, row)
    await n.tick(m)
    row = next(r for r in m.db.list(n.KIND) if r['kind'] == 'resume')
    assert row['status'] == 'retry' and row['evaluation_attempts'] == 2
    row['next_attempt_at'] = 0
    n._save(m.db, row)
    await n.tick(m)
    assert state['calls'] == 3
    text = next(c[3]['text'] for c in m._notification_client.calls if '收到简历' in c[3].get('text', ''))
    assert '推荐评分：4/5' in text


async def test_exhausted_retries_still_push_resume_with_reason(scored):
    m, state = scored
    state['results'] = [rs.ScoringUnavailable('模型暂不可用', retryable=True)] * 3
    n.enqueue(m)
    for _ in range(6):
        row = next(r for r in m.db.list(n.KIND) if r['kind'] == 'resume')
        row['next_attempt_at'] = 0
        n._save(m.db, row)
        await n.tick(m)
    assert state['calls'] == 3
    assert any(c[2] == 'file' for c in m._notification_client.calls)
    text = next(c[3]['text'] for c in m._notification_client.calls if '收到简历' in c[3].get('text', ''))
    assert '推荐评分：未生成（模型暂不可用）' in text


async def test_unavailable_score_does_not_block_resume_file(scored):
    m, state = scored
    state['results'] = [rs.ScoringUnavailable('未授权简历脱敏评分，未发送模型')]
    await n.tick(m)
    assert [c[2] for c in m._notification_client.calls][:1] == ['file']
    assert all(r['status'] == 'sent' for r in m.db.list(n.KIND))
    text = next(c[3]['text'] for c in m._notification_client.calls if '收到简历' in c[3].get('text', ''))
    assert '推荐评分：未生成（未授权简历脱敏评分，未发送模型）' in text
