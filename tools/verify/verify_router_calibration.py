# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""代码级 Router 优化点 ①：**置信度校准实测**（2026-11-07 米爸第 14 轮第 4 问）。

## 为什么要测
`agent/intent.py` 的 B5-7 注释明确写着：规则层 `_rule_done(intent,"rule",0.95,...)`
里的 0.95 是**常量不是测量值**。行业（Eucalipse 生产路由 playbook）要求：
「0.9+ 档的准确率应 >97%，否则阈值在撒谎」。
若平台 0.95 档实际准确率明显低于 0.95，则所有依赖 confidence 的下游
（澄清门 / llm_first 互证 / 编排信任度）都建立在假数字上。

## 判据
用带标注 gold set 跑真路由器，按 `(route, confidence)` 分档统计实际准确率，
输出 ECE（Expected Calibration Error）与倒挂档位。纯观测，不改写任何配置。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
    ("按八视图顺序把 BDD 和活动图展开成代码", "view_expansion"),
    ("先生成包结构和部件定义的骨架代码", "architecture_skeleton"),
    ("这段 V2 代码语法报错，帮我诊断并修到通过", "model_validation_repair"),
    ("批量删除这些包之前先评估一下安全边界", "change_safety_gate"),
    ("校验通过后把模型发布入库，生成一个新版本", "model_release"),
    ("帮我定清楚验收标准", "requirement_analysis"),
    ("系统要快速响应，这条需求写得合格吗", "requirement_quality"),
    ("建模前该按哪套方法论走？给我个规约卡", "design"),
]


def main() -> int:
    from core import config as cfg
    from agent.pipeline import AgentPipeline
    cfg._CONFIG.setdefault("intent", {})["cache_enabled"] = False
    p = AgentPipeline()
    p._load_db_agents()
    r = p.router

    rows = []
    for text, exp in GOLD:
        got = r.detect(text)
        m = r.get_last_meta() or {}
        conf = float(m.get("confidence") or 0.0)
        rows.append({"text": text[:30], "exp": exp, "got": got,
                     "hit": got == exp, "route": str(m.get("route") or ""),
                     "conf": conf})

    # 按置信分档
    BINS = [(0.90, 1.01, "0.90+"), (0.75, 0.90, "0.75~0.90"),
            (0.50, 0.75, "0.50~0.75"), (0.0, 0.50, "<0.50")]
    print("=" * 84)
    print("置信度校准实测（%d 条 gold，真路由器）" % len(rows))
    print("=" * 84)
    ece_num = 0.0
    for lo, hi, label in BINS:
        g = [x for x in rows if lo <= x["conf"] < hi]
        if not g:
            print("  %-12s  n=0" % label)
            continue
        acc = sum(x["hit"] for x in g) / len(g)
        avg_conf = sum(x["conf"] for x in g) / len(g)
        ece_num += len(g) * abs(avg_conf - acc)
        flag = "  << 校准倒挂" if avg_conf - acc > 0.15 else ""
        print("  %-12s  n=%-3d 平均conf=%.3f 实际acc=%.3f 偏差=%+.3f%s"
              % (label, len(g), avg_conf, acc, avg_conf - acc, flag))
    ece = ece_num / len(rows)
    print("\n  ECE = %.3f（越接近 0 越可信；>0.10 视为阈值不可用）" % ece)

    # 按 route 看「常量置信」的档
    print("\n  按 route 分组：")
    routes = {}
    for x in rows:
        routes.setdefault(x["route"], []).append(x)
    for k, g in sorted(routes.items(), key=lambda kv: -len(kv[1])):
        acc = sum(x["hit"] for x in g) / len(g)
        confs = sorted({x["conf"] for x in g})
        print("    %-20s n=%-3d acc=%.2f conf取值=%s"
              % (k, len(g), acc, [round(c, 2) for c in confs]))

    # 明确报出「0.95 常量」问题
    hi = [x for x in rows if x["conf"] >= 0.90]
    if hi:
        acc_hi = sum(x["hit"] for x in hi) / len(hi)
        print("\n  高置信档(≥0.90)：n=%d acc=%.1f%%  —— 行业要求 >97%%；%s"
              % (len(hi), acc_hi * 100,
                 "达标" if acc_hi >= 0.97 else "**未达标，阈值虚高**"))

    out = {"n": len(rows), "ece": round(ece, 4), "rows": rows}
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tmp", "router_calibration.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n结果已存 %s" % path)
    print("ALL_OK=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
