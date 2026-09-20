"""P1 知识引擎双引擎底座：向量引擎 + 查询路由决策 + 统计落库。

设计要点（与方案 P1 对齐）：
- 图引擎（GraphRAG，agent.py）：实体链接 + 子图遍历，精确但受限于图谱覆盖。
- 向量引擎（本模块 VectorEngine）：本地轻量语义检索，字符 bigram TF + 余弦相似度，
  对中文友好、零外部依赖（不调 embedding API），保证回归确定性（MBSE_LLM_FORCE_MOCK 下仍可用）。
- QueryRouter：graph 置信度 >= 阈值 → 纯图；命中但置信度不足 → 图+向量混合补充；
  图无命中 → 纯向量。每次查询写入 query_routing_stats 供平台观测双引擎消费比例。
"""
import json
import logging
import math
import re
import time
from collections import Counter

logger = logging.getLogger(__name__)


class VectorEngine:
    """轻量向量引擎：字符 bigram TF 词袋 + 余弦相似度（零外部依赖，中文友好）。"""

    def _tokenize(self, text: str) -> list:
        """字符 bigram 切分：'宽带通信' → ['宽带','带通','通信']。"""
        text = re.sub(r"[\s\W_]+", "", str(text).lower())
        if len(text) <= 1:
            return [text] if text else []
        return [text[i:i + 2] for i in range(len(text) - 1)]

    def _vector(self, text: str) -> Counter:
        return Counter(self._tokenize(text))

    def _cosine(self, qv: Counter, dv: Counter) -> float:
        if not qv or not dv:
            return 0.0
        dot = sum(qv[k] * dv.get(k, 0) for k in qv)
        qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
        dn = math.sqrt(sum(v * v for v in dv.values())) or 1.0
        return dot / (qn * dn)

    def search(self, query: str, items: list, top_k: int = 5, key: str = "text"):
        """对 items 按 key 字段文本相似度打分，返回 [(score, item), ...] 降序（score>0 的才返回）。"""
        qv = self._vector(query)
        scored = []
        for it in items:
            text = it.get(key) if isinstance(it, dict) else str(it)
            if not text:
                continue
            score = self._cosine(qv, self._vector(text))
            if score > 0:
                scored.append((score, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]


class QueryRouter:
    """双引擎查询路由决策 + 统计落库（P1：图 + 向量 + 路由统计）。"""

    def __init__(self, threshold: float = 0.75):
        self.threshold = threshold

    def decide(self, graph_results: list, graph_confidence: float, vector_results: list):
        """返回 (route, reason)。route ∈ {graph, vector, mixed}。"""
        if graph_results and graph_confidence >= self.threshold:
            return "graph", "graph_confidence_ok"
        if graph_results and vector_results:
            return "mixed", "graph_confidence_low_vector_supplement"
        if graph_results:
            return "graph", "graph_hit_insufficient_confidence"
        if vector_results:
            return "vector", "graph_no_hit"
        return "vector", "no_hit_fallback"

    def record(self, conn, query: str, route: str, reason: str, confidence: float,
               graph_count: int, vector_count: int, latency_ms: int, branch: str = "dev") -> None:
        conn.execute(
            "INSERT INTO query_routing_stats (query, route, reason, confidence, graph_count, vector_count, latency_ms, branch) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (query, route, reason, confidence, graph_count, vector_count, latency_ms, branch),
        )
        conn.commit()

    def stats(self, conn) -> dict:
        """聚合统计：总查询数、按 route 分组消费比例/平均置信度/平均延迟、最近 10 次查询。"""
        total = conn.execute("SELECT COUNT(*) FROM query_routing_stats").fetchone()[0]
        rows = conn.execute(
            "SELECT route, COUNT(*) AS n, ROUND(AVG(confidence),3) AS avg_conf, "
            "ROUND(AVG(latency_ms),1) AS avg_ms, SUM(graph_count) AS g_sum, SUM(vector_count) AS v_sum "
            "FROM query_routing_stats GROUP BY route ORDER BY n DESC"
        ).fetchall()
        routes = {}
        for r in rows:
            d = dict(r)
            routes[d["route"]] = {
                "count": d["n"],
                "pct": round(d["n"] * 100.0 / total, 1) if total else 0.0,
                "avg_confidence": d["avg_conf"],
                "avg_latency_ms": d["avg_ms"],
                "graph_hits": d["g_sum"],
                "vector_hits": d["v_sum"],
            }
        recent = conn.execute(
            "SELECT query, route, reason, confidence, graph_count, vector_count, latency_ms, created_at "
            "FROM query_routing_stats ORDER BY id DESC LIMIT 10"
        ).fetchall()
        return {
            "total_queries": total,
            "routes": routes,
            "recent": [dict(r) for r in recent],
        }

    def _monotonic_time(self):
        return time.time()


class BM25Engine:
    """KB-P1：零依赖 BM25 全文检索（中文友好：复用 bigram 切分 + 停用词过滤）。

    - 构建：对 chunks 的 bm25_text（含章节前缀）建倒排索引 + 文档长度归一化
    - 检索：BM25 打分公式，精确词命中（补充向量检索的语义召回）
    - 零外部依赖、确定性可回归
    """

    K1 = 1.5
    B = 0.75
    STOP = {"的", "了", "和", "是", "在", "与", "及", "或", "个", "有", "对", "为", "从", "这", "那", "等", "并"}

    def __init__(self):
        self._index = {}    # term -> {chunk_id: tf}
        self._doc_len = {}  # chunk_id -> length
        self._avg_len = 0.0
        self._total_len = 0  # 全部文档 token 累计（extend 增量构建用）
        self._docs = []     # chunk 元数据缓存（id 顺序对应）

    def _tokens(self, text: str) -> list:
        ve = VectorEngine()
        toks = ve._tokenize(text)
        return [t for t in toks if t not in self.STOP and len(t) >= 2]

    def build(self, chunks: list) -> None:
        """chunks: [{id, bm25_text}] 或 [{id, content}]。幂等重建。"""
        self._index, self._doc_len, self._docs = {}, {}, []
        self._total_len = 0
        self.extend(chunks)
        n = len(self._docs)
        self._avg_len = self._total_len / n if n else 0.0

    def extend(self, chunks: list) -> None:
        """增量追加 chunks（P1-3 分页流式构建）：与 build 等价可叠加，avg_len 在最后统一计算。"""
        for c in chunks:
            cid = c.get("id")
            text = c.get("bm25_text") or c.get("content") or ""
            toks = self._tokens(text)
            self._docs.append(cid)
            self._doc_len[cid] = len(toks)
            self._total_len += len(toks)
            tf = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            for t, n in tf.items():
                self._index.setdefault(t, {})[cid] = n
        n = len(self._docs)
        self._avg_len = self._total_len / n if n else 0.0

    def search(self, query: str, top_k: int = 5) -> list:
        """返回 [{chunk_id, score}] 降序。"""
        qtoks = self._tokens(query)
        if not qtoks or not self._docs:
            return []
        n = len(self._docs)
        scores = {}
        for t in set(qtoks):
            posting = self._index.get(t)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for cid, tf in posting.items():
                dl = self._doc_len.get(cid, 0)
                denom = tf + self.K1 * (1 - self.B + self.B * dl / self._avg_len) if self._avg_len else tf
                scores[cid] = scores.get(cid, 0.0) + idf * (tf * (self.K1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [{"chunk_id": cid, "score": round(s, 3)} for cid, s in ranked]


# P1-3：BM25 索引缓存（按检索范围签名 + 全局写水位指纹失效）。
# - key：检索范围（branches/domain/doc_names）→ 相同范围复用同一索引
# - 指纹：document_chunks 的 (COUNT, MAX(id))——INSERT/DELETE 均敏感；
#   content 级 UPDATE 不改指纹（ingest 语义下 content 更新走新行，可接受）
# - 容量上限 8，超限淘汰最旧（dict 插入序）
_BM25_CACHE = {}
_BM25_CACHE_MAX = 8
_CHUNK_COLS = "id, document_id, source_doc, section, chunk_index, content, domain, embed_version"
_BM25_PAGE = 2000  # 分页流式构建页大小（内存 O(page)）


def _bm25_cache_get(conn, branches, domain, doc_names):
    """取 BM25 缓存 (engine, rows)。未命中/指纹过期返回 None（调用方负责重建）。

    domain 不参与缓存 key：BM25 索引按 (branches, doc_names) 建全量，
    domain 过滤由调用方在 Python 侧裁剪（domain 子句不改变 BM25 统计）。
    """
    fp = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id),0) FROM document_chunks").fetchone()
    key = (tuple(branches or ()),
           tuple(sorted(doc_names or ())), (fp[0], fp[1]))
    hit = _BM25_CACHE.get(key)
    if hit is not None:
        return hit
    # 重建：按范围分页流式加载（列裁剪，不取 embedding 大字段）
    from knowledge_pipeline import _branch_clause, _doc_clause
    sql, params = _branch_clause(branches)
    dsql, dparams = _doc_clause(doc_names)
    base = "SELECT {cols} FROM document_chunks WHERE content != ''".format(cols=_CHUNK_COLS)
    total = conn.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE content != ''" + sql + dsql,
        params + dparams).fetchone()[0]
    engine = BM25Engine()
    rows = []
    for off in range(0, total, _BM25_PAGE):
        page = conn.execute(base + sql + dsql + " LIMIT ? OFFSET ?",
                            params + dparams + [_BM25_PAGE, off]).fetchall()
        if not page:
            break
        page_rows = [dict(r) for r in page]
        rows.extend(page_rows)
        engine.extend(page_rows)
    engine._avg_len = engine._total_len / len(rows) if rows else 0.0
    cached = (engine, rows)
    if len(_BM25_CACHE) >= _BM25_CACHE_MAX:
        _BM25_CACHE.pop(next(iter(_BM25_CACHE)))
    _BM25_CACHE[key] = cached
    return cached


