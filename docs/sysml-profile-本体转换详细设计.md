# SysML Profile 导入导出与本体模型转换 详细设计

> 设计日期：2026-08-11
> 前置：本体管理调研与优化方案 §6（概要）→ 本文档为可落地详细设计；P0-1 类层级（`ontology_types.parent_id` + `OntologyValidator.classify/ancestors`）已实现，本方案直接复用
> 范围：SysML 1.x Profile（UML XMI）与 SysML 2.x（KerML metadata）的**导入**（Profile → 本体 Schema）与**导出**（本体 Schema → Profile），以及两条路径的**转换映射、中间结构、API、前端交互、验证策略**
> 定位：与 O-3 SysML 模型导入（[sysml_importer.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/sysml_importer.py)）互补——O-3 管**模型实例**，本方案管 **Schema（Profile）**，共同组成"建模工具 ↔ 平台本体"闭环

---

## 1. 目标与术语

### 1.1 两个目标
1. **导入**：把建模工具（Cameo/MagicDraw、Enterprise Architect、Papyrus、SysML v2 工具）中已有的 SysML Profile 解析为平台本体类型（`ontology_types`），作为新项目的领域知识模型基础。
2. **导出**：把平台定义的本体模型生成为 SysML Profile（1.x `.profile` 或 2.x `.kerml`），供建模工具加载使用（作为 Stereotype / metadata 应用于模型元素）。

### 1.2 术语（对齐 OMG）

| 术语 | 含义 |
|---|---|
| Stereotype | SysML 1.x 原型（基于 UML Profile 扩展机制），描述领域概念 |
| Extension | 1.x 中 Stereotype 与 UML 元类（Block/Requirement/ValueType…）的绑定 |
| ownedAttribute | Stereotype 的属性（tagged value），有数据类型或对象类型 |
| Generalization | Stereotype 间继承（对应本体 subClassOf） |
| metadata def | SysML 2.x 的元数据定义（**官方取代 1.x Profile**） |
| specialization | SysML 2.x 继承（`metadata def Child : Parent`） |

**为什么必须支持两条语法**：OMG SysML v2 规范（formal-25-09-03）明确以 metadata 机制取代 1.x Profile，但存量工具/模型以 1.x `.profile` 为主（Cameo/EA/Papyrus 生态），两者必须并行支持。

---

## 2. 输入/输出格式标准

### 2.1 SysML 1.x Profile（XMI 2.x 序列化）

结构模板（Cameo/MagicDraw、Papyrus 可加载）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<uml:Profile xmi:version="2.1"
             xmlns:xmi="http://www.omg.org/XMI"
             xmlns:uml="http://www.eclipse.org/uml2/5.0.0/UML"
             name="XingWangProfile">
  <!-- 数据类型（PrimitiveType 引用） -->
  <packagedElement xmi:type="uml:PrimitiveType" xmi:id="_T_string" name="String"/>
  <packagedElement xmi:type="uml:PrimitiveType" xmi:id="_T_real" name="Real"/>

  <!-- 枚举 -->
  <packagedElement xmi:type="uml:Enumeration" xmi:id="_E_band" name="频段">
    <ownedLiteral xmi:id="_E_band_v" name="V"/>
    <ownedLiteral xmi:id="_E_band_ka" name="Ka"/>
  </packagedElement>

  <!-- Stereotype：部件 -->
  <packagedElement xmi:type="uml:Stereotype" xmi:id="_S_part" name="部件">
    <ownedAttribute xmi:id="_A_part_name" name="名称" type="_T_string"/>
    <ownedAttribute xmi:id="_A_part_band" name="频段" type="_E_band" lower="1" upper="1"/>
  </packagedElement>

  <!-- Stereotype：载荷（继承 部件） -->
  <packagedElement xmi:type="uml:Stereotype" xmi:id="_S_zai" name="载荷">
    <generalization xmi:id="_G_zai" general="_S_part"/>
    <ownedAttribute xmi:id="_A_zai_thru" name="吞吐量" type="_T_real"/>
  </packagedElement>

  <!-- Extension：扩展 Block（声明该 Profile 用于 SysML Block） -->
  <packagedElement xmi:type="uml:Extension" xmi:id="_E_block" name="Ext_部件">
    <memberEnd xmi:idref="_EE1"/>
    <memberEnd xmi:idref="_EE2"/>
  </packagedElement>
