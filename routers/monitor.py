"""统一监控平台（D12，8.2「监控与日志面板」+「监控面板和性能分析工具」）。

能力域：/api/monitor/*
- dashboard：统一仪表盘（系统/运行/LLM 真实指标 + 近 N 天趋势 + 节点耗时 + 最近告警）
- logs：运行日志检索（run_id / 节点类型 / 状态 / 关键词，分页）
- alerts：告警事件列表 + 确认
- alert-rules：告警规则 CRUD + 启停
- lifecycle：资产长尾治理建议（P0-1，四档 verdict，只建议不自动执行）
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


# ── P0-4（2026-10-04）：多租户隔离开启前的**数据体检**──
@router.get("/api/monitor/tenant-readiness")
def tenant_readiness(conn=Depends(db_session)):
    """回答一个问题：**现在能不能安全打开 `rag.tenant_isolation`？** 打开会漏掉什么？

    背景（实测 2026-10-04）：`entities.project_id` 189 行全是 `project-satnet-broadband`，
    而 `projects` 表里没有该项目；`conversations.project_id` 12/15 为空。
    此刻打开严格过滤，检索会从"189 条可召回"变成"0 条"——所以必须先体检再开。

    口径说明：`orphan_project` = project_id 非空但在 projects 表里查无此项目（挂错了/项目已删）。
    这是**开隔离前必须先修**的一类；`blank` = 没填，隔离下必然不可见。
    """
    def _cfg_get(group, key, default=None):
        try:
            from core import config as _c
            return _c.get(group, key, default)
        except Exception:
            return default

    def _cnt(sql, args=()):
        try:
            r = conn.execute(sql, args).fetchone()
            return int(r[0]) if r and r[0] is not None else 0
        except Exception:
            return -1        # -1 = 表/列缺失（老库），与"0 条"区分开

    ent_total = _cnt("SELECT COUNT(*) FROM entities")
    rel_total = _cnt("SELECT COUNT(*) FROM relations")
    ent_blank = _cnt("SELECT COUNT(*) FROM entities WHERE COALESCE(project_id,'')=''")
    rel_blank = _cnt("SELECT COUNT(*) FROM relations WHERE COALESCE(project_id,'')=''")
    ent_orphan = _cnt("SELECT COUNT(*) FROM entities e WHERE COALESCE(e.project_id,'')<>'' "
                      "AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=e.project_id)")
    rel_orphan = _cnt("SELECT COUNT(*) FROM relations r WHERE COALESCE(r.project_id,'')<>'' "
                      "AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=r.project_id)")
    conv_blank = _cnt("SELECT COUNT(*) FROM conversations WHERE COALESCE(project_id,'')=''")
    projects = []
    try:
        projects = [dict(r) for r in conn.execute(
            "SELECT id, name, code FROM projects ORDER BY id LIMIT 50").fetchall()]
    except Exception:
        pass
    blockers = []
    if ent_orphan > 0:
        blockers.append(f"{ent_orphan} 个实体挂在不存在的项目上（打开隔离后将检索不到）")
    if ent_blank > 0:
        blockers.append(f"{ent_blank} 个实体没有 project_id（打开隔离后将检索不到）")
    if rel_orphan > 0:
        blockers.append(f"{rel_orphan} 条关系挂在不存在的项目上")
    if not projects:
        blockers.append("projects 表为空：没有任何合法项目可作为过滤目标")
    return {
        "ok": not blockers,
        "mode": _cfg_get("rag", "tenant_isolation", "off"),        "projects": projects,
        "entities": {"total": ent_total, "blank": ent_blank, "orphan_project": ent_orphan},
        "relations": {"total": rel_total, "blank": rel_blank, "orphan_project": rel_orphan},
        "conversations_blank_project": conv_blank,
        "blockers": blockers,
        "next_step": ("先按 blockers 回填 project_id，再置 rag.tenant_isolation=on"
                      if blockers else "数据已就绪，可置 rag.tenant_isolation=on"),
        "known_gap": ("文档/分片路径无 project_id 维度（documents 表无该列），"
                      "向量兜底结果目前仍跨项目 —— 见图谱路径隔离 ≠ 全链路隔离"),
    }



@router.get("/api/monitor/alert-metrics")
def alert_metrics(conn=Depends(db_session)):
    """可用告警指标清单。**标注每个指标由谁评估** —— 此前只有 run 级四个，
    而它们只有在编排 run 结束时才会被评估；非编排流量（会话直答等绝大多数真实请求）
    长期处于无告警守护状态，这是评估报告的 P0-d。
    """
    from core.alert_evaluator import GLOBAL_METRICS
    return {
        "run_scope": [{"key": "success_rate", "desc": "run 级成功率（%）", "evaluated_at": "run 结束"},
                      {"key": "avg_latency", "desc": "run 总耗时（ms）", "evaluated_at": "run 结束"},
                      {"key": "error_count", "desc": "run 级错误数", "evaluated_at": "run 结束"},
                      {"key": "mock_rate", "desc": "近 24h Mock 降级率（%）", "evaluated_at": "run 结束"}],
        "global_scope": [{"key": k, "evaluated_at": "周期评估（每 5 分钟）"} for k in GLOBAL_METRICS],
    }


@router.post("/api/monitor/alerts/evaluate")
def evaluate_alerts_now(cooldown_min: int = 30, conn=Depends(db_session)):
    """立即执行一次全局规则评估（不等下一个周期；验证规则配得对不对就用它）。"""
    from core.alert_evaluator import evaluate_once
    res = evaluate_once(conn=conn, cooldown_min=max(0, int(cooldown_min or 0)))
    return {"ok": True, **res}


# ── P0-2（2026-10-03 整改）：编排运行 liveness —— 孤儿回收观测与控制 ──
@router.get("/api/monitor/orphan-runs")
def orphan_runs(limit: int = 20, conn=Depends(db_session)):
    """非终态批次一览 + 孤儿判定（`core/run_registry`）。

    为什么必须有这个端点：孤儿此前**完全不可见** —— 评估 §4.2 实测 60 个批次里 16 个
    残留在 planned/ready/blocked 长达 3~4 天，没有任何一个界面能显示"有任务卡住了"。
    看不见就不可能被治理，所以这里同时给出：
      · `active`：所有非终态批次（含未超时的正常批），让运维判断 TTL 设得合不合理；
      · `summary`：孤儿数 / 涉及批次数 / 最老年龄 / 当前 TTL。
    """
    from core.run_registry import active_runs, orphan_summary
    return {"ok": True, "active": active_runs(conn, limit=limit), **orphan_summary(conn)}


@router.post("/api/monitor/orphan-runs/reap")
def reap_now(dry_run: bool = False, conn=Depends(db_session)):
    """立即执行一次孤儿回收（不等下次启动；灰度时先用 `dry_run=true` 看会被收哪些）。

    为什么提供 dry_run：回收会把任务改成 failed，是不可逆语义（虽然上下文保留）。
    上线前后必须先跑一次 dry_run 确认没有误伤正在长跑的批次 —— 这是 TTL 配错的
    **唯一**止损点，没有它就只能靠改配置再重启。
    """
    from core.run_registry import reap_orphans
    return {"ok": True, **reap_orphans(conn, dry_run=bool(dry_run))}


# ── P0-2b（2026-10-04 整改）：编排自动重投守护的观测与手动触发 ──
@router.get("/api/monitor/orchestration-supervisor")
def supervisor_status(dry_run: bool = True, max_runs: int = 3):
    """守护状态 + 「若现在扫一轮会投哪些批次」（预演，不真的触发）。

    `dry_run=true`（**默认**）只走判定与计数，**不重排队、不执行**——
    这是上线前确认"这个开关打开后会不会误投正在跑的批次"的唯一手段
    （最大风险是 stale 阈值配得比单轮编排还短 ⇒ 把活着的批次误判为死）。
    """
    from core.orch_supervisor import supervisor_status as _st, scan_once
    from core.config import get as _cfg_get
    out = {"ok": True, **_st(), "dry_run": bool(dry_run)}
    if dry_run:
        try:
            from database import db_conn
            with db_conn() as c:
                # max_runs=0 = 预演：走完判定与分类，但一个都不触发
                out["preview"] = scan_once(c, max_runs=0,
                                           stale_s=_cfg_get("orchestration", "resume_stale_s", 1800))
        except Exception as e:
            out["preview_error"] = str(e)[:200]
    return out


@router.post("/api/monitor/orchestration-supervisor/scan")
def supervisor_scan(max_runs: int = 1, dry_run: bool = True):
    """立即扫一轮。`dry_run=false` 会**真的重排队并触发执行**（等价于人工点继续）。

    保留 dry_run 是因为这会调 LLM：与 `reap_orphans` 同款理由 —— 保护性操作
    必须能先看清影响面。
    """
    from core.orch_supervisor import scan_once
    #⚠️ dry_run 必须映射成 max_runs=0（"预演"），**绝不能兜成 1** ——
    #   scan_once 的 max_runs=0 是"走判定不触发"，而传 1 会真的 apply_resume
    #   并起线程调 LLM。一个"预览"按钮若默认触发真执行，是最危险的一类默认值。
    return {"ok": True, **scan_once(max_runs=(0 if dry_run else max(1, int(max_runs or 1))),
                                    stale_s=None)}


# ── 告警规则 CRUD + 启停 ──
@router.get("/api/monitor/alert-rules")
def list_alert_rules(conn=Depends(db_session)):
    return _rows(conn, "SELECT * FROM alert_rules ORDER BY id")


@router.post("/api/monitor/alert-rules")
def create_alert_rule(r: AlertRuleIn, conn=Depends(db_session)):
    # P0-d：白名单加入周期评估器的全局指标（run 级四个 + GLOBAL_METRICS）
    from core.alert_evaluator import GLOBAL_METRICS
    allowed = ("success_rate", "avg_latency", "error_count", "mock_rate") + GLOBAL_METRICS
    if r.metric not in allowed:
        return JSONResponse({"error": f"不支持的指标: {r.metric}（可选: {', '.join(allowed)}）"}, 400)
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
    from core.alert_evaluator import GLOBAL_METRICS
    if r.metric not in ("success_rate", "avg_latency", "error_count", "mock_rate") + GLOBAL_METRICS:
        return JSONResponse({"error": f"不支持的指标: {r.metric}"}, 400)
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


# ═══════════════════════════════════════════════════════════════════════════
# 会话级运行监控（2026-10-18《运行监控选型建议 v2》实施）
#
# 目标：给定任一 conversation_id，**一次调用**拿到该会话的完整执行证据链
# （编排 run / 子任务 / LLM 调用 / 工作流 run / HIL / 事件流），并给出错误画像；
# 排查入口从"人工查 5 张表"收敛为"两次点击"。
#
# 口径纪律：只读聚合，本域是会话级口径的唯一真源（与 metrics_core.py 同纪律）；
# 各分段独立 try/except —— 单表缺列/缺表只降级该段，不整体 500（同 dashboard 的守卫思路）。
# ═══════════════════════════════════════════════════════════════════════════

def _table_cols(conn, table: str) -> set:
    """表现有列集合（缺表 → 空集）。老库列差异按此自适应，缺列指标回退 0。"""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _sess_rows(conn, sql: str, args=(), clip: int = 0) -> list:
    """防御式查询：异常返回 []（单段降级不拖垮整体）。clip>0 时截断超长文本字段。"""
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []
    if clip:
        for r in rows:
            for k, v in r.items():
                if isinstance(v, str) and len(v) > clip:
                    r[k] = v[:clip] + "…"
    return rows


def _session_weight(s: dict) -> int:
    """问题信号加权（越大越优先展示）：失败调用/任务/运行 > 截断/回退 > 重试/HIL。"""
    return (int(s.get("llm_failed") or 0) * 5 + int(s.get("failed_tasks") or 0) * 4
            + int(s.get("failed_runs") or 0) * 4 + int(s.get("llm_trunc") or 0) * 2
            + int(s.get("llm_fallback") or 0) * 2 + int(s.get("llm_retries") or 0)
            + int(s.get("hil") or 0))


@router.get("/api/monitor/sessions")
def monitor_sessions(days: int = 7, limit: int = 30, q: str = "", conn=Depends(db_session)):
    """异常会话列表：按问题信号加权排序（有问题的排前，正常会话随后按活跃度排）。

    数据全部来自既有表（llm_usage_stats / agent_tasks / orch_checkpoints / hil_confirmations），
    不新造指标 —— 各列口径与 dashboard / llm_health 保持同一份 SQL 语义。
    """
    days = max(min(days, 90), 1)
    limit = min(max(limit, 1), 100)
    lus = _table_cols(conn, "llm_usage_stats")
    # 老库列自适应：call_failed / finish_reason / fallback_used / retry_count 均为后补列
    failed_e = "SUM(call_failed)" if "call_failed" in lus else "0"
    trunc_e = "SUM(CASE WHEN finish_reason='length' THEN 1 ELSE 0 END)" if "finish_reason" in lus else "0"
    fb_e = "SUM(fallback_used)" if "fallback_used" in lus else "0"
    retry_e = "SUM(retry_count)" if "retry_count" in lus else "0"
    mock_e = "SUM(used_mock)" if "used_mock" in lus else "0"
    lls = _sess_rows(conn, f"""
        SELECT conversation_id cid, COUNT(*) calls, {failed_e} failed, {trunc_e} trunc,
               {fb_e} fallback, {retry_e} retries, {mock_e} mock,
               ROUND(COALESCE(SUM(estimated_cost),0),6) cost,
               COALESCE(MAX(latency_ms),0) max_ms, MAX(created_at) last_at
        FROM llm_usage_stats WHERE conversation_id>0 AND created_at >= datetime('now', ?)
        GROUP BY conversation_id""", (f"-{days} days",))
    tasks = _sess_rows(conn, """
        SELECT conversation_id cid, COUNT(*) total_tasks,
               SUM(CASE WHEN status IN ('failed','error') THEN 1 ELSE 0 END) failed_tasks
        FROM agent_tasks GROUP BY conversation_id""")
    runs = _sess_rows(conn, """
        SELECT conversation_id cid, COUNT(*) runs,
               SUM(CASE WHEN phase='failed' THEN 1 ELSE 0 END) failed_runs,
               SUM(COALESCE(attempt_count,0)) resumes
        FROM orch_checkpoints GROUP BY conversation_id""")
    hils = _sess_rows(conn, """
        SELECT conversation_id cid, COUNT(*) hil FROM hil_confirmations
        GROUP BY conversation_id""")
    by = lambda rows: {r["cid"]: r for r in rows}
    lb, tb, ob, hb = by(lls), by(tasks), by(runs), by(hils)
    # 候选会话 = 近 N 天有 LLM 流量 或 有编排 run 的会话（纯闲聊无监控数据的按活跃度补充）
    conds, args = ["c.updated_at >= datetime('now', ?)"], [f"-{days} days"]
    if q:
        conds.append("(c.title LIKE ? OR c.current_intent LIKE ? OR CAST(c.id AS TEXT)=?)")
        args += [f"%{q}%", f"%{q}%", q]
    convs = _sess_rows(conn, f"""SELECT c.id, c.title, c.intent, c.status, c.phase, c.main_agent,
        c.updated_at, c.created_at FROM conversations c WHERE {' AND '.join(conds)}
        ORDER BY c.updated_at DESC LIMIT 500""", args)
    out = []
    for c in convs:
        cid = c["id"]
        l, t, o, h = lb.get(cid, {}), tb.get(cid, {}), ob.get(cid, {}), hb.get(cid, {})
        s = {"conversation_id": cid, "title": c.get("title") or "", "intent": c.get("current_intent") or c.get("intent") or "",
             "status": c.get("status") or "", "main_agent": c.get("main_agent") or "",
             "updated_at": c.get("updated_at") or "",
             "llm_calls": l.get("calls") or 0, "llm_failed": l.get("failed") or 0,
             "llm_trunc": l.get("trunc") or 0, "llm_fallback": l.get("fallback") or 0,
             "llm_retries": l.get("retries") or 0, "llm_mock": l.get("mock") or 0,
             "llm_cost": l.get("cost") or 0, "llm_max_ms": l.get("max_ms") or 0,
             "llm_last_at": l.get("last_at") or "",
             "total_tasks": t.get("total_tasks") or 0, "failed_tasks": t.get("failed_tasks") or 0,
             "runs": o.get("runs") or 0, "failed_runs": o.get("failed_runs") or 0,
             "resumes": o.get("resumes") or 0, "hil": h.get("hil") or 0}
        s["weight"] = _session_weight(s)
        out.append(s)
    # 排序：问题权重高的绝对优先；同权重按最近活跃（updated_at 为 'YYYY-MM-DD HH:MM:SS' 可字典序比较）
    out.sort(key=lambda s: (s["weight"], s["updated_at"] or ""), reverse=True)
    flagged = sum(1 for s in out if s["weight"] > 0)
    return {"ok": True, "days": days, "total": len(out), "flagged": flagged,
            "sessions": out[:limit]}


@router.get("/api/monitor/session/{conversation_id}")
def monitor_session_detail(conversation_id: int, conn=Depends(db_session)):
    """会话执行证据链：概览 + 时间线分段 + 错误画像。全部只读，单段降级。"""
    cid = int(conversation_id)
    out = {"ok": True, "conversation_id": cid}
    conv = _sess_rows(conn, "SELECT * FROM conversations WHERE id=?", (cid,), clip=200)
    out["conversation"] = conv[0] if conv else None
    if not conv:
        out["ok"] = False
        out["error"] = "会话不存在"
        return out
    run_rows = _sess_rows(conn, """SELECT run_id, phase, attempt_count, intent, team, skill_name,
        provider_id, created_at, updated_at FROM orch_checkpoints
        WHERE conversation_id=? ORDER BY run_id DESC LIMIT 50""", (cid,))
    out["orch_runs"] = run_rows
    out["agent_tasks"] = _sess_rows(conn, """SELECT * FROM agent_tasks WHERE conversation_id=?
        ORDER BY id DESC LIMIT 100""", (cid,), clip=240)
    lus = _table_cols(conn, "llm_usage_stats")
    extra = []
    for col, alias in (("call_failed", "call_failed"), ("failure_kind", "failure_kind"),
                       ("failure_msg", "failure_msg"), ("finish_reason", "finish_reason"),
                       ("retry_count", "retry_count"), ("fallback_used", "fallback_used"),
                       ("fallback_provider_id", "fallback_provider_id"),
                       ("sub_task_key", "sub_task_key")):
        if col in lus:
            extra.append(col)
    out["llm_calls"] = _sess_rows(conn, f"""SELECT id, created_at, provider_name, model_name, intent,
        used_mock, prompt_tokens, completion_tokens, total_tokens, estimated_cost, latency_ms,
        trace_id, run_id{',' + ','.join(extra) if extra else ''}
        FROM llm_usage_stats WHERE conversation_id=? ORDER BY id DESC LIMIT 200""",
        (cid,), clip=200)
    out["llm_calls"].reverse()
    out["messages"] = _sess_rows(conn, """SELECT id, role, msg_type, feedback, created_at,
        substr(content,1,200) content FROM messages WHERE conversation_id=?
        ORDER BY id DESC LIMIT 100""", (cid,))
    out["messages"].reverse()
    out["stream_events"] = _sess_rows(conn, """SELECT * FROM orch_stream_events
        WHERE conversation_id=? ORDER BY id DESC LIMIT 100""", (cid,), clip=200)
    out["stream_events"].reverse()
    fr_cols = _table_cols(conn, "flow_runs")
    out["flow_runs"] = _sess_rows(conn, """SELECT id, flow_name, status, total_latency_ms,
        error_count, created_at FROM flow_runs WHERE conversation_id=?
        ORDER BY id DESC LIMIT 20""", (cid,)) if "conversation_id" in fr_cols else []
    fr_ids = [r["id"] for r in out["flow_runs"]]
    out["flow_steps"] = (_sess_rows(conn, f"""SELECT run_id, seq, node_id, node_type, node_label,
        status, latency_ms, substr(content,1,160) content FROM flow_run_steps
        WHERE run_id IN ({','.join('?' * len(fr_ids))}) ORDER BY run_id, seq""",
        fr_ids, clip=200) if fr_ids else [])
    out["hil"] = _sess_rows(conn, """SELECT id, agent_id, run_id, action, status, created_at
        FROM hil_confirmations WHERE conversation_id=? ORDER BY id DESC LIMIT 20""", (cid,))
    # ── 错误画像：一次看全该会话的问题信号（排查首屏直接给结论）──
    llm = out["llm_calls"]
    def _n(key): return sum(1 for x in llm if x.get(key))
    profile = {
        "llm_calls": len(llm),
        "llm_failed": _n("call_failed"),
        "llm_trunc": sum(1 for x in llm if x.get("finish_reason") == "length"),
        "llm_fallback": _n("fallback_used"),
        "llm_retries": sum(int(x.get("retry_count") or 0) for x in llm),
        "llm_mock": _n("used_mock"),
        "failed_tasks": sum(1 for t in out["agent_tasks"] if t.get("status") in ("failed", "error")),
        "failed_runs": sum(1 for r in run_rows if r.get("phase") == "failed"),
        "hil": len(out["hil"]),
        "llm_cost": round(sum(float(x.get("estimated_cost") or 0) for x in llm), 6),
        "llm_max_ms": max([int(x.get("latency_ms") or 0) for x in llm] or [0]),
    }
    profile["weight"] = (profile["llm_failed"] * 5 + profile["failed_tasks"] * 4
                         + profile["failed_runs"] * 4 + profile["llm_trunc"] * 2
                         + profile["llm_fallback"] * 2 + profile["llm_retries"] + profile["hil"])
    out["error_profile"] = profile
    return out


@router.get("/api/monitor/lifecycle")
def monitor_lifecycle(kind: str = "all", verdict: str = "", limit: int = 200,
                      conn=Depends(db_session)):
    """资产长尾治理（P0-1）：agents/skills 的 keep/watch/review/retire_candidate 四档建议。

    判据全部走 core.config 的 lifecycle 组（模块不写死阈值），只建议不自动执行。
    kind=all 或逗号分隔种类（agent,skill）；verdict 非空时按档位过滤。
    """
    from core import lifecycle
    kinds = None if kind in ("", "all") else [k.strip() for k in kind.split(",") if k.strip()]
    try:
        res = lifecycle.analyze(conn, kinds=kinds)
    except Exception as e:                      # 看板不能因巡检失败而整体 500（既有哲学）
        return JSONResponse({"ok": False, "error": f"长尾分析失败: {e}"}, 500)
    items = res.get("items") or []
    if verdict:
        items = [it for it in items if it.get("verdict") == verdict]
    res["items"] = items[:limit]
    res["filter"] = {"kind": kind, "verdict": verdict}
    return res


_PROC_START = time.time()
