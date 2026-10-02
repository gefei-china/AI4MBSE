# -*- coding: utf-8 -*-
"""LLM 调用健康度与配置契约的**口径唯一来源**（P0-a / P0-b，2026-10-02）。

## 为什么要有这个模块

P1-27 是**手工**按 intent 聚合 `MAX(completion_tokens)` 才发现 4 处输出上限不足
（主对话 / plan_refine / plan_summary / conv_summary 各自触顶）。手工反推的问题不是
"慢"，而是**只能发现问题、无法发现回归**：这次调好了，下次改配置又配错，没有任何东西会叫。

本模块把那次手工分析固化成可重复执行的能力，对外只有一个入口：`health_report()`。

## 设计纪律（与 `metrics_core.py` 同族）

1. **纯函数与取数分离**：聚合逻辑（`aggregate_intent_health`）只吃 rows，不碰 DB
   —— 自检脚本因此可以精确构造边界样本，而不必"在测试里重算一遍指标"。
2. **数据不足必须报 `unknown`，不许报 `ok`**。`finish_reason` 在本模块启用时
   （2026-10-02 之前）在主路径上恒为空 —— 此时"截断率 0%"是**假绿**，
   真实含义是"无从判断"。这条铁律同样适用于新增指标。
3. **判据阈值集中在模块顶部**，是自检脚本变异测试的锚点；改阈值 = 改行为，必须同步跑自检。
"""
import json

# ══════════════════════════════════════════════════════════════════════════════
# 判据阈值（**唯一真源**；`tools/verify/verify_llm_health.py` 的变异直接改这里）
# ══════════════════════════════════════════════════════════════════════════════

TRUNCATION_REASON = "length"     # 截断判据：finish_reason 为该值 = 输出被 max_tokens 切断
REASONING_EATEN_RATIO = 0.9      # 「正文被推理吃光」：reasoning 占 completion 的比例下限
NEAR_LIMIT_RATIO = 0.95          # 「贴近上限」：max(completion) 达该 intent 上限的此比例
TRUNCATION_WARN = 0.10           # 截断率告警线（≥ 记 warn）
TRUNCATION_ALERT = 0.30          # 截断率严重线（≥ 记 alert）
REASONING_SHARE_WARN = 0.70      # 推理占比告警线（据此判断"该环节值得考虑非推理模型"）
MOCK_WARN = 0.20                 # Mock 率告警线（Mock 数据不能用于质量判断）
CACHE_LOW_WARN = 0.10            # 缓存命中率偏低线（长前缀场景下低于此值说明前缀被破坏）
FINISH_COVERAGE_MIN = 0.5        # 判据覆盖率下限：低于此值总状态不敢报 ok

# ── 「疑似触顶」的兜底判据（仅当 finish_reason 无样本时生效）──────────────────────
# 依据：P1-27 手工按 intent 聚合 `MAX(completion_tokens)` 时，一眼就能认出触顶 —— 因为
# 那些最大值**恰好等于某个"人配的上限值"**（conv_summary=301≈300、plan_summary=8000、
# plan_refine=8000、主对话=16384）。这些数字不是模型的自然产出长度，而是配置的指纹。
# ⚠️ 这是**经验判据**，故只报 warn 且措辞为"疑似"；有 finish_reason 样本时一律用真判据。
COMMON_LIMIT_VALUES = (300, 512, 1024, 2048, 3072, 4096, 8000, 8192, 16384, 32768)


# ══════════════════════════════════════════════════════════════════════════════
# 各环节输出上限的来源（**登记制**）
# ══════════════════════════════════════════════════════════════════════════════
# 只有"调用点自己显式传 max_tokens"的环节才需要登记（它们**不受 provider 上限约束**，
# 巡检必须知道真实上限才能判断触顶）；未登记的 intent 一律按 DB provider 的 max_tokens 判。
# ⚠️ 新增调用点显式传 max_tokens 时必须在此登记；否则该 intent 只报绝对值、不报触顶
#    （**不报 ≠ 正常** —— 这是本模块最容易悄悄退化的地方）。
EXPLICIT_INTENT_LIMITS = {
    "plan_refine":  ("refine", "max_tokens"),                  # workflows/refine.py
    "plan_summary": ("delegation", "summary_max_tokens"),       # workflows/planner.py
    "plan_eval":    ("refine", "eval_max_tokens"),              # workflows/nodes.py
    "conv_summary": ("conv_summary", "max_tokens"),             # services/conv_summary.py
}


