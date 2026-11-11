# -*- coding: utf-8 -*-
"""verify_run_scope_criteria —— run 维度下沉到**判据层**（2026-11-10）。

## 为什么要有这个门禁
真库当时的形态是「原料齐、判据没有」：
  · `orch_run_spans` 6,970 条 / 42 run（含 parent_id 的 span 树）
  · `agent_tasks` / `orch_checkpoints` / `tool_call_logs` 均有 run_id（后者 100% 填充）
  —— 但**三处判据面**拿不到批次坐标：
    ① `core/request_invariant` 的事件只有 mode/allowed/reasons/digest；
    ② `agent/context_audit` 只有 trace_id + conversation_id；
    ③ `agent/todo_store::machine_evidence` 只按会话查回执。

其中 ③ 不只是"观测缺口"，而是**判据错误**：
  同一会话第二轮没有真调 `sysml_v2_validate`，但第一轮调过；
  `ORDER BY id DESC LIMIT 1` 会捞到第一轮那条 ⇒ 判成 `machine`（假回执）。
  这一类错**夹具单批次时永远绿**，只在多批次真数据上显形。

## 判据
### A 组 · 请求不变量（ticket meta + 事件）
  A1 `ticket_scope` 从 meta 取 run_id/trace_id/conversation_id/sub_task_key
  A2 meta 缺失/损坏 ⇒ 全零，**不猜不补**（宁可缺，不可编）
  A3 `assert_before_dispatch` 的事件里带 run_id / trace_id
  A4 `LLMClient.chat` 冻结时把 trace 上下文写进 meta（源码级：唯一入口接线）

### B 组 · 上下文归因
  B1 块级 + 聚合两张表都落 run_id
  B2 **同一 trace 的两个 run 各自成行、不合并**（缓冲键含 run_id）
  B3 老库（无 run_id 列）⇒ 降级写旧列，**不是一条不落**

### C 组 · 机器回执（判据本身）
  C1 本批次有调用 ⇒ machine
  C2 ★**别的批次有调用、本批次没有 ⇒ claim**（下沉前这里恒 machine）
  C3 `run_id=0`（单 Agent 直行无批次）⇒ 维持旧行为，不退化成全员 claim
  C4 `todo_write` 落 `agent_todos.run_id`（此前恒 0）

### M 组 · 变异自证
  M1 把 machine_evidence 的 run 过滤去掉 ⇒ C2 必须翻（否则判据空转）
  M2 把 run_id 从归因缓冲键里去掉 ⇒ B2 必须翻

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_run_scope_criteria.py
"""
from __future__ import annotations
from _tmpdir import mkdtemp          # 共享：创建即登记，atexit 自动回收

import json
import os
import sqlite3
import sys
import textwrap
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PROD_DB = (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db"))

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:260])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + detail) if detail and not ok else ""))
    return bool(ok)


