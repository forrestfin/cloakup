# 一个寻访任务多岗位 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal：** 一个 BOSS 寻访任务同时寻访多个岗位，岗位按权重共用任务每日首聊额度；岗位可从 BOSS 职位管理列表勾选加入（只验收新岗位、安全点并入）或撤下，任务不暂停。

**Architecture：** 任务配置新增岗位成员表（权重、是否参与首聊）；验收结果从"整包签名"改为按岗位复用（运行时签名 + 任务摘要 + 岗位摘要）；岗位加减走仿照 `faq_answers` 的"派生版本 → 排队 → 执行器安全点原子换绑"；执行器首聊改由额度调度选岗位；BOSS 新增职位列表只读读取、推荐页岗位切换，运行中由执行器在安全点代办。

**Tech Stack：** Python 3.12、项目 `.venv`、pytest、Postgres/SQLite 文档库（`storage.Database`）、CloakBrowser/Playwright、原生 JS 前端（`cloakup/web/`）。

**Spec：** `docs/superpowers/specs/2026-11-24-multi-job-task-design.md`

## Global Constraints

- CLAUDE.md 的 10 条"不能破坏的约束"全部适用；本计划修改约束 6 的例外范围（Task 11 改 CLAUDE.md 原文）。
- 账号每日首聊硬上限 20（现 `daily_attempts` 全局占位计数）；任务 `daily_new_limit` 范围 0–20；岗位权重为整数 1–10，默认 1。
- 当天任务目标 = `limit × (0.75 + 0.25 × stable_fraction('daily_new', day_key(), task_id))` 四舍五入，limit>0 时至少 1。
- 额度计数按占位：`claimed_at` 当天（含 `delivery_unknown`）+ 平台外观察到的首聊，口径与 `autopilot.daily_attempts` 一致。
- 岗位名、行业词不进代码、正则、提示词（约束 8）；职位列表只在本地读，不进模型（约束 7）。
- 不改随机节奏、不朝"更难识别"扩展；BOSS 页面只读观察，不点修改控件。
- 跟随所在文件的紧凑单行写法；面向管理员的文字用中文，说清发生了什么、做了什么、要不要人处理。
- 暂停、改执行阶段、写平台的新路径写 `logbook.log`（带 actor、reason）；改 `autopilot.py`/`workbench.py` 事件文字时同步 `daily_report.EVENT_RULES` 与 `tests/test_daily_report.py`。
- 需要管理员处理的状态在 `attention.reconcile` 生成待办并自动关闭。
- 每个 Task 结束跑全量单测 `.venv/bin/python -m pytest tests -q` 与 `.venv/bin/ruff check .`，一个 Task 一个提交；提交信息英文祈使句标题 + 正文写根因与影响，不加署名行；分支 `cursor/resume-text-invite-policy`，不推 `main`、不 `--force`。
- 不新增依赖。

## Review Focus