def _pct(vals, p: float):
    """分位（Python 内算：SQLite 无 percentile 函数，窗口+别名写法极易错）。"""
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * p))]


def resolve_intent_limits(conn) -> dict:
    """解析 `intent → 输出上限(token)`。

    两级口径：
      ① 显式登记（`EXPLICIT_INTENT_LIMITS`）→ 读 `core.config` 的当前生效值；
      ② 其余 intent → 取该 intent **实际使用最多** 的 provider 的 DB `max_tokens`。
    `None` 表示无从判断（不报触顶，也不报正常）。
    """
    out: dict = {}
    try:
        from core import config as _cfg
        for intent, (sec, key) in EXPLICIT_INTENT_LIMITS.items():
            try:
                out[intent] = int(_cfg.get(sec, key, 0) or 0) or None
            except Exception:
                out[intent] = None
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT intent, provider_id, COUNT(*) n FROM llm_usage_stats "
            "WHERE used_mock=0 GROUP BY intent, provider_id").fetchall()
        best: dict = {}
        for r in rows:
            it = r["intent"] or ""
            if it in out and out[it]:
                continue
            if r["n"] > best.get(it, (0, 0))[0]:
                best[it] = (r["n"], r["provider_id"] or 0)
        for it, (_n, pid) in best.items():
            if out.get(it):
                continue
            row = conn.execute("SELECT max_tokens FROM llm_providers WHERE id=?",
                               (pid,)).fetchone()
            if row:
                out[it] = int(row["max_tokens"] or 0) or None
    except Exception:
        pass
    return out


