"""P2 语义缓存：高频相似查询 embedding 命中直返（LLM 不参与）。

对齐行业 Semantic Caching：查询向量化 → 与缓存 query 向量余弦 > threshold（默认 0.95）
→ 直接返回缓存答案（100% 省 token）。仅用于纯问答类意图（chat/knowledge_qa），
不涉及工具调用/写操作；真向量不可用时自动跳过（bigram 易误命中，不做语义缓存）。
"""
import json
import math


class SemanticCache:

    @staticmethod
    def get(conn, query: str, threshold: float = 0.95) -> str | None:
        """命中返回缓存答案（更新 hit_count）；未命中/不可用返回 None。"""
        try:
            if not query:
                return None
            rows = conn.execute(
                "SELECT id, query_embedding, embed_version, answer FROM semantic_cache").fetchall()
            if not rows:
                return None
            from knowledge_pipeline import Embedder
            qvec, qver = Embedder(conn).embed_with_version([query], batch_size=0)
            if qver == "bigram-tf" or not qvec:
                return None
            qvec = qvec[0]  # embed_with_version 返回外层列表，取单向量
            qn = math.sqrt(sum(x * x for x in qvec)) or 1.0
            best, best_id, best_s = None, None, 0.0
            for r in rows:
                if r["embed_version"] != qver:
                    continue
                try:
                    sv = json.loads(r["query_embedding"] or "[]")
                except Exception:
                    continue
                if not sv or len(sv) != len(qvec):
                    continue
                vn = math.sqrt(sum(x * x for x in sv)) or 1.0
                s = sum(a * b for a, b in zip(qvec, sv)) / (qn * vn)
                if s > best_s:
                    best_s, best, best_id = s, r["answer"], r["id"]
            if best is not None and best_s >= threshold:
                conn.execute(
                    "UPDATE semantic_cache SET hit_count=hit_count+1, "
                    "last_hit_at=datetime('now','localtime') WHERE id=?", (best_id,))
                conn.commit()
                return best
        except Exception:
            pass
        return None

    @staticmethod
    def put(conn, query: str, answer: str, max_entries: int = 2000) -> None:
        """写入缓存（真向量化；超上限按 id 淘汰最旧）。失败静默，不影响主流程。"""
        try:
            if not query or not answer:
                return
            from knowledge_pipeline import Embedder
            qvec, qver = Embedder(conn).embed_with_version([query], batch_size=0)
            if qver == "bigram-tf" or not qvec:
                return
            conn.execute(
                "INSERT INTO semantic_cache (query, query_embedding, embed_version, answer) VALUES (?,?,?,?)",
                (str(query)[:300], json.dumps(qvec[0]), qver, str(answer)[:4000]))
            try:
                cnt = conn.execute("SELECT COUNT(*) c FROM semantic_cache").fetchone()["c"]
                if cnt > max_entries:
                    conn.execute(
                        "DELETE FROM semantic_cache WHERE id IN "
                        "(SELECT id FROM semantic_cache ORDER BY id LIMIT ?)", (cnt - max_entries,))
            except Exception:
                pass
            conn.commit()
        except Exception:
            pass


# 便捷入口
semantic_cache = SemanticCache()