1. 增量变更排队期间，执行器又发布了整单新版本（或 FAQ 换绑先生效）→ 加入请求必须基于最新生效版本重新组装，摘要不符就失败并生成待办，绝不覆盖新版本。（Task 4 `test_add_rebases_on_newer_release_or_fails`）
2. 同一天岗位撤下又加回、或当天新加入 → 当天任务与岗位已用额度不重置，不会超发。（Task 2 `test_withdraw_and_readd_same_day_keeps_usage`）
3. 撤下的岗位当天还有新会话进来 → 仍被本任务认领、回复照常；彻底移除后不再认领。（Task 5 `test_withdrawn_job_still_syncs_inbox_but_no_outreach`）
4. 推荐页岗位切换后 `verify_list` 核对失败 → 只暂缓该岗位首聊、标记当天不可用，其他岗位继续，任务不暂停。（Task 8 `test_switch_failure_defers_only_that_job`）
5. 旧任务（只有 `case_ids`、发布包无成员表、验收记录无 `covered`）升级后 → 能照常启动和运行，不误报 `release_changed`。（Task 1 `test_legacy_case_ids_become_active_members`，Task 3 `test_legacy_full_validation_still_ready`）

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `cloakup/job_members.py`（新） | 成员表的规范化、校验、查询；兼容旧 `case_ids` |
| `cloakup/job_quota.py`（新） | 任务当天目标、权重分配、按岗位计数、选下一个首聊岗位 |
| `cloakup/job_changes.py`（新） | 岗位加入/撤下/恢复/移除/调权重/改总额：派生版本、仅新岗位验收、排队与安全点应用 |
| `cloakup/boss_jobs.py`（新） | BOSS 职位管理列表只读读取 |
| `cloakup/browser_errands.py`（新） | 运行中由执行器在安全点代办的只读浏览器步骤（读列表、读 JD） |
| `cloakup/agent_workspace.py` | 签名拆分、按岗位判定验收有效 |
| `cloakup/account_execution.py` | `availability` 按岗位判定验收 |
| `cloakup/autopilot.py` | 首聊额度判定、调度选岗位、安全点调用变更与代办 |
| `cloakup/boss_recommend.py` | 推荐页切换到目标岗位 |
| `cloakup/platform.py`、`task_policy.py`、`job_config.py` | 成员表校验、任务级 `daily_new_limit` |
| `cloakup/attention.py` | 新增待办 |
| `cloakup/workbench.py` | API 路由 |
| `cloakup/daily_report.py` | 新事件规则 |
| `cloakup/web/agents.js`、`web/sourcing.js` | 岗位区：权重、撤下/恢复、从 BOSS 选择、额度展示 |

---

### Task 1：岗位成员表

**Files：**
- Create: `cloakup/job_members.py`
- Modify: `cloakup/platform.py:78-90`（sourcing 校验）、`cloakup/account_execution.py`（`case_ids()` 不变语义：所有成员都参与收件）
- Test: `tests/test_job_members.py`、`tests/test_platform.py`

**Interfaces：**
- Produces：
  - `normalize(cfg: dict) -> list[dict]`：返回并写回 `cfg['members']`，每项 `{'case_id':str,'weight':int,'outreach':'active'|'withdrawn'}`；无 `members` 时由 `case_ids` 生成（weight=1、active）；写回 `cfg['case_ids']=[m['case_id'] for m in members]` 保持旧字段一致。
  - `outreach_ids(cfg) -> list[str]`：`outreach=='active'` 的岗位。
  - `weights(cfg) -> dict[str,int]`：active 岗位的权重。
  - 校验错误抛 `ValueError`（中文）：成员 1–20 个、case_id 唯一、weight 为 1–10 的 int、outreach 取值合法。

- [ ] **Step 1：写失败测试** `tests/test_job_members.py`
  - `test_legacy_case_ids_become_active_members`：`{'case_ids':['a','b']}` → members 两项 weight 1 active，`case_ids` 不变。
  - `test_members_drive_case_ids_and_outreach`：members 中 b 为 withdrawn → `case_ids==['a','b']`、`outreach_ids==['a']`、`weights=={'a':w}`。
  - `test_invalid_members_rejected`（参数化）：weight 0 / 11 / '2'、重复 case_id、0 个或 21 个成员、outreach='x' → `ValueError`。
- [ ] **Step 2：运行确认失败** `.venv/bin/python -m pytest tests/test_job_members.py -q` → ImportError。
- [ ] **Step 3：实现 `job_members.py`；`platform.py` sourcing 分支在现有 `case_ids` 校验前调用 `normalize(cfg)`**（"岗位已由启用任务管理"等原检查沿用 `case_ids`）。
- [ ] **Step 4：`tests/test_platform.py` 加 `test_task_save_normalizes_members`：保存带 members 的任务后读出 members 与 case_ids 一致。**
- [ ] **Step 5：全量单测 + ruff 通过。**
- [ ] **Step 6：提交** `Add job membership with weights and outreach state to sourcing tasks`

### Task 2：额度计算与选岗位

**Files：**
- Create: `cloakup/job_quota.py`
- Modify: `cloakup/autopilot.py:240-251,588-593,674-678`
- Test: `tests/test_job_quota.py`

