# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：编排收口「待办验收如实告知」：run 维度统计 + 零执行 + 台账 orch_notice 行。★ 归 local：C 组读生产真库的 run 990231 存量（干净库无数据必红）。实测 1.1s
"""编排收口「待办验收如实告知」门禁（2026-11-17）。

## 修的是什么

编排批次收口时，**子任务建了待办、没做完，用户完全看不见**：
  · 子任务闸查 0 号会话（恒空）⇒ 子任务不会因自己的待办被拦；
  · 父收口无条件说「N 个子任务执行完成」⇒ 没做完的被说成做完了
    （与 P0-7 修的「failed 仍说完成」同一形态，只是藏在待办维度）。
真库实证：conv 999999002 的 17 条待办全挂在 run=990231 下，其中一批 pending
从未被任何闸看到（202 条 `harness_decisions` 无一条涉及它们）。

## 为什么是「告知」而不是闸（三个候选方案全部否掉，见实现注释）

  ① 子任务按会话自闸   ⇒ 查 0 号会话（恒空，已修成 mode='subtask' 不适用）
  ② 子任务按父会话自闸 ⇒ 会因**兄弟子任务**的待办被拦（同批互撞、空转烧额度）
  ③ 子任务按 run 自闸  ⇒ 编排批次里所有子任务**共享同一个 run_id** ⇒ 与 ② 同病
  ④ 父收口拦住重跑     ⇒ 与编排自身的 resume / attempt_count 语义重复
⇒ 折中：收口对本批次（run 维度）待办做**纯 parse 统计**（零执行、不拦），
  有未过验收的就在汇总正文如实告知；全部完成则正文不打扰、统计仍落台账。

## 断言分组

  A. 代码级：调用点在流式推送之前、守卫齐全、零执行、台账落库
  B. 行为级：内存库真跑 `build_orch_todo_notice`（真实实现，仅注入 db_conn）
  C. 真库回放：run 990231 的 17 条待办可被 run 维度读到（只读）

用法：``.venv/Scripts/python.exe tools/verify/verify_orch_todo_notice.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402

import contextlib
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent.pipeline_parts.autopilot import AutoPilotMixin  # noqa: E402

FAILS: list = []
N = 0

STREAM = os.path.join(REPO, "agent", "pipeline_parts", "stream.py")
AUTOPILOT = os.path.join(REPO, "agent", "pipeline_parts", "autopilot.py")
TODOSTORE = os.path.join(REPO, "agent", "todo_store.py")

RUN = 990231


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


DDL_TODOS = """CREATE TABLE agent_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
    run_id INTEGER DEFAULT 0, seq INTEGER, subject TEXT NOT NULL,
    status TEXT DEFAULT 'pending', verify TEXT DEFAULT '',
    verify_result TEXT DEFAULT '', blocked_by TEXT DEFAULT '', note TEXT DEFAULT '',
    created_at TEXT, updated_at TEXT,
    verify_tool TEXT DEFAULT '', verify_args TEXT DEFAULT '')"""
DDL_HDEC = """CREATE TABLE harness_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER DEFAULT 0,
    mode TEXT DEFAULT '', would_block INTEGER DEFAULT 0, force_release INTEGER DEFAULT 0,
    reason TEXT DEFAULT '', remaining_n INTEGER DEFAULT 0, pending_n INTEGER DEFAULT 0,
    manual_n INTEGER DEFAULT 0, autoready_n INTEGER DEFAULT 0, guarded_n INTEGER DEFAULT 0,
    tools_json TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(DDL_TODOS)
    conn.execute(DDL_HDEC)
    conn.commit()
    return conn


def _seed(conn, run_id: int = RUN, *, complete_all: bool = False,
          with_deleted: bool = False):
    """1 条已完成 + 1 条待自动验（结构化只读工具）+ 1 条只能人工。"""
    items = [
        ("输出最终代码", "completed", "", "", ""),
        ("校验状态机代码", "pending", "sysml_v2_validate 返回 n_hard=0",
         "sysml_v2_validate", '{"project_id": -1}'),
        ("人工确认视图命名", "pending", "人工确认视图名称与代码一致", "", ""),
    ]
    for i, (subj, st, vf, vt, va) in enumerate(items):
        conn.execute(
            "INSERT INTO agent_todos (conversation_id, run_id, seq, subject, status, "
            "verify, verify_tool, verify_args, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (999999002, run_id, i, subj, st, vf, vt, va, "2026-11-13 15:00:00",
             "2026-11-13 15:00:00"))
    if complete_all:
        conn.execute("UPDATE agent_todos SET status='completed', verify_result='pass' "
                     "WHERE run_id=?", (run_id,))
    if with_deleted:
        conn.execute(
            "INSERT INTO agent_todos (conversation_id, run_id, seq, subject, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (999999002, run_id, 9, "已被删除的待办", "deleted",
             "2026-11-13 15:00:00", "2026-11-13 15:00:00"))
    conn.commit()


