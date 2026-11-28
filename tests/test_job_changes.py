"""增量岗位变更：加入只验收新岗位、撤下/恢复/移除/权重/总额在执行器安全点生效。"""
import asyncio
import copy
import time
from types import SimpleNamespace as NS

import pytest

from cloakup import agent_workspace as aw
from cloakup import faq_answers as fa
from cloakup import job_changes as jc
from cloakup.account_execution import AccountExecution
from cloakup.agent_workspace import AgentWorkspace,assert_release_ready,validated_cases
from cloakup.attention import AttentionStore
from cloakup.autopilot import recovery_pass
from cloakup.conversation_semantics import APPROVED_HOST
from cloakup.job_members import outreach_ids
from cloakup.platform import TaskStore
from cloakup.sourcing import CaseStore
from cloakup.storage import Database
from test_agent_workspace import passing_test


def _job(cases,title,requirements,configured=True):
    case=cases.create('https://www.zhipin.com/web/chat/job-list');cases.bind_job(case,{'title':title,'requirements':requirements})
    case['anonymous_job']={'role':title,'requirements':[requirements]};cases.set_autonomy(case,True,source='test')
    if configured:case.update(job_sourcing={'greeting':'您好，方便聊聊吗？','outreach_terms':[],'recommend_filters':[]},config_revision=1)
    cases.save(case);return cases.load(case['id'])


@pytest.fixture
def setup(tmp_path,monkeypatch):
    db=Database(tmp_path);cases=CaseStore(tmp_path,db)
    a=_job(cases,'交付顾问','支持远程协作。');b=_job(cases,'数据顾问','负责数据整理与分析。')
    store=TaskStore(db);task=store.save({'config':{'name':'寻访任务','website':'https://www.zhipin.com/','scenario':'寻访','goal':'持续取得简历','engine':'boss_sourcing','case_ids':[a['id']]}})
    account=AccountExecution(db);account.set_task(task['id'],[a['id']],'paused')
    db.put('disclosure_consent','recruiting-conversation',{'enabled':True,'mode':'anonymous_conversation','host':APPROVED_HOST})
    busy={'job':None}
    manager=NS(root=tmp_path,db=db,cases=cases,definitions=store,account_execution=account,model_client=lambda:None,
               task_plan_records=lambda:db.list('plan'),busy=lambda:busy['job'],planning=NS(definition=lambda tid:{'planning_context':{'revision':0}}))
    lab=AgentWorkspace(manager);manager.agent_workspace=lab
    draft=lab.draft(task['id'],{});test=passing_test(lab,draft);lab.publish(task['id'],{'draft_id':draft['id'],'expected_revision':1,'test_id':test['id']})
    account.set_task(task['id'],[a['id']],'running')
    outcome={'passed':True}
    async def fake_simulate(bundle,fixtures,client,root,disclosure):
        return {'outcomes':[{'fixture_id':f['id'],'case_id':f['case_id'],'passed':outcome['passed'],'harness_trace':{'runtime':'deepseek-harness'}} for f in fixtures],
                'passed':outcome['passed'],'real_writes':0}
    monkeypatch.setattr(aw,'simulate_bundle',fake_simulate)
    yield NS(m=manager,task=task,a=a,b=b,busy=busy,lab=lab,outcome=outcome)
    for worker in lab.workers.values():worker.cancel()


async def _validated(s,record):
    await s.lab.workers[record['test_id']];await asyncio.sleep(0)
    return s.m.db.get(jc.KIND,record['id'])


def _member(s):return next(x for x in s.m.account_execution.members() if x['id']==s.task['id'])


async def test_add_runs_only_new_job_fixtures(setup):
    s=setup;s.busy['job']={'id':'run'}
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id'],'weight':3})
    assert record['status']=='testing' and record['action']=='add' and record['payload']=={'weight':3}
    test=s.m.db.get('agent_test',record['test_id'])
    assert test['mode']=='validate' and test['case_ids']==[s.b['id']] and test['fixtures']
    assert all(f['id'].startswith(s.b['id']+':') for f in test['fixtures'])
    assert test['fixtures']==[f for f in aw.default_fixtures(test['bundle']) if f['case_id']==s.b['id']]
    assert test['covered']==aw.coverage(test['bundle'])
    assert (await _validated(s,record))['status']=='pending'
    # 排队期间生效版本与任务都不变
    assert s.b['id'] not in s.m.definitions.get(s.task['id'])['config']['case_ids']


