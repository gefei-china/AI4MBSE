# AI 建模入库向导：问题分析与方案说明

- 日期：2026-08-20
- 范围：仅分析/答复，不修改代码（如需落地可基于本文方案实施）
- 相关文件：
  - `text_normalize.py`（名称归一 / 别名表）
  - `vector2graph.py`（消歧 / 确认入库）
  - `sysml_importer.py`（SysML 解析 / 候选化 / 本体校验）
  - `routers/sysml_versions.py`（候选化接口）
  - `static/index.html`（入库向导前端）

---

## 0. 数据流（入库向导做什么）

```
消息卡「入库」/版本「入库」
  → sysmlImportWizard → sysmlImportByVersion          (index.html L5459/L5474)
  → POST /api/sysml-versions/{id}/import-candidates  (sysml_versions.py)
      → sysml_to_candidates                          (sysml_importer.py L668)
          解析 SysML(文本/JSON/XML) → 实体/关系候选写入 v2g_candidates(SYSM-批次)
          本体语义校验 validate_node → 不合法打 rejected
          消歧打标 _disambiguate_many / _disambiguate_rel
  → openSysmlImportDialog 右侧抽屉展示候选 + 消歧徽章  (index.html L5532)
  → POST /api/knowledge/v2g/confirm → confirm_candidates  (vector2graph.py L654)
      本体校验 + 重复策略(create/align/skip) + 写入 entities/relations
      + 进入「实体/关系审核队列」正式审核
```

向导里每个候选上显示的徽章，正是「本体校验 + 消歧」两套判定的可视化结果（`_sysmlMatchBadge`，index.html L5519）：✅新建 / ⚠与已有重复 / 🛑本体不通过。

---

## 1. 问题 2：这里的校验规则是什么？比对逻辑是什么？

### 1.1 结论：是「与已入库数据直接比对」，但不只是比对，而是三层

| 层 | 发生时机 | 比对对象 | 判定 | 失败表现 |
|---|---|---|---|---|
| ① 本体语义校验 | 候选化时（入库前） | 本体（`ontology` 实体类型 + 属性/关系约束） | `OntologyValidator.validate_node` | `🛑 本体校验不通过`，自动 `rejected` |
| ② 实体消歧 | 候选化时（入库前） | `entities` 表（已入库且 `status!='deprecated'`） | `_disambiguate_many` | `⚠ 重复`徽章 |
| ③ 关系消歧 | 候选化时（入库前） | `relations` 表（非废弃边） | `_disambiguate_rel` | `⚠ 与已有关系重复` |
| ④（兜底）确认入库 | 确认时 | 图谱现状 | `confirm_candidates` | 源/目标未入库 → 拒绝 |

对应代码：`sysml_importer.sysml_to_candidates`（②③）、`vector2graph._disambiguate_many/_disambiguate_rel`、`vector2graph.confirm_candidates`。

### 1.2 实体比对逻辑（`_disambiguate_many`，vector2graph.py L86）

对每个候选名，与已入库实体名比对，**按优先级**取首个命中：

1. `dup_high`（高度重复）：候选名与已入库实体名**完全相同**，或 `normalize_name` 归一后**全等**（全角→半角、去空白括号尾注、小写、去“/改进型/方案N/vN”等后缀）。
2. `dup_suspect`（疑似重复）：
   - 名称**互相包含**（双方 ≥2 字，如 `星载转发器` ⊃ `转发器`）；或
   - **字符重叠率 ≥0.8 且长度差 ≤2**（如 `高轨卫星系统` ↔ `低轨卫星系统`）。
3. `none`（无重复）：都不命中 → 判为「新建」。

性能上先分桶（canopy 前缀 + bigram，键取自 `normalize_full`），桶内才两两判；但**最终 dup/dup 高判定的比较对象是原始 `name`/`other` 字符，不是 normalize_full 后的规范名**——这一点对“中英能否判断”至关重要（见 §2）。

### 1.3 关系比对逻辑（`_disambiguate_rel`，vector2graph.py L169）

只有一个规则：

- 两端实体先在 `entities` 表精确按**原文名**查到 `id`（`name=?`）；任一端查不到 → 直接返回 `none`（无法比对）。
- 若已入库 `relations` 表存在**完全相同三元组** `(source_id, relation_type, target_id)`（且非废弃）→ `dup_high`，否则 `none`。

注意：关系消歧**两端实体必须已入库**。确认入库时（`confirm_candidates` L750 起）若源/目标节点未入库，该关系会被拒绝并提示「源/目标节点未入库（需先确认节点候选）」。

### 1.4 重复处理策略（确认时）

`confirm_candidates` 对消歧为重复的候选，弹窗里选 `dup_action`：
- `create` 强制新建（保留重复）
- `align` 对齐合并到已有元素（不建新节点/新边）
- `skip` 跳过不入库（留痕）

---

## 2. 名字中英文能否判断？—— 结论与关键漏洞

### 2.1 结论

**现状：基本不能可靠判断“中英文同名 = 同一实体”。** 内置的 4 组中英对照 + 若干缩写能够进入**同一分桶**，但**不能得出“判为同一实体”**。对于未内置的英文（如 天线/antenna、功放/power amplifier、热控/thermal control）则连分桶相撞的机会都没有，基本判为「新建」，可能产生重复实体。

### 2.2 根因（重要）

