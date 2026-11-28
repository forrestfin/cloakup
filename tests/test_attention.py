import copy
import pytest
from cloakup.storage import Database
from cloakup.attention import AttentionStore,sourcing_overview


def test_access_dedup_clear_and_reopen(tmp_path):
    s=AttentionStore(Database(tmp_path));r={'id':'a'*32,'status':'waiting','interaction':{'kind':'browser_access','reason':'login','prompt':'请登录'}}
    first=s.reconcile([r],[]);assert len(first)==1 and first[0]['status']=='pending'
    assert len(s.reconcile([r],[])[0]['history'])==1
    with pytest.raises(ValueError):s.update(first[0]['id'],{'status':'resolved','note':'已登录'})
    r['interaction']=None;r['status']='running'
    assert s.reconcile([r],[])[0]['status']=='resolved'
    r['interaction']={'kind':'browser_access','reason':'login'}
    reopened=s.reconcile([r],[])[0]
    assert reopened['status']=='pending' and reopened['history'][-1]['action']=='reopened'
    assert AttentionStore(Database(tmp_path)).db.list('attention')[0]['id']==first[0]['id']


def test_questions_and_manual_notes_never_send_or_clear_delivery(tmp_path):
    db=Database(tmp_path);s=AttentionStore(db)
    case={'id':'c'*32,'job':{'label':'Test'},'candidates':{'one':{'id':'one','inbox':[{'external_id':'in1','text':'社保怎么缴纳？'}],'outbox':[{'id':'m1','status':'delivery_unknown'}]}}}
    unchanged=copy.deepcopy(case)
    todos=s.reconcile([], [case]);assert len(todos)==1
    q=next(t for t in todos if t['kind']=='question')
    with pytest.raises(ValueError):s.update(q['id'],{'status':'resolved','note':''})
    s.update(q['id'],{'status':'resolved','note':'顾问已在平台接续'})
    assert next(t for t in s.reconcile([],[case]) if t['id']==q['id'])['status']=='resolved'
    assert not any(t['kind']=='delivery' for t in todos)
    assert case==unchanged
    assert db.list('run')==[]
    case['candidates']['one']['inbox']=[{'external_id':'in2','text':'好的谢谢'}]
    assert not any(t['kind']=='question' and t['status']=='pending' for t in s.reconcile([],[case]))


def test_old_receipt_reminders_retire_without_changing_evidence_or_replay_lock(tmp_path):
    from cloakup.action_gateway import ActionLedger,ActionBlocked
    import hashlib
    db=Database(tmp_path);store=AttentionStore(db);ledger=ActionLedger(db)
    ident=hashlib.sha256(b'delivery:old').hexdigest()[:32]
    db.put('attention',ident,{'id':ident,'kind':'delivery','title':'消息回执核验超时，请管理员处理',
                             'status':'pending','created_at':1,'updated_at':1,'history':[{'at':1,'action':'created'}]})
    op=ledger.claim(task_id='case:case',resource='candidate:p',capability='send_message',identity='old',run_id='r')
    ledger.transition(op,'executing');ledger.transition(op,'unknown',reason='missing receipt')
    case={'id':'case','job':{'label':'岗位'},'candidates':{'p':{'id':'p','inbox':[],
        'outbox':[{'id':'old','status':'delivery_unknown','verification':{'requires_admin':True}}]}}}
    original=copy.deepcopy(case)
    todos=store.reconcile([], [case]);retired=next(t for t in todos if t['id']==ident)
    assert retired['status']=='resolved' and retired['auto_resolved']
    assert retired['retired_reason']=='routine_delivery_observation'
    assert '未改变发送状态' in retired['history'][-1]['note']
    assert case==original and ledger.get(op)['state']=='unknown'
    with pytest.raises(ActionBlocked):ledger.claim(task_id='case:case',resource='candidate:p',capability='send_message',identity='old')
    assert not any(t['kind'] in ('delivery','action') and t['status']=='pending' for t in todos)
    assert len(store.reconcile([], [case])[0]['history'])==2


@pytest.mark.parametrize('status',['delivered','read'])
def test_delivered_or_read_messages_never_create_receipt_todos(tmp_path,status):
    store=AttentionStore(Database(tmp_path))
    case={'id':'case','job':{'label':'岗位'},'candidates':{'p':{'id':'p','inbox':[],
        'outbox':[{'id':'m','status':'sent','receipt':{'delivery_status':status},'sent_at':1}]}}}
    assert store.reconcile([], [case])==[]


def test_old_superseded_failures_do_not_flood_todos(tmp_path):
    s=AttentionStore(Database(tmp_path))
    runs=[{'id':'a'*32,'task_id':'task','created':1,'status':'failed','error':'old'}, {'id':'b'*32,'task_id':'task','created':2,'status':'running'}]
    assert s.reconcile(runs,[])==[]


def test_generic_operator_and_planning_failures_create_recoverable_todos(tmp_path):
 db=Database(tmp_path);s=AttentionStore(db)
 db.put('planning_request','p',{'id':'p','task_id':'task','status':'failed','error':'model unavailable'})
 run={'id':'run','status':'waiting','interaction':{'id':'q','kind':'reply','prompt':'Please clarify goal'}}
 todos=s.reconcile([run],[])
 assert {t['kind'] for t in todos}=={'operator','planning'}
 db.put('planning_request','p',{'id':'p','task_id':'task','status':'succeeded'})
 run['interaction']=None;run['status']='running'
 assert all(t['status']=='resolved' for t in s.reconcile([run],[]))


def test_platform_cards_and_routine_interest_do_not_create_fact_questions(tmp_path):
 from cloakup.storage import Database
 from cloakup.attention import AttentionStore
 store=AttentionStore(Database(tmp_path))
 texts=['对方想发送附件简历给您，您是否同意\n拒绝\n同意','我想要和您交换微信，您是否同意\n拒绝\n同意','BOSS好😆，我对这份工作很感兴趣，你看我有机会深入沟通下吗？','底薪、提成是如何的？']
 c={'id':'c','job':{'label':'岗位'},'candidates':{'p':{'id':'p','status':'waiting_reply','inbox':[{'external_id':str(i),'text':t} for i,t in enumerate(texts)],'outbox':[]}}}
 todos=store.reconcile([], [c]);assert len(todos)==1 and todos[0]['question']==texts[-1]