def aggregate_intent_health(rows, limits=None) -> list:
    """把 `llm_usage_stats` 行聚合成 per-intent 健康度（**纯函数**，无 DB 依赖）。

    rows: dict 列表，需含 intent / used_mock / completion_tokens / reasoning_tokens /
          finish_reason / prompt_cache_hit_tokens / prompt_cache_miss_tokens / estimated_cost。
    limits: `{intent: max_tokens}`；缺失 → 该 intent 的 `limit=None`（不判触顶）。

    返回按 calls 降序的列表；每项含 `status` + `issues` + `hint`（人类可读，供看板/CLI 直出）。
    """
    limits = limits or {}
    buckets: dict = {}
    for r in (rows or []):
        it = (r.get("intent") or "").strip()
        b = buckets.setdefault(it, {"calls": 0, "mock": 0, "ct": [], "sum_ct": 0, "sum_rt": 0,
                                    "trunc": 0, "fr_known": 0, "eaten": 0,
                                    "hit": 0, "miss": 0, "cost": 0.0})
        b["calls"] += 1
        _mock = int(r.get("used_mock") or 0)
        b["mock"] += 1 if _mock else 0
        ct = int(r.get("completion_tokens") or 0)
        rt = int(r.get("reasoning_tokens") or 0)
        b["ct"].append(ct)
        b["sum_ct"] += ct
        b["sum_rt"] += rt
        b["hit"] += int(r.get("prompt_cache_hit_tokens") or 0)
        b["miss"] += int(r.get("prompt_cache_miss_tokens") or 0)
        b["cost"] += float(r.get("estimated_cost") or 0)
        fr = str(r.get("finish_reason") or "").strip()
        if fr:
            b["fr_known"] += 1
            if fr == TRUNCATION_REASON:
                b["trunc"] += 1
                # 「正文被推理吃光」：被截断，且推理几乎吃满配额 ⇒ 用户实际拿到空回复。
                # 这正是 P1-26 实测 8 次空输出的形态（reasoning=8192 / content=0 / length）。
                if ct > 0 and rt * 1.0 / ct >= REASONING_EATEN_RATIO:
                    b["eaten"] += 1

    out = []
    for it, b in buckets.items():
        n = b["calls"]
        real = n - b["mock"]
        _ct = b["ct"]
        mx = max(_ct) if _ct else 0
        limit = limits.get(it)
        rate = (b["trunc"] / b["fr_known"]) if b["fr_known"] else None
        r_share = (b["sum_rt"] / b["sum_ct"]) if b["sum_ct"] else None
        cache_total = b["hit"] + b["miss"]
        cache_rate = (b["hit"] / cache_total) if cache_total else None
        near = bool(limit and mx >= limit * NEAR_LIMIT_RATIO)

        issues, status = [], "ok"
        if real <= 0:
            status = "unknown"
            issues.append("无真实调用样本（全部 Mock/无记录），任何质量结论都不成立")
        else:
            if b["eaten"] > 0:
                status = "alert"
                issues.append("%d 次「正文被推理吃光」（finish=%s 且 reasoning 占 completion ≥%.0f%%）"
                              "→ 用户拿到的是空回复"
                              % (b["eaten"], TRUNCATION_REASON, REASONING_EATEN_RATIO * 100))
            if rate is not None and rate >= TRUNCATION_ALERT:
                status = "alert"
                issues.append("截断率 %.1f%%（%d/%d）超严重线 %.0f%%"
                              % (rate * 100, b["trunc"], b["fr_known"], TRUNCATION_ALERT * 100))
            elif rate is not None and rate >= TRUNCATION_WARN:
                if status != "alert":
                    status = "warn"
                issues.append("截断率 %.1f%%（%d/%d）超告警线 %.0f%%"
                              % (rate * 100, b["trunc"], b["fr_known"], TRUNCATION_WARN * 100))
            if b["fr_known"] == 0:
                # 铁律：数据不足报 unknown，不报 ok（"截断率 0%" 在这里是假绿）。
                # 但 `max_completion` 恰为某个"人配的上限值"时给出**疑似触顶**的 warn
                # （P1-27 手工发现 4 处上限不足，靠的就是这个指纹）。
                # 容差 +2：上游用量计数比请求上限可能多 1~2（实测 conv_summary 请求 300、落库 301）。
                _suspect = 0
                for _v in COMMON_LIMIT_VALUES:
                    if _v <= mx <= _v + 2:
                        _suspect = _v
                        break
                if _suspect:
                    if status == "ok":
                        status = "warn"
                    issues.append("疑似触顶：max(completion)=%d 贴近常见配置上限 %d，但无 finish_reason "
                                  "佐证 → 请核对该环节的 max_tokens 配置" % (mx, _suspect))
                else:
                    if status == "ok":
                        status = "unknown"
                issues.append("finish_reason 无样本 → **无法判断是否截断**（不是「没有截断」）")
            elif near and rate is not None and rate == 0:
                if status == "ok":
                    status = "warn"
                issues.append("max(completion)=%d 已达上限 %d 的 %.0f%%（本次未截断，但余量已尽）"
                              % (mx, limit, NEAR_LIMIT_RATIO * 100))
            if r_share is not None and r_share >= REASONING_SHARE_WARN:
                issues.append("推理占比 %.0f%%（推理与正文共享 max_tokens，是成本与截断的主要来源）"
                              % (r_share * 100))
                if status == "ok":
                    status = "warn"
            if b["mock"] and b["mock"] / n >= MOCK_WARN:
                issues.append("Mock 率 %.0f%%（%d/%d）：Mock 数据不能用于质量判断"
                              % (b["mock"] / n * 100, b["mock"], n))
                if status == "ok":
                    status = "warn"

        hint = ""
        if limit and (near or (rate is not None and rate >= TRUNCATION_WARN)):
            hint = "提高该环节上限，或改用非推理模型（reasoning 与正文共享配额）"
        out.append({
            "intent": it or "(未标注)",
            "calls": n, "real_calls": real, "mock_calls": b["mock"],
            "mock_rate": round(b["mock"] / n, 4) if n else None,
            "limit": limit,
            "max_completion": mx, "p95_completion": _pct(_ct, 0.95), "sum_completion": b["sum_ct"],
            "near_limit": near,
            "truncated": b["trunc"], "finish_known": b["fr_known"],
            "truncation_rate": round(rate, 4) if rate is not None else None,
            "reasoning_tokens": b["sum_rt"],
            "reasoning_share": round(r_share, 4) if r_share is not None else None,
            "reasoning_eaten": b["eaten"],
            "cache_hit_tokens": b["hit"], "cache_miss_tokens": b["miss"],
            "cache_hit_rate": round(cache_rate, 4) if cache_rate is not None else None,
            "est_cost": round(b["cost"], 6),
            "status": status, "issues": issues, "hint": hint,
        })
    out.sort(key=lambda x: (-x["calls"], x["intent"]))
    return out