**Interfaces：**
- Consumes：`job_members.weights`、`job_members.outreach_ids`（Task 1）；`autonomy.day_key`、`pacing.stable_fraction`。
- Produces：
  - `ACCOUNT_DAILY_CAP = 20`
  - `task_target(task_id: str, limit: int, day: str) -> int`（公式见 Global Constraints）
  - `allocate(total: int, weights: dict[str,int]) -> dict[str,int]`：最大余数法；余数相同按 case_id 排序决定，结果稳定；和恰为 total。
  - `usage_by_case(store, case_ids: list[str], day: str) -> dict[str,int]`：与 `daily_attempts` 同口径，只统计给定岗位。
  - `pick_case(task_id, limit, weights, usage, exhausted: set[str], day) -> str | None`：
    1. 任务已用（usage 合计，含不在 weights 中的当天成员）≥ 目标 → None；
    2. 候选 = weights 中不在 exhausted 的岗位；
    3. 优先在 usage<份额 的候选中选 `usage/份额` 最小者（并列取权重大、再取 case_id 小）；
    4. 都没有份额剩余时，若任务未达目标，在候选中按 `usage/weight` 最小者选（让出的额度按权重再分）；
    5. 无候选 → None。
  - `account_exhausted(store, day) -> bool`：`daily_attempts(store,'first_contact') >= ACCOUNT_DAILY_CAP`（`daily_attempts` 增加可选 `day` 参数，默认今天）。
  - `allowed(store, task, case_id, day) -> bool`：账号未满、任务未达目标、且 `case_id` 是 `pick_case` 认可的岗位之一（用于 `first_contact` 入口的保守复核）。

- [ ] **Step 1：写失败测试**
  - `test_task_target_is_stable_and_bounded`：同 task/day 两次相等；limit=20 时在 15–20；limit=0 → 0；limit=1 → 1。
  - `test_allocate_by_weight_largest_remainder`：`allocate(10,{'a':3,'b':1})=={'a':8,'b':2}`；`allocate(7,{'a':1,'b':1,'c':1})` 合计 7、值为 {3,2,2} 且两次相同。
  - `test_pick_prefers_lowest_share_ratio`：份额 a=8,b=2，usage a=4,b=1 → 选 a 与 b 中比值低者（相等时权重大者 a）。
  - `test_exhausted_job_yields_share`：b 在 exhausted、a 份额已满但任务未达目标 → 选 a；a 也 exhausted → None。
  - `test_task_target_reached_returns_none`。
  - `test_withdraw_and_readd_same_day_keeps_usage`：当天 b 有 2 次占位后 b 从 weights 去掉再加回，usage 与任务合计不变，`pick_case` 不超目标。
  - `test_account_cap_blocks_every_task`：全账号当天占位 20 → `account_exhausted` True、`allowed` False。
  - `test_usage_counts_delivery_unknown_and_external_first_contacts`。
- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现 `job_quota.py`。**
- [ ] **Step 4：`autopilot.first_contact` 入口把 `daily_attempts(...)>=daily_new_target(case)` 换成 `not job_quota.allowed(store, task, case['id'], day_key())`**，task 由 `task_policy.effective_task(manager.db, case['id'])` 取得；无任务（旧 CLI）时退回 `daily_new_target` 原逻辑。保留 `daily_new_target` 供该兼容路径使用。
- [ ] **Step 5：任务级额度：`daily_new_limit` 仍在任务 `execution` 中、由 `overlay` 抄到岗位（不改 schema），此处只改为按任务读取。`tests/test_task_policy.py` 现有用例保持通过。**
- [ ] **Step 6：全量单测 + ruff；提交** `Share the daily first-contact quota across a task's jobs by weight`

### Task 3：验收结果按岗位复用

**Files：**
- Modify: `cloakup/agent_workspace.py:56-100,360-436`、`cloakup/account_execution.py:28-44`、`cloakup/faq_answers.py:98-147`
- Test: `tests/test_agent_workspace.py`、`tests/test_account_execution.py`、`tests/test_faq_answers.py`