def test_browser_failures_stay_with_the_executor_and_login_still_alerts(tmp_path):
 store=AttentionStore(Database(tmp_path))
 old={'id':'old','mode':'boss_contact','created':1,'status':'failed','params':{'case_id':'c'},'error':'核对期间资料或草稿已变化；未进行联系'}
 assert store.reconcile([old],[])==[]
 old['error']='发送超时，回执未知'
 assert store.reconcile([old],[])==[]
 old['error']='登录已失效，请重新登录'
 assert store.reconcile([old],[])[0]['kind']=='execution'


def test_missing_knowledge_is_sent_without_waiting_for_the_browser(tmp_path):
 store=AttentionStore(Database(tmp_path))
 store.db.put('disclosure_consent','recruiting-conversation',{'mode':'anonymous_conversation','host':'example.invalid','enabled':True})
 case={'id':'case','job':{'label':'岗位'},'faq':[],'candidates':{'p':{'id':'p','status':'waiting_reply','inbox':[{'external_id':'q1','text':'结算周期是多久？'}],'outbox':[],'decision_hold':{'reason':'对话仍有无法可靠脱敏的片段','retry_at':9e9},'platform_actions':[{'id':'a1','kind':'ask_resume','status':'awaiting_verification'}]}}}
 todos=store.reconcile([], [case])
 assert len(todos)==1 and todos[0]['kind']=='question' and todos[0]['question']=='结算周期是多久？'
 case['faq']=[{'question':'结算周期是多久？','answer':'按已确认的结算周期回复。','variants':[],'enabled':True}]
 assert all(t['status']=='resolved' for t in store.reconcile([], [case]))


def test_blank_detail_recovery_is_not_sent_to_the_operator(tmp_path):
    store=AttentionStore(Database(tmp_path))
    run={'id':'run','status':'running','created':1,'selection_recovery':{'case':{
        'status':'blocked','task_id':'task','incident_id':'pane','attempts':3,'updated_at':1,
        'diagnostics':{'reason':'detail_not_open'},'reason':'右侧详情尚未打开'}}}
    assert store.reconcile([run],[{'id':'case','job':{'label':'岗位'},'candidates':{}}])==[]
    run['selection_recovery']['case'].update(status='deferred',diagnostics={'reason':'header_job_unverified'})
    assert store.reconcile([run],[{'id':'case','job':{'label':'岗位'},'candidates':{}}])==[]


def test_selection_recovery_escalates_once_and_closes_only_after_verified_recovery(tmp_path):
 db=Database(tmp_path);store=AttentionStore(db)
 case={'id':'case','job':{'label':'岗位'},'candidates':{}}
 state={'status':'retrying','task_id':'task','incident_id':'episode-one','attempts':1,'max_attempts':3,
        'reason':'会话列表尚未稳定','failures':['private-chat-id'],'diagnostics':['not_clickable'],'updated_at':1}
 run={'id':'run','status':'running','account_runner':True,'created':1,'selection_recovery':{'case':state}}
 assert store.reconcile([run],[case])==[]
 state.update(status='blocked',attempts=3,updated_at=2)
 todo=store.reconcile([run],[case])[0]
 assert todo['kind']=='execution_recovery' and todo['task_id']=='task' and todo['case_id']=='case'
 assert todo['recovery_attempts']==3 and todo['job_label']=='岗位'
 assert 'private-chat-id' not in str(todo) and 'diagnostics' not in todo
 assert len(store.reconcile([run],[case]))==1
 assert len(store.reconcile([run],[case])[0]['history'])==1
 with pytest.raises(ValueError):store.update(todo['id'],{'status':'resolved','note':'开始运行了'})
 store.update(todo['id'],{'status':'in_progress','note':'已接管检查页面'})
 state.update(status='retrying',updated_at=3)
 assert store.reconcile([run],[case])[0]['status']=='in_progress'
 assert store.reconcile([], [case])[0]['status']=='in_progress'
 state.update(status='recovered',updated_at=4)
 cleared=store.reconcile([run],[case])[0]
 assert cleared['status']=='resolved' and cleared['auto_resolved']
 state.update(status='blocked',incident_id='episode-two',updated_at=5)
 again=store.reconcile([run],[case])
 assert len(again)==2 and len([t for t in again if t['status']=='pending'])==1
 assert next(t for t in again if t['status']=='pending')['id']!=todo['id']


def test_selection_recovery_maps_paused_owner_and_ignores_unrelated_members(tmp_path):
 db=Database(tmp_path);store=AttentionStore(db)
 db.put('account_task','affected',{'id':'affected','case_ids':['case'],'status':'paused'})
 db.put('account_task','other',{'id':'other','case_ids':['another-case'],'status':'running'})
 run={'id':'run','status':'running','account_runner':True,'selection_recovery':{'case':{'status':'blocked','attempts':3}}}
 todos=store.reconcile([run],[])
 assert len(todos)==1 and todos[0]['task_id']=='affected'
 run['status']='succeeded'
 assert store.reconcile([run],[])[0]['status']=='pending'
 run['selection_recovery']['case'].update(status='recovered',updated_at=2)
 assert store.reconcile([run],[])[0]['status']=='resolved'


def test_selection_recovery_new_success_supersedes_old_run_block(tmp_path):
 store=AttentionStore(Database(tmp_path))
 old={'id':'old','status':'running','created':1,'selection_recovery':{'c':{'task_id':'task','status':'blocked','updated_at':1}}}
 assert store.reconcile([old],[])[0]['status']=='pending'
 new={'id':'new','status':'running','created':2,'selection_recovery':{'c':{'task_id':'task','status':'recovered','updated_at':3}}}
 assert store.reconcile([old,new],[])[0]['status']=='resolved'


