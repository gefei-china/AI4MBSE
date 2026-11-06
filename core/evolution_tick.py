# -*- coding: utf-8 -*-
"""自进化周期触发器（2026-11-06 P1）——把「引擎」叫醒，但绝不越过 HIL 门。

硬纪律（静态门禁会断言，别改）：
  1. 默认 ``observe``：只统计待消费反馈，不提交作业、不写任何表。
  2. ``execute`` 也只到 **HIL 门口**：最多调 ``request_promotion``
     （只写 hil_confirmations），**绝不**调 execute_confirmation /
     apply_config_change / save_override / HILService.decide(approve=True)。
  3. 不自动调 ``config_effect.evaluate_pending(apply=True)``
     —— 那会 UPDATE config_history，违反「自动不改台账」。
  4. 所有作业经 job_queue.submit + 幂等 job_key，重复 tick 不产生重复动作。

为什么放在 core 而不是 workflows：workflows 是编排入口壳（画布已移除），
本模块是独立守护，与 events/watchdog.py 同款 threading.Thread 范式（sync）。
"""
import threading
import time

OBSERVE = "observe"
EXECUTE = "execute"
MODES = (OBSERVE, EXECUTE)

_state = {
    "stop": threading.Event(),
    "thread": None,
    "mode": OBSERVE,
    "interval_s": 300,
    "enabled": False,
    "ticks": 0,
    "last": {},
}


# 这三个名字只作为**文档字符串/注释**出现在本文件；门禁会断言代码行里
# 不存在对它们的调用，防止后人"顺手接上"自动生效。
def _conn():
    from database import db_conn
    return db_conn()


def _has_col(conn, table, col):
    try:
        return col in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return False


def reconcile_feedback_states(conn, *, apply: bool = True) -> dict:
    """把「已进 online_eval_queue 但仍为 new」的反馈推进到 sampled（幂等）。

    ★ 为什么必须独立成函数（2026-11-06 端到端实跑抓到的真缺陷）：
      采样作业是**异步**的。`scan_once` 提交作业当轮就去查队列，那时作业还没跑完
      ⇒ 永远查不到刚入队的项（实测 advanced=0）。而下一轮 tick 在 observe 模式下
      又不写库 ⇒ 状态永远停在 'new'。用户看到「待采样」，实际数据已在下游。
      修法：由「采样作业执行后」与「tick 扫描」两处共同调用本函数。

    forward-only：只把 new → sampled，不回退、不改既有列；`apply=False` 只统计。
    """
    out = {"linked": 0, "advanced": 0, "skipped_no_col": False, "items": []}
    if not _has_col(conn, "feedback", "state"):
        out["skipped_no_col"] = True
        return out
    rows = [dict(r) for r in conn.execute(
        "SELECT id, message_id, quality, feedback_type FROM feedback "
        "WHERE state='new' AND COALESCE(message_id, 0)>0 "
        "AND (COALESCE(quality,'')='down' "
        "OR COALESCE(feedback_type,'') IN ('modify','wrong','corrected')) "
        "ORDER BY id LIMIT 200").fetchall()]
    if not rows:
        return out
    mids = [r["message_id"] for r in rows]
    ph = ",".join("?" for _ in mids)
    linked = {}
    try:
        for q in conn.execute(
                "SELECT id, message_id FROM online_eval_queue "
                "WHERE message_id IN (%s) ORDER BY id" % ph, tuple(mids)).fetchall():
            linked.setdefault(q["message_id"], q["id"])
    except Exception as e:
        out["items"].append({"error": str(e)[:120]})
        return out
    from repositories.conversation_repo import ConversationRepo
    repo = ConversationRepo(conn)
    for r in rows:
        qid = linked.get(r["message_id"])
        if not qid:
            continue
        out["linked"] += 1
        if not apply:
            continue
        a = repo.advance_feedback_state(
            r["id"], "sampled", actor="online_eval_sample",
            link_kind="online_eval", link_id=str(qid),
            note="自动采样器已入队；等待人工标注与提案评估")
        if a.get("ok"):
            out["advanced"] += 1
            out["items"].append({"feedback_id": r["id"], "queue_id": qid})
    if apply and out["advanced"]:
        conn.commit()
    return out


