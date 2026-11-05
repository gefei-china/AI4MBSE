# -*- coding: utf-8 -*-
"""能力中心账本 API（P0-2 / P0-1 / P0-3 · 2026-10-27）—— **全部只读**。

## 为什么需要（走查结论）

`capability_registry` 自 2026-10-24 建好：205 行统一投影、度量口径经对拍完全准确
（`graph_retrieve` 442=442、`sysml_v2_validate` 159=159…），但 `routers/` 里**引用 0 处**
⇒ 只有告警循环与周报在读，**能力中心界面看不到它**。等于"有账本没账本柜"：
业务方想问"这个能力到底有没有在用"只能靠 SQL。

同时有两个"视图与事实不符"的问题，本路由一并给出**如实**的呈现：

| 问题 | 现象 | 本路由的回答 |
|---|---|---|
| P0-1 绑定 ≠ 消费 | `file_read` 绑定 0 个 Agent，却调用 25 次 | `/ledger` 的 2×2 矩阵：**声明集**与**调用集**分开算 |
| P0-3 任务级追溯断裂 | `run_id` 83.2% 为 0，与 `llm_usage_stats.trace_id` 不可 JOIN | `/trace` 给填充率；`trace_id` 已贯通（P0-3 落地后） |

## 端点

| 方法 | 路径 | 回答什么问题 |
|---|---|---|
| GET | `/api/capabilities/ledger` | 统一账本：每个能力的声明入口 × 实际调用（2×2 判定） |
| GET | `/api/capabilities/registry` | 注册表分页/筛选（按 kind / status / 关键字） |
| GET | `/api/capabilities/trace` | 追溯填充率 + "可 JOIN 的轮次"占比 |
| GET | `/api/capabilities/trace/detail` | 某个 trace 调了哪些能力、花了多少 token |

## 权限口径

与 `monitor` / `diagnostics` 同纪律：**只读端点不加权限门**（本仓既有运维只读端点均开放读）。
数据是"能力使用统计"，不含用户隐私正文（`arguments` / `result` 不在本路由返回）。
⚠️ 本路由全部方法只 `SELECT`，不做任何写操作（连 registry 刷新都不做 ——
保持"读接口不改状态"的直觉，刷新由 `mcp_health` 巡检与启动 reconcile 负责）。
"""
from fastapi import APIRouter, Depends, Query

from core.capability_effective import ENTRY_LABELS, build_ledger
from core.deps import db_session

router = APIRouter(tags=["能力中心 · 账本"])

_KINDS = ("tool", "skill", "plugin", "mcp_server", "mcp_tool", "prompt")
_STATUS = ("active", "disabled")


@router.get("/api/capabilities/ledger")
def capability_ledger(days: int = 30, verdict: str = "", kind: str = "",
                     limit: int = 500, conn=Depends(db_session)):
    """统一账本（**声明集 × 调用集**）。

    `verdict` 可选过滤：`in_use` / `declared_unused` / `runtime_only` / `unused`。
    这是本组端点里最重要的一个 —— 它把走查发现的"绑定 0 次却调用 25 次"
    这类矛盾变成**可筛选的判定**，而不是靠人肉 SQL 对比两张表。
    """
    data = build_ledger(conn, days=days)
    rows = data["rows"]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if verdict:
        rows = [r for r in rows if r["verdict"] == verdict]
    total = len(rows)
    data["rows"] = rows[:max(1, int(limit or 500))]
    data["total"] = total
    data["filtered"] = {"kind": kind, "verdict": verdict}
    return data


@router.get("/api/capabilities/registry")
def capability_registry(kind: str = "", status: str = "", q: str = "",
                       limit: int = 100, offset: int = 0, conn=Depends(db_session)):
    """注册表分页（原始投影行，含度量字段）。

    与 `/ledger` 的分工：本端点是"账本的原始流水"，`/ledger` 是"加上语义判定后的报表"。
    """
    where, params = [], []
    if kind:
        where.append("kind=?")
        params.append(kind)
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append("(capability_id LIKE ? OR display_name LIKE ?)")
        params += ["%%%s%%" % q, "%%%s%%" % q]
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) FROM capability_registry" + w, tuple(params)).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM capability_registry" + w +
        " ORDER BY call_count DESC, kind, capability_id LIMIT ? OFFSET ?",
        tuple(params) + (max(1, min(int(limit or 100), 1000)), max(0, int(offset or 0)))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append(d)
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, COUNT(*) FROM capability_registry GROUP BY kind").fetchall()}
    return {
        "total": total, "limit": limit, "offset": offset,
        "by_kind": by_kind,
        "rows": out,
        "entry_labels": ENTRY_LABELS,
        "hint": ("度量 call_count/fail_count/last_used_at 由 tool_call_logs 只读聚合而来"
                 "（不双写，避开放大 skills.use_stats 的丢更新）。"),
    }


