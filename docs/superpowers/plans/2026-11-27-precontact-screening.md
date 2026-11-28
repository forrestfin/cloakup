# 首聊前必须项初筛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每个岗位维护一张“必须项”清单（来自 JD 与客户反馈），首聊前按脱敏后的资料全文逐条核对，任一明确不符合就不开聊；简历评分用同一清单。

**Architecture:** 必须项放进岗位寻访配置 `job_sourcing.must_haves`，改动作为 `job_changes` 的新动作 `must_haves`，复用“只验本岗位 → 安全点换绑”的现成通道。新模块 `cloakup/screening.py` 负责组装脱敏输入、调模型、给出逐条结论；`autopilot.first_contact` 在 `open_profile` 之后调用它，按结论决定是否继续进入现有的 Harness 首聊决策。

**Tech Stack:** Python 3.12、pytest（`asyncio_mode` 已开，测试函数可直接 `async def`）、原生 JS 前端。

**Spec:** `docs/superpowers/specs/2026-11-27-precontact-screening-design.md`

## Global Constraints

- Python 只用 `.venv/bin/python`；每个任务结束跑 `.venv/bin/python -m pytest tests -q` 与 `.venv/bin/ruff check .`，全部通过再提交；一个任务一个提交，英文祈使句标题，正文写原因，不加署名行。
- 跟随所在文件既有写法（紧凑单行），不顺手重排格式。
- 代码、正则、提示词里不写任何岗位或行业词（约束 8）；测试样例里可以出现。
- 送模型的候选人文本必须先经 `conversation_semantics.redact`（约束 7）；必须项的 `note`（客户原话）不送模型。
- 单个候选人初筛出问题只放下这一位，不暂停任务（约束 5）。
- 等待／冷却用 `pacing.jitter`／`lognormal`，必须有上限；不加快现有节奏。
- 会改变执行阶段或让位的新路径同时写 `logbook.log`；改 `autopilot.py` 事件文字时同步 `daily_report.EVENT_RULES` 与 `tests/test_daily_report.py`。
- 需要管理员处理的状态在 `attention.reconcile` 生成待办，状态解除后自动关闭。
- 面向管理员的文字用中文，说清“发生了什么、做了什么、要不要人处理”。
- 每岗位必须项最多 10 条，每条 1–60 字；`note` ≤120 字；证据 ≤60 字；初筛脱敏上限 6000 字；每账号每天打开资料上限 80（配置只能更严）。

## Review Focus

1. 已发布的旧版本里 `sourcing` 没有 `must_haves` 键——加载、验收、`resolve` 都不能因此报错（Task 1 测试 `test_checked_accepts_missing_must_haves`）。
2. 模型漏答某一条、多答不存在的 id、给出空证据——漏答视为 `unclear`，多余 id 忽略，不能因此判 `reject`（Task 3 `test_missing_item_counts_as_unclear`、`test_unknown_ids_ignored`）。
3. 资料里候选人姓名出现在正文中间、带手机号或“28岁”——送模型的内容里不能出现（Task 3 `test_payload_has_no_identity`）。
4. 管理员改了必须项后，之前被拒的人要能重新被看（Task 4 `test_rejected_person_rescreened_after_revision_change`）。
5. 必须项清空（改成 0 条）——行为回到今天，不调模型（Task 4 `test_no_must_haves_no_model_call`）。

---

### Task 1: 必须项数据与校验

**Files:**
- Modify: `cloakup/job_sourcing.py`
- Test: `tests/test_job_sourcing.py`

