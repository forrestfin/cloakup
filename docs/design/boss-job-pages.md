# BOSS 职位页与推荐页岗位切换：页面观察记录

2026-11-25 13:31 用只读诊断 `read_job_pages` 采集（任务暂停中、管理员在场，用时 35 秒，`business_writes=0`，最后回到沟通页）。原始采集在容器 `/data/analysis/job-pages-20261125-053130.json`，不进仓库。

## 入口
- 左侧菜单“职位管理”是链接：`#wrap > div:nth-of-type(1) > div > dl:nth-of-type(2) > dt > a`，`href=/web/chat/job/list`，可直接导航。
- 职位管理页的列表在 frame `/web/frame/job_v2/list?jobversion=<版本号>` 里。

## 推荐页（`/web/chat/recommend`）
- 列表在 frame `/web/frame/recommend/?jobid=<encryptJobId>&status=0&...&version=<版本号>&source=0`。frame URL 里的 `jobid` 就是岗位链接里的 `encryptId`，可以用来判断当前是哪个岗位（与 `boss_recommend` 现有的核对口径一致）。
- 岗位切换控件在 frame 内 `#headerWrap`：
  - 外层：`div.ui-dropmenu.job-selecter-wrap`
  - 点开：`div.ui-dropmenu-label`，文字为“岗位名 + 城市／薪资等”
  - 选项：`div.ui-dropmenu-list ul.job-list > li.job-item`，**`value` 属性就是 encryptJobId**，当前岗位带 `curr` 类；选项文字在 `span.label` 里
  - 下拉顶部有岗位搜索框 `input.chat-job-search`
- 同一个头部还有“推荐／新牛人／看过我”等标签（`li.tab-item[data-status]`，推荐为 `0`）和城市筛选，切换岗位时不要动它们。

## 对 Task 7／Task 8 的影响
- Task 8（推荐页切换岗位）：可以按 `li.job-item[value="<encryptId>"]` 精确定位目标岗位，不必靠岗位名匹配；点击后等 frame URL 的 `jobid` 变成目标值，再走 `verify_list`。原计划担心的“选项与岗位对不上”不成立。
- 只展开了一次下拉，没有选选项；当天账号下拉里只有 1 个岗位，所以“真正切换到另一个岗位”还没在真实页面上走过，需要第二个岗位开放后、管理员在场时验证一次。
- Task 7（读职位管理列表）：本次采集时 frame 里还只有空的 `<div id="app"></div>`（页面没渲染完就读了），列表结构没拿到。实现 `read_job_list` 前，诊断要改成等 frame 内容出现再读，然后补采一次。

## Task 8 实现（`boss_recommend.switch_job`）
- 已在目标岗位不点；否则展开下拉 → 按 `value` 点唯一选项 → 等 frame `jobid` 变成目标且卡片列表换过（约 15 秒上限）→ 再走 `verify_list`。
- 任何失败都只暂缓该岗位开聊（文字“推荐页面未能切换到当前岗位（原因），已暂缓该岗位开聊”），任务不暂停。
- 已知局限：下拉岗位很多时，靠下的选项可能在可视区域外，点击会被拒绝、该岗位反复暂缓（当天 3 次后生成待办）。第二个岗位开放后，在管理员在场时做一次真实切换验证，再决定是否加滚动／搜索。

## 职位管理列表（2026-11-25 14:44 补采，诊断改为等 frame 渲染后读）
- frame `/web/frame/job_v2/list?jobversion=<版本号>`，Vue 渲染，约 15 秒内画好。
- 顶部标签 `.base-tabs .tab-item`：全部／开放中／待开放／审核未通过／已关闭；当前为“全部”。
- 每行 `ul.job-list-content > li.job-item-container`：
  - 岗位名 `.job-name`；`.info-labels .divider-label-text` 依次为公司、城市、区域、经验、学历、薪资、类型等（16:30 线上首读更正：第一个是公司，不是城市）
  - 状态 `.job-status-wrapper .status-box`，类名 `status-opening` 表示开放中（其他状态的类名未见到）
  - 操作 `.operation-container .operate-btn`（编辑／关闭）和“更多”菜单——不能碰
- **行里没有 encryptId**：没有链接、没有 data 属性，只有 Vue 内部数据。
- 当时列表有 2 个“开放中”岗位，但推荐页岗位下拉只出现 1 个（本任务岗位）；另一行底部有商业化倒计时条，推测还没生效为可推荐岗位。
- 对 Task 7 的影响：岗位 encryptId 的可靠来源是推荐页岗位下拉（`li.job-item[value]` + 选项文字），且只有出现在下拉里的岗位才能用于寻访；职位管理列表只能按名称补充公司／城市等标签与状态。
