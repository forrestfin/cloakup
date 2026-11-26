# 工作台“今天”首页与菜单整理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 首页改成“今天”（任务状态、按岗位的今日数字、待办、最近动态），菜单加“岗位”入口并把调试页收进“高级”，岗位加入已发布任务后只在任务里改事实。

**Architecture:** 新模块 `cloakup/today.py` 只读计算首页数据，`workbench.snapshot()` 以 `state.today` 下发；数字中与日报共有的列复用 `daily_report` 的公共计算。前端只改 `index.html`、`app.js`、`platform.js`、`sourcing.js`。“是否锁定岗位配置”的判断从 `job_config.save_config` 抽出成 `locked_task`，前后端共用。

**Tech Stack:** Python 3.12（`.venv`）、pytest、原生 JS；Postgres（运行时）。

**Spec:** `docs/superpowers/specs/2026-11-25-workbench-today-and-menu-design.md`（含“实现者的决定”一节，均已由管理员认可）

## Global Constraints
- 不改执行逻辑、发送规则、额度、追聊、隐私规则；不碰 `autopilot.py`、`boss_*.py`、`decision_runtime.py`。
- 首页不显示内部代号（如 `selection_blocked:…`、`harness_executing`），不写候选人姓名（统一“一位候选人”），不链接到对话。
- 与日报共有的数字（来信、已回复、收到简历、首聊已送达）必须同口径；日报输出不变、不加列、不重算历史日报。
- 首聊“已用／份额”取 `job_quota.task_quota()`，与任务页、执行器同口径。
- 前端沿用所在文件的紧凑单行写法；新增文字为中文；`localStorage` 读写包 try/catch。
- 每个任务跑全量单测 `.venv/bin/python -m pytest tests -q` 和 `.venv/bin/ruff check .`，一批一个提交（英文祈使句标题 + 正文，不加署名）。
- 不提交 `data/`、`.env*`、`.tmp_incident/`、`AGENTS.md`。

## Review Focus
1. 跨日边界：昨天 23:59 发出的消息、今天 00:01 的来信——只算今天的。→ Task 1 `test_counts_only_today_across_midnight`
2. 任务暂停但岗位仍有待回会话：状态显示暂停原因，“待回”照常计数。→ Task 1 `test_paused_task_still_counts_waiting_conversations`
3. 没有任何寻访任务（或任务没有岗位）：首页不报错，退回旧首页内容。→ Task 1 `test_no_sourcing_task_returns_empty` + Task 2 浏览器检查
4. 系统暂停且有对应待办：状态写待办的人话原因，不出现内部原因码。→ Task 1 `test_system_pause_uses_todo_wording`
5. 已加入已发布任务的岗位：岗位页不能保存配置、“去修改”跳到任务“岗位与事实”并选中该岗位。→ Task 4 `test_locked_task_matches_save_config` + 浏览器检查

---

### Task 1: `cloakup/today.py` 首页数据计算

**Files:**
- Create: `cloakup/today.py`
- Modify: `cloakup/daily_report.py`（把 `_outcomes` 改名为公共 `outcomes`，保留 `_outcomes = outcomes` 别名；不改行为）
- Test: `tests/test_today.py`

**Interfaces:**
- Consumes: `daily_report.outcomes(cases, start, end) -> (per_job, summary, unanswered)`；`daily_report.day_bounds(day, tz)`、`report_timezone()`；`job_quota.task_quota(store, task, day) -> {'shares':{cid:int},'usage':{cid:int},...}`；`task_policy.window_status(schedule, now)`；`autonomy.day_key()`。
- Produces: `today.build(db, store, cases, members, availability, now=None) -> dict`，其中 `store` 有 `.list()`（CaseStore），`members` 为 `account_execution.members()` 的列表，`availability` 为 `callable(cid) -> {'state','reason','next_at'}`。返回：
  ```
  {'day':'YYYY-MM-DD',
   'tasks':[{'id','name','state','label','detail','next_at','windows':'09:00–12:00、13:30–23:30','in_window':bool,
             'jobs':[{'case_id','label','quota_used','quota_share','first_contacts_sent','incoming','replies_sent',
                      'resumes','greetings','greetings_unanswered','waiting_now'}],
             'totals':{同上数字键}}],
   'todos':[{'id','title','job_label','created_at','detail'}],     # 未解决，最多 5 条，按 created_at 倒序
   'activity':[{'at','job_label','text'}]}                         # 今天，最多 10 条，按 at 倒序
  ```
  `state` 取值与 `label`：`running`→“运行中”、`outside_window`→“等待执行时段”、`resting`→“休息中”、`paused_admin`／`paused_system`→“暂停中”、`blocked`→“受阻”。

