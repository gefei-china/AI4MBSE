"""LLM 统一入口（P1-3 插件化）。

对外契约（与旧 llm.py 完全一致，全仓零改动）：
- llm_client：LLMClient 单例（chat/embed/test_connection/stats）
- llm_router：LLMRouter 单例（D10 智能路由）
- get_llm(provider_id)：延迟实例化 + 缓存（P1-3 新增，按 DB provider 配置创建对应 Provider）

新增 LLM = llm/providers/xxx.py 新建类（继承 BaseLLM）+ providers/__init__.py 注册一行
+ DB llm_providers 表新增一行（provider_type 填注册名）。零代码改动。
"""
import json
import re
import time
import logging

from .base import BaseLLM
from .registry import ProviderRegistry
from .providers import (  # noqa: F401  （注册副作用）
    MockLLM,
    OpenAICompatProvider,
)

#: LLM 层日志器（思考超限告警用；与 provider 层 `mbse.llm` 同族）
_log = logging.getLogger("mbse.llm")

#: 思考超限告警去重集合（2026-10-10）。
#: 键 = (intent, provider_id)。**为什么进程内去重**：真实量级 310/6890≈4.5%，
#: 且单次会话会连续多轮触发同一组合 ⇒ 不去重会把日志刷爆、反而看不见。
#: 为什么不用"每小时 N 条"：那要引入时间轮子状态，收益不抵复杂度。
_THINK_WARNED: set = set()

__all__ = ["BaseLLM", "ProviderRegistry", "MockLLM", "OpenAICompatProvider",
           "llm_client", "llm_router", "get_llm",
           "resolve_provider_cfg", "provider_supports_vision", "parse_stream_frame"]


def _load_provider_cfg(conn, provider_id):
    """按 provider_id 查 DB 配置；None/未找到 → {}（get_llm 默认走 Mock 兜底）。

    provider_id=None 时取「对话默认模型」（model_type IN ('chat','multimodal') 且 is_default=1；
    多模态模型可对话，故与 chat 同池）；
    向量默认模型由 knowledge_pipeline/embedder 按 model_type='embedding' 单独选择。
    """
    try:
        if provider_id is not None:
            row = conn.execute("SELECT * FROM llm_providers WHERE id=? AND status='active'",
                               (provider_id,)).fetchone()
        else:
            # ORDER BY / LIMIT 是 2026-09-20 的**确定性**修复：原写法在多行 is_default=1 时
            # 由 SQLite 按 rowid 序任意返回一行（无 ORDER BY 即无保证）。语义定为「默认模型中
            # priority 最高者」——priority 正是该列既有的唯一用途（智能路由里也这么用）。
            # 当前库只有 1 行 is_default=1 → 取值与改动前完全一致，属零回归。
            row = conn.execute("SELECT * FROM llm_providers WHERE model_type IN ('chat','multimodal') AND is_default=1 AND status='active' ORDER BY priority DESC, id ASC LIMIT 1").fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


# ── P2 视觉通道（2026-09-21）：provider 图像输入能力判据 ──────────────
def resolve_provider_cfg(provider_id=None, conn=None) -> dict:
    """按 provider_id（None=对话默认模型）取 provider 配置行；任何异常 → {}。

    与 `get_llm` 共用 `_load_provider_cfg`，保证「判能力」与「实际调用」看的是**同一行**，
    不会出现「按 A 判能力、实际调 B」的错配（provider_id=None 时两者都取对话默认模型）。
    """
    try:
        if conn is None:
            from database import get_db as _get_db
            c = _get_db()
            try:
                return _load_provider_cfg(c, provider_id)
            finally:
                c.close()
        return _load_provider_cfg(conn, provider_id)
    except Exception:
        return {}


# 视觉能力标签的匹配式 —— **必须与前端 static/js/mods/10-chatinput.js 的 modelSupportsVision 同套**。
# 前端是宽松匹配（用户手写中文「多模态」也算），后端若用精确 == 'vision'，
# 就会出现「前端提示支持、后端不发图」的静默矛盾。故两侧统一到本式（前端那处有对应注释）。
VISION_TAG_RE = re.compile(r"vision|image|multimodal|多模态|图片", re.I)


def provider_supports_vision(cfg) -> bool:
    """provider 是否具备**图像输入**能力（判据集中在此，agent 侧与前端共用同一份语义）。

    判据（二选一，要求显式声明）：
      1) `tags` 命中 `VISION_TAG_RE` —— 复用既有「能力标签」字段（`models/llm.py` 注释即此用途），
         **零 schema 改动**。这是**主路径**：模型配置弹窗的「支持图片理解」复选框写的即此标签
         （2026-09-04 起前端 `10-chatinput.js` 就按 tags 判能力，本函数对齐它）。
      2) `model_type == 'vision'` —— 兼容保留。**注意不要用它**：会话模型选择器按
         `model_type==='chat'` 过滤（`static/js/mods/13-reports.js`），标成 vision 的模型
         **不会出现在会话下拉里、用户选不到**，通道等于白做。

    为什么不按模型名关键词猜（'vl' / 'gpt-4o' / 'omni'）：猜错的两侧代价**不对称** ——
    误判为「有视觉能力」会把图片发给纯文本模型 → 上游 400 直接失败（用户看到报错）；
    误判为「没有」则静默不注入图片（退回改动前的老问题，至少不崩）。
    故**宁可要求显式声明**，把不确定性交还给配置方。
    """
    if not isinstance(cfg, dict) or not cfg:
        return False
    if str(cfg.get("model_type") or "").strip().lower() in ("vision", "multimodal"):
        return True
    raw = cfg.get("tags") or "[]"
    try:
        tags = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        tags = []
    if isinstance(tags, str):
        tags = [tags]
    try:
        return any(VISION_TAG_RE.search(str(t)) for t in (tags or []))
    except Exception:
        return False


# ── P0-a（2026-10-02）：流式帧解析 —— usage / finish_reason 采集的唯一真源 ──────────
# 背景：`_stream_api` 此前只做 SSE **透传**，`_stream_wrapped` 结束时以 `{"usage": {}}` 落库
# ⇒ **流式（= 前端主路径 `/chat/stream`）在 llm_usage_stats 里恒为 pt=0 / ct=0 / finish=''**。
# 三重后果（都不是"少个字段"这么轻）：
#   ① 成本统计：主路径全部记 0，`estimated_cost` 只反映少数非流式调用；
#   ② 截断观测：P1-27 新补的 `finish_reason` 列在主路径上**永远取不到值**
#      ⇒「回复被截断 / 正文为空」这个本批最大的收益点，在主路径上等于没接；
#   ③ token 记账：`total_tokens=0` 流入 TokenCounter 与子任务 meta。
# 实测（`tmp/probe_health/probe_stream_usage.py`，DeepSeek 官方端点 api.deepseek.com）：
#   · 流式**默认**就发 usage 帧（不带 `stream_options.include_usage` 也有；带上同样 200）；
#   · 该帧含 `completion_tokens_details.reasoning_tokens` 与
#     `prompt_cache_hit_tokens / prompt_cache_miss_tokens` —— 与 P1-20/P1-27 落库的列一一对应；
#   · `choices[0].finish_reason` 在结束帧可取（'stop' / 'length'）。
# ⇒ 本解析**零额外请求**即可拿到真实值，不必为"能观测"付任何成本。
def parse_stream_frame(raw) -> dict:
    """解析一帧 SSE 文本 → `{"usage": {...}, "finish_reason": str, "text_chars": int}`。

    输入形如 `_stream_api` yield 的 `data: {...}\\n\\n`。非 data 帧 / `[DONE]` / 任何异常 → `{}`
    （**绝不抛**：解析失败不该影响正在进行的流式回复）。
    `text_chars` = 本帧 delta 的 `(reasoning_content + content)` 字符数，供无 usage 帧时兜底估算。
    """
    out: dict = {}
    try:
        s = str(raw or "").strip()
        if not s.startswith("data:"):
            return out
        body = s[5:].strip()
        if not body or body == "[DONE]":
            return out
        j = json.loads(body)
        if isinstance(j.get("usage"), dict) and j["usage"]:
            out["usage"] = j["usage"]
        n = 0
        for ch in (j.get("choices") or []):
            if not isinstance(ch, dict):
                continue
            fr = ch.get("finish_reason")
            if fr:
                out["finish_reason"] = str(fr)
            d = ch.get("delta") or ch.get("message") or {}
            if isinstance(d, dict):
                n += len(str(d.get("reasoning_content") or "")) + len(str(d.get("content") or ""))
        if n:
            out["text_chars"] = n
    except Exception:
        return {}
    return out


