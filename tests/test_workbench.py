"""Workbench orchestration tests: no model calls or real browser required."""
import asyncio
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cloakup import workbench as web
from cloakup.agent import AgentResult
from cloakup.recipe import Recipe


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    m = web.Workbench(tmp_path)
    yield m
    m.close()


def sample_recipe():
    return Recipe.from_dict({
        'id': 'books_test', 'name': 'Books', 'goal': 'Read {{category}}',
        'inputs': {'type': 'object', 'properties': {'category': {'type': 'string'}}, 'required': ['category']},
        'outputs': web.BOOK_SCHEMA,
        'steps': [{'id': 'read', 'action': 'extract', 'intent': 'Read books', 'schema': web.BOOK_SCHEMA, 'merge': True}],
    })


def test_explore_compile_preview_save_restart(manager, monkeypatch):
    closed = []
    class Session:
        def __init__(self, m, job): self.job = job
        async def start(self): pass
        async def close(self): closed.append(self.job['id'])
    class Agent:
        def __init__(self, m, job, **kw): self.m, self.job = m, job
        async def run(self, task, url, params, schema):
            (self.m.directory(self.job) / 'trace').mkdir()
            return AgentResult(True, 'Collected', {'books': [{'title': 'Book', 'price': '£1', 'in_stock': 2}]})
    async def compile_trace(*a, **kw):
        r = sample_recipe()
        r.id = kw['recipe_id']
        return r
    monkeypatch.setattr(web, 'WebSession', Session)
    monkeypatch.setattr(web, 'TracedAgent', Agent)
    monkeypatch.setattr(web, 'compile_trace', compile_trace)
    async def scenario():
        created = await manager.create({'params': {'category': 'Poetry', 'max_items': 1}})
        jid = created['id']
        await manager.tasks[jid]
        job = manager.jobs[jid]
        assert job['status'] == 'succeeded' and closed == [jid]
        await manager.action(jid, {'action': 'compile'})
        await manager.tasks[jid]
        assert job['draft'] and not list((manager.root / 'recipes').glob('*'))
        await manager.action(jid, {'action': 'save', 'name': '我的图书'})
        await manager.action(jid, {'action': 'save', 'name': '重复保存'})
        r = manager.recipe(job['saved_recipe'])
        assert r.name == '我的图书'
        assert r.validate_params({'category': 'Travel'}) == {'category': 'Travel'}
        snapshot = await manager.snapshot()
        snapshot['runs'][0]['status'] = 'mutated'
        assert job['status'] == 'succeeded'
        return jid
    jid = manager.call(scenario())
    restored = web.Workbench(manager.root)
    try:
        assert restored.jobs[jid]['result']['result']['books'][0]['title'] == 'Book'
        assert len(restored.recipes()) == 1
    finally: restored.close()


def test_pause_resume_and_stale_human_answer(manager):
    async def scenario():
        jid = 'a' * 32
        job = {'id': jid, 'status': 'running', 'pause_requested': False, 'events': []}
        manager.jobs[jid] = job
        manager.gates[jid] = asyncio.Event()
        manager.gates[jid].set()
        await manager.action(jid, {'action': 'pause'})
        checkpoint = asyncio.create_task(manager.checkpoint(job))
        await asyncio.sleep(0)
        assert job['status'] == 'paused' and not checkpoint.done()
        await manager.action(jid, {'action': 'resume'})
        await checkpoint
        answer = asyncio.create_task(manager.question(job, 'confirm', 'Continue?'))
        await asyncio.sleep(0)
        qid = job['interaction']['id']
        with pytest.raises(ValueError):
            await manager.action(jid, {'action': 'answer', 'interaction_id': 'old', 'value': True})
        with pytest.raises(ValueError):
            await manager.action(jid, {'action': 'answer', 'interaction_id': qid, 'value': 'yes'})
        await manager.action(jid, {'action': 'answer', 'interaction_id': qid, 'value': False})
        assert await answer is False
        assert job['interaction'] is None and job['status'] == 'running'
        job['status'] = 'cancelled'
    manager.call(scenario())