- [ ] **Step 1: 写失败测试 `tests/test_today.py`**（用 `CaseStore(tmp_path)` 造岗位与候选人，`members`／`availability` 用简单假对象）
  - `test_numbers_match_daily_report_columns`：同一批数据，`incoming/replies_sent/resumes/first_contacts_sent` 等于 `daily_report.outcomes` 对该岗位的 `replies/messages_sent/resumes_archived/first_contacts`。
  - `test_quota_columns_come_from_task_quota`：`quota_used/quota_share` 等于 `job_quota.task_quota()` 的 `usage[cid]/shares[cid]`（含一条 `delivery_unknown` 首聊：计入已用、不计入已送达）。
  - `test_counts_only_today_across_midnight`：昨天 23:59 的发送与来信不计，今天 00:01 的计入。
  - `test_greetings_and_unanswered`：候选人先开口（`history[0].direction=='in'`）且首条来信在今天 → `greetings==1`；其后无我方消息 → `greetings_unanswered==1`；有 sent 回复后为 0。
  - `test_waiting_now_counts_conversations_not_messages`：一个会话连发 3 条来信未回 → `waiting_now==1`；终止状态会话不计。
  - `test_status_mapping`：参数化 running+open→“运行中”；outside_window（`next_at` 给出）→“等待执行时段”；resting→“休息中”；`release_changed`／`job_not_validated`（全部岗位）／`planning`→“受阻”。
  - `test_admin_pause_says_who_and_when`：成员 paused、history 末条 `actor='admin'` → `state=='paused_admin'`，`detail` 含“管理员”与 HH:MM。
  - `test_system_pause_uses_todo_wording`：成员 paused、末条 `actor='system'`，存在未解决待办（`task_id` 相同）→ `detail` 用待办 `title`；`detail` 不含 `selection_blocked` 等英文原因码（断言 `re.search(r'[a-z_]{6,}', detail) is None`）。
  - `test_paused_task_still_counts_waiting_conversations`。
  - `test_todos_top_five_unresolved_newest_first`。
  - `test_activity_plain_words_without_names`：一条首聊→“向一位候选人发出首聊”，一条回复→“回复了一位候选人”，一份简历审计→“收到一份简历”；断言候选人的 `display_name` 不出现在任何 `text` 中；最多 10 条、倒序、只含今天。
  - `test_no_sourcing_task_returns_empty`：没有寻访任务 → `tasks==[]`，不报错。

- [ ] **Step 2: 运行确认失败**
  Run: `.venv/bin/python -m pytest tests/test_today.py -q`　Expected: 收集失败（`cloakup.today` 不存在）

- [ ] **Step 3: 实现 `cloakup/daily_report.outcomes` 改名与 `cloakup/today.build`**
  - 寻访任务：`db.list('task')` 中 `scenarios.is_sourcing(cfg['engine'])` 且有成员记录的；岗位用 `job_members.normalize(cfg)` 后的成员顺序。
  - 状态：成员 paused → 看 history 末条 actor（`admin`→paused_admin，否则 paused_system，`detail` 取该任务未解决待办中最新一条的 `title`，没有则“系统暂停”）；成员 running → 对各岗位调 `availability`，任一 `release_changed`/`planning` 或全部 `job_not_validated` → blocked（`detail` 用 availability 的中文 `reason`），否则按第一个岗位的 `outside_window`/`resting`/open 映射。
  - 数字：对每个岗位调用 `outcomes([case], start, end)`；`greetings`／`waiting_now` 本模块自算；日界线用 `day_bounds(date, report_timezone())`。
  - 时段：`windows` 由任务 `config['schedule']['windows']` 拼出，`in_window` 用 `window_status`。
  - 动态：outbox `status=='sent'` 且 `sent_at` 在今天；审计 `resume_file_archived`／`candidate_opt_out`／`consultant_handoff`；成员 history 当天的暂停／恢复（“管理员暂停任务”“系统暂停任务”“任务恢复运行”）；当天创建的 `kind=='browser_health'` 待办 →“BOSS 页面打不开，系统正在自动重试”，其 `resolved_at`（如有）→“BOSS 页面已恢复”。

