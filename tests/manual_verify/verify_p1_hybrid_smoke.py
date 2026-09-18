# -*- coding: utf-8 -*-
"""P1-3 hybrid_search 优化冒烟（分页 + BM25 缓存 + HyDE 限定候选集）。

覆盖：
- H1 基本检索：hits 非空、含 vec/bm25/hyde 字段
- H2 BM25 缓存命中：二次调用 engine 对象复用（零重建）
- H3 指纹失效：插入新 chunk → 缓存重建（engine 对象更新）
- H4 domain 过滤：缓存共享 + Python 侧裁剪结果正确
- H5 HyDE 限定候选集：候选集内 hyde 加分、候选集外不扫描
- H6 增量一致性：BM25Engine.extend 分页构建 == build 一次性构建（search 结果一致）
"""
import os
import sys
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), "mbse_p1_hybrid_smoke.db")
for _p in (_TMP_DB, _TMP_DB + "-wal", _TMP_DB + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP_DB

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from database.schema import get_db, init_db  # noqa: E402
from knowledge_engine import (BM25Engine, _BM25_CACHE, hybrid_search)  # noqa: E402

init_db()
conn = get_db()
PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {detail if not cond else ''}")


# ── 准备数据：2 文档 × 3 chunks，含 hyde_questions ──
conn.execute("INSERT INTO documents (id, filename, branch) VALUES (1, '卫星通信方案.md', 'dev')")
conn.execute("INSERT INTO documents (id, filename, branch) VALUES (2, '载荷设计手册.md', 'dev')")
seed = [
    (1, 1, 0, "高通量卫星采用Ka频段转发器，支持宽带通信与波束成形", "卫星通信方案.md", "dev", "satellite", '["Ka频段转发器怎么选型？","高通量卫星架构是什么？"]'),
    (1, 2, 1, "转发器线性化器用于补偿功放非线性失真，提升EVM指标", "卫星通信方案.md", "dev", "satellite", "[]"),
    (1, 3, 2, "星上处理载荷支持再生中继，适合低轨星座组网", "卫星通信方案.md", "dev", "satellite", '["低轨星座星上处理有哪些方案？"]'),
    (2, 4, 0, "热控系统采用泵驱两相流回路，散热量可达5kW", "载荷设计手册.md", "dev", "thermal", "[]"),
    (2, 5, 1, "天线反射面采用碳纤维网格，面密度低于2kg/m²", "载荷设计手册.md", "dev", "thermal", "[]"),
    (2, 6, 2, "电源控制器实现MPPT最大功率点跟踪，效率96%", "载荷设计手册.md", "dev", "thermal", "[]"),
]
for did, cid, idx, content, doc, branch, domain, hyde in seed:
    conn.execute(
        "INSERT INTO document_chunks (id, document_id, chunk_index, content, source_doc, branch, domain, hyde_questions, embed_version) "
        "VALUES (?,?,?,?,?,?,?,?, 'bigram-tf')",
        (cid, did, idx, content, doc, branch, domain, hyde))
conn.commit()

print("\n== H1 基本检索 ==")
r = hybrid_search(conn, "Ka频段转发器选型", top_k=3, branches=["dev"])
check("H1a hits 非空", bool(r.get("hits")), str(r))
check("H1b 含完整字段", all(k in r["hits"][0] for k in
      ("vec_score", "bm25_score", "hyde_score", "source_doc", "content", "recall_reason")),
      str(r["hits"][0].keys()))
check("H1c 最相关 chunk 排前（chunk1 命中 Ka频段转发器）",
      r["hits"][0]["document_id"] == 1 and r["hits"][0]["chunk_index"] == 0,
      str([(h["chunk_index"], h["score"]) for h in r["hits"]]))

print("\n== H2 BM25 缓存命中 ==")
n0 = len(_BM25_CACHE)
eng0 = next(iter(_BM25_CACHE.values()))[0]
r2 = hybrid_search(conn, "Ka频段转发器选型", top_k=3, branches=["dev"])
check("H2a 缓存未新增（同一范围命中）", len(_BM25_CACHE) == n0, f"{len(_BM25_CACHE)} vs {n0}")
check("H2b engine 对象复用（零重建）",
      any(v[0] is eng0 for v in _BM25_CACHE.values()), "engine 被替换")
check("H2c 结果与首查一致", [(h["document_id"], h["chunk_index"]) for h in r2["hits"]] ==
      [(h["document_id"], h["chunk_index"]) for h in r["hits"]],
      str([(h["document_id"], h["chunk_index"]) for h in r2["hits"]]))

print("\n== H3 指纹失效重建 ==")
conn.execute("INSERT INTO document_chunks (id, document_id, chunk_index, content, source_doc, branch, domain, embed_version) "
             "VALUES (7, 1, 3, '新增：Ka频段低噪声放大器LNA噪声系数0.8dB', '卫星通信方案.md', 'dev', 'satellite', 'bigram-tf')")
conn.commit()
r3 = hybrid_search(conn, "低噪声放大器", top_k=3, branches=["dev"])
check("H3a 新 chunk 被检索到",
      any(h["document_id"] == 1 and h["chunk_index"] == 3 for h in r3["hits"]),
      str([(h["document_id"], h["chunk_index"]) for h in r3["hits"]]))
check("H3b 缓存已重建（engine 更新）",
      any(v[0] is not eng0 for v in _BM25_CACHE.values()), "engine 未替换")

print("\n== H4 domain 过滤 ==")
r4 = hybrid_search(conn, "转发器", top_k=5, branches=["dev"], domain="satellite")
check("H4a domain=satellite 只返回 satellite 域",
      all(h.get("domain", "unknown") == "satellite" for h in r4["hits"]),
      str([(h.get("domain"), h["chunk_index"]) for h in r4["hits"]]))
r4b = hybrid_search(conn, "转发器", top_k=5, branches=["dev"], domain="thermal")
check("H4b domain=thermal 返回 thermal 域（转发器不命中也走 RRF 兜底结构）",
      isinstance(r4b, dict) and "hits" in r4b, str(r4b)[:120])

print("\n== H5 HyDE 限定候选集 ==")
# chunk1 有 hyde 'Ka频段转发器怎么选型？' → 查询命中候选集 → hyde_score > 0
r5 = hybrid_search(conn, "Ka频段转发器怎么选型", top_k=3, branches=["dev"], domain="satellite")
h1 = next((h for h in r5["hits"] if h["document_id"] == 1 and h["chunk_index"] == 0), None)
check("H5a 候选集内 chunk 获得 hyde 分", h1 is not None and h1.get("hyde_score", 0) > 0,
      str(h1))
# 无 hyde_questions 的 chunk 不产生 hyde_score
r5b = hybrid_search(conn, "泵驱两相流", top_k=3, branches=["dev"], domain="thermal")
check("H5b 无 hyde 数据不报错且正常返回", bool(r5b.get("hits")), str(r5b))

print("\n== H6 增量一致性（extend 分页 == build 一次性） ==")
rows = [dict(r) for r in conn.execute(
    "SELECT id, content FROM document_chunks ORDER BY id").fetchall()]
e_paged = BM25Engine()
for i in range(0, len(rows), 2):          # 页大小 2 分页 extend
    e_paged.extend(rows[i:i + 2])
e_bulk = BM25Engine()
e_bulk.build(rows)
res_p = {h["chunk_id"]: h["score"] for h in e_paged.search("转发器", top_k=10)}
res_b = {h["chunk_id"]: h["score"] for h in e_bulk.search("转发器", top_k=10)}
check("H6a extend 与 build 召回集合一致", set(res_p) == set(res_b),
      f"{set(res_p)} vs {set(res_b)}")
check("H6b 打分一致", all(abs(res_p[k] - res_b[k]) < 1e-9 for k in res_p),
      str({k: (res_p[k], res_b[k]) for k in res_p}))

print("\n" + "=" * 56)
print(f"PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL GREEN ✅")