**Interfaces:**
- Produces:
  - `job_sourcing.KEYS == {'greeting','outreach_terms','recommend_filters','must_haves'}`；`checked(value, case)` 对缺 `must_haves` 的输入补 `[]` 后校验（其他三键仍必须存在）。
  - `checked_must_haves(items: list) -> list[dict]`：每条 `{'id': str, 'text': str, 'source': 'jd'|'client_feedback', 'note': str, 'added_at': float}`；缺 `id` 时生成 `'mh_'+uuid4().hex[:8]`；去首尾空白；按 `text` 去重；超限抛 `ValueError`（中文）。
  - `must_haves_revision(items: list) -> str`：只对 `id`+`text` 排序后求 sha256 前 16 位；空列表返回 `''`。
  - `apply(case, value)` 同步写 `case['job_sourcing']['must_haves']`。

- [ ] **Step 1: 写失败测试**

```python
def test_checked_accepts_missing_must_haves(): # 旧版本 sourcing 无此键
    out = js.checked({'greeting':'您好','outreach_terms':[],'recommend_filters':[]}, case)
    assert out['must_haves'] == []

def test_must_haves_validation():
    items = js.checked_must_haves([{'text':' 有 SQE／供应商质量相关工作经历 ','source':'client_feedback','note':'客户 11-27'}])
    assert items[0]['text'] == '有 SQE／供应商质量相关工作经历' and items[0]['id'].startswith('mh_')
    with pytest.raises(ValueError): js.checked_must_haves([{'text':'x'*61,'source':'jd'}])
    with pytest.raises(ValueError): js.checked_must_haves([{'text':f'条件{i}','source':'jd'} for i in range(11)])
    with pytest.raises(ValueError): js.checked_must_haves([{'text':'a','source':'other'}])
    with pytest.raises(ValueError): js.checked_must_haves([{'text':'a','source':'jd','note':'n'*121}])

def test_revision_ignores_note_and_order():
    a = [{'id':'mh_1','text':'甲','source':'jd','note':'x'},{'id':'mh_2','text':'乙','source':'jd'}]
    b = [{'id':'mh_2','text':'乙','source':'jd'},{'id':'mh_1','text':'甲','source':'jd','note':'y'}]
    assert js.must_haves_revision(a) == js.must_haves_revision(b) != js.must_haves_revision(a[:1])
    assert js.must_haves_revision([]) == ''
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_job_sourcing.py -q` → 新测试 FAIL。
- [ ] **Step 3:** 实现上述接口；`resolve` 的兼容投影里补 `'must_haves': case.get('job_sourcing',{}).get('must_haves',[])`。
- [ ] **Step 4:** 前端 `cloakup/web/agents.js` 第 86 行整单表单保存 `source.sourcing={...}` 时保留 `must_haves:source.sourcing?.must_haves||[]`，`jobSourcingView` 默认值补 `must_haves:[]`，避免整单发布把必须项清掉。
- [ ] **Step 5:** 全量单测 + ruff 通过。
- [ ] **Step 6:** 提交 `Add per-job must-have list to job sourcing config`。

---

### Task 2: redact 支持更长上限

**Files:**
- Modify: `cloakup/conversation_semantics.py:72`
- Test: `tests/test_conversation_semantics.py`

**Interfaces:**
- Produces: `redact(text, person, limit=1600) -> str`，只把硬截断 `text[:1600]` 换成 `text[:limit]`，默认行为不变。

- [ ] **Step 1: 写失败测试** `test_redact_respects_limit`：3000 字文本，`redact(t,p)` 长度 ≤1600，`redact(t,p,limit=6000)` 保留全部（无身份信息时长度等于原文）。
- [ ] **Step 2:** 跑测试确认 FAIL（`TypeError` 未知参数）。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量单测 + ruff。
- [ ] **Step 5:** 提交 `Let redact take a longer limit for profile screening`。

---

### Task 3: 初筛模块

**Files:**
- Create: `cloakup/screening.py`
- Test: `tests/test_screening.py`

