# -*- coding: utf-8 -*-
"""verify_cancel_and_reap —— 批次四 #19：取消链路 + 卡住自动收敛（健壮性）。

## 为什么要有这个门禁（真实账单，不是理论风险）
真库实测（2026-10-14 只读）：`run_id=530` 卡在 `phase=planned` 已 **10 天**、
`summary_written=0`、4 条 `agent_tasks` 悬着 —— **没有任何手段中止它**。
⇒ 「没有取消链路」不是抽象债务，它已经产生了一条永远不收敛的 run。

## 三条核心设计（各自都有反例驱动）
1. **协作式中止**，不是 kill 线程
   Python 无法安全杀线程，而子任务正持有 LLM 流式连接 / DB 事务 / 文件句柄
   ⇒ 强杀造成资源泄漏 + 事务悬挂，**比不取消更糟**。
   ⇒ 落一个"取消意图" + worker 在**产出热路径**上自行 break。
2. **终态不可取消**
   `done` 的任务取消不了 —— 否则"取消"变成"把已完成的事改成没做"。
3. **reaper 不看任务状态，只看 checkpoint 的推进时刻**
   ⚠️ 这是实测踩出来的：初版加了 `EXISTS(该 run 下有非终态任务)` 才收敛
   ⇒ **真库 run 530 恰好不符合**（4 条任务全是 `failed`，是终态）
   ⇒ 按初版条件它**永远不会被回收**。
   ⇒ 去掉该条件：判断"是否卡住"只看 checkpoint 自身。

## 判据
### C 组 · 取消
  C1 取消非终态 ⇒ 变 cancelled，**同 run 的终态任务不受影响**
  C2 幂等：重复取消返回 False 且不报错
  C3 终态（done/failed）不可取消
  C4 取消后探针立即为 True（worker 能看到 ⇒ 才谈得上"中止"）
  C5 `cancel_run` 整批取消 + **checkpoint 同步**（否则下次仍出现在"可恢复"清单）

### R 组 · reaper
  R1 卡住的 run 被收敛（**含"任务全终态但 checkpoint 卡住"这个真库形态**）
  R2 正在跑的**不被误杀**
  R3 慢但正常的（阈值内）**不被误杀**
  R4 ★反向判据：把"任务状态"条件加回去 ⇒ R1 必须判红
     （否则可以靠"永远不收敛"通过 —— 本门禁第一版就栽在这）

### W 组 · 接线
  W1 `stream.py` 的子任务产出循环里有协作式中止探针
  W2 探针跑在**独立连接**上（不碰主连接 —— 否则与"悬挂事务"同型）
  W3 探针被 try 包住（跑在热路径上，抛异常就是新的崩溃源）

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_cancel_and_reap.py
"""
from __future__ import annotations

import ast
import os
import shutil
import sqlite3
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
PROD_DB = os.path.join(_ROOT, "mbse.db")

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + detail) if detail and not ok else ""))
    return bool(ok)


def _fresh_db(tag):
    """每次用例一份**干净副本**，互不污染（用例要断言精确行数）。"""
    d = tempfile.mkdtemp(prefix="cancel_%s_" % tag)
    p = os.path.join(d, "t.db")
    s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    t = sqlite3.connect(p)
    s.backup(t)
    t.close()
    s.close()
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        from database.migrations.columns import _migrate_columns
        _migrate_columns(conn)
        conn.commit()
    except Exception:
        pass
    return conn, d


def _mk_run(conn, run_id, phase, updated_expr="datetime('now')", task_status="failed",
            n_tasks=1, summary_written=0):
    rid = conn.execute(
        "INSERT INTO conversations (title,intent,user_id,project_id)"
        " VALUES ('verify','chat',1,'')").lastrowid
    conn.execute(
        "INSERT INTO orch_checkpoints (run_id,conversation_id,phase,user_input,intent,"
        "created_at,updated_at,summary_written) VALUES (?,?,?,?,?,?,%s,?)"
        % updated_expr,
        (run_id, rid, phase, "x", "design",
         "datetime('now','-10 days')" if updated_expr != "datetime('now')"
         else "datetime('now')", summary_written))
    ids = []
    for i in range(n_tasks):
        ids.append(conn.execute(
            "INSERT INTO agent_tasks (run_id,task_key,title,status) VALUES (?,?,?,?)",
            (run_id, "t%d" % i, "X", task_status)).lastrowid)
    conn.commit()
    return rid, ids