def scan_once(conn, mode=OBSERVE):
    """一次扫描。返回可序列化结果（observe 只读，execute 才写）。"""
    out = {"ok": True, "mode": mode, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "pending_feedback": 0, "jobs": [], "advanced": 0,
           "proposals_created": 0, "promotion_queued": 0, "note": ""}
    if mode not in MODES:
        out.update(ok=False, note="未知模式 %r（只允许 observe/execute）" % mode)
        return out
    if not _has_col(conn, "feedback", "state"):
        out["note"] = "feedback 缺状态列：尚未执行补列迁移"
        return out
    rows = [dict(r) for r in conn.execute(
        "SELECT id, message_id, quality, feedback_type FROM feedback "
        "WHERE state='new' AND (COALESCE(quality,'')='down' "
        "OR COALESCE(feedback_type,'') IN ('modify','wrong','corrected')) "
        "ORDER BY id LIMIT 50").fetchall()]
    out["pending_feedback"] = len(rows)
    if mode == OBSERVE:
        out["note"] = "观察模式：只统计，未提交作业、未写任何表"
        return out

    # ── execute：① 提交幂等采样作业（作业卡可见）──
    from core import job_queue as _jq
    if rows:
        key = "evolution_tick:sample:%d" % max(r["id"] for r in rows)
        try:
            j = _jq.submit(conn, "online_eval_sample",
                           {"origin": "evolution_tick"}, job_key=key)
            out["jobs"].append({"kind": "online_eval_sample",
                                "job_id": j.get("job_id"),
                                "deduped": bool(j.get("deduped")),
                                "job_key": key})
        except Exception as e:
            out["note"] += "采样作业提交失败：%s；" % str(e)[:120]

    # ── ② 生成带来源的提案（generate 幂等：fingerprint 去重）──
    from core import config_proposal as _cp
    try:
        g = _cp.generate(conn, origin="auto",
                         source_feedback_ids=[r["id"] for r in rows])
        out["proposals_created"] = int(g.get("created") or 0)
    except Exception as e:
        out["note"] += "提案生成失败：%s；" % str(e)[:120]

    # ── ③ 状态回填（幂等；作业异步时由 handler 再调一次本函数）──
    rec = reconcile_feedback_states(conn, apply=True)
    out["advanced"] = rec["advanced"]
    out["reconciled"] = rec["linked"]

    # ── ④ 到 HIL 门口为止：只对「合法配置目标 + 沙箱 pass」的提案排队 ──
    #    request_promotion 自带 4 道门，被拒是**正常**结果（fail-closed）。
    if _has_col(conn, "config_proposals", "origin"):
        from core import config_promotion as _promo
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM config_proposals WHERE status='pending' "
                "AND origin='auto' ORDER BY id LIMIT 10").fetchall()]
        except Exception:
            ids = []
        for pid in ids:
            try:
                p = _promo.request_promotion(conn, int(pid), user="evolution_tick")
                if p and not p.get("error"):
                    out["promotion_queued"] += 1
            except Exception:
                pass
    conn.commit()
    out["note"] += "已到 HIL 门口；未执行任何配置变更，config_history 保持 0"
    return out


def start_tick(interval_s=300, mode=OBSERVE, enabled=True):
    """启动守护（幂等）。默认 observe = 引擎在跑但零副作用。"""
    _state["interval_s"] = max(5, int(interval_s or 300))
    _state["mode"] = mode if mode in MODES else OBSERVE
    _state["enabled"] = bool(enabled)
    if not _state["enabled"]:
        print("[startup] 自进化周期触发器未启用（evolution.tick_enabled=False）", flush=True)
        return False
    t = _state["thread"]
    if t and t.is_alive():
        return False
    _state["stop"].clear()

    def _loop():
        while not _state["stop"].wait(_state["interval_s"]):
            try:
                with _conn() as c:
                    res = scan_once(c, _state["mode"])
                _state["ticks"] += 1
                _state["last"] = res
                if res.get("pending_feedback") or res.get("advanced"):
                    print("[evolution-tick] mode=%s 待消费=%d 作业=%d 回填=%d 提案=%d"
                          % (res["mode"], res["pending_feedback"], len(res["jobs"]),
                             res["advanced"], res["proposals_created"]), flush=True)
            except Exception as e:
                print("[evolution-tick] 扫描异常（不中断）：%s" % str(e)[:160], flush=True)

    th = threading.Thread(target=_loop, daemon=True, name="evolution-tick")
    th.start()
    _state["thread"] = th
    print("[startup] 自进化周期触发器已启动（每 %ss，模式 %s）"
          % (_state["interval_s"], _state["mode"]), flush=True)
    return True


def stop_tick():
    _state["stop"].set()


def status():
    """只读状态（供周报 / 端点展示，让「引擎在不在跑」可见）。"""
    th = _state["thread"]
    return {"enabled": _state["enabled"],
            "running": bool(th and th.is_alive()),
            "mode": _state["mode"],
            "interval_s": _state["interval_s"],
            "ticks": _state["ticks"],
            "last": dict(_state["last"] or {}),
            "promise": "execute 模式也只到 HIL 门口，不自动写 config_history"}
