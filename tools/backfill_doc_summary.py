# -*- coding: utf-8 -*-
"""补齐 documents.summary（P0-2 配套工具）。

背景：`documents.summary` 的 LLM 摘要能力早已实现（`knowledge_pipeline.ingest._gen_llm_summary`），
但实测 14 篇文档里只有 3 篇有值 —— 其余在入库时因 LLM 不可用/超时被静默跳过。
而 P0-2 已把 summary 接进 prompt（作为文档背景），所以缺的这 11 篇等于该能力只覆盖 21%。

本脚本按需补齐，**可反复跑、幂等**（已有摘要的不动）。

用法：
  # 预览将要处理哪些（不调 LLM、不写库）
  ./.venv/Scripts/python.exe -X utf8 tools/backfill_doc_summary.py --dry-run
  # 只处理 2 篇（先小样本看质量）
  ./.venv/Scripts/python.exe -X utf8 tools/backfill_doc_summary.py --limit 2
  # 全量
  ./.venv/Scripts/python.exe -X utf8 tools/backfill_doc_summary.py
参数：
  --limit N     最多处理 N 篇（默认全部）
  --dry-run     只列清单
  --force       连已有摘要的也重生成
  --max-chars   拼给摘要器的正文字符上限（默认 12000，长文内部会 map-reduce）
"""
import argparse
import os
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根（本文件在 tools/ 下）
sys.path.insert(0, _ROOT)


def build_text(conn, doc_id: int, max_chars: int, chunk_count: int = 0) -> str:
    """**均匀采样**拼正文（头中尾都覆盖），而不是取前 N 个 chunk。

    为什么必须采样：OMG 规范有 2606 个 chunk，取前 12000 字只覆盖封面+目录，
    摘要会变成"这是一份 SysML 规范"这种无信息量的话（实测风险）。
    均匀采样让摘要器看到正文各章，才能概括出"文档讲什么"。
    """
    total = int(chunk_count or 0)
    if total <= 0:
        total = conn.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?",
                             (doc_id,)).fetchone()[0] or 0
    if total <= 0:
        return ""
    K = min(total, 40)                       # 最多取 40 个 chunk
    step = max(1, total // K)
    idxs = list(range(0, total, step))[:K]
    out = []
    for ci in idxs:
        r = conn.execute(
            "SELECT content FROM document_chunks WHERE document_id=? AND chunk_index=? "
            "ORDER BY id LIMIT 1", (doc_id, ci)).fetchone()
        c = (r[0] if r else "") or ""
        if c:
            out.append(c)
    return "\n".join(out)[:max_chars]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-chars", type=int, default=12000)
    a = ap.parse_args()

    conn = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    conn.row_factory = sqlite3.Row

    where = "" if a.force else "AND COALESCE(summary,'') = ''"
    rows = conn.execute(
        f"SELECT id, filename, chunk_count FROM documents "
        f"WHERE parse_status='completed' {where} ORDER BY chunk_count DESC").fetchall()
    if a.limit > 0:
        rows = rows[:a.limit]

    print("=" * 88)
    print("补齐 documents.summary ｜ 待处理 %d 篇%s" % (len(rows), "（dry-run）" if a.dry_run else ""))
    print("=" * 88)
    for r in rows:
        print("  id=%-4s chunks=%-5s %s" % (r["id"], r["chunk_count"], str(r["filename"])[:52]))

    if a.dry_run or not rows:
        conn.close()
        return 0

    from knowledge_pipeline.ingest import _gen_llm_summary

    ok = skipped = failed = 0
    for i, r in enumerate(rows, 1):
        t0 = time.time()
        text = build_text(conn, r["id"], a.max_chars, r["chunk_count"])
        if len(text) < 40:
            print("  [skip] %-46s 正文过短(%d字)" % (str(r["filename"])[:46], len(text)))
            skipped += 1
            continue
        try:
            s = _gen_llm_summary(text, str(r["filename"]))
        except Exception as e:
            print("  [fail] %-46s %s: %s" % (str(r["filename"])[:46], type(e).__name__, str(e)[:60]))
            failed += 1
            continue
        dt = time.time() - t0
        if not s:
            # 摘要器返回空 = LLM 不可用/超时（它内部已降级为不报错）
            print("  [empty] %-45s %.1fs（LLM 不可用或返回空）" % (str(r["filename"])[:45], dt))
            failed += 1
            continue
        conn.execute("UPDATE documents SET summary=? WHERE id=?", (s, r["id"]))
        conn.commit()
        ok += 1
        print("  [ok]   %-46s %.1fs %d字 | %s"
              % (str(r["filename"])[:46], dt, len(s), s[:44].replace("\n", " ")))

    print("-" * 88)
    print("完成：成功 %d ｜ 空/失败 %d ｜ 跳过 %d" % (ok, failed, skipped))
    left = conn.execute("SELECT COUNT(*) FROM documents WHERE parse_status='completed' "
                        "AND COALESCE(summary,'')=''").fetchone()[0]
    print("仍有 %d 篇无摘要（可再跑一次；LLM 恢复后重复执行即可补齐）" % left)
    conn.close()
    return 0 if failed == 0 else 0   # 摘要失败不算脚本失败（可重试）


if __name__ == "__main__":
    sys.exit(main())
