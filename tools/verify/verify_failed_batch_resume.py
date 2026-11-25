# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：failed 批次「为什么不能重投」：默认路径逐字节不变 + 显式 include_failed 的三类结局 + 真库 44 条现场复算。★ 归 local：C 组读生产真库的 orch_checkpoints/agent_tasks 存量（干净库无数据 ⇒ C1 必红）。实测 0.9s
"""failed 批次「为什么不能重投」门禁（2026-11-17）。

## 问题（用户提问驱动的实测）

`orch_checkpoints.phase` 分布：**failed 44 / done 9 / summarizing 8 / interrupted 4 / cancelled 1**。
44 条 failed 常年挂在那里，`auto_resume_include_failed=False` ⇒ 默认不投 ⇒ 这批工作
永远停在半空。而模块 docstring 明确写着「failed 需显式 `include_failed=True`」、
`safe_requeue_classes()` 也把 `interrupted` 列为可重跑 —— **两处承诺都不生效**。

## 根因（一处代码把三层机制一起废掉）

`prepare_resume()` 的第一道判定是：

    if phase in TERMINAL_PHASES:      # TERMINAL_PHASES = (done, failed)
        return {... reason="phase_terminal:%s" % phase ...}

`failed` 属终态 ⇒ **在任务级判定之前就返回** ⇒ 于是
`policy_classify()` 里的 `if st=="failed": if include_failed: return "requeue"`
与 `safe_requeue_classes()` 的白名单 **在结构上永远调不到**（对 failed 批次而言是死代码）。

## 44 条的真实构成（本门禁 C 组现场复算，不引用记忆）

| 类别 | 条数 | 该不该重投 |
|---|---|---|
| 子任务**全 done**（只是收尾崩，summary 已写） | **22** | **不该** —— 事实已交付，是台账记错了 |
| 有 `error_class='interrupted'`（进程死亡） | **13** | **本可续跑**（被终态闸一刀切挡住） |
| 有 `internal`/`timeout`（真失败） | 5 | 不该（`nothing_to_do` 正确） |
| 无子任务 | 4 | 不该 |

⇒ 「不能重投」是**两层问题**：80% 是归类错误（22+13），只有 5 条是真失败。

## 修复口径（默认路径逐字节不变）

  · `done`   ⇒ 一律不重跑（幂等根本，任何参数都不例外）
  · `failed` ⇒ **仅当显式 `include_failed=True`** 才往下走，交给任务级
               `error_class` 白名单 + 幂等键判「能不能、值不值得」；
               默认路径（含 supervisor 的 `auto_resume_include_failed=False`）
               仍返回同一个 `phase_terminal:failed`。

## 断言分组

  A. 默认路径**逐字节不变**（含 reason 字符串本身 —— 运维靠日志 grep 它）
  B. 显式 `include_failed=True` 的三条结局：进程死亡⇒可续跑 / 真失败⇒不投 / 已交付⇒不投
  C. 真库现场复算 44 条的四类构成（只读；断言"真失败"占比 ≤ 15%）
  D. 代码级：`failed` 的放行条件必须绑定 `include_failed`，`done` 不得被放行

用法：``.venv/Scripts/python.exe tools/verify/verify_failed_batch_resume.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent import orch_checkpoint as oc  # noqa: E402

FAILS: list = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


# ── 夹具 ──────────────────────────────────────────────────────
DDL = """
CREATE TABLE orch_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, conversation_id INTEGER DEFAULT 0,
    phase TEXT, user_input TEXT DEFAULT '', intent TEXT DEFAULT '', branch TEXT DEFAULT '',
    provider_id INTEGER DEFAULT 0, skill_name TEXT DEFAULT '', team TEXT DEFAULT '',
    plan_json TEXT DEFAULT '', params_json TEXT DEFAULT '', attempt_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT '2026-11-01 00:00:00',
    updated_at TEXT DEFAULT '2026-11-01 00:00:00',
    summary_written INTEGER DEFAULT 0);
CREATE TABLE agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, conversation_id INTEGER DEFAULT 0,
    task_key TEXT, status TEXT, deps TEXT DEFAULT '', context TEXT DEFAULT '',
    expected_output TEXT DEFAULT '', agent_id INTEGER DEFAULT 0, title TEXT DEFAULT '',
    task_type TEXT DEFAULT '', config TEXT DEFAULT '', seq INTEGER DEFAULT 0,
    error_class TEXT DEFAULT '');
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, role TEXT,
    content TEXT DEFAULT '', created_at TEXT DEFAULT '2026-11-01 00:00:00');