async def test_add_applies_at_safe_point_and_rebinds_running_task(setup):
    s=setup;s.busy['job']={'id':'run'};before=s.m.db.get('agent_release',s.task['id'])
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    await _validated(s,record)
    applied=jc.apply_pending(s.m)
    assert [r['id'] for r in applied]==[record['id']] and s.m.db.get(jc.KIND,record['id'])['status']=='applied'
    release=s.m.db.get('agent_release',s.task['id']);version=s.m.db.get('agent_bundle',release['version_id'])
    assert release['revision']==before['revision']+1 and version['origin']=='job_change' and release['validation']=='passed'
    member=_member(s)
    assert member['release_id']==release['version_id'] and member['case_ids']==[s.a['id'],s.b['id']] and member['status']=='running'
    assert member['history'][-1]['reason']=='job_change:add'
    cfg=s.m.definitions.get(s.task['id'])['config']
    assert cfg['case_ids']==[s.a['id'],s.b['id']] and cfg['members'][1]=={'case_id':s.b['id'],'weight':1,'outreach':'active'}
    # 新岗位的快照就是验收时那一份，原样并入；已有岗位沿用生效版本
    test=s.m.db.get('agent_test',record['test_id'])
    assert version['bundle']['sources'][1]==next(i for i in test['bundle']['sources'] if i['case_id']==s.b['id'])
    assert version['bundle']['sources'][0]==s.m.db.get('agent_bundle',before['version_id'])['bundle']['sources'][0]
    assert validated_cases(s.m.db,s.task['id'])=={s.a['id'],s.b['id']}
    assert_release_ready(s.m,s.task['id'])
    assert s.m.account_execution.availability(s.b['id'])['state'] not in ('release_changed','job_not_validated')
    assert s.m.cases.load(s.b['id'])['agent_release_id']==release['version_id']
    # 计划随任务一起更新，重启时不会因计划过期而无法启动
    from cloakup.task_planning import planning_signature
    definition={**copy.deepcopy(s.m.definitions.get(s.task['id'])),'planning_context':{'revision':0}}
    assert any(p['signature']==planning_signature(definition,[s.m.cases.load(c) for c in cfg['case_ids']]) for p in s.m.db.list('plan'))
    assert jc.apply_pending(s.m)==[]


async def test_add_rebases_on_newer_release_or_fails(setup):
    s=setup;s.busy['job']={'id':'run'}
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    await _validated(s,record)
    # 排队期间岗位问答先在安全点生效：加入基于最新版本重新组装，保留这条 FAQ
    rebased=fa.upsert([], '结算周期','按月结算。')
    fa_record={'id':'f'*32,'task_id':s.task['id'],'case_id':s.a['id'],'todo_id':'none','candidate_id':'none','question':'结算周期','answer':'按月结算。','status':'pending','at':time.time()}
    fa.apply(s.m,fa_record)
    assert len(jc.apply_pending(s.m))==1
    current=s.m.db.get('agent_bundle',s.m.db.get('agent_release',s.task['id'])['version_id'])['bundle']
    assert current['sources'][0]['faq']==rebased and [i['case_id'] for i in current['sources']]==[s.a['id'],s.b['id']]
    assert validated_cases(s.m.db,s.task['id'])=={s.a['id'],s.b['id']}


async def test_add_fails_when_task_settings_changed_after_validation(setup):
    s=setup;s.busy['job']={'id':'run'}
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    await _validated(s,record)
    # 验收后整单发布了新的对话提示词：新岗位的验收不再成立，变更失败并生成待办，不覆盖新版本
    s.m.account_execution.set_task(s.task['id'],[s.a['id']],'paused')
    draft=s.lab.draft(s.task['id'],{});bundle=copy.deepcopy(draft['bundle']);bundle['conversation_prompt']+='\n回复保持简洁。'
    draft=s.lab.draft(s.task['id'],{'draft_id':draft['id'],'expected_revision':1,'bundle':bundle})
    test=passing_test(s.lab,draft)
    newer=s.lab.publish(s.task['id'],{'draft_id':draft['id'],'expected_revision':draft['revision'],'test_id':test['id']})
    assert jc.apply_pending(s.m)==[]
    failed=s.m.db.get(jc.KIND,record['id'])
    assert failed['status']=='failed' and failed['error']
    assert s.m.db.get('agent_release',s.task['id'])['version_id']==newer['version_id']
    assert s.b['id'] not in s.m.definitions.get(s.task['id'])['config']['case_ids']
    todos=[t for t in AttentionStore(s.m.db).reconcile([],s.m.cases.list()) if t['kind']=='job_change']
    assert len(todos)==1 and todos[0]['status']=='pending' and todos[0]['case_id']==s.b['id'] and '数据顾问' in todos[0]['detail']
    # 重新发起后待办自动解除
    jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    assert next(t for t in AttentionStore(s.m.db).reconcile([],s.m.cases.list()) if t['kind']=='job_change')['status']=='resolved'


