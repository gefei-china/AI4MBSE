#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Trace 一致性评测器（2026-10-22）—— 补上断链②「trace_evalset 无消费方」。

═══════════════════════════════════════════════════════════════════
为什么要有它（AgentLoop 闭环的第二环，不是又造一个工具）
═══════════════════════════════════════════════════════════════════
`tools/eval/trace2dataset.py`（P1-1）已把生产证据变成评测样本
（tmp/p21/trace_evalset.json，228 条：strong 55 / weak 173），但
**没有任何评测器消费它** —— 行业闭环「Trace2Dataset：生产轨迹 →
评测集 → 优化 → 再部署」断在第二环：供料环通了，度量环没接。
llm_usage_stats 10593 条躺在表里无人消费，正是"数据坟墓"的形态。

本器回答一个此前**无人回答**的问题：

    **生产上人工裁决过的案例，机器今天还同意吗？**

- `intent_confirmed`（42 条，人工确认的正确意图）→ 当前路由命中率
  = `agreement`（裁决一致率）。它比 CI 门禁自建的 29 例更贴近生产，
  回答的是"人对过的题还在丢分吗"。
- `intent_rejected`（13 条，人工驳回）→ **先分类再计算**（口径复核，
  2026-10-22 实测后修正）：标注员驳回**不等于**"路由判错了"——
  实测 13 条全部是"非用户原话/内部上下文，不应入集"的**池有效性**
  裁决（note 明写），只有"这文本应走 X 不是 Y"的**路由纠错**裁决
  才算真负样本。拿池有效性驳回算"重错率"（repeat_mistake_rate），
  是用池卫生指标冒充路由指标。故：重错率只对路由纠错类驳回计算，
  当前该类为 0 ⇒ 指标判 SKIP（无有效分母），不判 0 也不恒绿。
  trace2dataset 抽取器 desc 写的"路由负样本"与实测语义有出入，
  已在报告与文档中如实记录（spec 与数据不符时，以数据为准并说出来）。
- `conv_clean`（weak，机器派生）→ **没有 gold，不做质量判定**
  （trace2dataset 的纪律：weak ≠ 正确，"没坏过"不是"正确"的证明）。
  只跑路由分布做观测：某一意图占比突然塌了/爆了，是回归的早期信号，
  但不是质量证据。分布进报告，**不进门禁**。

═══════════════════════════════════════════════════════════════════
三道纪律（都已实测踩过，写在这里免得后来人重新踩）
═══════════════════════════════════════════════════════════════════
1. **gold 再验证，不信旧集**：trace 集的 gold 来自生产 intent_samples，
   而生产数据每小时在变（intent 可被改、行可被删）。拿旧 gold 打分
   = 失效 gold 自欺。本器对每条 strong 样本回查当前库，
   (query, gold) 对不上 ⇒ 该样本**剔除并记账**（可见，不静默）；
   剔除比例超限 ⇒ 整体判"样本池被删"，不出分（与检索评测空库不出分同口径）。
   完整库指纹漂移另记 `fingerprint_drift` 字段（供 trustworthy 判定），
   但不因此终止——新消息到达不改变既有样本的标签有效性，
   把"池子变新"当"gold 失效"会让每次生产跑批都漂移（那是钝器不是纪律）。
2. **必须绕开意图缓存**（AGENTS.md 坑 26）：缓存命中时测的是历史结论，
   A/B 会假绿。评测期把 `_cache_get`/`_cache_set` 打成 no-op。
3. **必须带负对照**（与 run_eval --control 同构）：把 positive 的 gold
   轮转后重算一致率。若轮转后分数与真实分数相近 ⇒ 指标没在测
   "路由↔人工裁决"的对齐关系，是假绿。gold 意图少于 3 类时轮转
   恒等于自身 ⇒ 对照不具判别力，此时判 SKIP 并说明，不设阈值混过去。

用法：
    .venv/Scripts/python.exe -X utf8 tools/eval/eval_trace_consistency.py
    .venv/Scripts/python.exe -X utf8 tools/eval/eval_trace_consistency.py --save
    .venv/Scripts/python.exe -X utf8 tools/eval/eval_trace_consistency.py --no-llm
    .venv/Scripts/python.exe -X utf8 tools/eval/eval_trace_consistency.py --no-llm \
        --baseline docs/评测/nightly/latest-trace-report.json --save
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

# O2（2026-10-23）：优先读版本化评测集（docs/评测/datasets/，随 git），
# 缺失时退回 tmp/p21/ 旧路径。
_DEFAULT_DS_TRACE = os.path.join(ROOT, "docs", "评测", "datasets", "trace_evalset.json")
DEFAULT_TRACE = _DEFAULT_DS_TRACE if os.path.exists(_DEFAULT_DS_TRACE) else \
    os.path.join(ROOT, "tmp", "p21", "trace_evalset.json")
DEFAULT_OUT = os.path.join(ROOT, "tmp", "p21", "eval_trace_consistency.json")
DEFAULT_DB = os.path.join(ROOT, "mbse.db")
TRACE_SCHEMA = "p1-1/trace-evalset@1"

#: strong 标注里"人工确认的正确意图"与"人工驳回"两类抽取器
_POS_EXTRACTORS = ("intent_confirmed",)
_NEG_EXTRACTORS = ("intent_rejected",)

