# -*- coding: utf-8 -*-
"""P4 replanning：计划健康度巡检 + 提前收敛（纯函数，2026-10-21）。

**为什么需要**（v2 §4.4，对标差距 G2/G3）：
    P1~P3 的所有触发都是**失败后置**的 —— 任务失败了、用户改主意了，才反应。
    但工程现实里最隐蔽的一类失败是**缓慢漂移**：每一步都"成功"（status=done），
    整体却离 goal 越来越远（每个子任务都自报 partial、结论互相矛盾、剩余时间
    根本跑不完）。CrewAI 每步一次 observer、OpenAI DR 循环内持续评估覆盖度，
    干的都是这件事；v1 完全没有这一层 ⇒ 本模块补上（产出信号 S10）。
    与之配套的是**提前收敛**（early_stop，对标 CrewAI GoalAchievedEarly /
    OpenAI DR 覆盖度早停）："该停不跑"有无进展检测，"够了就停"是另一回事 ——
    必需产出已全部交付时，继续跑可选任务只是烧用户的预算。

**信号口径（v2 §4.4，规则版指标全部可解释 —— 防误报的第一道闸）**：

| 指标 | 计算 | 超阈动作 |
|---|---|---|
| M1 partial 密度 | 近 N 个完成任务中"自报缺东西"（summary.status∈{partial,failed} 或 missing 非空，口径同 S5）占比 ≥ 50% 且 N≥3 | S10 ⇒ T2-light 精修 |
| M2 冲突密度 | detect_conflicts 累计命中数 ≥ 1（无解决跟踪 ⇒ 一律按未解决，保守） | S10 ⇒ T2-light 精修 |
| M3 未验证 claims 密度 | —— | **暂不启用**（真库历史 to_verify=0 条，无基线无法标定，见基线报告 D4） |
| M4 预算偏离 | 剩余任务数 × 滑动窗口平均耗时 vs 剩余时间预算 > 120% | S9 语义：该停，不重画 |

**真库标定依据**（《replanning真库基线统计报告-20261021》+ P4 标定报告）：
    M1 阈值 50% = 全库基线 partial 率 10.0% 的 5 倍，远离噪声；M4 的 120%
    与滑动窗口口径（任务耗时中位 76s / p90 149s / max 347s，长尾 605s）见
    标定报告。**巡检只产信号，不直接动作** —— S10 仍要过 §4.2 矩阵
    （T2-light 原地精修），这是防"把设计使然报成失效"的第一道闸
    （Task #43 教训：按需不触发 ≠ 失效）。

**提前收敛判据（保守到苛刻）**：
    ① planner 建图时在 task config 标注 `required: true` 的产出型任务
    **全部 done**；② 且**零 failed**（有失败 ⇒ 走 T4/blocked，不提前收敛）；
    ③ 无 required 标注的图**不猜**（没有标注就没有判据，恒 none）。
    动作 = cancel 剩余非 required 任务（reason="目标已达成提前收敛"），
    汇总状态记 **full**（非 partial —— 剩余任务是主动战略放弃，不是失败）。

**纯函数纪律**：本模块零 IO、零 DB、零配置读取（开关由调用方传入）——
    指标与判据必须可被门禁逐格单测（G9）与内存孪生变异（M 系列）。
    配置读取入口在调用方（`core.config.get("replanning", ...)`）。
"""
import re

# ── 信号与动作常量（与 replan_signal / replan_redraw 同款命名纪律）──────────
S10_HEALTH = "S10"           # 健康度巡检指标超阈（v2 §3.2 新增信号）

ACTION_NONE = "none"
ACTION_REFINE = "refine"     # S10 ⇒ T2-light 原地精修（改 context，不动图）
ACTION_STOP = "stop"         # M4 ⇒ S9 语义：该停，不重画
ACTION_EARLY_STOP = "early_stop"

LEVEL_NONE = "T0"
LEVEL_LIGHT = "T2-light"     # 与 replan_redraw.LEVEL_LIGHT 同串（跨模块比对用）
LEVEL_ESCALATE = "T4"

#: M1 默认阈值（真库标定：基线 10.0%，50% = 5 倍远离噪声）
M1_RATIO = 0.5
#: M1 最小窗口（N≥3 —— 样本太少时"占比"没有统计意义）
M1_MIN_WINDOW = 3
#: M4 默认阈值（预计剩余耗时 > 剩余预算 × 120% ⇒ 停）
M4_RATIO = 1.2
#: M4 默认 run 级时间预算（秒）—— 与 reap_stuck_runs 的 30min 孤儿阈值对齐
#: （超预算不是"重画能救的"，是"该停了"，S9 语义）
DEFAULT_TIME_BUDGET_SEC = 1800