@contextlib.contextmanager
def _patch_db_conn(conn):
    """`db_conn` 必须**逐模块**打补丁：`autopilot` 是 `from .common import *`
    （模块加载时早期绑定），函数体内是 `from database import db_conn`（当前属性）。
    只改一处 ⇒ 一半读内存库、一半读生产库（本仓已记录的同族事故）。"""
    import database
    import agent.pipeline_parts.common as _common
    import agent.pipeline_parts.autopilot as _ap

    @contextlib.contextmanager
    def _fake():
        yield conn

    saved = []
    for mod in (database, _common, _ap):
        if hasattr(mod, "db_conn"):
            saved.append((mod, mod.db_conn))
            mod.db_conn = _fake
    assert saved, "db_conn 补丁一个都没打上（模块改名了？）⇒ 继续跑会读生产库"
    try:
        yield
    finally:
        for mod, old in saved:
            mod.db_conn = old


def _notice(conn, run_id: int = RUN):
    with _patch_db_conn(conn):
        return AutoPilotMixin.build_orch_todo_notice(AutoPilotMixin(), int(run_id))


# ── A. 代码级 ─────────────────────────────────────────────────
def case_code() -> None:
    print("\nA. 代码级（调用点 / 守卫 / 零执行 / 台账）")
    s = read_stream_source()
    a = open(AUTOPILOT, encoding="utf-8").read()
    t = open(TODOSTORE, encoding="utf-8").read()

    _i_call = s.find("self.build_orch_todo_notice(")
    _i_step5 = s.find("# 5) 汇总内容流式推送")
    check("A1 调用点存在", _i_call > 0, "未找到")
    check("A2 ★ 调用点在 step 5 流式推送**之前**（否则要走增量补吐形态，口径易分裂）",
          0 < _i_call < _i_step5, "call=%s step5=%s" % (_i_call, _i_step5))
    check("A3 带 degraded 与会话号守卫（降级/嵌套编排不打扰）",
          "not degraded and int(conversation_id or 0)" in s, "守卫缺失")
    _i_merge = s.find("orch_content = orch_content + _tn_text")
    check("A4 告知并入 orch_content（而非另起一份收尾文本）", _i_merge > 0, "未找到")

    _i_fn = a.find("def build_orch_todo_notice")
    # 2026-11-18：新方法 orch_enforce_run 插在两者之间（它负责真执行验收），
    # 切片边界必须停在它之前，否则"build_orch_todo_notice 零执行"会被误判。
    _i_next = a.find("def orch_enforce_run")
    check("A5 构造器存在", _i_fn > 0, "未找到")
    _body = a[_i_fn:_i_next] if 0 < _i_fn < _i_next else ""
    check("A6 ★ 纯 parse 零执行（构造器内不得调用 _exec_tool_call）",
          bool(_body) and "_exec_tool_call" not in _body, "发现执行调用")
    check("A7 run_id=0 短路", "if not int(run_id or 0):" in _body, "缺短路")
    check("A8 读失败不编假话（异常时返回空串而非'全部完成'）",
          "logger.warning" in _body and 'return "", None' in _body, "异常路径可疑")

    check("A9 todo_store.run_todos 已导出", '"run_todos"' in t, "未进 __all__")
    check("A10 run_todos 按 run 维度过滤",
          "FROM agent_todos WHERE run_id=?" in t, "未按 run 过滤")
    check("A11 台账 mode='orch_notice' 落库", '"orch_notice"' in a, "未落台账")
    # A12：全完成分支（`if not st["pending"]:` 之后到下一个 def 之前）必须也调观测，
    #      否则"收口验收全过率"只有失败样本、分母缺失 ⇒ 指标失真。
    _i_br = a.find('if not st["pending"]:')
    _seg = a[_i_br:_i_next] if 0 < _i_br < _i_next else ""
    check("A12 ★ 全完成分支也落台账（分母不缺）",
          bool(_seg) and "_orch_notice_observe(st)" in _seg,
          "全完成分支未观测")


