# 本体模块「实体 / 属性」维护字段维度缺口评估

> 评估日期：2026-09-07 · 评估对象：`ontology_types` 表（entity 37 / relation 26 / attribute 10）+ 前端本体维护表单 + 四个下游消费端（OWL 导出 / SHACL 形状 / 实例属性编辑器 / 校验器）
> 参照系：OWL 2（类/属性公理）、SHACL（约束形状）、SKOS / ISO 704 / TBX（术语维度）、MBSE 工程属性实践（量纲单位、值域）

---

## 0. 结论先行

**整体维度覆盖率约 3 成**。结构维度（父子/定义域值域）最完整，**属性约束维度（约 2 成）、公理维度（约 2 成）、溯源维度（约 2 成）最薄**。

三个**结构性缺陷**（不是"缺字段"，而是"同一语义多套写法"，会持续产生不一致）：

1. **数据类型词表四套并存**（P0）——UI 写 `string/date/enum/decimal/text`，SHACL 导出自带一份 XSD 映射表（`bool` 而非 `boolean`），OWL 导出读 `constraints.xsd_type`（`xsd:*` 前缀），实例编辑器与新建的 xsd 校验器又只认 `xsd:*`。四者互不映射，实测 **10 个属性类型仅 1 个声明了类型**，且该声明（constraints.xsd_type）**到不了实例编辑器和校验器**——本轮（12.11）新增的 xsd 强校验对 UI 产出的数据实际是死代码。
2. **关系定义域/值域双轨**（P0）——`constraints.domain/range`（单值，UI 单选，1 条数据）与 `allowed_values.src/tgt`（列表，25 条数据，SHACL 消费）。同一语义两套字段，SHACL 只认后者，UI 只写前者。
3. **字符串型唯一约束**（P1）——`constraints.unique` 靠 `properties LIKE '%"k": "v"%'` 匹配（对 JSON 序列化格式敏感），且导出为 SHACL 时被降级成 `sh:maxCount 1`（把"全局唯一"错译为"最多一个值"）。

---

## 1. 现有字段清单与八维度覆盖

### 1.1 现有 schema（`ontology_types`）

| 列 | 类型 | 填充 | 说明 |
|---|---|---|---|
| id / name / type_kind | - | 73/73 | name 与 (name,type_kind) 均有唯一索引 |
| parent_id | INT | 30/73 | 仅 entity：`subClassOf`，子类继承属性/必填/唯一/白名单 |
| properties | TEXT(JSON) | 73/73 | **entity/attribute 全为 `{}`**；relation 存 `{transitive:bool}`；内联属性行存 `{k:{note,type,required}}` |
| constraints | TEXT(JSON) | 73/73 | entity: `required/unique/desc/allowed_values/disjoint_with`；relation: `+replaced_by/cardinality/domain/range`；attribute: `domain_classes/xsd_type` |
| description | TEXT | 53/73 | 简短描述 |
| icon / color | TEXT | 12/73 · 73/73 | icon 基本弃用（表单已移除入口），color 恒默认 |
| profile_source / profile_ref | TEXT | **0/73** | 死列：设计用于外部 profile 溯源，从未写入 |
| iri / iri_legacy | TEXT | 73/73 · 55/73 | P0-1 统一到 core.ns，遗留值留痕 |
| project_id / branch / version / status | - | 73/73 | version 为 **INTEGER 恒 1**；status 仅 `released`(62) / `deprecated`(11) |
| created_at | TEXT | 73/73 | **无 created_by / updated_at / updated_by** |

### 1.2 八维度覆盖矩阵

| 维度 | 已有 | 缺失 | 覆盖率 |
|---|---|---|---|
| **A 标识** | name、IRI、遗留 IRI | 别名/同义名(altLabel)、多语言(lang)、外部等价映射(sameAs/exactMatch) | 40% |
| **B 定义** | description、长描述(constraints.desc，与 description 割裂) | 独立 definition 字段、示例、范围注释(scopeNote)、来源依据(标准条款) | 30% |
| **C 结构** | parent(subClassOf)、disjoint_with、relation domain/range、attribute domain_classes | abstract 抽象标记、逆关系(inverseOf) | 58%（含双轨/无 UI 入口扣分） |
| **D 属性约束** | required、unique(弱)、allowed_values(纯字符串枚举)、relation cardinality | **单位/量纲**、值域 min/max、精度、正则 pattern、枚举码值(code+label)、默认值、属性多值基数、类型词表统一 | **23%** |
| **E 公理** | characteristics 六元组（后端支持，无 UI 入口） | 等价类、覆盖公理、UI 维护入口 | 17% |
| **F 生命周期** | status(2 态)、version(整数)、relation.replaced_by | draft/review 态、SemVer 版本（与快照 SemVer 双轨）、entity/attribute 的 replaced_by、弃用说明与生效时间 | 25% |
| **G 治理** | 变更留痕(API 层 before 快照 + audit)、usage 影响分析 | 责任人/steward、变更理由(rationale)、SHACL `sh:severity` 分级、`sh:closed` 扩展属性开关 | 33% |
| **H 溯源** | created_at | created_by、updated_at/updated_by、外部标准溯源(死列)、置信度 reliability | 17% |

