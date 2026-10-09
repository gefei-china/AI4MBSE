# AI 建模节点体系设计规范 — Agent / Skill / Tool

> 版本 v1.0 ｜ 基线 2026-10-08 ｜ 仓库 `mbse_system`
> 所有现状数字均为**本轮实测**，非引述历史文档。测量命令见附录 B。

---

## 0. 结论先行

**核心主张：8 个视图 Agent 是按「视图类型」横切，但建模本质是按「阶段」纵切。视图应该是流水线的参数，不是节点本身。**

现状实测的三个结构性缺口：

| 缺口 | 实测证据 | 后果 |
|---|---|---|
| **节点无契约** | `agents` 表 19 个 active Agent 的 `input_schema` / `output_schema` **全部为 `{}`** | 节点间传自由文本，同一 query 两次拆解结果不同 |
| **节点无出口** | 8 个视图 Agent（id 165-173）**各绑 1 个工具**（`sysml_v2_validate`），无 `entity_create`、无写文件 | SysML 代码只停在正文 ```sysml 块，靠 `cards.py` 兜底补救 |
| **约束声明形同虚设** | 13 个 skill 的 `allowed_roles` / `dependencies` **全部为 `[]`**；`SysML v2 校验与修复`（id=99）`allowed_tools=[]` 且未绑给任何 Agent | 声明了约束但从未生效 |

**已具备、不该重做的能力**（复用而非重建）：

| 能力 | 位置 | 状态 |
|---|---|---|
| SysML v2 官方校验器封装 | `sysml_v2_check.py`（29KB） | ✅ 词法/语法/语义三路诊断，单次 4~6s |
| L0 生成端硬约束卡 | `v2_constraints.py:50` | ✅ 实测 A/B：无卡 6/9 ERROR → 有卡 **0/0** |
| 五档路由 | `agent/team_router.py:284` | ✅ 已上线 |
| 三级技能披露 | `agent/pipeline_parts/skills.py:112` | ✅ 已上线 |
| 工具权限链| RBAC→Hook→HIL→destructive | ✅ 全工程最成熟的一块 |
| 8 视图投影器 | `view_generator.py`（10 种视图） | ✅ 代码全通（但 3 视图 AST 缺元类） |

---

## 1. 节点体系：六阶段纵切

### 1.1 为什么必须纵切

现状把「生成需求视图」和「生成活动图」做成两个平级 Agent。问题：

- **路由不可分**：`team_router` 词法 2-gram 对 8 个视图成员区分不开（实测「满足需求→解析→结构视图」被误判为 `requirement_analysis`）
- **8 份重复 prompt**：8 个 `system_prompt` 2.5KB~8KB，公共约束（L0卡、import 纪律）重复 8 遍
- **无法表达依赖**：视图间有严格顺序（需求→结构→用例→活动→IBD→时序→状态机→参数），平级结构表达不了
- **无法收敛**：某视图失败只能整体重跑

纵切后：8 视图降为 **N3 的并行参数**（`view_type`），公共约束只写一次，依赖顺序由流水线拓扑保证。

### 1.2 六节点定义

| 节点 | 职责 | 产出物（结构化） | 门禁判据 | HIL |
|---|---|---|---|---|
| **N1** `requirement_structuring` | 自然语言→需求条目 | `RequirementSet` | 条目化率 ≥ 阈值；无主语/无动词条目 → 退回澄清 | L1 |
| **N2** `architecture_skeleton` | 需求→SysML 骨架 | `SysMLSkeleton`（包/部件/端口/需求声明） | `validate` verdict ≠ block | L0 |
| **N3** `view_expansion` | 骨架→8 视图（并行） | `ViewBundle` |逐视图 validate，独立失败独立重试 | L0 |
| **N4** `model_validation_repair` | 诊断→修复→复检 | `ValidatedModel` + 诊断闭环 | `n_hard` 单调下降；≤3 轮；不伪造通过 | L0 |
| **N5** `trace_verification` | 需求↔元素双向核验 | `TraceMatrix` + 缺项清单 | 数字必须来自工具，禁心算 | L0 |
| **N6** `model_release` | 落库+版本链 | `entities`/`relations` + `sysml_versions` | HIL L2 逐条确认 | **L2** |

**关键设计**：
1. **N6 是模型图谱实体（`entities`/`relations`）的唯一落库出口**。N1-N5 全程只产出内存/文件产物，不碰图谱库。当前 8 个 Agent 各自能落库，是数据不一致的根源。
   > ⚠️ **v3.0 修正（2026-10-08）**：此处原写「唯一落库出口」，实测发现表述过窄 —— 全库 write 工具 7 个中只有 `entity_create`/`zhiyuan_sysmlv2_import` 属建模域，其余 `file_write`/`file_append`/`file_mkdir`/`report_export`/`mbse_pull_ingest` 是**跨域写**。N6 约束的是**模型图谱实体**的落库，不是所有写操作。详见 `AI建模节点体系设计规范-v3-多任务域兼容-20261008.md` §0.1。
2. **N4 单独成节点**，不塞进 N3。当前校验只在 `cards.py:274` 留痕，不阻断交付。
3. **N1 是当前最大空白**。需求→模型全程无中间表示，LLM 每次从零猜测。

---

## 2. Agent 定义（6 个 + 1 主）

### 2.1 命名规范

```
格式：<阶段>_<职责>，全小写下划线
路由键（agents.name）与展示名（display_name）分离
  name         = 意图标识，代码/路由唯一依据，snake_case 英文
  display_name = 中文展示，主 Agent prompt 名册用
