# -*- coding: utf-8 -*-
"""V2 前置：operation 槽位可推导性实测。

目的
----
v3.0 的顶层分流（域→通路）依赖 `operation` 槽位。**槽位从哪来？**
上轮我提的方案是「关键词路由 → operation」，但V7 评估已证明关键词路由不该动。
本脚本用**真实语料**回答一个更基础的问题：

    operation 能否从现有路由结果（intent + route 路径）**可靠推导**？

若不能 ⇒ V2 的判据不成立，必须换思路（让 LLM 直接出operation，或问用户）。
若能   ⇒ 给出可用的推导规则 + 置信度，供 V2 实现。

方法
----
语料来源：`intent_samples` 里 status='confirmed' 的 44 条真实样本
（不自己编 —— 评测集必须与生产语料同源）。
每条样本跑真实 `AgentPipeline` 路由（会加载语义索引 + 清 intent_cache），
再对照我设计的 operation 枚举做人工标注的映射，检查可推导性。

用法
----
    ./.venv/Scripts/python.exe -X utf8 tools/eval/probe_operation_slot.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

# v3.0 定义的 operation 枚举
OPERATIONS = ["create", "analyze", "modify", "delete", "repair", "verify", "explain", "view"]

# 人工标注：intent →期望 operation（依据 intent_samples 的语义与 v3.0 §2.1 映射表）
# 标注规则写在EXPECT_FROM_INTENT 里，可复核
EXPECT_FROM_INTENT = {
    "design": "create",              # 生成模型
    "requirement_analysis": "create",  # 产出需求条目
    "requirement_quality": "analyze",  # 质量分析= 只读诊断
    "review": "verify",              # 校验
    "impact": "analyze",             # 影响分析 = 只读
    "knowledge_qa": "explain",       # 知识问答
    "chat": "explain",               # 闲聊
    "system_mgmt": "analyze",        # 系统查询= 只读
    "report_generation": "create",   # 产出报告文件
}


def probe():
    from database import get_db
    from agent.pipeline import AgentPipeline

    conn = get_db()
    rows = conn.execute(
        "SELECT text, intent FROM intent_samples "
        "WHERE status='confirmed' AND intent<>'' ORDER BY id").fetchall()

    pipe = AgentPipeline()
    pipe._load_db_agents()          # 必须：否则语义索引空，测的是退化层
    rt = pipe.router

    out = []
    for r in rows:
        text = r["text"]
        want_intent = r["intent"]
        try:
            conn.execute("DELETE FROM intent_cache")
            conn.commit()
        except Exception:
            pass
        got = rt.detect(text, conn=conn)
        meta = rt.get_last_meta() or {}
        exp_op = EXPECT_FROM_INTENT.get(want_intent)
        got_op = EXPECT_FROM_INTENT.get(got)      # 推导规则：intent → operation
        out.append({
            "text": text[:60],
            "want_intent": want_intent,
            "got_intent": got,
            "route": meta.get("route"),
            "expect_op": exp_op,
            "derived_op": got_op,
            "op_match": (exp_op == got_op) if (exp_op and got_op) else None,
        })
    conn.close()
    return out


def main() -> int:
    print("=" * 72)
    print("V2 前置实测：operation 槽位能否从路由结果可靠推导？")
    print("=" * 72)
    data = probe()
    if not data:
        print("  [FAIL] 样本池为空，无法评估")
        return 2

    n = len(data)
    intent_ok = sum(1 for d in data if d["want_intent"] == d["got_intent"])
    op_ok = sum(1 for d in data if d["op_match"])

    print(f"\n  样本数：{n}（来自 intent_samples confirmed，不自编）")
    print(f"\n  ① 意图识别正确：{intent_ok}/{n}  ({intent_ok/n:.1%})")

    print(f"\n  ② operation 推导正确：{op_ok}/{n}  ({op_ok/n:.1%})")
    print("     推导规则：operation = f(路由得到的 intent)，映射表见脚本 EXPECT_FROM_INTENT")
    print("     （前提：intent 判对。若 intent 判错，operation 必然错 —— 二者误差同源）")

    print(f"\n  ③ 误差分解：")
    print(f"     意图判错导致 operation 错：{n - intent_ok} 条")
    print(f"     意图对但 operation 映射错：{op_ok - intent_ok} 条"
          if op_ok >= intent_ok else "     （出现负值 ⇒ 映射表有误，需修正）")

    # 分operation 列出失败样本，供人工复核
    bad = [d for d in data if not d["op_match"]]
    if bad:
        print(f"\n  ④ 推导失败的 {len(bad)} 条（逐条可复核）：")
        for d in bad:
            print(f"     · 「{d['text']}」")
            print(f"       期望 intent={d['want_intent']} → op={d['expect_op']}"
                  f" | 实得 intent={d['got_intent']} → op={d['derived_op']}"
                  f" | route={d['route']}")

    print("\n" + "=" * 72)
    verdict = ("operation 可从 intent 可靠推导（阈值 0.90）⇒ V2 判据成立"
               if op_ok / n >= 0.90 else
               f"operation 推导准确率 {op_ok/n:.1%} < 0.90 ⇒ **V2 判据不成立**，"
               "须换思路（LLM 直接出 operation / 追问用户）")
    print(verdict)
    print("=" * 72)
    return 0 if op_ok / n >= 0.90 else 1


if __name__ == "__main__":
    sys.exit(main())
