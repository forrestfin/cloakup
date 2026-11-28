# 中止发送不留草稿、暂停提醒、撤下岗位旧会话：实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 点发送前中止的发送不留草稿、不暂停任务；系统暂停时飞书醒目提醒并按间隔重复；撤下岗位认不出的旧会话不再反复核验，有新消息就交给人工。

**Architecture:** 分两批上线。
- 第一批（Task 1–5）：
  - `boss_chat.send_once` 在点击前中止时抛 `SendAborted`，并且只清空自己刚打、逐字一致的文字；
  - `CaseStore.abort_send` 把消息改为 `stale`，账本记 `failed`；
  - `dispatch_reply` 接住这个异常；清不掉的草稿记成 `system_draft`，供待办说明使用。
- 第二批（Task 6–9）：
  - 头部岗位字段写进诊断；
  - 撤下岗位的放下会话改为只在列表摘要变化时复核，有新消息仍认不出就生成待办；
  - 飞书暂停提醒。

**Tech Stack:** Python 3.12（`.venv`）、pytest（asyncio auto）、SQLAlchemy／Postgres、Playwright 风格的 session 封装。

**Spec:** `docs/superpowers/specs/2026-11-27-aborted-send-pause-alert-withdrawn-chats-design.md`

## Global Constraints
- 点过发送之后的规则不变：拿不到回执就记 `delivery_unknown`，不重发（约束 2）。
- 只清空与本次正文**逐字一致**的输入框内容。其他文字一律不碰，照旧暂停（约束 5）。
- 写平台的动作仍先在账本占位（约束 3）。中止时把账本记为 `failed`，以便之后重新认领。
- 重试、间隔用 `pacing.jitter`，必须有上限。
- 会暂停任务、改变执行阶段或写平台的新路径，都要写 `logbook.log`。
- 面向管理员的文字用中文，说清三件事：发生了什么、系统做了什么、要不要人处理。
- 改 `autopilot.py`、`selection_recovery.py` 的事件文字时，同步 `daily_report.EVENT_RULES` 和 `tests/test_daily_report.py`。
- 需要管理员处理的状态在 `attention.reconcile` 生成待办，状态解除后自动关闭。
- 飞书提醒的数值：首次推送后 30 分钟补发一次，之后每 2 小时一次；每个待办每天最多 3 次（含首次）；只在任务执行时段内发。
- 跟随文件既有的紧凑写法，不重排格式。
- 每批跑全量单测和 ruff，一批一个提交，提交信息用英文祈使句，不加署名。

## Review Focus
1. 打字后读回的输入框内容与正文有空白差异（结尾换行、全角空格）：按不一致处理，不清空，照旧暂停。→ Task 1：`test_abort_keeps_editor_when_text_differs`。
2. 清空动作本身抛异常，或清空后再读仍不为空：`cleared=False`，异常不吞掉，仍抛 `SendAborted`。→ Task 1：`test_abort_reports_not_cleared_when_clear_fails`。
3. 同一候选人同一条回复中止后，下一轮生成同样的正文：账本允许重新认领，不报「动作已经执行或待核实」。→ Task 2：`test_aborted_send_can_be_reclaimed`。
4. 回执续发（`resume_receipt_ack`）的消息中止后，候选人状态回到认领前，不卡在 `sending`。→ Task 2：`test_abort_restores_pre_claim_status`。
5. 任务已恢复，但提醒记录还在队列里：发送前 `_current` 判定为取消，不发。→ Task 8：`test_pause_reminder_cancelled_after_resume`。

---

## 第一批：中止的发送

### Task 1: `send_once` 点击前中止时抛 `SendAborted`，并清空自己的文字

**Files:**
- Modify: `cloakup/boss_chat.py`（`send_once`，约 618 行起）
- Test: `tests/test_boss_chat.py`

**Interfaces:**
- Produces:
  - `class SendAborted(SourcingError)`，`__init__(self, message: str, *, reason: str, typed: bool, cleared: bool)`，属性同名；
  - `reason` 取值：`'changed_before_typing'`、`'existing_draft'`、`'editor_unavailable'`、`'reply_during_typing'`、`'text_mismatch'`、`'send_button_not_unique'`、`'not_authorized'`。
  - `async def _clear_own_text(session, text: str, editor) -> bool`：只有编辑框当前文字 `== text` 时才清空；用 `session._act('type', …)` 全选后按 Backspace；清完再读，为空返回 True，其余情况一律返回 False，不向外抛异常。