def test_recovery_incident_survives_owner_restart_without_duplicate(tmp_path):
 store=AttentionStore(Database(tmp_path))
 old={'id':'old','status':'running','created':1,'selection_recovery':{'c':{'task_id':'task','incident_id':'one','status':'blocked','updated_at':1}}}
 first=store.reconcile([old],[])[0]
 new=copy.deepcopy(old);new.update(id='new',created=2)
 new['selection_recovery']['c'].update(status='retrying',updated_at=2)
 copied=store.reconcile([old,new],[])
 assert len(copied)==1 and copied[0]['id']==first['id'] and copied[0]['run_id']=='new'
 new['selection_recovery']['c'].update(status='recovered',updated_at=3)
 assert store.reconcile([old,new],[])[0]['status']=='resolved'
 new['selection_recovery']['c'].update(status='blocked',incident_id='two',updated_at=4)
 renewed=store.reconcile([old,new],[])
 assert len(renewed)==2 and len([t for t in renewed if t['status']=='pending'])==1
 assert next(t for t in renewed if t['status']=='pending')['incident_id']=='two'


@pytest.mark.parametrize('status',['interrupted','failed'])
def test_persisted_block_is_reported_after_restart_before_first_monitor(tmp_path,status):
 store=AttentionStore(Database(tmp_path))
 old={'id':'old','status':status,'created':1,'selection_recovery':{'c':{
      'task_id':'task','incident_id':'draft-held','status':'blocked','attempts':2,'max_attempts':3,'updated_at':1}}}
 todo=store.reconcile([old],[])[0]
 assert todo['kind']=='execution_recovery' and todo['status']=='pending'
 assert todo['recovery_attempts']==2 and '自动恢复未能安全完成' in todo['detail']
 assert '连续恢复' not in todo['detail']
 recovered={'id':'new','status':'running','created':2,'selection_recovery':{'c':{
            'task_id':'task','incident_id':'draft-held','status':'recovered','updated_at':2}}}
 assert store.reconcile([old,recovered],[])[0]['status']=='resolved'


def _question_case(outbox):
    return {'id':'c'*32,'job':{'label':'岗位'},'faq':[],'candidates':{'one':{'id':'one','status':'waiting_reply',
            'inbox':[{'external_id':'q1','text':'社保怎么缴纳？'}],'outbox':outbox}}}


def test_question_todo_closes_once_a_real_answer_is_delivered(tmp_path):
    # 11-24 核对：10 条“候选人问题需顾问确认”里 6 条早已回答，待办却一直挂着。
    s=AttentionStore(Database(tmp_path))
    ack={'id':'a','status':'sent','text':'这部分我确认清楚后再回复您。','decision':{'reply_id':'q1','pending_ack':True,'answer_route':{'action':'defer'}}}
    case=_question_case([ack])
    assert [t['status'] for t in s.reconcile([],[case]) if t['kind']=='question']==['pending']
    # 确认收到、追聊、发送中或回执未知都不算回答。
    for msg in ({'id':'n','status':'sent','decision':{'reply_id':'q1','nudge':True,'answer_route':{'action':'answer'}}},
                {'id':'u','status':'delivery_unknown','decision':{'reply_id':'q1','answer_route':{'action':'answer'}}}):
        case['candidates']['one']['outbox'].append(msg)
        assert [t['status'] for t in s.reconcile([],[case]) if t['kind']=='question']==['pending']
    case['candidates']['one']['outbox'].append({'id':'b','status':'sent','text':'按已确认安排缴纳。','decision':{'reply_id':'q1','answer_route':{'action':'answer','source':'harness_context'}}})
    todo=next(t for t in s.reconcile([],[case]) if t['kind']=='question')
    assert todo['status']=='resolved' and todo['auto_resolved']


def test_privacy_hold_raises_a_todo_until_the_hold_clears(tmp_path):
    s=AttentionStore(Database(tmp_path))
    case=_question_case([])
    case['candidates']['one']['inbox']=[{'external_id':'q1','text':'好的'}]
    case['candidates']['one']['decision_hold']={'reason':'对话含无法可靠脱敏的片段，请管理员在本机复核或接续；没有将该对话发送模型','at':1,'retry_at':2}
    [todo]=[t for t in s.reconcile([],[case]) if t['kind']=='decision']
    assert todo['status']=='pending' and todo['candidate_id']=='one' and '人工接续' in todo['title']
    with pytest.raises(ValueError):s.update(todo['id'],{'status':'resolved','note':'x'})
    # 重新暂缓（时间戳变化）不重复生成。
    case['candidates']['one']['decision_hold'].update(at=5,retry_at=9,kind='privacy')
    assert len([t for t in s.reconcile([],[case]) if t['kind']=='decision'])==1
    case['candidates']['one'].pop('decision_hold')
    assert [t['status'] for t in s.reconcile([],[case]) if t['kind']=='decision']==['resolved']


def test_other_decision_holds_stay_with_the_executor(tmp_path):
    s=AttentionStore(Database(tmp_path))
    case=_question_case([])
    case['candidates']['one']['inbox']=[{'external_id':'q1','text':'好的'}]
    case['candidates']['one']['decision_hold']={'reason':'Harness决策未完成：RuntimeError','at':1,'retry_at':2}
    assert not any(t['kind']=='decision' for t in s.reconcile([],[case]))


def test_clarifying_followup_counts_as_the_answer(tmp_path):
    s=AttentionStore(Database(tmp_path))
    case=_question_case([{'id':'c','status':'sent','text':'补充回复您之前的问题：……','decision':{'reply_id':'q1','nudge':True,'nudge_variant':'clarify_previous'}}])
    assert [t['status'] for t in s.reconcile([],[case]) if t['kind']=='question']==[]


def test_long_isolated_chat_raises_a_todo_until_it_is_verified(tmp_path):
    # 11-24：管理员只该收到能处理的提醒。还在自动重试的会话一天后才提醒，并写成大白话。
    import time
    s=AttentionStore(Database(tmp_path))
    case=_question_case([])
    case['candidates']['one'].update(inbox=[{'external_id':'q1','text':'好的'}],conversation={'chat_id':'12-0'},profile={'display_name':'候选人 22'})
    case['deferred_chat_ids']=['12-0'];case['deferred_chat_since']={'12-0':time.time()-3*3600}
    assert not any(t['kind']=='isolated_chat' for t in s.reconcile([],[case]))
    case['deferred_chat_since']['12-0']=time.time()-25*3600
    [todo]=[t for t in s.reconcile([],[case]) if t['kind']=='isolated_chat']
    assert todo['status']=='pending' and todo['candidate_id']=='one' and todo['title'].startswith('候选人 22')
    assert '你可以' in todo['detail'] and 'target_row' not in todo['detail']
    case['deferred_chat_ids']=[]
    assert [t['status'] for t in s.reconcile([],[case]) if t['kind']=='isolated_chat']==['resolved']


