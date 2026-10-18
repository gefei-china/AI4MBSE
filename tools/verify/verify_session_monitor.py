# -*- coding: utf-8 -*-
"""会话级运行监控（2026-10-18《运行监控选型建议 v2》实施）真实库验证。

验证四件事：
  V1 迁移幂等：_migrate_columns 对真实库补齐 flow_runs.conversation_id /
     alert_events.conversation_id / alert_events.request_id，重复执行不报错；
  V2 写入链路（在**临时副本**上做，不污染真库）：
     - bind_trace(conversation_id) 上下文里 _ensure_run 落库的 flow_runs.conversation_id 正确；
     - _evaluate_alerts 落库的 alert_events.conversation_id / request_id 正确（含老库列缺失回落路径）；
  V3 列表端点：monitor_sessions 对真实数据返回异常会话加权排序 + flagged 计数；
  V4 下钻端点：monitor_session_detail 对真实会话返回完整证据链 + 错误画像，时间线时间可排序。

用法（项目 .venv，仓库根目录执行）：
    .venv/Scripts/python.exe tools/verify/verify_session_monitor.py

双环境口径（2026-10-18 接线 CI 时补齐）：
  真库（开发机）—— 全量断言；干净库（CI，init_db 种子只有 1 个空壳会话）——
  底层无证据行的断言一律 **SKIP**（判据面不存在 ≠ 产品缺陷，仓内惯例同
  verify_p14b / verify_uninstall_flow）；**有证据行却返 0 段仍然 FAIL**（真缺陷）。
"""
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  -- {detail}" if detail else ""))


def skip(name, why):
    """判据面不存在（干净库无证据行）⇒ SKIP：不计失败，也不装通过。"""
    SKIP.append(name)
    print("  [SKIP] " + name + f"  -- {why}")


