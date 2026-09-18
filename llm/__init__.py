"""LLM 统一入口（P1-3 插件化）。

对外契约（与旧 llm.py 完全一致，全仓零改动）：
- llm_client：LLMClient 单例（chat/embed/test_connection/stats）
- llm_router：LLMRouter 单例（D10 智能路由）
- get_llm(provider_id)：延迟实例化 + 缓存（P1-3 新增，按 DB provider 配置创建对应 Provider）

新增 LLM = llm/providers/xxx.py 新建类（继承 BaseLLM）+ providers/__init__.py 注册一行
+ DB llm_providers 表新增一行（provider_type 填注册名）。零代码改动。
"""
import json
import time

from .base import BaseLLM
from .registry import ProviderRegistry
from .providers import (  # noqa: F401  （注册副作用）
    MockLLM,
    OpenAICompatProvider,
)

__all__ = ["BaseLLM", "ProviderRegistry", "MockLLM", "OpenAICompatProvider",
           "llm_client", "llm_router", "get_llm"]


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
            row = conn.execute("SELECT * FROM llm_providers WHERE model_type='chat' AND is_default=1 AND status='active'").fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


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
            if not pt and not ct:
                # Mock 或响应无 usage → 按输出文本长度估算（len(输出文本)//2）
                try:
                    _content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
                    ct = max(len(str(_content)) // 2, 1) if _content else 0
                except Exception:
                    ct = 0
            est = 0.0
            if not used_mock:
                est = (pt * LLMClient.PRICE_PER_1M["input"] + ct * LLMClient.PRICE_PER_1M["output"]) / 1_000_000
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
                    "prompt_tokens, completion_tokens, total_tokens, estimated_cost, latency_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (provider.get("id", 0) if provider else 0,
                     (provider or {}).get("name", "未配置"),
                     (provider or {}).get("model_name", "-"),
                     intent, 1 if used_mock else 0, pt, ct, pt + ct, round(est, 6), latency_ms),
                )
        except Exception:
            pass

    def get_default_provider(self):
        from database import get_db
        conn = get_db()
        # 对话默认模型（model_type='chat'）；向量默认由 embedder 单独选择
        row = conn.execute("SELECT * FROM llm_providers WHERE model_type='chat' AND is_default=1 AND status='active'").fetchone()
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
            resp = impl.chat(messages, model=_impl_kw.pop("model", None),
                             temperature=_impl_kw.pop("temperature", None),
                             max_tokens=_impl_kw.pop("max_tokens", None),
                             stream=stream, tools=tools, thinking=thinking, **_impl_kw)
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
    """D10 LLM 智能路由：能力标签 + 优先级 + Token 预算 → 选模型（8.2 多模型切换 / Token 成本控制）。"""

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