# ── P1-3：延迟实例化 + 缓存（实例按 provider_id/默认 隔离缓存）──
_instances: dict = {}


class LLMUnavailableError(RuntimeError):
    """LLM 不可用（无 key / 额度耗尽 / 调用失败）——**默认直接报错，不再回落 Mock**。

    ── 为什么要改（2026-10-09，米爸决策 R1）──
    原实现在这三处失败时**静默回落 Mock**：
        ① 未配置 api_key      （`chat`）
        ② 真实调用失败（402/超时）（`chat`）
        ③ 流式调用失败        （`chat_stream`）
    ⇒ 用户拿到的是 `（Mock 回答）…当前为 Mock 降级模式` 这种**编造的内容**，
      而它在回答正文里看起来像一句正常回复（Mock 标记只在「执行详情」里可见）。

    ★ 这比"产出雷同"更严重：产出雷同交付的至少是**真代码**（错的），
      Mock 交付的是**不存在的内容**（假的）。

    ── 为什么用异常而不是返回错误字典 ──
    调用链有`execute` / `execute_stream` / 编排子任务等多层，
    返回错误字典极易被某一层"当成正常结果继续处理"（这正是原缺陷的成因）。
    抛异常则**默认不被吞**——只要某一层真的需要容错，它必须显式 `except`。

    ── 与 `llm.force_mock` 的区别（务必分清）──
    `force_mock=True` 是**显式**要求 Mock（回归测试不依赖网络）⇒ 仍然保留。
    本异常管的是"**没要求 Mock 却悄悄给了 Mock**"。
    """

    def __init__(self, message: str, *, reason: str = "", provider: str = "", model: str = ""):
        super().__init__(message)
        self.reason = reason
        self.provider = provider
        self.model = model

    def to_card(self) -> dict:
        """给前端 `card_data` 用的结构化错误（前端据此显示醒目错误块而非正常回复）。"""
        return {"llm_unavailable": True, "reason": self.reason,
                "provider": self.provider, "model": self.model,
                "error": str(self)}


class _CircuitOpen(Exception):
    """P1-4：主 provider 处于熔断态时抛出，用于把"熔断"与"调用失败"区分开。

    ⚠️ 为什么不直接 raise 原异常：熔断时**根本没有打下游**，
    若抛一个看起来像下游报错的异常，日志/告警会误导排查方向
    （运维会去查provider 挂了，而实际上是"我们主动不再打它"）。
    """


def get_llm(provider_id=None, conn=None):
    """按 provider 配置创建 LLM 客户端实例（缓存）。

    provider_id=None → DB 默认 provider；无配置 → Mock 兜底。
    实例创建仅首次发生（延迟实例化），后续复用。
    """
    key = str(provider_id) if provider_id is not None else "<default>"
    if key in _instances:
        return _instances[key]
    from database import get_db as _get_db
    c = conn or _get_db()
    cfg = _load_provider_cfg(c, provider_id)
    ptype = cfg.get("provider_type") or "openai_compat"
    if not ProviderRegistry.has(ptype):
        ptype = "openai_compat"  # 未注册类型 → 回落 OpenAI 兼容
    provider = ProviderRegistry.create(ptype, cfg or None)
    _instances[key] = provider
    return provider