**横向对照**：`glossary_concepts` 有 `pref_label / definition / domain / concept_status / maps_to_class / replaced_by / created_by / updated_at`，`glossary_terms` 有 `lang / term_status / source / reliability`——**词典层的元数据维度反而比本体层更完整**；且当前 **5 个概念的 `maps_to_class` 全部为空**，本体与词典两层实际是断链的。

---

## 2. 缺口清单（按优先级）

### P0 —— 正确性与一致性，直接影响已上线能力

| # | 缺口 | 现状证据 | 影响 | 建议 |
|---|---|---|---|---|
| P0-1 | **数据类型词表统一** | 四套写法并存（见 §0.1）；10 个 attribute 仅 1 个声明类型且到不了消费端 | SHACL/OWL/实例编辑器/校验器行为互不一致；枚举与数值校验形同虚设；xsd 强校验为死代码 | 建 `core/typevocab.py` 单一词表（内部规范名 ↔ `xsd:` ↔ SHACL ↔ 前端 input type 四处映射），四处消费端统一调用；回填 9 个未声明类型的属性 |
| P0-2 | **关系 domain/range 双轨归一** | `constraints.domain/range`(1 条) vs `allowed_values.src/tgt`(25 条)；SHACL 只认后者，UI 只写前者 | UI 改 FROM/TO 可能不进 SHACL；本体与门禁形状不一致 | 保留 `allowed_values.src/tgt` 为唯一事实源，`domain/range` 降级为兼容读取（读时合并写时只写一套） |
| P0-3 | **属性单位 / 量纲（unit）** | 全库无 unit 字段；带宽/EIRP/发射功率/寿命/可用度等工程属性无单位 | MBSE 语义残缺：数值无法比较/换算/校验，跨系统交换必然歧义 | 属性维度增 `unit`（建议绑定 QUDT 或单位词表）+ `quantity_kind`；实例编辑器显示单位后缀 |

### P1 —— 可维护性与治理

| # | 缺口 | 建议 |
|---|---|---|
| P1-1 | 生命周期不全：无 draft/review 态，version 是整数且恒 1（与快照 SemVer 双轨），replaced_by 只覆盖 relation | status 扩 `draft/review/released/deprecated`；version 明确为"本体条目版本"并对齐快照 SemVer 或废弃该列；replaced_by 覆盖 entity/attribute；补"弃用说明/生效时间" |
| P1-2 | 溯源缺失：无 created_by / updated_at / updated_by（词典层有 updated_at） | 对齐 `glossary_concepts` 补三列；本体变更目前只有 API 层 audit 日志，表内无法追溯"谁改的" |
| P1-3 | 多语言与别名缺失：无 lang、无 altLabel | 补 `lang`（默认 zh）+ `alt_labels`（JSON 数组）；与 `glossary_terms.lang` 打通 |
| P1-4 | 本体 ↔ 词典断链：5 概念 0 映射 | 落地 `maps_to_class` 填充（本体类型 ⇄ 概念双向），避免两套词表各自演化 |
| P1-5 | 唯一性约束脆弱：LIKE 字符串匹配 + 导出降级为 maxCount | 唯一性改为独立校验（同名同类型实例查重，不依赖 JSON 序列化格式）；SHACL 侧若需全局唯一改用自定义 `sh:sparql` 约束或保留应用层校验，不再伪装成 maxCount |
| P1-6 | 死列与死维度：`profile_source/profile_ref` 0 填充；`icon` 12/73 且表单无入口；`disjoint_with`/`characteristics`/`replaced_by` 有后端无 UI | 三选一：激活（补 UI 与导出消费）或明确废弃（标注 deprecated 不再维护） |

### P2 —— 增强项