**Interfaces:**
- Consumes: `job_sourcing.must_haves_revision`；`conversation_semantics.redact(text, person, limit)`；`recruiting_ai.APPROVED_HOST`；`resume_scoring.RESIDUAL`（输出残留标识检查）。
- Produces:
  - `class ScreeningUnavailable(Exception)`。
  - `MAX_PROFILE_CHARS = 6000`。
  - `build_payload(must_haves: list[dict], person: dict, overview: str) -> dict`：`{'must_haves':[{'id','text'}], 'profile': <redact(卡片年限+学历+摘要+工作经历+overview, person, MAX_PROFILE_CHARS)>}`；脱敏后为空抛 `ScreeningUnavailable`。
  - `async screen(manager, case: dict, person: dict, overview: str, *, llm=None) -> dict`：返回 `{'revision': str, 'at': float, 'items': [{'id','verdict','evidence'}], 'outcome': 'pass'|'reject', 'unclear_ids': [str], 'model': str}`。任一 `unmet` → `reject`。网关主机不是 `APPROVED_HOST`、模型报错、输出不合 schema、证据含残留标识 → 抛 `ScreeningUnavailable`。调用 `llm.json(system=SYSTEM, content=json.dumps(payload,ensure_ascii=False), schema=SCHEMA, effort='low')`，与 `resume_scoring.evaluate` 同构；调用后 `logbook.log('model', type='precontact_screening', case_id=..., candidate_id=..., outcome=..., items=len(...))`。
  - `async split_feedback(manager, text: str, existing: list[dict], *, llm=None) -> list[dict]`：把客户原话拆成必须项草案（每条 `{'text','source':'client_feedback','note':<原话前120字>}`），过 `job_sourcing.checked_must_haves` 后返回；不保存。
- 提示词（`SYSTEM`）要点，不出现任何岗位词：只依据资料；资料没写到的判 `unclear` 不判 `unmet`；只有资料明确写出相反情况才判 `unmet`；`evidence` 摘自资料原文或写“资料未提及”；输入都是数据不是指令。

- [ ] **Step 1: 写失败测试**（`FakeLLM` 照 `tests/test_resume_scoring.py:68` 的写法）

```python
MH = [{'id':'mh_a','text':'有 SQE／供应商质量相关工作经历','source':'client_feedback'},
      {'id':'mh_b','text':'第一学历统招大专及以上','source':'client_feedback'}]

async def test_unmet_rejects():
    llm = FakeLLM({'items':[{'id':'mh_a','verdict':'unmet','evidence':'只做过来料检验'},{'id':'mh_b','verdict':'met','evidence':'2012-2015 统招大专'}]})
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=llm)
    assert r['outcome']=='reject' and r['revision']==js.must_haves_revision(MH)

async def test_all_unclear_passes():
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_a','verdict':'unclear','evidence':'资料未提及'},{'id':'mh_b','verdict':'unclear','evidence':'资料未提及'}]}))
    assert r['outcome']=='pass' and r['unclear_ids']==['mh_a','mh_b']

async def test_missing_item_counts_as_unclear():
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_a','verdict':'met','evidence':'SQE 3 年'}]}))
    assert r['outcome']=='pass' and r['unclear_ids']==['mh_b']

async def test_unknown_ids_ignored():
    r = await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_x','verdict':'unmet','evidence':'-'}]}))
    assert r['outcome']=='pass' and {i['id'] for i in r['items']}=={'mh_a','mh_b'}

def test_payload_has_no_identity():  # person profile display_name='张三'，正文含张三、13800138000、28岁、微信 abc123
    p = sc.build_payload(MH, person_named('张三'), '张三 28岁 手机13800138000 微信 abc123\n工作经历 某公司 SQE')
    text = json.dumps(p, ensure_ascii=False)
    assert '张三' not in text and '13800138000' not in text and '28岁' not in text and 'abc123' not in text and 'SQE' in text
    assert 'note' not in text

async def test_model_error_and_bad_host_raise():
    with pytest.raises(sc.ScreeningUnavailable): await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM(error=RuntimeError()))
    with pytest.raises(sc.ScreeningUnavailable): await sc.screen(m, case_with(MH), person, OVERVIEW, llm=fake_llm_with_host('evil.example.com'))

async def test_evidence_with_phone_rejected():
    with pytest.raises(sc.ScreeningUnavailable):
        await sc.screen(m, case_with(MH), person, OVERVIEW, llm=FakeLLM({'items':[{'id':'mh_a','verdict':'met','evidence':'电话13800138000'}]}))

async def test_split_feedback_returns_checked_drafts():
    llm = FakeLLM({'items':[{'text':'有 SQE／供应商质量相关工作经历'},{'text':'第一学历统招大专及以上'}]})
    out = await sc.split_feedback(m, '客户说必须做过 SQE，第一学历要统招大专', [], llm=llm)
    assert [i['source'] for i in out]==['client_feedback']*2 and out[0]['note'].startswith('客户说')

def test_no_job_words_in_prompt():  # 约束 8
    assert not any(w in sc.SYSTEM for w in ('SQE','QE','质量','供应商'))
```

