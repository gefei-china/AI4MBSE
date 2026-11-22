#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SPARQL 档 CQ 一次性补种（P1-2，2026-11-21）。

## 为什么需要它（而不是把 SPARQL_SEEDS 塞进 ensure_seed）

`ensure_seed` 的幂等语义是**表空才补**——这个语义是刻意设计（用户的编辑/删除
是意图，不是待修复项，见该函数 docstring）。真库已有 6 条 sql 种子 ⇒ 表非空 ⇒
把 SPARQL 种子塞进 `ensure_seed` 也**永远不会被登记**（静默失效，比报错更糟）。

两个错误选项（都不做）：
  ✗ 改 ensure_seed 为"按 question 匹配补缺" —— 会破坏"用户删了种子不被重塞"的语义：
    用户删除后 question 不在表里 ⇒ 被当成"缺失"重塞 ⇒ 用户删除失效。
  ✗ 给表加 seed_version 列重放 —— 动表结构 + 迁移，为一个补种不值。

本脚本的做法：**显式、幂等、可重复跑**。按 `question` 精确匹配，不存在才插；
用户删过的不复活（除非用户手动再跑本脚本 —— 那是显式动作，等于重新登记）。

## 用法

    ./.venv/Scripts/python.exe -X utf8 tools/seed_sparql_cqs.py            # 执行
    ./.venv/Scripts/python.exe -X utf8 tools/seed_sparql_cqs.py --dry-run  # 只看会插什么
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from services.ontology_cq import SPARQL_SEEDS  # noqa: E402


def main() -> int:
    dry = "--dry-run" in sys.argv
    from database.connection import get_db
    conn = get_db()
    try:
        added, existed = [], []
        for q, qy, exp in SPARQL_SEEDS:
            hit = conn.execute("SELECT id FROM ontology_questions WHERE question=?",
                               (q,)).fetchone()
            if hit:
                existed.append(q)
                continue
            if not dry:
                conn.execute(
                    "INSERT INTO ontology_questions "
                    "(question, kind, query, expected, enabled, source, created_by) "
                    "VALUES (?,?,?,?,1,'seed','system')", (q, "sparql", qy, exp))
            added.append(f"{q}  [kind=sparql expected={exp}]")
        if not dry:
            conn.commit()
        print(f"新增 {len(added)} 条；已存在 {len(existed)} 条"
              + ("（dry-run，未写库）" if dry else ""))
        for a in added:
            print("  +", a)
        for e in existed:
            print("  =", e)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