async def test_failed_job_validation_marks_change_failed(setup):
    s=setup;s.busy['job']={'id':'run'};s.outcome['passed']=False
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    assert (await _validated(s,record))['status']=='failed'
    assert jc.apply_pending(s.m)==[] and s.b['id'] not in s.m.definitions.get(s.task['id'])['config']['case_ids']


async def test_idle_add_applies_after_validation_without_executor(setup):
    s=setup
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    assert (await _validated(s,record))['status']=='applied'
    assert s.b['id'] in s.m.definitions.get(s.task['id'])['config']['case_ids']


def test_add_requires_confirmed_job_configuration(setup):
    s=setup;c=_job(s.m.cases,'运营顾问','负责运营。',configured=False)
    with pytest.raises(ValueError,match='请先确认该岗位的问候语、事实和寻访条件'):jc.request(s.m,s.task['id'],{'action':'add','case_id':c['id']})
    assert not s.m.db.list(jc.KIND)


def test_add_without_anonymous_job_facts_says_what_to_fill(setup):
    # 11-25 17:05 FDE交付工程师：绑定后保存了问候语、启用了自动化，但脱敏岗位事实为空；加入时在验收前深处报 jsonschema 错（500“操作失败”）。
    s=setup;c=s.m.cases.load(s.b['id']);c['anonymous_job']=None;s.m.cases.save(c)
    with pytest.raises(ValueError,match='脱敏岗位事实'):jc.request(s.m,s.task['id'],{'action':'add','case_id':c['id']})
    assert not s.m.db.list(jc.KIND)


def test_add_rejects_duplicate_title_across_account(setup):
    s=setup;c=_job(s.m.cases,'交付顾问','另一份要求。')
    with pytest.raises(ValueError,match='同名'):jc.request(s.m,s.task['id'],{'action':'add','case_id':c['id']})
    assert not s.m.db.list(jc.KIND) and not [t for t in s.m.db.list('agent_test') if t.get('case_ids')==[c['id']]]


def test_add_rejects_job_owned_by_other_task(setup):
    s=setup;s.m.db.put('account_task','o'*32,{'id':'o'*32,'environment_id':'boss:default','case_ids':[s.b['id']],'status':'paused','history':[]})
    with pytest.raises(ValueError,match='其他任务'):jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    with pytest.raises(ValueError,match='已在任务中'):jc.request(s.m,s.task['id'],{'action':'add','case_id':s.a['id']})


async def _add_b(s):
    s.busy['job']={'id':'run'}
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    await _validated(s,record);assert len(jc.apply_pending(s.m))==1


async def test_withdraw_keeps_membership_and_stops_outreach(setup):
    s=setup;await _add_b(s);s.busy['job']={'id':'run'}
    record=jc.request(s.m,s.task['id'],{'action':'withdraw','case_id':s.b['id']})
    assert record['status']=='pending' and not record.get('test_id')
    assert len(jc.apply_pending(s.m))==1
    cfg=s.m.definitions.get(s.task['id'])['config']
    assert s.b['id'] in cfg['case_ids'] and s.b['id'] not in outreach_ids(cfg) and s.b['id'] in _member(s)['case_ids']
    assert validated_cases(s.m.db,s.task['id'])=={s.a['id'],s.b['id']} and _member(s)['release_id']==s.m.db.get('agent_release',s.task['id'])['version_id']
    record=jc.request(s.m,s.task['id'],{'action':'resume','case_id':s.b['id']});jc.apply_pending(s.m)
    assert s.b['id'] in outreach_ids(s.m.definitions.get(s.task['id'])['config'])
    with pytest.raises(ValueError,match='参与首聊'):jc.request(s.m,s.task['id'],{'action':'resume','case_id':s.b['id']})


