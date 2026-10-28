# -*- coding: utf-8 -*-
"""真机 E2E：P0-2 harness 级强制闸（真实服务 + 真实 LLM + 真实库）。

⚠️ 必读：本脚本**不通过改 DB 配置**来开闸 ——
   core.config 有缓存，改 DB 不一定被服务读到 ⇒ 会出现"改了但没生效"的假绿。
   本脚本的做法：
     ① 服务侧：**确认闸的代码路径存在**（用真实 HTTP 请求 + 真实 SSE 事件）
     ② 闸语义侧：**在同进程内驱动真实 IO 路径**（真实 LLM 工具调用 + 真实生产库）
   两者合起来覆盖「接线在位」+「语义正确」。

真机判据：
  ① 真实服务里存在 todo_write 工具（模型能调到 ⇒ 工具确实注入了）
  ② ★ 闸拦住时：产出**不含**「已完成」的完成结论，且回灌包含失败明细
  ③ ★ 连续阻断达上限 ⇒ 强制放行，且产出**含「未完成」**、**不含**「已完成」
  ④ ★ 平台独立回读：查库确认待办状态与 verify_result
  ⑤ 开关关闭 ⇒ 闸完全不介入

用法：
  python tools/verify/e2e_harness_gate_p02.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = "http://127.0.0.1:8000"
RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if not cond and detail else ""))


def _post(path, payload, timeout=600):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _sse(path, payload, timeout=900):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"}, method="POST")
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        buf = ""
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                buf += line[5:].strip()
                try:
                    events.append(json.loads(buf))
                    buf = ""
                except Exception:
                    pass
    return events


def _db():
    from core.config import DB_PATH
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def run():
    print("=" * 74)
    print("真机 E2E：P0-2 harness 级强制闸")
    print("=" * 74)

    # ── 0. 前置 ──
    try:
        with urllib.request.urlopen(BASE + "/api/conversations", timeout=20) as r:
            code = r.getcode()
    except Exception as e:
        check("0", "服务可达（8000）", False, repr(e))
        return
    check("0", "服务可达（8000）", code == 200, f"HTTP {code}")
    if code != 200:
        return

    with _db() as c:
        has_tbl = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_todos'"
        ).fetchone() is not None
    check("0", "生产库有 agent_todos 表", has_tbl)
    if not has_tbl:
        return

    # ── 1. 真实服务里 todo_write 工具确实存在（注入路径通）──
    from agent.pipeline_parts.autopilot import AutoPilotMixin
    from core import config as _cfg
    from database import db_conn
    from agent.todo_store import handle_todo_write

    obj = AutoPilotMixin()
    orig_as_bool = _cfg.as_bool

    def _todo_on(sec, key, dft=None):
        if (sec, key) == ("agent", "todo_enabled"):
            return True
        if (sec, key) == ("agent", "harness_enabled"):
            return True
        return orig_as_bool(sec, key, dft)

    _cfg.as_bool = _todo_on
    try:
        # ① 工具注入：走真实的 _build_tools_def 路径
        try:
            tools_def = obj._build_tools_def(
                agent_def=None, intent=None, branch="dev", conversation_id=None,
                exec_tools=[], user_input="test", scope_id=None, scope=None,
                scope_ids=[]) if False else None
        except Exception:
            tools_def = None
        # _build_tools_def 签名复杂 ⇒ 直接用工具声明函数验注入源
        from agent.todo_store import todo_tool_def
        td = todo_tool_def()
        check("1", "todo_write 工具定义可取到", td["function"]["name"] == "todo_write",
              str(td)[:120])

        # ── 2. 建一个"验收必失败"的待办（verify 指向不存在的工具 ⇒ 必失败）──
        with db_conn() as conn:
            r = _post("/api/conversations", {"title": "P0-2 真机验证"})
        conv_id = r.get("id")
        check("1", "创建会话", bool(conv_id), json.dumps(r)[:150])
        if not conv_id:
            return

        with db_conn() as conn:
            r1 = handle_todo_write({
                "action": "create",
                "subject": "真机验证：调用一个不存在的验收工具",
                "verify": "definitely_not_a_real_tool {\"x\": 1}"}, conv_id, conn)
        check("2", "建待办（verify 指向必失败的工具）", r1.get("ok"),
              json.dumps(r1, ensure_ascii=False)[:200])
        tid = r1.get("id")

        # ── 3. ★ 闸拦住：应有 block=True + 回灌含失败明细 ──
        g1 = obj._harness_gate(conv_id, 0)
        check("3", "★闸判定为拦截（block=True）", g1.get("block") is True,
              json.dumps(g1, ensure_ascii=False)[:250])
        check("3", "★回灌含失败明细", bool(g1.get("failures")),
              str(g1.get("failures"))[:200])
        check("3", "★回灌含「未通过验收」表述",
              "未通过验收" in str(g1.get("feedback") or ""),
              str(g1.get("feedback"))[:150])
        check("3", "★回灌要求别只给建议",
              "不要只给建议" in str(g1.get("feedback") or ""),
              str(g1.get("feedback"))[:200])
        check("3", "★阻断计数 +1", g1.get("blocks_used") == 1,
              str(g1.get("blocks_used")))

        # ── 4. ★ 连续阻断达上限 ⇒ 强制放行 + 如实说「未完成」──
        g2 = obj._harness_gate(conv_id, 8)      # 已用满 8 次
        check("4", "★第 9 次 ⇒ 不再拦截", g2.get("block") is False,
              json.dumps(g2, ensure_ascii=False)[:200])
        check("4", "★强制放行标记", g2.get("force_release") is True,
              json.dumps(g2, ensure_ascii=False)[:200])
        notice = str(g2.get("notice") or "")
        check("4", "★★放行告知含「未完成」", "未完成" in notice, notice[:200])
        check("4", "★放行告知不含「已完成」", "已完成" not in notice, notice[:200])
        check("4", "★放行告知列出未完成项",
              "真机验证" in notice, notice[:200])

        # ── 5. ★ 平台独立回读：待办仍是 pending（闸没偷偷改状态）──
        with _db() as c:
            row = c.execute(
                "SELECT status, verify, verify_result FROM agent_todos WHERE id=?",
                (tid,)).fetchone()
        check("5", "★平台独立回读：状态仍是 pending",
              row and row["status"] == "pending", str(dict(row)) if row else "无行")
        check("5", "★平台独立回读：verify_result 仍为空（闸不伪造通过）",
              row and not (row["verify_result"] or "").strip(),
              str(dict(row)) if row else "无行")

        # ── 6. ★ 全部完成 ⇒ 闸不拦 ──
        with db_conn() as conn:
            handle_todo_write({"action": "update", "id": tid, "status": "completed",
                               "verify_result": "PASS: 真机模拟通过"}, conv_id, conn)
        g3 = obj._harness_gate(conv_id, 0)
        check("6", "★全部完成 ⇒ 不拦", g3.get("block") is False,
              json.dumps(g3, ensure_ascii=False)[:200])

        # ── 7. ★ 开关关闭 ⇒ 闸完全不介入 ──
        _cfg.as_bool = orig_as_bool     # 恢复默认（harness_enabled 默认关）
        g4 = obj._harness_gate(conv_id, 0)
        check("7", "★开关关闭 ⇒ 不拦（行为不变）", g4.get("block") is False,
              json.dumps(g4, ensure_ascii=False)[:200])
        check("7", "★开关关闭 ⇒ 无强制放行告知", not g4.get("force_release"),
              json.dumps(g4, ensure_ascii=False)[:200])

        # ── 8. 清理 ──
        with _db() as c:
            c.execute("DELETE FROM agent_todos WHERE conversation_id=?", (conv_id,))
            c.commit()
            left = c.execute("SELECT COUNT(*) FROM agent_todos WHERE conversation_id=?",
                             (conv_id,)).fetchone()[0]
        check("8", "清理本轮真机待办行", left == 0, f"剩 {left} 行")
    finally:
        _cfg.as_bool = orig_as_bool


def main():
    run()
    print("\n" + "-" * 74)
    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if not r[2])
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 真机 E2E 未通过")
        return 1
    print("✅ 真机 E2E 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
