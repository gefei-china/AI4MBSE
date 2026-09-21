"""检索：_branch_clause/_doc_clause/_search_chunks_bigram/vector_search_embed/search_chunks/retry_document/doc_meta_title/get_source_text/reindex_document。"""
import json
import logging
import os
import re
import time
import uuid
from .extract import extract_text
from .embedder import Embedder
from .ingest import ingest_document, chunking_params

logger = logging.getLogger(__name__)

# 项目根目录（拆包后 __file__ 位于 knowledge_pipeline/ 子目录，须上溯一级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _branch_clause(branches) -> tuple:
    """KB分支过滤子句：branches=None 不过滤；否则生成 branch IN (...) 子句。

    ⚠️ 文档全局化（本函数**只作用于 document_chunks**，已逐处核对：bigram 兜底 / 向量检索 /
    BM25 索引构建与查询，共 5 处，无一处用于 entities/relations）：
    `branch='global'` 的文档是**全局资产**，不属于任何分支。若子句写成纯 `branch IN (...)`，
    则任何「传了 branches」的调用方会**静默拿到 0 条**——因为全库文档均为 global。
    实测证据：`/api/knowledge/stats?branch=release` 曾因此把「文档总数」报成 0。
    故此处恒带 `OR branch='global'`，把「全局文档永不被分支过滤掉」变成**结构性不变量**，
    而不是依赖每个调用方都记得传 None。

    返回 (sql_suffix, params)。
    """
    if not branches:
        return "", []
    return (" AND (branch IN ({}) OR branch = 'global')".format(",".join("?" * len(branches))),
            list(branches))


def _doc_clause(doc_names) -> tuple:
    """KB来源文档过滤子句（kb_scope.docs）：doc_names=None 不过滤；
    否则按 source_doc（= documents.filename）生成 IN 子句。返回 (sql_suffix, params)。"""
    if not doc_names:
        return "", []
    return " AND source_doc IN ({})".format(",".join("?" * len(doc_names))), list(doc_names)


def _ai_clause(exclude_ai) -> tuple:
    """2026-09-15 AI 产物消费隔离：exclude_ai=True 时排除 AI 收编文档的 chunks
    （model collapse 对策第二道闸——建模 RAG 默认不消费 ai_generated）。"""
    if not exclude_ai:
        return "", []
    return " AND (origin IS NULL OR origin != 'ai_generated')", []


def _lifecycle_clause(include_deprecated: bool = False) -> tuple:
    """生命周期过滤子句（2026-09-21 G1）：默认把「已下线」文档的 chunks 挡在检索之外。

    ⚠️ 这修的是一个**只写不读**的既有缺口，不是新功能：
    `document_chunks.lifecycle_status` 早在 `database/migrations/documents.py:209` 就建了列，
    建列注释写明的目的就是「**避免废弃文档仍命中检索**」，
    `MetaRepo.transition_lifecycle(chunk_sync=True)` 也确实会在废弃时把它置为 `'deprecated'`
    —— 但检索侧（本文件 5 处 chunks 查询 + `knowledge_engine` 的 BM25/向量/HyDE 三路）
    **从来没有读过它**。实测（2026-09-21）：`grep -rn lifecycle_status` 在
    `search.py`/`knowledge_engine.py`/`agent/rag.py`/`repositories/knowledge_repo.py` **0 命中**；
    对照实体/关系侧 `status != 'deprecated'` 过滤齐全（`agent/rag.py` 内 9 处）。
    后果：**已废弃文档仍会被 AI 建模 RAG 召回** —— 版本链/回收站的「不可见」语义全建立在沙子上。

    ⚠️ 作用范围**只限 `document_chunks`**（与 `_branch_clause` 同一道边界纪律）：
    实体/关系有自己的 `status` 状态轴，不要把这个子句扩散过去。

    `include_deprecated=True` 是给**管理侧**留的显式开关（回收站列表、版本列表、
    目录计数、管理统计必须能看到已下线文档）；检索默认 must-not 包含。

    写法说明：必须用 `IS NULL OR NOT IN (...)`。若写成 `lifecycle_status != 'deprecated'`，
    SQLite 下 `NULL != 'deprecated'` 求值为 NULL（非 TRUE）→ **老库中该列为 NULL 的行会被整批吃掉**，
    表现为「升级后检索突然空了一半」。返回 (sql_suffix, params)。
    """
    if include_deprecated:
        return "", []
    return " AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('deprecated'))", []