async def test_remove_requires_no_open_conversations(setup):
    s=setup;await _add_b(s);s.busy['job']={'id':'run'}
    case=s.m.cases.load(s.b['id']);p=s.m.cases.add_candidate(case,'p','https://www.zhipin.com/web/chat/index',{'summary':'职业资料'})
    p['conversation']={'chat_id':'1-0','receiver':'甲','job_title':'数据顾问'};s.m.cases.save(case)
    with pytest.raises(ValueError,match='该岗位还有进行中的会话，请先撤下，等会话结束后再移除'):jc.request(s.m,s.task['id'],{'action':'remove','case_id':s.b['id']})
    case=s.m.cases.load(s.b['id']);case['candidates'][p['id']]['status']='closed';s.m.cases.save(case)
    jc.request(s.m,s.task['id'],{'action':'remove','case_id':s.b['id']});assert len(jc.apply_pending(s.m))==1
    cfg=s.m.definitions.get(s.task['id'])['config']
    from cloakup.autonomy import day_key
    assert cfg['case_ids']==[s.a['id']] and _member(s)['case_ids']==[s.a['id']] and cfg['removed_today']==[{'case_id':s.b['id'],'day':day_key()}]
    assert [i['case_id'] for i in s.m.db.get('agent_bundle',s.m.db.get('agent_release',s.task['id'])['version_id'])['bundle']['sources']]==[s.a['id']]
    assert validated_cases(s.m.db,s.task['id'])=={s.a['id']}
    with pytest.raises(ValueError,match='至少保留'):jc.request(s.m,s.task['id'],{'action':'remove','case_id':s.a['id']})


async def test_weights_and_limit_need_no_validation(setup):
    s=setup;await _add_b(s);s.busy['job']={'id':'run'};tests=len(s.m.db.list('agent_test'))
    for body,match in [({'action':'weights','weights':{s.b['id']:11}},'1至10'),({'action':'weights','weights':{'x'*32:2}},'不在任务'),
                       ({'action':'limit','limit':21},'0至20'),({'action':'limit','limit':True},'0至20'),({'action':'shuffle'},'不支持')]:
        with pytest.raises(ValueError,match=match):jc.request(s.m,s.task['id'],body)
    assert jc.request(s.m,s.task['id'],{'action':'weights','weights':{s.b['id']:4}})['status']=='pending'
    assert jc.request(s.m,s.task['id'],{'action':'limit','limit':6})['status']=='pending'
    assert len(jc.apply_pending(s.m))==2 and len(s.m.db.list('agent_test'))==tests
    cfg=s.m.definitions.get(s.task['id'])['config']
    assert cfg['members'][1]['weight']==4 and cfg['execution']['daily_new_limit']==6
    assert s.m.cases.load(s.a['id'])['autonomy']['daily_new_limit']==6
    assert validated_cases(s.m.db,s.task['id'])=={s.a['id'],s.b['id']}
    assert _member(s)['release_id']==s.m.db.get('agent_release',s.task['id'])['version_id']


def test_idle_change_applies_immediately(setup):
    s=setup
    assert jc.request(s.m,s.task['id'],{'action':'limit','limit':5})['status']=='applied'
    assert s.m.definitions.get(s.task['id'])['config']['execution']['daily_new_limit']==5


def test_recovery_pass_does_not_apply_changes(setup):
    s=setup;s.busy['job']={'id':'run'}
    record=jc.request(s.m,s.task['id'],{'action':'limit','limit':5});params={'read_only':False}
    with recovery_pass(params,True):
        assert jc.apply_pending(s.m,params)==[]
    assert s.m.db.get(jc.KIND,record['id'])['status']=='pending'
    assert len(jc.apply_pending(s.m,params))==1


def test_executor_applies_job_changes_only_at_the_loop_safe_point():
    import inspect
    from cloakup import autopilot
    source=inspect.getsource(autopilot.run_account_executor)
    safe=source.index('apply_faq_answers(manager)');change=source.index('apply_job_changes(manager')
    assert safe<change<source.index('for member in manager.account_execution.members()')<source.index('with recovery_pass(')