**Interfaces：**
- Produces（`agent_workspace.py`）：
  - `runtime_signature() -> str`：现 `signature` 中除 `bundle` 外的全部输入的摘要。
  - `task_digest(bundle) -> str`：`conversation_prompt`、`plan`、`config` 去掉 `case_ids`、`members`、`execution.daily_new_limit` 后的摘要。
  - `source_check_digest(item) -> str`：sources 条目去掉 `faq` 后的摘要。
  - `coverage(bundle) -> dict`：`{'runtime':runtime_signature(),'task':task_digest(bundle),'sources':{cid:source_check_digest(item)}}`。
  - `signature(bundle)` 保留（整单发布仍用它核对草案未变）。
  - `validated_cases(db, task_id) -> set[str]`：取生效版本 bundle，对其每个岗位，在该任务所有 `status=='passed' and mode=='validate'` 且 `result.real_writes==0` 的 `agent_test` 中，找 `covered['runtime']`、`covered['task']` 与当前一致且 `covered['sources'][cid]` 与当前一致的记录；旧验收记录无 `covered` 时，按其 `bundle` 现算 `coverage` 并且要求 `signature(test.bundle)==test.signature`（即与当时代码一致仍有效）。
  - `assert_release_ready`：改为"生效版本中所有岗位 ∈ `validated_cases`"，否则：运行时或任务摘要不符 → `StartBlocked(...,'release_changed')`（原文案），仅部分岗位不符 → `StartBlocked('岗位「…」的验收已失效，请重新验收该岗位','job_not_validated')`。去掉 `tested_version_id`/`without_faq` 的专门分支（FAQ 已不在摘要里），但保留 `faq_answers` 写入的字段以兼容历史数据。
  - `start_test` 写入 `record['covered']=coverage(bundle)` 且 `record['case_ids']`=本次覆盖的岗位（整单验收 = 全部岗位）。
- Produces（`account_execution.py`）：`availability(cid)` 中 `release_id` 不一致时的 `release_changed` 判定改为：生效版本即成员绑定版本时，再判 `cid in validated_cases(...)`，不在则返回 `{'allowed':False,'state':'job_not_validated','reason':'该岗位验收已失效，只停这个岗位','next_at':None}`；版本不一致仍为 `release_changed`（增量变更会在同一事务换绑，Task 4）。

- [ ] **Step 1：写失败测试**（`tests/test_agent_workspace.py`，复用 `passing_test`，给它加 `covered` 字段）
  - `test_legacy_full_validation_still_ready`：无 `covered` 的旧记录 + 代码未变 → ready。
  - `test_faq_change_keeps_validation`：只改某岗位 FAQ → ready。
  - `test_limit_or_weight_change_keeps_validation`：改 `daily_new_limit`、members 权重 → ready。
  - `test_source_change_invalidates_only_that_job`：改岗位 b 的 facts → `validated_cases` 不含 b、含 a；`assert_release_ready` 抛 `job_not_validated`。
  - `test_runtime_change_invalidates_all`：monkeypatch `runtime_signature` → 抛 `release_changed`。
  - `tests/test_account_execution.py::test_unvalidated_job_stops_alone`：a 可发送，b 返回 `job_not_validated`。
- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现上述接口；`publish` 的现有校验保持（整单流程不变）。**
- [ ] **Step 4：`tests/test_faq_answers.py` 现有 7 个用例全部通过（`test_derived_release_stays_startable_only_if_it_differs_by_faq` 断言改为：只差 FAQ → ready；facts 不同 → `job_not_validated`）。**
- [ ] **Step 5：autopilot 的 `stage` 汇总把 `job_not_validated` 归为"部分岗位验收失效"事件：`'部分岗位验收已失效，这些岗位暂停发送，其他岗位继续'`；同步 `daily_report.EVENT_RULES` 加 `('release','job_not_validated',('msg','部分岗位验收已失效'))` 与对应测试。**
- [ ] **Step 6：全量单测 + ruff；提交** `Reuse validation per job so unchanged jobs keep their acceptance`

### Task 4：增量岗位变更

**Files：**
- Create: `cloakup/job_changes.py`
- Modify: `cloakup/workbench.py`（路由）、`cloakup/autopilot.py:855-858`（安全点）
- Test: `tests/test_job_changes.py`