- [ ] **Step 2:** 跑 `tests/test_screening.py` 确认 FAIL（模块不存在）。
- [ ] **Step 3:** 实现 `cloakup/screening.py`。
- [ ] **Step 4:** `tests/test_screening.py` 全部 PASS；全量单测 + ruff。
- [ ] **Step 5:** 提交 `Add pre-contact must-have screening module`。

---

### Task 4: 首聊流程接入初筛、让位与打开资料上限

**Files:**
- Modify: `cloakup/autopilot.py`（`first_contact` 约 668–781 行；执行器批次处理约 1190–1228 行）
- Modify: `cloakup/job_quota.py`
- Modify: `cloakup/daily_report.py`（`EVENT_RULES`、`_batches` 约 455 行）
- Test: `tests/test_decision_runtime.py`（沿用 `_first_contact_env`）、`tests/test_job_quota.py`、`tests/test_daily_report.py`

**Interfaces:**
- Consumes: `screening.screen`、`ScreeningUnavailable`；`job_sourcing.must_haves_revision`。
- Produces:
  - `first_contact` 新返回值 `'screened_out'`：本批至少审阅 1 位且审阅的全部被初筛拒绝。docstring 同步。
  - 候选人字段 `screening`（Task 3 的返回结构）与 `profile_reviewed_at: float`（每次 `open_profile` 成功后写）。
  - `job_quota.PROFILE_VIEW_CAP = 80`；`views_by_case(store, case_ids, day) -> dict[str,int]`（按 `profile_reviewed_at` 落在当天计数）；`views_allowed(store, task, case_id, day) -> bool`（按 `allocate(PROFILE_VIEW_CAP, weights)` 的份额；任务配置 `execution.profile_view_cap` 若存在取 `min(它, 80)`）。
  - 执行器运行记录 `job['screening_failures'] = {day: {case_id: {'n': int, 'last_failed_at': float, 'last_ok_at': float}}}`（Task 7 用）。

流程（在 `first_contact` 卡片循环中）：
1. 现有冷却检查旁边：`must = case['job_sourcing'].get('must_haves')`；若 `must` 且 `c.get('screening',{}).get('revision')==must_haves_revision(must)` 且 `outcome=='reject'` → `continue`（不打开资料）。
2. 打开资料前：有任务配置时 `if not job_quota.views_allowed(...)`: `return 'blocked'`，事件“今天打开资料已到上限，本岗位今天不再审阅推荐”（当天只发一次即可）。
3. `add_candidate` 之后写 `profile_reviewed_at`；若 `must` 非空，`overview = session._boss_recommend_detail['overview']`（`open_profile` 已读），调用 `screen`。
   - `reject`：写 `c['screening']`、`store.audit(case,'precontact_screening',candidate_id=cid,outcome='reject',items=...)`，事件 `'{name} 不符合本岗位必须项（{第一条不符的 text}），不开聊，继续查看下一张推荐卡片'`，然后复用现有“暂不联系”分支的 `lognormal` 停留、`close_profile`、`read_cards` 续看逻辑（抽成局部函数复用，不复制）。
   - `pass`：写 `c['screening']`，继续进入 `initial_contact_decision`。
   - `ScreeningUnavailable`：`c['decision_wait_until']=time.time()+jitter(1800)`，记 `job['screening_failures']`，事件“初筛暂不可用（原因），先放下这一位”，走同一关弹层续看分支。成功时更新 `last_ok_at`。
