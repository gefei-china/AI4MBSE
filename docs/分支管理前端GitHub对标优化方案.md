# 分支管理前端 GitHub 对标优化方案

> 版本：v1.0 | 日期：2026-09-03 | 分析对象：`static/index.html` 分支管理域（L640-670 页面骨架、L13467-13650 分支/KPI/提交时间线、L13822-13949 MR 列表/冲突解决、L13950+ 合并对比抽屉）
> 姊妹篇：[分支管理GitHub对标优化方案.md](./分支管理GitHub对标优化方案.md)（后端数据模型与合并语义，含决策点 D1-D5）
> 前置：2026-08-19《分支管理页面优化方案.md》（布局交互层）**已基本落地**，本方案在其上做 GitHub 语义增强，不推翻既有骨架。
> 状态：**F1–F4 已落地**（2026-09-23 逐项复核，附证据行号；F5 未落地）
> - F1 MR 状态机交互 ✅ `static/js/mods/27-branch.js:748 _mergeStatusOrd` / `:750 _mrStatusInfo`（draft📝/open🕐/merged🟪/closed❌）+ `:800+` reopen 操作
> - F2 冲突面板语义增强 ✅ `:816-835`（`conflict_type` 徽章 + `delete_modify` 双选 `keep_delete`/`keep_modify`）/ `:906-910`（`conflict_changed` 引导对话框列出新增/消失冲突）
> - F3 ahead/behind 徽章 + 三点式 diff ✅ `:40-43`（分支卡 `↑N ↓M` 徽章）/ `:484/:532/:552`（diff 模式切换 `full`|`merge-base`）
> - F4 人员真实化与意见展示 ✅ `:743`（搜索含 `created_by`/`reviewed_by`/`review_note`）/ `:796`（列表驳回意见）/ `:1339/:1367`（详情审批人与意见）
> - F5 P2 项 ❌ **未落地**（MR 时间线 Tab / 保护规则可视 / 合并后删分支 / cherry-pick 入口）
> ⚠️ **行号口径**：本文原文按 `static/index.html` 内联脚本标注（L13467+）；2026-09-23「合并请求页主从布局」重构后，该域已拆分至 `static/js/mods/27-branch.js`，**上列为拆分后行号**，引用时以文件为准。

---

## 0. 结论先行

1. **前端骨架已达标**：08-19 方案四项改造（KPI 概览条、分支按类型分组、MR 状态筛选 chips、操作按状态收敛）已在 `loadBranches`/`loadMerges`/`renderBranchMergeList` 落地，提交时间线（`openBranchCommits`）、发布日志聚合（`openPublishLogs`）、合并对比抽屉（`mrg-drawer` 双 Tab：差异分析 + 冲突解决）齐备。**本方案不再动布局，聚焦"GitHub 语义层"的交互增强**。
2. **前端改动强依赖后端决策**：本方案按建议组合（D1=A、D2=A、D3=B）设计；若 D2 选 B（保留 pending/approved/rejected 枚举），仅 F1 的状态映射表降级处理，其余不受影响。
3. **4 个 GitHub 前端标志性交互缺失**（用户可感知价值最高）：
   - **F1 MR 状态机交互**：GitHub 三色状态徽章（Open 绿 / Merged 紫 / Closed 灰）+ draft 草稿态 + closed 可 reopen——现状是"已驳回=终局只能删"，历史断裂；
   - **F2 冲突面板语义增强**：现状冲突项只有"属性值对比三选一"，不区分冲突类型；后端 P0-2 引入 `delete_modify` 冲突后需对应"保留删除 / 保留修改"双选 UI；后端 P0-1 引入 `conflict_changed` 拦截后需"冲突清单已变化"引导流程；
   - **F3 ahead/behind 徽章**：GitHub 分支列表 "This branch is X commits ahead, Y behind"——依赖后端 P1-2 merge commit 双父指针；
   - **F4 驳回必填意见 + 人员真实化**：GitHub request changes 必须写意见；MR 表已有 created_by 列展示（后端 P0-3 修复硬编码后自动变真实人，前端仅需补审批人/意见展示）。
4. **改动全部收敛在 `static/index.html` 单文件**（约 6 个函数改造 + 2 个新弹窗），无新依赖、无构建链变化，风险低。

---

## 1. 前端现状盘点

### 1.1 已落地（复用不动）

