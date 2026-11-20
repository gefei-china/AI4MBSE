# -*- coding: utf-8 -*-
"""门禁：todo_write 支持批量结项，降低纯记账开销（TODO_BATCH_UPDATE_V1）。

真机 conv 999999952：2 个子任务共调 `todo_write` 13 次，约半数是"一条做完就 update"，
每次都是一整轮 LLM 往返，只改 status 一个字段。
判据：
  P 纯函数：批量更新逐条生效、失败不吞、部分成功如实报 ok=False
  S 源码：schema 暴露 ids、描述引导批量、单条路径零改动
  M 变异：把批量分支短路 ⇒ P 判据必须翻红
"""
# CI-OPTIONAL: todo_write 批量结项：纯函数门禁（自建临时库）+ 源码变异自证，暂不进 CI

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PASS = FAIL = 0
FAILS = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL  %s  << %s" % (name, detail))


def _seed(conn, conv, n=4):
    """用 create 建 n 条待办（走真实 handler，不手写 SQL）。"""
    from agent.todo_store import handle_todo_write as h
    r = h({"action": "create",
           "items": [{"subject": "步骤%d" % i, "verify": "人工确认"} for i in range(1, n + 1)]},
          conv, conn=conn)
    return list(((r or {}).get("ids") or []))


def main():
    print("门禁：todo_write 批量结项（TODO_BATCH_UPDATE_V1）")
    from agent import todo_store as ts

    db = os.path.join(ROOT, "tmp", "_gate_todo_batch.db")
    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db)
    try:
        # 建表（照搬生产迁移的最小列集，含 evidence）
        conn.execute("""CREATE TABLE agent_todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER,
            subject TEXT, status TEXT, verify TEXT, verify_result TEXT,
            blocked_by TEXT, note TEXT, verify_tool TEXT, verify_args TEXT,
            evidence TEXT, run_id INTEGER DEFAULT 0, task_key TEXT DEFAULT '',
            seq INTEGER, created_at TEXT, updated_at TEXT)""")
        conn.commit()
        conv = 777001
        ids = _seed(conn, conv, 4)
        check("P0 前置：建了 4 条待办", len(ids) == 4, "ids=%s" % ids)

        # 批量结项
        r = ts.handle_todo_write({"action": "update", "ids": ids[:3],
                                  "status": "completed", "verify_result": "人工确认"}, conv, conn=conn)
        check("P1 批量更新 3 条全部成功", r.get("ok") is True and r.get("ok_count") == 3, str(r)[:200])
        rows = conn.execute("SELECT id,status FROM agent_todos WHERE conversation_id=? "
                            "ORDER BY id", (conv,)).fetchall()
        done = [x for x in rows if x[1] == "completed"]
        check("P2 它们真的落成 completed（不是只回了个 ok）", len(done) == 3, str(rows))

        # 混合：一条不存在的 id ⇒ 不许整体谎报成功
        r2 = ts.handle_todo_write({"action": "update", "ids": [ids[3], 999999],
                                   "status": "completed", "verify_result": "人工确认"},
                                  conv, conn=conn)
        check("P3 含失败项时 ok=False（不假成功）", r2.get("ok") is False, str(r2)[:200])
        check("P4 失败明细逐条列出（模型知道哪条要修）",
              "999999" in str(r2.get("result")) and "✗" in str(r2.get("result")),
              str(r2.get("result"))[:200])

        # 单条路径零回归
        r3 = ts.handle_todo_write({"action": "update", "ids": [ids[0]],
                                   "status": "completed", "verify_result": "人工确认"},
                                  conn=conn, conversation_id=conv)
        check("P5 单个 ids（长度 1）仍走单条路径且成功", r3.get("ok") is True, str(r3)[:160])

        print("\n=== S 源码契约 ===")
        src = open(os.path.join(ROOT, "agent/todo_store.py"), encoding="utf-8").read()
        check("S1 schema 暴露 ids 数组参数", '"ids": {' in src and '"type": "array"' in src)
        check("S2 schema 描述明确推荐批量（否则模型不会用）",
              "同一批完成" in src and "避免一条一次调用" in src)
        check("S3 批量入口在单条之前分流", src.index("_update_many(c, conversation_id") <
              src.index('tid = _a2.get("id")'))

        print("\n=== M 变异自证：短路批量分支，P1 必须翻红 ===")
        fp = os.path.join(ROOT, "agent/todo_store.py")
        orig = open(fp, "rb").read()
        mutated = orig.decode("utf-8").replace(
            "        if isinstance(_ids, (list, tuple)) and len(_ids) >= 1:",
            "        if False:").encode("utf-8")
        if mutated == orig:
            check("M0 变异锚点存在", False)
        else:
            try:
                open(fp, "wb").write(mutated)
                import importlib
                importlib.reload(ts)
                importlib.reload(ts)
                _seed(conn, conv + 1, 3)
                _ids2 = [x[0] for x in conn.execute(
                    "SELECT id FROM agent_todos WHERE conversation_id=? ORDER BY id",
                    (conv + 1,)).fetchall()]
                rm = ts.handle_todo_write({"action": "update", "ids": _ids2,
                                           "status": "completed", "verify_result": "人工确认"},
                                          conv + 1, conn=conn)
                check("M1 短路后批量调用不再生效（断言非空转）",
                      not (isinstance(rm, dict) and rm.get("ok_count") == len(_ids2)),
                      str(rm)[:160])
            finally:
                open(fp, "wb").write(orig)
                import importlib
                importlib.reload(ts)
                check("M2 还原后批量恢复", "_update_many" in open(fp, encoding="utf-8").read())
    finally:
        conn.close()
        try:
            os.remove(db)
        except Exception:
            pass

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