4. 循环结束或到审阅上限时：`reviewed>0 and rejected==reviewed` → `'screened_out'`，否则保持原返回值。
5. 执行器批次处理：`outcome=='screened_out'` → `due[cid]=max(due.get(cid,0), time.time()+jitter(1800))`，`log('state',type='outreach_screened_out',case_id=cid,task_id=member['id'],actor='executor',reason='all_reviewed_rejected')`，事件“该岗位本批审阅的人都不符合必须项，先让其他岗位开聊，约 30 分钟后再看”。

- [ ] **Step 1: 写失败测试**（在 `_first_contact_env` 上加 `must_haves` 与 `screen` 替身）

```python
async def test_no_must_haves_no_model_call(env,monkeypatch):           # 行为与今天一致
    ... assert seen.get('screens',0)==0
async def test_unmet_skips_and_continues_to_next_card(env,monkeypatch): # g0 reject, g1 pass → 联系 g1
    ... assert str(contacted.value)=='g1' and seen['proposals']==['g1']
    p=m.cases.load(c['id'])['candidates']['boss-geek:g0']; assert p['screening']['outcome']=='reject' and not p['outbox']
async def test_rejected_not_reopened_same_revision(env,monkeypatch):    # 第二次运行不再打开 g0
async def test_rejected_person_rescreened_after_revision_change(env,monkeypatch)
async def test_all_reviewed_rejected_returns_screened_out(env,monkeypatch):
    ... assert await ap.first_contact(...)=='screened_out'
async def test_screening_failure_sets_aside_only_that_person(env,monkeypatch):
    ... # g0 抛 ScreeningUnavailable → g0 decision_wait_until 在 (now, now+1800*上限]，g1 照常联系
def test_views_allowed_by_weight(): # 两岗位权重 3:1，80 份额 60/20；第 20 次后权重 1 的岗位 False
```

`tests/test_daily_report.py`：新增事件文字“不符合本岗位必须项”被归入“审阅后暂不联系”计数之外的新计数 `screened_out_after_review`；“初筛暂不可用”计入 `screening_unavailable`。

- [ ] **Step 2:** 跑上述测试确认 FAIL。
- [ ] **Step 3:** 实现 `job_quota` 两个函数与常量。
- [ ] **Step 4:** 实现 `first_contact` 改动与执行器 `'screened_out'` 分支。
- [ ] **Step 5:** `daily_report`：`EVENT_RULES` 增加两条事件文字；`_batches` 返回值加 `screened_out_after_review`、`screening_unavailable`；日报行改为“…审阅后暂不联系 X 位，不符合必须项 Y 位，初筛暂不可用 Z 次”。
- [ ] **Step 6:** 全量单测 + ruff。
- [ ] **Step 7:** 提交 `Screen recommended candidates against must-haves before first contact`。

---

### Task 5: 首聊决策提示改为复核口径

**Files:**
- Modify: `cloakup/decision_runtime.py`（`payload` 中 `first_contact_checks`，约 200–215 行）
- Test: `tests/test_decision_runtime.py`

**Interfaces:**
- Consumes: 候选人 `screening`。
- Produces: `content['first_contact_checks']` 增加 `'screening': {'passed': bool, 'unclear_count': int}`（无必须项时 `{'passed': None, 'unclear_count': 0}`），只放这两个值，不放条目文字或证据。`note` 中“有与 job.requirements 或本岗位职业关键词相关的经历线索时应联系，明显无关时再等待”改为：“程序已按本岗位必须项初筛通过（passed 为 null 表示本岗位未设必须项）；再对照 professional_context 与 job.requirements 复核，明显不相关时选择等待。”

