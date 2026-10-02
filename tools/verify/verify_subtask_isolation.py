# -*- coding: utf-8 -*-
"""子任务隔离契约自检（P1-20，对标 Claude Agent SDK「subagent fresh context + 只回传摘要」）。

背景：评估「子任务是否彻底隔离」时，逐条读码确认现状已达标——子任务走
`execute_stream(query, 0, ...)`（conversation_id=0 → _load_history 空 = fresh context），
上游结果经 `build_subtask_context` 只注入「任务定义 + 上游交付物摘要(≤400字) + 交付规范」，
汇总经 `_summarize_plan` 用 `_head_tail_clip` 头尾采样截断（不塞完整交付物）。

本脚本把这条「隔离契约」固化为可回归的断言（纯函数，不碰真实 LLM）：
  1) build_subtask_context 注入的是最小充分上下文，上游交付物被截到 ≤400 字；
  2) _head_tail_clip 头 60% + 尾 40%（保结论，非只留头）；
  3) _summarize_item_budget 预算随子任务数均分、夹在 floor 与 item 上限之间；
  4) 变异自证：去掉截断 / 去掉预算夹取，都会被抓住。
"""
import inspect
import json
import os
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from workflows.planner import build_subtask_context, FlowPlannerMixin  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── F1/F4：build_subtask_context 最小充分上下文 ─────────────────────────
_tk = {"task_key": "t3", "title": "结构视图生成",
       "expected_output": "输出 BDD 结构视图", "deps": ["t1", "t2"]}
_long_result = "A" * 800 + "（结论：三个方案选 B）"   # 上游交付物超长
_done = [
    {"task_key": "t1", "title": "需求分析", "result": _long_result},
    {"task_key": "t2", "title": "架构设计", "result": "B" * 500},
]
_ctx = build_subtask_context(_tk, "无人机动力系统建模", _done, artifact_digest="【产物】已有 SysML 模型")

check("F1 上游交付物摘要被截到 ≤400 字（最小上下文，非全量）",
      "A" * 400 in _ctx and "A" * 401 not in _ctx,
      "len=%d" % len(_ctx))
check("F2 只注入任务定义/期望产出/交付规范，不注入父会话历史消息",
      all(k in _ctx for k in ("任务定义", "期望产出", "交付规范", "上游交付物摘要")),
      "")
check("F3 artifact_digest（会话产物摘要）被显式注入（子任务看不到父历史，靠它知道会话已有）",
      "【产物】已有 SysML 模型" in _ctx, "")

# ── F5：_head_tail_clip 头尾采样 ────────────────────────────────────────
_ht = FlowPlannerMixin._head_tail_clip
_txt = "开头重要信息" + "中" * 200 + "结尾结论"
_capped = _ht(_txt, cap=50)
check("F4 _head_tail_clip 超限时保头尾（头 60% 尾 40%，保结论）",
      _capped.startswith("开头重要信息") and _capped.endswith("结尾结论") and "中间省略" in _capped,
      "len=%d" % len(_capped))
check("F5 _head_tail_clip 未超限原样返回", _ht("短文本", cap=50) == "短文本", "")

# ── F6：_summarize_item_budget 预算 ─────────────────────────────────────
_bud = FlowPlannerMixin._summarize_item_budget
check("F6 预算随子任务数均分并夹在 [floor, item_max]",
      _bud(1) == 1600 and _bud(10) == 1200 and _bud(100) == 600,
      "n=1→%d n=10→%d n=100→%d" % (_bud(1), _bud(10), _bud(100)))

# ── M 变异自证 ─────────────────────────────────────────────────────────
def _twin(fn_name, mutate):
    ns = {}
    for f in ("_head_tail_clip", "_summarize_item_budget"):
        src = textwrap.dedent(inspect.getsource(getattr(FlowPlannerMixin, f)))
        if f == fn_name:
            orig = src
            src = mutate(src)
            check("M 变异锚点命中(%s)" % fn_name, src != orig, "")
        exec(compile(src, "<twin>", "exec"), ns)
    return ns


# M1：_head_tail_clip 只留头（去掉尾采样）→ 结尾结论丢失，被抓住
_ns1 = _twin("_head_tail_clip", lambda s: s.replace("return t[:head] + mark + t[-tail:]",
                                                     "return t[:cap]"))
_r1 = _ns1["_head_tail_clip"](_txt, cap=50)
check("M1 _head_tail_clip 变异（只留头）→ 结尾结论丢失被抓住", not _r1.endswith("结尾结论"), "")

# M2：_summarize_item_budget 去掉夹取（return item_max 恒 1600）→ n=100 不再压到 600，被抓住
_ns2 = _twin("_summarize_item_budget", lambda s: s.replace(
    "return max(floor, min(item_max, total // n))", "return item_max"))
_r2 = _ns2["_summarize_item_budget"](100)
check("M2 _summarize_item_budget 变异（去掉夹取）→ n=100 仍 1600 被抓住", _r2 == 1600, "got=%d" % _r2)

print()
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
