"""统一监控平台（D12，8.2「监控与日志面板」+「监控面板和性能分析工具」）。

能力域：/api/monitor/*
- dashboard：统一仪表盘（系统/运行/LLM 真实指标 + 近 N 天趋势 + 节点耗时 + 最近告警）
- logs：运行日志检索（run_id / 节点类型 / 状态 / 关键词，分页）
- alerts：告警事件列表 + 确认
- alert-rules：告警规则 CRUD + 启停
告警评估在执行器（FlowExecutor._evaluate_alerts）运行结束后触发，本域只读/管理。
"""
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session
from models import AlertRuleIn

router = APIRouter(tags=["统一监控"])


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


@router.get("/api/monitor/dashboard")
def monitor_dashboard(days: int = 7, conn=Depends(db_session)):
    """统一监控仪表盘：真实采集的系统/运行/LLM 指标 + 趋势 + 节点耗时 + 最近告警。"""
    # ── 运行聚合（近 N 天）──
    runs = _rows(conn, "SELECT * FROM flow_runs WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 500",
                 (f"-{days} days",))
    total = len(runs)
    ok = sum(1 for r in runs if r.get("status") == "completed")
    err = sum(1 for r in runs if r.get("status") == "partial")
    avg_ms = int(sum(r.get("total_latency_ms") or 0 for r in runs) / total) if total else 0
    # ── LLM 用量聚合（近 N 天）──
    usage = _rows(conn, "SELECT * FROM llm_usage_stats WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1000",
                  (f"-{days} days",))
    mock_n = sum(1 for u in usage if u.get("used_mock"))
    total_tok = sum((u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0) for u in usage)
    cost = round(sum(u.get("estimated_cost") or 0 for u in usage), 6)
    # ── 系统级（真实进程信息 + DB 文件大小）──
    proc = {"pid": os.getpid(), "uptime_s": int(time.time() - _PROC_START), "db_size_kb": 0}
    try:
        proc["db_size_kb"] = _db_size_kb(conn)
    except Exception:
        pass
    active_subs = _rows(conn, "SELECT COUNT(*) c FROM flow_event_subscriptions WHERE status='active'")[0]["c"]
    open_alerts = _rows(conn, "SELECT COUNT(*) c FROM alert_events WHERE status='open'")[0]["c"]
    # ── 近 N 天趋势（按天分组：运行成功/失败/平均耗时 + LLM 调用/Token）──
    trend = _build_trend(conn, days)
    # ── 节点类型耗时分布 ──
    node_stats = _rows(conn, """SELECT node_type, COUNT(*) count, AVG(latency_ms) avg_ms,
        SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors
        FROM flow_run_steps GROUP BY node_type ORDER BY count DESC LIMIT 10""")
    # ── 最近告警（未确认优先）──
    recent_alerts = _rows(conn, """SELECT * FROM alert_events
        ORDER BY (status='open') DESC, id DESC LIMIT 8""")
    # ── P0-a（2026-10-02）：LLM 调用健康度 —— 各 intent 截断率 / 推理占比 / 配置契约 ──
    # 口径唯一真源在 `core/llm_health.py`；此处**只透传，不重算**（禁止第二份 SQL）。
    try:
        from core.llm_health import health_report
        llm_health = health_report(conn, days=days)
    except Exception as e:                      # 看板不能因巡检失败而整体 500
        llm_health = {"status": "unknown", "error": str(e)[:200], "intents": [],
                      "summary": {}, "contract": {"ok": True, "violations": [], "checked": 0}}
    return {
        "days": days,
        "runs": {"total": total, "success": ok, "failed": err, "success_rate": round(ok * 100 / total, 1) if total else 0,
                 "avg_latency_ms": avg_ms},
        "llm": {"calls": len(usage), "mock_rate": round(mock_n * 100 / len(usage), 1) if usage else 0,
                "total_tokens": total_tok, "estimated_cost": cost},
        "llm_health": llm_health,
        "system": proc,
        "subs_active": active_subs, "open_alerts": open_alerts,
        "trend": trend,
        "node_stats": [{"type": n["node_type"] or "?", "count": n["count"], "avg_ms": int(n["avg_ms"] or 0),
                        "errors": n["errors"] or 0} for n in node_stats],
        "recent_alerts": recent_alerts,
    }


