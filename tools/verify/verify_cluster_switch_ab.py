# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""簇路由开关的**真路由器** A/B（不是模拟 prompt）。

对同一批 gold 用例，用 `IntentRouter.detect()` 真实跑两遍：
  · off = 现状（22 选 1 + few-shot + 阶段粒度）
  · on  = 能力簇（7 选 1 + 族内确定性分工）
记录：意图输出、route、置信、耗时、**LLM 调用次数**（从 llm_client.stats 取差值）、
     以及「开关是否改变了确定性快路径的结果」（应为 0 条变化 —— 零回归的核心判据）。

⚠️ 用进程内 config patch（core.config.patch），不写系统配置文件，跑完自动还原。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from llm import llm_client  # noqa: E402

GOLD = [
    ("帮我设计一下无人机地面控制站的系统架构，给两套方案对比", "design"),
    ("这个模型语法上有问题吗？帮我预评审一下，打个分", "review"),
    ("改了发动机的接口定义，会影响哪些部件？给我影响范围", "impact"),
    ("把刚才的分析整理成一份报告导出来", "report_generation"),
    ("我有个需求描述得很模糊：系统要快速响应。这条需求怎么定？验收标准怎么写？", "requirement_analysis"),
    ("SysML v2 里 part def 和 part 有什么区别？", "knowledge_qa"),
    ("这个平台怎么新建一个工程？在哪个菜单", "chat"),
    ("校验一下这段 V2 代码是否符合规范", "review"),
    ("把需求条目化一下", "requirement_structuring"),
    ("这份需求的模糊词有哪些", "requirement_quality"),
    ("通过 MCP 读取工程 AI验证006 的 V2 代码", "design"),
    ("生成需求到模型的追溯矩阵，看看有没有孤儿元素", "trace_verification"),
]


def _run(switch: bool):
    from core import config as cfg
    from agent.pipeline import AgentPipeline
    # 直接改进程内 _CONFIG（core.config 没有 patch 接口；这是纯内存，不落盘、不影响生产）
    cfg._CONFIG.setdefault("intent", {})["cluster_route"] = switch
    p = AgentPipeline()
    p._load_db_agents()
    r = p.router
    before = llm_client.stats.get("real", 0)
    rows = []
    for text, exp in GOLD:
        # 关掉意图缓存：否则同一句话第二次跑直接命中缓存，测不到 LLM 层。
        # cache_enabled 默认 False，这里显式置 False 双保险。
        cfg._CONFIG["intent"]["cache_enabled"] = False
        t0 = time.time()
        got = r.detect(text)
        m = r.get_last_meta() or {}
        rows.append({"text": text[:30], "expect": exp, "got": got,
                     "hit": got == exp, "route": m.get("route"),
                     "conf": m.get("confidence"),
                     "ms": int((time.time() - t0) * 1000)})
    used_llm = llm_client.stats.get("real", 0) - before
    return {"rows": rows, "llm_calls": used_llm,
            "acc": round(sum(x["hit"] for x in rows) / len(rows), 3),
            "avg_ms": round(sum(x["ms"] for x in rows) / len(rows))}


def main() -> int:
    from core import config as cfg
    print("=" * 78)
    print("簇路由开关 A/B（真路由器 IntentRouter.detect，12 条 gold）")
    print("=" * 78)
    off = _run(False)
    on = _run(True)
    cfg._CONFIG["intent"]["cluster_route"] = False   # 还原零回归位
    cfg._CONFIG["intent"]["cache_enabled"] = False

    for label, d in (("OFF(现状 22选1)", off), ("ON(能力簇 7选1)", on)):
        print("%-20s acc=%.1f%% avg=%dms llm_calls=%d" %
              (label, d["acc"] * 100, d["avg_ms"], d["llm_calls"]))
        for x in d["rows"]:
            if not x["hit"]:
                print("    MISS exp=%-24s got=%-24s route=%s" %
                      (x["expect"], x["got"], x["route"]))

    # 核心判据：确定性快路径（rule/keyword/semantic/continue）结果必须逐条相同
    DET = ("rule", "keyword", "semantic", "continue", "platform", "cache", "explain")
    drift = []
    for a, b in zip(off["rows"], on["rows"]):
        ra, rb = str(a["route"] or ""), str(b["route"] or "")
        if ra.startswith(DET) and (a["got"], ra) != (b["got"], rb):
            drift.append((a["text"], a["got"], ra, b["got"], rb))
    print("-" * 78)
    print("确定性快路径漂移：%d 条（应为 0）" % len(drift))
    print("准确度：%.1f%% → %.1f%%" % (off["acc"] * 100, on["acc"] * 100))
    print("LLM 调用：%d → %d" % (off["llm_calls"], on["llm_calls"]))
    # ⚠️ 本脚本的 avg_ms **不可用于给簇化报提速**：实测第一轮撞上 embedding 服务
    #    502 → bigram 降级，第二轮已进 60s 短熔断 ⇒ 1152ms vs 262ms 主要是这个
    #    时序差，不是簇路由的收益。纯 LLM 层的可信数据见 verify_intent_placement_ab.py
    #    （993→931ms，-6.2%，同一 LLM/温度/样本，无 embedding 干扰）。
    print("⚠️ 延迟不作为结论：首轮 embedding 502 降级 + 次轮熔断是混杂变量；"
          "纯 LLM 层收益以 verify_intent_placement_ab.py 为准")

    out = {"off": off, "on": on, "deterministic_drift": drift}
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tmp", "cluster_switch_ab.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    ok = (not drift) and on["acc"] >= off["acc"] and cfg.get("intent", "cluster_route", None) is False
    print("ALL_OK=%s  （配置已还原 cluster_route=False）" % ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
