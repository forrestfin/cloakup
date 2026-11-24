import datetime
from zoneinfo import ZoneInfo

from cloakup import decision_runtime as dr
from cloakup.autonomy import intent_labels
from cloakup.followup import evaluate

# 2026-11-20 15:13（上海时间）：候选人回复“可以细聊”，我方 15:14 已用其他话术回复、未索要简历
T0 = datetime.datetime(2026, 11, 20, 15, 13, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def conversation(**person):
    p = {'id': 'p1', 'inbox': [{'external_id': 'in1', 'text': '可以细聊', 'observed_at': T0}], 'history': [],
         'outbox': [{'id': 'o1', 'status': 'sent', 'approval_actor': 'automation', 'sent_at': T0 + 60, 'claimed_at': T0 + 50,
                     'text': '您想先了解岗位的哪方面？', 'receipt': {'delivery_status': 'read', 'read_at': T0 + 3600}}],
         'resumes': [], 'platform_actions': [], 'status': 'waiting_reply'}
    p.update(person)
    return p


CASE = {'id': 'c', 'execution': {}, 'autonomy': {'max_messages_per_candidate_day': 6}}


def test_can_chat_in_detail_is_positive_but_negated_forms_are_not():
    assert intent_labels('可以细聊') == ['interest']
    assert 'interest' not in intent_labels('不想细聊')
    assert 'interest' not in intent_labels('不可以细聊')


def test_positive_reply_waits_about_two_hours_then_asks_for_resume_once():
    p = conversation()
    assert evaluate(CASE, p, now=T0 + 3600)['state'] == 'positive_waiting'
    state = evaluate(CASE, p, now=T0 + 3 * 3600)
    assert state['due'] is True and state['variants'] == ['resume_after_interest']


def test_only_one_resume_request_per_positive_reply():
    p = conversation(outbox=conversation()['outbox'] + [
        {'id': 'n1', 'status': 'sent', 'approval_actor': 'automation', 'sent_at': T0 + 3 * 3600,
         'claimed_at': T0 + 3 * 3600, 'text': '您好，方便时发一份最新版简历吗？', 'decision': {'nudge': True, 'nudge_anchor': 'in1'}}])
    state = evaluate(CASE, p, now=T0 + 6 * 3600)
    assert not state['due'] and state['variants'] != ['resume_after_interest']


def test_no_positive_follow_up_when_resume_was_already_asked_after_reply():
    p = conversation(outbox=conversation()['outbox'] + [
        {'id': 'r1', 'status': 'sent', 'approval_actor': 'automation', 'sent_at': T0 + 600, 'claimed_at': T0 + 590,
         'text': '您好，方便发一份最新简历吗？', 'receipt': {'delivery_status': 'read', 'read_at': T0 + 900}}])
    state = evaluate(CASE, p, now=T0 + 3 * 3600)
    assert not state['due']


def test_no_positive_follow_up_after_platform_resume_request():
    p = conversation(platform_actions=[{'id': 'a1', 'kind': 'ask_resume', 'status': 'pending', 'at': T0 + 120}])
    assert not evaluate(CASE, p, now=T0 + 3 * 3600)['due']


def test_no_positive_follow_up_when_candidate_declined():
    p = conversation(inbox=[{'external_id': 'in1', 'text': '不考虑', 'observed_at': T0}])
    assert evaluate(CASE, p, now=T0 + 3 * 3600)['state'] == 'paused_by_candidate'


def test_direct_decision_sends_resume_request_without_waiting_for_model():
    p = conversation()
    proposal = dr.direct_proposal({'wait': '等待', 'nudge_resume_after_interest': '索要简历'},
                                  {'action': 'continue', 'source': 'conversation'}, [], p)
    assert proposal['action'] == 'nudge_resume_after_interest'
