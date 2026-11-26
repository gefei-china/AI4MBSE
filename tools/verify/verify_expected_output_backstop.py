# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：expected_output（完成标准）落库兜底：prompt 口径一致 + create_plan 唯一收口兜底 + config 判空防误覆盖。★ 归 local：C 组读真库 11 月失败率口径（空20% vs 非空6.1%，干净库无数据必红）。实测 0.4s
"""expected_output（完成标准）落库兜底门禁（2026-11-17）。

## 问题与证据（全只读实测）

用户问「subtask 的上下文为什么只有几十个 token」。排查结论：
**派发载荷薄是真实现象，但不是丢失** —— planner 设计上就要求"精简事实"
（`build_subtask_context` 把依赖闭包摘要/产物摘要/交付规范显式注入，
子任务总 prompt 均值 2 万 token）。真正的缺口在**完成标准**：

| 月份 | context 空置率 | expected_output 空置率 | 任务失败率 |
|---|---|---|---|
| 2026-09 | 93.4% | 99.5% | 24.4% |
| 2026-10 | 0.0% | 60.4% | 30.6% |
| 2026-11 | 1.6% | **24.2%** | 4.3% |

11 月内部：expected_output 非空 141 例失败率 **2.1%**；空 45 例失败率 **11.1%**（5.3 倍）。

## 根因（两处，都已修）

  ① planner prompt **自相矛盾**：示例行写 `"expected_output": "…(可空)"`，
     下一行又要求「每个任务都要写 expected_output」—— 同一字段两处口径不一致，
     模型按"可空"省略（这正是本仓"同一语义两处各写一份迟早错位"的活样本）。
  ② 落库只警告不修：`create_plan` 对缺失仅 `logging.warning`，
     静默穿透到子任务 —— `build_subtask_context` 对空值**整行不输出**
     "- 期望产出" ⇒ 子任务在无验收依据下裸跑。

## 修复

  ① prompt 示例改为「必填：写清**可核查**的验收判据」+ 给正反例
  ② `create_plan`（所有建计划入口的**唯一收口**）缺失 ⇒ 从 title 兜底生成
     最小完成标准；判空**同时看 config**（委派协议字段可能被收进 config，
     只看顶层会误判，兜底文本反而覆盖真实完成标准、劣化注入）

## 断言分组

  A. 代码级：prompt 口径一致、兜底存在、判空含 config
  B. 行为级：内存库真跑 `create_plan`（缺⇒补、有⇒不覆盖、config 有⇒不误判）
  C. 真库证据：只读核对上表的 11 月数字（口径漂移时翻红）

用法：``.venv/Scripts/python.exe tools/verify/verify_expected_output_backstop.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from task_queue import TaskQueue  # noqa: E402

FAILS: list = []
N = 0

PLANNER = os.path.join(REPO, "workflows", "planner.py")
TASKQUEUE = os.path.join(REPO, "services/task_queue.py")

FAIL_RATE_EMPTY = 11.1      # 11 月 expected_output 空的任务失败率（%）
FAIL_RATE_FULL = 2.1        # 非空的任务失败率（%）


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


DDL = """
CREATE TABLE agents (
    id INTEGER PRIMARY KEY, name TEXT, display_name TEXT DEFAULT '',
    status TEXT DEFAULT 'active');
CREATE TABLE agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_key TEXT,
    title TEXT, agent_id TEXT, agent_name_snapshot TEXT DEFAULT '',
    task_type TEXT DEFAULT 'agent', config TEXT DEFAULT '{}',
    deps TEXT DEFAULT '[]', status TEXT DEFAULT 'ready', assigned_by TEXT DEFAULT '',
    seq INTEGER DEFAULT 0, error TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
    context TEXT DEFAULT '', expected_output TEXT DEFAULT '');
"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute("INSERT INTO agents (name, status) VALUES ('设计Agent','active')")
    conn.commit()
    return conn


