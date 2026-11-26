# JD 原文作为答复依据 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 候选人问到 JD 里写明的内容时，语义层直接用 JD 原句作答，不再转顾问待办。

**Architecture:** `facts.jd_statements(case)` 把 `job.snapshot` 的 JD 正文和地点拆句、过滤，返回可对外的原句。`conversation_semantics.source_catalog` 把这些原句作为 `jd:<序号>` 来源加入目录，模型仍然只能按 ID 挑句子。与已确认事实矛盾的 JD 句在进入目录前剔除。

**Tech Stack:** Python 3.12，pytest，项目 `.venv`。

**Spec:** `docs/superpowers/specs/2026-11-26-jd-as-answer-source-design.md`

## Global Constraints
- 只用通用规则：代码、正则、提示词里不写死任何岗位或行业的词（CLAUDE.md 约束 8）。小节标题词表只能用通用词，例如“岗位职责、任职要求、我们提供”。
- 薪酬不报数字：凡是 `communication_policy.check_compensation_disclosure` 拦下的、或命中 `facts.MONEY` 的句子，整句丢弃。
- 不改 `redact`，候选人身份不进模型。新增到载荷里的只有岗位文字。
- 已确认事实和 FAQ 优先于 JD。
- 目录里 JD 句最多 40 条，超出按原文顺序截断。单句须通过 `autonomy.validate_reply(text,[],[])`，即长度 ≤240，且不含 `DETAIL_TERMS` 和 `CONTACT`。
- `conversation_semantics.VERSION` 从 3 改为 4。
- 跟随文件既有写法（紧凑单行），不大面积重排。测试注释写明现场：“11-26 16:04 qe工程师 候选人问汇报对象”。

## Review Focus
1. JD 是一整行、用空格分段（BOSS 抓取的实际形态），没有换行。拆句仍要按条目编号切开。见 Task 1 测试 `test_jd_statements_split_single_line_numbered_jd`。
2. 地点里带客户公司名和门牌，例如“武汉东西湖区中盈智源有限公司1001”“深圳宝安区深圳西部硅谷-B座六楼”。只能留下“深圳宝安区”这一级。见 Task 1 `test_jd_statements_location_keeps_district_only`。
3. 薪酬句没有“薪”字但有数字，例如“8-12K＋绩效奖金”“15k”。见 Task 1 `test_jd_statements_drop_money_company_and_address`。
4. 旧 JD 与已确认地点矛盾，例如 JD 写武汉、事实是深圳宝安。JD 句必须剔除，不能让模型挑到。见 Task 2 `test_catalog_drops_jd_line_that_contradicts_confirmed_fact`。
5. 没有 snapshot 或 JD 为空的历史案例，以及隔离验收用的 bundle 案例：目录与改动前完全一致，不报错。见 Task 2 `test_catalog_without_jd_is_unchanged`。

---

### Task 1: `facts.jd_statements`

**Files:**
- Modify: `cloakup/facts.py`（文件末尾新增）
- Test: `tests/test_facts.py`

**Interfaces:**
- Produces: `jd_statements(case: dict) -> list[str]`。返回去掉编号、过滤后的原句，按原文顺序排列。若 `snapshot.location` 能匹配 `PLACE`，则第一项为 `'工作地点：'+PLACE匹配`。最多 40 条，去重。没有 snapshot 时返回 `[]`。