# ── 夹具 ────────────────────────────────────────────────────────────────────
def _db(tag, with_run_cols=True):
    """最小夹具库。**不 copy 生产库**（282 MB × N 太慢），改为按被测 SQL 建最小表 +
    对生产库做列契约断言（见 C0）——两条一起才既不慢又不漂。"""
    d = mkdtemp(prefix="runscope_%s_" % tag)
    p = os.path.join(d, "t.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE tool_call_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER DEFAULT 0,
        run_id INTEGER DEFAULT 0,
        tool_name TEXT DEFAULT '',
        ok INTEGER DEFAULT 1,
        call_kind TEXT DEFAULT 'tool',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    if with_run_cols:
        c.execute("""CREATE TABLE context_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
            run_id INTEGER DEFAULT 0, block_name TEXT DEFAULT '',
            layer TEXT DEFAULT 'dynamic', tokens INTEGER DEFAULT 0,
            chars INTEGER DEFAULT 0, phase TEXT DEFAULT 'system')""")
        c.execute("""CREATE TABLE context_audit_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
            run_id INTEGER DEFAULT 0, phase TEXT DEFAULT 'system',
            total_tokens INTEGER DEFAULT 0, total_chars INTEGER DEFAULT 0,
            block_count INTEGER DEFAULT 0, metrics TEXT DEFAULT '{}',
            sub_task_key TEXT DEFAULT '')""")
    else:
        # 老库形态（下沉前的两表）
        c.execute("""CREATE TABLE context_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
            block_name TEXT DEFAULT '', layer TEXT DEFAULT 'dynamic',
            tokens INTEGER DEFAULT 0, chars INTEGER DEFAULT 0,
            phase TEXT DEFAULT 'system')""")
        c.execute("""CREATE TABLE context_audit_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
            phase TEXT DEFAULT 'system', total_tokens INTEGER DEFAULT 0,
            total_chars INTEGER DEFAULT 0, block_count INTEGER DEFAULT 0,
            metrics TEXT DEFAULT '{}')""")
    c.execute("""CREATE TABLE agent_todos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER DEFAULT 0, run_id INTEGER DEFAULT 0,
        seq INTEGER DEFAULT 1, subject TEXT DEFAULT '', status TEXT DEFAULT 'pending',
        verify TEXT DEFAULT '', verify_tool TEXT DEFAULT '', verify_args TEXT DEFAULT '{}',
        verify_result TEXT DEFAULT '', blocked_by TEXT DEFAULT '', note TEXT DEFAULT '',
        evidence TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')""")
    c.commit()
    c.close()
    return p, d


def _open(path):
    """flush() 会 close 它拿到的连接（生产行为），所以每次读写都开新连接。"""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _prod_cols(table):
    """生产库该表的列名集合（只读）。拿不到就返回空集（不阻断，C0 会因此报红）。"""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
        try:
            return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}
        finally:
            c.close()
    except Exception:
        return set()


# ── A 组 ────────────────────────────────────────────────────────────────────
def a_group():
    print("\n=== A 组 · 请求不变量 ===")
    from core import request_invariant as ri

    t = ri.freeze_request([{"role": "user", "content": "hi"}], None,
                          {"intent": "design", "run_id": 546, "trace_id": "tr-9",
                           "conversation_id": 9628, "sub_task_key": "t2"})
    sc = ri.ticket_scope(t)
    check("A1 ticket_scope 取到批次坐标系",
          sc == {"run_id": 546, "trace_id": "tr-9", "conversation_id": 9628,
                 "sub_task_key": "t2"}, sc)

    t2 = ri.freeze_request([{"role": "user", "content": "hi"}], None, {"intent": "chat"})
    check("A2 meta 无批次字段 ⇒ 全零（不猜不补）",
          ri.ticket_scope(t2) == {"run_id": 0, "trace_id": "", "conversation_id": 0,
                                  "sub_task_key": ""},
          ri.ticket_scope(t2))
    check("A2b meta 损坏/为 None ⇒ 全零而非异常",
          ri.ticket_scope(None)["run_id"] == 0
          and ri.ticket_scope(ri.RequestTicket("d", "[]", "[]", "not-json", 0))["run_id"] == 0)

    # A3：拦截 _record 看事件
    seen = []
    _orig = ri._record
    ri._record = lambda ev: seen.append(dict(ev))
    try:
        ri.assert_before_dispatch(t, [{"role": "user", "content": "hi"}], None, mode="shadow")
    finally:
        ri._record = _orig
    ev = seen[-1] if seen else {}
    check("A3 不变量事件带 run_id / trace_id",
          ev.get("run_id") == 546 and ev.get("trace_id") == "tr-9",
          {k: ev.get(k) for k in ("run_id", "trace_id")})

    # A4：唯一入口接线（源码级 —— 三条调用形态共用 chat，接这里才不漏）
    p = os.path.join(_ROOT, "llm", "__init__.py")
    src = open(p, encoding="utf-8").read()
    seg = src[src.find("_ri.freeze_request"):]
    seg = seg[:seg.find("})") + 2] if seg else ""
    check("A4 LLMClient.chat 冻结时写入 run_id（唯一入口接线）",
          '"_ri.freeze_request"' not in seg and '"run_id"' in seg
          and "trace_context" in src[src.find("def chat("):src.find("def chat(") + 9000],
          seg[:160])


