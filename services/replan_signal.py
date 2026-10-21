# -*- coding: utf-8 -*-
"""P1 replanning：信号采集与触发判定（纯函数，2026-10-21）。

**为什么需要**（对标差距 G1/G2，设计文档 v2 §4.2/§4.6）：
    执行中重规划的第一公里不是"重画"，而是**把已经发生但没人看的信号收上来**。
    子任务失败、partial 交付、claims 未验证、结论互斥 —— 这些证据今天散落在
    `agent_tasks.error` / 协议 summary / `detect_conflicts` 里，**没有任何结构
    消费它们**。P1 把它们汇成观测结构 + 判定矩阵（T0/T1 两格可执行，T2+ 留 P2），
    每次判定落 kind=replan span 决策记录（含 action=none —— "为什么不重画"与
    "为什么重画"同等重要，对标 OpenAI Deep Research 停止原因显式 log）。

**信号口径（v1 §3.1，P1 只采集 S3~S7）**：
    S1 用户补充指令 / S2 附件变化 —— 用户指令类，挂点与竞态属 P3（v2 §4.7）；
    S3 任务 fail 且 error_class 可重试（timeout/transient/internal）；
    S4 任务 fail 且不可重试（permission/input_invalid/output_rejected/business）；
    S5 交付摘要 status=partial/failed 或 missing 非空；
    S6 claims 含 to_verify 且被下游依赖（下游在未验证结论上建房）；
    S7 detect_conflicts 检出互斥取值；
    S8 RefineGate 结构问题 / S9 预算超时取消 —— 弱信号，P1 仅留 ID 不采集。

**S3' 规则（真库基线修正，2026-10-21 米爸拍板）**：
    `failure_class.is_retryable_class` 判可重试还含 `interrupted`，但真库 58 条
    历史失败里 50 条是 `reap_stuck_runs` 的**孤儿标记**（run 已终止，补跑无意义，
    该走 resume）⇒ T1 可补跑类收窄为 {timeout, transient, internal}，
    活场景收益面 8/58（13.8%）。见《replanning真库基线统计报告-20261021》§2。

**纯函数纪律**：本模块零 IO、零 DB、零配置读取（开关由调用方传入）——
    判定矩阵必须可被门禁逐格单测（G1）与内存孪生变异（M 系列）。
    配置读取入口在调用方（`core.config.get("replanning", ...)`）。

**判定矩阵（P1 可执行格）**：

| 信号 | 级别 | 动作 | 条件 |
|---|---|---|---|
| S3 | T1 | retry（同 key 补跑一次） | retry_once 开 且 该 key 未补过 且 类 ∈ T1_RETRYABLE_CLASSES |
| S3' | T0 | none | 类=interrupted（孤儿标记归 resume）或已补过一次 |
| S4 | T0 | none | 不可重试类：不该自动补跑（重画换输入属 P2） |
| S5/S6/S7 | T0 | none | 观测留痕；差量重画属 P2（v2 §7） |
| 无信号 | T0 | none | 维持现状，partial 如实汇总 |
"""
import re

# ── 信号 ID（v1 §3.1 口径）────────────────────────────────────────────────
S3_RETRYABLE_FAIL = "S3"      # 任务 fail 且可重试
S4_NONRETRYABLE_FAIL = "S4"   # 任务 fail 且不可重试
S5_PARTIAL = "S5"             # 摘要 partial/failed 或 missing 非空
S6_TO_VERIFY = "S6"           # to_verify claims 被下游依赖
S7_CONFLICT = "S7"            # detect_conflicts 命中

#: T1 可补跑的错误类（S3'）：**刻意排除 interrupted** —— 它是 reap_stuck_runs
#: 的孤儿标记（run 已终止），补跑无意义，归 resume 路径（真库 50/58 属此类）。
T1_RETRYABLE_CLASSES = frozenset({"timeout", "transient", "internal"})

# 动作（P1 只有 none/retry；replan/skip/insert 留 P2+）
ACTION_NONE = "none"
ACTION_RETRY = "retry"

# 级别（T2/T3/T4 的常量为 P2/P3 预留，P1 不产出）
T0 = "T0"
T1 = "T1"


def _sig(sid, key, detail=""):
    return {"id": sid, "key": str(key or ""), "detail": str(detail or "")[:120]}


def _sig_text(s):
    """signal → 紧凑串（span meta 的 signals 字段用，如 "S3:t4:timeout"）。"""
    d = (s.get("detail") or "").strip()
    return "%s:%s%s" % (s.get("id"), s.get("key"), (":" + d) if d else "")