# 2026-09-17 S4：默认 top_k 5 → 4（与 RAG 消费侧 chunk_hits[:3] 对齐；显式传参的调用点不受影响）
def hybrid_search(conn, query: str, top_k: int = 4, bm25_weight: float = 0.3,
                  branches: list | None = None, domain: str | None = None,
                  glossary_boost: float = 1.0, doc_names: list | None = None) -> dict:
    """KB-P1：混合检索（BM25 稀疏 + 向量稠密权重融合）+ 轻量重排。

    P0-2/P0-4/P1-2 行业对齐升级：
    - domain：域过滤（AWS Metadata Filtering）——限定检索范围，防跨域污染
    - glossary_boost：术语命中加权（≥1 时按分排序前乘）
    - 融合改为 RRF（Reciprocal Rank Fusion）：按排名合并，免调参（替代固定 0.7/0.3）
    - 每条 hit 增加 recall_reason（可解释路由）
    - doc_names：kb_scope.docs 来源文档过滤（#标签 场景，None=不过滤）

    返回 {hits: [{score, source_doc, section, content, chunk_index, embed_version,
                  vec_score, bm25_score, domain, recall_reason}], bm25_count, vec_count}
    """
    from knowledge_engine import VectorEngine, BM25Engine
    from knowledge_pipeline import _branch_clause, _doc_clause

    def _domain_clause(d: str | None):
        if not d or d == "unknown" or d == "all":
            return "", []
        return " AND domain=?", [d]

    # P1-3：chunks 分页流式加载（列裁剪，不取 embedding 大字段）+ BM25 索引缓存
    sql, params = _branch_clause(branches)
    dsql, dparams = _domain_clause(domain)
    docs_sql, docs_params = _doc_clause(doc_names)
    bm25, rows = _bm25_cache_get(conn, branches, domain, doc_names)
    if not rows:
        return {"hits": [], "bm25_count": 0, "vec_count": 0, "mode": "hybrid"}
    # rows 来自缓存，但 domain 过滤可能裁剪部分行 → 展示/兜底一律用 filtered_rows
    if dsql:
        filtered_rows = [r for r in rows if r.get("domain", "unknown") == domain]
    else:
        filtered_rows = rows
    if not filtered_rows:
        return {"hits": [], "bm25_count": 0, "vec_count": 0, "mode": "hybrid"}
    rows = filtered_rows

    # 向量侧：优先消费存储向量（BGE-M3 等真 Embedding，按 embed_version 隔离维度）；
    # P2-1 矩阵化加速：numpy 批量余弦（无 numpy 自动降级逐行）
    # Embedder 不可用 / 无同版本向量 / 维度不一致 → 降级实时 bigram（与历史行为一致）
    import math as _mv
    vec_scores = {}
    try:
        from knowledge_pipeline import Embedder
        embedder = Embedder(conn)
        qv, version = embedder.embed_with_version([query])
        if qv and version != "bigram-tf":
            q = qv[0]
            vsql, vparams = _branch_clause(branches)
            vdsql, vdparams = _domain_clause(domain)
            vdocs_sql, vdocs_params = _doc_clause(doc_names)
            vrows = conn.execute(
                "SELECT id, embedding FROM document_chunks "
                "WHERE content != '' AND embedding != '[]' AND embed_version=?"
                + vsql + vdsql + vdocs_sql, [version] + vparams + vdparams + vdocs_params).fetchall()
            if vrows:
                allowed_ids = {vr["id"] for vr in vrows}
                from vector_index import ChunkVectorIndex
                mat = ChunkVectorIndex.search(conn, q, version, top_k=top_k * 2, only_ids=allowed_ids)
                if mat is not None:
                    vec_scores = {cid: s for s, cid in mat}
                else:  # 逐行降级（原逻辑）
                    qn = _mv.sqrt(sum(x * x for x in q)) or 1.0
                    for vr in vrows:
                        vec = json.loads(vr["embedding"] or "[]")
                        if not vec or len(vec) != len(q):
                            continue
                        vn = _mv.sqrt(sum(x * x for x in vec)) or 1.0
                        s = sum(a * b for a, b in zip(q, vec)) / (qn * vn)
                        if s > 0:
                            vec_scores[vr["id"]] = s
    except Exception as e:
        logger.warning("hybrid_search 向量侧失败，降级实时 bigram: %s", e)
    if not vec_scores:
        ve = VectorEngine()
        qv = ve._vector(query)
        for r in rows:
            s = ve._cosine(qv, ve._vector(r.get("content", "")))
            if s > 0:
                vec_scores[r["id"]] = s

    # BM25 侧（P1-3：索引已按范围缓存，命中零重建；未命中分页流式构建）
    bm25_hits = bm25.search(query, top_k=top_k * 2)
    bm25_scores = {h["chunk_id"]: h["score"] for h in bm25_hits}

    # P2-1 Reverse HyDE 兜底：当向量/BM25 命中过少时，用 chunk 假设问题匹配（用户说法≠文档措辞）
    # v2：优先消费 hyde_embedding（真向量，同版本余弦）；无真向量/维度不匹配 → bigram 兜底
    # P1-3：HyDE 限定候选集（bm25 ∪ vec 的 id），不再全量扫描 hyde 行
    hyde_scores = {}
    hyde_cand = set(vec_scores) | set(bm25_scores)
    if hyde_cand:
        try:
            qlow = query.lower()
            marks = ",".join("?" * len(hyde_cand))
            hyde_rows = [dict(r) for r in conn.execute(
                f"SELECT id, hyde_questions, hyde_embedding, embed_version FROM document_chunks "
                f"WHERE id IN ({marks}) AND hyde_questions != '[]' AND hyde_questions != ''" + sql,
                list(hyde_cand) + params).fetchall()]
            if hyde_rows:
                ve2 = VectorEngine()
                qv2 = ve2._vector(query)
                # 尝试真向量（embed_version 为 openai-compat 且 hyde_embedding 非空）
                hv_map = {hr["id"]: hr for hr in hyde_rows if (hr.get("hyde_embedding") or "") not in ("", "[]")}
                if hv_map and len(hv_map) >= max(1, len(hyde_rows) // 2):
                    try:
                        from knowledge_pipeline import Embedder
                        import math as _mh
                        embedder = Embedder(conn)
                        hqv, hver = embedder.embed_with_version([query], batch_size=0)
                        if hqv and hver != "bigram-tf":
                            hq = hqv[0]
                            hqn = _mh.sqrt(sum(x * x for x in hq)) or 1.0
                            for hr in hyde_rows:
                                hv = json.loads(hr.get("hyde_embedding") or "[]")
                                if hv and hr.get("embed_version") == hver and len(hv) == len(hq):
                                    hvn = _mh.sqrt(sum(x * x for x in hv)) or 1.0
                                    s = sum(a * b for a, b in zip(hq, hv)) / (hqn * hvn)
                                    if s > 0:
                                        hyde_scores[hr["id"]] = s
                    except Exception:
                        hyde_scores = {}
                # bigram 兜底（无真向量 或 部分块无 hyde_embedding）
                if not hyde_scores:
                    for hr in hyde_rows:
                        try:
                            questions = json.loads(hr["hyde_questions"] or "[]")
                        except Exception:
                            questions = []
                        best = 0.0
                        for qtext in questions[:3]:
                            s = ve2._cosine(qv2, ve2._vector(str(qtext)))
                            best = max(best, s)
                        if best > 0:
                            hyde_scores[hr["id"]] = best
        except Exception as e:
            logger.warning("hybrid_search HyDE 侧失败，跳过: %s", e)

    # ── RRF 融合（P1-2：按排名合并，替代固定权重）──
    # RRF score = Σ 1/(k + rank)，k=60（行业常用）；仅对双方都召回的合并，单侧命中也纳入
    def _rrf(ranks_a: dict, ranks_b: dict, k: int = 60) -> dict:
        fused = {}
        for cid, rank in ranks_a.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
        for cid, rank in ranks_b.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
        return fused

    vec_ranked = sorted(vec_scores.items(), key=lambda x: x[1], reverse=True)
    bm25_ranked = sorted(bm25_scores.items(), key=lambda x: x[1], reverse=True)
    rrf = _rrf(
        {cid: i + 1 for i, (cid, _) in enumerate(vec_ranked[:top_k * 2])},
        {cid: i + 1 for i, (cid, _) in enumerate(bm25_ranked[:top_k * 2])},
    )
    if not rrf:
        return {"hits": [], "bm25_count": len(bm25_scores), "vec_count": len(vec_scores),
                "mode": "hybrid"}

    # glossary 加权（P0-1：命中术语的 domain 内文档加权）+ Reverse HyDE 补充分
    scored = []
    by_id = {r["id"]: r for r in rows}
    for cid, score in rrf.items():
        r = by_id.get(cid)
        if not r:
            continue
        final = score
        boost = 1.0
        if glossary_boost and glossary_boost > 1.0:
            boost = glossary_boost
            final *= boost
        final += (hyde_scores.get(cid, 0.0) * 0.05)  # HyDE 弱辅助（5% 权重）
        scored.append((final, r, vec_scores.get(cid, 0.0), bm25_scores.get(cid, 0.0),
                       boost, hyde_scores.get(cid, 0.0)))
    scored.sort(key=lambda x: x[0], reverse=True)

    # 召回原因回显（P0-4）
    from glossary import format_recall_reason, GlossaryMatcher
    matched = GlossaryMatcher(conn).match(query) if domain else []
    hits = []
    for final, r, vs, bs, boost, hs in scored[:top_k]:
        # P2-F 可解释性：置信度等级（基于真向量相似度 vs，0-1 区间区分度高；无向量用 bm25 归一）
        _ref = vs if vs > 0 else (min(bs / 50.0, 1.0) if bs > 0 else 0.0)
        conf_level = "高" if _ref >= 0.70 else ("中" if _ref >= 0.45 else "低")
        hit = {
            "score": round(final, 3),
            "confidence_level": conf_level,
            "vec_score": round(vs, 3),
            "bm25_score": round(bs, 3),
            "hyde_score": round(hs, 3),
            "document_id": r.get("document_id"),
            "source_doc": r.get("source_doc", ""),
            "section": r.get("section", ""),
            "chunk_index": r.get("chunk_index"),
            "content": r.get("content", "")[:300],
            "embed_version": r.get("embed_version", ""),
            "domain": r.get("domain", "unknown"),
            "glossary_boost": round(boost, 2) if boost > 1.0 else None,
        }
        hit["recall_reason"] = format_recall_reason(hit, matched) or (
            f"RRF 融合(vec#{round(vs,3)} + bm25#{round(bs,3)})" + (" + HyDE" if hs > 0 else "")
        )
        hits.append(hit)

    # V2.6 LLM Rerank（用户拍板新增）：RRF 候选 → LLM 相关性打分重排。
    # 开关 rag.rerank_enabled（默认开）；LLM 不可用/超时/解析失败静默回退原排序，不阻断检索主链路。
    try:
        from services.rag_rerank import llm_rerank
        hits = llm_rerank(query, hits, top_k=top_k)
    except Exception:
        pass
    return {"hits": hits, "bm25_count": len(bm25_scores), "vec_count": len(vec_scores),
            "mode": "hybrid", "rrf": True}
