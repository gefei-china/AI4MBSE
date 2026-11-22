"""本体变更的 SemVer 语义判定（**单一真源**）。

## 为什么要这个模块

改造前「一次本体编辑该算 MAJOR 还是 MINOR」有三份各自为政的实现：

| 位置 | 函数 | 实际检查的键 | 问题 |
|---|---|---|---|
| `routers/knowledge_parts/shared.py:888` | `_count_dom_range_changed` | **只有** `allowed_values` | 名字叫「定义域/值域」，却不看 `domain_classes` / `xsd_type` |
| `routers/knowledge_parts/shared.py:911` | `_count_constraints_changed` | 除 `allowed_values` 外的全部 | 一律判 MINOR + 标记「兼容」 |
| `routers/knowledge_parts/shared.py:963` | `_snapshot_dom_range_diff` | `domain` / `range` / `allowed_values` | 实际数据用的是 `domain_classes`，**这个键从没被任何一份实现读到** |

同一语义写三份，必然错位——实测（2026-11-21）7 类破坏性变更中 **6 类被判「兼容」**：
收窄 `domain_classes`、改 `xsd_type`、摘 `symmetric`、新增 `required`、新增 `unique`、
改 `unit` —— 全部落进 MINOR 分支（`compatible=1`，下游自动跟随）。

后果不是版本号不好看：**下游实例会在无提示的情况下变违规**。

## 判据来源（业界共识，非自创）

- 删除类/属性、改属性 characteristics、收窄 range/domain → **MAJOR**
- 新增类/属性、放宽约束 → MINOR
- 改 label/comment 错字 → PATCH
- **Deprecate, do not delete**：先 `owl:deprecated true` 保留一个 MAJOR 周期再删

映射到本系统的存储键，见下方 `RULE_TABLE`。**新增约束键必须同时登记进本表**，
否则 `classify_snapshot_change` 会把它当「语义未知」按破坏性处理（fail-safe），
并由门禁 `verify_semver_semantics.py` 把未登记键直接判红。
"""
from __future__ import annotations

import json

LEVEL_MAJOR = "major"
LEVEL_MINOR = "minor"

#: 语义类型类字段：任何改动都改变数据的物理/逻辑解释（`dBm`→`W` 不是格式变化，是含义变化）
SEMANTIC_TYPE_FIELDS = ("xsd_type", "unit", "quantity_kind")

#: 特性类字段：任何改动都改变推理结果，消费方可能依赖被推出的三元组 ⇒ MAJOR
#: （业界表把 `symmetric → asymmetric` 明确归 MAJOR，方向不限）
CHARACTERISTIC_FIELDS = ("characteristics",)

#: 集合类字段（值域/约束集合）。
#:   narrow_on_remove —— 移除一项即收紧（收窄可选范围 / 收窄定义域）
#:   narrow_on_add    —— 新增一项即收紧（更多必填 / 更多唯一约束）
SET_FIELDS = (
    ("domain_classes", "narrow_on_remove"),
    ("allowed_values", "narrow_on_remove"),
    ("required", "narrow_on_add"),
    ("unique", "narrow_on_add"),
)

#: 基数类字段：min 上升 / max 下降为收紧
CARD_FIELDS = ("card_src", "card_tgt", "cardinality", "card")

#: 标注类字段：不影响推理与查询形状 ⇒ MINOR
ANNOTATION_FIELDS = ("alt_labels", "lang", "label", "comment", "description")

#: 规则表（供文档与门禁对照；键 = 存储键，值 = 出现 major 的条件描述）
RULE_TABLE = {
    "xsd_type": "任意变更 → major（数据类型语义改变）",
    "unit": "任意变更 → major（物理单位改变）",
    "quantity_kind": "任意变更 → major（物理量种类改变）",
    "characteristics": "任意变更 → major（推理结果改变）",
    "domain_classes": "移除项 → major；仅新增 → minor",
    "allowed_values": "移除项 → major；仅新增 → minor",
    "required": "新增项 → major（更多必填）；仅移除 → minor",
    "unique": "新增项 → major（更多唯一）；仅移除 → minor",
    "card_src": "min↑ 或 max↓ → major；反向 → minor",
    "card_tgt": "min↑ 或 max↓ → major；反向 → minor",
    "cardinality": "min↑ 或 max↓ → major；反向 → minor",
    "card": "min↑ 或 max↓ → major；反向 → minor",
    "alt_labels": "任意变更 → minor（标注）",
    "lang": "任意变更 → minor（标注）",
    "label": "任意变更 → minor（标注）",
    "comment": "任意变更 → minor（标注）",
    "description": "任意变更 → minor（标注）",
}