</uml:Profile>
```

解析关注点：`xmi:id`/`xmi:type`/`xmi:idref` 属性（带命名空间）、`type`/`general` 引用按 `xmi:id` 索引解析为名称、`lower/upper` 多重性。

### 2.2 SysML 2.x（KerML 文本符号）

```kerml
package XingWangProfile {
  metadata def 部件 {
    attribute 名称 : String;
    attribute 频段 : String;   // enum 由类型约束承载
  }
  metadata def 载荷 : 部件 {
    attribute 吞吐量 : ScalarValue;
  }
  metadata def 包含 : 部件 {
    attribute : 部件;          // 关系特征（引用目标类型）
  }
}
```

解析关注点：`metadata def Name [ : Parent ] { ... }` 块、`attribute name : Type ;` 行、`import` 语句、`feature` 关键字（v2 属性声明关键字）。

### 2.3 SysML 2.x XML 表示（sysml-v2 API 结构）

v2 模型可序列化为 XML（`metadataDefinition`/`feature`/`type` 元素）。作为第三路径，复用 1.x 解析器的命名空间处理，元素映射到同一中间结构（`kind="v2"`）。

---

## 3. 转换映射规则（详细）

### 3.1 Profile → 本体（导入）

| # | Profile 元素 | 本体落点（ontology_types） | 处理细节 |
|---|---|---|---|
| M1 | Stereotype / `metadata def` | entity 类型 | `name` 去重（冲突走 §6 策略）；`description` 记录来源 |
| M2 | Generalization / `specialization : P` | `parent_id` | 先建全部类型再补层级（防前向引用）；**环检测**（复用 `_validate_ont_parent`） |
| M3 | ownedAttribute（数据类型：String/Integer/Real/Boolean） | `properties[key]={type, note}` | 类型映射：`String→string, Integer→decimal, Real→decimal, Boolean→bool, Date→date` |
| M4 | ownedAttribute（枚举类型） | `properties[key].type=enum` + `constraints.allowed_values[key]=[...]` | 枚举字面量 → 白名单 |
| M5 | ownedAttribute（对象类型：type 指向另一 Stereotype） | **relation 类型** | domain=所属 stereotype，range=被引用 stereotype → `constraints.allowed_values={src, tgt}`；同名关系合并去重 |
| M6 | 多重性 `lower=1` / `[1]` | `constraints.required[]` | `lower≥1` → required；`upper=1` 记录 `cardinality`（可选） |
| M7 | OCL `ownedRule` / 注释 | `constraints`（required/unique 尽力解析）或 `description` 原文保留 | 不做 OCL 完整解析，仅提取 `required`/`unique` 关键字，其余不静默丢失 |
| M8 | Extension 基类（Block/Requirement/ValueType…） | `ontology_profile_meta.base_metaclass_map` | 不建独立本体类型；导出时反查恢复 Extension |
| M9 | Profile 包名/版本 | `ontology_profile_meta.name/version` + `ontology_types.profile_source/profile_ref` | 每个类型标记来源，供溯源/反查 |

**对象类型属性 → relation 的判定**：`M5` 需要区分"数据类型属性"与"对象引用属性"。判定依据：属性 `type` 引用的是本 Profile 内的 Stereotype（xmi:id 在已收集集合内）→ relation；引用 PrimitiveType/Enumeration → properties。

**v2 特例**：`attribute 频段 : String` 无枚举信息时生成普通属性；枚举以 `metadata def` 内注释或独立 `enumeration` 结构（v2 通过 `range` 约束）承载，本方案以注释/`allowed_values` 兜底。

### 3.2 本体 → Profile（导出）

| # | 本体概念 | SysML 1.x 落点 | SysML 2.x 落点 |
|---|---|---|---|
| E1 | entity 类型 | `uml:Stereotype`（name=类型名，`xmi:id=_S_<名>`） | `metadata def <名> { }` |
| E2 | entity `parent_id` | `uml:Generalization general=_S_<父>` | `metadata def <子> : <父> { }` |
| E3 | `properties[key]` | `ownedAttribute`（`type` 引用 PrimitiveType；枚举生成 `uml:Enumeration` + `ownedLiteral`） | `attribute <key> : <类型>` |
| E4 | relation（含 allowed_values.src/tgt） | 引用型 `ownedAttribute`（`type=_S_<range>`，挂 domain 类型下）+ 注释标注"关系" | `feature <rel> : <目标类型>` |
| E5 | required | `lower="1"` | 注释保留（KerML 无 required 关键字） |
| E6 | allowed_values（枚举） | Enumeration + 注释 | `metadata def` 内注释 |
| E7 | cardinality | `lower/upper` | 注释保留 |
| E8 | profile 元信息（base_metaclass_map） | `uml:Extension`（恢复 Block/Requirement 绑定） | `import` + 注释 |

**关系导出为引用属性 vs Extension**：1.x 中关系（`包含`）导出为**domain 类型下的引用型 ownedAttribute**（type=range 类型），并在 `rdfs:comment`/描述里标注关系名——避免为每条关系生成独立 Stereotype（与本体"关系是类型"的模型不同构，映射到属性更贴近 1.x 用法）。完整保持关系语义靠 §7 往返测试保障。

### 3.3 中间结构（ProfileModel）

解析与生成解耦：任何来源（1x/v2/v2xml）先归一到 `ProfileModel`，映射逻辑只依赖它：

```python
ProfileModel = {
    "name": str, "version": str, "format": "1x" | "v2" | "v2xml",
    "base_metaclass_map": {"载荷": "Block"},          # stereotype → UML 元类（M8/E8）
    "enumerations": {"频段": ["V", "Ka"]},            # 枚举名 → 字面量
    "stereotypes": [{
        "name": "载荷",
        "parent": "部件" | None,                       # M2/E2
        "kind": "entity" | "relation",                # M5 判定
        "attributes": [                                # M3/M4/M6
            {"name": "吞吐量", "type": "Real", "dataType": "decimal",
             "min": 1, "max": None, "enum": None}
        ],
        "relation_ends": [{"src": "部件", "tgt": "部件"}],  # M5 relation
        "required": ["名称"],                          # M6
        "constraints_raw": "OCL 原文…",                # M7
    }],
}
```

---

## 4. 后端实现设计（新文件 `sysml_profile.py`）

零依赖，风格对齐 [ontology_owl.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/ontology_owl.py)（xml.etree + 字符串模板）；命名空间处理复用 [sysml_importer.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/sysml_importer.py) 的 `_local/_attr` 技巧（已稳定用于 XMI 解析）。

### 4.1 解析（Profile → ProfileModel）

```
parse_profile_1x(xml_text) -> ProfileModel
    1) ET 解析；_local() 去命名空间
    2) 第一遍收集：PrimitiveType/Enumeration(id→name)、Stereotype(id→name)、xmi:id 索引
    3) 第二遍遍历 Stereotype：
       - ownedAttribute：type 引用 PrimitiveType/Enumeration → attributes；引用 Stereotype → relation_ends
       - lower/upper → min/max
       - generalization general → parent
       - ownedRule → constraints_raw
    4) Extension memberEnd → base_metaclass_map
    5) 关系判定：被引用的 Stereotype 若只作"对象引用"则保持 entity，不派生 relation；
       生成的 relation 命名 = 引用属性名（如属性"包含"→ 关系"包含"）

