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


class EmbedQuotaExceeded(RuntimeError):
    """embedding 配额耗尽（且策略为 block）—— **必须让入库失败**而不是降级。

    为什么单独一个异常类：调用方（`ingest_document`）要靠它把
    `parse_status` 置为 `failed` 并写入明确原因，让用户看到"额度不足"；
    若沿用普通 `Exception`，上层 `except` 会把它当成偶发网络抖动，
    而 `_gen_llm_summary` 那种"失败返回 ''继续跑"的模式会把它吞掉。
    """


class Embedder:
    """可插拔 Embedding：OpenAI-compatible /embeddings → bigram TF 降级。

    - 真 embedding：llm_providers 有 key 的 provider 走 POST {base_url}/embeddings
    - 降级：VectorEngine 字符 bigram TF（零外部依赖，确定性回归可用）

    ⚠️ **降级策略受 `embedding.on_quota_exhausted` 控制**（P0-2，2026-10-04）：
    - `block`（默认）：配额耗尽时抛 `EmbedQuotaExceeded`，**不写任何 bigram 向量**。
      实测事故：403 `Free quota exhausted` 被静默吞掉 ⇒ 810 chunk 用词面向量污染检索，
      而界面无任何提示。降级向量会**永久留在库里**且事后无法区分，
      所以宁可失败重来。
    - `degrade`：旧行为（写 bigram + 在 pipeline_detail 标 degraded），仅供离线回归。
    - `warn`：写 bigram + 留痕 + 告警，不阻断。
    """

    def __init__(self, conn=None):
        self._api = None  # (base_url, api_key, model)
        if conn is not None:
            self._probe_api(conn)

    # P2-1 缓存：60s 内不重复 probe（避免每次查询查 llm_providers 表）
    _PROBE_CACHE = None
    _PROBE_STAMP = 0.0
    _PROBE_TTL = 60.0

    # P1-10 配额熔断：insufficient_quota（免费额度耗尽）属「非临时」错误，反复重试只会
    # 每次打一次 403 + 刷一条日志、白等一次网络往返。识别到后熔断 10 分钟：期间直接降级
    # bigram，不再调 API。用户充值后下一个 probe 周期自然恢复。
    # P0-2（2026-10-04）：熔断**不再等于降级** —— 熔断只是"别再打 API 了"，
    # 接下来按 on_quota_exhausted 策略决定"抛错"还是"降级"。
    _QUOTA_BLOCK_UNTIL = 0.0
    _QUOTA_BLOCK_SEC = 600.0
    # 最近一次熔断的原因（供 UI/监控显示："为什么这批 chunk 是词面向量"）
    LAST_QUOTA_ERROR = ""

    # ── P1-34（2026-10-10）：服务不可用短熔断（区别于配额熔断）──────────────────
    # 实测事故：内网 embedding 网关（192.168.100.202:8004）经代理返回 502/连接超时，
    # 每批都要白等 30s 超时；而工具 JIT 排序 / 语义路由一次请求里要做**多批多次**调用
    # （27 候选 = 3 批 × 30s，还有多组候选集）⇒ 单个 SSE 请求在 embedding 上爬 5~10 分钟，
    # 用户看到的是"无限转圈"（py-spy 实证卡在 _receive_response_headers）。
    # 配额熔断（_QUOTA_*）只认 403/429；5xx / 连接失败 / 超时属**临时不可用**，
    # 用短窗口熔断（默认 60s，可配 embedding.unavail_block_sec）：期间直接走 bigram，
    # 不再打网络。窗口短 ⇒ 服务恢复后最多延迟 60s 自动回归真向量。
    _UNAVAIL_BLOCK_UNTIL = 0.0
    _UNAVAIL_BLOCK_SEC = 60.0

    @classmethod
    def _note_unavail_error(cls, exc) -> bool:
        """识别「服务临时不可用」类错误（5xx/连接失败/超时）→ 短熔断。

        :returns: True 表示已触发熔断（调用方日志里说明一次即可）。
        """
        try:
            import httpx
            hit = False
            if isinstance(exc, httpx.HTTPStatusError):
                hit = exc.response.status_code >= 500
            elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                                  httpx.ReadTimeout, httpx.WriteTimeout,
                                  httpx.PoolTimeout, httpx.RemoteProtocolError)):
                hit = True
            elif isinstance(exc, OSError):
                # WinError 10054/10060 等 OS 网络错误（httpx 有时会包成 OSError）
                hit = True
            if hit:
                try:
                    from core import config as _c
                    sec = float(_c.get("embedding", "unavail_block_sec", 60) or 60)
                except Exception:
                    sec = 60.0
                if sec > 0:
                    cls._UNAVAIL_BLOCK_SEC = sec
                    cls._UNAVAIL_BLOCK_UNTIL = time.time() + sec
                    cls.LAST_QUOTA_ERROR = ("embedding 服务不可用（%s: %s），熔断 %ds"
                                            % (type(exc).__name__, str(exc)[:120], int(sec)))
            return hit
        except Exception:
            return False

    @classmethod
    def unavail_blocked(cls) -> bool:
        """当前是否处于服务不可用熔断期（期间直接走 bigram，不打网络）。"""
        return time.time() < cls._UNAVAIL_BLOCK_UNTIL

    @classmethod
    def _note_quota_error(cls, exc) -> bool:
        """识别 403/429 且 body 含 quota/insufficient → 熔断（设类级封锁时间戳）。

        :returns: **True 表示确认为配额耗尽**（调用方据此决定抛错还是降级）。
        ⚠️ 第一版忘了 return，标注也是 `-> None` ⇒ 调用方 `if blocked:` 恒假
        ⇒ 配额耗尽时**第一次调用仍静默降级**（真机验证抓到的：策略=block
        但实测仍返回 bigram-tf）。"记了熔断"不等于"告知了调用方"，两者都要。

        P0-2：同时把**响应体原文**（截断）存进 `LAST_QUOTA_ERROR` ——
        只记"发生了配额错误"没有用，用户要看到的是**供应商原话**
        （实测：`Free quota exhausted. To continue accessing the model on a paid basis...`），
        否则前端只能显示"额度不足"这种无法对账的措辞。
        """
        try:
            import httpx
            if isinstance(exc, httpx.HTTPStatusError):
                r = exc.response
                if r.status_code in (403, 429):
                    body = (r.text or "").lower()
                    if "quota" in body or "insufficient" in body:
                        cls._QUOTA_BLOCK_SEC = cls._quota_block_sec()
                        cls._QUOTA_BLOCK_UNTIL = time.time() + cls._QUOTA_BLOCK_SEC
                        try:
                            import json as _j
                            msg = (_j.loads(r.text or "{}").get("error") or {}).get("message") or ""
                        except Exception:
                            msg = ""
                        cls.LAST_QUOTA_ERROR = (msg or (r.text or "")[:200])[:300]
                        return True
        except Exception:
            pass
        return False

    @classmethod
    def _quota_block_sec(cls) -> float:
        try:
            from core import config as _c
            return float(_c.get("embedding", "quota_block_sec", 600) or 600)
        except Exception:
            return 600.0

    @classmethod
    def quota_blocked(cls) -> bool:
        """当前是否处于配额熔断期（即：API 已不可用，别再打）。"""
        return time.time() < cls._QUOTA_BLOCK_UNTIL

    @classmethod
    def _on_quota_exhausted(cls) -> str:
        """降级策略：block（默认，抛错）| degrade（旧行为）| warn（留痕不阻断）。"""
        try:
            from core import config as _c
            v = _c.get("embedding", "on_quota_exhausted", "block")
            return (v or "block").strip().lower()
        except Exception:
            return "block"

    @classmethod
    def reset_quota_block(cls) -> None:
        """人工解除熔断（充值后调用，或运维面板"重试"按钮）。

        为什么需要：熔断是**类级**的（`_QUOTA_BLOCK_UNTIL`），一旦触发，
        后续 10 分钟内**即使供应商已恢复也不再尝试** —— 用户充值后立刻重试
        仍会拿到"额度不足"，除非等到熔断自然过期或显式调用本方法。

        P1-34：同时解除「服务不可用」短熔断 —— 运维面板点"重试"时，
        用户预期是"立刻再试一次"，若只解配额熔断，短熔断仍会让它走 bigram。
        """
        cls._QUOTA_BLOCK_UNTIL = 0.0
        cls._UNAVAIL_BLOCK_UNTIL = 0.0
        cls.LAST_QUOTA_ERROR = ""

    @classmethod
    def reset_unavail_block(cls) -> None:
        """只解除「服务不可用」短熔断（窗口短，一般等自然过期即可）。"""
        cls._UNAVAIL_BLOCK_UNTIL = 0.0

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
                # ★ 2026-10-28（用户报「知识库明明有，检索却说没有」）：
                #   `model_name` 必须 **strip** —— 实测 provider 110 存的是
                #   `'bge-m3  '`（两个尾随空格，len=8），服务端原样报
                #   `The model `bge-m3  ` does not exist.` ⇒ HTTP 404
                #   ⇒ embed() 的 except 吞掉后**静默降级 bigram** ⇒
                #   向量分全面走低（实测 final 仅 0.05~0.08）⇒ 下游把
                #   「检索到了内容」判成「未检索到实质相关内容」。
                #   空格是人手填配置时带进来的（name 与 model_name 都带），
                #   属于**脏数据**，但代码不该假设配置一定干净：
                #   OpenAI 兼容协议里 model 是精确匹配，多一个空格就是另一个模型。
                self._api = (row["base_url"].rstrip("/"), row["api_key"],
                             str(row["model_name"] or "").strip())
        except Exception:
            self._api = None
        Embedder._PROBE_CACHE = self._api
        Embedder._PROBE_STAMP = _t.time()

    def embed(self, texts: list) -> list:
        """批量向量化，返回 list[list[float]]。真 API 失败自动降级 bigram TF。

        P0-2：配额耗尽且策略=block 时抛 `EmbedQuotaExceeded`（不再静默降级）。
        """
        if not texts:
            return []
        if self._api and not self.quota_blocked() and not self.unavail_blocked():
            try:
                return self._embed_api(texts)
            except Exception as e:
                blocked = self._note_quota_error(e)
                if blocked and self._on_quota_exhausted() == "block":
                    raise EmbedQuotaExceeded(self._quota_message())
                if self._note_unavail_error(e):
                    logger.warning("embedding 服务不可用，短熔断 %ds：%s",
                                   int(self._UNAVAIL_BLOCK_SEC), type(e).__name__)
                logger.warning("embedding API 调用失败，降级 bigram: %s", e)
        elif self._api and self.quota_blocked():
            if self._on_quota_exhausted() == "block":
                raise EmbedQuotaExceeded(self._quota_message())
        elif self._api and self.unavail_blocked():
            logger.info("embedding 服务熔断期内（剩 %ds），本次直接走 bigram",
                        int(self._UNAVAIL_BLOCK_UNTIL - time.time()))
        return self._embed_bigram(texts)

    @classmethod
    def _quota_message(cls) -> str:
        """给**用户看**的文案：供应商原话 + 可执行的下一步。

        为什么不能只说"额度不足"：用户无法据此判断是"充值"还是"换模型"，
        也不知道本系统到底调用了谁（实测供应商是阿里云百炼代理，
        错误文案来自**上游账号**，不是本系统的 key）。
        """
        raw = (cls.LAST_QUOTA_ERROR or "").strip()
        head = ("embedding 供应商额度已耗尽，本次入库**未生成语义向量**"
                "（已阻断，未写入降级向量）。")
        tail = ("请在供应商控制台充值或关闭「仅用免费额度」模式后重试；"
                "若要临时离线运行，可将 embedding.on_quota_exhausted 设为 degrade。")
        return head + (("供应商原话：%s" % raw) if raw else "") + tail

    def embed_with_version(self, texts: list, batch_size: int = 8) -> tuple:
        """批量向量化并返回实际使用的版本标记（openai-compat / bigram-tf）。

        分批调用（每批 batch_size 条）：BGE-M3 等服务端有 batch/长度限制；
        batch_size<=0 表示不分批一次全发（语义出口等小集合场景减少往返）；
        任一批失败整体降级 bigram（保持检索确定性回归可用）。

        P2-1 query 缓存：真向量模式下单文本调用（查询侧）加 TTL 缓存，
        同 query 短时间重复检索直返，省一次 embedding API 往返（~300ms）。

        ⚠️ P0-2（2026-10-04）：配额耗尽且 `embedding.on_quota_exhausted=block`
        时抛 `EmbedQuotaExceeded`（**不返回 bigram-tf**）。
        调用方（`ingest_document`）据此把 parse_status 置 failed 并写入原因 ——
        让用户看到"额度不足"，而不是拿到一份没有语义的检索却毫无察觉。
        """
        if not texts:
            return [], "bigram-tf"
        # P2-1 query 缓存：单文本调用（查询侧）命中直返，省一次 embedding API 往返
        if self._api and not self.quota_blocked() and not self.unavail_blocked() and len(texts) == 1:
            key = ("q", texts[0])
            hit = Embedder._QUERY_CACHE.get(key)
            if hit and time.time() - hit[1] < Embedder._QUERY_TTL:
                return [hit[0]], "openai-compat"
        if self._api and not self.quota_blocked() and not self.unavail_blocked():
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
                blocked = self._note_quota_error(e)
                if blocked and self._on_quota_exhausted() == "block":
                    raise EmbedQuotaExceeded(self._quota_message())
                if self._note_unavail_error(e):
                    logger.warning("embedding 服务不可用，短熔断 %ds：%s",
                                   int(self._UNAVAIL_BLOCK_SEC), type(e).__name__)
                logger.warning("embedding API 调用失败，降级 bigram: %s", e)
        elif self._api and self.quota_blocked():
            if self._on_quota_exhausted() == "block":
                raise EmbedQuotaExceeded(self._quota_message())
        elif self._api and self.unavail_blocked():
            logger.info("embedding 服务熔断期内（剩 %ds），本次直接走 bigram",
                        int(self._UNAVAIL_BLOCK_UNTIL - time.time()))
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

