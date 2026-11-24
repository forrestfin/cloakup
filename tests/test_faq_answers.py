"""待办答案加入岗位常见问题：校验、安全点生效、版本派生与执行端可见。"""
import copy
from types import SimpleNamespace as NS

import pytest

from cloakup import faq_answers as fa
from cloakup.account_execution import AccountExecution
from cloakup.agent_workspace import AgentWorkspace,assert_release_ready
from cloakup.attention import AttentionStore
from cloakup.conversation_semantics import APPROVED_HOST,fingerprint,source_catalog
from cloakup.platform import TaskStore
from cloakup.sourcing import CaseStore
from cloakup.storage import Database
from test_agent_workspace import passing_test


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path);cases=CaseStore(tmp_path,db)
    case=cases.create('https://www.zhipin.com/web/chat/job-list');cases.bind_job(case,{'title':'交付顾问','requirements':'支持远程协作。'})
    case['anonymous_job']={'role':'交付顾问','requirements':['支持远程协作。']};cases.set_autonomy(case,True,source='test')
    p=cases.add_candidate(case,'p','https://www.zhipin.com/web/chat/index',{'summary':'职业资料','display_name':'甲'})
    p['conversation']={'chat_id':'12-0','receiver':'甲','job_title':'交付顾问'}
    cases.receive(case,p['id'],'q','结算周期是多久？');p['pending_question_id']='q';p['handled_reply_id']='q'
    p['conversation_semantics']={'state_key':'old','answer':{'action':'defer'}};p['decision_wait_until']=9e9
    other=cases.add_candidate(case,'o','https://www.zhipin.com/web/chat/index',{'summary':'职业资料'})
    cases.receive(case,other['id'],'q2','你们多久结算一次？');other['pending_question_id']='q2';other['conversation_semantics']={'state_key':'old'}
    cases.save(case)
    store=TaskStore(db);task=store.save({'config':{'name':'寻访任务','website':'https://www.zhipin.com/','scenario':'寻访','goal':'持续取得简历','engine':'boss_sourcing','case_ids':[case['id']]}})
    account=AccountExecution(db);account.set_task(task['id'],[case['id']],'running')
    db.put('disclosure_consent','recruiting-conversation',{'enabled':True,'mode':'anonymous_conversation','host':APPROVED_HOST})
    busy={'job':None}
    manager=NS(root=tmp_path,db=db,cases=cases,definitions=store,account_execution=account,model_client=lambda:None,
               task_plan_records=lambda:db.list('plan'),busy=lambda:busy['job'],planning=NS(definition=lambda tid:{'planning_context':{'revision':0}}))
    lab=AgentWorkspace(manager);manager.agent_workspace=lab
    todo=next(t for t in AttentionStore(db).reconcile([],[cases.load(case['id'])]) if t['kind']=='question' and t['candidate_id']==p['id'])
    yield manager,task,case,p,todo,busy
    for worker in lab.workers.values():worker.cancel()


def body(todo,**kw):
    return {'todo_id':todo['id'],'question':'候选人询问合作结算周期','answer':'按月结算，次月上旬到账。','reviewed':True,**kw}


def test_idle_submit_applies_faq_to_release_and_case_for_all_waiting_candidates(setup):
    m,t,c,p,todo,busy=setup
    before=m.cases.load(c['id']);release=m.agent_workspace.bootstrap(t['id'])
    record=fa.submit(m,t['id'],body(todo))
    assert record['status']=='applied'
    saved=m.cases.load(c['id'])
    assert saved['faq']==[{'question':'候选人询问合作结算周期','answer':'按月结算，次月上旬到账。','variants':[],'enabled':True}]
    # 不提升 config_revision，避免当前任务计划签名失效。
    assert saved['config_revision']==before['config_revision']
    # 新 FAQ 进入对话理解模型的已确认来源；原问题不进入 FAQ。
    assert any(s['kind']=='faq' and s['text']=='按月结算，次月上旬到账。' for s in source_catalog(saved).values())
    assert '结算周期是多久' not in str(saved['faq'])
    person=saved['candidates'][p['id']]
    assert person['pending_question_id']=='q' and 'conversation_semantics' not in person and 'decision_wait_until' not in person
    # 另一位等待同类问题答复的候选人也会按新 FAQ 重新理解。
    other=next(x for x in saved['candidates'].values() if x.get('pending_question_id')=='q2')
    assert 'conversation_semantics' not in other
    new_release=m.db.get('agent_release',t['id']);version=m.db.get('agent_bundle',new_release['version_id'])
    assert new_release['revision']==release['revision']+1 and version['origin']=='admin_todo_answer' and version['tested_version_id']==release['version_id']
    assert saved['agent_release_id']==version['id']
    assert any(a['kind']=='admin_answer_confirmed' for a in saved['audit'])
    stored=m.db.get('attention',todo['id']);assert stored['faq_answer']['status']=='applied' and stored['status']=='in_progress'


def test_running_executor_applies_at_safe_point(setup):
    m,t,c,p,todo,busy=setup
    busy['job']={'id':'run'}
    record=fa.submit(m,t['id'],body(todo))
    assert record['status']=='pending' and m.cases.load(c['id']).get('faq',[])==[]
    with pytest.raises(ValueError,match='等待生效'):fa.submit(m,t['id'],body(todo))
    assert fa.apply_pending(m)==1
    assert m.cases.load(c['id'])['faq'][0]['answer']=='按月结算，次月上旬到账。'
    assert m.db.get('faq_application',record['id'])['status']=='applied' and fa.apply_pending(m)==0