#: "自报缺东西"的摘要状态（口径同 replan_signal 的 S5，不另造第二份判据）
_INCOMPLETE_STATES = ("partial", "failed")


def _sig(sid, key, detail=""):
    return {"id": sid, "key": str(key or ""), "detail": str(detail or "")[:120]}


def _deps_of(t):
    d = t.get("deps") if isinstance(t, dict) else None
    if isinstance(d, str):
        try:
            import json as _json
            d = _json.loads(d or "[]")
        except Exception:
            return []
    if isinstance(d, (list, tuple)):
        return [str(x) for x in d]
    return []


def _cfg_of(t):
    c = t.get("config") if isinstance(t, dict) else None
    if isinstance(c, str):
        try:
            import json as _json
            c = _json.loads(c or "{}")
        except Exception:
            return {}
    return c if isinstance(c, dict) else {}


def _is_incomplete(summary):
    """该任务的交付摘要是否"自报缺东西"（M1 的计数单元，口径同 S5）。"""
    if not isinstance(summary, dict):
        return False
    if str(summary.get("status") or "") in _INCOMPLETE_STATES:
        return True
    miss = summary.get("missing")
    return bool(miss) if isinstance(miss, (list, tuple)) else bool(miss)


# ── 巡检节奏（v2 §4.4：每 N 个任务完成 或 每 M 秒，取先到者）────────────────
def probe_due(done_since_probe, sec_since_probe, *, every_tasks=3, every_sec=60):
    """是否到了巡检节奏（纯函数）。

    `done_since_probe`：上次巡检以来完成的任务数；`sec_since_probe`：距上次巡检秒数。
    两者任一越限即到期（取先到者）。**两个上限都 ≤0 ⇒ 永不到期**（等价于关节奏，
    调用方仍可用总闸关 —— 双闸门，与 P2 `redraw_on_stall` 同款纪律）。
    """
    et = int(every_tasks or 0)
    es = int(every_sec or 0)
    if et <= 0 and es <= 0:
        return False
    if et > 0 and int(done_since_probe or 0) >= et:
        return True
    if es > 0 and float(sec_since_probe or 0) >= es:
        return True
    return False


# ── M1：partial 密度 ───────────────────────────────────────────────────────
def partial_density(done_items, *, window=3):
    """近 `window` 个完成任务中"自报缺东西"的占比（M1，纯函数）。

    窗口取**最近**的 N 个（done_items 按完成顺序传）—— 健康度是"现在进行时"，
    5 分钟前的 partial 不该替现在的状态背锅（与 M4 用滑动窗口同源）。

    返回 `{"n": 实际窗口, "incomplete": 计数, "ratio": 占比, "hit": bool}`。
    `hit` = ratio ≥ 50% 且 n ≥ 3（样本不足不判 —— 2 个任务 1 个 partial 就 50%，
    那是噪声不是信号）。
    """
    items = [it for it in (done_items or []) if isinstance(it, dict)]
    n = max(0, int(window or 0))
    recent = items[-n:] if n > 0 else []
    cnt = sum(1 for it in recent if _is_incomplete(it.get("summary")))
    ratio = (cnt / len(recent)) if recent else 0.0
    return {"n": len(recent), "incomplete": cnt, "ratio": round(ratio, 4),
            "hit": bool(len(recent) >= M1_MIN_WINDOW and ratio >= M1_RATIO)}


# ── M2：冲突密度 ───────────────────────────────────────────────────────────
def conflict_density(done_items, tasks=None):
    """detect_conflicts 累计命中数（M2，纯函数；**强信号**：≥1 即超阈）。

    复用既有事实源 `subtask_protocol.detect_conflicts`（不另造第二份判据 ——
    与 replan_signal S7 同款纪律）。v1 无"冲突解决"状态机 ⇒ 检出的冲突一律按
    **未解决**计（保守：宁可多巡检一次，不可把未消化的矛盾当健康）。
    协议模块不可用 ⇒ 返回 hit=False（指标缺失 ≠ 指标健康，调用方记录 n/a）。
    """
    try:
        from services.subtask_protocol import detect_conflicts as _dc
    except Exception:
        return {"conflicts": 0, "done": len(done_items or []), "hit": False,
                "available": False}
    summaries = []
    for it in (done_items or []):
        if not isinstance(it, dict):
            continue
        sm = it.get("summary")
        if isinstance(sm, dict) and sm.get("claims"):
            sm = dict(sm)
            sm.setdefault("task_key", it.get("task_key"))
            summaries.append(sm)
    hits = []
    if summaries:
        try:
            hits = list(_dc(summaries) or [])
        except Exception:
            hits = []
    return {"conflicts": len(hits), "done": len(done_items or []),
            "hit": len(hits) >= 1, "available": True,
            "subjects": [str(c.get("subject") or "")[:40] for c in hits[:3]]
            if isinstance(hits, list) else []}