| 区域 | 函数/位置 | 现状 |
|---|---|---|
| KPI 概览条 | `renderBranchKpis` L13529 | 分支数/当前分支元素/待评审 MR/未解决冲突，红色告警可点击定位 |
| 分支分组列表 | `loadBranches` L13469 | 主开发/发布/个人·本地三组，两行式卡片，受保护分支只读降级 |
| 提交时间线 | `openBranchCommits` L13561 | kind 徽章+消息+变更数，点击展开变更清单，release 提交可回滚（预览→确认→执行） |
| 发布日志 | `openPublishLogs` L13646 | 按版本聚合卡片 + 实体清单折叠 |
| MR 列表 | `renderBranchMergeTable` L13852 | 状态筛选 chips、中文徽章、冲突进度徽章、时间列、操作按状态收敛 |
| 冲突解决 | `renderConflictItem` L13887 | 属性值对比 + source/target/manual 三选一，逐项保存 |
| 合并对比抽屉 | `openMergeDiffDrawer` L13956 | 差异分析 + 冲突解决双 Tab，approve/reject/rollback 操作 |
| 回滚 | `rollbackMergeRequest` L13941 | MR 级快照回滚（confirm 双确认） |

### 1.2 与 GitHub 前端的差距

| # | GitHub 交互 | 现状 | 差距 |
|---|---|---|---|
| U1 | PR 状态徽章三色系：Open 绿 / Merged 紫 / Closed 灰 | pending=w / approved=ok / rejected=r，语义对但色系与 GitHub 惯例不符（merged 应紫） | 小 |
| U2 | draft PR（草稿，标记灰色且不可 merge） | 无 | 中 |
| U3 | closed PR 可 reopen | 驳回后只能删除重建，决策历史断裂 | 中 |
| U4 | review 意见流：request changes 必填评论 | 驳回无意见输入，只有 audit 日志 | 中 |
| U5 | 冲突项类型区分（属性/删除vs修改） | 仅属性值对比三选一 | **高（配合后端 P0-2）** |
| U6 | "This branch has conflicts" 持续提示 | 冲突只在 MR 创建时算一次，前端无从感知数据变化 | **高（配合后端 P0-1）** |
| U7 | ahead/behind 计数徽章 | 无 | **高（配合后端 P1-2）** |
| U8 | PR conversation 时间线（事件流） | 无（只有创建时间列） | 低（P2） |
| U9 | Files changed 三点式 diff 切换 | diff 为两分支全量对比（两点式） | 中（配合后端 P1-2） |
| U10 | PR 标题可读化 | 标题恒为 "source → target" | 低 |
| U11 | 分支保护规则可视（⚙ 保护标识/规则摘要） | 仅 release 硬编码 🔒 提示 | 低（配合后端 P1-3，后置） |

---

## 2. 设计方案

### F1 MR 状态机交互（对应 U1/U2/U3/U4，依赖后端 D2）

**状态映射与徽章**（`_mergeStatusOrd`、`renderBranchMergeFilter`、`renderBranchMergeTable` 的 `stMap` 统一改造）：

| 后端状态 | 徽章 | 色 | 操作列 |
|---|---|---|---|
| draft | 📝 草稿 | 灰(mut) | 发布评审 / 🗑 |
| open | 🕐 待评审 | 绿 | 对比分析 / 通过 / 驳回(必填意见) / 🗑 |
| merged | ✅ 已合并 | 紫 | 对比分析 / (release)↩ 回滚 |
| closed | ⬜ 已关闭 | 灰 | 对比分析 / ↻ 重新打开 / 🗑 |

- 紫色徽章新增 CSS 变量 `--purple-l/--purple-d`（复用 `.st` 结构，不改组件）；
- 筛选 chips 从 4 个扩到 5 个（全部/待评审/草稿/已合并/已关闭）；
- **reopen**：closed 行操作列出现"↻ 重新打开"，调 `POST /api/branches/merge-requests/{id}/reopen`（后端 P1-1），成功后刷新列表；
- **draft → open**：草稿行"发布评审"按钮调 `POST .../{id}/open`；
- **D2=B 降级方案**：保留 pending/approved/rejected 枚举，draft/reopen 仅在前端用 `merge_requests.status` 新增两个枚举值实现（后端仍需支持，仅不做存量迁移），徽章色系改造不变。

