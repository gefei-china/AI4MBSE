"""向量检索加速：numpy 矩阵批量余弦（零强制依赖，numpy 缺失自动降级逐行）。

现状问题（P2-1）：
- document_chunks.embedding 存 JSON 文本，检索时逐行 json.loads + 双重 sqrt
  —— 831 条可忍，万条每次查询 O(N*dim) 逐行 Python 循环必崩。
- 本模块：numpy 可用时，按 (embed_version, dim) 将向量载入 float32 矩阵
  （加载时一次归一化，幂等），查询 = 单次 matmul + 归一化点积，向量化 10-100 倍加速。
- numpy 不可用 / 维度不匹配 → search 返回 None，调用方走原逐行逻辑（行为不变）。

缓存：模块级 _MATRICES 常驻（数据变更后调用 ChunkVectorIndex.invalidate()）。
"""
import json

try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:  # 生产环境无 numpy 时优雅降级
    _np = None
    _HAS_NUMPY = False


class ChunkVectorIndex:
    """按 (embed_version, dim) 分组的归一化向量矩阵 + 批量余弦检索。"""

    _MATRICES = {}  # (version, dim) -> {"m": float32 ndarray(单位行向量), "ids": [chunk_id]}

    @classmethod
    def available(cls) -> bool:
        return _HAS_NUMPY

    @classmethod
    def invalidate(cls) -> None:
        """数据变更（ingest/reindex/删除 chunk）后调用，下次检索重建。"""
        cls._MATRICES.clear()

    @classmethod
    def stats(cls) -> dict:
        return {"numpy": _HAS_NUMPY,
                "groups": {f"{v}/{d}": len(g["ids"]) for (v, d), g in cls._MATRICES.items()}}

    @classmethod
    def _ensure_group(cls, conn, version: str, dim: int) -> bool:
        """加载指定 (version, dim) 组（首次检索时全量载入，之后缓存）。"""
        key = (version, dim)
        if key in cls._MATRICES:
            return True
        if not _HAS_NUMPY:
            return False
        rows = conn.execute(
            "SELECT id, embedding FROM document_chunks "
            "WHERE content != '' AND embedding != '[]' AND embed_version=?",
            (version,)).fetchall()
        ids, vecs = [], []
        for r in rows:
            try:
                v = json.loads(r["embedding"])
            except Exception:
                continue
            if not isinstance(v, list) or len(v) != dim:
                continue
            ids.append(r["id"])
            vecs.append(v)
        if not ids:
            return False
        m = _np.array(vecs, dtype=_np.float32)
        norms = _np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        m = m / norms  # 单位向量（幂等：预归一化落库后再次归一化无副作用）
        cls._MATRICES[key] = {"m": m, "ids": ids}
        return True

    @classmethod
    def search(cls, conn, query_vec: list, version: str, top_k: int = 5,
               only_ids: set | None = None) -> list | None:
        """批量余弦检索，返回 [(score, chunk_id), ...] 降序（score>0）。

        - only_ids：分支/文档过滤后的允许 id 集合（None=不过滤）
        - 返回 None 表示不可用（无 numpy / 无同版本同维度向量）→ 调用方降级逐行
        """
        if not _HAS_NUMPY or not query_vec or not isinstance(query_vec, (list, tuple)):
            return None
        q = _np.asarray(query_vec, dtype=_np.float32)
        dim = len(q)
        if not cls._ensure_group(conn, version, dim):
            return None
        g = cls._MATRICES.get((version, dim))
        if not g:
            return None
        m, ids = g["m"], g["ids"]
        qn = float(_np.linalg.norm(q))
        if qn == 0:
            return None
        scores = (m @ q) / qn  # 单位行向量 · q / |q| = 余弦
        order = _np.argsort(-scores)
        out = []
        for idx in order:
            cid = ids[int(idx)]
            if only_ids is not None and cid not in only_ids:
                continue
            s = float(scores[int(idx)])
            if s > 0:
                out.append((s, cid))
            if len(out) >= top_k:
                break
        return out or None


def normalize_vector(vec: list) -> list:
    """向量归一化为单位向量（幂等）。非数值/零向量原样返回。"""
    try:
        n = sum(x * x for x in vec) ** 0.5
    except Exception:
        return vec
    if not n:
        return vec
    return [x / n for x in vec]
