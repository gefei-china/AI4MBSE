# -*- coding: utf-8 -*-
"""待办完成态绑定机器回执（2026-11-09，P1-2b）。

被验对象：`agent/todo_store.py::machine_evidence` / `handle_todo_write` / `list_todos`

为什么：`verify_result` 一向由模型自己写。conv 990541 那 4 条待办全是自述 completed、
`verify_tool` 全空 —— 闸门只能看到 status，于是"说完成就完成"。
本机制把「声称」与「有机器回执」分开记录（shadow：记录但不阻断 status 迁移）。

关键判据（最容易做错的一条）：
    **skill 注入不算回执** —— conv 990541 的 01:49 那条 `SysML v2 校验与修复`
    是 call_kind='skill'（result 明写"未独立执行"），把它当回执 =
    用"看了说明书"证明"做过了"。

运行：<venv>/python.exe -X utf8 tools/verify/verify_todo_evidence.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.config as cfg  # noqa: E402
from agent.todo_store import (  # noqa: E402
    handle_todo_write, list_todos, machine_evidence, upsert_todo,
)

fails, checks = [], [0]


def chk(name, cond, detail=""):
    checks[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def add_log(conn, conv, tool, ok=1, kind="tool"):
    conn.execute(
        "INSERT INTO tool_call_logs (intent, agent_name, tool_name, tool_type, arguments, result,"
        " ok, latency_ms, conversation_id, created_at, call_kind, user_name, run_id, task_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("自检", "自检", tool, "builtin", "{}", "ok" if ok else "err", ok, 1,
         conv, "2026-11-09 10:00:00", kind, "", 0, ""))


conn = sqlite3.connect(cfg.DB_PATH)
conn.row_factory = sqlite3.Row
C = 990999801
try:
    conn.execute("BEGIN")
    conn.execute("DELETE FROM tool_call_logs WHERE conversation_id=?", (C,))
    conn.execute("DELETE FROM agent_todos WHERE conversation_id=?", (C,))

    print("[1] machine_evidence 三态")
    chk("无 verify_tool → claim",
        machine_evidence(conn, C, "")["kind"] == "claim")
    chk("有工具名但无调用记录 → claim",
        machine_evidence(conn, C, "sysml_v2_validate")["kind"] == "claim")

    add_log(conn, C, "sysml_v2_validate", ok=1, kind="tool")
    ev = machine_evidence(conn, C, "sysml_v2_validate")
    chk("有成功 tool 调用 → machine", ev["kind"] == "machine", ev)
    chk("回执带 log_id 与工具名",
        isinstance(ev.get("log_id"), int) and ev.get("tool") == "sysml_v2_validate", ev)

    print("\n[2] skill 注入不算回执（本项最容易做错）")
    add_log(conn, C, "SysML v2 校验与修复", ok=1, kind="skill")
    chk("call_kind='skill' → claim（不是 machine）",
        machine_evidence(conn, C, "SysML v2 校验与修复")["kind"] == "claim",
        machine_evidence(conn, C, "SysML v2 校验与修复"))

    print("\n[3] 失败调用不算回执")
    add_log(conn, C, "failing_tool", ok=0, kind="tool")
    chk("ok=0 → claim", machine_evidence(conn, C, "failing_tool")["kind"] == "claim")

    print("\n[4] update 到 completed 自动写入 evidence")
    r = upsert_todo(conn, C, subject="跑校验", verify="verdict=pass",
                    verify_tool="sysml_v2_validate", verify_args={"code": "package P;"})
    tid = r["id"]
    out = handle_todo_write({"action": "update", "id": tid, "status": "in_progress"},
                            C, conn=conn)
    chk("in_progress 迁移成功", out.get("ok"), out.get("result"))
    out = handle_todo_write({"action": "update", "id": tid, "status": "completed",
                             "verify_result": "pass"}, C, conn=conn)
    chk("completed 迁移成功", out.get("ok"), out.get("result"))
    chk("返回文本标注机器回执", "机器回执" in (out.get("result") or ""), out.get("result"))
    row = list_todos(conn, C)[0]
    chk("DB evidence.kind == machine", (row.get("evidence") or {}).get("kind") == "machine",
        row.get("evidence"))
    chk("status 仍为 completed（shadow：不阻断）", row["status"] == "completed", row["status"])

    print("\n[5] 无回执的 completed → claim，且在清单里可见")
    r2 = upsert_todo(conn, C, subject="人工看一遍", verify="人工确认")
    t2 = r2["id"]
    handle_todo_write({"action": "update", "id": t2, "status": "in_progress"}, C, conn=conn)
    out2 = handle_todo_write({"action": "update", "id": t2, "status": "completed",
                              "verify_result": "看过了"}, C, conn=conn)
    chk("提示「仅声称完成」", "仅声称完成" in (out2.get("result") or ""), out2.get("result"))
    rows = {x["id"]: x for x in list_todos(conn, C)}
    chk("DB evidence.kind == claim", (rows[t2].get("evidence") or {}).get("kind") == "claim",
        rows[t2].get("evidence"))

    print("\n[6] list 输出把两类完成分开标注")
    lst = handle_todo_write({"action": "list"}, C, conn=conn)["result"]
    chk("清单含「✅机器回执」", "✅机器回执" in lst)
    chk("清单含「⚠️仅声称完成」", "⚠️仅声称完成" in lst)

    print("\n[7] 非 completed 的状态迁移不写 evidence（避免旧值误导）")
    r3 = upsert_todo(conn, C, subject="第三项", verify="人工确认")
    t3 = r3["id"]
    handle_todo_write({"action": "update", "id": t3, "status": "in_progress"}, C, conn=conn)
    e3 = conn.execute("SELECT COALESCE(evidence,'') FROM agent_todos WHERE id=?",
                      (t3,)).fetchone()[0]
    chk("in_progress 的 evidence 仍为空", e3 == "", repr(e3))

finally:
    conn.rollback()
    conn.close()

print("\n" + "=" * 62)
if fails:
    print(f"FAILED {len(fails)}/{checks[0]} 项：{fails}")
    sys.exit(1)
print(f"ALL PASS（{checks[0]} 项断言）")