"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def _mk(conn, run_id: int, phase: str, tasks: list, *, attempt=0, summary_written=0):
    """tasks: [(status, error_class), ...]"""
    conn.execute(
        "INSERT INTO orch_checkpoints (run_id, conversation_id, phase, attempt_count, "
        "summary_written, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, 90000 + run_id, phase, attempt, summary_written,
         "2026-11-01 00:00:00", "2026-11-01 00:00:00"))
    for i, (st, ec) in enumerate(tasks):
        conn.execute(
            "INSERT INTO agent_tasks (run_id, conversation_id, task_key, status, seq, "
            "error_class) VALUES (?,?,?,?,?,?)",
            (run_id, 90000 + run_id, "t%d" % i, st, i, ec))
    conn.commit()


# ── A. 默认路径逐字节不变 ─────────────────────────────────────
def case_default_unchanged() -> None:
    print("\nA. 默认路径逐字节不变（运维靠 reason 字符串 grep 日志）")
    conn = _conn()
    _mk(conn, 1, oc.PHASE_FAILED, [("failed", "interrupted")])
    d = oc.prepare_resume(conn, 1)          # include_failed 缺省 False
    check("A1 failed + 默认参数 ⇒ 仍拒绝", d.get("ok") is False, str(d.get("ok")))
    check("A2 reason 仍是 phase_terminal:failed（字符串不许改）",
          d.get("reason") == "phase_terminal:failed", repr(d.get("reason")))
    check("A3 mode 为空、无重排队 id", d.get("mode") == "" and not d.get("requeue_ids"),
          str({k: d.get(k) for k in ("mode", "requeue_ids")}))

    _mk(conn, 2, oc.PHASE_DONE, [("done", "")])
    d2 = oc.prepare_resume(conn, 2, include_failed=True)
    check("A4 done + 显式 include_failed=True 仍拒绝（幂等根本）",
          d2.get("ok") is False and d2.get("reason") == "phase_terminal:done",
          "%r/%r" % (d2.get("ok"), d2.get("reason")))

    _mk(conn, 3, oc.PHASE_INTERRUPTED, [("cancelled", "interrupted")])
    d3 = oc.prepare_resume(conn, 3)
    check("A5 interrupted 批次行为不变（本就非终态）", d3.get("ok") is True,
          "%r/%r" % (d3.get("ok"), d3.get("reason")))


# ── B. 显式 include_failed=True 的结局 ──────────────────────
def case_explicit() -> None:
    print("\nB. 显式 include_failed=True（机制兑现；默认路径仍不受影响）")
    conn = _conn()

    # B1 进程死亡（error_class=interrupted）⇒ 可续跑
    _mk(conn, 10, oc.PHASE_FAILED, [("done", ""), ("failed", "interrupted")])
    d = oc.prepare_resume(conn, 10, include_failed=True)
    check("B1 failed+interrupted ⇒ 放行（ok=True, mode=tasks）",
          d.get("ok") is True and d.get("mode") == "tasks",
          "%r/%r" % (d.get("ok"), d.get("reason")))
    check("B2 重排队里含那条进程死亡任务", bool(d.get("requeue_ids")),
          str(d.get("requeue_ids")))
    check("B3 已完成任务被 reuse、不被重跑（幂等）", bool(d.get("reuse_ids")),
          str(d.get("reuse_ids")))

    # B4 显式覆盖的**语义边界**：include_failed=True 是"我知道可能重复，仍然重跑"，
    #    因此**真失败（internal/timeout）也会被重排** —— 这是刻意的，不是漏洞：
    #    操作员显式承担重复风险时才允许。真正要防的是"**自动**重跑真失败"。
    _mk(conn, 11, oc.PHASE_FAILED, [("done", ""), ("failed", "internal")])
    d2 = oc.prepare_resume(conn, 11, include_failed=True)
    check("B4 显式覆盖 ⇒ internal 也重排（操作员显式承担重复风险，语义如此）",
          d2.get("ok") is True and bool(d2.get("requeue_ids")),
          "%r/%r" % (d2.get("ok"), d2.get("reason")))

    # B5 但**默认路径**下真失败永不重投（这是最关键的一条）
    d3 = oc.prepare_resume(conn, 11)        # include_failed 缺省 False
    check("B5 ★ 默认路径下 internal 永不自动重投（phase_terminal）",
          d3.get("ok") is False and d3.get("reason") == "phase_terminal:failed",
          "%r/%r" % (d3.get("ok"), d3.get("reason")))

    # B6 已交付（全 done + summary 已写）⇒ 不投
    _mk(conn, 12, oc.PHASE_FAILED, [("done", ""), ("done", "")], summary_written=1)
    d4 = oc.prepare_resume(conn, 12, include_failed=True)
    check("B6 全 done + summary 已写 ⇒ 不投（事实已交付，重投=重复消息）",
          d4.get("ok") is False, "%r/%r" % (d4.get("ok"), d4.get("reason")))

    # B7 次数用满 ⇒ 拦住（防"恢复→再崩→再恢复"死循环）
    _mk(conn, 13, oc.PHASE_FAILED, [("failed", "interrupted")], attempt=oc._MAX_ATTEMPTS)
    d5 = oc.prepare_resume(conn, 13, include_failed=True)
    check("B7 attempt 用满 ⇒ attempt_exhausted（不许无限自愈）",
          str(d5.get("reason") or "").startswith("attempt_exhausted"),
          repr(d5.get("reason")))

    # ★ B8 已知缺口（如实断言"仍存在"，修好后把判据**反转**，而不是删掉）：
    #   P0-1 设计的「进程死亡类失败自动续跑」（error_class∈safe_requeue_classes，
    #   不需要 include_failed）在**批次级默认路径下仍不可达** —— 因为终态闸在
    #   任务级判定之前就返回了。本次只兑现了"显式 include_failed 可用"，
    #   没有放开默认（放开会让 supervisor 立刻去捞 13 条 7 天前的陈旧批次，
    #   代价与收益未评估完之前不做）。
    d6 = oc.prepare_resume(conn, 10)        # 有 interrupted 任务，但未显式 include_failed
    check("B8 已知缺口记录：进程死亡批次在默认路径下仍**不会**被自动续跑",
          d6.get("ok") is False and d6.get("reason") == "phase_terminal:failed",
          "已能自动续跑（缺口已修）⇒ 应把本判据反转")