**Interfaces：**
- Consumes：`job_members.normalize`（T1）、`agent_workspace.coverage/validated_cases/source/default_fixtures/simulate_bundle`（T3）、`faq_answers` 的提交模式（`db.put_batch` + `account_task` 换绑）。
- Produces：
  - `KIND='job_change'`；记录 `{id, task_id, action, case_id?, payload, base_version_id, status:'testing'|'pending'|'applied'|'failed', test_id?, at, error?}`。
  - `request(manager, task_id, body) -> dict`：`body.action ∈ {'add','withdraw','resume','remove','weights','limit'}`。
    - `add`：`case_id` 必须是已绑定 JD（`case['job']['snapshot']` 非空）且管理员已在岗位确认页保存过问候语与寻访配置（`job_sourcing` 存在且 `config_revision>0`，沿用现有 `job_config` 保存流程；实现前核对 `job_config.py:44-45` 对未入任务岗位不拦截）的岗位，否则 `ValueError('请先确认该岗位的问候语、事实和寻访条件')`；校验不被其他未停止任务管理、全账号无同名；以生效版本派生 bundle（追加 `agent_workspace.source(case)` 与成员 `{weight:payload.weight or 1,'outreach':'active'}`），只用该岗位的 `default_fixtures` 启动验收（`mode='validate'`，`case_ids=[cid]`，写 `covered`）；状态 `testing`，验收通过后转 `pending`，失败转 `failed`。
    - `remove`：仅当该岗位无进行中会话（候选人非 TERMINAL 且有 `conversation`）时允许，否则 `ValueError('该岗位还有进行中的会话，请先撤下，等会话结束后再移除')`。
    - `withdraw`/`resume`/`weights`/`limit`：直接 `pending`；`limit` 校验 0–20 int；`weights` 校验 1–10。
  - `apply_pending(manager) -> int`：仿 `faq_answers.apply_pending`；对每条 `pending`：取**当前**生效版本重新组装（不用 `base_version_id` 的 bundle 原样覆盖）；`add` 时若 `coverage` 的 runtime/task/该岗位 source 与验收记录 `covered` 不一致 → `failed`；同一事务写 `agent_bundle`（`origin='job_change'`）、`agent_release`、`task`（members/case_ids/limit）、受影响 `case`（`overlay` 后）与运行中 `account_task`（`case_ids`、`release_id` 换绑，history 记 `reason='job_change:<action>'`）。任务未运行时 `request` 直接调用 `apply`。
  - 每次 apply 后 `logbook.log('state',type='job_change',task_id,case_id,action,actor='admin',reason=...)`，返回值供执行器发事件 `'岗位变更已在安全点生效：<动作>'`。
  - 路由：`POST /api/tasks/<id>/jobs`（body 同 `request`）、`GET /api/tasks/<id>/jobs/changes`。写请求带 `X-Cloakup-Token`（沿用现有校验）。
  - `autopilot.run_account_executor` 每轮在 `apply_faq_answers` 之后调用 `job_changes.apply_pending`，并且恢复轮次（`recovery_pass`）中不调用（约束 4）。

- [ ] **Step 1：写失败测试** `tests/test_job_changes.py`（fixture 仿 `tests/test_faq_answers.py::setup`，两个岗位）
  - `test_add_runs_only_new_job_fixtures`：发起 add 后测试记录 `fixtures` 只含新岗位 id 前缀。
  - `test_add_applies_at_safe_point_and_rebinds_running_task`：模拟验收通过、任务 running → `apply_pending` 后 `account_task.release_id` 为新版本、`case_ids` 含新岗位、`assert_release_ready` 通过。
  - `test_add_rebases_on_newer_release_or_fails`：排队期间先应用一条 FAQ 答案 → add 仍成功且保留该 FAQ；改动任务 `conversation_prompt` 后 → add 为 `failed` 并生成待办。
  - `test_add_rejects_duplicate_title_across_account`、`test_add_rejects_job_owned_by_other_task`。
  - `test_withdraw_keeps_membership_and_stops_outreach`：withdraw 后 `case_ids` 仍含该岗位、`outreach_ids` 不含。
  - `test_remove_requires_no_open_conversations`。
  - `test_weights_and_limit_need_no_validation`。
  - `test_recovery_pass_does_not_apply_changes`。
- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现 `job_changes.py`、路由与安全点调用。**
- [ ] **Step 4：`daily_report.EVENT_RULES` 加 `('job_change','applied',('msg','岗位变更已在安全点生效'))` 及测试。**
- [ ] **Step 5：全量单测 + ruff；提交** `Add and withdraw jobs on a running task through incremental releases`

### Task 5：执行器按额度调度首聊岗位

**Files：**
- Modify: `cloakup/autopilot.py:982-1035`
- Test: `tests/test_account_execution.py`

**Interfaces：**
- Consumes：`job_quota.pick_case/usage_by_case/account_exhausted`（T2）、`job_members.weights/outreach_ids`（T1）。
- Produces：首聊段改为：对每个运行中任务，`exhausted = job['outreach_exhausted'].get(day, set())` ∪ 当前 `held`/不可发送/`due` 未到/有隔离会话的岗位；`cid = pick_case(...)`；对该岗位跑一批（现有批次代码不变，含 1–3 次随机）；批次中 `first_contact` 因推荐页无可联系卡片返回 False 且本批 `confirmed==0` 时，把 cid 记入当天 `outreach_exhausted`（存 `job` 上，按 `day_key()` 分键，次日自然失效），事件 `'该岗位今天推荐页暂无可联系的人，额度让给其他岗位'`。删除 `if confirmed:break`，每轮每个任务至多一批，批次后照旧返回收件检查。
- `first_contact` 需区分"没人可联系"与"其他原因返回 False"：返回值改为 `'contacted' | 'no_candidates' | 'blocked'`（调用处 `if not await first_contact(...)` 改为 `!= 'contacted'`；`autopilot.py:809` 的单岗位旧路径同步）。

- [ ] **Step 1：写失败测试**（`tests/test_account_execution.py`，桩 `first_contact`）
  - `test_outreach_picks_job_by_weight_share`：权重 a=3,b=1、目标 8 → 多轮后批次分配接近 6:2，任务合计不超目标。
  - `test_exhausted_job_yields_to_others_until_next_day`。
  - `test_withdrawn_job_still_syncs_inbox_but_no_outreach`：撤下的 b 被 `sync_and_follow` 处理、从不进入首聊。
  - `test_no_round_ends_after_first_success`：a 成功后同一轮 b 所在的另一任务仍可调度。
- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。**
- [ ] **Step 4：同步 `daily_report.EVENT_RULES` 加 `('outreach','job_exhausted',('msg','额度让给其他岗位'))` 及测试。**
- [ ] **Step 5：全量单测 + ruff；提交** `Schedule first-contact batches across jobs by weighted quota`

### Task 6：真实页面只读观察（职位管理列表、推荐页岗位切换）

需要管理员在场、登录态正常、在任务执行时段内；任务先暂停（避免与执行器抢浏览器）。只导航和读取。

**Files：**
- Create: `tests/fixtures/boss_job_list.html`、`tests/fixtures/boss_recommend_job_switch.html`（脱敏：职位名改为"岗位甲/岗位乙"，去掉 encryptId 真值换成假值、去掉人名与公司名）
- Create: `docs/design/boss-job-pages.md`（记录观察到的 URL、frame、选择器、状态取值、切换控件的交互步骤）

> 11-24 调整：执行浏览器没有调试端口，外部读不到 DOM。改为新增只读诊断 `read_job_pages`（`cloakup/job_page_probe.py`，提交 b2e4445、d073b0d、e8bbac8），随整个分支在 Task 11 上线后、管理员在场时执行一次：
> 暂停全部任务 → `POST /api/tasks/<id>/browser-diagnostics {"action":"read_job_pages"}`（带 `X-Cloakup-Token`）→ 完整结构在容器 `/data/analysis/job-pages-*.json`（命名卷，不在宿主 `data/`，用 `docker compose cp app:/data/analysis/<文件> .tmp_incident/` 取出）。之后按采集结果做 Task 7、8，再第二次上线。
> 已知局限：只展开下拉、不选选项，`jobid` 不会变化；选项上多半没有 encryptId，“选项 ↔ 岗位”的对应关系可能要在 Task 8 有人在场时再确认。

