from copy import deepcopy

import pytest

from cloakup.sourcing import CaseStore, SourcingError


@pytest.fixture
def case_data(tmp_path):
    store = CaseStore(tmp_path)
    case = store.create('https://www.zhipin.com/web/boss/job/position-1')
    store.bind_job(case, {'title': '知识架构工程师', 'requirements': ['知识库项目经验']})
    store.add_candidate(case, 'person-1', 'https://www.zhipin.com/candidate/1', {'project': '知识库项目'})
    return store, case


def approved(store, case, intent='first_contact'):
    message = store.draft(case, 'person-1', intent, '你好，想和你交流岗位。')
    shown = store.preview(case, 'person-1', message['id'])
    store.approve(case, 'person-1', message['id'], shown['fingerprint'])
    return message


def test_claim_requires_exact_approved_content(case_data):
    store, case = case_data
    m = store.draft(case, 'person-1', 'first_contact', '原文')
    with pytest.raises(SourcingError): store.claim_send(case, 'person-1', m['id'])
    shown = store.preview(case, 'person-1', m['id'])
    m['text'] = '已变化的正文'
    with pytest.raises(SourcingError): store.approve(case, 'person-1', m['id'], shown['fingerprint'])
    shown = store.preview(case, 'person-1', m['id'])
    store.approve(case, 'person-1', m['id'], shown['fingerprint'])
    m['text'] = '确认后再次变化'
    with pytest.raises(SourcingError): store.claim_send(case, 'person-1', m['id'])


def test_new_reply_invalidates_old_draft_and_deduplicates(case_data):
    store, case = case_data
    m = approved(store, case)
    assert store.receive(case, 'person-1', 'reply-1', '请介绍一下岗位')
    assert not store.receive(case, 'person-1', 'reply-1', '请介绍一下岗位')
    assert len(case['candidates']['person-1']['inbox']) == 1
    with pytest.raises(SourcingError): store.claim_send(case, 'person-1', m['id'])
    assert m['status'] == 'stale'


def test_crash_after_claim_does_not_repeat_send(case_data, tmp_path):
    store, case = case_data
    m = approved(store, case)
    store.claim_send(case, 'person-1', m['id'])
    # Normal reads must not declare an in-progress send failed.
    assert store.load(case['id'])['candidates']['person-1']['outbox'][0]['status'] == 'sending'
    restarted = CaseStore(tmp_path)
    recovered = restarted.load(case['id'])
    assert recovered['candidates']['person-1']['status'] == 'delivery_unknown'
    with pytest.raises(SourcingError): restarted.claim_send(recovered, 'person-1', m['id'])
    with pytest.raises(SourcingError): restarted.draft(recovered, 'person-1', 'first_contact', '再次打招呼')
    restarted.confirm_delivery(recovered, 'person-1', m['id'], {'evidence': '人工核实真实会话中存在相同消息'})
    assert recovered['candidates']['person-1']['status'] == 'waiting_reply'


def test_full_conversation_resume_and_human_decision(case_data):
    store, case = case_data
    m = approved(store, case)
    store.claim_send(case, 'person-1', m['id'])
    store.confirm_delivery(case, 'person-1', m['id'], {'evidence': '会话消息ID: first'})
    store.receive(case, 'person-1', 'reply-1', '可以聊聊')
    request = approved(store, case, 'request_resume')
    store.claim_send(case, 'person-1', request['id'])
    store.confirm_delivery(case, 'person-1', request['id'], {'evidence': '会话消息ID: resume-request'})
    assert case['candidates']['person-1']['status'] == 'resume_requested'
    store.attach_resume(case, 'person-1', 'document:resume-1')
    assert case['candidates']['person-1']['status'] == 'resume_received'
    store.decide(case, 'person-1', 'recommended', '顾问已核实相关项目经历')
    store.receive(case, 'person-1', 'reply-2', '谢谢')
    assert case['candidates']['person-1']['status'] == 'recommended'
    assert store.load(case['id'])['candidates']['person-1']['decision']['actor'] == 'consultant'
    with pytest.raises(SourcingError): store.draft(case, 'person-1', 'follow_up', '额外跟进')


