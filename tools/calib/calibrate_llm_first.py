#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""llm_first 置信度标定（2026-10-23，P2）—— 把"0.75 是拍的"变成"0.75 是测的"。

## 为什么需要
`intent.llm_first_min_conf=0.75` 是上线时的保守起点（介于旧 0.85 与被实测否决的
0.60 之间），**从未用标注数据校准过**。弃权与校准的行业共识：LLM 自报置信普遍
过自信，必须用标注数据重映射门槛——self-reported ≠ calibrated。

## 方法（直连 detect_llm 取未截断分，吸取 probe_gate_calib 的"截断分布"教训）
1. 从真库 intent_samples（status=confirmed）取**真实说法 + 人工裁决**；
2. 每条先走完整 detect（llm_first 生产口径），记录 route/conf/对错；
3. 对"llm 主裁判给出结论"的样本，按置信分桶统计**分桶准确率**；
4. 给出标定建议：直接采纳门槛应设在"分桶准确率 ≥ 生产可接受线（默认 0.90）
   的最低桶边界"；澄清带随动。

## 输出
分桶表（0.50-0.60/0.60-0.70/0.70-0.80/0.80-0.90/0.90-1.00 × n/acc）+ 建议门槛
+ 不一致明细（verdict 标注）。**只读不改**：不写任何配置，建议供人拍板。

用法：
    .venv/Scripts/python.exe -X utf8 tools/calib/calibrate_llm_first.py
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]
MIN_PROD_ACC = 0.90   # 直接采纳桶的生产可接受线（与 golden 0.931 同量级）
MIN_N_PER_BUCKET = 5  # 桶内样本不足则不具判别力，不参与门槛建议

# 族容差（2026-10-23 标定发现）：流水线阶段意图语义上是父粗类的**特化**
# （"生成结构树"→architecture_skeleton、"追溯矩阵检查"→trace_verification
# 在语义上是对的，gold 只反映了流水线上线前的旧分类）。评分时阶段意图
# 命中父粗类算对 —— 这才是生产真实口径；"是否按阶段分派"属编排层决策。
FAMILY = {
    "methodology_resolver": "requirement_analysis",
    "requirement_structuring": "requirement_analysis",
    "architecture_skeleton": "design",
    "view_expansion": "design",
    "model_validation_repair": "review",
    "trace_verification": "review",
    "change_safety_gate": "impact",
    "model_release": "design",
}


def _ok(want, got):
    return got == want or FAMILY.get(got) == want