def test_cancellation_releases_browser_and_worker(manager, monkeypatch):
    closed = []
    class Session:
        def __init__(self, *a): pass
        async def start(self): await asyncio.Event().wait()
        async def close(self): closed.append(True)
    monkeypatch.setattr(web, 'WebSession', Session)
    async def scenario():
        jid = (await manager.create({'params': {'category': 'Travel', 'max_items': 1}}))['id']
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match='已有任务'):
            await manager.create({'params': {'category': 'Travel', 'max_items': 1}})
        task = manager.tasks[jid]
        await manager.action(jid, {'action': 'cancel'})
        await task
        assert closed and manager.jobs[jid]['status'] == 'cancelled'
        assert manager.busy() is None
    manager.call(scenario())


def test_invalid_inputs_and_path_rejected(manager):
    async def scenario():
        for params in ({'category': 'Poetry', 'max_items': True}, {'category': 'Poetry', 'max_items': 0}, {'category': 'Other', 'max_items': 1}):
            with pytest.raises(ValueError): await manager.create({'params': params})
    manager.call(scenario())
    for filename in ('../example.yaml', '/tmp/x.yaml', None):
        with pytest.raises(ValueError): manager.recipe(filename)


def test_restart_marks_active_runs_interrupted(tmp_path):
    p = tmp_path / 'runs/workbench' / ('b' * 32) / 'state.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'id': 'b'*32, 'status': 'waiting', 'interaction': {'prompt': 'test'}}))
    m = web.Workbench(tmp_path)
    try:
        assert m.jobs['b'*32]['status'] == 'interrupted'
        assert m.jobs['b'*32]['interaction'] is None
    finally: m.close()


def test_http_localhost_boundary_and_csv(manager):
    server = web.make_server(manager, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, data=None, headers=None):
        req = urllib.request.Request(base+path, data=json.dumps(data).encode() if data is not None else None, headers=headers or {})
        return urllib.request.urlopen(req)
    try:
        assert request('/').status == 200
        assert request('/app.js').headers['Content-Type'].startswith('text/javascript')
        snapshot = json.load(request('/api/state'))
        for headers in ({'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'}):
            with pytest.raises(urllib.error.HTTPError) as exc: request('/api/state', headers=headers)
            assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as exc: request('/api/runs', data={})
        assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            request('/api/runs', data={'params': {}}, headers={'X-Cloakup-Token': snapshot['token']})
        assert exc.value.code == 400
        async def add_result():
            manager.jobs['c'*32] = {'id': 'c'*32, 'created': 0, 'status': 'succeeded', 'result': {'result': {'books': [{'title': '=1+1', 'price': '£1', 'in_stock': 2}]}}}
        manager.call(add_result())
        csv = request('/api/runs/'+('c'*32)+'/csv').read().decode('utf-8-sig')
        assert "'=1+1" in csv and '£1' in csv
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_boss_login_waits_without_model_or_login_capture(manager, monkeypatch):
    from types import SimpleNamespace
    calls = []
    # Separate persistent identity; doesn't inherit the book demo's browser data.
    session = web.WebSession(manager, {'mode': 'login'})
    assert session.cfg.profile_dir.endswith('profiles/boss')
    class Session:
        def __init__(self, *a): pass
        async def start(self): calls.append('start')
        async def navigate(self, url): calls.append(url)
        async def observe(self, screenshot):
            assert screenshot is False
            calls.append('observe-after-reply')
            return SimpleNamespace(url='https://www.zhipin.com/web/boss/job')
        async def close(self): calls.append('close')
    monkeypatch.setattr(web, 'WebSession', Session)
    async def scenario():
        jid = (await manager.create({'mode': 'login'}))['id']
        task = manager.tasks[jid]
        await asyncio.sleep(0)
        job = manager.jobs[jid]
        assert job['status'] == 'waiting'
        assert 'observe-after-reply' not in calls
        assert manager.client is None
        await manager.action(jid, {'action': 'answer', 'interaction_id': job['interaction']['id'], 'value': '已登录'})
        await task
        assert job['status'] == 'succeeded'
        assert calls[-2:] == ['observe-after-reply', 'close']
        assert manager.client is None
    manager.call(scenario())


