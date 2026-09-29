# -*- coding: utf-8 -*-
"""意图确认（「确定不了就别硬选」）回归 —— 2026-09-26，用户要求。

## 要保证什么
系统**只在"自己没把握"时**才停下来问，且问的时候给的是"选择题 + 其他/自定义"（复用内容级澄清卡）。
既不能"硬选一个"去执行（用户的原意），也不能到处弹卡打扰（每次对话都拦一下同样是坏的）。

判定面（`_should_confirm_intent`）：
  问：llm_weak（LLM 给了意图但 <0.85）/ fused_conflict（关键词与语义打架）/ semantic_weak /
      llm 且 conf<0.85 / 完全无信号但**像在求助**
  不问：inherit（追问续写）/ 规则命中 / fused（两路互证）/ 高置信 / 纯寒暄 / **续答消息**（防循环）

用法：.venv/Scripts/python.exe -X utf8 tests/manual_verify/verify_intent_confirm.py
"""
import os
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.pipeline import AgentPipeline  # noqa: E402
from core import config as _cfg  # noqa: E402

OK, FAIL = [], []


def chk(name, cond, evidence=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  ← " + evidence) if evidence else ""))


pipe = AgentPipeline()
pipe._load_db_agents()

print("── 1. 判定面：该问的问，不该问的不问 ──")
CASES = [
    # (文本, meta, 期望是否问, 说明)
    ("帮我看看这个项目的预算", {"route": "llm_weak", "confidence": 0.5}, True, "LLM 猜了但置信低"),
    ("提供一段需求，进行需求分析、方案设计、代码校验",
     {"route": "fused_conflict", "confidence": 0.6}, True, "关键词与语义打架"),
    ("帮我看看这个系统大概是怎么设计的", {"route": "semantic_weak", "confidence": 0.69}, True, "弱语义命中"),
    ("随便说点啥", {"route": "llm", "confidence": 0.7}, True, "LLM 判了但 <0.85"),
    ("帮我看看这个系统", {"route": "chat", "confidence": 0.0}, True, "完全无信号，但像在求助"),
    ("你好呀", {"route": "chat", "confidence": 0.0}, False, "纯寒暄，不打扰"),
    ("帮我生成这个系统的SysML v2模型代码", {"route": "rule", "confidence": 0.95}, False, "规则高置信"),
    ("请设计星网宽带通信系统的架构方案", {"route": "fused", "confidence": 0.92}, False, "两路互证"),
    ("对这个系统做总体设计", {"route": "semantic", "confidence": 0.87}, False, "高置信语义"),
    ("再详细一点", {"route": "inherit", "confidence": 0.55}, False, "追问续写，问了反而打扰"),
    ("【澄清补充】用户已补充以下建模信息…回答「需求分析」",
     {"route": "llm_weak", "confidence": 0.5}, False, "续答消息，再问就成循环"),
]
for text, meta, want, why in CASES:
    got = pipe._should_confirm_intent(text, meta)
    chk(f"[{why}] route={meta['route']} conf={meta['confidence']} → {'问' if want else '不问'}",
        got is want, f"实得={'问' if got else '不问'}")

print("── 2. 开关：intent.confirm_when_unsure=False 时一律不问（可一键回退）──")
_cfg.DEFAULT_CONFIG.setdefault("intent", {})["confirm_when_unsure"] = False
_cfg.reload()
try:
    off = [pipe._should_confirm_intent(t, m) for t, m, w, _ in CASES if w]
    chk("关闭后全部为 False", not any(off))
finally:
    _cfg.DEFAULT_CONFIG["intent"]["confirm_when_unsure"] = True
    _cfg.reload()
chk("恢复后仍为 True", pipe._should_confirm_intent("帮我看看这个项目的预算",
                                                {"route": "llm_weak", "confidence": 0.5}))

print("── 3. 选项构造：选择题形态（选项 + 可补充输入 + 当前猜测如实标注）──")
qs = pipe._intent_confirm_questions("帮我看看这个项目的预算", "report_generation")
chk("恰 1 题", len(qs) == 1)
q = qs[0]
chk("allow_custom=True（有「其他/自定义」入口）", q["allow_custom"] is True)
chk("选项 2~4 个（与既有澄清卡上限一致）", 2 <= len(q["options"]) <= 4, f"实得={len(q['options'])}")
chk("当前猜测置顶且如实标注「（我的猜测）」",
    q["options"][0].endswith("（我的猜测）"), f"首项={q['options'][0]}")
chk("每个选项都带职责说明（保证选中后续答能被关键词层认出）",
    all("：" in o for o in q["options"]), f"options={q['options']}")
chk("选项文本包含该意图的特异词而非裸泛词",
    any(("方案设计" in o or "建模" in o) for o in q["options"] + [pipe._INTENT_LABEL.get("design", "")]),
    f"options={q['options']}")
qs2 = pipe._intent_confirm_questions("随便说点啥", "chat")
chk("当前意图不在候选里时不置顶、也不报错", len(qs2[0]["options"]) >= 3, f"options={qs2[0]['options']}")

print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：\n  " + "\n  ".join(FAIL))
    sys.exit(1)