def main():
    from database import get_db          # noqa: E402
    from database.migrations import _migrate_columns   # noqa: E402
    from routers.monitor import monitor_sessions, monitor_session_detail  # noqa: E402
    from workflows.persistence import FlowPersistenceMixin  # noqa: E402
    from core import audit as audit_mod  # noqa: E402

    print("== V1 迁移幂等（真实库）==")
    conn = get_db()
    # 先做安全备份（sqlite3 backup API：WAL 一致性快照）
    bak = os.path.join(ROOT, "backups", f"mbse.db.bak-before-session-monitor-{time.strftime('%Y%m%d')}")
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    dst = sqlite3.connect(bak)
    conn.backup(dst)
    dst.close()
    print(f"  备份: {bak}")
    _migrate_columns(conn)
    _migrate_columns(conn)     # 幂等：第二次执行不得报错
    fr_cols = {r[1] for r in conn.execute("PRAGMA table_info(flow_runs)").fetchall()}
    ae_cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_events)").fetchall()}
    check("flow_runs.conversation_id 已补齐", "conversation_id" in fr_cols)
    check("alert_events.conversation_id 已补齐", "conversation_id" in ae_cols)
    check("alert_events.request_id 已补齐", "request_id" in ae_cols)
    idx = conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_fr_conv'").fetchone()
    check("索引 ix_fr_conv 已建", bool(idx))

    print("== V3 异常会话列表（真实数据）==")
    res = monitor_sessions(days=30, limit=20, q="", conn=conn)
    check("端点返回 ok", bool(res.get("ok")))
    ss = res.get("sessions") or []
    if not ss:
        # 干净库（CI）常态：无会话样本 ⇒ 列表内容断言整体 SKIP（端点 ok 已验）
        skip("返回会话数>0", f"本库无会话样本（total={res.get('total')}）⇒ 判据面不存在")
        skip("加权排序正确（非升序破坏）", "同上：无会话样本")
    else:
        check("返回会话数>0", True)
        weights = [s["weight"] for s in ss]
        check("加权排序正确（非升序破坏）", weights == sorted(weights, reverse=True))
    print(f"  近30天会话 {res.get('total')} 个，问题信号加权命中的 {res.get('flagged')} 个")
    for s in ss[:5]:
        print(f"    #{s['conversation_id']} w={s['weight']:>3} 失败={s['llm_failed']} 截断={s['llm_trunc']} "
              f"回退={s['llm_fallback']} 重试={s['llm_retries']} 任务失败={s['failed_tasks']} "
              f"run失败={s['failed_runs']} LLM={s['llm_calls']}次 ${s['llm_cost']} | {s['title'][:24]}")

    target = next((s for s in ss if s["weight"] > 0), ss[0] if ss else None)
    if target:
        print(f"== V4 会话下钻（真实会话 #{target['conversation_id']}）==")
        det = monitor_session_detail(conversation_id=target["conversation_id"], conn=conn)
        check("下钻端点返回 ok", bool(det.get("ok")))
        check("会话概览存在", bool(det.get("conversation")))
        # ★ 判据面检查：目标会话在底层表里有没有证据行。
        #   干净库的种子会话是空壳（0 消息/0 调用）⇒ 下钻返 0 段是判据面不存在；
        #   有证据行却返 0 段才是端点真坏。两者不可混为一谈。
        _cid = target["conversation_id"]
        _ev = conn.execute(
            "SELECT (SELECT COUNT(*) FROM messages WHERE conversation_id=?)"
            " + (SELECT COUNT(*) FROM llm_usage_stats WHERE conversation_id=?)"
            " + (SELECT COUNT(*) FROM flow_runs WHERE conversation_id=?)"
            " + (SELECT COUNT(*) FROM agent_tasks WHERE conversation_id=?)",
            (_cid, _cid, _cid, _cid)).fetchone()[0]
        seg_n = sum(len(det.get(k) or []) for k in
                    ("messages", "llm_calls", "orch_runs", "agent_tasks", "stream_events",
                     "flow_runs", "flow_steps", "hil"))
        if _ev == 0:
            skip("证据链分段齐备（≥1 段）",
                 f"会话 #{_cid} 底层 0 条证据行（干净库常态）⇒ 判据面不存在")
        else:
            check("证据链分段齐备（≥1 段）", seg_n > 0,
                  f"共 {seg_n} 段（底层证据 {_ev} 行 ⇒ 有样本必须返段）")
        p = det.get("error_profile") or {}
        check("错误画像 weight 与列表一致",
              p.get("weight") == target["weight"], f"detail={p.get('weight')} list={target['weight']}")
        times = [r.get("created_at") or "" for r in det.get("messages", [])]
        if len(times) < 2:
            skip("时间线时间可排序（非降序破坏）", f"仅 {len(times)} 条消息 ⇒ 排序判据面不存在")
        else:
            check("时间线时间可排序（非降序破坏）",
                  all(times[i] <= times[i + 1] for i in range(len(times) - 1)))
        print(f"  错误画像: {p}")
        bad = [l for l in det.get("llm_calls", []) if l.get("call_failed") or l.get("failure_kind")]
        if bad:
            b = bad[0]
            print(f"  失败调用样例: #{b['id']} kind={b.get('failure_kind')} msg={str(b.get('failure_msg'))[:60]}")
        trunc = [l for l in det.get("llm_calls", []) if l.get("finish_reason") == "length"]
        if trunc:
            print(f"  截断调用样例: #{trunc[0]['id']} finish_reason=length {trunc[0]['total_tokens']}tok")
    conn.close()

    print("== V2 写入链路（临时副本，不污染真库）==")
    copy_path = os.path.join(ROOT, "tmp", "verify_session_monitor_copy.db")
    os.makedirs(os.path.dirname(copy_path), exist_ok=True)
    if os.path.exists(copy_path):
        os.remove(copy_path)
    src = sqlite3.connect(os.path.join(ROOT, "mbse.db"))
    cp = sqlite3.connect(copy_path)
    src.backup(cp)
    src.close()
    cp.row_factory = sqlite3.Row
    cp.execute("PRAGMA journal_mode=WAL")
    _migrate_columns(cp)
    cid_row = cp.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
    cid = int(cid_row["id"])
    # 模拟请求上下文 + 链路上下文（与 HTTP 中间件/流水线入口同款调用）
    class _FakeReq:
        headers = {"x-request-id": "verify-req-1234", "user-agent": "verify-session-monitor",
                   "x-forwarded-for": "127.0.0.1"}
        client = None
    audit_mod.bind_request(_FakeReq())
    audit_mod.bind_trace(conversation_id=cid, trace_id="verify-trace-1")

    class _P(FlowPersistenceMixin):
        pass
    p = _P()
    run_id = p._ensure_run(cp, 0, "verify-session-monitor")
    row = cp.execute("SELECT conversation_id FROM flow_runs WHERE id=?", (run_id,)).fetchone()
    check("_ensure_run 写入 conversation_id", row and int(row["conversation_id"]) == cid,
          f"run_id={run_id} conv={row['conversation_id'] if row else None}")

    cp.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level, notify_url, secret, status) "
               "VALUES ('verify-规则','success_rate','<',100,'warning','','','active')")
    rid_row = cp.execute("SELECT last_insert_rowid() rid").fetchone()
    fired = p._evaluate_alerts(cp, run_id, {"status": "partial", "total_latency_ms": 123, "errors": ["boom"]})
    check("run 级告警命中并落库", bool(fired))
    ev = cp.execute("SELECT conversation_id, request_id, run_id FROM alert_events "
                    "WHERE rule_id=? ORDER BY id DESC LIMIT 1", (rid_row["rid"],)).fetchone()
    check("alert_events 带会话溯源", bool(ev) and int(ev["conversation_id"]) == cid,
          f"conv={ev['conversation_id'] if ev else None}")
    check("alert_events 带请求溯源", bool(ev) and ev["request_id"] == "verify-req-1234",
          f"rid={ev['request_id'] if ev else ''}")
    # 会话下钻端点应能看到刚写入的 run（证明读写闭环）
    det2 = monitor_session_detail(conversation_id=cid, conn=cp)
    check("下钻端点可见新写入的工作流 run",
          any(fr["id"] == run_id for fr in det2.get("flow_runs", [])),
          f"flow_runs={[(fr['id'], fr['flow_name']) for fr in det2.get('flow_runs', [])]}")
    cp.close()
    print(f"  副本保留于 {copy_path}（验证痕迹，可随时删除）")

    print(f"\n结果: {len(PASS)} PASS / {len(FAIL)} FAIL / {len(SKIP)} SKIP")
    if FAIL:
        print("失败项:", "; ".join(FAIL))
        sys.exit(1)
    print("全部通过 —— 会话级运行监控（列表+下钻+断链补齐）验证有效"
          + ("（干净库：内容断言按判据面不存在 SKIP）" if SKIP else "。"))


if __name__ == "__main__":
    main()