def c_group():
    print("\n=== C 组 · 取消 ===")
    from task_queue import TaskQueue
    conn, d = _fresh_db("c")
    try:
        # 一个 run 造两个任务：一个 ready（可取消）、一个 done（终态不可取消）
        _rid, _ids = _mk_run(conn, 95001, "planned", task_status="ready", n_tasks=2)
        t_ready, t_done = _ids[0], _ids[1]
        conn.execute("UPDATE agent_tasks SET status='done' WHERE id=?", (t_done,))
        conn.commit()

        check("C0 基线：ready/done 各一",
              [r[0] for r in conn.execute(
                  "SELECT status FROM agent_tasks WHERE run_id=95001 ORDER BY id")] == ["ready", "done"],
              [r[0] for r in conn.execute(
                  "SELECT status FROM agent_tasks WHERE run_id=95001 ORDER BY id")])

        check("C0b 未取消时探针为 False",
              TaskQueue.is_cancel_requested(conn, t_ready) is False)

        ok1 = TaskQueue.cancel(conn, t_ready)
        sts = [r[0] for r in conn.execute(
            "SELECT status FROM agent_tasks WHERE run_id=95001 ORDER BY id")]
        check("C1 取消非终态 ⇒ cancelled，且同 run 终态不受影响",
              ok1 and sts == ["cancelled", "done"], "sts=%s" % sts)

        check("C2 幂等：重复取消返回 False 且不抛",
              TaskQueue.cancel(conn, t_ready) is False)

        check("C3 终态不可取消（done 不会被改成 cancelled）",
              TaskQueue.cancel(conn, t_done) is False
              and conn.execute("SELECT status FROM agent_tasks WHERE id=?",
                               (t_done,)).fetchone()[0] == "done")

        check("C4 取消后探针立即为 True（worker 看得到）",
              TaskQueue.is_cancel_requested(conn, t_ready) is True)

        _rid2, ids = _mk_run(conn, 95002, "planned", task_status="ready", n_tasks=3)
        n = TaskQueue.cancel_run(conn, 95002)
        ph = conn.execute("SELECT phase FROM orch_checkpoints WHERE run_id=95002"
                          ).fetchone()[0]
        check("C5 cancel_run 整批取消 + checkpoint 同步",
              n == 3 and ph == "cancelled", "n=%s phase=%s" % (n, ph))
    finally:
        conn.close()
        shutil.rmtree(d, ignore_errors=True)


