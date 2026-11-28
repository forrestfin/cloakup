import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cloakup.agent_workspace import AgentWorkspace,SimulationTools,simulate_bundle,signature,digest,default_fixtures,administrator_text,coverage,validated_cases,assert_release_ready
from cloakup.account_execution import AccountExecution
from cloakup.platform import TaskStore
from cloakup.sourcing import CaseStore,SourcingError
from cloakup.storage import Database
from cloakup.conversation_semantics import APPROVED_HOST,fingerprint


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path);cases=CaseStore(tmp_path,db)
    case=cases.create('https://www.zhipin.com/web/chat/job-list');cases.bind_job(case,{'title':'招聘顾问','requirements':'负责人才寻访与交付。支持远程协作。'})
    case['anonymous_job']={'role':'招聘顾问','requirements':['支持远程协作。','负责人才寻访与交付。']};cases.set_autonomy(case,True,source='test')
    case['faq']=[{'question':'薪酬结构','answer':'固定底薪加绩效工资和提成，整体方案合理、有竞争力。','variants':[],'enabled':True}];cases.save(case)
    store=TaskStore(db);task=store.save({'config':{'name':'寻访任务','website':'https://www.zhipin.com/','scenario':'寻访','goal':'持续取得简历','engine':'boss_sourcing','case_ids':[case['id']]}})
    account=AccountExecution(db);account.set_task(task['id'],[case['id']],'paused')
    db.put('disclosure_consent','recruiting-conversation',{'enabled':True,'mode':'anonymous_conversation','host':APPROVED_HOST})
    manager=NS(root=tmp_path,db=db,cases=cases,definitions=store,account_execution=account,model_client=lambda:None,
               task_plan_records=lambda:db.list('plan'),busy=lambda:None,planning=NS(definition=lambda tid:{'planning_context':{'revision':0}}))
    lab=AgentWorkspace(manager);manager.agent_workspace=lab
    yield lab,manager,task,case
    for worker in lab.workers.values():worker.cancel()


def test_draft_isolated_and_direct_live_config_cannot_bypass_release(setup):
    lab,m,t,c=setup;before=m.definitions.get(t['id']);oldcase=m.db.get('case',c['id'])
    draft=lab.draft(t['id'],{})
    bundle=copy.deepcopy(draft['bundle']);bundle['config']['goal']='新目标仍只保存在草案'
    changed=lab.draft(t['id'],{'draft_id':draft['id'],'expected_revision':1,'bundle':bundle})
    assert changed['revision']==2 and m.definitions.get(t['id'])==before and m.db.get('case',c['id'])==oldcase
    with pytest.raises(ValueError,match='草案已变化'):lab.draft(t['id'],{'draft_id':draft['id'],'expected_revision':1,'bundle':bundle})
    with pytest.raises(ValueError,match='验证后发布'):m.definitions.save({'expected_revision':before['revision'],'config':bundle['config']},t['id'])
    from cloakup.job_config import save_config,view_config
    with pytest.raises(Exception,match='验证后发布'):save_config(m.cases,m.cases.load(c['id']),{'expected_revision':m.cases.load(c['id'])['config_revision'],'config':view_config(m.cases.load(c['id']))})


async def test_simulation_tools_cannot_reach_real_transports():
    tools=SimulationTools();result=await tools.run('ask_resume',{})
    assert result.detail['simulation'] and result.detail['real_write'] is False
    for action in ('send_message','browser.click','lark.send','shell','publish'):
        with pytest.raises(ValueError):await tools.run(action,{})
    assert len(tools.calls)==1


def passing_test(lab,draft,mode='validate'):
    record={'id':'a'*32,'task_id':draft['task_id'],'draft_id':draft['id'],'draft_revision':draft['revision'],'signature':draft['signature'],'mode':mode,'status':'passed',
            'fixtures':default_fixtures(draft['bundle']),
            'result':{'passed':True,'real_writes':0,'outcomes':[{'fixture_id':f['id'],'passed':True,'harness_trace':{'runtime':'deepseek-harness'}} for f in default_fixtures(draft['bundle'])]},'created_at':1,
            'bundle':copy.deepcopy(draft['bundle']),'covered':coverage(draft['bundle']),'case_ids':list(draft['bundle']['config']['case_ids'])}
    lab.db.put('agent_test',record['id'],record);return record


