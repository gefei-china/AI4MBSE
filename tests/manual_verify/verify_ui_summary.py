# -*- coding: utf-8 -*-
"""子任务卡「交付摘要」清洗回归 —— 2026-09-26（用户报障：卡片下那行是内部交接语 + 硬截断）。

## 报障原文
用户截图里每张子任务卡下面都有一行，内容是：
  `# 一、对上游意图识别结果的承接` / `# 本任务承接 t2 的执行计划，职责是**产` / `# 最终答复：团队交付物汇总校验与结论`
问："这是必须的嘛 价值是什么？"并要求优化内容/布局/交互（占用太多区域）。

## 结论（本脚本固化）
"有一行交付摘要"要保留（编排下过程可审计）；但内容必须是**面向用户的一句话**：
  ① 剔除内部交接语（承接上游/本任务/职责是…）→ 全被剔掉时**返回空**（宁可没有这行）；
  ② 去 markdown 标记（`#`、`**`）；
  ③ **按句子收口**，不再 `slice(0,60)` 断在词中间。

## 重要约束（防回归）
`ui_summary` 只用于**展示**；`normalize_text` 产出的 `summary` 必须原样保留 ——
它作为"上游快照"喂给下游子任务（Task 9/10），清洗它会破坏子 Agent 的承接能力。

用法：.venv/Scripts/python.exe -X utf8 tests/manual_verify/verify_ui_summary.py
"""
import os
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from services.subtask_protocol import ui_summary, normalize_text  # noqa: E402

OK, FAIL = [], []


def chk(name, cond, evidence=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  ← " + evidence) if evidence else ""))


print("── 1. 报障原文：内部交接语必须被剔掉（剔干净就返回空，不硬凑）──")
NOISE = [
    ("截图第 1 行", "# 一、对上游意图识别结果的承接\n上游 t1 已完成意图识别，其核心结论是：**任务标题**为星网宽带通信系统。"),
    ("截图第 2 行", "# 本任务承接 t2 的执行计划，职责是**产出**结构视图与代码。"),
    ("纯交接语", "承接上游交付物，本子任务职责是补齐接口定义。"),
    ("编号+交接", "1. 本任务依赖 t1 的输出，作为上游快照的消费方。"),
]
for name, raw in NOISE:
    got = ui_summary(raw)
    chk(f"[{name}] → 空串", got == "", f"实得={got!r}")

print("── 2. 真实交付内容：压成一句话（去 markdown、按句收口、带省略号）──")
KEEP = [
    ("去粗体与井号", "## 交付说明\n已产出 **BDD 结构视图**与对应的 SysML v2 代码。\n另有 3 个功能包待确认。",
     "已产出 BDD 结构视图与对应的 SysML v2 代码。"),
    ("按句收口（不切词）", "已完成需求条目提取，共识别 42 条可验证需求。其余为背景描述，未纳入交付。",
     "已完成需求条目提取，共识别 42 条可验证需求。"),
    ("长单句按标点收口", "本阶段完成了星网宽带通信系统总体方案的设计与论证" + "，并对载荷、天线、转发器、链路预算等关键指标进行了逐项校核" * 3 + "。",
     None),       # 只断言"以省略号结尾且不超 61 字"
]
for name, raw, want in KEEP:
    got = ui_summary(raw)
    if want is None:
        chk(f"[{name}] 截断得体（≤61 字 + 省略号）", got.endswith("…") and len(got) <= 61,
            f"实得={got!r} len={len(got)}")
    else:
        chk(f"[{name}]", got == want, f"实得={got!r}")

print("── 3. 边界：空/None/纯标题 不得抛异常，且返回空串 ──")
for raw in (None, "", "   ", "# 标题", "。。。"):
    try:
        got = ui_summary(raw)
        chk(f"[{raw!r}] → 空串且不抛异常", got == "", f"实得={got!r}")
    except Exception as e:
        chk(f"[{raw!r}] → 空串且不抛异常", False, f"抛异常 {e}")

print("── 4. 防回归：协议 summary 原样保留（下游承接靠它，不能被清洗）──")
proto = normalize_text("# 一、对上游意图识别结果的承接\n上游 t1 已完成意图识别。")
chk("normalize_text 的 summary 仍含内部交接语（未被动过）",
    "承接" in proto["summary"], f"summary={proto['summary'][:40]!r}")
chk("但 ui_summary 已把同一段文本清干净", ui_summary(proto["summary"]) == "",
    f"ui_summary={ui_summary(proto['summary'])!r}")

print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：\n  " + "\n  ".join(FAIL))
    sys.exit(1)