**新建合并请求弹窗增强**（`createMergeRequest` 弹窗，L18435 附近）：
- 增加"📝 存为草稿"复选（默认关）→ `draft: true`；
- 增加"标题"输入（默认自动生成 `source→target`，可改，U10）；
- 目标为 release 时显示发布版本号输入（已有，保留）。

**驳回必填意见**（`resolveMerge(id,'reject')` L13932）：
- 现状直接调 API；改为先弹 `confirmDialog` 升级版（含 textarea）——驳回意见 < 5 字不允许提交；
- 意见随 `action:'reject', review_note` 提交；MR 表格新增"意见"列（超长省略 + title 悬浮全文）。

### F2 冲突面板语义增强（对应 U5/U6，依赖后端 P0-1/P0-2）

**冲突类型徽章与双选 UI**（`renderConflictItem` L13887 改造）：

- 冲突项头部增加类型徽章：`[属性冲突]`（蓝）/ `[删除 vs 修改]`（橙）——读后端新增的 `conflict_type` 字段，缺省视为 `property`（兼容存量 MR）；
- `delete_modify` 类型渲染双选卡（替代三选一）：

```
┌ 实体「电池包」 [删除 vs 修改] ⚠ 未解决 ────────────┐
│ 源分支(dev)：已删除(标记废弃)                       │
│ 目标分支(release)：修改为 status=reviewed            │
│ ○ 保留删除（合并后该实体在目标分支置废弃）              │
│ ○ 保留修改（合并后该实体保留目标分支版本）              │
│                                [💾 保存]            │
└──────────────────────────────────────────────────┘
```

- pick 值复用后端 P0-2 定义的 `keep_delete` / `keep_modify`，接口不变（`resolve-conflict` 透传）。

**conflict_changed 引导流程**（`resolveMerge(id,'approve')` L13932 改造）：

- 后端 P0-1 返回 `conflict_changed` 错误码时，不再 toast 一闪而过，改弹对话框：

```
⚠ 冲突清单已变化
创建合并请求后分支数据发生了修改，冲突清单需要重新确认：
  · 新增冲突 2 处（实体「电池包」.status、「泵」.name）
  · 已消失冲突 1 处（之前的决策已自动保留）
[刷新冲突清单]   [取消]
```

- "刷新冲突清单" → 调后端重算接口（或重新拉取 MR 详情）→ 切到冲突 Tab 并滚动到首个新增冲突（复用 `branchKpiGoConflict` 的定位模式）；
- 冲突 Tab 顶部增加**清单版本提示条**：`冲突清单更新于 {conflict_updated_at}`（后端 P0-1 新增列），数据变化时徽章变橙提醒。

### F3 ahead/behind 徽章 + 三点式 diff 切换（对应 U7/U9，依赖后端 P1-2）

**分支卡片**（`branchCardHtml` L13497）：

- dev 卡片第二行追加：`<span>↑3 领先 · ↓1 落后 release</span>`（数据来自 `/api/branches` 返回的 `ahead`/`behind` 字段）；
- ahead>0 时徽章高亮（提示"有未发布变更"），点击直接打开 `openBranchDiffDrawer('dev')`；
- 个人分支同理显示相对基线 parent_branch 的 ahead/behind。

**diff 模式切换**（`openBranchDiffDrawer` 差异分析 Tab 头部）：

- 增加模式切换 chips：`[全量对比] [上次合并以来]`（默认全量，兼容现状）；
- "上次合并以来"= 后端 `/api/branches/diff?mode=merge-base`（三点式，只显示上次 merge commit 之后 dev 的增量）；
- 模式选择持久化到 localStorage。

### F4 人员真实化与时间线展示（对应 U4，依赖后端 P0-3/P1-1）

- MR 表格"创建时间"列扩展为两行：创建人 + 审批人（`reviewed_by`），去除后端硬编码后自动生效，前端无逻辑改动，仅补列展示；
- MR 详情抽屉标题行（`loadMergeDetail` L13988）追加审批信息：`· 由 {reviewed_by} 于 {resolved_at} {通过/驳回}（{review_note}）`。

### F5 P2 项（后续按需）

| 项 | 内容 | 依赖 |
|---|---|---|
| MR 时间线 Tab | 对比抽屉增加第三个 Tab「时间线」：创建→草稿转正式→冲突解决→审批/驳回→回滚，事件流展示（GitHub conversation） | 后端 P2-3 评审意见流 |
| 分支保护标识 | 分支卡片显示规则摘要徽章（如 `🛡 需1人评审 · 禁直写`），超管可视化 | 后端 P1-3 保护规则配置化 |
| 合并后删分支 | MR merged 且源为个人分支时，列表行出现"清理分支"按钮 | 后端 P2-1 |
| cherry-pick 入口 | 实体详情"修改历史"中单条提交支持「局部发布到 release」 | 后端 P2 cherry-pick |