def test_publish_requires_current_validation_paused_task_and_is_atomic(setup):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{})
    bundle=copy.deepcopy(draft['bundle']);bundle['config']['goal']='经过测试的新目标'
    draft=lab.draft(t['id'],{'draft_id':draft['id'],'expected_revision':1,'bundle':bundle})
    test=passing_test(lab,draft,'simulate')
    body={'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']}
    with pytest.raises(ValueError,match='完整验收'):lab.publish(t['id'],body)
    test=passing_test(lab,draft)
    m.account_execution.set_task(t['id'],[c['id']],'running')
    with pytest.raises(ValueError,match='暂停'):lab.publish(t['id'],body)
    m.account_execution.set_task(t['id'],[c['id']],'paused')
    released=lab.publish(t['id'],body)
    assert released['validation']=='passed' and m.definitions.get(t['id'])['config']['goal']=='经过测试的新目标'
    assert m.account_execution.members()[0]['status']=='paused'
    assert m.cases.load(c['id'])['agent_release_id']==released['version_id']
    assert m.db.get('agent_draft',draft['id'])['status']=='published'
    assert m.db.list('plan')[0]['origin']=='validated_release'


def test_edit_invalidates_test_and_concurrent_fact_changes_block_release(setup):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{});test=passing_test(lab,draft)
    bundle=copy.deepcopy(draft['bundle']);bundle['config']['goal']='已更改'
    revised=lab.draft(t['id'],{'draft_id':draft['id'],'expected_revision':1,'bundle':bundle})
    with pytest.raises(ValueError,match='完整验收'):lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':2,'test_id':test['id']})
    test=passing_test(lab,revised)
    case=m.cases.load(c['id']);case['config_revision']+=1;m.cases.save(case)
    with pytest.raises(ValueError,match='岗位事实'):lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':2,'test_id':test['id']})
    assert m.definitions.get(t['id'])['revision']==t['revision']


def test_atomic_batch_rolls_back_first_write_if_second_cas_fails(tmp_path):
    db=Database(tmp_path);db.put('task','one',{'value':1});db.put('task','two',{'value':2})
    with pytest.raises(ValueError):db.put_batch([('task','one',{'value':3},1,True),('task','two',{'value':4},0,True)])
    assert db.get('task','one')=={'value':1} and db.history('task','one')==[]


def test_independent_agent_sessions_and_task_scope(setup):
    lab,m,t,c=setup;d=lab.draft(t['id'],{})
    a=lab.session(t['id'],{'draft_id':d['id'],'agent':'planner'});b=lab.session(t['id'],{'draft_id':d['id'],'agent':'executor'})
    assert a['id']!=b['id'] and a['context']['tools']!=b['context']['tools']
    assert a['context']['mode']==b['context']['mode']=='draft_only'
    with pytest.raises(ValueError):lab.session('f'*32,{'draft_id':d['id'],'agent':'executor'})


async def test_real_decision_code_uses_isolated_db_and_leaves_production_unchanged(setup,monkeypatch,tmp_path):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{})
    from cloakup import conversation_semantics as cs
    async def fake(manager,case,person,plan):
        assert manager.db.backend=='sqlite' and manager.db is not m.db and not hasattr(manager,'cases') and not hasattr(manager,'boss_session')
        answer={'action':'answer','source':'harness_context','semantic_route':'continue','text':'您好，可以的。','reason':'正常交流','fact_indices':[],'resume_readiness':'ready','sources':[],'parts':['welcome'],'missing_information':[]}
        return {'state_key':fingerprint(case,person),'answer':answer,'model_input':{'conversation':[]},'harness_trace':{'runtime':'fake'},'system_prompt':'synthetic'}
    monkeypatch.setattr(cs,'ensure',fake)
    fixture=lab.fixtures(draft,{'mode':'simulate','text':'可以聊聊吗？'})
    before={k:m.db.list(k) for k in ('case','task','attention','task_notification')}
    result=await simulate_bundle(draft['bundle'],fixture,None,tmp_path/'isolated',m.db.get('disclosure_consent','recruiting-conversation'))
    assert result['passed'] and result['real_writes']==0 and result['outcomes'][0]['action']=='answer'
    assert before=={k:m.db.list(k) for k in before}


