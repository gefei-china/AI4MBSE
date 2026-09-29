"""统一语义出口（P0）：真 embedding（可插拔）→ bigram 降级。

行业对齐（见 docs/industry-research-2026-08.md）：
- 语义路由的「嵌入信号」：embedding 相似度 + 阈值
- Semantic Tool Selection：top_k + similarity_threshold + 空回退

用途：意图路由 / Skill 匹配 / 工具 JIT 预筛 / 附件检索 / 记忆检索五路共用，
一处升级（配置 embedding provider，见 core.config 的 embedding 分组）全链路收益。

设计：
- rank(query, items, top_k, threshold, key)：返回 [(score, item)] 降序，仅 score > threshold
- 真向量：Embedder 对候选集与 query 分别向量化（同一 provider/model → 维度一致）→ 余弦
  （候选集向量按文本指纹缓存，见 `_embed_candidates`；2026-09-25 前是"候选+query 一次全发"）
- bigram 降级：VectorEngine Counter 余弦（稀疏交集，任意两文本可比；不消费 list-vector 存储）
- 空回退：无命中返回 []，调用方自行降级/LLM 兜底（不抛异常）
"""
import hashlib
import math
import threading

# ── 2026-09-25 候选集向量缓存 ────────────────────────────────────────────────
# 背景：`_rank_dense` 原本把「候选集 + query」**整批**重新向量化，于是**每次**调用都要重算
#   全部候选 —— 意图语义兜底的候选集（语义索引）补了示例 utterance 后从 20 → 52 条，
#   单次弱信号路由平白多付 ~5 次 embedding 往返（实测 21 条时单次 rank ≈1.9s，线性放大）。
#   候选文本在进程生命周期内基本不变（索引只在 `_load_db_agents` 时重建），故按
#   「候选集文本指纹」缓存其向量；query 侧继续吃 Embedder 自带的单文本 TTL 缓存。
# 失效：指纹只由候选文本决定，文本一变（换模型/改索引）自动换 key；容量上限防无界增长。
_CAND_CACHE: dict = {}
_CAND_CACHE_MAX = 8
# 2026-09-26 预热状态：已预热/正在预热的**文本指纹**（同一索引不重复起线程）
_WARM_LOCK = threading.Lock()
_WARM_KEY = ""


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

    # ── 真向量路：候选集（缓存）+ query（Embedder TTL 缓存）分别向量化 ──
    @staticmethod
    def _embed_candidates(ed, texts: list) -> tuple:
        """候选集向量：(vecs, version)。按文本指纹缓存，避免每次调用整批重算（见模块头注释）。

        只缓存**成功且条数一致**的真向量结果：bigram 降级结果不缓存（那是失败路径，
        下次仍应重试真向量）。
        """
        # 2026-09-26：**先规范化再算 key**。实测两侧文本只差一个尾随换行（`'账号角色管理\n'` vs
        #   `'账号角色管理'`）→ md5 不同 → 预热永远命中不上（"预热说填了、请求仍冷启动"）。
        #   尾随/前导空白不影响语义，故统一 strip 后再算 key **并用同一份规范文本向量化**；
        #   顺序仍然重要（向量与 items 一一对应），所以只做去空白、不做排序。
        texts = [str(t or "").strip() for t in texts]
        key = hashlib.md5("\n".join(texts).encode("utf-8", "ignore")).hexdigest()
        hit = _CAND_CACHE.get(key)
        if hit:
            return hit
        # 2026-09-26 诊断：**未命中时打出指纹**，用于与 `prewarm` 的指纹比对 ——
        #   "预热说它填了、请求却仍冷启动"只可能是两边 key 不同（实测在查这条）。
        print("[semantic] 候选缓存未命中 fp=%s texts=%d（已缓存 %d 个 key：%s）首=%r 尾=%r"
              % (key[:8], len(texts), len(_CAND_CACHE), [k[:8] for k in _CAND_CACHE],
                 str(texts[0])[:38], str(texts[-1])[:38]), flush=True)
        vecs, version = ed.embed_with_version(texts, batch_size=0)
        vecs = list(vecs or [])
        if version != "bigram-tf" and len(vecs) == len(texts):
            _CAND_CACHE[key] = (vecs, version)
            if len(_CAND_CACHE) > _CAND_CACHE_MAX:
                _CAND_CACHE.clear()
        return vecs, version

    def prewarm(self, items: list, key: str = "text") -> bool:
        """**后台预热**候选集向量（2026-09-26）：把"首次整批向量化"从第一个请求挪到进程启动后。

        为什么需要（有实测数字）：候选集向量是惰性算的，这笔开销此前全落在**第一个真实请求**上 ——
        重启服务后首次 `run-eval`（44 条意图判定、多条走语义）实测 **13.00s**，缓存热了之后 **4.43s**，
        即用户要为"第一次有人用语义层"白等约 **8.6s**。预热让这段时间发生在用户开口之前。

        实现要点（每条都是为了不引入新问题）：
          - **守护线程**：预热失败/挂起都不能影响启动与请求（失败就退化成原来的冷启动，行为不劣化）；
          - **按文本指纹去重**：`_load_db_agents` 每个请求都会重建索引并调到这里，无指纹守卫会反复起线程；
          - **复用 `_embed_candidates` 的同一个 key**（`md5("\\n".join(texts))`）→ 预热必须传**同序同文本**，
            故这里用与 `_rank_dense` 完全相同的取值表达式（items 的 `key` 字段按序），而不是另建一份；
          - **自我验证**：预热完检查缓存是否真的填上了，没填就记一行日志（否则"预热形同虚设"会静默存在）。
        """
        texts = [str(it.get(key) or "").strip() for it in (items or [])]   # 与 _embed_candidates 同款规范化
        if not texts:
            return False
        fp = hashlib.md5("\n".join(texts).encode("utf-8", "ignore")).hexdigest()
        if fp in _CAND_CACHE:            # 已被某次真实请求（或上次预热）填过 → 无需再热
            return True
        global _WARM_KEY
        with _WARM_LOCK:
            if _WARM_KEY == fp:          # 同一索引已有线程在跑或跑过了
                return False
            _WARM_KEY = fp

        def _run():
            try:
                ed = self._ensure_embedder()
                if not ed:
                    # 不许静默空转：首版就是这里直接 return，服务端日志里什么都看不到，
                    # 于是"预热没生效"只能靠人肉推断（实测踩到）。
                    print("[semantic-prewarm] 无可用 Embedder，跳过预热（请求侧仍会冷启动）", flush=True)
                    return
                vecs, version = type(self)._embed_candidates(ed, texts)
                if fp in _CAND_CACHE:
                    print("[semantic-prewarm] 完成：%d 条候选已缓存（backend=%s, fp=%s）首=%r 尾=%r"
                      % (len(texts), version, fp[:8], str(texts[0])[:38], str(texts[-1])[:38]), flush=True)
                else:
                    print("[semantic-prewarm] 预热未生效（缓存未命中，可能降级 bigram）", flush=True)
            except Exception as e:       # 预热是"锦上添花"，任何异常都不得外溢
                print("[semantic-prewarm] 异常跳过：%s" % str(e)[:160], flush=True)

        threading.Thread(target=_run, name="semantic-prewarm", daemon=True).start()
        return True

    def _rank_dense(self, query: str, pairs: list):
        try:
            ed = self._ensure_embedder()
            if not ed:
                return None
            # 2026-09-25：候选与 query **分开**向量化（原实现一次全发）。两者仍由同一个
            # Embedder（同一 provider/model）产出，维度一致的前提不变；拆开后才可能对
            # 候选侧做缓存 —— 原实现把 query 混在批次里，导致候选每次都随 query 一起重算。
            vecs, version = self._embed_candidates(ed, [t for _, t in pairs])
            qvecs, qver = ed.embed_with_version([query], batch_size=0)
            if version == "bigram-tf" or qver == "bigram-tf":
                return None
            if not vecs or len(vecs) != len(pairs) or not qvecs:
                return None
            qv = qvecs[0]
            qn = math.sqrt(sum(x * x for x in qv)) or 1.0
            out = []
            for (it, _), v in zip(pairs, vecs):
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
