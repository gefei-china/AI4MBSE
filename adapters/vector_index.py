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

══════════════════════════════════════════════════════════════════════
★ 2026-11-26 事故修复：mmap 跨进程文件锁 ⇒ 缓存写不进去，门禁"时红时绿"
══════════════════════════════════════════════════════════════════════
**现象**：门禁 `verify_vector_binary_cache` 单独跑绿、批量/真机跑红
（`hit_bin 增量=+0`），且 `tmp/vec_cache/` 里散落 12 个 `*.tmp.npy` 半成品。

**根因（受控实验，两条互相咬合）**：
  ① `_load_binary` 用 `np.load(mmap_mode="r")` ⇒ **Windows 下文件被映射期间不允许替换**。
     实测：另起一个进程持有 mmap 后，本进程 `os.replace` 立刻
     `PermissionError [WinError 5]`；而对运行中的服务用 `os.rename` 探针得到
     `LOCKED PermissionError 32`（服务正持有 `openai-compat_1024_0.npy`）。
  ② `_save_binary` 的 `except Exception: return False` **静默吞掉**该失败 ⇒
     缓存永远更新不了 ⇒ 只要"磁盘缓存与库行数不一致"（插入/重算后必然如此），
     每次检索都判陈旧→重建→写盘失败→下次还陈旧 ⇒ `hit_bin` **恒 +0**。
     更精确：`_ensure_group` 里 `got = cls._load_binary(...)` 之后 `got[0]` **一直持有映射**，
     直到函数返回 —— 而 `_save_binary` 就在返回之前调用 ⇒ **陈旧缓存的第一次重建写盘必失败**，
     与"有没有别的进程"无关。（跨进程只是让失败面更大：连正常保存也会被锁。）

**为什么算 flaky 而不是常红**：只有当「缓存陈旧」**且**「有进程正持有该文件」两件事
同时成立才红。服务没起 / 纪元刚 bump（新路径无人持有）时就是绿的 —— 红绿取决于时序。

**修法**：
  ① 改用**普通加载**（`np.load(mp)`，不走 mmap）：实测 4.6ms → 10.6ms，
     多花 6ms 换掉整个跨进程锁问题（缓存 29MB，本来也要读）。**别再改回 mmap。**
  ② 写盘失败**必须留痕**：`_STATS["save_fail"]` + 明细 + WARNING，
     不再静默 —— 静默失败是"红=有问题"这个信号失效的直接原因。
  ③ `bump_epoch()` 顺手**清掉更早的纪元文件**（保留新纪元 + 前 1 个）：
     实测累积 85 个纪元 ≈ **2.6 GB** 磁盘占用，且删不掉（safe-delete）。
