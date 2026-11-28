# CLAUDE.md

## 项目概述
本机运行的 BOSS 直聘寻访自动化工作台：CloakBrowser 操作网页，DeepSeek Harness（Anthropic 兼容网关）做决策，确定性执行网关负责授权、去重与回执核验。Python 3.12，Postgres，Docker 部署，前端为原生 JS（`cloakup/web/`）。概况见 `README.md`，策略来龙去脉见 `docs/architecture-boundaries.md`。

## 常用命令
- Python 只用项目 `.venv`（uv 管理），不要用全局 Python：`.venv/bin/python`
- 运行单个测试：`.venv/bin/python -m pytest tests/test_followup.py::test_xxx -q`
- 运行全部单元测试：`.venv/bin/python -m pytest tests -q`（不依赖真实凭据；`tests/e2e_*.py` 需真实浏览器，不在默认集）
- 静态检查：`.venv/bin/ruff check .`（只拦 E9/F/B，无格式检查、无类型检查）
- 用新代码重建服务：`.venv/bin/python scripts/docker_up.py --reuse-runtime`（约 1 分钟，重建会中断任务）
- 日志：`docker compose logs app`；结构化日志 `data/logs/<event|state|action|model|error>-<日期>.jsonl`；日报 `data/reports/<日期>.md`；健康 `curl 127.0.0.1:8766/api/health`

## 目录结构
- `cloakup/autopilot.py`：账号执行器主循环（收件同步 → 回复 → 首聊），`sync_and_follow`、`dispatch_reply`
- `cloakup/decision_runtime.py`：每个会话的动作准入（`capabilities`）、程序直办（`direct_proposal`）、Harness 决策与暂缓（`hold`）
- `cloakup/answering.py`、`faq.py`、`facts.py`：静态答复路由；岗位依据分两处：已确认事实 + 岗位 FAQ（管理员补答进 FAQ）
- `cloakup/conversation_semantics.py`：对话理解与脱敏 `redact`；`followup.py`：追聊与体验底线；`resume_closure.py`：收简历致谢
- `cloakup/screening.py`：首聊前初筛，脱敏资料逐条对照岗位必须项（符合／不符合／看不出），明确不符合就不开聊
- `cloakup/attention.py`：管理员待办（由真实状态推导，`reconcile`）；`agent_workspace.py`：草案、隔离验收、发布与签名
- `cloakup/workbench.py`：HTTP 服务、任务管理、重启恢复；`boss_*.py`：BOSS 页面适配；`logbook.py`：结构化日志
- `tests/test_<模块>.py`：与模块一一对应
- 菜单 → 页面 → 代码对照：docs/workbench-map.md

## 不能破坏的约束（测试都有覆盖）
1. **先核验再发送**：发送前核实当前会话收件人与岗位；核不实就只放下这一条。
2. **送达以回执为准**：拿不到回执记为 `delivery_unknown`，不自动重发。
3. **发送先占位**：写平台的动作先在 `operations`／`action_leases` 占位。
4. **恢复轮次只读**（`autopilot.recovery_pass`），只允许同意接收候选人主动发起的简历。
5. **单条问题不停整单**：只有页面级问题（登录失效、平台验证、未发送草稿、页面换了）才暂停任务。
6. **发布走草案与隔离验收**：运行中的任务不直接换版本；例外是 `faq_answers.apply`、`job_changes.apply` 与 `job_changes` 的 `must_haves`（必须项）在安全点换绑，新岗位和必须项改动须先通过本岗位的隔离验收（必须项 2026-11-27 管理员决定）。
7. **候选人身份不进模型**：`redact` 隐去姓名、称呼、联系方式、证件／平台标识、年龄性别、附件文件名；单位、学校、城市保留；只隐去、不整段拒绝（2026-11-24 管理员确认）。Harness 决策载荷只含意图标签与资料状态（首聊前初筛另加 `screening.passed`／`unclear_count`）。首聊前初筛（`screening.py`）把经 `redact` 脱敏的资料正文送模型，是独立调用、不进 Harness 载荷，不另设授权记录（2026-11-27 管理员确认）。
8. **岗位信息只作为参数**：代码、正则、提示词里不写死任何岗位或行业的词。
9. **不让候选人不适**：追聊受 `followup.py` 顶部底线约束（无回复者最多 1 次、回复后沉默最多 2 次、未读不追、已读后 ≥72 小时、只在 9–20 点），配置只能更严。
10. **薪酬不报数字**：只说已确认的结构，数字类追问回“进一步详聊”（`communication_policy.SALARY_POLICY`）。