parse_profile_v2(text) -> ProfileModel
    1) 正则提取 package 块；匹配 metadata def 名/父/体
    2) 体内 attribute/feature 行：`attribute 名 : 类型 ;` / `feature 名 : 类型 ;`
    3) import/注释 → 记录
    4) 类型引用到本包其他 metadata def → relation_ends；否则属性

parse_profile_v2_xml(xml_text) -> ProfileModel
    1) 复用 _local/_attr；metadataDefinition/feature/type 映射
    2) 归一到同一 ProfileModel（format="v2xml"）
```

### 4.2 映射（ProfileModel → 本体候选/入库）

```
profile_to_candidates(conn, pm) -> {candidates: [...], conflicts: [...]}
    # candidates 与 v2g 候选同构，走"预览 → 确认"；conflicts 标记同名/父缺失

import_profile(conn, pm, strategy) -> {imported, skipped, conflicts, profile_id}
    1) 写 ontology_profile_meta（name/version/format/base_metaclass_map）
    2) 建类型：entity 全部先建（含 parent 占位），relation 后建（domain/range 已存在）
    3) 补 parent_id（复用 _validate_ont_parent 防环）
    4) strategy: skip | overlay（见 §6）
    5) 每个类型写 profile_source/profile_ref
```

### 4.3 生成（本体 → Profile）

```
to_profile_1x(conn) -> str
    1) 读 ontology_types（含 parent_id/properties/constraints）+ ontology_profile_meta
    2) 生成 PrimitiveType/Enumeration 定义块（按需）
    3) entity → uml:Stereotype + ownedAttribute + generalization
    4) relation → 挂到 domain 类型的引用属性（type=range）
    5) Extension（base_metaclass_map 反查；缺省 Block）
    6) 名称 → 合法 xmi:id（复用 _uri 的 re.sub 清洗）