- [ ] **Step 1: 写失败测试** `test_first_contact_payload_carries_screening_summary`：候选人 `screening={'outcome':'pass','unclear_ids':['mh_b'],...}` → `content['first_contact_checks']['screening']=={'passed':True,'unclear_count':1}`，且 `json.dumps(content)` 不含必须项文字与证据。
- [ ] **Step 2:** 确认 FAIL。
- [ ] **Step 3:** 实现。若已有测试断言旧 note 原文，同步更新断言。
- [ ] **Step 4:** 全量单测 + ruff。
- [ ] **Step 5:** 提交 `Make the first-contact decision a review after screening`。

---

### Task 6: 必须项改动走安全点（job_changes 新动作）与接口

**Files:**
- Modify: `cloakup/job_changes.py`
- Modify: `cloakup/workbench.py`（`do_POST` 路由，参考 1628 行 `/api/tasks/<id>/jobs`）
- Test: `tests/test_job_changes.py`、`tests/test_workbench*.py`（找现有路由测试所在文件跟随）

**Interfaces:**
- Consumes: `job_sourcing.checked_must_haves`、`screening.split_feedback`、`agent_workspace.start_job_test`、`agent_workspace.normalize`。
- Produces:
  - `job_changes.ACTIONS['must_haves'] = '更新必须项'`。`request(manager, task_id, {'action':'must_haves','case_id':cid,'must_haves':[...]})`：校验 → 以生效版本派生 bundle，只替换该岗位 `source['sourcing']['must_haves']` → `start_job_test(task_id, bundle, cid, job_change_id=...)` → 状态 `testing`（与 `add` 同路径，`settle` 通过转 `pending`）。同一岗位已有 OPEN 变更时拒绝（沿用现有检查）。
  - `apply` 中 `action=='must_haves'`：确认岗位仍是成员，用测试 bundle 中该岗位 `sourcing.must_haves` 替换当前 bundle 对应项，`touched=[cid]`，写 `case['job_sourcing']['must_haves']`；**不**提升 `autonomy.revision`、不清候选人决策状态（旧初筛结论靠 revision 自然过期）；验收覆盖校验与 `add` 相同（运行时签名、任务摘要、岗位快照摘要）。
  - 接口：`POST /api/tasks/<id>/jobs`（现有）接受 `action=must_haves`；新增 `POST /api/tasks/<id>/must-haves/split` `{'case_id','text'}` → `{'items':[...]}`（只预览，不保存）。写请求沿用 `X-Cloakup-Token`。
  - 失败与超时待办沿用 `attention` 现有 `job_change`／`job_change_stuck`，不另加。

- [ ] **Step 1: 写失败测试**

```python
def test_must_haves_change_tests_only_that_job_then_applies_at_safe_point(...):
    rec = jc.request(m, task_id, {'action':'must_haves','case_id':cid,'must_haves':MH})
    assert rec['status']=='testing' and test_record['case_ids']==[cid]
    # 模拟验收通过 → settle → apply_pending
    assert m.cases.load(cid)['job_sourcing']['must_haves'][0]['text']==MH[0]['text']
    assert account_task['release_id']==applied['version_id']     # 运行中任务已换绑，未暂停
def test_must_haves_change_keeps_candidate_decisions(...)      # decision_wait_until、harness_decision 原样保留
def test_must_haves_failed_validation_keeps_old_list(...)      # 验收失败 → status failed，岗位仍是旧清单
def test_split_endpoint_previews_without_saving(...)
```

- [ ] **Step 2:** 确认 FAIL。
- [ ] **Step 3:** 实现 `job_changes` 新动作；`describe` 输出“更新必须项「岗位名」”。
- [ ] **Step 4:** 实现两个接口。
- [ ] **Step 5:** 全量单测 + ruff。
- [ ] **Step 6:** 提交 `Apply must-have edits at the executor safe point`。