## 代码规范
- 跟随所在文件既有写法（大量紧凑单行），不顺手大面积重排格式。
- 面向管理员的提示、事件消息、待办用中文，说清“发生了什么、做了什么、要不要人处理”。
- 重试、冷却、等待用 `pacing.jitter`／`lognormal`，必须有上限。
- 会暂停任务、改变执行阶段或写平台的新路径，同时写 `logbook.log`；暂停带 `actor` 和 `reason`。
- 改 `autopilot.py`、`selection_recovery.py`、`workbench.py` 的事件文字时，同步 `daily_report.EVENT_RULES` 和 `tests/test_daily_report.py`。
- 需要管理员处理的状态必须在 `attention.reconcile` 生成待办，并在状态解除后自动关闭；不要只写一行日志或原因。

## 工作流规则
- 先定位根因再改：修 bug 先写能复现的测试（测试名或注释写明日期和现场，如“11-22 候选人 30”）。
- 每批改动跑全量单测和 ruff，一批一个提交。
- 涉及多个文件或方案不确定（尤其隐私、追聊、发送规则）时，先给方案等确认。
- 发给候选人的新话术、补发或更正，上线前把原文给管理员确认。
- 会给候选人发消息的改动，上线前先用新代码对线上数据只读演练影响范围（见补充说明）。
- 不改与当前任务无关的代码，发现的问题单独列出；新增依赖先征求同意。
- 已启用 Superpowers 插件，按场景使用其技能：排查线上异常用 `systematic-debugging`；修 bug、加功能用 `test-driven-development`（先写失败的复现测试）；多文件或规则类改动先用 `brainstorming` → `writing-plans` 出方案，确认后用 `executing-plans`；宣布“已修好／已上线”前用 `verification-before-completion`（以测试输出、日志、回执为证据）；合入前用 `requesting-code-review`。
- 与本文件冲突时以本文件为准：不能破坏的约束、上线流程（发布签名、隔离验收）、给候选人发消息前确认原文，都优先于插件的通用建议；不用 git worktree 并行改线上相关代码（同一时间只有一个维护方）。

## Git 约定
- 当前工作分支 `cursor/resume-text-invite-policy`；不要直接推送 `main`，不要 `--force`。
- 提交信息用英文祈使句标题 + 正文写清根因与影响（沿用 `git log` 风格），不加署名行。
- 计划新建**私有**仓库推送完整历史（`gh repo create <名称> --private --source . --push`），原远端改名 `old`。

## 禁止事项
- 不提交 `data/`（数据库密码、飞书凭据）、`.env*`、`.tmp_incident/`。
- 不做以规避平台机器人检测为目的的改动；现有随机节奏原样保留，不朝“更难识别”扩展。
- 不手工改线上数据库里的候选人记录来“修数据”；用代码里的幂等恢复逻辑在执行器安全点处理。

## 补充说明
- **发布签名**：`agent_workspace.signature` 含 30 个执行文件的哈希。改了这些文件，重建后已发布版本失效，自动恢复会失败（`auto_resume_failed:release_changed`，并生成待办）。上线流程：重建 → `POST /api/tasks/<id>/agent-workspace/draft` → `.../test`（mode=validate，9 个场景）→ `.../publish` → `POST /api/account-tasks/<id> {"action":"resume"}`，写请求带 `X-Cloakup-Token`（取自 `/api/state`）。多个改动尽量合并一次上线。
- **容器内读线上数据**：`DATABASE_URL` 只在入口进程里设置；脚本要自己用 `/run/secrets/postgres_password` 组装，否则会连到 `/data/data/cloakup.db` 旧 SQLite（空）。脚本放 `.tmp_incident/`，`docker compose cp` 进容器运行。
- **只读演练**：把本地 `cloakup/` 拷到容器 `/tmp/newpkg`（`chmod -R a+rX`），脚本里 `sys.path.insert(0,'/tmp/newpkg')`，对线上数据只读计算谁会收到什么。
- **核对“系统没回答”**：已确认事实和岗位 FAQ 两处都要查；“确认收到、等待核实”不是回答（`decision_runtime.answer_sent`）。
- **执行时段**：任务有自己的时段（目前 9:00–12:00、13:30–23:30），时段外不操作网站；追聊另受 9–20 点限制。每天首聊额度是上限不是目标（2026-11-27 管理员：宁缺毋滥）；账号每天最多打开 80 份推荐资料，按岗位权重分。
- **沟通页卡在“加载中”**：`boss_chat.open_menu` 会有冷却地重新打开一次；仍不行再用 noVNC（`127.0.0.1:6080`，默认 `view_only=1`）查看。
- 用户说“看一下日志”时，读 `data/logs/`、`data/reports/`，结论写到 `data/analysis/<日期>-<时>.md`。
- 待办：拆分 `run_account_executor`、`sync_and_follow` 等超大函数；日报可选飞书摘要、日报页面、读取 operations／action_leases；候选人 30 的“上班时间”作息尚无 FAQ。