- [ ] **Step 1: 写失败测试**（沿用 `test_post_send_receipt_scrolls_list_back_when_row_left_virtual_list` 的 Session、Editor 桩）
  - `test_reply_during_typing_clears_own_text_and_raises_aborted`：第二次读到的 fingerprint 不同、编辑框内容为 `'正文'`。断言抛 `SendAborted`，`reason=='reply_during_typing'`、`typed and cleared`，编辑框最后为空，`clicks==[]`。
  - `test_abort_keeps_editor_when_text_differs`：编辑框内容为 `'正文\n'`。断言 `cleared is False`，编辑框内容不变。
  - `test_abort_reports_not_cleared_when_clear_fails`：清空时 `_act` 抛 `RuntimeError`。断言 `SendAborted.cleared is False`。
  - `test_pre_send_refusal_clears_and_raises_aborted`：`pre_send` 抛 `SourcingError`。断言 `reason=='not_authorized'`、已清空、未点击。
  - 现有的 `test_intervening_reply_prevents_typing_and_send` 改为断言 `SendAborted`，且 `reason=='changed_before_typing'`、`typed is False`。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest tests/test_boss_chat.py -q`。Expected：新测试 FAIL（`SendAborted` 未定义）。
- [ ] **Step 3:** 实现 `SendAborted` 和 `_clear_own_text`，把 `send_once` 中点击前的每个 `raise SourcingError` 换成 `SendAborted`。打字后的中止先调用 `_clear_own_text` 再抛出；`pre_send` 抛出的异常包成 `not_authorized`。点击之后的代码不动。
- [ ] **Step 4:** 运行同一条命令，Expected：全部 PASS。

### Task 2: `CaseStore.abort_send`

**Files:**
- Modify: `cloakup/sourcing.py`（`claim_send` 约 263 行，在其后新增方法）
- Test: `tests/test_sourcing.py`

**Interfaces:**
- Consumes: `ActionLedger.transition(ident, 'failed', reason=…)`（`action_gateway.py`；'failed' 允许从 claimed／executing 进入，并释放占位）。
- Produces: `CaseStore.abort_send(self, case, candidate_id, message_id, reason: str) -> None`，只修改 case，不保存（与 `claim_send` 一致，由调用方 `save`）。具体做法：
  - 账本记 `failed`；
  - 消息设为 `status='stale'`、`abort_reason=reason`、`operation_state='failed'`；
  - 候选人状态恢复为 `message['pre_claim_status']`；
  - 调用 `_invalidate(candidate)`；
  - `audit(case,'send_aborted',candidate_id=…,message_id=…,reason=…)`。
- `claim_send` 在写 `status='sending'` 之前，记录 `message['pre_claim_status']=candidate['status']`。

- [ ] **Step 1: 写失败测试**
  - `test_abort_restores_pre_claim_status`：认领后调用 `abort_send`。断言消息为 `stale` 且带 `abort_reason`，候选人状态等于认领前的值，`context_revision` 加 1，账本该操作为 `failed`，`action_leases` 中没有它。
  - `test_aborted_send_can_be_reclaimed`：中止后为同一条回复起草同样的正文并认领。断言不抛异常。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest tests/test_sourcing.py -q`。Expected：FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 运行同一条命令，Expected：PASS。

### Task 3: `dispatch_reply` 接住 `SendAborted`

**Files:**
- Modify: `cloakup/autopilot.py`（约 384–404 行的 try／except 和 else 分支）、`cloakup/daily_report.py`（`EVENT_RULES`）
- Test: `tests/test_planning_dialogue.py`（沿用它对 `send_once` 的 monkeypatch 写法）、`tests/test_daily_report.py`

**Interfaces:**
- Consumes: `boss_chat.SendAborted`（Task 1）、`CaseStore.abort_send`（Task 2）。
- Produces: 清不掉的草稿写入记录 `manager.db.put('system_draft','boss',{'chat_id','receiver','job_label','case_id','text_sha256','at'})`，供 Task 4 读取。