| # | 缺口 | 说明 |
|---|---|---|
| P2-1 | 逆关系 `owl:inverseOf` | 当前 OWL 导出有 `owl:InverseFunctionalProperty` 但无 `inverseOf`；CONTAINS/⊇ 这类天然成对关系无法双向推理 |
| P2-2 | `abstract` 抽象标记 | 区分"不可实例化的分类节点"（如系统元素）与叶子类型，影响实例创建与图谱着色 |
| P2-3 | 枚举码值化 + 默认值 + pattern/precision | `allowed_values` 现为纯字符串列表，无法承载 code+label+说明；数值型缺值域/精度/正则 |
| P2-4 | SHACL `sh:severity` 分级 + `sh:closed` 按类型开关 | 门禁三模式目前是应用层全局设置；应允许"按类型"声明严重度与是否禁止扩展属性（直接对应"自由扩展 vs 严格管控"的产品策略） |
| P2-5 | 等价类 / 覆盖公理、外部标准映射（SysML V2 元类、ISO 15288） | 可复用已存在的死列 `profile_source/profile_ref`，或引入 `sameAs/exactMatch` |

---

## 3. 建议的最小修复包（下一轮可执行）

1. **P0-1 类型词表统一**（约 1 个模块 + 4 处调用点改造 + 9 条属性回填）——解锁后续所有数值/枚举校验的真实生效
2. **P0-3 单位字段**（属性维度增 2 列 + 编辑器显示 + OWL/SHACL 可选导出）——MBSE 场景刚需，成本可控
3. **P1-1/P1-2 生命周期与溯源列补齐**（migration 加 5 列 + 表单补字段 + 写入填充）——与词典层对齐，补齐"谁在什么时候改的"
4. **P1-6 死列处置**（激活或废弃二选一）——减少维护歧义

> 注：本轮仅评估，未改动代码。以上均需确认后再实施。

---

## 4. 实施记录（2026-09-07，用户确认"执行"后完成）

### 4.1 P0-1 数据类型词表统一 ✅

新模块 **`core/typevocab.py`**（词表单一事实来源）：
- `normalize()`：任意历史写法（`xsd:*` 前缀 / `bool` / `integer` / `double` / `datetime` / friendly 名）→ 规范名 `string/text/int/decimal/boolean/date/dateTime/enum`；未识别宽松兜底为 string
- `xsd_of()`：规范名 → `xsd:` IRI（enum 返回空，枚举由 allowed_values 承载）
- `check_value()`：int/decimal（含科学计数）/boolean/date/dateTime 格式正则校验

四个消费端全部接入：①`_xsd_type_errors`（声明位置同时兼容 `properties.type` 与 `constraints.xsd_type` 两种历史写法——修复"独立属性声明到不了校验器"）；②`shacl_export` 数据类型（此前缺 int、bool 拼写不一致）；③`ontology_owl` 两处 rdfs:range；④`_dproj_proj` 规范化输出 `xsd:*`。前端 `addPropRow` 类型选项补 int/boolean/dateTime，`gvRenderOntPropRow` 渲染映射兼容新旧词表。

### 4.2 P0-2 关系 domain/range 双轨归一 ✅

`_normalize_relation_domain_range()`：POST/PUT 时 `constraints.domain/range`（单值旧写法）自动归一进 `allowed_values.src/tgt`（列表唯一事实源，SHACL 消费方），原键移除 + 指向存在性校验；非关系类型误带则剥除防脏写。

### 4.3 P0-3 单位/量纲维度 ✅

attribute 的 `constraints.unit` + `constraints.quantity_kind`（免 migration）；`_dproj_proj` 暴露 `unit/quantity_kind`；UI 表单新增"单位/量纲"行（仅 attribute 显示）；实例编辑器受控属性行显示单位后缀。**首个落地：发射功率 → dBW / 功率**。

### 4.4 P1 生命周期/溯源/死列激活 ✅

- migration 加 5 列：`created_by / updated_at / updated_by / replaced_by / deprecated_note`（对齐 glossary_concepts，补"谁在什么时候创建/修改"）
- POST 填 `created_by`，PUT 填 `updated_at/updated_by`；`status` 白名单扩 `draft/review/released/deprecated`（PUT 可选变更，非法值 400）；`replaced_by/deprecated_note` 随 PUT 持久化
- 死列 `profile_source/profile_ref` **激活**（选激活而非废弃）：模型/表单/API/持久化全链打通，用途=外部标准映射（SysML V2 元类 / ISO 15288 条款），UI 表单"外部标准映射"行

