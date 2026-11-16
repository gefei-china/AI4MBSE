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
    ① 2026-10-25：设计文档 §7 的 low_conf_threshold 默认 0.5 是**拍脑袋值**——
      对 mbse.db 只读实测：query_trace.top_score 全域量级 [0, 0.096]，0.5 会命中
      全表 3276 行，故改 0.036（=p25）。
    ② 2026-11-08：**0.036 本身也是错的**，因为"全域量级"这个说法掩盖了
      两种不同量纲混在同一列。按 route 分组重测：
          vector  n=6514  [0, 0.632]  → 0.036 命中 20.0%
          mixed   n=27    [0, 0.0]    → 0.036 命中 100%（这 27 行是 bug 写出的 0）
      结论：绝对阈值这条路走不通，改为 **按 score_kind 分组取组内分位**
      （low_conf_quantile，默认 0.25），并用**组内相对位次**跨组混排。
      这样跨量纲不需要可比，也不维护任何魔法数字。
"""
import json
import math
import os
import random
import sqlite3
from datetime import datetime

# 反馈词表单一真源（2026-10-29）：负反馈类型**不在本模块另存一份**
#（原先本模块与 core/badcase.py 各存一份 NEGATIVE 词表，两份必然漂移）。
from core.feedback_types import (
    NEGATIVE_BEHAVIOR_EVENTS,
    NEGATIVE_QUALITY,
    NEGATIVE_REVIEW_TYPES,
)

# ── 配置（core.config eval_online 组；禁模块内常量旁路，默认值仅兜底）──
_SETTING_DEFAULTS = {
    "enabled": True,               # 总开关（False 时采样返空而非 500）
    "random_rate": 0.05,           # 随机采样比例（负反馈/低置信优先不受此限）
    # ★ 2026-11-08 废弃：`low_conf_threshold`（绝对值阈值）不再参与低置信判断。
    #   原因：`query_trace.top_score` 的量纲**随 route 变**（图置信度 0~1，
    #   融合检索分实测 0~0.63），一个绝对阈值同时套两种分布必然只在其中一种上准。
    #   实证：0.036 在 vector 上命中 20%，而 mixed 路因旧 bug 全为 0.0 ⇒ 100% 命中
    #   且恒占 ORDER BY 榜首。键保留只为兼容既有配置文件，读它不会再有效果。
    "low_conf_threshold": 0.036,   # @deprecated 用 low_conf_quantile 替代
    "low_conf_quantile": 0.25,     # 各组内取最低的 q 比例（组内分位，跨组不比较绝对值）
    "sample_window_days": 30,      # 采样窗口（同 badcase：只扫最近 N 天）
    "sample_batch": 20,            # 单次采样入队上限（防低置信单源撑爆队列）
    "auto_eval_limit": 50,         # 单次自动评批量
    "backlog_alert": 20,           # R12 阈值（权威值在 seed_registry._ALERT_RULES，此处仅默认）
    "promote_window_days": 7,      # R13 统计窗口
}

#: 参与"低置信"判断的分数种类。两类被**刻意排除**：
#:   `''`          —— 本次没有可用分数（≠ 0 分）。当 0 分采会污染判断。
#:   `legacy_zero` —— 旧 bug 写出的 0（图路从实体对象上取不存在的 score 字段），
#:                    0 在这里的含义是"没取到"，当"最低分"采会永久霸榜。
_LOW_CONF_KINDS = ("graph_conf", "chunk_score", "vec_score", "legacy_fusion")

#: 负反馈信号源一：ux_metrics 事件白名单中的负向事件（repositories/ux_metrics_repo.EVENTS）
_UX_NEG_EVENTS = NEGATIVE_BEHAVIOR_EVENTS
#: 负反馈信号源二：feedback 表负向类型（与 core/badcase._SQL_FEEDBACK 同口径，
#: 不造第二份词表——两份必然漂移）
#: ⚠️ 2026-10-29：改为从单一真源 `core/feedback_types.py` 取，并**新增质量通道**
#:   （quality='down'，用户点踩）。此前只有评审侧负反馈，而前端根本没有能产生
#:   `modify/wrong/corrected` 的入口 ⇒ 这条路常年采不到东西（队列 0 行）。
_FB_NEG_TYPES = NEGATIVE_REVIEW_TYPES


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
                "SELECT id, message_id, conversation_id, feedback_type, quality, context "
                "FROM feedback WHERE (feedback_type IN (%s) OR COALESCE(quality,'') IN (%s)) "
                "AND created_at >= datetime('now', ?) ORDER BY id DESC"
                % (",".join("?" * len(_FB_NEG_TYPES)), ",".join("?" * len(NEGATIVE_QUALITY))),
                (*_FB_NEG_TYPES, *NEGATIVE_QUALITY, window_arg[0])).fetchall()
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
                        int(r["id"]), r.get("quality") or r.get("feedback_type") or ""),
                })
        except Exception as e:                                 # noqa: BLE001
            failures.append("neg_feedback(feedback): %s: %s"
                            % (type(e).__name__, str(e)[:160]))
    return out


def _cand_low_conf(conn, window_arg, quantile: float, cap: int,
                   failures: list) -> list:
    """低置信候选（第二优先）：**按 score_kind 分组、组内取最低分位**。

    为什么不再用绝对值阈值（2026-11-08）
    ------------------------------------
    旧实现 `WHERE top_score < 0.036 ORDER BY top_score ASC`，前提是"所有 top_score
    可以互相比较"。但实测这个前提不成立：

    | route  | 分数含义                        | 量纲（实测）       |
    |--------|--------------------------------|-------------------|
    | graph  | `graph_confidence` 多因子加权   | 0 ~ 1（样本 0.96） |
    | vector | 融合检索分（chunk_hits）        | 0 ~ 0.63（样本 0.195） |

    一个 0.036 在两个分布里的**分位完全不同**，所以采出来的是"某个量纲里最低的"，
    而不是"系统最没底的"。更糟的是旧 bug 让 mixed 路的 top_score 恒为 0.0
    ⇒ 那 27 行永远排在榜首，每次采样都采同一批，**采样偏置且看不出来**。

    现在的做法：各组内按分位取，再按**组内相对位次**混排。
    跨组不比较绝对值，只比较"在本组里有多差"—— 这正是"量纲不同不可直接比较"
    的正确处理，且阈值完全由数据分布决定，不维护任何魔法数字。
    """
    if not _tbl_exists(conn, "query_trace"):
        return []
    try:
        q = float(quantile)
    except (TypeError, ValueError):
        q = 0.25
    q = max(0.0, min(1.0, q))
    ph = ",".join(["?"] * len(_LOW_CONF_KINDS))
    picked = []          # [(组内相对位次, 行, kind)]
    try:
        stats = conn.execute(
            "SELECT score_kind AS k, COUNT(*) AS n FROM query_trace "
            "WHERE created_at >= datetime('now', ?) AND score_kind IN (%s) "
            "GROUP BY score_kind" % ph,
            (window_arg[0], *_LOW_CONF_KINDS)).fetchall()
        for st in stats or []:
            kind, n = st["k"], int(st["n"] or 0)
            if n <= 0:
                continue
            take = max(1, int(math.ceil(n * q)))    # 该组"最差"的条数，随组规模伸缩
            limit = min(take, int(cap))
            # 每组取多少条属于"该组最差"，由组自身规模决定；cap 防单组撑爆
            rows = conn.execute(
                "SELECT id, query, intent, route, top_score FROM query_trace "
                "WHERE score_kind=? AND created_at >= datetime('now', ?) "
                "ORDER BY top_score ASC, id ASC LIMIT ?",
                (kind, window_arg[0], limit)).fetchall()
            # ⚠️ 分母必须是**实际取出条数**而非 take：组大时 take 可达上千，
            #   而 limit 只有几十，用 take 做分母会让所有相对位次挤在 0 附近，
            #   "组内最差优先"的排序退化成按 id 排（实测 22 条位次全为 0.00）。
            denom = float(max(1, len(rows or [])))
            for rank, r in enumerate(rows or []):
                # 相对位次 ∈ [0,1)：0 = 本组最差。跨组可比，且不依赖绝对分数。
                picked.append((rank / denom, r, kind))
    except Exception as e:                                     # noqa: BLE001
        failures.append("low_conf: %s: %s" % (type(e).__name__, str(e)[:160]))
        return []
    picked.sort(key=lambda x: (x[0], x[1]["id"]))
    return [{
        "sample_type": "retrieval",
        "conversation_id": 0,
        "message_id": 0,
        "query_trace_id": int(r["id"]),
        "trace_id": "",
        "sample_reason": "low_conf",
        # kind 必须写进 reason：否则看到 `top_score=0.96` 被标成低置信会莫名其妙
        "reason_detail": "top_score=%.4f kind=%s 组内位次=%.2f route=%s" % (
            float(r["top_score"] or 0), kind, rel, r["route"] or "-"),
    } for rel, r, kind in picked]


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
                                  float(cfg.get("low_conf_quantile") or 0.25),
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


def _col_exists(conn, table: str, col: str) -> bool:
    """列存在性探测（老库可能还没跑迁移，直接 SELECT 该列会抛）。"""
    try:
        return any(r[1] == col
                   for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall())
    except Exception:                                          # noqa: BLE001
        return False


def _trace_texts(conn, trace_ids) -> dict:
    """批量取 query_trace 的「用户原话 / 检索串 / 归一化」三件事（2026-11-08）。

    为什么标注面需要这一步：`online_eval_queue` 只存 `query_trace_id` 外键，
    标注员在队列里看到的原本只有 `qt 6885` 这种编号 —— 要看查询就得自己回查
    `query_trace`，而那一列 `query` 存的是**已拼装且已归一化**的串
    （`宽带载荷` → `宽带通信载荷`），拿它当"用户原话"标注等于给样本喂了错的前提。

    返回 {trace_id: {"query":…, "normalized":…, "query_raw":…}}；
    老库/缺列/任何异常 ⇒ 返回已取到的部分，异常走 `observe_fail` 留痕（不静默）。
    """
    ids = sorted({int(i or 0) for i in (trace_ids or [])} - {0})
    if not ids or not _tbl_exists(conn, "query_trace"):
        return {}
    has_raw = _col_exists(conn, "query_trace", "query_raw")
    sel = "id, query, normalized" + (", query_raw" if has_raw else ", NULL AS query_raw")
    out = {}
    try:
        ph = ",".join(["?"] * len(ids))
        for r in conn.execute(
                "SELECT %s FROM query_trace WHERE id IN (%s)" % (sel, ph), ids).fetchall():
            out[int(r["id"])] = {"query": r["query"] or "",
                                 "normalized": r["normalized"] or "",
                                 "query_raw": r["query_raw"]}
    except Exception as e:                                     # noqa: BLE001
        # 读不到就少这一段展示，但必须留痕：否则"标注员看不到原话"会表现成
        # "这些样本本来就没有原话"，与"确实来自 2026-11-08 之前"无法区分。
        try:
            from core.observe_fail import note as _note
            _note("online_eval.trace_texts", e, n=len(ids), has_raw=has_raw)
        except Exception:                                      # noqa: BLE001
            pass
    return out


def queue_rows(conn, status: str = "pending", limit: int = 50) -> tuple:
    """队列列表（routers 层经此取数，不直连 SQL——layering L1 卡 routers 直连）。

    返回 (items, total)。老库无表 ⇒ ([], 0)。

    ★ 2026-11-08：每行附 `trace_query_raw`（用户原话）/ `trace_query`（实际检索串）/
    `trace_normalized`，并给 `trace_raw_missing` 标记。**该标记只表示"这一行写于
    query_raw 列存在之前"，不表示用户没说话** —— 历史行的原话不可恢复（上游早已
    把 kb_hint/槽位/附件片段拼进去并归一化），所以前端必须把它显示成
    「原话未记录」而不是退化成展示归一化后的串去冒充原话。
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
    _texts = _trace_texts(conn, [r.get("query_trace_id") for r in rows])
    for r in rows:
        _t = _texts.get(int(r.get("query_trace_id") or 0))
        if not _t:
            continue
        r["trace_query"] = _t["query"]
        r["trace_normalized"] = _t["normalized"]
        r["trace_query_raw"] = _t["query_raw"]
        # NULL 与空白同判「未记录」：与 `_query_of` 的判据保持一致
        # （两处各写一套判据，迟早出现"这里说有、那里说没有"）。
        r["trace_raw_missing"] = not str(_t["query_raw"] or "").strip()
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
    """样本 → (query, intent, src)。回查链：query_trace → 同会话上一条 user 消息。

    ★ 2026-11-08：**优先取用户原话 `query_raw`**，取不到才退回 `query`。
    为什么这条比展示层更要紧：转正是**唯一真写**，写进 `qrels.jsonl` /
    `intent_samples` 的 query 会成为评测集的"标准问法"。若写的是 `query`
    （= 上游拼过 kb_hint/槽位/附件片段、再被 Glossary 归一化的串），那评测集
    测的就是**另一句话**：用户问「宽带载荷」，集合里存「宽带通信载荷」，日后
    评测刷分好坏都与真实提问无关，且这种失真**在数据里看不出来**。

    src 取值：raw（原话）/ retrieval（历史行，原话不可恢复）/ message（回查消息）/
    ''. 必须随 query 一起返回：下游据此标注"这条样本的问法来源"，否则将来无法
    区分"数据集里本来就是归一化串"和"有人又把链路改回去了"。
    取不到返 ("","",'')——转正时留空比编造好（编造的 query 会污染 dataset，
    而空 query 在下游评测里显性失败，可追查）。
    """
    qtid = int(r.get("query_trace_id") or 0)
    if qtid:
        has_raw = _col_exists(conn, "query_trace", "query_raw")
        sel = "query, intent" + (", query_raw" if has_raw else ", NULL AS query_raw")
        row = conn.execute(
            "SELECT %s FROM query_trace WHERE id=?" % sel, (qtid,)).fetchone()
        if row:
            row = dict(row)
            raw = row.get("query_raw")
            if raw is not None and str(raw).strip():
                return str(raw), (row.get("intent") or ""), "raw"
            return (row.get("query") or ""), (row.get("intent") or ""), "retrieval"
    cid = int(r.get("conversation_id") or 0)
    if cid:
        row = conn.execute(
            "SELECT content FROM messages WHERE conversation_id=? "
            "AND role='user' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        if row:
            return (dict(row).get("content") or "")[:200], "", "message"
    return "", "", ""


def _promote_intent(conn, r: dict, out: dict, failures: list) -> None:
    """intent → intent_samples（status='confirmed', source='prod'）。

    幂等靠 text UNIQUE（同 query 只应有一行）；queue_id 累积进 note，
    二次 apply 时 note 不变（不重复追加）⇒ 行数与内容双幂等。
    """
    query, intent, src = _query_of(conn, r)
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
             # src 随 note 落库：日后要回答"这条样本的问法是原话还是历史归一化串"
             # 只能靠它（intent_samples 没有独立列，也不值得为此加列）。
             "online_eval_queue#%d src=%s" % (qid, src or "none"), "online-eval"))
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
    query, _, src = _query_of(conn, r)
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
        # query_src 与 query 同行落盘：qrels 是评测集的"标准问法"，必须能分辨
        # 它是用户原话（raw）还是历史行的归一化串（retrieval）—— 否则日后
        # 看到"召回率掉了"无法判断是检索退步还是这批样本的问法本来就不同。
        rec = {"query": query, "relevant": [], "human_confirmed": True,
               "queue_id": qid, "query_src": src or "none",
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
