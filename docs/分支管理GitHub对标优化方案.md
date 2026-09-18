# 分支管理模块 GitHub 对标优化方案

> 版本：v1.0 | 日期：2026-09-02 | 分析对象：`routers/branches.py`、`repositories/branch_repo.py`、`repositories/commit_repo.py`、`database/schema.py`（branches / merge_requests / knowledge_commits）
> 关联文档：[分支版本管理方案.md](./分支版本管理方案.md)（Git 式提交链设计）、[分支管理页面优化方案.md](./分支管理页面优化方案.md)（前端布局交互）
> 定位：**后端数据模型与合并语义层**的 GitHub 对标优化；不改前端骨架，与上述两份既有方案互补而非替代。
> 状态：**待确认**（确认优先级与决策点后实施）

---

## 0. 结论先行

1. **模块骨架已对齐 Git 心智**（dev/release/personal/local 四类分支、MR 审批闭环、属性级冲突解决、发布门禁、提交链+head_commit 指针、发布版本号），既有《分支版本管理方案》Task1-5 已落地。本次对标聚焦 GitHub 与现状的**语义级差距**，而非推倒重来。
2. **发现 3 个正确性缺陷（P0）**，均属于"GitHub 有明确机制、现状缺失会出错"的硬差距：
   - **冲突检测只在 MR 创建时执行一次**：MR 创建后源/目标分支数据变化，approve 时不会重算冲突，目标分支会被静默覆盖（GitHub 持续异步重算 mergeability）；
   - **冲突检测漏检**：属性 key 比对只取交集（`set(src) & set(tgt)`），源新增 key 不报冲突；"删除 vs 修改"冲突未实现（既有方案 4.5 已列为增强，未落地）；
   - **操作人硬编码**：`create_merge_request` 写死 `created_by='李工'`，`resolve_merge` 写死 `reviewed_by='李工'`，`snapshot_documents` 写死 `'王工'`——无法回答 GitHub "who merged"。
3. **5 个 GitHub 核心机制缺失（P1）**：merge commit 双父指针（→ ahead/behind 提示）、三点式 diff（merge-base）、分支保护规则配置化、MR 状态机（open/merged/closed + reopen）、commit 内容哈希。
4. **个人分支全量物理复制（fork = O(N) 数据拷贝）**与 GitHub"分支=指针，创建零成本"差距最大，但改造涉及查询层全面改写，建议单独立项（P1-4，含可行性评估），不与 P0 混做。
5. **两处刻意偏离 GitHub 应保留**：不引入 rebase（审计红线，不重写历史）；回滚仅限 release 分支（发布级操作）。对齐不等于照搬。

---

## 1. 现状盘点

### 1.1 数据模型

| 表 | 关键列 | 语义 |
|---|---|---|
| `branches` | name(唯一)、branch_type(dev/release/personal/local)、parent_branch、head_commit | 分支=数据工作区 + 提交链指针 |
| `merge_requests` | source/target_branch、conflicts、resolutions、status(pending/approved/rejected)、prev_release_snapshot、release_version | 合并请求（≈PR） |
| `knowledge_commits` | branch、parent_id（分支内单父链）、kind(import/review/merge/manual/rollback)、changes、snapshot | 提交链（≈commit） |
| `knowledge_publish_logs` | version_label、commit_id、action(publish/rollback) | 发布版本（≈tag） |

### 1.2 核心流程语义

| 流程 | 现状语义 |
|---|---|
| 创建个人分支 | **全量复制**基线（dev/release）实体+关系到新分支（物理拷贝，非指针） |
| 冲突检测 | MR 创建时同步计算：同名实体 name + **属性交集 key** 差异 |
| 冲突解决 | resolutions 按 (entity_id, field) pick source/target/manual |
| personal→dev 合并 | 迁移语义：实体行 UPDATE branch，冲突行按决策改写后删源行 |
| dev→release 合并 | 快照复制语义：源覆盖目标同 id 行 + 发布留痕 + prev_release_snapshot |
| 回滚 | ① merge 级：release 快照整分支还原（一次性消费）；② commit 级：仅 release 分支可 revert |
| 门禁 | 未评审实体禁止进 release（创建+approve 双重校验）；approve 需 branch_release:review_merge 权限 |

---

## 2. GitHub 实现方案要点（对标基准）

