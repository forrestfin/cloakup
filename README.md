# Cloakup

本机运行的 AI 浏览器自动化工作台。基于 CloakBrowser 操作网页，由 DeepSeek Harness SDK（Anthropic 兼容模型网关）做决策，确定性的本机执行网关负责授权、去重与回执核验。

当前场景：

| 场景 | 说明 | 状态 |
| --- | --- | --- |
| `boss_sourcing` | BOSS 直聘候选人寻访与跟进 | 已接入 |
| `liepin_sourcing` | 猎聘寻访，复用同一套任务与岗位设施 | 页面动作未接入 |
| `web` | 通用网页，仅公开文字 | 已接入 |

场景登记见 `cloakup/scenarios.py`。

## 执行模型

观察 → Harness 选择一个结构化动作 → 执行网关校验（授权、暂停、时间窗口、额度、事实来源、去重、发送占位）→ 浏览器执行 → 按真实回执核验 → 反馈到下一轮。

- 模型只决定下一步做什么；动作能否执行由程序决定。
- 候选人姓名、称呼、联系方式、年龄性别、附件文件名不进入模型。对话理解在管理员授权后使用脱敏文字（单位、学校、城市保留）；脱敏只隐去、不整段拒绝。Harness 决策只接收意图标签和资料状态。
- 送达无法确认时记为 `unknown`，不重发。
- 会话选择恢复后的核验轮次只读（`autopilot.recovery_pass`），只允许同意接收候选人主动发起的简历。
- 配置修改先成为草案，隔离验收通过后才能原子发布；发布不会自动启动任务。
- 服务重启时，原先在运行的任务先标为暂停（原因 `service_restart`），随后自动核验环境并恢复；核验不通过则保持暂停，在状态历史写明原因（`auto_resume_failed:<原因>`），并生成“服务重启后任务未自动恢复”待办。注意：更新执行代码或模型会使已发布版本的验收签名失效，这种重启无法自动恢复，需要重新建立草案、验收并发布。设置 `CLOAKUP_AUTO_RESUME=0` 可关闭自动恢复。
- 主动追聊有对话体验底线：无回复者最多追 1 次，回复后沉默最多 2 次，未读不追，已读后至少隔 3 天，只在 9–20 点发。

架构图：`docs/images/cloakup-execution-agent-architecture.png`

## 启动（Docker）

```bash
# 首次启动：交互输入 CloakBrowser 授权（不回显、不落盘）
python scripts/docker_up.py --license

# 更新代码后复用运行中容器的已有凭据
python scripts/docker_up.py --reuse-runtime
```

模型凭据通过环境变量传入：`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY`、`CLOAKUP_MODEL`。飞书凭据可放在 `data/docker/lark.env`。

- 工作台：<http://127.0.0.1:8766>（`CLOAKUP_PUBLIC_PORT`）
- 浏览器实时画面（noVNC）：<http://127.0.0.1:6080>（`CLOAKUP_BROWSER_PORT`）

`data/` 下的数据库密码和凭据文件已在 `.gitignore` 中，不要提交。

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock && pip install --no-deps -e .
pip install ruff

python -m pytest tests            # 单元测试（test_*.py）
ruff check .                      # 静态检查，配置见 pyproject.toml
```

单元测试不依赖本机环境变量或真实凭据。`tests/e2e_*.py` 需要真实浏览器与运行中的工作台，不在默认测试集里。

命令行入口 `cloakup`：`web`（启动工作台）、`report`（执行日报）、`run`、`recipe`、`ask`、`batch`、`open`、`check-api`，详见 `cloakup --help`。

## 执行日报

每天一份，只读本地记录（运行事件、任务暂停历史、岗位审计与消息、待办、飞书通知），不访问网站、不调用模型。内容：

- 异常与自动修复：每类异常的次数、结果、能否自动修复、是否造成暂停，以及暂停后手动恢复是否立刻通过
- 操作分析：开聊批次、新开聊被暂缓的时长与原因、来信响应时长、日终未处理来信、执行时段内的长时间空档
- 建议：根据以上数据给出的处理建议

工作台在次日 00:10 后生成前一天的报告；服务停机错过的日期，恢复运行后自动补生成（最多 14 天）。报告存入数据库，并写到 `<root>/reports/YYYY-MM-DD.md`（Docker 中为 `/data/reports`，绑定到本机 `data/reports/`）。

结构化日志写在 `<root>/logs/<类别>-YYYY-MM-DD.jsonl`（本机 `data/logs/`），类别为 event、state、action、model、error，保留 30 天，详见 `cloakup/logbook.py`。时区默认 `Asia/Shanghai`，可用 `CLOAKUP_REPORT_TZ` 修改。

```bash
curl http://127.0.0.1:8766/api/daily-reports                 # 列表
curl http://127.0.0.1:8766/api/daily-reports/2026-11-20.md   # Markdown
docker compose exec app cloakup report --root /data --day 2026-11-20   # 生成某天；不带 --day 为今天截至目前
docker compose exec app cloakup report --root /data --backfill         # 补生成缺失日期
```

## 目录

```
cloakup/               核心包
  workbench.py         工作台 HTTP 服务与任务管理
  autopilot.py         账号执行器：收件同步、回复、首聊
  decision_runtime.py  Harness 持续决策
  boss_*.py            BOSS 页面适配
  agent_workspace.py   规划端与执行端 Agent、草案与发布
  scenario_workspace.py
  web/                 工作台前端（原生 JS）
docker/                容器入口与 supervisord 配置
docs/                  设计文档
tests/                 单元测试与 e2e 测试
scripts/               启动与评估脚本
```

## 设计文档

- `docs/architecture-boundaries.md`：执行职责、动作账本与各项业务策略的演变
- `docs/decision-runtime.md`：Harness 持续决策执行器
- `docs/agent-workspaces.md`：规划端与执行端 Agent、版本与隔离验证
- `docs/answering-policy.md`、`docs/job-sourcing-boundaries.md`、`docs/task-notifications.md`
- `.cursor/rules/core-principles.mdc`：两条最高开发原则
