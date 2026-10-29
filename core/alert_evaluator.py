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

# 告警状态机单一真源（2026-10-29）：未终结状态的取值域不在本模块另写一份
#（此前 'acked' 字面量在本模块与 routers/monitor.py 各写一次，必然漂移）。
from core.alert_state import open_statuses_sql

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
    "badcase_open_count",      # 未处理 Bad Case 数（P0-3：core/badcase 台账，open 状态计）
    # ── P2-1（2026-10-19）：misevolution 安全回归信号（红线 #5 常驻门禁）──
    # 与上面 _1h 指标不同：这五个走 core/misevolution 的**窗口化速率**（窗口 =
    # misevolution.window_days，默认 7 天；值是百分数 0-100，不是次数）。分母
    # 不足 min_denominator 时发布 **None**（不是 0！空窗口不是 0%），评估器
    # 跳过 None——「没数据」不得伪装成「没问题」。除 kind_coverage 是「越高
    # 越好」（规则用 < 判据）外，其余四个「越低越好」（规则用 > 判据）。
    "misev_auth_reject_rate",          # 身份拒绝率（拒答代理口径，P0-5 伪造嫌疑）
    "misev_perm_denied_rate",          # 工具权限拒绝率（RBAC 越权尝试）
    "misev_whitelist_denied_rate",     # 白名单越权尝试率（委派最小权限）
    "misev_tool_fail_rate",            # 工具失败率（健康度）
    "misev_kind_coverage",             # failure_kind 覆盖率（可监控性，红线 #4）
    # ── P0-a（2026-10-24）度量收口：6 个新指标（告警规则体系化）──
    # 口径说明：
    # - llm_fail_rate_1h / llm_fail_1h：近 1h LLM 失败率/次数。**含探测调用**
    #   （conversation_id=0 AND run_id=0，真库 278 条失败中 261 条是探测）——
    #   与 badcase 检测器的 include_probes=False 不同：告警要看全部失败，
    #   探测失败同样是 provider 故障信号。绝对次数 llm_fail_1h 防小样本失真。
    # - orch_stale_runs：非终态 orch_checkpoints 心跳超 30min 的编排数。
    #   口径与 core/orch_supervisor resume_stale_s=1800 对齐（同源不造第二口径）。
    # - hil_pending_old24h：pending 且超 24h 的 HIL 确认单数（真库实测 10/10 全超）。
    # - auth_login_failed_1h：**真·登录失败**（event_type='auth_login' AND failed）。
    #   与既有 auth_failed_1h（auth_% 宽口径，99.8% 是身份拒绝）拆分，规则语义归位。
    # - rag_low_quality_rate_1h：检索低质占比（**代理口径**：top_score<0.1 或 0 命中）。
    #   P1 的 query_trace.rag_quality 列落地后只换取数 SQL，规则行不动。
    # - intent_mismatch_rate：意图误判率（**代理口径**：有命中结论样本中
    #   intent<>hit_intent，近 1 天）。意图评测落库（P0-b）后校准阈值。
    "llm_fail_rate_1h",
    "llm_fail_1h",
    "orch_stale_runs",
    "hil_pending_old24h",
    "auth_login_failed_1h",
    "rag_low_quality_rate_1h",
    "intent_mismatch_rate",
    # ── P0-b（2026-10-25）生产回流三指标（R12/R13/R14 告警供数）──
    # 口径与 core/online_eval.online_eval_metrics 同源（该函数是唯一计算处，
    # 这里只做发布——不造第二套口径）；缺表 ⇒ 0/None，评估器按规则判。
    # - eval_backlog_pending：pending 样本数（R12 积压，阈值见 alert_rules 行）。
    # - eval_promoted_7d：近 7 天 labeled_confirmed 数（R13 回流停滞：=0 且
    #   backlog>0 才告警——零样本期不误报，规则行带组合条件）。
    # - eval_auto_coverage：自动评覆盖率（可评样本中 auto_score 非 NULL 占比，
    #   R14）。**分母为 0 时发布 None 不是 0**（空队列不是 0% 覆盖率）。
    "eval_backlog_pending",
    "eval_promoted_7d",
    "eval_auto_coverage",
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

    # ── Bad Case 台账（P0-3）：未处理坏样本数；缺表（迁移未跑）⇒ 0，不制造假告警 ──
    try:
        from core.badcase import open_badcase_count
        m["badcase_open_count"] = float(open_badcase_count(conn))
    except Exception:
        pass

    # ── P0-a（2026-10-24）度量收口：6 个新指标取数 ──
    # LLM 失败率/次数（近 1h，含探测——探测失败同样是 provider 故障信号）
    try:
        r = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(call_failed),0) f FROM llm_usage_stats "
            "WHERE created_at >= datetime('now','-1 hour')").fetchone()
        c = int(r["c"] or 0)
        m["llm_fail_1h"] = float(r["f"] or 0)
        if c:
            m["llm_fail_rate_1h"] = round((r["f"] or 0) * 100.0 / c, 1)
    except Exception:
        pass
    # 编排停滞：非终态 checkpoint 心跳超 30min（口径同 orch_supervisor.resume_stale_s）
    try:
        m["orch_stale_runs"] = float(_one(
            "SELECT COUNT(*) FROM orch_checkpoints "
            "WHERE phase NOT IN ('done','failed') "
            "AND (julianday('now') - julianday(updated_at))*24*60 > 30") or 0)
    except Exception:
        pass
    # HIL 确认队列积压：pending 且超 24h
    try:
        m["hil_pending_old24h"] = float(_one(
            "SELECT COUNT(*) FROM hil_confirmations "
            "WHERE status='pending' AND (julianday('now') - julianday(created_at))*24 > 24") or 0)
    except Exception:
        pass
    # 真·登录失败（与 auth_failed_1h 的身份拒绝宽口径拆分）
    try:
        m["auth_login_failed_1h"] = float(_one(
            "SELECT COUNT(*) FROM audit_logs "
            "WHERE event_type='auth_login' AND result='failed' "
            "AND created_at >= datetime('now','-1 hour')") or 0)
    except Exception:
        pass
    # 检索低质占比（代理口径，待 rag_quality 列替换）
    try:
        r = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(CASE WHEN top_score < 0.1 OR hit_count = 0 "
            "THEN 1 ELSE 0 END),0) low FROM query_trace "
            "WHERE created_at >= datetime('now','-1 hour')").fetchone()
        c = int(r["c"] or 0)
        if c:
            m["rag_low_quality_rate_1h"] = round((r["low"] or 0) * 100.0 / c, 1)
    except Exception:
        pass
    # 意图误判率（代理口径，近 1 天有命中结论样本）
    try:
        r = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(CASE WHEN intent <> hit_intent THEN 1 ELSE 0 END),0) mm "
            "FROM intent_samples "
            "WHERE COALESCE(NULLIF(TRIM(hit_intent),''),'') <> '' "
            "AND created_at >= datetime('now','-1 day')").fetchone()
        c = int(r["c"] or 0)
        if c:
            m["intent_mismatch_rate"] = round((r["mm"] or 0) * 100.0 / c, 1)
    except Exception:
        pass

    # ── P0-b（2026-10-25）生产回流三指标：委托 core/online_eval 唯一计算处 ──
    try:
        from core.online_eval import online_eval_metrics as _oe_metrics
        _oem = _oe_metrics(conn)
        m["eval_backlog_pending"] = float(_oem.get("eval_backlog_pending") or 0)
        m["eval_promoted_7d"] = float(_oem.get("eval_promoted_7d") or 0)
        _cov = _oem.get("eval_auto_coverage")
        # 分母为 0 ⇒ None（空队列 ≠ 0% 覆盖；评估器跳过 None，见 evaluate_once）
        m["eval_auto_coverage"] = (None if _cov is None
                                   else round(float(_cov) * 100.0, 1))
    except Exception:
        # 缺表/供数失败 ⇒ 覆盖率同样发布 None（0% 会让 R14「< 阈值」假告警；
        # 两个计数指标回落 0 是安全的：R12 是 > 判据、R13 需 backlog>0 组合）
        m["eval_auto_coverage"] = None

    # ── P2-1 misevolution 安全回归信号（红线 #5）─────────────────────────
    # 口径全部委托 core/misevolution（只读引擎，窗口/阈值走其配置组）；本处
    # 只做「速率 → 百分数」的发布换算。**分母不足 ⇒ 发布 None**（评估器跳过，
    # 见 evaluate_once）——把空窗口报成 0% 会让 kind_coverage 的 < 判据假告警。
    try:
        from core import misevolution as _mev
        _mev_settings = _mev.get_settings()
        if _mev_settings.get("enabled"):
            _sig = _mev.collect_signals(conn, settings=_mev_settings)
            for _name, _key in (("misev_auth_reject_rate", "auth_reject_rate"),
                                ("misev_perm_denied_rate", "perm_denied_rate"),
                                ("misev_whitelist_denied_rate", "whitelist_denied_rate"),
                                ("misev_tool_fail_rate", "tool_fail_rate"),
                                ("misev_kind_coverage", "kind_coverage")):
                _rate = (_sig.get(_key) or {}).get("rate")
                m[_name] = (round(_rate * 100.0, 2) if _rate is not None else None)
        else:
            for _name in ("misev_auth_reject_rate", "misev_perm_denied_rate",
                          "misev_whitelist_denied_rate", "misev_tool_fail_rate",
                          "misev_kind_coverage"):
                m[_name] = None      # 监控关停 ⇒ 不发布数（不是 0）
    except Exception:
        for _name in ("misev_auth_reject_rate", "misev_perm_denied_rate",
                      "misev_whitelist_denied_rate", "misev_tool_fail_rate",
                      "misev_kind_coverage"):
            m[_name] = None          # 引擎异常 ⇒ 不发布数，不制造假告警

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
            return {"evaluated": 0, "fired": 0, "skipped_cooldown": 0,
                    "skipped_unknown": 0, "metrics": {}}
        metrics = collect_global_metrics(c)
        fired, cooled, unknown = 0, 0, 0
        for r in candidates:
            metric = r["metric"]
            actual = metrics.get(metric)
            # P2-1：None = 分母不足/监控关停（「没数据」）⇒ 跳过不判。
            # 不能回落 0：kind_coverage 用 < 判据，0 会直接假告警（空窗口不是 0%）。
            if actual is None:
                unknown += 1
                continue
            actual = float(actual)
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
                # ★ 2026-10-29 折叠：同一规则的**未终结**告警累加计数，而非新增行。
                #   此前每次命中都 INSERT ⇒ 同因告警累积 81 次（实测），看板被淹没
                #   ⇒ 告警疲劳（"BadCase 积压"响了 81 次，而那 6 条坏例一条没动）。
                #   折叠后：首次 INSERT（hit_count=1，created_at 保留首次触发时刻），
                #   后续命中 UPDATE（hit_count+1 / last_seen_at / actual 刷新）。
                #   「已终结(fixed/verified)」不参与折叠 ⇒ 问题复发会开**新事件**，
                #   不会被算成旧事件的又一次命中。
                _open_sql = open_statuses_sql()
                _row = c.execute(
                    "SELECT id FROM alert_events WHERE rule_id=? AND status IN (%s) "
                    "ORDER BY id DESC LIMIT 1" % _open_sql, (r["id"],)).fetchone()
                if _row:
                    c.execute(
                        "UPDATE alert_events SET hit_count=COALESCE(hit_count,1)+1, actual=?, "
                        "last_seen_at=datetime('now') WHERE id=?",
                        (actual, int(_row[0])))
                else:
                    c.execute(
                        "INSERT INTO alert_events (rule_id, rule_name, run_id, flow_name, metric, actual, "
                        "threshold, operator, level, status, hit_count, last_seen_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,'open',1,datetime('now'))",
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
                "skipped_unknown": unknown, "metrics": metrics}

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
            # P0-a：能力度量聚合搭车本周期（零新线程）。从 tool_call_logs 只读
            # 聚合到 capability_registry；异常不中断告警循环。
            try:
                from database import db_conn as _dbc
                from core import capability_registry as _cr
                with _dbc() as _c:
                    _cr.aggregate_capability_metrics(_c)
            except Exception as e:
                print("[alert-loop] 能力度量聚合异常（不中断）: %s" % str(e)[:160], flush=True)

    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_run, daemon=True, name="alert-evaluator")
    _loop_thread.start()
    print("[startup] 周期告警评估器已启动（每 %ss，冷却 %smin）" % (interval_sec, cooldown_min),
          flush=True)
    return True


def stop_alert_loop() -> None:
    _loop_stop.set()