# ── B 组 ────────────────────────────────────────────────────────────────────
def b_group():
    print("\n=== B 组 · 上下文归因 ===")
    import core.audit as audit_mod
    import database as db_mod
    from agent import context_audit as ca

    # B1/B2：同一 trace、两个 run
    path, d = _db("b")
    _orig_tc = audit_mod.trace_context
    _orig_get = db_mod.get_db
    try:
        cur_ctx = {"trace_id": "tr-x", "conversation_id": 77, "run_id": 0,
                   "sub_task_key": ""}
        audit_mod.trace_context = lambda: dict(cur_ctx)
        db_mod.get_db = lambda: _open(path)      # flush 会关连接 ⇒ 每次给新的

        cur_ctx["run_id"] = 601
        ca._BUF.clear()
        ca.record_blocks({"role": "A" * 40, "memory": "B" * 40}, phase="system")
        ca.flush()
        cur_ctx["run_id"] = 602
        ca.record_blocks({"role": "C" * 80}, phase="system")
        ca.flush()

        _c = _open(path)
        try:
            rows = [dict(r) for r in _c.execute(
                "SELECT run_id, total_tokens, block_count FROM context_audit_summary "
                "ORDER BY id").fetchall()]
            br = [dict(r) for r in _c.execute(
                "SELECT run_id, block_name FROM context_audit ORDER BY id").fetchall()]
        finally:
            _c.close()
        check("B1 块级 + 聚合两表都落 run_id",
              {r["run_id"] for r in br} == {601, 602}
              and {r["run_id"] for r in rows} == {601, 602},
              {"chunks": br, "summary": rows})
        check("B2 同一 trace 的两个 run 各自成行（缓冲键含 run_id，不合并）",
              len(rows) == 2 and [r["block_count"] for r in rows] == [2, 1],
              rows)
        check("B2b 两个 run 的 token 各自独立（不是相加）",
              rows[0]["total_tokens"] != rows[1]["total_tokens"]
              if len(rows) == 2 and rows[0]["total_tokens"] and rows[1]["total_tokens"] else False,
              rows)
    finally:
        audit_mod.trace_context = _orig_tc
        db_mod.get_db = _orig_get

    # B3：老库降级（无 run_id 列）—— 必须仍然写进去
    path2, d2 = _db("b_old", with_run_cols=False)
    try:
        audit_mod.trace_context = lambda: {"trace_id": "tr-old", "conversation_id": 78,
                                           "run_id": 603, "sub_task_key": ""}
        db_mod.get_db = lambda: _open(path2)
        ca._BUF.clear()
        ca.record_blocks({"role": "Z" * 40}, phase="system")
        n = ca.flush()
        _c2 = _open(path2)
        try:
            got = _c2.execute("SELECT COUNT(*) FROM context_audit").fetchone()[0]
        finally:
            _c2.close()
        check("B3 老库（无 run_id 列）降级写旧列，不是一条不落", n == 1 and got == 1,
              "flush_ret=%s rows=%s" % (n, got))
    finally:
        audit_mod.trace_context = _orig_tc
        db_mod.get_db = _orig_get


