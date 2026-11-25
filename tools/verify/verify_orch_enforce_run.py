# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：编排主路径收口真执行只读验收：enforce/shadow 三态 + 白名单护栏 + 台账分母，16 项内存夹具实测 0.5s；属编排链路专项，登记 local
"""编排主路径收口只读验收门禁（2026-11-18）。

修的是什么：编排的三处入口（resume / 团队 / 常规）都在收口前 return，
永远到不了 ReAct 尾部的 harness 闸 ⇒ 编排会话从未被 enforce 验收过。
原 `build_orch_todo_notice` 只 parse 不执行，只是台账不是闸。
新增 `orch_enforce_run`：enforce 模式下真跑只读白名单验收（上限 8），
失败则在交付正文如实标注 + 落 `harness_decisions(mode='orch_enforce')`。

边界：不另起 ReAct、不改任务状态（重跑归编排 resume 管）；shadow/off 零执行。

用法：``python tools/verify/verify_orch_enforce_run.py``
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
import io
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent.pipeline_parts.autopilot import AutoPilotMixin  # noqa: E402

FAILS: list = []
N = 0
RUN = 990300


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


DDL_TODOS = """CREATE TABLE agent_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, run_id INTEGER,
    seq INTEGER, subject TEXT, status TEXT DEFAULT 'pending', verify TEXT DEFAULT '',
    verify_tool TEXT DEFAULT '', verify_args TEXT DEFAULT '',
    created_at TEXT, updated_at TEXT)"""
DDL_HDEC = """CREATE TABLE harness_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER DEFAULT 0,
    mode TEXT DEFAULT '', would_block INTEGER DEFAULT 0, force_release INTEGER DEFAULT 0,
    reason TEXT DEFAULT '', remaining_n INTEGER DEFAULT 0, pending_n INTEGER DEFAULT 0,
    manual_n INTEGER DEFAULT 0, autoready_n INTEGER DEFAULT 0, guarded_n INTEGER DEFAULT 0,
    tools_json TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(DDL_TODOS); c.execute(DDL_HDEC)
    return c


def _seed(conn, *, n_ok=1, n_bad=1, write_tool=False):
    i = 0
    for ok in ([True] * n_ok + [False] * n_bad):
        i += 1
        tool = "sysml_v2_validate"
        if write_tool and not ok:
            tool = "save_artifact"          # 写类：必须被护栏挡下、不得执行
        conn.execute(
            "INSERT INTO agent_todos (conversation_id,run_id,seq,subject,status,verify,"
            "verify_tool,verify_args,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (999999300, RUN, i, "验收项%d" % i, "pending", "判定 n_hard=0",
             tool, '{"project_id": -1}', "2026-11-18 01:00:00", "2026-11-18 01:00:00"))
    conn.commit()


class _Ctx:
    def __init__(self): self.saved = []

    def set(self, obj, name, val):
        self.saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def close(self):
        for obj, name, val in reversed(self.saved):
            setattr(obj, name, val)
        self.saved.clear()


class Harness(AutoPilotMixin):
    def __init__(self, *, fail=False):
        self.exec_calls = []
        self.fail = fail

    def _exec_tool_call(self, tool, args):
        self.exec_calls.append((tool, args))
        return {"ok": (not self.fail), "result": "夹具：n_hard=3" if self.fail else "pass"}


@contextlib.contextmanager
def _ctx(conn, mode):
    import database
    import agent.pipeline_parts.common as _common
    import agent.pipeline_parts.autopilot as _ap
    import core.config as _cfg

    @contextlib.contextmanager
    def _fake():
        yield conn

    c = _Ctx()
    for mod in (database, _common, _ap):
        if hasattr(mod, "db_conn"):
            c.set(mod, "db_conn", _fake)
    _orig_get = _cfg.get

    def _get(section, key=None, default=None):
        if section == "agent" and key == "harness_mode":
            return mode
        if section == "agent" and key == "harness_max_blocks":
            return 8
        return _orig_get(section, key, default)

    c.set(_cfg, "get", _get)
    try:
        yield
    finally:
        c.close()