@pytest.mark.parametrize('change,match',[({'reviewed':False},'脱敏'),({'answer':'结算周期待确认'},'不确定'),
                                         ({'answer':'提成比例为20%'},'薪酬'),({'question':''},'通用问法')])
def test_unusable_answers_are_rejected_before_queueing(setup,change,match):
    m,t,c,p,todo,busy=setup
    with pytest.raises(ValueError,match=match):fa.submit(m,t['id'],body(todo,**change))
    assert not m.db.list('faq_application') and m.cases.load(c['id']).get('faq',[])==[]


def test_failed_application_marks_todo_without_stopping_executor(setup):
    m,t,c,p,todo,busy=setup
    busy['job']={'id':'run'};record=fa.submit(m,t['id'],body(todo))
    case=m.cases.load(c['id']);next(iter(case['candidates'].values()))['outbox'].append({'id':'x','status':'sending'});m.cases.save(case)
    assert fa.apply_pending(m)==0
    assert m.db.get('faq_application',record['id'])['status']=='failed'
    assert m.db.get('attention',todo['id'])['faq_answer']['status']=='failed'


def test_derived_release_stays_startable_only_if_it_differs_by_faq(setup):
    m,t,c,p,todo,busy=setup;lab=m.agent_workspace
    m.account_execution.set_task(t['id'],[c['id']],'paused')
    draft=lab.draft(t['id'],{});test=passing_test(lab,draft);lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':1,'test_id':test['id']})
    fa.submit(m,t['id'],body(todo))
    assert_release_ready(m,t['id'])
    release=m.db.get('agent_release',t['id']);version=m.db.get('agent_bundle',release['version_id'])
    version['bundle']['sources'][0]['facts'].append({'kind':'duties','value':'未经验收的职责','status':'confirmed'});m.db.put('agent_bundle',version['id'],version)
    with pytest.raises(ValueError,match='验收已失效') as raised:assert_release_ready(m,t['id'])
    assert raised.value.code=='job_not_validated'


def test_running_task_is_rebound_to_the_faq_release_and_keeps_sending(setup):
    """16:20 问答在运行中写入新版本后，任务仍显示运行但发送被关，直到 18:01 暂停再恢复。"""
    m,t,c,p,todo,busy=setup
    release=m.agent_workspace.bootstrap(t['id'])
    m.account_execution.set_task(t['id'],[c['id']],'running')
    assert m.account_execution.availability(c['id'])['state']!='release_changed'
    busy['job']={'id':'run'}
    fa.submit(m,t['id'],body(todo))
    assert fa.apply_pending(m)==1
    new_release=m.db.get('agent_release',t['id'])
    member=next(x for x in m.account_execution.members() if x['id']==t['id'])
    assert new_release['version_id']!=release['version_id'] and member['release_id']==new_release['version_id']
    assert member['status']=='running' and m.account_execution.availability(c['id'])['state']!='release_changed'


def test_task_bound_to_an_older_release_is_not_silently_rebound(setup):
    m,t,c,p,todo,busy=setup
    m.agent_workspace.bootstrap(t['id'])
    member=next(x for x in m.account_execution.members() if x['id']==t['id'])
    m.db.put('account_task',t['id'],{**member,'release_id':'some-older-version'})
    busy['job']={'id':'run'}
    fa.submit(m,t['id'],body(todo));fa.apply_pending(m)
    member=next(x for x in m.account_execution.members() if x['id']==t['id'])
    assert member['release_id']=='some-older-version'
    assert m.account_execution.availability(c['id'])['state']=='release_changed'


def _owed_newer_question(m,c,p):
    # 候选人后来又问了一个新问题，我方只回了“确认清楚后再回复”。
    case=m.cases.load(c['id']);m.cases.receive(case,p['id'],'q3','每月考核怎么定？');case=m.cases.load(c['id'])
    person=case['candidates'][p['id']];person['handled_reply_id']='q3'
    person['outbox'].append({'id':'ack','status':'sent','text':'好的。这部分我确认清楚后再回复您。',
                             'decision':{'pending_ack':True,'reply_id':'q3'},'receipt':{'message_id':'m-ack'}})
    person.setdefault('history',[]).append({'direction':'out','external_id':'m-ack','text':'好的。这部分我确认清楚后再回复您。'})
    return case,person


def test_a_later_answer_for_an_older_question_keeps_the_newest_owed_question(setup):
    # 11-24 16:10 候选人 30：管理员先后补了“考核任务”（最新问题）和“上班时间”（几天前的问题）。
    # 第二条生效时把待答指针改回了旧问题，最新问题就再也不会被补答。
    m,t,c,p,todo,busy=setup
    case,person=_owed_newer_question(m,c,p);person['pending_question_id']='q3';m.cases.save(case)
    fa.submit(m,t['id'],body(todo))   # 这条待办对应较早的问题 q
    assert m.cases.load(c['id'])['candidates'][p['id']]['pending_question_id']=='q3'


def test_safe_point_moves_a_stale_pointer_to_the_newest_owed_question(setup):
    # 已经指错的现场：每轮安全点按幂等规则纠正，不需要手工改数据。
    m,t,c,p,todo,busy=setup
    case,person=_owed_newer_question(m,c,p);person['pending_question_id']='q';m.cases.save(case)
    fa.apply_pending(m)
    saved=m.cases.load(c['id'])
    assert saved['candidates'][p['id']]['pending_question_id']=='q3'
    assert any(a['kind']=='answer_owed_restored' for a in saved['audit'])
    fa.apply_pending(m)
    assert sum(a['kind']=='answer_owed_restored' for a in m.cases.load(c['id'])['audit'])==1