### 4.5 E2E 五场景 + 回归 + 发布

| 场景 | 结果 |
|---|---|
| PUT 发射功率（unit=dBW/quantity_kind=功率/profile）→ data-properties 回读 | type=xs d:string 规范化、unit/quantity_kind 全部透出 |
| xsd:int 写 'abc' / '32'（constraints.xsd_type 声明路径） | 400 报错 / 通过（死代码复活） |
| 别名声明 `decimal` 写 '1.2.3' | 正确拦截 |
| relation 旧写法 domain/range 落库 | 自动归一 `allowed_values.src/tgt`，domain 键零残留 |
| status='draft' 持久化 + updated_by 留痕；status='bogus' | 通过 / 400 白名单拦截 |

- 回归：**P0 9/9**、**SHACL conforms=True（0 违规）**、4 个内联 script `node --check` 通过
- **本体 v1.3.0 发布**：OWL 422 triples（+9，发射功率 rdfs:range/unit 注释）、SHACL 3040 triples
- 备份：`*.bak-p13-20260907`（4 文件）+ 库备份（上一轮 `bak-deltestent-*` 之后无 schema 破坏性变更，migration 为增量加列）

### 4.6 未实施（后续按需）

- P1-3 多语言 `lang`/`alt_labels`、P1-4 本体↔词典 maps_to_class 落地（0/5）、P1-5 唯一性约束换实现、P2 全部（inverseOf/abstract/枚举码值/sh:severity/sh:closed 等）
- 存量 9 个未声明类型的属性回填需业务判断（不能代填），已有 typevocab 兜底为 string 不阻塞

---

## 5. 「新增类型」右侧弹窗表单字段评估（2026-09-07，聚焦 UI）

评估对象：本体模块新增 类/对象属性/数据属性 的右侧滑窗表单（`onttype` 模板 + `saveOntType`）。骨架合理（kind 分支显隐、重名即时检查、IRI 自动生成策略、profile/单位行为本轮已补），但**三类 kind 的字段错配程度差异极大**。

### 5.1 数据属性（attribute）——错位最严重，需重构

| 判定 | 字段 | 依据 |
|---|---|---|
| ❌ **缺（最核心）** | **数据类型声明入口** | 独立数据属性的 `constraints.xsd_type` UI 无字段；表单里唯一的类型下拉是"属性行"的（为实体内联属性设计，按属性名分键）——**typevocab 统一后，UI 仍造不出带类型的独立数据属性，只能走 API** |
| ❌ 缺 | 生命周期（status） | API 已支持 draft/review/released/deprecated，表单无入口 |
| ❌ 缺（前后端不一致） | 父属性（subPropertyOf） | 后端 `_validate_ont_parent` 明确允许 attribute 挂父（层级/继承），`saveOntType` 却强制 `parent_id=null`——功能被前端掐死 |
| ⚠️ 冗余/错位 | "属性行"区（Add 属性名/说明/类型） | 该编辑器为实体内联属性设计；对 attribute kind 产出的 `properties={名:{...}}` 无任何消费端 |
| ⚠️ 冗余/错位 | "必填属性/唯一属性" | 实例级约束作用于**类**，对属性类型无意义（`validate_node` 只读实体类型约束） |
| ⚠️ 错位 | "取值白名单 JSON（键=属性 值=列表）" | 对 attribute 应为**本属性的允许值 plain list**；当前 shape（map）对独立属性零消费端 |

### 5.2 关系类型（relation）——缺关键约束入口，带装饰性字段

| 判定 | 字段 | 依据 |
|---|---|---|
| ❌ 缺 | **特性公理 UI**（传递/对称/反对称/自反/函数型/反函数型） | 后端白名单校验、OWL `owl:*Property`、推理机全链就绪，表单无入口——现存 4 条 transitive 全是程序写入 |
| ❌ 缺 | **多域 / 多值域** | `allowed_values.src/tgt` 是列表语义（SHACL 按 `sh:or` 并集消费），UI 单选只能表达 1 个；上轮写路径已归一，但 UI 画不出多域关系 |
| ⚠️ 装饰性 | **Cardinality 四选项** | 消费端只认 `'1'`（→ sh:maxCount 1 / FunctionalProperty）；`1:N/N:1/N:M` 存了没人读——误导用户以为有约束力 |
| ⚠️ 死数据 | "关系属性行"（constraints.attributes） | 写入后**零消费端**（全库 grep 无读取） |
| P2 缺 | 逆关系 inverseOf | 后端无字段无导出 |

