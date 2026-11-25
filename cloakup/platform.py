"""Website + scenario tasks and goal-driven exploration / implementation."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import os
import re
import tempfile
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin, urlparse
import jsonschema
from .config import AgentConfig, BrowserConfig, PacingConfig
from .browser import BrowserSession
from .pacing import jitter
from .tools import Executor, ToolOutcome

PLAN_SCHEMA = {'type':'object','required':['steps','success_criteria','risks'], 'properties':{
    'steps':{'type':'array','minItems':1,'items':{'type':'string','minLength':1}},
    'success_criteria':{'type':'array','minItems':1,'items':{'type':'string','minLength':1}},
    'risks':{'type':'array','items':{'type':'string'}}},'additionalProperties':False}

def website(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError('请输入网站地址')
    u = urlparse(value)
    if u.scheme not in ('https','http') or not u.hostname or u.username or u.password or u.fragment:
        raise ValueError('网站需使用 http/https 地址，不得包含密码或页面片段')
    # Never use generic page observations for the already restricted BOSS data.
    return u

class TaskStore:
    def __init__(self, db): self.db = db
    def get(self, key):
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key): raise ValueError('任务标识不合法')
        return self.db.get('task',key)
    def list(self): return self.db.list('task')
    def save(self, body, key=None, *, validate_only=False):
        old = self.get(key) if key else None
        cfg = copy.deepcopy(body.get('config',{}))
        if not isinstance(cfg,dict): raise ValueError('任务配置必须为对象')
        for k,limit in [('name',100),('scenario',100),('goal',8000)]:
            if not isinstance(cfg.get(k),str) or not cfg[k].strip() or len(cfg[k])>limit: raise ValueError(f'请填写有效的 {k}')
            cfg[k]=cfg[k].strip()
        u = website(cfg.get('website'))
        engine=cfg.setdefault('engine','web')
        from .scenarios import is_sourcing, scenario_for_engine, scenario_for_host
        spec=scenario_for_engine(engine)
        from .task_notifications import validate_config as notifications_config
        from .task_policy import checked_schedule
        cfg['notifications']=notifications_config(cfg.get('notifications'))
        cfg['schedule']=checked_schedule(cfg.get('schedule',{}))
        host_spec=scenario_for_host(u.hostname)
        if spec['kind']=='sourcing':
            if host_spec is None or host_spec['engine']!=spec['engine']:
                raise ValueError('寻访场景的网站须与该场景登记的网站一致')
        elif host_spec is not None:
            raise ValueError('该网站已登记为寻访场景，请使用对应的寻访适配器')
        # 一个网站 + 一个场景只对应一个任务。寻访场景的新岗位并入已有任务。
        for other in self.db.list('task'):
            if other['id']!=key and self._scope(other)==self._scope({'config':cfg}):
                name=other['config'].get('name','')
                if spec['kind']=='sourcing':
                    raise ValueError(f"同一网站与场景已有任务「{name}」；请把岗位加入该任务，不另建任务")
                raise ValueError(f"同一网站与场景已有任务「{name}」")
        if not isinstance(cfg.setdefault('params',{}),dict): raise ValueError('参数需要为 JSON 对象')
        schema=cfg.setdefault('output_schema',{'type':'object'})
        if not isinstance(schema,dict): raise ValueError('结果 Schema 需要为对象')
        try: jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as e: raise ValueError('结果 Schema 不合法') from e
        if type(cfg.setdefault('max_steps',40)) is not int or not 5<=cfg['max_steps']<=200: raise ValueError('步骤上限需为5至200')
        if cfg.setdefault('data_policy','public_text') not in ('public_text','anonymized_job_text'): raise ValueError('不支持的数据策略')
        if engine=='web' and cfg['data_policy']!='public_text': raise ValueError('通用引擎目前只处理已授权的公开页面文字')
        if is_sourcing(engine):
            cfg['data_policy']='anonymized_job_text'
            if cfg.setdefault('sourcing_mode','continuous') not in ('continuous','quota'):raise ValueError('寻访模式不合法')
            from .job_members import normalize as normalize_members
            if old and 'members' not in cfg and old['config'].get('members') is not None:cfg['members']=copy.deepcopy(old['config']['members'])  # 任务表单只提交 case_ids：沿用已存的权重与首聊状态
            normalize_members(cfg)
            ids=cfg.get('case_ids',[])
            if not isinstance(ids,list) or not 1<=len(ids)<=20 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids): raise ValueError('请选择1至20个岗位')
            if any(m['id']!=key and m['status']!='stopped' and set(ids)&set(m['case_ids']) for m in self.db.list('account_task')):raise ValueError('岗位已由启用任务管理，请先停止原任务，再重新绑定')
            for field,default,low,high in [('target_resumes',1,1,100),('max_cycles',3,1,100)]:
                if type(cfg.setdefault(field,default)) is not int or not low<=cfg[field]<=high: raise ValueError('推进目标或轮次上限不合法')
            from .task_policy import normalize_config
            normalize_config(self.db,cfg)
            for cid in ids:
                try:case=self.db.get('case',cid)
                except FileNotFoundError:continue
                if case.get('platform') and case['platform']!=spec['platform']:
                    raise ValueError('岗位属于另一个寻访场景，请加入该场景的任务')
                if any(m['status']=='sending' for p in case.get('candidates',{}).values() for m in p.get('outbox',[])):raise ValueError('岗位有消息发送中，请待回执核实后保存任务策略')
        from .prompts import DEFAULT_PLANNING_PROMPT,DEFAULT_WEB_PLANNING_PROMPT
        prompt=cfg.setdefault('planning_prompt',DEFAULT_WEB_PLANNING_PROMPT if engine=='web' else DEFAULT_PLANNING_PROMPT)
        if engine=='web' and prompt==DEFAULT_PLANNING_PROMPT:prompt=DEFAULT_WEB_PLANNING_PROMPT
        if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=12000:raise ValueError('规划提示词必须为1至12000字')
        from .recruiting_ai import IDENTIFIER
        if IDENTIFIER.search(prompt):raise ValueError('规划提示词请勿填写候选身份、联系方式或具体任职公司')
        cfg['planning_prompt']=prompt.strip()
        plan=cfg.setdefault('plan',None)
        if plan is not None:
            try: jsonschema.validate(plan,PLAN_SCHEMA)
            except jsonschema.ValidationError as e: raise ValueError('探索计划应包含 steps、success_criteria、risks 数组') from e
        revision=(old['revision'] if old else 0)+1
        expected=body.get('expected_revision')
        if old and (type(expected) is not int or expected!=old['revision']): raise ValueError('配置版本已变化，请刷新后再保存')
        item={'id':key or uuid.uuid4().hex,'revision':revision,'created_at':old['created_at'] if old else time.time(),'updated_at':time.time(),'config':cfg}
        if validate_only:return item
        if old and any(r['id']==key for r in self.db.list('agent_release')):
            raise ValueError('该任务已启用验证后发布，请在规划端 Agent 的配置草案中修改、验证并发布')
        self.db.put('task',item['id'],item,expected=revision-1,history=True)
        if is_sourcing(engine):
            from .task_policy import apply_to_cases
            apply_to_cases(self.db,item)
        return item

    @staticmethod
    def _scope(task):
        """任务的范围：网站主机名 + 场景。同一范围只允许一个任务。"""
        u=urlparse(task['config'].get('website') or '')
        return ((u.hostname or '').lower(), task['config'].get('scenario'))

    def import_cases(self,cases):
        """导入岗位：并入同一网站与场景的已有任务；没有则新建一个。返回未能并入的岗位编号。

        已启用验证后发布的任务不能直接修改，新岗位须在该任务的草案中加入并发布。
        """
        from .scenarios import scenario_for_host, scenario_for_platform
        existing={cid for i in self.list() for cid in i['config'].get('case_ids',[])}
        unattached=[]
        for c in cases:
            if c['id'] in existing or not c['job'].get('snapshot'): continue
            spec=scenario_for_platform(c.get('platform')) or scenario_for_host(urlparse(c['job'].get('url') or '').hostname)
            if spec is None: continue
            scope=(urlparse(spec['default_website']).hostname, spec['default_scenario'])
            target=next((t for t in self.list() if self._scope(t)==scope),None)
            if target:
                cfg=copy.deepcopy(target['config'])
                cfg['case_ids']=[*cfg.get('case_ids',[]),c['id']]
                try:self.save({'expected_revision':target['revision'],'config':cfg},target['id'])
                except ValueError:
                    unattached.append(c['id']);continue
                existing.add(c['id']);continue
            self.save({'config':{'name':c['job']['label']+' · 寻访','website':spec['default_website'],'scenario':spec['default_scenario'],'engine':spec['engine'],'goal':'按岗位 JD 寻访合适候选人，持续跟进，获取简历后交接；不承诺未核实的信息。','case_ids':[c['id']],'imported_case':c['id']}})
        return unattached

class ExplorationExecutor(Executor):
    """Exploration is read-only at the tool boundary, not just a prompt."""
    async def _dispatch(self,name,a):
        if name=='click':
            el=self.browser.last_obs.find(int(a['element_id']))
            href=el.raw.get('href') if el else None
            if not href or el.raw.get('tag')!='a':
                return ToolOutcome('探索阶段只允许阅读链接，不能提交或修改页面；记录此入口供实施阶段使用。',is_error=True)
            target=urljoin(self.browser.page.url,href)
            if re.search(r'delete|logout|remove|unsubscribe|confirm|submit|action=',target,re.I):
                return ToolOutcome('探索阶段拒绝可能产生变更的链接。',is_error=True)
            return await super()._dispatch('navigate',{'url':target})
        if name=='navigate' and re.search(r'delete|logout|remove|unsubscribe|confirm|submit|action=',str(a.get('url','')),re.I):
            return ToolOutcome('探索阶段拒绝可能产生变更的地址。',is_error=True)
        if name not in ('navigate','go_back','scroll','hover','wait','ask_human','save_data','done'):
            return ToolOutcome('探索阶段不执行输入、提交或修改；请把操作方法写入计划。',is_error=True)
        return await super()._dispatch(name,a)

class PublicPageAgentMixin:
    """Deny restricted pages *before* sending any model request, including redirects."""
    def _obs_blocks(self,obs):
        urls=[obs.url]+[e.raw.get('frame_url','') for e in obs.elements]
        for url in urls:
            host=urlparse(url).hostname or ''
            if host=='zhipin.com' or host.endswith('.zhipin.com'):
                raise ValueError('BOSS 页面只能经专用脱敏适配器处理')
            if host and not any(host==d or host.endswith('.'+d) for d in self.cfg.allowed_domains):
                raise ValueError('页面跳转到任务授权范围外，已停止模型读取')
        if any(e.raw.get('type')=='password' for e in obs.elements) or obs.raw.get('challenge'):
            raise ValueError('当前为登录或验证页，请通过浏览器完成登录后重试')
        return super()._obs_blocks(obs)

def environment_signature():
    fields=['DATABASE_URL','ANTHROPIC_BASE_URL','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLOAKUP_MODEL','CLOAKBROWSER_LICENSE_KEY','DISPLAY']
    return hashlib.sha256(json.dumps({**{k:os.environ.get(k,'') for k in fields},'harness_contract':'universal-v1','executor_contract':'executor-v1','executor_runtime':{name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in ('executor_check.py','dom_reader.py','browser.py','observe.py','locate.py','boss_chat.py')}},sort_keys=True).encode()).hexdigest()

def reusable_preflight(previous, signature, now):
    """Reuse recent successes only for the same environment signature."""
    if not previous or previous.get('signature')!=signature or previous.get('status')!='failed':
        return {}
    reuse={}
    for item in previous.get('checks',[]):
        checked_at=item.get('checked_at',previous.get('finished_at',0))
        if item.get('status')=='passed' and 0<=now-checked_at<600:
            reuse[item['key']]={**item,'checked_at':checked_at,'reused':True}
    if not {'browser','executor'}<=reuse.keys() or previous.get('executor',{}).get('status')!='passed' or previous.get('executor',{}).get('contract')!='executor-v1':
        reuse.pop('browser',None)
        reuse.pop('executor',None)
    return reuse


WARN_ONLY = {'boss_pages'}


async def boss_pages(manager):
    """环境检查“BOSS 页面可用”（11-24 20:40 沟通页卡在启动页）：用执行浏览器实际打开沟通页、推荐牛人页，不正常就自愈。
    没有执行浏览器或不在任务执行时段时不打开网站，只说明原因。"""
    from . import page_health
    from .task_policy import window_status
    live=manager.boss_session if manager.browser_alive(manager.boss_session) else None
    if not live:return '执行浏览器未启动，未检查 BOSS 页面；任务运行时执行器每轮开始前会自动检查'
    schedules=[]
    for member in manager.account_execution.members():
        if member.get('status') in ('stopped','completed'):continue
        try:schedule=manager.db.get('task',member['id'])['config'].get('schedule')
        except (FileNotFoundError,KeyError):schedule=None
        if schedule:schedules.append(schedule)
    if schedules and not any(window_status(s)['allowed'] for s in schedules):
        return '当前不在任务执行时段，未打开 BOSS 页面；进入时段后执行器每轮开始前会自动检查'
    lock=getattr(manager,'_browser_diagnostic_lock',None)
    if lock is None:lock=manager._browser_diagnostic_lock=asyncio.Lock()
    if lock.locked():raise ValueError('已有执行器诊断正在进行，请等待完成')
    async with lock:
        results=await page_health.check_pages(live)
    detail=page_health.describe(results)
    # 不正常时抛出，由 preflight 记为提醒（warning）：报告写明哪一页、卡在哪一步、做了什么，但不拦任务启动。
    if not all(r.get('healthy') and not r.get('has_draft') for r in results) or len(results)<2:raise ValueError(detail)
    return detail


async def preflight(manager, report, *, reuse=None):
    reuse=reuse or {}
    async def check(key,title,fn,timeout=30):
        if key in reuse:
            report['checks'].append(dict(reuse[key]))
            manager.db.put('readiness',report['id'],report)
            return
        started=time.monotonic()
        item={'key':key,'title':title,'status':'running'}
        report['checks'].append(item); manager.db.put('readiness',report['id'],report)
        try:
            detail=await asyncio.wait_for(fn(),timeout)
            item.update(status='passed',detail=detail)
        except Exception as e:
            # Errors can contain headers/URLs; only expose known configuration hints.
            if type(e).__name__=='CloakBrowserLicenseError':
                hint=('浏览器并发会话名额已占用，请关闭其他环境的 CloakBrowser 后重试'
                      if 'session limit' in str(e).lower() else 'CloakBrowser 授权失败，请检查许可证与网络')
            elif isinstance(e,TimeoutError):
                hint={
                    'model':'模型网关未在规定时间内响应',
                    'harness':'Harness 合成请求未在规定时间内返回',
                    'browser':'浏览器启动、观察或隔离动作检查超时',
                    'boss_pages':'BOSS 沟通页或推荐牛人页检查超时（含自动刷新、重开标签页、从菜单进入）',
                }.get(key,title+'超时')
            else: hint=str(e) if isinstance(e,ValueError) else type(e).__name__
            for name in ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLOAKBROWSER_LICENSE_KEY','DATABASE_URL'):
                if os.environ.get(name): hint=hint.replace(os.environ[name],'[已隐藏]')
            # BOSS 页面检查只提醒、不拦启动（复审决定）：不通过记为 warning，不影响整份报告是否通过。
            item.update(status='warning' if key in WARN_ONLY else 'failed',detail=hint[:300])
        item.update(duration_ms=int((time.monotonic()-started)*1000),checked_at=time.time())
        manager.db.put('readiness',report['id'],report)
    async def db(): return await asyncio.to_thread(manager.db.probe)
    async def disk():
        path=manager.root/'runs'/('.probe-'+uuid.uuid4().hex)
        try:
            path.write_text('cloakup'); assert path.read_text()=='cloakup'
        finally: path.unlink(missing_ok=True)
        return '数据卷可读写'
    async def model():
        if not (os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')): raise ValueError('未配置模型凭据')
        response=await manager.model_client().create(model=__import__('cloakup.client',fromlist=['default_model']).default_model(),max_tokens=32,messages=[{'role':'user','content':'Reply with exactly CLOAKUP_OK. This is a synthetic connectivity test.'}])
        text=''.join(b.text for b in response.content if b.type=='text')
        if text.strip()!='CLOAKUP_OK': raise ValueError('模型返回内容不符合连通测试约定')
        return '模型已响应合成测试；未发送业务资料'
    async def harness():
        from .harness_runtime import HarnessPlanner
        from .llm import LLM
        result=await HarnessPlanner(LLM(manager.model_client()),manager.root).json(system='Synthetic readiness check. Return JSON only.',content='Return {"ready":true}. No business data.',schema={'type':'object','required':['ready'],'properties':{'ready':{'const':True}},'additionalProperties':False})
        if result!={'ready':True}:raise ValueError('Harness响应未通过校验')
        return '官方 Harness 运行时与结构化响应通过；未发送业务资料'
    async def browser():
        if not os.environ.get('CLOAKBROWSER_LICENSE_KEY'): raise ValueError('未配置 CloakBrowser 授权；不会占用业务浏览器')
        profiles=manager.root/'profiles'
        profiles.mkdir(parents=True,exist_ok=True)
        probe_profile=Path(tempfile.mkdtemp(prefix='preflight-',dir=profiles))
        session=BrowserSession(BrowserConfig(profile_dir=str(probe_profile),headless=os.environ.get('CLOAKUP_HEADLESS')=='1'),PacingConfig())
        live=manager.boss_session if manager.browser_alive(manager.boss_session) else None
        previous=(live.page,live.last_obs,live._observed_frames) if live else None
        probe_page=None
        lock=getattr(manager,'_browser_diagnostic_lock',None)
        if lock is None:lock=manager._browser_diagnostic_lock=asyncio.Lock()
        locked=False
        try:
            if lock.locked():raise ValueError('已有执行器诊断正在进行，请等待完成')
            await lock.acquire();locked=True
            if live:
                # One-seat plans cannot launch a second process. A synthetic
                # tab verifies the live process without reading business pages.
                probe_page=await live.context.new_page()
                session.context=live.context
                session.page=probe_page
            else:
                owner=manager.jobs.get(report.get('run_id'))
                if owner: await manager.start_browser(owner,session)
                else: await session.start()
            await session.page.goto('data:text/html,<title>Cloakup readiness</title><button>Ready</button>')
            obs=await session.observe(False)
            if obs.title!='Cloakup readiness' or not obs.elements: raise ValueError('浏览器观察能力未通过')
            try:
                from .executor_check import verify,url
                await session.page.goto(url())
                report['executor']=await verify(session)
            except Exception as exc:
                report['executor']=getattr(exc,'report',None) or {'contract':'executor-v1','status':'failed','reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__,'business_writes':0}
            return '现有浏览器通过独立测试页核验；业务页面已恢复' if live else 'CloakBrowser 启动与页面观察通过；使用独立测试身份'
        finally:
            try:
                if live:
                    try:
                        if probe_page: await probe_page.close()
                    finally:
                        live.page,live.last_obs,live._observed_frames=previous
                        session.context=None
                else: await session.close()
            finally:
                if locked:lock.release()
                shutil.rmtree(probe_profile,ignore_errors=True)
    async def executor():
        result=report.get('executor',{})
        if result.get('status')!='passed':raise ValueError(result.get('reason','浏览器未就绪，执行器链路未核验'))
        return '隔离测试页通过定位、原生输入、原生点击、结果回读与单次执行核验；不代表目标网站业务已通过'
    try:
        for key,title,fn,timeout in [('database','数据库事务',db,10),('storage','持久卷读写',disk,5),('model','模型网关响应',model,35),('harness','Harness运行时',harness,130),('browser','浏览器与观察能力',browser,150),('executor','执行器完整链路',executor,5),('boss_pages','BOSS 页面可用',lambda:boss_pages(manager),300)]:
            await check(key,title,fn,300 if key=='browser' and report.get('run_id') else timeout)
        report.update(status='passed' if all(c['status'] in ('passed','warning') for c in report['checks']) else 'failed',
                      warnings=[c['key'] for c in report['checks'] if c['status']=='warning'],finished_at=time.time())
    except asyncio.CancelledError:
        report.update(status='interrupted',finished_at=time.time())
        raise
    finally: manager.db.put('readiness',report['id'],report)

async def run_task(manager,job,session,agent_class,confirm,ask):
    cfg=job['task_snapshot']['config']
    if cfg['engine']=='boss_sourcing':
        from .autopilot import run_autopilot
        manager.phase(job,'exploration','从已绑定 JD、可编辑岗位计划和沟通菜单核验执行条件')
        ids=cfg['case_ids']
        for cid in ids:
            c=manager.cases.load(cid)
            if not c['job'].get('snapshot'): raise ValueError('所选岗位未完成绑定')
            if not job['params'].get('explore_only') and not c.get('autonomy',{}).get('enabled'): raise ValueError('所选岗位未启用自动化')
        original=job['params']
        try:
            job['params']={'case_id':ids[0],'case_ids':ids,'continuous':False,'new_contacts':False,'read_only':True}
            discovery=await run_autopilot(manager,job,session)
            if discovery['status']!='succeeded': raise ValueError('寻访探索未能核验沟通入口，请检查登录与岗位权限')
        finally: job['params']=original
        plan=cfg.get('plan') or {'steps':['核验岗位与话术配置','同步沟通历史并核对回执','寻访、跟进并索要简历','持续记录归档进度，暂停时保留结果'], 'success_criteria':(['持续取得并归档合适候选人的真实简历；不限累计数量，直到管理员暂停或停止'] if cfg.get('sourcing_mode','continuous')=='continuous' else [f"获取 {cfg['target_resumes']} 份简历"]), 'risks':['登录或平台限制时停止；不重发结果未知消息']}
        manager.record_plan(job,plan,origin='configured' if cfg.get('plan') else 'boss_adapter')
        if job['params'].get('explore_only'): return {'success':True,'summary':'寻访探索完成，未联系候选人','result':plan}
        manager.phase(job,'implementation','按岗位策略自动推进候选人')
        original=job['params']; rounds=[]
        def qualified():
            return [{'case_id':cid,'candidate_id':c['id']} for cid in ids
                    for c in manager.cases.load(cid)['candidates'].values()
                    if not c.get('alias_of') and c.get('resumes')]
        ready=qualified()
        if cfg.get('sourcing_mode','continuous')=='quota' and len(ready)>=cfg['target_resumes']:
            return {'success':True,'summary':f'已核验 {len(ready)} 位候选人的简历，目标已满足','result':{'candidates':ready,'rounds':[]}}
        try:
            if cfg.get('sourcing_mode','continuous')=='continuous':
                cycle=0
                while True:
                    await manager.checkpoint(job);cycle+=1
                    for cid in ids:
                        job['params']={'case_id':cid,'continuous':False,'new_contacts':True,'read_only':False}
                        result=await run_autopilot(manager,job,session)
                        if result['status']!='succeeded':return {'success':False,'summary':result['summary'],'result':{'candidates':qualified()}}
                    # 两轮之间的等待以配置轮询间隔为中位数随机抖动，避免固定周期。
                    for _ in range(round(jitter(max(manager.cases.load(cid).get('autonomy',{}).get('poll_seconds',120) for cid in ids)))):
                        await asyncio.sleep(1);await manager.checkpoint(job)
            for cycle in range(cfg['max_cycles']):
                await manager.checkpoint(job)
                job['params']={'case_id':ids[0],'case_ids':ids,'continuous':False,'new_contacts':True,'read_only':False}
                for cid in ids:
                    if manager.cases.load(cid).get('config_revision',0)!=job['case_config_snapshots'][cid].get('revision',0):
                        raise ValueError('岗位配置已修改，本次任务停止，请按新配置重新运行')
                result=await run_autopilot(manager,job,session); rounds.append(result)
                ready=qualified()
                if cfg.get('sourcing_mode','continuous')=='quota' and len(ready)>=cfg['target_resumes']:
                    return {'success':True,'summary':f'已取得 {len(ready)} 位候选人的简历','result':{'candidates':ready,'rounds':rounds}}
                if result['status']!='succeeded': break
                if cycle+1<cfg['max_cycles']:
                    manager.phase(job,'implementation',f"已完成第 {cycle+1} 轮，等待候选人回复")
                    wait_seconds=round(jitter(max(manager.cases.load(i).get('autonomy',{}).get('poll_seconds',120) for i in ids)))
                    for waiting_second in range(wait_seconds):
                        await asyncio.sleep(1)
                        await manager.checkpoint(job)
                        if waiting_second+1==wait_seconds:
                            job['params']={'case_id':ids[0],'case_ids':ids,'continuous':False,'new_contacts':False,'read_only':False}
                            inbox_result=await run_autopilot(manager,job,session)
                            if inbox_result['status']!='succeeded':
                                return {'success':False,'summary':inbox_result['summary'],'result':{'rounds':rounds}}
                            ready=qualified()
                            if cfg.get('sourcing_mode','continuous')=='quota' and len(ready)>=cfg['target_resumes']:
                                return {'success':True,'summary':f'已取得 {len(ready)} 位候选人的简历','result':{'candidates':ready,'rounds':rounds}}
            return {'success':False,'summary':'本次预算内未达到简历目标；已保留推进进度，可继续运行','result':{'rounds':rounds,'target_resumes':cfg['target_resumes']}}
        finally: job['params']=original
    class Agent(PublicPageAgentMixin,agent_class): pass
    host=urlparse(cfg['website']).hostname
    ac=AgentConfig(allowed_domains=[host],screenshots=False,max_steps=cfg['max_steps'],trace_dir=None)
    current=next((p for p in manager.task_plan_records() if p.get('task_id')==job['task_id'] and not p.get('stale') and p.get('harness_trace')),None)
    if not current:current=await manager.generate_definition_plan(job['task_id'])
    reference=current['plan']
    manager.phase(job,'exploration','Harness 基于任务规划读取页面并核验路径与验收条件')
    agent=Agent(manager,job,agent=ac,session=session,client=manager.model_client(),confirm=confirm,ask=ask,harness_root=manager.root,reference_plan=reference)
    agent.executor=ExplorationExecutor(session,agent.guard,ask=ask)
    prompt=('只读探索这个网站中指定场景的操作路径，不执行任何业务提交。目标：'+cfg['goal']+'\n场景：'+cfg['scenario']+'\n识别实际可见入口、必要信息与结果验证方法。以计划 Schema 返回。用户编辑的计划是执行约束，需核验其可行性，不能悄悄删改其目标与边界：'+json.dumps({'configured':cfg.get('plan'),'harness_reference':reference},ensure_ascii=False))
    result=await agent.run(prompt,cfg['website'],cfg['params'],PLAN_SCHEMA)
    if not result.success: return asdict(result)
    manager.record_plan(job,result.result,origin='llm')
    if job['params'].get('explore_only'): return {'success':True,'summary':'探索完成，操作计划已保存，尚未实施业务操作','result':result.result}
    if manager.definitions.get(job['task_id'])['revision']!=job['task_snapshot']['revision']: raise ValueError('探索期间任务配置发生变化，请重新运行')
    reference=result.result
    manager.phase(job,'implementation','Harness 持续选择动作，并逐步验证页面反馈')
    agent=Agent(manager,job,agent=ac,session=session,client=manager.model_client(),confirm=confirm,ask=ask,harness_root=manager.root,reference_plan=reference)
    result=await agent.run(cfg['goal']+'\n按此探索计划自主决策，只有页面证据证明达到目标才可报告成功：'+json.dumps(result.result,ensure_ascii=False),None,cfg['params'],cfg['output_schema'])
    return asdict(result)