def test_boss_binding_persists_job_and_plan_without_platform_write(manager, monkeypatch):
    class Session:
        def __init__(self, *a): pass
        async def start(self): pass
        async def close(self): pass
    async def read_job(session, url):
        return {'title':'高端猎聘交付达人', 'requirements':'至少5年经验', 'source_url':url}
    async def plan(llm, snapshot):
        return {'greeting':'你好，请问方便交流吗？', 'keywords':['猎头顾问'], 'requirements':[], 'follow_up_questions':['请介绍交付经验']}
    monkeypatch.setattr(web,'WebSession',Session)
    monkeypatch.setattr(web,'read_job',read_job)
    monkeypatch.setattr(web,'prepare_plan',plan)
    async def scenario():
        jid=(await manager.create({'mode':'boss_bind','params':{'job_url':'https://www.zhipin.com/web/chat/job/edit?encryptId=test'}}))['id']
        await manager.tasks[jid]
        job=manager.jobs[jid]
        assert job['status']=='succeeded'
        case=manager.cases.load(job['case_id'])
        assert case['greeting_draft'] == '你好，请问方便交流吗？'
        assert case['stage']=='plan_review'
        assert case['candidates']=={}
        assert (await manager.snapshot())['cases'][0]['id']==case['id']
    manager.call(scenario())


