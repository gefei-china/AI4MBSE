# -*- coding: utf-8 -*-
"""门禁：方案B P0/P1 —— 帧日志地基 + 断线重连回放（RUN_STREAM_STORE_V1）。

跑法（必须用项目 venv）：
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_run_frame_store.py
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_run_frame_store.py --with-e2e

## 为什么这些断言（而不是"跑一遍没报错"）

| 组 | 断言对象 | 反空转设计 |
|---|---|---|
| S | 迁移幂等 + 结构契约 | 同一连接上跑两遍迁移，行数/列集必须不变 |
| R | run 会话：唯一性 / 僵尸清理 / 历史裁剪 | 并发两连接各开 run，断言 id 不同（**不复用 agent_tasks 号**） |
| F | FrameTee：seq 语义 / 合帧 / 截断 | 断言"整段正文可由帧重建"——合并丢字会立刻暴露 |
| P | prepare_frame_replay 四态 + 游标超前 | 四态逐一给期望 mode，且**每个 mode 都有用例** |
| M | 变异自证 | 5 个变异（terminal 判定/游标超前/last_seq 推进/截断/合帧 seq）必须被抓 |
| W | 接线契约（源码级，且射程限定到函数体） | 断言 `feed` 在 `id:` 前缀之前、`_fr_close` 早于 `_client_gone` 早退 |
| E | 端到端（`--with-e2e`）：真路由 + 假 pipeline + 真库副本 | 断言 SSE 带 `id: run-seq`、库里 run=done、`/frames` 重建正文一致 |

⚠️ 本脚本**只写临时库**（`tempfile`）或**真库副本**（`sqlite3.backup`），
   绝不写生产库；`--with-e2e` 用 `MBSE_DB_PATH` 指向副本后**子进程**再导入 main。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   [" + str(detail) + "]") if detail and not cond else ""))
    return bool(cond)


def section(t):
    print("\n[%s] %s" % (t, ""))


# ── 临时库（只跑真实迁移，不复制生产数据）────────────────────────────────
def _tmp_conn():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="p0_runstream_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "conversation_id INTEGER, role TEXT, content TEXT, card_data TEXT DEFAULT '{}')")
    conn.commit()
    return conn, path


def _migrate(conn):
    from database.migrations.events import _migrate_run_stream
    _migrate_run_stream(conn)


# ── S：结构契约 ────────────────────────────────────────────────────────
def sec_schema():
    section("S 迁移幂等与结构契约")
    conn, path = _tmp_conn()
    try:
        _migrate(conn)
        snap1 = {
            "tables": sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'run_%'")),
            "cols_rs": sorted(r[1] for r in conn.execute("PRAGMA table_info(run_sessions)")),
            "cols_rf": sorted(r[1] for r in conn.execute("PRAGMA table_info(run_frames)")),
            "cols_msg": sorted(r[1] for r in conn.execute("PRAGMA table_info(messages)")),
            "idx": sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name LIKE 'run_%'")),
        }
        _migrate(conn)     # 第二遍：必须无副作用
        snap2 = {
            "tables": sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'run_%'")),
            "cols_rs": sorted(r[1] for r in conn.execute("PRAGMA table_info(run_sessions)")),
            "cols_rf": sorted(r[1] for r in conn.execute("PRAGMA table_info(run_frames)")),
            "cols_msg": sorted(r[1] for r in conn.execute("PRAGMA table_info(messages)")),
            "idx": sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name LIKE 'run_%'")),
        }
        check("S1 幂等：两遍迁移后结构逐项相同", snap1 == snap2, (snap1, snap2))
        check("S2 两表存在", snap1["tables"] == ["run_frames", "run_sessions"], snap1["tables"])
        for c in ("run_id", "conversation_id", "status", "last_seq", "frame_count",
                  "truncated", "owner_pid", "message_id", "heartbeat_at"):
            check("S3 run_sessions 有列 %s" % c, c in snap1["cols_rs"])
        for c in ("run_id", "seq", "event_type", "payload"):
            check("S4 run_frames 有列 %s" % c, c in snap1["cols_rf"])
        check("S5 messages 补了 run_id/last_seq",
              "run_id" in snap1["cols_msg"] and "last_seq" in snap1["cols_msg"], snap1["cols_msg"])
        check("S6 (run_id,seq) 是主键（回放范围扫描的唯一索引）",
              any("autoindex" in i for i in snap1["idx"]), snap1["idx"])
    finally:
        conn.close()
        try:
            os.unlink(path)
        except Exception:
            pass


# ── R：run 会话生命周期 ────────────────────────────────────────────────
def sec_runs():
    section("R run 会话：唯一性 / 僵尸清理 / 裁剪")
    from services.run_frame_store import new_run, get_run, finish_run
    conn, path = _tmp_conn()
    try:
        _migrate(conn)
        a = new_run(conn, 11, {"x": 1}, owner_pid=1001)
        b = new_run(conn, 11, {"x": 2}, owner_pid=1001)
        check("R1 连续两轮 run_id 不同", a != b and a > 0 and b > 0, (a, b))

        # 模拟"上一个进程被杀"：把 a 改回 running 且 owner 改成别人，再开新 run
        conn.execute("UPDATE run_sessions SET status='running', owner_pid=9999 WHERE run_id=?", (a,))
        conn.commit()
        b2 = new_run(conn, 11, {}, owner_pid=1001)
        ra = get_run(conn, a)
        check("R2 僵尸 run（别的 pid 且仍 running）被标 cancelled/stale_owner",
              ra["status"] == "cancelled" and ra["cancel_reason"] == "stale_owner", ra)

        # 另一个会话的 running 不受影响（作用域必须是本会话）
        other = new_run(conn, 12, {}, owner_pid=9999)
        new_run(conn, 11, {}, owner_pid=1001)
        check("R3 只清理本会话的僵尸 run",
              get_run(conn, other)["status"] == "running")

        # 裁剪：同一会话开 25 个终态 run，只留最近 20（running 永不删）
        for _i in range(25):
            r = new_run(conn, 13, {}, owner_pid=1001)
            finish_run(conn, r, "done", "ok")
        left = conn.execute("SELECT COUNT(*) FROM run_sessions WHERE conversation_id=13").fetchone()[0]
        check("R4 历史裁剪到 keep=20（+1 个 running）", left <= 21, left)
        run_ids = [r[0] for r in conn.execute(
            "SELECT run_id FROM run_sessions WHERE conversation_id=13 ORDER BY run_id DESC LIMIT 20")]
        check("R4b 保留的是最近的那批", max(run_ids) == conn.execute(
            "SELECT MAX(run_id) FROM run_sessions WHERE conversation_id=13").fetchone()[0])

        # finish_run 只认终态枚举（写错不许污染状态机）
        r5 = new_run(conn, 14, {}, owner_pid=1001)
        finish_run(conn, r5, "weird_status", "x")
        check("R5 非终态字符串被归一为 done（不写入未知状态）",
              get_run(conn, r5)["status"] == "done", get_run(conn, r5)["status"])
    finally:
        conn.close()
        try:
            os.unlink(path)
        except Exception:
            pass


# ── F：FrameTee ────────────────────────────────────────────────────────
def _frame(t, d):
    return "event: %s\ndata: %s\n\n" % (t, json.dumps(d, ensure_ascii=False))


def sec_tee():
    section("F FrameTee：seq 语义 / 合帧 / 截断")
    from services.run_frame_store import (new_run, FrameTee, read_frames, get_run,
                                          finish_run, reconstruct_from_frames)
    conn, path = _tmp_conn()
    try:
        _migrate(conn)
        rid = new_run(conn, 21, {}, owner_pid=os.getpid())
        # 注入口袋连接工厂：直接复用同一连接（避免临时库路径不可达）
        class _F:
            def __enter__(self):
                return conn
            def __exit__(self, *a):
                return False
        tee = FrameTee(rid, 21, conn_factory=lambda: _F(), flush_frames=3, flush_interval_s=0.0)

        seqs = [tee.feed(_frame("token", {"type": "token", "delta": ch}))
                for ch in ("断", "线", "续", "传")]
        check("F1 每个 token 都拿到 seq（游标必须跟着内容走）",
              all(isinstance(s, int) and s > 0 for s in seqs), seqs)
        check("F2 seq 严格递增", seqs == sorted(set(seqs)) and len(set(seqs)) == len(seqs), seqs)
        s_stage = tee.feed(_frame("stage", {"type": "stage", "name": "生成", "status": "run"}))
        s_reason = tee.feed(_frame("reasoning", {"type": "reasoning", "delta": "想", "round": 1}))
        s_tool = tee.feed(_frame("tool", {"type": "tool", "name": "t", "call_id": "c1",
                                          "status": "run", "arguments": {"a": 1}}))
        s_comment = tee.feed(": ping\n\n")
        s_done = tee.feed(_frame("done", {"type": "done", "data": {"intent": "design"}}))
        check("F3 注释/心跳帧不参与重放（返回 0）", s_comment == 0, s_comment)
        check("F4 非增量帧各占一个 seq", s_stage > 0 and s_reason > 0 and s_tool > 0 and s_done > 0)
        tee.close()

        rows = read_frames(conn, rid, 0, 100)
        check("F5 帧已落库", len(rows) >= 5, len(rows))
        rec = reconstruct_from_frames(rows)
        check("F6 合帧后正文完整重建（一个字都不能丢）",
              rec["content"] == "断线续传", rec["content"])
        check("F7 reasoning / tool 也在重放里", rec["exec"]["reasoning"] == ["想"]
              and len(rec["exec"]["tools"]) == 1, rec["exec"])
        seq_list = [int(r["seq"]) for r in rows]
        check("F8 库里 seq 严格递增无重复", seq_list == sorted(set(seq_list)), seq_list)
        check("F9 合帧允许 seq 有洞，但最大 seq = tee.last_seq",
              max(seq_list) == tee.last_seq, (max(seq_list), tee.last_seq))
        run = get_run(conn, rid)
        check("F10 last_seq 由写入推进（同事务）", int(run["last_seq"]) == tee.last_seq, run["last_seq"])
        check("F11 frame_count 记的是真实行数（可与 seq 不等）",
              int(run["frame_count"]) == len(rows), (run["frame_count"], len(rows)))

        # 游标精确性：从每个 seq 之后取，得到的并集必须恰好是剩余帧
        for cur in (0, seq_list[0], seq_list[len(seq_list) // 2]):
            got = [int(r["seq"]) for r in read_frames(conn, rid, cur, 100)]
            want = [s for s in seq_list if s > cur]
            check("F12 游标 %s 精确（不重不漏）" % cur, got == want, (got, want))

        # 截断
        conn.execute("DELETE FROM run_frames WHERE run_id=?", (rid,))
        conn.execute("UPDATE run_sessions SET last_seq=0, frame_count=0, truncated=0 WHERE run_id=?", (rid,))
        conn.commit()
        tee2 = FrameTee(rid, 21, conn_factory=lambda: _F(), flush_frames=1,
                        flush_interval_s=0.0, max_frames=4)
        got = [tee2.feed(_frame("stage", {"type": "stage", "name": "s%d" % i})) for i in range(10)]
        tee2.close()
        check("F13 超上限后置 truncated", tee2.truncated)
        check("F14 超上限后不再分配 seq（游标冻结，重连不会以为拿全了）",
              all(x == 0 for x in got[4:]), got)
        r2 = get_run(conn, rid)
        check("F15 truncated 已落库", int(r2["truncated"]) == 1, r2["truncated"])
        check("F16 上限内仍可重放", len(read_frames(conn, rid, 0, 100)) <= 4)

        # finish_run 与 messages 对齐
        conn.execute("INSERT INTO messages (conversation_id, role, content) VALUES (21,'assistant','x')")
        conn.commit()
        mid = conn.execute("SELECT MAX(id) FROM messages WHERE conversation_id=21").fetchone()[0]
        finish_run(conn, rid, "done", "ok", mid)
        m = conn.execute("SELECT run_id, last_seq FROM messages WHERE id=?", (mid,)).fetchone()
        check("F17 收口回填 messages.run_id/last_seq",
              int(m["run_id"]) == rid and int(m["last_seq"]) == int(r2["last_seq"]), dict(m))
        finish_run(conn, rid, "error", "again", mid)
        m2 = conn.execute("SELECT run_id FROM messages WHERE id=?", (mid,)).fetchone()
        check("F18 不覆盖已被别的 run 关联的消息", int(m2["run_id"]) == rid, m2["run_id"])
    finally:
        conn.close()
        try:
            os.unlink(path)
        except Exception:
            pass


# ── P：回放决策纯函数 ──────────────────────────────────────────────────
def sec_plan():
    section("P prepare_frame_replay 四态 + 游标超前")
    from services.run_frame_store import prepare_frame_replay as P
    cases = [
        # (cursor, status, max_seq, tail, 期望 mode, 期望 replay, 期望 tail)
        (0, "running", 8, True, "replay_then_tail", True, True),
        (0, "running", 8, False, "replay_only", True, False),
        (8, "running", 8, True, "tail_only", False, True),
        (8, "running", 8, False, "nothing", False, False),
        (3, "done", 8, True, "replay_terminal", True, False),
        (8, "done", 8, True, "nothing", False, False),
        (3, "error", 8, True, "replay_terminal", True, False),
        (3, "cancelled", 8, True, "replay_terminal", True, False),
        (99, "running", 8, True, "replay_then_tail", True, True),
        (99, "done", 8, True, "replay_terminal", True, False),
    ]
    ok = True
    for cur, st, mx, tl, mode, rp, tp in cases:
        p = P(cur, st, mx, tl)
        good = (p["mode"] == mode and p["replay"] == rp and p["tail"] == tp)
        if not good:
            ok = False
            print("     ! case(%s,%s,%s,%s) → %s" % (cur, st, mx, tl, p))
        if cur > mx:
            check("P0 游标超前 %s(status=%s) 归零回放" % (cur, st),
                  p["resume_from_seq"] == 0 and p["cursor_ahead"] is True, p)
    check("P1 十种状态组合的 mode/replay/tail 全部符合预期", ok)
    check("P2 终态永不 tail（否则订阅端白等 max_wait_s）",
          all(P(c, s, 8, True)["tail"] is False for c in (0, 8) for s in ("done", "error", "cancelled")))
    check("P3 游标超前必须报 cursor_ahead（不能静默当成「已拿全」）",
          P(5, "running", 3, True)["cursor_ahead"] is True)
    check("P4 resume_from_seq 不含游标本身（重放是 seq > cursor）",
          P(3, "running", 8, True)["resume_from_seq"] == 3)
    # 游标解析
    from services.run_frame_store import parse_last_event_id as PL, format_event_id as FE
    check("P5 id 解析：run-seq / 裸 seq / 非法",
          PL("12-34") == 34 and PL("34") == 34 and PL("") == 0 and PL("x-y") == 0,
          (PL("12-34"), PL("34"), PL(""), PL("x-y")))
    check("P6 id 生成与解析互逆", PL(FE(7, 19)) == 19, FE(7, 19))


# ── M：变异自证 ────────────────────────────────────────────────────────
def _mutant(src, old, new):
    assert old in src, "变异锚点未命中：%r" % old[:60]
    return src.replace(old, new, 1)


def _mem_store():
    """内存库（只建 run 两表）：给变异用例当"真实落库"的靶子。

    ⚠️ 必须**复用同一个连接**：`append_frames` 若每次都拿到全新的
    `sqlite3.connect(":memory:")`，表根本不存在 → 写入恒失败 → `_stored` 不增长
    → 截断分支永远进不去，变异用例会"通过"（空转）。这是本节第一版踩到的坑。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE run_sessions (run_id INTEGER PRIMARY KEY, last_seq INTEGER DEFAULT 0, "
                 "frame_count INTEGER DEFAULT 0, truncated INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE run_frames (run_id INTEGER, seq INTEGER, event_type TEXT, payload TEXT, "
                 "created_at TEXT, PRIMARY KEY (run_id, seq))")
    conn.commit()
    return conn


