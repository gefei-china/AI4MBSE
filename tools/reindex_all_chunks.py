"""v2 切片策略全量重建：按新参数重新 ingest 全部/指定文档（重切片 + 重向量化 + 新 HyDE）。

用法（在 mbse_system 目录下）：
    python tools/reindex_all_chunks.py --dry-run         # 预览计划（默认，不写库）
    python tools/reindex_all_chunks.py                   # 全量重建（删旧 chunks 重 ingest）
    python tools/reindex_all_chunks.py --doc 104         # 只重建指定文档
    python tools/reindex_all_chunks.py --keep-bigram     # 强制 bigram（无 embedding key 时用）

说明：
- 源文件从 data/uploads/{doc_id}_{safe_name} 读取（无副本的文档跳过并提示重传）
- 重建 = DELETE 旧 chunks + 重新 ingest_document（新切片/重叠/表格/代码块/embed_text/HyDE）
- 默认保留旧文档 id（doc_id 复用），doc_metadata 同步更新
"""
import argparse
import json
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.config import DB_PATH


def safe_name(filename: str) -> str:
    return re.sub(r"[^\w.\-\u4e00-\u9fa5]", "_", filename) or "doc"


def plan(conn, doc_id=None):
    """列出可重建文档（含源文件是否在）。"""
    q = "SELECT id, filename, file_type, parse_status, chunk_count FROM documents"
    params = []
    if doc_id:
        q += " WHERE id=?"
        params = [doc_id]
    rows = conn.execute(q + " ORDER BY id", params).fetchall()
    out = []
    for r in rows:
        path = os.path.join(BASE, "data", "uploads", f"{r['id']}_{safe_name(r['filename'])}")
        out.append({"id": r["id"], "filename": r["filename"], "file_type": r["file_type"] or "",
                    "status": r["parse_status"], "chunk_count": r["chunk_count"],
                    "has_source": os.path.exists(path), "source_path": path})
    return out


def rebuild(conn, doc: dict, force_bigram: bool = False) -> dict:
    """重建单个文档：删旧 chunks → 重新 ingest。"""
    from knowledge_pipeline import ingest_document, doc_meta_title
    if not doc["has_source"]:
        return {"doc_id": doc["id"], "status": "skipped", "reason": "源文件副本缺失，需重新上传"}
    with open(doc["source_path"], "rb") as f:
        content = f.read()
    conn.execute("DELETE FROM document_chunks WHERE document_id=?", (doc["id"],))
    conn.execute("UPDATE documents SET parse_status='parsing', chunk_count=0, error_msg='' WHERE id=?", (doc["id"],))
    conn.commit()
    md = {"title": doc_meta_title(conn, doc["id"]) or ""}
    # 强制 bigram：临时屏蔽 llm_providers 的 embedding key（不落库，仅进程内生效）
    if force_bigram:
        import knowledge_pipeline as kp
        kp.Embedder._probe_api = lambda self, c: setattr(self, "_api", None)
    return ingest_document(conn, doc["filename"], doc["file_type"] or "md", content,
                           uploaded_by="reindex", metadata=md, doc_id=doc["id"])


def main():
    ap = argparse.ArgumentParser(description="v2 切片全量重建")
    ap.add_argument("--dry-run", action="store_true", help="只预览计划（默认）")
    ap.add_argument("--doc", type=int, default=None, help="只重建指定文档 id")
    ap.add_argument("--keep-bigram", action="store_true", help="强制 bigram 向量（无 embedding key 时兜底）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    docs = plan(conn, args.doc)

    if args.dry_run:
        print(f"{'id':>4}  {'状态':<12} {'旧chunks':>7}   {'源文件':<6}  文件名")
        for d in docs:
            print(f"{d['id']:>4}  {d['status']:<12} {d['chunk_count']:>7}   {'有' if d['has_source'] else '缺':<6}  {d['filename'][:60]}")
        n_ok = sum(1 for d in docs if d["has_source"])
        print(f"\n可重建 {n_ok}/{len(docs)} 个文档（缺源文件的需重新上传）。")
        print("确认后运行：python tools/reindex_all_chunks.py  （去掉 --dry-run）")
        conn.close()
        return

    ok = skip = fail = 0
    for d in docs:
        r = rebuild(conn, d, force_bigram=args.keep_bigram)
        if r.get("status") == "skipped":
            print(f"  SKIP  doc {d['id']} {d['filename'][:40]}: {r.get('reason')}")
            skip += 1
        elif r.get("parse_status") == "completed":
            print(f"  OK    doc {d['id']} {d['filename'][:40]}: {r.get('chunk_count')} chunks ({r.get('embed_version')})")
            ok += 1
        else:
            print(f"  FAIL  doc {d['id']} {d['filename'][:40]}: {r.get('error')}")
            fail += 1
    print(f"\n完成：成功 {ok}，跳过 {skip}，失败 {fail}")
    conn.close()


if __name__ == "__main__":
    main()