**规则（实现者照此写，正则细节自定）：**
- 拆分点：换行、`。`、`；`、`;`，以及条目编号前的位置。条目编号包括 `\d+、`、`（[一二三四五六七八九十]+）`、`[一二三四五六七八九十]+、`。拆完后去掉句首编号和空白。
- 丢弃“纯小节标题”片段，即去掉标点后整段是以下通用词之一：岗位职责、工作职责、职位描述、岗位描述、任职要求、任职资格、岗位要求、硬性条件、软性要求、加分项、我们提供、福利待遇、岗位亮点。
- 丢弃以下片段：
  - 长度 <4 的片段；
  - 命中 `MONEY` 的；
  - `check_compensation_disclosure` 抛错的；
  - 命中 `recruiting_ai.IDENTIFIER` 的；
  - 包含 `snapshot['company']`（非空时）的；
  - 命中机构后缀 `集团|股份|分公司|研究院|事务所` 的；
  - 命中门牌 `[\dA-Za-z一二三四五六七八九十]+\s*(?:号楼|号|栋|座|楼|室|层)` 的；
  - `autonomy.validate_reply(text,[],[])` 抛错的。
- 保留的句子末尾统一补一个“。”：先去掉原有的句末 `。；;，,`，再加“。”。

- [ ] **Step 1: 写失败测试**（复用该文件的 `_case`，可以传入自定义 snapshot）

```python
QE_JD=('岗位职责 1、全流程质量管控：负责来料（IQC）、制程（IPQC）、出货（OQC）三个环节的检验标准制定与执行监督。 '
       '任职要求 （一）硬性条件 1、大专及以上学历，电子、电气、材料、机械等理工科专业。 2、3年以上电子制造业质量工作经验，5年以上优先。 '
       '我们提供 1、8-12K＋绩效奖金，具体结构面议。 2、直接向品质负责人汇报，体系优化有实际决策空间。 3、服务头部消费电子／新能源客户，产品线稳定')

def _jd_case(jd=QE_JD,location='深圳宝安区深圳西部硅谷-B座六楼',company='君润人力'):
    return _case(job={'label':'qe工程师','revision':2,'snapshot':{'title':'qe工程师','company':company,'location':location,'requirements':jd,'platform_fields':[]}})

def test_jd_statements_split_single_line_numbered_jd():
    # 11-26 16:04 qe工程师 候选人问汇报对象：JD 写明“直接向品质负责人汇报”，系统却转了顾问。
    out=jd_statements(_jd_case())
    assert '直接向品质负责人汇报，体系优化有实际决策空间。' in out
    assert '服务头部消费电子／新能源客户，产品线稳定。' in out
    assert '3年以上电子制造业质量工作经验，5年以上优先。' in out
    assert not any(s.rstrip('。') in ('岗位职责','任职要求','硬性条件','我们提供') for s in out)
    assert not any(re.match(r'\d+、|（',s) for s in out)

def test_jd_statements_location_keeps_district_only():
    assert jd_statements(_jd_case())[0]=='工作地点：深圳宝安区。'
    assert jd_statements(_jd_case(location='武汉东西湖区中盈智源有限公司1001'))[0]=='工作地点：武汉东西湖区。'

def test_jd_statements_drop_money_company_and_address():
    jd='1、底薪15k起。 2、月度8-12K＋绩效奖金。 3、客户为某某科技有限公司。 4、隶属华东集团。 5、办公在B座六楼。 6、君润人力直招。 7、负责供应商审核。'
    out=jd_statements(_jd_case(jd=jd))
    assert out[1:]==['负责供应商审核。']

def test_jd_statements_empty_without_snapshot_and_capped_at_40():
    assert jd_statements({'job':{}})==[]
    many=' '.join(f'{i}、负责第{i}项检验工作' for i in range(1,61))
    assert len(jd_statements(_jd_case(jd=many,location='')))==40
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_facts.py -q -k jd_statements`
Expected: FAIL（ImportError: cannot import name 'jd_statements'）。先把 `jd_statements` 加到测试文件的 import 行。

- [ ] **Step 3: 在 `cloakup/facts.py` 实现 `jd_statements(case)`，规则见上**

- [ ] **Step 4: 运行通过**

Run: `.venv/bin/python -m pytest tests/test_facts.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add cloakup/facts.py tests/test_facts.py
git commit -m "Split the job description into answerable statements"
```