def config_contract(conn) -> dict:
    """P0-b 配置契约：`llm_providers` 的 `max_tokens` / `context_window` 自洽性。

    实据（P1-27）：一刀切把 max_tokens 提到 32768 时，id=103 的 `context_window` 只有
    8192 → 若不回退就会 `max > ctx`，被 `openai_compat` 的守卫**静默夹到 8192**
    （于是"配置里写着 32768、实际只有 8192"，是最难查的一类配置谎言）。
    DB 是可视化配置写入的，**没有任何东西拦住这类写入** —— 本函数就是那道检查。
    """
    violations, checked = [], 0
    try:
        rows = conn.execute(
            "SELECT id, name, model_name, model_type, max_tokens, context_window, status "
            "FROM llm_providers ORDER BY id").fetchall()
    except Exception as e:
        return {"ok": False, "checked": 0, "violations": [
            {"level": "alert", "reason": "无法读取 llm_providers: %s" % e}]}
    for r in rows:
        if str(r["status"] or "") not in ("active", "enabled", ""):
            continue
        checked += 1
        mt = int(r["max_tokens"] or 0)
        cw = int(r["context_window"] or 0)
        base = {"provider_id": r["id"], "name": r["name"], "model_name": r["model_name"],
                "model_type": r["model_type"], "max_tokens": mt, "context_window": cw}
        if mt <= 0:
            violations.append({**base, "level": "alert",
                               "reason": "max_tokens 非正数 → 请求会用到实现内置默认值，配置形同虚设"})
        if cw <= 0:
            violations.append({**base, "level": "alert",
                               "reason": "context_window 非正数 → 守卫算不出余量，超窗留痕会失真"})
        if mt > 0 and cw > 0 and mt > cw:
            violations.append({**base, "level": "alert",
                               "reason": "max_tokens > context_window → 实际输出上限被静默夹到 "
                                         "%d（配置写的 %d 不生效）" % (cw, mt)})
    return {"ok": not violations, "checked": checked, "violations": violations}


def health_report(conn, days: int = 7) -> dict:
    """健康度巡检总入口（看板 / CLI / 自检共用）。

    返回 `{days, summary, intents, contract}`。`summary` 里的 `status` 取各项最差：
    任一 alert → alert；任一 warn → warn；全部 ok/unknown 且**至少一项判据可用** → ok；
    全部 unknown → unknown（**不许在无数据时报 ok**）。
    """
    rows = []
    try:
        for r in conn.execute(
                "SELECT intent, used_mock, completion_tokens, reasoning_tokens, finish_reason, "
                "prompt_cache_hit_tokens, prompt_cache_miss_tokens, estimated_cost "
                "FROM llm_usage_stats WHERE created_at >= datetime('now', ?)",
                ("-%d days" % max(int(days or 0), 1),)).fetchall():
            rows.append(dict(r))
    except Exception:
        rows = []
    intents = aggregate_intent_health(rows, resolve_intent_limits(conn))
    contract = config_contract(conn)

    judged = [i for i in intents if i["status"] in ("ok", "warn", "alert")]
    _fr_known = sum(i["finish_known"] for i in intents)
    _calls = sum(i["calls"] for i in intents)
    coverage = (_fr_known / _calls) if _calls else None
    if contract["violations"]:
        status = "alert"
    elif any(i["status"] == "alert" for i in intents):
        status = "alert"
    elif any(i["status"] == "warn" for i in intents):
        status = "warn"
    elif not judged:
        status = "unknown"
    elif coverage is not None and coverage < FINISH_COVERAGE_MIN:
        # 判据覆盖率不足 → **不敢报 ok**。这一条防的正是"看板全绿、其实什么都测不到"的假绿。
        status = "unknown"
    else:
        status = "ok"

    total_calls = sum(i["calls"] for i in intents)
    total_trunc = sum(i["truncated"] for i in intents)
    total_ct = sum(i["sum_completion"] for i in intents)
    total_rt = sum(i["reasoning_tokens"] for i in intents)
    total_hit = sum(i["cache_hit_tokens"] for i in intents)
    total_miss = sum(i["cache_miss_tokens"] for i in intents)
    total_mock = sum(i["mock_calls"] for i in intents)
    return {
        "days": days,
        "status": status,
        "summary": {
            "calls": total_calls,
            "real_calls": total_calls - total_mock,
            "mock_calls": total_mock,
            "mock_rate": round(total_mock / total_calls, 4) if total_calls else None,
            "truncated": total_trunc,
            "reasoning_eaten": sum(i["reasoning_eaten"] for i in intents),
            "finish_known_calls": _fr_known,
            "finish_coverage": round(coverage, 4) if coverage is not None else None,
            "completion_tokens": total_ct,
            "reasoning_tokens": total_rt,
            "reasoning_share": round(total_rt / total_ct, 4) if total_ct else None,
            "cache_hit_tokens": total_hit,
            "cache_miss_tokens": total_miss,
            "cache_hit_rate": round(total_hit / (total_hit + total_miss), 4) if (total_hit + total_miss) else None,
            "est_cost": round(sum(i["est_cost"] for i in intents), 6),
            "intents": len(intents),
            "intents_alert": sum(1 for i in intents if i["status"] == "alert"),
            "intents_warn": sum(1 for i in intents if i["status"] == "warn"),
            "intents_unknown": sum(1 for i in intents if i["status"] == "unknown"),
        },
        "intents": intents,
        "contract": contract,
    }


