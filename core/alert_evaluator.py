# -*- coding: utf-8 -*-
"""P0-d（2026-10-03《运行监控与审计日志差距评估》）：周期性告警评估器。

补齐的缺口：`workflows/persistence._evaluate_alerts` **只在编排 run 结束时**评估，
且只认 4 个 run 级指标。这意味着：
- 非编排路径（占绝大多数真实流量：会话直答、知识问答、意图识别）的 LLM 调用、失败、
  成本突变、延迟劣化 —— **没有任何周期性守护**；
- 「LLM 账单半夜翻倍」这类事故在现状下无人知晓（只有等到下一个 run 结束才可能撞上
  mock_rate 这种全局指标，属于偶然覆盖）。

本模块做的：读同一张 alert_rules 表，评估**全局/时间窗指标**，写同一张 alert_events。
与 run 级评估器的分工靠 **metric 白名单隔离**（两套集合零重叠），因此不改动任何既有行为。

  run 级   success_rate | avg_latency | error_count | mock_rate      （run 结束时评估）
  全局级   llm_* | cost_* | auth_* | audit_*                          （周期评估，本文件）
"""
import json
import threading
import time
from datetime import datetime

# 全局专有指标（与 run 级四个 metric 零重叠 —— 保证两套评估器互不重复告警）
GLOBAL_METRICS = (
    "llm_mock_rate_1h",        # 近 1h Mock 降级率（%）：provider 不可用/未配置的直接信号
    "llm_fallback_rate_1h",    # 近 1h provider 回退率（%）
    "llm_trunc_rate_1h",       # 近 1h 输出被截断率（%）：finish_reason='length'
    "llm_p95_latency_1h",      # 近 1h 调用延迟 P95（ms）—— 均值会被长尾掩盖
    "llm_calls_1h",            # 近 1h LLM 调用次数（突增)/骤降为 0 都值得看
    "cost_1h_usd",             # 近 1h 估算成本（美元）
    "cost_today_vs_yday",      # 今日累计 / 昨日同期(同一时刻前的工作量) —— 成本突增倍数
    "auth_failed_1h",          # 近 1h 登录失败次数（P0-a 已有认证审计，这里消费它）
    "audit_blocked_1h",        # 近 1h 被权限门拦下的操作次数
    "audit_chain_ok",          # 审计链完整性：1=完好，0=有损（P0-b 的 verify_chain 结果）
)

_P95_MIN_SAMPLES = 5     # 样本不足不出分位数（避免用 3 个样本算 p95 误导告警）