- [ ] **Step 4: 运行通过**　Run: `.venv/bin/python -m pytest tests/test_today.py tests/test_daily_report.py -q`　Expected: PASS

- [ ] **Step 5: 全量单测 + ruff，提交**
  ```bash
  git add cloakup/today.py cloakup/daily_report.py tests/test_today.py
  git commit -m "Compute the Today page from live records"
  ```

### Task 2: 下发 `state.today` 并把首页改成“今天”

**Files:**
- Modify: `cloakup/workbench.py`（`snapshot()` 增加 `"today"` 键）
- Modify: `cloakup/web/platform.js`（`workspaceHome()`）
- Test: `tests/test_workbench.py`

**Interfaces:**
- Consumes: Task 1 `today.build(db, store, cases, members, availability, now=None)`。
- Produces: `/api/state` 的 `today` 字段（结构同 Task 1）。

- [ ] **Step 1: 写失败测试** `test_snapshot_includes_today`：用 test_workbench 现有 manager 夹具调用 `snapshot()`，断言 `'today' in state and state['today']['tasks']==[]`（夹具无寻访任务）且 `today.build` 抛异常时 `snapshot()` 仍返回、`state['today']=={'error':'今天的数据暂时算不出来'}`（首页不能把整个工作台拖垮）。
- [ ] **Step 2: 运行确认失败**　Run: `.venv/bin/python -m pytest tests/test_workbench.py -q -k today`
- [ ] **Step 3: 实现**：`snapshot()` 用已有 `cases` 调 `today.build(self.db, self.cases, cases, self.account_execution.members(), self.account_execution.availability)`，异常时记 `logging.getLogger('cloakup.web').exception` 并给上面的兜底值。
- [ ] **Step 4: 前端 `workspaceHome()`**：标题“今天”，副标题为日期；有 `state.today.tasks` 时依次渲染：状态条（`label`+`detail`、时段与“去任务”按钮 `data-task-workspace`）、数字表（列：岗位／首聊“已用/份额”+小字“已送达 N”／来信／已回复／收到简历／主动打招呼“N（未回 M）”／待回“此刻”，末行合计）、“需要你处理”（每条“去处理”按钮设置 `ui.todoSelection=id` 后 `data-nav="attention"`）、“最近动态”。无任务或 `today.error` 时保留旧首页内容并在顶部显示 `today.error`。
- [ ] **Step 5: 验证**：`node --check cloakup/web/platform.js`；全量单测 + ruff；在 `.venv` 本地起服务不可行时，部署后（Task 6）在浏览器检查。
- [ ] **Step 6: 提交** `git commit -m "Show today's run on the home page"`

### Task 3: 菜单与岗位页入口

**Files:**
- Modify: `cloakup/web/index.html`（导航）、`cloakup/web/app.js`（`contextLabels`、`parentView`）、`cloakup/web/sourcing.js`（`sourcing()` 标题与“所在任务”列）、`cloakup/web/style.css`（“高级”折叠样式，如需要）

**Interfaces:**
- Consumes: 现有 `data-nav` 路由；`state.definitions`。
- Produces: `data-nav="sourcing"` 成为一级菜单“岗位”。

- [ ] **Step 1: 导航**：分组改为——工作空间：今天（operations）、任务（tasks）、待办（attention，保留 `#todo-count`）、岗位（sourcing）；平台管理：执行环境；底部：系统设置；“高级”分组（场景模板、规划端、执行端）默认收起，点标题展开，展开状态存 `localStorage['nav-advanced']`（try/catch）；当前页属于“高级”时自动展开。
- [ ] **Step 2: `app.js`**：`contextLabels` 改为 `{operations:'今天',tasks:'任务',attention:'待办',sourcing:'岗位',…}`；`parentView` 中 `sourcing` 归自身（高亮“岗位”），`chat/audit/execution` 仍归 `tasks`。
- [ ] **Step 3: 岗位页**：`pageHead('岗位','绑定 BOSS 岗位、设置问候语与事实；加入任务在任务里操作', …)`；岗位表新增“所在任务”列（`state.definitions` 中 `config.case_ids` 含该岗位的任务名，没有写“—”）。
- [ ] **Step 4: 验证**：`node --check` 三个 JS 文件；全量单测 + ruff。
- [ ] **Step 5: 提交** `git commit -m "Give jobs a menu entry and fold debug pages under Advanced"`