def t1_retryable(error_class) -> bool:
    """S3' 规则：该类失败是否值得 DAG 级自动补跑一次。

    与 `failure_class.is_retryable_class` 的差异是**刻意收窄**：那边含
    interrupted（宁可多重试一次的保守取向适用于 worker 内重试），但 DAG 级
    补跑面对的是"worker 已重试过仍失败"的任务 —— 孤儿标记再补一次纯属浪费。
    """
    return str(error_class or "") in T1_RETRYABLE_CLASSES


def decide_on_failure(task_key, error_text, error_class, *,
                      retry_once=True, already_t1=False):
    """任务失败点的 T1 判定（stream.py 主循环 per-future 收尾处调用）。

    参数：
        task_key      失败任务 key
        error_text    错误文本（留痕用，截断）
        error_class   `services.failure_class.classify` 的分类名
        retry_once    配置 `replanning.retry_once`（P1 默认 True）
        already_t1    该 key 本次编排是否已 DAG 级补跑过（内存集合传入，
                      防"补跑→再失败→再补跑"；进程重启后由 resume 语义收敛：
                      failed 是终态，resume 不再派发它）

    返回 decision dict（G6 字段全集，可直接转 span meta）：
        {"signals": [...], "level": "T0"|"T1", "action": "none"|"retry",
         "reason": str, "retry_keys": [...]}
    """
    cls = str(error_class or "")
    if t1_retryable(cls):
        sig = _sig(S3_RETRYABLE_FAIL, task_key, cls)
        if not retry_once:
            return {"signals": [sig], "level": T0, "action": ACTION_NONE,
                    "reason": "S3 可重试失败（%s），但 retry_once 关 ⇒ 不补跑" % cls,
                    "retry_keys": []}
        if already_t1:
            return {"signals": [sig], "level": T0, "action": ACTION_NONE,
                    "reason": "S3 可重试失败（%s），但该 key 已补跑过一次 ⇒ 不再补"
                              "（无进展防护）" % cls,
                    "retry_keys": []}
        return {"signals": [sig], "level": T1, "action": ACTION_RETRY,
                "reason": "S3 可重试失败（%s）⇒ 同 key 补跑一次" % cls,
                "retry_keys": [str(task_key)]}
    # 不可重试类（含 interrupted —— 归 resume，不归补跑）
    if cls == "interrupted":
        return {"signals": [_sig(S3_RETRYABLE_FAIL, task_key, cls)],
                "level": T0, "action": ACTION_NONE,
                "reason": "interrupted 孤儿标记（run 已终止）⇒ 归 resume，不补跑",
                "retry_keys": []}
    return {"signals": [_sig(S4_NONRETRYABLE_FAIL, task_key, cls)],
            "level": T0, "action": ACTION_NONE,
            "reason": "S4 不可重试失败（%s）⇒ 不自动补跑；改输入/换 Agent 属 P2 重画"
                      % (cls or "未分类"),
            "retry_keys": []}


def _deps_of(t):
    """任务行的 deps 归一化为 list（DB 里可能是 JSON 字符串 —— 与 stream.py
    主循环 L1037 同款解析；不归一化会把 '["t1"]' 迭代成字符）。"""
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


def _summary_of(item):
    s = item.get("summary") if isinstance(item, dict) else None
    return s if isinstance(s, dict) else {}


def collect_delivery_signals(done_items, tasks):
    """从已完成任务与任务图采集 S5/S6/S7（收敛点调用，纯函数）。

    参数：
        done_items  [{"task_key", "summary"}]（协议 summary dict）
        tasks       [{"task_key", "deps", "status"}]（run 内全部任务；
                    S6 的"被下游依赖"判定需要非 done 任务的 deps）

    返回 signal dict 列表（可空）。**采集失败不抛** —— 坏数据跳过，
    观测旁路铁律（宁缺勿假）。
    """
    out = []
    done_items = done_items or []
    tasks = tasks or []

    # S5：partial/failed 或 missing 非空
    for it in done_items:
        sm = _summary_of(it)
        st = str(sm.get("status") or "")
        missing = sm.get("missing")
        miss_n = len(missing) if isinstance(missing, (list, tuple)) else 0
        if st in ("partial", "failed") or miss_n:
            out.append(_sig(S5_PARTIAL, it.get("task_key"),
                            "%s missing=%d" % (st or "?", miss_n)))

    # S6：done 任务的 to_verify claims 被**未完成**下游依赖
    downstream_of = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "") == "done":
            continue
        for d in _deps_of(t):
            downstream_of.setdefault(d, []).append(str(t.get("task_key") or ""))
    for it in done_items:
        tk = str(it.get("task_key") or "")
        downs = downstream_of.get(tk) or []
        if not downs:
            continue
        sm = _summary_of(it)
        for c in (sm.get("claims") or []):
            if isinstance(c, dict) and str(c.get("kind") or "") == "to_verify":
                out.append(_sig(S6_TO_VERIFY, tk,
                                "to_verify 被 %s 依赖" % ",".join(downs[:3])))
                break

    # S7：互斥结论（复用既有事实源 subtask_protocol.detect_conflicts，
    #      不另造第二份判据 —— 与 failure_class 复用 tool_resilience 同款纪律）
    try:
        from services.subtask_protocol import detect_conflicts as _dc
        summaries = []
        for it in done_items:
            sm = _summary_of(it)
            if sm.get("claims"):
                sm = dict(sm)
                sm.setdefault("task_key", it.get("task_key"))
                summaries.append(sm)
        if summaries:
            for cf in (_dc(summaries) or [])[:3]:
                out.append(_sig(S7_CONFLICT, cf.get("subject") or "",
                                str(cf.get("reason") or "")[:100]))
    except Exception:
        pass        # 协议模块不可用 ⇒ 丢弃 S7，不影响其他信号

    return out