def collect_global_metrics(conn) -> dict:
    """计算全局时间窗指标（纯查询，不写库）。缺表/缺列 → 该指标取安全缺省值。"""
    m = {k: 0.0 for k in GLOBAL_METRICS}
    m["audit_chain_ok"] = 1.0     # 缺省视为完好：链路尚不可查时不制造假告警

    def _one(sql, args=()):
        try:
            row = conn.execute(sql, args).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    # ── LLM 用量窗口（近 1h）──
    base = ("SELECT COUNT(*) c, COALESCE(SUM(used_mock),0) mock, "
            "COALESCE(SUM(fallback_used),0) fb, "
            "COALESCE(SUM(CASE WHEN finish_reason='length' THEN 1 ELSE 0 END),0) trunc, "
            "COALESCE(SUM(estimated_cost),0) cost FROM llm_usage_stats WHERE created_at >= datetime('now','-1 hour')")
    try:
        r = conn.execute(base).fetchone()
        c = int(r["c"] or 0)
        m["llm_calls_1h"] = float(c)
        if c:
            m["llm_mock_rate_1h"] = round((r["mock"] or 0) * 100.0 / c, 1)
            m["llm_fallback_rate_1h"] = round((r["fb"] or 0) * 100.0 / c, 1)
            m["llm_trunc_rate_1h"] = round((r["trunc"] or 0) * 100.0 / c, 1)
        m["cost_1h_usd"] = round(float(r["cost"] or 0), 6)
    except Exception:
        pass

    # ── 延迟 P95（近 1h）：内存排序，1h 量级记录数可接受；样本不足不出值 ──
    try:
        rows = conn.execute(
            "SELECT latency_ms FROM llm_usage_stats WHERE created_at >= datetime('now','-1 hour') "
            "AND latency_ms > 0 ORDER BY latency_ms").fetchall()
        vals = [int(x[0]) for x in rows]
        if len(vals) >= _P95_MIN_SAMPLES:
            idx = max(0, int(round(len(vals) * 0.95)) - 1)
            m["llm_p95_latency_1h"] = float(vals[idx])
    except Exception:
        pass

    # ── 成本突增：今日累计 vs 昨日同一时刻前（同口径比较，避免"刚过零点就暴涨"的伪阳性）──
    try:
        today = float(_one("SELECT COALESCE(SUM(estimated_cost),0) FROM llm_usage_stats "
                           "WHERE date(created_at)=date('now','localtime')") or 0)
        yday = float(_one("SELECT COALESCE(SUM(estimated_cost),0) FROM llm_usage_stats "
                          "WHERE date(created_at)=date('now','localtime','-1 day') "
                          "AND time(created_at) <= time('now','localtime')") or 0)
        m["cost_today_vs_yday"] = round(today / yday, 2) if yday > 0.000001 else 0.0
    except Exception:
        pass

    # ── 认证失败与被拦截（消费 P0-a 新增的认证审计）──
    m["auth_failed_1h"] = float(_one(
        "SELECT COUNT(*) FROM audit_logs WHERE event_type LIKE 'auth_%' AND result='failed' "
        "AND created_at >= datetime('now','-1 hour')") or 0)
    m["audit_blocked_1h"] = float(_one(
        "SELECT COUNT(*) FROM audit_logs WHERE result='blocked' "
        "AND created_at >= datetime('now','-1 hour')") or 0)

    # ── 审计链完整性（P0-b）：随机抽查最近 3000 行，不全表扫（避免大库拖慢周期任务）──
    try:
        from core.audit import verify_chain
        v = verify_chain(limit=3000, conn=conn)
        m["audit_chain_ok"] = 0.0 if (v.get("broken_count") or 0) else 1.0
    except Exception:
        pass

    return m