# ── M4：预算偏离 ───────────────────────────────────────────────────────────
def budget_deviation(latencies_ms, remaining_tasks, budget_left_sec):
    """预计剩余耗时 vs 剩余时间预算（M4，纯函数）。

    口径（真库标定）：`预计剩余 = 剩余任务数 × 滑动窗口平均耗时`（**不用全历史
    平均** —— task span 实测长尾可达 605s，全历史平均会被早期快任务稀释）。
    `hit` = 预计剩余 > 剩余预算 × 120%。输入不足（无耗时样本 / 预算 ≤0）⇒
    hit=False 且 ratio=None（**算不出 ≠ 健康**，调用方按 n/a 记录）。
    """
    lats = [float(x) for x in (latencies_ms or [])
            if isinstance(x, (int, float)) and x > 0]
    remain = int(remaining_tasks or 0)
    left = float(budget_left_sec or 0)
    if not lats or remain <= 0 or left <= 0:
        return {"projected_sec": None, "budget_sec": left or None,
                "ratio": None, "hit": False}
    avg_ms = sum(lats) / len(lats)
    projected_sec = remain * avg_ms / 1000.0
    ratio = projected_sec / left
    return {"projected_sec": round(projected_sec, 1),
            "budget_sec": round(left, 1), "ratio": round(ratio, 3),
            "hit": ratio > M4_RATIO}


# ── 主入口：健康度巡检判定 ─────────────────────────────────────────────────
def health_probe(done_items, tasks, *, enabled=False, window=3,
                 latencies_ms=None, remaining_tasks=0, budget_left_sec=0,
                 llm_verdict=None):
    """巡检判定（纯函数，v2 §4.4 A 全流程）。

    参数：
        done_items       [{"task_key","summary"}]（按完成顺序，M1 取尾部窗口）
        tasks            当前任务图行（含非 done 的 deps，备用）
        enabled          配置 `replanning.health_probe`（默认 False）
        window           M1 窗口 = 巡检节奏的任务数（调用方传 probe_every_tasks）
        latencies_ms     近 window 个 done 任务的实际耗时（M4 滑动窗口）
        remaining_tasks  未执行任务数（M4 乘数）
        budget_left_sec  剩余时间预算（M4 除数；≤0 ⇒ M4 n/a）
        llm_verdict      LLM 版巡检结论（`parse_llm_health` 产物；None=未启用）

    返回 decision dict（与 replan_signal/plan_redraw 同款形状）：
        `{"action": "refine"|"stop"|"none", "level": ..., "signals": [...],
          "reason": str, "metrics": {...}}`

    ⚠️ M4（stop）优先于 M1/M2（refine）：时间不够时"精修上下文"救不了交付，
    先停再说（S9 语义：该停，不重画）。
    """
    if not enabled:
        return _none("replanning.health_probe 关 ⇒ 不巡检（维持 P3 原逻辑）")
    done_items = [it for it in (done_items or []) if isinstance(it, dict)]
    if not done_items:
        return _none("尚无已完成任务 ⇒ 无巡检对象")

    m1 = partial_density(done_items, window=window)
    m2 = conflict_density(done_items, tasks)
    m4 = budget_deviation(latencies_ms, remaining_tasks, budget_left_sec)
    metrics = {"M1_partial": m1, "M2_conflict": m2, "M4_budget": m4}
    sigs = []

    if m4.get("hit"):
        sigs.append(_sig(S10_HEALTH, "M4",
                         "预计剩余 %.0fs > 预算 %.0fs×%.1f"
                         % (m4.get("projected_sec") or 0, m4.get("budget_sec") or 0,
                            M4_RATIO)))
        return {"action": ACTION_STOP, "level": LEVEL_NONE, "signals": sigs,
                "reason": "M4 预算偏离：剩余 %d 个任务预计 %.0fs，超剩余预算 %.0fs 的 "
                          "%d%% ⇒ S9 语义（该停，不重画）"
                          % (int(remaining_tasks or 0), m4.get("projected_sec") or 0,
                             m4.get("budget_sec") or 0, int(M4_RATIO * 100)),
                "metrics": metrics}

    drift = []
    if m1.get("hit"):
        drift.append("M1 partial 密度 %d/%d=%.0f%%" % (m1.get("incomplete") or 0,
                                                    m1.get("n") or 0,
                                                    (m1.get("ratio") or 0) * 100))
        sigs.append(_sig(S10_HEALTH, "M1", "partial %d/%d"
                         % (m1.get("incomplete") or 0, m1.get("n") or 0)))
    if m2.get("hit"):
        drift.append("M2 冲突密度 %d 次未解决" % (m2.get("conflicts") or 0))
        sigs.append(_sig(S10_HEALTH, "M2", "conflicts=%d"
                         % (m2.get("conflicts") or 0)))
    if llm_verdict and not llm_verdict.get("match", True):
        drift.append("LLM 巡检：%s" % str(llm_verdict.get("reason") or "")[:60])
        sigs.append(_sig(S10_HEALTH, "llm", str(llm_verdict.get("reason") or "")[:60]))

    if drift:
        return {"action": ACTION_REFINE, "level": LEVEL_LIGHT, "signals": sigs,
                "reason": "S10 健康度超阈（%s）⇒ T2-light 原地精修剩余任务上下文"
                          % "；".join(drift),
                "metrics": metrics}
    return _none("巡检未超阈（M1 %.0f%%/M2 %d/M4 %s）⇒ 计划仍健康"
                 % ((m1.get("ratio") or 0) * 100, m2.get("conflicts") or 0,
                    ("%.0f%%" % ((m4.get("ratio") or 0) * 100))
                    if m4.get("ratio") is not None else "n/a"),
                 metrics=metrics)