def test_job_change_and_opt_out_revoke_send(case_data):
    store, case = case_data
    m = approved(store, case)
    store.bind_job(case, {'title': '另一岗位', 'requirements': ['另一项要求']})
    with pytest.raises(SourcingError): store.claim_send(case, 'person-1', m['id'])
    fresh = approved(store, case)
    store.decide(case, 'person-1', 'do_not_contact', '候选人明确拒绝继续联系')
    with pytest.raises(SourcingError): store.claim_send(case, 'person-1', fresh['id'])


def test_binding_and_identifiers_validated(tmp_path):
    store = CaseStore(tmp_path)
    for link in ('http://www.zhipin.com/job', 'https://www.zhipin.com.attacker.test/job', 'https://attacker.test/job'):
        with pytest.raises(SourcingError): store.create(link)
    with pytest.raises(SourcingError): store.load('../state')
    case = store.create('https://www.zhipin.com/')
    with pytest.raises(SourcingError): store.bind_job(case, {'title': '只有名称，没有JD'})


def test_existing_contact_revokes_pending_draft_without_fabricated_approval(case_data):
    store, case = case_data
    pending = approved(store, case)
    receipt = {'candidate_id': 'person-1', 'job_revision': case['job']['revision'],
               'evidence': '会话中逐字核对正文，显示送达',
               'identity_evidence': '打开的会话履历与已审阅资料一致',
               'delivery_status': 'delivered'}
    observed = store.record_existing_contact(case, 'person-1', 'observed-1', '已有首聊', receipt)
    assert pending['status'] == 'stale'
    assert observed['approved_fingerprint'] is None
    assert 'approved_at' not in observed and 'claimed_at' not in observed
    assert observed['origin'] == 'external_observed'
    assert case['candidates']['person-1']['status'] == 'waiting_reply'
    assert store.record_existing_contact(case, 'person-1', 'observed-1', '已有首聊', receipt) is observed
    with pytest.raises(SourcingError): store.claim_send(case, 'person-1', pending['id'])
    with pytest.raises(SourcingError): store.draft(case, 'person-1', 'first_contact', '重复开聊')
    with pytest.raises(SourcingError): store.record_existing_contact(case, 'person-1', 'observed-1', '不同正文', receipt)


def test_existing_contact_requires_recipient_and_job_evidence(case_data):
    store, case = case_data
    receipt = {'candidate_id': 'person-1', 'job_revision': case['job']['revision'],
               'evidence': '正文与送达标识', 'identity_evidence': '履历核对',
               'delivery_status': 'delivered'}
    for key, bad in [('candidate_id', 'other'), ('job_revision', -1),
                     ('evidence', ''), ('identity_evidence', ''), ('delivery_status', 'unknown')]:
        with pytest.raises(SourcingError):
            store.record_existing_contact(case, 'person-1', 'observed-1', '已有首聊', {**receipt, key: bad})
    assert not case['candidates']['person-1']['outbox']


def test_orphaned_send_claim_recovers_into_candidate_history(tmp_path):
 from cloakup.action_gateway import ActionLedger
 store=CaseStore(tmp_path);case=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j')
 store.bind_job(case,{'title':'岗位','requirements':'确认岗位'})
 p=store.add_candidate(case,'p','https://www.zhipin.com/web/chat/index',{'summary':'职业资料'})
 m=store.draft(case,p['id'],'follow_up','您好')
 ledger=ActionLedger(store.db)
 ident=ledger.claim(task_id='case:'+case['id'],resource='candidate:p',capability='send_message',identity='synthetic',run_id='interrupted',expectation={'case_id':case['id'],'candidate_id':p['id'],'message_id':m['id']})
 ledger.transition(ident,'executing')
 restored=CaseStore(tmp_path).load(case['id'])['candidates']['p']
 assert restored['outbox'][0]['status']=='delivery_unknown' and restored['outbox'][0]['operation_id']==ident
