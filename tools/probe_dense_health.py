# -*- coding: utf-8 -*-
"""embedding 恢复后的 dense 检索质量探针（只读，2026-10-06）。

背景：preflight 显示 4/5 查询 route=vector 但 **conf=0.0**（reason=graph_no_hit），
而 7269 个 chunk 中 810 个是 bigram-tf 降级向量（`hybrid_search` 会因
`version != "bigram-tf"` 跳过它们）。本脚本拆开看三件事：

  A. dense 侧到底召回没召回（直接打 `ChunkVectorIndex.search` / `hybrid_search`）
  B. 相似度分数的量纲（是不是被 rerank 阈值卡掉）
  C. 降级 chunk 的实际影响面（那 810 条是否完全进不了 dense 路）

只读：不写库。
"""
import os
import sys
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import knowledge_engine as KE                        # noqa: E402
from knowledge_pipeline import Embedder                # noqa: E402
from database import get_db                          # noqa: E402

QUERIES = [
    "卫星通信系统包含哪些分系统",
    "OMG 规范里 requirement 的写法",
    "属性定义与量的区别",
    "SysML_V2 端口定义语法",
    "测控分系统与地面控制站的关系",
]

conn = get_db()
conn.row_factory = sqlite3.Row

emb = Embedder(conn)
qv, ver = emb.embed_with_version(["探针"])
print("[A] embedding 探针: dim=%s version=%s provider=%s"
      % (len(qv[0]) if qv and qv[0] else None, ver, bool(getattr(emb, "_api", None))))

n_tot = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
n_deg = conn.execute(
    "SELECT COUNT(*) FROM document_chunks WHERE embed_version='bigram-tf'").fetchone()[0]
print("[C] chunks 总数=%d  降级=%d (%.1f%%)" % (n_tot, n_deg, 100.0 * n_deg / max(1, n_tot)))

print("\n[B] dense 侧直查（ChunkVectorIndex.search，绕开 hybrid/rerank）")
try:
    import vector_index as VI            # ChunkVectorIndex 定义在仓库根的 vector_index.py
    for q in QUERIES:
        hits = VI.ChunkVectorIndex(conn).search(q, top_k=5)
        hits = list(hits or [])
        print("  %-30s → %d 条" % (q[:28], len(hits)))
        for h in hits[:3]:
            d = dict(h) if hasattr(h, "keys") else {"_raw": h}
            print("        score=%s doc=%s"
                  % (str(d.get("score"))[:8], str(d.get("source_doc") or d.get("_raw"))[:36]))
except Exception as e:
    print("  ChunkVectorIndex 直查失败: %s: %s" % (type(e).__name__, e))

print("\n[D] hybrid_search 全量（bm25 + vector 融合 + 轻量重排）")
for q in QUERIES:
    try:
        res = KE.hybrid_search(conn, q, top_k=5)
        hits = list(res.get("hits") or [])
        print("  %-30s → %d 条 (bm25=%s vec=%s mode=%s)"
              % (q[:28], len(hits), res.get("bm25_count"), res.get("vec_count"), res.get("mode")))
        for h in hits[:3]:
            d = dict(h) if hasattr(h, "keys") else {"_raw": h}
            print("        score=%-8s vec=%-7s bm25=%-7s %s"
                  % (str(d.get("score"))[:8], str(d.get("vec_score"))[:7],
                     str(d.get("bm25_score"))[:7],
                     str(d.get("source_doc") or d.get("chunk_id") or "")[:28]))
    except Exception as e:
        print("  %-30s → 异常 %s: %s" % (q[:28], type(e).__name__, e))

print("\n[E] 降级 chunk 抽样（这些在 dense 路里是否可见）")
rows = conn.execute(
    "SELECT id, source_doc, length(embedding) AS elen FROM document_chunks "
    "WHERE embed_version='bigram-tf' ORDER BY id LIMIT 3").fetchall()
for r in rows:
    print("  id=%s doc=%s embedding长度=%s" % (r["id"], str(r["source_doc"])[:30], r["elen"]))
rows2 = conn.execute(
    "SELECT id, source_doc, length(embedding) AS elen FROM document_chunks "
    "WHERE embed_version='openai-compat' ORDER BY id LIMIT 2").fetchall()
print("  （对照组：真向量）")
for r in rows2:
    print("  id=%s doc=%s embedding长度=%s" % (r["id"], str(r["source_doc"])[:30], r["elen"]))

conn.close()