```

**铁律**：`display_name` 会被写进主 Agent 的 system_prompt 名册（`tools/seed_team_definitions.py:85`），改名必须同步 seed 脚本，否则编排会出现幽灵成员。

### 2.2 六个节点 Agent

#### N1 需求结构化

| 字段 | 值 |
|---|---|
| `name` | `requirement_structuring` |
| `display_name` | 需求结构化Agent |
| `agent_role` | `sub` |
| `hil_level` | `L1` |
| `description` | 把自然语言需求转成结构化需求条目集：条目化、分类（功能/性能/接口/约束）、标注来源追溯，识别歧义与缺项。不产出 SysML 代码。 |
| `capabilities` | `自然语言需求解析`,`需求条目化与规格化`,`需求分类与追溯标注`,`歧义与缺项识别` |
| `intent_keywords` | `需求条目化`,`需求结构化`,`需求拆解`,`需求规格化`,`条目化需求`,`需求分类` |
| `input_schema` | `{"type":"object","required":["raw_text"],"properties":{"raw_text":{"type":"string"},"domain":{"type":"string"},"source_ref":{"type":"string"}}}` |
| `output_schema` | `{"type":"object","required":["items"],"properties":{"items":{"type":"array","items":{"type":"object","required":["id","text","category"],"properties":{"id":{"type":"string"},"text":{"type":"string"},"category":{"enum":["functional","performance","interface","constraint"]},"subject":{"type":"string"},"source_ref":{"type":"string"},"ambiguous":{"type":"boolean"}}}}},"gaps":{"type":"array"}}}` |
| 绑工具 | `requirement_quality` 通道、`graph_retrieve`、`coverage_matrix` |

**system_prompt 要点**（不写全文，写不可协商的部分）：
```
每条需求必须可独立验证：主语 + 动作 + 可测量判据。
禁止输出"系统应具有良好的性能"这类不可测条目 —— 归入 gaps 并标注 need_clarification。
category 取值仅限 functional/performance/interface/constraint。
条目 id 采用 REQ-{类别首字母}{序号}，供下游 satisfy 关系引用。
你不产出 SysML 代码；下游 N2 消费你的 items。
```

#### N2 架构骨架生成

| 字段 | 值 |
|---|---|
| `name` | `architecture_skeleton` |
| `display_name` | 架构骨架生成Agent |
| `hil_level` | `L0` |
| `description` | 消费结构化需求条目，生成 SysML v2 架构骨架：包结构、部件定义、端口定义、需求声明与 satisfy 分配。只出骨架，不出视图细节。 |
| `capabilities` | `SysML v2 骨架生成`,`包与部件定义`,`端口与接口定义`,`需求声明与满足分配`,`类型族匹配` |
| `intent_keywords` | `架构骨架`,`骨架生成`,`系统架构定义`,`部件定义`,`包结构生成` |
| `input_schema` | `{"type":"object","required":["requirements"],"properties":{"requirements":{"type":"array"},"package_name":{"type":"string"},"domain":{"type":"string"}}}` |
| `output_schema` | `{"type":"object","required":["skeleton_code","package_name"],"properties":{"skeleton_code":{"type":"string"},"package_name":{"type":"string"},"declared_requirements":{"type":"array"},"skeleton_hash":{"type":"string"}}}` |
| 绑工具 | `sysml_v2_validate`（**必须**）、`graph_retrieve`、`sysml_stdlib_meta`（新增） |

**必须绑 `sysml_v2_validate`** —— `_is_v2_code_agent`（`prompt.py:104`）靠 `agent_tools` 里有没有它来决定是否注入 L0 卡，不绑就拿不到硬约束。

**system_prompt 要点**：
```
骨架只含：package 声明、part def、port def、requirement def、satisfy 分配。
禁止出现 action/state/flow（那是 N3 的活）。
生成后必须调用 sysml_v2_validate，verdict=block 时按诊断位置修复后重试，最多 3 轮。
part/port/item 分别由 part def/port def/item def 定型，不可混用。
```

#### N3 视图展开

| 字段 | 值 |
|---|---|
| `name` | `view_expansion` |
| `display_name` | 视图展开Agent |
| `hil_level` | `L0` |
| `description` | 消费已通过校验的架构骨架，按 view_type 参数展开单个 SysML v2 视图（需求/结构/用例/活动/IBD/时序/状态机/参数），每个视图独立校验、独立重试。 |
| `capabilities` | `SysML v2 视图展开`,`八视图固定顺序依赖`,`活动决策与异常分支`,`状态转换与守护条件`,`端口连接器建模` |
| `intent_keywords` | `视图展开`,`生成活动图`,`生成用例视图`,`生成状态机`,`生成时序图`,`生成IBD`,`生成参数视图`,`视图建模` |
| `input_schema` | `{"type":"object","required":["skeleton_code","view_type"],"properties":{"skeleton_code":{"type":"string"},"view_type":{"enum":["requirement","structure","usecase","activity","ibd","sequence","state","parameter"]},"upstream_views":{"type":"object"}}}}` |
| `output_schema` | `{"type":"object","required":["view_type","view_code"],"properties":{"view_type":{"type":"string"},"view_code":{"type":"string"},"check":{"type":"object"},"depends_on":{"type":"array"},"degraded":{"type":"boolean"}}}}` |
| 绑工具 | `sysml_v2_validate`、`sysml_ast_extract`（新增）、`graph_retrieve` |

**`view_type` 是枚举，不是自由文本** —— 这决定了 8 视图能否被 JIT 预筛和独立重试。

**降级纪律**：某视图上游缺失时，`degraded=true` + 显式声明缺失风险，**禁止编造上游元素**。

#### N4 校验与修复

| 字段 | 值 |
|---|---|
| `name` | `model_validation_repair` |
| `display_name` | 模型校验修复Agent |
| `hil_level` | `L0` |
| `description` | 消费视图产出与校验诊断，按「词法→语法→语义」顺序定位修复并复检，最多 3 轮。n_hard 必须单调下降。绝不伪造通过。 |
| `capabilities` | `SysML v2 三层诊断定位`,`按优先级修复`,`修复轮次预算控制`,`修复有效性判定` |
| `intent_keywords` | `校验模型`,`模型检查`,`修复语法错误`,`校验不过`,`模型报错`,`诊断修复` |
| `input_schema` | `{"type":"object","required":["code","diagnostics"],"properties":{"code":{"type":"string"},"diagnostics":{"type":"array"},"round":{"type":"integer"},"prev_n_hard":{"type":"integer"}}}` |
| `output_schema` | `{"type":"object","required":["fixed_code","rounds_used"],"properties":{"fixed_code":{"type":"string"},"rounds_used":{"type":"integer"},"n_hard":{"type":"integer"},"verdict":{"enum":["pass","report","block","unavailable"]},"residual_errors":{"type":"array"},"fabricated":{"type":"boolean"}}}` |
| 绑工具 | `sysml_v2_validate`、`sysml_v2_autofix`（新增） |

**必须绑 `sysml_v2_autofix`** —— 确定性规则修复（`import X::*` → `private import X::*` 等高频错误）。LLM 记忆靠不住，规则才可靠。

#### N5 追溯核验

| 字段 | 值 |
|---|---|
| `name` | `trace_verification` |
| `display_name` | 追溯核验Agent |
| `hil_level` | `L0` |
| `description` | 生成需求↔模型元素双向追溯矩阵，识别未覆盖需求、无源元素、断裂 satisfy 链。所有数字来自确定性工具。 |
| `capabilities` | `需求到元素追溯矩阵`,`反向孤儿元素识别`,`satisfy 链完整性检查`,`覆盖缺项汇总` |
| `intent_keywords` | `追溯矩阵`,`覆盖性分析`,`需求覆盖检查`,`追溯链检查`,`缺项分析` |
| `input_schema` | `{"type":"object","required":["requirement_ids"],"properties":{"requirement_ids":{"type":"array"},"model_refs":{"type":"array"},"branches":{"type":"array"}}}` |
| `output_schema` | `{"type":"object","required":["matrix","uncovered","orphans"],"properties":{"matrix":{"type":"array"},"uncovered":{"type":"array"},"orphans":{"type":"array"},"broken_chains":{"type":"array"},"metrics_source":{"type":"string"}}}` |
| 绑工具 | `coverage_matrix`、`trace_chain_check`、`modeling_coverage`、`gap_summary`（**全部 4个**） |

#### N6 发布落库

| 字段 | 值 |
|---|---|
| `name` | `model_release` |
| `display_name` | 模型发布Agent |
| `agent_role` | `sub` |
| `hil_level` | **`L2`** |
| `description` | 消费通过校验的模型产物，落库为 entities/relations，写 sysml_versions 版本链与 check 留痕，推送智源平台。所有写操作逐条人工确认。 |
| `capabilities` | `模型落库与版本链`,`实体关系图谱回灌`,`智源平台推送`,`check 留痕审计` |
| `intent_keywords` | `发布模型`,`落库`,`导入智源`,`模型入库`,`生成版本` |
| `input_schema` | `{"type":"object","required":["view_bundle","check_verdict"],"properties":{"view_bundle":{"type":"object"},"check_verdict":{"enum":["pass","report"]},"project_id":{"type":"string"},"branch_id":{"type":"string"}}}` |
| `output_schema` | `{"type":"object","required":["released","version_id"],"properties":{"released":{"type":"boolean"},"version_id":{"type":"string"},"entities_created":{"type":"integer"},"relations_created":{"type":"integer"},"pushed":{"type":"boolean"},"confirmed_by":{"type":"string"}}}}` |
| 绑工具 | `entity_create`、`zhiyuan_sysmlv2_import`、`mbse_pull_ingest`、`graph_db_query` |

**HIL L2 强制** —— `agents.hil_level='L2'` + 绑 `write` 类工具 → `hil_service.py` 弹确认队列。

### 2.3 主 Agent 改造

保留 `MBSE建模总体负责人`（id=160），但重写 `system_prompt` 的名册为 6 节点，并**把八视图顺序从 prompt 规则改为流水线拓扑**：

```
## 你的团队（6 名阶段节点，禁止虚构）
| 需求结构化Agent | 需求条目化与结构化 |
| 架构骨架生成Agent | SysML v2 骨架（包/部件/端口/需求声明） |
| 视图展开Agent | 八视图展开（view_type 参数驱动） |
| 模型校验修复Agent | 三层诊断定位与修复 |
| 追溯核验Agent | 需求↔元素追溯矩阵 |
| 模型发布Agent | 落库与版本链（需人工确认） |