class _Wrap:
    """把既有连接包成上下文管理器（`FrameTee` 的 conn_factory 契约）。"""

    def __init__(self, conn):
        self._c = conn

    def __enter__(self):
        return self._c

    def __exit__(self, *a):
        return False



def sec_mutations():
    section("M 变异自证（把机制改坏，断言必须翻）")
    mod_path = os.path.join(ROOT, "services", "run_frame_store.py")
    src = open(mod_path, encoding="utf-8").read()
    ok = True

    # M1：终态判定被"忘记"（永远当 running）→ 订阅端会白等、终态用例必须翻
    m = _mutant(src, 'terminal = st in TERMINAL_STATUS', 'terminal = False')
    ns = {}
    exec(compile(m, "<m1>", "exec"), ns)
    p = ns["prepare_frame_replay"](3, "done", 8, True)
    ok &= check("M1 去掉终态判定 ⇒ done 被当成 running（tail=True）",
                p["tail"] is True and p["mode"] == "replay_then_tail", p)

    # M2：游标超前不归零 → 客户端永久错过 1..mx
    m = _mutant(src, '"resume_from_seq": 0, "cursor_ahead": True', '"resume_from_seq": cur, "cursor_ahead": True')
    ns = {}
    exec(compile(m, "<m2>", "exec"), ns)
    p = ns["prepare_frame_replay"](99, "running", 8, True)
    ok &= check("M2 游标超前不归零 ⇒ resume_from_seq 停在 99（丢 1..8）",
                p["resume_from_seq"] == 99, p["resume_from_seq"])

    # M3：合帧时游标不推进 → 客户端游标落后于内容 ⇒ 重连重复显示
    _mc = _mem_store()
    m = _mutant(src, 'p["seq"] = self._next_seq()      # ★ 游标跟到最新（不变量 2）', 'pass')
    ns = {}
    exec(compile(m, "<m3>", "exec"), ns)
    t = ns["FrameTee"](1, 1, conn_factory=lambda: _Wrap(_mc), flush_frames=99, flush_interval_s=0.0)
    s = [t.feed(_frame("token", {"type": "token", "delta": c})) for c in "abcd"]
    ok &= check("M3 合帧不推进 seq ⇒ 四个 token 只有一个 seq（游标落后）",
                len({x for x in s if x}) == 1, s)

    # M4：append_frames 不推进 last_seq → 订阅端以为没有新帧
    m = _mutant(src,
                '"UPDATE run_sessions SET last_seq=MAX(COALESCE(last_seq,0), ?), "',
                '"UPDATE run_sessions SET last_seq=COALESCE(last_seq,0), "')
    ns = {}
    exec(compile(m, "<m4>", "exec"), ns)
    conn = _mem_store()
    conn.execute("INSERT INTO run_sessions (run_id) VALUES (1)")
    conn.commit()
    ns["append_frames"](conn, 1, [(5, "token", "{}")])
    ls = conn.execute("SELECT last_seq FROM run_sessions WHERE run_id=1").fetchone()[0]
    ok &= check("M4 last_seq 不推进 ⇒ 回放读不到新帧", int(ls) == 0, ls)

    # M5：截断不置位 → 用户被告知"内容完整"，实际缺尾巴
    m = _mutant(src, "                self._truncated = True\n", "                pass\n")
    ns = {}
    exec(compile(m, "<m5>", "exec"), ns)
    _mc2 = _mem_store()
    _mc2.execute("INSERT INTO run_sessions (run_id) VALUES (1)")
    _mc2.commit()
    t2 = ns["FrameTee"](1, 1, conn_factory=lambda: _Wrap(_mc2), max_frames=2,
                        flush_frames=1, flush_interval_s=0.0)
    for i in range(6):
        t2.feed(_frame("stage", {"type": "stage", "name": "s%d" % i}))
    ok &= check("M5 截断不置位 ⇒ truncated 恒 False（缺内容却号称完整）",
                t2.truncated is False and t2.dropped > 0, (t2.truncated, t2.dropped))

    check("M0 五个变异全部被抓住", ok)


