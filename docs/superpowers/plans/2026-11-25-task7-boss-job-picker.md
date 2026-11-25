# Task 7（修订）：从 BOSS 选岗位绑定（2026-11-25）

## 为什么修订
14:44 补采：职位管理列表行里没有 encryptId；推荐页岗位下拉的选项 `value` 就是 encryptId，且只有出现在下拉里的岗位才能寻访（docs/design/boss-job-pages.md）。管理员确认：取 id 用推荐页下拉、点开下拉一次可以；不会有同名岗位。

## 流程（不变的部分）
选岗位 → 绑定（读编辑页 JD、建岗位档案、生成计划草稿，即现有 `boss_bind`）→ 管理员在岗位里确认问候语、寻访配置、启用自动化 → 在任务“岗位与首聊额度”区加入（现有 `job_changes`，只验收新岗位）。本任务只替换第一步“粘贴链接”，加入任务的规则不动。

## 改动
1. **`cloakup/boss_jobs.py`（新）**
   - `read_recommend_options(session)`：打开推荐页（复用 `page_health.heal`），点开岗位下拉一次（`boss_recommend.SWITCH_LABEL`，原生点击），读 `li.job-item` 的 `value` 与文字，不选选项。
   - `read_job_rows(session)`：打开职位管理页，等列表 frame 有内容后读每行岗位名、城市等标签、状态（`status-opening` → open，`已关闭` → closed，其余 other）。只读“全部”标签当前页，上限 50 行（不翻页，超出时在结果里注明）。
   - `merge(options, rows)`：按“选项文字以岗位名开头”对应（无同名岗位）。每项 `{encrypt_id|None, title, city, status, recommendable}`；列表里有、下拉里没有的 → `recommendable=False`。
   - 读完回到沟通页；登录、验证按页面级问题报出。
2. **`cloakup/browser_errands.py`（新）**：浏览器代办记录 `browser_errand`（`job_list`／`job_bind`）。执行器运行中：只在安全点、执行时段内做，一次最多做一条；失败只标记这一条。执行器未运行：直接作为一次 BOSS 操作执行（与现有 `boss_bind` 相同的浏览器占用规则）。
3. **`workbench.py` 路由**
   - `GET /api/boss/jobs`：最近一次读取结果 + 读取时间，并标注“已绑定（对应岗位档案）/ 已在本任务 / 由其他任务管理 / 平台上尚未可推荐”。
   - `POST /api/boss/jobs/refresh`：登记或直接执行一次读取。
   - `POST /api/boss/jobs/bind {encrypt_ids:[...]}`：只接受最近一次读取里 `recommendable` 且未绑定的；按 `https://www.zhipin.com/web/chat/job/edit?encryptId=<id>` 走现有绑定（`validate_job_url` 校验），每个岗位一条代办。
4. **`autopilot.py`**：执行器安全点调用 `browser_errands.run_pending`，做完回沟通页继续。写 `logbook.log`（actor=executor）。
5. **前端 `web/sourcing.js`**：“绑定已创建的岗位”改为列表：刷新按钮、读取时间、每行岗位名／城市／状态；可绑定的可多选；不可推荐的灰色，提示“平台上还未生效为可推荐岗位，稍后刷新再试”；已绑定的显示“已绑定”。保留粘贴链接作为备用。
6. **待办**：代办失败在 `attention.reconcile` 生成待办（说明哪个岗位、卡在哪一步），重试成功或放弃后关闭。

## 测试（先写失败测试）
- 下拉选项与列表行合并：id、名称、状态、不可推荐标记；读下拉不点选项。
- 只能绑定可推荐且未绑定的岗位；拼出的地址通过 `validate_job_url`。
- 代办：时段外、非安全点不执行；失败只标记自己；执行器未运行时直接执行。
- 页面级问题（登录、验证）照常停止并生成待办。

## 影响与上线
- 不改任何发给候选人的内容；岗位列表只在本地，不进模型（绑定读 JD 后生成计划草稿沿用现有逻辑）。
- 改 `autopilot.py`、`workbench.py`（签名文件），上线走 重建 → 草案 → 验收 → 发布 → 恢复，可与 Task 9 一起上线。
- 上线后：刷新列表 → 绑定第二个岗位 → 配置 → 加入任务 → 管理员在场看第一次推荐页切换（Task 8 真实验证）。
