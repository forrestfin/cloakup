"""Persistence, phase gates and isolation. No external sites or candidate sends."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
import pytest
from cloakup.storage import Database
from cloakup.platform import TaskStore, ExplorationExecutor, PublicPageAgentMixin, PLAN_SCHEMA, environment_signature, run_task
from cloakup.workbench import Workbench
from cloakup.agent import AgentResult

@pytest.fixture(autouse=True)
def synthetic_planning(monkeypatch):
    async def plan(manager,key,conversation=False):
        return {'id':'synthetic-plan','plan':{'steps':['Inspect before execution'],'success_criteria':['Verify results'],'risks':[]},'harness_trace':{'runtime':'deepseek-harness'}}
    monkeypatch.setattr(Workbench,'generate_definition_plan',plan)


def config(**kw):
    return {'name':'公开目录采集','website':'https://books.toscrape.com/','scenario':'图书采集','goal':'采集首本书名并返回真实页面证据',**kw}


def test_database_revision_cas_and_restart(tmp_path):
    db=Database(tmp_path); store=TaskStore(db)
    one=store.save({'config':config()})
    two=store.save({'expected_revision':1,'config':config(goal='采集两本')},one['id'])
    with pytest.raises(ValueError,match='版本'):store.save({'expected_revision':1,'config':config()},one['id'])
    assert len(db.history('task',one['id']))==2
    restored=TaskStore(Database(tmp_path)).get(one['id'])
    assert restored['revision']==2 and restored['config']['goal']=='采集两本'
    for i in range(270):db.append('a'*32,{'step':i})
    assert len(Database(tmp_path).trace('a'*32))==270
    assert db.probe()=='sqlite'


@pytest.mark.parametrize('changes',[
    {'website':'https://www.zhipin.com/'},
    {'website':'https://foo.zhipin.com/'},
    {'website':'https://user:password@example.com'},
    {'website':'javascript:alert(1)'},
    {'engine':'boss_sourcing','sourcing_mode':'quota'},
    {'params':[]}, {'max_steps':True}, {'max_steps':201},
    {'plan':{'steps':[]}}, {'output_schema':{'type':'wrong'}},
    {'data_policy':'raw_candidates'},
])
def test_invalid_task_configs(tmp_path,changes):
    with pytest.raises(ValueError):TaskStore(Database(tmp_path)).save({'config':config(**changes)})


def test_migrate_once_database_is_primary(tmp_path):
    folder=tmp_path/'runs/workbench'/('a'*32);folder.mkdir(parents=True)
    p=folder/'state.json';p.write_text(json.dumps({'id':'a'*32,'status':'waiting','events':[]}))
    m=Workbench(tmp_path)
    assert m.jobs['a'*32]['status']=='interrupted'
    m.jobs['a'*32]['status']='succeeded';m.persist(m.jobs['a'*32]);m.close()
    p.write_text(json.dumps({'id':'a'*32,'status':'failed','events':[]}))
    m=Workbench(tmp_path)
    assert m.jobs['a'*32]['status']=='succeeded'
    m.close()


@pytest.mark.asyncio
async def test_exploration_executor_blocks_mutations():
    invoked=[]
    b=SimpleNamespace(last_obs=SimpleNamespace(find=lambda i:SimpleNamespace(label='Button',css='button',raw={'tag':'button','href':None})))
    e=ExplorationExecutor(b,SimpleNamespace())
    for name,args in [('type_text',{}),('press_key',{}),('click_at',{}),('select_option',{}),('click',{'element_id':0})]:
        assert (await e.run(name,args)).is_error
    assert not invoked


def test_redirect_never_enters_model():
    class Parent:
        def _obs_blocks(self,o):return ['would send']
    class Agent(PublicPageAgentMixin,Parent):pass
    a=Agent();a.cfg=SimpleNamespace(allowed_domains=['books.toscrape.com'])
    for url in ['https://zhipin.com/','https://www.zhipin.com/web/chat','https://outside.test/']:
        with pytest.raises(ValueError):a._obs_blocks(SimpleNamespace(url=url,elements=[],raw={}))


def test_failed_readiness_prevents_business_browser(tmp_path,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','test')
    m=Workbench(tmp_path)
    t=m.definitions.save({'config':config()})
    started=[]
    class Browser:
        def __init__(self,*args):pass
        async def start(self):started.append(True)
        async def close(self):pass
    async def failed(manager,report):
        report.update(status='failed',finished_at=time.time());manager.db.put('readiness',report['id'],report)
    monkeypatch.setattr('cloakup.workbench.WebSession',Browser)
    monkeypatch.setattr('cloakup.workbench.preflight',failed)
    async def scenario():
        r=await m.create({'mode':'automation_task','task_id':t['id'],'params':{}})
        await m.tasks[r['id']]
        assert m.jobs[r['id']]['status']=='failed'
        assert m.jobs[r['id']]['phase']=='readiness'
    try:m.call(scenario());assert not started
    finally:m.close()


def test_full_task_phases_snapshot_and_result(tmp_path,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','test')
    m=Workbench(tmp_path);t=m.definitions.save({'config':config()})
    m.db.put('readiness','b'*32,{'id':'b'*32,'status':'passed','started_at':time.time(),'finished_at':time.time(),'signature':environment_signature(),'checks':[]})
    seen=[]
    class Browser:
        def __init__(self,*args):pass
        async def start(self):pass
        async def close(self):pass
    class Agent:
        def __init__(self,manager,job,**kwargs):
            self.guard=SimpleNamespace();self.job=job
        async def run(self,task,url,params,schema):
            seen.append(self.job['phase'])
            if schema==PLAN_SCHEMA:return AgentResult(True,'路径已核验',{'steps':['阅读书名'],'success_criteria':['返回真实标题'],'risks':[]})
            return AgentResult(True,'采集成功',{'title':'真实标题'})
    monkeypatch.setattr('cloakup.workbench.WebSession',Browser)
    monkeypatch.setattr('cloakup.workbench.TracedAgent',Agent)
    async def scenario():
        r=await m.create({'mode':'automation_task','task_id':t['id'],'params':{}})
        await m.tasks[r['id']]
        j=m.jobs[r['id']]
        assert j['status']=='succeeded'
        assert [p['phase'] for p in j['phases']]==['readiness','exploration','implementation','result']
        assert j['task_snapshot']['revision']==1
        assert j['result']['result']['title']=='真实标题'
        assert m.db.get('plan',r['id'])['task_revision']==1
        assert len(m.db.trace(r['id']))>3
    try:m.call(scenario());assert seen==['exploration','implementation']
    finally:m.close()


def test_preflight_real_checks_with_fake_components(tmp_path,monkeypatch):
    from cloakup.platform import preflight
    monkeypatch.setenv('ANTHROPIC_API_KEY','test');monkeypatch.setenv('CLOAKBROWSER_LICENSE_KEY','test')
    m=Workbench(tmp_path)
    class Client:
        async def create(self,**kw):
            assert 'synthetic connectivity test' in kw['messages'][0]['content']
            return SimpleNamespace(content=[SimpleNamespace(type='text',text='CLOAKUP_OK')])
    class Browser:
        def __init__(self,cfg,*a):assert '/profiles/preflight-' in cfg.profile_dir;self.page=self
        async def start(self):pass
        async def goto(self,url):assert url.startswith('data:')
        async def observe(self,*a):return SimpleNamespace(title='Cloakup readiness',elements=[1])
        async def close(self):pass
    monkeypatch.setattr('cloakup.platform.BrowserSession',Browser)
    async def executor_probe(session):return {'contract':'executor-v1','status':'passed','steps':[],'business_writes':0}
    monkeypatch.setattr('cloakup.executor_check.verify',executor_probe)
    monkeypatch.setattr(m,'model_client',lambda:Client())
    class Harness:
        def __init__(self,*a):pass
        async def json(self,**kw):return {'ready':True}
    monkeypatch.setattr('cloakup.harness_runtime.HarnessPlanner',Harness)
    report={'id':'c'*32,'status':'running','started_at':time.time(),'signature':environment_signature(),'checks':[]}
    try:
        m.call(preflight(m,report))
        assert report['status']=='passed' and len(report['checks'])==6
        assert m.latest_readiness()['fresh']
        monkeypatch.setenv('CLOAKUP_MODEL','changed')
        assert not m.latest_readiness()['fresh']
    finally:m.close()


@pytest.mark.parametrize('target,expected_status,expected_calls',[(1,'succeeded',1),(2,'failed',2)])
def test_boss_satisfied_goal_does_not_start_more_outreach(tmp_path,monkeypatch,target,expected_status,expected_calls):
    monkeypatch.setenv('ANTHROPIC_API_KEY','test')
    m=Workbench(tmp_path)
    c=m.cases.create('https://www.zhipin.com/web/chat/job-list')
    m.cases.bind_job(c,{'title':'寻访顾问','requirements':'高端寻访交付'})
    m.cases.set_autonomy(c,True,source='test')
    c['candidates']['person']={'id':'person','resumes':['platform-reference'],'contacts':[],'outbox':[]}
    c['candidates']['alias']={**c['candidates']['person'],'id':'alias','alias_of':'person'}
    m.cases.save(c)
    t=m.definitions.save({'config':config(website='https://www.zhipin.com/',engine='boss_sourcing',sourcing_mode='quota',case_ids=[c['id']],target_resumes=target,max_cycles=1)})
    m.db.put('readiness','b'*32,{'id':'b'*32,'status':'passed','started_at':time.time(),'finished_at':time.time(),'signature':environment_signature(),'checks':[]})
    called=[]
    async def autopilot(manager,job,session):
        called.append(copy.deepcopy(job['params']))
        return {'status':'succeeded','summary':'只读核验完成'}
    class Browser:
        def __init__(self,*a):self.context=None
        async def start(self):pass
        async def close(self):pass
    monkeypatch.setattr('cloakup.autopilot.run_autopilot',autopilot)
    monkeypatch.setattr('cloakup.workbench.WebSession',Browser)
    async def scenario():
        r=await m.create({'mode':'automation_task','task_id':t['id'],'params':{}})
        await m.tasks[r['id']]
        assert m.jobs[r['id']]['status']==expected_status
    try:
        m.call(scenario())
        assert len(called)==expected_calls and called[0]['read_only'] and not called[0]['new_contacts']
    finally:m.close()


def test_preflight_reuses_one_seat_without_touching_business_page(tmp_path,monkeypatch):
    from cloakup.platform import preflight
    monkeypatch.setenv('CLOAKBROWSER_LICENSE_KEY','test')
    monkeypatch.setenv('ANTHROPIC_API_KEY','test')
    m=Workbench(tmp_path);closed=[];started=[]
    original=SimpleNamespace(is_closed=lambda:False)
    temporary=SimpleNamespace()
    async def goto(url):assert url.startswith('data:')
    async def close():closed.append('tab')
    temporary.goto=goto;temporary.close=close
    live=SimpleNamespace(page=original,last_obs='previous observation',_observed_frames={'one':'frame'})
    async def new_page():live.page=temporary;return temporary
    live.context=SimpleNamespace(new_page=new_page)
    m.boss_session=live
    class Browser:
        def __init__(self,*a):self.context=None
        async def start(self):started.append(True)
        async def observe(self,*a):return SimpleNamespace(title='Cloakup readiness',elements=[1])
        async def close(self):closed.append('process')
    class Client:
        async def create(self,**kw):return SimpleNamespace(content=[SimpleNamespace(type='text',text='CLOAKUP_OK')])
    monkeypatch.setattr('cloakup.platform.BrowserSession',Browser)
    async def executor_probe(session):return {'contract':'executor-v1','status':'passed','steps':[],'business_writes':0}
    monkeypatch.setattr('cloakup.executor_check.verify',executor_probe)
    monkeypatch.setattr(m,'model_client',lambda:Client())
    class Harness:
        def __init__(self,*a):pass
        async def json(self,**kw):return {'ready':True}
    monkeypatch.setattr('cloakup.harness_runtime.HarnessPlanner',Harness)
    report={'id':'d'*32,'status':'running','started_at':time.time(),'signature':environment_signature(),'checks':[]}
    try:
        m.call(preflight(m,report))
        assert report['status']=='passed'
        assert live.page is original and live.last_obs=='previous observation'
        assert live._observed_frames=={'one':'frame'}
        assert not started and closed==['tab']
    finally:m.boss_session=None;m.close()


def test_retry_preflight_reuses_only_fresh_matching_success_and_browser_pair():
    from cloakup.platform import reusable_preflight
    report={'signature':'current','status':'failed','finished_at':100,
            'checks':[{'key':'model','status':'failed','checked_at':100},
                      {'key':'database','status':'passed','checked_at':98},
                      {'key':'browser','status':'passed','checked_at':99},
                      {'key':'executor','status':'failed','checked_at':100}]}
    reuse=reusable_preflight(report,'current',105)
    assert list(reuse)==['database'] and reuse['database']['checked_at']==98
    assert reuse['database']['reused']
    assert reusable_preflight(report,'changed',105)=={}
    assert reusable_preflight(report,'current',700)=={}
    report['executor']={'status':'passed','contract':'executor-v1'}
    report['checks'][-1]['status']='passed'
    assert set(reusable_preflight(report,'current',105))=={'database','browser','executor'}


def test_task_save_normalizes_members(tmp_path):
    db=Database(tmp_path);store=TaskStore(db)
    members=[{'case_id':'a','weight':3,'outreach':'active'},{'case_id':'b','weight':1,'outreach':'withdrawn'}]
    t=store.save({'config':config(website='https://www.zhipin.com/',engine='boss_sourcing',members=members)})
    saved=store.get(t['id'])['config']
    assert saved['members']==members and saved['case_ids']==['a','b']
    legacy=store.save({'config':config(name='旧任务',scenario='旧寻访',website='https://www.zhipin.com/',engine='boss_sourcing',case_ids=['c'])})
    assert store.get(legacy['id'])['config']['members']==[{'case_id':'c','weight':1,'outreach':'active'}]
    with pytest.raises(ValueError,match='权重'):store.save({'config':config(name='坏任务',scenario='新寻访',website='https://www.zhipin.com/',engine='boss_sourcing',members=[{'case_id':'d','weight':0,'outreach':'active'}])})


def test_task_form_resave_without_members_keeps_weight_and_outreach(tmp_path):
    # 11-24 评审：任务表单只提交 case_ids，不能把撤下的岗位重置为权重 1、参与首聊
    db=Database(tmp_path);store=TaskStore(db)
    members=[{'case_id':'a','weight':3,'outreach':'active'},{'case_id':'b','weight':2,'outreach':'withdrawn'}]
    t=store.save({'config':config(website='https://www.zhipin.com/',engine='boss_sourcing',members=members)})
    again=store.save({'expected_revision':t['revision'],'config':config(website='https://www.zhipin.com/',engine='boss_sourcing',case_ids=['a','b','c'])},t['id'])
    assert again['config']['members']==[*members,{'case_id':'c','weight':1,'outreach':'active'}]