| # | GitHub 机制 | 实现本质 | 对本模块的意义 |
|---|---|---|---|
| G1 | **分支 = ref 指针** | 分支仅是指向 commit 的可变指针，创建/删除零成本 | 个人分支创建从 O(N) 拷贝变为零成本 |
| G2 | **mergeability 持续计算** | PR 创建后后台异步重算可合并性，数据变化立即标 "conflicted" | MR 生命周期内冲突状态始终新鲜 |
| G3 | **merge commit 双父指针** | 合并产生双父提交，天然记录两分支交汇点 | ahead/behind 计数、三点式 diff 的基础 |
| G4 | **三点式 diff（base...merge-base）** | diff 对比"公共祖先→head"，而非两个分支头 | 已合并的变更不再出现在后续 diff 中 |
| G5 | **PR 状态机** | open → merged / closed；closed 可 reopen；支持 draft | 驳回≠终结，可重新打开；草稿态保护误触发 |
| G6 | **分支保护规则** | 按分支配置：required reviews 数量、必需检查、禁止直接 push、禁止删除 | dev 目前无任何直接写保护，规则硬编码在代码里 |
| G7 | **合并策略可选** | merge / squash / rebase merge | squash：个人分支多次提交压缩为 dev 一条提交 |
| G8 | **PR 元数据完整** | author/reviewers/assignees/title/body/labels/timeline | 现状仅 created_by(硬编码)/reviewed_by(硬编码) |
| G9 | **commit SHA 完整性** | 提交 id = 内容哈希，防篡改、可校验 | 审计场景下数据可信性 |
| G10 | **分支删除宽松 + 建议合并后删** | 数据都在提交链上，删分支不丢数据；提供 auto-delete 选项 | 现状要求分支必须清空才能删 |

---

## 3. 差距分析

| GitHub 能力 | 现状 | 差距等级 | 影响 |
|---|---|---|---|
| G2 冲突持续计算 | 仅 MR 创建时算一次；approve 只检查**存量清单**未解决项 | **P0 缺陷** | 目标分支在 MR 存续期被修改 → approve 静默覆盖，无冲突提示 |
| G2/G3 冲突完整性 | 属性交集 key 才比对；无删除vs修改冲突 | **P0 缺陷** | 源新增属性 key / 一方删除一方修改时不报冲突，合并语义退化 |
| G8 操作人真实化 | created_by/reviewed_by 硬编码"李工/王工" | **P0 缺陷** | 审计追溯失真；多用户场景无法追责 |
| G5 PR 状态机 | pending/approved/rejected 三态终局，无 reopen/draft | P1 | 驳回后需求变更只能删 MR 重建，历史断裂 |
| G3 双父 merge commit | merge 提交只记目标分支，不记源分支头 | P1 | 无法计算 ahead/behind（GitHub 分支页标志性能力） |
| G4 三点式 diff | 两分支当前状态全量比对（两点式） | P1 | 语义上"上次发布后 dev 又改了什么"需人工比对 |
| G6 分支保护配置化 | release 只读、保护名单硬编码于 routers/branches.py | P1 | 无法按需保护任意分支；规则改动需改代码 |
| G7 squash 合并 | 个人→dev 合并后源提交链与目标无关联 | P2 | 个人分支多次迭代的提交在 dev 侧不可见（无 squash 归并） |
| G9 commit 哈希 | 自增 id，无内容哈希 | P1 | 提交记录可被改库篡改而无感知（审计红线场景） |
| G1 零成本分支 | 全量物理复制 | P1（大改，单独立项） | N 个个人分支 = N 份全量数据；实体多时创建慢、库膨胀 |
| G10 合并后删分支 | 有数据/有子分支即禁删；无软删除 | P2 | 合并完成后分支清理门槛高，分支堆积 |
| rebase | 明确不引入 | 保留偏离 | 审计红线：知识数据不重写历史（正确决策） |
| 回滚任意分支 | revert 仅限 release | 保留偏离 | 回滚是发布级操作的定位合理（正确决策） |

---

## 4. 优化方案

### P0：正确性修复（合并语义安全，必须做）

#### P0-1 冲突检测时效性（对标 G2）

**问题**：`create_merge_request`（routers L189-206）算完冲突存入 `mr.conflicts`；`resolve_merge` approve 时只检查 `pending_conflicts(mr)`（存量清单），不重算。

