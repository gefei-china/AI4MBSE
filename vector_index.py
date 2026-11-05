"""向量检索加速：二进制矩阵缓存 + numpy 批量余弦（零强制依赖，缺失自动降级）。

现状问题（P2-1 → L2，2026-10-10 实测定位）：
- document_chunks.embedding 存 **JSON 文本**，检索时逐行 json.loads。
  ★ **实测占比（6727 行 × 1024 维）**：
    json.loads  **2135~2360ms（占 91%）** │ numpy 建矩阵 180ms │ 归一化 45ms
    ⇒ 瓶颈**不在检索、也不在矩阵运算**，而在"把 JSON 文本解析成数字"。
- L2 解法：**把向量另存为 float32 二进制（.npy）+ np.load(mmap_mode='r')**
  实测 **8.8ms**（vs json.loads 2135ms，**≈243 倍**），**零新依赖**（numpy 已在）。
  ⚠️ **JSON 仍是权威源**：二进制只是**可重建的派生缓存**，
  与 document_chunks.embedding 一致才用、不一致/缺失就重建 ⇒ 数据不出本库、可秒级重建。

架构（与 graph_db 同一定位：权威源 + 可弃缓存）：
    SQLite document_chunks.embedding(JSON)  ← 权威源
              │ rebuild（唯一写入口）
              ▼
    tmp/vec_cache/{version}_{dim}.npy + .ids.npy   ← 派生缓存，可随时删

- numpy 不可用 / 维度不匹配 / 缓存损坏 → search 返回 None，
  调用方走原逐行 json.loads 逻辑（**行为不变**）。

缓存失效：模块级 _MATRICES 常驻；数据变更后调用 ChunkVectorIndex.invalidate()。

══════════════════════════════════════════════════════════════════════
★ 2026-11-02 事故修复：陈旧缓存导致"重算向量没生效"（真实发生，损失一整轮结论）
══════════════════════════════════════════════════════════════════════
**现象**：`tools/reembed_degraded.py --apply` 报告成功（720 条降级向量已重算，
`audit_embed_degradation` 显示降级 720→0），但检索里这些块的 `vec_score` 恒为 **0.0**。
实测：中文 query「自主可控要求对组件部署有什么限制」→ 目标块 `847:51`
余弦相似度 **0.6111（全局最高）**，却在检索结果里排第 6 且 `vec_score=0.0`，
位置被相似度只有 0.497 的英文块占着。

**根因（两条，缺一不可）**：
  ① `_load_binary()` 成功后**只校验 shape，不校验行集**：
       磁盘缓存 6457 行（2026-10-28） vs 数据库 7179 行（今日重算后）
       差的 722 行正是被重算的块 ⇒ 它们在矩阵里根本不存在 ⇒ 余弦分 0
  ② `invalidate()` **只清内存 `_MATRICES`，不清磁盘 `.npy`** ——
     原注释写"精确一致性由 invalidate() 在写入后显式清理保证"，**这句是错的**：
     清完内存下次仍从同一个 `.npy` 重载，于是**永远用旧向量**。
     而 `_rows_fingerprint()` 定义了却**从未被调用**（作者想到了但没接上）。

**为什么难发现**：缓存"能加载成功"、检索"不报错"、分数"看起来只是低"。
只有把余弦相似度单独算出来，才能看出"最高分的块竟然没被召回"。

**修法（双保险）**：
  ① **行数校验**（`_ensure_group`）：二进制行数必须等于数据库同版本行数，
     不等则丢弃缓存、走 JSON 重建。零风险，只多一次 `SELECT COUNT(*)`（进程首次加载时）。
  ② **epoch 后缀**（`_cache_paths`）：写入方改完向量调 `bump_epoch()`，
     缓存文件名随之变化 ⇒ 旧文件被**自然绕过**。
     为什么用 epoch 而不是删文件：本环境 `os.remove` 被 safe-delete 策略拦截，
     `drop_disk_cache()` 会**静默返回 0**（"删不掉就算了"）⇒ 依赖删除的方案在这里不可靠。
"""
import hashlib
import json
import os

try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:  # 生产环境无 numpy 时优雅降级
    _np = None
    _HAS_NUMPY = False

_ROOT = os.path.dirname(os.path.abspath(__file__))
#: 二进制缓存目录（与权威源分离；删掉不影响正确性，只影响首次检索耗时）
CACHE_DIR = os.path.join(_ROOT, "tmp", "vec_cache")


def _epoch_path() -> str:
    return os.path.join(CACHE_DIR, "epoch.txt")