## 拆解规则
1. 阶段顺序固定：N1→N2→N3→N4→N5→N6，不得跳步、不得倒序。
2. N3 内八视图按需求→结构→用例→活动→IBD→时序→状态机→参数 并行展开。
3. 生成≠校验≠落库，三个阶段严格分离，不得合并成一个任务。
4. N6 必须置于末尾，且 notes 中标注「需人工确认」。
5. 缺能力时在 notes 写明「团队暂不支持」，不得硬派。
```

**同时清理**：现有 8 个视图 Agent（id 165-173）中的**需求/结构/用例/活动/IBD/时序/状态机/参数**8 个降级为 `status='disabled'`（保留 9 个多方案生成），迁移到 N3 的 `view_type` 参数。理由：路由不可分、prompt 重复、依赖无法表达。

---

## 3. Skill 定义

### 3.1 命名与定位

**边界铁律**（沿用工程既有判据）：
- **Tool** = 有副作用、可被函数调用、有 `input_schema`/`side_effect`/`risk_level`
- **Skill** = 纯提示词、无副作用、通过 `allowed_tools` 声明可调哪些 tool

### 3.2 必备约束字段（当前全空，必须填）

| 字段 | 强制 | 用途 |
|---|---|---|
| `allowed_tools` | **必填** | 白名单，空=该skill 不得调任何工具 |
| `allowed_roles` | 有角色限制时必填 | 空=全员可见 |
| `dependencies` | 依赖其他 skill 时必填 | 发布前校验存在性+版本 |
| `references` | 建议填 | 渐进披露（只列文件名，不注入正文） |

### 3.3 六个建模 Skill

#### S1 `sysml_requirement_to_model_method`

| 字段 | 值 |
|---|---|
| `name` | `sysml_requirement_to_model_method` |
| `description` | 需求条目到SysML 元素的映射方法论：功能需求→activity行为、性能需求→constraint/attribute、接口需求→port/connector、约束需求→requirement def。含satisfy 分配与追溯标注纪律。 |
| `triggers` | `需求映射`,`需求到模型`,`如何建模`,`需求转元素`,`映射方法`,`建模方法论` |
| `category` | `AI建模` |
| `allowed_tools` | `["graph_retrieve", "coverage_matrix"]` |
| `allowed_roles` | `["建模工程师", "系统工程师", "架构师"]` |
| `dependencies` | `[]` |
| `references` | `[{"title":"需求类型映射表","path":"references/需求类型映射表.md"}]` |

**正文铁律**：
```
映射前必须先检索本体（graph_retrieve），确认目标元类存在。
禁止建立本体中不存在的元素类型；缺失时在 gaps 中标注。
每条 requirement def 至少被一个 part 的 satisfy 覆盖，否则视为未实现。
```

#### S2 `sysml_skeleton_generation_guide`

| 字段 | 值 |
|---|---|
| `name` | `sysml_skeleton_generation_guide` |
| `description` | SysML v2 架构骨架生成指南：包与导入纪律、part/port/item 类型族匹配、需求声明与 satisfy 分配、部件层级划分原则。 |
| `triggers` | `骨架生成`,`架构定义`,`包结构`,`部件定义`,`端口定义` |
| `category` | `AI建模` |
| `allowed_tools` | `["sysml_v2_validate", "sysml_stdlib_meta"]` |
| `allowed_roles` | `["建模工程师", "系统工程师", "架构师"]` |
| `references` | `[{"title":"骨架模板集","path":"references/骨架模板集.md"},{"title":"标准库可导入包清单","path":"references/stdlib_packages.md"}]` |

**注意**：`allowed_tools` 里加了 `sysml_stdlib_meta`（新增工具），解决「标准库内容一条都没进 prompt」的问题。

#### S3 `sysml_view_generation_<view_type>` × 8

**一套模板，8 个实例。** 命名 `sysml_view_generation_activity` 等。

| 字段 | 值（以 activity 为例） |
|---|---|
| `name` | `sysml_view_generation_activity` |
| `description` | 活动视图生成指南：动作顺序与并发分区、决策分支、**异常分支（Exception Branch 必含）**、数据流传递、约束条件。 |
| `triggers` | `活动图`,`活动视图`,`action`,`并发`,`异常分支` |
| `category` | `AI建模` |
| `allowed_tools` | `["sysml_v2_validate", "graph_retrieve"]` |
| `allowed_roles` | `["建模工程师", "系统工程师"]` |
| `references` | `[{"title":"活动视图模板","path":"references/activity_template.md"}]` |

**8 个实例清单**：

| 实例 name | 视图 | 必须覆盖的建模要点 |
|---|---|---|
| `sysml_view_generation_requirement` | 需求 | 需求层次、利益相关方、subject 位置、需求间 refine |
| `sysml_view_generation_structure` | 结构 | 层级组成、part def、继承与 specialize |
| `sysml_view_generation_usecase` | 用例 | 参与者、use case 分解、include/extend |
| `sysml_view_generation_activity` | 活动 | 并发、决策、**异常分支**、数据流 |
| `sysml_view_generation_ibd` | IBD | 端口、连接器、跨层级连接 |
| `sysml_view_generation_sequence` | 时序 | 消息交换、时间顺序、**AST 需先补 MessageUsage** |
| `sysml_view_generation_state` | 状态 | 状态、转换、触发/守护/执行、运行模式 |
| `sysml_view_generation_parameter` | 参数 | 约束、参数化关系、属性绑定、**AST 需先补 ParameterUsage** |

⚠️ 3 个视图（时序/状态/参数）**当前 AST 元类缺失**，skill 上线前必须先补 `sysml_ast.py:43 NODE_KINDS`，否则投影产不出边。

#### S4 `sysml_validation_repair_loop`

| 字段 | 值 |
|---|---|
| `name` | `sysml_validation_repair_loop` |
| `description` | SysML v2 校验修复闭环：生成后调 sysml_v2_validate 取三路诊断，按「词法→语法→语义」顺序修复并复检，最多 3 轮；n_hard 必须单调下降；不伪造通过。 |
| `triggers` | `校验`,`检查模型`,`模型检查`,`校验模型`,`语法错误`,`语义错`,`编译错误`,`修复` |
| `category` | `AI建模` |
| `allowed_tools` | `["sysml_v2_validate", "sysml_v2_autofix"]` |
| `allowed_roles` | `["建模工程师", "系统工程师", "评审工程师"]` |
| `references` | `[{"title":"高频错误对照表","path":"references/error_pairs.md"}]` |

**这是现有 id=99 的正式替代**。原 skill 有三个缺陷：① `allowed_tools=[]` ② 未绑给任何 Agent（靠全局池 triggers 碰运气）③ `references=[]`。新 skill 三个字段全填，且**显式绑给 N3/N4 两个 Agent**。

**正文铁律**：
```
第 1 步：先跑 sysml_v2_autofix（确定性规则修复，零 LLM 成本）
第 2 步：残余错误按词法→语法→语义顺序处理（硬错误优先）
第 3 步：每轮记录 n_hard，若本轮 n_hard ≥ 上轮则放弃本轮修复并报错
第 4 步：3 轮后仍不过 → 明确报告"未通过 + 残余错误清单"