# ── C 组 ────────────────────────────────────────────────────────────────────
def c_group():
    print("\n=== C 组 · 机器回执（判据本身）===")
    from agent import todo_store as ts

    cols = _prod_cols("tool_call_logs")
    check("C0 生产库 tool_call_logs 确有 run_id 列（夹具前提）",
          "run_id" in cols, sorted(cols)[:12])

    path, d = _db("c")
    conn = _open(path)
    try:
        C = 1
        # run 601 真调过校验工具；run 602 没有
        conn.execute("INSERT INTO tool_call_logs (conversation_id, run_id, tool_name, ok, "
                     "call_kind) VALUES (?,?,?,?,?)", (C, 601, "sysml_v2_validate", 1, "tool"))
        conn.execute("INSERT INTO tool_call_logs (conversation_id, run_id, tool_name, ok, "
                     "call_kind) VALUES (?,?,?,?,?)", (C, 601, "sysml_v2_validate", 0, "tool"))
        conn.commit()

        e1 = ts.machine_evidence(conn, C, "sysml_v2_validate", run_id=601)
        check("C1 本批次有成功调用 ⇒ machine", e1.get("kind") == "machine", e1)

        e2 = ts.machine_evidence(conn, C, "sysml_v2_validate", run_id=602)
        check("C2 ★别的批次有调用、本批次没有 ⇒ claim（下沉前恒 machine）",
              e2.get("kind") == "claim", e2)

        e3 = ts.machine_evidence(conn, C, "sysml_v2_validate")
        check("C3 run_id=0（单 Agent 直行）⇒ 维持旧行为，不退化成全员 claim",
              e3.get("kind") == "machine" and int(e3.get("run_id") or 0) == 0, e3)

        # C4：todo_write 落 run_id
        r = ts.handle_todo_write({"action": "create", "subject": "校验模型",
                                  "verify": "调用校验工具通过",
                                  "verify_tool": "sysml_v2_validate"},
                                 C, conn=conn, run_id=602, task_key="t2")
        row = conn.execute("SELECT run_id FROM agent_todos WHERE conversation_id=? "
                           "ORDER BY id DESC LIMIT 1", (C,)).fetchone()
        check("C4 todo_write 落 agent_todos.run_id（此前恒 0）",
              row is not None and int(row[0]) == 602, {"ret": r, "row": row and row[0]})
    finally:
        conn.close()


# ── M 组 · 变异自证 ─────────────────────────────────────────────────────────
def m_group():
    print("\n=== M 组 · 变异自证（判据必须有牙齿）===")
    import inspect
    import textwrap as _tw
    from agent import todo_store as ts

    # M1：把 run 过滤去掉（等价下沉前的实现）
    src = _tw.dedent(inspect.getsource(ts.machine_evidence))
    anchor = "if _rid > 0:"
    idx = src.find(anchor)
    _IN_MUT[0] = True
    if idx < 0:
        check("M1 锚点存在（run 分支）", False, "anchor not found")
    else:
        mutated = src.replace("if _rid > 0:", "if False:", 1)
        ns = dict(vars(ts))
        exec(compile(mutated, "<mutant_m1>", "exec"), ns)
        path_m, d = _db("m1")
        conn = _open(path_m)
        try:
            conn.execute("INSERT INTO tool_call_logs (conversation_id, run_id, tool_name, ok,"
                         " call_kind) VALUES (1,701,'sysml_v2_validate',1,'tool')")
            conn.commit()
            got = ns["machine_evidence"](conn, 1, "sysml_v2_validate", run_id=702)
            check("M1 去掉 run 过滤 ⇒ 别的批次回执被当成 machine（C2 翻红）",
                  got.get("kind") == "machine", got)
        finally:
            conn.close()
    _IN_MUT[0] = False

    # M2：缓冲键去掉 run_id ⇒ B2 的"两 run 各自成行"退化成一 行相加
    p = os.path.join(_ROOT, "agent", "context_audit.py")
    text = open(p, encoding="utf-8").read()
    _IN_MUT[0] = True
    check("M2 缓冲键按 run_id 分离（源码级：key 含 run_id）",
          "key = (trace_id, conv_id, run_id, phase)" in text,
          "缓冲键里没有 run_id ⇒ 两个 run 会相加")
    _IN_MUT[0] = False


def main():
    t0 = time.time()
    print("verify_run_scope_criteria —— run 维度下沉（判据层三处）")
    a_group()
    b_group()
    c_group()
    m_group()
    n_pass = sum(1 for r in _results if r[0] == PASS)
    n_mut = sum(1 for r in _MUT_ROWS if r[0] == PASS)
    n_fail = sum(1 for r in _results + _MUT_ROWS if r[0] == FAIL)
    print("\n结果：%d PASS / %d FAIL　（其中变异自证 %d 条）　%.1fs"
          % (n_pass, n_fail, n_mut, time.time() - t0))
    if n_fail:
        for st, name, detail in _results + _MUT_ROWS:
            if st == FAIL:
                print("  ✗ %s　%s" % (name, detail))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
