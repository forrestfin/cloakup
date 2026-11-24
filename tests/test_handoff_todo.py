from cloakup.storage import Database
from cloakup.attention import AttentionStore


def case_with(candidate):
    return {'id': 'c' * 32, 'job': {'label': 'Test'}, 'candidates': {candidate['id']: candidate}}


def test_received_resume_handoff_creates_no_todo_but_keeps_candidate_flag(tmp_path):
    # 已取得简历转顾问接续属于正常流程，不生成“待顾问接续”待办，候选人的 handoff 标记保留。
    candidate = {'id': 'one', 'status': 'resume_received', 'resumes': ['file'], 'handoff': {'reason': '已取得简历'},
                 'inbox': [], 'outbox': []}
    case = case_with(candidate)
    todos = AttentionStore(Database(tmp_path)).reconcile([], [case])
    assert not any(t['kind'] == 'handoff' for t in todos)
    assert case['candidates']['one']['handoff'] == {'reason': '已取得简历'}


def test_other_exceptions_still_create_todos_without_handoff(tmp_path):
    # 其他异常仍照常生成待办：候选人提出需顾问确认的问题即生成 question 待办。
    candidate = {'id': 'one', 'status': 'waiting_reply', 'handoff': {'reason': '已取得简历'},
                 'inbox': [{'external_id': 'in1', 'text': '社保怎么缴纳？'}], 'outbox': []}
    todos = AttentionStore(Database(tmp_path)).reconcile([], [case_with(candidate)])
    assert any(t['kind'] == 'question' for t in todos)
    assert not any(t['kind'] == 'handoff' for t in todos)
