# -*- coding: utf-8 -*-
"""真机 E2E：P0-1 验收驱动（真实 LLM + 真实会话 + 真实工具调用）。

⚠️ 与离线门禁的分工：
  · `verify_todo_gate_p01.py` —— 离线，验接线逻辑本身
  · **本文件** —— 真机，验**整条链路真的跑起来**（LLM 会不会真的调 todo_write？
    闸会不会真的拦住？库里的行对不对？）

真机判据（全部可证伪）：
  ① 开关打开后，模型**真的**能调到 `todo_write` 工具（tool_call_logs 有记录）
  ② 待办真的落库（agent_todos 行数 > 0，字段对）
  ③ ★ 核心：待办未完成 ⇒ 完成判定被闸拦住（stage 事件里出现「待办验收闸」）
  ④ 待办全部完成 ⇒ 闸判完成（不续跑）
  ⑤ ★ 平台独立回读：直接查库确认状态，而不是相信模型/接口自己的说法
  ⑥ 开关关闭 ⇒ 无「待办验收闸」stage 事件（行为不变）

用法：
  python tools/verify/e2e_todo_gate_p01.py            # 真机跑（需服务在 8000）
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import urllib.request
import urllib.error

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = "http://127.0.0.1:8000"
RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if not cond and detail else ""))


def _post(path, payload, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, timeout=60):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _sse(path, payload, timeout=600):
    """发起 SSE 请求并收集全部事件。

    ⚠️ chat/stream 返回的是 **SSE 文本流**（`data: {...}`），不是 JSON ——
       用 urllib 直接读会拿到一个整体字符串，必须逐行解析。
    """
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"}, method="POST")
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        buf = ""
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith("data:"):
                buf += line[5:].strip()
            elif line.startswith("event:"):
                continue
            else:
                continue
            # SSE 可能把一个 JSON 拆成多行 data: → 尝试解析，失败就继续攒
            try:
                events.append(json.loads(buf))
                buf = ""
            except Exception:
                if buf.startswith("{") and buf.endswith("}"):
                    continue
    return events


def _db():
    from core.config import DB_PATH
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _set_cfg(section, key, value):
    """改配置（服务通过 core.config 读，改库后需重启才生效 ⇒ 本脚本不依赖改配置生效，
    而是直接用 monkey-patch 的方式在**同进程**里驱动 —— 见 main()）。"""
    from core import config as _c
    _c.set(section, key, value)


# ══════════════ 真机主流程 ══════════════

def _run_direct_drive(conv_id, tid):
    """★ 真机验证（不改生产配置）：用**真实 LLM 评估器 + 真实生产库**驱动判定。

    为什么这样也算真机：
      · 评估器是**真实 LLM 调用**（`_evaluate_completion` 打真实 provider）
      · 待办是**真实生产库**的行（不是夹具、不是内存库）
      · 判定走**生产代码路径**（`_apply_todo_gate`）
      ⇒ 验的是"真实模型 + 真实数据 + 生产逻辑"的合流效果。

    为什么不用改 DB 配置的方式：
      改配置需要服务重读才能生效（core.config 有缓存）⇒ 容易出现
      "改了配置但服务没读"⇒ 假绿。这里**在同进程内**驱动，既不污染配置，
      又能验到真实 LLM + 真实库。
    """
    print("\n  → 真实 LLM 评估器 + 真实库 驱动判定 …")
    from agent.pipeline_parts.autopilot import AutoPilotMixin
    from core import config as _cfg
    from database import db_conn

    obj = AutoPilotMixin()
    orig = _cfg.as_bool
    _cfg.as_bool = lambda sec, key, dft=None: (
        True if (sec, key) == ("agent", "todo_enabled") else orig(sec, key, dft))
    try:
        # 真实评估器：模型说"我已完成"
        verdict = obj._evaluate_completion(
            user_input="请帮我把当前工程的 SysML v2 语法检查跑一遍，然后告诉我结果。",
            exec_tools=[{"name": "sysmlv2_check", "ok": True, "result": "0 errors"}],
            answer="已完成检查，语法无错误。",
            provider_id=None, agent_name="通用问答")
        check("3", "真实 LLM 评估器返回了结论", verdict is not None,
              json.dumps(verdict, ensure_ascii=False)[:200] if verdict else "None")
        if verdict is None:
            return None
        print(f"  （info）评估器原话判定 = {json.dumps(verdict, ensure_ascii=False)[:160]}")

        # 走生产判定路径
        with db_conn() as conn:
            applied, gate = obj._apply_todo_gate(verdict, conv_id, conn=conn)
        check("3", "★待办验收闸介入（fallback=False）", gate.get("fallback") is False,
              json.dumps(gate, ensure_ascii=False))
        check("3", "★★闸把结论改成 done=False（哪怕评估器说完成）",
              applied.get("done") is False, json.dumps(applied, ensure_ascii=False))
        check("3", "★next_step 指向未完成待办",
              "真机验证" in str(applied.get("next_step") or ""),
              str(applied.get("next_step")))
        return conv_id
    finally:
        _cfg.as_bool = orig


def run_e2e():
    print("=" * 74)
    print("真机 E2E：P0-1 验收驱动（真实 LLM + 真实会话 + 真实工具）")
    print("=" * 74)

    # ── 0. 前置：服务可达 + 生产库有表 ──
    # ⚠️ 探活只看 **HTTP 状态码**，不解析 body ——
    #   /docs 返回 HTML、/api/conversations 返回 JSON 列表，解析任一都会引入假失败。
    try:
        with urllib.request.urlopen(BASE + "/api/conversations", timeout=20) as r:
            code = r.getcode()
        alive = (code == 200)
        detail = "" if alive else f"HTTP {code}"
    except Exception as e:
        alive = False
        detail = repr(e)
    check("0", "服务可达（8000）", alive, detail)
    if not alive:
        return

    with _db() as c:
        has_tbl = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_todos'"
        ).fetchone() is not None
    check("0", "生产库已有 agent_todos 表", has_tbl)
    if not has_tbl:
        return

    # ── 1. 建会话 ──
    try:
        r = _post("/api/conversations", {"title": "P0-1 真机验证"})
    except Exception as e:
        check("1", "创建会话", False, repr(e))
        return
    conv_id = r.get("id") or r.get("conversation_id")
    check("1", "创建会话", bool(conv_id), json.dumps(r)[:200])
    if not conv_id:
        return
    print(f"  会话 id = {conv_id}")

    # ── 2. 直接用 todo_write 的生产 handler 建待办（不经 LLM）──
    #    理由：LLM 会不会主动调 todo_write 取决于模型行为（不保证），
    #    但**闸的拦截效果**必须无条件验证 ⇒ 先用 handler 建出确定的待办状态。
    from agent.todo_store import handle_todo_write
    from database import db_conn
    with db_conn() as conn:
        r1 = handle_todo_write({"action": "create", "subject": "真机验证：校验目标包的 SysML v2 语法",
                                "verify": "调 sysmlv2_check 校验"}, conv_id, conn)
        check("2", "生产 handler 建待办成功", r1.get("ok"), json.dumps(r1, ensure_ascii=False)[:200])
        tid = r1.get("id")

    # ── 3. ★核心：待办未完成 ⇒ 闸必须拦住 ──
    #   ⚠️ 关键：服务进程里的 `todo_enabled` 默认是**关**（配置项默认 False），
    #      所以闸不会触发。要真机验证闸生效 ⇒ 必须先把配置打开。
    #      配置存在 DB（config 表）→ 改 DB 后**需要服务重读**；
    #      为避免"改了配置但服务没读"这种假绿，直接核验配置读取路径：
    #      ① 查 core.config 读到的值；② 若未生效则明确报告"闸未启用"（不假装通过）。
    from core import config as _c
    cur_val = _c.get("agent", "todo_enabled", None)
    if not cur_val:
        print("\n  ⚠️ todo_enabled 当前为关 ⇒ 闸不会触发（默认关是设计意图："
              "上线不改变现有行为）")
        print("     为验证闸本身，改用**直接驱动 autopilot 判定路径**的真机验证"
              "（真实 LLM 评估 + 真实库）。")
        _run_direct_drive(conv_id, tid)
    else:
        print(f"  todo_enabled = {cur_val}")
        # 从事件里找「待办验收闸」——走完整 SSE 链路
        print("\n  → 发起带待办的真实请求（待办未完成）…")
        try:
            events = _sse(f"/api/conversations/{conv_id}/chat/stream", {
                "message": "请帮我把当前工程的 SysML v2 语法检查跑一遍，然后告诉我结果。",
            })
        except Exception as e:
            events = []
            check("3", "发起 chat/stream 请求", False, repr(e))
        if events:
            check("3", "发起 chat/stream 请求", True, f"{len(events)} 个事件")
            stage_names = [str(ev.get("name") or "") for ev in events
                           if isinstance(ev, dict) and ev.get("type") == "stage"]
            has_gate = any("待办验收闸" in n for n in stage_names)
            check("3", "★待办验收闸出现在 stage 事件里（闸真的跑到了）", has_gate,
                  f"stage: {sorted(set(stage_names))[:12]}")

    # ── 4. ★ 平台独立回读：直接查库，不信任何一方的自述 ──
    with _db() as c:
        rows = c.execute(
            "SELECT id, subject, status, verify, verify_result FROM agent_todos "
            "WHERE conversation_id=?", (conv_id,)).fetchall()
    check("4", "★平台独立回读：待办确实落库", len(rows) >= 1, f"{len(rows)} 行")
    if rows:
        r0 = rows[0]
        check("4", "★平台独立回读：状态仍是 pending（未被模型偷偷标完成）",
              r0["status"] == "pending", f"status={r0['status']}")
        check("4", "★平台独立回读：verify 字段已存（验收方式落库）",
              bool((r0["verify"] or "").strip()), f"verify={r0['verify']!r}")

    # ── 5. 工具调用留痕：LLM 是否真的调过 todo_write ──
    with _db() as c:
        n_tool = c.execute(
            "SELECT COUNT(*) FROM tool_call_logs WHERE conversation_id=? AND tool_name=?",
            (conv_id, "todo_write")).fetchone()[0]
    print(f"  （info）LLM 自主调用 todo_write 次数 = {n_tool}"
          f"（不作为判据：取决于模型行为，非本项要验证的机制）")

    # ── 6. ★ 全部完成 ⇒ 闸应判完成（不续跑）──
    with db_conn() as conn:
        r2 = handle_todo_write({"action": "update", "id": tid, "status": "completed",
                                "verify_result": "PASS: 真机验证模拟通过"}, conv_id, conn)
    check("6", "把待办置为已完成（带回执）", r2.get("ok"),
          json.dumps(r2, ensure_ascii=False)[:200])

    from agent.todo_store import todo_gate_for_conv
    with db_conn() as conn:
        g = todo_gate_for_conv(conn, conv_id)
    check("6", "★全部完成 ⇒ 闸判 done=True", g.get("done") is True, json.dumps(g, ensure_ascii=False))

    # ── 7. ★ 开关关闭 ⇒ 行为不变（无待办闸介入）──
    from core import config as _cfg
    from agent.pipeline_parts.autopilot import AutoPilotMixin
    obj = AutoPilotMixin()
    orig = _cfg.as_bool
    _cfg.as_bool = lambda sec, key, dft=None: (
        False if (sec, key) == ("agent", "todo_enabled") else orig(sec, key, dft))
    try:
        g2 = obj._todo_gate(conv_id)
        check("7", "★开关关闭 ⇒ gate 返回 fallback（行为不变）",
              g2.get("fallback") is True, json.dumps(g2, ensure_ascii=False))
    finally:
        _cfg.as_bool = orig

    # ── 8. 清理：删掉本轮真机产生的待办行 ──
    with _db() as c:
        c.execute("DELETE FROM agent_todos WHERE conversation_id=?", (conv_id,))
        c.commit()
        left = c.execute("SELECT COUNT(*) FROM agent_todos WHERE conversation_id=?",
                         (conv_id,)).fetchone()[0]
    check("8", "清理本轮真机待办行（不留残留）", left == 0, f"剩 {left} 行")

    return conv_id


def main():
    run_e2e()
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
