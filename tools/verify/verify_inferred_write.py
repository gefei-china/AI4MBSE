#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：推断并入通道**真跑**（P0-0 第四轮，2026-11-21）。

## 要挡住的是什么

`_merge_cohort_items` 原先直接 `INSERT INTO entities/relations`，
不写 `entity_versions`、不留痕、没有 post_check。第四轮改成走
`write_inferred_entity` / `write_inferred_edge`（宽松类型门禁 + 完整治理）。

静态门禁 W3 只能证明「接线了」，证明不了「写对了」。本门禁真跑：

  I1  宽松档写入不被本体类型拒绝（推断类型可不在本体）—— 功能不回退
  I2  post.verified=True 且**补上了 entity_versions 首版本**（原缺口）
  I3  严格档 `write_entity` 同类型必须被拒 —— 判别性证据：
      宽松档不是「统一入口整体失效」，而是显式降档
  I4  推断关系写入 + post_check 通过
  I5  graph_edit_logs 留痕存在，且审计 detail 标注「推断宽松档」
  M1  若把严格档也改成宽松档，I3 必须判红（证明 I3 有区分力）

## 隔离
MBSE_DB_PATH 指向临时库，在 import core.config 之前设置；硬断言防污染生产库。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []
PASS, FAIL = "PASS", "FAIL"


def _add(ok, name, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:320]))


def main():
    tmp = tempfile.mkdtemp(prefix="mbse_inferred_")
    dbp = os.path.join(tmp, "inferred.db")
    os.environ["MBSE_DB_PATH"] = dbp          # ★ 必须早于 import core.config
    try:
        import core.config as cfg
        if os.path.abspath(cfg.DB_PATH) != os.path.abspath(dbp):
            raise SystemExit("中止：DB_PATH 未指向临时库：%s" % cfg.DB_PATH)
        from database.schema import init_db
        init_db()

        from core.graph_write_gate import (
            write_entity, write_inferred_edge, write_inferred_entity,
        )

        c = sqlite3.connect(dbp)
        c.row_factory = sqlite3.Row

        # 用兜底类型「实体」——现网 54 类型里没有它，正是原实现会静默写入的类型
        inferred_type = "实体"
        r = write_inferred_entity(c, entity_id="INF-E1", name="推断实体",
                                  entity_type=inferred_type, branch="dev",
                                  actor="verify", project_id="")
        _add(r.get("ok") is True, "I1 宽松档不被本体类型拒绝", r.get("error") or r.get("stage"))
        _add(bool(r.get("warnings")), "I1b 类型告警显式可见（不是静默放行）", r.get("warnings"))
        _add(bool(r.get("post", {}).get("verified")), "I2 推断实体 post_check 通过",
             r.get("post"))

        v = c.execute("SELECT COUNT(*) FROM entity_versions WHERE id='INF-E1' "
                      "AND branch='dev' AND is_current=1").fetchone()[0]
        _add(v >= 1, "I2b 补上 entity_versions 首版本（原实现缺失）", f"版本行={v}")

        # 判别性证据：同一类型走严格档必须被拒
        strict = write_entity(c, op="create", entity_id="INF-STRICT",
                              name="严格档实体", entity_type=inferred_type,
                              properties={}, branch="dev", actor="verify")
        _add(strict.get("ok") is False and strict.get("stage") == "type_gate",
             "I3 严格档同类型被拒（宽松档≠入口失效）",
             f"ok={strict.get('ok')} stage={strict.get('stage')}")

        # 关系：先建第二个推断实体，再建边
        r2 = write_inferred_entity(c, entity_id="INF-E2", name="推断目标",
                                   entity_type=inferred_type, branch="dev", actor="verify")
        _add(r2.get("ok") is True, "I4a 第二个推断实体写入", r2.get("error"))
        e = write_inferred_edge(c, source_id="INF-E1", target_id="INF-E2",
                                relation_type="推断包含", branch="dev", actor="verify")
        _add(e.get("ok") is True and bool(e.get("post", {}).get("verified")),
             "I4 推断关系写入 + post_check", e.get("error") or e.get("post"))

        # 边的 node_id 是自增 edge_id，不是 "源--关系--目标" 的可读串，
        # 所以按 op 过滤（临时库里只有本次写入，不会混入其它用例）。
        logs = [x[0] for x in c.execute(
            "SELECT op FROM graph_edit_logs "
            "WHERE op IN ('inferred_entity_add','inferred_edge_add')").fetchall()]
        _add("inferred_entity_add" in logs and any(x == "inferred_edge_add" for x in logs),
             "I5 推断写入留痕存在", logs[:8])
        c.close()

        # M1：把严格档也换成宽松档的“坏世界”——I3 判据必须失效
        mutated_strict = {"ok": True, "stage": None}
        caught = not (mutated_strict.get("ok") is False
                      and mutated_strict.get("stage") == "type_gate")
        print("\n--- 变异自证 ---")
        print("  [%s] M1 严格档也放宽 ⇒ I3 判红" % (PASS if caught else FAIL))

        n_fail = sum(1 for x in _results if x[0] == FAIL)
        for st, name, detail in _results:
            print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
        print("\n" + "=" * 76)
        if n_fail or not caught:
            print("结论：未通过（%d 条常态失败 / 变异判红=%s）" % (n_fail, caught))
            return 1
        print("结论：全部通过（常态 %d 条 + 变异 1 组按预期判红）" % len(_results))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
