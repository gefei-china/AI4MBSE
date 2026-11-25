# -*- coding: utf-8 -*-
"""多方案打分算子（平台工具 `alternatives_score` 的实现）。

## 定位

**平台化原则**（对齐 `register_sysml_check_tools.py` 文件头的既有范式）：

> 「生成后必须自校验、有错则修、最多 3 轮」属**编排策略**……写在 pipeline 里意味着
> 每次调整都要改代码 + 重启；写在 DB 里则**改文本即生效**。

据此，多方案能力拆成两层：

- **算子**（本模块）：归一化、加权、推荐判定 —— 纯函数、零 IO、可单测。任何工具
  都要有实现，算法本身无法配置化。
- **策略**（维度清单、权重、门槛、编排规则）：**在 DB 里**（`generate_rules` 表 +
  `skills` 表），改配置即生效，**不改本文件**。

本模块的**唯一硬编码**是「维度清单的兜底默认值」——当 DB 未配置时用，
保证工具在**空配置**下也能工作（契约自洽，不依赖 DB 有数据）。

## 三条硬约束（与打分正确性直接相关）

1. **打分不臆造**：模型只提供**各维度的原始量**（时延 ms / 成本万元 / 覆盖条数），
   归一化与加权由本模块完成。模型自报的"综合 85 分"**不进入算法**。
2. **缺维度不补零**：缺失维度记 `None`（**不参与加权**），并在 `coverage` 里如实报告。
   补零会把"没测"变成"最差"，是隐性造假。
3. **可解释**：每方案输出逐维度归一化值与总分；推荐并列时**如实标并列**，不擅自宣称最优。

## 归一化口径

对每个维度，在**参与该维度的方案集合**内做 min-max 归一化到 0~1：

    norm = (x - min) / (max - min)        # direction=higher
    norm = (max - x) / (max - min)        # direction=lower

⚠️ **max == min（该维度所有方案取值相同）时不做除法**，直接取 0.5（中性）——
否则会 ZeroDivisionError，且"全都一样"在业务上就是"该维度无区分度"。

## 加权汇总与推荐门槛

    total = Σ(weight_i × norm_i) / Σ(weight_i, 仅计入有值且权重>0 的维度)

分母是**实际参与的权重和**，不是全部权重和 —— 否则缺维度的方案会被系统性压低，
等价于惩罚"没提供数据"，与硬约束 2 冲突。

⚠️ **推荐门槛（`min_coverage`）**：因分母只算参与维度，一个**只测了 1 个维度**
且恰好在那一维领先的方案会拿到接近满分并压过维度齐全的方案（实测复现：
coverage 20% 的方案 total=1.0，压过 coverage 100% 的方案 total=0.375）——
这就是**「少测多赢」**，会让推荐被**数据缺失**左右而非被**设计优劣**左右。
故：覆盖率低于门槛的方案**仍展示、仍计分**，但**不参与推荐**。

⚠️ **coverage 的分母 = 权重>0 的维度数**（不是全部维度数）：把某维度权重设为 0
= 用户**不关心**那一维，它没测不该拉低覆盖率。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: 打分口径版本：口径变更时递增，便于历史结果可比性
SCORING_VERSION = "2026-11-03.2"

#: 维度目录的**兜底默认值**（DB 未配置 `alternatives_dimensions` 时用）。
#: ⚠️ 这是本模块**唯一**的硬编码维度清单 —— 真实生效维度以 DB 配置为准。
#:   运营改 `generate_rules.alternatives_dimensions` 即可换维度集，**不必改代码**。
DEFAULT_DIMENSIONS: List[Dict[str, Any]] = [
    {"key": "performance", "label": "性能", "direction": "higher", "unit": "分/指标值",
     "desc": "吞吐量、时延、容量等性能表现（原始值越大越好）"},
    {"key": "cost", "label": "成本", "direction": "lower", "unit": "万元/指标值",
     "desc": "研制成本、集成成本、运维成本（原始值越小越好）"},
    {"key": "risk", "label": "风险", "direction": "lower", "unit": "条/等级分",
     "desc": "技术风险、进度风险、依赖风险（原始值越小越好）"},
    {"key": "extensibility", "label": "可扩展性", "direction": "higher", "unit": "分/指标值",
     "desc": "接口开放度、模块解耦度、后续演进空间（原始值越大越好）"},
    {"key": "requirement_coverage", "label": "需求覆盖", "direction": "higher", "unit": "条/比率",
     "desc": "覆盖的需求条目数或覆盖比率（原始值越大越好）"},
]

#: 兜底权重（DB 未配置 `alternatives_weights` 时用）
DEFAULT_WEIGHTS: Dict[str, float] = {
    "performance": 0.25,
    "cost": 0.20,
    "risk": 0.20,
    "extensibility": 0.15,
    "requirement_coverage": 0.20,
}

#: 兜底阈值
DEFAULT_TIE_EPSILON = 0.02
DEFAULT_MIN_COVERAGE = 0.6

#: 归一化后总分保留的小数位
TOTAL_PRECISION = 4

# ── 配置读取（DB → 内存参数） ─────────────────────────────────────────
#: `generate_rules` 表里承载多方案配置的 rule_key
RULE_KEY_DIMENSIONS = "alternatives_dimensions"
RULE_KEY_WEIGHTS = "alternatives_weights"
RULE_KEY_TIE_EPSILON = "alternatives_tie_epsilon"
RULE_KEY_MIN_COVERAGE = "alternatives_min_coverage"
RULE_KEY_MAX_COUNT = "alternatives_max_count"
RULE_KEY_ALIASES = "alternatives_dimension_aliases"


def _norm_key(text: Any) -> str:
    """归一化维度名：转小写、去空白、去括号内容。"""
    s = str(text or "").strip().lower()
    out = []
    depth = 0
    for ch in s:
        if ch in "（(":
            depth += 1
            continue
        if ch in "）)":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out).replace("　", "").replace(" ", "")


def normalize_dimensions(raw: Any) -> List[Dict[str, Any]]:
    """把配置里的维度清单规整为规范形态。

    接受：list[dict]（每项含 key/label/direction/unit）或 dict[key → {label,direction,...}]。
    非法项跳过；**全空时回退 `DEFAULT_DIMENSIONS`**。
    """
    out: List[Dict[str, Any]] = []
    items: List[Any] = []
    if isinstance(raw, list):
        items = list(raw)
    elif isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                d = dict(v)
                d.setdefault("key", k)
                items.append(d)
            else:
                items.append({"key": k, "label": str(k)})
    for it in items:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip()
        if not key:
            continue
        direction = str(it.get("direction") or "higher").strip().lower()
        if direction not in ("higher", "lower"):
            direction = "higher"
        out.append({
            "key": key,
            "label": str(it.get("label") or key),
            "direction": direction,
            "unit": str(it.get("unit") or ""),
            "desc": str(it.get("desc") or ""),
        })
    return out or [dict(d) for d in DEFAULT_DIMENSIONS]


def normalize_weights(raw: Any, dimension_keys: Sequence[str]) -> Dict[str, float]:
    """规整权重：只认已知维度 key；非法值忽略（未知维度记一条提示）。"""
    weights: Dict[str, float] = {k: 0.0 for k in dimension_keys}
    notes: List[str] = []
    if not isinstance(raw, dict) or not raw:
        for k in dimension_keys:
            weights[k] = DEFAULT_WEIGHTS.get(k, 0.0)
        return weights, notes
    for k, v in raw.items():
        if k not in weights:
            notes.append("忽略未知维度权重：%s" % k)
            continue
        f = coerce_number(v)
        if f is None or f < 0:
            notes.append("忽略非法权重：%s=%r" % (k, v))
            continue
        weights[k] = f
    return weights, notes


def coerce_number(value: Any) -> Optional[float]:
    """把原始量转成 float；无法转成有限数值的一律视为**缺失**（None）。

    刻意**不**把 None/""/非法值当 0 —— 见模块 docstring 硬约束 2。
    bool 不当作数字（`True` 不是 1 分）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            f = float(s)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None
    return None