def test_post_commit_failure_keeps_change_applied(setup,monkeypatch):
    # 评审发现：提交后重写计划出错时，已生效的变更被改记为 failed 并生成“未写入”的错误待办。
    s=setup;s.busy['job']={'id':'run'}
    from cloakup import logbook
    logged=[];monkeypatch.setattr(logbook,'log',lambda category,**fields:logged.append((category,fields)))
    def broken(*args,**kwargs):raise RuntimeError('plan write failed')
    monkeypatch.setattr(jc,'_refresh_plan',broken)
    record=jc.request(s.m,s.task['id'],{'action':'limit','limit':5})
    applied=jc.apply_pending(s.m)
    assert [r['id'] for r in applied]==[record['id']] and s.m.db.get(jc.KIND,record['id'])['status']=='applied'
    assert s.m.definitions.get(s.task['id'])['config']['execution']['daily_new_limit']==5
    assert not [t for t in AttentionStore(s.m.db).reconcile([],s.m.cases.list()) if t['kind']=='job_change']
    assert any(f.get('step')=='post_commit_failed' and f.get('type')=='job_change' and 'plan write failed' in f.get('reason','') for _,f in logged)
    assert any(f.get('step')=='applied' for _,f in logged)


async def test_overview_reports_rows_quota_validation_and_changes(setup):
    # 任务岗位区的只读视图：每个岗位的权重、首聊状态、进行中会话数、验收状态，以及排队/验收中的变更；不写任何记录
    s=setup;s.busy['job']={'id':'run'}
    case=s.m.cases.load(s.a['id'])
    case['candidates']={'p1':{'status':'contacted','conversation':{'receiver':'x'},'outbox':[]},'p2':{'status':'closed','conversation':{'receiver':'y'},'outbox':[]},
                        'p3':{'status':'contacted','conversation':{'receiver':'z'},'alias_of':'p1','outbox':[]},'p4':{'status':'new','outbox':[]}}
    s.m.cases.save(case)
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id'],'weight':2})
    before={kind:[(r['id'],s.m.db.revision(kind,r['id'])) for r in s.m.db.list(kind)] for kind in (jc.KIND,'agent_test','attention')}
    view=jc.overview(s.m.db,s.m.definitions.get(s.task['id']),s.m.cases.list())
    assert {kind:[(r['id'],s.m.db.revision(kind,r['id'])) for r in s.m.db.list(kind)] for kind in before}==before
    assert view['validation']=='passed' and view['shared'] is True
    assert view['quota']['account_cap']==20 and view['quota']['used']==0 and 'shares' not in view['quota']
    assert view['jobs']==[{'case_id':s.a['id'],'label':'交付顾问','weight':1,'outreach':'active','share':view['quota']['target'],'used':0,'open_conversations':1,'validated':True}]
    assert [(c['id'],c['action'],c['case_id'],c['label'],c['status']) for c in view['changes']]==[(record['id'],'add',s.b['id'],'数据顾问','testing')]
    # 加入生效、撤下后：撤下的岗位份额为 0、仍计入已用；新岗位带着自己的权重和验收
    await _validated(s,record);jc.apply_pending(s.m)
    jc.request(s.m,s.task['id'],{'action':'withdraw','case_id':s.a['id']});jc.apply_pending(s.m)
    view=jc.overview(s.m.db,s.m.definitions.get(s.task['id']),s.m.cases.list())
    rows={r['case_id']:r for r in view['jobs']}
    assert rows[s.a['id']]['outreach']=='withdrawn' and rows[s.a['id']]['share']==0 and rows[s.b['id']]['weight']==2 and rows[s.b['id']]['share']==view['quota']['target']
    assert rows[s.b['id']]['validated'] is True and [c['status'] for c in view['changes']]==['applied','applied']


def test_overview_survives_malformed_members(setup):
    s=setup;task=copy.deepcopy(s.m.definitions.get(s.task['id']));task['config']['members']=[{'case_id':s.a['id'],'weight':99}]
    view=jc.overview(s.m.db,task,s.m.cases.list())
    assert view['jobs']==[] and view['error']


