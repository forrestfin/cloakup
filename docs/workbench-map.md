# 工作台功能对照

菜单 → 页面 → 代码对照，给管理员和后续维护者查“这个按钮在哪、背后是哪段代码”。工作台地址 `http://127.0.0.1:8766`（容器内服务；本机直接运行时默认 8765）。

前端是三层：`cloakup/web/index.html` 放菜单（`data-nav="<view 名>"`），`app.js` 的 `render()` 用 `views` 表把 view 名对到页面函数，页面函数分在 `platform.js`、`sourcing.js`、`agents.js`。所有页面都靠 `GET /api/state`（`workbench.Workbench.snapshot`）取数，每次写操作走 `POST`，路由都在 `cloakup/workbench.py` 的 `do_GET`／`do_POST`。

## 菜单对照

| 菜单 | 页面（view） | 主要功能 | 前端函数 | 后端接口 | Python 模块 |
|---|---|---|---|---|---|
| 今天 | `operations` | 各任务今天的状态与执行时段、按岗位的今日数字（首聊额度、来信、回复、简历、打招呼、待回）、需要你处理、最近动态 | `workspaceHome`（无任务时退回 `legacyHome`），`platform.js` | `GET /api/state` 里的 `today` | `today.py`（`build`，由 `workbench.today_view` 调用）、`attention.py` |
| 任务 | `tasks` | 任务目录；新建／编辑任务；任务内分页：概览（执行时间、岗位加入／撤下／移除／权重、问候语就绪）、执行设置、岗位与事实、候选人与结果、运行记录；启动／暂停／恢复 | `taskList` → `taskEditor`／`taskWorkspace`、`taskJobsPanel`、`taskPublishedFacts`、`taskFactsEditor`、`mustHavePanel`（必须项，`agents.js`）、`taskControls`，`platform.js` | `POST /api/tasks`、`/api/tasks/<id>`、`/api/tasks/<id>/jobs`（含 `action=must_haves`）、`/api/tasks/<id>/must-haves/split`、`/api/tasks/<id>/agent-workspace/(draft｜test｜publish)`、`/api/account-tasks/<id>`、`/api/runs`、`/api/runs/<id>/action`、`/api/tasks/<id>/greetings/<岗位>/recheck`；`GET /api/tasks/<id>/history`、`/api/tasks/<id>/jobs/changes` | `workbench.py`（`save_definition`、`account_task_action`、`action`）、`account_execution.py`、`agent_workspace.py`、`job_changes.py`、`greeting_policy.py`、`task_policy.py`、`job_sourcing.py`（必须项）、`screening.py`（首聊前初筛） |
| 待办 | `attention` | 待办列表（待处理／已处理／全部）、详情、标记处理、补答岗位问题（进岗位 FAQ）、重试决策、核验动作 | `attentionView`、`answerTodoForm`、`actionVerificationForm`，`platform.js` | `POST /api/attention/<id>`、`/api/attention/<id>/answer`、`/api/tasks/<id>/agent-workspace/todo-answer`、`/api/cases/<id>/decision-retry`、`/api/actions/<id>/verify` | `attention.py`（`reconcile` 生成和关闭）、`faq_answers.py`、`planning_dialogue.py`、`workbench.update_attention` |
| 岗位 | `sourcing` | 岗位列表；新增岗位（从 BOSS 刷新岗位列表、绑定）；岗位详情：JD、问候语、已确认事实、FAQ、答复预览、自动化开关、简历归档。已加入已发布任务的岗位只读（含必须项清单与近 7 天初筛统计），提示去任务的“岗位与事实” | `sourcing`、`bossJobPicker`、`jobDetail`、`mustHaveReadonly`（`agents.js`），`sourcing.js` | `GET /api/boss/jobs`；`POST /api/boss/jobs/refresh`、`/api/boss/jobs/bind`、`/api/cases/<id>/config`、`/api/cases/<id>/autonomy`、`/api/cases/<id>/answer-preview`、`/api/cases/<id>/resume-archive` | `browser_errands.py`、`boss_jobs.py`、`job_config.py`（`save_config`、`locked_task`／`locked_tasks`）、`answering.py`、`facts.py`、`faq.py`、`resume_archive.py` |
| 执行环境 | `environments`（“健康检查”页签和 `readiness` 用同一函数） | 账号与浏览器：浏览器席位容量、执行器状态（接管／交还）、BOSS 登录、打开执行浏览器；健康检查：环境与执行器核验 | `environmentsView`、`browserCapacityPanel`、`runtimePanel`、`readinessView`，`platform.js` | `POST /api/browser-capacity`、`/api/preflight`、`/api/runs`（`browser_open`）、`/api/runs/<id>/action`、`/api/tasks/<id>/browser-diagnostics`；`GET /api/readiness/<id>`、`/api/health` | `capacity.py`、`workbench.test_environment`、`executor_check.py`、`browser_diagnostics.py`、`access.py` |
| 系统设置 | `settings` | 执行指标、LLM 运行时、模型与存储、全局沟通规则、外部集成（只读展示为主） | `workspaceSettings`、`executionMetricsPanel`，`platform.js` | `GET /api/state` | `workbench.snapshot`、`task_notifications.py`、`communication_policy.py` |
| 高级 → 场景模板 | `templates` | 场景（网站＋业务场景）列表、新建场景、应用到任务 | `scenarioTemplates`，`agents.js` | `POST /api/scenarios`、`/api/scenarios/<id>/(save｜apply)` | `scenario_workspace.py`、`scenarios.py` |
| 高级 → 规划端 | `planner` | 场景的目标与执行策略、和规划代理对话 | `planningAgent` → `scenarioPage('planner')`，`agents.js` | `POST /api/scenarios/<id>/(session｜chat｜save)` | `scenario_workspace.py`、`prompts.py` |
| 高级 → 执行端 | `executor` | 场景的基础能力开关、模拟测试上下文 | `executionAgent` → `scenarioPage('executor')`，`agents.js` | `POST /api/scenarios/<id>/(context｜session｜chat｜save)` | `scenario_workspace.py` |