def coerce_score(v: Any) -> Optional[float]:
    """把 LLM/调用方给的维度值转成 float。

    支持：数字；带单位的字符串（"120ms" / "35万元" / "80%"）→ 抽取首个数字。
    **等级词**（"高/中/低"、"优/良/差"）⇒ **不映射**（返回 None）。
    理由：等级词没有跨方案可比的数值语义，硬映射会造出假精度。
    """
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if not m:
            return None
        return coerce_number(m.group(0))
    return coerce_number(v)


def _normalize(values: Sequence[Optional[float]], direction: str) -> List[Optional[float]]:
    """min-max 归一化到 0~1。

    - 全缺失 ⇒ 全部 None（该维度整体不参与）。
    - max == min ⇒ 全部 0.5（中性；无区分度）。
    - direction == "lower" ⇒ 反向。
    """
    present = [v for v in values if v is not None]
    if not present:
        return [None] * len(values)
    lo, hi = min(present), max(present)
    span = hi - lo
    if span == 0:
        return [None if v is None else 0.5 for v in values]
    out: List[Optional[float]] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        norm = (v - lo) / span
        if direction == "lower":
            norm = (hi - v) / span
        out.append(norm)
    return out


def _extract_scores(raw: Any, dimension_keys: Sequence[str],
                    aliases: Dict[str, str]) -> Dict[str, Any]:
    """从一个候选方案对象里抽出各维度的原始量。

    兼容两种入参形态：
    - `{"scores": {"performance": 88, ...}}`（推荐）
    - `{"performance": 88, ...}`（扁平）

    维度名经**别名表**映射到规范 key；未识别 ⇒ 丢弃（不猜、不硬塞）。
    """
    if not isinstance(raw, dict):
        return {}
    src = raw.get("scores")
    if not isinstance(src, dict):
        src = raw
    out: Dict[str, Any] = {}
    for dim_name, dim_val in (src or {}).items():
        nk = _norm_key(dim_name)
        dk = dim_name if dim_name in dimension_keys else aliases.get(nk)
        if dk is None or dk not in dimension_keys:
            continue
        num = coerce_score(dim_val)
        if num is None:
            continue
        out[dk] = num
    return out