def notify_alert_webhook(rule: dict, metric: str, actual: float, threshold: float,
                         op: str, run_id: int = 0) -> bool:
    """A2A alert 事件 webhook（HMAC-SHA256 可选签名）。

    run 级（workflows/persistence）与全局级（本模块）**共用这一份实现** —— 此前只在
    persistence 里有同款逻辑，若另写一份，将来改签名结构/重试策略必漏一处。
    """
    import hashlib
    import hmac as hmac_mod
    import uuid as uuid_mod
    import httpx
    try:
        body = {
            "protocol": "a2a", "version": "0.1", "kind": "event",
            "event": {"id": "alr-" + uuid_mod.uuid4().hex[:12], "type": "alert_triggered",
                      "run_id": run_id, "node_id": "", "node_label": rule.get("name", ""),
                      "node_type": "alert", "scope": "global" if not run_id else "run",
                      "payload": {"rule_id": rule.get("id"), "metric": metric, "actual": actual,
                                   "threshold": threshold, "operator": op,
                                   "level": rule.get("level", "warning")},
                      "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            "sender": {"name": "mbse-monitor", "type": "flow", "role": "alerting"},
        }
        data_str = json.dumps(body, ensure_ascii=False)
        headers = {"Content-Type": "application/json"}
        secret = str(rule.get("secret") or "").strip()
        if secret:
            headers["X-A2A-Signature"] = hmac_mod.new(secret.encode(), data_str.encode(),
                                                      hashlib.sha256).hexdigest()
        resp = httpx.post(rule["notify_url"], content=data_str, headers=headers, timeout=8)
        # ⚠️ 必须校验状态码：httpx 默认不抛 HTTP 错误码 —— 对方返 404/500 若也算"送达"，
        # 调用方会把事件 ack 掉，告警等于投递失败还被记为已处理。
        return int(resp.status_code) < 400
    except Exception:
        return False


def evaluate_once(conn=None, cooldown_min: int = 30, notify: bool = True) -> dict:
    """执行一次全局规则评估。

    命中 → alert_events(status='open', run_id=0, flow_name='(periodic)')。
    同一规则在冷却窗口内不重复落事件 —— 否则每 5 分钟一轮会刷出成百上千条同因告警。
    """
    def _do(c):
        try:
            rules = c.execute("SELECT * FROM alert_rules WHERE status='active'").fetchall()
        except Exception:
            return {"evaluated": 0, "fired": 0, "skipped_cooldown": 0, "metrics": {},
                    "error": "alert_rules 不可用"}
        candidates = [r for r in rules if (r["metric"] or "") in GLOBAL_METRICS]
        if not candidates:
            return {"evaluated": 0, "fired": 0, "skipped_cooldown": 0, "metrics": {}}
        metrics = collect_global_metrics(c)
        fired, cooled = 0, 0
        for r in candidates:
            metric = r["metric"]
            actual = float(metrics.get(metric) or 0)
            th = float(r["threshold"] or 0)
            op = r["operator"] or ">"
            if not {">": actual > th, ">=": actual >= th,
                    "<": actual < th, "<=": actual <= th}.get(op, False):
                continue
            # 冷却去重
            try:
                last = c.execute(
                    "SELECT created_at FROM alert_events WHERE rule_id=? "
                    "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
                if last and last["created_at"]:
                    # ⚠️ 时区：SQLite CURRENT_TIMESTAMP 是 **UTC**，datetime.now() 是**本地时间**
                    # （UTC+8 下相差 8 小时）—— 用 now() 会让冷却窗口恒视为已过期，去重完全失效。
                    _age = (datetime.utcnow() - datetime.strptime(
                        last["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
                    if _age < cooldown_min * 60:
                        cooled += 1
                        continue
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO alert_events (rule_id, rule_name, run_id, flow_name, metric, actual, "
                    "threshold, operator, level, status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
                    (r["id"], r["name"], 0, "(periodic)", metric, actual, th, op,
                     r["level"] or "warning"))
                c.execute("UPDATE alert_rules SET last_fired_at=datetime('now') WHERE id=?", (r["id"],))
                c.commit()
                fired += 1
            except Exception:
                pass
            if notify and (r["notify_url"] or "").strip():
                if notify_alert_webhook(dict(r), metric, actual, th, op, run_id=0):
                    try:
                        c.execute(
                            "UPDATE alert_events SET status='acked' WHERE rule_id=? AND run_id=0 "
                            "AND status='open'", (r["id"],))
                        c.commit()
                    except Exception:
                        pass
        return {"evaluated": len(candidates), "fired": fired, "skipped_cooldown": cooled,
                "metrics": metrics}

    from database import db_conn
    if conn is not None:
        return _do(conn)
    with db_conn() as c:
        return _do(c)


_loop_thread = None
_loop_stop = threading.Event()


def start_alert_loop(interval_sec: float = 300, cooldown_min: int = 30) -> bool:
    """启动后台周期评估（幂等：已启动则忽略）。线程 daemon=True，不阻塞退出。"""
    global _loop_thread
    if _loop_thread and _loop_thread.is_alive():
        return False

    def _run():
        while not _loop_stop.wait(interval_sec):
            try:
                res = evaluate_once(cooldown_min=cooldown_min)
                if res.get("fired"):
                    print("[alert-loop] 周期评估触发 %d 条告警: %s"
                          % (res["fired"], {k: v for k, v in res.get("metrics", {}).items()
                                            if v}), flush=True)
            except Exception as e:
                print("[alert-loop] 评估异常（不中断）: %s" % str(e)[:160], flush=True)

    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_run, daemon=True, name="alert-evaluator")
    _loop_thread.start()
    print("[startup] 周期告警评估器已启动（每 %ss，冷却 %smin）" % (interval_sec, cooldown_min),
          flush=True)
    return True


def stop_alert_loop() -> None:
    _loop_stop.set()