def _search_chunks_bigram(conn, query: str, top_k: int = 5, branches: list | None = None,
                          doc_names: list | None = None, exclude_ai: bool = False,
                          include_deprecated: bool = False) -> list:
    """bigram 实时相似度兜底：查询侧与内容侧同构（字符 bigram TF），不依赖存储向量维度。"""
    from knowledge_engine import VectorEngine
    sql, params = _branch_clause(branches)
    dsql, dparams = _doc_clause(doc_names)
    asql, aparams = _ai_clause(exclude_ai)
    lsql, lparams = _lifecycle_clause(include_deprecated)
    chunks = conn.execute(
        "SELECT * FROM document_chunks WHERE content != ''" + sql + dsql + asql + lsql,
        params + dparams + aparams + lparams).fetchall()
    if not chunks:
        return []
    ve = VectorEngine()
    qv = ve._vector(query)
    scored = []
    for c in chunks:
        d = dict(c)
        score = ve._cosine(qv, ve._vector(d.get("content", "")))
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, it in scored[:top_k]:
        out.append({
            "score": round(score, 3),
            "document_id": it["document_id"],
            "source_doc": it.get("source_doc", ""),
            "chunk_index": it["chunk_index"],
            "content": it.get("content", "")[:300],  # 命中预览
            "embed_version": it.get("embed_version", ""),
            "origin": it.get("origin") or "upload",  # 2026-09-15：命中标注来源（AI 生成/用户上传）
        })
    return out


def vector_search_embed(conn, query: str, top_k: int = 5, branches: list | None = None,
                        doc_names: list | None = None, exclude_ai: bool = False,
                        include_deprecated: bool = False) -> list:
    """真向量检索：query 用 Embedder（BGE-M3 等）向量化后，与 document_chunks.embedding 余弦比较。

    只与相同 embed_version 的 chunk 比较（防 bigram 与真向量维度混合）；
    Embedder 不可用 / 无同版本存储向量 / 维度不一致 → 降级实时 bigram（_search_chunks_bigram）。
    doc_names：kb_scope.docs 来源文档过滤（None=不过滤）。
    exclude_ai：排除 AI 收编文档（origin='ai_generated'）——AI 建模 RAG 消费隔离。
    include_deprecated：默认 False = 已下线（deprecated）文档的 chunk 不参与检索（G1）。
    返回结构与 search_chunks 一致（供命中预览 / GraphRAG 检索消费）。
    """
    import math as _m
    try:
        embedder = Embedder(conn)
        qv, version = embedder.embed_with_version([query])
        if not qv or version == "bigram-tf":
            return _search_chunks_bigram(conn, query, top_k, branches, doc_names, exclude_ai,
                                         include_deprecated)
        q = qv[0]
        sql, params = _branch_clause(branches)
        dsql, dparams = _doc_clause(doc_names)
        asql, aparams = _ai_clause(exclude_ai)
        lsql, lparams = _lifecycle_clause(include_deprecated)
        rows = conn.execute(
            "SELECT * FROM document_chunks WHERE content != '' AND embedding != '[]' AND embed_version=?"
            + sql + dsql + asql + lsql, [version] + params + dparams + aparams + lparams).fetchall()
        if not rows:
            return _search_chunks_bigram(conn, query, top_k, branches, doc_names, exclude_ai,
                                         include_deprecated)

        # P2-1 矩阵化加速：numpy 批量余弦（无 numpy 自动降级逐行）
        from vector_index import ChunkVectorIndex
        allowed_ids = {r["id"] for r in rows}
        mat = ChunkVectorIndex.search(conn, q, version, top_k, only_ids=allowed_ids)
        if mat is not None:
            by_id = {r["id"]: dict(r) for r in rows}
            out = []
            for score, cid in mat:
                d = by_id.get(cid)
                if not d:
                    continue
                out.append({
                    "score": round(score, 3),
                    "document_id": d["document_id"],
                    "source_doc": d.get("source_doc", ""),
                    "chunk_index": d["chunk_index"],
                    "content": d.get("content", "")[:300],  # 命中预览
                    "embed_version": d.get("embed_version", ""),
                    "origin": d.get("origin") or "upload",
                })
            return out

        # 降级：逐行余弦（原逻辑）
        qn = _m.sqrt(sum(x * x for x in q)) or 1.0
        scored = []
        for c in rows:
            d = dict(c)
            vec = json.loads(d.get("embedding") or "[]")
            if not vec or len(vec) != len(q):
                continue
            dot = sum(a * b for a, b in zip(q, vec))
            vn = _m.sqrt(sum(x * x for x in vec)) or 1.0
            s = dot / (qn * vn)
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, it in scored[:top_k]:
            out.append({
                "score": round(score, 3),
                "document_id": it["document_id"],
                "source_doc": it.get("source_doc", ""),
                "chunk_index": it["chunk_index"],
                "content": it.get("content", "")[:300],  # 命中预览
                "embed_version": it.get("embed_version", ""),
                "origin": it.get("origin") or "upload",
            })
        return out
    except Exception:
        return _search_chunks_bigram(conn, query, top_k, branches, doc_names, exclude_ai,
                                     include_deprecated)