def test_missing_chat_todo_explains_what_to_do_and_clears_when_back(tmp_path):
    s=AttentionStore(Database(tmp_path))
    case=_question_case([])
    case['candidates']['one'].update(conversation={'chat_id':'12-0'},profile={'display_name':'候选人 7'})
    case['missing_chat_ids']=['12-0']
    [todo]=[t for t in s.reconcile([],[case]) if t['kind']=='missing_chat']
    assert '找不到' in todo['title'] and '30 天' in todo['detail'] and '你可以' in todo['detail']
    s.update(todo['id'],{'status':'resolved','note':'顾问已在 BOSS 手动联系'})   # 管理员可以自己关闭
    case['missing_chat_ids']=[]
    assert all(t['status']=='resolved' for t in s.reconcile([],[case]) if t['kind']=='missing_chat')


@pytest.mark.parametrize('record,title',[({'healthy':False,'reason':'draft_present','needs_admin':True},'没发出去的文字'),
                                         ({'healthy':False,'reason':'loading_stuck','exhausted':True,'kind':'chat'},'页面打不开')])
def test_browser_health_todo_only_for_things_the_admin_can_fix(tmp_path,record,title):
    import time
    db=Database(tmp_path);s=AttentionStore(db)
    db.put('page_health','boss',{'id':'boss','at':time.time(),**record})
    [todo]=[t for t in s.reconcile([],[]) if t['kind']=='browser_health']
    assert title in todo['title'] and '127.0.0.1:6080' in todo['detail']
    with pytest.raises(ValueError):s.update(todo['id'],{'status':'resolved','note':'x'})
    db.put('page_health','boss',{'id':'boss','at':time.time(),'healthy':True})
    assert [t['status'] for t in s.reconcile([],[]) if t['kind']=='browser_health']==['resolved']


def test_login_found_by_the_health_check_uses_the_existing_login_flow(tmp_path):
    import time
    db=Database(tmp_path);s=AttentionStore(db)
    db.put('page_health','boss',{'id':'boss','at':time.time(),'healthy':False,'reason':'login_required','needs_admin':True})
    assert not any(t['kind']=='browser_health' for t in s.reconcile([],[]))

# 11-24 多岗位任务 Task 9：岗位变更与逐岗位验收的待办，状态解除后自动关闭。
def _job_case(cid,label,people=None):return {'id':cid,'job':{'label':label},'candidates':people or {}}


def _change(db,ident,status,at,action='add',case_id='b',error=None):
    db.put('job_change',ident,{'id':ident,'task_id':'t','action':action,'case_id':case_id,'status':status,'at':at,**({'error':error} if error else {})})


def test_job_change_failed_todo_appears_and_closes_on_retry_or_dismiss(tmp_path):
    import time
    db=Database(tmp_path);s=AttentionStore(db);cases=[_job_case('b','数据顾问')]
    _change(db,'c1','failed',time.time()-10,error='新岗位隔离验收未通过（8/9 项通过）')
    [todo]=[t for t in s.reconcile([],cases) if t['kind']=='job_change']
    assert todo['status']=='pending' and todo['detail']=='岗位「数据顾问」加入未完成：新岗位隔离验收未通过（8/9 项通过）。已做：未改动正在运行的任务。需要：查看失败场景后重试或放弃。'
    _change(db,'c2','testing',time.time())
    assert [t['status'] for t in s.reconcile([],cases) if t['kind']=='job_change']==['resolved']
    # 管理员标记放弃：待办保持关闭，不会被下一轮重新打开
    _change(db,'c3','failed',time.time()+5,case_id='x',action='withdraw',error='岗位已不属于该任务的生效版本')
    todo=next(t for t in s.reconcile([],cases) if t.get('job_change_id')=='c3')
    s.update(todo['id'],{'status':'resolved','note':'放弃这次变更'})
    assert next(t for t in s.reconcile([],cases) if t['id']==todo['id'])['status']=='resolved'


def test_job_change_stuck_todo_appears_after_30_minutes_and_closes_once_applied(tmp_path):
    import time
    db=Database(tmp_path);s=AttentionStore(db);cases=[_job_case('a','数据顾问')]
    db.put('account_task','t',{'id':'t','case_ids':['a'],'status':'running'})
    _change(db,'w1','pending',time.time()-29*60,action='withdraw',case_id='a')
    assert not [t for t in s.reconcile([],cases) if t['kind']=='job_change_stuck']
    _change(db,'w1','pending',time.time()-31*60,action='withdraw',case_id='a')
    [todo]=[t for t in s.reconcile([],cases) if t['kind']=='job_change_stuck']
    assert todo['status']=='pending' and todo['detail'].startswith('岗位变更等待执行器安全点超过 30 分钟') and '撤下岗位「数据顾问」' in todo['detail']
    with pytest.raises(ValueError):s.update(todo['id'],{'status':'resolved','note':'x'})
    _change(db,'w1','applied',time.time()-31*60,action='withdraw',case_id='a')
    assert [t['status'] for t in s.reconcile([],cases) if t['kind']=='job_change_stuck']==['resolved']


def test_job_change_stuck_needs_a_running_task(tmp_path):
    import time
    db=Database(tmp_path);s=AttentionStore(db)
    db.put('account_task','t',{'id':'t','case_ids':['a'],'status':'paused'})
    _change(db,'w1','pending',time.time()-31*60,action='withdraw',case_id='a')
    assert not [t for t in s.reconcile([],[]) if t['kind']=='job_change_stuck']


def _bound_task(db,status='running',release='v1',bound='v1'):
    db.put('account_task','t',{'id':'t','case_ids':['a','b'],'status':status,'release_id':bound})
    db.put('agent_release','t',{'id':'t','version_id':release,'validation':'passed'})


