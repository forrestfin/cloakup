"""Loopback-only web workbench, backed by the existing browser/recipe engine.

HTTP threads dispatch to one asyncio loop. Only one browser/compile job runs at a
 time; state lives under runs/workbench, while recipes remain ordinary YAML.
"""
from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import logging
import os
import re
import secrets
import signal
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from .answering import ANSWER_ORDER, ANSWER_POLICY_VERSION
from .conversation_semantics import jd_review
from .account_execution import AccountExecution
from .agent import WebAgent
from .browser import BrowserSession
from .client import ModelClient, default_model
from .compile import compile_trace
from .config import AgentConfig, BrowserConfig, PacingConfig
from .llm import LLM
from .recipe import Recipe, RecipeError
from .runner import RecipeRunner
from .sourcing import CaseStore, SourcingError
from .boss import read_job, prepare_plan, validate_job_url
from .boss_contact import run_first_contact, valid_message
from .boss_recommend import open_list as read_recommendations, run_recommend_review
from .autopilot import run_autopilot
from .job_config import view_config, save_config, locked_tasks
from .facts import draft_anonymous_job
from .access import is_boss_url, boss_login_required, boss_verification_required, boss_access_ready, can_recover_login
from .attention import AttentionStore, sourcing_overview
from .storage import Database
from .platform import TaskStore, preflight, environment_signature, run_task
from .pacing import jitter

BROWSER_LICENSE_RETRIES = 3

ASSETS = Path(__file__).with_name("web")
ACTIVE = {"queued", "running", "pausing", "paused", "waiting", "compiling", "cancelling", "closing"}
BOSS_MODES = ("browser_open", "login", "boss_bind", "boss_contact", "boss_recommend", "boss_autopilot", "boss_errand")
BOOK_SCHEMA = {"type": "object", "required": ["books"], "properties": {"books": {
    "type": "array", "items": {"type": "object", "required": ["title", "price", "in_stock"],
    "properties": {"title": {"type": "string"}, "price": {"type": "string"},
                   "in_stock": {"type": ["integer", "null"]}}}}}}
BOOK_TASK = "在 books.toscrape.com 打开 {{category}} 分类，逐本查看前 {{max_items}} 本书，记录书名、价格和库存数量。"


def _exception_frames(exc):
    # Source locations only: never serialize locals, source text or page data.
    frames=[]
    current=exc
    seen=set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        frames.extend({'file':Path(f.filename).name,'function':f.name,'line':f.lineno}
                      for f in traceback.extract_tb(current.__traceback__))
        current=current.__cause__
    return frames[-16:]


class WebSession(BrowserSession):
    def __init__(self, manager: "Workbench", job: dict[str, Any]):
        super().__init__(BrowserConfig(profile_dir=str(manager.root / ("profiles/boss" if job.get("mode") in BOSS_MODES else "profiles/default"))), PacingConfig())
        if job.get('mode') == 'automation_task':
            if job['task_snapshot']['config']['engine'] == 'boss_sourcing':
                self.cfg.profile_dir = str(manager.root / 'profiles/boss')
            else:
                self.cfg.profile_dir = str(manager.root / 'profiles/tasks' / job['task_id'])
        self.cfg.headless = os.environ.get('CLOAKUP_HEADLESS') == '1'
        if job.get('mode') in BOSS_MODES or job.get('task_snapshot',{}).get('config',{}).get('engine')=='boss_sourcing':
            self.cfg.extra_args.append('--restore-last-session')
        self.manager, self.job = manager, job
        self.instance_id = uuid.uuid4().hex

    def is_boss_task(self):
        return (self.job.get('mode') in BOSS_MODES or
                self.job.get('task_snapshot',{}).get('config',{}).get('engine')=='boss_sourcing')

    def _on_new_page(self,page):
        if self.is_boss_task():
            self._notes.append('BOSS 打开了新标签页，保留当前沟通页；登录检测会核验本任务全部标签页。')
            return
        super()._on_new_page(page)

    async def navigate(self, url):
        from urllib.parse import urlparse
        if self.is_boss_task() and urlparse(url).path.startswith('/web/chat/'):
            self._requested_boss_url=url
        return await super().navigate(url)

    async def observe(self, screenshot: bool = True, text_limit: int = 3500):
        # BOSS observations always stay local. Login screens are read without
        # screenshots and held before adapters can act on missing controls.
        local_screenshot=screenshot and self.is_boss_task() and self.job.get("mode")!="login"
        if self.is_boss_task(): screenshot=False
        obs = await super().observe(screenshot, text_limit)
        if self.is_boss_task() and self.job.get('mode') not in ('login','browser_open'):
            while boss_login_required(obs) or boss_verification_required(obs):
                resume_url=getattr(self,'_requested_boss_url','https://www.zhipin.com/web/chat/index')
                await self.manager.wait_for_access(self.job,self,'login' if boss_login_required(obs) else 'verification')
                # Auth redirects discard current selection and cached proofs.
                self._boss_greeting=None
                self._boss_recommend_list=None
                self._boss_recommend_detail=None
                self.last_obs=None
                self._observed_frames={}
                await super().navigate(resume_url)
                obs=await super().observe(False,text_limit)
        if self.job.get('mode')=='automation_task' and self.job['task_snapshot']['config']['engine']=='web':
            # Authentication screens never enter the model context. Keep the
            # visible browser open while the operator completes the exception.
            while (any(e.raw.get('type')=='password' for e in obs.elements)
                   or obs.raw.get('challenge')
                   or re.search(r'/(login|signin|passport)(?:[/?#]|$)',obs.url,re.I)):
                await self.manager.question(self.job,'reply','请在操作浏览器完成网站登录或验证码，然后在这里提交处理结果。登录画面不会发送给模型。')
                obs = await super().observe(False,text_limit)
        if local_screenshot:
            obs=await super().observe(True,text_limit)
        if obs.screenshot:
            self.manager.atomic_bytes(self.manager.directory(self.job) / "latest.jpg", obs.screenshot)
            self.job["screenshot_at"] = time.time()
        self.job["page"] = {"url": obs.url, "title": obs.title}
        self.manager.persist(self.job)
        return obs

    async def _act(self, kind, fn):
        takeover_before=self.job.get('takeover_epoch',0)
        was_manual=self.job.get('manual_takeover',False)
        await self.manager.checkpoint(self.job)
        if was_manual or takeover_before!=self.job.get('takeover_epoch',0):
            raise SourcingError('管理员接管使当前动作失效，未继续执行旧操作')
        if self.job.get('account_runner') and self.job.get('active_case_id') and not self.manager.account_execution.can_send(self.job['active_case_id']):
            raise SourcingError('岗位任务已暂停，取消后续页面操作')
        if not self.job.get('account_runner'):
            from .task_policy import window_status
            cfg=self.job.get('task_snapshot',{}).get('config',{})
            schedule=cfg.get('schedule')
            cid=self.job.get('params',{}).get('case_id')
            if not schedule and cid:
                schedule=self.manager.cases.load(cid).get('task_policy',{}).get('schedule')
            while schedule and not window_status(schedule)['allowed']:
                state=window_status(schedule)
                self.job.update(autopilot_stage='outside_window',next_check_at=state['next_at'])
                await asyncio.sleep(1)
                await self.manager.checkpoint(self.job)
            if self.job.get('autopilot_stage')=='outside_window':self.job.update(autopilot_stage='running',next_check_at=None)
        self.manager.event(self.job, "action", f"浏览器操作：{kind}")
        return await super()._act(kind, fn)


class TracedAgent(WebAgent):
    def __init__(self, manager, job, **kwargs):
        from .action_gateway import ActionLedger
        kwargs.setdefault('operation_ledger',ActionLedger(manager.db))
        kwargs.setdefault('operation_scope',{'task_id':job.get('task_id') or job['id'],'run_id':job['id'],'revision':job.get('task_snapshot',{}).get('revision',1)})
        super().__init__(**kwargs)
        self.manager, self.job = manager, job

    def _start_trace(self):
        self._trace = self.manager.directory(self.job) / "trace" / self.job.get("phase", "execution")
        self._trace.mkdir(parents=True, exist_ok=True)

    async def _call_model(self, messages):
        await self.manager.checkpoint(self.job)
        async def planning_ready():
            while any(r.get('task_id')==self.job.get('task_id') and r['status'] in ('queued','running','failed') for r in self.manager.db.list('planning_request')):
                self.job['status']='waiting';self.manager.persist(self.job)
                await asyncio.sleep(1);await self.manager.checkpoint(self.job)
            if self.job['status']=='waiting':self.job['status']='running';self.manager.persist(self.job)
        await planning_ready()
        records=[p for p in self.manager.task_plan_records() if p.get('task_id')==self.job.get('task_id') and not p.get('stale')]
        plan_id=records[0]['id'] if records else None
        if hasattr(self,'_active_plan_id') and self._active_plan_id!=plan_id and records:
            self.reference_plan=records[0]['plan']
        self._active_plan_id=plan_id;epoch=self.job.get('takeover_epoch',0);self._decision_epoch=epoch
        self.manager.event(self.job, "thinking", "Harness 正在理解任务与当前页面")
        response = await super()._call_model(messages)
        await self.manager.checkpoint(self.job)
        await planning_ready()
        latest=[p for p in self.manager.task_plan_records() if p.get('task_id')==self.job.get('task_id') and not p.get('stale')]
        response.invalidated=epoch!=self.job.get('takeover_epoch',0) or plan_id!=(latest[0]['id'] if latest else None)
        text = '\n'.join(b.text for b in response.content if b.type=='text')
        self.manager.db.append(self.job['id'], {'type':'model_response','phase':self.job.get('phase'),'at':time.time(),'text':text,'usage':dict(self.usage)})
        return response

    def _assert_operation_authorized(self):
        from .action_gateway import ActionBlocked
        if self.job.get('pause_requested') or self.job.get('manual_takeover') or self.job['status'] not in ('running','waiting') or self.job.get('takeover_epoch',0)!=getattr(self,'_decision_epoch',0):raise ActionBlocked('任务已暂停或操作权已变化')
        task_id=self.job.get('task_id')
        if task_id and self.job.get('task_snapshot') and self.manager.definitions.get(task_id)['revision']!=self.job['task_snapshot']['revision']:raise ActionBlocked('任务配置已变化')
        latest=next((p for p in self.manager.task_plan_records() if p.get('task_id')==task_id and not p.get('stale')),None)
        if hasattr(self,'_active_plan_id') and self._active_plan_id!=(latest['id'] if latest else None):raise ActionBlocked('任务规划已变化')

    def _record(self, step, event, obs=None):
        super()._record(step, event, obs)
        self.manager.db.append(self.job["id"], {"step":step,"phase":self.job.get("phase"),"at":time.time(),**event})
        if event.get('type')=='harness_decision':
            self.job.setdefault('harness_steps',[]).append({'at':time.time(),'tool':event['tool'],'reason':event['reason'],'harness_trace':event.get('harness_trace'),'phase':self.job.get('phase')})
            self.manager.event(self.job,'decision','Harness选择：'+event['tool']+' · '+event['reason'])
        if event.get('type') in ('action','receipt_unknown') and self.job.get('harness_steps'):
            self.job['harness_steps'][-1].update(result=event.get('result'),error=event.get('error',event['type']=='receipt_unknown'))
            self.manager.persist(self.job)
        if event.get('type')=='harness_plan':
            self.job['harness_reference_plan']=event['plan'];self.manager.persist(self.job)
        if event.get("type") == "action":
            self.manager.event(self.job, "error" if event.get("error") else "step",
                               event.get("result", event.get("tool", "")))


class TracedRunner(RecipeRunner):
    def __init__(self, manager, job, **kwargs):
        super().__init__(**kwargs)
        self.manager, self.job = manager, job

    async def _step(self, st, scope):
        await self.manager.checkpoint(self.job)
        self.manager.event(self.job, "step", f"{st['id']} · {st.get('intent', st['action'])}")
        return await super()._step(st, scope)