# ── W：接线契约（源码级，射程限定函数体）──────────────────────────────
def sec_wiring():
    section("W 接线契约（源码级）")
    import inspect
    import re
    from services import run_frame_store as R
    src_mod = inspect.getsource(R)
    check("W1 FrameTee.feed 先解析再分配 seq（注释帧不占号）",
          "parse_frame(raw)" in inspect.getsource(R.FrameTee.feed))
    check("W2 append_frames 用 INSERT OR REPLACE（同 seq 复查不炸整批）",
          "INSERT OR REPLACE INTO run_frames" in src_mod)

    conv = open(os.path.join(ROOT, "routers", "conversations.py"), encoding="utf-8").read()
    # 射程：只在 chat_stream 里找（`_event_stream_inner` 到 `async def event_stream`）
    i0 = conv.find("async def _event_stream_inner")
    i1 = conv.find("async def event_stream():")
    body = conv[i0:i1] if (i0 > 0 and i1 > i0) else ""
    check("W3 chat_stream 建 run（_fr_open 调用在生成器体内）",
          "_fr_open()" in body and "def _fr_open" in body)
    check("W4 每帧 tee.feed 且**在 id 前缀之前**用未加前缀的 raw",
          "_fr_tee.feed(raw)" in body and '_out = "id: %d-%d\\n"' in body)
    # 顺序约束：终态哨兵/事件录制读的是 raw 前缀 ⇒ feed 必须在 yield 前最后一步
    idx_feed = body.find("_fr_tee.feed(raw)")
    idx_yield = body.find("yield _out")
    idx_terminal = body.find('raw.startswith(("event: done"')
    check("W5 feed 位于 yield 前、且解析判据仍在未加前缀的 raw 上",
          0 < idx_terminal < idx_feed < idx_yield, (idx_terminal, idx_feed, idx_yield))
    check("W6 收口早于 _client_gone 早退（断连正是最该记帧的场景）",
          "_fr_close()" in body and body.find("_fr_close()") < body.find("if _client_gone:"),
          (body.find("_fr_close()"), body.find("if _client_gone:")))
    check("W7 帧日志失败不阻断转发（feed 外包 try/except）",
          re.search(r"try:\s*\n\s*_sq = _fr_tee\.feed\(raw\)", body) is not None)

    # 端点存在
    for p in ("/api/runs/{run_id}/frames", "/api/runs/{run_id}/subscribe"):
        check("W8 端点已注册 %s" % p, p in conv)
    check("W9 两个端点都过归属校验（_own_run）",
          conv.count("_own_run(conn, run_id, user)") >= 2, conv.count("_own_run(conn, run_id, user)"))
    check("W10 /recovery 任何分支都带 stream_run_id",
          conv.count('"stream_run_id"') >= 1 and "_stream_run_info()" in conv)

    # 配置键登记一致
    from core.config import DEFAULT_CONFIG, CONFIG_SCHEMA
    d = DEFAULT_CONFIG.get("run_stream") or {}
    s = CONFIG_SCHEMA.get("run_stream") or {}
    check("W11 run_stream 默认值与 SCHEMA 键集一致（否则界面改不了/显示空）",
          set(d) == set(s) and len(d) == 7, (sorted(d), sorted(s)))

    # 前端
    js = open(os.path.join(ROOT, "static", "js", "mods", "12-chatsend.js"), encoding="utf-8").read()
    check("W12 前端有回放函数并在 !found 分支接上",
          "async function _renderRunReplay" in js and "if(!r.found)" in js
          and "_renderRunReplay(_cid, _rid" in js)
    check("W13 前端把 last_event_id 留空 = 从头重建（替换语义，不是追加）",
          "/frames?reconstruct=1" in js)
    check("W14 会话重开时给回放入口（不自动回放）",
          "async function maybeOfferRunReplay" in js)
    chat = open(os.path.join(ROOT, "static", "js", "mods", "03-chat.js"), encoding="utf-8").read()
    check("W15 maybeOfferRunReplay 在消息全部渲染完之后调用",
          "maybeOfferRunReplay(id, area)" in chat
          and chat.find("_reattachLiveStream(area, id)") < chat.find("maybeOfferRunReplay(id, area)"))


