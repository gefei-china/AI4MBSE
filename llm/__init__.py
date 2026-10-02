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

from .base import BaseLLM
from .registry import ProviderRegistry
from .providers import (  # noqa: F401  （注册副作用）
    MockLLM,
    OpenAICompatProvider,
)

__all__ = ["BaseLLM", "ProviderRegistry", "MockLLM", "OpenAICompatProvider",
           "llm_client", "llm_router", "get_llm",
           "resolve_provider_cfg", "provider_supports_vision"]


def _load_provider_cfg(conn, provider_id):
    """按 provider_id 查 DB 配置；None/未找到 → {}（get_llm 默认走 Mock 兜底）。

    provider_id=None 时取「对话默认模型」（model_type='chat' 且 is_default=1）；
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
            row = conn.execute("SELECT * FROM llm_providers WHERE model_type='chat' AND is_default=1 AND status='active' ORDER BY priority DESC, id ASC LIMIT 1").fetchone()
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
    if str(cfg.get("model_type") or "").strip().lower() == "vision":
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


# ── P1-3：延迟实例化 + 缓存（实例按 provider_id/默认 隔离缓存）──
_instances: dict = {}


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
        self.stats = {"real": 0, "mock": 0, "last_provider": "", "last_used_mock": True,
                      "last_prompt_tokens": 0, "last_completion_tokens": 0}

    def _record_usage(self, provider, resp, used_mock: bool, intent: str = "", latency_ms: int = 0) -> None:
        """M7：LLM 调用统计落库（失败不阻断主流程）。

        Task 7 token 记账：响应含 usage 时按其 prompt/completion_tokens 落库；
        Mock / 无 usage 时按 len(输出文本)//2 估算 completion_tokens（估算兜底）；
        并将 token 总数回填 resp._meta.token_count（供子任务侧 meta.token_count 记账）。
        """
        try:
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
            # V2.6：最近一次调用 token 记账（stats 供 done 事件透传前端展示）
            try:
                self.stats["last_prompt_tokens"] = pt
                self.stats["last_completion_tokens"] = ct
            except Exception:
                pass
            from database import db_conn
            with db_conn() as conn:
                conn.execute(
                    "INSERT INTO llm_usage_stats (provider_id, provider_name, model_name, intent, used_mock, "
                    "prompt_tokens, completion_tokens, total_tokens, "
                    "prompt_cache_hit_tokens, prompt_cache_miss_tokens, "
                    "finish_reason, reasoning_tokens, estimated_cost, latency_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (provider.get("id", 0) if provider else 0,
                     (provider or {}).get("name", "未配置"),
                     (provider or {}).get("model_name", "-"),
                     intent, 1 if used_mock else 0, pt, ct, pt + ct, pch, pcm,
                     _fr, rt, round(est, 6), latency_ms),
                )
        except Exception:
            pass

    def get_default_provider(self):
        from database import get_db
        conn = get_db()
        # 对话默认模型（model_type='chat'）；向量默认由 embedder 单独选择
        # ORDER BY / LIMIT：同上（多行 default 时取 priority 最高者，消除不确定性）
        row = conn.execute("SELECT * FROM llm_providers WHERE model_type='chat' AND is_default=1 AND status='active' ORDER BY priority DESC, id ASC LIMIT 1").fetchone()
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
                "route_reason": route_reason,
                "usage": dict(resp.get("usage") or {}),   # T5：真实 token 透传（TokenCounter 复用）
            }
            self._record_usage(provider, resp, True, kwargs.get("_intent", ""), resp["_meta"]["latency_ms"])
            return resp

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
            resp, _retry_n, provider = self._chat_resilient(
                provider, messages, _impl_kw, stream, tools, thinking)
            # 回退成功时 provider 已被换成实际使用的那个 → 同步名字，保证 _meta / usage 归属正确
            provider_name = provider.get("name", provider_name)
            model_name = provider.get("model_name", model_name)
            if stream:
                # 流式：包装生成器，复用 usage 落库（M7）
                return self._stream_wrapped(resp, provider, provider_name, model_name, t0, route_reason,
                                            kwargs.get("_intent", ""))
            self.stats["real"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = False
            self.stats["last_latency_ms"] = int((time.time() - t0) * 1000)
            resp["_meta"] = {
                "provider": provider_name, "model": model_name,
                "used_mock": False, "latency_ms": int((time.time() - t0) * 1000),
                "route_reason": route_reason,
                "retry_count": _retry_n,   # P1-1：重试次数（0=首次成功），供可观测
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
                _lg.getLogger("mbse.llm").warning(
                    "LLM 真实调用失败 → 静默回落 Mock：调用点=%s intent=%s provider_id=%s provider=%s model=%s "
                    "stream=%s tools=%d 异常=%s: %s",
                    _caller, kwargs.get("_intent", ""), (provider or {}).get("id"), provider_name,
                    model_name, stream, len(tools or []),
                    type(e).__name__, str(e)[:200],
                )
            except Exception:
                pass
            resp = self.mock.chat(messages, stream=stream, thinking=thinking, **kwargs)
            self.stats["mock"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = True
            resp["_meta"] = {
                "provider": provider_name, "model": model_name,
                "used_mock": True, "latency_ms": int((time.time() - t0) * 1000),
                "fallback_reason": str(e)[:120] if isinstance(e, Exception) else "api_error",
                "route_reason": route_reason,
                "usage": dict(resp.get("usage") or {}),   # T5：真实 token 透传（TokenCounter 复用）
            }
            self._record_usage(provider, resp, True, kwargs.get("_intent", ""), resp["_meta"]["latency_ms"])
            return resp

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
        if stream:
            times = 0
        last = None
        for attempt in range(times + 1):
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
        """
        from core import config as _cfg
        ptype = provider.get("provider_type") or "openai_compat"
        if not ProviderRegistry.has(ptype):
            ptype = "openai_compat"
        impl = ProviderRegistry.create(ptype, provider)
        try:
            resp, n = self._chat_with_retry(
                impl, messages, impl_kw, stream, tools, thinking,
                provider.get("name", "-"), provider.get("model_name", "-"))
            return resp, n, provider
        except Exception as e:  # noqa: BLE001
            last = e
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
        resp, n = self._chat_with_retry(
            fimpl, messages, impl_kw, stream, tools, thinking,
            fb.get("name", "-"), fb.get("model_name", "-"))
        return resp, n, fb

    def _stream_wrapped(self, gen, provider, provider_name, model_name, t0, route_reason, intent):
        """流式生成器包装：透传 SSE 行 + 结束时落 usage 统计（M7 可观测）。"""
        try:
            for ev in gen:
                yield ev
            self.stats["real"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = False
            self.stats["last_latency_ms"] = int((time.time() - t0) * 1000)
            self._record_usage(provider, {"usage": {}}, False, intent,
                               int((time.time() - t0) * 1000))
        except Exception as e:
            # 2026-09-17 S4：流式路径是**第三个静默降级点** —— 原先只累加 stats["mock"]，
            # 既不落 llm_usage_stats 也不打日志，"流式调用失败"在系统里完全不可见。
            # 此处只补留痕与计数落库，不改变控制流（生成器已中断，无法再补吐内容）。
            self.stats["mock"] += 1
            self.stats["last_used_mock"] = True
            try:
                import logging as _lg
                _lg.getLogger("mbse.llm").warning(
                    "LLM 流式调用失败（已中断，无法回落内容）：provider_id=%s provider=%s model=%s intent=%s "
                    "异常=%s: %s",
                    (provider or {}).get("id"), provider_name, model_name, intent,
                    type(e).__name__, str(e)[:200],
                )
            except Exception:
                pass
            try:
                self._record_usage(provider, {"usage": {}}, True, intent,
                                   int((time.time() - t0) * 1000))
            except Exception:
                pass

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
                "SELECT * FROM llm_providers WHERE model_type='chat' AND status='active'").fetchall()
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