禁止：不调用 validate 就声称通过
禁止：把 verdict=unavailable 说成通过
禁止：引入未在 sysml.library 中验证的 import
```

#### S5 `sysml_trace_coverage_analysis`

| 字段 | 值 |
|---|---|
| `name` | `sysml_trace_coverage_analysis` |
| `description` | 需求↔模型双向覆盖核验方法：正向查未覆盖需求，反向查孤儿元素，断裂 satisfy 链检测。所有数字来自确定性工具，禁止心算。 |
| `triggers` | `覆盖性`,`追溯`,`覆盖分析`,`缺项`,`追溯矩阵` |
| `category` | `AI建模` |
| `allowed_tools` | `["coverage_matrix", "trace_chain_check", "scene_coverage", "gap_summary", "modeling_coverage"]` |
| `allowed_roles` | `["验证工程师", "系统工程师", "质量工程师"]` |
| `references` | `[{"title":"调用序与解读模板","path":"references/coverage_reading.md"}]` |

**正文铁律（沿用现有 id=100 的正确做法）**：
```
调用序（固定，不可颠倒）：coverage_matrix → trace_chain_check → scene_coverage → gap_summary
gap_summary 必须最后调（它消费前三者结果，单独跑会遗漏）。

数字一律来自确定性工具，你负责编排、解读、建议。
你心算或估算的任何百分比都是伪造。
报告须标注规则版本（随工具结果返回的 cov-v1.0）。
```

#### S6 `sysml_release_checklist`

| 字段 | 值 |
|---|---|
| `name` | `sysml_release_checklist` |
| `description` | 模型发布前检查清单：校验 verdict 判定、追溯覆盖率阈值、归一决策确认、版本留痕、智源推送前置条件。 |
| `triggers` | `发布模型`,`入库检查`,`发布检查`,`上线模型` |
| `category` | `AI建模` |
| `allowed_tools` | `["entity_create", "graph_db_query"]` |
| `allowed_roles` | `["建模工程师", "架构师"]` |
| `dependencies` | `["sysml_validation_repair_loop", "sysml_trace_coverage_analysis"]` |

**正文铁律**：
```
verdict=block → 禁止发布
verdict=report → 允许发布但须在报告首屏列出全部语义错
verdict=unavailable → 允许发布但标注"未校验"，禁止表述为"已通过"

