# -*- coding: utf-8 -*-
"""UX 埋点仓储行为验证（§11 六指标聚合口径）。运行：python tools/verify/verify_ux_metrics.py"""
import json
import sqlite3
import sys

sys.path.insert(0, ".")

from repositories.ux_metrics_repo import UxMetricsRepo  # noqa: E402

conn = sqlite3.connect(":memory:")
repo = UxMetricsRepo(conn)

pass_n, fail_l = 0, []
def check(name, cond, detail=""):
    global pass_n
    if cond: pass_n += 1; print("  PASS " + name)
    else: fail_l.append(name); print("  FAIL " + name + (" —— " + str(detail) if detail else ""))

# ── 1. 白名单与上限 ──
print("[W] 写入口径")
n = repo.insert_batch([
    {"event": "task_done"}, {"event": "task_done"},
    {"event": "task_error", "detail": json.dumps({"message": "boom"})},
    {"event": "interrupt"},
    {"event": "evil_event"},                      # 白名单外 → 丢弃
    {"event": None}, {"event": 123}, {}, "notdict",  # 非法形态 → 丢弃
])
check("W1 白名单外/非法形态静默丢弃（4/9 入库）", n == 4, n)
n2 = UxMetricsRepo(sqlite3.connect(":memory:")).insert_batch([{"event": "task_done"}] * 60)
check("W2 单批上限 50（独立库，不污染后续聚合）", n2 == 50, n2)

# ── 2. 聚合口径 ──
print("[S] 聚合口径")
repo.insert_batch([
    {"event": "task_done"}, {"event": "task_done"}, {"event": "task_done"}, {"event": "task_error"},
    {"event": "interrupt"},
    {"event": "clarify_shown"}, {"event": "clarify_shown"}, {"event": "clarify_shown"},
    {"event": "clarify_answered"}, {"event": "clarify_answered"},
    {"event": "confirm_shown"}, {"event": "confirm_decided", "detail": json.dumps({"cid": 1, "approve": True})},
    {"event": "scroll_force", "detail": json.dumps({"reason": "user"})},
    {"event": "scroll_force", "detail": json.dumps({"reason": "clarify"})},
    {"event": "scroll_force", "detail": json.dumps({"reason": "unknown_bug"})},   # 抢夺！
])
s = repo.summary(days=7)
# W1 段已入桶 4 条（2 done + 1 error + 1 interrupt），与 S 段同库聚合（全表口径）：
# done = 2+3 = 5, error = 1+1 = 2, interrupt = 1+1 = 2
check("S1 完成率 = done/(done+error) = 5/7 ≈ 0.7143", s["tasks"]["completion_rate"] == 0.7143, s["tasks"])
check("S2 中断率 = 2/7 ≈ 0.2857", s["interrupt"]["rate"] == 0.2857, s["interrupt"])
check("S3 澄清命中率 = 2/3 ≈ 0.6667", abs(s["clarify"]["hit_rate"] - 0.6667) < 1e-4, s["clarify"])
check("S4 确认闸门 曝光1/处置1", s["confirm_gate"] == {"shown": 1, "decided": 1}, s["confirm_gate"])
check("S5 滚动抢夺守护：非法 reason 计 1（§8.4 红线告警）", s["scroll_steal"]["count"] == 1, s["scroll_steal"])

# ── 3. 会话内时序指标（失败自恢复 / 等待放弃）──
print("[Q] 时序口径（conversation_id>0，按 id 顺序）")
repo.insert_batch([
    # conv 1：error 后恢复 done → 自恢复
    {"event": "task_error", "conversation_id": 1},
    {"event": "task_done",  "conversation_id": 1},
    # conv 2：error 后无 done → 未恢复
    {"event": "task_error", "conversation_id": 2},
    # conv 3：两次 clarify_shown 一次 answered → 一弃一命中（1:1 配对）
    {"event": "clarify_shown",   "conversation_id": 3},
    {"event": "clarify_shown",   "conversation_id": 3},
    {"event": "clarify_answered","conversation_id": 3},
    # conv 4：confirm_shown 无处置 → 放弃
    {"event": "confirm_shown",   "conversation_id": 4},
    # conv 5：done 在 error 之前 → 不算恢复（时序判定，非共现判定）
    {"event": "task_done",  "conversation_id": 5},
    {"event": "task_error", "conversation_id": 5},
])
s2 = repo.summary(days=7)
check("Q1 失败自恢复 = 1/3（conv5 的 done 在 error 前不算）",
      s2["failure_recovery"] == {"err_convs": 3, "recovered": 1, "rate": 0.3333}, s2["failure_recovery"])
check("Q2 等待放弃 = 2/3（conv3 两次shown一次answered→一弃一配对，conv4 全弃）",
      s2["wait_abandon"] == {"waits": 3, "abandoned": 2, "rate": 0.6667}, s2["wait_abandon"])

# ── 4. 边界 ──
print("[E] 边界")
s0 = UxMetricsRepo(sqlite3.connect(":memory:")).summary(days=7)
check("E1 空库比率全 None（不除零）", s0["tasks"]["completion_rate"] is None and s0["scroll_steal"]["count"] == 0)
check("E2 days 越界收敛", UxMetricsRepo(sqlite3.connect(":memory:")).summary(days=999)["days"] == 90)

print(f"\n══ {pass_n} PASS / {len(fail_l)} FAIL ══")
if fail_l:
    print("失败项: " + " | ".join(fail_l)); sys.exit(1)
