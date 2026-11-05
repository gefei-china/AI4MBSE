#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超长 query 不得撑爆 SQLite 的 LIKE 模式（conv 990461 实证回归门禁）。

## 事故
用户粘贴 94,538 字符的 SysML v2 代码后，一轮编排「刚发就失败」：
`agent/pipeline_parts/stream.py:5056 → agent/rag.py:173 → agent/rag.py:1202`
把整段原文拼成 `%{query}%`，突破 SQLite 的
`SQLITE_MAX_LIKE_PATTERN_LENGTH`（默认 50,000 字节）⇒
`sqlite3.OperationalError: LIKE or GLOB pattern too complex`。
异常被顶层 except 转成 error 事件 ⇒ 子任务零产出 ⇒ `ok=False` ⇒ 首个任务 failed
⇒ 下游 4 个全部 blocked ⇒ 整轮「（计划已执行，但无成功交付物）」。

## 判据（不靠源码字串，靠真实行为）
A1 **反证**：未截断的原始模式必须真的抛错（证明本环境能复现事故，不是假绿）
A2 修复后 `_entity_link` 吃下 105,600 字符 query 不抛异常
A3 短 query 仍命中实体（正常召回路径未被破坏）
A4/A5 静态接线：整句 LIKE 参数走截断变量，且上限有定义
"""
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def chk(label, ok, detail=""):
    (PASS if ok else FAIL).append((label, detail))
    print(("PASS" if ok else "FAIL"), label, detail)


def main():
    # 确定性构造，量级与事故样本同级（94,538 字符）；重复串让 _tokens 也可复现
    long_q = ("package '发动机功能架构基线' { doc /* 双转子、分排、无加力涡扇发动机 */ "
              "part def Engine; }" * 1600)
    chk("Z0 合成样本确实超过 SQLite LIKE 上限", len(long_q) > 50000,
        "len=%d" % len(long_q))

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT, entity_type TEXT, "
                 "status TEXT DEFAULT 'reviewed', branch TEXT, source_doc TEXT)")
    conn.execute("CREATE TABLE entity_aliases (id INTEGER PRIMARY KEY, entity_id TEXT, alias_name TEXT)")
    conn.execute("INSERT INTO entities VALUES "
                 "('e1','发动机功能架构基线','model','reviewed','release','doc1')")
    conn.execute("INSERT INTO entity_aliases VALUES (1,'e1','发动机')")

    # A1 反证（判据自证）：不带截断的原始形态必须复现事故
    try:
        conn.execute("SELECT * FROM entities WHERE name LIKE ?", ("%" + long_q + "%",)).fetchall()
        chk("A1 反证:未截断模式必须抛 OperationalError", False,
            "没抛 ⇒ 本环境无法复现，后续断言不足以证明修复")
    except sqlite3.OperationalError as e:
        chk("A1 反证:未截断模式确实复现事故", "LIKE or GLOB pattern too complex" in str(e),
            str(e)[:70])

    try:
        from agent.rag import GraphRAG
        chk("A0 GraphRAG 可导入", True)
    except Exception as e:
        GraphRAG = None
        chk("A0 GraphRAG 可导入", False, "%s: %s" % (type(e).__name__, e))

    if GraphRAG is not None:
        try:
            t0 = time.time()
            out = GraphRAG._entity_link(conn, long_q, ["release"])
            chk("A2 超长 query 不再抛异常", True,
                "%.3fs hits=%d" % (time.time() - t0, len(out or [])))
        except Exception as e:
            chk("A2 超长 query 不再抛异常", False, "%s: %s" % (type(e).__name__, e))
        try:
            short = GraphRAG._entity_link(conn, "发动机功能架构基线", ["release"])
            chk("A3 短 query 仍能命中实体（正常路径未破坏）",
                any(x.get("id") == "e1" for x in short), "hits=%d" % len(short))
        except Exception as e:
            chk("A3 短 query 仍能命中实体", False, "%s: %s" % (type(e).__name__, e))

    src = open(os.path.join(ROOT, "agent", "rag.py"), encoding="utf-8").read()
    chk("A4 LIKE 参数统一走截断变量", src.count('f"%{_like_q}%"') >= 4,
        "count=%d" % src.count('f"%{_like_q}%"'))
    chk("A5 截断上限有定义", "_LIKE_MAX = 200" in src)
    conn.close()

    print("\n==== %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
    for label, detail in FAIL:
        print("  FAIL:", label, detail)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
