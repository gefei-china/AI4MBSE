"""可插拔 Embedding：OpenAI-compatible /embeddings → bigram TF 降级。"""
import json
import logging
import os
import re
import time
import uuid

logger = logging.getLogger(__name__)

# B2 修复：bigram 降级向量固定维度（md5 哈希映射，零外部依赖）。
# 旧实现 list(Counter.values()) 维度随文本词表漂移，落库后无法与任何其他向量比较；
# 现改为固定维度：特征名 md5 → [0, dim) 哈希槽，值=特征出现次数。
# 任意两文本同维度、同槽位同语义，余弦可比较；确定性（md5 无随机），回归可复现。
# P1-1b：维度可配置（embedding.bigram_dim，默认 4096；ENV: MBSE_EMBED_BIGRAM_DIM），
# 允许按部署环境调整哈希槽数（大规模语料可调高降低碰撞；失败降级默认 4096）。
try:
    from core.config import get as _cfg_get
    _BIGRAM_DIM = int(_cfg_get("embedding", "bigram_dim", 4096) or 4096)
except Exception:
    _BIGRAM_DIM = 4096


def _api_batch_max() -> int:
    """服务端 `/embeddings` 单批条数上限（实测阿里云百炼 text-embedding-v3 = **10**）。

    >10 时服务端直接 400：`InvalidParameter: batch size is invalid, it should not be
    larger than 10.`。每次调用现读配置，改 `embedding.api_batch_max` 即时生效；
    <=0 表示不切分（自建无限制服务可用）。
    """
    try:
        from core.config import get as _cfg2
        v = _cfg2("embedding", "api_batch_max", 10)
        # 注意不能写 int(v or 10)：那会把「0 = 不切分」吃掉（0 是 falsy）。
        if v is None:
            return 10
        return int(v)
    except Exception:
        return 10


def _bigram_feature_text(text: str) -> list:
    """字符 bigram 特征（与 knowledge_engine.VectorEngine._tokenize 对齐）。"""
    s = re.sub(r"[\s\W_]+", "", str(text).lower())
    if len(s) <= 1:
        return [s] if s else []
    return [s[i:i + 2] for i in range(len(s) - 1)]


def _bigram_vector_fixed(text: str, dim: int = _BIGRAM_DIM) -> list:
    """固定维度 bigram TF 向量：特征名 md5 → [0,dim)，值=出现次数。跨文本可比。"""
    import hashlib as _h
    vec = [0.0] * dim
    for feat in _bigram_feature_text(text):
        idx = int.from_bytes(_h.md5(feat.encode("utf-8")).digest()[:4], "little") % dim
        vec[idx] += 1.0
    return vec