def test_recommend_read_needs_no_model_and_preserves_new_replies(manager, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN', raising=False)
    case=manager.cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=test')
    manager.cases.bind_job(case,{'title':'交付顾问','requirements':'五年招聘经验'})
    manager.cases.add_candidate(case,'existing','https://www.zhipin.com/web/chat/recommend',{})
    closed=[]
    class Session:
        def __init__(self,*args):pass
        async def start(self):pass
        async def close(self):closed.append(True)
    async def read(session, snapshot):
        latest=manager.cases.load(snapshot['id'])
        manager.cases.receive(latest,'existing','reply-1','方便交流')
        return [{'geek_id':'g-1','summary':'招聘经历'}]
    monkeypatch.setattr(web,'WebSession',Session)
    monkeypatch.setattr(web,'read_recommendations',read)
    async def scenario():
        jid=(await manager.create({'mode':'boss_recommend','params':{'case_id':case['id']}}))['id']
        await manager.tasks[jid]
        assert manager.jobs[jid]['status']=='succeeded' and closed==[True]
        latest=manager.cases.load(case['id'])
        assert latest['recommend_pool']['cards'][0]['geek_id']=='g-1'
        assert latest['candidates']['existing']['inbox'][0]['text']=='方便交流'
        assert manager.client is None
    manager.call(scenario())


@pytest.mark.parametrize('read_failure',[False,True])
def test_live_boss_browser_survives_login_and_next_task_until_shutdown(manager,monkeypatch,read_failure):
    from types import SimpleNamespace
    instances=[];calls=[]
    class Session:
        def __init__(self,m,job):self.job=job;self.context=None;self.page=None;instances.append(self)
        async def start(self):
            calls.append('start');self.context=object();self.page=SimpleNamespace(is_closed=lambda:False)
        async def navigate(self,url):pass
        async def observe(self,screenshot):return SimpleNamespace(url='https://www.zhipin.com/web/chat/index')
        async def close(self):calls.append('close');self.context=None
    async def read(session,case):
        assert session is instances[0] and session.context is not None
        if read_failure:raise ValueError('只读核对失败')
        return []
    monkeypatch.setattr(web,'WebSession',Session);monkeypatch.setattr(web,'read_recommendations',read)
    case=manager.cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=test')
    manager.cases.bind_job(case,{'title':'交付顾问','requirements':'五年招聘经验'})
    async def scenario():
        jid=(await manager.create({'mode':'login'}))['id'];task=manager.tasks[jid]
        await asyncio.sleep(0)
        await manager.action(jid,{'action':'answer','interaction_id':manager.jobs[jid]['interaction']['id'],'value':'已登录'})
        await task
        assert manager.jobs[jid]['browser_kept_open'] and calls==['start']
        next_id=(await manager.create({'mode':'boss_recommend','params':{'case_id':case['id']}}))['id']
        await manager.tasks[next_id]
        assert manager.jobs[next_id]['status']==('failed' if read_failure else 'succeeded')
        assert manager.jobs[next_id]['browser_kept_open'] and calls==['start'] and len(instances)==1
        assert instances[0].job['id']==next_id and manager.client is None
        await manager.shutdown()
        assert calls==['start','close'] and manager.boss_session is None
    manager.call(scenario())


def test_autopilot_runs_without_per_message_questions_and_read_only_cannot_send(manager,monkeypatch):
    calls=[]
    class Session:
        def __init__(self,*a):pass
        async def start(self):pass
        async def close(self):calls.append('close')
    async def run(manager,job,session):
        calls.append(dict(job['params']))
        return {'status':'succeeded','summary':'已同步'}
    async def forbidden(*a):raise AssertionError('no per-message human confirmation')
    monkeypatch.setattr(web,'WebSession',Session)
    monkeypatch.setattr(web,'run_autopilot',run)
    monkeypatch.setattr(manager,'question',forbidden)
    async def scenario():
        c=manager.cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j1')
        manager.cases.bind_job(c,{'title':'招聘顾问','requirements':'招聘经历'})
        with pytest.raises(ValueError):
            await manager.create({'mode':'boss_autopilot','params':{'case_id':c['id']}})
        j=(await manager.create({'mode':'boss_autopilot','params':{'case_id':c['id'],'read_only':True,'continuous':True,'new_contacts':True}}))['id']
        await manager.tasks[j]
        assert calls[0]['read_only'] and not calls[0]['continuous'] and not calls[0]['new_contacts']
        manager.cases.set_autonomy(c,True,source='explicit user authorization')
        j=(await manager.create({'mode':'boss_autopilot','params':{'case_id':c['id'],'continuous':False}}))['id']
        await manager.tasks[j]
        assert manager.jobs[j]['status']=='succeeded'
    manager.call(scenario())


def test_legacy_browser_restore_record_does_not_break_state(manager):
    async def scenario():
        manager.jobs['d'*32]={'id':'d'*32,'status':'interrupted','events':[]}
        record=(await manager.snapshot())['runs'][0]
        assert record['created']==0 and record['params']=={} and record['mode']=='login'
    manager.call(scenario())


def test_unknown_action_requires_explicit_evidence_and_cannot_override_messages(manager):
    from cloakup.action_gateway import ActionLedger
    ledger=ActionLedger(manager.db)
    def unknown(capability,resource):
        ident=ledger.claim(task_id='task',resource=resource,capability=capability,identity='test',run_id='test-run')
        ledger.transition(ident,'executing');ledger.transition(ident,'unknown')
        return ident
    ident=unknown('web_click','web')
    with pytest.raises(ValueError):manager.call(manager.verify_action(ident,{'state':'verified','evidence':'record 123'}))
    assert ledger.get(ident)['state']=='unknown'
    result=manager.call(manager.verify_action(ident,{'state':'verified','evidence':'record 123 confirmed','confirmed':True}))
    assert result['state']=='verified' and ledger.get(ident)['payload']['verification']=='administrator'
    message=unknown('send_message','candidate')
    with pytest.raises(ValueError):manager.call(manager.verify_action(message,{'state':'verified','evidence':'manual claim','confirmed':True}))
    assert ledger.get(message)['state']=='unknown'


def test_legacy_global_followup_update_is_rejected_without_changing_jobs(manager):
 from cloakup.followup import DEFAULTS
 c=manager.cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=one')
 previous_execution=dict(c['execution'])
 previous_policy=manager.followup_policy()
 policy={**DEFAULTS,'nudge_unread_hours':12,'nudge_read_hours':24}
 with pytest.raises(ValueError,match='追聊策略已迁移到各任务'):
  manager.call(manager.configure_followup_policy({'expected_revision':0,'policy':policy}))
 assert manager.cases.load(c['id'])['execution']==previous_execution
 assert manager.followup_policy()==previous_policy
 assert not manager.db.list('planning_request')
 new=manager.cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=two')
 assert new['execution']['nudge_unread_hours']==previous_execution['nudge_unread_hours']
 assert new['execution']['nudge_read_hours']==previous_execution['nudge_read_hours']


def test_daily_report_endpoints_require_token_to_generate(manager):
    server = web.make_server(manager, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, data=None, headers=None):
        req = urllib.request.Request(base+path, data=json.dumps(data).encode() if data is not None else None, headers=headers or {})
        return urllib.request.urlopen(req)
    try:
        token = json.load(request('/api/state'))['token']
        with pytest.raises(urllib.error.HTTPError) as exc: request('/api/daily-reports/2026-11-20', data={})
        assert exc.value.code == 403
        report = json.load(request('/api/daily-reports/2026-11-20', data={}, headers={'X-Cloakup-Token': token}))
        assert report['day'] == '2026-11-20'
        assert json.load(request('/api/daily-reports'))['reports'][0]['day'] == '2026-11-20'
        markdown = request('/api/daily-reports/2026-11-20.md')
        assert markdown.headers['Content-Type'].startswith('text/markdown') and markdown.read().decode().startswith('# 执行日报')
        with pytest.raises(urllib.error.HTTPError) as exc: request('/api/daily-reports/2026-11-19')
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_snapshot_carries_job_overview_for_sourcing_tasks(manager, monkeypatch):
    # 任务岗位区随状态轮询拿到额度与岗位行；单个任务算不出来时只给该任务带上原因，不拖垮整个状态
    case=manager.cases.create('https://www.zhipin.com/web/chat/job-list');manager.cases.bind_job(case,{'title':'交付顾问','requirements':'支持远程协作。'});manager.cases.save(case)
    task=manager.definitions.save({'config':{'name':'寻访任务','website':'https://www.zhipin.com/','scenario':'寻访','goal':'持续取得简历','engine':'boss_sourcing','case_ids':[case['id']]}})
    async def scenario():return (await manager.snapshot())['job_overview']
    assert 'quota' not in manager.call(scenario())[task['id']]  # 不在账号执行器里的任务不算额度
    manager.account_execution.set_task(task['id'],[case['id']],'paused')
    view=manager.call(scenario())[task['id']]
    assert [j['case_id'] for j in view['jobs']]==[case['id']] and view['quota']['account_cap']==20
    from cloakup import job_changes
    monkeypatch.setattr(job_changes,'overview',lambda *a:(_ for _ in ()).throw(RuntimeError('boom')))
    assert manager.call(scenario())[task['id']]=={'jobs':[],'changes':[],'error':'岗位视图暂时无法读取：boom'}


def test_snapshot_includes_today(manager, monkeypatch):
    # 11-25 Task 2：首页“今天”随状态下发；today.build 出错只给兜底文字，不把整个工作台拖垮
    async def scenario():return await manager.snapshot()
    state=manager.call(scenario())
    assert 'today' in state and state['today']['tasks']==[]
    from cloakup import today
    monkeypatch.setattr(today,'build',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('boom')))
    state=manager.call(scenario())
    assert state['today']=={'error':'今天的数据暂时算不出来'} and 'attention' in state


