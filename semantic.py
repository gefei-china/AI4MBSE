"""统一语义出口（P0）：真 embedding（可插拔）→ bigram 降级。

行业对齐（见 docs/industry-research-2026-08.md）：
- 语义路由的「嵌入信号」：embedding 相似度 + 阈值
- Semantic Tool Selection：top_k + similarity_threshold + 空回退

用途：意图路由 / Skill 匹配 / 工具 JIT 预筛 / 附件检索 / 记忆检索五路共用，
一处升级（配置 embedding provider，见 core.config 的 embedding 分组）全链路收益。

设计：
- rank(query, items, top_k, threshold, key)：返回 [(score, item)] 降序，仅 score > threshold
- 真向量：Embedder.embed_with_version 对 items+query 同一批向量化（维度一致）→ 余弦
- bigram 降级：VectorEngine Counter 余弦（稀疏交集，任意两文本可比；不消费 list-vector 存储）
- 空回退：无命中返回 []，调用方自行降级/LLM 兜底（不抛异常）
"""
import math


class SemanticSearch:
    """语义匹配执行器（进程内可复用；Embedder 惰性初始化，失败自动降级 bigram）。"""

    def __init__(self, conn=None):
        self._embedder = None
        # 最近一次 rank() **实际走的后端**：'dense'（真 embedding）| 'bigram'（降级路）。
        # 2026-09-19 新增：两路余弦**量纲不同**（同一对文本实测 top1 dense 0.6652 / bigram 0.3091），
        # 调用方必须据此选阈值，否则一套阈值套两路必有一路失准 → 见 docs 报告 §5-①。
        self.last_backend = "bigram"
        if conn is not None:
            self._ensure_embedder(conn)

    def _ensure_embedder(self, conn=None):
        if self._embedder is not None:
            return self._embedder
        try:
            from knowledge_pipeline import Embedder
            if conn is None:
                from database import get_db
                conn = get_db()
                own = True
            else:
                own = False
            try:
                self._embedder = Embedder(conn)
            finally:
                if own:
                    conn.close()
        except Exception:
            self._embedder = False  # 不可用标记
        return self._embedder

    def rank(self, query: str, items: list, top_k: int = 5, threshold: float = 0.0,
             key: str = "text") -> list:
        """对 items 语义打分排序，返回 [(score, item)] 降序（score > threshold）。

        items 元素可为 dict（取 key 字段文本）或 str（直接作文本）。
        真 embedding 可用时用真向量；否则 bigram 降级（score 量纲不同，threshold 按调用方经验）。

        ⚠️ **两路量纲不同**：调用方若要用「相似度门限」做判定，必须读 `self.last_backend`
        （或模块级 `semantic.last_backend()`）来选阈值 —— 这是 2026-09-19 修复
        「embedding 静默降级」后**首次真正生效的 dense 路**带来的一致性要求。
        """
        if not query or not items:
            self.last_backend = "bigram"
            return []
        pairs = []
        for it in items:
            t = it.get(key) if isinstance(it, dict) else str(it)
            if t and str(t).strip():
                pairs.append((it, str(t).strip()))
        if not pairs:
            self.last_backend = "bigram"
            return []
        # 总开关（embedding.enabled=False → 强制 bigram，Mock/离线确定性）
        try:
            from core import config as _cfg
            enabled = _cfg.as_bool("embedding", "enabled", True)
        except Exception:
            enabled = True
        if enabled:
            scored = self._rank_dense(query, pairs)
            if scored is not None:
                self.last_backend = "dense"
                if threshold:
                    scored = [s for s in scored if s[0] > threshold]
                return scored[:top_k] if top_k else scored
        self.last_backend = "bigram"
        scored = self._rank_bigram(query, pairs)
        if threshold:
            scored = [s for s in scored if s[0] > threshold]
        return scored[:top_k] if top_k else scored

    # ── 真向量路：items + query 同批向量化（维度一致）──
    def _rank_dense(self, query: str, pairs: list):
        try:
            ed = self._ensure_embedder()
            if not ed:
                return None
            texts = [t for _, t in pairs] + [query]
            # batch_size=0 = 一次全发（省往返）。**必须依赖 Embedder._embed_api 内部按
            # embedding.api_batch_max 切分**：本方法把「所有 items + query」塞进一次调用，
            # items 一多就超服务端单批上限（实测 10）→ 400 → 静默降级 bigram，
            # "语义匹配"会悄悄退化成"词面匹配"（2026-09-19 实测：19 条即触发）。
            vecs, version = ed.embed_with_version(texts, batch_size=0)
            if version == "bigram-tf" or not vecs or len(vecs) != len(texts):
                return None
            qv = vecs[-1]
            qn = math.sqrt(sum(x * x for x in qv)) or 1.0
            out = []
            for (it, _), v in zip(pairs, vecs[:-1]):
                if not v or len(v) != len(qv):
                    continue
                vn = math.sqrt(sum(x * x for x in v)) or 1.0
                s = sum(a * b for a, b in zip(qv, v)) / (qn * vn)
                out.append((s, it))
            out.sort(key=lambda x: x[0], reverse=True)
            return out
        except Exception:
            return None

    # ── bigram 降级路：Counter 稀疏余弦（任意两文本可比）──
    @staticmethod
    def _rank_bigram(query: str, pairs: list):
        from knowledge_engine import VectorEngine
        ve = VectorEngine()
        qv = ve._vector(query)
        out = []
        for it, t in pairs:
            s = ve._cosine(qv, ve._vector(t))
            if s > 0:
                out.append((s, it))
        out.sort(key=lambda x: x[0], reverse=True)
        return out


# 便捷函数（进程级默认实例）
_default = SemanticSearch()


def rank_items(query: str, items: list, top_k: int = 5, threshold: float = 0.0,
               key: str = "text", conn=None) -> list:
    """模块级便捷入口：semantic.rank_items(query, items, top_k, threshold, key)。"""
    if conn is not None:
        return SemanticSearch(conn).rank(query, items, top_k, threshold, key)
    return _default.rank(query, items, top_k, threshold, key)


def last_backend() -> str:
    """最近一次**模块级** `rank_items(...)` 实际走的后端：'dense' | 'bigram'。

    调用方据此选阈值（两路量纲不同）。注意 `rank_items(..., conn=<连接>)` 会新建实例、
    不更新这里的值 —— 那种调用请自行持有 `SemanticSearch` 实例读其 `.last_backend`。
    """
    return _default.last_backend