def score_alternatives(
    alternatives: Iterable[Any],
    weights: Optional[Dict[str, Any]] = None,
    tie_epsilon: float = DEFAULT_TIE_EPSILON,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    dimensions: Optional[List[Dict[str, Any]]] = None,
    aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """对一组候选方案做多维度归一化 + 加权汇总 + 推荐判定。

    参数
    ----
    alternatives : 候选方案列表。每项 dict：`{"name", "scores", "rationale"}`。
    weights : 权重覆盖（`None` ⇒ 用默认权重）。
    tie_epsilon : 最高分与次高分差 < 该值 ⇒ 判并列推荐。
    min_coverage : 推荐门槛（覆盖率低于它的方案不参与推荐）。
    dimensions : **维度清单（配置驱动）**；`None` ⇒ 用 `DEFAULT_DIMENSIONS`。
    aliases : 维度别名表（配置驱动）；`None` ⇒ 只认规范 key。

    返回
    ----
    dict：`{version, dimensions, weights, items, recommended, tie, tie_candidates,
    ineligible, notes}`。`items` 每项：
    `{name, summary, scores, normalized, rationale, total, coverage,
      missing_dimensions, rank, recommended, tie, eligible}`。
    """
    dims = normalize_dimensions(dimensions)
    dim_keys = [d["key"] for d in dims]
    dim_map = {d["key"]: d for d in dims}

    alias_map: Dict[str, str] = {}
    if isinstance(aliases, dict):
        for k, v in aliases.items():
            if isinstance(v, str) and v in dim_keys:
                alias_map[_norm_key(k)] = v

    items_in = [a for a in (alternatives or []) if isinstance(a, dict)]
    eff_weights, notes = normalize_weights(weights if weights else None, dim_keys)

    per_item_scores: List[Dict[str, Any]] = [
        _extract_scores(a, dim_keys, alias_map) for a in items_in]
    rationales: List[Dict[str, Any]] = []
    names: List[str] = []
    summaries: List[str] = []
    for i, a in enumerate(items_in):
        names.append(str(a.get("name") or a.get("title") or "方案%d" % (i + 1)))
        summaries.append(str(a.get("summary") or a.get("description") or "")[:400])
        rat = a.get("rationale")
        rationales.append(dict(rat) if isinstance(rat, dict) else {})

    # 逐维度归一化
    normalized: List[Dict[str, Optional[float]]] = [{} for _ in items_in]
    for key in dim_keys:
        direction = dim_map[key]["direction"]
        col = [per_item_scores[i].get(key) for i in range(len(items_in))]
        norm_col = _normalize(col, direction)
        for i, nv in enumerate(norm_col):
            normalized[i][key] = nv

    # 加权汇总（分母 = 实际参与的权重和）
    # coverage 的分母 = 权重>0 的维度数（权重 0 = 用户不关心，不该拉低覆盖率）
    _scored_dims = [k for k in dim_keys if float(eff_weights.get(k, 0.0)) > 0]
    if not _scored_dims:
        _scored_dims = list(dim_keys)
    out_items: List[Dict[str, Any]] = []
    for i, a in enumerate(items_in):
        num = 0.0
        den = 0.0
        missing: List[str] = []
        for key in dim_keys:
            nv = normalized[i].get(key)
            if nv is None:
                missing.append(key)
                continue
            w = float(eff_weights.get(key, 0.0))
            num += w * nv
            den += w
        total = (num / den) if den > 0 else None
        _covered = len([k for k in _scored_dims if normalized[i].get(k) is not None])
        coverage = round(_covered / len(_scored_dims), 4) if _scored_dims else 0.0
        out_items.append({
            "name": names[i],
            "summary": summaries[i],
            "scores": {k: per_item_scores[i].get(k) for k in dim_keys},
            "normalized": {k: (round(v, TOTAL_PRECISION) if v is not None else None)
                           for k, v in normalized[i].items()},
            "rationale": rationales[i],
            "total": (round(total, TOTAL_PRECISION) if total is not None else None),
            "coverage": coverage,
            "missing_dimensions": missing,
        })

    # 排名（竞赛排名：同分同名次 1,1,3）
    order = sorted(range(len(out_items)),
                   key=lambda i: (-(out_items[i]["total"]
                                   if out_items[i]["total"] is not None else -1.0), i))
    rank_of: Dict[int, int] = {}
    last_rank = 0
    last_total = None
    for pos, idx in enumerate(order, start=1):
        tot = out_items[idx]["total"]
        if last_total is not None and tot is not None and abs(tot - last_total) < 1e-12:
            rank_of[idx] = last_rank
        else:
            rank_of[idx] = pos
            last_rank = pos
        last_total = tot
    for idx, it in enumerate(out_items):
        it["rank"] = rank_of.get(idx, idx + 1)

    # 推荐门槛：覆盖率不足者仍展示计分，但不参与推荐（防「少测多赢」）
    _min_cov = max(0.0, min(1.0, float(min_coverage)))
    ineligible: List[str] = []
    for it in out_items:
        eligible = bool(it["coverage"] >= _min_cov and it["total"] is not None)
        it["eligible"] = eligible
        if not eligible and it["coverage"] < _min_cov:
            ineligible.append(it["name"])

    tie = False
    tie_candidates: List[str] = []
    recommended = None
    scored = [i for i, it in enumerate(out_items) if it.get("eligible")]
    if not scored and out_items:
        notes.append("所有方案覆盖率均低于门槛 %.2f，**不给出推荐**（需补齐维度测量后再选型）" % _min_cov)
    if scored:
        best = max(scored, key=lambda i: (out_items[i]["total"], -i))
        best_total = out_items[best]["total"]
        peers = [i for i in scored
                 if abs(out_items[i]["total"] - best_total) < max(float(tie_epsilon), 0.0)]
        recommended = out_items[best]["name"]
        if len(peers) > 1:
            tie = True
            tie_candidates = [out_items[i]["name"] for i in peers]
            for i in peers:
                out_items[i]["recommended"] = True
                out_items[i]["tie"] = True
        else:
            out_items[best]["recommended"] = True
            out_items[best]["tie"] = False
    for it in out_items:
        it.setdefault("recommended", False)
        it.setdefault("tie", False)

    return {
        "version": SCORING_VERSION,
        "dimensions": [dict(d) for d in dims],
        "weights": {k: eff_weights.get(k) for k in dim_keys},
        "items": out_items,
        "recommended": recommended,
        "tie": tie,
        "tie_candidates": tie_candidates,
        "ineligible": ineligible,
        "notes": notes,
    }


def build_comparison_table(scored: Dict[str, Any]) -> Dict[str, Any]:
    """把 `score_alternatives` 的结果转成前端可直接渲染的「矩阵」形态。

    矩阵：行 = 维度，列 = 方案；每格带 `raw`（原始量）、`norm`（归一化 0~1）、
    `rationale`（依据）。前端只渲染，不做算法。
    """
    dims = scored.get("dimensions") or []
    items = scored.get("items") or []
    rows: List[Dict[str, Any]] = []
    for d in dims:
        key = d.get("key")
        cells: List[Dict[str, Any]] = []
        for it in items:
            raw = (it.get("scores") or {}).get(key)
            norm = (it.get("normalized") or {}).get(key)
            cells.append({
                "name": it.get("name"),
                "raw": raw,
                "norm": (round(float(norm), TOTAL_PRECISION) if norm is not None else None),
                "rationale": (it.get("rationale") or {}).get(key, ""),
            })
        rows.append({
            "key": key,
            "label": d.get("label"),
            "direction": d.get("direction"),
            "unit": d.get("unit"),
            "weight": (scored.get("weights") or {}).get(key),
            "cells": cells,
        })
    return {
        "version": scored.get("version"),
        "rows": rows,
        "columns": [
            {
                "name": it.get("name"),
                "total": it.get("total"),
                "rank": it.get("rank"),
                "recommended": bool(it.get("recommended")),
                "tie": bool(it.get("tie")),
                "eligible": bool(it.get("eligible", True)),
                "coverage": it.get("coverage"),
                "missing_dimensions": it.get("missing_dimensions") or [],
                "summary": it.get("summary") or "",
            }
            for it in items
        ],
        "recommended": scored.get("recommended"),
        "tie": bool(scored.get("tie")),
        "tie_candidates": scored.get("tie_candidates") or [],
        "ineligible": scored.get("ineligible") or [],
        "notes": scored.get("notes") or [],
    }


# ── DB 配置读取（工具 handler 用；本模块其余部分保持零 IO） ─────────────
def load_config_from_db(conn=None) -> Dict[str, Any]:
    """从 `generate_rules` 表读多方案配置。

    读不到 / 解析失败 ⇒ 全部回退默认值（**工具仍可用**，不因配置缺失而失败）。
    返回：`{dimensions, weights, tie_epsilon, min_coverage, max_count, aliases}`。
    """
    cfg: Dict[str, Any] = {
        "dimensions": None,
        "weights": None,
        "tie_epsilon": DEFAULT_TIE_EPSILON,
        "min_coverage": DEFAULT_MIN_COVERAGE,
        "max_count": 3,
        "aliases": None,
    }
    _owned = False
    try:
        if conn is None:
            from database import get_db
            conn = get_db()
            _owned = True
        rows = conn.execute(
            "SELECT rule_key, rule_value FROM generate_rules "
            "WHERE rule_key IN (?,?,?,?,?,?)",
            (RULE_KEY_DIMENSIONS, RULE_KEY_WEIGHTS, RULE_KEY_TIE_EPSILON,
             RULE_KEY_MIN_COVERAGE, RULE_KEY_MAX_COUNT, RULE_KEY_ALIASES)
        ).fetchall()
        got = {r[0]: r[1] for r in rows}
    except Exception:
        return cfg
    finally:
        if _owned:
            try:
                conn.close()
            except Exception:
                pass

    def _json(key):
        raw = got.get(key)
        if raw in (None, ""):
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    dims = _json(RULE_KEY_DIMENSIONS)
    if isinstance(dims, (list, dict)):
        cfg["dimensions"] = dims
    w = _json(RULE_KEY_WEIGHTS)
    if isinstance(w, dict):
        cfg["weights"] = w
    al = _json(RULE_KEY_ALIASES)
    if isinstance(al, dict):
        cfg["aliases"] = al
    try:
        cfg["tie_epsilon"] = float(got.get(RULE_KEY_TIE_EPSILON) or DEFAULT_TIE_EPSILON)
    except (TypeError, ValueError):
        pass
    try:
        cfg["min_coverage"] = float(got.get(RULE_KEY_MIN_COVERAGE) or DEFAULT_MIN_COVERAGE)
    except (TypeError, ValueError):
        pass
    try:
        cfg["max_count"] = int(got.get(RULE_KEY_MAX_COUNT) or 3)
    except (TypeError, ValueError):
        pass
    return cfg