def test_job_not_validated_todo_appears_for_bound_running_task_and_closes_when_revalidated(tmp_path,monkeypatch):
    from cloakup import agent_workspace
    db=Database(tmp_path);s=AttentionStore(db);cases=[_job_case('a','数据顾问'),_job_case('b','运营专员')]
    _bound_task(db);state={'validated':{'a'},'shared':True}
    monkeypatch.setattr(agent_workspace,'validation_state',lambda db,tid:({},set(state['validated']),state['shared']))
    [todo]=[t for t in s.reconcile([],cases) if t['kind']=='job_not_validated']
    assert todo['case_id']=='b' and todo['task_id']=='t' and todo['detail']=='岗位「运营专员」验收已失效，已只停这个岗位的发送；需要重新验收该岗位。'
    with pytest.raises(ValueError):s.update(todo['id'],{'status':'resolved','note':'x'})
    state['validated']={'a','b'}
    assert [t['status'] for t in s.reconcile([],cases) if t['kind']=='job_not_validated']==['resolved']


@pytest.mark.parametrize('variant',['paused','version_mismatch','release_changed'])
def test_job_not_validated_leaves_release_changed_and_paused_tasks_alone(tmp_path,monkeypatch,variant):
    from cloakup import agent_workspace
    db=Database(tmp_path);s=AttentionStore(db)
    _bound_task(db,status='paused' if variant=='paused' else 'running',bound='v0' if variant=='version_mismatch' else 'v1')
    monkeypatch.setattr(agent_workspace,'validation_state',lambda db,tid:({},set(),variant!='release_changed'))
    assert not [t for t in s.reconcile([],[]) if t['kind']=='job_not_validated']


def test_job_outreach_blocked_todo_appears_at_three_deferrals_today_and_closes_next_day(tmp_path):
    from cloakup.autonomy import day_key
    db=Database(tmp_path);s=AttentionStore(db);cases=[_job_case('a','数据顾问')]
    today=day_key()
    old={'id':'r0','status':'failed','account_runner':True,'created':1,'outreach_deferrals':{today:{'a':1}}}
    run={'id':'r1','status':'running','account_runner':True,'created':2,'outreach_deferrals':{today:{'a':1}}}
    assert not [t for t in s.reconcile([old,run],cases) if t['kind']=='job_outreach_blocked']
    run['outreach_deferrals'][today]['a']=2  # 重启前后的执行器记录合计当天次数
    [todo]=[t for t in s.reconcile([old,run],cases) if t['kind']=='job_outreach_blocked']
    assert todo['case_id']=='a' and '只停这个岗位的首聊' in todo['detail'] and '3 次' in todo['detail'] and '次日自动解除' in todo['detail']
    with pytest.raises(ValueError):s.update(todo['id'],{'status':'resolved','note':'x'})
    old['outreach_deferrals']={'2000-01-01':{'a':9}};run['outreach_deferrals']={'2000-01-01':{'a':9}}
    assert [t['status'] for t in s.reconcile([old,run],cases) if t['kind']=='job_outreach_blocked']==['resolved']


def test_job_removable_hint_appears_for_withdrawn_job_without_open_chats(tmp_path):
    db=Database(tmp_path);s=AttentionStore(db)
    talking={'p':{'id':'p','status':'contacted','conversation':{'id':'x'}}}
    cases=[_job_case('a','数据顾问'),_job_case('b','运营专员',talking)]
    db.put('account_task','t',{'id':'t','case_ids':['a','b'],'status':'running'})
    db.put('task','t',{'id':'t','config':{'members':[{'case_id':'a','weight':1,'outreach':'active'},{'case_id':'b','weight':1,'outreach':'withdrawn'}]}})
    assert not [t for t in s.reconcile([],cases) if t['kind']=='job_removable']
    talking['p']['status']='closed'
    [todo]=[t for t in s.reconcile([],cases) if t['kind']=='job_removable']
    assert todo['case_id']=='b' and '运营专员' in todo['detail'] and todo['status']=='pending'
    db.put('task','t',{'id':'t','config':{'members':[{'case_id':'a','weight':1,'outreach':'active'}]}})
    db.put('account_task','t',{'id':'t','case_ids':['a'],'status':'running'})
    assert [t['status'] for t in s.reconcile([],cases) if t['kind']=='job_removable']==['resolved']


# 复审 Important 1：运行中任务验收整体不再一致（shared=False）时执行器停发全部岗位，必须有任务级待办。
def test_task_not_validated_todo_appears_when_whole_validation_lapses_and_closes_when_revalidated(tmp_path,monkeypatch):
    from cloakup import agent_workspace
    db=Database(tmp_path);s=AttentionStore(db);cases=[_job_case('a','数据顾问'),_job_case('b','运营专员')]
    _bound_task(db);state={'validated':set(),'shared':False}
    monkeypatch.setattr(agent_workspace,'validation_state',lambda db,tid:({},set(state['validated']),state['shared']))
    todos=s.reconcile([],cases)
    [todo]=[t for t in todos if t['kind']=='task_not_validated']
    assert not [t for t in todos if t['kind']=='job_not_validated']
    assert todo['task_id']=='t' and '全部岗位已停止发送' in todo['detail'] and '收件读取照常' in todo['detail'] and '重新验收' in todo['detail'] and '无需发布' in todo['detail'] and '发布；' not in todo['detail']
    with pytest.raises(ValueError):s.update(todo['id'],{'status':'resolved','note':'x'})
    state.update(validated={'a','b'},shared=True)
    assert [t['status'] for t in s.reconcile([],cases) if t['kind']=='task_not_validated']==['resolved']


def test_task_not_validated_skips_version_mismatch(tmp_path,monkeypatch):
    from cloakup import agent_workspace
    db=Database(tmp_path);s=AttentionStore(db);_bound_task(db,bound='v0')
    monkeypatch.setattr(agent_workspace,'validation_state',lambda db,tid:({},set(),False))
    assert not [t for t in s.reconcile([],[]) if t['kind'] in ('task_not_validated','job_not_validated')]