#: 已登记的全部存储键（门禁用它断言「DB 里出现的键都在规则表内」）
KNOWN_FIELDS = frozenset(
    SEMANTIC_TYPE_FIELDS + CHARACTERISTIC_FIELDS
    + tuple(f for f, _ in SET_FIELDS) + CARD_FIELDS + ANNOTATION_FIELDS
)


def _load(c) -> dict:
    """约束字段 → dict。解析失败按空 dict（与各调用点既有容错一致）。"""
    if isinstance(c, dict):
        return c
    try:
        d = json.loads(c or "{}")
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _str_set(v) -> set:
    """标量/列表 → 去空、去 `?` 占位后的字符串集合。"""
    if v is None:
        return set()
    if not isinstance(v, list):
        v = [v]
    return {str(x).strip() for x in v if str(x).strip() and str(x).strip() != "?"}


def _allowed_sides(c: dict) -> dict:
    """`allowed_values` → {'src': set, 'tgt': set}。"""
    av = c.get("allowed_values") or {}
    if not isinstance(av, dict):
        return {"src": set(), "tgt": set()}
    return {"src": _str_set(av.get("src")), "tgt": _str_set(av.get("tgt"))}


def _card_pair(v) -> dict:
    """基数 → {'min': int|None, 'max': int|None}；无法解析返回空 dict。"""
    if isinstance(v, dict):
        def _i(x):
            try:
                return int(x)
            except Exception:
                return None
        out = {}
        for k in ("min", "max"):
            if k in v:
                out[k] = _i(v[k])
        return out
    if isinstance(v, str) and v.strip().isdigit():
        return {"min": int(v.strip()), "max": int(v.strip())}
    return {}


def _card_narrowed(prev: dict, cur: dict) -> bool:
    """基数收紧判定：min 上升 或 max 下降 ⇒ True。"""
    pmin, cmin = prev.get("min"), cur.get("min")
    pmax, cmax = prev.get("max"), cur.get("max")
    if pmin is not None and cmin is not None and cmin > pmin:
        return True
    if pmax is not None and cmax is not None and cmax < pmax:
        return True
    # 一侧从「无界」变为「有界」也是收紧
    if pmin is None and cmin is not None and cmin > 0:
        return True
    if pmax is None and cmax is not None:
        return True
    return False


def classify_type_change(prev_c, cur_c) -> dict:
    """单类型（同 name + type_kind）的约束变更分级。

    返回 `{'level': 'major'|'minor'|'none', 'reasons': [{'field','kind','detail'}], 'fields': set}`。
    """
    p, c = _load(prev_c), _load(cur_c)
    reasons = []
    fields = set()

    def _add(field, kind, detail):
        reasons.append({"field": field, "kind": kind, "detail": detail})
        fields.add(field)

    # ① 语义类型类：任意变更即 major
    for f in SEMANTIC_TYPE_FIELDS:
        pv, cv = p.get(f), c.get(f)
        if (pv or None) != (cv or None):
            _add(f, "semantic_type", f"{pv!r} → {cv!r}")

    # ② 特性类：任意变更即 major
    for f in CHARACTERISTIC_FIELDS:
        pv, cv = _str_set(p.get(f)), _str_set(c.get(f))
        if pv != cv:
            _add(f, "characteristics", f"{sorted(pv)} → {sorted(cv)}")

    # ③ 集合类：按方向判收紧/放宽
    for f, orient in SET_FIELDS:
        if f == "allowed_values":
            pv, cv = _allowed_sides(p), _allowed_sides(c)
            sides = ("src", "tgt")
        else:
            pv, cv = _str_set(p.get(f)), _str_set(c.get(f))
            sides = (None,)
        for side in sides:
            a = pv[side] if side else pv
            b = cv[side] if side else cv
            if a == b:
                continue
            label = f"{f}.{side}" if side else f
            removed_, added_ = a - b, b - a
            if orient == "narrow_on_remove" and removed_:
                _add(label, "narrowing", f"移除 {sorted(removed_)}")
            elif orient == "narrow_on_add" and added_:
                _add(label, "narrowing", f"新增 {sorted(added_)}")
            else:
                _add(label, "relaxing", f"移除 {sorted(removed_)} / 新增 {sorted(added_)}")

    # ④ 基数类
    for f in CARD_FIELDS:
        pv, cv = _card_pair(p.get(f)), _card_pair(c.get(f))
        if pv == cv:
            continue
        if _card_narrowed(pv, cv):
            _add(f, "narrowing", f"{pv} → {cv}")
        else:
            _add(f, "relaxing", f"{pv} → {cv}")

    # ⑤ 标注类
    for f in ANNOTATION_FIELDS:
        pv, cv = p.get(f), c.get(f)
        if (pv or None) != (cv or None):
            _add(f, "annotation", f"{str(pv)[:40]!r} → {str(cv)[:40]!r}")

    # ⑥ 未登记键：语义未知 ⇒ fail-safe 按破坏性处理（宁可让它响）
    for f in set(p) | set(c):
        if f in KNOWN_FIELDS:
            continue
        if _load({f: p.get(f)}) == _load({f: c.get(f)}):
            continue
        _add(f, "unknown_field", "约束键未登记进 RULE_TABLE，无法证明安全")

    level = LEVEL_MAJOR if any(r["kind"] in ("semantic_type", "characteristics", "narrowing", "unknown_field")
                               for r in reasons) else (LEVEL_MINOR if reasons else "none")
    return {"level": level, "reasons": reasons, "fields": fields}


