# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：harness 闸「真的会拦」：enforce/shadow 真机 A/B（唯一变量=mode）+ conv=0 不再假绿 + 真库台账证据。★ 归 local 而非 pr：A1 断言生产配置 harness_mode='enforce'，干净库/env 下取默认值会翻红 —— 它是「落地验收」而不是「代码回归」。实测 1.0s
"""harness 闸「真的会拦」门禁（2026-11-17，shadow→enforce 落地后的第一批判据）。

## 为什么需要这道门禁

`harness_decisions` 上线以来累积 **202 条判定，全部 `mode='shadow'`，
`would_block=1` 只有 1 条**。台账看着正常，实际是**三处同时失效**：

  1. **开关关着**：`agent.harness_mode='shadow'` ⇒ 闸只判定、从不执行验收、从不拦。
  2. **查错了会话**：202 条里有 **36 条 `conversation_id=0`**，reason 一律
     「无待办 ⇒ 无验收条件，不拦」—— 而**同一时刻**父会话 999999002 正有
     17 条 pending 待办（15:09~15:13 建待办，15:11/15:12/15:13 的判定却记成
     conv=0、pending=0）。根因：编排子任务以
     `execute_stream(query, 0, session_conv_id=父会话)` 嵌套调用，
     `todo_write` 走 `_tool_conv_ctx`（= session_conv_id）把待办写进**父会话**，
     而闸拿的是裸 `conversation_id`(=0) ⇒ `list_todos(0)` 首行即
     `if not conversation_id: return []` ⇒ 恒空 ⇒ 恒"不拦"。
     这与 `stream.py:5380` 注释里「不要再各处写 `int(conversation_id or 0)`」
     是**同一类错误**（同一语义两个口径），只是漏在了闸这一处。
  3. **拦截面 ≈ 0**：53 个会话有 pending 待办，其中只有 **1 个**（conv 990727）
     曾走到闸 —— 另外 52 个走的是编排路径，而编排路径在收口前就 `return` 了
     （`stream.py` 4909/5108/5122），**根本到不了闸**。见本文件 D 组。

## 断言分组

  A. 配置生效：`harness_mode='enforce'`，且保护（max_blocks/max_stall）都在
  B. **真机 A/B**：同一批 pending 待办，`enforce` 必须 `block=True`、
     `shadow` 必须 `block=False` 但 `would_block=True` —— **唯一变量就是 mode**
  C. 无会话归属不再假绿：conv=0 必须落 `mode='subtask'`、reason 含「不适用」，
     且**不得**再出现「无待办 ⇒ 无验收条件，不拦」这句假话
  D. 代码级：subtask 短路必须在 `_todo_gate` 之前；两处调用点仍在
     （含**已知缺口**：编排主路径提前 return，尚未经过闸 —— 由 D6 如实记录）

用法：``.venv/Scripts/python.exe tools/verify/verify_harness_real_effect.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent.pipeline_parts.autopilot import AutoPilotMixin as _AutoPilotMixin  # noqa: E402

FAILS: list = []
N = 0

#: 本修复（subtask 短路 + shadow→enforce）的生效时刻。
#: 判据只针对**此后**写入的行 —— 历史 36 条 `conv=0` 是审计事实，不删不改。
FIX_TS = "2026-11-17 14:50:00"

AUTOPILOT = os.path.join(REPO, "agent", "pipeline_parts", "autopilot.py")
STREAM = os.path.join(REPO, "agent", "pipeline_parts", "stream.py")
EXECUTE = os.path.join(REPO, "agent", "pipeline_parts", "execute.py")

#: 被测会话号（只存在于本脚本的内存库里，绝不碰生产库）
CID = 990991


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


# ── 夹具：内存库（与生产同 schema 子集）+ 把 database.db_conn 指过去 ──────
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


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(DDL_TODOS)
    conn.execute(DDL_HDEC)
    conn.commit()
    return conn


def _seed(conn: sqlite3.Connection, cid: int = CID) -> None:
    """两条 pending：一条可自动验（只读白名单 + 结构化参数）、一条只能人工。"""
    conn.execute(
        "INSERT INTO agent_todos (conversation_id, seq, subject, status, verify, "
        "verify_tool, verify_args, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, 1, "校验状态机代码", "pending", "sysml_v2_validate 返回 n_hard=0",
         "sysml_v2_validate", '{"project_id": -1}', "2026-11-17 14:00:00",
         "2026-11-17 14:00:00"))
    conn.execute(
        "INSERT INTO agent_todos (conversation_id, seq, subject, status, verify, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (cid, 2, "人工确认视图命名", "pending", "人工确认视图名称与代码一致",
         "2026-11-17 14:00:00", "2026-11-17 14:00:00"))
    conn.commit()


class Harness(_AutoPilotMixin):
    """只用**生产实现**跑闸：仅覆盖「配置」与「工具执行」两个 IO 点。

    · `_harness_cfg` 被覆盖 ⇒ A/B 两组之间**唯一变量就是 mode**（这是 A/B 的全部价值）
    · `_exec_tool_call` 被覆盖 ⇒ 不去真调智源/checker，只记账并返回"不通过"
    · 其余（`_todo_gate` / `_verify_stats` / `_run_verify` / `_harness_observe`）
      **一律走 AutoPilotMixin 真身**，数据落在本脚本的内存库里
    """

    MAX_BLOCKS = 8
    MAX_STALL = 2

    def __init__(self, mode: str = "enforce"):
        self.mode = mode
        self.exec_calls: list = []

    # —— 唯一覆盖点 ①：配置 ——
    def _harness_cfg(self) -> dict:
        m = self.mode
        return {"mode": m, "enabled": m != "off", "shadow": m == "shadow",
                "max_blocks": self.MAX_BLOCKS, "max_stall": self.MAX_STALL}

    # —— 唯一覆盖点 ②：工具执行（不碰外部系统）——
    def _exec_tool_call(self, tool, args):
        self.exec_calls.append((tool, args))
        return {"ok": False, "result": "夹具：n_hard=3（故意不通过）"}


@contextlib.contextmanager
def _patch_db_conn(conn):
    """把 `db_conn()` 指向内存库。

    ⚠️ 必须**逐个模块**打补丁，而不是只改 `database.db_conn`：
      · `autopilot.py` 是 `from .common import *`，`db_conn` 在**模块加载时**就绑进了
        自己的命名空间（`_todo_gate` 用的就是这个早期绑定）；
      · `_harness_observe` / `_verify_stats` 等函数体内是 `from database import db_conn`
        ⇒ 走的是 `database` 的当前属性。
      只改一处会造成"一半读内存库、一半读生产库"——本仓已记录过同族事故
      （"两种库路径口径并存 ⇒ 拿到假绿 33 项"）。所以两处都改，且**先断言都改到了**。
    """
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
    try:
        yield
    finally:
        for mod, old in saved:
            mod.db_conn = old


def _gate(conn, mode: str, cid: int = CID, blocks_used: int = 0,
          prev_remaining=None, stall_streak: int = 0):
    with _patch_db_conn(conn):
        h = Harness(mode)
        d = _AutoPilotMixin._harness_gate(h, cid, blocks_used,
                                          prev_remaining=prev_remaining,
                                          stall_streak=stall_streak)
        return d, h


# ── A. 配置生效 ────────────────────────────────────────────────
def case_config() -> None:
    print("\nA. 配置生效（开关真的开了，且保护仍在）")
    from core import config as cfg
    v = str(cfg.get("agent", "harness_mode", "") or "").strip().lower()
    check("A1 agent.harness_mode = enforce", v == "enforce", "实际 %r" % v)
    check("A2 阻断上限 > 0（0 会让闸永不拦）",
          int(cfg.get("agent", "harness_max_blocks", 8) or 0) > 0,
          "max_blocks=%r" % cfg.get("agent", "harness_max_blocks", 8))
    check("A3 无进展阈值 >= 1（否则卡死检测失效）",
          int(cfg.get("agent", "harness_max_stall", 2) or 0) >= 1,
          "max_stall=%r" % cfg.get("agent", "harness_max_stall", 2))

    from agent.pipeline_parts.autopilot import AutoPilotMixin
    c = AutoPilotMixin._harness_cfg(Harness("enforce"))
    check("A4 _harness_cfg 解析出 mode=enforce/enabled=True/shadow=False",
          c.get("mode") == "enforce" and c.get("enabled") is True and c.get("shadow") is False,
          str(c))


# ── B. 真机 A/B：enforce 真的拦，shadow 只判定 ────────────────
def case_ab() -> None:
    print("\nB. 真机 A/B（同一批 pending 待办，唯一变量 = mode）")
    conn = _make_conn()
    _seed(conn)

    d_en, h_en = _gate(conn, "enforce")
    check("B1 enforce 下 block=True（真拦）", d_en.get("block") is True,
          "block=%r reason=%r" % (d_en.get("block"), d_en.get("reason")))
    check("B2 enforce 下真执行了只读验收工具", bool(h_en.exec_calls),
          "exec_calls=%r" % (h_en.exec_calls,))
    check("B3 只执行了白名单内的工具",
          all(t == "sysml_v2_validate" for t, _ in h_en.exec_calls),
          str(h_en.exec_calls))
    check("B4 failures 非空且指向该待办",
          bool(d_en.get("failures")) and "校验状态机代码" in " ".join(d_en.get("failures") or []),
          str(d_en.get("failures"))[:200])
    check("B5 blocks_used 递增 0→1", int(d_en.get("blocks_used") or 0) == 1,
          str(d_en.get("blocks_used")))
    _fb = str(d_en.get("feedback") or "")
    check("B6 回灌内容非空且含未通过明细（模型能据此修）",
          bool(_fb) and "校验状态机代码" in _fb, _fb[:200])
    _wb1 = conn.execute("SELECT COUNT(*) FROM harness_decisions WHERE mode='enforce' "
                        "AND would_block=1").fetchone()[0]
    check("B6b ★ enforce 真拦时落台账（would_block=1）", _wb1 >= 1, "行数=%d" % _wb1)

    d_sh, h_sh = _gate(conn, "shadow")
    check("B7 shadow 下 block=False（不真拦）", d_sh.get("block") is False,
          str(d_sh.get("block")))
    check("B8 shadow 下 would_block=True（判定照样做）", d_sh.get("would_block") is True,
          str(d_sh.get("would_block")))
    check("B9 shadow 下零执行（观察期不得有副作用）", not h_sh.exec_calls,
          str(h_sh.exec_calls))
    check("B10 A/B 差异只来自 mode（同一批数据、同一套实现）",
          d_en.get("block") is True and d_sh.get("block") is False)

    # 全部通过验收 ⇒ 不得误拦
    conn.execute("UPDATE agent_todos SET status='completed', verify_result='pass' "
                 "WHERE conversation_id=?", (CID,))
    conn.commit()
    d_ok, h_ok = _gate(conn, "enforce")
    check("B11 待办全部通过验收 ⇒ block=False（不误拦）", d_ok.get("block") is False,
          "block=%r reason=%r" % (d_ok.get("block"), d_ok.get("reason")))
    check("B12 全部通过时不再跑验收工具（省一次执行）", not h_ok.exec_calls,
          str(h_ok.exec_calls))
    _wb0 = conn.execute("SELECT COUNT(*) FROM harness_decisions WHERE mode='enforce' "
                        "AND would_block=0").fetchone()[0]
    check("B12b ★ enforce『未拦截』也落台账（拦截率分母不缺）", _wb0 >= 1,
          "行数=%d" % _wb0)

    # 用满上限 ⇒ 强制放行且**如实说明未完成**
    conn.execute("UPDATE agent_todos SET status='pending', verify_result='' "
                 "WHERE conversation_id=?", (CID,))
    conn.commit()
    d_cap, _ = _gate(conn, "enforce", blocks_used=Harness.MAX_BLOCKS)
    check("B13 达上限 ⇒ block=False 且 force_release=True（不许无限拦）",
          d_cap.get("block") is False and d_cap.get("force_release") is True,
          "block=%r force_release=%r" % (d_cap.get("block"), d_cap.get("force_release")))
    check("B14 强制放行的 reason 如实说明「未完成」",
          "强制放行" in str(d_cap.get("reason") or ""), str(d_cap.get("reason")))

    # 无进展 ⇒ 提前放行（避免"拦 8 次、结果与立刻放行相同"的老毛病）
    _rem = list(d_en.get("remaining") or [])
    d_stall, _ = _gate(conn, "enforce", blocks_used=1,
                       prev_remaining=_rem, stall_streak=1)
    check("B15 连续无进展达阈值 ⇒ force_release（不空转到上限）",
          d_stall.get("block") is False and d_stall.get("force_release") is True,
          "block=%r force_release=%r stalled=%r"
          % (d_stall.get("block"), d_stall.get("force_release"), d_stall.get("stalled")))


# ── C. 无会话归属不再假绿 ─────────────────────────────────────
def case_subtask() -> None:
    print("\nC. 无会话归属（编排子任务）不再伪装成「跑了且通过」")
    conn = _make_conn()
    _seed(conn, CID)
    d, h = _gate(conn, "enforce", cid=0)
    check("C1 conv=0 ⇒ block=False（不拦、也不崩）", d.get("block") is False,
          "block=%r" % d.get("block"))
    check("C2 conv=0 ⇒ mode='subtask'（与 shadow/enforce 可区分）",
          d.get("mode") == "subtask", "mode=%r" % d.get("mode"))
    _rs = str(d.get("reason") or "")
    check("C3 reason 明说「不适用」，不得暗示验收通过", "不适用" in _rs, _rs)
    check("C4 ★ 不再出现「无待办 ⇒ 无验收条件，不拦」这句假话",
          "无待办" not in _rs, _rs)
    _row = conn.execute("SELECT mode, would_block, reason FROM harness_decisions "
                        "WHERE conversation_id=0 ORDER BY id DESC LIMIT 1").fetchone()
    check("C5 落库 mode='subtask'（台账可统计，不再混进 shadow）",
          _row is not None and _row["mode"] == "subtask",
          str(dict(_row)) if _row else "无记录")
    check("C6 conv=0 时不执行任何验收工具（子任务不该替父会话干活）",
          not h.exec_calls, str(h.exec_calls))


# ── D. 代码级 ─────────────────────────────────────────────────
def _read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


def case_code() -> None:
    print("\nD. 代码级（短路位置 / 调用点 / 已知缺口）")
    a = _read(AUTOPILOT)
    _i_sub = a.find("if not int(conversation_id or 0):")
    _i_gate = a.find("gate = self._todo_gate(conversation_id)")
    check("D1 subtask 短路存在", _i_sub > 0, "未找到短路分支")
    check("D2 ★ 短路在「读待办」**之前**（否则还是会去查 0 号会话）",
          0 < _i_sub < _i_gate, "sub=%s gate=%s" % (_i_sub, _i_gate))
    check("D3 落库 mode 字面量含 'subtask'",
          '"subtask"' in a or "'subtask'" in a, "未找到 subtask 字面量")

    s, e = _read(STREAM), _read(EXECUTE)
    check("D4 stream.py 调 _harness_gate", s.count("self._harness_gate(") >= 1,
          "count=%d" % s.count("self._harness_gate("))
    check("D5 execute.py 调 _harness_gate", e.count("self._harness_gate(") >= 1,
          "count=%d" % e.count("self._harness_gate("))

    # ★ 已知缺口：编排主路径在收口前 return，走不到闸。
    #   这里**如实断言"缺口仍在"**，等修好后把判据反转（而非删掉这条）——
    #   删掉即"这个维度不再受保护"，下次又会有人以为编排会话也受闸保护。
    _orch_returns = len(re.findall(r"_stream_orchestrated_traced\(", s))
    check("D6 已知缺口如实记录：编排主路径仍提前 return（尚未经过闸）",
          _orch_returns >= 2, "orch 调用点=%d" % _orch_returns)


# ── E. 真库证据（只读） ───────────────────────────────────────
def case_real_db() -> None:
    print("\nE. 真库证据（只读，不写生产库）")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("E0 生产库存在", False, db)
        return
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        _tot = conn.execute("SELECT COUNT(*) FROM harness_decisions").fetchone()[0]
        _pre = conn.execute(
            "SELECT COUNT(*) FROM harness_decisions WHERE conversation_id=0 "
            "AND mode IN ('shadow','enforce')").fetchone()[0]
        print("    历史台账：共 %d 条；其中 conv=0 且 mode∈{shadow,enforce} = %d 条"
              "（修复前的「假绿」，作为证据保留不删）" % (_tot, _pre))
        _post = conn.execute(
            "SELECT COUNT(*) FROM harness_decisions WHERE conversation_id=0 "
            "AND mode IN ('shadow','enforce') AND created_at >= ?", (FIX_TS,)).fetchone()[0]
        check("E1 ★ 修复生效后**不得新增** conv=0 的 shadow/enforce 行",
              _post == 0, "新增 %d 条（>= %s）" % (_post, FIX_TS))
        _en = conn.execute("SELECT COUNT(*) FROM harness_decisions WHERE mode='enforce'"
                           ).fetchone()[0]
        print("    真库 enforce 行：%d 条（当前运行进程仍是旧代码；重启后"
              "未拦截回合也会落行，分母才完整）" % _en)
    finally:
        conn.close()


def main() -> int:
    print("harness 闸「真的会拦」门禁（shadow→enforce）")
    print("=" * 68)
    case_config()
    case_ab()
    case_subtask()
    case_code()
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