# ── A. 代码级 ─────────────────────────────────────────────────
def case_code() -> None:
    print("\nA. 代码级（prompt 口径一致 / 兜底存在 / 判空含 config）")
    p = open(PLANNER, encoding="utf-8").read()
    t = open(TASKQUEUE, encoding="utf-8").read()

    _eo_line = next((ln for ln in p.splitlines()
                     if '"expected_output":' in ln and "完成标准" in ln), "")
    check("A1 ★ prompt 示例不再写 expected_output「(可空)」（矛盾源头）",
          bool(_eo_line) and "可空" not in _eo_line and "必填" in _eo_line,
          repr(_eo_line)[:160])
    check("A2 prompt 含「必填」+ 可核查判据要求",
          "必填" in p and "可核查" in p, "口径未改")

    _i = t.find("_noexp = []")
    check("A3 create_plan 存在确定性兜底（不再只警告）",
          _i > 0 and 't["expected_output"] = (' in t, "兜底赋值缺失")
    check("A4 ★ 判空同时看 config（否则兜底文本覆盖真实完成标准）",
          '_cfg_eo = str((t.get("config") or {}).get("expected_output")' in t,
          "判空漏了 config")
    check("A5 兜底留痕（warning 里说明已兜底）",
          "已兜底生成最小完成标准" in t, "留痕缺失")
    check("A6 兜底文本含 title（可追溯是哪条任务的兜底）",
          "%.60s" in t and "「" in t, "兜底文本缺 title")


# ── B. 行为级 ─────────────────────────────────────────────────
def _plan(eo1="", eo2=None, cfg_eo=None):
    t2 = {"key": "t2", "title": "生成 sequence 视图代码", "agent": "设计Agent",
          "deps": ["t1"], "task_type": "agent", "config": {"required": True}}
    if eo2 is not None:
        t2["expected_output"] = eo2
    if cfg_eo is not None:
        t2["config"]["expected_output"] = cfg_eo
    return [
        {"key": "t1", "title": "分析需求并输出需求条目清单", "agent": "设计Agent",
         "deps": [], "task_type": "agent", "config": {"required": True},
         "context": "目标系统：热管理", "expected_output": eo1},
        t2,
    ]


def _rows(conn):
    return {r["task_key"]: dict(r) for r in conn.execute(
        "SELECT * FROM agent_tasks").fetchall()}


def case_behavior() -> None:
    print("\nB. 行为级（create_plan 真实现 + 内存库）")
    # B1 缺失 ⇒ 兜底生成，且含 title
    conn = _conn()
    n = TaskQueue.create_plan(conn, 1, _plan(eo1=""), strict=False)
    rows = _rows(conn)
    check("B1 两条任务都落库", n == 2 and len(rows) == 2, str(n))
    _eo1 = rows["t1"]["expected_output"]
    check("B2 缺失 ⇒ 兜底生成非空完成标准", bool(_eo1.strip()), repr(_eo1)[:80])
    check("B3 兜底文本引用 title（可追溯）",
          "分析需求并输出需求条目清单" in _eo1, repr(_eo1)[:120])
    check("B4 兜底文本要求「说明缺口」（不许静默略过）",
          ("显式说明缺口" in _eo1) or ("不得静默略过" in _eo1), repr(_eo1)[:160])

    # B5 非空 ⇒ 原样保留（兜底绝不覆盖 planner 写的）
    conn2 = _conn()
    TaskQueue.create_plan(conn2, 2, _plan(eo1="", eo2="输出包含 5 条消息的 sequence 视图，validate n_hard=0"),
                          strict=False)
    rows2 = _rows(conn2)
    check("B5 planner 写了 ⇒ 原样保留（不被兜底覆盖）",
          rows2["t2"]["expected_output"] == "输出包含 5 条消息的 sequence 视图，validate n_hard=0",
          repr(rows2["t2"]["expected_output"])[:100])

    # B6 config 有、顶层无 ⇒ 不误判（列被兜底覆盖反而劣化注入：列优先于 config）
    conn3 = _conn()
    TaskQueue.create_plan(conn3, 3, _plan(eo1="", cfg_eo="必须复用上游需求条目，不得新建"),
                          strict=False)
    rows3 = _rows(conn3)
    check("B6 ★ config 已有 ⇒ 不写兜底文本（避免劣化列优先注入）",
          "兜底生成" not in rows3["t2"]["expected_output"],
          repr(rows3["t2"]["expected_output"])[:120])

    # B7 落库截断口径不变（≤500）
    conn4 = _conn()
    TaskQueue.create_plan(conn4, 4, _plan(eo1="长" * 600), strict=False)
    check("B7 落库仍按 ≤500 截断（不因兜底改动截断口径）",
          len(_rows(conn4)["t1"]["expected_output"]) <= 500, "")