`_disambiguate_many` 里：
- **分桶**（缩候选集）用的是 `normalize_full(name)` 的前缀 + bigram；`_ALIAS_TABLE`/`_SHORT_FULL_TABLE` 只在 `normalize_full` 里生效，所以中英文名会在这一步被归到同一个候选桶。
- **但最终 `dup_high`/`dup_suspect` 的判定用的是 `name`/`other` 的原始字符**（vector2graph.py L146-157）：

```python
for other, eid in cand_items:
    if len(name) >= 2 and len(other) >= 2 and (name in other or other in name):
        out[name] = ("dup_suspect", eid); break
    ...
    s1, s2 = set(name), set(other)          # 原始字符集
    if m and inter / m >= 0.8 and ... and abs(len(name) - len(other)) <= 2:
        ...
```

因此即使 `normalize_full("antenna") == normalize_full("天线") == "天线"`（同一桶），最后的 `("天线" in "antenna")` 与中英字符集重叠均为 **假/0** → 判 `none`（新建）。

**推论**：即使“不改消歧逻辑、只扩别名表”，在**入库向导这条路径（`_disambiguate_many`）上也不会自动生效**。别名表扩充唯一能“自动生效”的地方是 `normalize_full` 全等的强规则合并（`staging_fuse`/`detect_and_save_candidates`），但 AI 建模入库走的是 `_disambiguate_many + confirm_candidates`，不是那条强规则合并路径。

### 2.3 增强方案（两处，缺一不可）

**① 扩充 `_ALIAS_TABLE`**（text_normalize.py L120），补齐 MBSE 中英高频术语对照，规范名对齐本体/种子已入库实体，避免误合并不存在概念：

- 天线 / antenna、相控阵天线 / phased(array) antenna
- 功率放大器（功放/TWTA）/ power amplifier / amplifier / travelling wave tube amplifier
- 电源（电源分系统）/ power / power subsystem / power supply
- 热控（热控分系统）/ thermal control / thermal subsystem
- 姿态 / attitude、轨控 / orbit control、姿轨控 / attitude and orbit control / AOCS
- 信关站 / gateway、地面段 / ground segment、通信载荷 / communication payload

**② 在 `_disambiguate_many` 桶内循环补一条 `normalize_full` 全等判定**（vector2graph.py L146 处，`nf = normalize_full(name)` 已算好，补 `if nf and normalize_full(other) == nf: → dup_high; break`）。只在分桶命中后比较规范名，**不影响现有阈值与顺序**，且不破坏既有行为（如「高轨/低轨卫星系统 → dup_suspect」：两者 normalize_full 不全等，仍走原 0.8 重叠逻辑）。

可选：本体/术语表（glossary）动态加载映射，落库维护而非硬编码（`glossary.py` 已有 `GlossaryMatcher` 可扩展）。

---

## 3. 问题 1：关系展示不完整

### 3.1 前端渲染逻辑（index.html `openSysmlImportDialog` L5532 / `_relLabel` L5544）

关系候选行名：
```js
const _relLabel = c => (c.rel_source && c.rel_type && c.rel_target)
  ? `${c.rel_source} —${c.rel_type}→ ${c.rel_target}`
  : (_nm(c) === '关系候选' ? '' : _nm(c));
```

- 正常数据流下，候选带 `rel_source/rel_type/rel_target` → 显示完整 `A —R→ B`（首开与幂等重开均如此）。
- 兜底分支里：若候选名恰为 `“关系候选”` 且缺少 `rel_*` 字段，返回**空串** → 关系行显示为空白。
- 抽屉容器 `.drawer .d-body` 已设 `overflow-y:auto`（index.html L508），整体可滚动，不存在“被裁掉看不到”的问题。

### 3.2 对「展示不完整」的几种可能性与建议

- **空白/不完整行**：若某些关系候选缺 `rel_*` 字段，`_relLabel` 会渲染空行。修复：`_relLabel` 增加对 `entity_name` 中 `A --R-- B` 拼接串的兜底解析（与确认入库时 `confirm_candidates` 的解析逻辑一致）。
- **数量少于源 SysML**：候选化阶段有多种合法收敛——跨视图三元组去重、容器/分组节点（需求清单/组）及其关联边剔除、`entity_name` `[:80]` 截断。若“展示不完全”指关系条数与源模型不一致，属**数量口径**差异而非渲染缺陷，需到具体版本复现确认。
- **确认后被拒**：两端实体未入库的关系，展示时正常、确认时被拒（`源/目标节点未入库`）。

建议先**在向导上复现**：生成一个候选批次，核对「源 SysML 关系数 vs 向导展示关系数 vs 确认成功数」，区分是渲染空白、数量收敛、还是被拒，再对症修复。

---

## 4. 相关代码定位（速查）

| 关注点 | 位置 |
|---|---|
| 别名表 / 简全称表 / normalize_full | `text_normalize.py` L120 / L131 / L144 |
| 实体消歧（分桶 + 原始字符判定） | `vector2graph.py` L86-158 |
| 关系消歧（三元组比对） | `vector2graph.py` L169-192 |
| 确认入库（重复策略 + 两端未入库拒绝） | `vector2graph.py` L654-893（L741 起关系、L773 起节点） |
| 候选化 + 本体校验 + 消歧打标 | `sysml_importer.py` sysml_to_candidates L668-853 |
| 候选化接口（幂等重开） | `routers/sysml_versions.py` L91 |
| 入库向导前端（渲染 / 关系行名 / 徽章 / 确认） | `static/index.html` L5532 / L5544 / L5519 / L5612 |