class Workbench:
    def __init__(self, root: Path):
        self.root = root.resolve()
        from .logbook import configure as configure_logbook
        configure_logbook(self.root)
        self._logged_stage = {}
        self.data = self.root / "runs/workbench"
        self.data.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.answers: dict[str, asyncio.Future] = {}
        self.access_sessions = {}
        self.access_locks = {}
        self.token = secrets.token_urlsafe(32)
        self.client: ModelClient | None = None
        self.boss_session: WebSession | None = None
        self.active_session = None
        self.execution_lock = asyncio.Lock()
        self.license_capacity = {'state':'checking','limit':None,'active':None,'checked_at':None}
        self.capacity_refresh = None
        self.monitor_heartbeat = time.time()
        self.service_instance_id = uuid.uuid4().hex
        self.db = Database(self.root)
        self.cases = CaseStore(self.root, self.db)
        self.account_execution = AccountExecution(self.db)
        self.attention = AttentionStore(self.db)
        self.definitions = TaskStore(self.db)
        self.definitions.import_cases(self.cases.list())
        from .planning_dialogue import PlanningDialogue
        self.planning=PlanningDialogue(self)
        from .agent_workspace import AgentWorkspace
        self.agent_workspace=AgentWorkspace(self)
        # 重启时隔离验收已被标为中断：排队中的岗位加入请求随即结算为失败（生成待办，可重新加入），不停在“验收中”等执行器安全点。
        try:
            from .job_changes import settle as settle_job_changes
            settle_job_changes(self)
        except Exception as exc:
            logging.getLogger("cloakup").warning("重启时结算岗位变更失败：%s",exc)
        # 重启前停在 running 的浏览器代办：标为中断失败（生成待办、可重试），不一直显示“绑定中”。
        try:
            from .browser_errands import settle as settle_errands
            settle_errands(self)
        except Exception as exc:
            logging.getLogger("cloakup").warning("重启时结算浏览器代办失败：%s",exc)
        from .scenario_workspace import ScenarioWorkspace
        self.scenario_workspace=ScenarioWorkspace(self)
        self.preflight_task = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        for p in sorted(self.data.glob("*/state.json")):
            try:
                job = json.loads(p.read_text())
                if not re.fullmatch(r"[a-f0-9]{32}", job["id"]):
                    continue
                try:
                    self.db.get('run', job['id'])
                except FileNotFoundError:
                    self.db.put('run', job['id'], job)
            except (OSError, ValueError, KeyError):
                logging.getLogger("cloakup").warning("跳过无法读取的工作台记录 %s", p.name)
        from .action_gateway import ActionLedger
        for job in self.db.list('run'):
            ActionLedger(self.db).recover(job['id'])
            try:
                if job["status"] in ACTIVE:
                    job.update(status="interrupted", error="服务已重启；该次运行中断，请重新运行。", interaction=None)
                    self.persist(job)
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                logging.getLogger("cloakup").warning("跳过无法读取的数据库记录")
        for report in self.db.list("readiness"):
            if report["status"] == "running":
                report.update(status="interrupted",finished_at=time.time())
                self.db.put("readiness",report["id"],report)
        # 服务重启时，原先在运行的账号任务已经没有执行器了：先如实标成暂停（原因 service_restart），
        # 再由 resume_after_restart 自动核验环境并恢复，不让任务显示“运行”却什么都不做。
        self.restart_resume=[]
        for member in self.account_execution.members():
            if member['status']=='running':
                self.restart_resume.append(member['id'])
                self.account_execution.set_task(member['id'],member['case_ids'],'paused',reason='service_restart')
        if os.environ.get('CLOAKUP_AUTO_RESUME','1')=='0':self.restart_resume=[]

        self.attention_monitor_future=asyncio.run_coroutine_threadsafe(self.monitor_attention(),self.loop)
        self.restart_resume_future=(asyncio.run_coroutine_threadsafe(self.resume_after_restart(),self.loop)
                                    if self.restart_resume else None)

    async def resume_after_restart(self,delay=None):
        """服务重启后恢复原先在运行的任务。

        环境指纹变了（例如更新了执行器代码）就先自动跑一次环境检查；通过后按管理员恢复的同一流程启动。
        任何一步失败，任务保持暂停并写明原因，不反复重试。期间管理员手动改过状态的任务不动。
        """
        from .pacing import jitter
        from .logbook import log
        ids=list(self.restart_resume)
        await asyncio.sleep(jitter(30) if delay is None else delay)
        log('state',type='auto_resume',stage='start',task_ids=ids)
        def still_waiting(tid):
            member=next((m for m in self.account_execution.members() if m['id']==tid),None)
            return member if member and member['status']=='paused' and (member.get('history') or [{}])[-1].get('reason')=='service_restart' else None
        try:
            if not any(still_waiting(tid) for tid in ids):return []
            if not (self.latest_readiness() or {}).get('executor_current'):
                await self.test_environment()
                while self.preflight_task and not self.preflight_task.done():await asyncio.sleep(2)
                if not (self.latest_readiness() or {}).get('executor_current'):
                    from .agent_workspace import StartBlocked
                    raise StartBlocked('服务重启后的环境检查未通过，请在执行环境 → 环境检查查看失败项','environment_check_failed')
            resumed=[]
            for tid in ids:
                if not still_waiting(tid):continue
                await self.start_account_task(tid,actor='system',reason='auto_resume_after_restart')
                resumed.append(tid)
            log('state',type='auto_resume',stage='done',task_ids=resumed)
            return resumed
        except Exception as exc:
            logging.getLogger('cloakup').warning('服务重启后自动恢复任务失败：%s',exc,exc_info=True)
            for tid in ids:
                member=still_waiting(tid)
                if member:self.account_execution.set_task(tid,member['case_ids'],'paused',reason='auto_resume_failed:'+getattr(exc,'code',type(exc).__name__))
            return []

    async def monitor_attention(self):
        while True:
            await asyncio.sleep(10)
            self.monitor_heartbeat=time.time()
            if time.time()-(self.license_capacity.get('checked_at') or 0)>60 and (self.capacity_refresh is None or self.capacity_refresh.done()):
                self.capacity_refresh=asyncio.create_task(self.refresh_capacity())
            for jid,task in self.tasks.items():
                if not task.done() and self.jobs[jid].get('account_runner'):
                    self.jobs[jid]['executor_heartbeat_at']=self.monitor_heartbeat
            try:
                self.attention.reconcile(list(self.jobs.values()),self.cases.list())
                from .task_notifications import tick
                await tick(self)
            except Exception:
                logging.getLogger('cloakup').exception('管理员待办同步失败')
            await self.ensure_daily_reports()

    async def ensure_daily_reports(self):
        """次日生成前一天的执行日报；停机错过的日期在恢复运行后补生成。只读本地记录。"""
        from .daily_report import CHECK_INTERVAL, ensure
        if time.time()-getattr(self,'_daily_report_checked_at',0)<CHECK_INTERVAL:return []
        self._daily_report_checked_at=time.time()
        try:
            made=await asyncio.to_thread(ensure,self)
        except Exception:
            logging.getLogger('cloakup').exception('执行日报生成失败')
            return []
        if made:logging.getLogger('cloakup').info('已生成执行日报：%s',', '.join(made))
        return made

    def call(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def directory(self, job):
        return self.data / job["id"]

    @staticmethod
    def atomic_bytes(path: Path, value: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(value)
        tmp.replace(path)

    def persist(self, job):
        self.db.put("run", job["id"], job)
        self.atomic_bytes(self.directory(job) / "state.json", json.dumps(job, ensure_ascii=False).encode())
        # 阶段切换写进 state 日志：空档时能看出执行器停在哪一步、下次什么时候检查。
        seen = getattr(self, '_logged_stage', None)
        if seen is not None:
            current = (job.get('status'), job.get('autopilot_stage'), job.get('active_case_id'))
            if seen.get(job['id']) != current:
                from .logbook import log
                before = seen.get(job['id'])
                seen[job['id']] = current
                log('state', type='executor_stage', run_id=job['id'], status=job.get('status'),
                    stage=job.get('autopilot_stage'), before_stage=before[1] if before else None,
                    case_id=job.get('active_case_id'), next_check_at=job.get('next_check_at'),
                    account_runner=bool(job.get('account_runner')))

    def event(self, job, kind, message):
        # The browser/model may echo injected credentials in error messages.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLOAKBROWSER_LICENSE_KEY"):
            value = os.environ.get(key)
            if value:
                message = str(message).replace(value, "[已隐藏]")
        job["events"].append({"at": time.time(), "kind": kind, "message": str(message)[:1200]})
        self.db.append(job["id"], {**job["events"][-1], "phase":job.get("phase")})
        from .logbook import log
        log('event', run_id=job['id'], kind=kind, message=str(message), status=job.get('status'), mode=job.get('mode'),
            stage=job.get('autopilot_stage'), case_id=job.get('active_case_id'), phase=job.get('phase'))
        job["events"] = job["events"][-250:]
        self.persist(job)

    def model_client(self):
        if self.client is None:
            self.client = ModelClient()
        return self.client

    def busy(self):
        return next((j for j in self.jobs.values() if j["status"] in ACTIVE), None)

    async def diagnostics(self,jid):
        from urllib.parse import urlparse
        task=self.tasks.get(jid)
        chain=[]
        current=task.get_coro() if task else None
        for _ in range(24):
            if current is None:break
            code=getattr(current,'cr_code',None) or getattr(current,'gi_code',None)
            frame=getattr(current,'cr_frame',None) or getattr(current,'gi_frame',None)
            if code:chain.append({'function':code.co_name,'file':Path(code.co_filename).name,'line':frame.f_lineno if frame else None})
            current=getattr(current,'cr_await',None) or getattr(current,'gi_yieldfrom',None)
        session=self.boss_session
        resume_controls=None;menu_probe=None
        if session and self.browser_alive(session):
            from .boss_resume import inspect_permissions
            try:resume_controls=await asyncio.wait_for(inspect_permissions(session),3)
            except Exception:pass
        if session and self.browser_alive(session):
            from . import boss_chat
            try:menu_probe=await asyncio.wait_for(boss_chat.evaluate(session,'''(()=>({row_nodes:document.querySelectorAll('.geek-item[data-id]').length,visible_rows:Array.from(document.querySelectorAll('.geek-item[data-id]')).filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0}).length,filters:Array.from(document.querySelectorAll('*')).filter(e=>!e.children.length&&(e.innerText||'').trim()==='全部').slice(0,12).map(e=>({tag:e.tagName,cls:e.className,parent:e.parentElement?.className,grand:e.parentElement?.parentElement?.className,labels:(e.parentElement?.innerText||'').slice(0,80)}))}))()'''),3)
            except Exception:pass
        return {'menu_probe':menu_probe,'resume_controls':resume_controls,'run_id':jid,'status':self.jobs[jid]['status'],'phase':self.jobs[jid].get('phase'),
                'await_chain':chain,'browser_alive':self.browser_alive(session),
                'page_paths':[urlparse(p.url).path for p in session.context.pages] if session and session.context else []}

    def recipes(self):
        items = []
        for p in sorted((self.root / "recipes").glob("*.y*ml")):
            try:
                r = Recipe.load(p)
                items.append({"file": p.name, **r.to_dict()})
            except (OSError, ValueError, TypeError, KeyError, RecipeError):
                continue
        return items

    def recipe(self, filename):
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith((".yaml", ".yml")):
            raise ValueError("流程文件名不合法")
        return Recipe.load(self.root / "recipes" / filename)

    def execution_metrics(self,cases=None):
        events=[a for c in (cases if cases is not None else self.cases.list()) for a in c.get('audit',[])]
        traces=[a['details']['harness_trace'] for a in events if a['details'].get('harness_trace')]
        traces += [s['harness_trace'] for r in self.jobs.values() for s in r.get('harness_steps',[]) if s.get('harness_trace')]
        traces=list({t['session_id']:t for t in traces if t.get('session_id')}.values())
        return {'program_decisions':sum(a['kind']=='program_decision' for a in events),'model_decisions':len(traces),'gateway_requests':sum(t.get('gateway_requests',0) for t in traces),'runtime_reuses':sum(bool(t.get('runtime_reused')) for t in traces),'input_tokens':sum(t.get('input_tokens',0) for t in traces),'output_tokens':sum(t.get('output_tokens',0) for t in traces),'scope':'执行决策记录；不含规划与环境测试，历史未记录用量不计入'}

    def runtime_status(self):
        from .selection_recovery import public_records
        selection_recovery=public_records(self.jobs.values())
        blocked_selection=any(r['status']=='blocked' for r in selection_recovery)
        current=self.busy()
        recent=max((r for r in self.jobs.values() if r.get('mode') not in ('login','browser_open')),key=lambda r:r.get('created',0),default=None)
        session=self.active_session if self.browser_alive(self.active_session) else self.boss_session
        alive=self.browser_alive(session)
        stopped=not current and recent and recent.get('status') in ('failed','interrupted')
        label=('任务受阻 · 等待管理员' if blocked_selection and (not current or current.get('autopilot_stage') in ('standby','selection_blocked')) else '执行器待机 · 所有任务已暂停或结束' if current and current.get('account_runner') and not any(m['status']=='running' for m in self.account_execution.members()) and not current.get('manual_takeover') else '等待管理员处理' if current and current['status']=='waiting' else
               '任务已暂停' if current and current['status'] in ('paused','pausing') else
               '非执行时段 · 保留会话' if current and current.get('autopilot_stage')=='outside_window' else
               '休息中 · 保留会话' if current and current.get('autopilot_stage')=='resting' else
               '发布版本已变化 · 需暂停并恢复任务' if current and current.get('autopilot_stage')=='release_changed' else
               '等待规划或策略更新' if current and current.get('autopilot_stage')=='waiting_policy' else
               '页面读取待重试' if current and current.get('autopilot_stage')=='read_retry' else
               '会话选择自动恢复中' if current and current.get('autopilot_stage')=='selection_retry' else
               '等待下一轮检查' if current and current.get('autopilot_stage')=='waiting_next_cycle' else
               '任务执行中' if current else '任务异常停止' if stopped else '暂无运行任务')
        last=current.get('events',[])[-1]['at'] if current and current.get('events') else None
        return {'checked_at':time.time(),'application':'online','database':'readable',
                'service_instance_id':self.service_instance_id,'browser_instance_id':getattr(session,'instance_id',None) if alive else None,
                'executor_heartbeat_at':current.get('executor_heartbeat_at') if current else None,
                'monitor_heartbeat':self.monitor_heartbeat,'task_state':label,
                'task_id':current['id'] if current else recent['id'] if recent else None,
                'task_status':current['status'] if current else recent.get('status') if recent else None,
                'task_error':recent.get('error') if stopped else current.get('error') if current else None,
                'browser_alive':alive,'browser_url':'http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale',
                'last_event_at':last,'next_check_at':current.get('next_check_at') if current else None,
                'selection_recovery':selection_recovery,
                'needs_attention':bool(blocked_selection or stopped or current and (current['status']=='waiting' or current.get('autopilot_stage')=='release_changed')),
                'slow_progress':bool(current and current['status']=='running' and label not in ('等待下一轮检查','非执行时段 · 保留会话','休息中 · 保留会话','等待规划或策略更新','页面读取待重试','会话选择自动恢复中','任务受阻 · 等待管理员') and last and time.time()-last>180)}

    async def refresh_capacity(self):
        from .capacity import license_capacity
        try:self.license_capacity=await asyncio.to_thread(license_capacity)
        except Exception:self.license_capacity={'state':'unreachable','limit':None,'active':None,'checked_at':time.time()}
        return self.capacity_status()

    def capacity_status(self):
        sessions={id(s):s for s in (self.active_session,self.boss_session) if self.browser_alive(s)}
        executing=sum(1 for s in sessions.values() if s is self.active_session and s.job.get('status') in ('running','waiting') and not s.job.get('manual_takeover') and not (s.job.get('account_runner') and not self.account_execution.case_ids()))
        return {'license':copy.deepcopy(self.license_capacity),'local_running':executing,'local_retained':len(sessions)-executing,
                'local_instances':len(sessions),'queued':sum(j['status']=='queued' for j in self.jobs.values()),
                'can_release_boss':self.browser_alive(self.boss_session) and not any(j.get('release_requested') and j['status'] in ACTIVE for j in self.jobs.values()) and not self.account_execution.case_ids() and not (self.busy() or {}).get('manual_takeover'),
                'policy':'paused_account_yields_to_queue'}

    async def release_browser_seat(self):
        lock=getattr(self,'_browser_diagnostic_lock',None)
        if lock and lock.locked():raise ValueError('执行器核验正在使用浏览器，请等待完成后释放席位')
        if self.account_execution.case_ids():raise ValueError('请先暂停该账号的所有任务，再释放浏览器席位')
        if any(m['status']=='sending' for c in self.cases.list() for p in c['candidates'].values() for m in p.get('outbox',[])):
            raise ValueError('仍有消息发送中，请等待回执核验后释放')
        owner=next((j for j in self.jobs.values() if j.get('account_runner') and j['status'] in ACTIVE),None)
        if owner:
            if owner.get('manual_takeover'):raise ValueError('管理员接管中，请先交还自动化')
            owner.update(release_requested=True,status='cancelling')
            self.event(owner,'operator','请求释放浏览器席位，保留资料及任务暂停状态')
            self.tasks[owner['id']].cancel()
            return {'status':'releasing'}
        session=self.boss_session
        if self.browser_alive(session):
            if self.active_session is session:raise ValueError('浏览器正在执行其他操作，请先暂停或停止')
            await session.close();self.boss_session=None
        return {'status':'released'}

    async def snapshot(self):
        from .prompts import prompt_defaults
        from .action_gateway import ActionLedger
        from importlib.metadata import version,PackageNotFoundError
        try:harness_version=version('deepseek-harness-sdk')
        except PackageNotFoundError:harness_version=None
        cases=self.cases.list();locks=locked_tasks(self.db,[c['id'] for c in cases])
        cases=[{**c,"editable_config":view_config(c),"config_locked_by":locks.get(c['id'])} for c in cases]
        todos=self.attention.reconcile(list(self.jobs.values()),cases)
        executor_signature=environment_signature();browser_instance=getattr(self.boss_session,'instance_id',None) if self.browser_alive(self.boss_session) else None
        executor_checks=[{**r,'current':bool(browser_instance and r.get('browser_instance_id')==browser_instance and r.get('signature')==executor_signature)} for r in self.db.list('executor_check')[:20]]
        ready = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        return copy.deepcopy({"greeting_readiness":self.db.list("greeting_readiness"),"scenario_workspace":self.scenario_workspace.catalog(),"agent_workspace":self.agent_workspace.catalog(),"notifications":__import__("cloakup.task_notifications",fromlist=["status"]).status(self),"prompt_defaults":prompt_defaults(),"planning_runtime":{"name":"DeepSeek Harness","version":harness_version,"enabled":bool(harness_version),"profile":"sdk-minimal","scope":"全场景任务规划与持续决策","tools":"disabled"},"planning_dialogues":self.db.list("planning_dialogue"),"planning_requests":self.db.list("planning_request"),"execution_metrics":self.execution_metrics(cases),"followup_policy":self.followup_policy(),"actions":ActionLedger(self.db).list(),"task_plans":self.task_plan_records(),"answer_policy":{"version":ANSWER_POLICY_VERSION,"order":ANSWER_ORDER},"browser_capacity":self.capacity_status(),"account_execution":self.account_execution.view(next((r for r in self.jobs.values() if r.get("account_runner") and r["status"] in ACTIVE),None),self.browser_alive(self.boss_session)),"runtime_status":self.runtime_status(),"attention":todos,"sourcing_overview":sourcing_overview(cases,list(self.jobs.values())),"job_overview":self.job_overview(cases),"today":self.today_view(cases),"token": self.token, "model": default_model(), "model_ready": ready,
            "ui_version": str((ASSETS / "app.js").stat().st_mtime_ns),
            "definitions": self.definitions.list(), "readiness": self.latest_readiness(), "executor_checks": executor_checks, "database": self.db.backend, "runtime": "docker" if Path("/.dockerenv").exists() else "local",
            "capabilities": {"boss_first_contact": True, "boss_recommend": True, "boss_autopilot": True},
            "cases": [{**c,"editable_config":view_config(c),"anonymous_draft":draft_anonymous_job(c),"config_locked_by":locks.get(c['id']),"jd_review":jd_review(c)} for c in self.cases.list()], "recipes": self.recipes(), "runs": [{"created":0,"params":{},"mode":"login","task":"历史浏览器恢复记录",**j,"can_recover_login":can_recover_login(j)} for j in sorted(self.jobs.values(), key=lambda j: j.get("created",0), reverse=True)],
            "busy": self.busy()["id"] if self.busy() else None})

    def today_view(self,cases):
        """首页“今天”的只读视图；cases 须是完整、未过滤的岗位列表（账号已用额度由它算）。算不出来只给兜底文字，不拖垮整个状态。"""
        from . import today
        try:return today.build(self.db,self.cases,cases,self.account_execution.members(),self.account_execution.availability)
        except Exception:
            logging.getLogger('cloakup.web').exception('today view failed')
            return {'error':'今天的数据暂时算不出来'}

    def job_overview(self,cases):
        """寻访任务岗位区的只读视图；单个任务算不出来只给该任务带上原因，不影响整个状态。"""
        from . import job_changes
        from .scenarios import is_sourcing
        views={};context=None
        for task in self.definitions.list():
            if not is_sourcing(task['config'].get('engine')):continue
            try:
                context=context or job_changes.overview_context(self.db,cases)
                views[task['id']]=job_changes.overview(self.db,task,cases,context)
            except Exception as exc:views[task['id']]={'jobs':[],'changes':[],'error':'岗位视图暂时无法读取：'+(str(exc)[:200] or type(exc).__name__)}
        return views

    @staticmethod
    def browser_alive(session):
        try:
            return bool(session and session.context is not None and (not session.page.is_closed() or any(not p.is_closed() for p in getattr(session.context,'pages',[]))))
        except (AttributeError, RuntimeError):
            return False

    def latest_readiness(self):
        reports = self.db.list('readiness')
        if not reports: return None
        report = reports[0]
        report['fresh'] = report['status']=='passed' and time.time()-report.get('finished_at',0)<600 and report.get('signature')==environment_signature()
        report['executor_current']=report['status']=='passed' and report.get('executor',{}).get('status')=='passed' and report.get('executor',{}).get('contract')=='executor-v1' and report.get('signature')==environment_signature()
        report.pop('signature',None)
        return report

    async def test_environment(self, body=None):
        if self.preflight_task and not self.preflight_task.done():
            return {'id':self.db.list('readiness')[0]['id']}
        owner=self.busy()
        if owner and (not owner.get('account_runner') or owner.get('manual_takeover') or owner.get('active_case_id') or any(m['status']=='running' for m in self.account_execution.members())):
            raise ValueError('请先暂停全部账号任务并等待安全点，再测试环境')
        report={'id':uuid.uuid4().hex,'status':'running','started_at':time.time(),'signature':environment_signature(),'checks':[]}
        reuse={}
        if (body or {}).get('retry_failed') is True:
            from .platform import reusable_preflight
            previous=next(iter(self.db.list('readiness')),None)
            reuse=reusable_preflight(previous,report['signature'],report['started_at'])
            if 'executor' in reuse:
                report['executor']=copy.deepcopy(previous['executor'])
        self.db.put('readiness',report['id'],report)
        self.preflight_task=asyncio.create_task(preflight(self,report,reuse=reuse))
        return {'id':report['id']}

    def phase(self, job, phase, message):
        job['phase']=phase
        job.setdefault('phases',[]).append({'phase':phase,'at':time.time(),'message':message})
        self.event(job,'phase',message)

    def record_plan(self,job,plan,origin):
        job['plan']=copy.deepcopy(plan)
        self.db.put('plan',job['id'],{'id':job['id'],'task_id':job['task_id'],'task_revision':job['task_snapshot']['revision'],'origin':origin,'plan':plan,'at':time.time()},history=True)
        self.event(job,'plan','探索计划已持久化')

    async def save_definition(self,body,key=None):
        if key and any(m['id']==key and m['status']=='running' for m in self.account_execution.members()):raise ValueError('请先暂停该账号任务再修改配置')
        if key and self.busy() and self.busy().get('task_id')==key: raise ValueError('任务正在执行，请先停止后再修改配置')
        from .scenarios import is_sourcing
        if is_sourcing(body.get('config',{}).get('engine')):
            for cid in body['config'].get('case_ids',[]): self.cases.load(cid)
        result=self.definitions.save(body,key)
        if is_sourcing(result['config']['engine']):self.agent_workspace.bootstrap(result['id'])
        return result

    def task_plan_records(self):
        from .task_planning import planning_signature
        records=[]
        # 同一次调用内按任务复用当前签名：一个任务可有数十条历史计划，
        # 逐条重新加载大体积岗位文档会长时间占用事件循环，阻塞浏览器操作。
        signatures={}
        for p in self.db.list('plan'):
            item=copy.deepcopy(p)
            try:
                key=p['task_id']
                if key not in signatures:
                    definition=self.planning.definition(key)
                    cases=[self.cases.load(cid) for cid in definition['config'].get('case_ids',[])]
                    signatures[key]=planning_signature(definition,cases)
                item['stale']=p.get('signature')!=signatures[key]
            except (KeyError,FileNotFoundError):item['stale']=True
            records.append(item)
        return records

    async def generate_definition_plan(self,key,conversation=False):
        releases=[r for r in self.db.list('agent_release') if r['id']==key]
        if releases:
            records=[p for p in self.task_plan_records() if p.get('task_id')==key and not p.get('stale')]
            if records:return records[0]
            raise ValueError('该任务的计划须在规划端 Agent 草案中生成、验证后发布')
        from .task_planning import generate_boss_plan,planning_signature
        from .harness_runtime import get_harness
        if not conversation and any(r['task_id']==key and r['status'] in ('queued','running','failed') for r in self.db.list('planning_request')):raise ValueError('请先完成规划对话中的请求，失败时重试原请求')
        definition=self.planning.definition(key)
        cases=[self.cases.load(cid) for cid in definition['config'].get('case_ids',[])]
        prior=next((p for p in self.db.list('plan') if p.get('task_id')==key and p.get('origin')=='llm'),None)
        if prior:definition['previous_plan']=prior['plan']
        record=await asyncio.wait_for(generate_boss_plan(get_harness(LLM(self.model_client()),self.root,"task-planning"),definition,cases,conversation=conversation),timeout=150)
        latest=self.planning.definition(key)
        if record['signature']!=planning_signature(latest,[self.cases.load(cid) for cid in latest['config'].get('case_ids',[])]):
            raise ValueError('生成期间配置变化，本次计划未采用，请重新生成')
        self.db.put('plan',record['id'],record,history=True)
        for cid in latest['config'].get('case_ids',[]):
            case=self.cases.load(cid);changed=False
            for person in case['candidates'].values():
                if person.get('decision_hold') or person.get('decision_wait_until'):
                    if person.get('decision_hold'):person['decision_hold']['retry_at']=0
                    person['decision_wait_until']=0;person['harness_wait_count']=0
                    person.pop('harness_decision',None);changed=True
            if changed:self.cases.audit(case,'harness_plan_updated',plan_id=record['id'],reason='新计划已生成，下轮重新观察；不启动暂停任务或重发未知消息')
        return record

    def verify_account_plan(self,task_id,case_id,run_id):
        for p in self.task_plan_records():
            if p['task_id']!=task_id or p.get('stale'):continue
            if case_id in p.get('observed_case_ids',[]):return
            p.pop('stale',None)
            p.setdefault('observed_case_ids',[]).append(case_id)
            p.update(verification='menu_observed',verification_run_id=run_id,verified_at=time.time())
            self.db.put('plan',p['id'],p,history=True);return

    def inherit_selection_recovery(self, owner, case_ids):
        records={}
        for prior in self.jobs.values():
            for cid,record in prior.get('selection_recovery',{}).items():
                if cid in case_ids and record.get('updated_at',0)>=records.get(cid,{}).get('updated_at',-1):records[cid]=copy.deepcopy(record)
        owner.setdefault('selection_recovery',{}).update(records)

    def resume_blocker(self, task_id):
        """启动／恢复该任务会被哪条前置检查拒绝（发布验收、环境核验、简历目标、岗位绑定、岗位冲突）；能启动时返回 None。
        系统自动恢复前先查：这些问题刷新页面修不好，就不去碰页面。"""
        try:Workbench._check_startable(self,task_id)
        except Exception as exc:return str(exc) or type(exc).__name__  # 任何意外都当作“不能自动恢复”，交给管理员
        return None

    def _check_startable(self, task_id):
        definition=self.definitions.get(task_id);cfg=definition['config']
        from .agent_workspace import assert_release_ready
        assert_release_ready(self,task_id)
        if cfg['engine']!='boss_sourcing':raise ValueError('该任务不是账号寻访任务')
        try:self.db.get('agent_release',task_id);governed=True
        except FileNotFoundError:governed=False
        if governed and not (self.latest_readiness() or {}).get('executor_current'):
            raise ValueError('当前环境与执行器尚未通过核验，请先在执行环境 → 环境检查运行快速测试；模型验收不能代替浏览器执行核验')
        ids=cfg['case_ids'];selected=[self.cases.load(cid) for cid in ids]
        received=sum(bool(p.get('resumes')) for c in selected for p in c['candidates'].values() if not p.get('alias_of'))
        if cfg.get('sourcing_mode','continuous')=='quota' and received>=cfg.get('target_resumes',1):raise ValueError(f"任务已达到简历目标（已收到 {received} 位，目标 {cfg.get('target_resumes',1)} 位），请先调整目标人数后再启动")
        if any(not c.get('autonomy',{}).get('enabled') or not c['job'].get('snapshot') for c in selected):
            raise ValueError('请先绑定岗位并启用自动化')
        old=[m for m in self.account_execution.members() if m['id']!=task_id and m['status']!='stopped']
        if any(set(m['case_ids']) & set(ids) for m in old):raise ValueError('岗位已由另一个账号任务管理，请在原任务继续，避免重复调度')
        all_ids=list(dict.fromkeys([cid for m in old for cid in m['case_ids']]+ids))
        names=[self.cases.load(cid)['job']['label'] for cid in all_ids]
        if len(names)!=len(set(names)):raise ValueError('同名岗位不能可靠分流，请先区分名称')
        return ids,all_ids

    async def start_account_task(self, task_id, *, actor='admin', reason='start_or_resume'):
        if self.preflight_task and not self.preflight_task.done():raise ValueError('环境与执行器核验进行中，请等待完成后启动任务')
        ids,all_ids=Workbench._check_startable(self,task_id)
        current=self.busy()
        if current and not current.get('account_runner'):raise ValueError('执行环境正被其他操作占用，请待当前操作完成')
        if current:
            self.inherit_selection_recovery(current,all_ids)
            current['params']['new_contacts']=True;self.persist(current)
            self.account_execution.set_task(task_id,ids,'running',actor=actor,reason=reason)
            return {'id':current['id'],'environment_id':'boss:default'}
        # A single owner runs all memberships, never one loop per task.
        result=await self.create({'mode':'boss_autopilot','params':{'case_ids':all_ids,'continuous':True,'new_contacts':True}})
        self.account_execution.set_task(task_id,ids,'running',actor=actor,reason=reason)
        owner=self.jobs[result['id']];owner['account_runner']=True;owner['task']='招聘账号执行器 · 统一收件与岗位调度'
        # Retain recovery budgets across service/run restarts. Starting a task
        # does not prove the previously blocked page is safe or working again.
        self.inherit_selection_recovery(owner,all_ids)
        self.persist(owner);return {**result,'environment_id':'boss:default'}

    async def account_task_action(self,task_id,body):
        action=body.get('action')
        member=next((m for m in self.account_execution.members() if m['id']==task_id),None)
        if action=='keep_session':
            if self.preflight_task and not self.preflight_task.done():raise ValueError('环境与执行器核验进行中，请等待完成')
            if not member:raise ValueError('尚未绑定账号任务')
            current=self.busy()
            if current:
                if not current.get('account_runner'):raise ValueError('浏览器被其他任务占用')
                return {'id':current['id'],'environment_id':'boss:default'}
            ids=list(dict.fromkeys(cid for m in self.account_execution.members() if m['status']!='stopped' for cid in m['case_ids']))
            if not ids:raise ValueError('没有需要保留会话的任务')
            result=await self.create({'mode':'boss_autopilot','params':{'case_ids':ids,'continuous':True,'new_contacts':False}})
            owner=self.jobs[result['id']];owner.update(account_runner=True,task='招聘账号执行器 · 会话待机')
            self.inherit_selection_recovery(owner,ids)
            self.persist(owner);return {**result,'environment_id':'boss:default'}
        if action == 'allow_outreach_with_isolation':
            cid, chat_id = body.get('case_id'), body.get('chat_id')
            if not member or member['status']!='paused' or cid not in member['case_ids']:
                raise ValueError('只允许在暂停的关联岗位中设置开聊例外')
            case=self.cases.load(cid)
            if not chat_id or chat_id not in case.get('deferred_chat_ids',[]):
                raise ValueError('目标会话不在隔离列表中')
            approved=case.setdefault('outreach_isolation_exceptions',[])
            if chat_id not in approved:
                approved.append(chat_id)
                self.cases.audit(case,'outreach_isolation_exception',chat_id=chat_id,
                                 reason='管理员允许该岗位新增开聊，原会话保持隔离')
            return {'status':'allowed'}
        if action == 'defer_failed_chat':
            if not member or member['status']!='paused':
                raise ValueError('请先暂停任务再隔离原失败会话')
            matches=[]
            for cid in member['case_ids']:
                latest=max((r for r in self.jobs.values() if cid in r.get('selection_recovery',{})),
                           key=lambda r:r['selection_recovery'][cid]['updated_at'],default=None)
                if latest:
                    record=latest['selection_recovery'][cid]
                    if record['status']=='blocked':matches.append((cid,latest,record))
            if len(matches)!=1:raise ValueError('无法唯一确定当前阻塞事件')
            cid,run,record=matches[0]
            chat_id=record.get('last_failed_chat_id')
            if record.get('failures')!=[chat_id] or not re.fullmatch(r'[1-9]\d*-\d+',chat_id or '') or record.get('diagnostics',{}).get('reason') not in ('header_name_unverified','header_identity_unverified','identity_ambiguous'):
                raise ValueError('当前异常缺少单一明确的失败会话，不能隔离')
            case=self.cases.load(cid)
            case['deferred_chat_ids']=list(dict.fromkeys([*case.get('deferred_chat_ids',[]),chat_id]))
            case.setdefault('deferred_chat_since',{}).setdefault(chat_id,time.time())
            self.cases.audit(case,'failed_chat_deferred',reason='隔离未核验会话，暂停该岗位新增开聊')
            record.update(status='deferred',updated_at=time.time(),reason='原失败会话已隔离，其他会话仍需逐条核验')
            self.persist(run)
            return {'status':'deferred'}
        if action in ('start','resume'):return await self.start_account_task(task_id)
        if action not in ('pause','stop') or not member:raise ValueError('账号任务操作不合法')
        return self.account_execution.set_task(task_id,member['case_ids'],'paused' if action=='pause' else 'stopped',
                                               actor='admin',reason=str(body.get('reason') or 'manual')[:200])

    async def create(self, body):
        if body.get('mode')=='automation_task' and body.get('params',{}).get('account_managed') is True and not body.get('params',{}).get('explore_only'):
            definition=self.definitions.get(body.get('task_id'))
            from .scenarios import is_sourcing, scenario_for_engine
            if is_sourcing(definition['config']['engine']):
                if scenario_for_engine(definition['config']['engine'])['page_adapter']!='boss':
                    raise ValueError('该寻访场景的页面动作尚未接入，任务与岗位已按场景保存')
                return await self.start_account_task(definition['id'])
        if self.busy() and not body.get("allow_queue",False):
            raise ValueError("已有任务在运行，请先完成或停止该任务")
        if body.get("mode") not in ("browser_open", "login", "boss_contact", "boss_recommend", "boss_autopilot", "boss_errand") and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise ValueError("尚未配置模型凭据，请在启动终端设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN 后重启工作台")
        mode = body.get("mode", "explore")
        if mode not in ("explore", "replay", "automation_task", *BOSS_MODES):
            raise ValueError("未知执行方式")
        params = body.get("params", {} if mode in ("login","browser_open") else None)
        if not isinstance(params, dict):
            raise ValueError("参数必须为对象")
        recipe = None
        if mode == "automation_task":
            definition = self.definitions.get(body.get('task_id'))
            params = {'explore_only': params.get('explore_only',False)}
            if type(params['explore_only']) is not bool: raise ValueError('探索选项必须为布尔值')
            from .scenarios import is_sourcing, scenario_for_engine
            if is_sourcing(definition['config']['engine']) and scenario_for_engine(definition['config']['engine'])['page_adapter']!='boss':
                raise ValueError('该寻访场景的页面动作尚未接入，任务与岗位已按场景保存')
            if definition['config']['engine']=='boss_sourcing':
                selected=[self.cases.load(cid) for cid in definition['config']['case_ids']]
                if len({c['job']['label'] for c in selected})!=len(selected): raise ValueError('同名岗位无法可靠分流，请先区分岗位名称')
            task = definition['config']['goal']
            if self.preflight_task and not self.preflight_task.done(): raise ValueError('环境测试进行中，请等待结果')
        elif mode == "explore":
            category, count = params.get("category"), params.get("max_items")
            if category not in ("Poetry", "Travel", "Mystery") or type(count) is not int or not 1 <= count <= 5:
                raise ValueError("请选择有效分类与数量（1–5 本）")
            task = body.get("task", BOOK_TASK)
            if not isinstance(task, str) or not task.strip() or len(task) > 3000:
                raise ValueError("请输入任务描述（最多 3000 字）")
        elif mode == "boss_autopilot":
            ids=params.get('case_ids',[params.get('case_id','')])
            if not isinstance(ids,list) or not 1<=len(ids)<=20 or len(set(ids))!=len(ids) or any(not isinstance(i,str) for i in ids):
                raise ValueError('请选择1至20个不重复的岗位')
            selected=[self.cases.load(i) for i in ids]
            case=selected[0]
            if len({c['job']['label'] for c in selected})!=len(selected):
                raise ValueError('同名岗位无法从可见会话可靠分流，请先区分岗位名称')
            if any(not c['job']['snapshot'] for c in selected):
                raise ValueError('请先绑定正式岗位')
            for key in ('read_only','continuous','new_contacts'):
                if key in params and type(params[key]) is not bool:
                    raise ValueError('自动沟通参数必须为布尔值')
            params = {'case_id':case['id'], 'read_only':params.get('read_only',False),
                      'continuous':params.get('continuous',True),'new_contacts':params.get('new_contacts',True)}
            if len(ids)>1:params['case_ids']=ids
            if params['read_only']:
                params.update(continuous=False,new_contacts=False)
            if not params['read_only'] and any(not c.get('autonomy',{}).get('enabled') for c in selected):
                raise ValueError('请先启用本岗位自动沟通')
            task = '从沟通菜单同步历史，自动首聊、跟进与索要简历；完整记录判断和回执'
        elif mode == "boss_recommend":
            case = self.cases.load(params.get("case_id", ""))
            if not case["job"]["snapshot"]:
                raise ValueError("请先绑定正式岗位")
            review = params.get("review", False)
            if type(review) is not bool:
                raise ValueError("审阅方式不合法")
            params = {"case_id": case["id"], "review": review}
            task = "读取普通推荐并逐条审阅首聊" if review else "按本岗位条件读取普通推荐；不联系候选人、不调用模型"
        elif mode == "boss_contact":
            case = self.cases.load(params.get("case_id", ""))
            candidate = self.cases.candidate(case, params.get("candidate_id"))
            message = self.cases.preview(case, candidate["id"], params.get("message_id"))
            if message["status"] not in ("draft", "approved"):
                raise ValueError("草稿已失效或已处理，请重新查看")
            if message["intent"] != "first_contact":
                raise ValueError("首次联系入口只接受首聊草稿")
            valid_message(message["text"])
            params = {"case_id": case["id"], "candidate_id": candidate["id"], "message_id": message["id"]}
            task = "核对当前岗位和目标候选人，顾问确认后仅发送一条消息"
        elif mode == "boss_bind":
            params = {"job_url": validate_job_url(params.get("job_url", ""))}
            task = "读取已建 BOSS 岗位并生成寻访计划与开聊草稿"
        elif mode == "boss_errand":
            # 浏览器代办（browser_errands）：读取 BOSS 岗位列表、绑定岗位；参数在代办记录里，绑定前已核对凭据。
            params = {}
            task = "执行排队的 BOSS 浏览器代办：读取已发布岗位、绑定所选岗位"
        elif mode == "browser_open":
            params = {}
            task = "打开并保留执行浏览器，不启动候选人沟通"
        elif mode == "login":
            params = {}
            task = "在独立浏览器中登录 BOSS 直聘，并打开拟试跑的已建岗位"
        else:
            recipe = self.recipe(body.get("recipe"))
            params = recipe.validate_params(params)
            task = recipe.goal
        job = {"id": uuid.uuid4().hex, "created": time.time(), "status": "queued", "mode": mode,
               "preserve_preflight": mode in ("boss_contact", "boss_recommend"),
               "params": params, "task": task, "recipe": body.get("recipe"), "events": [],
               "result": None, "error": None, "interaction": None, "page": None,
               "screenshot_at": None, "draft": None, "saved_recipe": None, "pause_requested": False}
        if mode == 'automation_task':
            if body.get('resume_from'):
                previous=self.jobs.get(body['resume_from'])
                if not previous or previous.get('task_id')!=definition['id'] or previous['status'] in ACTIVE: raise ValueError('续接来源任务不匹配或仍在运行')
                job['resume_from']=previous['id']
            job.update(task_id=definition['id'],task_snapshot=copy.deepcopy(definition))
            if definition['config']['engine']=='boss_sourcing':
                job['case_config_snapshots']={cid:view_config(self.cases.load(cid)) for cid in definition['config']['case_ids']}
        boss_environment=mode in BOSS_MODES or mode=='automation_task' and definition['config']['engine']=='boss_sourcing'
        job['environment_id']='boss:default' if boss_environment else 'task:'+str(job.get('task_id',job['id']))
        job['environment_label']='BOSS · 当前招聘账号' if boss_environment else '独立网站任务环境'
        self.jobs[job["id"]] = job
        self.persist(job)
        self.gates[job["id"]] = asyncio.Event()
        self.gates[job["id"]].set()
        self.tasks[job["id"]] = asyncio.create_task(self.execute(job, recipe))
        return {"id": job["id"]}

    async def checkpoint(self, job):
        if job.get("pause_requested"):
            job["status"] = "paused"
            self.persist(job)
            await self.gates[job["id"]].wait()
        if job["status"] not in ("waiting", "compiling"):
            job["status"] = "running"

    async def question(self, job, kind, prompt):
        job["status"] = "waiting"
        interaction = {"id": uuid.uuid4().hex, "kind": kind, "prompt": prompt}
        job["interaction"] = interaction
        future = self.loop.create_future()
        self.answers[job["id"]] = future
        self.event(job, "human", prompt)
        try:
            return await future
        finally:
            self.answers.pop(job["id"], None)
            job["interaction"] = None
            if job["status"] == "waiting":
                job["status"] = "running"
            self.persist(job)

    async def start_browser(self,job,session):
        deadline=time.monotonic()+270
        warned=False
        license_retries=0
        try:
            while True:
                try:
                    await session.start()
                    if warned: self.event(job,'browser_ready','浏览器名额已释放，继续核验环境和登录状态')
                    return
                except Exception as exc:
                    if type(exc).__name__!='CloakBrowserLicenseError':raise
                    if 'session limit' not in str(exc).lower():
                        # 容器刚重建时授权校验可能瞬间失败，许可证本身有效、名额也空着。
                        # 有限次随机退避重试；仍失败再按原样报错，不无限重试。
                        if license_retries>=BROWSER_LICENSE_RETRIES:raise
                        license_retries+=1
                        await session.close()
                        self.event(job,'step',f'浏览器授权校验暂未通过，稍后重试（{license_retries}/{BROWSER_LICENSE_RETRIES}）')
                        await asyncio.sleep(jitter(8))
                        continue
                    await session.close()
                    if time.monotonic()>=deadline:
                        job['failure_kind']='browser_capacity'
                        raise ValueError('浏览器会话名额仍被占用；请关闭其他设备或环境中的 CloakBrowser 后恢复任务') from None
                    job.update(status='waiting',interaction={'kind':'browser_capacity','id':uuid.uuid4().hex,
                        'prompt':'浏览器许可名额已占用，正在等待释放。若其他设备或环境正在使用 CloakBrowser，请先关闭其中的浏览器。当前任务尚未开始业务操作。'})
                    job['capacity_wait']={'since':job.get('capacity_wait',{}).get('since',time.time()),'next_retry_at':time.time()+15}
                    if not warned:
                        self.event(job,'browser_capacity','等待浏览器会话名额释放；保留当前任务')
                        warned=True
                    self.persist(job)
                    await asyncio.sleep(15)
        finally:
            job.pop('capacity_wait',None)
            if job.get('interaction',{} ) and job['interaction']['kind']=='browser_capacity':job['interaction']=None
            if job['status']=='waiting':job['status']='running'
            self.persist(job)

    async def access_observation(self,session):
        # Login/QR flows can leave a login popup behind after the original tab
        # is authenticated. Inspect only pages in this task's own context.
        original=session.page
        previous_obs,previous_frames=session.last_obs,session._observed_frames
        pages=[original]+[p for p in session.context.pages if p is not original]
        fallback=None
        selected=False
        try:
            for page in pages:
                if page.is_closed() or not is_boss_url(page.url): continue
                session.page=page
                try:
                    obs=await BrowserSession.observe(session,False,text_limit=5000)
                except Exception:
                    continue  # Another tab can still contain the valid session.
                if boss_access_ready(obs):
                    selected=True
                    return obs
                if fallback is None: fallback=obs
            if fallback is not None: return fallback
            raise ValueError('未找到可核验的 BOSS 浏览器标签页')
        finally:
            if not selected:
                session.page=original
                session.last_obs,session._observed_frames=previous_obs,previous_frames

    async def check_access(self,job,session):
        lock=self.access_locks.setdefault(job['id'],asyncio.Lock())
        async with lock:
            future=self.answers.get(job['id'])
            if not future or future.done(): return False
            obs=await asyncio.wait_for(self.access_observation(session),timeout=15)
            return boss_access_ready(obs)

    async def wait_for_access(self,job,session,reason):
        prompt=('请在操作浏览器完成 BOSS 招聘者账号登录。平台会自动检测，核验后继续当前任务。'
                if reason=='login' else 'BOSS 需要人工完成页面校验，请在操作浏览器处理。平台核验后继续当前任务。')
        job.update(status='waiting',error=None,access_wait={'reason':reason,'since':time.time()})
        interaction={'id':uuid.uuid4().hex,'kind':'browser_access','reason':reason,'prompt':prompt}
        job['interaction']=interaction
        future=self.loop.create_future()
        self.answers[job['id']]=future
        self.access_sessions[job['id']]=session
        self.event(job,'access_required','等待 BOSS 登录' if reason=='login' else '等待平台人工校验')
        try:
            while not future.done():
                await asyncio.wait({future},timeout=3)
                if future.done(): break
                try:
                    ready=await self.check_access(job,session)
                except Exception:
                    if not self.browser_alive(session):
                        job['failure_kind']='browser_login'
                        raise ValueError('操作浏览器已关闭，请恢复任务后重新登录')
                    continue  # Navigation during login is transient; keep waiting.
                if ready and not future.done(): future.set_result(True)
            await future
            self.event(job,'access_restored','已在本机核验招聘者页面，继续当前任务')
        finally:
            self.answers.pop(job['id'],None)
            self.access_sessions.pop(job['id'],None)
            self.access_locks.pop(job['id'],None)
            job.pop('access_wait',None)
            job['interaction']=None
            if not future.done(): future.cancel()
            if job['status']=='waiting': job['status']='running'
            self.persist(job)

    async def execute(self, job, recipe):
        try:
            async with self.execution_lock:
                await self._execute(job,recipe)
            if job.get('mode')=='boss_errand':
                # 运行期间新登记的浏览器代办不能被丢下：浏览器空闲就再起一次（browser_errands.after_run 有上限与时段判断）。
                from . import browser_errands
                try:await browser_errands.after_run(self,job)
                except Exception as exc:logging.getLogger("cloakup").warning("浏览器代办续跑失败：%s",exc)
        except asyncio.CancelledError:
            if job.get('status') in ('queued','cancelling'):
                job.update(status='cancelled',finished=time.time(),interaction=None)
                self.persist(job);self.tasks.pop(job['id'],None);self.gates.pop(job['id'],None)
            raise

    async def _execute(self, job, recipe):
        is_platform = job['mode']=='automation_task'
        is_boss = job['mode'] in BOSS_MODES or (is_platform and job['task_snapshot']['config']['engine']=='boss_sourcing')
        reuse = is_boss and self.browser_alive(self.boss_session)
        session = self.boss_session if reuse else WebSession(self, job)
        self.active_session=session
        if reuse:
            session.job = job
            session.last_obs = None
            session._observed_frames = {}
        job["status"] = "running"
        self.event(job, "start", "正在复用已打开的 BOSS 浏览器窗口" if reuse else "正在启动本机浏览器，首次启动可能需要下载浏览器")
        cfg = AgentConfig(allowed_domains=["books.toscrape.com"], trace_dir=None)
        async def confirm(prompt):
            return bool(await self.question(job, "confirm", prompt))
        async def ask(prompt):
            return str(await self.question(job, "reply", prompt))
        try:
            if job.get('account_runner'):
                for member in self.account_execution.members():
                    if member['status']!='running':continue
                    definition=self.definitions.get(member['id'])
                    if not definition['config'].get('plan') and not any(p['task_id']==member['id'] and not p.get('stale') for p in self.task_plan_records()):
                        job['autopilot_stage']='planning'
                        self.event(job,'planning','正在生成任务参考计划，尚未进行页面操作')
                        await self.generate_definition_plan(member['id'])
            if is_platform:
                self.phase(job,'readiness','正在检查执行环境')
                ready=self.latest_readiness()
                if not ready or not ready.get('fresh'):
                    report={'id':uuid.uuid4().hex,'status':'running','started_at':time.time(),'signature':environment_signature(),'checks':[],'run_id':job['id']}
                    self.db.put('readiness',report['id'],report)
                    await preflight(self,report)
                    ready=self.latest_readiness()
                job['readiness_id']=ready['id']
                if not ready.get('fresh'): raise ValueError('环境测试未通过，请到环境检查查看失败项；业务操作尚未开始')
            # Keep authenticated BOSS sessions in the same live context across
            # login, read and review jobs. Session cookies need not survive a
            # complete Chromium shutdown even with a persistent profile.
            if not reuse:
                if self.boss_session is not None:
                    await self.boss_session.close()
                    self.boss_session = None
                if is_boss:
                    self.boss_session = session
                await self.start_browser(job,session)
            if is_platform:
                job['result']=await run_task(self,job,session,TracedAgent,confirm,ask)
                job['status']='succeeded' if job['result']['success'] else 'failed'
                job['error']=None if job['result']['success'] else job['result']['summary']
                self.phase(job,'result',job['result']['summary'])
            elif job['mode']=='browser_open':
                if session.page.url in ('about:blank',''):
                    await session.navigate('https://www.zhipin.com/web/chat/index')
                job['result']={'success':True,'summary':'执行浏览器已打开并保持；未启动自动联系。登录状态将在实际任务中核验。'}
                job['status']='succeeded'
            elif job["mode"] == "login":
                await session.navigate("https://www.zhipin.com/")
                reply = await self.question(job, "reply", "请在独立的 BOSS 浏览器窗口手动完成登录，并进入招聘者端、打开拟试跑的已创建岗位。完成后在这里回复；也可以直接在聊天中告诉我已登录。等待期间不会调用 AI 或采集登录画面。")
                obs = await session.observe(False)
                job["result"] = {"success": True, "summary": "已结束手动登录；本机浏览器窗口将保持打开并供后续岗位任务复用。登录权限与岗位可见性将在读取岗位时核验。", "result": {"reply": reply, "page_url": obs.url}}
                job["status"] = "succeeded"
            elif job['mode'] == 'boss_autopilot':
                result = await run_autopilot(self,job,session)
                job['status'] = result.pop('status')
                job['case_id'] = job['params']['case_id']
                job['result'] = {'success':job['status']=='succeeded','summary':result['summary'],'result':result}
            elif job["mode"] == "boss_recommend":
                case = self.cases.load(job["params"]["case_id"])
                self.event(job, "step", "只读核对普通推荐通道、岗位和学历经验条件")
                pool = await read_recommendations(session, case)
                # Reload before applying a page snapshot, preserving unrelated
                # candidate replies/decisions made during the read.
                case = self.cases.load(case["id"])
                case["recommend_pool"] = {"cards": pool, "filters": case.get("recommend_filters", []), "observed_at": time.time()}
                case["connection_check"] = {"status": "connected", "at": time.time()}
                self.cases.save(case)
                job["case_id"] = case["id"]
                if job["params"]["review"]:
                    result = await run_recommend_review(self, job, session, case, pool)
                    job["status"] = result.pop("status")
                    job["result"] = {"success": job["status"] == "succeeded", "summary": result["summary"], "result": result}
                    job["error"] = result["summary"] if job["status"] == "failed" else None
                else:
                    job["result"] = {"success": True, "summary": f"已读取当前普通推荐页的 {len(pool)} 份资料；未联系任何人，原始资料未发送给模型。", "result": {"case_id": case["id"], "visible_count": len(pool)}}
                    job["status"] = "succeeded"
            elif job["mode"] == "boss_contact":
                result = await run_first_contact(self, job, session)
                job["status"] = result.pop("status")
                job["result"] = {"success": job["status"] == "succeeded", "summary": result["summary"], "result": result}
                job["case_id"] = job["params"]["case_id"]
                job["error"] = result["summary"] if job["status"] == "failed" else None
            elif job["mode"] == "boss_bind":
                outcome = await self.bind_boss_job(job, session, job["params"]["job_url"])
                job["result"] = {"success": True, "summary": outcome["summary"], "result": {"case_id": outcome["case_id"], "job_title": outcome["job_title"]}}
                job["status"] = "succeeded"
            elif job["mode"] == "boss_errand":
                from .browser_errands import run_direct
                result = await run_direct(self, job, session)
                job["status"] = result.pop("status")
                job["result"] = {"success": job["status"] == "succeeded", "summary": result["summary"], "result": result}
                job["error"] = result["summary"] if job["status"] == "failed" else None
            elif recipe:
                runner = TracedRunner(self, job, recipe=recipe, session=session, llm=LLM(self.model_client()),
                                      agent_cfg=cfg, confirm=confirm, ask=ask)
                result = await runner.run(job["params"])
                job["result"] = result.to_dict()
                job["status"] = "succeeded" if result.success else "failed"
                job["error"] = result.error
            else:
                agent = TracedAgent(self, job, agent=cfg, session=session, client=self.model_client(), confirm=confirm, ask=ask)
                result = await agent.run(job["task"], "https://books.toscrape.com/", job["params"], BOOK_SCHEMA)
                job["result"] = asdict(result)
                job["status"] = "succeeded" if result.success else "failed"
                job["error"] = None if result.success else result.summary
            self.event(job, "finish", "任务完成" if job["status"] == "succeeded" else "任务未完成，请查看错误与执行记录")
        except asyncio.CancelledError:
            job.update(status='cancelled', error='已释放浏览器席位，资料与任务状态已保留' if job.get('release_requested') else '已由操作员停止')
            self.event(job, "finish", "已停止执行")
        except Exception as exc:
            job["status"] = "failed"
            job['error_trace'] = _exception_frames(exc)
            from .logbook import exception as log_exception
            log_exception('error',exc,run_id=job['id'],mode=job.get('mode'),stage=job.get('autopilot_stage'),
                          case_id=job.get('active_case_id'),account_runner=bool(job.get('account_runner')))
            if job.get('account_runner'):
                for member in self.account_execution.members():
                    if member['status']=='running':
                        self.account_execution.set_task(member['id'],member['case_ids'],'paused',reason=f'executor_error:{type(exc).__name__}')
            message = ("CloakBrowser 授权未通过，请检查容器许可证配置与网络连接。"
                       if type(exc).__name__ == "CloakBrowserLicenseError" else f"{type(exc).__name__}: {str(exc)[:1200]}")
            self.event(job, "error", message)
            job["error"] = job["events"][-1]["message"]
        finally:
            terminal = job["status"]
            keep_open = is_boss and not job.get('release_requested') and terminal != 'cancelled' and self.browser_alive(session)
            if not keep_open:
                job["status"] = "closing"
                self.persist(job)
                cleanup = asyncio.create_task(session.close())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                if self.boss_session is session:
                    self.boss_session = None
            if self.active_session is session: self.active_session=None
            job['browser_kept_open'] = keep_open
            job.update(status=terminal, interaction=None, pause_requested=False, finished=time.time())
            self.persist(job)
            self.tasks.pop(job["id"], None)
            self.gates.pop(job["id"], None)

    async def bind_boss_job(self, job, session, job_url, case_id=None, encrypt_id=None):
        """读取已建岗位 JD、建岗位档案、生成计划草稿；boss_bind 与浏览器代办（browser_errands）共用。

        encrypt_id：建档前核对读到的地址仍是所选岗位（否则不建档，重试也不会多出一份）；case_id：接着用上次只建了档的半成品。"""
        self.event(job, "step", "正在读取已建岗位的完整 JD")
        snapshot = await read_job(session, job_url)
        if encrypt_id is not None:
            from urllib.parse import parse_qs, urlparse
            if (parse_qs(urlparse(snapshot.get("source_url") or "").query).get("encryptId") or [None])[0] != encrypt_id:
                raise SourcingError("读到的岗位地址里没有所选岗位的岗位标识（地址缺少或换了岗位），未建岗位档案")
        case = self.cases.load(case_id) if case_id else self.cases.create(snapshot["source_url"], snapshot["title"])
        self.cases.bind_job(case, snapshot)
        if job.get("mode") == "boss_bind":
            job["case_id"] = case["id"]
        self.event(job, "thinking", "AI 正在区分必备与加分条件，并起草话术和后续问题")
        plan = await prepare_plan(LLM(self.model_client()), snapshot)
        case.update(greeting_draft=plan["greeting"], search_plan=plan, stage="plan_review")
        self.cases.save(case)
        unattached = self.definitions.import_cases([case])
        summary = "岗位已绑定，寻访计划与话术草稿已生成；平台内容未修改。"
        if unattached:
            summary += "该网站与场景的寻访任务已启用验证后发布，新岗位需在该任务的草案中加入并发布后才参与寻访。"
        return {"case_id": case["id"], "job_title": snapshot["title"], "summary": summary}

    async def compile(self, job):
        job.update(status="compiling", compile_error=None)
        self.event(job, "compile", "正在把成功的探索记录编译成参数化流程")
        try:
            recipe = await compile_trace(self.directory(job) / "trace", LLM(self.model_client()), recipe_id="books_" + job["id"][:12])
            job["draft"] = recipe.to_dict()
            self.event(job, "compile", "流程草稿已生成，请预览参数与步骤后保存")
        except asyncio.CancelledError:
            job["compile_error"] = "已停止编译，原采集结果仍然保留"
        except Exception as exc:
            self.event(job, "error", f"流程编译失败：{str(exc)[:1000]}")
            job["compile_error"] = job["events"][-1]["message"]
        finally:
            job["status"] = "succeeded"
            self.persist(job)
            self.tasks.pop(job["id"], None)

    async def action(self, jid, body):
        job = self.jobs.get(jid)
        if not job:
            raise ValueError("未找到该运行")
        action = body.get("action")
        if action == 'recover_login':
            if not can_recover_login(job) or self.busy(): raise ValueError('当前任务不能按登录恢复方式继续')
            current=self.definitions.get(job['task_id'])
            if current['revision']!=job['task_snapshot']['revision']: raise ValueError('任务配置已变更，请按新配置重新运行')
            job.setdefault('recoveries',[]).append({'at':time.time(),'reason':job.get('error'),'phase':job['phase']})
            job.update(status='queued',error=None,result=None,interaction=None,pause_requested=False)
            job.pop('finished',None)
            self.gates[jid]=asyncio.Event();self.gates[jid].set()
            self.event(job,'recovery','保留原运行记录，恢复登录前的只读探索')
            self.tasks[jid]=asyncio.create_task(self.execute(job,None))
        elif action == 'check_access':
            interaction=job.get('interaction')
            future=self.answers.get(jid)
            session=self.access_sessions.get(jid)
            if (not interaction or interaction['kind']!='browser_access'
                    or interaction['id']!=body.get('interaction_id') or not session
                    or not future or future.done()): raise ValueError('登录等待已更新，请刷新页面')
            ready=await self.check_access(job,session)
            if future.done() or job.get('interaction') is not interaction: return {'ok':True}
            if not ready: raise ValueError('尚未核验到已登录的招聘者页面，请在操作浏览器完成登录或校验')
            future.set_result(True)
        elif action in ('takeover','release'):
            if not job.get('account_runner'):raise ValueError('该任务不是账号执行器')
            if action=='takeover':
                job.update(manual_takeover=True,pause_requested=True,status='pausing',takeover_epoch=job.get('takeover_epoch',0)+1)
                self.gates[jid].clear()
                self.event(job,'operator','管理员请求接管，等待当前操作到达安全暂停点')
            else:
                if job['status']!='paused':raise ValueError('请等待执行器完成暂停后再交还')
                job.update(manual_takeover=False,pause_requested=False,status='running')
                session=self.boss_session
                if session:session.last_obs=None
                self.gates[jid].set()
                self.event(job,'operator','管理员交还，后续动作重新核验页面和会话')
        elif action == "pause":
            if job["status"] not in ("running", "pausing", "paused"):
                raise ValueError("当前任务不能暂停")
            job["pause_requested"] = True
            job["status"] = "pausing"
            self.gates[jid].clear()
        elif action == "resume":
            if job.get('manual_takeover'):raise ValueError('管理员已接管，请使用交还自动化完成核验')
            if job["status"] not in ("paused", "pausing"):
                raise ValueError("任务未暂停")
            job.update(pause_requested=False, status="running")
            self.gates[jid].set()
        elif action == "cancel":
            task = self.tasks.get(jid)
            if not task or job["status"] in ("cancelling", "closing"):
                raise ValueError("任务已结束或正在停止")
            job["status"] = "cancelling"
            task.cancel()
        elif action == "answer":
            interaction, future = job.get("interaction"), self.answers.get(jid)
            if not interaction or interaction["id"] != body.get("interaction_id") or not future or future.done():
                raise ValueError("该请求已处理，请刷新页面")
            if interaction["kind"]=="browser_access":
                # Old open tabs submit the former reply form. Treat it as a
                # request for the SAME real browser check; ignore reply text.
                return await self.action(jid,{'action':'check_access','interaction_id':interaction['id']})
            value = body.get("value")
            if interaction["kind"] == "confirm" and type(value) is not bool:
                raise ValueError("请确认或拒绝该操作")
            if interaction["kind"] == "reply" and (not isinstance(value, str) or len(value) > 4000):
                raise ValueError("回复需要为文本且不超过 4000 字")
            future.set_result(value)
        elif action == "compile":
            if job["status"] != "succeeded" or job["mode"] != "explore" or self.busy():
                raise ValueError("仅可编译成功的探索记录，且需等待其他任务结束")
            job["status"] = "compiling"
            self.tasks[jid] = asyncio.create_task(self.compile(job))
        elif action == "save":
            if job["status"] != "succeeded" or not job.get("draft"):
                raise ValueError("请先生成流程草稿")
            if job.get("saved_recipe"):
                return {"file": job["saved_recipe"]}
            data = copy.deepcopy(job["draft"])
            name = body.get("name", "按分类采集图书")
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ValueError("请输入流程名称（最多 80 字）")
            data["name"] = name.strip()
            recipe = Recipe.from_dict(data)
            path = self.root / "recipes" / f"{recipe.id}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Never overwrite a pre-existing user's recipe.
            with path.open("x", encoding="utf-8") as f:
                yaml.safe_dump(recipe.to_dict(), f, allow_unicode=True, sort_keys=False)
            job["saved_recipe"] = path.name
            self.event(job, "save", f"已保存流程：{name}")
        else:
            raise ValueError("未知操作")
        self.persist(job)
        return {"ok": True}

    async def shutdown(self):
        if hasattr(self,"attention_monitor_future"): self.attention_monitor_future.cancel()
        tasks = list(self.tasks.values())+list(self.planning.workers.values())+list(self.agent_workspace.workers.values())+list(self.scenario_workspace.workers.values())
        if self.preflight_task and not self.preflight_task.done(): tasks.append(self.preflight_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.boss_session is not None:
            await self.boss_session.close()
            self.boss_session = None
        if self.client is not None:
            from .harness_runtime import close_harnesses
            await close_harnesses(self.client)
            await self.client.raw.close()

    async def verify_action(self,ident,body):
        from .action_gateway import ActionLedger
        ledger=ActionLedger(self.db);operation=ledger.get(ident)
        if not operation['capability'].startswith('web_'):raise ValueError('平台消息必须通过真实会话回执核验，不能人工绕过')
        if operation['state']!='unknown':raise ValueError('只允许核验结果未知的动作')
        state=body.get('state');evidence=body.get('evidence','')
        if body.get('confirmed') is not True or state not in ('verified','failed') or not isinstance(evidence,str) or not 5<=len(evidence.strip())<=2000:raise ValueError('请在原系统核实后确认结果，并填写核验依据')
        ledger.transition(ident,state,evidence=evidence.strip(),verification='administrator',verified_at=time.time())
        self.db.append(operation['run_id'] or ident,{'type':'administrator_action_verification','operation_id':ident,'state':state,'evidence':evidence.strip(),'at':time.time()})
        return {'ok':True,'state':state}

    async def retry_decision(self,case_id,body):
        case=self.cases.load(case_id);person=self.cases.candidate(case,body.get('candidate_id'))
        if not person.get('decision_hold'):raise ValueError('该候选人没有待恢复的决策')
        person['decision_hold']['retry_at']=0;person['decision_wait_until']=0;person['harness_wait_count']=0
        person.pop('harness_decision',None)
        self.cases.audit(case,'harness_decision_retry',candidate_id=person['id'],reason='管理员请求下轮重新观察和决策；不重发未知消息、不启动暂停任务')
        return {'ok':True}

    async def update_attention(self,ident,body):
        return self.attention.update(ident,body)

    async def request_resume_archive(self,case_id,body):
        case=self.cases.load(case_id);person=self.cases.candidate(case,body.get('candidate_id'))
        if not person.get('conversation') or not person.get('resumes'):raise ValueError('尚无已绑定会话的附件简历')
        for state in person.get('resume_archives',{}).values():
            if state['status'] in ('failed','downloading'):state['status']='queued'
        person['resume_archive_requested']=True
        self.cases.audit(case,'resume_archive_requested',candidate_id=person['id'],reason='用户请求归档已有简历；不发送候选人消息')
        if not self.busy():
            await self.create({'mode':'boss_autopilot','params':{'case_id':case_id,'read_only':True,'continuous':False,'new_contacts':False}})
        return {'ok':True,'status':'queued'}

    def followup_policy(self):
        from .followup import DEFAULTS
        try:return self.db.get('followup_policy','default')
        except FileNotFoundError:return {'id':'default','revision':0,'policy':dict(DEFAULTS)}

    async def configure_followup_policy(self,body):
        raise ValueError('追聊策略已迁移到各任务，请在任务配置中修改，不再统一覆盖所有岗位')

    async def configure_case(self, case_id, body):
        return save_config(self.cases,self.cases.load(case_id),body)

    async def autonomy(self, case_id, body):
        case = self.cases.load(case_id)
        enabled = body.get('enabled')
        self.cases.set_autonomy(case, enabled, source='用户在本机工作台设置岗位自动沟通')
        return {'ok':True, 'enabled':enabled}

    def close(self):
        try:
            self.call(self.shutdown())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)


def make_server(manager: Workbench, port: int = 8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logging.getLogger("cloakup.web").debug(fmt, *args)

        def send(self, data, status=200, kind="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def trusted(self, write=False):
            # Host validation also protects localhost APIs from DNS rebinding.
            ports={str(self.server.server_port),os.environ.get("CLOAKUP_PUBLIC_PORT",str(self.server.server_port))}
            hosts = {f"{h}:{p}" for h in ("127.0.0.1","localhost") for p in ports}
            host = self.headers.get("Host")
            if host not in hosts:
                self.send({"error": "仅允许本机工作台访问"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                self.send({"error": "拒绝跨站请求"}, 403)
                return False
            if write and not secrets.compare_digest(self.headers.get("X-Cloakup-Token", ""), manager.token):
                self.send({"error": "页面会话已过期，请刷新"}, 403)
                return False
            return True

        def do_GET(self):
            if not self.trusted():
                return
            path = self.path.split("?", 1)[0]
            try:
                if path == '/api/health':
                    self.send({'status':'ok','database':manager.db.probe()})
                elif re.fullmatch(r'/api/agent-tests/[a-f0-9]{32}(?:/export)?',path):
                    self.send(manager.agent_workspace.get_test(path.split('/')[3]))
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/agent-workspace',path):
                    ident=path.split('/')[3]
                    async def workspace_state():
                        release=manager.agent_workspace.bootstrap(ident)
                        return {'release':release,'version':manager.db.get('agent_bundle',release['version_id']),
                                'versions':[v for v in manager.db.list('agent_bundle') if v['task_id']==ident],
                                'catalog':manager.agent_workspace.catalog()}
                    self.send(manager.call(workspace_state()))
                elif re.fullmatch(r'/api/runs/[a-f0-9]{32}/diagnostics',path):
                    self.send(manager.call(manager.diagnostics(path.split('/')[3])))
                elif re.fullmatch(r'/api/readiness/[a-f0-9]{32}',path):
                    report=manager.db.get('readiness',path.split('/')[3])
                    report.pop('signature',None)
                    self.send(report)
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/history',path):
                    self.send(manager.db.history('task',path.split('/')[3]))
                elif path=='/api/boss/jobs':
                    # 岗位选择器：最近一次从 BOSS 读到的岗位（只在本机）与绑定标注；task_id 用于标注“已在本任务／由其他任务管理”。
                    from urllib.parse import parse_qs as _qs
                    from .browser_errands import view as errand_view
                    task_id=(_qs(self.path.split('?',1)[1]).get('task_id') or [None])[0] if '?' in self.path else None
                    if task_id is not None and not re.fullmatch(r'[a-f0-9]{32}',task_id):task_id=None
                    async def boss_jobs_view():return errand_view(manager,task_id)
                    self.send(manager.call(boss_jobs_view()))
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/jobs/changes',path):
                    from .job_changes import changes as job_changes
                    async def job_change_list():return job_changes(manager,path.split('/')[3])
                    self.send(manager.call(job_change_list()))
                elif re.fullmatch(r'/api/runs/[a-f0-9]{32}/trace',path):
                    self.send({'run':manager.db.get('run',path.split('/')[3]),'events':manager.db.trace(path.split('/')[3])})
                elif re.fullmatch(r'/api/resume-files/[a-f0-9]{32}(?:/preview)?',path):
                    from .resume_archive import read_file,word_preview
                    record,data=read_file(manager.cases,path.split('/')[3])
                    if path.endswith('/preview') and record['extension']=='docx':self.send(word_preview(data),kind='text/html; charset=utf-8')
                    else:self.send(data,kind=record['mime'])
                elif path == '/api/daily-reports':
                    from .daily_report import listing
                    self.send({'reports':listing(manager.db)})
                elif re.fullmatch(r'/api/daily-reports/\d{4}-\d{2}-\d{2}(?:\.md)?',path):
                    from .daily_report import KIND
                    ident=path.rsplit('/',1)[1]
                    report=manager.db.get(KIND,ident.removesuffix('.md'))
                    if ident.endswith('.md'):self.send(report['markdown'].encode(),kind='text/markdown; charset=utf-8')
                    else:self.send(report)
                elif path == "/api/state":
                    self.send(manager.call(manager.snapshot()))
                elif re.fullmatch(r"/api/cases/[a-f0-9]{32}/audit", path):
                    case = manager.cases.load(path.split('/')[3])
                    self.send({'case_id':case['id'],'job':case['job'],
                               'audit':case.get('audit',[]),'candidates':case['candidates'],
                               'config_revision':case.get('config_revision',0),'config_history':case.get('config_history',[]),
                               'effective_config':view_config(case),'source_job_snapshot':case.get('source_job_snapshot')})
                elif re.fullmatch(r"/api/tasks/[a-f0-9]{32}/browser-diagnostics", path):
                    from .browser_diagnostics import inspect
                    self.send(manager.call(inspect(manager,path.split('/')[3])))
                elif re.fullmatch(r'/api/runs/[a-f0-9]{32}/incident-screenshots/[a-f0-9]{32}',path):
                    run_id, screenshot_id = path.split('/')[3::2]
                    run = manager.db.get('run',run_id)
                    if not any(r.get('screenshot_id')==screenshot_id for r in run.get('selection_recovery',{}).values()):
                        raise FileNotFoundError()
                    self.send((manager.data / run_id / f'incident-{screenshot_id}.jpg').read_bytes(),kind='image/jpeg')
                elif re.fullmatch(r"/api/runs/[a-f0-9]{32}/screenshot", path):
                    p = manager.data / path.split("/")[3] / "latest.jpg"
                    self.send(p.read_bytes(), kind="image/jpeg")
                elif re.fullmatch(r"/api/runs/[a-f0-9]{32}/csv", path):
                    jid = path.split("/")[3]
                    snapshot = manager.call(manager.snapshot())
                    job = next((r for r in snapshot["runs"] if r["id"] == jid), None)
                    if not job or not job.get("result"):
                        raise FileNotFoundError()
                    data = job["result"].get("result") or {}
                    rows = data.get("books", []) if isinstance(data, dict) else []
                    out = io.StringIO()
                    writer = csv.writer(out)
                    writer.writerow(["书名", "价格", "库存"])
                    for b in rows:
                        # Prevent spreadsheet formula injection from scraped text.
                        writer.writerow([("'" + str(v)) if str(v).startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else v
                                         for v in (b.get("title"), b.get("price"), b.get("in_stock"))])
                    self.send(out.getvalue().encode("utf-8-sig"), kind="text/csv; charset=utf-8")
                elif path in ("/", "/app.js", "/sourcing.js", "/platform.js", "/agents.js", "/style.css"):
                    filename = {"/": "index.html", "/app.js": "app.js", "/sourcing.js": "sourcing.js", "/platform.js":"platform.js", "/agents.js":"agents.js", "/style.css": "style.css"}[path]
                    mime = {"/": "text/html", "/app.js": "text/javascript", "/sourcing.js": "text/javascript", "/platform.js":"text/javascript", "/agents.js":"text/javascript", "/style.css": "text/css"}[path]
                    self.send((ASSETS / filename).read_bytes(), kind=mime + "; charset=utf-8")
                else:
                    self.send({"error": "未找到资源"}, 404)
            except FileNotFoundError:
                self.send({"error": "未找到资源"}, 404)
            except Exception:
                logging.getLogger("cloakup.web").exception("读取工作台失败")
                self.send({"error": "读取失败，请查看启动终端"}, 500)

        def do_POST(self):
            if not self.trusted(write=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > (262144 if '/agent-workspace/' in self.path or '/scenarios' in self.path else 32768):
                    raise ValueError("请求大小不合法")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求需要为对象")
                if re.fullmatch(r'/api/daily-reports/\d{4}-\d{2}-\d{2}',self.path):
                    # 手动生成或重算某一天（含当天截至目前）的日报；只读本地记录。
                    from datetime import date
                    from .daily_report import generate
                    day=date.fromisoformat(self.path.rsplit('/',1)[1])
                    self.send(generate(manager,day))
                    return
                if re.fullmatch(r'/api/tasks/[a-f0-9]{32}/greetings/[a-f0-9]{32}/recheck',self.path):
                    tid,cid=self.path.split('/')[3],self.path.split('/')[5]
                    if cid not in manager.definitions.get(tid)['config']['case_ids']:raise ValueError('岗位不属于当前任务')
                    from .greeting_policy import key
                    record=manager.db.get('greeting_readiness',key(tid,cid))
                    record['next_check_at']=0;manager.db.put('greeting_readiness',record['id'],record)
                    result={'status':'queued','detail':'将在本任务下一允许周期只读复核，不立即发送消息或重复保存'}
                elif self.path=='/api/scenarios':
                    result=manager.scenario_workspace.create(body)
                elif re.fullmatch(r'/api/scenarios/[a-f0-9]{32}/(save|context|session|chat|apply)',self.path):
                    ident=self.path.split('/')[3];action=self.path.split('/')[-1]
                    async def scenario_action():
                        lab=manager.scenario_workspace
                        if action=='chat':return await lab.chat(ident,body)
                        return {'save':lab.save,'context':lab.test_context,'session':lab.session,'apply':lab.apply}[action](ident,body)
                    result=manager.call(scenario_action(),timeout=180)
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/agent-workspace/todo-answer',self.path):
                    from .faq_answers import submit as submit_faq_answer
                    async def faq_answer_action():return submit_faq_answer(manager,self.path.split('/')[3],body)
                    result=manager.call(faq_answer_action())
                elif self.path=='/api/boss/jobs/refresh':
                    from .browser_errands import request_refresh
                    result=manager.call(request_refresh(manager))
                elif self.path=='/api/boss/jobs/bind':
                    from .browser_errands import request_bind
                    result={'errands':manager.call(request_bind(manager,body))}
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/jobs',self.path):
                    from .job_changes import request as request_job_change
                    async def job_change_action():return request_job_change(manager,self.path.split('/')[3],body)
                    result=manager.call(job_change_action())
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/agent-workspace/(draft|session|chat|test|publish|todo)',self.path):
                    ident=self.path.split('/')[3];action=self.path.split('/')[-1]
                    async def workspace_action():
                        lab=manager.agent_workspace
                        if action=='draft':return lab.draft(ident,body)
                        if action=='session':return lab.session(ident,body)
                        if action=='chat':return await lab.chat(ident,body.get('session_id'),body)
                        if action=='todo':return await lab.todo_draft(ident,body)
                        if action=='test':return lab.start_test(ident,body)
                        if action=='publish':return lab.publish(ident,body)
                    result=manager.call(workspace_action(),timeout=180)
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/browser-diagnostics',self.path):
                    from .browser_diagnostics import verify_target,greeting_settings,job_pages
                    if body not in ({'action':'verify_existing_target'},{'action':'read_greeting_settings'},{'action':'read_job_pages'}):raise ValueError('不支持的诊断操作')
                    fn={'read_greeting_settings':greeting_settings,'read_job_pages':job_pages}.get(body['action'],verify_target)
                    result=manager.call(fn(manager,self.path.split('/')[3]),timeout=240 if body['action']=='read_job_pages' else 120)
                elif self.path=='/api/browser-capacity':
                    if body.get('action')=='refresh':result=manager.call(manager.refresh_capacity())
                    elif body.get('action')=='release':result=manager.call(manager.release_browser_seat())
                    else:raise ValueError('席位操作不合法')
                elif re.fullmatch(r'/api/account-tasks/[a-f0-9]{32}',self.path):
                    result=manager.call(manager.account_task_action(self.path.split('/')[3],body))
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/planning-message',self.path):
                    result=manager.call(manager.planning.submit(self.path.split('/')[3],body))
                elif re.fullmatch(r'/api/planning-requests/[a-f0-9]{32}/retry',self.path):
                    result=manager.call(manager.planning.retry(self.path.split('/')[3]))
                elif re.fullmatch(r'/api/attention/[a-f0-9]{32}/answer',self.path):
                    todo=manager.db.get('attention',self.path.split('/')[3])
                    result=manager.call(manager.planning.submit(body.get('task_id'),body,todo))
                elif re.fullmatch(r'/api/attention/[a-f0-9]{32}',self.path):
                    result=manager.call(manager.update_attention(self.path.split('/')[3],body))
                elif re.fullmatch(r'/api/cases/[a-f0-9]{32}/resume-archive',self.path):
                    result=manager.call(manager.request_resume_archive(self.path.split('/')[3],body))
                elif self.path == '/api/followup-policy':
                    result=manager.call(manager.configure_followup_policy(body))
                elif self.path == '/api/preflight':
                    result=manager.call(manager.test_environment(body))
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}/plan',self.path):
                    result=manager.call(manager.generate_definition_plan(self.path.split('/')[3]),timeout=160)
                elif self.path == '/api/tasks':
                    result=manager.call(manager.save_definition(body))
                elif re.fullmatch(r'/api/tasks/[a-f0-9]{32}',self.path):
                    result=manager.call(manager.save_definition(body,self.path.split('/')[3]))
                elif self.path == "/api/runs":
                    result = manager.call(manager.create(body))
                elif re.fullmatch(r'/api/actions/[a-f0-9]{64}/verify',self.path):
                    result=manager.call(manager.verify_action(self.path.split('/')[3],body))
                elif re.fullmatch(r'/api/cases/[a-f0-9]{32}/decision-retry',self.path):
                    result=manager.call(manager.retry_decision(self.path.split('/')[3],body))
                elif re.fullmatch(r'/api/cases/[a-f0-9]{32}/answer-preview',self.path):
                    from .answering import answer_question
                    question=body.get('question')
                    if not isinstance(question,str) or not 1<=len(question.strip())<=1000:raise ValueError('请输入1至1000字的问题')
                    result=answer_question(manager.cases.load(self.path.split('/')[3]),question)
                elif re.fullmatch(r"/api/cases/[a-f0-9]{32}/config", self.path):
                    result = manager.call(manager.configure_case(self.path.split('/')[3],body))
                elif re.fullmatch(r"/api/cases/[a-f0-9]{32}/autonomy", self.path):
                    result = manager.call(manager.autonomy(self.path.split('/')[3],body))
                elif re.fullmatch(r"/api/runs/[a-f0-9]{32}/action", self.path):
                    result = manager.call(manager.action(self.path.split("/")[3], body))
                else:
                    self.send({"error": "未找到接口"}, 404)
                    return
                self.send(result)
            except (ValueError, RecipeError, FileNotFoundError, FileExistsError, KeyError) as exc:
                self.send({"error": str(exc)}, 400)
            except Exception:
                logging.getLogger("cloakup.web").exception("工作台操作失败")
                self.send({"error": "操作失败，请查看启动终端"}, 500)

    return ThreadingHTTPServer((os.environ.get("CLOAKUP_BIND_HOST","127.0.0.1"), port), Handler)


def serve(root: Path, port: int = 8765):
    manager = Workbench(root)
    try:
        server = make_server(manager, port)
    except Exception:
        manager.close()
        raise
    previous_term = None
    if threading.current_thread() is threading.main_thread():
        def stop_signal(signum, frame):
            raise KeyboardInterrupt()
        previous_term = signal.signal(signal.SIGTERM, stop_signal)
    print(f"\nCloakup 工作台：http://127.0.0.1:{server.server_port}\n按 Ctrl+C 停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
        if previous_term is not None: signal.signal(signal.SIGTERM, previous_term)