覆盖矩阵存在 uncovered 项 → 列清单交人工决策，不自动判失败
归一决策（中文名→英文标识符）必须逐条人工确认后才应用
```

---

## 4. Tool 定义

### 4.1 新增工具（当前 33 个全查过，这6 个都不存在）

| 工具名 | description（直接进 LLM 载荷，需高信息密度） | side_effect | risk | input_schema 关键字段 |
|---|---|---|---|---|
| `sysml_v2_autofix` | SysML v2 确定性规则修复：按实测高频错误表（import 可见性前缀、`:>`/`:>>` 误用、doc 语法、`&&`/`||`、extend 不存在等）逐条定位并改写，返回修复项与未修复项。不做语义判断，不猜测。 | write | low | `{"code":string,"rule_set":string,"max_fixes":integer}` |
| `sysml_v2_project_check` | 工程级 SysML v2 门禁：对整包多文件合并校验（避免单文件伪错），返回 verdict 与按文件聚合的诊断。发布前必调。 | read | low | `{"package_path":string,"files":string[]}` |
| `sysml_stdlib_meta` | 查询 sysml.library 标准库元类：给定元类名返回必填/可选特征、可导入包清单、标准示例用法。生成前查标准库，禁止臆造属性。 | read | low | `{"metaclass":string,"package":string}` |
| `sysml_ast_extract` | 解析 SysML v2 AST，抽取声明元素与关系边（part/port/requirement/connection/transition/message/constraint），返回结构化清单与位置。用于视图投影与覆盖核验。 | read | low | `{"code":string,"kinds":string[]}` |
| `requirement_itemize` | 需求条目化：把自然语言需求按句切分并结构化（主语/动作/判据/分类），标出不可测与歧义项。确定性规则 + 可选 LLM 增强。 | read | low | `{"raw_text":string,"domain":string}` |
| `sysml_import_graph` | 把校验通过的 SysML 产物导入图谱为 entities/relations，按本体类型对齐，回写映射报告。 | write | medium | `{"code":string,"branch":string,"domain":string}` |

**`description` 写法规范**（工程既有唯一方法论，见 `register_sysml_check_tools.py:34`）：
> 「描述会随工具定义每次注入 LLM 的 tools 载荷，是最高优先级的常驻提示 →「生成后必须调用」这句话写在这里，比写在任何提示词里都可靠」

所以 `sysml_v2_autofix` 的描述里必须写死「**修复前先调本工具，再考虑 LLM 手改**」。

### 4.2 必须修复的工具侧断链

| 问题 | 位置 | 修法 |
|---|---|---|
| **`input_schema` 注入时被整体丢弃** | `agent/pipeline_parts/tools.py:242-262` 统一输出空 `properties` | 改为透传 DB `input_schema`。**这是 P0** —— LLM 现在只能靠 description 猜参数名 |
| `sysml_v2_validate` 未绑给 N3/N4 | `agent_tools` | 随新 skill 绑定 |
| 描述长度无校验（实测 11~242 字符离散） | 无 | 加 `sanitize_description` 上下限（`registry.py:56` 已有 ≤200 字，转用于 tools） |

---

## 5. 匹配规则

### 5.1 三层路由（沿用现有五档，只补三处）

| 层 | 规则 | 阈值/来源 |
|---|---|---|
| L1 会话入口 | 关键词强特异优先 → 建模强信号正则 → 语义 → LLM | 沿用 `intent.py:413` 八层 |
| L2 团队模式 | 五档：⓪多交付物 / ①噪声 / ②词法 / ③LLM裁决 / ④回落 | `LEX_STRONG=2.0`、`LEX_MARGIN=0.6`、`NOISE_LEN=6` |
| L3 技能匹配 | triggers 关键词 → bigram 语义（top1<top2×1.5 弃权）→ LLM 兜底 | 语义阈值 `0.15` |

**新增第4 层：N3 内部视图路由**

```
输入：skeleton_hash + view_type 集合
规则：按八视图固定顺序拓扑排序；上游 degraded → 下游必须标degraded
判据：view_type 是枚举 → 不走语义匹配，走确定性映射
```

### 5.2 路由能力补强（关键）

**问题**：6 个新 Agent 的 `intent_keywords` 会比现有 8 个视图 Agent 更难区分（都是"生成"类）。

**对策**（三条，按成本排序）：

1. **capabilities 差异化** —— planner 靠 `capabilities` 判断谁能吃下任务（`registry.discover:366` 按 capabilities 交集筛选）。S1-N6 的 capabilities 用**不同动词**：`解析/条目化` vs `骨架/定义` vs `展开/视图` vs `诊断/修复` vs `矩阵/缺项` vs `落库/版本`。

2. **阶段状态机兜底** —— 建模是流程性任务，路由不应只靠关键词。**在 pipeline 层加阶段锁**：`session_artifacts` 已有会话状态（`agent/session_artifacts.py`），记录当前阶段后，同阶段的请求直接命中对应 Agent，跳过意图识别。证据：`docs/主Agent提示词反推草案-20261007.md:106` 指出当前 `intent_keywords` 为空是已知问题。

3. **词法权重上调** —— `lexical_score`（`team_router.py:73`）里 `intent_keywords` 命中权重 `1.0+0.5×min(len,8)`。**建模域关键词普遍较长**（`需求条目化`=5字 → 1.0+0.5×5=3.5，高于 `LEX_STRONG=2.0`），可直接越过 LLM 裁决档。**须实测标定后固化**，不可凭印象填阈值。

---

## 6. 应用约束

### 6.1 节点级约束（需新增字段）

现有 `agents` 表缺三个字段，需迁移添加：

| 新字段 | 类型 | 用途 | 当前对应 |
|---|---|---|---|
| `stage_order` | INTEGER | 流水线位置，路由状态机用 | 无（`max_concurrency` 是并发数，不是顺序） |
| `stage_gate` | JSON | 该节点的产出判据 | `output_schema` 只描述结构，不含判据 |
| `mutually_exclusive` | JSON | 互斥节点组 | 无 |

**注意**：`max_concurrency`（实测全为 2）当前**声明了但执行层不读** —— 编排并发由全局 `_ORCH_MAX_WORKERS=3` 控制。不要拿它当顺序用。

### 6.2 运行时约束（现有机制，需接线）

| 约束 | 机制 | 现状 |
|---|---|---|
| 写工具门控 | 写意图闸（`tools.py:157`）→ RBAC → Hook → HIL L2 → destructive | ✅ 已上线 |
| 工具白名单 | `skills.allowed_tools` → `tools.py:292` 委派白名单 | ⚠️ 机制有，但建模 skill 全空 |
| 角色可见性 | `skills.allowed_roles` → `skills.py:129` | ⚠️ 机制有，但全空 |
| 熔断 | **全仓零命中** | ❌ 缺失（`docs/Agent生产化Harness对照核查-20261006.md` §4 已列） |

**建议 P0 补熔断**：建模链路的失败模式是「反复生成同样错的模型」，正是熔断典型场景。判据：同一 session 内同一节点连续失败 ≥3 次 → 中断并要求人工介入。

### 6.3 流程沉淀约束

现状：`agent_flows` 57 条**全部 draft**（4 manual + 53 planner_auto），`_try_reuse_planner_flow`（`orchestration.py:520`）只复用 published ⇒ **恒返回 None，沉淀-复用闭环不通**。

**修**：把 N1→N6 的六节点流水线**作为一条 manual flow 预置并 published**，让复用机制真正跑起来。同时 `planner_auto` 沉淀的 flow 必须过 `stage_gate` 校验才能 published，否则会把劣质拆解固化。

**另一处实测缺陷**：`planner_auto` flow 的子任务只传 `{{payload.input}}`（run 193 样例），t2/t3 拿不到 t1 结果。节点契约落地后，`context` 字段必须由编排层从上游 `output` 自动填充，**不能依赖 LLM 自觉填 `context`**。

---

## 7. 落地顺序（按「收益÷成本」排）

| 序 | 事项 | 收益 | 成本 | 说明 |
|---|---|---|---|---|
| **1** | 修 `input_schema` 注入断链 | 高 | 极低 | `tools.py:242` 一处改动，33 个工具全部受益 |
| **2** | 6 个 Skill 的 `allowed_tools`/`allowed_roles` 填实 | 高 | 低 | 纯数据，`register_skills.py` 加 record |
| **3** | `stage_order`/`stage_gate` 字段迁移 + 六节点 Agent 入库 | 高 | 中 | 一次 migration + 6 条 INSERT |
| **4** | 6 个新 Tool 实现 | 高 | 中高 | 3 个是纯包装（autofix/project_check/stdlib_meta），2 个需新逻辑 |
| **5** | 预置 published 六节点 flow + context 自动填充 | 高 | 低 | 直接激活复用机制 |
| **6** | 补 3 个 AST 元类（Message/Transition/Parameter） | 中 | 中 | 解锁时序/状态/参数三视图 |
| **7** | 熔断器 | 中 | 中 | Harness 核查已列，缺 N 次失败闸 |
| **8** | 阶段状态机路由 | 中 | 中高 | 需动 pipeline，风险最高放最后 |
| **9** | 8 个旧视图 Agent 迁disabled | 低 | 低 | 必须在 3、5 之后 |

**不建议做**：确定性自动修复的 P4「LLM 改写模型」—— 工程已明确不做（`sysml_v2_check.py:45`：自动改写会掩盖真实建模缺陷）。本方案只做**规则级** autofix，不做语义级改写。

---

## 附录 A：与现有 8 视图 Agent 的映射

| 现有 Agent（id） | 迁移去向 |
|---|---|
| 需求视图生成（165） | N3 `view_type=requirement` + S3 对应 skill |
| 结构视图生成（166） | N3 `view_type=structure` |
| 用例视图生成（168） | N3 `view_type=usecase` |
| 活动图生成（169） | N3 `view_type=activity` |
| 交互视图IBD（167） | N3 `view_type=ibd` |
| 状态机视图生成（170） | N3 `view_type=state` |
| 参数视图生成（171） | N3 `view_type=parameter` |
| 顺序视图时序（172） | N3 `view_type=sequence` |
| 多方案生成（173） | **保留 active**（变体空间候选提议，不产出模型代码，职责独立） |
| 需求分析（requirement_analysis） | N1 `requirement_structuring` 升级 |
| 预评审（review） | N4 `model_validation_repair` + N5 |
| 变更影响（impact） | 保留（独立能力） |

## 附录 B：本轮实测命令

```bash
# Agent 契约字段全空
SELECT name,input_schema,output_schema,max_concurrency FROM agents WHERE status='active';
# → 19 行，input/output_schema 全 '{}'，max_concurrency 全 2