- [ ] **Step 1: 写失败测试**
  - `test_aborted_send_marks_stale_and_does_not_mark_unknown`：`send_once` 抛 `SendAborted(reason='reply_during_typing', typed=True, cleared=True)`。断言：
    - 消息为 `stale`，不是 `delivery_unknown`；
    - 事件中有「本次未点击发送：输入期间收到新消息；已删除未发送的文字」；
    - 没有 `system_draft` 记录。
  - `test_aborted_send_not_cleared_records_system_draft`：同上但 `cleared=False`。断言写入了 `system_draft`，`chat_id` 和 `job_label` 正确；事件文字以「未能删除，任务会在页面检查时暂停」结尾。
  - `test_other_send_errors_still_mark_unknown`：抛普通 `SourcingError`。断言仍是 `delivery_unknown`（现有行为不变）。
  - `tests/test_daily_report.py`：新增的事件文字被 `EVENT_RULES` 归类。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest tests/test_planning_dialogue.py tests/test_daily_report.py -q`。Expected：FAIL。
- [ ] **Step 3:** 实现。
  - 新增 `except boss_chat.SendAborted as exc`（放在通用 `except Exception` 之前）：调用 `store.abort_send` 并 `save`，写事件，再 `log('action', type='send_aborted', reason=exc.reason, typed=…, cleared=…, case_id=…, candidate_id=…)`。`typed and not cleared` 时写 `system_draft`。然后 `return`，不进入 `delivery_unknown` 分支。
  - 原因对应的中文写成常量表 `ABORT_TEXT`：
    - `reply_during_typing` → 输入期间收到新消息
    - `text_mismatch` → 输入后的正文不一致
    - `send_button_not_unique` → 发送按钮不唯一
    - `not_authorized` → 发送前授权已停止
    - `changed_before_typing` → 读取后会话有变化
    - `existing_draft` → 输入框已有人工草稿
    - `editor_unavailable` → 找不到唯一输入框
  - 通用 `except Exception` 的事件里补上 `str(exc)[:120]`，现在只有类型名。
- [ ] **Step 4:** 运行同一条命令，Expected：PASS。

### Task 4: 草稿待办写明「这是系统自己没发出的文字」

**Files:**
- Modify: `cloakup/attention.py`（`execution_recovery` 的 detail 约 75 行；`browser_health:draft` 约 271 行）
- Test: `tests/test_attention.py`

**Interfaces:**
- Consumes: `system_draft` 记录（Task 3）。只有 `at` 在 24 小时内的记录有效。

- [ ] **Step 1: 写失败测试** `test_draft_todo_names_system_own_unsent_text`
  - 前提：存在 24 小时内的 `system_draft`（receiver「赵清山」、岗位「qe工程师」），选择恢复记录为 `blocked` 且 `diagnostics.reason=='draft_present'`。
  - 断言 `execution_recovery` 待办的 detail 同时包含「系统自己没发出的文字」「赵清山」「qe工程师」「确认后删除即可继续」。
  - 再加一条断言：`system_draft` 超过 24 小时时，detail 不含「系统自己」。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest tests/test_attention.py -q`。Expected：FAIL。
- [ ] **Step 3:** 实现：两处草稿待办的 detail 都读取有效的 `system_draft`，在原句前加上「输入框里是系统给 <候选人>（<岗位>）准备、但没发出的文字，删除失败。确认后删除即可继续；」。
- [ ] **Step 4:** 运行同一条命令，Expected：PASS。

### Task 5: 第一批验证、演练与上线

- [ ] **Step 1:** 运行 `.venv/bin/python -m pytest tests -q` 和 `.venv/bin/ruff check .`。Expected：全部通过、ruff 无报错。
- [ ] **Step 2:** 提交第一批（`git add` 上述文件、spec 和 plan）。标题：`Clear own text and keep the task running when a send aborts before clicking`。
- [ ] **Step 3:** 只读演练：把新包拷到容器 `/tmp/newpkg_abort/cloakup`，列出线上所有 `status in ('sending','delivery_unknown')` 且没有 `pre_claim_status` 的消息。确认新逻辑不会去改这些旧消息：`abort_send` 只在新的中止时调用，旧消息维持原状。结论写进 `data/analysis/2026-11-27-abort-rehearsal.md`。
- [ ] **Step 4:** 上线：
  1. 暂停任务，等到 standby；
  2. `scripts/docker_up.py --reuse-runtime`；
  3. draft → test（validate）→ publish（`expected_revision` 用草案版本）→ resume；
  4. 核对 `/api/health`、任务状态为 running、验收 36/36。