def test_replay_snapshots_cannot_reach_other_tasks_or_change_history(setup):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{})
    case=m.cases.load(c['id']);p=m.cases.add_candidate(case,'candidate','https://www.zhipin.com/web/chat/index',{'platform_label':'某先生'})
    p['history']=[{'direction':'in','text':'可以聊聊吗？','external_id':'q'}];p['inbox']=[{'text':'可以聊聊吗？','external_id':'q'}];m.cases.save(case)
    before=m.db.get('case',c['id'])
    fixtures=lab.fixtures(draft,{'mode':'replay','case_id':c['id'],'candidate_id':p['id'],'expected_routes':['continue']})
    assert fixtures[0]['replay_scope']=='current_saved_snapshot' and m.db.get('case',c['id'])==before
    with pytest.raises(ValueError):lab.fixtures(draft,{'mode':'replay','case_id':c['id'],'candidate_id':p['id'],'event_id':'not-an-event'})


async def test_two_agent_dialogues_persist_independent_context_and_only_edit_draft(setup,monkeypatch):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{})
    planner=lab.session(t['id'],{'draft_id':draft['id'],'agent':'planner'});executor=lab.session(t['id'],{'draft_id':draft['id'],'agent':'executor'})
    import cloakup.agent_workspace as aw
    inputs=[]
    class Engine:
        client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))
        last_trace={'runtime':'deepseek-harness','version':'test'}
        async def json(self,**kw):
            import json
            data=json.loads(kw['content']);inputs.append(data)
            return {'reply':'已形成草案建议，未发布','config_patch':{'goal':'通过讨论形成的新目标'} if data['agent']=='planner' else {},'source_patches':[],'plan':None,'tool_request':None}
    monkeypatch.setattr(aw,'get_harness',lambda *a:Engine())
    before=m.definitions.get(t['id'])
    await lab.chat(t['id'],planner['id'],{'expected_revision':1,'message':'调整目标，但不要直接上线'})
    await lab.chat(t['id'],planner['id'],{'expected_revision':2,'message':'解释一下刚才的修改'})
    await lab.chat(t['id'],executor['id'],{'expected_revision':1,'message':'先解释当前版本'})
    assert len(inputs[1]['dialogue'])==3 and len(inputs[2]['dialogue'])==1
    assert m.definitions.get(t['id'])==before
    assert lab.db.get('agent_draft',draft['id'])['bundle']['config']['goal']=='通过讨论形成的新目标'
    assert lab.db.get('agent_session',planner['id'])['revision']==3
    assert lab.db.get('agent_session',executor['id'])['revision']==2


def test_publish_cannot_use_incomplete_or_forged_pass_summary(setup):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{});test=passing_test(lab,draft)
    test['result']['outcomes']=[];lab.db.put('agent_test',test['id'],test)
    with pytest.raises(ValueError,match='必需场景'):lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':1,'test_id':test['id']})
    assert m.definitions.get(t['id'])['revision']==t['revision']


def test_admin_control_vocabulary_does_not_weaken_candidate_privacy():
    from cloakup.conversation_semantics import redact
    value=administrator_text('请让执行端 Agent 使用 Harness simulate，预期 continue，检查 nudge_max_attempts。')
    assert all(term in value for term in ('Agent','Harness','simulate','continue','nudge_max_attempts'))
    assert 'Michael' not in redact('联系人 Michael',{'profile':{}})
    assert '13812345678' not in administrator_text('请联系 13812345678')