# 8 视图 Agent 工具绑定数
SELECT a.display_name, count(t.tool_name), group_concat(t.tool_name)
FROM agents a LEFT JOIN agent_tools t ON t.agent_id=a.id
WHERE a.id BETWEEN 165 AND 173 GROUP BY a.id;
# → 9 行，count全为 1，工具全是 sysml_v2_validate

# Skill 约束字段全空
SELECT id,name,allowed_tools,allowed_roles,dependencies FROM skills;
# → 13 行，allowed_roles/dependencies 全 '[]'

# flow 全draft
SELECT status,source,count(*) FROM agent_flows GROUP BY 1,2;
# → ('draft','manual',4), ('draft','planner_auto',53)

# 6 个待建工具确认不存在
SELECT name FROM tools WHERE name LIKE '%fix%' OR name LIKE '%project_check%'
  OR name LIKE '%skeleton%' OR name LIKE '%itemize%' OR name LIKE '%stdlib%';
# → 空集

# 无 requirement_items 表
SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%requirement%';
# → 仅 requirement_quality 相关，无条目表
```

## 附录 C：阈值标定要求

本文档**不填任何未经实测的阈值**。以下项必须在实施时按判据自己测出来：

| 阈值 | 现状 | 要求 |
|---|---|---|
| N1 条目化率阈值 | 未定 | 用 36 条真实 query 采样后取分位数 |
| 阶段锁的 intents 集合 | 未定 | 采样后按召回/误召双向标定 |
| 词法权重上调幅度 | 未定 | 改 `team_router.py:73` 后跑 `verify_team_defined_orchestration.py`（59 断言守护） |
| autofix 的 `max_fixes` 默认值 | 未定 | 按实测错误密度分布定 |
| 熔断 N 次 | 未定 | 参考 `loop_guard.DEFAULT_DUP_THRESHOLD=2` 量级，实测校准 |

> 铁律：任何阈值若不来自判据本身跑出来的数，就是凭印象填的。门禁阈值虚高会让门禁形同虚设且看不出来。
