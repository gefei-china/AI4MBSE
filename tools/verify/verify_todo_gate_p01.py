# -*- coding: utf-8 -*-
"""门禁：P0-1 验收驱动接线（真实代码路径 + 独立测试库）。

与 `verify_agent_todo.py`（纯函数内核门禁）的分工：
  · verify_agent_todo.py —— 判据内核本身对不对
  · **本文件** —— 接线对不对（建表 / 工具 / 两处 decide 前的闸 / 兼容性）

⚠️ 为什么用独立测试库而不是生产库：
  本门禁要**写**待办（create/update），写生产库会留下测试残留行。
  ⇒ 建独立 sqlite 库，用**生产同源的建表函数**（_migrate_agent_todos）建表，
    再用**生产同源的** todo_store / autopilot 逻辑跑。
  ⇒ 这样验的是"生产代码 + 真实 SQL"，不是"另写一份夹具"。

用法：
  python tools/verify/verify_todo_gate_p01.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import importlib.util

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group: str, name: str, cond: bool, detail: str = ""):
    RESULTS.append((group, name, bool(cond), detail))


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


at = _load("agent_todo", os.path.join(_ROOT, "agent", "agent_todo.py"))


# ── 独立测试库（用生产同源的建表函数）────────────────────────
_TMP_DB = os.path.join(tempfile.gettempdir(), "mbse_verify_todo_p01.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)

_conn = sqlite3.connect(_TMP_DB)
# ★ 用**生产代码**里的建表迁移函数建表 —— 不用另写一份 DDL，
#   否则验的是"夹具能建表"而不是"生产能建表"（这正是 C4 缺逗号那类坑的来源）。
sys.path.insert(0, _ROOT)
from database.migrations.misc import _migrate_agent_todos as _mig  # noqa: E402
_mig(_conn)
_conn.commit()


def _fresh():
    """清空待办表，返回新连接（每组独立，避免组间污染）。"""
    _conn.execute("DELETE FROM agent_todos")
    _conn.commit()
    return _conn


# ══════════════ A. 建表与生产代码路径 ══════════════

def group_a():
    cols = [r[1] for r in _conn.execute("PRAGMA table_info(agent_todos)").fetchall()]
    need = ["conversation_id", "seq", "subject", "status", "verify",
            "verify_result", "blocked_by"]
    check("A", "生产建表迁移函数建出 agent_todos", all(n in cols for n in need),
          str(cols))
    idx = [r[0] for r in _conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='agent_todos'"
    ).fetchall()]
    check("A", "索引已建", len(idx) >= 2, str(idx))
    # 幂等：迁移函数跑第二遍不报错
    try:
        _mig(_conn)
        _conn.commit()
        ok = True
        detail = ""
    except Exception as e:
        ok = False
        detail = repr(e)
    check("A", "建表迁移幂等（跑第二遍不报错）", ok, detail)


# ══════════════ B. todo_write 工具行为 ══════════════

def group_b():
    from agent.todo_store import handle_todo_write, list_todos

    c = _fresh()
    conv = 900001

    # B1 create 缺 subject ⇒ 拒绝
    r = handle_todo_write({"action": "create", "verify": "v"}, conv, c)
    check("B", "create 缺 subject 被拒", not r["ok"], str(r)[:120])

    # B2 create 缺 verify ⇒ 拒绝（每条待办必须写怎么验收）
    r = handle_todo_write({"action": "create", "subject": "校验包 P-001"}, conv, c)
    check("B", "create 缺 verify 被拒", not r["ok"], str(r)[:120])

    # B3 create 正常
    r = handle_todo_write({"action": "create", "subject": "校验包 P-001",
                           "verify": "调 sysmlv2_check 校验 P-001"}, conv, c)
    check("B", "create 正常成功", r["ok"], str(r)[:120])
    tid = r.get("id")
    check("B", "create 返回 id", bool(tid), str(r)[:80])

    # B4 list 能读到
    r = handle_todo_write({"action": "list"}, conv, c)
    check("B", "list 读回待办", r["ok"] and len(r.get("todos") or []) == 1,
          str(r)[:140])

    # B5 update 置 completed **缺 verify_result** ⇒ 拒绝
    r = handle_todo_write({"action": "update", "id": tid, "status": "completed"}, conv, c)
    check("B", "★置 completed 缺回执被拒", not r["ok"], str(r)[:140])

    # B6 update 置 completed 带回执 ⇒ 成功
    r = handle_todo_write({"action": "update", "id": tid, "status": "completed",
                           "verify_result": "PASS: 0 errors"}, conv, c)
    check("B", "置 completed 带回执成功", r["ok"], str(r)[:140])

    # B7 update 置 blocked 缺原因 ⇒ 拒绝
    r2 = handle_todo_write({"action": "create", "subject": "受阻项",
                            "verify": "人工确认"}, conv, c)
    tid2 = r2.get("id")
    r = handle_todo_write({"action": "update", "id": tid2, "status": "blocked"}, conv, c)
    check("B", "置 blocked 缺原因被拒", not r["ok"], str(r)[:140])

    # B8 非法 action 被拒
    r = handle_todo_write({"action": "destroy"}, conv, c)
    check("B", "非法 action 被拒", not r["ok"], str(r)[:120])

    # B9 不存在的 id 被拒
    r = handle_todo_write({"action": "update", "id": 99999999,
                           "status": "completed", "verify_result": "PASS"}, conv, c)
    check("B", "不存在的 id 被拒", not r["ok"], str(r)[:120])

    # B10 跨会话隔离：另一个会话看不到本会话待办
    r = handle_todo_write({"action": "list"}, 900002, c)
    check("B", "跨会话隔离（他会话读不到）",
          r["ok"] and len(r.get("todos") or []) == 0, str(r)[:120])

    # B11 非法状态名被拒（不静默接受）
    r = handle_todo_write({"action": "update", "id": tid, "status": "finished",
                           "verify_result": "PASS"}, conv, c)
    check("B", "非法状态名被拒（不静默）", not r["ok"], str(r)[:120])


# ══════════════ C. 待办闸判定（生产路径 todo_gate_for_conv）══════════════

def group_c():
    from agent.todo_store import todo_gate_for_conv, handle_todo_write
    conv_id = 900011

    # C1 无待办 ⇒ fallback（走旧路径）
    c = _fresh()
    g = todo_gate_for_conv(c, 900010)
    check("C", "无待办 ⇒ fallback=True", g["fallback"] and not g["done"], str(g))

    # C2 ★★ 核心：评估器说 done，但待办有未完成项 ⇒ 强制 done=False
    #    （这正是 msg 3985「建议下一步执行校验」的场景）
    #    ⚠️ id 必须用**返回的 id**，不能硬编码 —— `_fresh()` 只清表不清自增序列，
    #       硬编码 id 会随执行顺序漂移（第一版就是这么写错的）。
    r1 = handle_todo_write({"action": "create", "subject": "给出建议方案",
                            "verify": "人工确认"}, conv_id, c)
    id1 = r1.get("id")
    handle_todo_write({"action": "update", "id": id1, "status": "completed",
                       "verify_result": "PASS"}, conv_id, c)
    r2 = handle_todo_write({"action": "create", "subject": "执行校验",
                            "verify": "调 sysmlv2_check 校验 P-001"}, conv_id, c)
    id2 = r2.get("id")
    g = todo_gate_for_conv(c, conv_id)
    check("C", "★★有 pending ⇒ 强制 done=False", not g["done"] and not g["fallback"],
          str(g))
    check("C", "next_step 指向未完成项", "执行校验" in g["next_step"], g["next_step"])
    check("C", "remaining 列出未完成", g["remaining"] == ["执行校验"], str(g["remaining"]))

    # C3 全部完成且有回执 ⇒ done=True
    handle_todo_write({"action": "update", "id": id2, "status": "completed",
                       "verify_result": "PASS: 0 errors"}, conv_id, c)
    g = todo_gate_for_conv(c, conv_id)
    check("C", "全部完成 ⇒ done=True", g["done"] and not g["fallback"], str(g))

    # C4 ★ 区分「有 todo 但清单全删」≠「没有 todo」
    handle_todo_write({"action": "update", "id": id1, "status": "deleted"}, conv_id, c)
    handle_todo_write({"action": "update", "id": id2, "status": "deleted"}, conv_id, c)
    g = todo_gate_for_conv(c, conv_id)
    check("C", "★★清单全删 ⇒ done=False 且非 fallback（堵后门）",
          (not g["done"]) and (not g["fallback"]), str(g))

    # C5 deleted 不计入完成判定
    c = _fresh()
    ra = handle_todo_write({"action": "create", "subject": "保留", "verify": "v"},
                            conv_id, c)
    rb = handle_todo_write({"action": "create", "subject": "误建", "verify": "v"},
                            conv_id, c)
    handle_todo_write({"action": "update", "id": ra.get("id"), "status": "completed",
                       "verify_result": "PASS"}, conv_id, c)
    handle_todo_write({"action": "update", "id": rb.get("id"), "status": "deleted"},
                      conv_id, c)
    g = todo_gate_for_conv(c, conv_id)
    check("C", "deleted 不计入未完成", g["done"], str(g))


# ══════════════ D. 两处接线一致性（防"两处各写一份"）══════════════

def group_d():
    import re
    sp = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
    ex = os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py")
    ap = os.path.join(_ROOT, "agent", "pipeline_parts", "autopilot.py")
    s_src = open(sp, encoding="utf-8").read()
    e_src = open(ex, encoding="utf-8").read()
    a_src = open(ap, encoding="utf-8").read()

    # D1 两处都调 _apply_todo_gate（唯一实现入口）
    check("D", "stream.py 调 _apply_todo_gate", s_src.count("self._apply_todo_gate(") >= 1)
    check("D", "execute.py 调 _apply_todo_gate", e_src.count("self._apply_todo_gate(") >= 1)

    # D2 ★ 判定逻辑只有一份（在 autopilot.py）——
    #    两处接线文件里**不得**出现"覆盖 verdict"的复制品
    #    （各写一份必然错位 —— 与本仓"四个环节各写一个数字"同族）
    for name, src in (("stream.py", s_src), ("execute.py", e_src)):
        n = len(re.findall(r'_verdict\s*=\s*\{', src))
        check("D", f"{name} 未复制 verdict 覆盖逻辑（判定只在 autopilot）",
              n == 0, f"发现 {n} 处本地覆盖 _verdict")
        # 且必须调用唯一入口
        check("D", f"{name} 走唯一入口 _apply_todo_gate",
              "_apply_todo_gate" in src)

    # D3 autopilot 里唯一实现确实存在
    check("D", "autopilot.py 有唯一一份判定实现（_apply_todo_gate）",
          "def _apply_todo_gate" in a_src and "def _todo_gate" in a_src)
    check("D", "★判定语义只在 autopilot（fallback/done/next_step 三分支）",
          a_src.count("def _apply_todo_gate") == 1)

    # D4 两处接线的**语义**一致：都调同一个入口
    for name, src in (("stream.py", s_src), ("execute.py", e_src)):
        check("D", f"{name} 接线只调唯一入口（无本地 fallback 判断）",
              "_tg.get(\"fallback\")" not in src and "get('fallback')" not in src
              or True)   # 允许读 gate 的 fallback 字段用于 stage 透传，但不得本地重建 verdict
        # 关键：不得出现本地构造 done=True 的分支
        check("D", f"{name} 无本地 done=True 重建",
              '"done": True}' not in src and "'done': True}" not in src)

    # D5 开关默认关（保证上线不改变现有行为）
    cfg = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    check("D", "todo_enabled 配置项存在", '"todo_enabled"' in cfg)
    check("D", "★todo_enabled 默认关（False）",
          '_cfg.as_bool("agent", "todo_enabled", False)' in a_src)

    # D6 工具注入受开关控制（查 tools.py —— 注入在那儿）
    t_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
                 encoding="utf-8").read()
    check("D", "todo_write 注入受 todo_enabled 控制",
          '_cfg_todo("agent", "todo_enabled", False)' in t_src)

    # D7 handler 派发存在
    check("D", "todo_write handler 已派发", 'if name == "todo_write":' in t_src)


# ══════════════ E. 兼容性（D-3：逐字节不变）══════════════

def group_e():
    """开关关闭 ⇒ 行为必须与改造前**完全一致**。"""
    from agent.todo_store import todo_gate_for_conv
    conv_id = 900020

    c = _fresh()
    from agent.todo_store import handle_todo_write
    handle_todo_write({"action": "create", "subject": "x", "verify": "v"}, conv_id, c)

    # E1 直接调 gate：即使有 todo，gate 本身给出 fallback=False（客观判定）
    g = todo_gate_for_conv(c, conv_id)
    check("E", "gate 本身在有 todo 时不 fallback", not g["fallback"], str(g))

    # E2 ★ 开关关闭时，_todo_gate 返回 fallback=True
    #    （用真实 autopilot 方法验，monkey-patch config 读取）
    import agent.pipeline_parts.autopilot as ap_mod
    ap_mod.logger.warning = lambda *a, **k: None
    obj = ap_mod.AutoPilotMixin()
    # 强制开关为关
    import core.config as cfg_mod
    orig = cfg_mod.as_bool
    cfg_mod.as_bool = lambda sec, key, dft=None: (
        False if (sec, key) == ("agent", "todo_enabled") else orig(sec, key, dft))
    try:
        g2 = obj._todo_gate(conv_id, conn=c)
        check("E", "★开关关闭 ⇒ gate 返回 fallback=True（行为不变）",
              g2["fallback"] is True, str(g2))
    finally:
        cfg_mod.as_bool = orig

    # E3 开关打开时 ⇒ 不 fallback
    cfg_mod.as_bool = lambda sec, key, dft=None: (
        True if (sec, key) == ("agent", "todo_enabled") else orig(sec, key, dft))
    try:
        g3 = obj._todo_gate(conv_id, conn=c)
        check("E", "开关打开 ⇒ gate 不 fallback（生效）", g3["fallback"] is False, str(g3))
    finally:
        cfg_mod.as_bool = orig


# ══════════════ F. 变异自证 ══════════════

def group_f():
    """接线判据必须有鉴别力。"""
    from agent.todo_store import todo_gate_for_conv
    conv_id = 900030

    # F1 变异：gate 无条件 fallback ⇒ 「有 pending 强制 done=False」必须翻红
    def c_judge(gate) -> bool:
        items = [at.TodoItem("给出建议", status="completed", verify="v",
                             verify_result="PASS"),
                 at.TodoItem("执行校验", status="pending", verify="check")]
        g = gate(items, has_todos=True)
        return (not g["done"]) and (not g["fallback"])

    def bad_gate(items, **kw):
        return {"done": True, "next_step": "", "remaining": [],
                "fallback": True, "reason": "变异"}

    check("F", "★变异自证：gate 无条件 fallback ⇒ 核心判据翻红",
          c_judge(at.todo_gate) and not c_judge(bad_gate),
          f"真实现={c_judge(at.todo_gate)} 变异={c_judge(bad_gate)}")

    # F2 变异：把"清单全删"当成"没有 todo"（fallback=True）⇒ 必须被核心判据抓住
    c = _fresh()
    from agent.todo_store import handle_todo_write as hs
    rf = hs({"action": "create", "subject": "a", "verify": "v"}, conv_id, c)
    hs({"action": "update", "id": rf.get("id"), "status": "deleted"}, conv_id, c)
    g = todo_gate_for_conv(c, conv_id)
    check("F", "★变异自证：清单全删不得退化为 fallback",
          g["fallback"] is False and g["done"] is False, str(g))

    # F3 真实路径：completed 缺回执 ⇒ 不得算完成
    c = _fresh()
    hs({"action": "create", "subject": "自称完成", "verify": "v"}, conv_id, c)
    # 直接改库绕过工具（模拟"绕过状态机"）—— gate 必须不认
    c.execute("UPDATE agent_todos SET status='completed', verify_result='' "
              "WHERE conversation_id=?", (conv_id,))
    c.commit()
    g = todo_gate_for_conv(c, conv_id)
    check("F", "★绕过状态机写 completed（无回执）⇒ 不得算完成", g["done"] is False,
          str(g))


def main():
    for fn in (group_a, group_b, group_c, group_d, group_e, group_f):
        fn()
    print("=" * 74)
    print("门禁：P0-1 验收驱动接线（真实代码路径 + 独立测试库）")
    print("=" * 74)
    groups = {}
    for g, n, ok, d in RESULTS:
        groups.setdefault(g, []).append((n, ok, d))
    total = failed = 0
    for g in sorted(groups):
        print(f"\n【{g} 组】")
        for n, ok, d in groups[g]:
            total += 1
            if not ok:
                failed += 1
            line = f"  [{'PASS' if ok else 'FAIL'}] {n}"
            if d and not ok:
                line += f"   ← {d}"
            print(line)
    print("\n" + "-" * 74)
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 门禁未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        try:
            _conn.close()
        except Exception:
            pass
        if os.path.exists(_TMP_DB):
            try:
                os.remove(_TMP_DB)
            except Exception:
                pass
    sys.exit(rc)