async def _two_jobs(s,weight=3):
    """任务未运行（执行器空闲）时加入第二个岗位并立即生效。"""
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id'],'weight':weight})
    assert (await _validated(s,record))['status']=='applied'
    return s.m.definitions.get(s.task['id'])


def _claims(s,cid,n):
    case=s.m.cases.load(cid);now=time.time()
    case['candidates']['p'+str(n)]={'status':'contacted','outbox':[{'intent':'first_contact','status':'sent','claimed_at':now} for _ in range(n)]}
    s.m.cases.save(case)


async def test_overview_shares_and_usage_match_scheduler_inputs(setup):
    # 界面显示的份额与已用，必须就是调度（pick_case/allowed）用的同一组输入：多种用量下逐一核对
    from cloakup import job_quota as jq
    from cloakup.autonomy import day_key
    from cloakup.job_members import weights as member_weights
    s=setup;task=await _two_jobs(s);tid=task['id'];day=day_key()
    for a_used,b_used in ((0,0),(2,0),(2,5),(7,9)):
        if a_used:_claims(s,s.a['id'],a_used)
        if b_used:_claims(s,s.b['id'],b_used)
        cases=s.m.cases.list();task=s.m.definitions.get(tid)
        view=jc.overview(s.m.db,task,cases);rows={r['case_id']:r for r in view['jobs']}
        cfg=copy.deepcopy(task['config']);w=member_weights(cfg);limit=cfg['execution'].get('daily_new_limit',0)
        usage=jq.usage_by_case(s.m.cases,jq.counted_case_ids(cfg,day),day)
        assert view['quota']['target']==jq.task_target(tid,limit,day) and view['quota']['used']==sum(usage.values())
        assert {k:r['share'] for k,r in rows.items()}==jq.allocate(view['quota']['target'],w)
        assert {k:r['used'] for k,r in rows.items()}==usage
        for cid in w:assert jq.allowed(s.m.cases,task,cid,day)==(jq.pick_case(tid,limit,w,{k:r['used'] for k,r in rows.items()},set(w)-{cid},day)==cid)


async def test_overview_marks_add_whose_validation_was_interrupted(setup):
    # 11-24 复审：服务重启后加入请求的验收已中断，但记录仍是 testing（执行器安全点才结算）；视图如实显示“验收已中断”，不永远显示验收中
    s=setup;s.busy['job']={'id':'run'}
    record=jc.request(s.m,s.task['id'],{'action':'add','case_id':s.b['id']})
    test=s.m.db.get('agent_test',record['test_id']);test.update(status='interrupted');s.m.db.put('agent_test',test['id'],test)
    view=jc.overview(s.m.db,s.m.definitions.get(s.task['id']),s.m.cases.list())
    assert [c['status'] for c in view['changes']]==['interrupted'] and s.m.db.get(jc.KIND,record['id'])['status']=='testing'
    test.update(status='running');s.m.db.put('agent_test',test['id'],test)
    assert [c['status'] for c in jc.overview(s.m.db,s.m.definitions.get(s.task['id']),s.m.cases.list())['changes']]==['testing']


def test_overview_skips_quota_for_tasks_the_executor_does_not_run(setup):
    # 额度只对账号执行器里运行或暂停的任务计算；其他任务只给岗位行
    s=setup;s.m.account_execution.set_task(s.task['id'],[s.a['id']],'stopped')
    view=jc.overview(s.m.db,s.m.definitions.get(s.task['id']),s.m.cases.list())
    assert 'quota' not in view and view['jobs'][0]['share'] is None and view['jobs'][0]['used'] is None and view['jobs'][0]['weight']==1


def test_overview_reports_per_job_limits_for_legacy_task_without_execution(setup):
    # 没有任务级执行设置的旧任务：执行器按各岗位自己的上限，视图不给任务总额、目标与份额
    s=setup;task=copy.deepcopy(s.m.definitions.get(s.task['id']));task['config'].pop('execution',None)
    view=jc.overview(s.m.db,task,s.m.cases.list())
    assert view['quota']=={'per_job':True,'account_used':0,'account_cap':20} and view['jobs'][0]['share'] is None and view['jobs'][0]['used']==0


