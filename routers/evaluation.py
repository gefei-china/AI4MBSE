# -*- coding: utf-8 -*-
"""评测管理端点（2026-10-22）：给「评测管理」页提供数据。

## 为什么是独立功能域（而不是塞进知识域）

评测的**对象**不是知识，而是"这套系统现在答得好不好"——它横跨检索、
决策、记忆三类能力，也横跨开发（门禁）与产品（趋势与可信度）两个视角。
塞进 `/api/knowledge/*` 会让"知识库的一个子功能"承担全局质量语义。

**命名纪律**：`/api/eval/*`（不是 `/api/evaluation/*`）——
与前端 `static/js/mods/48-eval.js` 的模块名对齐，避免两套叫法。

## 三个端点对应用户的三个问题

| 端点 | 回答什么 |
|---|---|
| `GET /api/eval/overview` | **现在什么状态** —— 各类型最新分 + 门禁接线 + 能力覆盖 |
| `GET /api/eval/history` | **历史怎么变** —— 按类型分组的时间序列（`/api/knowledge/eval/history` 的同源数据，另加趋势序列） |
| `GET /api/eval/gates` | **什么在拦着我** —— 门禁接线与豁免状态 |

## 纪律

- **router 不写 SQL** —— 一律委派 `repositories/eval_report_repo.py`
  （`verify_layering.py` 卡 router 里的 `conn.execute("SELECT...")` 增量）。
- **失败显式化**：返回 `{"ok": False, "error": ...}`，不裸奔 500。
- **不伪造数据**：没有记录就返回空 + note，**不用 0 画一条零线**。
- **缺失即 null 不填 0**：记忆评测没有 `doc_recall_at_5` 这个概念，
  填 0 会被读成"召回 0 分"。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from core.deps import db_session
from repositories import eval_report_repo as _repo

router = APIRouter(tags=["评测管理"])

#: 工程根（门禁扫描需要读 tools/verify 与 .github/workflows）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@router.get("/api/eval/overview")
def eval_overview(conn=Depends(db_session)):
    """评测总览：各类型最新分 + 门禁接线状态 + 能力覆盖。

    回答"现在什么状态"：每个评测类型最新一次跑出了什么、这些分数能不能信、
    以及有多少门禁在真正拦着回归。
    """
    hist = _repo.fetch_history(conn, limit=1)
    if not hist.get("ok"):
        return {"ok": False, "error": hist.get("error") or "评测数据不可用",
                "migrated": False, "kinds": [], "latest": {}, "gates": None}

    latest = {}
    for k, rows in (hist.get("history") or {}).items():
        if not rows:
            continue
        item = dict(rows[0])
        item["kind_label"] = _repo.EVAL_KIND_LABELS.get(k, k or "未标注类型")
        # 指标列含 (key, label, direction, gate_key)；**阈值判定在后端完成**，
        # 前端只渲染 status（"只渲染不计算"纪律）。
        item["metric_columns"] = _repo.metric_columns_for(k)
        item["metric_status"] = item.get("metric_status") or {}
        latest[k] = item

    gates = _repo.fetch_gate_wiring(_ROOT)

    # 能力覆盖：哪些层有度量、哪些没有 —— **缺口要显式说出来**，
    # 否则用户会以为"跑绿了就等于系统好"。
    covered = sorted(k for k in latest if k)
    known = set(_repo.EVAL_KIND_LABELS.keys())
    missing = sorted(k for k in known if k and k not in covered)

    return {
        "ok": True,
        "migrated": True,
        "kinds": hist.get("kinds") or [],
        "counts": hist.get("counts") or {},
        "latest": latest,
        "gates": gates,
        "coverage": {
            "covered": covered,
            "missing": missing,
            # 生成质量（忠实度/接地度）2026-10-22 起有确定性指标
            # （citation 完整性/使用率/无据声称密度/接地信号，见
            # tools/eval/eval_generation_quality.py）。动态推导而非写死：
            # 没有 generation_quality 行时这里就是 False，缺口重新可见。
            "generation_quality": "generation_quality" in covered,
            "note": ("生成质量评测已接入（引用完整性/使用率/无据声称密度）；"
                     "检索与决策分数高不代表答案对——两者都要看"),
        },
        "note": hist.get("note") or "",
    }


@router.get("/api/eval/history")
def eval_history(eval_kind: str = "", limit: int = 20, conn=Depends(db_session)):
    """评测历史（按类型分组）+ 每个类型的时间序列（用于画趋势线）。"""
    hist = _repo.fetch_history(conn, eval_kind=eval_kind, limit=limit)
    if not hist.get("ok"):
        return {"ok": False, "error": hist.get("error"), "history": {}, "trends": {}}

    trends = {}
    for k, rows in (hist.get("history") or {}).items():
        cols = _repo.metric_columns_for(k)
        # 时间序列：按时间正序（画线要正序），null 断点（不画 0）
        seq = []
        for r in reversed(rows):
            pt = {"at": r.get("created_at"), "id": r.get("id"),
                  "trustworthy": bool(r.get("trustworthy"))}
            for col in cols:
                pt[col["key"]] = r.get(col["key"])
            seq.append(pt)
        trends[k] = seq

    return {"ok": True, "kinds": hist.get("kinds") or [],
            "counts": hist.get("counts") or {},
            "history": hist.get("history") or {},
            "trends": trends,
            "metric_columns": {k: _repo.metric_columns_for(k)
                               for k in (hist.get("kinds") or [])},
            "kind_labels": _repo.EVAL_KIND_LABELS,
            "note": hist.get("note") or ""}


@router.get("/api/eval/gates")
def eval_gates():
    """门禁接线状态：哪些进了 CI、哪些带理由豁免、哪些漏了。"""
    g = _repo.fetch_gate_wiring(_ROOT)
    g["kind_labels"] = _repo.EVAL_KIND_LABELS
    return g