def _read_epoch() -> str:
    """读缓存纪元。无文件/读失败 → "0"（**不清空、不报错**：读不到只意味着用初始纪元）。"""
    try:
        with open(_epoch_path(), encoding="utf-8") as f:
            v = (f.read() or "").strip()
        return v if v else "0"
    except Exception:
        return "0"


def bump_epoch() -> str:
    """**写入路径的责任**：改完向量后调用，让所有磁盘缓存被自然绕过。

    为什么需要它而 `invalidate()` 不够：`invalidate()` 只清进程内存，
    下次仍从同一 `.npy` 重载 ⇒ 原地更新向量（行数不变）时永远检测不到。
    改文件名则无需删除（绕开 safe-delete 拦截）。
    返回新纪元字符串。
    """
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        cur = _read_epoch()
        try:
            nxt = str(int(cur) + 1)
        except Exception:
            nxt = "1"
        with open(_epoch_path(), "w", encoding="utf-8") as f:
            f.write(nxt)
        ChunkVectorIndex._MATRICES.clear()
        return nxt
    except Exception:
        # 写不进纪元只意味着"缓存可能陈旧"，不该让调用方失败
        return _read_epoch()


def _cache_paths(version: str, dim: int) -> tuple:
    # epoch 进文件名：写入方 bump 后旧文件自动失效（无需删除）
    tag = f"{_safe(version)}_{dim}_{_safe(_read_epoch())}"
    return (os.path.join(CACHE_DIR, tag + ".npy"),
            os.path.join(CACHE_DIR, tag + ".ids.npy"))


def _safe(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(s))[:48]