- [ ] **Step 1：通过 noVNC 或执行浏览器打开职位管理页，用 `session.observe` / frame 读 DOM，记录：列表所在 frame URL、每行的职位名、encryptId 来源（链接或 data 属性）、城市、状态文本取值、分页方式。**
- [ ] **Step 2：打开推荐牛人页，记录岗位选择控件：所在 frame、展开方式、选项文本与 encryptId 的对应、切换后 frame URL 中 `jobid` 的变化。**
- [ ] **Step 3：保存脱敏 fixture 与 `docs/design/boss-job-pages.md`；请管理员确认无真实标识后提交** `Record read-only observations of BOSS job list and recommend job switcher`

### Task 7：读取 BOSS 职位列表与运行中代办

**Files：**
- Create: `cloakup/boss_jobs.py`、`cloakup/browser_errands.py`
- Modify: `cloakup/workbench.py`（路由；`boss_bind` 支持 `params.encrypt_id`）、`cloakup/autopilot.py`（安全点调用代办）
- Test: `tests/test_boss_jobs.py`、`tests/test_browser_errands.py`

**Interfaces：**
- Produces：
  - `boss_jobs.JOB_LIST_URL`（按 Task 6 记录）；`parse_job_list(html_or_observation) -> list[dict]`，每项 `{'encrypt_id':str,'title':str,'city':str,'status':'open'|'closed'|'other','status_text':str}`；`async read_job_list(session) -> list[dict]`（翻完所有页，有上限 20 页）。
  - `boss_jobs.job_edit_url(encrypt_id) -> str`：`https://www.zhipin.com/web/chat/job/edit?encryptId=<id>`（复用 `validate_job_url` 校验）。
  - `browser_errands.KIND='browser_errand'`；`request(manager, kind:'job_list'|'job_bind', params) -> dict`；`async run_pending(manager, job, session) -> int`：只在执行器安全点、`can_send` 至少一个岗位（即在执行时段内）时执行；`job_list` 结果存记录 `result`，`job_bind` 走现有 `boss_bind` 逻辑（抽成 `workbench.bind_job(manager, session, job_url)` 复用）。失败只标记该条。
  - 路由：`GET /api/boss/jobs`（返回最近一次列表 + 标注 `in_task`/`owned_by_other_task`/`closed`）、`POST /api/boss/jobs/refresh`（未运行任务时直接以 `boss_*` 模式执行，运行中则建代办）、`POST /api/boss/jobs/bind`（body `{encrypt_ids:[...]}`，仅 `open` 状态可选）。
- [ ] **Step 1：写失败测试**：`test_parse_job_list_from_fixture`（字段与状态）、`test_closed_jobs_not_bindable`、`test_errand_waits_for_safe_point_and_window`（时段外不执行）、`test_errand_failure_marks_only_itself`、`test_bind_by_encrypt_id_builds_edit_url`。
- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。**
- [ ] **Step 4：全量单测 + ruff；提交** `Read published jobs from BOSS and bind them without pausing the task`

### Task 8：推荐页切换到目标岗位

**Files：**
- Modify: `cloakup/boss_recommend.py:53-75`
- Test: `tests/test_boss_recommend.py`

**Interfaces：**
- Produces：`async switch_job(session, job_id: str, title: str) -> None`：读当前 frame URL `jobid`，等于目标则返回；否则按 Task 6 记录的控件选择目标岗位，等待 frame 重新加载，失败抛 `SourcingError('推荐页面未能切换到当前岗位，已暂缓该岗位开聊')`。`open_list` 在点"推荐"后、`verify_list` 前调用。点击使用现有 `_click`（节奏不变）。
- [ ] **Step 1：写失败测试**（用 Task 6 fixture 的桩 session）：`test_open_list_switches_to_target_job`、`test_switch_skipped_when_already_on_job`、`test_switch_failure_defers_only_that_job`（异常文字不含"登录/校验/验证/过于频繁/账号存在异常"，因此 `autopilot` 只 `due` 该岗位）。
- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。**
- [ ] **Step 4：全量单测 + ruff；提交** `Switch the recommend page to the job being sourced`