#: 驳回样本的两类语义（2026-10-22 实测 13 条全部是第一类，见文件头
#: 「口径复核」节）：标注员驳回**不等于**"路由判错了"——
#:   · 池有效性驳回（"非用户原话/内部上下文，不应入集"）⇒ 不是路由错误，
#:     拿它算"重错率"是用池卫生指标冒充路由指标；
#:   · 路由纠错驳回（"这文本应走 X 而不是 Y"）⇒ 才是真负样本。
#: 按 note 标记区分；note 为空按路由纠错处理（保守：未知语义算强信号）。
_POOL_INVALID_MARKERS = ("非用户原话", "不属于单标签", "内部上下文",
                         "测试残留", "澄清续跑包装")


def classify_rejection(note: str) -> str:
    """驳回样本语义分类：'pool_invalid'（不应入集）/'routing_error'（路由纠错）。"""
    n = note or ""
    return "pool_invalid" if any(m in n for m in _POOL_INVALID_MARKERS) \
        else "routing_error"


# ══════════════════════════════════════════════════════════════════
# 纯函数层（夹具可直接测，不碰 DB、不建路由器）
# ══════════════════════════════════════════════════════════════════
def _shared_metrics(y_true, y_pred):
    """与线上端点/CI 门禁**同一份**指标封装（run_ci_eval E4c 纪律：
    不各写一份算法，否则两套数字必然互相打架）。"""
    from repositories.intent_sample_repo import metrics
    return metrics(y_true, y_pred)


def positive_agreement(pairs):
    """positive 集一致率。pairs = [(gold, pred), ...]

    委托 `intent_sample_repo.metrics`：accuracy 即一致率，
    同时给 macro_f1 / per_class / confusion（哪个意图在丢分，一眼可见）。
    """
    if not pairs:
        return {"n": 0, "agreement": None, "macro_f1": None,
                "per_class": [], "confusion": {}}
    m = _shared_metrics([g for g, _ in pairs], [p for _, p in pairs])
    return {"n": m["n"], "agreement": m["accuracy"], "macro_f1": m["macro_f1"],
            "per_class": m["per_class"], "confusion": m["confusion"]}


def repeat_mistake_rate(negatives):
    """negative 集重错率：当前路由仍判成"被驳回意图"的比例。

    negatives = [{"gold": 被驳回的意图, "pred": 当前路由意图}, ...]
    返回 (rate, repeats)。空集返回 (None, []) —— 分母 0 不是 0 分。
    """
    if not negatives:
        return None, []
    repeats = [n for n in negatives if n["gold"] and n["pred"] == n["gold"]]
    return round(len(repeats) / len(negatives), 4), repeats


def weak_stability(baseline_detail, current_detail, min_compared=10):
    """weak 回归快照对比：同一批生产查询，本轮路由与上轮是否一致。

    baseline_detail / current_detail = [{"query", "pred", "route"}, ...]
    （各自报告 weak.weak_detail；旧格式报告没有该字段 ⇒ 调用方传 None）。

    为什么按 query 精确匹配取交集：weak 切片是"最近的 N 条"，trace 集
    重建后组成会变（新会话挤掉最旧的）⇒ 直接逐位比对会把"池子组成
    变化"误读成"路由变化"。交集内比对才是同一问题的重问。

    返回 {n_compared, stability, shifts, note}：
    - 无基线 / 交集 < min_compared ⇒ stability=None + note（不具判别力
      不比，与 grounded 的条件判定同构——小样本上算出来的稳定率是噪声）；
    - stability = 交集内 pred 相同的比例；shifts 列出变化项（ capped ）。
    """
    if not baseline_detail or not current_detail:
        return {"n_compared": 0, "stability": None, "shifts": [],
                "note": "无基线明细（首轮或旧格式报告）—— 不判定"}
    base = {}
    for d in baseline_detail:
        base.setdefault(d.get("query") or "", d.get("pred") or "")
    shifts, same, compared = [], 0, 0
    for d in current_detail:
        q = d.get("query") or ""
        if q not in base:
            continue
        compared += 1
        if (d.get("pred") or "") == base[q]:
            same += 1
        else:
            shifts.append({"query": q, "from": base[q], "to": d.get("pred") or ""})
    if compared < min_compared:
        return {"n_compared": compared, "stability": None, "shifts": shifts[:20],
                "note": "交集仅 %d 条（< %d）—— 不具判别力，不判定"
                        % (compared, min_compared)}
    return {"n_compared": compared,
            "stability": round(same / compared, 4),
            "shifts": shifts[:20], "note": ""}


def rotated_control(golds, preds):
    """负对照：gold 做**固定种子**洗牌（pred 不动）后重算一致率。

    为什么洗牌而不是"轮转一位"：实测 strong 样本按意图**聚集**
    （同意图样本相邻），轮转一位仍有 ~52% 相邻同意图 ⇒ 轮转后
    一致率塌不到机遇水平，对照拉不开差距（第一版就踩了这个：
    拿 golds 与 rotated golds 相比，测的是多重集聚碰率，不是
    "指标在测对齐关系"）。固定种子 ⇒ 对照本身可复现。

    轮转后一致率应塌到接近机遇水平（≈ Σ pred份额(i)·gold份额(i)）；
    若与真实一致率相近 ⇒ 指标没在测"路由↔人工裁决"的对齐关系，
    是假绿。gold 少于 2 个不同意图时任何重排恒等于自身 ⇒ 返回
    None（对照不具判别力，调用方必须判 SKIP）。
    """
    import random
    n = len(golds)
    if n < 2 or len(set(golds)) < 2 or len(preds) != n:
        return None
    idx = list(range(n))
    random.Random(20261022).shuffle(idx)
    rotated = [golds[i] for i in idx]
    m = _shared_metrics(rotated, preds)
    return m["accuracy"]


