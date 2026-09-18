"""OpenAI 兼容 Provider（P1-3 插件化）：OpenAI-compatible /chat/completions 调用。

自 llm.py LLMClient 的真实调用逻辑抽取为独立 provider（P1-3 插件化）。
约定：接收 provider 配置 dict（DB llm_providers 行），提供 chat/stream/test_connection。
"""
import json
import time
from typing import Optional

from ..base import BaseLLM


class OpenAICompatProvider(BaseLLM):
    """OpenAI-compatible Chat Completions provider。

    配置（cfg）：DB llm_providers 行 —— base_url / api_key / model_name / model_type /
    max_tokens / context_window / temperature / name。
    """

    name = "openai_compat"

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.stats = {"real": 0, "mock": 0, "last_provider": "", "last_used_mock": True}

    # ── 对外接口（BaseLLM 契约）──
    # 2026-09-17 S4：三个采样参数默认值改为 None，让「调用方显式传参 > DB 配置 > 内置默认」成立。
    # 旧的 `max_tokens=4096 / temperature=0.3` 默认值 + `cfg.get(...)` 优先的写法，会导致
    # 调用方显式传的 max_tokens 被 DB 配置（provider 71/72 = 16384）**顶掉**——即"传了也不生效"
    # （这正是历史上有 3 次 completion 精确等于 16,384 的原因）。
    # 语义：显式传参才覆盖 DB；不传则完全沿用原行为（仍取 DB 配置，没有 DB 配置才用内置默认）。
    def chat(self, messages, model=None, temperature=None, max_tokens=None, stream=False,
             tools=None, thinking=False, **kwargs):
        """OpenAI-compatible 调用。返回 OpenAI 响应 dict（附 _meta）；stream=True 返回 SSE 生成器。"""
        t0 = time.time()
        cfg = self.cfg
        provider_name = cfg.get("name", "未配置")
        # 优先级：调用方显式 model > DB model_name > "-"
        model_name = model or cfg.get("model_name") or "-"
        cw = cfg.get("context_window") or 8192
        # 优先级：调用方显式 max_tokens > DB max_tokens > 4096
        mt = max_tokens if max_tokens is not None else cfg.get("max_tokens", 4096)
        if mt > cw:
            mt = cw
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature if temperature is not None else cfg.get("temperature", 0.3),
            "max_tokens": mt,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        # 模型参数模板（DB llm_providers.model_params）：top_p / top_k / thinking_mode
        mp = {}
        try:
            mp = json.loads(cfg.get("model_params") or "{}")
        except Exception:
            mp = {}
        if mp.get("top_p") is not None and mp["top_p"] != "":
            try:
                payload["top_p"] = float(mp["top_p"])
            except (TypeError, ValueError):
                pass
        if mp.get("top_k") is not None and mp["top_k"] != "":
            try:
                payload["top_k"] = int(mp["top_k"])
            except (TypeError, ValueError):
                pass
        # 思考模式：模型默认 → 跟随调用方 thinking；开启/关闭 → 显式覆盖
        thinking_mode = mp.get("thinking_mode")
        if thinking_mode == "开启":
            thinking = True
        elif thinking_mode == "关闭":
            thinking = False
        if thinking:
            payload["thinking"] = {"type": "enabled"}
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        url = str(cfg.get("base_url", "")).rstrip("/") + "/chat/completions"
        if stream:
            return self._stream_api(url, payload, headers, provider_name, model_name, t0)
        import httpx
        resp = httpx.post(url, json=payload, headers=headers, timeout=180)
        data = resp.json()
        if resp.status_code != 200 or not data.get("choices"):
            raise ValueError(f"LLM API {resp.status_code}: {str(data)[:200]}")
        self.stats["real"] += 1
        self.stats["last_provider"] = provider_name
        self.stats["last_used_mock"] = False
        data["_meta"] = {
            "provider": provider_name, "model": model_name,
            "used_mock": False, "latency_ms": int((time.time() - t0) * 1000),
        }
        return data

    def _stream_api(self, url, payload, headers, provider_name="", model_name="", t0=None):
        import httpx
        try:
            with httpx.stream("POST", url, json=payload, headers=headers, timeout=120) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        yield line + "\n\n"
        except Exception as e:
            yield f"data: {{\"choices\":[{{\"delta\":{{\"content\":\"\"}}}}]}}\n\n"
            raise e from None
        finally:
            self.stats["real"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = False
            self.stats["last_latency_ms"] = int((time.time() - (t0 or time.time())) * 1000)

    def test_connection(self, cfg: Optional[dict] = None) -> dict:
        """连通性测试：优先 GET /models，失败则降级最小 chat 探测。cfg 覆盖实例配置。"""
        c = cfg or self.cfg
        if not c.get("api_key"):
            return {"ok": False, "error": "API key not configured", "mock": True}
        if "{" in str(c.get("base_url", "")) or "}" in str(c.get("base_url", "")):
            return {"ok": False, "error": "Base URL 含未替换的占位符（如 {WorkspaceId}），请替换为实际值后再测试"}
        try:
            import httpx
            headers = {"Authorization": f"Bearer {c['api_key']}"}
            if c.get("model_type") == "embedding":
                url = str(c["base_url"]).rstrip("/") + "/embeddings"
                resp = httpx.post(url, json={"model": c["model_name"], "input": ["ping"]},
                                  headers=headers, timeout=15)
                if resp.status_code == 200:
                    return {"ok": True, "status": 200, "method": "embeddings"}
                return {"ok": False, "status": resp.status_code, "method": "embeddings",
                        "error": f"HTTP {resp.status_code}: {self._resp_error(resp)}"}
            url = str(c["base_url"]).rstrip("/") + "/models"
            resp = httpx.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return {"ok": True, "status": resp.status_code, "method": "models"}
            chat_url = str(c["base_url"]).rstrip("/") + "/chat/completions"
            payload = {
                "model": c["model_name"],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            }
            resp2 = httpx.post(chat_url, json=payload, headers=headers, timeout=15)
            if resp2.status_code == 200:
                return {"ok": True, "status": 200, "method": "chat"}
            return {"ok": False, "status": resp2.status_code, "method": "chat",
                    "error": f"HTTP {resp2.status_code}: {self._resp_error(resp2)}"}
        except UnicodeEncodeError:
            return {"ok": False,
                    "error": "配置含非 ASCII 字符（中文等）：请检查 Base URL / API Key / 模型名是否含中文，API Key 应为服务商签发的真实密钥（sk- 开头）"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _resp_error(resp) -> str:
        """从 OpenAI 兼容错误响应中提取可读错误信息（百炼返回 {"error":{"message":...}}）。"""
        try:
            j = resp.json()
        except Exception:
            return (resp.text or "")[:200]
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                return err.get("message") or json.dumps(err, ensure_ascii=False)[:200]
            if isinstance(err, str):
                return err[:200]
            return (j.get("message") or str(j)[:200])
        return str(j)[:200]