# ── C. 真库证据 ───────────────────────────────────────────────
def case_real_db() -> None:
    print("\nC. 真库证据（只读）：核对口径未漂移")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("C0 生产库存在", False, db)
        return
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("""SELECT
              ROUND(100.0*SUM(length(COALESCE(expected_output,''))=0)/COUNT(*),1) eo_empty_pct,
              ROUND(100.0*SUM(status='failed')/COUNT(*),1) fail_pct,
              SUM(length(COALESCE(expected_output,''))=0) eo_empty_n,
              COUNT(*) n FROM agent_tasks WHERE created_at>='2026-11-01'""").fetchone()
        print("    11 月：n=%d，expected_output 空置率 %.1f%%（空置 %d 条），任务失败率 %.1f%%"
              % (r["n"], r["eo_empty_pct"], r["eo_empty_n"] or 0, r["fail_pct"]))
        # ★ 2026-11-26 修判据方向：原判据是「与登记值 24.2% 相差 ≤10pct」（双边）。
        #   问题：**指标真的变好也会判红**。实测 2026-11-26 空置率 13.3%，差 10.9pct ⇒ 红，
        #   而这恰恰是兜底上线后的**期望结果**（登记时 n=186 / 空 45；现在 n=315 / 空 42
        #   ⇒ 分母近翻倍而空置绝对数还略降），不是口径漂移。
        #   「变好」与「口径坏了」在原判据下**信号完全相同** ⇒ 必须拆成两条：
        #     C1  只在**变差**时红（守兜底不被回退）—— 这是真正要保护的那一侧；
        #     C1b 变好时要求**样本量未缩水**（分母腰斩 ⇒ 是口径漂移，不是改善）。
        #   ⚠️ 别把 C1b 写成「n ≥ 某个固定数」：那个数会随月份增长再次过期，
        #      条件挂在"变好"这一前提上，只有改善时才要求样本量，正常波动不误伤。
        _EO_BASELINE, _EO_N_BASE = 24.2, 186      # 登记值（2026-11-17 实测）
        check("C1 11 月空置率未变差（兜底仍生效；容差 +10pct）",
              r["eo_empty_pct"] <= _EO_BASELINE + 10.0,
              "实测 %.1f%% > 登记 %.1f%%+10 ⇒ 兜底可能被回退" % (r["eo_empty_pct"], _EO_BASELINE))
        _improved = r["eo_empty_pct"] < _EO_BASELINE - 10.0
        check("C1b 空置率改善时样本量未缩水（排除『分母变小』冒充改善）",
              (not _improved) or (r["n"] or 0) >= _EO_N_BASE * 0.9,
              "空置率 %.1f%% 显著低于登记 %.1f%%，但 n=%d < 登记 %d×0.9 ⇒ 疑似口径漂移而非改善"
              % (r["eo_empty_pct"], _EO_BASELINE, r["n"] or 0, _EO_N_BASE))
        r2 = conn.execute("""SELECT
              ROUND(100.0*SUM(status='failed')/SUM(CASE WHEN length(COALESCE(expected_output,''))=0 THEN 1 ELSE 0 END),1) fr_empty,
              ROUND(100.0*SUM(status='failed')/SUM(CASE WHEN length(COALESCE(expected_output,''))!=0 THEN 1 ELSE 0 END),1) fr_full
              FROM agent_tasks WHERE created_at>='2026-11-01'""").fetchone()
        print("    11 月失败率：空 %.1f%% vs 非空 %.1f%%（差距倍数 %.1f）"
              % (r2["fr_empty"] or 0, r2["fr_full"] or 0,
                 (r2["fr_empty"] or 0) / max(r2["fr_full"] or 1, 0.1)))
        check("C2 ★ 空置任务的失败率仍显著高于非空（差距 <1.5 倍 ⇒ 判据失真需复核）",
              (r2["fr_empty"] or 0) >= (r2["fr_full"] or 0) * 1.5,
              "空 %.1f%% / 非空 %.1f%%" % (r2["fr_empty"] or 0, r2["fr_full"] or 0))
    finally:
        conn.close()


def main() -> int:
    print("expected_output（完成标准）落库兜底门禁")
    print("=" * 68)
    case_code()
    case_behavior()
    case_real_db()
    print("\n" + "=" * 68)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
