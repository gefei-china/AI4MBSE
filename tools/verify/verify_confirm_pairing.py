# -*- coding: utf-8 -*-
"""写前确认（HITL）消息配对 + 子任务错误归因 门禁。

## 背景（conv 990403 完整一轮真机实证，2026-11-04）

完整编排跑通后发现必需任务 t2（架构骨架）两次尝试都零产出失败：
`todo_write + save_artifact` 同轮并发，`save_artifact` 命中**写前确认闸**
（confirm_required）→ 内层 for break，但**轮级没有 break** →
`if results: messages.append(pmsg)` 仍执行（pmsg 带确认工具的 tool_call、
无配对 tool_result）→ 下一轮探测 **HTTP 400**，且 messages 已永久带毒 ⇒
后续每次调用都 400 ⇒ 子任务零产出。错误又被顶层 except 转成的
`{"type":"error"}` 事件在 `_run_subtask` 里**无人认领** ⇒ DB 只剩
兜底文案「子任务未产生输出」，真因完全不可见。

## 三个修复的回归判据

1. confirm_required 轮级 break（在 `if results:` 回填**之前**，与 clarify 同构）；
2. 失败决策 source 按任务键拼接（`"%s_fail" % tkey`，不再硬编码 t1_fail）；
3. `_run_subtask` 捕获 error 事件并把真因带进返回值第 6 位。

退出码 0 = 全绿。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")

PASS = 0
FAIL = 0
FAILED = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, detail))


with open(SRC, encoding="utf-8") as f:
    st = f.read()

CLARIFY_BREAK = "if _clarify_req:\n                            break"
CONFIRM_BREAK = "if _hil_confirm_req:\n                            break"
BACKFILL = "messages.append(pmsg)\n                            messages.extend(results)"

print("=" * 74)
print("[1] confirm_required 轮级 break（防悬空 tool_call ⇒ HTTP 400）")
print("=" * 74)
i_clarify = st.find(CLARIFY_BREAK)
i_confirm = st.find(CONFIRM_BREAK)
i_backfill = st.find(BACKFILL)
check("1.1 clarify 轮级 break 存在（同构参照物）", i_clarify > 0)
check("1.2 confirm 轮级 break 存在（本轮修复）", i_confirm > 0)
check("1.3 results 回填点存在", i_backfill > 0)
check("1.4 ★ 顺序正确：clarify break < confirm break < results 回填",
      0 < i_clarify < i_confirm < i_backfill,
      "clarify=%d confirm=%d backfill=%d" % (i_clarify, i_confirm, i_backfill))
check("1.5 confirm break 落在轮循环内（缩进 24 空格，与 clarify 同层，"
      "而非轮循环外 16 空格的收口分支）",
      i_confirm > 0 and st[i_confirm - 1] != "\n" * 0
      and "\n                        " + CONFIRM_BREAK.split("\n")[0] in st)

print()
print("=" * 74)
print("[2] 失败决策 source 按任务键拼接")
print("=" * 74)
check("2.1 `_write_decision(\"%s_fail\" % tkey, ...)` 存在",
      '_write_decision("%s_fail" % tkey' in st)
check("2.2 硬编码 \"t1_fail\" 调用点已消失",
      '_write_decision("t1_fail"' not in st)

print()
print("=" * 74)
print("[3] 子任务错误归因（error 事件接住 + 真因带回）")
print("=" * 74)
check("3.1 `_run_subtask` 事件循环有 error 分支", '_et == "error"' in st)
check("3.2 error 消息被记入 `_sub_err`",
      '_sub_err = str(ev.get("message")' in st)
check("3.3 `_sub_err` 有初始化（空串，非未定义）", "_sub_err = \"\"" in st)
check("3.4 ★ 成功路径 return 第 6 位带出 `_sub_err`（原为硬编码 \"\"）",
      "evs, _sub_err, int((_t.time() - _t0) * 1000), tok_used" in st)
# 3.5：error 分支必须在 done 分支之后（elif 链尾部），且不能吞掉既有分支
i_done = st.find('elif _et == "done":')
i_err = st.find('elif _et == "error":')
check("3.5 error 分支挂在 done 之后（elif 链完整）", 0 < i_done < i_err)

print()
print("=" * 74)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f_ in FAILED:
        print("  - %s" % f_)
print("口径：源码顺序断言（本仓 verify_plan_guard 1.6 同款手法）；"
      "行为级证明见 conv 990403 完整一轮真机报告。")
sys.exit(0 if FAIL == 0 else 1)