def test_overview_context_is_shared_across_tasks(setup,monkeypatch):
    # /api/state 轮询：账号级读取（变更记录、账号已用、各岗位已用）每次只做一次，不随任务数增加
    from cloakup import autopilot
    calls=[];real=autopilot.daily_attempts
    monkeypatch.setattr(autopilot,'daily_attempts',lambda *a,**k:calls.append(1) or real(*a,**k))
    s=setup;cases=s.m.cases.list();ctx=jc.overview_context(s.m.db,cases)
    task=s.m.definitions.get(s.task['id'])
    for _ in range(3):jc.overview(s.m.db,task,cases,ctx)
    assert len(calls)==1


def test_redraft_and_validate_restores_lapsed_validation_without_publishing(setup,monkeypatch):
    # “重新验收”：执行代码/套件变了，全部岗位验收失效；以当前生效版本新建草案并通过完整验收后，不发布即恢复
    s=setup;tid=s.task['id'];release=s.m.db.get('agent_release',tid)
    monkeypatch.setattr(aw,'SUITE_VERSION',aw.SUITE_VERSION+1)
    assert aw.validation_state(s.m.db,tid)[1:]==(set(),False)
    draft=s.lab.draft(tid,{});passing_test(s.lab,draft)
    assert aw.validation_state(s.m.db,tid)[1:]==({s.a['id']},True) and s.m.db.get('agent_release',tid)==release


MH=[{'text':'有分布式系统研发经历','source':'client_feedback','note':'客户 11-27：要做过分布式'}]


def _sourcing(s,cid):
    return next(i for i in s.m.db.get('agent_bundle',s.m.db.get('agent_release',s.task['id'])['version_id'])['bundle']['sources'] if i['case_id']==cid)['sourcing']


async def test_must_haves_change_tests_only_that_job_then_applies_at_safe_point(setup):
    # 11-27 必须项：只验收该岗位，执行器安全点换绑，运行中的任务不暂停
    s=setup;await _add_b(s);s.busy['job']={'id':'run'};before=s.m.db.get('agent_release',s.task['id'])
    rec=jc.request(s.m,s.task['id'],{'action':'must_haves','case_id':s.a['id'],'must_haves':MH})
    test=s.m.db.get('agent_test',rec['test_id'])
    assert rec['status']=='testing' and rec['action']=='must_haves' and test['case_ids']==[s.a['id']]
    assert next(i for i in test['bundle']['sources'] if i['case_id']==s.a['id'])['sourcing']['must_haves'][0]['text']==MH[0]['text']
    assert jc.describe(s.m,rec)=='更新必须项「交付顾问」'
    assert _sourcing(s,s.a['id'])['must_haves']==[] and s.m.cases.load(s.a['id'])['job_sourcing']['must_haves']==[]
    assert (await _validated(s,rec))['status']=='pending'
    applied=jc.apply_pending(s.m)
    assert [r['id'] for r in applied]==[rec['id']] and s.m.db.get(jc.KIND,rec['id'])['status']=='applied'
    release=s.m.db.get('agent_release',s.task['id'])
    assert release['revision']==before['revision']+1 and applied[0]['version_id']==release['version_id']
    must=s.m.cases.load(s.a['id'])['job_sourcing']['must_haves']
    assert must[0]['text']==MH[0]['text'] and must[0]['note']==MH[0]['note'] and must==_sourcing(s,s.a['id'])['must_haves']
    # 其他岗位与其余寻访配置原样
    assert _sourcing(s,s.b['id'])['must_haves']==[] and {k:v for k,v in _sourcing(s,s.a['id']).items() if k!='must_haves'}=={k:v for k,v in s.m.db.get('agent_bundle',before['version_id'])['bundle']['sources'][0]['sourcing'].items() if k!='must_haves'}
    member=_member(s)
    assert member['release_id']==applied[0]['version_id'] and member['status']=='running' and member['history'][-1]['reason']=='job_change:must_haves'
    assert validated_cases(s.m.db,s.task['id'])=={s.a['id'],s.b['id']}
    assert_release_ready(s.m,s.task['id'])
    assert s.m.cases.load(s.a['id'])['agent_release_id']==release['version_id']
    assert jc.apply_pending(s.m)==[]