def _db_size_kb(conn) -> int:
    """真实 DB 文件大小（KB）。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = row["file"]
        if path and os.path.exists(path):
            return int(os.path.getsize(path) / 1024)
    except Exception:
        pass
    return 0


def _build_trend(conn, days: int) -> list:
    """近 N 天按天分组趋势（补齐无数据日期为 0）。"""
    days = max(days, 1)
    run_rows = _rows(conn, """SELECT substr(created_at,1,10) d,
        COUNT(*) c, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) ok,
        SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END) fail,
        AVG(total_latency_ms) avg_ms
        FROM flow_runs WHERE created_at >= datetime('now', ?)
        GROUP BY d ORDER BY d""", (f"-{days} days",))
    usage_rows = _rows(conn, """SELECT substr(created_at,1,10) d, COUNT(*) c,
        SUM(prompt_tokens + completion_tokens) tok
        FROM llm_usage_stats WHERE created_at >= datetime('now', ?)
        GROUP BY d ORDER BY d""", (f"-{days} days",))
    run_by, use_by = {}, {}
    for r in run_rows:
        run_by[r["d"]] = r
    for u in usage_rows:
        use_by[u["d"]] = u
    out = []
    # SQLite CURRENT_TIMESTAMP 为 UTC，用本地日期序列对齐（近似）
    import datetime
    base = datetime.datetime.now()
    for i in range(days - 1, -1, -1):
        d = (base - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        r = run_by.get(d, {})
        u = use_by.get(d, {})
        out.append({
            "date": d,
            "runs": r.get("c") or 0, "ok": r.get("ok") or 0, "fail": r.get("fail") or 0,
            "avg_ms": int(r.get("avg_ms") or 0),
            "llm_calls": u.get("c") or 0, "llm_tokens": u.get("tok") or 0,
        })
    return out


# ── 运行日志检索（节点级执行日志）──
@router.get("/api/monitor/logs")
def monitor_logs(run_id: int = 0, node_type: str = "", status: str = "", q: str = "",
                 limit: int = 50, conn=Depends(db_session)):
    conds, args = [], []
    if run_id:
        conds.append("s.run_id=?"); args.append(run_id)
    if node_type:
        conds.append("s.node_type=?"); args.append(node_type)
    if status:
        conds.append("s.status=?"); args.append(status)
    if q:
        conds.append("(s.content LIKE ? OR s.node_label LIKE ? OR s.node_id LIKE ?)")
        args += [f"%{q}%"] * 3
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    limit = min(max(limit, 1), 200)
    rows = _rows(conn, f"""SELECT s.*, r.flow_name, r.status run_status FROM flow_run_steps s
        LEFT JOIN flow_runs r ON r.id=s.run_id {where}
        ORDER BY s.id DESC LIMIT ?""", args + [limit])
    return {"total": len(rows), "limit": limit, "logs": rows}


# ── 告警事件：列表 + 确认 ──
@router.get("/api/monitor/alerts")
def monitor_alerts(status: str = "", level: str = "", limit: int = 30, conn=Depends(db_session)):
    conds, args = [], []
    if status:
        conds.append("status=?"); args.append(status)
    if level:
        conds.append("level=?"); args.append(level)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    limit = min(max(limit, 1), 200)
    rows = _rows(conn, f"SELECT * FROM alert_events {where} ORDER BY id DESC LIMIT ?", args + [limit])
    open_n = _rows(conn, "SELECT COUNT(*) c FROM alert_events WHERE status='open'")[0]["c"]
    return {"total_open": open_n, "alerts": rows}


@router.post("/api/monitor/alerts/{aid}/ack")
def ack_alert(aid: int, conn=Depends(db_session)):
    conn.execute("UPDATE alert_events SET status='acked' WHERE id=?", (aid,))
    conn.commit()
    return {"ok": True, "id": aid}


# ── 告警规则 CRUD + 启停 ──
@router.get("/api/monitor/alert-rules")
def list_alert_rules(conn=Depends(db_session)):
    return _rows(conn, "SELECT * FROM alert_rules ORDER BY id")


@router.post("/api/monitor/alert-rules")
def create_alert_rule(r: AlertRuleIn, conn=Depends(db_session)):
    if r.metric not in ("success_rate", "avg_latency", "error_count", "mock_rate"):
        return JSONResponse({"error": f"不支持的指标: {r.metric}"}, 400)
    if r.operator not in (">", ">=", "<", "<="):
        return JSONResponse({"error": f"不支持的运算符: {r.operator}"}, 400)
    cur = conn.execute(
        "INSERT INTO alert_rules (name, metric, operator, threshold, level, notify_url, secret, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (r.name, r.metric, r.operator, r.threshold, r.level, r.notify_url, r.secret, r.status))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@router.put("/api/monitor/alert-rules/{rid}")
def update_alert_rule(rid: int, r: AlertRuleIn, conn=Depends(db_session)):
    row = conn.execute("SELECT id FROM alert_rules WHERE id=?", (rid,)).fetchone()
    if not row:
        return JSONResponse({"error": "告警规则不存在"}, 404)
    conn.execute(
        "UPDATE alert_rules SET name=?, metric=?, operator=?, threshold=?, level=?, "
        "notify_url=?, secret=?, status=?, updated_at=datetime('now') WHERE id=?",
        (r.name, r.metric, r.operator, r.threshold, r.level, r.notify_url, r.secret, r.status, rid))
    conn.commit()
    return {"ok": True, "id": rid}


@router.delete("/api/monitor/alert-rules/{rid}")
def delete_alert_rule(rid: int, conn=Depends(db_session)):
    conn.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
    conn.commit()
    return {"ok": True, "id": rid}


@router.post("/api/monitor/alert-rules/{rid}/toggle")
def toggle_alert_rule(rid: int, conn=Depends(db_session)):
    row = conn.execute("SELECT status FROM alert_rules WHERE id=?", (rid,)).fetchone()
    if not row:
        return JSONResponse({"error": "告警规则不存在"}, 404)
    new = "paused" if row["status"] == "active" else "active"
    conn.execute("UPDATE alert_rules SET status=?, updated_at=datetime('now') WHERE id=?", (new, rid))
    conn.commit()
    return {"ok": True, "id": rid, "status": new}


_PROC_START = time.time()