正文写根因：11-26 16:04 qe工程师 候选人问汇报对象，JD 写明了却转了顾问；影响：只新增函数，尚未接入。

---

### Task 2: JD 原句进入语义来源目录

**Files:**
- Modify: `cloakup/conversation_semantics.py`：`VERSION`、`SYSTEM`、`source_catalog`
- Test: `tests/test_conversation_semantics.py`

**Interfaces:**
- Consumes: `facts.jd_statements(case) -> list[str]`
- Produces: `source_catalog(case)` 新增条目 `'jd:<i>'`（i 为 `jd_statements` 结果里的下标），值为 `{'text':句子,'kind':'jd','revision':case['job'].get('revision',0)}`。这些条目排在 fact 和 faq 条目之后，并经过同样的 safe 校验。

**矛盾剔除规则：** 对每条 JD 句，按 `facts.ANCHORS` 判断它属于 `work_arrangement`、`experience` 还是 `education`。如果该类型存在 `confirmed` 且 `scope=='job'` 的事实，就从 JD 句里取出“关键词元”：
- `work_arrangement`：特殊处理，不取词元。如果句中能 `PLACE.search` 到地名，但已确认地点事实里的任何一个 `PLACE` 地名都不出现在句中，就剔除。原因是 `PLACE` 会从句首起懒惰匹配，可能取到“点在深圳宝安区”这类带前缀的片段，不能直接拿来当词元比对；
- `experience`：所有阿拉伯数字；
- `education`：`大专|本科|硕士|博士`。

只要有一个词元不在该类型所有已确认事实的拼接文本里，这条 JD 句就剔除。这条比设计稿“内容不同就剔除”收窄了一些，避免把“3年以上…5年以上优先”这类与“3-5年”不矛盾的细化句误删。

- [ ] **Step 1: 写失败测试**（复用 `example` fixture）

```python
QE_JD='我们提供 1、8-12K＋绩效奖金，具体结构面议。 2、直接向品质负责人汇报，体系优化有实际决策空间。'

def _with_jd(case,jd=QE_JD,location='深圳宝安区深圳西部硅谷-B座六楼'):
    case['job']={'label':'qe工程师','revision':2,'snapshot':{'company':'君润人力','location':location,'requirements':jd}}
    return case

def test_catalog_offers_jd_statements_after_facts_and_faq(example):
    # 11-26 16:04 qe工程师 候选人问汇报对象：JD 有答案却转了顾问。
    case,_=example;catalog=cs.source_catalog(_with_jd(case))
    jd={k:v for k,v in catalog.items() if k.startswith('jd:')}
    assert '直接向品质负责人汇报，体系优化有实际决策空间。' in [v['text'] for v in jd.values()]
    assert all(v['kind']=='jd' and v['revision']==2 for v in jd.values())
    assert not any('12K' in v['text'] for v in catalog.values())
    assert list(catalog).index('faq:0')<min(list(catalog).index(k) for k in jd)

def test_answer_grounded_on_jd_statement_is_valid(example):
    case,p=example;_with_jd(case)
    p['history']=p['inbox']=[{'external_id':'m2','direction':'in','text':'您好，这个职位汇报对象是谁'}]
    data=cs.model_input(case,p,None)
    ident=next(k for k,v in data['sources'].items() if v['text'].startswith('直接向品质负责人汇报'))
    out=cs.validate(dict(route='answer',reason='JD 写明汇报对象',parts=[ident],grounding_ids=[ident],candidate_question='',missing_information=[],resume_readiness='ready'),data)
    assert out['action']=='answer' and out['text']=='直接向品质负责人汇报，体系优化有实际决策空间。'

def test_catalog_drops_jd_line_that_contradicts_confirmed_fact(example):
    case,_=example;_with_jd(case,jd='1、工作地点在武汉东西湖区。 2、3年以上质量工作经验，5年以上优先。',location='武汉东西湖区')
    case['confirmed_facts']=[{'id':'loc','kind':'work_arrangement','scope':'job','value':'工作地点：深圳宝安区','status':'confirmed'},
                             {'id':'exp','kind':'experience','scope':'job','value':'经验要求：3-5年','status':'confirmed'}]
    texts=[v['text'] for k,v in cs.source_catalog(case).items() if k.startswith('jd:')]
    assert not any('武汉' in t for t in texts)
    assert '3年以上质量工作经验，5年以上优先。' in texts

def test_catalog_without_jd_is_unchanged(example):
    case,_=example
    assert not any(k.startswith('jd:') for k in cs.source_catalog(case))
    case['job']={'revision':1,'snapshot':{'requirements':''}}
    assert not any(k.startswith('jd:') for k in cs.source_catalog(case))

def test_semantic_version_and_prompt_cover_jd():
    assert cs.VERSION==4
    assert 'JD' in cs.SYSTEM and '已确认事实' in cs.SYSTEM
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_conversation_semantics.py -q`
Expected: 新增 5 条 FAIL，原有用例 PASS。