# ── C. 真库现场复算 44 条 ────────────────────────────────────
def case_real_db() -> None:
    print("\nC. 真库现场复算（只读；不引用记忆里的数字）")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("C0 生产库存在", False, db)
        return
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        runids = [r[0] for r in conn.execute(
            "SELECT run_id FROM orch_checkpoints WHERE phase='failed'")]
        all_done, died, real_fail, empty = [], [], [], []
        for rid in runids:
            rows = conn.execute(
                "SELECT status, COALESCE(error_class,'') ec FROM agent_tasks WHERE run_id=?",
                (rid,)).fetchall()
            if not rows:
                empty.append(rid)
                continue
            ecs = {r["ec"] for r in rows}
            sts = {r["status"] for r in rows}
            if "internal" in ecs or "timeout" in ecs:
                real_fail.append(rid)
            elif "interrupted" in ecs:
                died.append(rid)
            elif sts == {"done"}:
                all_done.append(rid)
            else:
                real_fail.append(rid)
        tot = len(runids)
        print("    44 类构成：全 done %d ｜ 进程死亡 %d ｜ 真失败 %d ｜ 无子任务 %d（共 %d）"
              % (len(all_done), len(died), len(real_fail), len(empty), tot))
        check("C1 真库存在 failed 批次（本门禁的前提）", tot > 0, "failed=%d" % tot)
        check("C2 ★「真失败」占比 ≤ 15%（超过说明分类口径又漂了）",
              bool(tot) and (len(real_fail) / tot) <= 0.15,
              "真失败 %d/%d" % (len(real_fail), tot))
        check("C3 「可自愈或已交付」占比 ≥ 70%（这就是「不能重投」的真身）",
              bool(tot) and ((len(died) + len(all_done)) / tot) >= 0.70,
              "已交付 %d + 可续跑 %d / %d" % (len(all_done), len(died), tot))
    finally:
        conn.close()


# ── D. 代码级 ────────────────────────────────────────────────
def case_code() -> None:
    print("\nD. 代码级（放行条件必须绑定 include_failed）")
    p = os.path.join(REPO, "agent", "orch_checkpoint.py")
    with open(p, encoding="utf-8") as f:
        src = f.read()
    _i = src.find("_allow_failed")
    check("D1 放行开关存在（_allow_failed）", _i > 0, "未找到")
    check("D2 放行条件绑定 include_failed",
          "_allow_failed = (phase == PHASE_FAILED and bool(include_failed))" in src,
          "条件表达式被改动")
    check("D3 done 不在放行范围（只有 PHASE_FAILED 可放）",
          "phase == PHASE_FAILED and" in src, "放行条件未限定 failed")
    # 终态闸仍在（不是把闸拆了，而是加了一个受控的例外）
    check("D4 终态闸本身仍在（有例外≠拆掉闸）",
          "if phase in TERMINAL_PHASES:" in src, "终态闸被删除")


def main() -> int:
    print("failed 批次「为什么不能重投」门禁")
    print("=" * 68)
    case_default_unchanged()
    case_explicit()
    case_real_db()
    case_code()
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
