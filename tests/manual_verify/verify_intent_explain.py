# -*- coding: utf-8 -*-
"""意图识别回归：说明类提问 vs 建模动作类（2026-09-25 修「MBSE建模方法论介绍」误路由 design）。

报障原文：「我输入"MBSE建模方法论介绍"直接匹配到了建模方案设计agent，而我的目的并不是这个」。
本脚本用**真实 AgentPipeline 的 router**（含 DB 关键词/语义索引/规则表）跑断言：
  正例（说明类 → knowledge_qa）：不能被 design 的泛词「建模」劫持
  反例（动作类 → design/impact/report）：不能被新加的"说明类优先"抢走
用法：.venv/Scripts/python.exe -X utf8 tests/manual_verify/verify_intent_explain.py
"""
import os
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.intent import IntentRouter  # noqa: E402
from agent.pipeline import AgentPipeline  # noqa: E402
from database import get_db  # noqa: E402

OK, FAIL = [], []


def chk(name, cond, evidence=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  ← " + evidence) if evidence else ""))


pipe = AgentPipeline()
# 2026-09-25 修正保真度：必须按真实请求路径注入 DB Agent 关键词与语义索引
# （`AgentPipeline.__init__` 只建 router；stream.py 里是 `self._load_db_agents(user)`）。
pipe._load_db_agents()
rt = pipe.router

print("── 1. 说明/介绍类：必须走 knowledge_qa（报障场景 + 同类）──")
EXPLAIN = [
    "MBSE建模方法论介绍",
    "什么是MBSE建模方法论",
    "介绍一下SysML v2的作用",
    "MBSE建模方法论与传统方法的区别",
    "MBSE方法论包含哪些建模活动",
    "需求追溯的原理是什么",
    "变更影响分析的用途",
    "SysML v2 和 SysML v1 有何区别",
]
for s in EXPLAIN:
    c = get_db()
    try:
        got = rt.detect(s, conn=c)
        meta = rt.get_last_meta()
    finally:
        c.close()
    chk(f"「{s}」→ knowledge_qa", got == "knowledge_qa",
        f"实得 intent={got} route={meta['route']} conf={meta['confidence']}")

print("── 2. 动作/建模类正例：不能被评为说明类（回归防线）──")
ACTION = [
    ("请设计星网宽带通信系统的架构方案", "design"),
    ("帮我生成这个系统的SysML v2模型代码", "design"),
    ("输出一份 BDD 视图", "design"),
    ("分析一下这个需求的变更影响", "impact"),
    ("对巡飞弹做变更影响分析", "impact"),
    ("生成一份需求分析报告", "report_generation"),
    ("做一次需求质量评审", "requirement_quality"),
    # ⚠️ 行为变更（2026-09-25，eval_intent_routing.py 驱动）：「评审/校验」新增强信号前置后，
    #    本句由 requirement_analysis 改为 **review**（"评审"是动作明确的意图，语义上更正确）。
    #    注意 V2.6 的既有决定仍成立：**"验收标准"不算 requirement_quality 的强信号**
    #    （历史上它会误触发质量评审卡，见 agent/intent.py 的 V2.6 注释）——所以本句不是 quality。
    ("评审一下这个需求的验收标准", "review"),
]
for s, want in ACTION:
    c = get_db()
    try:
        got = rt.detect(s, conn=c)
        meta = rt.get_last_meta()
    finally:
        c.close()
    chk(f"「{s}」→ {want}", got == want, f"实得 intent={got} route={meta['route']}")

print("── 3. 判定器单元级边界（_is_explain_ask 纯函数，不依赖链路）──")
r0 = IntentRouter()
chk("含说明信号且无动作词 → True", r0._is_explain_ask("mbse建模方法论介绍") is True)
chk("说明+动作混用（介绍代码生成）→ False（保守：不抢 design 正例）",
    r0._is_explain_ask("介绍一下sysmlv2代码生成") is False)
chk("纯动作句 → False", r0._is_explain_ask("帮我生成模型代码") is False)
chk("空串 → False", r0._is_explain_ask("") is False)

print("── 4. 缓存不挡新逻辑（报障输入的旧误判缓存不得复用）──")
c = get_db()
try:
    # 连跑两次：第 2 次若命中缓存，仍必须是 knowledge_qa（说明类判定在缓存之前）
    a = rt.detect("MBSE建模方法论介绍", conn=c)
    b = rt.detect("MBSE建模方法论介绍", conn=c)
    m2 = rt.get_last_meta()
finally:
    c.close()
chk("连跑两次结果一致且均为 knowledge_qa", a == "knowledge_qa" and b == "knowledge_qa",
    f"1st={a} 2nd={b} (2nd route={m2['route']})")

print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)