def _run(conn, mode, **kw):
    h = Harness(**kw)
    with _ctx(conn, mode):
        text = AutoPilotMixin.orch_enforce_run(h, RUN, 999999300)
    return text, h


def case_code() -> None:
    print("\nA. 代码级")
    s = read_stream_source()
    a = io.open(os.path.join(REPO, "agent", "pipeline_parts", "autopilot.py"),
                encoding="utf-8").read()
    i_call = s.find("self.orch_enforce_run(")
    i_notice = s.find("self.build_orch_todo_notice(")
    check("A1 收口调用存在", i_call > 0, "未找到")
    check("A2 在 parse 告知之后调用（先统计、后验收）",
          0 < i_notice < i_call, "notice=%s enforce=%s" % (i_notice, i_call))
    check("A3 调用被 try 兜住（验收设施故障不崩主流程）",
          "except Exception:\n                _enf_text = \"\"" in s, "无保护")
    body = a[a.find("def orch_enforce_run"):a.find("def _orch_notice_observe")]
    check("A4 enforce 模式才执行（shadow/off 零执行）",
          'if _mode != "enforce"' in body, "未判模式")
    check("A5 复用 _run_verify 三重护栏（不自己调工具）",
          "self._run_verify(" in body and "_exec_tool_call(" not in body, "绕过护栏")
    check("A6 有上限（不会无限验）", "_cap" in body and "[:_cap]" in body, "无上限")
    check("A7 不另起 ReAct / 不改任务状态",
          "execute_stream" not in body and "UPDATE agent_tasks" not in body, "越界")


def case_behavior() -> None:
    print("\nB. 行为级（内存库真跑）")
    conn = _conn(); _seed(conn)
    text, h = _run(conn, "shadow")
    check("B1 shadow 模式零执行", not h.exec_calls and text == "", str(h.exec_calls))
    _srow = conn.execute("SELECT COUNT(*) FROM harness_decisions WHERE mode='orch_enforce'"
                         ).fetchone()[0]
    check("B2 shadow 不落 orch_enforce 行（不冒充已验收）", _srow == 0, str(_srow))

    conn2 = _conn(); _seed(conn2)
    text2, h2 = _run(conn2, "enforce", fail=True)
    check("B3 enforce 真执行了 2 条只读验收", len(h2.exec_calls) == 2, str(h2.exec_calls))
    check("B4 失败写入交付正文（不许声称完成）",
          "只读验收未通过" in text2 and "验收项2" in text2, text2[:200])
    row = conn2.execute("SELECT mode,would_block FROM harness_decisions ORDER BY id DESC LIMIT 1"
                        ).fetchone()
    check("B5 台账 would_block=1", row["mode"] == "orch_enforce" and row["would_block"] == 1,
          str(dict(row)))

    conn3 = _conn(); _seed(conn3, n_ok=2, n_bad=0)
    text3, h3 = _run(conn3, "enforce")
    check("B6 全通过 ⇒ 正文无告知（不打扰）", text3 == "", repr(text3)[:80])
    row3 = conn3.execute("SELECT would_block FROM harness_decisions ORDER BY id DESC LIMIT 1"
                         ).fetchone()
    check("B7 全通过也落台账（分母不缺）", row3 and row3["would_block"] == 0, str(dict(row3)))

    conn4 = _conn(); _seed(conn4, n_ok=0, n_bad=2, write_tool=True)
    text4, h4 = _run(conn4, "enforce")
    check("B8 ★ 写类工具被护栏挡下（未被真执行）",
          all(t == "sysml_v2_validate" for t, _ in h4.exec_calls)
          and len(h4.exec_calls) == 0, str(h4.exec_calls))
    check("B9 ★ 被挡下的工具显式告知「未自动执行」（不静默放过）",
          "未自动执行" in text4 and "save_artifact" in text4, text4[:200])


def main() -> int:
    print("编排主路径收口验收门禁")
    print("=" * 66)
    case_code()
    case_behavior()
    print("\n" + "=" * 66)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