### Task 9：待办

**Files：**
- Modify: `cloakup/attention.py`（`reconcile`）
- Test: `tests/test_attention.py`

生成并在状态解除后自动关闭：
- `job_change_failed`：`job_change.status=='failed'` → "岗位「X」加入未完成：<原因>。已做：未改动正在运行的任务。需要：查看失败场景后重试或放弃。"（重试或标记放弃后关闭）
- `job_change_stuck`：`pending` 超过 30 分钟且任务运行中 → "岗位变更等待执行器安全点超过 30 分钟……"
- `job_not_validated`：某运行中任务的岗位不在 `validated_cases` → "岗位「X」验收已失效，已只停这个岗位的发送；需要重新验收该岗位。"
- `job_outreach_blocked`：同一岗位当天推荐页切换/核对失败 ≥3 次 → "只停这个岗位的首聊"。
- `job_removable`（提示级）：已撤下且无进行中会话。

- [ ] **Step 1：写失败测试**，每种待办一个"出现"+"解除后关闭"用例。
- [ ] **Step 2：运行确认失败。**
- [ ] **Step 3：实现。**
- [ ] **Step 4：全量单测 + ruff；提交** `Raise admin todos for job changes and per-job validation`

### Task 10：前端

**Files：**
- Modify: `cloakup/web/agents.js:75-90`、`cloakup/web/sourcing.js`、`cloakup/web/platform.js:313-321`
- Test: `tests/e2e_agent_workspace_ui.py`（拦截全部 API 写入的浏览器测试，按项目现有做法；不在默认单测集）

- 任务岗位区每行：岗位名、权重（1–10）、今日份额/已用、状态（参与首聊/已撤下（仍跟进 N 个会话）/验收中/验收失效），操作：撤下、恢复、移除（无会话时可用）、重新验收。
- 任务额度：总额（0–20）、今日目标、账号今日已用/20。
- "从 BOSS 选择岗位"：列表（刷新按钮；运行中显示"等待执行器读取"），已关闭不可选、已被其他任务管理不可选；勾选 → 绑定 → 跳到岗位确认（问候语、事实、FAQ、筛选）→"验收并加入"。
- 粘贴链接入口保留。
- [ ] **Step 1：在 e2e 中写断言：权重修改发出 `POST /api/tasks/<id>/jobs {action:'weights'}`、撤下发出 withdraw、已关闭职位 checkbox disabled。**
- [ ] **Step 2：实现并本地跑 e2e。**
- [ ] **Step 3：提交** `Show per-job weights, quota and BOSS job picker in the task view`

### Task 11：规则文档、只读演练与上线

**Files：**
- Modify: `CLAUDE.md`（约束 6 改为："发布走草案与隔离验收：运行中的任务不直接换版本；例外是 `faq_answers.apply` 与 `job_changes.apply` 在安全点换绑，新岗位须先通过本岗位的隔离验收"）、`docs/job-sourcing-boundaries.md`（额度归属与增量变更一节）
- Create: `.tmp_incident/multi_job_dry_run.py`（不提交）

- [ ] **Step 1：改文档；全量单测 + ruff；提交** `Document per-job increments in the release rules`
- [ ] **Step 2：只读演练**：按 CLAUDE.md"只读演练"把本地 `cloakup/` 拷到容器 `/tmp/newpkg`，脚本对线上数据计算：现有任务迁移后的 members、今日任务目标与各岗位份额、`validated_cases`（预期上线后全部失效，需全量验收一次）。把结果给管理员确认。
- [ ] **Step 3：管理员确认后上线**：重建 → 草案 → validate → publish → resume（CLAUDE.md"发布签名"流程）。
- [ ] **Step 4：verification-before-completion：以 `/api/health`、`data/logs/state-*.jsonl` 中的 `job_change`/首聊占位记录、日报为证据，确认额度分配与岗位切换按预期。**