# ══════════════════════════════════════════════════════════════════
# gold 再验证（纪律 1）：回查当前库，(query, gold) 对不上即失效
# ══════════════════════════════════════════════════════════════════
def _gold_now(conn, sample):
    """当前库中该样本的 gold（与 trace2dataset 抽取器同口径）。

    confirmed → intent；rejected → COALESCE(hit_intent, intent)。
    同时取 note（驳回语义分类用）。行不存在返回 (None, None, "")。
    """
    row = conn.execute(
        "SELECT text, intent, hit_intent, COALESCE(note,'') FROM intent_samples WHERE id=?",
        (sample.get("source_id"),)).fetchone()
    if not row:
        return None, None, ""
    text, intent, hit_intent, note = row[0], row[1] or "", row[2] or "", row[3] or ""
    gold = intent if sample["extractor"] in _POS_EXTRACTORS else (hit_intent or intent)
    return text, gold, note


def revalidate_gold(conn, samples):
    """逐条回查。返回 (valid, stale) —— stale 每条带原因（可见记账）。

    valid 的 rejected 样本附带 `reject_kind`（pool_invalid / routing_error），
    供指标层区别对待（见 classify_rejection）。
    """
    valid, stale = [], []
    for s in samples:
        if s.get("extractor") not in _POS_EXTRACTORS + _NEG_EXTRACTORS:
            continue  # weak 无 gold，不参与再验证
        try:
            text, gold, note = _gold_now(conn, s)
        except sqlite3.Error as e:
            stale.append({"id": s.get("id"), "reason": "回查失败: %s" % e})
            continue
        if text is None:
            stale.append({"id": s.get("id"), "source_id": s.get("source_id"),
                          "reason": "源行已删除"})
            continue
        if (text or "").strip() != (s.get("query") or "").strip():
            stale.append({"id": s.get("id"), "source_id": s.get("source_id"),
                          "reason": "query 已变（样本文本与源行不一致）"})
            continue
        if (gold or "") != (s.get("gold") or ""):
            stale.append({"id": s.get("id"), "source_id": s.get("source_id"),
                          "reason": "gold 已变（%r → %r）" % (s.get("gold"), gold)})
            continue
        item = dict(s)
        if s.get("extractor") in _NEG_EXTRACTORS:
            item["reject_kind"] = classify_rejection(note)
            item["reject_note"] = note
        valid.append(item)
    return valid, stale


# ══════════════════════════════════════════════════════════════════
# 路由执行（纪律 2：缓存旁路；LLM 兜底可关）
# ══════════════════════════════════════════════════════════════════
def build_router(no_llm=False):
    """建生产路由器（与 run_ci_eval 同一条实例化路径，只是库不同）。

    ⚠ 顺序：先 import 业务模块再建实例 —— MBSE_DB_PATH 在 import 期
    绑定，本脚本不改库路径（评的就是生产路由），故无干净库那套前置。
    """
    from agent.intent import IntentRouter
    from agent.pipeline import AgentPipeline
    pipe = AgentPipeline()
    pipe._load_db_agents()
    rt = pipe.router
    if no_llm:
        # 确定性口径（规则+语义层）：LLM 兜底是模型相关、有温度、且依赖
        # 外部服务可用性 —— 关掉它才能让门禁结论可复现（同 CI 门禁哲学：
        # 「规则层+语义层不许塌」进门禁，模型质量另跑）。
        rt.detect_llm = lambda text, context="": ("", 0.0)
    return rt, IntentRouter


def route_queries(rt, IntentRouter, queries):
    """逐条跑 detect，返回 decisions + 路由期记账。

    缓存旁路（坑 26）；单条异常不杀全程（记 route='error' 并计数 ——
    静默丢样本比丢错样本更糟）。
    """
    og, os_ = IntentRouter._cache_get, IntentRouter._cache_set
    IntentRouter._cache_get = lambda self, *a, **k: None
    IntentRouter._cache_set = lambda self, *a, **k: None
    decisions, errors = [], 0
    try:
        for q in queries:
            try:
                intent = rt.detect(q)
                meta = rt.get_last_meta() or {}
                decisions.append({"query": q, "pred": intent,
                                  "route": meta.get("route", ""),
                                  "conf": meta.get("confidence"),
                                  "needs_clarify": meta.get("needs_clarification")})
            except Exception as e:  # noqa: BLE001 —— 跑批器自己不能崩
                errors += 1
                decisions.append({"query": q, "pred": "", "route": "error",
                                  "conf": None, "needs_clarify": None,
                                  "error": "%s: %s" % (type(e).__name__, e)})
    finally:
        IntentRouter._cache_get, IntentRouter._cache_set = og, os_
    return decisions, errors


def semantic_backend(rt):
    """本次实际走的语义后端（'dense' 真 embedding / 'bigram' 降级）。

    两路余弦量纲不同（同对文本实测 dense 0.665 / bigram 0.309）⇒
    一致率数值**不可跨后端比较**。不记录后端 = 报告不可解释。
    """
    try:
        idx = getattr(rt, "_semantic_index", None)
        if not idx:
            return "none"
        from semantic import SemanticSearch
        return SemanticSearch().last_backend or "bigram"
    except Exception:
        return "unknown"


# ══════════════════════════════════════════════════════════════════
# 夹具自证（F1~F8）：指标算错 ⇒ 生产数字一律不可信
# ══════════════════════════════════════════════════════════════════
def _fixture_cases():
    """黄金样本：手算期望，不用被测代码算期望。"""
    pos_all_match = [("design", "design"), ("review", "review"), ("chat", "chat")]
    pos_all_miss = [("design", "review"), ("review", "chat"), ("chat", "design")]
    negs = [{"gold": "design", "pred": "design"},      # 重错
            {"gold": "review", "pred": "review"},      # 重错
            {"gold": "impact", "pred": "chat"},
            {"gold": "knowledge_qa", "pred": "knowledge_qa"},  # 重错... 见 F3 期望
            {"gold": "chat", "pred": "design"}]
    return pos_all_match, pos_all_miss, negs