def test_release_invalidates_only_planning_caches_not_candidate_business_history(setup):
    lab,m,t,c=setup
    case=m.cases.load(c['id']);person=m.cases.add_candidate(case,'business','https://www.zhipin.com/web/chat/index',{'platform_label':'候选人'})
    person.update(status='resume_received',inbox=[{'external_id':'q','text':'已经发送简历'}],history=[{'external_id':'q','direction':'in','text':'已经发送简历'}],
                  outbox=[{'id':'sent','status':'sent','text':'谢谢','receipt':{'delivery_status':'read'},'sent_at':1}, {'id':'draft','status':'draft','text':'旧草稿'}],
                  contacts=['local-contact'],resumes=['local-resume'],platform_actions=[{'id':'ask','kind':'ask_resume','status':'obtained'}],
                  decision_wait_until=9999,harness_decision={'old':True},conversation_semantics={'old':True})
    m.cases.save(case)
    before={k:copy.deepcopy(person[k]) for k in ('status','inbox','history','contacts','resumes','platform_actions')}
    sent=copy.deepcopy(person['outbox'][0]);draft=lab.draft(t['id'],{});test=passing_test(lab,draft)
    lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']})
    after=m.cases.load(c['id'])['candidates'][person['id']]
    assert before=={k:after[k] for k in before} and after['outbox'][0]==sent
    assert after['outbox'][1]['status']=='stale' and 'conversation_semantics' not in after and 'harness_decision' not in after


async def test_published_conversation_can_continue_and_edits_fork_new_draft(setup,monkeypatch):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{});test=passing_test(lab,draft)
    release=lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':1,'test_id':test['id']})
    session=lab.session(t['id'],{'agent':'executor','draft_id':draft['id']})
    import cloakup.agent_workspace as aw
    class Engine:
        client=NS(raw=NS(base_url='https://ark.cn-beijing.volces.com/api/'))
        last_trace={'runtime':'deepseek-harness'}
        async def json(self,**kw):return {'reply':'另建草案讨论，不覆盖已发布版本','config_patch':{'goal':'新一轮讨论目标'},'source_patches':[],'plan':None,'tool_request':None}
    monkeypatch.setattr(aw,'get_harness',lambda *a:Engine())
    result=await lab.chat(t['id'],session['id'],{'expected_revision':1,'message':'提出下一版目标'})
    assert result['draft_id']!=draft['id']
    assert lab.db.get('agent_draft',draft['id'])['status']=='published'
    assert m.definitions.get(t['id'])['config']['goal']!='新一轮讨论目标'
    assert lab.db.get('agent_release',t['id'])==release


def test_recorded_observation_replay_uses_then_state_not_current_state(setup):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{})
    case=m.cases.load(c['id']);person=m.cases.add_candidate(case,'person','https://www.zhipin.com/web/chat/index',{'platform_label':'候选人'})
    event=m.cases.audit(case,'harness_conversation_understood',candidate_id=person['id'],
        model_input={'conversation':[{'role':'candidate','text':'可以聊聊吗？'}],'state':{'has_resume':False,'pending_applications':[]}},
        answer={'semantic_route':'continue','action':'answer'})
    person['resumes']=['later-resume'];person['history']=[{'direction':'in','text':'后来的问题'}];m.cases.save(case)
    fixture=lab.fixtures(draft,{'mode':'replay','case_id':c['id'],'candidate_id':person['id'],'event_id':event['id']})[0]
    assert not fixture['has_resume'] and fixture['messages'][0]['text']=='可以聊聊吗？'
    assert fixture['replay_scope']=='recorded_observation'


def test_restart_preserves_sessions_versions_and_interrupts_incomplete_tests(setup):
    lab,m,t,c=setup;draft=lab.draft(t['id'],{});session=lab.session(t['id'],{'agent':'planner','draft_id':draft['id']})
    lab.db.put('agent_test','pending',{'id':'pending','status':'running'})
    restored=AgentWorkspace(m)
    assert restored.db.get('agent_session',session['id'])==session
    assert restored.db.get('agent_draft',draft['id'])==draft
    assert restored.db.get('agent_test','pending')['status']=='interrupted'


def test_start_requires_release_validated_against_current_runtime(setup):
    from cloakup.agent_workspace import assert_release_ready
    lab,m,t,c=setup;draft=lab.draft(t['id'],{})
    with pytest.raises(ValueError,match='完整隔离验收'):assert_release_ready(m,t['id'])
    test=passing_test(lab,draft);lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':1,'test_id':test['id']})
    assert_release_ready(m,t['id'])
    report=lab.db.get('agent_test',test['id']);report['signature']='old-code';lab.db.put('agent_test',test['id'],report)
    with pytest.raises(ValueError,match='执行代码'):assert_release_ready(m,t['id'])