def classify_snapshot_change(prev_rows, cur_rows) -> dict:
    """快照级变更分级（publish / release 两条路径共用）。

    `prev_rows` / `cur_rows`：可下标取 `name` / `type_kind` / `constraints` 的行
    （sqlite3.Row 或 dict 均可）。

    返回：
      level          —— 'major' | 'minor' | 'none'
      added/removed  —— 新增/删除的类型名（removed 含改名后的旧名）
      major_types    —— 触发 major 的类型数
      minor_types    —— 仅触发 minor 的类型数
      dom_range_types—— 定义域/值域/allowed_values 有变化的类型数（前端「域值域变更」用它）
      other_types    —— 其它约束有变化的类型数
      reasons        —— 全部原因明细（含类型名）
      unknown_fields —— 出现过的未登记约束键
    """
    def _key(r):
        return (r["name"], r["type_kind"])

    prev_map = {_key(r): _load(r["constraints"]) for r in prev_rows}
    cur_map = {_key(r): _load(r["constraints"]) for r in cur_rows}
    prev_names = {_key(r) for r in prev_rows}
    cur_names = {_key(r) for r in cur_rows}

    reasons, major_types, minor_types, dom_range_types, other_types = [], 0, 0, 0, 0
    unknown_fields = set()
    for key in prev_names & cur_names:
        res = classify_type_change(prev_map[key], cur_map[key])
        if res["level"] == "none":
            continue
        if res["level"] == LEVEL_MAJOR:
            major_types += 1
        else:
            minor_types += 1
        if res["fields"] & {"domain_classes", "allowed_values", "allowed_values.src", "allowed_values.tgt"}:
            dom_range_types += 1
        if res["fields"] & (set(KNOWN_FIELDS) - {"domain_classes", "allowed_values"}):
            other_types += 1
        for r in res["reasons"]:
            if r["kind"] == "unknown_field":
                unknown_fields.add(r["field"])
            reasons.append({"type": key[0], "kind": key[1], **r})

    removed = sorted({n for n, _ in prev_names - cur_names})
    added = sorted({n for n, _ in cur_names - prev_names})
    # 删除类型 = 破坏性；新增 = 兼容
    if removed:
        major_types += len(removed)
    level = LEVEL_MAJOR if (removed or major_types) else (LEVEL_MINOR if (added or minor_types) else "none")
    return {
        "level": level,
        "added": added,
        "removed": removed,
        "major_types": major_types,
        "minor_types": minor_types,
        "dom_range_types": dom_range_types,
        "other_types": other_types,
        "reasons": reasons,
        "unknown_fields": sorted(unknown_fields),
    }


def uncovered_keys(rows) -> dict:
    """扫描约束字段里出现但未登记进 `RULE_TABLE` 的键（门禁用）。

    返回 `{键: 出现该键的类型数}`。空 dict = 全部键已登记。
    """
    out = {}
    for r in rows:
        for k in _load(r["constraints"]):
            if k not in KNOWN_FIELDS:
                out[k] = out.get(k, 0) + 1
    return out