# 11-27 首聊前初筛：同一岗位当天初筛失败 ≥3 次且之后没有成功过 → 待办；之后一次成功或跨天自动解除。
def test_screening_unavailable_todo_opens_and_closes(tmp_path):
    from cloakup.autonomy import day_key
    db=Database(tmp_path);s=AttentionStore(db);cases=[_job_case('a','数据顾问')]
    today=day_key();kind=lambda todos:[t for t in todos if t['kind']=='screening_unavailable']
    old={'id':'r0','status':'failed','account_runner':True,'created':1,'screening_failures':{today:{'a':{'n':1,'last_failed_at':100.0}}}}
    run={'id':'r1','status':'running','account_runner':True,'created':2,'screening_failures':{today:{'a':{'n':1,'last_failed_at':200.0}}}}
    assert not kind(s.reconcile([old,run],cases))  # n=2 不提醒
    run['screening_failures'][today]['a']['n']=2  # 重启前后的执行器记录合计当天次数
    [todo]=kind(s.reconcile([old,run],cases))
    assert todo['id'] and todo['case_id']=='a' and todo['title']=='岗位初筛暂不可用'
    assert todo['detail']=='岗位「数据顾问」今天首聊前初筛已连续失败 3 次。已做：失败的候选人先放下、稍后再看，其他工作照常，没有向候选人发消息。需要：查看模型网关是否可用、执行浏览器里推荐牛人详情能否打开；恢复后此待办自动解除。'
    run['screening_failures'][today]['a']['reason']='没有取到推荐页详情，只凭卡片不初筛'  # 11-27：待办写明最近一次原因，便于区分网关和页面问题
    [todo]=kind(s.reconcile([old,run],cases))
    assert '（最近一次：没有取到推荐页详情，只凭卡片不初筛）。已做' in todo['detail']
    with pytest.raises(ValueError):s.update(todo['id'],{'status':'resolved','note':'x'})  # 只随状态自动解除，不能手动关闭
    old['screening_failures'][today]['a']['last_ok_at']=300.0  # 另一条运行记录里的成功也算
    assert [t['status'] for t in kind(s.reconcile([old,run],cases))]==['resolved']
    run['screening_failures'][today]['a']['last_failed_at']=400.0  # 成功后只又失败一次：不算连续 3 次
    assert [t['status'] for t in kind(s.reconcile([old,run],cases))]==['resolved']
    run['screening_failures'][today]['a'].update(n=3,last_failed_at=500.0)  # 成功后又连续失败 3 次：再次提醒
    assert [t['status'] for t in kind(s.reconcile([old,run],cases))]==['pending']
    old['screening_failures']={'2000-01-01':{'a':{'n':9,'last_failed_at':500.0}}};run['screening_failures']={'2000-01-01':{'a':{'n':9,'last_failed_at':500.0}}}
    assert [t['status'] for t in kind(s.reconcile([old,run],cases))]==['resolved']  # 跨天自动解除


def test_screening_unavailable_todo_not_reopened_by_single_failure_after_recovery(tmp_path):
    # 11-27 终审：计数是“连续失败”。失败 3 次 → 成功（计数清零）→ 再失败 1 次，不再提醒。
    from cloakup.autonomy import day_key
    s=AttentionStore(Database(tmp_path));cases=[_job_case('a','数据顾问')];today=day_key()
    kind=lambda todos:[t for t in todos if t['kind']=='screening_unavailable']
    run={'id':'r','status':'running','account_runner':True,'created':1,'screening_failures':{today:{'a':{'n':3,'last_failed_at':100.0}}}}
    assert [t['status'] for t in kind(s.reconcile([run],cases))]==['pending']
    run['screening_failures'][today]['a']={'n':1,'last_failed_at':300.0,'last_ok_at':200.0}  # 执行器在成功时把 n 清零，之后只失败 1 次
    assert [t['status'] for t in kind(s.reconcile([run],cases))]==['resolved']
    # 另一条（重启前）记录里的失败早于最新成功，不计入连续次数。
    old={'id':'r0','status':'failed','account_runner':True,'created':0,'screening_failures':{today:{'a':{'n':5,'last_failed_at':150.0}}}}
    assert [t['status'] for t in kind(s.reconcile([old,run],cases))]==['resolved']


@pytest.mark.parametrize('value',['oops',['a'],{'TODAY':'bad'},{'TODAY':{'a':'3'}},{'TODAY':{'a':{'n':'3','last_failed_at':1}}},{'TODAY':{'a':{'n':3,'last_failed_at':None}}}])
def test_malformed_screening_failures_do_not_break_reconcile(tmp_path,value):
    from cloakup.autonomy import day_key
    s=AttentionStore(Database(tmp_path))
    if isinstance(value,dict):value={day_key() if k=='TODAY' else k:v for k,v in value.items()}
    run={'id':'r','status':'running','account_runner':True,'created':1,'screening_failures':value}
    assert not [t for t in s.reconcile([run],[]) if t['kind']=='screening_unavailable']

@pytest.mark.parametrize('value',['oops',['a'],{'TODAY':'bad'},{'TODAY':{'a':'3'}},{'TODAY':None}])
def test_malformed_outreach_deferrals_do_not_break_reconcile(tmp_path,value):
    from cloakup.autonomy import day_key
    s=AttentionStore(Database(tmp_path))
    if isinstance(value,dict):value={day_key() if k=='TODAY' else k:v for k,v in value.items()}
    run={'id':'r','status':'running','account_runner':True,'created':1,'outreach_deferrals':value}
    assert not [t for t in s.reconcile([run],[]) if t['kind']=='job_outreach_blocked']


# 11-24 20:40 沟通页卡在启动页：任务被系统暂停后执行器不再重试，待办却写“每 10 分钟会再自动试一次”。待办文字按真实状态写。
RUNNER=[{'id':'r'*32,'account_runner':True,'status':'running','created':1}]


def _stuck_page(db,at):
    db.put('page_health','boss',{'id':'boss','at':at,'healthy':False,'reason':'content_missing','exhausted':True,'kind':'chat'})