def run_fixture():
    """F1~F8 全部纯函数/内存库，零外部依赖。"""
    pos_ok, pos_miss, negs = _fixture_cases()
    cases = []

    def _c(cid, name, expect, got, tol=1e-9):
        if expect is None or got is None:
            ok = expect is got
        else:
            ok = abs(float(got) - float(expect)) < tol
        cases.append({"case": cid, "metric": name, "expect": expect,
                      "got": got, "ok": ok})

    # F1/F2：一致率两端（全对 1.0 / 全错 0.0）
    _c("F1", "agreement", 1.0, positive_agreement(pos_ok)["agreement"])
    _c("F2", "agreement", 0.0, positive_agreement(pos_miss)["agreement"])
    # F3：重错率精确值（5 条里 3 条仍判成被驳回意图）
    rate, repeats = repeat_mistake_rate(negs)
    _c("F3", "repeat_mistake_rate", 0.6, rate)
    cases.append({"case": "F3b", "metric": "repeats", "expect": 3,
                  "got": len(repeats), "ok": len(repeats) == 3})
    # F4：负对照可判别 —— 标签被洗乱后一致率必须塌下来。
    # 6 个互异意图（seed=20261022 的洗牌是 [1,3,0,4,2,5]，非恒等，
    # 期望值按该置换手算：仅 1/6 命中）。n=3 时同 seed 恰好恒等
    # （第一版踩过：拿 3 条夹具测出 1.0 假红）—— 夹具规模要够大。
    _g6 = ["a", "b", "c", "d", "e", "f"]
    _p6 = ["a", "b", "c", "d", "e", "f"]
    _c("F4", "rotated_agreement", 0.1667, rotated_control(_g6, _p6))
    # F4b：同质 gold（全 design）任何重排恒等于自身 ⇒ 必须返回 None（不具判别力）
    _c("F4b", "rotated_agreement", None,
       rotated_control(["design"] * 4, ["design"] * 4))
    # F4c：**聚集样本**上对照仍须可判别 —— 这是第一版真正踩的坑：
    #   strong 样本按意图聚集（同意图相邻），"轮转一位"仍有 52%
    #   相邻同意图 ⇒ 轮转后塌不到机遇水平，对照拉不开差距（假绿）。
    #   洗牌 + 差距判定才对。聚集夹具：真实 1.0，洗牌后期望
    #   ≈ Σshare² = 0.5（本例 0.6667），差距 ≥0.25 即"塌下来了"。
    _gc = ["a", "a", "a", "b", "b", "b"]
    _pc = ["a", "a", "a", "b", "b", "b"]
    rot_c = rotated_control(_gc, _pc)
    cases.append({"case": "F4c", "metric": "control_discriminates_when_clustered",
                  "expect": "gap>=0.25", "got": rot_c,
                  "ok": rot_c is not None and (1.0 - rot_c) >= 0.25})
    # F5：gold 再验证能抓住"gold 已变"（内存库造假行）
    # ⚠ 内存表必须带 note 列 —— _gold_now 查它（驳回语义分类用）。
    #   第一版漏了它 ⇒ sqlite3.Error 被记成"回查失败"⇒ F5a 假红
    #   （夹具缺列，不是判据的错）。
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE intent_samples (id INTEGER PRIMARY KEY, text TEXT, "
                "intent TEXT, hit_intent TEXT, note TEXT)")
    con.execute("INSERT INTO intent_samples VALUES (7,'画一张状态机图','design','design','')")
    sample = {"id": "intent_confirmed-001", "extractor": "intent_confirmed",
              "query": "画一张状态机图", "gold": "design", "source_id": 7}
    valid, stale = revalidate_gold(con, [sample])
    cases.append({"case": "F5a", "metric": "gold_unchanged_is_valid",
                  "expect": 1, "got": len(valid), "ok": len(valid) == 1})
    con.execute("UPDATE intent_samples SET intent='review' WHERE id=7")
    valid2, stale2 = revalidate_gold(con, [sample])
    cases.append({"case": "F5b", "metric": "gold_changed_is_stale",
                  "expect": 1, "got": len(stale2), "ok": len(stale2) == 1})
    con.execute("DELETE FROM intent_samples WHERE id=7")
    _v3, stale3 = revalidate_gold(con, [sample])
    cases.append({"case": "F5c", "metric": "row_deleted_is_stale",
                  "expect": 1, "got": len(stale3), "ok": len(stale3) == 1})
    con.close()
    # F7：驳回语义分类（池有效性 ≠ 路由纠错 —— 拿前者算重错率是
    #   用池卫生指标冒充路由指标，2026-10-22 实测 13 条全是前者）
    cases.append({"case": "F7a", "metric": "classify_pool_invalid",
                  "expect": "pool_invalid",
                  "got": classify_rejection("编排子任务的内部上下文/测试残留文本（非用户原话）"),
                  "ok": classify_rejection("内部上下文（非用户原话）") == "pool_invalid"})
    cases.append({"case": "F7b", "metric": "classify_routing_error",
                  "expect": "routing_error",
                  "got": classify_rejection("这文本应走 knowledge_qa 而不是 design"),
                  "ok": classify_rejection("应走 knowledge_qa") == "routing_error"})
    cases.append({"case": "F7c", "metric": "classify_empty_note",
                  "expect": "routing_error",
                  "got": classify_rejection(""),
                  "ok": classify_rejection("") == "routing_error"})
    # F8：weak 回归快照对比（纯函数，零依赖）
    # ⚠ 夹具规模必须 ≥ min_compared（默认 10）—— 交集过小函数本就返回
    #   None（不具判别力不比），拿 3 条夹具测默认路径会恒 None（第一版
    #   就这么踩了：F8a/F8b 假红，是夹具规模错不是判据错）。
    _b = [{"query": "q%d" % i, "pred": p} for i, p in enumerate(
        ["chat", "chat", "design", "chat", "chat", "chat", "review",
         "chat", "chat", "chat", "impact", "chat"], start=1)]
    _c_same = [{"query": d["query"], "pred": d["pred"]} for d in _b]
    _c_shift = [dict(d) for d in _c_same]
    _c_shift[2]["pred"] = "requirement_analysis"      # q3: design → req_analysis
    _c("F8a", "weak_stability", 1.0, weak_stability(_b, _c_same)["stability"])
    r8b = weak_stability(_b, _c_shift)
    _c("F8b", "weak_stability", 0.9167, r8b["stability"])
    cases.append({"case": "F8c", "metric": "shift_recorded",
                  "expect": 1, "got": len(r8b["shifts"]),
                  "ok": len(r8b["shifts"]) == 1
                  and r8b["shifts"][0] == {"query": "q3", "from": "design",
                                           "to": "requirement_analysis"}})
    # F8d：交集过小 ⇒ None（不具判别力不比，与 grounded 条件判定同构）
    r8d = weak_stability(_b[:2], _c_shift[:2] + [{"query": "q9", "pred": "chat"}],
                         min_compared=3)
    cases.append({"case": "F8d", "metric": "small_intersection_no_verdict",
                  "expect": None, "got": r8d["stability"],
                  "ok": r8d["stability"] is None and r8d["n_compared"] == 2})
    # F8e：无基线/旧格式 ⇒ None + note（首轮不判定）
    r8e = weak_stability(None, _c_same)
    cases.append({"case": "F8e", "metric": "no_baseline_no_verdict",
                  "expect": None, "got": r8e["stability"],
                  "ok": r8e["stability"] is None and bool(r8e["note"])})
    # F6：与共享指标封装一致（防"自己另写一套算法"漂移）。
    # ⚠ 期望值必须按共享封装的**输出精度**（round 4 位）手算，
    #   拿 2/3 原值比会因舍入恒假红 —— 夹具错，不是判据的错。
    m = _shared_metrics(["a", "b", "c"], ["a", "b", "x"])
    _c("F6", "shared_metrics_delegation", 0.6667, m["accuracy"])
    return all(c["ok"] for c in cases), cases


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════
def _dataset_id_for(set_path):
    """O2：从评测集同目录 manifest.json 取 dataset_id；取不到返回 None
    （不编造 —— null = 跑在未版本化旧集上）。"""
    mf = os.path.join(os.path.dirname(os.path.abspath(set_path)), "manifest.json")
    try:
        with open(mf, encoding="utf-8") as f:
            return json.load(f).get("dataset_id")
    except Exception:
        return None