---

### Task 7: 初筛不可用待办

**Files:**
- Modify: `cloakup/attention.py`（参照 178–192 行 `outreach_deferrals` 的写法）
- Test: `tests/test_attention.py`

**Interfaces:**
- Consumes: 运行记录 `job['screening_failures']`（Task 4）。
- Produces: 当天某岗位 `n >= 3` 且 `last_failed_at > last_ok_at` → 待办 key `'screening_unavailable:'+day+':'+cid`，kind `screening_unavailable`，标题“岗位初筛暂不可用”，正文“岗位「X」今天首聊前初筛已失败 N 次。已做：失败的候选人先放下、稍后再看，其他工作照常，没有向候选人发消息。需要：查看模型网关是否可用；恢复后此待办自动解除。”之后一次成功（`last_ok_at` 更新）或跨天即自动关闭。

- [ ] **Step 1: 写失败测试** `test_screening_unavailable_todo_opens_and_closes`：n=3 无成功 → 有待办；`last_ok_at` 晚于 `last_failed_at` → reconcile 后关闭；n=2 → 无待办。
- [ ] **Step 2:** 确认 FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量单测 + ruff。
- [ ] **Step 5:** 提交 `Raise a todo when pre-contact screening keeps failing`。

---

### Task 8: 简历评分使用必须项

**Files:**
- Modify: `cloakup/resume_scoring.py`
- Test: `tests/test_resume_scoring.py`

**Interfaces:**
- Produces: `evaluate` 载荷增加 `must_haves`（`[{'id','text'}]`，无 `note`）和 `precontact_unclear`（首聊前 `unclear` 条目的 text 列表）；`SCORE_SCHEMA` 增加可选 `must_haves: [{'id','verdict','evidence'}]`；返回值增加 `must_haves` 与 `unmet`（text 列表）、`to_verify`（仍为 `unclear` 的 text 列表）；任一 `unmet` 时 `score=min(score,2)`。`evaluation_lines` 在有 `unmet` 时加一行“不符合必须项：…”，有 `to_verify` 时加一行“待核实：…”。无必须项时行为与今天完全一致。

- [ ] **Step 1: 写失败测试** `test_unmet_must_have_caps_score_at_two`（模型给 5 分且一条 `unmet` → 返回 score 2，`evaluation_lines` 含“不符合必须项”）；`test_unclear_listed_to_verify`；`test_no_must_haves_unchanged`（载荷无 `must_haves` 键）。
- [ ] **Step 2:** 确认 FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量单测 + ruff。
- [ ] **Step 5:** 提交 `Score resumes against the job's must-haves`。

---

### Task 9: 维护页面与统计

**Files:**
- Modify: `cloakup/screening.py`（加统计函数）
- Modify: `cloakup/workbench.py`（`snapshot` 中各岗位附 `screening_stats`）
- Modify: `cloakup/today.py`（`NUMBERS` 与岗位行）
- Modify: `cloakup/web/agents.js`（任务“岗位与事实”区块）、`cloakup/web/sourcing.js`（岗位详情只读区块）
- Test: `tests/test_screening.py`、`tests/test_today.py`