def test_restart_settles_job_adds_whose_validation_was_interrupted(manager):
    # 11-24 复审：重启时隔离验收被标为中断，排队中的加入请求随之结算为失败（生成待办、可重新加入），不再停在“验收中”
    manager.db.put('agent_test','a'*32,{'id':'a'*32,'task_id':'b'*32,'status':'running','mode':'validate','bundle':{},'fixtures':[]})
    manager.db.put('job_change','c'*32,{'id':'c'*32,'task_id':'b'*32,'action':'add','case_id':'d'*32,'payload':{'weight':1},'status':'testing','test_id':'a'*32,'at':1})
    restarted=web.Workbench(manager.root)
    try:
        record=restarted.db.get('job_change','c'*32)
        assert record['status']=='failed' and '中断' in record['error']
    finally:restarted.close()


def test_boss_job_picker_routes_read_then_bind_through_the_shared_binding(manager, monkeypatch):
    # 11-25 Task 7：没有执行器时，刷新与绑定各起一次 BOSS 操作（boss_errand）；绑定复用 boss_bind 的读 JD → 建档 → 计划草稿。
    from types import SimpleNamespace
    from cloakup import boss_jobs
    eid = 'abcDEF123456~~'
    class Session:
        def __init__(self, *a): self.page = SimpleNamespace(url='about:blank', frames=[]); self.context = None
        async def start(self): pass
        async def navigate(self, url): self.page.url = url
        async def close(self): pass
    async def read_published(session):
        return {'jobs': [{'encrypt_id': eid, 'title': '数据顾问', 'detail': '上海', 'status': 'open', 'status_text': '开放中', 'recommendable': True, 'option_text': '数据顾问 上海'},
                         {'encrypt_id': None, 'title': '交付顾问', 'detail': '北京', 'status': 'open', 'status_text': '开放中', 'recommendable': False, 'option_text': None}],
                'read_at': 1.0, 'truncated': False, 'option_count': 1, 'row_count': 2, 'total_rows': 2}
    async def read_job(session, url):
        assert url == boss_jobs.job_url(eid)
        return {'title': '数据顾问', 'requirements': '负责数据整理', 'company': '', 'location': '上海', 'source_url': url, 'source_type': 'visible_job_form', 'platform_fields': []}
    async def prepare_plan(llm, snapshot):
        return {'greeting': '您好，方便聊聊吗？', 'keywords': [], 'requirements': [], 'follow_up_questions': []}
    async def guard(session): return None
    monkeypatch.setattr(web, 'WebSession', Session);monkeypatch.setattr(boss_jobs, 'read_published', read_published)
    monkeypatch.setattr(boss_jobs, '_guard', guard);monkeypatch.setattr(web, 'read_job', read_job);monkeypatch.setattr(web, 'prepare_plan', prepare_plan)
    from cloakup import browser_errands, page_health
    async def chat_check(session, kind): return {'healthy': True, 'reason': None, 'has_draft': False}
    monkeypatch.setattr(browser_errands, 'window_open', lambda *a, **k: True);monkeypatch.setattr(page_health, 'check', chat_check)
    server = web.make_server(manager, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, data=None, token=None):
        req = urllib.request.Request(base+path, data=json.dumps(data).encode() if data is not None else None, headers={'X-Cloakup-Token': token} if token else {})
        return json.load(urllib.request.urlopen(req))
    def wait_idle():
        async def idle():
            while manager.busy(): await asyncio.sleep(0.01)
        manager.call(idle())
    try:
        token = request('/api/state')['token']
        assert request('/api/boss/jobs')['jobs'] == []
        with pytest.raises(urllib.error.HTTPError) as exc: request('/api/boss/jobs/refresh', {})
        assert exc.value.code == 403
        assert request('/api/boss/jobs/refresh', {}, token)['kind'] == 'job_list'
        wait_idle()
        listed = request('/api/boss/jobs')
        assert [j['bindable'] for j in listed['jobs']] == [True, False] and listed['read_at'] == 1.0
        with pytest.raises(urllib.error.HTTPError) as exc: request('/api/boss/jobs/bind', {'encrypt_ids': ['zzzzzz999999']}, token)
        assert exc.value.code == 400
        request('/api/boss/jobs/bind', {'encrypt_ids': [eid]}, token)
        wait_idle()
        job = request('/api/boss/jobs')['jobs'][0]
        assert job['bound_case_id'] and manager.cases.load(job['bound_case_id'])['greeting_draft'] == '您好，方便聊聊吗？'
        assert [r['mode'] for r in manager.jobs.values()] == ['boss_errand', 'boss_errand']
        assert all(r['status'] == 'succeeded' for r in manager.jobs.values())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_bind_checks_the_job_identity_before_creating_and_resumes_a_stub(manager, monkeypatch):
    # 11-25 审阅：读到的岗位地址没有或换了 encryptId 时不建档（重试不会多出一份）；上次只建了档时接着用那份档案。
    from types import SimpleNamespace
    from cloakup import boss_jobs
    eid = 'abcDEF123456~~';url = boss_jobs.job_url(eid)
    source = {'url': 'https://www.zhipin.com/web/chat/job/edit'}
    async def read_job(session, job_url):
        return {'title': '数据顾问', 'requirements': '负责数据整理', 'company': '', 'location': '上海', 'source_url': source['url'], 'source_type': 'visible_job_form', 'platform_fields': []}
    async def prepare_plan(llm, snapshot): return {'greeting': '您好', 'keywords': [], 'requirements': [], 'follow_up_questions': []}
    monkeypatch.setattr(web, 'read_job', read_job);monkeypatch.setattr(web, 'prepare_plan', prepare_plan)
    job = {'id': 'e'*32, 'events': [], 'status': 'running'}
    with pytest.raises(web.SourcingError, match='岗位标识'):await manager.bind_boss_job(job, SimpleNamespace(), url, encrypt_id=eid)
    assert manager.cases.list() == []
    stub = manager.cases.create(url, '待绑定岗位');source['url'] = url
    outcome = await manager.bind_boss_job(job, SimpleNamespace(), url, case_id=stub['id'], encrypt_id=eid)
    assert outcome['case_id'] == stub['id'] and len(manager.cases.list()) == 1 and manager.cases.load(stub['id'])['search_plan']