- [ ] **Step 3: 实现**
  - `VERSION=4`。
  - 在 `SYSTEM` 末尾追加一句（原文照写）：`sources 中 kind=jd 的条目是岗位 JD 原句，与已确认事实、FAQ 一样算有确认来源，可直接引用作答；同一问题上 JD 句与已确认事实或 FAQ 说法不同时，只能引用已确认事实或 FAQ。`
  - `source_catalog`：在 faq 之后加入 `jd_statements(case)` 中未被矛盾剔除的句子，然后走现有的 safe 循环。

- [ ] **Step 4: 运行全部单测与静态检查**

Run: `.venv/bin/python -m pytest tests -q && .venv/bin/ruff check .`
Expected: 全部 PASS，ruff 无报错。若 `test_agent_workspace.py` 里有断言目录内容或 `SEMANTIC_VERSION` 的用例失败，只更新这些断言，不改行为。

- [ ] **Step 5: 提交**

```bash
git add cloakup/conversation_semantics.py tests/test_conversation_semantics.py
git commit -m "Answer candidates from the job description's own sentences"
```

正文写根因（同 Task 1）、过滤与优先级规则；影响：签名文件变化，需要重新发布。

---

### Task 3: 只读演练与上线（控制方执行，不派子代理）

- [ ] **Step 1: 写演练脚本** `.tmp_incident/jd_rehearsal.py`，不提交。
  - 按 CLAUDE.md“只读演练”的做法：拷贝 `cloakup/` 到容器 `/tmp/newpkg`，`chmod -R a+rX`，脚本里先 `sys.path.insert(0,'/tmp/newpkg')`，用 `/run/secrets/postgres_password` 组装 `DATABASE_URL`。
  - 对任务 `d697e9d8503e4129823580710524d7f2` 里每个未撤下的岗位，列出：
    - 进目录的 JD 句；
    - 被拦的片段及原因（薪酬、公司、门牌、标识、校验、矛盾）。
  - 为了标出原因，脚本可以本地复刻过滤逻辑，只读不写。
- [ ] **Step 2: 运行演练**，把结果写到 `data/analysis/2026-11-26-jd-rehearsal.md`，交管理员确认。**管理员确认前不发布。**
- [ ] **Step 3: 上线**（管理员确认后执行）：
  1. 暂停任务，等执行器空闲；
  2. `.venv/bin/python scripts/docker_up.py --reuse-runtime`；
  3. `POST /api/tasks/$T/agent-workspace/draft {}`；
  4. `.../test`，mode=validate，等全部场景通过；
  5. `.../publish`；
  6. `POST /api/account-tasks/$T {"action":"resume"}`。

  写请求都带 `X-Cloakup-Token`。
- [ ] **Step 4: 验证**：上线后第一条命中 JD 的回复，从日志和回执确认引用的是 `jd:` 来源，正文与 JD 原句逐字一致。