def test_page_blocked_pause_todo_tells_the_truth_and_closes_on_auto_resume(tmp_path):
    import time
    from cloakup.account_execution import AccountExecution
    db=Database(tmp_path);s=AttentionStore(db);a=AccountExecution(db);now=time.time()
    a.set_task('task',['case'],'paused',actor='system',reason='selection_blocked:editor_unavailable');_stuck_page(db,now)
    db.put('page_recovery','boss',{'id':'boss','task_ids':['task'],'paused_at':a.members()[0]['history'][-1]['at'],'page_stuck':True,'attempts':2,'next_at':now+400,'result':'unhealthy','reason':'content_missing'})
    todos=[t for t in s.reconcile(RUNNER,[]) if t['kind']=='browser_health' and t['status']!='resolved']
    assert len(todos)==1 and '已暂停' in todos[0]['title']
    detail=todos[0]['detail']
    assert '每 10 分钟' in detail and '下次约' in detail and '自动恢复任务' in detail and '127.0.0.1:6080' in detail and '已试 2 次' in detail
    with pytest.raises(ValueError):s.update(todos[0]['id'],{'status':'resolved','note':'x'})
    a.set_task('task',['case'],'running',actor='system',reason='page_recovered')
    db.put('page_health','boss',{'id':'boss','at':time.time(),'healthy':True})
    assert all(t['status']=='resolved' for t in s.reconcile(RUNNER,[]) if t['kind']=='browser_health')


def test_page_blocked_pause_without_a_running_executor_asks_for_a_manual_resume(tmp_path):
    # 11-24 20:40 沟通页卡在启动页：执行器没在运行（如服务重启后）就没有人重试，不能写“每 10 分钟”
    import time
    from cloakup.account_execution import AccountExecution
    db=Database(tmp_path);s=AttentionStore(db);a=AccountExecution(db)
    a.set_task('task',['case'],'paused',actor='system',reason='selection_blocked:editor_unavailable');_stuck_page(db,time.time())
    [todo]=[t for t in s.reconcile([],[]) if t['kind']=='browser_health']
    assert '不会自动重试' in todo['detail'] and '每 10 分钟' not in todo['detail'] and '手动恢复' in todo['detail']


def test_page_blocked_pause_outside_the_window_says_when_it_will_retry(tmp_path):
    import time
    from cloakup.account_execution import AccountExecution
    db=Database(tmp_path);s=AttentionStore(db);a=AccountExecution(db);now=time.time()
    a.set_task('task',['case'],'paused',actor='system',reason='selection_blocked:editor_unavailable');_stuck_page(db,now)
    db.put('page_recovery','boss',{'id':'boss','task_ids':['task'],'paused_at':a.members()[0]['history'][-1]['at'],'page_stuck':True,'attempts':0,'next_at':now+60,'outside_window_until':now+3600})
    [todo]=[t for t in s.reconcile(RUNNER,[]) if t['kind']=='browser_health']
    assert '执行时段' in todo['detail'] and '不操作网站' in todo['detail']


def test_page_blocked_pause_needing_the_admin_says_it_will_not_resume(tmp_path):
    import time
    from cloakup.account_execution import AccountExecution
    db=Database(tmp_path);s=AttentionStore(db);a=AccountExecution(db);now=time.time()
    a.set_task('task',['case'],'paused',actor='system',reason='selection_blocked:editor_unavailable');_stuck_page(db,now)
    db.put('page_recovery','boss',{'id':'boss','task_ids':['task'],'paused_at':a.members()[0]['history'][-1]['at'],'page_stuck':True,'attempts':1,'next_at':now+600,'result':'needs_admin','reason':'login_required'})
    [todo]=[t for t in s.reconcile(RUNNER,[]) if t['kind']=='browser_health']
    assert '不会自动恢复' in todo['detail'] and '登录已失效' in todo['detail']


@pytest.mark.parametrize('status,claim',[('running',True),('paused',False)])
def test_stuck_page_todo_only_promises_retries_while_a_task_runs(tmp_path,status,claim):
    # 11-24 20:40 沟通页卡在启动页：管理员暂停的任务不会自动重试，不能写“每 10 分钟会再自动试一次”
    import time
    from cloakup.account_execution import AccountExecution
    db=Database(tmp_path);s=AttentionStore(db);a=AccountExecution(db)
    a.set_task('task',['case'],status,actor='admin',reason='manual');_stuck_page(db,time.time())
    [todo]=[t for t in s.reconcile([],[]) if t['kind']=='browser_health']
    assert ('会自动再试' in todo['detail'])==claim and ('不会自动重试' in todo['detail'])==(not claim)
    assert '从左侧菜单' in todo['detail']


# 复审：待办“已做”只写实际做过的步骤；何时停止重试、为什么停、要不要人处理都照实写；让管理员点击时说明怎样拿到控制权。
CONTROL='view_only=0'


def _paused(tmp_path,doc=None,health=None,runs=RUNNER):
    import time
    from cloakup.account_execution import AccountExecution
    db=Database(tmp_path);s=AttentionStore(db);a=AccountExecution(db);now=time.time()
    a.set_task('task',['case'],'paused',actor='system',reason='selection_blocked:editor_unavailable')
    db.put('page_health','boss',{'id':'boss','at':now,'healthy':False,'reason':'content_missing','exhausted':True,'kind':'chat','steps':['reload','reload','tab_reset'],**(health or {})})
    if doc is not None:db.put('page_recovery','boss',{'id':'boss','task_ids':['task'],'paused_at':a.members()[0]['history'][-1]['at'],'attempts':0,'next_at':now+600,'page_stuck':True,'steps':['reload','reload','tab_reset'],**doc})
    [todo]=[t for t in s.reconcile(runs,[]) if t['kind']=='browser_health' and t['status']!='resolved']
    return todo['detail']


def test_page_todo_lists_only_the_steps_actually_taken(tmp_path):
    detail=_paused(tmp_path,{'attempts':2})
    assert '已做：自动刷新 2 次、重新打开标签页 1 次；暂停后又自动检查 2 次' in detail and '从左侧菜单重新进入' not in detail


@pytest.mark.parametrize('doc,expected',[({'result':'capped','cap':'incident','attempts':6},'已停止自动重试'),
                                         ({'result':'precondition','reason':'当前环境与执行器尚未通过核验'},'页面恢复后也无法自动恢复'),
                                         ({'page_stuck':False},'不会自动重试')])
def test_page_todo_says_when_the_system_has_stopped_and_how_to_take_control(tmp_path,doc,expected):
    detail=_paused(tmp_path,doc)
    assert expected in detail and '手动恢复' in detail
    if doc.get('reason'):assert doc['reason'] in detail   # 前置条件问题不用点浏览器
    else:assert CONTROL in detail and 'View Only' in detail


