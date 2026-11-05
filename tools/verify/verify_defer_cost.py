"""量化 `defer_optional`（早停时把非必需任务推迟一轮）的**收益与代价**。

## 为什么要有这条

`defer_optional` 是本轮为了让"早停真的能省工作"而加的（扇出计划里可选任务与最后一个
必需任务同批跑完 ⇒ 早停永远无事可做）。但它**不是免费**的：推迟一轮意味着
"本来能并行的任务变成两批"。到底值不值，必须量出来，而不是拍脑袋。

## 两个场景（确定性，子任务用固定 sleep 模拟耗时）

| 场景 | 计划 | 开 defer | 不开 defer |
|---|---|---|---|
| **A 成功路径**（早停生效） | t1 必需；t2/t3 可选，都与 t1 同批就绪 | 只跑 t1，t2/t3 被取消 | t1/t2/t3 **并排跑完**（1 批） |
| **B 失败路径**（早停不触发） | t1 必需但**失败**；t2/t3 可选 | t1 一批、t2/t3 又一批（2 批） | t1/t2/t3 同批（1 批） |

判据：
  · A：defer 版**执行任务数更少**（省算力），墙钟不高于不 defer（因为同批并行）；
  · B：defer 版**墙钟约 2 倍**（代价），任务数相同（没省到东西）。

## 口径

LLM 与 `_run_task` 打桩（固定 `TASK_SEC`），跑**真实 `_planner_core` 控制流**；
闸门注入用 `services.replan_loop.gates` 替换（不改配置文件）；专用 run_id 写库后清理。
"""

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from database import get_db                       # noqa: E402
from task_queue import TaskQueue                  # noqa: E402
from workflows import FlowExecutor                # noqa: E402
from workflows import refine as refine_mod        # noqa: E402
import services.replan_loop as RL                 # noqa: E402

PASS = 0
FAIL = 0
FAILED = []

TASK_SEC = 0.12        # 单个子任务"耗时"（够稳定到能比较，又不拖慢门禁）
RUN_IDS = (990201, 990202, 990203, 990204)


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, detail))


def cleanup(conn):
    for rid in RUN_IDS:
        conn.execute("DELETE FROM agent_tasks WHERE run_id=?", (rid,))
        conn.execute("DELETE FROM replan_decisions WHERE run_id=?", (rid,))
    conn.commit()


