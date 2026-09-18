# AI 建模 SysML v2 版本管理与入库设计方案

> 版本：v1.1 · 2026-08-16
>
> 范围：① AI 输出交互改造——消息级「采纳/拒绝」按钮移除，确认动作收敛到关键环节；② AI 生成的 SysML v2 代码支持版本管理，最终采纳版本可「入库」，入库过程覆盖实体/关系/属性的审核确认、合并与消歧。
>
> **P0 已实施（2026-08-16）**：消息级按钮移除 + `sysml_versions` 版本链建档 + `sysml_to_candidates` 统一写入 v2g_candidates（SYSM- 批次）+ 消歧打标 + 复用 v2g/confirm 入库 + 版本状态回填 + 前端版本栏/入库向导/采纳标记。已通过后端 API 端到端断言 + playwright 页面交互验证。

---

## 一、需求理解与目标

### 1.1 需求背景

1. **交互噪音**：当前 AI 消息按 HIL 级别（L1/L2）在每条消息后渲染「采纳/拒绝」「确认/修改重跑/拒绝」按钮（[static/index.html](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/static/index.html#L2979-L3003)），用户每次生成都要被迫表态，反馈过于密集且与真实写库动作脱节。
2. **模型资产缺失闭环**：AI 生成的 SysML v2 代码目前只作为「会话产物」预览（artifacts），虽然有分支版本管理（knowledge_commits），但**代码本身没有版本链**，也没有"从代码到图谱"的正式入库通道——现有的 `/api/knowledge/sysml/import`（O-3）是解析后直接 `create_node/create_edge` 落库，**跳过候选/消歧/审核**，与文档抽取 v2g 的「候选→确认入库→消歧→正式审核」两级治理链不一致。

### 1.2 目标

- **G1**：AI 输出后不再展示消息级采纳/拒绝按钮；仅在「确认写入、审核通过」等关键环节提示用户是否采纳。
- **G2**：AI 生成的 SysML v2 代码形成**可追溯的版本链**（生成即建档，每次修订生成新版本，采纳版本可标记）。
- **G3**：最终采纳的 SysML 版本可**入库**——解析为实体/关系/属性候选，**统一写入 v2g 待审候选（v2g_candidates），与文档抽取共用同一候选治理入口**（消歧/确认/审核/入库/提交），并衔接既有分支版本管理（提交/合并/发布）。
- **G4**：行业调研结论沉淀为本方案设计依据（见第二章）。

---

## 二、行业调研结论（优秀方案对照）

### 2.1 MBSE 模型仓库与版本管理

| 方案 | 做法 | 可借鉴点 |
|------|------|---------|
| **CATIA Magic Teamwork Cloud (TWC) + GitLab CI/CD**（达索，2026） | 模型存中心仓库，工程师提交时打 tag → webhook 触发流水线 → 通过 REST API 导出 SysML v2 文本 → 提交进 GitLab，形成模型变更审计链 | ①「打 tag 提交 = 一次里程碑版本」②模型与文本导出分离，文本进 Git 做 diff 审查 |
| **SysGit**（SysML v2 原生 Git 架构，2026） | 模型以 `.sysml` 文件形式活在 Git 仓库，载入内存图数据库；**解析错误禁止提交**；PR diff 页、验证工作流；MCP Server 让 AI Agent 直接在模型图上工作 | ① 版本控制管"模型要素"而非整文件 ② 提交前门禁（语法/校验）③ Agent 直接操作模型图 |
| **MBSE 模型版本谱系化迭代方法**（国知局专利，2026） | 属性图建模；DAG 记录版本演进；**主版本/合并版本/修订版本三重版本号**；首次全量存储、后续仅存差异；模型对比算法输出变更集与差异图谱；实时冲突用 DB 锁、非实时冲突人工决策 | ① 版本号语义化（主/合并/修订）② 差异存储 ③ 变更集 = 发布清单 ④ 冲突分级处理 |

### 2.2 知识图谱实体消歧与合并

| 方案 | 做法 | 可借鉴点 |
|------|------|---------|
| **sift-kg**（开源 LLM→KG 管线） | 抽取 → 构建 → **实体消歧（resolve）→ 人审（review：批准/驳回/跳过）→ 应用合并（apply）**；高置信（>0.85）自动批准、低置信自动驳回；每个实体/关系回链源文档 | ①「提议 → 人审 → 应用」三段式，规则只产出 proposal 不直接改数据 ② 置信度阈值分档 ③ 溯源闭环 |
| **Neo4j Agent Memory SAME_AS 模式** | embedding+fuzzy 组合打分：**高于自动合并阈值→自动合并；阈值与标记阈值之间→建 SAME_AS 待审关系（pending/confirmed/rejected）；低于→新建实体** | ① 三档决策（合并/待审/新建）② 同类型约束匹配（苹果公司≠苹果水果）③ 审核状态机 |
| **「提议+人审」消歧工程方案**（掘金，2026） | 默认不自动合并（合并不可逆）；**置信度规则梯子 + guard 否决链**（职责分离）；每个提议**必须附带证据**（命中哪条规则、依据、被哪个 guard 拦过）给人审；决策不可变审计 | ① 破坏性操作必须人审 ② 规则与否决层分离 ③ 提议带证据（人审不返工） |
| **SysML 元素语义融合**（Cambridge DESIGN 2026） | 利用知识图谱 + LLM 实体对齐 + 相似度阈值做 SysML 模型集成：**高相似自动合并、低相似保持分离、中间相似触发歧义处理**（对照术语表/上下文决策） | ① 三态决策与 MBSE 场景结合 ② 术语表/本体作为消歧上下文 |

### 2.3 共性设计原则（本方案来源）

1. **默认不自动合并**：合并是破坏性且近乎不可逆的操作，规则只产生「提议/候选」，最终由人审确认（sift-kg、掘金方案）。
2. **置信度三档决策**：高置信自动处理、中间置信进人审队列、低置信保持独立（Neo4j SAME_AS、Cambridge）。
3. **提议必须带证据**：命中哪条规则、相似度分数、来源版本——审核人不需返工查证。
4. **版本即提交**：模型变更以"原子变更集/提交"为单位留痕，可回答"这个版本包含什么、谁在何时改了什么"（TWC+Git、SysGit、专利方案）。
5. **入库前门禁**：语法/本体校验不过不落库（SysGit 解析错误禁止提交）。
6. **溯源完整**：每个入库元素回链到源（文档/模型版本/消息）。

---

## 三、现状与差距分析

### 3.1 现状（已具备，保留复用）

- **AI 建模页**：AI 生成 SysML v2 → `card_data.sysml_views` → 消息内 Cytoscape 缩略图预览 + 右侧预览分栏；生成即归档 `artifacts(kind=sysml)`（`_archive_artifacts`）。
- **消息反馈**：`POST /api/messages/{id}/feedback` 记录 approve/reject/modify 到 feedbacks 表（生成与反馈台账的数据源）。
- **v2g 两级治理链**（文档抽取）：`v2g_candidates` 候选 → 确认入库（含 `dup_action` 消歧前移 skip/align/create）→ entities status=candidate → 正式审核队列（实体/关系）→ reviewed → 分支/发布。
- **消歧与合并**：`entity_dup_candidates`（blocking + 双判据，高分自动合并/低分建 pending）+ `entity_merges` 审计 + `merge_entities` 属性融合/关系重指向/可撤销。
- **分支版本管理**：`knowledge_commits`（import/review/merge/manual/rollback 提交链）+ 分支合并门禁 + 发布版本号/清单 + 提交级回滚。
- **SysML 导入（O-3）**：`/api/knowledge/sysml/import` 解析 → `GraphStore.create_node/create_edge` 直接落库（entities/relations，`graph_source='sysml_import'`，`sysml_import_id` 关联批次）。
- **HIL 强制写库确认**：`hil_service.py` 对写类工具（entity_create 等）生成 `hil_confirmations` 待确认队列。

### 3.2 差距清单

| # | 差距 | 现状 | 本次补齐 |
|---|------|------|---------|
| G1-1 | 消息级采纳/拒绝按钮噪音 | L1/L2 每条消息后渲染按钮 | 移除按钮，确认动作收敛到「确认写入/审核通过」关键点 |
| G1-2 | 反馈数据源 | 依赖按钮 sendFeedback | 由关键环节动作隐式/显式捕获（确认=采纳、修订=修正、驳回=拒绝），台账语义不变 |
| G2-1 | SysML 代码无版本链 | artifacts 只存最新快照，`sysml_views` 无版本语义 | 新增 `sysml_versions` 表：生成即建档 v0.x，修订递增，采纳版本标记 |
| G2-2 | 无法回答"该版本包含哪些要素" | 无 | 版本内容 = 解析出的实体/关系/属性清单 + diff |
| G3-1 | SysML 入库跳过候选/消歧/审核 | `import_sysml` 直接 create | **复用 v2g_candidates 统一候选入口**：解析为候选写入 v2g_candidates（batch 前缀区分来源），走既有消歧→确认→审核→入库链路 |
| G3-2 | 与已入库图谱重复未检测 | create 时仅 name 查重 | 复用 v2g 实体消歧（`matching_status`）+ 关系三元组消歧（`rel_matching_status`），属性冲突并入候选 properties 检测 |
| G4-1 | 入库与提交/发布割裂 | import 无 commit | 一次入库 = 一次 `knowledge_commits`（kind=import，message 带版本号），发布走既有链路 |

---

## 四、总体方案

### 4.1 总体流程

```
┌─────────────────────────────────────────────────────────────────────┐
│  AI 建模会话（pg-ai）                                               │
│                                                                     │
│  用户输入建模需求 → AI 生成 SysML v2 → 预览（消息内 + 右栏产物）        │
│         │                                                            │
│         ▼  （G1：无采纳/拒绝按钮，直接展示）                          │
│  ① 生成即建档：sysml_versions 版本链 v0.1 → v0.2 …（修订即新版本）      │
│         │                                                            │
│         ▼  （G2：版本管理）                                          │
│  ② 用户选择「入库」（关键确认点 #1）→ 弹出入库向导                    │
│         │                                                            │
│         ▼  （G3：统一候选入口 = v2g 治理链）                          │
│  ③ 解析：SysML v2 → 实体/关系候选，统一写入 v2g_candidates           │
│     （batch_id=SYSM-xxx，source_doc=AI建模·v0.2，属性并入实体 properties）│
│  ④ 消歧与合并检测（复用 v2g 消歧，写 matching_status /                │
│     rel_matching_status）：                                           │
│      ├─ 高置信（>0.9）   → 自动对齐（align，复用已有实体/边）          │
│      ├─ 中间置信（0.6~0.9）→ 人工决策（merge/skip/create）            │
│      └─ 低置信（<0.6）   → 新建（create，带源版本溯源）               │
│         │                                                            │
│         ▼  （关键确认点 #2：审核通过）                               │
│  ⑤ 用户确认候选（复用 v2g_confirm，含消歧决策）→ 正式审核队列          │
│  ⑥ 审核通过 → 落库 entities/relations（source_type=ai_generated，    │
│     sysml_version_id 关联版本）→ 一次 knowledge_commits 提交          │
│         │                                                            │
│         ▼  （衔接既有分支版本管理）                                  │
│  ⑦ dev 分支内积累 → 创建合并请求 → 审批 → 发布 release（版本号/清单）   │
└─────────────────────────────────────────────────────────────────────┘
```

> **统一入口说明**：AI 建模入库的候选**不新建独立候选表**，直接写入 `v2g_candidates`（batch 前缀 `SYSM-` 与文档抽取 `DOC-` 区分），「抽取治理中心」可统一按批次/来源筛选处理；确认/驳回/编辑/消歧全部复用既有 v2g 接口与前端组件，仅增加「AI 建模」来源标识。

### 4.2 设计原则

1. **确认点收敛**：全局只有两类人机确认——**写入前确认**（入库向导）与**审核确认**（审核队列）；消息本身只展示与迭代。
2. **破坏性操作默认不自动执行**：合并、消歧、入库均以「候选 + 人审」完成，规则只产出建议（对齐行业原则 1/2）。
3. **一切可追溯**：版本 → 候选 → 实体/关系/属性 → 提交 → 发布，全链路留痕可回滚。
4. **复用既有机制**：不新造轮子——消歧复用 `entity_dup_candidates`、审核复用实体/关系审核队列、提交复用 `knowledge_commits`、发布复用合并门禁。

---

## 五、需求 1：AI 输出交互改造（去采纳/拒绝按钮）

### 5.1 消息渲染改造

**改动点**：[static/index.html](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/static/index.html#L2979-L3003) 中 L1/L2 的操作按钮块整体移除：

- **移除**：L1 的「采纳/拒绝」按钮、L2 的「确认/修改重跑/拒绝」按钮及 `#revise-*` 修订框。
- **保留**：L0 直出逻辑（原本就无按钮）；消息反馈徽章（已采纳/已拒绝/已修订）展示保留。
- **替代交互**（消息尾部新增一条被动提示，不打断阅读）：
  - 含 SysML 产物 → 提示「💾 如需写入模型库，点击右上角『入库』」；
  - 提示条不携带任何确认/拒绝动作，仅引导下一步（符合"只在关键环节提示"）。
- **迭代方式**：用户直接在输入框提出修改要求（如"把载荷改成两个转发器"），AI 修订 → 生成新版本 v0.x+1（自然替代原"修改重跑"按钮的语义）。

### 5.2 关键确认点定义（唯一两处"是否采纳"提示）

| 关键点 | 触发时机 | 提示形式 | 采纳语义 |
|--------|---------|---------|---------|
| **确认写入（入库向导）** | 用户点击「入库」（作用于某条含 SysML 的消息/产物） | 居中卡片（`#lbx`）：展示候选清单 + 消歧建议 + 确认/取消 | 确认 → 进入候选/审核流程；取消 → 保持草稿版本 |
| **审核通过（审核队列）** | 候选确认后进入实体/关系审核队列的正式审核动作 | 审核页 confirm 按钮（沿用现有 `review_entity`/关系审核） | 审核通过 → 正式入库；驳回 → 候选标记 rejected 留痕 |

> 补充：`hil_confirmations`（L2 写工具强制确认队列）作为**运行时的第二道防线**保留——当 AI 编排流程尝试直接写库（entity_create 等）时仍拦截确认，与上述业务确认点互补，二者均命中时以后者（业务确认）为准，避免重复弹窗。

### 5.3 反馈数据捕获（台账语义不变）

移除按钮后，反馈由关键动作**隐式生成**，写入 feedbacks 表与生成台账：

| 原按钮 | 新捕获点 | 记录类型 |
|--------|---------|---------|
| 采纳 approve | 用户确认入库（候选确认） | approve |
| 修改重跑 modify | 用户针对该消息发起新一轮生成（修订产生新版本） | modify |
| 拒绝 reject | 入库向导中"全部驳回" / 审核队列驳回候选 | reject |

- 数据落 `feedbacks` 表（复用 `message_feedback` 逻辑，`context` 记录"经入库动作触发"）。
- 「生成与反馈」台账（FR-MG-5）与反馈统计（FR-HIL-2）无需改动，仅数据来源从按钮变为动作。

---

## 六、需求 2：SysML v2 版本管理与入库

### 6.1 数据模型（新增）

#### 6.1.1 `sysml_versions`——AI 生成 SysML 代码版本链

```sql
CREATE TABLE IF NOT EXISTS sysml_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id INTEGER DEFAULT 0,        -- 关联 artifacts(kind=sysml)，产物索引
    conversation_id INTEGER DEFAULT 0,    -- 来源会话
    message_id INTEGER DEFAULT 0,         -- 来源消息（修订版本可同消息）
    version_label TEXT NOT NULL,          -- v0.1 / v0.2 / v1.0（主.修订，见 6.2）
    content TEXT DEFAULT '',              -- SysML v2 源码快照（完整存首版，修订存 diff，见 6.2）
    diff TEXT DEFAULT '{}',               -- JSON：相对上一版变更 {added:[],removed:[],modified:[]}（文本级）
    element_summary TEXT DEFAULT '{}',    -- JSON：解析摘要 {entities:[{name,type}], relations:[...], attributes:[...]}
    parent_id INTEGER DEFAULT NULL,       -- 修订链（上一版本 id）
    status TEXT DEFAULT 'draft',          -- draft 草稿 | current 当前工作版本 | committed 已入库 | superseded 被替代
    adopted INTEGER DEFAULT 0,            -- 1=用户标记为最终采纳版本
    imported_batch TEXT DEFAULT '',       -- 入库后回填 sysml_candidates 批次/提交 id
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sv_conv ON sysml_versions(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_sv_art  ON sysml_versions(artifact_id);
```

**版本号语义**（对齐专利方案三重版本号简化版）：
- `v{主}.{修订}`：**主版本**在入库/结构重构后递增（如 v0 → v1）；**修订版本**在会话内每次生成/修订递增（v0.1 → v0.2 …）。采纳即锁定 `adopted=1`。

**版本链规则**：
- 每次 AI 生成（含修订重跑）→ 新建一行，`parent_id` 指向上版本，形成单链；
- 首版存完整 `content`，后续版本若 diff 后可读则存 diff + 全量重建缓存（简化实现：全量存 content，diff 仅用于变更展示）；
- `element_summary` 由解析器（`parse_text`/`parse_json`）即时产出，支撑"该版本包含哪些要素"。

#### 6.1.2 入库候选——**复用 `v2g_candidates`，不新建候选表**

AI 建模入库候选与文档抽取候选是同一类"待审图谱元素"，统一写入 `v2g_candidates`（[migrations.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/database/migrations.py#L518-L540) 既有表），仅迁移补一列关联版本：

| 列 | 文档抽取（DOC-） | AI 建模（SYSM-） |
|----|-----------------|-----------------|
| `batch_id` | `DOC-{uuid}` | `SYSM-{uuid}`（前端按前缀区分来源） |
| `chunk_id` | 来源 chunk id | 0 |
| `source_doc` | 文档名 | `AI建模·{会话名}·v0.2` |
| `entity_name` | 候选实体名 | SysML 实体名 |
| `entity_type` | 本体类型 | 同（`_resolve_entity_type` 映射） |
| `properties` | 候选属性 | **SysML 元素属性/值/单位并入 properties**（不拆独立 attribute 行，与 v2g 一致） |
| `rel_type/rel_source/rel_target` | 关系候选 | SysML 关系候选 |
| `matching_status/rel_matching_status` | 实体/关系消歧 | 同（统一消歧逻辑） |
| `confidence` | LLM 抽取置信度 | 解析确定性/映射置信度 |
| `status` | pending/confirmed/rejected/merged | 同（统一状态机） |

新增迁移（幂等 `_add`）：
```sql
-- v2g_candidates 补列：AI 建模入库时关联 SysML 版本（migrations._add 幂等）
_add("v2g_candidates", "sysml_version_id", "INTEGER DEFAULT 0")
```

> 属性不单独建候选：SysML 元素属性（如 `mass=18kg`）是实体 properties 的一部分，随实体候选统一确认/审核/入库；属性冲突（同名不同值）在消歧阶段写入候选 `properties.conflict` 供人工决策（见 6.3 Step 2）。

### 6.2 版本管理流程

1. **生成即建档**：AI 消息落库时（`_archive_artifacts` 增强）：
   - 有 `sysml_views` → 写入 `sysml_versions`（`version_label=v0.1` 或按当前会话版本计数递增；`element_summary` 由解析器产出）；
   - `artifacts.meta.version` 回填版本号。
2. **修订即新版本**：同一会话再次生成（含对旧消息的修订）→ 新行 `parent_id=上版`，`status=superseded`（旧版），新版本 `status=current`。
3. **采纳标记**：用户在版本详情中可「标记为采纳版本」（`adopted=1`）；一次会话仅一个采纳版本（后标记覆盖前标记，前版回到 current/superseded）。
4. **版本展示**：右栏产物面板点 SysML 产物 → 版本时间线（v0.1 → v0.2 → v1.0，点击切换源码/图谱预览 + diff 高亮）。

### 6.3 入库管线（核心：候选化 → 消歧 → 确认 → 审核 → 落库，统一 v2g 入口）

复用并增强 `sysml_importer.py` 的解析能力，替换 `import_sysml` 的**直接落库段**为候选化；**候选写入 `v2g_candidates`，后续确认/驳回/审核/入库全部走既有 v2g 链路，不另起流程**：

**Step 1 解析候选化**（新增 `sysml_to_candidates(conn, content, version_id)`）
- `parse_text/parse_json` 产出 nodes/edges → 按 **实体** 与 **关系** 两类候选，统一写入 `v2g_candidates`：
  - `batch_id = SYSM-{uuid}`（与文档抽取 `DOC-` 前缀区分）；
  - `source_doc = AI建模·{会话名}·{version_label}`、`sysml_version_id = version_id`、`source_type` 由入库阶段落库；
  - 实体候选：`entity_name`/`entity_type`（`_resolve_entity_type` 映射）/`properties`（SysML 元素属性/值/单位并入）；
  - 关系候选：`rel_type`/`rel_source`/`rel_target`；
  - `confidence`：解析确定性分数（映射到本体类型则高，兜底类型则低）。
- **属性不拆独立候选行**：并入实体候选 `properties`，与 v2g 抽取一致（属性审核随实体确认展示）。

**Step 2 消歧与合并检测**（**复用既有 v2g 消歧，写同一批字段**）
- **实体候选 vs 已入库实体**（`status!='deprecated'`）：blocking（同类型 + 名称归一）→ 双判据打分（名称相似度 + embedding 向量相似度，复用 `entity_dup_candidates`/`_disambiguate` 的判定函数），写 `matching_status`（none | dup_suspect | dup_high）与 `match_entity_id`：
  - `score ≥ 0.9` → `dup_high`，建议 **align**（复用已有实体，不新建）；
  - `0.6 ≤ score < 0.9` → `dup_suspect`，建议 **merge 或 create**（人工决策）；
  - `score < 0.6` → 新建。
- **关系候选 vs 已有三元组**（`source/target/relation_type` 一致）：写 `rel_matching_status`（none | dup_high）与 `rel_match_rel_id` → 建议 skip（不重复建边）。
- **属性冲突**：同实体属性名相同但值不同 → 写入候选 `properties.conflict`（记录新旧值，对齐 merge_requests 属性级冲突语义）。
- 候选写入时**带证据**（命中规则、相似度分数、被对齐实体 id/名称），审核人可直接判断（对齐行业原则 3）。

**Step 3 用户确认（关键确认点 #1：确认写入）**——**直接复用 `v2g_confirm`**
- 前端「入库向导」展示：实体 N / 关系 M 候选清单 + 每条候选的消歧建议徽章（⚠ 重复建议对齐 / 🔀 建议合并 / ✅ 新建）+ 属性冲突列表（数据来自 `v2g_candidates`，按 `batch_id=SYSM-xxx` 拉取）；
- 用户逐条或批量选择：`create`（新建）/ `align`（对齐复用）/ `skip`（跳过）/ `merge`（合并进已有，走 `merge_entities`）——即 `dup_action` 三态 + merge，对齐抽取治理中心既有交互；
- 调用既有 `POST /api/knowledge/v2g/confirm`（`dup_action` 传参），候选 `status=confirmed`，进入正式审核队列（entities status=candidate 或关系审核队列）。

**Step 4 正式审核（关键确认点 #2：审核通过）**
- 完全复用既有「实体/关系审核队列」：确认 → reviewed；驳回 → deprecated（软删留痕）；
- 属性随实体审核详情展示（`properties` 差异高亮），确认即合并属性。

**Step 5 落库与提交**（复用 `confirm_candidates` 落库段 + 补充来源字段）
- 审核通过 → 写入 `entities`/`relations`（`source_type='ai_generated'`、`source_doc=AI建模·会话·vX.Y`、`sysml_version_id` 透传、`graph_source='sysml_import'` 保留以便回查批次）；
- **一次入库 = 一次 `knowledge_commits`**：`kind=import`，`message` 含"AI 建模入库 vX.Y：实体 N/关系 M"；`changes` 记实体/关系清单（对齐既有提交语义）；
- `sysml_versions.imported_batch` 回填提交 id，版本状态 → `committed`。

**兼容与迁移**：
- 旧 `/api/knowledge/sysml/import`（O-3，直接落库）保留为**导入工具**入口（文件导入场景），但内部切换为同一管线（候选化 + 消歧 + 确认），`dry_run=true` 时只返回候选不落库；
- 已有 `sysml_imports` 批次数据不动（历史留痕），新流程与之并存；
- 「抽取治理中心」批次卡可识别 `SYSM-` 前缀展示「AI 建模」来源徽章，与文档抽取同列表/同队列处理。

### 6.4 消歧与合并——复用与增强

| 场景 | 复用机制 | 增强点 |
|------|---------|-------|
| 实体候选 vs 已入库实体 | `entity_dup_candidates` 判定函数 + `merge_entities` | 候选阶段即预检（消歧前移，与 v2g confirm 一致）；中间置信强制人工 |
| 关系候选 vs 已入库三元组 | P0-A 关系消歧（`rel_matching_status`/三态） | 入库向导内联展示 |
| 合并执行 | `merge_entities`（属性融合保留方优先 + 关系重指向 + `entity_merges` 审计可撤销） | 无新增，直接调用 |
| 属性冲突 | `merge_requests` 属性级冲突判定逻辑 | 提取为通用函数 `detect_property_conflicts(a,b)` 供候选阶段调用 |

### 6.5 与分支版本管理衔接

- 入库落库走 `get_current_branch()`（默认 dev），**不直接写 release**（延续发布门禁）；
- 审核通过 → 用户在 dev 上积累 → 创建合并请求 → 冲突分析（属性级）→ 审批 → 发布 release（`knowledge_publish_logs` 带 version_label + commit_id）；
- 发布清单可从合并提交的 `changes` 提取（既有能力），回答"这次发布包含 AI 建模的哪些要素"。

### 6.6 回滚与追溯

- **版本级回滚**：回到某历史版本 → 生成新版本（`parent_id` 指向目标版本）重跑入库，不物理删除；
- **提交级回滚**：复用 `POST /api/knowledge/commits/{id}/revert`（kind=rollback），撤销某次 AI 建模入库提交；
- **元素级追溯**：实体详情展示来源（`source_type=ai_generated` + 版本号 + 会话/消息链接 + `sysml_version_id`），点击跳转会话。

---

## 七、前端交互设计

### 7.1 消息流（pg-ai）

- 移除 L1/L2 按钮块（5.1）；
- SysML 产物卡片底部新增操作区（不弹确认，仅入口）：
  - 「📌 标记为采纳版本」——在版本时间线内操作；
  - 「💾 入库」——打开入库向导（关键确认点 #1）；
- 消息反馈徽章保留。

### 7.2 右栏产物面板（SysML 产物详情）

- 顶部：版本下拉（v0.1 / v0.2 …，`adopted` 版本带 ✅）＋「📜 版本历史」时间线；
- 中部：源码预览（只读 + 复制/下载）与图谱预览（复用 Cytoscape）切换；
- 底部：元素摘要（实体 N / 关系 M，属性并入实体）+「入库」按钮。

### 7.3 入库向导（居中卡片，复用 `#lbx` 交互规范）

> 数据源 = `v2g_candidates`（`batch_id=SYSM-xxx`），与「抽取治理中心」同源同表；向导即治理中心候选确认交互的会话内快捷入口，前端组件复用治理中心候选表格/消歧徽章能力。

```
┌─ AI 建模入库向导（SYSM-xxx · 来源 AI建模·v0.2）─────┐
│ SysML v0.2 · 共 实体 6 / 关系 4（属性并入实体）         │
│ ┌ 实体候选 ──────────────────────────────────┐ │
│ │ ☑ 载荷           [✅ 新建] [✓ 采纳版本]      │ │
│ │ ☑ 转发器         [⚠ 重复→建议对齐「转发器」] │ │
│ │ ☐ TWTA           [🔀 相似 0.72→建议合并]     │ │
│ └──────────────────────────────────────────┘ │
│ 属性冲突：载荷.mass 12kg → 18kg（与已入库冲突）  │
│ 批量动作：□ 新建 □ 对齐 □ 跳过  应用          │
│ [取消]  [✓ 确认入库 → 进入审核队列]             │
└──────────────────────────────────────────────┘
```

- 确认动作调用既有 `POST /api/knowledge/v2g/confirm`（`batch_id=SYSM-xxx`、`dup_action` 三态），候选 `status=confirmed` → 进入正式审核队列；
- 确认后 toast「候选已确认，进入实体/关系审核队列正式审核」（对齐 v2g 确认文案）；
- 审核队列页沿用现有「实体关系审核」tab，候选来源展示为「AI 建模（SysML v0.2）」；
- 「抽取治理中心」批次卡中 `SYSM-` 前缀批次带「AI 建模」来源徽章，点击可复用同一处理界面。

---

## 八、API 设计（新增/变更）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sysml-versions?conversation_id=&artifact_id=` | 版本链列表（label/status/adopted/摘要） |
| GET | `/api/sysml-versions/{id}` | 版本详情（源码/元素摘要/diff） |
| POST | `/api/sysml-versions/{id}/adopt` | 标记为采纳版本（幂等，同会话后标记覆盖） |
| POST | `/api/sysml-versions/{id}/import-candidates` | **入库 Step1/2**：解析 SysML → 候选写入 `v2g_candidates`（batch_id=SYSM-xxx）+ 消歧检测，返回候选清单（向导数据源） |
| POST | `/api/knowledge/v2g/confirm` | **入库 Step3 复用**：`batch_id=SYSM-xxx` + `dup_action` 三态，确认 → 进审核队列（零新增） |
| POST | `/api/knowledge/v2g/reject` | 复用：向导/治理中心驳回候选 |
| PUT | `/api/knowledge/v2g/candidates/{cid}` | 复用：单条候选编辑（改名称/类型/属性后重新消歧打标） |
| POST | `/api/knowledge/sysml/import` | 增强：`dry_run` 参数；非 dry_run 时内部走同一候选→确认→审核链路（兼容旧调用） |
| POST | `/api/messages/{id}/feedback` | 复用：入库确认/修订动作隐式调用（context 标注触发点） |

> 审核通过/驳回复用既有 `review_entity`、关系审核、`merge_entities` 接口，无新增；候选查询复用 `GET /api/knowledge/v2g/candidates?batch_id=SYSM-xxx`。

---

## 九、分阶段实施计划

- **P0（本次核心）**：交互改造（5.1~5.3）+ `sysml_versions` 建档/版本链 + 入库管线（`sysml_to_candidates` 写 v2g_candidates → 消歧 → 复用 v2g 确认向导 → 审核队列衔接 → 落库 + commit）。
- **P1**：入库向导高级交互（属性冲突可视化、批量决策、版本 diff 视图、抽取治理中心 `SYSM-` 来源徽章）。
- **P2**：SysML v2 语法校验门禁（对齐 SysGit：解析错误禁止提交/入库，复用既有 sysml-v2-validator 能力）；旧 O-3 文件导入统一切换新管线。

---

## 十、验证方案（对齐项目「前端闭环验证」惯例）

1. **后端断言**（pytest，对齐既有知识链路测试）：
   - 生成 SysML → `sysml_versions` 建档 v0.1；修订 → v0.2 且 v0.1 置 superseded；
   - 采纳标记幂等；`import-candidates` 后候选写入 **v2g_candidates**（batch_id 前缀 `SYSM-`、source_doc 含版本号、属性并入实体 properties、**不入库**）；
   - 消歧：构造重复候选（同类型同名）→ `matching_status=dup_high` 建议 align；构造相似不同名 → `dup_suspect` 建议 merge；关系三元组重复 → `rel_matching_status=dup_high` 建议 skip；
   - 复用 `v2g/confirm` 确认（dup_action 三态）→ 进审核队列（entities status=candidate）；审核通过 → 落库且 `source_type='ai_generated'` + `sysml_version_id` 正确；
   - 一次入库生成一条 `knowledge_commits`（kind=import，changes 清单正确）；回滚该提交可撤销；
   - 属性冲突写入候选 `properties.conflict` 并在向导返回。
2. **前端 playwright**：
   - 消息流无「采纳/拒绝/确认」按钮；含 SysML 消息仅显示「入库」入口；
   - 入库向导：候选列表（来自 v2g_candidates）+ 消歧徽章 + 批量决策 → 复用 v2g confirm → toast 进入审核队列 → 审核页出现 AI 建模来源候选；
   - 「抽取治理中心」批次卡识别 `SYSM-` 批次并显示「AI 建模」来源徽章；
   - 右栏产物：版本时间线切换、采纳标记徽章；反馈台账仍累计（确认=采纳计数 +1）。
3. **回归**：既有 v2g 抽取/审核/分支版本管理用例不回归；O-3 SysML 文件导入 dry_run 行为正确。

---

## 十一、参考资料

- Dassault Systèmes：Automated SysML v2 Export From Teamwork Cloud to GitLab Using GitLab CI/CD Pipeline（2026-02）— [链接](https://3dswym.3dexperience.3ds.com/en/post/catia-mbse-cyber-systems/automated-sysml-v2-export-from-teamwork-cloud-to-gitlab-using-gitlab-ci-cd-pipeline_HFbL1o63QCOznBfccrkCVQ)
- SysGit v1.3.10 Release Notes：SysML v2 MCP Server, Rest API, and more（2026-07）— [链接](https://resources.sysgit.io/sysgit-release-notes-sysml-v2-mcp-server-rest-api-and-more/)
- 国知局专利：一种基于图论与知识图谱的 MBSE 模型版本谱系化迭代方法 — [链接](https://www.xjishu.com/zhuanli/55/202511658166.html)
- R. Stephan et al.：Semantic fusion of SysML elements for model integration utilizing knowledge graphs（DESIGN 2026，Cambridge）— [链接](https://www.cambridge.org/core/journals/proceedings-of-the-design-society/article/semantic-fusion-of-sysml-elements-for-model-integration-utilizing-knowledge-graphs/C522C9F1FF496EE2A33CC245E2363183)
- sift-kg：Document-to-Knowledge-Graph pipeline（LLM propose → human review → apply）— [链接](https://pypi.org/project/sift-kg/0.3.1/)
- Neo4j Agent Memory：Entity Resolution and Deduplication（SAME_AS 三档决策 + 人审）— [链接](https://neo4j.com/labs/agent-memory/explanation/resolution-deduplication/)
- 掘金：LLM 抽出来的实体，为什么不能自动合并——一套"提议 + 人审"的消歧工程方案（2026-06）— [链接](https://juejin.cn/post/7652778266403274786)
- TheAIops：What is Entity Resolution?（blocking/打分/聚类/审计/人审闭环）— [链接](https://www.theaiops.com/entity-resolution/)
- Visual Paradigm：Collaborative Modeling and Version Control（提交前完整性校验、tag 发布）— [链接](https://skills.visual-paradigm.com/cn/docs/sysml-essentials-for-beginners/sysml-best-practices/sysml-collaboration-teamwork-version-control/)