不在菜单里、从别处进入的页面：`execution`（运行详情，`platformExecution`／`runView`／`platformRun`，可下载 `GET /api/runs/<id>/trace`，画面 `GET /api/runs/<id>/screenshot`）、`chat`（候选人与结果，`communicationHistory`）、`audit`（业务审计，`auditView`，数据来自 `/api/state`；单岗位审计另有 `GET /api/cases/<id>/audit`）、`readiness`（健康检查单页）。顶栏“待办 N”按钮也进 `attention`。

## 常见操作在哪

- **看今天**：菜单“今天”。数字来自 `today.py`，首聊按额度占位计（含回执不明），“待回”是此刻的会话数。
- **处理待办**：菜单“待办”（或“今天”里“需要你处理 → 去处理”，或顶栏“待办 N”）。选中一项看详情，补答或标记处理；状态解除后 `attention.reconcile` 会自动关闭。
- **加岗位**：菜单“岗位” → “新增岗位” → “刷新岗位列表”（从 BOSS 读取，浏览器后台执行） → 勾选后“绑定所选岗位” → 点岗位名进入配置（问候语、事实、FAQ），“保存配置” → 菜单“任务” → 打开任务 → “概览”的岗位区“加入岗位”（填权重）。加入会先做本岗位的隔离验收，通过后在安全点生效。
- **改问候语／事实**：
  - 岗位还没加入已发布任务：菜单“岗位” → 点岗位名 → 改完“保存配置”。
  - 已加入任务：岗位页只读，点提示里的“去修改”，或菜单“任务” → 打开任务 → “岗位与事实” → “编辑岗位资料与寻访配置”。保存会生成草案并验收，通过后发布生效。
- **维护必须项**：菜单“任务” → 打开任务 → “岗位与事实” → 岗位的“必须项”：可直接增改，或“录入客户反馈”让模型拆条（`POST /api/tasks/<id>/must-haves/split`，只预览）→ 确认后保存（`POST /api/tasks/<id>/jobs`，`action=must_haves`）。先过本岗位隔离验收，在安全点生效，不中断任务。岗位页只读显示清单与近 7 天初筛统计，点“去修改”回到这里。初筛代码在 `screening.py`。
- **看日报**：文件 `data/reports/<日期>.md`；接口 `GET /api/daily-reports`（列表）、`GET /api/daily-reports/<日期>.md`（原文）、`POST /api/daily-reports/<日期>`（重算某天）。代码在 `daily_report.py`。工作台页面里还没有日报入口。
- **环境检查**：菜单“执行环境” → “健康检查”页签 → “快速测试”／“重试未通过项”（`POST /api/preflight`）。也可以 `curl 127.0.0.1:8766/api/health` 看服务和数据库。
- **暂停／恢复任务**：菜单“任务” → 打开任务，页面右上角“暂停”／“继续”（账号执行器下的任务发 `POST /api/account-tasks/<id> {"action":"pause"|"resume"}`；独立运行发 `POST /api/runs/<id>/action`）。也可在“今天”点“去任务”进入。换版本后的恢复流程见 `CLAUDE.md` 的“发布签名”。