**Interfaces:**
- Produces:
  - `screening.stats(case: dict, since: float) -> dict`：`{'reviewed': int, 'rejected': int, 'unclear': int, 'contacted': int, 'by_item': {must_have_id: int}}`，`reviewed` 按 `profile_reviewed_at>=since` 计，`rejected`/`by_item` 按 `screening.at>=since and outcome=='reject'` 中各 `unmet` 条目计，`contacted` 按当期首聊占位计（复用 `job_quota.attempted_on` 口径）。
  - `snapshot` 每个岗位附 `screening_stats`（近 7 天）；`today.NUMBERS` 增加 `'profiles_reviewed','screened_out'`（当天）。
  - 前端区块按 spec §5.2 草图：标题“必须项（N 条）”、`[+ 录入客户反馈]`（弹出文本框 → 调 `/must-haves/split` 预览 → 管理员可逐条改删 → 确认后 `POST /jobs action=must_haves`）、每条显示来源／日期／客户原话、`[改] [删]`、底部统计行“近 7 天初筛：审阅 N · 不符 M（第 k 条 x，…）· 开聊 K”。有变更在验收或排队时显示“等待生效”，期间禁用编辑。清单为空时显示“本岗位未设必须项，首聊前不做初筛”（今天页该岗位行也显示同一提示）。岗位详情页（`sourcing.js`）对已加入任务的岗位只读展示清单，并给“去修改”链接到任务页对应岗位。

- [ ] **Step 1: 写失败测试** `test_stats_counts_by_item`（3 位审阅、2 位因 mh_a 被拒、1 位 pass 且已开聊 → `{'reviewed':3,'rejected':2,'by_item':{'mh_a':2},'contacted':1,...}`）；`test_today_reports_screening_numbers`。
- [ ] **Step 2:** 确认 FAIL。
- [ ] **Step 3:** 实现 `stats`、`snapshot`、`today`。
- [ ] **Step 4:** 实现前端区块；`.venv/bin/python scripts/docker_up.py --reuse-runtime` 后在浏览器打开工作台，录入一条反馈→预览→确认，看到“等待生效”，验收结束后条目出现；截图确认岗位详情页只读区块与“去修改”链接。
- [ ] **Step 5:** 全量单测 + ruff。
- [ ] **Step 6:** 提交 `Show and edit must-haves with screening stats on the task page`。

---

### Task 10: 规则文字同步与上线前演练

**Files:**
- Modify: `CLAUDE.md`（约束 6 加第三个例外；约束 7 注明首聊前初筛送脱敏资料正文、属独立调用不进 Harness 载荷）
- Modify: `cloakup/job_quota.py`（`task_target` docstring：额度是上限不是目标，11-27 管理员决定宁缺毋滥）
- Modify: `docs/workbench-map.md`（任务页“岗位与事实”加必须项）
- Create: `.tmp_incident/screening_dryrun.py`（不提交）

- [ ] **Step 1:** 改上述文字，提交 `Document must-have screening rules`。
- [ ] **Step 2:** 演练脚本：按 CLAUDE.md“只读演练”拷 `cloakup/` 到容器 `/tmp/newpkg`，用 `/run/secrets/postgres_password` 组装 `DATABASE_URL`，对每个设了必须项的岗位，取已开聊候选人的 `online_resume.sections` 拼成 overview，调用 `screening.screen`（只读：不写 case，不发消息），输出每人 `outcome`、逐条结论、后来是否回复／有无简历，写到 `data/analysis/2026-11-27-screening-dryrun.md`。
- [ ] **Step 3:** 抽约 20 条交给管理员看，等管理员确认误杀可以接受。**未确认前不上线。**
- [ ] **Step 4:** 上线：重建 → `draft` → `test`（validate）→ `publish` → `resume`（流程见 CLAUDE.md“发布签名”）；用 `verification-before-completion` 以日志中的 `precontact_screening` 记录、今天页数字为证据确认生效。

---

## 与 spec 的差异（实现者照此执行）

- spec §5.2 说“复用规划对话录入”。规划对话产出的是整单草案、走完整发布，与“必须项在安全点生效”冲突，因此录入改为任务页的“录入客户反馈”（模型拆条 → 管理员确认 → `job_changes` 安全点生效）；规划对话不扩展。整单表单只负责不把必须项丢掉。
- spec §5.2 说“隔离验收 9 个场景中加入一条”。现有验收场景都是对话回复场景，初筛的“输入不含身份信息、输出结构合法”由 Task 3 的单元测试和 `checked_must_haves` 的确定性校验保证，不新增模型验收场景，验收仍是 9 个场景。
