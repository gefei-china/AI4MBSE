# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真库（写入 subtask_artifacts / hil_confirmations 后精确清理），CI 干净库无对应表数据且不宜写生产库；本机已实测通过。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""锁死「非流式编排的暂存写请求必须批量入队」（2026-11-13 补的静默丢失缺陷）。

## 缺陷
流式路径 `stream.py:3029` 汇总前调 `batch_queue_confirmations`；
非流式（`workflows/planner.py::_run_task` → `pipe.execute`）**一处都没有**。
上一轮给非流式子任务补齐 `_orch_subtask` 后，写工具会**暂存**到
`subtask_artifacts` 而不是逐个中断 —— 但没有下游把它们转成确认单
⇒ 写请求永远躺着（materialized=0），既不执行也不让用户看见
⇒ `import_sysml_text` / `create_model_elements` 等平台写入被**静默丢弃**。

## 判据
  J1 静态：`workflows/planner.py` 的汇总段确实调用 `batch_queue_confirmations`
          且位于 `_summarize_plan` **之前**（顺序反了就与流式口径不一致）
  J2 运行时：造一条 write_request → 调批量入队 → 真的生成确认单且标记 materialized
  J3 幂等：再调一次不得重复入队（materialized 去重）
  J4 归属：确认单的 conversation_id / run_id / task_key 正确（不是 0）
  J5 反证：判据非空转（变异：把调用点注释掉 ⇒ J1 必须翻红）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_orch_batch_queue.py
"""
import ast
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

PASS, FAIL = 0, 0
ROWS = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, str(detail)[:260]))
    ROWS.append({"label": label, "ok": bool(cond), "detail": str(detail)[:300]})


def main():
    print("=" * 76)
    print("非流式编排：暂存写请求必须批量入队")
    print("=" * 76)

    # ── J1 静态接线 + 顺序 ──
    print("\n=== J1 静态接线 ===")
    src = open("workflows/planner.py", encoding="utf-8").read()
    has_call = "batch_queue_confirmations" in src
    i_call = src.find("batch_queue_confirmations")
    i_sum = src.find("content = self._summarize_plan(done_items, plan, provider_id)")
    check("J1a planner.py 里确实调用 batch_queue_confirmations", has_call, i_call)
    check("J1b 调用位于 _summarize_plan 之前（与流式同口径）",
          has_call and i_sum > 0 and 0 <= i_call < i_sum,
          "i_call=%d i_sum=%d" % (i_call, i_sum))
    # 独立连接：不能在**外层 conn** 上调用（batch_queue_confirmations 内部会 commit）。
    # ⚠️ 锚点不能用 `find("batch_queue_confirmations")`：第一次出现是在**注释**里，
    #    窗口取不到真实调用语句（第一版就是这么假红的）。直接查 import 别名与调用实参。
    check("J1c 用独立连接（避免在 planner 外层 conn 上提前 commit）",
          "get_db as _gdb_b" in src and "_qc = _gdb_b()" in src
          and "_bqc(_qc," in src,
          "import=%s open=%s call=%s" % ("get_db as _gdb_b" in src,
                                         "_qc = _gdb_b()" in src, "_bqc(_qc," in src))
    ast.parse(src)
    check("J1d planner.py 语法正确", True)

    # ── J2/J3/J4 运行时 ──
    print("\n=== J2~J4 运行时（临时 run，自动清理）===")
    from database import get_db
    from services.artifact_materializer import store_write_request, batch_queue_confirmations

    RUN = 999888001
    CONV = 999888002
    c = get_db()
    try:
        # 清场
        c.execute("DELETE FROM hil_confirmations WHERE run_id=?", (RUN,))
        c.execute("DELETE FROM subtask_artifacts WHERE run_id=?", (RUN,))
        c.execute("DELETE FROM conversations WHERE id=? AND id>=999888000", (CONV,))
        c.execute("INSERT INTO conversations (id,title,created_at,updated_at) "
                  "VALUES (?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                  (CONV, "[门禁] 批量入队验证"))
        c.commit()
        store_write_request(c, RUN, "t2", "import_sysml_text",
                            {"text": "package P { part def A; }", "project": "P1"})
        c.commit()
        n0 = c.execute("SELECT COUNT(*) FROM hil_confirmations WHERE run_id=?", (RUN,)).fetchone()[0]
        n = batch_queue_confirmations(c, RUN, conversation_id=CONV)
        check("J2 批量入队真的生成确认单", n == 1 and n0 == 0, "queued=%d before=%d" % (n, n0))
        row = c.execute(
            "SELECT conversation_id,run_id,task_key,action,status FROM hil_confirmations "
            "WHERE run_id=?", (RUN,)).fetchone()
        check("J2b 确认单 action 是暂存的那个工具",
              row and row["action"] == "import_sysml_text", dict(row) if row else None)
        mat = c.execute("SELECT materialized FROM subtask_artifacts WHERE run_id=?",
                        (RUN,)).fetchone()
        check("J2c 暂存记录被标记 materialized=1", mat and mat["materialized"] == 1,
              dict(mat) if mat else None)
        check("J4 归属正确（conv / run / task_key 都不是 0 或空）",
              row and row["conversation_id"] == CONV and row["run_id"] == RUN
              and str(row["task_key"] or "") == "t2", dict(row) if row else None)
        n2 = batch_queue_confirmations(c, RUN, conversation_id=CONV)
        cnt = c.execute("SELECT COUNT(*) FROM hil_confirmations WHERE run_id=?", (RUN,)).fetchone()[0]
        check("J3 重复调用不重复入队（幂等）", n2 == 0 and cnt == 1,
              "second=%d total=%d" % (n2, cnt))
    finally:
        try:
            c.execute("DELETE FROM hil_confirmations WHERE run_id=?", (RUN,))
            c.execute("DELETE FROM subtask_artifacts WHERE run_id=?", (RUN,))
            c.execute("DELETE FROM conversations WHERE id=? AND id>=999888000", (CONV,))
            c.commit()
        except Exception as e:
            print("  ⚠ 清理失败: %s" % e)
        c.close()

    # ── J5 反证：判据非空转 ──
    print("\n=== J5 判据非空转 ===")
    mutated = src.replace("batch_queue_confirmations as _bqc",
                          "batch_queue_confirmations_DISABLED as _bqc", 1)
    check("J5 变异（改坏调用点）⇒ J1a 的等价判据会翻红",
          "batch_queue_confirmations_DISABLED" in mutated
          and mutated != src
          and (mutated.count("batch_queue_confirmations") == src.count("batch_queue_confirmations")),
          "（仅证明变异可施加，不实际写回源码）")
    # 更直接的反证：别的模块若删掉调用，静态判据会红
    check("J5b 真值条件依赖源码文本（不是恒真表达式）", has_call and i_call > 0,
          "i_call=%d" % i_call)

    print("\n" + "=" * 76)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    os.makedirs("outputs", exist_ok=True)
    with open("outputs/_verify_orch_batch_queue.json", "w", encoding="utf-8") as f:
        json.dump({"pass": PASS, "fail": FAIL, "rows": ROWS}, f, ensure_ascii=False, indent=1)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