def decide_on_stall(done_items, tasks, *, redraw_enabled=False):
    """收敛点（主循环 `if not ready`）判定。

    P1 语义：**恒 action=none** —— 差量重画属 P2（v2 §7），此处只把
    S5/S6/S7 收上来留痕（G6：没有信号也要记"为什么不重画"）。
    `redraw_enabled` 为 P2 预留：开启后本函数仍返回 none（P2 另接 §4.2 算法），
    参数存在只为让 P1 的调用形状对 P2 稳定。

    返回 decision dict（同 decide_on_failure 字段全集）。
    """
    sigs = collect_delivery_signals(done_items, tasks)
    if not sigs:
        return {"signals": [], "level": T0, "action": ACTION_NONE,
                "reason": "无重规划信号（S5/S6/S7 均未命中）⇒ 维持现状，"
                          "partial 如实汇总",
                "retry_keys": []}
    ids = sorted({s["id"] for s in sigs})
    return {"signals": sigs, "level": T0, "action": ACTION_NONE,
            "reason": "命中 %s（%d 条）⇒ P1 观测留痕；差量重画属 P2（默认关）"
                      % ("/".join(ids), len(sigs)),
            "retry_keys": []}


def decision_meta(decision, *, round_no=0, tokens=0, latency_ms=0):
    """decision → replan span 的 meta_json（G6 字段完整性）。

    必发字段（v2 §4.6）：signals / level / action / reason；缺一即门禁红。
    round/tokens/latency 由调用方补（span 收口时才有 latency）。
    """
    d = decision or {}
    meta = {
        "signals": [_sig_text(s) for s in (d.get("signals") or [])],
        "level": str(d.get("level") or T0),
        "action": str(d.get("action") or ACTION_NONE),
        "reason": str(d.get("reason") or "")[:200],
        "round": int(round_no or 0),
        "tokens": int(tokens or 0),
        "latency_ms": int(latency_ms or 0),
    }
    if d.get("retry_keys"):
        meta["retry_keys"] = [str(k) for k in d["retry_keys"]]
    return meta


def plan_revised_payload(decision, *, run_id=0):
    """action≠none ⇒ SSE `plan_revised` 事件 payload；action=none ⇒ None。

    P1 只会产出 none（T2 重画未上线）⇒ 实际不发事件，但发送路径随 P1 就位，
    P2 接线时只差"把 decision 传进来"这一步（v2 §4.3 payload 口径）。
    """
    d = decision or {}
    if str(d.get("action") or ACTION_NONE) == ACTION_NONE:
        return None
    return {"type": "plan_revised", "run_id": int(run_id or 0),
            "level": str(d.get("level") or ""),
            "action": str(d.get("action") or ""),
            "reason": str(d.get("reason") or "")[:200],
            "signals": [_sig_text(s) for s in (d.get("signals") or [])],
            "retry_keys": [str(k) for k in (d.get("retry_keys") or [])]}


#: 归一化错误文本（无进展检测 P2 用：同因第 2 次 ⇒ T4）。P1 先落纯函数，
#: 消费方在 P2 —— 放这里因为它的输入（error_text）与 decide_on_failure 同源。
_NORM_RE = re.compile(r"[0-9]+|\s+")


def norm_error_text(error_text) -> str:
    """错误文本归一化（去数字/压空白）—— 无进展检测的比对键（P2 消费）。"""
    return _NORM_RE.sub("#", str(error_text or "").strip().lower())[:160]