def _none(reason, *, level=LEVEL_NONE, metrics=None):
    return {"action": ACTION_NONE, "level": level, "signals": [], "reason": reason,
            "metrics": metrics or {}}


# ── 提前收敛（v2 §4.4 B）───────────────────────────────────────────────────
def early_stop_plan(tasks, *, enabled=False):
    """提前收敛判定（纯函数）。

    判据（保守到苛刻，三条全满足才动作）：
        ① 图上有 `config.required` 为真的任务（**无标注不猜**）；
        ② 这些 required 任务**全部 done**；
        ③ **零 failed**（有失败 ⇒ 走 T4/blocked 路径，不提前收敛）。
    动作 = cancel 剩余**非 required** 的非终态任务。

    返回 `{"action": "early_stop"|"none", "cancel_keys": [...],
            "required_keys": [...], "reason": str}`。
    """
    if not enabled:
        return {"action": ACTION_NONE, "cancel_keys": [], "required_keys": [],
                "reason": "replanning.early_stop 关 ⇒ 不提前收敛"}
    rows = [t for t in (tasks or []) if isinstance(t, dict)]
    required, failed, pending = [], [], []
    for t in rows:
        k = str(t.get("task_key") or t.get("key") or "")
        if not k:
            continue
        st = str(t.get("status") or "")
        if _cfg_of(t).get("required"):
            required.append(k)
            if st != "done":
                pending.append(k)
        elif st == "failed":
            failed.append(k)
    if not required:
        return {"action": ACTION_NONE, "cancel_keys": [], "required_keys": [],
                "reason": "无 required 标注任务 ⇒ 不猜目标边界，不提前收敛"}
    if failed:
        return {"action": ACTION_NONE, "cancel_keys": [], "required_keys": required,
                "reason": "存在 failed 任务（%s）⇒ 不提前收敛（失败走 T4/blocked）"
                          % ",".join(failed[:5])}
    if pending:
        return {"action": ACTION_NONE, "cancel_keys": [], "required_keys": required,
                "reason": "required 任务未全部 done（缺 %s）⇒ 目标未达成，不收敛"
                          % ",".join(pending[:5])}
    cancel = [str(t.get("task_key") or t.get("key") or "") for t in rows
              if str(t.get("status") or "") in ("planned", "ready")
              and not _cfg_of(t).get("required")]
    if not cancel:
        return {"action": ACTION_NONE, "cancel_keys": [], "required_keys": required,
                "reason": "剩余任务均为 required 或无剩余任务 ⇒ 无需收敛"}
    return {"action": ACTION_EARLY_STOP, "cancel_keys": cancel,
            "required_keys": required,
            "reason": "required 产出已全部达成（%s）⇒ 提前收敛，主动放弃 %d 个"
                      "非必需任务（%s）"
                      % (",".join(required[:5]), len(cancel), ",".join(cancel[:5]))}