@pytest.mark.parametrize('attempts,expected',[(2,'系统会在下次检查时再试'),(6,'已停止自动重试')])
def test_resume_failed_todo_says_whether_retries_continue(tmp_path,attempts,expected):
    detail=_paused(tmp_path,{'result':'resume_failed','reason':'发布版本已变化','attempts':attempts})
    assert expected in detail and '发布版本已变化' in detail


def test_stuck_page_and_draft_todos_explain_how_to_take_control(tmp_path):
    import time
    db=Database(tmp_path);s=AttentionStore(db)
    db.put('page_health','boss',{'id':'boss','at':time.time(),'healthy':False,'reason':'loading_stuck','exhausted':True,'kind':'chat','steps':['reload','reload','tab_reset','menu_entry']})
    [todo]=[t for t in s.reconcile([],[]) if t['kind']=='browser_health']
    assert CONTROL in todo['detail'] and '刷新 2 次' in todo['detail'] and '从左侧菜单重新进入 1 次' in todo['detail']
    db.put('page_health','boss',{'id':'boss','at':time.time(),'healthy':False,'reason':'draft_present','needs_admin':True})
    [todo]=[t for t in s.reconcile([],[]) if t['kind']=='browser_health' and t['status']!='resolved']
    assert CONTROL in todo['detail']


def test_question_todo_names_the_question_before_a_resume_card(tmp_path,monkeypatch):
 # 11-26 17:36 qe工程师 伍礼鹏：问“还有其他职位吗”后发了附件简历；语义层针对问题转人工，
 # 待办却取了最后一条（简历卡片“伍礼鹏-个人简历.pdf / 点击预览附件简历”）。
 from cloakup import conversation_semantics as cs
 monkeypatch.setattr(cs,'current',lambda case,person:{'answer':{'action':'defer'}})
 store=AttentionStore(Database(tmp_path))
 inbox=[{'external_id':'q','text':'还有其他职位吗'},{'external_id':'perm','text':'对方想发送附件简历给您，您是否同意\n\n拒绝\n同意','platform_control':True},
        {'external_id':'card','text':'某某-个人简历.pdf\n点击预览附件简历','resume':True}]
 c={'id':'c','job':{'label':'岗位'},'candidates':{'p':{'id':'p','status':'resume_received','inbox':inbox,'outbox':[],'resumes':[{'id':'r'}]}}}
 todos=[t for t in store.reconcile([], [c]) if t['kind']=='question']
 assert [(t['question'],t['reply_id']) for t in todos]==[('还有其他职位吗','q')]


def test_failed_resume_archive_raises_todo_until_archived(tmp_path):
 # 11-26 周全起：简历归档失败只留在候选人记录里，管理员不知道这份简历没推到飞书群。
 store=AttentionStore(Database(tmp_path))
 person={'id':'p','status':'resume_received','inbox':[],'outbox':[],'resumes':['platform-message:m1'],
         'resume_archives':{'platform-message:m1':{'status':'failed','error':'未取得唯一PDF原文件','at':1}}}
 c={'id':'c','job':{'label':'岗位'},'candidates':{'p':person}}
 todos=[t for t in store.reconcile([], [c]) if t['kind']=='resume_archive']
 assert len(todos)==1 and todos[0]['title']=='简历未能归档' and todos[0]['status']=='pending' and '未取得唯一PDF原文件' in todos[0]['detail']
 person['resume_archives']['platform-message:m1']={'status':'interrupted','at':2}
 assert [t for t in store.reconcile([], [c]) if t['kind']=='resume_archive'][0]['status']=='resolved'


def test_draft_todo_names_system_own_unsent_text(tmp_path):
 # 11-27 10:43：暂停原因是系统自己没删掉的文字，待办要说清是谁的会话、可以直接删。
 import time
 db=Database(tmp_path);store=AttentionStore(db)
 run={'id':'r','status':'running','created':1,'selection_recovery':{'c':{'task_id':'task','incident_id':'i','status':'blocked','attempts':3,'max_attempts':3,
      'updated_at':1,'diagnostics':{'stage':'recover_guard','reason':'draft_present','exception_type':None}}}}
 db.put('system_draft','boss',{'id':'boss','chat_id':'733-0','receiver':'赵清山','job_label':'qe工程师','case_id':'c','text':'好的，谢谢您！','at':time.time()-60})
 todo=store.reconcile([run],[])[0]
 for word in ('系统自己没发出的文字','赵清山','qe工程师','「好的，谢谢您！」','确认后删除即可继续','不是这段文字'):assert word in todo['detail']
 db.put('system_draft','boss',{'id':'boss','chat_id':'733-0','receiver':'赵清山','job_label':'qe工程师','case_id':'c','at':time.time()-25*3600})
 todo=store.reconcile([run],[])[0]
 assert '系统自己' not in todo['detail']


def test_withdrawn_unreadable_chat_raises_todo_until_verified(tmp_path):
 # 11-27 旧 QE 岗位：撤下后旧会话认不出，候选人来了新消息也没人回，交给人工。
 store=AttentionStore(Database(tmp_path))
 case={'id':'c'*32,'job':{'label':'QE工程师'},'candidates':{},'deferred_chat_ids':['626363397-0'],'withdrawn_unreadable':{'626363397-0':{'at':1,'receiver':'朱奇'}}}
 todo=next(t for t in store.reconcile([],[case]) if t['kind']=='withdrawn_chat')
 assert todo['title']=='旧岗位会话需人工回复' and todo['status']=='pending' and todo['job_label']=='QE工程师'
 for word in ('朱奇','QE工程师','不会自动回复','在 BOSS 手动回复'):assert word in todo['detail']
 case['withdrawn_unreadable']={}
 assert next(t for t in store.reconcile([],[case]) if t['id']==todo['id'])['status']=='resolved'


def test_withdrawn_todo_only_for_chats_still_parked(tmp_path):
 store=AttentionStore(Database(tmp_path))
 case={'id':'c'*32,'job':{'label':'QE工程师'},'candidates':{},'deferred_chat_ids':['1-0'],
       'withdrawn_unreadable':{'1-0':{'at':1,'receiver':'甲'},'2-0':{'at':1,'receiver':'乙'}}}
 todos=[t for t in store.reconcile([],[case]) if t['kind']=='withdrawn_chat']
 assert len(todos)==1 and '甲' in todos[0]['detail']