def search_chunks(conn, query: str, top_k: int = 5, branches: list | None = None,
                  doc_names: list | None = None, exclude_ai: bool = False,
                  include_deprecated: bool = False) -> list:
    """向量检索 document_chunks（真分块命中）。

    优先消费存储向量（BGE-M3 等真 Embedding，见 vector_search_embed）；
    无真向量 / 不可用 → 降级实时 bigram（_search_chunks_bigram）。
    branches：KB分支过滤（None=不过滤；['dev','release']=限定检索范围）。
    doc_names：kb_scope.docs 来源文档过滤（None=不过滤）。
    exclude_ai：排除 AI 收编文档（origin='ai_generated'）——AI 建模 RAG 默认排除（消费隔离）。
    include_deprecated：默认 False = 已下线文档不进检索结果（G1 生命周期过滤）。
    """
    return vector_search_embed(conn, query, top_k, branches, doc_names, exclude_ai,
                               include_deprecated)


def retry_document(conn, doc_id: int, metadata: dict | None = None) -> dict:
    """失败文档一键重试：从 data/uploads 源文件副本重新执行整条管道。

    流程：删旧 chunks → 重置 pipeline_detail → 重新 ingest（用副本内容）。
    返回与 ingest_document 一致（含 parse_status/chunk_count/pipeline）。
    """
    import os
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0, "error": "document not found"}
    # 副本定位走唯一权威实现（同日 2026-09-21 收敛，见 ingest.safe_doc_name）
    from .ingest import source_copy_path
    path = source_copy_path(doc_id, doc["filename"])
    if not os.path.exists(path):
        return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0,
                "error": "源文件副本不存在，无法重试（请重新上传）"}
    with open(path, "rb") as f:
        content = f.read()
    # 清理旧 chunks + 重置状态（F3 修复：包写事务，中途失败整体回滚，防旧数据半删）
    in_tx = conn.in_transaction
    if not in_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM document_chunks WHERE document_id=?", (doc_id,))
        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()
        except Exception as e:
            logger.warning("ChunkVectorIndex 失效失败（不阻断）: %s", e)
        conn.execute("UPDATE documents SET parse_status='parsing', chunk_count=0, error_msg='' WHERE id=?", (doc_id,))
        if not in_tx:
            conn.commit()
    except Exception as e:
        if not in_tx:
            conn.rollback()
        logger.error("retry_document 重置失败（已回滚）: doc_id=%s err=%s", doc_id, e)
        return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0,
                "error": f"重置文档状态失败，已回滚: {e}"}
    md = metadata or {}
    md.setdefault("title", doc_meta_title(conn, doc_id))
    return ingest_document(conn, doc["filename"], doc["file_type"], content,
                           uploaded_by=doc["uploaded_by"] or "system", metadata=md, doc_id=doc_id)


