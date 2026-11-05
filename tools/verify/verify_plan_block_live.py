"""`mode=block` 的**真实 `_planner_core`** 验证（合成逆序计划，LLM 打桩）。

## 为什么要有这条

此前 `mode=block` 的真机证据只有 warn 级违规（「缺必需阶段」不 block，是设计如此）。
error 级违规（阶段逆序）会不会真的中止，只靠单测覆盖了校验器本身，
**没验证过 planner 的接线**。本脚本用合成计划补上。

## 口径

LLM 打桩返回**逆序**计划（view_expansion 早于 architecture_skeleton ⇒ error 级违规）；
`load_pipeline_config` 打桩（mode=block / warn 两档对照）；跑真实 `_planner_core`；
专用 run_id 写库后清理。

判据：
  1. block：`data.blocked_by == 'plan_guard'`、违规里有 error 级、**零子任务落库**；
  2. 对照 warn：同一份计划照常执行（不阻断）。
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from database import get_db                       # noqa: E402
from workflows import FlowExecutor                # noqa: E402
from workflows import refine as refine_mod        # noqa: E402
import services.replan_loop as RL                 # noqa: E402
import workflows.planner as planner_mod           # noqa: E402

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


RUN_IDS = (990301, 990302)


def cleanup(conn):
    for rid in RUN_IDS:
        conn.execute("DELETE FROM agent_tasks WHERE run_id=?", (rid,))
        conn.execute("DELETE FROM replan_decisions WHERE run_id=?", (rid,))
    conn.commit()


def reversed_plan():
    """error 级逆序：`architecture_skeleton`（阶段序 2）**依赖** `view_expansion`（阶段序 3）。

    ⚠️ 判据是**依赖驱动**的（`stage.order_reversed` = X 依赖 Y 但 stage(X) 早于 stage(Y)），
    不是"任务出现顺序"。第一版夹具两个任务都无 deps ⇒ 逆序永不触发，校验为空 ——
    又一次"夹具没打到判据上"。deps 反着写才真正命中。
    """
    return json.dumps({"tasks": [
        {"key": "t1", "title": "架构骨架", "agent": "architecture_skeleton",
         "task_type": "agent", "deps": ["t2"], "seq": 1,
         "config": {"required": True}, "expected_output": "ok"},
        {"key": "t2", "title": "视图展开", "agent": "view_expansion",
         "task_type": "agent", "deps": [], "seq": 2,
         "config": {"required": True}, "expected_output": "ok"},
    ]}, ensure_ascii=False)


class StubLLM:
    def __init__(self, plan):
        self.plan = plan

    def chat(self, messages, provider_id=None, **kw):
        text = ""
        try:
            text = str((messages or [{}])[-1].get("content") or "")
        except Exception:
            pass
        content = self.plan if ("拆解" in text or "计划" in text or "子任务" in text) \
            else "## 汇总\n（桩）"
        return {"choices": [{"message": {"content": content}}],
                "provider": "stub", "model": "stub", "used_mock": True}


def run_once(conn, *, mode: str, run_id: int):
    """mode=block|warn 跑一次真实 `_planner_core`（逆序计划 + LLM/子任务打桩）。"""
    calls = []

    def fake_run_task(self, tk, goal, conn_, attachments=None, done_items=None,
                      artifact_digest=None, plan=None, branch="dev", user=None):
        calls.append(tk["task_key"])
        return "产出-%s" % tk["task_key"], {"latency_ms": 800}, True, ""

    old_rt = FlowExecutor._run_task
    old_refine = refine_mod.RefineGate.run
    old_gates = RL.gates
    old_lpc = planner_mod.load_pipeline_config
    import llm as llm_mod
    old_chat = llm_mod.llm_client.chat
    try:
        FlowExecutor._run_task = fake_run_task
        refine_mod.RefineGate.run = staticmethod(
            lambda content, goal, done, plan, provider_id=None: {"content": content, "passed": True})
        llm_mod.llm_client.chat = StubLLM(reversed_plan()).chat
        RL.gates = lambda cfg=None: {
            "enabled": False, "health_probe": False, "early_stop": False,
            "probe_every_tasks": 1, "probe_every_sec": 3600, "probe_time_budget_sec": 0,
            "health_probe_llm": False, "retry_once": True, "redraw_on_stall": False,
            "redraw_light": True, "heavy_redraw": False, "skip_leaf": False,
            "no_progress_stop": True, "max_redraw_rounds": 1, "redraw_expand_ratio": 1.5,
            "user_directive": False, "user_confirm_redraw": False, "confirm_ttl_sec": 60,
            "directive_max_touch": 3, "llm_redraw": False, "llm_actions": "replace",
            "source": "stub"}
        # 阶段序打桩（与真库 `agents.stage_order` 同一份语义，模式可切）
        planner_mod.load_pipeline_config = lambda conn_=None: {
            "stage_order": ["methodology_resolver", "requirement_structuring",
                            "architecture_skeleton", "view_expansion",
                            "model_validation_repair", "trace_verification",
                            "change_safety_gate", "model_release"],
            "required_stages": [], "mode": mode,
            "source": "stub"}
        pipe = FlowExecutor()
        out = pipe.run_planner_plan(goal="桩目标：逆序计划验证", agents=["view_expansion"],
                                    max_tasks=6, parallel=False, provider_id=None,
                                    run_id=run_id, conn=conn)
    finally:
        FlowExecutor._run_task = old_rt
        refine_mod.RefineGate.run = old_refine
        llm_mod.llm_client.chat = old_chat
        RL.gates = old_gates
        planner_mod.load_pipeline_config = old_lpc
    n_tasks = conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE run_id=?",
                           (run_id,)).fetchone()[0]
    return {"out": out, "calls": calls, "n_tasks": n_tasks}


conn = get_db()
cleanup(conn)

print("=" * 74)
print("[1] mode=block + 逆序计划 ⇒ 中止，**一个子任务都不落库**")
print("=" * 74)
b = run_once(conn, mode="block", run_id=RUN_IDS[0])
data = b["out"].get("data") or {}
print("  content 头 200 字：", str(b["out"].get("content") or "")[:200].replace("\n", " "))

check("1.1 `data.blocked_by == 'plan_guard'`（确实被校验器拦下）",
      data.get("blocked_by") == "plan_guard", data.get("blocked_by"))
_v = [v for v in ((data.get("plan_guard") or {}).get("violations") or [])
      if v.get("severity") == "error"]
check("1.2 违规清单含 error 级（阶段逆序）",
      any("逆序" in str(v.get("message")) for v in _v),
      [v.get("code") for v in _v])
check("1.3 **零子任务落库**（宁可不跑，也不静默跑错）", b["n_tasks"] == 0, b["n_tasks"])
check("1.4 零子任务执行", b["calls"] == [], b["calls"])
check("1.5 回复明确告知改回 warn 的办法",
      "pipeline_guard_mode" in str(b["out"].get("content") or ""))

print()
print("=" * 74)
print("[2] 对照 mode=warn：同一份计划照常执行（只告警不阻断）")
print("=" * 74)
w = run_once(conn, mode="warn", run_id=RUN_IDS[1])
data2 = w["out"].get("data") or {}
print("  执行 %s 落库 %d" % (w["calls"], w["n_tasks"]))
check("2.1 warn 档不拦截（blocked_by 不出现）",
      data2.get("blocked_by") is None, data2.get("blocked_by"))
check("2.2 warn 档两个子任务照常落库并执行",
      w["n_tasks"] == 2 and sorted(w["calls"]) == ["t1", "t2"],
      "%d / %s" % (w["n_tasks"], w["calls"]))

cleanup(conn)
conn.close()

print()
print("=" * 74)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  - %s" % f)
print("口径：LLM/_run_task/阶段序配置打桩；被验的是 `_planner_core` 的真实 block 接线；"
      "专用 run_id 写库后清理。")
sys.exit(0 if FAIL == 0 else 1)
