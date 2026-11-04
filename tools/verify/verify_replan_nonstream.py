"""非流式编排路径的重规划端到端自检（`_planner_core`）。

## 为什么必须有这一条

上一轮只验了流式路径（`stream.py`）的巡检/早停。非流式（Flow 节点 / 试跑 / 直调
`run_planner_plan`）**此前一行 replan 都没有**，本轮补上后必须证明它真的会
「观察 → 重新评估 → 调整计划」，而不只是"代码里有几行 if"。

## 口径

· LLM 与子任务执行**打桩**（确定性）；被验的是 `_planner_core` 的**真实控制流**
  ——建计划、落 agent_tasks、边界决策、取消/阻塞任务、汇总。
· 闸门通过替换 `services.replan_loop.gates` 注入（不改配置文件）。
· 用**专用 run_id** 写生产库的 `agent_tasks` / `replan_decisions`，跑完即清理
  （开头也清一次，防上次中断留垃圾）。

判据（8 条）：
  1. 早停开 ⇒ 只需任务全完成后，非必需任务被 **cancel**（不是 done）
  2. 决策进 `data["replan"]`（非流式没有 SSE，必须靠返回值可见）
  3. 决策进 `replan_decisions`（与流式同表，可聚合）
  4. `pending_count` 归零（本轮修的 cancelled 拼写 bug 的直接回归）
  5. 早停关 ⇒ 同一计划三个任务**全部 done**（对照，证明差异来自闸门）
  6. 早停关 ⇒ `data["replan"]` 为空（不臆造决策）
  7. 计划含 failed 时**不早停**（保守语义在真实链路上成立）
  8. 无 required 标注时**不早停**（不猜目标边界）
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from database import get_db                      # noqa: E402
from task_queue import TaskQueue                 # noqa: E402
from workflows import FlowExecutor               # noqa: E402
from workflows import refine as refine_mod       # noqa: E402
import services.replan_loop as RL                # noqa: E402

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


RUN_IDS = (990101, 990102, 990103, 990104)


def cleanup(conn):
    for rid in RUN_IDS:
        conn.execute("DELETE FROM agent_tasks WHERE run_id=?", (rid,))
        conn.execute("DELETE FROM replan_decisions WHERE run_id=?", (rid,))
    conn.commit()


def plan_json(req_flags):
    """三个任务：t1/t2 必需（t3 可选，依赖 t1）。"""
    tasks = []
    for i, (key, agent, deps, required) in enumerate([
            ("t1", "requirement_analysis", [], req_flags[0]),
            ("t2", "design", ["t1"], req_flags[1]),
            ("t3", "review", ["t1"], req_flags[2])], start=1):
        tasks.append({"key": key, "title": "任务%s" % key, "agent": agent,
                      "task_type": "agent", "deps": deps, "seq": i,
                      "config": {"required": required},
                      "expected_output": "ok"})
    return json.dumps({"tasks": tasks}, ensure_ascii=False)


class StubLLM:
    """规划提示词 → 返回给定计划；其余（总结/反思）→ 返回纯文本。"""

    def __init__(self, plan):
        self.plan = plan

    def chat(self, messages, provider_id=None, **kw):
        text = ""
        try:
            text = str((messages or [{}])[-1].get("content") or "")
        except Exception:
            pass
        if "子任务" in text and "JSON" in text:
            content = self.plan
        elif "拆解" in text or "计划" in text:
            content = self.plan
        else:
            content = "## 汇总\n（桩）：子任务已完成。"
        return {"choices": [{"message": {"content": content}}],
                "provider": "stub", "model": "stub", "used_mock": True}


def run_once(conn, *, early_stop, probe, required_flags, run_id, fail_keys=()):
    """跑一次真实的 `_planner_core`（LLM/子任务打桩）。返回结果 dict。"""
    calls = []

    def fake_run_task(self, tk, goal, conn_, attachments=None, done_items=None,
                      artifact_digest=None, plan=None, branch="dev", user=None):
        calls.append(tk["task_key"])
        if tk["task_key"] in fail_keys:
            return "", {}, False, "桩：故意失败"
        return "产出-%s" % tk["task_key"], {"latency_ms": 1200}, True, ""

    old_rt = FlowExecutor._run_task
    old_refine = refine_mod.RefineGate.run
    import llm as llm_mod
    old_chat = llm_mod.llm_client.chat
    old_gates = RL.gates
    try:
        FlowExecutor._run_task = fake_run_task
        refine_mod.RefineGate.run = staticmethod(
            lambda content, goal, done, plan, provider_id=None: {"content": content, "passed": True})
        llm_mod.llm_client.chat = StubLLM(plan_json(required_flags)).chat
        # 注入闸门（只替换读取器，决策逻辑仍是真实的 replan_health）
        # ⚠️ 必须是**普通函数**：模块属性赋 `staticmethod(...)` 后不能直接调用
        #    （`ReplanLoop.__init__` 里是 `gates()` 而非 `gates.__get__()`），
        #    静默失效会表现成"闸门没生效"——本次就踩了一次。
        RL.gates = lambda cfg=None: {
            "enabled": True, "health_probe": probe, "early_stop": early_stop,
            "probe_every_tasks": 1, "probe_every_sec": 3600,
            "probe_time_budget_sec": 0, "health_probe_llm": False,
            "retry_once": True, "redraw_on_stall": False, "redraw_light": True,
            "heavy_redraw": False, "skip_leaf": False, "no_progress_stop": True,
            "max_redraw_rounds": 1, "redraw_expand_ratio": 1.5,
            "user_directive": False, "user_confirm_redraw": False,
            "confirm_ttl_sec": 60, "directive_max_touch": 3, "llm_redraw": False,
            "llm_actions": "replace", "source": "stub",
        }
        pipe = FlowExecutor()
        out = pipe.run_planner_plan(goal="桩目标：完成三个步骤", agents=["requirement_analysis"],
                                    max_tasks=6, parallel=False, provider_id=None,
                                    run_id=run_id, conn=conn)
    finally:
        FlowExecutor._run_task = old_rt
        refine_mod.RefineGate.run = old_refine
        llm_mod.llm_client.chat = old_chat
        RL.gates = old_gates
    return out, calls


def statuses(conn, run_id):
    return {r["task_key"]: r["status"] for r in
            conn.execute("SELECT task_key, status FROM agent_tasks WHERE run_id=?",
                         (run_id,)).fetchall()}


conn = get_db()
cleanup(conn)

print("=" * 74)
print("[1] 早停开：必需全完成 ⇒ 非必需任务被取消（真实控制流）")
print("=" * 74)

out, calls = run_once(conn, early_stop=True, probe=False,
                      required_flags=[True, True, False], run_id=RUN_IDS[0])
st = statuses(conn, RUN_IDS[0])
print("  agent_tasks 状态：%s" % st)
print("  子任务实际执行顺序：%s" % calls)

check("1.1 三个任务中 t1/t2 完成、t3 被取消",
      st.get("t1") == "done" and st.get("t2") == "done" and st.get("t3") == "cancelled", st)
check("1.2 被取消的 t3 **没有被执行**（取消发生在派发前）",
      "t3" not in calls, calls)
check("1.3 `data['replan']` 含 early_stop 决策（非流式靠返回值可见）",
      any(d.get("action") == "early_stop" for d in (out.get("data") or {}).get("replan") or []),
      (out.get("data") or {}).get("replan"))
check("1.4 决策写明放弃了哪个任务（skipped=['t3']）",
      any(d.get("skipped") == ["t3"] for d in (out.get("data") or {}).get("replan") or []))
check("1.5 `replan_decisions` 有 early_stop 记录（与流式同表）",
      conn.execute("SELECT COUNT(*) FROM replan_decisions WHERE run_id=? AND action='early_stop'",
                   (RUN_IDS[0],)).fetchone()[0] >= 1)
check("1.6 `pending_count` 归零（cancelled 拼写 bug 的真实链路回归）",
      TaskQueue.pending_count(conn, RUN_IDS[0]) == 0,
      TaskQueue.pending_count(conn, RUN_IDS[0]))
check("1.7 汇总口径：完成 2、失败/阻塞 0（取消不算失败）",
      (out.get("task_summary") or {}).get("counts", {}).get("done") == 2
      and (out.get("task_summary") or {}).get("counts", {}).get("failed", 0) == 0,
      (out.get("task_summary") or {}).get("counts"))

print()
print("=" * 74)
print("[2] 对照：早停关 ⇒ 同一计划三个任务全部执行")
print("=" * 74)

out2, calls2 = run_once(conn, early_stop=False, probe=False,
                        required_flags=[True, True, False], run_id=RUN_IDS[1])
st2 = statuses(conn, RUN_IDS[1])
print("  agent_tasks 状态：%s" % st2)

check("2.1 三个任务全部 done（差异只来自闸门）",
      all(st2.get(k) == "done" for k in ("t1", "t2", "t3")), st2)
check("2.2 三个任务全部被派发执行", len(calls2) == 3, calls2)
check("2.3 `data['replan']` 为空（关闸不臆造决策）",
      not (out2.get("data") or {}).get("replan"), (out2.get("data") or {}).get("replan"))

print()
print("=" * 74)
print("[3] 保守语义在真实链路上成立：有 failed / 无 required ⇒ 不早停")
print("=" * 74)

out3, calls3 = run_once(conn, early_stop=True, probe=False,
                        required_flags=[True, True, False], run_id=RUN_IDS[2],
                        fail_keys=("t2",))
st3 = statuses(conn, RUN_IDS[2])
# ⚠️ 夹具必须让**必需任务**失败：让非必需任务失败是测不出来的 ——
#    开启早停后非必需任务会被推迟，根本不派发，也就不会失败（这一点本身是正确行为）。
check("3.1 必需任务失败时不做早停（失败走 T4/blocked，不提前收敛）",
      not any(d.get("action") == "early_stop"
              for d in (out3.get("data") or {}).get("replan") or []),
      (out3.get("data") or {}).get("replan"))
check("3.2 失败任务保持 failed（没被取消掩盖）", st3.get("t2") == "failed", st3)

out4, calls4 = run_once(conn, early_stop=True, probe=False,
                        required_flags=[False, False, False], run_id=RUN_IDS[3])
st4 = statuses(conn, RUN_IDS[3])
check("3.3 无 required 标注 ⇒ 不早停（不猜目标边界），三任务照跑",
      len(calls4) == 3 and all(st4.get(k) == "done" for k in ("t1", "t2", "t3")), st4)

cleanup(conn)
conn.close()

print()
print("=" * 74)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  - %s" % f)
print("口径：LLM 与子任务执行打桩；被验的是 `_planner_core` 真实控制流；"
      "专用 run_id 写库后清理。")
sys.exit(0 if FAIL == 0 else 1)