"""
import hashlib
import json
import logging
import os

_log = logging.getLogger(__name__)

try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:  # 生产环境无 numpy 时优雅降级
    _np = None
    _HAS_NUMPY = False

from core.paths import repo_root  # 2026-11-25 P1：仓库根单点解析
_ROOT = repo_root()
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
        _prune_old_epochs(keep=(nxt, cur))
        return nxt
    except Exception:
        # 写不进纪元只意味着"缓存可能陈旧"，不该让调用方失败
        return _read_epoch()


def _prune_old_epochs(keep: tuple, keep_extra: int = 1) -> int:
    """清掉更早纪元的缓存文件，只保留 `keep` 里的纪元 + 前 `keep_extra` 个。

    为什么需要：epoch 进文件名后旧文件**没人再读**，但每对 ≈29MB。
    实测累积 **85 个纪元 / 2.6 GB** —— 而且本环境 `os.remove` 被 safe-delete 拦，
    `drop_disk_cache()` 只会静默返回 0 ⇒ 只能在这里顺手清。
    best-effort：删不掉就当无事发生（只影响磁盘占用，不影响正确性）。
    """
    try:
        if not os.path.isdir(CACHE_DIR):
            return 0
        keepset = {str(k) for k in keep if str(k)}
        epochs = set()
        for f in os.listdir(CACHE_DIR):
            parts = f.rsplit(".", 2)          # x.npy / x.ids.npy
            stem = parts[0] if len(parts) > 1 else f
            if "_" in stem:
                epochs.add(stem.rsplit("_", 1)[-1])
        try:
            ordered = sorted(epochs, key=lambda s: int(s))
        except Exception:
            return 0
        # 保留目标纪元 + 序号在它之前的 keep_extra 个
        idx = {e: i for i, e in enumerate(ordered)}
        protected = set(keepset)
        for e in keepset:
            i = idx.get(e)
            if i is None:
                continue
            protected.update(ordered[max(0, i - keep_extra):i + 1])
        n = 0
        for f in os.listdir(CACHE_DIR):
            if f == "epoch.txt":
                continue
            parts = f.rsplit(".", 2)
            stem = parts[0] if len(parts) > 1 else f
            if "_" not in stem:
                continue
            if stem.rsplit("_", 1)[-1] in protected:
                continue
            try:
                os.remove(os.path.join(CACHE_DIR, f))
                n += 1
            except Exception:
                pass                      # safe-delete 拦截等：删不掉就算了
        return n
    except Exception:
        return 0


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
    _STATS = {"hit_bin": 0, "hit_json": 0, "miss": 0, "save_fail": 0}   # 命中率/写盘失败观测

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
        """尝试 np.load（**普通加载，不用 mmap**）。返回 (matrix, ids) 或 None。

        ⚠️ 2026-11-26 实测：`mmap_mode="r"` 会让本进程持有文件映射，期间**任何进程的
        `os.replace` 写这个文件都会 PermissionError**（WinError 5/32）⇒ `_save_binary`
        静默失败 ⇒ 陈旧缓存永远修不掉 ⇒ 门禁 `hit_bin` 恒 0（时红时绿）。
        代价实测：mmap 4.6ms / 普通 10.6ms（29MB × 7079 行）——**6ms 换掉整类跨进程锁问题**。
        """
        if not _HAS_NUMPY:
            return None
        mp, ip = _cache_paths(version, dim)
        if not (os.path.exists(mp) and os.path.exists(ip)):
            return None
        try:
            m = _np.load(mp)          # ← 故意不用 mmap：见上面的实测说明
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

        ⚠️ 2026-11-26：失败**必须留痕**（`_STATS["save_fail"]` + WARNING）。
        原实现 `except: return False` 静默吞掉 —— 而 Windows 下目标文件被别的进程 mmap
        时 `os.replace` 恰好会抛 `PermissionError`，于是"缓存永远更新不了"这件事
        **在日志里一点痕迹都没有**，只剩一个恒为 0 的 `hit_bin`。
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
                cls._note_save_fail(version, dim, "tmp 未生成", (tmpm, tmpi))
                return False
            os.replace(tmpm, mp)
            os.replace(tmpi, ip)
            return True
        except Exception as e:
            cls._note_save_fail(version, dim, f"{type(e).__name__}: {e}",
                                (locals().get("tmpm"), locals().get("tmpi")))
            return False

    @classmethod
    def _note_save_fail(cls, version: str, dim: int, why: str, tmps=()) -> None:
        """写盘失败留痕（不抛异常：调用方只损失"下次快一点"）。

        同时 best-effort 清掉半成品 `.tmp.npy` —— 实测残留 12 个 × 29MB = 348MB，
        而且它们不会再被任何代码读到（`_load_binary` 只认最终名）。
        """
        cls._STATS["save_fail"] = cls._STATS.get("save_fail", 0) + 1
        cls._STATS["save_fail_detail"] = {"version": version, "dim": dim, "why": why[:200]}
        _log.warning("[vec_cache] 二进制缓存写盘失败（下次仍走 JSON 重建）：%s", why)
        for p in tmps:
            if not p:
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

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