---

## 第二批：头部诊断、撤下岗位旧会话、暂停提醒

### Task 6: 头部「沟通职位」原文写进诊断

**Files:**
- Modify: `cloakup/boss_chat.py`（`read_conversation` 的 JS 约 40–63 行的 evidence；`ChatSelectionUnavailable` 的 diagnostics）、`cloakup/selection_recovery.py`（`failure` 的日志）、`cloakup/autopilot.py`（`recheck_deferred_chat` 的日志）
- Test: `tests/test_boss_chat.py`、`tests/test_selection_recovery.py`

**Interfaces:**
- Produces: `evidence.header_jobs: list[str]`，即各头部候选中以「沟通职位」开头的字段，去掉前缀，最多 3 条、每条最多 40 字。`header_job_unverified` 时这个列表会进入 `exc.diagnostics['header_jobs']`。

- [ ] **Step 1: 写失败测试**
  - `test_header_job_mismatch_reports_observed_job_field`：evaluate 返回 `header_job_unverified` 且 evidence 含 `header_jobs:['QE工程师（已关闭）']`。断言异常的 `diagnostics['header_jobs']` 与之相同。
  - `test_selection_failure_log_includes_header_jobs`：断言 `failure` 写出的 `selection_failure` 日志带有 `header_jobs`。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest tests/test_boss_chat.py tests/test_selection_recovery.py -q`。Expected：FAIL。
- [ ] **Step 3:** 实现。JS 中 `fields(e)` 已有，筛出「沟通职位」开头的字段放进 evidence；Python 侧透传。
- [ ] **Step 4:** 运行同一条命令，Expected：PASS。

### Task 7: 撤下岗位的放下会话只在有新消息时复核；仍认不出就生成人工待办

**Files:**
- Modify: `cloakup/autopilot.py`（`sync_and_follow` 的 `deferred_due`、约 524 行的复核分支、`recheck_deferred_chat`）、`cloakup/attention.py`（新待办）、`cloakup/daily_report.py`（如有新事件文字）
- Test: `tests/test_selection_sync.py`、`tests/test_attention.py`

**Interfaces:**
- Produces:
  - `def withdrawn_case(manager, case_id) -> bool`（放在 `autopilot.py`）：该岗位在某个未停止任务的成员里 `outreach=='withdrawn'`（借助 `job_members.normalize`）。
  - 案件字段 `deferred_chat_preview: dict[chat_id, str]`：放下时记录的列表摘要哈希。
  - 案件字段 `withdrawn_unreadable: dict[chat_id, {'at': float, 'receiver': str}]`：有新消息但复核失败的会话。复核通过时移除。
  - 待办 kind `'withdrawn_chat'`，标题「旧岗位会话需人工回复」，一位候选人一条，key 为 `withdrawn_chat:<case_id>:<chat_id>`。

- [ ] **Step 1: 写失败测试**
  - `test_withdrawn_deferred_chat_not_rechecked_on_timer`：岗位已撤下，会话已放下，`deferred_chat_check_at` 已到期，行摘要哈希与 `deferred_chat_preview` 相同。断言不调用 `recheck_deferred_chat`。
  - `test_withdrawn_deferred_chat_rechecked_once_on_new_message`：行摘要哈希变了。断言复核一次；复核失败后写入 `withdrawn_unreadable[chat_id]`，并把 `deferred_chat_preview` 更新为新哈希，之后同一哈希不再复核。
  - `test_active_job_deferred_chat_still_rechecked_on_timer`：参与首聊的岗位维持现有的定时复核（回归测试）。
  - `test_withdrawn_unreadable_chat_raises_todo_until_verified`（`test_attention.py`）：有 `withdrawn_unreadable` 时生成 `withdrawn_chat` 待办，detail 含候选人和岗位，写明「系统认不出这条旧岗位会话，不会自动回复。需要：在 BOSS 手动回复这位候选人」。记录移除后待办自动关闭。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest tests/test_selection_sync.py tests/test_attention.py -q`。Expected：FAIL。