def r_group():
    print("\n=== R 组 · reaper（卡住自动收敛）===")
    from task_queue import TaskQueue
    conn, d = _fresh_db("r")
    try:
        # R1 ★真库 run 530 的真实形态：任务**全终态**（failed）但 checkpoint 卡住
        _mk_run(conn, 96001, "planned",
                updated_expr="'2026-10-04 09:14:21'", task_status="failed", n_tasks=4)
        # R2 正在跑
        _mk_run(conn, 96002, "running", updated_expr="datetime('now')",
                task_status="running", n_tasks=1)
        # R3 慢但正常（15 分钟前推进，阈值 30 分钟）
        _mk_run(conn, 96003, "running", updated_expr="datetime('now','-15 minutes')",
                task_status="running", n_tasks=1)
        conn.commit()

        out = TaskQueue.reap_stuck_runs(conn, older_than_minutes=30)
        got = {int(o["run_id"]) for o in out}
        p1 = conn.execute("SELECT phase FROM orch_checkpoints WHERE run_id=96001"
                          ).fetchone()[0]
        p2 = conn.execute("SELECT phase FROM orch_checkpoints WHERE run_id=96002"
                          ).fetchone()[0]
        p3 = conn.execute("SELECT phase FROM orch_checkpoints WHERE run_id=96003"
                          ).fetchone()[0]

        check("R1 卡住的 run 被收敛（**含任务全终态这一形态**）",
              96001 in got and p1 == "cancelled", "phase=%s" % p1)
        check("R2 正在跑的**不被误杀**", 96002 not in got and p2 == "running",
              "phase=%s" % p2)
        check("R3 慢但正常的（阈值内）不被误杀", 96003 not in got and p3 == "running",
              "phase=%s" % p3)

        # R4 ★反向判据：验证「要求存在非终态任务」那种写法**收不到真库形态**
        # ⚠️ 必须扫**真实的 SQL 字符串字面量**，不能扫整个源码段 ——
        #   `reap_stuck_runs` 的注释里就写着 "原实现加了 EXISTS(...)"，
        #   用字符串 find 判会**命中注释** ⇒ 误报"源码仍带该条件"。
        #   ⇒ 用 AST 取函数体内所有 `str` 常量，只在其中判 EXISTS。
        tq = ast.parse(open(os.path.join(_ROOT, "task_queue.py"), encoding="utf-8").read())
        fn = next((n for n in ast.walk(tq)
                   if isinstance(n, ast.FunctionDef) and n.name == "reap_stuck_runs"), None)
        sqls = [s.value for s in ast.walk(fn)
                if isinstance(s, ast.Constant) and isinstance(s.value, str)
                and ("SELECT" in s.value or "UPDATE" in s.value)] if fn else []
        has_task_cond = any("EXISTS" in q and "status NOT IN" in q for q in sqls)
        # 并在同一个库上验证该条件会漏掉真库形态
        n_all_terminal = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE run_id=96001"
            " AND status NOT IN ('done','failed','cancelled')").fetchone()[0]
        check("R4 反向判据：'要求存在非终态任务'那种写法会漏掉真库形态",
              n_all_terminal == 0 and has_task_cond is False,
              "真库形态的非终态任务=%d（=0，故那种写法收不到）；"
              "reap 的真实 SQL 里仍带该条件=%s" % (n_all_terminal, has_task_cond))
    finally:
        conn.close()
        shutil.rmtree(d, ignore_errors=True)


def w_group():
    print("\n=== W 组 · 主路径接线 ===")
    src = open(STREAM, encoding="utf-8").read()
    # ⚠️ 必须定位**调用处**（`_cancel_probe(...)`）而不是**定义处**
    #   （`def _cancel_probe` 里没有 break）—— 首版用 `find("is_cancel_requested")`
    #   命中的是函数体/定义，⇒ W1b 误判红。判红的是判据，不是代码。
    i = src.find("if _cancel_probe(")
    check("W1 子任务产出循环里有协作式中止探针", i >= 0)
    seg = src[i:i + 320] if i >= 0 else ""
    check("W1b 探针命中即 break（真收敛，不是只记录）", "break" in seg,
          "调用处 320 字符内未见 break")

    j = src.find("def _cancel_probe")
    seg2 = src[j:j + 1000] if j >= 0 else ""
    check("W2 探针用**独立连接**（不碰可能持事务的主连接）",
          "get_db()" in seg2 and "is_cancel_requested" in seg2,
          "未见独立连接取库")
    check("W3 探针被 try 包住（跑在热路径上，抛异常=新崩溃源）",
          "except Exception" in seg2)

    # AST：确认探针调用在子任务 for 循环体内（不是循环外只查一次）
    tree = ast.parse(src)
    found_in_loop = False
    for n in ast.walk(tree):
        if isinstance(n, ast.For):
            if "_cancel_probe" in ast.dump(n):
                found_in_loop = True
                break
    check("W4 探针在**产出循环体内**（每个产出查一次，不是只查一次）",
          found_in_loop)


def main() -> int:
    print("=" * 72)
    print("判据 · #19 取消链路 + 卡住自动收敛（健壮性）")
    try:
        c_group()
        r_group()
        w_group()
    except Exception as e:                                       # noqa: BLE001
        check("整段执行", False, "异常：%r" % e)
    npass = sum(1 for st, _, _ in _results if st == PASS)
    nfail = sum(1 for st, _, _ in _results if st == FAIL)
    print("\n" + "=" * 72)
    print("%d pass / %d fail" % (npass, nfail))
    if nfail:
        for st, n, dd in _results:
            if st == FAIL:
                print("  - %s%s" % (n, ("　— " + dd) if dd else ""))
        return 1
    print("✅ 取消链路可用、卡住的 run 能自动收敛、慢的不误杀")
    return 0


if __name__ == "__main__":
    sys.exit(main())