# ── LLM 版巡检（health_probe_llm，默认关；CrewAI observer 同款）────────────
def llm_health_prompt(goal, tasks, *, done_summaries=None, directive=""):
    """LLM 巡检提示词（纯函数）：轻量模型评估"剩余计划是否仍匹配 goal"。

    成本口径：+1 次轻量调用/巡检，计入 run_tokens（v2 §4.9）。
    返回 `(prompt, schema_hint)`；LLM 输出仍要过 `parse_llm_health` 的形状校验
    （与 T3 同款纪律：LLM 输出不豁免校验）。
    """
    lines = []
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        lines.append("- %s [%s] %s" % (str(t.get("task_key") or t.get("key") or ""),
                                       str(t.get("status") or "planned"),
                                       str(t.get("title") or "")[:60]))
    done_txt = ""
    if done_summaries:
        try:
            done_txt = ("\n已完成任务的结论摘要：\n"
                        + "\n".join("  %s" % str(x)[:120]
                                    for x in list(done_summaries)[:10]))
        except Exception:
            done_txt = ""
    prompt = (
        "你是执行中重规划巡检器。判断**剩余计划**是否仍匹配原始目标。\n"
        "\n# 原始目标\n%s\n"
        "\n# 任务图（含状态）\n%s\n"
        "%s%s"
        "\n# 你的任务\n"
        "只输出 JSON：{\"match\": true|false, \"reason\": \"...\"}。\n"
        "match=false 的判据：剩余任务即使全部完成也达不成目标，或目标已被\n"
        "既有产物覆盖、剩余任务纯属浪费。信息不足时判 true（宁可漏报）。\n"
        % (str(goal or "")[:500],
           "\n".join(lines[:40]) or "（空）",
           done_txt,
           ("\n# 用户中途指令\n%s\n" % str(directive or "")[:300]) if directive else "")
    )
    return prompt, {"match": "bool", "reason": "str"}


def parse_llm_health(raw):
    """解析 LLM 巡检输出 ⇒ `({"match": bool, "reason": str}, parse_error)`。

    严格三条（解析层就拒）：非 JSON/非对象 ⇒ error；`match` 非布尔 ⇒ error；
    `reason` 缺失可容忍（空串），但 match=false 时**必须有理由**（没有理由的
    "不匹配"不可解释、不可门禁 —— 与 directive 的 basis 纪律同款）。
    """
    import json as _j
    try:
        obj = _j.loads(str(raw or "").strip())
    except Exception as e:
        return {}, "LLM 巡检输出非 JSON：%s" % e
    if not isinstance(obj, dict):
        return {}, "LLM 巡检输出非对象"
    m = obj.get("match")
    if not isinstance(m, bool):
        return {}, "LLM 巡检输出缺布尔 match 字段"
    reason = str(obj.get("reason") or "").strip()[:200]
    if not m and not reason:
        return {}, "LLM 巡检判不匹配但未给理由 ⇒ 拒收（不可解释的信号不用）"
    return {"match": m, "reason": reason}, ""


# ── 决策记录（span meta，与 replan_signal.decision_meta 同款键集）──────────
def health_meta(decision, *, latency_ms=0, tokens=0, round_no=0):
    """health decision → replan span 的 meta_json（G6 字段完整性）。

    为什么复用 `replan_signal.decision_meta` 而不是自己拼：前端 trace 面板
    （03-chat.js kindMeta）按 `action/level/reason/signals/round/tokens/
    latency_ms` 这套键渲染，另起一套键名会让巡检节点在面板上"什么都没有"。
    metrics 挂在其上（M1/M2/M4 的原始数值 —— 阈值调参时排障就靠它）。
    """
    try:
        from services.replan_signal import decision_meta as _dm
        meta = _dm(decision, round_no=round_no, tokens=tokens,
                   latency_ms=latency_ms)
    except Exception:
        d = decision or {}
        meta = {"action": str(d.get("action") or ACTION_NONE),
                "level": str(d.get("level") or LEVEL_NONE),
                "reason": str(d.get("reason") or "")[:200],
                "signals": [str(s) for s in (d.get("signals") or [])],
                "round": int(round_no or 0), "tokens": int(tokens or 0),
                "latency_ms": int(latency_ms or 0)}
    m = (decision or {}).get("metrics")
    if isinstance(m, dict) and m:
        meta["metrics"] = m
    return meta