def doc_meta_title(conn, doc_id: int) -> str:
    try:
        row = conn.execute("SELECT title FROM doc_metadata WHERE document_id=?", (doc_id,)).fetchone()
        return row["title"] if row else ""
    except Exception:
        return ""


def get_source_text(conn, doc_id: int) -> dict:
    """源文件预览：优先读 data/uploads 源文件副本；缺失则用 document_chunks 拼接全文。

    返回 {filename, file_type, content, source: 'file'|'chunks'}
    """
    import os
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return {"error": "document not found"}
    # 副本定位走唯一权威实现（同日 2026-09-21 收敛，见 ingest.safe_doc_name）
    from .ingest import source_copy_path
    path = source_copy_path(doc_id, doc["filename"])
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = f.read()
        return {"filename": doc["filename"], "file_type": doc["file_type"],
                "content": extract_text(doc["filename"], raw), "source": "file"}
    # 兜底：chunks 拼接
    chunks = conn.execute(
        "SELECT content FROM document_chunks WHERE document_id=? ORDER BY chunk_index",
        (doc_id,)).fetchall()
    full = "\n\n".join(c["content"] for c in chunks)
    return {"filename": doc["filename"], "file_type": doc["file_type"],
            "content": full, "source": "chunks"}


def reindex_document(conn, doc_id: int) -> dict:
    """KB-P0：重索引文档——按新 embedder 重新向量化全部块（保留 section/bm25_text）。

    v2：与 ingest 对齐——用 embed_text（标题+section+content，Title-Enriched）重建向量，
    并同步重算 hyde_embedding（真向量模式下），保证重索引前后检索语义一致。

    返回 {doc_id, parse_status, chunk_count, embed_version}
    """
    try:
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not doc:
            return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0, "error": "document not found"}
        chunks = conn.execute(
            "SELECT id, content, section, hyde_questions FROM document_chunks WHERE document_id=? ORDER BY chunk_index",
            (doc_id,)).fetchall()
        if not chunks:
            return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0, "error": "无分块"}
        title = doc_meta_title(conn, doc_id) or doc["filename"]
        ck = chunking_params(doc["filename"])
        embed_texts = []
        for c in chunks:
            content = c["content"] or ""
            section = c["section"] or ""
            if ck["title_enriched"]:
                embed_texts.append(f"{title} {section} {content}".strip())
            else:
                embed_texts.append(content)
        embedder = Embedder(conn)
        vectors, embed_version = embedder.embed_with_version(embed_texts)
        # hyde_embedding：真向量模式下重算（与 ingest 一致）
        hyde_texts, hyde_vecs = [], []
        for c in chunks:
            try:
                qs = json.loads(c["hyde_questions"] or "[]")
            except Exception:
                qs = []
            hyde_texts.append("；".join(qs) if qs else "")
        if embed_version != "bigram-tf" and any(hyde_texts):
            try:
                hv, _ = embedder.embed_with_version([t for t in hyde_texts if t], batch_size=0)
                it = iter(hv)
                hyde_vecs = [next(it) if t else [] for t in hyde_texts]
            except Exception:
                hyde_vecs = [[] for _ in hyde_texts]
        else:
            hyde_vecs = [[] for _ in hyde_texts]
        for c, vec, hvec in zip(chunks, vectors, hyde_vecs):
            from vector_index import normalize_vector
            conn.execute(
                "UPDATE document_chunks SET embedding=?, embed_version=?, hyde_embedding=? WHERE id=?",
                (json.dumps(normalize_vector(vec), ensure_ascii=False), embed_version,
                 json.dumps(normalize_vector(hvec), ensure_ascii=False) if hvec else "[]", c["id"]))
        conn.commit()
        # 数据变更：向量索引失效
        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()
        except Exception as e:
            logger.warning("ChunkVectorIndex 失效失败（不阻断）: %s", e)
        return {"doc_id": doc_id, "parse_status": "completed", "chunk_count": len(chunks),
                "embed_version": embed_version}
    except Exception as e:
        # F3 修复：中途失败整体回滚（原实现留下半成品向量/状态）
        if conn.in_transaction:
            conn.rollback()
        logger.error("reindex_document 失败（已回滚）: doc_id=%s err=%s", doc_id, e)
        return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0, "error": str(e)[:200]}