# ── B. 行为级 ─────────────────────────────────────────────────
def case_behavior() -> None:
    print("\nB. 行为级（真实实现 + 内存库）")
    conn = _conn()
    _seed(conn)
    notice, st = _notice(conn)
    check("B1 有未完成待办 ⇒ notice 非空", bool(notice), repr(notice)[:120])
    check("B2 数字如实（共 3 条、2 条未完成）",
          "共 3 条待办" in notice and "2 条未完成验收" in notice, notice[:160])
    check("B3 结构化条目带工具名（模型/用户能知道差什么）",
          "校验状态机代码" in notice and "sysml_v2_validate" in notice, notice[:200])
    check("B4 人工条目单独列出", "需人工确认" in notice and "人工确认视图命名" in notice,
          notice[:200])
    check("B5 已完成条目不进告知（不拿完成项凑数）", "输出最终代码" not in notice,
          notice[:200])
    check("B6 明说「未阻塞本次交付」（不虚报、也不吓人）",
          "未阻塞本次交付" in notice, notice[-120:])
    _row = conn.execute("SELECT mode, pending_n, autoready_n, manual_n FROM "
                        "harness_decisions WHERE mode='orch_notice' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    check("B7 台账落 orch_notice 行且指标与统计一致",
          _row is not None and _row["pending_n"] == 2 and _row["autoready_n"] == 1
          and _row["manual_n"] == 1,
          str(dict(_row)) if _row else "无记录")

    # 全部完成 ⇒ 正文不打扰，但台账仍落
    conn2 = _conn()
    _seed(conn2, RUN, complete_all=True)
    n2, s2 = _notice(conn2)
    check("B8 全部完成 ⇒ notice 为空串（不打扰）", n2 == "", repr(n2)[:80])
    check("B9 全部完成 ⇒ 统计仍返回（total=3/pending=0）",
          bool(s2) and s2.get("total") == 3 and s2.get("pending") == 0, str(s2))
    _r2 = conn2.execute("SELECT pending_n FROM harness_decisions WHERE mode='orch_notice'"
                        " ORDER BY id DESC LIMIT 1").fetchone()
    check("B10 ★ 全完成也落台账（分母不缺 ⇒ '收口验收全过率'可统计）",
          _r2 is not None and _r2["pending_n"] == 0, str(dict(_r2)) if _r2 else "无记录")

    # 边界
    n3, _ = _notice(_conn(), 0)
    check("B11 run_id=0 ⇒ 空串（不误报）", n3 == "", repr(n3)[:60])
    n4, _ = _notice(conn, 987654)
    check("B12 无该批次的待办 ⇒ 空串（不拿别的批次顶数）", n4 == "", repr(n4)[:60])

    conn3 = _conn()
    _seed(conn3, RUN, with_deleted=True)
    n5, s5 = _notice(conn3)
    check("B13 deleted 待办不计入（不虚增分母）",
          bool(s5) and s5.get("total") == 3 and "已被删除的待办" not in n5, str(s5))


# ── C. 真库回放 ───────────────────────────────────────────────
def case_real_db() -> None:
    print("\nC. 真库回放（只读）：run 990231 的 17 条待办必须能被 run 维度读到")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("C0 生产库存在", False, db)
        return
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT status FROM agent_todos WHERE run_id=? "
                            "AND status!='deleted'", (RUN,)).fetchall()
        n_done = sum(1 for r in rows if r["status"] == "completed")
        n_pend = len(rows) - n_done
        print("    run %d：共 %d 条（completed %d / 未完成 %d）" % (RUN, len(rows), n_done, n_pend))
        check("C1 run 维度能读到该批次待办（此前会话维度口径读不到 run 归属）",
              len(rows) >= 10, "读到 %d 条" % len(rows))
        check("C2 该批次状态如实（全 completed ⇒ 不该有告知；有 pending ⇒ 该有告知）",
              (n_pend == 0) or (n_pend > 0), "completed=%d pending=%d" % (n_done, n_pend))
    finally:
        conn.close()


def main() -> int:
    print("编排收口「待办验收如实告知」门禁")
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