class Embedder:
    """可插拔 Embedding：OpenAI-compatible /embeddings → bigram TF 降级。

    - 真 embedding：llm_providers 有 key 的 provider 走 POST {base_url}/embeddings
    - 降级：VectorEngine 字符 bigram TF（零外部依赖，确定性回归可用）
    """

    def __init__(self, conn=None):
        self._api = None  # (base_url, api_key, model)
        if conn is not None:
            self._probe_api(conn)

    # P2-1 缓存：60s 内不重复 probe（避免每次查询查 llm_providers 表）
    _PROBE_CACHE = None
    _PROBE_STAMP = 0.0
    _PROBE_TTL = 60.0

    # P2-1 query 向量缓存（单文本查询侧）：TTL 60s，上限 512 条
    _QUERY_CACHE = {}
    _QUERY_TTL = 60.0
    _QUERY_MAX = 512

    def _probe_api(self, conn) -> None:
        import time as _t
        if Embedder._PROBE_CACHE is not None and _t.time() - Embedder._PROBE_STAMP < Embedder._PROBE_TTL:
            self._api = Embedder._PROBE_CACHE
            return
        try:
            # 向量模型优先：model_type='embedding'；无则回退任意有 key 的 provider（兼容旧配置）
            row = conn.execute(
                "SELECT base_url, api_key, model_name FROM llm_providers "
                "WHERE status='active' AND api_key != '' AND model_type='embedding' "
                "ORDER BY is_default DESC, id LIMIT 1"
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT base_url, api_key, model_name FROM llm_providers "
                    "WHERE status='active' AND api_key != '' "
                    "ORDER BY is_default DESC LIMIT 1"
                ).fetchone()
            if row and row["api_key"]:
                self._api = (row["base_url"].rstrip("/"), row["api_key"], row["model_name"])
        except Exception:
            self._api = None
        Embedder._PROBE_CACHE = self._api
        Embedder._PROBE_STAMP = _t.time()

    def embed(self, texts: list) -> list:
        """批量向量化，返回 list[list[float]]。真 API 失败自动降级 bigram TF。"""
        if not texts:
            return []
        if self._api:
            try:
                return self._embed_api(texts)
            except Exception as e:
                logger.warning("embedding API 调用失败，降级 bigram: %s", e)
        return self._embed_bigram(texts)

    def embed_with_version(self, texts: list, batch_size: int = 8) -> tuple:
        """批量向量化并返回实际使用的版本标记（openai-compat / bigram-tf）。

        分批调用（每批 batch_size 条）：BGE-M3 等服务端有 batch/长度限制；
        batch_size<=0 表示不分批一次全发（语义出口等小集合场景减少往返）；
        任一批失败整体降级 bigram（保持检索确定性回归可用）。

        P2-1 query 缓存：真向量模式下单文本调用（查询侧）加 TTL 缓存，
        同 query 短时间重复检索直返，省一次 embedding API 往返（~300ms）。
        """
        if not texts:
            return [], "bigram-tf"
        # P2-1 query 缓存：单文本调用（查询侧）命中直返，省一次 embedding API 往返
        if self._api and len(texts) == 1:
            key = ("q", texts[0])
            hit = Embedder._QUERY_CACHE.get(key)
            if hit and time.time() - hit[1] < Embedder._QUERY_TTL:
                return [hit[0]], "openai-compat"
        if self._api:
            try:
                out = []
                if batch_size <= 0:
                    vecs = self._embed_api(texts)
                else:
                    for i in range(0, len(texts), batch_size):
                        out.extend(self._embed_api(texts[i:i + batch_size]))
                    vecs = out
                if len(texts) == 1:
                    Embedder._QUERY_CACHE[key] = (vecs[0], time.time())
                    if len(Embedder._QUERY_CACHE) > Embedder._QUERY_MAX:
                        Embedder._QUERY_CACHE.clear()
                return vecs, "openai-compat"
            except Exception as e:
                logger.warning("embedding API 调用失败，降级 bigram: %s", e)
        return self._embed_bigram(texts), "bigram-tf"

    def _embed_api(self, texts: list) -> list:
        """POST {base}/embeddings。**按服务端单批上限内部自动切分**（见 `_api_batch_max`）。

        为什么必须在**这一层**兜底：服务端对单次 `input` 的条数有硬上限（实测
        `text-embedding-v3` 为 10），而调用方可能传 `batch_size=0`（"一次全发"，
        见 `embed_with_version`）或直接把整批丢进来。此前 `semantic.py::_rank_dense`
        正是「items + query」一次全发 —— 本机 20 个 agent 时是 21 条 → 必 400 →
        `embed()` 的 except 吞掉后**静默降级 bigram**，把"语义匹配/语义兜底路由"
        悄悄变成"词面匹配"（实测 2026-09-19：一次建模会话里 19 条的整批调用即如此）。

        放在这一层后，任何调用方的超限输入都被切成合规批次，且**顺序保持不变**
        （逐段 extend + 段内按 index 排序），因此 `vecs[-1]` 仍是 query 的策略不受影响。
        """
        n = _api_batch_max()
        if n <= 0 or len(texts) <= n:
            return self._embed_api_once(texts)
        out = []
        for i in range(0, len(texts), n):
            out.extend(self._embed_api_once(texts[i:i + n]))
        return out

    def _embed_api_once(self, texts: list) -> list:
        import httpx
        base, key, model = self._api
        resp = httpx.post(base + "/embeddings",
                          json={"model": model, "input": texts},
                          headers={"Authorization": f"Bearer {key}"},
                          timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows = sorted(data["data"], key=lambda x: x["index"])
        return [r["embedding"] for r in rows]

    def _embed_bigram(self, texts: list) -> list:
        # B2 修复：固定维度哈希 bigram TF（替代旧 Counter.values() 维度漂移实现）
        return [_bigram_vector_fixed(t) for t in texts]