def format_report(rep: dict, only_problem: bool = False) -> str:
    """把 `health_report` 结果渲染成可粘贴的纯文本表（CLI 与自检共用，避免两处各写一份）。"""
    s = rep.get("summary") or {}
    lines = ["LLM 调用健康度巡检（近 %s 天）｜总体状态：%s" % (rep.get("days"), rep.get("status", "").upper()),
             "  调用 %s（真实 %s / Mock %s，Mock 率 %s）"
             % (s.get("calls"), s.get("real_calls"), s.get("mock_calls"), _fmt_pct(s.get("mock_rate"))),
             "  截断 %s 次｜正文被推理吃光 %s 次｜推理占比 %s｜缓存命中率 %s"
             % (s.get("truncated"), s.get("reasoning_eaten"),
                _fmt_pct(s.get("reasoning_share")), _fmt_pct(s.get("cache_hit_rate"))),
             "  判据覆盖率（有 finish_reason 的调用占比）%s（%s/%s）%s"
             % (_fmt_pct(s.get("finish_coverage")), s.get("finish_known_calls"), s.get("calls"),
                "" if (s.get("finish_coverage") or 0) >= FINISH_COVERAGE_MIN
                else "  ⚠️ 不足 %.0f%% → 截断率类判据不可信，总状态不报 ok" % (FINISH_COVERAGE_MIN * 100)),
             "  估算成本 $%s" % s.get("est_cost"),
             ""]
    head = ("%-22s %7s %9s %11s %8s %8s %8s  %s"
            % ("intent", "调用", "max_ct", "上限", "截断率", "推理占比", "缓存", "状态"))
    lines.append(head)
    lines.append("-" * len(head))
    for i in rep.get("intents") or []:
        if only_problem and i["status"] in ("ok",):
            continue
        lines.append("%-22s %7d %9s %11s %8s %8s %8s  %s"
                     % (i["intent"][:22], i["calls"], i["max_completion"],
                        i["limit"] if i["limit"] else "-",
                        _fmt_pct(i["truncation_rate"]), _fmt_pct(i["reasoning_share"]),
                        _fmt_pct(i["cache_hit_rate"]), i["status"]))
        for _is in i["issues"]:
            lines.append("      · %s" % _is)
    c = rep.get("contract") or {}
    lines.append("")
    lines.append("配置契约（llm_providers max_tokens ≤ context_window）：%s（检查 %s 行）"
                 % ("通过" if c.get("ok") else "**违规 %d 项**" % len(c.get("violations") or []),
                    c.get("checked")))
    for v in (c.get("violations") or []):
        lines.append("      · [%s] id=%s %s：%s" % (v.get("level"), v.get("provider_id"),
                                                 v.get("name"), v.get("reason")))
    return "\n".join(lines)


def _fmt_pct(v) -> str:
    return "-" if v is None else "%.1f%%" % (v * 100)


def to_json(rep: dict) -> str:
    return json.dumps(rep, ensure_ascii=False, indent=2)
