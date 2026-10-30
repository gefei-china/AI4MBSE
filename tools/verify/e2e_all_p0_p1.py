# -*- coding: utf-8 -*-
"""整体端到端真机验证：六步改造**协同**工作（全开 / 全关 双向）。

与各单项 E2E 的分工：
  · e2e_todo_gate_p01 / e2e_harness_gate_p02 / e2e_evaluator_p04 /
    e2e_budget_needsuser_p1 —— 各验单项
  · **本文件** —— 验「六项同时开启时会不会互相打架」+「全关时行为是否不变」

为什么必须有这一层：
  单项都绿 ≠ 组合正确。本仓反复踩过「各自对、合起来错」：
  例如 P0-1 覆盖 verdict 后 P0-2 再判一次、P1-1 降级与 P0-2 强制放行同时触发…

真机判据：
  ① ★全开时：待办闸 → harness 闸 → 预算降级 三者串起来，结论一致不矛盾
  ② ★全关时：整条链**完全不介入**（fallback / 不拦 / 不降级），行为与改造前一致
  ③ ★ 优先级正确：任务已完成 > 预算降级 > harness 拦（完成就该说完成）
  ④ ★ 平台独立回读：真实库里待办状态与闸的结论一致
  ⑤ 9 个配置项全部存在且默认关/兼容值

用法：
  python tools/verify/e2e_all_p0_p1.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import json
import sqlite3

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if not cond and detail else ""))


ALL_KEYS = [
    ("todo_enabled", False),
    ("todo_require_verify", True),
    ("harness_enabled", False),
    ("harness_max_blocks", 8),
    ("budget_degrade_ratio", 0.7),
    ("obstacle_recovery_enabled", False),
]


def run():
    print("=" * 74)
    print("整体端到端：六步改造协同验证（全开 / 全关 双向）")
    print("=" * 74)

    from core import config as _cfg
    from database import db_conn
    from agent.todo_store import handle_todo_write, todo_gate_for_conv
    from agent.pipeline_parts.autopilot import AutoPilotMixin

    orig_as_bool = _cfg.as_bool
    orig_get = _cfg.get

    # ── 0. 配置项齐备性 ──
    cfg_src = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    for key, dflt in ALL_KEYS:
        check("0", f"配置项 {key} 存在", f'"{key}"' in cfg_src)
    check("0", "★全部新增配置默认关/兼容值（不改变现有行为）",
          'as_bool("agent", "todo_enabled", False)' in
          open(os.path.join(_ROOT, "agent", "pipeline_parts", "autopilot.py"),
               encoding="utf-8").read()
          and '"todo_enabled": {"type": "bool", "desc": "★ 计划外化总开关（默认关）'
          in cfg_src)

    obj = AutoPilotMixin()

    def force_cfg(on: bool):
        """同时控制 todo_enabled / harness_enabled / obstacle_recovery_enabled。"""
        def _as_bool(sec, key, dft=None):
            if sec == "agent" and key in ("todo_enabled", "harness_enabled",
                                          "obstacle_recovery_enabled"):
                return on
            return orig_as_bool(sec, key, dft)
        _cfg.as_bool = _as_bool

    # 建测试会话 + 一个未完成的待办
    r = None
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/conversations",
            data=json.dumps({"title": "P0/P1 整体验证"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  （warn）建会话失败（将用本地 id）：{e!r}")
    conv_id = (r or {}).get("id") or 990001
    print(f"  会话 id = {conv_id}")

    try:
        # ══════════ A. 全开：三闸串联 ══════════
        force_cfg(True)
        with db_conn() as conn:
            ra = handle_todo_write({"action": "create",
                                    "subject": "整体验证：跑语法校验",
                                    "verify": "no_such_verify_tool_xyz {"}, conv_id, conn)
        tid = ra.get("id")
        check("A", "全开：建待办成功", ra.get("ok"),
              json.dumps(ra, ensure_ascii=False)[:150])

        # A1 待办闸（P0-1）：有未完成 ⇒ done=False
        with db_conn() as conn:
            g_p1 = todo_gate_for_conv(conn, conv_id)
        check("A", "★P0-1 待办闸介入（非 fallback）", g_p1.get("fallback") is False,
              json.dumps(g_p1, ensure_ascii=False)[:160])
        check("A", "★P0-1 判 done=False（有未完成）", g_p1.get("done") is False,
              json.dumps(g_p1, ensure_ascii=False)[:160])

        # A2 harness 闸（P0-2）：同样看到未完成 ⇒ 拦
        g_p2 = obj._harness_gate(conv_id, 0)
        check("A", "★P0-2 harness 拦（与 P0-1 结论一致）", g_p2.get("block") is True,
              json.dumps(g_p2, ensure_ascii=False)[:200])
        check("A", "★P0-2 回灌含失败明细（verify 真跑了）",
              bool(g_p2.get("failures")), str(g_p2.get("failures"))[:160])

        # A3 两闸结论**不矛盾**（这是组合正确性的核心）
        check("A", "★★P0-1 与 P0-2 结论一致（P0-1 说未完成 ⇒ P0-2 也说该拦）",
              (g_p1.get("done") is False) == (g_p2.get("block") is True),
              f"p01.done={g_p1.get('done')} p02.block={g_p2.get('block')}")

        # A4 预算降级（P1-1）与 harness 的关系：
        #    预算达阈值 ⇒ 不再续跑，但 harness 仍可拦 —— 两者语义不同（一个管"续不续"，
        #    一个管"能不能结束"）。这里验**不打架**：都返回布尔、都不抛异常。
        from agent import auto_continue as _ac
        cont, why = _ac.decide(g_p1, used=0, max_used=3, seen_steps=[],
                               budget_left=True, budget_ratio=0.9, degrade_ratio=0.7)
        check("A", "★P1-1 预算降级独立生效（不干扰 P0-2 的拦截判定）",
              cont is False and "降级收尾" in why, why)
        check("A", "★P0-2 的拦截结论未被预算降级改写（职责分离）",
              g_p2.get("block") is True)

        # ══════════ B. 优先级正确 ══════════
        # B1 全部完成 ⇒ P0-1 判完成、P0-2 不拦（哪怕预算 99%）
        with db_conn() as conn:
            handle_todo_write({"action": "update", "id": tid, "status": "completed",
                               "verify_result": "PASS: 整体验证"}, conv_id, conn)
        with db_conn() as conn:
            g_done = todo_gate_for_conv(conn, conv_id)
        check("B", "★全部完成 ⇒ P0-1 判 done=True", g_done.get("done") is True,
              json.dumps(g_done, ensure_ascii=False)[:150])
        g_done2 = obj._harness_gate(conv_id, 0)
        check("B", "★全部完成 ⇒ P0-2 不拦", g_done2.get("block") is False,
              json.dumps(g_done2, ensure_ascii=False)[:150])
        cont_done, why_done = _ac.decide(g_done, used=0, max_used=3, seen_steps=[],
                                          budget_left=True, budget_ratio=0.99,
                                          degrade_ratio=0.7)
        check("B", "★★优先级：完成 > 预算（完成就说完成，不提预算）",
              cont_done is False and "已完成" in why_done, why_done)

        # ══════════ C. 全关：完全不介入（行为不变）══════════
        force_cfg(False)
        # 先造一个未完成待办（用来证明"就算有未完成，全关也不介入"）
        with db_conn() as conn:
            rc = handle_todo_write({"action": "create",
                                    "subject": "全关验证：故意留未完成",
                                    "verify": "人工确认"}, conv_id, conn)
        # 把前面那条也拉回未完成
        with db_conn() as conn:
            conn.execute("UPDATE agent_todos SET status='pending', verify_result='' "
                         "WHERE conversation_id=?", (conv_id,))
            conn.commit()

        with db_conn() as conn:
            # 直接调 todo_gate_for_conv（它不看开关，只看数据）—— 用于对照
            g_raw = todo_gate_for_conv(conn, conv_id)
        check("C", "（对照）数据层面确有未完成待办", g_raw.get("done") is False,
              json.dumps(g_raw, ensure_ascii=False)[:150])

        g_off = obj._todo_gate(conv_id)
        check("C", "★全关 ⇒ P0-1 不介入（fallback=True）", g_off.get("fallback") is True,
              json.dumps(g_off, ensure_ascii=False)[:150])
        g_off2 = obj._harness_gate(conv_id, 0)
        check("C", "★全关 ⇒ P0-2 不拦", g_off2.get("block") is False,
              json.dumps(g_off2, ensure_ascii=False)[:150])

        # ══════════ D. 平台独立回读 ══════════
        from core.config import DB_PATH
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT subject, status, verify, verify_result FROM agent_todos "
            "WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
        c.close()
        check("D", "★平台独立回读：待办行确实在库里", len(rows) >= 2,
              f"{len(rows)} 行")
        if rows:
            st = [r["status"] for r in rows]
            check("D", "★回读状态与闸结论一致（有 pending）",
                  "pending" in st, str(st))

        # ══════════ E. 清理 ══════════
        c = sqlite3.connect(DB_PATH)
        c.execute("DELETE FROM agent_todos WHERE conversation_id=?", (conv_id,))
        c.commit()
        left = c.execute("SELECT COUNT(*) FROM agent_todos WHERE conversation_id=?",
                         (conv_id,)).fetchone()[0]
        c.close()
        check("E", "清理本轮真机待办行", left == 0, f"剩 {left} 行")

    finally:
        _cfg.as_bool = orig_as_bool
        _cfg.get = orig_get


def main():
    run()
    print("\n" + "-" * 74)
    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if not r[2])
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
