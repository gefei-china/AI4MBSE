# -*- coding: utf-8 -*-
"""多意图「两级切分」回归（2026-09-25）。

报障场景（用户原话）：提供一段需求，进行**需求分析**、**方案设计**、**代码校验** ——
这类「顿号并列清单」是用户最自然的写法，但修前 `_split_stage_clauses` 只认**阶段连词**
（先…再…/最后），于是 `detect_multi` 返回 None，三个阶段的活只跑一个。

本脚本用**真实 IntentRouter**（含 DB Agent 关键词，按真实请求路径 `_load_db_agents`）断言：
  T1 L1 阶段连词行为**零变更**（先…再…最后 仍按原样拆）
  T2 L2 并列清单能拆出 ≥2 个不同意图，且**顺序 == 用户表述顺序**
  T3 L2 背景句过滤：映射不到意图的片段（"提供一段需求"）不得被当成阶段
  T4 L2 不误报：同一意图的多个说法 / 单意图长句 → 不判多意图
  T5 detect_multi 契约：sequence 与 raw_subtasks 一一对应；raw_subtasks 仍是**字符串列表**
     （前端 11-pipeline.js 按字符串消费，改成 dict 会渲染 [object Object]）；新增 tasks
  T6 stage_hint 组装形式 = `{意图}（{该阶段原句}）`
  T7 detect() 主流程不受影响（多意图句仍能正常识别出单意图，不抛异常）
用法：.venv/Scripts/python.exe -X utf8 tests/manual_verify/verify_multi_intent_split.py
"""
import os
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.intent import IntentRouter  # noqa: E402
from agent.pipeline import AgentPipeline  # noqa: E402

OK, FAIL = [], []


def chk(name, cond, evidence=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  ← " + evidence) if evidence else ""))


pipe = AgentPipeline()
pipe._load_db_agents()
rt = pipe.router

print("── T1 L1 阶段连词：行为零变更 ──")
L1 = [
    ("先做需求分析，再输出 BDD 视图，最后出报告", ["requirement_analysis", "design", "report_generation"]),
    ("先解析需求，然后做方案设计", ["requirement_analysis", "design"]),
    ("需求分析并建模，最后出报告", ["requirement_analysis", "design", "report_generation"]),
]
for text, want in L1:
    got = rt.split_multi_intent(text)
    chk(f"「{text}」→ {want}", got == want, f"实得={got}")

print("── T2 L2 并列清单：报障句必须拆出 3 阶段且保序 ──")
L2 = [
    ("提供一段需求，进行需求分析、方案设计、代码校验",
     ["requirement_analysis", "design", "review"]),
    ("帮我解析需求，然后做方案设计、代码校验",
     ["requirement_analysis", "design", "review"]),
    ("需求分析、变更影响分析以及生成报告",
     ["requirement_analysis", "impact", "report_generation"]),
]
for text, want in L2:
    got = rt.split_multi_intent(text)
    chk(f"「{text}」→ {want}", got == want, f"实得={got}")

print("── T3 L2 背景句过滤：映射不到意图的片段不得成为阶段 ──")
tasks = rt.split_multi_tasks("提供一段需求，进行需求分析、方案设计、代码校验")
texts = [t["text"] for t in tasks]
chk("背景句「提供一段需求」被过滤（首阶段是「进行需求分析」）",
    texts and "进行需求分析" in texts[0] and "提供一段需求" not in texts[0],
    f"stages={texts}")
chk("阶段原句逐条可读（3 段）", len(texts) == 3, f"stages={texts}")

print("── T4 L2 不误报 ──")
REJECT = [
    "帮我生成这个系统的SysML v2模型代码，并输出 BDD 视图",   # 都是 design → 单意图
    "提供一段需求",                                          # 只有一个动作
    "生成结构树",                                            # 无分隔符
    "对巡飞弹做变更影响分析，评估影响范围",                    # 都是 impact → 单意图
    "你好",
]
for text in REJECT:
    got = rt.detect_multi(text)
    chk(f"「{text}」不判多意图", got is None, f"实得={None if got is None else got['sequence']}")

print("── T5 detect_multi 契约 ──")
m = rt.detect_multi("提供一段需求，进行需求分析、方案设计、代码校验")
chk("sequence 与 raw_subtasks 长度一致",
    m and len(m["sequence"]) == len(m["raw_subtasks"]),
    f"seq={m and m['sequence']} raw={m and m['raw_subtasks']}")
chk("raw_subtasks 仍是字符串列表（前端按字符串消费）",
    m and all(isinstance(x, str) for x in m["raw_subtasks"]))
chk("tasks 为 [{intent,text}] 且意图与 sequence 对齐",
    m and [t["intent"] for t in m["tasks"]] == m["sequence"]
    and all(isinstance(t["text"], str) and t["text"] for t in m["tasks"]),
    f"tasks={m and m['tasks']}")

print("── T6 stage_hint 组装形式 ──")
hint = [f"{t['intent']}（{t['text']}）" for t in m["tasks"]]
chk("每段形如 意图（原句）", all("（" in h and "）" in h and h.startswith(k)
                                for h, k in zip(hint, m["sequence"])), f"hint={hint}")
print("      实际注入 planner 的阶段序：" + " → ".join(hint))

print("── T7 detect() 主流程不受影响 ──")
for text in ["提供一段需求，进行需求分析、方案设计、代码校验", "帮我解析需求，然后做方案设计、代码校验"]:
    got = rt.detect(text)
    meta = rt.get_last_meta()
    chk(f"「{text}」仍能识别单意图", bool(got) and got in rt._db_intents,
        f"intent={got} route={meta['route']}")

print("── T8 多意图驱动编排门槛（_needs_orchestration）──")
# 关键证据：把 LLM 复杂度判定打成"不可用"（确定性），对比 multi 参数带来的差异 ——
# 证明"顿号清单"确实能**单独**触发编排，而不是靠 LLM 兜底碰巧为真。
from llm import llm_client as _llm  # noqa: E402

_orig_chat = _llm.chat
_llm.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("LLM 不可用（本项自证用）"))
try:
    s_multi = "提供一段需求，进行需求分析、方案设计、代码校验"
    s_single = "对这个系统做总体设计"
    base_multi = pipe._needs_orchestration(s_multi, "design", multi=None)
    with_multi = pipe._needs_orchestration(s_multi, "design", multi=rt.detect_multi(s_multi))
    base_single = pipe._needs_orchestration(s_single, "design", multi=None)
    with_single = pipe._needs_orchestration(s_single, "design", multi=rt.detect_multi(s_single))
finally:
    _llm.chat = _orig_chat
chk("LLM 不可用时，多阶段清单句 baseline=不编排 / 带多意图识别=编排", 
    base_multi is False and with_multi is True, f"baseline={base_multi} with_multi={with_multi}")
chk("单意图句不被多意图规则误判为编排", base_single is False and with_single is False,
    f"baseline={base_single} with_multi={with_single}")

print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)