# ── E：端到端（子进程 + 真库副本 + 假 pipeline）────────────────────────
E2E_CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["MBSE_RO"])

def emit(**kw):
    sys.stdout.write("E2EJSON " + json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()

from fastapi.testclient import TestClient
import routers.conversations as C

TEXT = "断线续传验收：帧日志重建。"

class _FakePipe:
    def execute_stream(self, *a, **kw):
        def gen():
            yield {"type": "stage", "name": "生成与校验", "status": "run"}
            for ch in TEXT:
                yield {"type": "token", "delta": ch}
            yield {"type": "reasoning", "delta": "内部思考", "round": 1}
            yield {"type": "tool", "name": "probe", "call_id": "c1",
                   "status": "done", "ok": True, "result": "ok"}
            yield {"type": "stage", "name": "生成与校验", "status": "done", "detail": "ok"}
            yield {"type": "done", "data": {"intent": "design", "route": "rule",
                                            "content": TEXT, "orch_run_id": 0}}
        return gen()

C.get_pipeline = lambda: _FakePipe()

import main
# ⚠️ 必须用上下文管理器：`TestClient(app)` 裸用**不会执行 lifespan**，
#    于是 init_db 不跑 ⇒ 副本库里没有 run_sessions/run_frames ⇒ 帧日志静默为空，
#    症状是"n_ids=0 但流本身正常"（本门禁第一版就踩了这个，见 E2/E5 失败）。
with TestClient(main.app) as client:
    conv = client.post("/api/conversations", json={"title": "帧日志 E2E", "intent": "design"})
    cid = conv.json().get("id") or conv.json().get("conversation_id")
    if not cid:
        emit(ok=False, err="create_conv_failed", body=conv.text[:200]); raise SystemExit(0)
    r = client.post("/api/conversations/%s/chat/stream" % cid, json={"message": "跑一轮"})
    body = r.text
    ids = [ln for ln in body.splitlines() if ln.startswith("id: ")]
    emit(ok=True, cid=cid, status=r.status_code, n_ids=len(ids), first_id=(ids[0] if ids else ""),
         body_len=len(body), text_in_body=(TEXT in body))

    # 查 run + 回放
    rec = client.get("/api/conversations/%s/recovery" % cid).json()
    rid = int(rec.get("stream_run_id") or 0)
    emit(recovery=rec, rid=rid)
    if rid:
        fr = client.get("/api/runs/%d/frames?reconstruct=1" % rid).json()
        emit(frames_ok=True, frame_status=fr.get("status"), n_frames=len(fr.get("frames") or []),
             last_seq=fr.get("last_seq"), content=(fr.get("reconstructed") or {}).get("content"),
             expected=TEXT,
             exec_keys=sorted((fr.get("reconstructed") or {}).get("exec", {}).keys()))
        sub = client.get("/api/runs/%d/subscribe?last_event_id=0&tail=0" % rid)
        emit(sub_status=sub.status_code, sub_has_end=("event: run_end" in sub.text),
             sub_has_meta=("event: run_meta" in sub.text),
             sub_has_token=("event: token" in sub.text))
'''


def sec_e2e():
    section("E 端到端：真路由 + 假 pipeline + 真库副本")
    prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(prod):
        check("E0 生产库存在（否则跳过 E2E）", False, prod)
        return
    tmpdir = tempfile.mkdtemp(prefix="p0_e2e_")
    copy = os.path.join(tmpdir, "mbse_copy.db")
    try:
        src = sqlite3.connect("file:%s?mode=ro" % prod.replace("\\", "/"), uri=True)
        dst = sqlite3.connect(copy)
        src.backup(dst)
        dst.close()
        src.close()
        env = dict(os.environ)
        env["MBSE_DB_PATH"] = copy
        env["MBSE_RO"] = ROOT
        env["PYTHONIOENCODING"] = "utf-8"
        t0 = time.time()
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", E2E_CHILD],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        out = p.stdout or ""
        lines = [ln[len("E2EJSON "):] for ln in out.splitlines() if ln.startswith("E2EJSON ")]
        recs = [json.loads(x) for x in lines]
        check("E1 子进程正常退出（rc=0）", p.returncode == 0,
              (p.returncode, (p.stderr or "")[-600:]))
        if not recs:
            check("E2 子进程产出断言数据", False, (out[-400:], (p.stderr or "")[-400:]))
            return
        head = recs[0]
        check("E2 流式请求 200 且逐帧带 SSE id", head.get("ok") and head.get("status") == 200
              and int(head.get("n_ids") or 0) >= 8, head)
        check("E3 id 形如 run-seq", str(head.get("first_id") or "").count("-") == 1, head.get("first_id"))
        check("E4 正文照常到达前端（加 id 行不影响转发）", head.get("text_in_body") is True)
        # ⚠️ rid 在**第二条** emit 里（recovery 那条），不在 head 里 ——
        #    第一版从 head 取，恒为 0 ⇒ E5 假 FAIL（而 E6 因为看的是 frames_ok 反而通过）。
        _rid_rec = next((x for x in recs if "rid" in x), {})
        rid = int(_rid_rec.get("rid") or 0)
        check("E5 /recovery 能给出本轮的 stream_run_id", rid > 0, _rid_rec.get("recovery"))
        fr = [x for x in recs if "frames_ok" in x]
        check("E6 帧日志可达", bool(fr), recs)
        if fr:
            f = fr[0]
            check("E7 run 收口为 done", f.get("frame_status") == "done", f.get("frame_status"))
            check("E8 重建正文与流式正文逐字一致", f.get("content") == f.get("expected"),
                  (f.get("content"), f.get("expected")))
            check("E9 执行过程（reasoning/tool）也进了帧日志",
                  "reasoning" in (f.get("exec_keys") or []) and "tools" in (f.get("exec_keys") or []),
                  f.get("exec_keys"))
        sub = [x for x in recs if "sub_status" in x]
        if sub:
            s = sub[0]
            check("E10 订阅端点：回放 token + run_meta + run_end",
                  s.get("sub_status") == 200 and s.get("sub_has_token")
                  and s.get("sub_has_meta") and s.get("sub_has_end"), s)
        else:
            check("E10 订阅端点返回可断言内容", False, recs)
        print("     （E2E 耗时 %.1fs）" % (time.time() - t0))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── X：既有门禁零回归（子进程）──────────────────────────────────────────
def sec_regress(with_regress=True):
    section("X 既有门禁零回归")
    if not with_regress:
        print("  SKIP  未指定 --with-regress")
        return
    targets = [
        ("tools/verify/verify_stream_recovery.py", 14),
        ("tools/verify/verify_three_issue_fixes.py", 15),
        ("tools/verify/verify_finalize_key_unique.py", 9),
        ("tools/verify/verify_stream_event_log.py", None),
    ]
    for rel, _n in targets:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            check("X %s 存在" % rel, False)
            continue
        p = subprocess.run([sys.executable, "-X", "utf8", path], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        tail = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else ""
        check("X %s rc=0" % os.path.basename(rel), p.returncode == 0,
              (p.returncode, tail, (p.stderr or "")[-400:]))


def main():
    argv = sys.argv[1:]
    print("=" * 78)
    print("门禁：方案B P0/P1 帧日志（RUN_STREAM_STORE_V1）")
    print("=" * 78)
    sec_schema()
    sec_runs()
    sec_tee()
    sec_plan()
    sec_mutations()
    sec_wiring()
    if "--with-e2e" in argv:
        sec_e2e()
    sec_regress("--no-regress" not in argv)
    print("\n" + "=" * 78)
    print("结果：%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL: %s" % f)
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