def _load_trace(path):
    if not os.path.exists(path):
        print("⛔ 找不到 trace 评测集: %s" % path)
        print("   生成方式: .venv/Scripts/python.exe -X utf8 tools/eval/build_evalset.py")
        sys.exit(2)
    with io.open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema") != TRACE_SCHEMA:
        print("⛔ trace 评测集 schema 不匹配: %r（期望 %r）—— 请重建评测集"
              % (data.get("schema"), TRACE_SCHEMA))
        sys.exit(2)
    return data


def _connect_ro(db_path):
    if not os.path.exists(db_path):
        raise SystemExit("找不到数据库: %s" % db_path)
    uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/")
    return sqlite3.connect(uri, uri=True)


def main():
    ap = argparse.ArgumentParser(description="Trace 一致性评测（生产裁决 ↔ 当前路由）")
    ap.add_argument("--trace", default=DEFAULT_TRACE, help="trace 评测集 JSON")
    ap.add_argument("--db", default=os.environ.get("MBSE_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--save", action="store_true",
                    help="主指标落库 eval_reports（eval_kind=trace_consistency）")
    ap.add_argument("--no-llm", action="store_true",
                    help="停用 LLM 兜底（确定性口径：规则+语义层）")
    ap.add_argument("--weak-limit", type=int, default=50,
                    help="weak 回归集抽样条数（只出分布观测，不判定；0=不跑）")
    ap.add_argument("--baseline", default="",
                    help="上一轮 trace 报告 JSON（weak 回归快照对比用；缺省不对比）")
    ap.add_argument("--no-control", action="store_true", help="关闭负对照（不推荐）")
    ap.add_argument("--no-fixture", action="store_true", help="跳过黄金样本自证")
    ap.add_argument("--allow-stale-gold", action="store_true",
                    help="gold 失效样本仍计分（仅排查用；分数不得作为依据）")
    args = ap.parse_args()

    print("=" * 74)
    print("Trace 一致性评测（生产人工裁决 ↔ 当前意图路由）")
    print("=" * 74)

    # ── 0. 夹具自证（先跑：指标算错 ⇒ 后面数字不可信）──────────────
    fixture_ok, fixture_cases = (True, [])
    if not args.no_fixture:
        print("\n=== 黄金样本自证（F1~F8）===")
        fixture_ok, fixture_cases = run_fixture()
        for c in fixture_cases:
            print("  [%s] %s %s expect=%s got=%s"
                  % ("PASS" if c["ok"] else "FAIL", c["case"], c["metric"],
                     c["expect"], c["got"]))
        print("自证结论：%s" % ("通过" if fixture_ok else "未通过 —— 指标算错，"
                                                    "生产数字一律不可信"))
        if not fixture_ok:
            return 1

    # ── 1. 载入 trace 集 + gold 再验证 ────────────────────────────
    trace = _load_trace(args.trace)
    cases = trace.get("cases") or []
    strong = [c for c in cases if c.get("label") == "strong"]
    weak = [c for c in cases if c.get("label") == "weak"]
    pos = [c for c in strong if c.get("extractor") in _POS_EXTRACTORS]
    neg = [c for c in strong if c.get("extractor") in _NEG_EXTRACTORS]

    con = _connect_ro(args.db)
    try:
        valid_strong, stale = revalidate_gold(con, strong)
    finally:
        con.close()
    valid_map = {c["id"]: c for c in valid_strong}
    pos = [valid_map[c["id"]] for c in pos if c["id"] in valid_map]
    neg = [valid_map[c["id"]] for c in neg if c["id"] in valid_map]
    # 驳回样本按语义分流（口径复核：池有效性驳回不是路由错误）
    neg_err = [c for c in neg if c.get("reject_kind") == "routing_error"]
    neg_pool = [c for c in neg if c.get("reject_kind") == "pool_invalid"]

    print("\n=== 取材记账（排除了什么、为什么，全部可见）===")
    print("  %-26s %d" % ("trace_total", len(cases)))
    print("  %-26s %d" % ("strong_positive", len(pos)))
    print("  %-26s %d（路由纠错 %d / 池有效性 %d）"
          % ("strong_negative", len(neg), len(neg_err), len(neg_pool)))
    print("  %-26s %d" % ("weak", len(weak)))
    print("  %-26s %d" % ("gold_stale_excluded", len(stale)))
    for s in stale[:10]:
        print("      - %s: %s" % (s.get("id"), s.get("reason")))
    if neg_pool and not neg_err:
        print("  ⚠ 驳回样本全部是'非用户原话/不应入集'的池有效性裁决"
              "（人工没说路由判错）⇒ 重错率无有效分母，判 SKIP 不判 0")

    stale_ratio = (len(stale) / len(strong)) if strong else 0.0
    if not pos and not neg:
        print("\n✗ 无有效 strong 样本（全部失效或 trace 集为空）—— 没测到，不是质量差。")
        print("  常见原因：intent_samples 被清/改（gold 失效）或 trace 集需重建。")
        return 2
    if not pos:
        print("\n✗ 无有效 positive 样本 —— 一致率没有分母，不出分（没测到≠质量差）。")
        return 2

    # ── 1b. 载入基线报告（weak 回归快照对比用；口径不符则不对比）──────
    baseline = None
    if args.baseline:
        baseline = _load_baseline(args.baseline,
                                  llm_fallback="disabled" if args.no_llm else "enabled",
                                  backend=None)  # backend 在路由后才知道，先记口径无关部分

    # ── 2. 跑路由（生产库口径）────────────────────────────────────
    rt, IntentRouter = build_router(no_llm=args.no_llm)
    backend = semantic_backend(rt)
    print("\n=== 路由执行（%s 口径，语义后端 %s）==="
          % ("无LLM" if args.no_llm else "含LLM兜底", backend))
    t0 = time.time()
    strong_queries = [c["query"] for c in pos + neg]
    strong_dec, route_errors = route_queries(rt, IntentRouter, strong_queries)
    by_query = {d["query"]: d for d in strong_dec}
    print("  strong %d 条，耗时 %.1fs，路由异常 %d 条"
          % (len(strong_queries), time.time() - t0, route_errors))

    weak_dec, weak_errors = [], 0
    if args.weak_limit > 0 and weak:
        wk = weak[:args.weak_limit]
        weak_dec, weak_errors = route_queries(rt, IntentRouter, [c["query"] for c in wk])
        print("  weak 抽样 %d 条（分布观测，不判定），路由异常 %d 条"
              % (len(wk), weak_errors))

    # ── 3. 指标 ──────────────────────────────────────────────────
    pos_pairs, pos_detail = [], []
    for c in pos:
        d = by_query.get(c["query"]) or {}
        pos_pairs.append((c["gold"], d.get("pred", "")))
        pos_detail.append({"id": c["id"], "query": c["query"], "gold": c["gold"],
                           "pred": d.get("pred", ""), "route": d.get("route", ""),
                           "conf": d.get("conf"), "match": d.get("pred") == c["gold"]})
    pos_m = positive_agreement(pos_pairs)
    wrong = [d for d in pos_detail if not d["match"]]

    # 负样本：只有"路由纠错类"驳回才进重错率；池有效性驳回单独记账
    neg_detail, neg_pool_detail = [], []
    for c in neg:
        d = by_query.get(c["query"]) or {}
        item = {"id": c["id"], "query": c["query"], "gold": c["gold"],
                "pred": d.get("pred", ""), "route": d.get("route", ""),
                "conf": d.get("conf"), "reject_kind": c.get("reject_kind"),
                "reject_note": c.get("reject_note", "")}
        (neg_detail if c.get("reject_kind") == "routing_error"
         else neg_pool_detail).append(item)
    rep_rate, repeats = repeat_mistake_rate(neg_detail)

    # 负对照（纪律 3）：gold 固定种子洗牌（pred 不动）后重算
    control = {"enabled": not args.no_control, "rotated_agreement": None,
               "gap": None, "discriminates": None, "distinct_gold": None,
               "note": ""}
    if not args.no_control and pos_pairs:
        golds = [g for g, _ in pos_pairs]
        preds = [p for _, p in pos_pairs]
        control["distinct_gold"] = len(set(golds))
        rot = rotated_control(golds, preds)
        control["rotated_agreement"] = rot
        if rot is None:
            control["discriminates"] = False
            control["note"] = ("gold 意图仅 %d 类（需 ≥2）—— 任何重排恒等于自身，"
                               "对照不具判别力" % control["distinct_gold"])
        else:
            gap = round((pos_m["agreement"] or 0.0) - rot, 4)
            control["gap"] = gap
            control["discriminates"] = gap >= _ctl_gap()

    # weak 分布（观测）+ 逐条明细（供下轮回归快照对比）
    weak_dist = {"n": len(weak_dec), "intent_dist": {}, "route_dist": {},
                 "llm_route_count": 0, "chat_share": None,
                 "weak_detail": [{"query": d["query"], "pred": d["pred"],
                                  "route": d["route"]} for d in weak_dec]}
    for d in weak_dec:
        weak_dist["intent_dist"][d["pred"] or "(空)"] = \
            weak_dist["intent_dist"].get(d["pred"] or "(空)", 0) + 1
        weak_dist["route_dist"][d["route"] or "(空)"] = \
            weak_dist["route_dist"].get(d["route"] or "(空)", 0) + 1
        if (d["route"] or "").startswith("llm") or d["route"] in (
                "rule_llm_vetoed", "rule_llm_confirmed"):
            weak_dist["llm_route_count"] += 1
    if weak_dist["n"]:
        weak_dist["chat_share"] = round(
            weak_dist["intent_dist"].get("chat", 0) / weak_dist["n"], 4)

    # weak 回归快照对比（口径不符 ⇒ 不判定，见 _load_baseline）
    stab = {"n_compared": 0, "stability": None, "shifts": [], "note": ""}
    if baseline is not None:
        if baseline.get("_incompatible"):
            stab["note"] = baseline["_incompatible"]
        else:
            stab = weak_stability((baseline.get("weak") or {}).get("weak_detail"),
                                  weak_dist["weak_detail"])
            if baseline.get("semantic_backend") not in (None, backend):
                stab = {"n_compared": 0, "stability": None, "shifts": [],
                        "note": "基线的语义后端是 %s、本轮是 %s —— 两路余弦量纲"
                                "不同，稳定率不可比，不判定"
                                % (baseline.get("semantic_backend"), backend)}

    strong_route_dist = {}
    llm_route_count = 0
    for d in strong_dec:
        strong_route_dist[d["route"] or "(空)"] = \
            strong_route_dist.get(d["route"] or "(空)", 0) + 1
        if (d["route"] or "").startswith("llm") or d["route"] in (
                "rule_llm_vetoed", "rule_llm_confirmed"):
            llm_route_count += 1

    print("\n=== 指标（裁决一致率 / 重错率）===")
    print("  %-26s %s（n=%d）" % ("裁决一致率 agreement",
                                 pos_m["agreement"], pos_m["n"]))
    print("  %-26s %s" % ("macro_f1", pos_m["macro_f1"]))
    print("  %-26s %s（路由纠错类 n=%d；池有效性 %d 条不计入）"
          % ("重错率 repeat_mistake", rep_rate, len(neg_detail), len(neg_pool_detail)))
    if wrong:
        print("  不一致样本（前 10）：")
        for w in wrong[:10]:
            print("    - %-30s gold=%-18s got=%-18s route=%s conf=%s"
                  % (w["query"][:30], w["gold"], w["pred"], w["route"], w["conf"]))
    if repeats:
        print("  仍犯旧错（前 10）：")
        for r in repeats[:10]:
            print("    - %-30s 仍判成 %s（route=%s）"
                  % (r["query"][:30], r["gold"], r["route"]))
    print("  %-26s %s" % ("强标注路由层分布", json.dumps(strong_route_dist,
                                                    ensure_ascii=False)))
    if control["enabled"]:
        print("  %-26s 真实 %s vs 洗牌 %s ⇒ %s"
              % ("负对照", pos_m["agreement"], control["rotated_agreement"],
                 "可判别" if control["discriminates"] else
                 ("不具判别力（见 note）" if control["discriminates"] is False else "未判定")))
    if baseline is not None:
        print("  %-26s %s（交集 %d 条，变化 %d 条）%s"
              % ("weak 回归稳定率", stab["stability"], stab["n_compared"],
                 len(stab["shifts"]), stab["note"]))
        for s in stab["shifts"][:5]:
            print("    - %-30s %s → %s" % (s["query"][:30], s["from"], s["to"]))

    # ── 4. 报告 ──────────────────────────────────────────────────
    report = {
        "kind": "trace_consistency",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "db": os.path.basename(args.db),
        "trace_file": os.path.relpath(args.trace, ROOT).replace("\\", "/"),
        "trace_built_at": trace.get("built_at"),
        "dataset_id": _dataset_id_for(args.trace),
        "llm_fallback": "disabled" if args.no_llm else "enabled",
        "semantic_backend": backend,
        "population": {
            "trace_total": len(cases), "strong": len(strong),
            "positive": len(pos),
            "negative_total": len(neg), "negative_routing_error": len(neg_detail),
            "negative_pool_invalid": len(neg_pool_detail),
            "weak_total": len(weak), "weak_sampled": len(weak_dec),
            "gold_stale": len(stale), "gold_stale_ratio": round(stale_ratio, 4),
            "route_errors": route_errors + weak_errors,
            "strong_route_dist": strong_route_dist,
            "llm_route_count": llm_route_count,
        },
        "positive": {
            "n": pos_m["n"], "agreement": pos_m["agreement"],
            "macro_f1": pos_m["macro_f1"], "per_class": pos_m["per_class"],
            "confusion": pos_m["confusion"], "wrong": wrong,
        },
        "negative": {
            # ⚠ n 只数路由纠错类驳回。池有效性驳回（"非用户原话不应
            #    入集"）不是路由错误，放进分母会把"重错率"变成池卫生指标。
            "n": len(neg_detail), "repeat_mistake_rate": rep_rate,
            "repeats": repeats, "detail": neg_detail,
            "pool_invalid_detail": neg_pool_detail,
        },
        "weak": weak_dist,
        # weak 回归快照对比（诊断用，**不进门禁**：它是跑批间差值，
        # 有意的路由改动会让它动 —— 那种场景走使用指南 §4.3 的
        # before/after A/B 节奏，不靠 nightly 自动红）
        "weak_regression": {
            "baseline": os.path.relpath(args.baseline, ROOT).replace("\\", "/")
            if args.baseline else "",
            "n_compared": stab["n_compared"], "stability": stab["stability"],
            "shifts": stab["shifts"], "note": stab["note"],
        },
        # ↓ 这两个字段名与 eval_report_repo 的 trustworthy 判定契约一致
        #   （negative_control.recall 非空 且 fingerprint_drift 为空 ⇒ 可信）
        "negative_control": {
            "enabled": control["enabled"],
            "recall": control["rotated_agreement"],
            "gap": control["gap"],
            "discriminates": control["discriminates"],
            "distinct_gold": control["distinct_gold"],
            "note": control["note"],
        },
        # gold 级漂移（空 = 所有 strong 样本回查一致）。注意与检索评测的
        # 语料漂移语义不同：这里记的是"标签失效"，不是"池子变新"。
        "fingerprint_drift": [s.get("reason", "") for s in stale],
        "fixture_self_check": {"ok": fixture_ok, "cases": fixture_cases},
        "decisions": pos_detail + neg_detail + neg_pool_detail,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("\n报告: %s" % os.path.relpath(args.out, ROOT).replace("\\", "/"))

    if args.save:
        _save(report)
    if not fixture_ok:
        return 1
    return 0


def _ctl_gap():
    """负对照可判别的最小差距（core.config.eval_gates 唯一真源，惰性读）。"""
    try:
        from core.config import get as _g
        v = _g("eval_gates", "trace_control_min_gap", None)
        return float(v) if v is not None else 0.30
    except Exception:
        return 0.30


def _load_baseline(path, llm_fallback, backend):
    """载入上一轮 trace 报告作 weak 回归基线。口径不符 ⇒ 返回带
    `_incompatible` 标记的 dict（调用方据此不判定，而不是硬算）。

    为什么卡口径：含 LLM 与无 LLM 是两条链路（实测一致率差 ~5pp），
    语义后端 bigram/dense 余弦量纲不同 —— 跨口径比"稳定率"会把
    链路差异读成路由变化。旧格式报告（无 weak_detail）由
    weak_stability 的 note 兜底，不在这里卡。
    """
    if not os.path.exists(path):
        print("⚠ 基线报告不存在（%s）—— weak 回归对比跳过" % path)
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            rep = json.load(f)
    except Exception as e:
        print("⚠ 基线报告读取失败（%s）—— weak 回归对比跳过" % e)
        return None
    if rep.get("kind") != "trace_consistency":
        print("⚠ 基线报告不是 trace_consistency（kind=%r）—— 对比跳过"
              % rep.get("kind"))
        return None
    base_llm = rep.get("llm_fallback")
    if base_llm and base_llm != llm_fallback:
        return {"_incompatible": "基线的 LLM 口径是 %s、本轮是 %s —— 两条链路，"
                                 "稳定率不可比，不判定" % (base_llm, llm_fallback)}
    return rep


def _save(report):
    """主指标落库 eval_reports（eval_kind=trace_consistency）。

    列语义：entity_f1 ← 裁决一致率（与 intent_routing 把准确率存
    entity_f1 同构），trace_* 两列见 database/migrations/misc.py。
    detail ← 全量报告（wrong/repeats/分布/对照都在里面）。
    """
    from database.connection import get_db
    p, n = report["positive"], report["negative"]
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO eval_reports (doc_id, doc_name, entity_f1, relation_f1, "
            "detail, created_by, model_version, is_golden, eval_kind, "
            "trace_agreement_rate, trace_repeat_mistake_rate) "
            "VALUES (0,?,?,?,?,?,?,0,?,?,?)",
            ("Trace一致性评测 (strong %d 例: pos %d / neg %d)"
             % (p["n"] + n["n"], p["n"], n["n"]),
             p["agreement"], n["repeat_mistake_rate"],
             json.dumps(report, ensure_ascii=False),
             "tools/eval/eval_trace_consistency",
             "trace-eval %s" % report["db"],
             "trace_consistency",
             p["agreement"], n["repeat_mistake_rate"]))
        # O3：指标快照表（每指标一行，三色落库时算好；固定列照旧保留）
        from repositories.eval_report_repo import write_metric_rows
        write_metric_rows(conn, cur.lastrowid, "trace_consistency", {
            "trace_agreement_rate": p["agreement"],
            "trace_repeat_mistake_rate": n["repeat_mistake_rate"],
        })
        conn.commit()
        print("eval_reports 落库成功: id=%d [eval_kind=trace_consistency] "
              "agreement=%s repeat_mistake=%s"
              % (cur.lastrowid, p["agreement"], n["repeat_mistake_rate"]))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