### Task 4: 已加入已发布任务的岗位只在任务里改

**Files:**
- Modify: `cloakup/job_config.py`（新增 `locked_task`，`save_config` 改用它）
- Modify: `cloakup/workbench.py`（`snapshot()` 的 cases 增加 `config_locked_by`）
- Modify: `cloakup/web/sourcing.js`（`jobDetail()`）
- Test: `tests/test_job_config.py`

**Interfaces:**
- Produces: `job_config.locked_task(db, case_id) -> dict | None`，返回 `{'id': task_id, 'name': task_name}`：当 `task_policy.effective_task(db, case_id)` 存在且 `db.list('agent_release')` 中有该任务 id 时；否则 `None`。`save_config` 在其非 None 时抛出原有那句 `SourcingError`（文字不变）。
- Produces: `/api/state` 每个 case 带 `config_locked_by`（同上 dict 或 null）。

- [ ] **Step 1: 写失败测试** `test_locked_task_matches_save_config`：无任务 → `None` 且可保存；任务无发布 → `None`；任务有 `agent_release` → 返回 `{'id','name'}` 且 `save_config` 抛原文 `SourcingError`。
- [ ] **Step 2: 运行确认失败**　Run: `.venv/bin/python -m pytest tests/test_job_config.py -q -k locked`
- [ ] **Step 3: 实现 `locked_task` 并让 `save_config` 调用它**；`snapshot()` 两处 cases 构造都加 `"config_locked_by":locked_task(self.db,c['id'])`。
- [ ] **Step 4: 前端 `jobDetail()`**：`c.config_locked_by` 存在时，顶部 `notice`：“这个岗位已在任务「名称」中运行，事实和问候语请在任务的『岗位与事实』里修改（修改会先验收再生效）”+按钮“去修改”（`data-edit-in-task="<cid>"`，点击调用 `openCaseWorkspace(cid,'strategy')`）；整张 `#job-config-form` 只读（`fieldset disabled` 或逐项禁用），隐藏“保存配置”“重置修改”，不显示脱敏事实草稿提示。
- [ ] **Step 5: 验证**：单测、`node --check`、全量单测 + ruff。
- [ ] **Step 6: 提交** `git commit -m "Point jobs in a published task to the task's facts editor"`

### Task 5: 功能对照文档

**Files:**
- Create: `docs/workbench-map.md`
- Modify: `CLAUDE.md`（“目录结构”末尾加一行：`- 菜单 → 页面 → 代码对照：docs/workbench-map.md`）

- [ ] **Step 1: 写对照表**：每个菜单项一行：菜单 → 页面（view 名）→ 主要功能 → 前端函数 → 后端接口 → Python 模块；再列“常见操作在哪”：看今天、处理待办、加岗位（岗位 → 刷新岗位列表 → 绑定 → 配置 → 任务概览加入）、改问候语／事实（未加入任务：岗位页；已加入：任务 → 岗位与事实）、看日报（`data/reports/<日期>.md`）、环境检查、暂停／恢复。
- [ ] **Step 2: 提交** `git commit -m "Map workbench menus to pages and code"`

### Task 6: 上线与实看

- [ ] **Step 1**：确认任务当前状态（管理员 19:03 手动暂停）；保持暂停，按 CLAUDE.md 流程重建 → 草案 → 验收（mode=validate）→ 发布；若恢复被要求环境检查，先跑 `/api/preflight`。**恢复任务由管理员决定**，本计划不自动恢复。
- [ ] **Step 2**：浏览器打开工作台，逐项核对：首页四块与数字（与 `data/reports/<今天>.md` 共有列一致）、菜单分组与“高级”折叠、岗位页标题与“所在任务”列、FDE交付工程师 只读与“去修改”跳转、待办“去处理”选中正确条目；手机宽度无横向滚动。
- [ ] **Step 3**：把核对结果与截图要点报告给管理员。