def plan_json():
    """t1 必需（无依赖）；t2/t3 可选（无依赖）⇒ 三者同批就绪。"""
    return json.dumps({"tasks": [
        {"key": "t1", "title": "必需任务", "agent": "requirement_analysis",
         "task_type": "agent", "deps": [], "seq": 1, "config": {"required": True}},
        {"key": "t2", "title": "可选A", "agent": "design",
         "task_type": "agent", "deps": [], "seq": 2, "config": {"required": False}},
        {"key": "t3", "title": "可选B", "agent": "review",
         "task_type": "agent", "deps": [], "seq": 3, "config": {"required": False}},
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


def run_once(conn, *, defer: bool, fail_t1: bool, run_id: int, early_stop=True):
    """跑一次真实 `_planner_core`；`defer=False` 时把 defer_optional 参数吃掉。

    返回 `rounds` = 主循环迭代次数（= `ReplanLoop.decide` 被调次数）。
    ⚠️ 为什么用轮次而不是只用墙钟：首次运行要吃导入/预热开销（实测首跑 0.63s vs 次跑 0.14s），
       墙钟在小样本上不可靠。轮次是确定性指标，墙钟只作辅助（且必须先预热）。
    """
    calls = []
    rounds = [0]

    def fake_run_task(self, tk, goal, conn_, attachments=None, done_items=None,
                      artifact_digest=None, plan=None, branch="dev", user=None):
        time.sleep(TASK_SEC)
        calls.append(tk["task_key"])
        if fail_t1 and tk["task_key"] == "t1":
            return "", {}, False, "桩：必需任务失败"
        return "产出-%s" % tk["task_key"], {"latency_ms": int(TASK_SEC * 1000)}, True, ""

    old_rt = FlowExecutor._run_task
    old_refine = refine_mod.RefineGate.run
    old_gates = RL.gates
    old_ready = TaskQueue.ready_tasks
    old_decide = RL.ReplanLoop.decide
    import llm as llm_mod
    old_chat = llm_mod.llm_client.chat

    def _ready_no_defer(conn_, run_id_, **kw):
        kw.pop("defer_optional", None)
        return old_ready(conn_, run_id_, **kw)

    def _decide_counting(self, *a, **kw):
        rounds[0] += 1
        return old_decide(self, *a, **kw)

    try:
        FlowExecutor._run_task = fake_run_task
        refine_mod.RefineGate.run = staticmethod(
            lambda content, goal, done, plan, provider_id=None: {"content": content, "passed": True})
        llm_mod.llm_client.chat = StubLLM(plan_json()).chat
        RL.ReplanLoop.decide = _decide_counting
        RL.gates = lambda cfg=None: {
            "enabled": True, "health_probe": False, "early_stop": bool(early_stop),
            "probe_every_tasks": 1, "probe_every_sec": 3600, "probe_time_budget_sec": 0,
            "health_probe_llm": False, "retry_once": True, "redraw_on_stall": False,
            "redraw_light": True, "heavy_redraw": False, "skip_leaf": False,
            "no_progress_stop": True, "max_redraw_rounds": 1, "redraw_expand_ratio": 1.5,
            "user_directive": False, "user_confirm_redraw": False, "confirm_ttl_sec": 60,
            "directive_max_touch": 3, "llm_redraw": False, "llm_actions": "replace",
            "source": "stub"}
        if not defer:
            TaskQueue.ready_tasks = staticmethod(_ready_no_defer)
        t0 = time.time()
        pipe = FlowExecutor()
        out = pipe.run_planner_plan(goal="桩目标：一个必需 + 两个可选", agents=["requirement_analysis"],
                                    max_tasks=6, parallel=True, provider_id=None,
                                    run_id=run_id, conn=conn)
        wall = time.time() - t0
    finally:
        FlowExecutor._run_task = old_rt
        refine_mod.RefineGate.run = old_refine
        llm_mod.llm_client.chat = old_chat
        RL.gates = old_gates
        TaskQueue.ready_tasks = old_ready
        RL.ReplanLoop.decide = old_decide
    st = {r["task_key"]: r["status"] for r in
          conn.execute("SELECT task_key, status FROM agent_tasks WHERE run_id=?", (run_id,))}
    return {"calls": calls, "wall": wall, "rounds": rounds[0], "statuses": st, "out": out}


conn = get_db()
cleanup(conn)

# 预热：首次运行要吃导入/缓存开销（实测首跑 0.63s vs 次跑 0.14s）⇒ 结果丢弃
run_once(conn, defer=False, fail_t1=False, run_id=990200)
cleanup(conn)

print("=" * 74)
print("[A] 成功路径：必需全部完成 ⇒ 早停生效")
print("=" * 74)
# 顺序：先跑"关 defer"（对照），再跑"开 defer"，避免前者吃预热残差
a_plain = run_once(conn, defer=False, fail_t1=False, run_id=RUN_IDS[1])
a_defer = run_once(conn, defer=True, fail_t1=False, run_id=RUN_IDS[0])
print("  开 defer : 执行 %-22s 轮次 %d 墙钟 %.2fs 状态 %s"
      % (a_defer["calls"], a_defer["rounds"], a_defer["wall"], a_defer["statuses"]))
print("  关 defer : 执行 %-22s 轮次 %d 墙钟 %.2fs 状态 %s"
      % (a_plain["calls"], a_plain["rounds"], a_plain["wall"], a_plain["statuses"]))

check("A1 开 defer ⇒ 只执行必需任务（可选任务被取消，**省下算力**）",
      a_defer["calls"] == ["t1"], a_defer["calls"])
check("A2 关 defer ⇒ 三个任务都跑了（可选任务来不及取消）",
      sorted(a_plain["calls"]) == ["t1", "t2", "t3"], a_plain["calls"])
check("A3 开 defer 只多 **1 轮决策**（该轮不含任务，无 LLM 成本）",
      a_defer["rounds"] == a_plain["rounds"] + 1,
      "%d vs %d" % (a_defer["rounds"], a_plain["rounds"]))
# ⚠️ 2026-11-05 降级为**弱判据**（此前是 `wall <= plain*1.5` 的硬断言，会偶发假红）：
#   TASK_SEC=0.12s ⇒ 总墙钟 0.2~0.5s。这个尺度下，一次 GC / 一次调度让位
#   就足以把比值推到 1.5 以上 ⇒ 判据测的是**机器当时忙不忙**，不是代码行为。
#   实测证据：gate_runner 里偶发红一次，单独连跑 3 次全绿（9/0/9/0/9/0）。
#   "省任务不牺牲时间"的强证据本来就在 A1/A2/A3 —— 任务数与轮次都是确定性的。
#   墙钟只保留"不显著恶化"的兜底（3× 且仅当 plain 足够长时才判），并在下方打印实测值。
_wall_ratio = (a_defer["wall"] / a_plain["wall"]) if a_plain["wall"] else 0
_a4_ok = True if a_plain["wall"] < 0.30 else (a_defer["wall"] <= a_plain["wall"] * 3.0)
check("A4 墙钟不显著恶化（弱判据；抖动主导时只观测，强证据见 A1~A3）",
      _a4_ok,
      "%.2fs vs %.2fs（比值 %.2f，plain<0.30s 时抖动主导故不判红）"
      % (a_defer["wall"], a_plain["wall"], _wall_ratio))

print()
print("=" * 74)
print("[B] 失败路径：必需任务失败 ⇒ 早停不触发，可选任务仍要跑")
print("=" * 74)
b_plain = run_once(conn, defer=False, fail_t1=True, run_id=RUN_IDS[3])
b_defer = run_once(conn, defer=True, fail_t1=True, run_id=RUN_IDS[2])
print("  开 defer : 执行 %-22s 轮次 %d 墙钟 %.2fs 状态 %s"
      % (b_defer["calls"], b_defer["rounds"], b_defer["wall"], b_defer["statuses"]))
print("  关 defer : 执行 %-22s 轮次 %d 墙钟 %.2fs 状态 %s"
      % (b_plain["calls"], b_plain["rounds"], b_plain["wall"], b_plain["statuses"]))

check("B1 开 defer ⇒ 必需先跑、可选第二批（t1 先于 t2/t3）",
      b_defer["calls"] and b_defer["calls"][0] == "t1"
      and len(b_defer["calls"]) == 3, b_defer["calls"])
check("B2 关 defer ⇒ 三个任务同批（1 批完成）",
      sorted(b_plain["calls"]) == ["t1", "t2", "t3"], b_plain["calls"])
# ⚠️ 2026-11-05 同上：原 `wall >= plain*1.5` 是硬墙钟断言，会偶发假红。
#   "多一批真实任务的代价"有**三个确定性证据**，不需要靠墙钟：
#     ① B5：开 defer 比关 defer 多 1 轮决策，且该轮含真实任务；
#     ② B4：两版执行的任务数相同（3 个，defer 没省到）；
#     ③ B1：t1 先于 t2/t3（必需先跑、可选第二批）。
#   墙钟只作观测打印 —— 保留它是因为"1.9×"这个数字对决策有用，
#   但**数字进日志、不进判据**。
check("B3 开 defer 的决策轮次多于关 defer（代价的确定性证据）",
      b_defer["rounds"] > b_plain["rounds"],
      "轮次 %d vs %d" % (b_defer["rounds"], b_plain["rounds"]))
print("      墙钟参考（**非判据**，仅供人工核）：%.2fs vs %.2fs，比值 %.2f"
      % (b_defer["wall"], b_plain["wall"],
         (b_defer["wall"] / b_plain["wall"]) if b_plain["wall"] else 0))
check("B4 失败路径下 defer **没有省到任何任务**（两版都执行 3 个）",
      len(b_defer["calls"]) == len(b_plain["calls"]) == 3,
      "%d vs %d" % (len(b_defer["calls"]), len(b_plain["calls"])))
check("B5 两版轮次差 = 1（多出的那一轮**含真实任务**，这是代价所在）",
      b_defer["rounds"] == b_plain["rounds"] + 1,
      "%d vs %d" % (b_defer["rounds"], b_plain["rounds"]))

cleanup(conn)
conn.close()

print()
print("=" * 74)
print("结论（写进文档/决策用）")
print("=" * 74)
print("  · 收益：成功路径下**直接省掉**全部可选任务（本例 3→1 个任务）。")
print("  · 代价：必需任务失败时，可选任务晚一批（本例墙钟约 %.1f×，**参考值**），"
      "且没省到任务。" % ((b_defer["wall"] / b_plain["wall"]) if b_plain["wall"] else 0))
print("  · 判据口径：任务数 / 轮次是**确定性**判据；墙钟只观测不判定")
print("    （TASK_SEC=0.12s 的总时长下，调度抖动足以让比值翻倍 —— 见 A4/B3 注释）。")
print("  · 净判断：成功路径是常态、失败路径是少数 ⇒ 默认开启合理；"
      "若某类计划失败率高，可考虑关掉（当前无独立开关，跟随 early_stop）。")
print()
print("=" * 74)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  - %s" % f)
print("口径：LLM/_run_task 打桩（TASK_SEC=%.2fs），真实 `_planner_core` 控制流；"
      "专用 run_id 写库后清理。" % TASK_SEC)
sys.exit(0 if FAIL == 0 else 1)