@pytest.mark.parametrize('filename',['browser.py','boss_chat.py','autopilot.py','selection_recovery.py','browser_diagnostics.py'])
def test_browser_adapter_changes_invalidate_release_signature(setup,monkeypatch,filename):
    lab,m,task,case=setup
    bundle=lab.draft(task['id'],{})['bundle'];before=signature(bundle)
    original=Path.read_bytes
    def changed(path):
        data=original(path)
        return data+b'\n# changed execution implementation' if path.name==filename else data
    from cloakup.agent_workspace import runtime_files
    monkeypatch.setattr(Path,'read_bytes',changed);runtime_files.cache_clear()  # 哈希按进程缓存：模拟“换代码后重启”
    try:assert signature(bundle)!=before
    finally:monkeypatch.undo();runtime_files.cache_clear()


def test_precontact_screening_files_are_in_release_signature():
    # 11-27 首聊前初筛：初筛与每日额度决定首聊行为，改了它们已发布版本须重新验收。
    from cloakup.agent_workspace import runtime_files
    assert {'screening.py','job_quota.py'}<=set(runtime_files())


async def test_model_validation_cannot_replace_executor_readiness(setup):
    from cloakup.workbench import Workbench
    lab,m,task,case=setup
    draft=lab.draft(task['id'],{});test=passing_test(lab,draft)
    lab.publish(task['id'],{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']})
    m.preflight_task=None;m.latest_readiness=lambda:{'executor_current':False}
    with pytest.raises(ValueError,match='模型验收不能代替浏览器执行核验'):
        await Workbench.start_account_task(m,task['id'])


async def test_resume_blocker_reports_what_a_start_would_refuse(setup):
    # 复审：系统自动恢复前先查刷新页面修不好的前置条件（发布验收、环境核验），会被拒绝就不碰页面
    from cloakup.workbench import Workbench
    lab,m,task,case=setup
    draft=lab.draft(task['id'],{});test=passing_test(lab,draft)
    lab.publish(task['id'],{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']})
    m.latest_readiness=lambda:{'executor_current':False}
    assert '模型验收不能代替浏览器执行核验' in Workbench.resume_blocker(m,task['id'])
    m.latest_readiness=lambda:{'executor_current':True}
    assert Workbench.resume_blocker(m,task['id']) is None


def two_jobs(lab,m,t,c):
    other=m.cases.create('https://www.zhipin.com/web/chat/job-list');m.cases.bind_job(other,{'title':'交付顾问','requirements':'负责候选人交付。支持远程协作。'})
    other['anonymous_job']={'role':'交付顾问','requirements':['负责候选人交付。','支持远程协作。']};m.cases.set_autonomy(other,True,source='test');m.cases.save(other)
    task=m.definitions.get(t['id']);m.definitions.save({'expected_revision':task['revision'],'config':{**task['config'],'case_ids':[c['id'],other['id']]}},t['id'])
    m.account_execution.set_task(t['id'],[c['id'],other['id']],'paused')
    return other


def publish_validated(lab,t,*,legacy=False):
    draft=lab.draft(t['id'],{});test=passing_test(lab,draft)
    if legacy:
        for key in ('covered','case_ids'):test.pop(key)
        lab.db.put('agent_test',test['id'],test)
    return lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']}),test


def edit_release(lab,t,change):
    release=lab.db.get('agent_release',t['id']);version=lab.db.get('agent_bundle',release['version_id'])
    change(version['bundle']);lab.db.put('agent_bundle',version['id'],version)


def test_legacy_full_validation_still_ready(setup):
    # 升级前的验收记录没有 covered：代码、模型与策略未变时仍有效，旧任务照常启动。
    lab,m,t,c=setup;publish_validated(lab,t,legacy=True)
    assert assert_release_ready(m,t['id']) is None and validated_cases(lab.db,t['id'])=={c['id']}
    report=lab.db.get('agent_test','a'*32);report['signature']='old-code';lab.db.put('agent_test',report['id'],report)
    with pytest.raises(ValueError,match='执行代码') as raised:assert_release_ready(m,t['id'])
    assert raised.value.code=='release_changed' and validated_cases(lab.db,t['id'])==set()


def test_faq_change_keeps_validation(setup):
    lab,m,t,c=setup;publish_validated(lab,t)
    edit_release(lab,t,lambda b:b['sources'][0]['faq'].append({'question':'结算周期','answer':'按月结算。','variants':[],'enabled':True}))
    assert_release_ready(m,t['id']);assert validated_cases(lab.db,t['id'])=={c['id']}


def test_limit_or_weight_change_keeps_validation(setup):
    lab,m,t,c=setup;other=two_jobs(lab,m,t,c);publish_validated(lab,t)
    def change(b):
        b['config']['execution']['daily_new_limit']=5;b['config']['members'][1].update(weight=3,outreach='withdrawn')
    edit_release(lab,t,change)
    assert_release_ready(m,t['id']);assert validated_cases(lab.db,t['id'])=={c['id'],other['id']}
    edit_release(lab,t,lambda b:b['config'].update(goal='未经验收的目标'))
    with pytest.raises(ValueError,match='执行代码') as raised:assert_release_ready(m,t['id'])
    assert raised.value.code=='release_changed' and validated_cases(lab.db,t['id'])==set()


def test_source_change_invalidates_only_that_job(setup):
    lab,m,t,c=setup;other=two_jobs(lab,m,t,c);publish_validated(lab,t)
    edit_release(lab,t,lambda b:b['sources'][1]['facts'].append({'kind':'duties','value':'负责候选人交付。','status':'confirmed'}))
    assert validated_cases(lab.db,t['id'])=={c['id']}
    with pytest.raises(ValueError,match='岗位「交付顾问」的验收已失效') as raised:assert_release_ready(m,t['id'])
    assert raised.value.code=='job_not_validated'


def test_runtime_change_invalidates_all(setup,monkeypatch):
    from cloakup import agent_workspace
    lab,m,t,c=setup;other=two_jobs(lab,m,t,c);publish_validated(lab,t)
    monkeypatch.setattr(agent_workspace,'runtime_signature',lambda:'new-runtime')
    assert validated_cases(lab.db,t['id'])==set()
    with pytest.raises(ValueError,match='执行代码') as raised:assert_release_ready(m,t['id'])
    assert raised.value.code=='release_changed' and other


def test_validation_must_be_a_passed_isolated_full_run(setup):
    lab,m,t,c=setup;publish_validated(lab,t)
    for change in ({'mode':'simulate'},{'status':'failed'},{'result':{'passed':True,'real_writes':1,'outcomes':[]}},{'case_ids':[]},{'task_id':'other'}):
        report=lab.db.get('agent_test','a'*32);saved=copy.deepcopy(report);report.update(change);lab.db.put('agent_test',report['id'],report)
        assert validated_cases(lab.db,t['id'])==set(),change
        lab.db.put('agent_test',saved['id'],saved)
    assert validated_cases(lab.db,t['id'])=={c['id']}


async def test_validation_record_carries_its_coverage(setup):
    lab,m,t,c=setup;other=two_jobs(lab,m,t,c);draft=lab.draft(t['id'],{})
    record=lab.start_test(t['id'],{'draft_id':draft['id'],'mode':'validate'})
    assert record['covered']==coverage(draft['bundle']) and record['case_ids']==[c['id'],other['id']]
    assert set(record['covered'])=={'runtime','task','sources'} and set(record['covered']['sources'])=={c['id'],other['id']}


@pytest.mark.parametrize('name,change',[
    ('missing_harness_trace',lambda r:r['result']['outcomes'][0].pop('harness_trace')),
    ('other_runtime',lambda r:r['result']['outcomes'][0].update(harness_trace={'runtime':'other'})),
    ('outcome_failed',lambda r:r['result']['outcomes'][0].update(passed=False)),
    ('result_not_passed',lambda r:r['result'].update(passed=False)),
    ('missing_fixture',lambda r:(r['fixtures'].pop(),r['result']['outcomes'].pop())),
    ('missing_outcome',lambda r:r['result']['outcomes'].pop())])
def test_reuse_applies_the_same_completeness_gate_as_publish(setup,name,change):
    # 复用验收不能比发布宽松：缺场景、缺 Harness 运行记录、单条失败都不算已验收，也不能发布。
    lab,m,t,c=setup;publish_validated(lab,t)
    report=lab.db.get('agent_test','a'*32);change(report);lab.db.put('agent_test',report['id'],report)
    assert validated_cases(lab.db,t['id'])==set()
    with pytest.raises(ValueError,match='执行代码'):assert_release_ready(m,t['id'])
    draft=lab.draft(t['id'],{});test=passing_test(lab,draft);change(test);lab.db.put('agent_test',test['id'],test)
    with pytest.raises(ValueError,match='没有通过隔离验收|必需场景'):lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']})


def test_single_job_validation_counts_only_its_own_scenarios(setup):
    lab,m,t,c=setup;other=two_jobs(lab,m,t,c);publish_validated(lab,t)
    report=lab.db.get('agent_test','a'*32);bundle=report['bundle']
    fixtures=[f for f in default_fixtures(bundle) if f['case_id']==other['id']]
    report.update(case_ids=[other['id']],fixtures=fixtures,result={**report['result'],'outcomes':[o for o in report['result']['outcomes'] if o['fixture_id'].startswith(other['id']+':')]})
    lab.db.put('agent_test',report['id'],report)
    assert validated_cases(lab.db,t['id'])=={other['id']}
    with pytest.raises(ValueError,match='招聘顾问') as raised:assert_release_ready(m,t['id'])
    assert raised.value.code=='job_not_validated'
    # 单岗位验收不能拿来整单发布。
    draft=lab.draft(t['id'],{});test=passing_test(lab,draft)
    test.update(case_ids=[other['id']],fixtures=[f for f in default_fixtures(draft['bundle']) if f['case_id']==other['id']]);lab.db.put('agent_test',test['id'],test)
    with pytest.raises(ValueError,match='必需场景'):lab.publish(t['id'],{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']})


def test_cached_validation_sees_new_and_changed_tests(setup,monkeypatch):
    lab,m,t,c=setup;publish_validated(lab,t)
    assert validated_cases(lab.db,t['id'])=={c['id']}
    with monkeypatch.context() as patch:  # 无变化时命中缓存，不再读取验收记录内容
        patch.setattr(lab.db,'list',lambda kind:pytest.fail('cache miss'))
        assert validated_cases(lab.db,t['id'])=={c['id']}
    report=lab.db.get('agent_test','a'*32);report['status']='failed';lab.db.put('agent_test',report['id'],report)
    assert validated_cases(lab.db,t['id'])==set()
    fresh={**report,'id':'b'*32,'status':'passed'};lab.db.put('agent_test',fresh['id'],fresh)
    assert validated_cases(lab.db,t['id'])=={c['id']}
    edit_release(lab,t,lambda b:b['sources'][0]['facts'].append({'kind':'duties','value':'新职责','status':'confirmed'}))
    assert validated_cases(lab.db,t['id'])==set()


@pytest.mark.parametrize('value,question',[('工作地点：武汉东西湖区','那您前面说的工作地点是在哪里？'),
                                           ('远程协作，地区不限，华中区域优先。','那您前面说的工作安排是怎样的？')])
def test_known_fact_question_matches_a_place_fact(monkeypatch,value,question):
    # 11-25 17:25 FDE交付工程师加入验收 8/9：唯一的工作安排类事实是“工作地点：…”，固定问“工作安排”，模型合理地澄清而判不通过。
    import cloakup.agent_workspace as aw
    monkeypatch.setattr(aw,'case_for',lambda bundle,cid:{'confirmed_facts':[{'kind':'work_arrangement','value':value,'status':'confirmed'}]})
    known=[f for f in default_fixtures({'sources':[{'case_id':'c1'}]}) if f['id']=='c1:known_fact']
    assert [m['text'] for m in known[0]['messages']]==[value,question] and known[0]['expected_routes']==['answer']
