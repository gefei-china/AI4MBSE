# -*- coding: utf-8 -*-
"""P0-b 在线评测：生产流量采样 + 确定性自动评（2026-10-25，S3）。

设计 docs/P0-b-生产回流详细设计-2026-10-25.md §4.1。五段闭环的第①②段：
   ① 采样器 sample_candidates：负反馈优先 > 低置信优先 > 随机 5%
   ② 自动评估器 auto_evaluate_pending：确定性层（复用 eval_generation_quality）
第③④段（人工标注 / 转正三分流）已同模块落地：promote_confirmed 按
sample_type 三分流（intent→intent_samples / retrieval→qrels.jsonl /
generation→eval_gen_labels），幂等 + dry-run 纪律同全模块；4 个端点见
routers/monitor.py（S4 接线）。

**贯穿纪律**（与 P0-a core/badcase.py 同源，全部来自已付学费的教训）：
1. **dry-run 默认**：sample_candidates(apply=False) 不写库，只返回候选与
   预判；真写必须显式 apply=True（人工关口，同 collect_bad_cases）。
2. **自动打分永不自动转正**：本模块只写 auto_score/auto_detail（"建议"），
   status 流转只由人工标注触发（B1 纪律，防"LLM 评 LLM 再自动喂回"自我循环）。
3. **失败可见、不补零**：任何一步异常记入返回值的 failures 列表（上层
   evolution_report 的 collect_failures 消费它上周报），auto_score 留 NULL——
   "没评到"与"评了 0 分"必须可区分（补零会让看板把前者渲染成后者，假绿）。
4. **不造假数据**：入队行必须带完整外键（conversation_id/message_id/
   query_trace_id），转正时按外键回查原文，不留孤儿标签。

**阈值校准记录**（判据跟着真实数据改，MEMORY 纪律）：
    设计文档 §7 的 low_conf_threshold 默认 0.5 是**拍脑袋值**——2026-10-25
    对 mbse.db 只读实测：query_trace.top_score 全域量级 [0, 0.096]
    （p25=0.036 / p50=0.055 / avg=0.0498），0.5 会命中**全表 3276 行**。
    故默认改 0.036（=p25，取最低四分位为"低置信"），并加 sample_batch 上限
    （默认 20）与 sample_window_days（默认 30，同 badcase 窗口）——无上限时
    低置信单源即 ~819 行，队列会被一次采样撑爆（R12 积压告警失去意义）。
"""
import json
import os
import random
import sqlite3
from datetime import datetime

# ── 配置（core.config eval_online 组；禁模块内常量旁路，默认值仅兜底）──
_SETTING_DEFAULTS = {
    "enabled": True,               # 总开关（False 时采样返空而非 500）
    "random_rate": 0.05,           # 随机采样比例（负反馈/低置信优先不受此限）
    "low_conf_threshold": 0.036,   # query_trace.top_score 低于此值入队（实测 p25）
    "sample_window_days": 30,      # 采样窗口（同 badcase：只扫最近 N 天）
    "sample_batch": 20,            # 单次采样入队上限（防低置信单源撑爆队列）
    "auto_eval_limit": 50,         # 单次自动评批量
    "backlog_alert": 20,           # R12 阈值（权威值在 seed_registry._ALERT_RULES，此处仅默认）
    "promote_window_days": 7,      # R13 统计窗口
}

#: 负反馈信号源一：ux_metrics 事件白名单中的负向事件（repositories/ux_metrics_repo.EVENTS）
_UX_NEG_EVENTS = ("task_error", "interrupt")
#: 负反馈信号源二：feedback 表负向类型（与 core/badcase._SQL_FEEDBACK 同口径，
#: 不造第二份词表——两份必然漂移）
_FB_NEG_TYPES = ("modify", "wrong", "corrected")


