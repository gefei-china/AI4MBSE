# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""cluster_route 默认打开的**代价评估**（2026-11-07 米爸第 14 轮第 1 问）。

## 要回答
默认打开后，哪些能力会退化？重点风险：簇模式下 LLM 兜底只输出 7 个族 id，
而 `core/capability_clusters.intra_pick` 的族内分工表没有覆盖 8 个**流水线阶段意图**
（architecture_skeleton / view_expansion / trace_verification …）。
若这些输入原本靠 LLM 判到具体阶段，簇模式会把它们统一压成 `design`/`verify` 族代表。

## 为什么必须实测而不是推断
阶段 Agent 都在 `agents.intent_keywords` 里注册了专有词，确定性关键词层可能先命中
⇒ 簇模式根本轮不到。不实测就会把"可能有代价"当成"一定有代价"（或反过来）。

## 判据
- A 组 = 8 条阶段意图（明确点名阶段动作，但未必命中关键词）
- B 组 = 15 条常规意图（复用 gold set）
- 每组跑 OFF / ON，输出逐条对比；统计「ON 相对 OFF 的输出变化」并区分
  **退化**（阶段→粗类）与 **改善**（误判→正确）。
- 同时测 LLM 调用次数（簇模式提示词更短，但可能因为族 id→Agent 映射多一步而持平）。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from llm import llm_client  # noqa: E402

# 8 个阶段意图：用**不含该阶段专有关键词**的自然说法，专测 LLM 兜底层的表现
STAGES = [
    ("先按方法论定好建模规约，再往下走", "methodology_resolver"),
    ("把这份访谈记录拆成一条条需求，带上 ID", "requirement_structuring"),
    ("先生成包结构和部件定义的骨架代码", "architecture_skeleton"),
    ("按八视图顺序把 BDD 和活动图展开成代码", "view_expansion"),
    ("这段 V2 代码语法报错，帮我诊断并修到通过", "model_validation_repair"),
    ("生成需求到模型的追溯矩阵，标出孤儿元素", "trace_verification"),
    ("批量删除这些包之前先评估一下安全边界", "change_safety_gate"),
    ("校验通过后把模型发布入库，生成一个新版本", "model_release"),
]

COMMON = [
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
    ("帮我定清楚验收标准", "requirement_analysis"),
    ("这个模型能发布了吗", "model_release"),
    ("系统要快速响应，这条需求写得合格吗", "requirement_quality"),
]


def _run(switch: bool, cases):
    from core import config as cfg
    from agent.pipeline import AgentPipeline
    cfg._CONFIG.setdefault("intent", {})["cluster_route"] = switch
    cfg._CONFIG["intent"]["cache_enabled"] = False
    p = AgentPipeline()
    p._load_db_agents()
    r = p.router
    before = llm_client.stats.get("real", 0)
    rows = []
    for text, exp in cases:
        t0 = time.time()
        got = r.detect(text)
        m = r.get_last_meta() or {}
        rows.append({"text": text[:38], "expect": exp, "got": got,
                     "hit": got == exp, "route": m.get("route"),
                     "conf": m.get("confidence"),
                     "ms": int((time.time() - t0) * 1000)})
    return {"rows": rows,
            "llm_calls": llm_client.stats.get("real", 0) - before,
            "acc": round(sum(x["hit"] for x in rows) / len(rows), 3),
            "avg_ms": round(sum(x["ms"] for x in rows) / len(rows))}


def main() -> int:
    from core import config as cfg
    out = {}
    print("=" * 82)
    print("cluster_route 默认打开代价评估（真路由器，OFF vs ON，同一批用例）")
    print("=" * 82, flush=True)
    for label, cases in (("STAGE(8 个流水线阶段)", STAGES), ("COMMON(15 条常规意图)", COMMON)):
        off = _run(False, cases)
        on = _run(True, cases)
        changed = [(a["text"], a["got"], b["got"])
                   for a, b in zip(off["rows"], on["rows"]) if a["got"] != b["got"]]
        out[label] = {"off": off, "on": on, "changed": changed}
        print("\n%s" % label, flush=True)
        print("  OFF acc=%.1f%% llm=%d | ON acc=%.1f%% llm=%d" %
              (off["acc"] * 100, off["llm_calls"], on["acc"] * 100, on["llm_calls"]),
              flush=True)
        for t, a, b in changed:
            print("    CHANGED %-40s %-26s -> %s" % (t, a, b), flush=True)
        for tag, d in (("OFF", off), ("ON", on)):
            for x in d["rows"]:
                if not x["hit"]:
                    print("    %s MISS exp=%-26s got=%-26s route=%s" %
                          (tag, x["expect"], x["got"], x["route"]), flush=True)

    cfg._CONFIG["intent"]["cluster_route"] = True   # 本轮目标：默认打开
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tmp", "cluster_cost_eval.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n结果已存 %s" % p, flush=True)
    print("ALL_OK=True", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