to_profile_v2(conn) -> str
    1) package 包裹；entity → metadata def（父用 specialization）
    2) properties → attribute；relation → feature
    3) 注释保留 required/枚举
```

---

## 5. API 设计（routers/knowledge.py 增一组，注册进 knowledge_router）

| 端点 | 方法 | 请求 | 响应 |
|---|---|---|---|
| `/api/knowledge/profile/parse` | POST | `{content, format: "1x"\|"v2"\|"v2xml", name?}` | `{candidates, conflicts, enumerations, base_metaclass_map, stats}`（不入库） |
| `/api/knowledge/profile/import` | POST | `{content 或 profile_model, format, strategy: "skip"\|"overlay"}` | `{imported, skipped, conflicts, profile_id}` + audit |
| `/api/knowledge/profile/export?fmt=1x\|v2` | GET | — | `text/plain`（.profile / .kerml），Content-Disposition 带时间戳文件名 |

校验：
- format 非法 → 400
- 解析失败 → 400 `{error}`（XML 解析失败/无 metadata def）
- import 时同名冲突且 strategy=skip → 计入 skipped；overlay → 更新（先校验实例影响，见 §6）
- **删除保护**：Profile 中不存在的现有类型不删除（只增不删）

---

## 6. 冲突与一致性策略

| 场景 | 策略 |
|---|---|
| 同名类型（Profile vs 现有） | import 弹窗二选一：`skip`（默认，对齐 OWL 导入）\| `overlay`（以 Profile 覆盖属性/层级） |
| overlay 破坏性变更 | 覆盖前统计影响实例数：`SELECT COUNT(*) FROM entities WHERE entity_type=名`，>0 时前端红字提示"影响 N 个实例"，仍可确认（复用 R2RML 引用阻断的检查思路，此处为提示非阻断） |
| parent 缺失（父类型不在 Profile 也不在库） | candidates 标记 conflict，默认跳过该类型的层级，不静默挂错父 |
| parent 成环 | 复用 `_validate_ont_parent` 防环校验，阻断并提示 |
| 关系 domain/range 引用缺失 | 跳过该条 relation 并计入 conflicts（提示补建类型） |
| 重复导入（同一 Profile 二次导入） | 依据 profile_meta 版本比对：同版本 → 提示"已导入过"；新版本 → 走 diff 报告（新增/修改/跳过） |

---

## 7. 验证策略（含往返测试）

### 7.1 单元级（tests/ 新增 test_sysml_profile.py）
1. `parse_profile_1x`：给定 §2.1 XMI → 断言 ProfileModel（stereotypes/attributes/generalization/enum/extension）
2. `parse_profile_v2`：给定 §2.2 KerML → 断言 ProfileModel
3. `to_profile_1x/v2`：给定内存库（部件/载荷/天线/包含 + 层级）→ 断言输出含 `uml:Stereotype`/`generalization`/`metadata def`/`specialization`
4. **往返 round-trip**：`to_profile_v2(库A)` → `parse_profile_v2` → 重建库B → 断言库A 与库B 的 (name, parent, properties, required) 集合一致
5. 冲突/防环/overlay 用例

### 7.2 接口级
- parse/import/export 三端点：合法/非法输入、skip/overlay、重复导入

### 7.3 页面交互（复用既有验证范式）
- KB-C 页签导入 Profile → 预览候选 → 确认入库 → 左栏出现新类型（含层级缩进，P0-1 已支持）
- RDF/Profile 视图导出 → 下载文件 → 用 Cameo/Papyrus 打开验证可加载（人工抽查一次）

---

## 8. 前端交互设计（复用现有 UI 范式，不引入新组件）

### 8.1 导入入口（KB-C / O-3 面板，[index.html](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/static/index.html#L782-L788)）
- 新增按钮「⬆ 导入 SysML Profile」→ 弹窗（复用 promptDialog 风格）：
  - 格式选择：`SysML 1.x (.profile/.xmi/.uml)` / `SysML 2.x (.kerml)` / `SysML 2.x XML`
  - 上传文件（accept=".profile,.xmi,.uml,.kerml,.xml"）或粘贴文本
  - 解析预览：类型候选卡（名称/父类/属性数/关系数 + 冲突徽标，勾选确认，对齐 v2g 治理中心交互）+ 映射统计（新增 N / 跳过 M / 冲突 K）
  - 冲突弹窗：同名类型选 skip/overlay

### 8.2 导出入口（KB-C / RDF 视图旁）
- 新增「Profile」Tab（与 rdf-fmt 三格式同款切换）：
  - `SysML 1.x (.profile)` / `SysML 2.x (.kerml)` 双格式，预览（<pre> 复用 ont-rdf-pre 样式）+「⬇ 下载」

---

## 9. 数据模型扩展（database/migrations.py `_ensure_ontology_types` 同批）

```sql
-- ontology_types 增两列
ALTER TABLE ontology_types ADD COLUMN profile_source TEXT DEFAULT '';
ALTER TABLE ontology_types ADD COLUMN profile_ref TEXT DEFAULT '';