def _fill_rate(conn, col: str, days: int, extra_where: str = "") -> dict:
    """某一列的填充率（**口径必须一起返回**，否则数字会被误读）。

    S0（2026-10-28）：默认排除探针列（`core.probe_mark`）。理由：填充率衡量的是
    **生产链路**有没有正确写关联键，而验证脚本的假工具本就不会写 trace_id/run_id
    ⇒ 混进来会凭空压低覆盖率（实测 fake_tool 126 行全空）。口径随结果一起返回，
    调用方看得见"这是排除探针后的数"。
    """
    try:
        from core.probe_mark import probe_where as _pw
        probe = _pw()
    except Exception:
        probe = ""
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN COALESCE(%s,'')<>'' AND %s<>0 THEN 1 ELSE 0 END) "
            "FROM tool_call_logs WHERE created_at >= datetime('now', ?) %s%s"
            % (col, col, extra_where, probe), ("-%d days" % days,)).fetchone()
        total, filled = int(row[0] or 0), int(row[1] or 0)
    except Exception:
        return {"column": col, "total": 0, "filled": 0, "rate": None}
    return {"column": col, "total": total, "filled": filled,
            "rate": (round(filled * 100.0 / total, 2) if total else None),
            "excludes_probe": True}


@router.get("/api/capabilities/trace")
def capability_trace(days: int = 7, conn=Depends(db_session)):
    """任务级追溯现状：各关联键的填充率 + `trace_id` 能否与 LLM 使用量 JOIN。

    这直接回答走查里的 P0-3：**「这一轮对话调了哪些能力、花了多少钱」能不能答**。
    `joinable_traces` = 两表都出现过的 trace_id 个数 —— 它是"能不能答"的硬指标。
    """
    cols = [
        _fill_rate(conn, "trace_id", days),
        _fill_rate(conn, "run_id", days),
        _fill_rate(conn, "task_key", days),
        _fill_rate(conn, "conversation_id", days),
        _fill_rate(conn, "capability_id", days),
        _fill_rate(conn, "user_name", days),
    ]
    joinable = 0
    tool_traces = 0
    llm_traces = 0
    try:
        tool_traces = conn.execute(
            "SELECT COUNT(DISTINCT trace_id) FROM tool_call_logs "
            "WHERE COALESCE(trace_id,'')<>'' AND created_at >= datetime('now', ?)",
            ("-%d days" % days,)).fetchone()[0]
        llm_traces = conn.execute(
            "SELECT COUNT(DISTINCT trace_id) FROM llm_usage_stats "
            "WHERE COALESCE(trace_id,'')<>'' AND created_at >= datetime('now', ?)",
            ("-%d days" % days,)).fetchone()[0]
        joinable = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT trace_id FROM tool_call_logs "
            " WHERE COALESCE(trace_id,'')<>'' AND created_at >= datetime('now', ?)) t "
            "WHERE EXISTS (SELECT 1 FROM llm_usage_stats l WHERE l.trace_id=t.trace_id "
            "              AND l.created_at >= datetime('now', ?))",
            ("-%d days" % days, "-%d days" % days)).fetchone()[0]
    except Exception:
        pass
    return {
        "window_days": days,
        "fill": cols,
        "tool_traces": tool_traces,
        "llm_traces": llm_traces,
        "joinable_traces": joinable,
        "note": ("run_id = 一轮对话的编排批次号：多 Agent 编排路径由 `_stream_orchestrated_flow` 分配；"
                 "单 Agent 直行路径现也在每轮就地分配（2026-10-27 归正），故直行工具调用不再恒 0。"
                 "run_id 用于「按轮归批」；跨系统的「这一轮花了多少/调了什么」仍用 trace_id 与 "
                 "llm_usage_stats JOIN（两套 id 各司其职：run_id 管批、trace_id 管链）。"),
    }


@router.get("/api/capabilities/trace/detail")
def capability_trace_detail(trace_id: str = Query(..., min_length=4), conn=Depends(db_session)):
    """某一轮（一个 trace）调了哪些能力、花了多少 —— P0-3 的最终验收口径。"""
    calls = []
    try:
        for r in conn.execute(
                "SELECT id, tool_name, tool_type, capability_id, ok, latency_ms, "
                "       run_id, task_key, conversation_id, user_name, failure_kind, created_at "
                "FROM tool_call_logs WHERE trace_id=? ORDER BY id", (trace_id,)).fetchall():
            calls.append(dict(r))
    except Exception:
        calls = []
    llm = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "cache_hit_tokens": 0, "cost": 0.0, "intents": []}
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
            "       COALESCE(SUM(prompt_cache_hit_tokens),0), COALESCE(SUM(estimated_cost),0) "
            "FROM llm_usage_stats WHERE trace_id=?", (trace_id,)).fetchone()
        llm.update({"calls": int(row[0] or 0), "prompt_tokens": int(row[1] or 0),
                    "completion_tokens": int(row[2] or 0),
                    "cache_hit_tokens": int(row[3] or 0),
                    "cost": round(float(row[4] or 0.0), 6)})
        llm["intents"] = [r[0] for r in conn.execute(
            "SELECT DISTINCT intent FROM llm_usage_stats WHERE trace_id=? ORDER BY intent",
            (trace_id,)).fetchall()]
    except Exception:
        pass
    return {
        "trace_id": trace_id,
        "tool_calls": calls,
        "tool_call_count": len(calls),
        "llm": llm,
        "hint": ("tool_call_count>0 且 llm.calls>0 ⇒ 这一轮的工具与成本已串成一条链"
                 "（P0-3 的目标）。任一为 0 = 该轮没有工具调用 / 没有 LLM 调用（正常）。"),
    }