async def test_must_haves_change_keeps_candidate_decisions(setup):
    # 必须项改了不清候选人决策；旧初筛结论按必须项修订号自然过期
    s=setup;s.busy['job']={'id':'run'}
    case=s.m.cases.load(s.a['id']);p=s.m.cases.add_candidate(case,'p','https://www.zhipin.com/web/chat/index',{'summary':'职业资料'})
    p.update(decision_wait_until=123.0,harness_decision={'action':'reply'});s.m.cases.save(case)
    revision=s.m.cases.load(s.a['id'])['autonomy']['revision']
    rec=jc.request(s.m,s.task['id'],{'action':'must_haves','case_id':s.a['id'],'must_haves':MH})
    await _validated(s,rec);assert len(jc.apply_pending(s.m))==1
    person=s.m.cases.load(s.a['id'])['candidates'][p['id']]
    assert person['decision_wait_until']==123.0 and person['harness_decision']=={'action':'reply'}
    assert s.m.cases.load(s.a['id'])['autonomy']['revision']==revision


async def test_must_haves_failed_validation_keeps_old_list(setup):
    s=setup;s.busy['job']={'id':'run'};s.outcome['passed']=False;before=s.m.db.get('agent_release',s.task['id'])
    rec=jc.request(s.m,s.task['id'],{'action':'must_haves','case_id':s.a['id'],'must_haves':MH})
    assert (await _validated(s,rec))['status']=='failed'
    assert jc.apply_pending(s.m)==[] and s.m.db.get('agent_release',s.task['id'])==before
    assert s.m.cases.load(s.a['id'])['job_sourcing']['must_haves']==[] and _sourcing(s,s.a['id'])['must_haves']==[]
    todos=[t for t in AttentionStore(s.m.db).reconcile([],s.m.cases.list()) if t['kind']=='job_change']
    assert len(todos)==1 and '更新必须项' in todos[0]['detail']


async def test_must_haves_change_rebases_on_faq_but_fails_when_job_snapshot_changed(setup):
    # 排队期间 FAQ 在安全点生效不影响必须项验收；岗位快照其他内容变了，验收不再对应，变更失败
    s=setup;s.busy['job']={'id':'run'}
    rec=jc.request(s.m,s.task['id'],{'action':'must_haves','case_id':s.a['id'],'must_haves':MH})
    await _validated(s,rec)
    fa.apply(s.m,{'id':'f'*32,'task_id':s.task['id'],'case_id':s.a['id'],'todo_id':'none','candidate_id':'none','question':'结算周期','answer':'按月结算。','status':'pending','at':time.time()})
    assert len(jc.apply_pending(s.m))==1 and _sourcing(s,s.a['id'])['must_haves'][0]['text']==MH[0]['text']
    assert validated_cases(s.m.db,s.task['id'])=={s.a['id']}
    rec=jc.request(s.m,s.task['id'],{'action':'must_haves','case_id':s.a['id'],'must_haves':[]})
    await _validated(s,rec)
    release=s.m.db.get('agent_release',s.task['id']);version=s.m.db.get('agent_bundle',release['version_id'])
    version['bundle']['sources'][0]['sourcing']['greeting']='您好，有个机会想和您聊聊。';s.m.db.put('agent_bundle',version['id'],version)
    assert jc.apply_pending(s.m)==[] and s.m.db.get(jc.KIND,rec['id'])['status']=='failed' and '必须项的验收不再有效' in s.m.db.get(jc.KIND,rec['id'])['error']
    assert s.m.cases.load(s.a['id'])['job_sourcing']['must_haves'][0]['text']==MH[0]['text']


async def test_must_haves_change_rejects_bad_input(setup):
    s=setup;s.busy['job']={'id':'run'}
    for body,match in [({'action':'must_haves','case_id':s.b['id'],'must_haves':MH},'不在任务'),
                       ({'action':'must_haves','case_id':s.a['id'],'must_haves':[{'text':'x'*61,'source':'jd'}]},'1至60'),
                       ({'action':'must_haves','case_id':s.a['id'],'must_haves':'甲'},'列表'),
                       ({'action':'must_haves','case_id':s.a['id']},'列表')]:
        with pytest.raises(ValueError,match=match):jc.request(s.m,s.task['id'],body)
    assert not s.m.db.list(jc.KIND)
    jc.request(s.m,s.task['id'],{'action':'must_haves','case_id':s.a['id'],'must_haves':MH})
    with pytest.raises(ValueError,match='等待生效'):jc.request(s.m,s.task['id'],{'action':'must_haves','case_id':s.a['id'],'must_haves':[]})