**方案**：approve 前重算冲突并与存量清单比对：
- 重算结果与存量不一致 → 标记 MR `stale`（或直接返回 `conflict_changed` 拒绝 approve，前端引导刷新冲突清单）；
- resolutions 保留仍命中的 (entity_id, field) 决策，新增冲突待解决，已消失冲突自动视为 resolved；
- 前端 approve 报错提示"冲突清单已变化，请重新确认"（GitHub 同体验："This branch has conflicts that must be resolved"）。

**改动点**：`BranchRepo.resolve_merge` 开头插入 `_recompute_conflicts(mr)`；`merge_requests` 增列 `conflict_updated_at`。

#### P0-2 冲突检测完整性（对标 G2/G3）

**问题**：routers L201 `for key in set(src_props) & set(tgt_props)` 漏检属性 key 增删；"删除 vs 修改"（dev 删除实体、个人分支修改同实体，或反向）未实现（既有方案 4.5 承诺未落地）。

**方案**：
- 属性比对改为 `set(src) | set(tgt)`（新增 key 一方值为空视为冲突，pick 语义不变）；
- 增加 `delete_modify` 冲突类型：diff 阶段检测源 deprecated/缺失 vs 目标非 deprecated（双向），冲突项结构加 `conflict_type` 字段（`property` / `delete_modify`），解决动作增加 `keep_delete` / `keep_modify`；
- `resolve_merge` 两种冲突类型分别处理。

**改动点**：`create_merge_request` 冲突检测段、`resolve_merge` 合并执行段、`get_conflict_status` 输出结构（前端冲突面板需同步适配）。

#### P0-3 操作人真实化（对标 G8）

**问题**：三处硬编码（branch_repo L264 `'李工'`、L313 `'李工'`、L594/602 `'王工'`）。路由层已有 `_actor(user)`（branches.py L24）但未传入 repo。

**方案**：`create_merge_request` / `resolve_merge` / `snapshot_documents` 增加 `actor` 参数，路由层传 `_actor(user)`；存量数据不动（兼容），audit 与 MR 字段一致。

**改动点**：branch_repo 三个方法签名 + branches.py 两处调用。改动小、无风险。

### P1：GitHub 核心机制补齐

#### P1-1 MR 状态机对齐（对标 G5）

- `merge_requests.status` 枚举扩展为：`draft`（草稿，不可 approve）→ `open`（待评审）→ `merged` / `closed`；`closed` 可 `reopen` 回 `open`；
- 存量映射：`pending→open`、`approved→merged`、`rejected→closed`（**兼容问题见决策点 D2**）；
- 增加字段：`title`（默认"source→target"）、`review_note`（驳回原因，GitHub request changes 必填意见）。

#### P1-2 merge commit 双父指针 + ahead/behind（对标 G3/G4）

- `_merge_commit` 时在 `knowledge_commits` 增记 `source_branch` + `source_head_commit`（合并时源分支 head）；
- 分支 API 返回 ahead/behind：dev 相对 release = 上次 merge commit 之后 dev 的新提交数（GitHub 分支列表 "3 commits behind release"）；
- diff API 支持三点式：`base=merge-base` 时只显示自上次合并以来的变更（dev 相对 release 的增量），作为 `/api/branches/diff` 的新参数 `mode=merge-base|full`，默认 full 兼容现有前端。

#### P1-3 分支保护规则配置化（对标 G6）

- `branches` 增列 `protection_rules`（JSON，默认 `{}`）：
  ```json
  {"direct_write": false, "require_review": true, "min_approvals": 1, "allow_delete": false}
  ```
- `_check_writable_branch` / `PROTECTED` 硬编码改为读规则；dev/release 预置规则与现状等价（行为不变），管理员可调整；
- 超管设置入口本期只做 API，UI 可后置。

#### P1-4 commit 内容哈希（对标 G9）

- `create_commit` 增加 `content_hash = sha256(branch|parent_id|kind|changes|snapshot)`；
- 提供校验 API/启动自检：重算哈希不匹配 → 审计告警（防篡改）。

#### P1-5 个人分支懒 fork（对标 G1，大改单独立项）

- 现状：创建个人分支全量复制（branch_repo L30-48），N 个个人分支 = N 份全量数据；
- 方案方向：personal 分支只存**增量行**（diverged entities/relations），查询层 `branch IN (personal, parent)` + 个人分支优先（图谱消费侧已有该模式，可复用）；
- **风险高**：涉及图谱、审核、检索、合并全部读路径改写，须单独做可行性 PoC + 分阶段灰度，**建议确认后单独立项，不并入本次**。