---

## 3. 改动点清单（函数级）

| # | 改动 | 位置（index.html） | 规模 |
|---|---|---|---|
| 1 | 状态枚举映射 + 徽章色系（含紫色） | `_mergeStatusOrd` L13837、`renderBranchMergeFilter` L13839、`stMap` L13869 + CSS 变量 | 小 |
| 2 | draft/open/reopen 操作与按钮 | `renderBranchMergeTable` L13881-13883、新函数 `openMrForReview`/`reopenMr` | 小 |
| 3 | 新建 MR 弹窗：草稿复选 + 标题 | createMergeRequest 弹窗 L18435 附近 | 小 |
| 4 | 驳回必填意见弹窗 + 意见列 | `resolveMerge` L13932、表格列 L13856 | 小 |
| 5 | 冲突类型徽章 + delete_modify 双选卡 | `renderConflictItem` L13887 重构（按 conflict_type 分支渲染） | 中 |
| 6 | conflict_changed 对话框 + 清单版本提示条 | `resolveMerge` L13932、`loadMergeConflicts` L14105 | 中 |
| 7 | ahead/behind 徽章 | `branchCardHtml` L13497 | 小 |
| 8 | diff 模式切换 chips | `openBranchDiffDrawer` L13956 差异 Tab | 小 |
| 9 | MR 详情审批信息行 | `loadMergeDetail` L13988 | 小 |

**兼容性原则**：所有新字段（`conflict_type`、`ahead`/`behind`、`review_note`、`draft`）读取时缺省兜底（`|| 'property'`、`|| 0`、`|| ''`），**后端未升级时前端不报错、功能自动降级**——支持前后端分批上线。

---

## 4. 与后端方案的依赖关系

| 前端改动 | 依赖后端 | 无后端时的降级行为 |
|---|---|---|
| F1 状态机（draft/reopen） | D2 + P1-1 | reopen 调用返回 400 → toast 提示"当前版本不支持"；draft 复选不发送 |
| F2 delete_modify 双选 | P0-2 | conflict_type 缺省按 property 渲染（现状不变） |
| F2 conflict_changed 引导 | P0-1 | 后端仍返回旧错误文案 → 走现状 toast |
| F3 ahead/behind | P1-2 | 字段缺省 → 徽章不渲染 |
| F3 merge-base diff | P1-2 | mode 参数被后端忽略 → 返回全量（现状） |
| F4 审批人展示 | P0-3 / P1-1 | 显示 '-' |

**建议实施顺序**（与后端 4 阶段对齐）：

| 阶段 | 后端 | 前端（本方案） |
|---|---|---|
| 1 | P0-3 操作人真实化 | 改动 9（审批信息行，随手做） |
| 2 | P0-1 冲突重算 | 改动 6（conflict_changed 引导） |
| 3 | P0-2 冲突完整性 | 改动 5（冲突类型双选卡） |
| 4 | P1-1 + P1-2 | 改动 1-4（状态机全套）+ 7、8（ahead/behind + diff 切换） |
| 回归 | verify_branch_* 脚本 | 手动回归：MR 全生命周期（draft→open→冲突解决→merged/closed→reopen）+ 降级场景（模拟后端旧版响应） |

---

## 5. 验收要点

1. MR 列表五态徽章正确（含紫色 merged），draft 不可通过，closed 可 reopen 且历史保留
2. 驳回必须填写意见（<5 字拦截），意见在列表与详情抽屉可见
3. delete_modify 冲突显示双选卡，选择"保留删除/保留修改"后合并结果与决策一致
4. MR 创建后修改目标分支数据 → 通过时弹"冲突清单已变化"对话框 → 刷新后新增冲突待解决、已消失冲突自动消解
5. dev 分支卡片显示 ahead/behind，点击可跳转 diff；diff 抽屉可切换"上次合并以来"模式且只含增量
6. **降级验收**：对旧后端（无新字段）打开前端，所有区域正常渲染无 JS 报错，功能回到现状
7. 既有 KPI 定位、提交时间线、发布日志、回滚流程不回归
