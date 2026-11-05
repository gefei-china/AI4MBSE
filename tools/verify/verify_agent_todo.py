# -*- coding: utf-8 -*-
"""门禁：持续自主执行 —— todo 状态机 / 完成判定 / 障碍分类。

为什么要有这个门禁：
  本工程反复踩的坑是「验证器错了，产品是对的」（症状与"功能没写"完全一致）。
  所以本门禁必须**自己先被证伪过**：每个关键判据都配一条**变异自证**用例
  （把正确实现改坏 ⇒ 该用例必须翻红）。变异用例翻红 ⇒ 判据有鉴别力；
  变异用例仍绿 ⇒ 判据是假绿，必须改判据。

用法：
  python tools/verify/verify_agent_todo.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import sqlite3
import importlib.util

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, "agent", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


at = _load("agent_todo")

RESULTS = []


def check(group: str, name: str, cond: bool, detail: str = ""):
    RESULTS.append((group, name, bool(cond), detail))


# ══════════════ A. 状态机 ══════════════

def group_a():
    # A1 合法迁移
    it = at.TodoItem("校验包 P-001")
    try:
        at.transition(it, "in_progress")
        ok = it.status == "in_progress"
    except at.TransitionError:
        ok = False
    check("A", "pending→in_progress 合法", ok, f"status={it.status}")

    # A2 completed 必须先有 verify
    it2 = at.TodoItem("校验包 P-002")
    try:
        at.transition(it2, "completed")
        ok = False
        detail = "未抛异常（应拒绝）"
    except at.TransitionError as e:
        ok = True
        detail = str(e)
    check("A", "无 verify 置 completed 被拒", ok, detail)

    # A3 completed 必须有验收回执
    it3 = at.TodoItem("校验包 P-003", verify="sysmlv2_check")
    try:
        at.transition(it3, "completed")
        ok = False
        detail = "未抛异常（应拒绝）"
    except at.TransitionError as e:
        ok = True
        detail = str(e)
    check("A", "无 verify_result 置 completed 被拒", ok, detail)

    # A4 有 verify + verify_result ⇒ 允许
    it4 = at.TodoItem("校验包 P-004", verify="sysmlv2_check")
    try:
        at.transition(it4, "completed", verify_result="PASS: 0 errors")
        ok = it4.status == "completed"
    except at.TransitionError as e:
        ok = False
        detail = str(e)
    check("A", "verify+回执齐备可置 completed", ok, f"status={it4.status}")

    # A5 非法迁移被拒（pending→deleted 可行；deleted→completed 不可）
    it5 = at.TodoItem("撤销项")
    at.transition(it5, "deleted")
    try:
        at.transition(it5, "completed")
        ok = False
        detail = "未抛异常（应拒绝）"
    except at.TransitionError as e:
        ok = True
        detail = str(e)
    check("A", "deleted→completed 被拒", ok, detail)

    # A6 未知状态**必须抛**（不能静默）—— 我写错状态不能变成"静默无效果"
    it6 = at.TodoItem("未知态")
    raised = False
    try:
        at.transition(it6, "finished")
    except at.TransitionError:
        raised = True
    except Exception:
        raised = False
    check("A", "未知状态抛异常（不静默）", raised)

    # A7 blocked 必须说明原因
    it7 = at.TodoItem("受阻项")
    try:
        at.transition(it7, "blocked")
        ok = False
        detail = "未抛异常（应拒绝）"
    except at.TransitionError as e:
        ok = True
        detail = str(e)
    check("A", "blocked 无原因被拒", ok, detail)

    # A8 同态迁移幂等
    it8 = at.TodoItem("幂等项", status="pending")
    try:
        at.transition(it8, "pending")
        ok = it8.status == "pending"
    except at.TransitionError:
        ok = False
    check("A", "同态迁移幂等", ok)


# ══════════════ B. 完成判定（验收驱动）══════════════

def group_b():
    # B1 无 todo ⇒ 退回旧路径
    g = at.todo_gate([])
    check("B", "无 todo ⇒ fallback=True", g["fallback"] and not g["done"])

    # B2 全部完成且有回执 ⇒ done=True
    items = [
        at.TodoItem("A", status="completed", verify="v", verify_result="PASS"),
        at.TodoItem("B", status="completed", verify="v", verify_result="PASS"),
    ]
    g = at.todo_gate(items)
    check("B", "全 completed ⇒ done=True", g["done"] and not g["fallback"], g["reason"])

    # B3 ★ 核心用例：答复说「建议下一步执行校验」⇒ 必须 done=False
    #    这是实证里评估器判错的那类（3985 样本），todo_gate 应当强制拦住。
    items2 = [
        at.TodoItem("已给出建议", status="completed", verify="v", verify_result="PASS"),
        at.TodoItem("校验", status="pending", verify="sysmlv2_check"),
    ]
    g = at.todo_gate(items2)
    check("B", "★ 有 pending ⇒ 强制 done=False", not g["done"] and not g["fallback"])
    check("B", "next_step 指向第一个 pending", "校验" in g["next_step"], g["next_step"])

    # B4 deleted 不计入
    items3 = [
        at.TodoItem("保留项", status="completed", verify="v", verify_result="PASS"),
        at.TodoItem("误建项", status="deleted"),
    ]
    g = at.todo_gate(items3)
    check("B", "deleted 不计入未完成", g["done"], g["reason"])

    # B5 completed 但缺回执 ⇒ 不得算完成
    items4 = [at.TodoItem("自称完成但没验收", status="completed", verify="v",
                          verify_result="")]
    g = at.todo_gate(items4)
    check("B", "completed 缺回执 ⇒ 不算完成", not g["done"])

    # B6 有 todo 清单但全被删 ⇒ 未完成（不是"没 todo"）
    g = at.todo_gate([at.TodoItem("x", status="deleted")])
    check("B", "清单全删 ⇒ done=False 且非 fallback",
          (not g["done"]) and (not g["fallback"]), g["reason"])

    # B7 has_todos 显式为 True 但清单空 ⇒ 仍非 fallback
    g = at.todo_gate([], has_todos=True)
    check("B", "has_todos=True 空清单 ⇒ 非 fallback", not g["fallback"])

    # B8 dict 形态入参（DB 行直出）能正常判定
    g = at.todo_gate([{"subject": "D", "status": "pending", "verify": "v"}])
    check("B", "dict 形态入参可判定", not g["done"] and not g["fallback"])

    # B9 remaining 只列未完成项
    items5 = [
        at.TodoItem("ok", status="completed", verify="v", verify_result="PASS"),
        at.TodoItem("todo1", status="pending"),
        at.TodoItem("todo2", status="blocked"),
    ]
    g = at.todo_gate(items5)
    check("B", "remaining 只列未完成", g["remaining"] == ["todo1", "todo2"],
          str(g["remaining"]))


# ══════════════ C. 障碍分类 ══════════════

def group_c():
    cases = [
        ("HTTP 500 内部错误", "transient"),
        ("request timeout 超时", "transient"),
        ("401 Unauthorized 凭据失效", "auth"),
        ("STALE_READ 凭据过期", "auth"),
        ("404 not found 包不存在", "not_found"),
        ("找不到 ID 无效", "not_found"),
        ("version mismatch 版本过期", "precondition"),
        ("父容器已变", "precondition"),
        ("not supported 不支持该操作", "capability"),
        ("工具不支持", "capability"),
    ]
    for text, want in cases:
        got = at.classify_obstacle(text)
        check("C", f"「{text}」⇒ {want}", got["kind"] == want,
              f"实得 {got['kind']}")

    # C11 只有 user_only 允许问人
    for kind in ("transient", "auth", "not_found", "precondition", "capability"):
        got = at.classify_obstacle({
            "transient": "timeout", "auth": "401", "not_found": "404",
            "precondition": "version mismatch", "capability": "not supported",
        }[kind])
        check("C", f"{kind} 不允许问人", not got["ask_user_allowed"])

    # C12 未识别特征 ⇒ 保守 transient（不误判成 user_only）
    got = at.classify_obstacle("某个从没见过的怪错误 xyzzy")
    check("C", "未识别 ⇒ transient 而非 user_only",
          got["kind"] == "transient" and not got["ask_user_allowed"])

    # C13 空文本不崩
    got = at.classify_obstacle("")
    check("C", "空错误文本不崩", got["kind"] in at.OBSTACLE_KINDS)

    # C14 action 映射齐全
    check("C", "OBSTACLE_KINDS 与 RECOVERY_ACTIONS 一一对应",
          set(at.OBSTACLE_KINDS) == set(at.RECOVERY_ACTIONS))


# ══════════════ D. DDL 真跑 ══════════════

def group_d():
    DDL = """
    CREATE TABLE IF NOT EXISTS agent_todos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        run_id INTEGER DEFAULT 0,
        seq INTEGER,
        subject TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        verify TEXT DEFAULT '',
        verify_result TEXT DEFAULT '',
        blocked_by TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """
    try:
        c = sqlite3.connect(":memory:")
        c.execute(DDL)
        c.execute(DDL)                       # 幂等
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_todos)").fetchall()]
        need = ["conversation_id", "status", "verify", "verify_result",
                "blocked_by", "subject"]
        check("D", "建表 SQL 真跑通过 + 幂等", all(n in cols for n in need), str(cols))
        c.execute("INSERT INTO agent_todos (conversation_id, subject) VALUES (?,?)",
                  (1, "测试项"))
        n = c.execute("SELECT COUNT(*) FROM agent_todos").fetchone()[0]
        check("D", "插入可用", n == 1, f"count={n}")
        c.close()
    except Exception as e:
        check("D", "建表 SQL 真跑通过 + 幂等", False, repr(e))

    # D3 列迁移幂等（模拟 database/migrations/columns.py 的 _add）
    def _add(conn, table, column, ddl):
        ex = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (table,)).fetchone()
        if not ex:
            return False
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            return True
        return False

    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE agent_todos (id INTEGER PRIMARY KEY, subject TEXT)")
        r1 = _add(c, "agent_todos", "blocked_by", "TEXT DEFAULT ''")
        r2 = _add(c, "agent_todos", "blocked_by", "TEXT DEFAULT ''")
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_todos)").fetchall()]
        check("D", "列迁移幂等（第2遍不重复加列）",
              r1 is True and r2 is False and cols.count("blocked_by") == 1,
              f"r1={r1} r2={r2} cols={cols}")
        c.close()
    except Exception as e:
        check("D", "列迁移幂等（第2遍不重复加列）", False, repr(e))


# ══════════════ E. 变异自证（判据必须有鉴别力）══════════════

def group_e():
    """把正确实现改坏 ⇒ **同一判据**必须翻红。变异仍绿 ⇒ 判据假绿。

    ⚠️ 判据抽成 lambda：正确实现上求值为 True，变异实现上必须求值为 False。
       （第一版把断言写成「变异后仍通过」⇒ 变异必然让断言变红 ⇒ 自证形同虚设。
        这是「验证器错 ≠ 功能没写」的老坑，必须靠**同源判据**消掉。）
    """
    # E1 判据：completed 必须有 verify_result
    def c_a3(trans) -> bool:
        it = at.TodoItem("x", verify="v")
        try:
            trans(it, "completed")
            return False          # 未拒绝 ⇒ 判据假
        except at.TransitionError:
            return True

    def bad_transition(item, to_status, **kw):
        for k, v in kw.items():
            setattr(item, k, v)
        item.status = to_status
        return item

    check("E", "★变异自证：去掉回执约束 ⇒ A3 判据翻红",
          c_a3(at.transition) and not c_a3(bad_transition),
          f"真实现={c_a3(at.transition)} 变异实现={c_a3(bad_transition)}")

    # E2 判据：有 pending ⇒ done=False（核心用例 B3）
    def c_b3(gate) -> bool:
        items = [at.TodoItem("A", status="completed", verify="v",
                             verify_result="PASS"),
                 at.TodoItem("校验", status="pending", verify="sysmlv2_check")]
        g = gate(items)
        return (not g["done"]) and (not g["fallback"])

    def bad_gate(items, **kw):
        return {"done": True, "next_step": "", "remaining": [],
                "fallback": False, "reason": "变异"}

    check("E", "★变异自证：todo_gate 无条件完成 ⇒ B3 判据翻红",
          c_b3(at.todo_gate) and not c_b3(bad_gate),
          f"真实现={c_b3(at.todo_gate)} 变异实现={c_b3(bad_gate)}")

    # E3 判据：未识别错误 ⇒ transient 且不允许问人（C12）
    def c_c12(classify) -> bool:
        g = classify("某个从没见过的怪错误 xyzzy")
        return g["kind"] == "transient" and not g["ask_user_allowed"]

    def bad_classify(t):
        return {"kind": "user_only", "action": "ask_user",
                "ask_user_allowed": True, "matched": ""}

    check("E", "★变异自证：恒判 user_only ⇒ C12 判据翻红",
          c_c12(at.classify_obstacle) and not c_c12(bad_classify),
          f"真实现={c_c12(at.classify_obstacle)} 变异实现={c_c12(bad_classify)}")

    # E4 反向自证：真实现本身必须通过对应判据（防"判据恒红"）
    def c_a2(trans) -> bool:
        it = at.TodoItem("x")
        try:
            trans(it, "completed")
            return False
        except at.TransitionError:
            return True

    check("E", "反向自证：真实现拒绝无 verify 置完成", c_a2(at.transition))


# ══════════════ F. 与既有模块一致性 ══════════════

def group_f():
    # F1 新模块不得引入 IO 依赖（纯函数约束）
    src = open(os.path.join(_ROOT, "agent", "agent_todo.py"),
               encoding="utf-8").read()
    banned = ["sqlite3", "requests", "httpx", "urllib.request", "open("]
    hits = [b for b in banned if b in src]
    # 'open(' 允许出现在 __all__ 之外？本文件不应有文件 IO —— 一律不允许
    check("F", "纯函数层无 IO 依赖", not hits, f"命中 {hits}")

    # F2 todo_gate 与旧路径并存：fallback 分支必须存在且可被触发
    check("F", "fallback 分支存在（兼容旧路径）",
          "fallback" in at.__all__ or hasattr(at, "todo_gate"))
    g = at.todo_gate([])
    check("F", "fallback 可触发（无 todo ⇒ 旧路径）", g["fallback"] is True)

    # F3 状态集合与文档一致
    check("F", "TODO_STATUSES = 官方四态 + blocked/deleted",
          set(at.TODO_STATUSES) == {"pending", "in_progress", "completed",
                                    "blocked", "deleted"},
          str(at.TODO_STATUSES))


def main():
    for fn in (group_a, group_b, group_c, group_d, group_e, group_f):
        fn()
    print("=" * 74)
    print("门禁：持续自主执行（agent_todo）")
    print("=" * 74)
    groups = {}
    for g, n, ok, d in RESULTS:
        groups.setdefault(g, []).append((n, ok, d))
    total = 0
    failed = 0
    for g in sorted(groups):
        print(f"\n【{g} 组】")
        for n, ok, d in groups[g]:
            total += 1
            mark = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
            line = f"  [{mark}] {n}"
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
    sys.exit(main())