- [ ] **Step 3:** 实现：
  - 撤下岗位的 `deferred_due` 只包含摘要哈希变化的会话；
  - `failure` 放下撤下岗位的会话时，记录当时的行摘要哈希；
  - `pending_chat_ids` 的优先核验排除撤下岗位已放下的会话。
- [ ] **Step 4:** 运行同一条命令，Expected：PASS。

### Task 8: 飞书「任务已暂停」提醒与重复提醒

**Files:**
- Modify: `cloakup/task_notifications.py`（`enqueue`、`_current`、`_process` 的 todo 分支）
- Test: `tests/test_task_notifications.py`

**Interfaces:**
- Consumes: `today.PAUSE_KINDS`、`today.ACCOUNT_KINDS`、`task_policy.window_status(schedule, now)`、`account_execution.members()`。
- Produces:
  - `def system_paused(manager, task_id) -> float | None`：该任务成员状态不是 running、且最后一次 history 的 `actor!='admin'` 时，返回暂停时间，否则返回 None。
  - 提醒记录的 source 为 `<todo_id>:reminder:<n>`（n=1、2），带 `todo_id` 和 `reminder=n`。
  - 常量 `REMIND_AFTER=(1800, 7200)` 和 `REMIND_DAILY_MAX=3`。

- [ ] **Step 1: 写失败测试**
  - `test_pause_todo_uses_paused_headline_and_workbench_url`：`execution_recovery` 待办，任务被系统暂停。断言正文首行为「【任务已暂停】<任务名>」，并包含「暂停期间不收件、不回复候选人」和 `http://127.0.0.1:8766`。
  - `test_question_todo_keeps_plain_headline`：问题待办的首行仍是「待办提醒」。
  - `test_pause_reminder_after_30_minutes_inside_window`：首条在 31 分钟前发出、在执行时段内、仍是系统暂停。断言生成 `reminder:1`；29 分钟时不生成。
  - `test_pause_reminder_skipped_outside_window`：时段外不生成。
  - `test_pause_reminder_capped_at_three_per_day`：首条加两条提醒都已发出。2 小时后不再生成。
  - `test_pause_reminder_cancelled_after_resume`：提醒已入队，任务恢复 running 后 `_process` 记为 `cancelled`，不调用 `client.send`。
- [ ] **Step 2:** 运行 `.venv/bin/python -m pytest tests/test_task_notifications.py -q`。Expected：FAIL。
- [ ] **Step 3:** 实现：
  - `enqueue` 对 PAUSE_KINDS 待办，若 `system_paused` 为真、在时段内，且上一条（首条或前一次提醒）的 `sent_at` 距今已超过 `REMIND_AFTER` 中对应的间隔，就追加下一条提醒；间隔加 `pacing.jitter`，上限为间隔的 1.2 倍。
  - `_current` 对提醒记录额外要求 `system_paused` 为真。
  - 工作台端口取自环境变量 `CLOAKUP_PUBLIC_PORT`，默认 8766。
- [ ] **Step 4:** 运行同一条命令，Expected：PASS。

### Task 9: 第二批验证与上线

- [ ] **Step 1:** 运行全量单测和 ruff。Expected：全部通过。
- [ ] **Step 2:** 提交。标题：`Alert on system pauses and stop rechecking unreadable withdrawn-job chats`。
- [ ] **Step 3:** 只读演练：
  - 对线上旧 QE 岗位列出当前放下的会话，以及新逻辑下谁会生成 `withdrawn_chat` 待办；
  - 用当前待办列出哪些会触发暂停提醒。
  - 结论写进 `data/analysis/2026-11-27-batch2-rehearsal.md`。
- [ ] **Step 4:** 按 Task 5 Step 4 的流程上线。上线后在 `selection_failure`／`deferred_chat_check` 日志里查看 `header_jobs`，判断旧会话头部实际显示什么。根因若可确定，单独提方案。
