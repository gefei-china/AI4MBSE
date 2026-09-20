"""FlowPersistenceMixin：运行轨迹/检查点/暂停/告警/黑板/会话日志落库。"""
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional


class FlowPersistenceMixin:
    """FlowExecutor 持久化拆分（解耦拆分：原 workflows.py 单体类方法）。"""

    def _ensure_run(self, conn, flow_id, flow_name, parent_run_id: int = 0) -> int:
        cur = conn.execute(
            "INSERT INTO flow_runs (flow_id, flow_name, status, order_json, total_latency_ms, error_count, parent_run_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (flow_id or 0, flow_name or "", "running", "[]", 0, 0, parent_run_id or 0))
        return cur.lastrowid

    def _safe_cp(self, conn, run_id, seq, node_id, results) -> None:
        try:
            conn.execute(
                "INSERT INTO flow_checkpoints (run_id, seq, node_id, state_json) VALUES (?,?,?,?)",
                (run_id, seq, node_id, json.dumps({"results": results}, ensure_ascii=False)[:400000]))
            conn.commit()  # D6：检查点立即提交——释放写锁，使外部 pause 标记（另一连接 UPDATE）即时可见
        except Exception:
            pass

    def _check_pause(self, conn, run_id) -> bool:
        """D6：主动暂停轮询——flow_runs.status=='paused' 时执行器在下一检查点后静默停机（检查点保留，可从断点恢复）。"""
        if not conn or not run_id:
            return False
        try:
            row = conn.execute("SELECT status FROM flow_runs WHERE id=?", (run_id,)).fetchone()
            return bool(row and row["status"] == "paused")
        except Exception:
            return False

    def _load_checkpoints(self, conn, run_id):
        """从最新检查点恢复 {results, executed, cp_seq, resumed}。"""
        row = conn.execute(
            "SELECT state_json FROM flow_checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,)).fetchone()
        if not row:
            return {}, set(), 0, set()
        try:
            state = json.loads(row["state_json"] or "{}")
            results = state.get("results", {})
        except Exception:
            return {}, set(), 0, set()
        executed = {k for k, v in results.items() if v.get("status") in ("done", "error")}
        last = conn.execute(
            "SELECT seq FROM flow_checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,)).fetchone()
        return results, executed, (last["seq"] if last else 0), set(results.keys())

    def _persist_steps(self, conn, run_id, order, results, summary) -> None:
        conn.execute(
            "UPDATE flow_runs SET status=?, order_json=?, total_latency_ms=?, error_count=? WHERE id=?",
            (summary["status"], json.dumps(order, ensure_ascii=False),
             summary["total_latency_ms"], len(summary["errors"]), run_id))
        conn.execute("DELETE FROM flow_run_steps WHERE run_id=?", (run_id,))  # resume 场景防重复
        for seq, nid in enumerate(order):
            o = results.get(nid, {})
            try:
                data_str = json.dumps(o.get("data", {}), ensure_ascii=False)[:2000]
            except Exception:
                data_str = "{}"
            conn.execute(
                "INSERT INTO flow_run_steps (run_id, seq, node_id, node_type, node_label, status, content, data, latency_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, seq, nid, o.get("type", ""), o.get("node", ""), o.get("status", ""),
                 str(o.get("content", ""))[:2000], data_str, o.get("latency_ms", 0)))

    # ── D12 统一监控：运行结束后告警规则评估（阈值命中 → alert_events + 可选 webhook 通知）──
    def _evaluate_alerts(self, conn, run_id: int, summary: dict) -> bool:
        """评估 active 告警规则并落事件。

        指标：
        - success_rate（run 级：completed=100，否则 0）/ avg_latency（run 总耗时 ms）/ error_count（run 级错误数）
        - mock_rate（全局：近 24h llm_usage_stats mock 占比）
        命中：写 alert_events(open) + 更新规则 last_fired_at；notify_url 配置时 POST A2A alert 事件（失败不阻断）。
        """
        if not run_id or conn is None:
            return False
        try:
            rules = conn.execute("SELECT * FROM alert_rules WHERE status='active'").fetchall()
        except Exception:
            return False
        if not rules:
            return False
        status = summary.get("status")
        metrics = {
            "success_rate": 100.0 if status == "completed" else 0.0,
            "avg_latency": float(summary.get("total_latency_ms") or 0),
            "error_count": float(len(summary.get("errors") or [])),
        }
        # mock_rate：近 24h LLM 调用 mock 占比（全局指标）
        try:
            row = conn.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(used_mock),0) m FROM llm_usage_stats "
                "WHERE created_at >= datetime('now','-1 day')").fetchone()
            total = row["c"] or 0
            metrics["mock_rate"] = round((row["m"] or 0) * 100.0 / total, 1) if total else 0.0
        except Exception:
            metrics["mock_rate"] = 0.0
        fired = False
        for r in rules:
            metric = r["metric"] or ""
            if metric not in metrics:
                continue
            actual = metrics[metric]
            op = r["operator"] or ">"
            th = float(r["threshold"] or 0)
            hit = {"<": actual < th, "<=": actual <= th, ">": actual > th, ">=": actual >= th}.get(op, False)
            if not hit:
                continue
            fired = True
            flow_name = getattr(self, "_run_flow_name", "") or ""
            try:
                conn.execute(
                    "INSERT INTO alert_events (rule_id, rule_name, run_id, flow_name, metric, actual, "
                    "threshold, operator, level, status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
                    (r["id"], r["name"], run_id, flow_name, metric, actual, th, op, r["level"] or "warning"))
                conn.execute("UPDATE alert_rules SET last_fired_at=datetime('now') WHERE id=?", (r["id"],))
                conn.commit()
            except Exception:
                pass
            # 可选 webhook 通知（A2A alert 事件，失败不阻断主流程）
            if (r["notify_url"] or "").strip():
                self._notify_alert(conn, run_id, dict(r), metric, actual, th, op)
        return fired

    # ── D12 告警 webhook 通知：POST 标准化 A2A 事件（event.type='alert_triggered'，HMAC-SHA256 签名）──
    def _notify_alert(self, conn, run_id: int, rule: dict, metric: str, actual: float,
                      threshold: float, op: str) -> None:
        import hashlib
        import hmac as hmac_mod
        import uuid as uuid_mod
        import httpx
        try:
            body = {
                "protocol": "a2a", "version": "0.1", "kind": "event",
                "event": {"id": "alr-" + uuid_mod.uuid4().hex[:12], "type": "alert_triggered",
                          "run_id": run_id, "node_id": "", "node_label": rule.get("name", ""),
                          "node_type": "alert", "payload": {"rule_id": rule.get("id"),
                                                             "metric": metric, "actual": actual,
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
            httpx.post(rule["notify_url"], content=data_str, headers=headers, timeout=8)
            try:
                conn.execute("UPDATE alert_events SET status='acked' WHERE rule_id=? AND run_id=? AND status='open'",
                             (rule.get("id"), run_id))
                conn.commit()
            except Exception:
                pass
        except Exception:
            pass  # 告警通知失败不阻断主流程（仅落库事件）


    # ── P1 记忆系统：共享黑板（L1 工作记忆）/ 编排级会话（L2）──
    def _load_blackboard(self, conn, run_id) -> dict:
        """加载该 run 的共享黑板（flow_working_memory）。"""
        bb = {}
        for r in conn.execute(
                "SELECT key, value_json FROM flow_working_memory WHERE run_id=?", (run_id,)).fetchall():
            try:
                bb[r["key"]] = json.loads(r["value_json"] or "null")
            except Exception:
                bb[r["key"]] = r["value_json"]
        return bb

    def _write_blackboard(self, conn, run_id, node, out, blackboard) -> None:
        """按节点 config.write_keys 把产出写入黑板；keys 格式 '名称' 或 '名称:data.字段'，逗号分隔。"""
        cfg = node.get("config", {}) or {}
        wks = cfg.get("write_keys") or []
        if isinstance(wks, str):
            wks = [x.strip() for x in wks.split(",") if x.strip()]
        for wk in wks:
            wk = str(wk).strip()
            if not wk:
                continue
            if ":" in wk:
                k, path = wk.split(":", 1)
                val = out
                for p in path.split("."):
                    if isinstance(val, dict):
                        val = val.get(p)
                    else:
                        val = None
                        break
            else:
                k, val = wk, out.get("content")
            if val is None:
                continue
            blackboard[k] = val
            try:
                conn.execute(
                    "INSERT INTO flow_working_memory (run_id, key, value_json, mem_type) VALUES (?,?,?,?) "
                    "ON CONFLICT(run_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=CURRENT_TIMESTAMP",
                    (run_id, k, json.dumps(val, ensure_ascii=False)[:200000], "artifacts"))
            except Exception:
                try:  # 旧 SQLite 无 ON CONFLICT → 先删后插
                    conn.execute("DELETE FROM flow_working_memory WHERE run_id=? AND key=?", (run_id, k))
                    conn.execute(
                        "INSERT INTO flow_working_memory (run_id, key, value_json, mem_type) VALUES (?,?,?,?)",
                        (run_id, k, json.dumps(val, ensure_ascii=False)[:200000], "artifacts"))
                except Exception:
                    pass

    def _log_conversation(self, conn, run_id, seq, role, content) -> None:
        if not run_id:
            return
        try:
            conn.execute(
                "INSERT INTO flow_conversations (run_id, seq, role, content) VALUES (?,?,?,?)",
                (run_id, seq, role, content))
        except Exception:
            pass