def get_settings() -> dict:
    """读 eval_online 组配置（门禁用 monkeypatch core.config 变异自证真被消费）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTING_DEFAULTS.items():
        if isinstance(default, bool):
            out[key] = _config.as_bool("eval_online", key, default)
            continue
        try:
            out[key] = type(default)(_config.get("eval_online", key, default))
        except Exception:
            out[key] = default
    return out


def _tbl_exists(conn, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════
# ① 采样器
# ══════════════════════════════════════════════════════════════════════════
def _cand_neg_feedback(conn, window_arg, failures: list) -> list:
    """负反馈候选（第一优先）：ux_metrics 负向事件 + feedback 负向类型。

    sample_type='generation'（抱怨的对象是**答案**）。ux_metrics 只有
    conversation_id 无 message_id ⇒ 回查该会话最近一条助手消息（启发式，
    写进 reason_detail 让人能看出是回查的，不冒充原始引用）。
    failures：源级异常**记入不吞**——静默跳过会让"采样器坏了"长得像
    "没有负反馈"（2026-10-25 绑参 bug 就是这么漏过一轮的，见模块头纪律）。
    """
    out = []
    if _tbl_exists(conn, "ux_metrics"):
        try:
            rows = conn.execute(
                "SELECT id, event, conversation_id, detail, created_at "
                "FROM ux_metrics WHERE event IN (%s) "
                "AND created_at >= datetime('now', ?) ORDER BY id DESC"
                % ",".join("?" * len(_UX_NEG_EVENTS)),
                (*_UX_NEG_EVENTS, window_arg[0])).fetchall()
            for r in rows:
                r = dict(r)
                mid = 0
                if r.get("conversation_id"):
                    m = conn.execute(
                        "SELECT id FROM messages WHERE conversation_id=? "
                        "AND role='assistant' ORDER BY id DESC LIMIT 1",
                        (int(r["conversation_id"]),)).fetchone()
                    mid = int(m[0]) if m else 0
                out.append({
                    "sample_type": "generation",
                    "conversation_id": int(r.get("conversation_id") or 0),
                    "message_id": mid,
                    "query_trace_id": 0,
                    "trace_id": "",
                    "sample_reason": "neg_feedback",
                    "reason_detail": "ux_metrics#%d %s%s" % (
                        int(r["id"]), r.get("event") or "",
                        "（助手消息为回查）" if mid else "（无助手消息）"),
                })
        except Exception as e:                                 # noqa: BLE001
            failures.append("neg_feedback(ux_metrics): %s: %s"
                            % (type(e).__name__, str(e)[:160]))
    if _tbl_exists(conn, "feedback"):
        try:
            rows = conn.execute(
                "SELECT id, message_id, conversation_id, feedback_type, context "
                "FROM feedback WHERE feedback_type IN (%s) "
                "AND created_at >= datetime('now', ?) ORDER BY id DESC"
                % ",".join("?" * len(_FB_NEG_TYPES)),
                (*_FB_NEG_TYPES, window_arg[0])).fetchall()
            for r in rows:
                r = dict(r)
                out.append({
                    "sample_type": "generation",
                    "conversation_id": int(r.get("conversation_id") or 0),
                    "message_id": int(r.get("message_id") or 0),
                    "query_trace_id": 0,
                    "trace_id": "",
                    "sample_reason": "neg_feedback",
                    "reason_detail": "feedback#%d %s" % (
                        int(r["id"]), r.get("feedback_type") or ""),
                })
        except Exception as e:                                 # noqa: BLE001
            failures.append("neg_feedback(feedback): %s: %s"
                            % (type(e).__name__, str(e)[:160]))
    return out


def _cand_low_conf(conn, window_arg, threshold: float, cap: int,
                   failures: list) -> list:
    """低置信候选（第二优先）：query_trace.top_score < threshold（升序，最没底先采）。

    sample_type='retrieval'（置信度是**检索侧**信号）。cap 限制单源条数——
    实测全表 3276 行 top_score 全域 <0.096，无 cap 会一次采上千条。
    """
    if not _tbl_exists(conn, "query_trace"):
        return []
    try:
        rows = conn.execute(
            "SELECT id, query, intent, route, top_score FROM query_trace "
            "WHERE top_score < ? AND created_at >= datetime('now', ?) "
            "ORDER BY top_score ASC, id ASC LIMIT ?",
            (float(threshold), window_arg[0], int(cap))).fetchall()
    except Exception as e:                                     # noqa: BLE001
        failures.append("low_conf: %s: %s" % (type(e).__name__, str(e)[:160]))
        return []
    return [{
        "sample_type": "retrieval",
        "conversation_id": 0,
        "message_id": 0,
        "query_trace_id": int(r["id"]),
        "trace_id": "",
        "sample_reason": "low_conf",
        "reason_detail": "top_score=%.4f<%.4f route=%s" % (
            float(r["top_score"] or 0), float(threshold), r["route"] or "-"),
    } for r in rows]


def _cand_random(conn, window_arg, rate: float, cap: int,
                 failures: list) -> list:
    """随机候选（第三优先）：窗口内 query_trace 按 rate 抽样。

    sample_type='intent'（query 原文即意图样本，转正进 intent_samples）。
    为什么用 query_trace 而非 messages：意图样本的消费方（calibrate_routing）
    读的就是 query/intent/route 三件套，与 query_trace 列一一对应。
    """
    if not _tbl_exists(conn, "query_trace"):
        return []
    try:
        rows = conn.execute(
            "SELECT id, query, intent, route, top_score FROM query_trace "
            "WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 500",
            window_arg).fetchall()
    except Exception as e:                                     # noqa: BLE001
        failures.append("random: %s: %s" % (type(e).__name__, str(e)[:160]))
        return []
    pool = [dict(r) for r in rows]
    if not pool:
        return []
    k = max(1, int(len(pool) * float(rate)))
    k = min(k, int(cap))
    rng = random.Random(0x5eed)          # 固定种子：dry-run 与 apply 预判一致（可复现）
    picked = rng.sample(pool, k)
    return [{
        "sample_type": "intent",
        "conversation_id": 0,
        "message_id": 0,
        "query_trace_id": int(r["id"]),
        "trace_id": "",
        "sample_reason": "random_5pct",
        "reason_detail": "random rate=%.3f pool=%d" % (float(rate), len(pool)),
    } for r in picked]


def _already_queued(conn, sample_type: str, message_id: int,
                    query_trace_id: int) -> bool:
    """去重：同 (sample_type, 源键) 已在队列（任何状态）⇒ 不重复入队。

    为什么不分理由去重（§3.1 说同消息可因不同理由多次入队）：负反馈走
    generation 键（message_id），低置信/随机走 retrieval/intent 键
    （query_trace_id）⇒ 不同理由天然落在不同 sample_type，不会互刷；
    同理由同源的重复入队才是噪声（每次采样都重采同一条低置信 trace）。
    """
    if not _tbl_exists(conn, "online_eval_queue"):
        return False
    try:
        if message_id:
            row = conn.execute(
                "SELECT 1 FROM online_eval_queue WHERE sample_type=? "
                "AND message_id=? LIMIT 1", (sample_type, int(message_id))).fetchone()
            if row:
                return True
        if query_trace_id:
            row = conn.execute(
                "SELECT 1 FROM online_eval_queue WHERE sample_type=? "
                "AND query_trace_id=? LIMIT 1",
                (sample_type, int(query_trace_id))).fetchone()
            if row:
                return True
    except Exception:
        return False
    return False


def sample_candidates(conn, *, apply: bool = False, settings=None) -> dict:
    """采样器：负反馈优先 > 低置信优先 > 随机 5%。默认 dry-run 不写库。

    返回 {ok, enabled, dry_run, window_days, batch, candidates, plan,
          inserted, failures, generated_at}。plan 按 sample_reason 给出
    各源候选/去重后条数（dry-run 即可回答"这一轮会采到什么"）。
    """
    cfg = settings or get_settings()
    out = {"ok": True, "enabled": bool(cfg.get("enabled")),
           "dry_run": not apply, "window_days": int(cfg.get("sample_window_days") or 30),
           "batch": int(cfg.get("sample_batch") or 20),
           "candidates": [], "plan": {}, "inserted": 0,
           "failures": [], "generated_at": ""}
    if not out["enabled"]:
        return out
    window_arg = ("-" + str(out["window_days"]) + " day",)
    failures = out["failures"]
    try:
        cands = (_cand_neg_feedback(conn, window_arg, failures)
                 + _cand_low_conf(conn, window_arg,
                                  float(cfg.get("low_conf_threshold") or 0.036),
                                  out["batch"], failures)
                 + _cand_random(conn, window_arg,
                                float(cfg.get("random_rate") or 0.05),
                                out["batch"], failures))
    except Exception as e:                                     # noqa: BLE001
        failures.append("sample: %s: %s" % (type(e).__name__, str(e)[:160]))
        cands = []
    # 去重 + 批量上限（按优先级序：负反馈 > 低置信 > 随机，截断到 batch）
    kept, seen = [], set()
    for c in cands:
        key = (c["sample_type"], c["message_id"], c["query_trace_id"])
        if key in seen:
            continue
        if _already_queued(conn, c["sample_type"], c["message_id"],
                           c["query_trace_id"]):
            continue
        seen.add(key)
        kept.append(c)
        if len(kept) >= out["batch"]:
            break
    out["candidates"] = kept
    plan = {}
    for c in kept:
        p = plan.setdefault(c["sample_reason"], {"kept": 0, "sample_types": {}})
        p["kept"] += 1
        st = p["sample_types"].setdefault(c["sample_type"], 0)
        p["sample_types"][c["sample_type"]] = st + 1
    out["plan"] = plan
    out["generated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if apply:
        if not _tbl_exists(conn, "online_eval_queue"):
            out["ok"] = False
            out["error"] = "online_eval_queue 表不存在（迁移未执行）"
            return out
        n = 0
        for c in kept:
            try:
                conn.execute(
                    "INSERT INTO online_eval_queue (sample_type, conversation_id, "
                    "message_id, query_trace_id, trace_id, sample_reason, reason_detail) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (c["sample_type"], c["conversation_id"], c["message_id"],
                     c["query_trace_id"], c["trace_id"], c["sample_reason"],
                     c["reason_detail"]))
                n += 1
            except Exception as e:                             # noqa: BLE001
                failures.append("insert: %s: %s" % (type(e).__name__, str(e)[:160]))
        conn.commit()
        out["inserted"] = n
    return out


# ══════════════════════════════════════════════════════════════════════════
# ② 自动评估器（reference-free 确定性层；复用 eval_generation_quality）
# ══════════════════════════════════════════════════════════════════════════
def _load_answer(conn, message_id: int):
    """按 message_id 取答案体 + citations（card_data JSON）。

    返回 evaluate_answers 吃的 {id, body, citations:[{content,...}]}；
    取不到返 None（调用方记 failure，不补零）。
    """
    row = conn.execute(
        "SELECT id, content, card_data FROM messages WHERE id=?",
        (int(message_id),)).fetchone()
    if not row:
        return None
    row = dict(row)
    body = row.get("content") or ""
    citations = []
    cd = row.get("card_data")
    if cd:
        try:
            card = json.loads(cd) if isinstance(cd, str) else cd
            citations = card.get("citations") or []
        except Exception:
            citations = []
    return {"id": int(row["id"]), "body": body, "citations": citations}


def _score_one(m: dict) -> tuple:
    """单条答案 → (auto_score, auto_detail)。口径写进 auto_detail 可复查。

    综合分定义（0-1，越高越像"有据可依的答案"）：
      · 有引用：0.5*编号合法率（无 [n] 标记时记 1.0——没有错编号，扣分由
        使用率承担）+ 0.3*引用使用率 + 0.2*接地率（无可核查实体时记 1.0——
        与 eval_generation_quality 的"接地不门禁"同理：没有实体就没有
        可核查声称，不是幻觉证据）
      · 无引用：1 - 无据声称（含可核查实体=1 ⇒ 0 分；不含=1 分）
    任何分母为 0 的指标返 None 不返 0（"没测到"≠"测到 0 分"，模块头纪律③）。
    """
    from tools.eval.eval_generation_quality import evaluate_answers
    res = evaluate_answers([m])
    detail = {"citation_valid_rate": res.get("citation_valid_rate"),
              "citation_usage_rate": res.get("citation_usage_rate"),
              "no_citation_factual_rate": res.get("no_citation_factual_rate"),
              "grounded_entity_rate": res.get("grounded_entity_rate")}
    if m.get("citations"):
        valid = res.get("citation_valid_rate")
        usage = res.get("citation_usage_rate")
        ground = res.get("grounded_entity_rate")
        score = (0.5 * (1.0 if valid is None else valid)
                 + 0.3 * (0.0 if usage is None else usage)
                 + 0.2 * (1.0 if ground is None else ground))
    else:
        factual = res.get("no_citation_factual_rate")
        score = 0.0 if factual is None else (1.0 - factual)
    detail["formula"] = ("0.5*valid+0.3*usage+0.2*ground" if m.get("citations")
                         else "1-no_citation_factual")
    return round(score, 4), json.dumps(detail, ensure_ascii=False)


def auto_evaluate_pending(conn, *, limit: int = 50, apply: bool = False,
                          settings=None) -> dict:
    """对 pending 且未评的样本跑确定性指标。只写 auto_score（建议）。

    返回 {ok, dry_run, scanned, scored, failed, failures, items, generated_at}。
    失败（取不到答案体/评测器异常）记 failures 并留 auto_score=NULL——
    **不补零**（补零会让"没评"长得像"评了 0 分"）。
    """
    cfg = settings or get_settings()
    out = {"ok": True, "dry_run": not apply,
           "limit": int(limit or cfg.get("auto_eval_limit") or 50),
           "scanned": 0, "skipped_no_message": 0, "scored": 0, "failed": 0,
           "failures": [], "items": [], "generated_at": ""}
    if not _tbl_exists(conn, "online_eval_queue"):
        out["ok"] = False
        out["error"] = "online_eval_queue 表不存在（迁移未执行）"
        return out
    rows = conn.execute(
        "SELECT id, sample_type, message_id FROM online_eval_queue "
        "WHERE status='pending' AND auto_score IS NULL ORDER BY id LIMIT ?",
        (out["limit"],)).fetchall()
    for r in rows:
        r = dict(r)
        mid = int(r.get("message_id") or 0)
        if not mid:
            # intent/retrieval 样本没有答案体（转正走 dataset 不需 auto_score）——
            # 跳过但不算失败：不是每条样本都该有生成质量分。单列计数，不混进
            # scanned（否则"扫过多少可评样本"这个数被不可评行灌水）。
            out["skipped_no_message"] += 1
            continue
        out["scanned"] += 1
        try:
            m = _load_answer(conn, mid)
            if m is None:
                raise ValueError("message %d 不存在（孤儿引用）" % mid)
            score, detail = _score_one(m)
        except Exception as e:                                 # noqa: BLE001
            out["failed"] += 1
            out["failures"].append("queue#%d msg#%d: %s: %s"
                                   % (r["id"], mid, type(e).__name__, str(e)[:160]))
            continue
        out["scored"] += 1
        out["items"].append({"queue_id": r["id"], "message_id": mid,
                             "auto_score": score})
        if apply:
            try:
                conn.execute(
                    "UPDATE online_eval_queue SET auto_score=?, auto_detail=?, "
                    "auto_eval_at=CURRENT_TIMESTAMP WHERE id=?",
                    (score, detail, r["id"]))
            except Exception as e:                             # noqa: BLE001
                out["failures"].append("update queue#%d: %s: %s"
                                       % (r["id"], type(e).__name__, str(e)[:160]))
    if apply:
        conn.commit()
    out["generated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return out


# ══════════════════════════════════════════════════════════════════════════
# 面板/告警数据源
# ══════════════════════════════════════════════════════════════════════════
def queue_summary(conn) -> dict:
    """队列全景：状态 × 采样理由 × 类型 × auto 覆盖率（monitor 页/G 段真库对拍）。"""
    out = {"ok": True, "total": 0, "by_status": {}, "by_reason": {},
           "by_type": {}, "auto_coverage": None, "note": ""}
    if not _tbl_exists(conn, "online_eval_queue"):
        out["note"] = "online_eval_queue 表未建立（迁移未执行）"
        return out
    try:
        out["total"] = conn.execute(
            "SELECT COUNT(*) FROM online_eval_queue").fetchone()[0]
        for col, key in (("status", "by_status"), ("sample_reason", "by_reason"),
                         ("sample_type", "by_type")):
            d = {}
            for r in conn.execute(
                    "SELECT %s AS k, COUNT(*) AS n FROM online_eval_queue "
                    "GROUP BY %s" % (col, col)).fetchall():
                d[str(r["k"] or "")] = int(r["n"])
            out[key] = d
        tot, scored, evable = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN auto_score IS NOT NULL "
            "THEN 1 ELSE 0 END), SUM(CASE WHEN message_id > 0 "
            "THEN 1 ELSE 0 END) FROM online_eval_queue").fetchone()
        # 覆盖率分母=**可评样本**（message_id>0）：retrieval/intent 样本的
        # "评分对象"是 query 而非答案，转正走 dataset 不需要生成质量分 ⇒ 不进
        # 分母。否则可评占比一低，覆盖率永远够不着 >80% 验收线（口径失真）。
        out["auto_coverage"] = (scored / evable) if evable else None
        out["evaluable"] = int(evable or 0)
    except Exception as e:                                     # noqa: BLE001
        out["ok"] = False
        out["note"] = "%s: %s" % (type(e).__name__, e)
    return out


def online_eval_metrics(conn) -> dict:
    """告警指标（S5 进 alert_evaluator.GLOBAL_METRICS；F 段断言白名单制）。

    三个指标与 R12/R13/R14 一一对应：积压 / 7 日转正 / 自动评覆盖率。
    """
    out = {"eval_backlog_pending": 0, "eval_promoted_7d": 0,
           "eval_auto_coverage": None}
    if not _tbl_exists(conn, "online_eval_queue"):
        return out
    try:
        out["eval_backlog_pending"] = conn.execute(
            "SELECT COUNT(*) FROM online_eval_queue "
            "WHERE status='pending'").fetchone()[0]
        out["eval_promoted_7d"] = conn.execute(
            "SELECT COUNT(*) FROM online_eval_queue "
            "WHERE status='labeled_confirmed' AND labeled_at >= "
            "datetime('now', '-7 day')").fetchone()[0]
        tot, scored, evable = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN auto_score IS NOT NULL "
            "THEN 1 ELSE 0 END), SUM(CASE WHEN message_id > 0 "
            "THEN 1 ELSE 0 END) FROM online_eval_queue").fetchone()
        out["eval_auto_coverage"] = round(scored / evable, 4) if evable else None
    except Exception:
        pass    # 供数失败返上默认值（0/None），由 alert_evaluator 决定是否告警
    return out


def queue_rows(conn, status: str = "pending", limit: int = 50) -> tuple:
    """队列列表（routers 层经此取数，不直连 SQL——layering L1 卡 routers 直连）。

    返回 (items, total)。老库无表 ⇒ ([], 0)。
    """
    if not _tbl_exists(conn, "online_eval_queue"):
        return [], 0
    conds, args = [], []
    if status:
        conds.append("status=?")
        args.append(status)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM online_eval_queue%s" % where, args).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM online_eval_queue%s ORDER BY id LIMIT ?" % where,
        (*args, int(limit))).fetchall()]
    return rows, int(total)


def get_queue_row(conn, queue_id: int):
    """单行样本（label 端点存在性校验 + conversation_id 回查用）。"""
    if not _tbl_exists(conn, "online_eval_queue"):
        return None
    row = conn.execute("SELECT * FROM online_eval_queue WHERE id=?",
                       (int(queue_id),)).fetchone()
    return dict(row) if row else None


# ── S4 转正：labeled_confirmed → 三分流（唯一真写，幂等）────────────────────
# 三条纪律（设计 §5 B 段，门禁 B1/B3 逐条断言）：
#   ① auto 只进建议：pending/auto_score 样本**永不**自动转正，只认人工
#      labeled_confirmed（"自动打分永不自动转正"——否则 LLM 自循环）；
#   ② labeled_rejected 不进任何 dataset（负样本本身不进训练集）；
#   ③ 幂等：同 queue_id 二次 apply 零增量（三路各自查重，见各分支注释）。
_QRELS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "eval", "qrels.jsonl")


def _query_of(conn, r: dict) -> tuple:
    """样本 → (query, intent)。回查链：query_trace → 同会话上一条 user 消息。

    取不到返 ("","")——转正时留空比编造好（编造的 query 会污染 dataset，
    而空 query 在下游评测里显性失败，可追查）。
    """
    qtid = int(r.get("query_trace_id") or 0)
    if qtid:
        row = conn.execute(
            "SELECT query, intent FROM query_trace WHERE id=?",
            (qtid,)).fetchone()
        if row:
            row = dict(row)
            return (row.get("query") or ""), (row.get("intent") or "")
    cid = int(r.get("conversation_id") or 0)
    if cid:
        row = conn.execute(
            "SELECT content FROM messages WHERE conversation_id=? "
            "AND role='user' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        if row:
            return (dict(row).get("content") or "")[:200], ""
    return "", ""


def _promote_intent(conn, r: dict, out: dict, failures: list) -> None:
    """intent → intent_samples（status='confirmed', source='prod'）。

    幂等靠 text UNIQUE（同 query 只应有一行）；queue_id 累积进 note，
    二次 apply 时 note 不变（不重复追加）⇒ 行数与内容双幂等。
    """
    query, intent = _query_of(conn, r)
    if not query:
        failures.append("promote intent qid=%s: query 回查为空，跳过（不编造）"
                        % r["id"])
        return
    qid = int(r["id"])
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO intent_samples "
            "(text, intent, status, source, note, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (query, intent or "unknown", "confirmed", "prod",
             "online_eval_queue#%d" % qid, "online-eval"))
        if cur.rowcount:
            out["intent_samples"] += 1
        else:
            # 已存在 ⇒ 幂等命中；补溯源 note（仅当未含该 queue_id）
            row = conn.execute(
                "SELECT note FROM intent_samples WHERE text=?", (query,)).fetchone()
            note = (dict(row).get("note") or "") if row else ""
            if ("queue#%d" % qid) not in note and ("#%d" % qid) not in note:
                conn.execute(
                    "UPDATE intent_samples SET note=? WHERE text=?",
                    ((note + ",online_eval_queue#%d" % qid).strip(","), query))
    except Exception as e:                                      # noqa: BLE001
        failures.append("promote intent qid=%d: %s: %s"
                        % (qid, type(e).__name__, str(e)[:160]))


def _promote_retrieval(conn, r: dict, out: dict, failures: list) -> None:
    """retrieval → qrels.jsonl append（human_confirmed=true, queue_id 溯源）。

    幂等：append 前全文读一遍查 queue_id（qrels 无主键，只能文件级查重）。
    relevant 留空 + note 明示"相关集待人工补"——标注确认卡只评了"这条检索
    被人工认可"，**没有**指出哪些 chunk 相关；留 [] 是诚实，编 relevant
    会让 recall 虚高（口径纪律：没测到 ≠ 测到 0）。
    """
    query, _ = _query_of(conn, r)
    qid = int(r["id"])
    if not query:
        failures.append("promote retrieval qid=%d: query 回查为空，跳过" % qid)
        return
    try:
        existed = False
        if os.path.exists(_QRELS_PATH):
            with open(_QRELS_PATH, encoding="utf-8") as f:
                for line in f:
                    if '"queue_id": %d' % qid in line or \
                            '"queue_id":%d' % qid in line:
                        existed = True
                        break
        if existed:
            return
        rec = {"query": query, "relevant": [], "human_confirmed": True,
               "queue_id": qid,
               "conversation_id": int(r.get("conversation_id") or 0),
               "note": "生产回流人工确认；相关集待人工补（标注卡未指定 chunk）"}
        with open(_QRELS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out["qrels"] += 1
    except Exception as e:                                      # noqa: BLE001
        failures.append("promote retrieval qid=%d: %s: %s"
                        % (qid, type(e).__name__, str(e)[:160]))


def _promote_generation(conn, r: dict, out: dict, failures: list) -> None:
    """generation → eval_gen_labels INSERT。

    幂等：queue_id 查重（S1 建表时 ix_egl_queue 索引就是为这个）。
    faithfulness/citation_ok 留 NULL：确认卡只有总分+原因，没有分维度打分；
    写 0 会把"没问这一维"伪装成"这一维零分"（模块头纪律③）。
    """
    qid = int(r["id"])
    try:
        if conn.execute("SELECT 1 FROM eval_gen_labels WHERE queue_id=?",
                        (qid,)).fetchone():
            return
        conn.execute(
            "INSERT INTO eval_gen_labels "
            "(queue_id, message_id, conversation_id, human_score, reason, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (qid, int(r.get("message_id") or 0),
             int(r.get("conversation_id") or 0),
             int(r["human_score"]) if r.get("human_score") else None,
             str(r.get("human_reason") or "")[:500], "online-eval"))
        out["eval_gen_labels"] += 1
    except Exception as e:                                      # noqa: BLE001
        failures.append("promote generation qid=%d: %s: %s"
                        % (qid, type(e).__name__, str(e)[:160]))


def promote_confirmed(conn, *, apply: bool = False) -> dict:
    """转正适配器：labeled_confirmed 样本按 sample_type 三分流进 dataset。

    dry-run 默认（apply=False 只返预判不写库）；apply=True 是**唯一真写**。
    转正后样本 status 保持 labeled_confirmed（不回 pending——已转正事实
    由三侧 dataset 行数自证，改状态反而丢"待复查"语义）。
    """
    out = {"ok": True, "dry_run": not apply, "scanned": 0,
           "intent_samples": 0, "qrels": 0, "eval_gen_labels": 0,
           "skipped": 0, "failures": [], "items": []}
    if not _tbl_exists(conn, "online_eval_queue"):
        out["ok"] = False
        out["error"] = "online_eval_queue 表不存在（迁移未执行）"
        return out
    rows = conn.execute(
        "SELECT * FROM online_eval_queue WHERE status='labeled_confirmed' "
        "ORDER BY id").fetchall()
    out["scanned"] = len(rows)
    for r in rows:
        r = dict(r)
        st = str(r.get("sample_type") or "")
        if not apply:
            out["items"].append({"queue_id": int(r["id"]), "sample_type": st,
                                 "would_write": {"intent": "intent_samples",
                                                 "retrieval": "qrels.jsonl",
                                                 "generation": "eval_gen_labels"}
                                 .get(st, "(未知类型，跳过)")})
            continue
        if st == "intent":
            _promote_intent(conn, r, out, out["failures"])
        elif st == "retrieval":
            _promote_retrieval(conn, r, out, out["failures"])
        elif st == "generation":
            _promote_generation(conn, r, out, out["failures"])
        else:
            out["skipped"] += 1
            out["failures"].append("promote qid=%d: 未知 sample_type=%r"
                                   % (int(r["id"]), st))
    if apply:
        conn.commit()
    return out


# ── S4 人工标注（HIL 确认卡 action='eval_label' 的执行体）───────────────────
# 双入口共用同一执行函数（单一事实源）：
#   · POST /api/monitor/online-eval/label（待标注卡片一键提交+批准）
#   · /api/studio/hil-confirmations/{cid}/decide 的 eval_label 分支
#     （用户在 HIL 页面处置同一张确认单）
# 两路都经 HILService.queue_confirmation 留痕（幂等键防重复标注）。
def apply_human_label(conn, queue_id: int, *, approve: bool, score: int = 0,
                      reason: str = "", actor: str = "") -> dict:
    """把人工标注写进 online_eval_queue（status → labeled_confirmed/rejected）。

    approve=True  ⇒ labeled_confirmed（后续 promote_confirmed 才会转正）
    approve=False ⇒ labeled_rejected（B3：永不进 dataset，状态留痕可复查）
    已标注样本重复提交 ⇒ 幂等返回现状，不覆盖（防看板"标注结果漂移"）。
    """
    out = {"ok": False, "queue_id": int(queue_id), "status": "", "idempotent": False}
    if not _tbl_exists(conn, "online_eval_queue"):
        out["error"] = "online_eval_queue 表不存在（迁移未执行）"
        return out
    row = conn.execute("SELECT * FROM online_eval_queue WHERE id=?",
                       (int(queue_id),)).fetchone()
    if not row:
        out["error"] = "queue_id=%d 不存在" % int(queue_id)
        return out
    row = dict(row)
    cur = str(row.get("status") or "")
    if cur in ("labeled_confirmed", "labeled_rejected"):
        out["ok"] = True
        out["idempotent"] = True
        out["status"] = cur
        out["note"] = "已标注过，保留原标注（不覆盖）"
        return out
    new_status = "labeled_confirmed" if approve else "labeled_rejected"
    hs = int(score) if score else None
    if hs is not None and not (1 <= hs <= 5):
        out["error"] = "score 须在 1-5（当前 %r）" % score
        return out
    try:
        conn.execute(
            "UPDATE online_eval_queue SET status=?, human_score=?, "
            "human_reason=?, labeled_by=?, labeled_at=? WHERE id=?",
            (new_status, hs, str(reason or "")[:500],
             str(actor or "")[:80] or "unknown",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(queue_id)))
        conn.commit()
        out.update({"ok": True, "status": new_status,
                    "sample_type": str(row.get("sample_type") or "")})
    except Exception as e:                                      # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    return out