-- 新表：Profile 导入元信息
CREATE TABLE IF NOT EXISTS ontology_profile_meta (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  version TEXT DEFAULT '1.0',
  format TEXT NOT NULL,              -- 1x | v2 | v2xml
  base_metaclass_map TEXT DEFAULT '{}',   -- JSON: {stereotype: Block|Requirement|...}
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## 10. 实施计划与优先级

| # | 任务 | 优先级 | 依赖 |
|---|---|---|---|
| 1 | `parse_profile_v2` + `to_profile_v2`（KerML 主路径） | **P1** | 无 |
| 2 | `parse_profile_1x` + `to_profile_1x`（Cameo/EA 兼容） | P1 | 无 |
| 3 | 数据迁移（两列 + profile_meta 表） | P1 | 无 |
| 4 | 三个 API 端点 + import 冲突策略 | P1 | 1/2/3 |
| 5 | 前端导入/导出交互 | P1 | 4 |
| 6 | 往返测试 + 冲突/防环用例 | P1 | 1/2 |
| 7 | overlay 实例影响提示（红字统计） | P2 | P0-1 层级 |

---

## 11. 明确不做 / 边界

- **不解析 OCL 完整语义**：仅提取 required/unique，其余原文保留（防信息丢失）
- **不做 1.x 与 2.x 互转**：统一经"本体"中转（Profile→本体→Profile），避免维护两套直转映射
- **不引入 rdflib/JVM 推理器**：保持零依赖；导出 Profile 后由建模工具自行校验
- **不做 Profile 的增量同步（建模工具修改后回写平台）**：列为观察项，客户明确要求再评估（可复用 sysml_sync 的 ref 映射思路）