class ChunkVectorIndex:
    """按 (embed_version, dim) 分组的归一化向量矩阵 + 批量余弦检索。"""

    _MATRICES = {}   # (version, dim) -> {"m": float32 ndarray(单位行向量), "ids": [chunk_id]}
    _STATS = {"hit_bin": 0, "hit_json": 0, "miss": 0}   # 命中率观测

    @classmethod
    def available(cls) -> bool:
        return _HAS_NUMPY

    @classmethod
    def invalidate(cls) -> None:
        """数据变更（ingest/reindex/删除 chunk）后调用，下次检索重建。"""
        cls._MATRICES.clear()

    @classmethod
    def drop_disk_cache(cls, version: str = "", dim: int = 0) -> int:
        """删磁盘二进制缓存（下次检索从 JSON 重建）。返回删除文件数。

        ⚠️ 环境里 `os.remove` 可能被安全策略拦截（safe-delete）⇒ 捕获后静默跳过：
           删不掉**只影响磁盘占用，不影响正确性**（下次仍会 hit_json 重建并覆盖写）。
           ⚠️ 不要在这里 raise——否则"清缓存"这种运维动作会把检索打挂。
        """
        if not os.path.isdir(CACHE_DIR):
            return 0
        n = 0
        for f in os.listdir(CACHE_DIR):
            if (not version or version in f) and (not dim or f.endswith(f"_{dim}.npy")
                                                 or f.endswith(f"_{dim}.ids.npy")):
                try:
                    os.remove(os.path.join(CACHE_DIR, f))
                    n += 1
                except Exception:
                    pass       # 删不掉就算了：不影响正确性
        return n

    @classmethod
    def stats(cls) -> dict:
        return {"numpy": _HAS_NUMPY,
                "cache_dir": CACHE_DIR,
                "load": dict(cls._STATS),
                "groups": {f"{v}/{d}": len(g["ids"]) for (v, d), g in cls._MATRICES.items()}}

    # ── 磁盘二进制缓存（L2 新增）──────────────────────────────────────
    @classmethod
    def _load_binary(cls, version: str, dim: int):
        """尝试 np.load(mmap)。返回 (matrix, ids) 或 None。"""
        if not _HAS_NUMPY:
            return None
        mp, ip = _cache_paths(version, dim)
        if not (os.path.exists(mp) and os.path.exists(ip)):
            return None
        try:
            m = _np.load(mp, mmap_mode="r")
            ids = _np.load(ip)
            if m.ndim != 2 or m.shape[1] != dim or len(ids) != m.shape[0]:
                return None
            # 一维字符串数组 → python list（逐块 id 是 int）
            return m, [int(x) for x in ids.tolist()]
        except Exception:
            return None

    @classmethod
    def _save_binary(cls, version: str, dim: int, mat, ids: list) -> bool:
        """原子写入（先写 .tmp 再 rename）——避免并发读到半截文件。

        ⚠️ **踩过的坑**：`np.save(path,...)` 会在**路径末尾自动补 `.npy`**，
           传 "x.npy.tmp" 会写成 "x.npy.tmp.npy" ⇒ `os.replace` 找不到源文件
           ⇒ 静默失败 ⇒ 下次仍走 JSON（实测 3242ms，缓存形同虚设）。
        ⇒ 这里用**已带 .npy 的临时名**，并显式断言源文件存在后再 rename。
        """
        if not _HAS_NUMPY:
            return False
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            mp, ip = _cache_paths(version, dim)
            # np.save 会补 .npy ⇒ 临时名**必须已以 .npy 结尾**，否则又被追加一层
            tmpm, tmpi = mp[:-4] + ".tmp.npy", ip[:-4] + ".tmp.npy"
            _np.save(tmpm, mat)
            _np.save(tmpi, _np.asarray(ids, dtype=_np.int64))
            if not (os.path.exists(tmpm) and os.path.exists(tmpi)):
                return False
            os.replace(tmpm, mp)
            os.replace(tmpi, ip)
            return True
        except Exception:
            return False

    @classmethod
    def _rows_fingerprint(cls, rows) -> str:
        """行集指纹（id + embedding 长度），用于判定磁盘缓存是否过期。"""
        h = hashlib.md5()
        for r in rows:
            h.update(f"{r['id']}:{len(r['embedding'] or '')};".encode())
        return h.hexdigest()

    @classmethod
    def _db_group_count(cls, conn, version: str):
        """数据库里该版本"应进矩阵"的行数（与重建时的 WHERE 条件**必须一致**）。

        ⚠️ 条件必须和 ② 里重建用的 SQL 逐字相同，否则会出现"永远认为缓存陈旧"
        或"永远认为缓存新鲜"的假判据 —— 这正是"同一口径写两遍"的坑。
        异常时返回 None（=不做校验，退化为旧行为，比误判重建安全）。
        """
        try:
            r = conn.execute(
                "SELECT COUNT(*) FROM document_chunks "
                "WHERE content != '' AND embedding != '[]' AND embed_version=?",
                (version,)).fetchone()
            return int(r[0]) if r else None
        except Exception:
            return None

    @classmethod
    def _ensure_group(cls, conn, version: str, dim: int) -> bool:
        """加载指定 (version, dim) 组。优先二进制缓存，其次 JSON 重建。"""
        key = (version, dim)
        if key in cls._MATRICES:
            return True
        if not _HAS_NUMPY:
            return False

        # ① 先试磁盘二进制（≈8ms）
        got = cls._load_binary(version, dim)
        if got is not None:
            m, ids = got
            # ★ 2026-11-02：**必须校验行数**，不能"能加载就用"。
            #   实测事故：缓存 6457 行 / 数据库 7179 行（差 722 = 被重算的块），
            #   缺的块在矩阵里不存在 ⇒ 余弦分 0 ⇒ 明明最高相似度却排不上来。
            #   原注释"精确一致性由 invalidate() 保证"是错的（它不清磁盘），
            #   故此处用**数据库行数**做权威对账；不一致就丢弃缓存走 JSON 重建。
            #   代价：进程首次加载多一次 COUNT(*)（毫秒级），且只在 _MATRICES 未命中时发生。
            want = cls._db_group_count(conn, version)
            if want is not None and want != len(ids):
                cls._STATS["stale_bin"] = cls._STATS.get("stale_bin", 0) + 1
                cls._STATS["stale_detail"] = {
                    "version": version, "dim": dim,
                    "cached": len(ids), "db": want, "diff": want - len(ids)}
                # 落到 ② 用 JSON 重建（不 return，继续往下）
            else:
                cls._MATRICES[key] = {"m": m, "ids": ids}
                cls._STATS["hit_bin"] += 1
                return True

        # ② 回退：从 JSON 权威源重建（≈2.4s），并写回二进制
        rows = conn.execute(
            "SELECT id, embedding FROM document_chunks "
            "WHERE content != '' AND embedding != '[]' AND embed_version=?",
            (version,)).fetchall()
        if not rows:
            cls._STATS["miss"] += 1
            return False
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
            cls._STATS["miss"] += 1
            return False
        m = _np.array(vecs, dtype=_np.float32)
        norms = _np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        m = m / norms  # 单位向量（幂等：预归一化落库后再次归一化无副作用）
        cls._MATRICES[key] = {"m": m, "ids": ids}
        cls._STATS["hit_json"] += 1
        # 写回二进制（下次进程启动 ≈8ms 而非 ≈2.4s）
        # ★失败无所谓：只是慢，不影响正确性
        cls._save_binary(version, dim, m, ids)
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