### 5.3 实体类型（entity）——基本合理，3 缺 2 冗余

| 判定 | 字段 | 依据 |
|---|---|---|
| ❌ 缺 | abstract 抽象标记 | 区分不可实例化分类节点 |
| ❌ 缺 | disjoint_with 互斥 UI | 后端校验+OWL `owl:disjointWith`+SHACL `sh:not` 全支持，表单无入口（数据仅 1 条） |
| ❌ 缺 | 生命周期 status / 别名 | 同 §4 P1-1/P1-3 |
| ⚠️ 合理性 | 必填/唯一/白名单 与 属性行**无联动** | 自由文本逗号分隔，易拼出不存在的属性名（无前端校验）；应改为从已定义属性行下拉多选 |
| ⚠️ 合理性 | 取值白名单手写 JSON textarea | 易错且失败静默（safeParse 吞错）；应结构化编辑 |
| ⚠️ 冗余 | **描述 + 描述（详细）双字段** | 分别写 `description` 与 `constraints.desc`，语义重叠，prefill"优先 desc_long"本身即冗余自证 |
| ⚠️ 冗余（可折叠） | IRI 字段 | 自动生成策略已好用，建议折叠为高级项 |

### 5.4 修复优先级建议

- **P0（语义错位，防继续产出死数据）**：数据属性表单重构——加数据类型下拉（typevocab 规范名）、值域白名单改 plain list、attribute kind 隐藏"属性行/必填/唯一"、补父属性选择
- **P1**：关系特性 checkbox 组（六元组）、FROM/TO 改多选、必填/唯一改属性行联动下拉、Cardinality 收敛为"函数型（1:1）"开关、关系属性行处置（激活消费或删除）
- **P2**：描述双字段合并、abstract、disjoint UI、生命周期选择、白名单结构化编辑器、IRI 折叠

> 本节仅评估，未改代码。

### 5.5 实施记录（2026-09-07，用户确认"P0 P1 P2 一起实施"后完成）

**P0 数据属性表单重构**：attribute kind 新增**数据类型下拉**（string/text/int/decimal/boolean/date/dateTime/enum，写 `constraints.xsd_type` 规范名，typevocab 全链兼容）；**允许值改 plain list**（逗号分隔 → `constraints.allowed_values` 列表，`data-properties` 按数组直出下拉、`validate_node` 按 domain 命中校验白名单——属性级白名单首次有消费端）；attribute kind 隐藏属性行/必填/唯一/白名单 map 四块错位区；**父属性打通**（`populateOntParentSelect` 扩展 kind 感知，attribute 可挂 subPropertyOf，前后端一致）。

**P1 关系约束入口**：**特性公理 checkbox 组**（传递/对称/反对称/自反/函数型/反函数型 → `constraints.characteristics`，后端白名单/OWL/推理机既有链路直接生效）；**FROM/TO 改多选**（多域/多值域 → `allowed_values.src/tgt` 列表，兼容旧单值回填）；**Cardinality 收敛为"函数型关系（1:1）"开关**（仅写 `'1'`，与 FunctionalProperty 消费语义对齐，1:N/N:1/N:M 装饰值退出）；关系属性行区块移除（存量 0 条无迁移）。

**P2 实体与通用**：**abstract 开关**（entity）+ `validate_node` 拦截抽象类型实例化（含祖先链）；**disjoint_with 多选 UI**；**必填/唯一改联动多选**（候选项实时跟随属性行增删改，`refreshOntConsLinkage`）；**白名单结构化编辑**（按属性行逐键填逗号分隔值，替代手写 JSON，兼容旧 map 形状回填）；**描述合并单字段**（历史 `constraints.desc` prefill 后收敛到 `description`，不再双写）；**生命周期下拉**（draft/review/released/deprecated，PUT 白名单既有）；**IRI 折叠为高级项**。

**验证**：4 个内联 script `node --check` 通过；E2E——抽象拦截（祖先链命中/恢复后放行）、属性白名单（拦截 X / 放行 Ka / domain 不命中不校验 / 非字符串值安全）；回归 **P0 9/9、SHACL 0 违规**；**本体 v1.4.0 发布**（OWL 426 / SHACL 3040 triples）。备份 `index.html.bak-form-20260907`、`ontology_semantics.py.bak-form-20260907`。

**遗留（低优先）**：standalone attribute 的 allowed_values/xsd_type 未进 SHACL 导出（应用层校验已覆盖，形状导出待后续）；`inverseOf` 仍无（P2 评估时已标注）。
