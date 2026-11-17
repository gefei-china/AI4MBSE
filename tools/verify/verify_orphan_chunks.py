# -*- coding: utf-8 -*-
"""verify_orphan_chunks.py —— 检索底座里不得有"指向不存在文档"的片段。

## 为什么这是真缺陷（2026-11-17 实测）

检索是 `SELECT * FROM document_chunks WHERE content != ''`
（`knowledge_pipeline/search.py:93` 与 `:145` 的向量路），**不 join `documents`**
⇒ **孤儿 chunk 会被正常检索到并注入 prompt**，但用户点引用溯源时
`documents` 里找不到它。

这比"少召回几条"严重得多：引用是本仓「不得编造」纪律的落点，
一条指向不存在文档的引用会**直接破坏可信度** —— 用户点了打不开，
会合理怀疑整段回答都是编的。

## 孤儿从哪来

正常删除路径会清 chunk（`search.py:243` `DELETE FROM document_chunks WHERE document_id=?`）。
存量孤儿来自**更早的删除路径**（那时还没清 chunk 或不走该函数）。
实测：2 条来自 2026-10-30 上传测试（`t_upload.txt` / `ui_upload_check.txt`，
内容 `hello continuity test` / `UPLOAD_MARKER_ok`）。

⇒ 判据必须**同时**覆盖两个方向：
  A1 孤儿 chunk 数 == 0（现有存量）
  A2 删除路径确实清 chunk（防回归：有人删掉那行 DELETE）

## 判据

A 组（真库，只读）：
  A1 `document_id` 非空且无对应 `documents` 行的 chunk 数 == 0
  A2 `document_id IS NULL` 单独统计并**不判红**（语义不明，交人工；
     本工具也不动它们 —— 宁可漏清，不可误删）
B 组（删除路径接线）：
  B1 `MetaRepo.delete_document`（或等价实现）里有删除 chunk 的语句
  B2 该语句用的是 `document_id`（不是按 source_doc 之类易错的键）
C 组（工具）：
  C1 `tools/cleanup_orphan_chunks.py` 存在且默认 dry-run
  C2 `--apply` 才真删

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_orphan_chunks.py
退出码：0 全绿（无库时 A 组 SKIP）／1 有失败
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
TOOL = os.path.join(ROOT, "tools", "cleanup_orphan_chunks.py")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def main():
    print("=" * 78)
    print("门禁：检索底座无孤儿 chunk（指向不存在文档的片段）")
    print("=" * 78)

    print("\n--- GROUP_A ---")
    if not os.path.exists(DB):
        skip("A1 孤儿 chunk 数为 0", "干净库")
    else:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(document_chunks)")}
        if "document_id" not in cols:
            conn.close()
            skip("A1 孤儿 chunk 数为 0", "document_chunks 无 document_id 列")
        else:
            rows = conn.execute(
                "SELECT dc.id, dc.document_id, dc.source_doc FROM document_chunks dc "
                "WHERE dc.document_id IS NOT NULL "
                "  AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.id = dc.document_id) "
                "ORDER BY dc.id").fetchall()
            n_null = conn.execute(
                "select count(*) from document_chunks where document_id is null").fetchone()[0]
            total = conn.execute("select count(*) from document_chunks").fetchone()[0]
            conn.close()
            chk("A1 孤儿 chunk 数为 0（doc_id 悬空的片段会被检索到却无法溯源）",
                not rows,
                "%d 条: %s" % (len(rows),
                               [(r["id"], r["document_id"], r["source_doc"]) for r in rows[:5]]))
            print("      document_chunks 共 %d 条；document_id 为 NULL 的 %d 条（不判红，交人工）"
                  % (total, n_null))
            if rows:
                print("      修复：python tools/cleanup_orphan_chunks.py --apply")

    print("\n--- GROUP_B ---")
    # 删除路径必须清 chunk（防有人删掉那行 DELETE，导致孤儿持续产生）
    cands = [
        os.path.join(ROOT, "knowledge_pipeline", "search.py"),
        os.path.join(ROOT, "repositories", "meta_repo.py"),
    ]
    found_del, found_key = False, False
    hit_files = []
    for p in cands:
        if not os.path.isfile(p):
            continue
        src = open(p, encoding="utf-8").read()
        for m in re.finditer(r"DELETE\s+FROM\s+document_chunks\s+WHERE\s+([^\n\"']+)", src, re.I):
            found_del = True
            hit_files.append((os.path.basename(p), m.group(1).strip()[:40]))
            if "document_id" in m.group(1):
                found_key = True
    chk("B1 删除路径里有清 chunk 的语句（防孤儿持续产生）",
        found_del, "未在 %s 中找到 DELETE FROM document_chunks"
                   % [os.path.basename(x) for x in cands])
    chk("B2 该语句按 document_id 删（不是按 source_doc 之类易错的键）",
        found_key, str(hit_files))
    if hit_files:
        print("      命中: %s" % hit_files)

    print("\n--- GROUP_C ---")
    chk("C1 孤儿清理工具存在", os.path.isfile(TOOL), TOOL)
    if os.path.isfile(TOOL):
        src = open(TOOL, encoding="utf-8").read()
        chk("C2 工具默认 dry-run（--apply 才真删）",
            'action="store_true"' in src and "if not args.apply" in src, "")
        chk("C3 工具不动 document_id 为 NULL 的行（语义不明，宁可漏清不可误删）",
            "IS NOT NULL" in src.upper() or "IS NOT NULL" in src, "")

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
