# -*- coding: utf-8 -*-
"""意图识别回归：说明类提问 vs 建模动作类（2026-09-25 修「MBSE建模方法论介绍」误路由 design）。

报障原文：「我输入"MBSE建模方法论介绍"直接匹配到了建模方案设计agent，而我的目的并不是这个」。
本脚本用**真实 AgentPipeline 的 router**（含 DB 关键词/语义索引/规则表）跑断言：
  正例（说明类 → knowledge_qa）：不能被 design 的泛词「建模」劫持
  反例（动作类 → design/impact/report）：不能被新加的"说明类优先"抢走

★ 2026-11-12（米爸反馈1，conv 9571/msg 4698）：第 5 段新增——
  附件任务句「…请基于如上需求分析的框架、流程与规则，帮我梳理出清晰的条目话需求清单」
  （带附件 doc 847）因「流程」命中 _EXPLAIN_SIGNALS 被抢去 knowledge_qa，
  附件交给知识问答 Agent 空转（"上传附件无法正常解析识别消费"的用户观感）。
  修法：_ACTION_SIGNALS 补「梳理/条目/清单」三个交付物动作词。
  第 5 段同时锁 A/B 零回退（确定性层逐条比对）与 explain 正例防误伤。
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

print("── 5. #52 附件任务句：动作信号兜底 + explain 正例防误伤 + 确定性层 A/B ──")
MSG4698 = ("基于上传的附件文档，这是一份客户的需求文档，请基于如上需求分析的框架、"
           "流程与规则，帮我梳理出清晰的条目话需求清单")
c = get_db()
try:
    got = rt.detect(MSG4698, conn=c)
    meta = rt.get_last_meta()
finally:
    c.close()
chk("5a 症状1 实证（msg 4698 原话，带 doc 847 附件）→ requirement_analysis",
    got == "requirement_analysis",
    f"实得 intent={got} route={meta['route']} conf={meta['confidence']}")
chk("5a2 且不是 explain 抢占（route 不得为 explain）", meta["route"] != "explain",
    f"route={meta['route']}")

EXPLAIN52 = [
    "介绍一下需求分析的流程",
    "这份附件里讲的流程是什么",   # 带附件的**概念问**：仍须走知识问答（附件此时是阅读对象）
    "什么是MBSE",
    "需求分析的原理是什么",
]
for s in EXPLAIN52:
    c = get_db()
    try:
        got = rt.detect(s, conn=c)
        meta = rt.get_last_meta()
    finally:
        c.close()
    chk(f"5b explain 正例防误伤「{s[:18]}…」→ knowledge_qa/explain",
        got == "knowledge_qa" and meta["route"] == "explain",
        f"实得 intent={got} route={meta['route']}")

r0 = IntentRouter()
chk("5c 单元：梳理+条目+流程（无其他动作词）→ False（不再判说明类）",
    r0._is_explain_ask("基于需求分析的流程帮我梳理条目化需求清单") is False)
chk("5c2 单元：流程+是什么（纯概念问）→ True（说明类判定不受损）",
    r0._is_explain_ask("需求分析的流程是什么") is True)

# 5d 确定性层 A/B：主评测集 confirmed 样本中，基线走**确定性路由**（rule/rule_scored/
#   explain/fused）的那些，加动作信号后必须逐条不变。LLM 兜底路线（llm*/semantic*）
#   本身带非确定性（mock/真实服务波动），不纳入比对——本改动只可能经 explain 门与
#   _db_kw_priority 影响确定性层，故该子集即为"零回退"的严格判据。
c = get_db()
try:
    rows = c.execute("SELECT text,intent FROM intent_samples "
                     "WHERE status='confirmed' AND text IS NOT NULL").fetchall()
finally:
    c.close()
SAMPLES = [(r["text"], r["intent"]) for r in rows]
det = []
for text, want in SAMPLES:
    got = rt.detect(text, conn=c)
    meta = rt.get_last_meta()
    if meta["route"] in ("rule", "rule_scored", "explain", "fused", "fused_conflict"):
        det.append((text, want, got))
ORIG_ACT = IntentRouter._ACTION_SIGNALS
try:
    IntentRouter._ACTION_SIGNALS = ORIG_ACT + ("梳理", "条目", "清单")
    changed = []
    for text, want, base_got in det:
        got = rt.detect(text, conn=c)
        if got != base_got:
            changed.append((text[:36], base_got, got))
finally:
    IntentRouter._ACTION_SIGNALS = ORIG_ACT
chk(f"5d 确定性层 A/B 零回退（n={len(det)}，加动作信号后逐条不变）",
    not changed, f"变化项：{changed}" if changed else "")
chk("5e 动作信号已入生产 _ACTION_SIGNALS（梳理/条目/清单）",
    all(k in IntentRouter._ACTION_SIGNALS for k in ("梳理", "条目", "清单")))

print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)