async def test_finished_errand_run_starts_a_follow_up_for_late_requests(manager, monkeypatch):
    # 11-25 审阅：代办运行结束时还有运行中新登记的代办，浏览器空闲就再起一次，不被丢下。
    from cloakup import browser_errands
    calls = []
    async def after_run(m, job): calls.append(job['id'])
    async def run_direct(m, job, session): return {'status': 'succeeded', 'summary': 'ok'}
    class Session:
        def __init__(self, *a): self.context = None
        async def start(self): pass
        async def close(self): pass
    monkeypatch.setattr(browser_errands, 'after_run', after_run);monkeypatch.setattr(browser_errands, 'run_direct', run_direct);monkeypatch.setattr(web, 'WebSession', Session)
    jid = (await manager.create({'mode': 'boss_errand', 'params': {}}))['id']
    await manager.tasks[jid]
    assert calls == [jid]


def test_snapshot_reads_lock_tables_once(manager, monkeypatch):
    # 11-25 岗位锁定随 /api/state 轮询下发：每次快照只批量算一次（两处 cases 共用），结果与逐个 locked_task 一致
    cases=[manager.cases.create(f'https://www.zhipin.com/web/chat/job/edit?encryptId=j{i}') for i in range(3)]
    task=manager.definitions.save({'config':{'name':'寻访任务','website':'https://www.zhipin.com/','scenario':'寻访','goal':'持续取得简历','engine':'boss_sourcing','case_ids':[cases[0]['id']]}})
    manager.db.put('agent_release',task['id'],{'id':task['id'],'version_id':'v1'})
    from cloakup import job_config
    locks={c['id']:job_config.locked_task(manager.db,c['id']) for c in cases}
    calls=[]
    real_batch=job_config.locked_tasks
    monkeypatch.setattr(web,'locked_tasks',lambda db,ids:(calls.append(list(ids)),real_batch(db,ids))[1])
    snap=manager.call(manager.snapshot())
    assert len(calls)==1 and {c['id']:c['config_locked_by'] for c in snap['cases']}==locks
    assert locks[cases[0]['id']]=={'id':task['id'],'name':'寻访任务'} and locks[cases[1]['id']] is None