### P2：体验扩展（按需）

| 项 | 内容 | 对标 |
|---|---|---|
| P2-1 合并后分支清理 | 个人分支合并回 dev 后：数据已迁移，允许直接删除（带确认弹窗），可选"合并后自动删分支" | G10 |
| P2-2 squash 合并 | personal→dev 合并选项：`squash=true` 时 dev 侧生成一条归并提交（源分支多次提交→一条） | G7 |
| P2-3 MR 评审意见流 | resolutions 之外增加评审评论时间线（approve/reject 必填意见已并入 P1-1） | G8 |
| P2-4 cherry-pick | 既有方案 P2 已列，维持 | — |

### 刻意保留的偏离（不对齐）

| 偏离 | 理由 |
|---|---|
| 不引入 rebase / 历史重写 | 审计红线（既有方案明确） |
| revert 仅限 release 分支 | 回滚定位为发布级操作，非个人工作区操作 |
| rollback 快照一次性消费 | 防二次回滚冲突；如需"撤销回滚"，通过新增正向发布实现（与 GitHub revert-the-revert 等价但语义更安全） |

---

## 5. 决策点（请确认）

| # | 决策 | 选项 | 建议 |
|---|---|---|---|
| D1 | P0 三项是否全部实施 | A. 全部（约 3 个文件改动，含前端冲突面板适配） / B. 仅 P0-1+P0-3（不动前端） | **A**（P0-2 漏检是真实缺陷） |
| D2 | MR 状态枚举 | A. 改库枚举 open/merged/closed + 存量迁移（前端同步改） / B. 保留 pending/approved/rejected，仅新增 draft+reopen 语义 | **A**（一次到位，避免长期双语义） |
| D3 | P1 范围 | A. P1-1~P1-4 全做 / B. 仅 P1-1+P1-2（状态机+双父指针，用户可感知） / C. P1 全部推迟 | **B**（P1-3/4 可后置） |
| D4 | 个人分支懒 fork（P1-5） | A. 本次立项做 PoC / B. 单独立项后续评估 | **B** |
| D5 | P2-1 合并后删分支 | A. 纳入本次 / B. 后续 | **B**（依赖 P1-1 状态机先行） |

## 6. 实施路径（按 D1=A、D2=A、D3=B 的建议组合）

| 阶段 | 内容 | 改动文件 | 验证 |
|---|---|---|---|
| 阶段1 | P0-3 操作人真实化（独立小改，先行） | branch_repo.py、branches.py | `tests/manual_verify/verify_branch_v2.py` 补断言：MR created_by = 当前用户 |
| 阶段2 | P0-1 冲突重算 | branch_repo.py、schema.py（增列）、branches.py | MR 创建后修改目标分支实体 → approve 被拦截并提示冲突已变化 |
| 阶段3 | P0-2 冲突完整性 | branches.py（检测段）、branch_repo.py（合并段）、前端冲突面板 | 属性新增 key / 删除vs修改 场景均产生冲突且可解决 |
| 阶段4 | P1-1 状态机 + P1-2 双父指针 | schema.py、branch_repo.py、commit_repo.py、branches.py、前端 MR 列表 | draft→open→merged/closed→reopen 全链路；dev 分支显示 ahead/behind |
| 回归 | 全链路：导入→审核→个人分支→合并 dev→发布 release→回滚 | — | 既有 verify_branch_* 脚本 + 新增用例全绿 |

> 每阶段独立可交付、可回退；阶段2/3 涉及 merge_requests 表结构变更，走 `_migrate_columns` 幂等补列，存量数据自动映射。

---

## 7. 验收要点

1. MR 创建后目标分支数据变化 → approve 返回 `conflict_changed`，重算冲突清单正确（P0-1）
2. 源分支新增属性 key / 源删除目标修改 → 均出现在冲突清单且可 pick 解决，合并结果与决策一致（P0-2）
3. MR 创建人/审批人 = 实际登录用户，审计日志一致（P0-3）
4. draft 不可 approve；closed 可 reopen；驳回必填意见（P1-1）
5. dev 分支列表显示 "N commits ahead / M behind release"，merge-base 模式 diff 只含增量（P1-2）
6. 既有发布门禁、回滚、提交链、消费侧行为全部不回归
