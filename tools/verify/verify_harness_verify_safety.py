# -*- coding: utf-8 -*-
"""P0-2a（2026-11-09）验收闸安全性门禁。

修的是什么（生产事故，不是假想）：
  `agent_todos.verify` 由 LLM 自由书写 ⇒ 761 条全是自然语言。
  旧解析器「首段是 ASCII 就当工具名、参数解析失败就塞 value=自然语言」，
  ⇒ 44%（333 条）被判成 tool 并**真去执行**，其中混着写类/交互类
  （save_artifact 7 / requirement_commit 5 / export_sysml_v2 4 / ask_user 15 …）。
  已发生：conv 990463 `verify="JSON 含 artifacts/…"` ⇒ 真调 `JSON` ⇒
  「未知工具: JSON」⇒ 判定验收未通过 ⇒ 拦住不让结束 ⇒ 模型只能反复改 todo 状态空转。

本门禁断言四组（缺一组就会出现"改了但没修住"）：

  A. **解析语义**：自然语言一律 manual；结构化只读 + JSON 参数才 tool
  B. **历史回放**：真实库里 761 条 verify，新实现判 tool 的必须是 0 条
     （旧实现同口径是 333 条 —— 脚本自己复现旧算法做对照，不靠记忆）
  C. **shadow 零执行**：shadow 分支下 **不得**调用 _run_verify / _exec_tool_call
     （旧语义"观察"也走真执行 ⇒ 观察期本身就是事故现场）
  D. **落库透传**：todo_write 的 verify_tool/verify_args 真的进库，且 list 能读回

用法：``.venv/Scripts/python.exe tools/verify/verify_harness_verify_safety.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent import harness_gate as hg  # noqa: E402

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


# ── A. 解析语义 ────────────────────────────────────────────────
def case_parse() -> None:
    print("\nA. 解析语义（自然语言一律 manual；结构化只读 + JSON 才 tool）")

    # A1 真实历史样本：一直被误判成 tool 的那类
    sp = hg.parse_verify_spec("sysml_v2_validate 返回 verdict=pass 或 n_hard=0")
    check("A1 自然语言参数（经典误判样本）⇒ manual", sp["kind"] == "manual",
          "实际 kind=%s tool=%s" % (sp["kind"], sp["tool"]))
    check("A1b 该样本不得给出任何工具名", not sp["tool"], "tool=%r" % sp["tool"])

    # A2 事故原样：verify 以 ASCII 词开头，但根本不是工具
    sp = hg.parse_verify_spec("JSON 含 artifacts/evidence/risks/claims，status 与 missing 一致")
    check("A2 事故样本「JSON 含…」⇒ manual（旧实现会调 JSON 工具）",
          sp["kind"] == "manual", "实际 kind=%s" % sp["kind"])

    # A3 首段是合法工具名但无参数 ⇒ 不执行（无法确定校验对象）
    sp = hg.parse_verify_spec("sysml_v2_validate")
    check("A3 只有工具名无参数 ⇒ manual", sp["kind"] == "manual",
          "实际 kind=%s" % sp["kind"])

    # A4 写类工具即便参数是合法 JSON 也**不执行**
    for t in ("save_artifact", "requirement_commit", "export_sysml_v2",
              "zhiyuan_sysmlv2_import", "import_sysml_text", "file_write",
              "autofix", "sysml_v2_autofix", "ask_user"):
        sp = hg.parse_verify_spec("%s {\"x\": 1}" % t)
        check("A4 写类/交互类 %s ⇒ manual（白名单外不执行）" % t,
              sp["kind"] == "manual", "实际 kind=%s" % sp["kind"])
        check("A4b %s 的拒绝原因可见" % t, bool(sp.get("blocked_reason")))

    # A5 伪工具名
    for t in ("verdict", "AST", "risks", "gaps", "n_hard", "t1", "READ"):
        sp = hg.parse_verify_spec("%s 一些说明" % t)
        check("A5 伪工具名 %s ⇒ manual" % t, sp["kind"] == "manual")

    # A6 结构化：只读工具 + dict 参数 ⇒ tool
    sp = hg.parse_verify_spec("校验代码", "sysml_v2_validate",
                              {"code": "package P { }"})
    check("A6 结构化 verify_tool=sysml_v2_validate ⇒ tool",
          sp["kind"] == "tool" and sp["origin"] == "structured",
          "kind=%s origin=%s" % (sp["kind"], sp["origin"]))
    check("A6b 参数原样传入（不塞 value）",
          sp["args"].get("code") == "package P { }", str(sp["args"]))

    # A7 结构化：参数是 JSON 字符串也接受
    sp = hg.parse_verify_spec("", "sysml_v2_lint", '{"code":"x"}')
    check("A7 verify_args 传 JSON 字符串 ⇒ tool", sp["kind"] == "tool",
          "kind=%s err=%s" % (sp["kind"], sp.get("parse_error")))

    # A8 结构化：参数不是 JSON 对象 ⇒ 不执行
    sp = hg.parse_verify_spec("", "sysml_v2_validate", "返回 pass 就行")
    check("A8 verify_args 是自然语言 ⇒ manual", sp["kind"] == "manual",
          "kind=%s" % sp["kind"])

    # A9 verify 本身是 JSON 形态
    sp = hg.parse_verify_spec('{"tool": "query_projects", "args": {"limit": 5}}')
    check("A9 verify 为 JSON 对象 ⇒ tool",
          sp["kind"] == "tool" and sp["tool"] == "query_projects" and sp["args"].get("limit") == 5,
          "kind=%s tool=%s" % (sp["kind"], sp["tool"]))

    # A10 legacy 形态：参数必须是 JSON 对象
    sp = hg.parse_verify_spec('sysml_v2_validate {"code": "x"}')
    check("A10 legacy `工具名 {json}` ⇒ tool", sp["kind"] == "tool",
          "kind=%s" % sp["kind"])
    sp = hg.parse_verify_spec("sysml_v2_validate 代码内容")
    check("A10b legacy 参数非 JSON ⇒ manual（旧实现会塞 value 硬调）",
          sp["kind"] == "manual", "kind=%s" % sp["kind"])

    # A11 空 / 人工确认
    check("A11 空 verify ⇒ manual", hg.parse_verify_spec("")["kind"] == "manual")
    check("A11b 『人工确认』⇒ manual", hg.parse_verify_spec("人工确认")["kind"] == "manual")

    # A12 白名单本身不应包含写类工具（防止有人把写类加进去）
    WRITE = {"save_artifact", "requirement_commit", "export_sysml_v2",
             "zhiyuan_sysmlv2_import", "import_sysml_text", "file_write",
             "autofix", "sysml_v2_autofix", "ask_user"}
    check("A12 只读白名单里没有任何写类/交互类工具",
          not (WRITE & hg.READONLY_VERIFY_TOOLS),
          "交集=%s" % (WRITE & hg.READONLY_VERIFY_TOOLS))


# ── B. 历史回放 ────────────────────────────────────────────────
def _legacy_parse(raw: str) -> dict:
    """复现**旧**解析算法（仅用于对照，不能用于生产判定）。

    旧逻辑：首段是 ASCII 词 ⇒ 当工具名；参数 JSON 失败 ⇒ 塞 value 硬传。
    """
    import re
    raw = str(raw or "").strip()
    if not raw or raw in ("人工确认", "manual", "人工"):
        return {"kind": "manual", "tool": ""}
    parts = raw.split(None, 1)
    tool = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9_.:]*$", tool):
        return {"kind": "manual", "tool": ""}
    args = {}
    if rest:
        try:
            obj = json.loads(rest)
            args = obj if isinstance(obj, dict) else {"value": obj}
        except Exception:
            args = {"value": rest}
    return {"kind": "tool", "tool": tool, "args": args}


def case_replay() -> None:
    print("\nB. 历史回放（真实库 761 条 verify；旧实现 333 条误判为对照）")
    db = os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        print("  [SKIP] 找不到 mbse.db，跳过历史回放")
        return
    try:
        c = sqlite3.connect(db)
        rows = c.execute("SELECT verify FROM agent_todos").fetchall()
        c.close()
    except Exception as e:
        print("  [SKIP] 读库失败：%r" % (e,))
        return
    total = len(rows)
    new_tool = legacy_tool = legacy_write = 0
    WRITE = {"save_artifact", "requirement_commit", "export_sysml_v2",
             "zhiyuan_sysmlv2_import", "import_sysml_text", "file_write",
             "autofix", "sysml_v2_autofix", "ask_user"}
    for (v,) in rows:
        if hg.parse_verify_spec(v or "")["kind"] == "tool":
            new_tool += 1
        lp = _legacy_parse(v or "")
        if lp["kind"] == "tool":
            legacy_tool += 1
            if lp["tool"] in WRITE:
                legacy_write += 1
    print("    样本 %d 条：旧实现判 tool=%d（其中写类/交互类 %d）；新实现判 tool=%d"
          % (total, legacy_tool, legacy_write, new_tool))
    check("B1 旧实现确实会误判（对照组非空，证明修的是真问题）", legacy_tool > 0,
          "旧实现判 tool=%d" % legacy_tool)
    check("B2 旧实现会把写类/交互类当验收执行（事故面）", legacy_write > 0,
          "写类 %d 条" % legacy_write)
    check("B3 新实现判 tool 的条数 = 0（全是自然语言 ⇒ 全 manual）", new_tool == 0,
          "新实现判 tool=%d 条" % new_tool)


# ── C. shadow 零执行 ───────────────────────────────────────────
def case_shadow() -> None:
    print("\nC. shadow 零执行（观察期不得调用任何工具）")

    class Stub:
        def __init__(self):
            self.calls = []

        def _harness_cfg(self):
            return {"mode": "shadow", "enabled": True, "shadow": True,
                    "max_blocks": 8, "max_stall": 2}

        def _todo_gate(self, conv, conn=None):
            return {"done": False, "fallback": False,
                    "remaining": ["未完成的 A", "未完成的 B"]}

        def _verify_stats(self, conv):
            self.calls.append("stats")
            return {"pending": 2, "manual": 2, "autoready": 0, "guarded": 0, "tools": {}}

        def _harness_observe(self, *a, **k):
            self.calls.append("observe")

        def _run_verify(self, *a, **k):
            self.calls.append("RUN_VERIFY")
            raise AssertionError("shadow 下不得执行验收")

        def _exec_tool_call(self, *a, **k):
            self.calls.append("EXEC_TOOL")
            raise AssertionError("shadow 下不得调用工具")

    from agent.pipeline_parts.autopilot import AutoPilotMixin
    st = Stub()
    d = AutoPilotMixin._harness_gate(st, 990999, 0)
    check("C1 shadow 下 block=False（不真拦）", d.get("block") is False, str(d.get("block")))
    check("C2 判定结果带 shadow 标记与 would_block", d.get("shadow") is True
          and "would_block" in d, str({k: d.get(k) for k in ("shadow", "would_block")}))
    check("C3 该拦的情况下 would_block=True（判定照样做）", d.get("would_block") is True,
          str(d.get("would_block")))
    check("C4 shadow 未调用 _run_verify", "RUN_VERIFY" not in st.calls, str(st.calls))
    check("C5 shadow 未调用 _exec_tool_call", "EXEC_TOOL" not in st.calls, str(st.calls))
    check("C6 shadow 落了观测记录", "observe" in st.calls, str(st.calls))

    # off 模式：连 todo_gate 都不读
    class OffStub(Stub):
        def _harness_cfg(self):
            return {"mode": "off", "enabled": False, "shadow": False,
                    "max_blocks": 8, "max_stall": 2}

        def _todo_gate(self, conv, conn=None):
            self.calls.append("TODO_GATE")
            return {"done": False, "fallback": False, "remaining": ["x"]}

    off = OffStub()
    d2 = AutoPilotMixin._harness_gate(off, 990999, 0)
    check("C7 off 模式直接不介入", d2.get("block") is False and d2.get("mode") == "off")
    check("C8 off 模式不读 todo_gate（节省一次查询）", "TODO_GATE" not in off.calls,
          str(off.calls))


# ── D. 落库透传 ────────────────────────────────────────────────
def case_store() -> None:
    print("\nD. 落库透传（verify_tool / verify_args 真进库且能读回）")
    from agent.todo_store import handle_todo_write, list_todos

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE agent_todos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
        run_id INTEGER DEFAULT 0, seq INTEGER, subject TEXT NOT NULL,
        status TEXT DEFAULT 'pending', verify TEXT DEFAULT '',
        verify_result TEXT DEFAULT '', blocked_by TEXT DEFAULT '', note TEXT DEFAULT '',
        created_at TEXT, updated_at TEXT,
        verify_tool TEXT DEFAULT '', verify_args TEXT DEFAULT '')""")

    r = handle_todo_write({"action": "create", "items": [
        {"subject": "校验代码", "verify": "判据 pass",
         "verify_tool": "sysml_v2_validate", "verify_args": {"code": "package P{}"}},
        {"subject": "人工看一眼", "verify": "人工确认"},
    ]}, 1, conn=conn)
    check("D1 批量 create 成功", bool(r.get("ok")), str(r.get("result"))[:120])

    rows = list_todos(conn, 1)
    a = [x for x in rows if x["subject"] == "校验代码"][0]
    b = [x for x in rows if x["subject"] == "人工看一眼"][0]
    check("D2 verify_tool 落库", a.get("verify_tool") == "sysml_v2_validate",
          repr(a.get("verify_tool")))
    check("D3 verify_args 以 JSON 字符串落库",
          json.loads(a.get("verify_args") or "{}").get("code") == "package P{}",
          repr(a.get("verify_args")))
    check("D4 未填结构化的条目两列为空",
          not b.get("verify_tool") and not b.get("verify_args"),
          "%r/%r" % (b.get("verify_tool"), b.get("verify_args")))

    # 读回后能直接喂给解析器并判为 tool
    sp = hg.parse_verify_spec(a["verify"], a["verify_tool"], a["verify_args"])
    check("D5 读回后可直接解析成可执行规格", sp["kind"] == "tool" and sp["tool"] == "sysml_v2_validate",
          "kind=%s err=%s" % (sp["kind"], sp.get("parse_error")))
    sp2 = hg.parse_verify_spec(b["verify"], b["verify_tool"], b["verify_args"])
    check("D6 未填结构化的条目 ⇒ manual（不会被乱调）", sp2["kind"] == "manual")

    # list 输出要显式告诉模型"会不会被自动验收"
    r2 = handle_todo_write({"action": "list"}, 1, conn=conn)
    txt = str(r2.get("result") or "")
    check("D7 list 输出标明自动验收状态", "自动验收" in txt, txt[:150])


def main() -> int:
    print("P0-2a 验收闸安全性门禁")
    print("=" * 64)
    case_parse()
    case_replay()
    case_shadow()
    case_store()
    print("\n" + "=" * 64)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
