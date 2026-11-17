# -*- coding: utf-8 -*-
"""孤儿 chunk 治理：`document_chunks` 里 document_id 已不存在于 `documents` 的行。

## 为什么必须清（2026-11-17 实测）

检索是 `SELECT * FROM document_chunks WHERE content != ''`（`knowledge_pipeline/search.py:93`
与 `:145`），**不 join `documents`** ⇒ 孤儿 chunk 会被**正常检索到并注入 prompt**，
但用户点引用溯源时 `documents` 里找不到它 —— 等于**给了无法核实的证据**。

这比"少召回几条"严重：引用是本仓"不得编造"纪律的落点，
一条指向不存在文档的引用会直接破坏可信度。

## 孤儿怎么来的

正常删除路径会清 chunk（`knowledge_pipeline/search.py:243`
`DELETE FROM document_chunks WHERE document_id=?`）。
存量孤儿来自**更早的删除路径**（那时还没清 chunk，或不走该函数）。

实测（2026-11-17）：库里 2 条
  · chunk#24684  document_id=849  source_doc=t_upload.txt
  · chunk#24685  document_id=850  source_doc=ui_upload_check.txt
都是 2026-10-30 上传测试的残留（内容 `hello continuity test` / `UPLOAD_MARKER_ok`）。

## 安全约定（沿用本仓不可逆操作纪律）
- **默认 dry-run**：只打印清单，`--apply` 才真删；
- **只删 document_id 悬空的行**：`document_id IS NULL` 的**不动**
  （那可能是未挂文档的合法片段，语义不明，交给人工）；
- 逐条打印删了什么；失败即停保留现场；
- 写 JSON 清单（含 id/document_id/source_doc/content 摘要），可据以复核。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_orphan_chunks.py            # 只出清单
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_orphan_chunks.py --apply    # 真删
"""
import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def find_orphans(conn):
    """document_id 非空但 documents 里没有对应行的 chunk。"""
    return conn.execute(
        "SELECT dc.id, dc.document_id, dc.source_doc, dc.chunk_index, "
        "       substr(coalesce(dc.content,''),1,60) AS snippet, dc.created_at "
        "FROM document_chunks dc "
        "WHERE dc.document_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.id = dc.document_id) "
        "ORDER BY dc.id").fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "mbse.db"))
    ap.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run）")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("✗ 库不存在:", args.db)
        return 2
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total = conn.execute("select count(*) from document_chunks").fetchone()[0]
    orphans = find_orphans(conn)
    print("=" * 74)
    print("孤儿 chunk 治理 · %s" % ("APPLY" if args.apply else "DRY-RUN（不写库）"))
    print("=" * 74)
    print("document_chunks 共 %d 条｜孤儿(doc_id 悬空) %d 条" % (total, len(orphans)))

    null_doc = conn.execute(
        "select count(*) from document_chunks where document_id is null").fetchone()[0]
    if null_doc:
        print("  ⚠️ 另有 %d 条 document_id 为 NULL（**本工具不动**：语义不明，需人工判断）"
              % null_doc)

    if not orphans:
        print("\n✓ 没有孤儿 chunk。")
        conn.close()
        return 0

    print("\n清单:")
    for r in orphans:
        print("  chunk#%-7s doc_id=%-6s %-30s idx=%-4s %r"
              % (r["id"], r["document_id"], str(r["source_doc"])[:30],
                 r["chunk_index"], str(r["snippet"])[:46].replace("\n", " ")))

    if not args.apply:
        print("\n（dry-run：未删除。加 --apply 才真删）")
        _write_report(args, orphans, total, applied=False)
        conn.close()
        return 0

    # ── 真删（逐条，失败即停）──
    deleted = []
    try:
        for r in orphans:
            conn.execute("DELETE FROM document_chunks WHERE id=?", (r["id"],))
            deleted.append(r["id"])
            print("  已删 chunk#%s" % r["id"])
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("\n✗ 删除失败，已回滚本轮：%r" % (e,))
        conn.close()
        return 1

    left = len(find_orphans(conn))
    print("\n结果：删除 %d 条｜剩余孤儿 %d 条" % (len(deleted), left))
    if left:
        print("  ⚠️ 仍有剩余（可能期间又有文档被删，重跑本脚本即可）")
    # 向量索引缓存失效（否则内存里还留着被删的向量）
    try:
        from vector_index import ChunkVectorIndex
        ChunkVectorIndex.invalidate()
        print("  已失效 ChunkVectorIndex 缓存")
    except Exception as e:
        print("  （ChunkVectorIndex 失效跳过：%r）" % (e,))

    _write_report(args, orphans, total, applied=True)
    conn.close()
    return 0


def _write_report(args, orphans, total, applied):
    path = args.report or os.path.join(
        ROOT, "tmp", "_orphan_chunks_%s_%s.json"
        % ("applied" if applied else "preview", time.strftime("%Y%m%d_%H%M%S")))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "db": args.db, "dry_run": not applied,
                       "total_chunks": total, "orphan_count": len(orphans),
                       "rows": [dict(r) for r in orphans]},
                      f, ensure_ascii=False, indent=1)
        print("\n清单已写: %s" % path)
    except Exception as e:
        print("\n（清单写入失败）: %r" % (e,))


if __name__ == "__main__":
    sys.exit(main())