def main():
    real_db = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(real_db):
        print("mbse.db 不存在 → 无法标定（需要生产 confirmed 池）")
        return 2
    import sqlite3
    con = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT text, intent FROM intent_samples "
        "WHERE status='confirmed' AND intent IS NOT NULL AND intent != '' "
        "AND text IS NOT NULL AND intent != 'chat' ORDER BY id").fetchall()
    con.close()
    if len(rows) < 15:
        print(f"confirmed 样本仅 {len(rows)} 条（<15）→ 不具标定力，先攒样本")
        return 2

    from agent.pipeline import AgentPipeline
    from agent.intent import IntentRouter
    pipe = AgentPipeline()
    pipe._load_db_agents()
    rt = pipe.router
    from database import get_db
    rt.load_rules(get_db())
    og, os_ = IntentRouter._cache_get, IntentRouter._cache_set
    IntentRouter._cache_get = lambda self, *a, **k: None
    IntentRouter._cache_set = lambda self, *a, **k: None

    print("=" * 76)
    print("llm_first 置信度标定（confirmed 池 n=%d，生产口径 llm_first=开）" % len(rows))
    print("=" * 76)
    obs = []   # (conf, ok, text, want, got, route, verdict)
    n_llm = 0  # LLM 主裁判真正给出结论的条数
    try:
        for text, want in rows:
            got = rt.detect(str(text))
            meta = rt.get_last_meta() or {}
            route, conf = meta.get("route", ""), float(meta.get("confidence") or 0)
            llm_judged = route in ("llm_first", "llm_first_weak", "llm", "llm_history",
                                   "llm_conflict", "llm_weak")
            if llm_judged:
                n_llm += 1
                obs.append((conf, _ok(want, got), str(text), want, got, route,
                            meta.get("verdict", "")))
    finally:
        IntentRouter._cache_get, IntentRouter._cache_set = og, os_

    print(f"完整 detect 口径：LLM 主裁判给出结论 {n_llm}/{len(rows)} 条"
          f"（其余被规则/语义/继承截走——confirmed 样本天然多含强信号词）")
    if n_llm < 10:
        # 回退口径：**直连 detect_llm** 测 LLM 裁判本身的分带准确率（含 few-shot）。
        # 这才是标定要的量：P(LLM 对 | LLM 自报置信带) —— 规则层截走多少不影响它。
        # 教训来源：probe_gate_calib 的"截断分布"——只在被采纳时记分会把最小值
        # 钳在门槛上；直连取未截断分才是真分布。
        print("detect 口径样本不足 → 回退**直连 detect_llm** 口径（测 LLM 裁判本身）")
        obs = []
        try:
            for text, want in rows:
                intent, conf = rt.detect_llm(str(text))
                if intent and intent != "":
                    obs.append((float(conf or 0), _ok(want, intent), str(text), want,
                                intent, "direct", ""))
        except Exception as e:
            print("直连探测失败：%s" % e)
        n_llm = len(obs)
        print(f"直连口径：LLM 可判 {n_llm}/{len(rows)} 条（其余解析失败/服务异常，如实计数）")
    if n_llm < 10:
        print("LLM 结论样本不足（<10）→ 无法标定（可能是 LLM 服务不可用，如实报告）")
        return 2

    print("\n分桶（含 llm 全部路由）—— 自报置信 vs 实际对错：")
    print("  %-12s %5s %6s %8s" % ("置信带", "n", "对", "准确率"))
    bucket_acc = {}
    for lo, hi in BUCKETS:
        band = [o for o in obs if lo <= o[0] < hi]
        acc = (sum(1 for o in band if o[1]) / len(band)) if band else None
        bucket_acc[(lo, hi)] = (len(band), acc)
        print("  %-12s %5d %6d %8s" % (
            "%.2f-%.2f" % (lo, hi), len(band), sum(1 for o in band if o[1]),
            ("%.3f" % acc) if acc is not None else "n/a"))

    # 门槛建议：从高到低找第一个"n 达标且 acc≥线"的桶下界
    suggest = None
    for lo, hi in BUCKETS:
        n, acc = bucket_acc[(lo, hi)]
        if n >= MIN_N_PER_BUCKET and acc is not None and acc >= MIN_PROD_ACC:
            suggest = lo
            break
    overall_ok = sum(1 for o in obs if o[1])
    print("\nLLM 结论整体准确率：%.3f（%d/%d）" % (overall_ok / len(obs), overall_ok, len(obs)))
    if suggest is not None:
        cur = 0.75
        action = ("维持" if abs(suggest - cur) < 1e-9 else
                  (f"可下调至 {suggest:.2f}" if suggest < cur else f"应上调至 {suggest:.2f}"))
        print(f"标定建议：llm_first_min_conf → {suggest:.2f}（{action}；当前 0.75）。")
        print("  说明：这是该数据上的分界，样本增长后应重跑本脚本复核（预期随"
              " few-shot 飞轮改善而松动）。")
    else:
        print("标定建议：没有任何达标桶 —— 维持 0.75 或上调，并优先补 few-shot 样本。")

    bad = [o for o in obs if not o[1]]
    if bad:
        print(f"\n不一致明细（{len(bad)} 条，verdict 标注错得诚实与否）：")
        for conf, ok, text, want, got, route, verdict in bad[:10]:
            print("  [%.2f %s→%s|%s|%s] %s" % (conf, want, got, route, verdict, text[:44]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