class LLMClient:
    """统一 LLM 入口（Facade，兼容旧契约）。

    职责：按 provider_id/route_tags 选模型 → 真实调用（经 ProviderRegistry 插件化）
    或 Mock 兜底；统计 real/mock 调用并落库 llm_usage_stats（M7 可观测）。
    """

    # 估算单价（美元 / 1M tokens）：输入、输出（覆盖常见 OpenAI 兼容模型量级）
    PRICE_PER_1M = {
        "input": 0.15,
        "output": 0.60,
        # P1-21 缓存命中单价 ≈ input 的 10%。依据（标杆口径一致）：
        #   DeepSeek 上下文磁盘缓存 hit $0.014/M vs miss $0.14/M（约 1/10）；
        #   Anthropic cache read = 0.10× input；OpenAI cached 0.50×（部分新模型 0.90× 折扣）。
        #   取 10% 作通用保守口径——provider 未返回缓存字段时不启用，不污染既有估算。
        "cache_hit": 0.015,
    }

    def __init__(self):
        self.mock = ProviderRegistry.create("mock")
        # ── P1-3（2026-10-03 降级显式化）：`real`/`mock`/`fallback_total` 是**单调累计计数** ──
        # 用途：调用方只需在任务开始前快照一次、结束后再读一次，差值即"这段任务里发生了几次
        # 降级"，从而把「AI 的回答其实来自 Mock」这一事实**显式告知用户**。
        # 为什么不能用 `last_used_mock`：它是**瞬时值**，多会话/多子任务并发时会互相覆盖，
        # 编排链路（ThreadPoolExecutor 跑子任务）恰恰是并发最多的路径 —— `last_*` 不可信。
        self.stats = {"real": 0, "mock": 0, "last_provider": "", "last_used_mock": True,
                      "last_prompt_tokens": 0, "last_completion_tokens": 0,
                      "fallback_total": 0}

    #: P0-3（2026-10-09）异常 → failure_kind 分类表。
    #:
    #: **为什么要有这列**：2026-10-09 实测出现「单次调用 545 秒后失败」，
    #: 而 `llm_usage_stats` 里所有失败的 `finish_reason` 都是空、
    #: `used_mock` 与真 Mock **同列为1** ⇒ 只能逐条翻日志才能知道是超时还是余额。
    #: 有了 failure_kind，「哪些会话在超时上烧时间」变成一条 `GROUP BY`。
    #:
    #: ★ **首版bug（写这张表时踩的）**：`kind` 我写成了**单元素元组**
    #:   `(("timeout",), ...)`，`return kind` 于是返回**元组**而非字符串
    #:   ⇒ 所有用例都判失败（返回 `('timeout',)` ≠ `'timeout'`）。
    #:   **判据当场抓住**（单测 8/8 失败），没等到线上。
    #:   ⇒ 现在 `kind` 是**裸字符串**。
    #: 判据用**异常类型 + 消息双通道**：同一类型在不同 provider 上文案不同
    #: （httpx 的 ReadTimeout vs SDK 的 TimeoutException），只认类型会漏。
    _FAILURE_KINDS = (
        # (kind, 类型名子串, 消息小写子串)——按顺序匹配，先命中先用
        ("timeout", ("readtimeout", "connecttimeout", "timeout", "timeoutexception"),
         ("timed out", "timeout")),
        ("balance", ("insufficientbalanceerror",),
         ("insufficient balance", "insufficient_balance", "arrears")),
        ("quota", ("ratelimiterror", "toomanyrequests"),
         ("free quota exhausted", "quota", "rate limit", "too many requests")),
        ("http_4xx", ("badrequesterror", "unprocessableentityerror",
                      "permissiondeniederror", "authenticationerror"),
         ("400", "401", "403", "404", "422")),
        ("conn", ("remoteprotocolerror", "apiconnectionerror",
                  "connecterror", "readerror", "writeerror"),
         ("server disconnected", "connection", "broken pipe")),
        # ⚠️ 必须排在**最后**：消息含 "未配置 API Key"，但也可能含别的词
        ("no_api_key", ("llmunavailableerror",), ("no_api_key", "未配置 api key")),
    )

    @staticmethod
    def classify_failure(exc: "BaseException | str | None") -> str:
        """把异常分类成 failure_kind（不可判定时返回 `other`；无异常返回 `''`）。"""
        if exc is None:
            return ""
        if isinstance(exc, BaseException):
            tname = type(exc).__name__.lower()
            msg = str(exc).lower()
        else:
            tname, msg = "", str(exc).lower()
        for kind, tsubs, msubs in LLMClient._FAILURE_KINDS:
            if any(s in tname for s in tsubs) or any(s in msg for s in msubs):
                return kind
        return "other" if (tname or msg) else ""

    def _record_usage(self, provider, resp, used_mock: bool, intent: str = "",
                      latency_ms: int = 0, *, call_failed: bool = False,
                      failure: BaseException | str | None = None) -> None:
        """M7：LLM 调用统计落库（失败不阻断主流程）。

        Task 7 token 记账：响应含 usage 时按其 prompt/completion_tokens 落库；
        Mock / 无 usage 时按 len(输出文本)//2 估算 completion_tokens（估算兜底）；
        并将 token 总数回填 resp._meta.token_count（供子任务侧 meta.token_count 记账）。

        ★ P0-3（2026-10-09）**`used_mock` 与"调用失败"是两件事**：
          · `used_mock=True, call_failed=False` ⇒ 内容是 Mock（force_mock 测试通道等）
          · `call_failed=True` ⇒ 真实调用失败，**用户什么都没拿到**
          此前两者都记成 `used_mock=1`，于是"哪些回答是 Mock 做的"
          会把**所有失败调用**算进来 —— 我据此误判过一次（见 docs 报告 §5.3）。
          新增 `failure_kind` / `failure_msg` 两列，让失败可按类聚合。
        """
        try:
            failure_kind = self.classify_failure(failure) if call_failed else ""
            failure_msg = ""
            if call_failed and failure is not None:
                failure_msg = (str(failure) if isinstance(failure, str)
                               else f"{type(failure).__name__}: {failure}")[:200]
            usage = resp.get("usage") or {}
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            # P1-20：DeepSeek 上下文磁盘缓存命中/未命中 token（自动缓存，命中≈1/10 价）。
            # 仅真实调用有；Mock/无 usage 时自然为 0（不估算，缓存命中无法估算）。
            pch = int(usage.get("prompt_cache_hit_tokens") or 0)
            pcm = int(usage.get("prompt_cache_miss_tokens") or 0)
            # P1-27：**截断诊断** —— finish_reason（stop=正常结束 / length=被上限截断）与
            # reasoning tokens。思考模型的 reasoning 与正文**共享 max_tokens 配额**，
            # 这两项是「输出被截断 / 为空」的直接证据（此前只能拿 completion_tokens 是否
            # 触及上限去猜）。Mock/无 usage 时分别为 '' / 0。
            _fr = str(((resp.get("choices") or [{}])[0].get("finish_reason")) or "")
            rt = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            if not pt and not ct:
                # Mock 或响应无 usage → 按输出文本长度估算（len(输出文本)//2）
                try:
                    _content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
                    ct = max(len(str(_content)) // 2, 1) if _content else 0
                except Exception:
                    ct = 0
            est = 0.0
            if not used_mock:
                _pi = LLMClient.PRICE_PER_1M["input"]
                _po = LLMClient.PRICE_PER_1M["output"]
                _pc = LLMClient.PRICE_PER_1M["cache_hit"]
                if pch or pcm:
                    # P1-21：区分「缓存命中 / 未命中」两种单价。此前把 prompt_tokens 全按
                    # input 混合价算 → 缓存折扣被抹平，成本数字虚高（命中越多偏差越大）。
                    # 余量 = pt - hit - miss（正常为 0；异常时按 input 价兜底，绝不丢 token）。
                    _rest = max(pt - pch - pcm, 0)
                    est = ((pcm + _rest) * _pi + pch * _pc + ct * _po) / 1_000_000
                else:
                    # 无缓存字段（provider 未返回 / Mock / 老数据）→ 原式，零行为漂移
                    est = (pt * _pi + ct * _po) / 1_000_000
            _meta = resp.get("_meta")
            if isinstance(_meta, dict):
                _meta["token_count"] = pt + ct
            # P1-1c：重试/回退观测（值由各调用路径写入 _meta；Mock/旧路径缺失 → 全 0，零行为漂移）
            try:
                _rc = int((_meta or {}).get("retry_count") or 0)
                _fu = 1 if (_meta or {}).get("fallback_used") else 0
                _fp = int((_meta or {}).get("fallback_provider_id") or 0)
            except Exception:
                _rc, _fu, _fp = 0, 0, 0
            # ── 思考超限告警（2026-10-10）────────────────────────────────
            # ★ **为什么在这里打点**：这是流式与非流式**唯一都经过**的记账点，
            #   而超限形态恰恰只在部分路径出现（实测极端例是流式主对话的收尾轮）。
            #   在 provider 层打会漏掉流式 usage 帧，在业务层打会漏掉所有非主对话路径。
            #
            # 实测依据（mbse.db 只读，2026-10-10）：`reasoning>2000tok` 的调用 **310** 次，
            #   极端例 id=8188 `reasoning=13430 / completion=13535 = 99.2%`、耗时 **4017 秒**
            #   ⇒ 68 分钟换一个几乎空的回答。此前这类长尾**无任何留痕**，
            #   只能靠翻日志逐条猜；而 `_record_usage` 已经算出了 `rt`，此处零成本。
            #
            # ⚠️ **默认关闭（`llm.thinking_warn_tokens=0`）** ——
            #   一是避免刷屏（真实量级是 310/6890≈4.5%），二是告警本身是新增可见行为，
            #   不应默认改变运行观感。**它是留痕，不是拦截**（真要拦用 thinking_budget）。
            # 去重按 (intent, provider) 维度，同一组合只报一次，避免单会话连续多轮刷屏。
            if not call_failed:
                try:
                    _wcap = 0
                    try:
                        from core import config as _cfg2
                        _wcap = int(_cfg2.get("llm", "thinking_warn_tokens", 0) or 0)
                    except Exception:
                        _wcap = 0
                    if _wcap > 0 and rt >= _wcap:
                        _wkey = (intent, (provider or {}).get("id"))
                        if _wkey not in _THINK_WARNED:
                            _THINK_WARNED.add(_wkey)
                            _log.warning(
                                "[thinking] 思考 token 超限｜intent=%s provider=%s(%s) "
                                "reasoning=%s completion=%s 占比=%.0f%% latency=%.1fs finish=%s"
                                "｜思考与正文共享 max_tokens，正文仅得 %s tok"
                                "｜处置：调低 llm.thinking_budget，或把该 intent 加入 thinking_policy.off",
                                intent or "(空=未标注调用点)",
                                (provider or {}).get("name"), (provider or {}).get("id"),
                                rt, ct, (rt / ct * 100.0) if ct else 0.0,
                                latency_ms / 1000.0, _fr or "-", max(ct - rt, 0),
                            )
                except Exception:
                    pass
            # V2.6：最近一次调用 token 记账（stats 供 done 事件透传前端展示）
            try:
                self.stats["last_prompt_tokens"] = pt
                self.stats["last_completion_tokens"] = ct
            except Exception:
                pass
            from database import db_conn
            # P0-c（2026-10-03 评估）：trace→span 归属 —— 本条 LLM 调用属于哪次会话 / 哪个编排 run。
            # 此前本表是孤立流水，只能按 created_at 猜相邻，无法从会话或 run 下钻（标杆必备能力）。
            try:
                from core.audit import trace_context as _tctx
                _t = _tctx()
            except Exception:
                _t = {}
            _cid = int(_t.get("conversation_id") or 0)
            _rid = int(_t.get("run_id") or 0)
            _tid = str(_t.get("trace_id") or "")
            _skey = str(_t.get("sub_task_key") or "")
            _vals = (provider.get("id", 0) if provider else 0,
                     (provider or {}).get("name", "未配置"),
                     (provider or {}).get("model_name", "-"),
                     # ★ P0-3：`used_mock` 只保留"内容是 Mock"这一语义；
                     #   调用失败改记 `call_failed`（下方另两列）。
                     intent, 1 if (used_mock and not call_failed) else 0,
                     pt, ct, pt + ct, pch, pcm,
                     _fr, rt, _rc, _fu, _fp, round(est, 6), latency_ms)
            with db_conn() as conn:
                try:
                    conn.execute(
                        "INSERT INTO llm_usage_stats (provider_id, provider_name, model_name, intent, used_mock, "
                        "prompt_tokens, completion_tokens, total_tokens, "
                        "prompt_cache_hit_tokens, prompt_cache_miss_tokens, "
                        "finish_reason, reasoning_tokens, retry_count, fallback_used, fallback_provider_id, "
                        "estimated_cost, latency_ms, conversation_id, run_id, trace_id, sub_task_key, "
                        # ★ P0-3新增三列
                        "call_failed, failure_kind, failure_msg) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _vals + (_cid, _rid, _tid, _skey,
                                 1 if call_failed else 0, failure_kind, failure_msg),
                    )
                except Exception as _e2:
                    # 关联列迁移未跑到 → 降级写入原有 17 列（不丢用量数据，绝不因记账失败阻断调用）
                    if "no such column" in str(_e2).lower() or "has no column" in str(_e2).lower():
                        conn.execute(
                            "INSERT INTO llm_usage_stats (provider_id, provider_name, model_name, intent, used_mock, "
                            "prompt_tokens, completion_tokens, total_tokens, "
                            "prompt_cache_hit_tokens, prompt_cache_miss_tokens, "
                            "finish_reason, reasoning_tokens, retry_count, fallback_used, fallback_provider_id, "
                            "estimated_cost, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _vals)
                    else:
                        raise
        except Exception:
            pass
        finally:
            # P0-b M5：闭 llm span——放 **finally** 而非 try 尾：记账成败
            # （DB 插入被上面 except 吞掉）与 span 生命周期解耦，否则 flush
            # 只能按 unclosed_at_flush 收口、token 归属丢失。pt/ct/_tid 可能
            # 因早期异常未绑定 ⇒ locals() 兜底（不伪造：拿不到就记 0）。
            try:
                from services.orch_span import current_tracer as _ct
                _tracer = _ct()
                if _tracer is not None:
                    _tracer.close_llm(
                        trace_id=str(locals().get("_tid") or ""),
                        tokens_in=int(locals().get("pt") or 0),
                        tokens_out=int(locals().get("ct") or 0),
                        latency_ms=int(latency_ms or 0),
                        ok=not call_failed)
            except Exception:
                pass   # 观测收口失败绝不阻断记账/主链路

    def get_default_provider(self):
        from database import get_db
        conn = get_db()
        # 对话默认模型（model_type IN ('chat','multimodal')，多模态可对话故同池）；向量默认由 embedder 单独选择
        # ORDER BY / LIMIT：同上（多行 default 时取 priority 最高者，消除不确定性）
        row = conn.execute("SELECT * FROM llm_providers WHERE model_type IN ('chat','multimodal') AND is_default=1 AND status='active' ORDER BY priority DESC, id ASC LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else None

    def get_provider(self, provider_id):
        from database import get_db
        conn = get_db()
        row = conn.execute("SELECT * FROM llm_providers WHERE id=?", (provider_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def chat(self, messages, provider_id=None, stream=False, tools=None, thinking=False,
             route_tags=None, **kwargs):
        """统一 LLM 入口（兼容旧签名，逻辑等价旧实现，真实调用经 ProviderRegistry 插件化）。"""
        provider = None
        route_reason = ""
        if provider_id:
            provider = self.get_provider(provider_id)
        elif route_tags:
            rd = llm_router.route(required_tags=route_tags, task_desc=kwargs.get("_task_desc", ""))
            if rd["selected"]:
                provider = self.get_provider(rd["selected"]["id"])
                route_reason = "智能路由: " + rd["reason"]
            elif rd["exhausted"]:
                provider = self.get_default_provider()
                route_reason = "智能路由: 候选模型预算全部耗尽，回退默认模型"
            elif rd["rejected"]:
                provider = self.get_default_provider()
                route_reason = "智能路由: " + rd["reason"] + "，回退默认模型"
        else:
            provider = self.get_default_provider()
        provider_name = provider.get("name", "未配置") if provider else "未配置"
        model_name = provider.get("model_name", "-") if provider else "-"
        t0 = time.time()
        # P0-b M5：llm span（会话级 trace）。开在 chat() 入口——mock / no-key /
        # 真实非流式 / 真实流式 / 异常五类出口的**唯一公共前躯**；闭点在
        # _record_usage 的 finally（流式与非流式唯一记账点，8 个调用全覆盖）。
        # 无 tracer（编排子任务线程持空 Context / 非 chat/stream 路径）⇒ no-op。
        # ⚠️ 不在此处存 sid：嵌套 chat（工具执行中再发起）由 llms 栈 LIFO 保证
        #    内外层各自闭对（见 close_llm docstring）。
        try:
            from services.orch_span import current_tracer as _ct
            _tracer = _ct()
            if _tracer is not None:
                _tracer.open_llm(model_name, kwargs.get("_intent", ""), t0=t0)
        except Exception:
            pass   # 观测装配失败绝不阻断 LLM 调用

        # 测试确定性：llm.force_mock 强制 Mock（回归测试不依赖外部网络；兼容 MBSE_LLM_FORCE_MOCK）
        from core import config as _cfg
        if _cfg.as_bool("llm", "force_mock", False):
            resp = self.mock.chat(messages, stream=stream, tools=tools, thinking=thinking, **kwargs)
            self.stats["mock"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = True
            self.stats["last_latency_ms"] = int((time.time() - t0) * 1000)
            if stream:
                return resp
            resp["_meta"] = {
                "provider": provider_name, "model": model_name,
                "used_mock": True, "latency_ms": int((time.time() - t0) * 1000),
                "fallback_reason": "forced_mock", "route_reason": route_reason,
            }
            self._record_usage(provider, resp, True, kwargs.get("_intent", ""), resp["_meta"]["latency_ms"])
            return resp

        if not provider or not provider.get("api_key"):
            # 2026-10-09（R1）：**不再回落 Mock，直接报错**。
            # 原静默给一段假回答，用户无法察觉——这是"编造内容"，比报错危险得多。
            # ⚠️ 与上面 `llm.force_mock` 分工：那是有意的测试通道，此处是"没配却假装能跑"。
            # ★ P0-3：同样记 `call_failed=1`（没产出任何内容），
            #   kind=`no_api_key`——这是**配置错**，与运行期超时/余额要能分开统计。
            self._record_usage(provider, {"usage": {}}, True,
                               kwargs.get("_intent", ""), int((time.time() - t0) * 1000),
                               call_failed=True,
                               failure="no_api_key: LLM 未配置 API Key")
            raise LLMUnavailableError(
                f"LLM 未配置 API Key（provider={provider_name or '未配置'}）"
                f"——已停止请求，不返回模拟内容。"
                f"请在「模型管理」配置该 provider 的 API Key；"
                f"若需离线演示，显式开启 llm.force_mock。",
                reason="no_api_key", provider=provider_name, model=model_name)

        # 真实调用（P1-3 插件化：按 provider_type 经注册表创建实现）
        try:
            ptype = provider.get("provider_type") or "openai_compat"
            if not ProviderRegistry.has(ptype):
                ptype = "openai_compat"
            impl = ProviderRegistry.create(ptype, provider)
            # 2026-09-17 S4：修真实链路 bug —— 原先同时把 model/temperature/max_tokens 作为**具名参数**传下去，
            # 又用 `**kwargs` 把同一批键再透传一次；只要调用方带了这三个参数之一，就会抛
            #   TypeError: got multiple values for keyword argument 'model'/'temperature'/'max_tokens'
            # 而该异常被下方 `except Exception` 吞掉 → **静默回落 Mock**。
            # 现网正在这样调用的有两处：`norm_apply.py:281`（temperature+max_tokens）、
            # `workflows/engine.py:569`（model）—— 等于这两处的真实调用从未生效。
            # 修法：先把这三个键从透传字典里摘出，再分别走具名参数（行为保真、不再重复）。
            _impl_kw = dict(kwargs)
            # P1-1（2026-09-24）：失败重试（指数退避）+ provider 回退。此前一次失败即静默回落 Mock。
            # P1-1c：回退检测 —— _chat_resilient 重试耗尽换备选后，返回的 provider 已是备选实例；
            # 先记主 provider id，回来比对才知道"本次调用是否发生过回退"（落 llm_usage_stats 观测）。
            _prim_pid = (provider or {}).get("id", 0)
            resp, _retry_n, provider = self._chat_resilient(
                provider, messages, _impl_kw, stream, tools, thinking)
            _fallback_used = 1 if (provider or {}).get("id", 0) != _prim_pid else 0
            _fb_pid = (provider or {}).get("id", 0) if _fallback_used else 0
            if _fallback_used:
                # P1-3：provider 回退也是"降级"（答案来自备选模型，可能更弱/更贵），同样要可见
                self.stats["fallback_total"] = int(self.stats.get("fallback_total", 0)) + 1
            # 回退成功时 provider 已被换成实际使用的那个 → 同步名字，保证 _meta / usage 归属正确
            provider_name = provider.get("name", provider_name)
            model_name = provider.get("model_name", model_name)
            if stream:
                # 流式：包装生成器，复用 usage 落库（M7）；重试/回退观测一并透传（P1-1c）
                return self._stream_wrapped(resp, provider, provider_name, model_name, t0, route_reason,
                                            kwargs.get("_intent", ""), retry_count=_retry_n,
                                            fallback_used=bool(_fallback_used), fallback_provider_id=_fb_pid)
            self.stats["real"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = False
            self.stats["last_latency_ms"] = int((time.time() - t0) * 1000)
            resp["_meta"] = {
                "provider": provider_name, "model": model_name,
                "used_mock": False, "latency_ms": int((time.time() - t0) * 1000),
                "route_reason": route_reason,
                "retry_count": _retry_n,   # P1-1：重试次数（0=首次成功），供可观测
                "fallback_used": bool(_fallback_used),   # P1-1c：本次调用是否发生过 provider 回退
                "fallback_provider_id": _fb_pid,         # P1-1c：回退后实际使用的 provider id（未回退=0）
                "usage": dict(resp.get("usage") or {}),   # T5：真实 token 透传（TokenCounter 复用）
            }
            self._record_usage(provider, resp, False, kwargs.get("_intent", ""), resp["_meta"]["latency_ms"])
            return resp
        except Exception as e:
            # 2026-09-17 S4：静默回落 Mock 会让"AI 回答其实来自 Mock"完全无人知晓
            #（实测就发生过：tools 载荷含中文工具名 → provider 400 → 静默降级）。
            # 此处只补留痕，**不改变降级行为**（控制流与返回值保持原样）。
            try:
                import logging as _lg
                import sys as _sys
                try:
                    _fr = _sys._getframe(1)
                    _caller = "%s:%d" % (_fr.f_code.co_filename.rsplit("\\", 1)[-1], _fr.f_lineno)
                except Exception:
                    _caller = "-"
                _lg.getLogger("mbse.llm").error(
                    "LLM 真实调用失败 → 已终止（不再回落 Mock）：调用点=%s intent=%s provider_id=%s provider=%s model=%s "
                    "stream=%s tools=%d 异常=%s: %s",
                    _caller, kwargs.get("_intent", ""), (provider or {}).get("id"), provider_name,
                    model_name, stream, len(tools or []),
                    type(e).__name__, str(e)[:200],
                )
            except Exception:
                pass
            # 2026-10-09（R1）：**不再回落 Mock，改为抛错终止**。
            # 实测该路径最常见的触发源就是 402 额度耗尽（2026-10-09 实测：
            # 「LLM API 402: Insufficient Balance」→ 回落 Mock → 用户拿到编造内容）。
            # 保留统计与落库（成本要计入、失败要可观测），但**不再伪造回答**。
            #
            # ★ P0-3（2026-10-09）修正：此前这里记`used_mock=1`，
            #   但本分支**没有**返回任何 Mock 内容 —— 它 raise，用户什么都没拿到。
            #   两件事混在一列 ⇒ "哪些回答是 Mock 做的"会把**所有失败**算进来
            #   （我据此误判过一次，实测 conv 9244 落库 0 行）。
            #   ⇒ 改为记 `call_failed=1` + `failure_kind`，`used_mock` 留0。
            self.stats["mock"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = True
            self._record_usage(provider, {"usage": {}}, True,
                               kwargs.get("_intent", ""), int((time.time() - t0) * 1000),
                               call_failed=True, failure=e)
            _hint = ""
            _s = str(e)
            if "402" in _s or "Insufficient Balance" in _s or "quota" in _s.lower():
                _hint = ("（检测到额度/余额不足：请充值，或临时把 embedding/LLM 的"
                         "仅用免费额度开关关闭）")
            raise LLMUnavailableError(
                f"LLM 调用失败，本次未生成任何内容。{_hint}原因：{type(e).__name__}: {_s[:200]}",
                reason="call_failed", provider=provider_name, model=model_name) from e


    # P1-1（2026-09-24）：参数类错误**不重试** —— 重试也不会成功，只会放大延迟。
    _NO_RETRY_MARKERS = ("400", "401", "403", "404", "422")

    def _retryable(self, e) -> bool:
        """是否值得重试：参数类 4xx 不重试；其余（超时/连接/5xx/429）重试。"""
        s = "%s: %s" % (type(e).__name__, e)
        return not any(m in s for m in self._NO_RETRY_MARKERS)

    def _chat_with_retry(self, impl, messages, impl_kw, stream, tools, thinking,
                         provider_name, model_name):
        """真实调用的重试包装（指数退避）。返回 (resp, retry_count)。

        实测依据：llm_usage_stats 1792 条里 196 条（10.9%）曾"一次失败即静默回落 Mock"，
        且失败样本 latency_ms 多为 0 —— 是瞬时连接失败，重试大概率能成功。
        · **流式不重试**：生成器一旦开始迭代就无法安全重放，保持原行为。
        · 每次重试打 warning 留痕（本仓纪律：静默兜底会让人去追不存在的"偶发"）。
        """
        from core import config as _cfg
        import time as _tt
        times = max(0, int(_cfg.get("llm", "retry_times", 2) or 0))
        backoff_ms = max(0, int(_cfg.get("llm", "retry_backoff_ms", 500) or 0))
        # ★ 2026-10-09：**总时间预算**（默认 0=不启用，零行为漂移）。
        #   为什么需要：`http_timeout_s` × `retry_times` 是**乘法**，
        #   实测 180×3+ 退避 ≈ **541.5 秒**（实测 545s 吻合）⇒ 用户等 9 分钟。
        #   只调小单次超时仍会乘出长尾；真正的闸门是**总预算**。
        #   语义：每轮重试前检查"已耗时 + 单次超时 > 预算" ⇒ 不再重试，直接抛。
        try:
            _budget = float(_cfg.get("llm", "total_timeout_budget", 0) or 0)
        except Exception:
            _budget = 0.0
        _t_start = _tt.time()
        _per_try = float(_cfg.get("llm", "http_timeout_s", 180) or 180)
        if stream:
            times = 0
        last = None
        for attempt in range(times + 1):
            # 总预算闸：剩余预算不够再跑一次就**不再重试**（避免长尾）
            if _budget and attempt > 0:
                _elapsed = _tt.time() - _t_start
                if _elapsed + _per_try > _budget:
                    try:
                        import logging as _lg2
                        _lg2.getLogger("mbse.llm").warning(
                            "LLM 重试中止：已耗时 %.0fs + 单次超时 %.0fs > 总预算 %.0fs"
                            "（provider=%s model=%s）",
                            _elapsed, _per_try, _budget, provider_name, model_name)
                    except Exception:
                        pass
                    break
            try:
                return impl.chat(messages, model=impl_kw.get("model"),
                                 temperature=impl_kw.get("temperature"),
                                 max_tokens=impl_kw.get("max_tokens"),
                                 stream=stream, tools=tools, thinking=thinking,
                                 **{k: v for k, v in impl_kw.items()
                                    if k not in ("model", "temperature", "max_tokens")}), attempt
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < times and self._retryable(e):
                    try:
                        import logging as _lg
                        _lg.getLogger("mbse.llm").warning(
                            "LLM 调用失败，第 %d/%d 次重试：provider=%s model=%s 异常=%s: %s",
                            attempt + 1, times, provider_name, model_name,
                            type(e).__name__, str(e)[:120])
                    except Exception:
                        pass
                    _tt.sleep(backoff_ms * (2 ** attempt) / 1000.0)
                    continue
                break
        raise last

    def _chat_resilient(self, provider, messages, impl_kw, stream, tools, thinking):
        """P1-1b（2026-09-24）：重试 + provider 回退。返回 (resp, retry_count, 实际使用的 provider)。

        · 主 provider 先走 `_chat_with_retry`（指数退避；流式不重试）
        · 仍失败且配置了 `llm.fallback_provider_id`（≠ 当前）→ 用备选 provider 再走一遍
        · **回退成功必须把 provider 换成实际用的那个**：否则 `_meta` 与 `usage` 统计
          会把成功记到主 provider 头上，回退形同"隐形"，下次看数据还是错的
        · 全部失败 → 抛出最后一次异常（交由外层回落 Mock，行为不变）

        P1-4（2026-10-06）：外层加**熔断**（llm/circuit_breaker.py，三态 CLOSED/OPEN/HALF_OPEN）。
        ⚠️ 三条设计约束，改动时务必保留：
        ① **熔断包在重试之外层**：若放在 `_chat_with_retry` 内部，一次调用产生的
           3 次重试失败会被计成 3 次 → 阈值形同被放大 3 倍，熔断提前触发。
        ② **熔断拒绝 ≠ 抛异常路径**：主 provider 被熔断时**不直接 raise**，
           而是**跳过它、继续走 provider 回退** —— 因为「主路挂了就走备路」正是
           回退机制存在的意义。若直接 raise，熔断会让「备选 provider」这条退路失效。
        ③ **流式不计入熔断**：流式无重试，一次失败即整轮失败，
           计入会把瞬时中断误判成"下游不可用"。
        """
        from core import config as _cfg
        ptype = provider.get("provider_type") or "openai_compat"
        if not ProviderRegistry.has(ptype):
            ptype = "openai_compat"
        impl = ProviderRegistry.create(ptype, provider)
        # P1-4：熔断键用 provider 的稳定标识（id 优先，退回名字）。
        _br = None
        _pkey = str(provider.get("id") or provider.get("name") or "?")
        try:
            from llm.circuit_breaker import get_breaker as _gb
            # P1-4：总开关。没开则完全退回"纯重试"旧行为（零漂移，便于灰度）。
            if not bool(_cfg.get("llm", "circuit_breaker_enabled", True)):
                _br = None
            else:
                _br = _gb()
                _ok, _why = _br.allow(_pkey)
        except Exception:
            _br, _ok, _why = None, True, ""
        if not _ok:
            # 主 provider 已熔断 ⇒ 不打它，直接走回退（见约束②）
            try:
                import logging as _lg
                _lg.getLogger("mbse.llm").warning("LLM 熔断跳过主 provider(%s)：%s",
                                                 provider.get("name"), _why)
            except Exception:
                pass
            return self._chat_fallback(provider, messages, impl_kw, stream, tools,
                                       thinking, _CircuitOpen(_why))
        try:
            resp, n = self._chat_with_retry(
                impl, messages, impl_kw, stream, tools, thinking,
                provider.get("name", "-"), provider.get("model_name", "-"))
            if _br and not stream:
                _br.record_success(_pkey)
            return resp, n, provider
        except Exception as e:  # noqa: BLE001
            last = e
            # P1-4：非流式失败计入熔断（见约束③）
            if _br and not stream and self._retryable(e):
                try:
                    _br.record_failure(_pkey, f"{type(e).__name__}: {str(e)[:150]}")
                except Exception:
                    pass
        return self._chat_fallback(provider, messages, impl_kw, stream, tools, thinking, last)

    def _chat_fallback(self, provider, messages, impl_kw, stream, tools, thinking, last):
        """主provider 之后的那一步：按配置走备选 provider，全败则抛 `last`。

        P1-4 抽出此方法，是为了让「主路熔断」与「主路重试失败」走**同一条**回退路径，
        避免两条路径各写一份、久而久之行为漂移。
        """
        from core import config as _cfg
        fb_id = int(_cfg.get("llm", "fallback_provider_id", 0) or 0)
        if not fb_id or fb_id == provider.get("id"):
            raise last
        fb = self.get_provider(fb_id)
        if not fb:
            raise last
        try:
            import logging as _lg
            _lg.getLogger("mbse.llm").warning(
                "LLM 主 provider(%s) 重试后仍失败，回退备选 provider(%s)：%s: %s",
                provider.get("name"), fb.get("name"), type(last).__name__, str(last)[:120])
        except Exception:
            pass
        ftype = fb.get("provider_type") or "openai_compat"
        if not ProviderRegistry.has(ftype):
            ftype = "openai_compat"
        fimpl = ProviderRegistry.create(ftype, fb)
        # P1-4：备选 provider 也各自计熔断（两个下游的可用性是独立事实）
        _fbr, _fkey = None, str(fb.get("id") or fb.get("name") or "?")
        try:
            from llm.circuit_breaker import get_breaker as _gb2
            _fbr = _gb2()
            _fok, _fwhy = _fbr.allow(_fkey)
        except Exception:
            _fbr, _fok, _fwhy = None, True, ""
        if not _fok:
            raise last          # 两条路都熔断 ⇒ 无路可走，抛主路的原因（更有诊断价值）
        try:
            resp, n = self._chat_with_retry(
                fimpl, messages, impl_kw, stream, tools, thinking,
                fb.get("name", "-"), fb.get("model_name", "-"))
            if _fbr and not stream:
                _fbr.record_success(_fkey)
            return resp, n, fb
        except Exception:
            if _fbr and not stream:
                try:
                    _fbr.record_failure(_fkey, "fallback provider 调用失败")
                except Exception:
                    pass
            raise last

    @staticmethod
    def _stream_resp(usage, finish, chars) -> dict:
        """把流式累积结果包装成 `_record_usage` 认识的响应形态（P0-a）。

        · 有 usage 帧 → 原样透传（真实 prompt/completion/reasoning/缓存命中全部落库）；
        · 无 usage 帧 → 按 `(reasoning_content + content) 字符数 // 2` 兜底估算
          `completion_tokens`，口径与 `_record_usage` 内的既有估算一致。
          ⚠️ **必须含 reasoning**：实测 reasoning 占 completion 的 79~92%（P1-26 / P1-27），
          只按正文估会把 completion 严重低估（实测真实 ct=25，而正文仅 1 字符）。
        """
        u = dict(usage or {})
        if not u.get("completion_tokens"):
            u["completion_tokens"] = max(int(chars) // 2, 0)
        return {"usage": u,
                "choices": [{"message": {"content": ""}, "finish_reason": finish or ""}]}

    @staticmethod
    def _warn_if_truncated(finish, usage, provider_name, model_name, intent, chars) -> None:
        """流式输出被截断时**当场留痕**（P0-a）。

        此前这类问题只能靠"用户反馈回复不完整"再回头翻数据反推；现在发生时即入日志。
        两档（都只在 `finish_reason == 'length'` 时触发）：
          · reasoning 占 completion ≥90% ⇒ **正文被推理吃光**（用户拿到的基本是空回复）
            —— 这正是 P1-26 实测 8 次空输出的形态（reasoning=8192 / content=0 / length）；
          · 其余 ⇒ 一般性截断（回复不完整）。
        """
        if str(finish or "") != "length":
            return
        try:
            _u = usage or {}
            ct = int(_u.get("completion_tokens") or 0)
            rt = int((_u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            share = (rt / ct) if ct else 0.0
            import logging as _lg
            if share >= 0.9:
                _lg.getLogger("mbse.llm").warning(
                    "[stream] ⚠️ 正文被推理吃光：intent=%s provider=%s model=%s "
                    "completion=%s reasoning=%s(%.0f%%) finish=length 累计字符=%s "
                    "→ 用户可能拿到空回复；请提高该环节 max_tokens 或改用非推理模型",
                    intent, provider_name, model_name, ct, rt, share * 100, chars)
            else:
                _lg.getLogger("mbse.llm").warning(
                    "[stream] 输出被 max_tokens 截断：intent=%s provider=%s model=%s "
                    "completion=%s reasoning=%s(%.0f%%) finish=length",
                    intent, provider_name, model_name, ct, rt, share * 100)
        except Exception:
            pass

    def _stream_wrapped(self, gen, provider, provider_name, model_name, t0, route_reason, intent,
                        retry_count: int = 0, fallback_used: bool = False, fallback_provider_id: int = 0):
        """流式生成器包装：透传 SSE 行 + 结束时落 usage 统计（M7 可观测）。

        P0-a（2026-10-02）：**逐帧解析**（`parse_stream_frame`）累积 usage 与 finish_reason，
        结束时落**真实值**；截断当场留痕。此前只透传、以空 dict 落库 ⇒ 流式记录恒为 0。
        · 解析在 `yield` **之前**（先记账再透传）：保证"已收到的帧"与"已记账的帧"恒等，
          否则最后一帧会在客户端中断时丢失（实测 usage 帧恰为倒数第二帧，直接丢 pt/ct）。
        · 中断（客户端断开）也记账 —— `GeneratorExit` 是 `BaseException`，须单独接。
        · P1-1c：retry/fallback 观测由 chat() 透传进来，落库时挂到 _meta（见 _record_usage）。
        """
        _usage: dict = {}
        _finish = ""
        _chars = 0
        try:
            for ev in gen:
                # ⚠️ 解析必须在 `yield` **之前**（实测踩过）：若放在 yield 之后，最后一帧
                #    "已发给客户端、但还没解析"就遇到客户端 close⇒ 该帧的 usage 永久丢失
                #    （实测：usage 帧正好是倒数第二帧 ⇒ 中断时 pt/ct 全丢，只剩正文估算值）。
                #    顺序改为"先记账再透传"后，"已收到的帧"与"已记账的帧"恒等。
                try:
                    _f = parse_stream_frame(ev)
                except Exception:
                    _f = {}
                if _f.get("usage"):
                    _usage = _f["usage"]                 # 末次非空覆盖（不依赖帧序）
                if _f.get("finish_reason"):
                    _finish = _f["finish_reason"]
                _chars += int(_f.get("text_chars") or 0)
                yield ev
            self.stats["real"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = False
            self.stats["last_latency_ms"] = int((time.time() - t0) * 1000)
            _sr = self._stream_resp(_usage, _finish, _chars)
            # P1-1c：把重试/回退观测挂到 _meta（_record_usage 统一从 _meta 取值落库）
            _sr["_meta"] = {"retry_count": retry_count, "fallback_used": fallback_used,
                            "fallback_provider_id": fallback_provider_id}
            self._record_usage(provider, _sr, False, intent,
                               int((time.time() - t0) * 1000))
            self._warn_if_truncated(_finish, _usage, provider_name, model_name, intent, _chars)
        except GeneratorExit:
            # 客户端提前断开（用户点「停止」/ 关掉页面）——**高频且此前完全不记账**。
            # `GeneratorExit` 继承 `BaseException` 而非 `Exception` ⇒ 下方 `except Exception`
            # **抓不到它** ⇒ 这次调用既不落 success 也不落 fail，而已产生的 token 已经真实
            # 计费 ⇒ 成本被系统性低估（长回复被中断的场景尤其明显）。
            # ⚠️ 此处**不能再 yield**（生成器正在关闭，yield 会抛 RuntimeError）。
            # 用 used_mock=False：它确实是真实调用（只是没跑完），要计入成本。
            try:
                _sr = self._stream_resp(_usage, _finish, _chars)
                _sr["_meta"] = {"retry_count": retry_count, "fallback_used": fallback_used,
                                "fallback_provider_id": fallback_provider_id}
                self._record_usage(provider, _sr, False,
                                   intent, int((time.time() - t0) * 1000))
                import logging as _lg
                _lg.getLogger("mbse.llm").warning(
                    "[stream] 客户端提前断开（已中断，usage 按已收到的部分记账）："
                    "provider=%s model=%s intent=%s 累计字符=%s prompt=%s completion=%s",
                    provider_name, model_name, intent, _chars,
                    (_usage or {}).get("prompt_tokens"), (_usage or {}).get("completion_tokens"))
            except Exception:
                pass
            raise
        except Exception as e:
            # 2026-10-09（R1）：流式路径的第三个降级点，**改为抛错**。
            # 原行为：只累加 stats["mock"] 并打日志（生成器已中断，无法再补吐内容）
            # ⇒ 前端收到的是一个**正常结束的流**，里面却没有任何有效内容。
            # 现在抛 `LLMUnavailableError`：上层 execute_stream 会把它转成错误事件，
            # 让前端显示"生成失败"而不是空回答。
            #
            # ⚠️ 此处在生成器内：raise 会终止迭代，
            #    所以必须先落库 usage（P0-a：已累积的 token 是**真实计费**的）。
            self.stats["mock"] += 1
            self.stats["last_used_mock"] = True
            try:
                self._record_usage(provider, self._stream_resp(_usage, _finish, _chars), True, intent,
                                   int((time.time() - t0) * 1000))
            except Exception:
                pass
            try:
                import logging as _lg
                _lg.getLogger("mbse.llm").warning(
                    "LLM 流式调用失败（已中断，本次未产出内容）：provider_id=%s provider=%s model=%s intent=%s "
                    "异常=%s: %s",
                    (provider or {}).get("id"), provider_name, model_name, intent,
                    type(e).__name__, str(e)[:200],
                )
            except Exception:
                pass
            raise LLMUnavailableError(
                f"LLM 流式调用中断，本次未生成完整内容。原因：{type(e).__name__}: {str(e)[:200]}",
                reason="stream_failed", provider=provider_name, model=model_name) from e

    def test_connection(self, provider_id):
        """连通性测试：经注册表 provider 实现（P1-3）。"""
        provider = self.get_provider(provider_id)
        if not provider:
            return {"ok": False, "error": "Provider not found"}
        if not provider.get("api_key"):
            return {"ok": False, "error": "API key not configured", "mock": True}
        ptype = provider.get("provider_type") or "openai_compat"
        if not ProviderRegistry.has(ptype):
            ptype = "openai_compat"
        try:
            impl = ProviderRegistry.create(ptype, provider)
            return impl.test_connection(provider)
        except Exception as e:
            return {"ok": False, "error": str(e)}


class LLMRouter:
    """D10 LLM 智能路由：能力标签 + 优先级 + Token 预算 → 选模型（8.2 多模型切换 / Token 成本控制）。

    ⚠️ **2026-09-20 实测：本类当前「无生产调用点」。**
    `LLMClient.chat()` 只有调用方显式传 `route_tags` 时才会走 `route()`，而全仓生产代码里
    `route_tags` 的传参处**只有一个**：`tests/manual_verify/verify_llm_route_d10.py`
    （复现：`grep -rn "route_tags" --include=*.py`，排除 `.venv/tmp/skill_packages`）。
    因此 `llm_providers` 的 `priority` / `tags` / `budget_tokens` 三列与 `usage_of()` 的
    「预算耗尽自动降级」**在生产中完全不生效**：
      - 不传 `route_tags` → 走 `get_default_provider()`（WHERE is_default=1，现已按 priority 排序）；
      - 传了 `route_tags` → 才走本路由。
    → **不要因为「priority 没生效」就去改 DB 配置**：先确认它到底有没有被调用。
    要启用本能力：给编排链路传 `route_tags`（属产品决策，会让选模型变成动态行为）。
    """

    @staticmethod
    def parse_tags(p: dict) -> list:
        try:
            tags = json.loads(p.get("tags") or "[]")
        except Exception:
            tags = []
        return [str(t) for t in tags] if isinstance(tags, list) else []

    @staticmethod
    def usage_of(conn, pid: int) -> int:
        """聚合该 provider 真实调用累计 token（Mock 不计入预算）。"""
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_tokens),0) s FROM llm_usage_stats "
                "WHERE provider_id=? AND used_mock=0", (pid,)).fetchone()
            return int(row["s"] or 0) if row else 0
        except Exception:
            return 0

    def route(self, required_tags=None, task_desc: str = "", ignore_budget: bool = False) -> dict:
        from database import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM llm_providers WHERE model_type IN ('chat','multimodal') AND status='active'").fetchall()
            req = [str(t).strip() for t in (required_tags or []) if str(t).strip()]
            candidates, exhausted, rejected = [], [], []
            for r in rows:
                p = dict(r)
                tags = self.parse_tags(p)
                miss = [t for t in req if t not in tags]
                if req and not tags:
                    rejected.append({"id": p["id"], "name": p["name"], "model_name": p["model_name"],
                                     "reason": "未配置能力标签"})
                    continue
                if miss and tags:
                    rejected.append({"id": p["id"], "name": p["name"], "model_name": p["model_name"],
                                     "reason": f"能力标签缺失: {', '.join(miss)}"})
                    continue
                budget = int(p.get("budget_tokens") or 0)
                used = 0 if ignore_budget else self.usage_of(conn, p["id"])
                if budget > 0 and used >= budget:
                    exhausted.append({"id": p["id"], "name": p["name"], "model_name": p["model_name"],
                                      "budget_tokens": budget, "used_tokens": used,
                                      "reason": "Token 预算已耗尽（自动降级到次优模型）"})
                    continue
                score = int(p.get("priority") or 0) * 10 + (5 if p.get("is_default") else 0) \
                    + (2 if p.get("api_key") else 0)
                candidates.append({"id": p["id"], "name": p["name"], "model_name": p["model_name"],
                                   "provider_type": p.get("provider_type"), "tags": tags,
                                   "priority": p.get("priority"), "is_default": p.get("is_default"),
                                   "has_key": bool(p.get("api_key")),
                                   "budget_tokens": budget, "used_tokens": used, "score": score})
            candidates.sort(key=lambda c: (-c["score"], c["name"]))
        finally:
            conn.close()
        selected = candidates[0] if candidates else None
        reason = ""
        if selected:
            reason = (f"选中「{selected['name']}」({selected['model_name']})"
                      f" · 得分 {selected['score']}"
                      + (f" · 预算剩余 {max(selected['budget_tokens'] - selected['used_tokens'], 0)} tokens"
                         if selected["budget_tokens"] > 0 else "")
                      + (f" · 已用 {selected['used_tokens']} tokens" if selected["used_tokens"] else ""))
        elif exhausted and not req:
            reason = "全部候选模型 Token 预算已耗尽，无可用模型"
        elif rejected and not candidates and not exhausted:
            reason = f"无模型具备所需能力标签: {', '.join(req)}"
        else:
            reason = "无可用候选模型（请检查 provider 配置）"
        return {"task_desc": task_desc, "required_tags": req, "selected": selected,
                "candidates": candidates, "exhausted": exhausted, "rejected": rejected,
                "reason": reason}


# 全局实例（兼容旧契约：from llm import llm_client, llm_router）
llm_client = LLMClient()
llm_router = LLMRouter()
