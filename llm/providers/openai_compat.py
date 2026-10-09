"""OpenAI 兼容 Provider（P1-3 插件化）：OpenAI-compatible /chat/completions 调用。

自 llm.py LLMClient 的真实调用逻辑抽取为独立 provider（P1-3 插件化）。
约定：接收 provider 配置 dict（DB llm_providers 行），提供 chat/stream/test_connection。
"""
import json
import logging
import re
import time
from typing import Optional

from ..base import BaseLLM

# ══════════════════════════════════════════════════════════════════
# 行内工具调用兜底解析（2026-10-09）
#
# 现象：部分模型不总走标准 `tool_calls` 字段，而是把工具调用
#      **以标记语言写进 content 文本**：
#        `<｜｜DSML｜｜ calls>`
#        `<｜｜DSML｜｜ invoke name="sysml_v2_validate">`
#        `  <｜｜DSML｜｜ parameter name="code" string="true">...</｜｜DSML｜｜>`
#        `<｜｜DSML｜｜ endinvoke>`
#      ⇒ 上游拿不到 tool_calls ⇒ 判定"无工具调用"⇒ 直接结束
#      ⇒ 模型想调的工具没调、代码没产出，**还会把上一轮代码贴出来**
#      （实测 N3 视图展开 state/parameter 产出雷同即此因）。
#
# 为什么修在 provider 层：这是**模型兼容性问题**，与哪个 Agent 无关
# ⇒ 任何用该模型的 Agent 都会踩；修在这里对所有调用方生效。
#
# 安全性：解析不出任何调用时**返回 None**，调用方保留原响应 ⇒ 零回归面。
# ══════════════════════════════════════════════════════════════════

#: 各种模型可能用的标记前缀（实测 deepseek 用DSML；其它模型可能用别的）
_INLINE_MARKERS = (r"DSML", r"tool_call", r"TOOL_CALL")

#: `... invoke name="xxx"` 的参数块起始
_RE_INVOKE = re.compile(
    r"invoke\s+name\s*=\s*[\"']([^\"']+)[\"']", re.I)
#: `... parameter name="xxx" [string="true"]>值<｜｜DSML｜｜ parameter>`
#: ★ 实测 deepseek-v4-flash 的真实格式（2026-10-09 从库里捞的原文）：
#:   `<｜｜DSML｜｜ parameter name="code" string="true">package ...<｜｜DSML｜｜ parameter>`
#:   ⇒ 结束标记带**词**（parameter / invoke / calls），且用**全角** `｜`
#:   ⇒ 不能用半角 `\|`；也不能只匹配 `>`（会一路吃到下一个标记）。
_RE_PARAM = re.compile(
    r"parameter\s+name\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)"
    r"<[｜|]{2}\s*DSML\s*[｜|]{2}\s*parameter\s*>",
    re.I | re.S)

#: 剥标记语言：`<｜｜DSML｜｜ ... >` 与 `<｜｜DSML｜｜ ... />`
_RE_STRIP_MARKUP = re.compile(
    r"<[｜|]{2}\s*DSML\s*[｜|]{2}[^>]*?>[\s\S]*?"
    r"<[｜|]{2}\s*DSML\s*[｜|]{2}\s*(?:parameter|invoke|calls"
    r"|endinvoke|endcalls|end_calls)\s*>"
    r"|<[｜|]{2}\s*DSML\s*[｜|]{2}[^>]*/>",
    re.I)


def _salvage_inline_tool_calls(data: dict):
    """把 content 里的行内工具调用还原成标准 `tool_calls`。

    返回**新的**响应 dict；**识别不到则返回 None**（调用方保留原响应）。
    ★ 只在「原本没有 tool_calls」时出手——标准路径优先，绝不覆盖。
    """
    try:
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        if msg.get("tool_calls"):          # 标准路径已有 ⇒ 不碰
            return None
        content = msg.get("content") or ""
        if not any(m in content for m in _INLINE_MARKERS):
            return None
        names = _RE_INVOKE.findall(content)
        if not names:
            return None

        # 按 invoke 出现顺序切参数块：每个 invoke 之后的 parameter 归它所有
        calls, pending = [], None
        for seg in re.split(r"(?=invoke\s+name\s*=)", content, flags=re.I):
            m = _RE_INVOKE.search(seg)
            if not m:
                continue
            args = {}
            for pname, pval in _RE_PARAM.findall(seg):
                v = pval.strip()
                # 能当 JSON 就解，否则当字符串（工具入参几乎都是字符串）
                try:
                    args[pname] = json.loads(v)
                except (ValueError, TypeError):
                    args[pname] = v
            calls.append({"id": f"inline_{len(calls) + 1}",
                          "type": "function",
                          "function": {"name": m.group(1),
                                       "arguments": json.dumps(args, ensure_ascii=False)}})
        if not calls:
            return None

        # 把标记语言从 content 里剥掉，剩余文本仍作为正文（若有）
        cleaned = _RE_STRIP_MARKUP.sub("", content).strip()
        new = dict(data)
        new["choices"] = [dict(choices[0])]
        new["choices"][0] = dict(choices[0])
        new["choices"][0]["message"] = dict(msg)
        new["choices"][0]["message"]["content"] = cleaned
        new["choices"][0]["message"]["tool_calls"] = calls
        logging.getLogger(__name__).info(
            "inline tool_calls salvaged: %s", [c["function"]["name"] for c in calls])
        return new
    except Exception:                # noqa: BLE001 —— 兜底解析失败不得影响主链路
        return None

_log = logging.getLogger("mbse.llm")

# 上下文额度告警去重：同一 (provider_id, context_window, guard) 组合**只报一次**。
# 本模块是全平台 LLM 调用的唯一出口，若每轮都刷会把日志淹掉 → 必须去重。
_ctx_warned: set = set()


def _ctx_guard() -> str:
    """读 `llm.context_window_guard`；读取失败或非法值一律回落 `clamp`（= 改动前行为）。"""
    try:
        from core import config as _cfg
        v = str(_cfg.get("llm", "context_window_guard", "clamp") or "clamp").strip().lower()
    except Exception:
        return "clamp"
    return v if v in ("clamp", "warn", "off") else "clamp"


def _est_input_tokens(messages) -> int:
    """估算输入 token 数（仅供超窗留痕）。异常 → 0（绝不阻断主链路）。"""
    try:
        from core.token_counter import count_messages_tokens
        return int(count_messages_tokens(messages))
    except Exception:
        return 0


def _warn_context_once(cfg, cw, mt, est_in, guard, why) -> None:
    """上下文额度留痕（同 provider + cw + guard 只报一次；自身异常静默，不影响调用）。

    这是「假天花板」的**唯一可见出口**：DB `llm_providers.context_window` 可能只是保守
    配置而非模型真实上限（实测 id=1 配 cw=8192，上游在 in=6575 + out=5841 = 12416 时仍 200）。
    没有这条日志时，「输出被悄悄截短」与「模型本来就写不长」在现象上无法区分。
    """
    try:
        key = (cfg.get("id"), cw, guard)
        if key in _ctx_warned:
            return
        _ctx_warned.add(key)
        from core.token_counter import input_budget
        _log.warning(
            "[context_window] %s｜provider=%s(id=%s) model=%s｜context_window=%s "
            "max_tokens生效=%s 估算输入=%s 输入余量=%s guard=%s"
            "｜若该 context_window 是保守配置，置 llm.context_window_guard=warn/off 即可放开（warn 只告警不截断）",
            why, cfg.get("name"), cfg.get("id"), cfg.get("model_name"),
            cw, mt, est_in, input_budget(cw, mt, 512), guard,
        )
    except Exception:
        pass


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
        # ── context_window 守卫（2026-09-20：由「静默截断」改为「可配置 + 留痕」）────────
        # 原实现只有一句**静默截断**（`mt` 一旦超过 `cw` 就把它赋值成 `cw`）：把 DB 的
        # context_window 当硬上限，但该值可能是**保守配置而非模型真实上限**
        # （实测 id=1 配 cw=8192，上游在 in=6575 + out=5841 = 12416 时照常 200 返回）
        # → 后果是**静默压低输出上限，且现象上与「模型本来就写不长」无法区分**。
        # 三档（`llm.context_window_guard`；默认 clamp **与改动前逐字节等价**）：
        #   clamp = 超窗截到 cw（原行为）+ 首次触发 WARNING 留痕
        #   warn  = 不截断，只 WARNING（把「假天花板」暴露出来，交上游判定是否接受）
        #   off   = 完全不介入（静默，等价于删掉这一段）
        # 顺带**接线**：`core/token_counter.input_budget`（ctx - mt - safety）此前全仓无生产
        # 调用点（死代码），这里用它的余量判据识别「本次请求必然超窗」这一原先完全不可见的情形。
        _guard = _ctx_guard()
        _est_in = _est_input_tokens(messages)
        if mt > cw:
            if _guard == "clamp":
                mt = cw
            if _guard != "off":      # off = 完全不介入（含不告警），否则「静默」语义不成立
                _warn_context_once(cfg, cw, mt, _est_in, _guard, "max_tokens 超过 context_window")
        elif _guard != "off" and _est_in > 0 and _est_in + mt > cw:
            _warn_context_once(cfg, cw, mt, _est_in, _guard, "估算输入 + max_tokens 超过 context_window")
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
        # ★ 2026-10-09：部分模型（实测 deepseek-v4-flash）**不总是**把工具调用
        #   放进标准 `tool_calls` 字段，而是把`<｜｜DSML｜｜ calls>` 这类标记
        #   **直接写进 content 文本**。
        #   ⇒ 上游 `msg.get("tool_calls")` 拿不到 ⇒ 判定为"无工具调用"⇒ 直接结束，
        #   模型想调的工具没调、代码没产出，**还会把上一轮的代码贴出来**。
        #   ⚠️ 这是 **provider 层通用缺口**（任何用该模型的 Agent 都会踩），
        #   不是某个 Agent 的问题 ⇒ 修在这里，对所有 OpenAI 兼容模型生效。
        #   解析不出内容时**逐字返回原响应**（零回归面）。
        _salvaged = _salvage_inline_tool_calls(data)
        if _salvaged:
            data = _salvaged
        self.stats["real"] += 1
        self.stats["last_provider"] = provider_name
        self.stats["last_used_mock"] = False
        data["_meta"] = {
            "provider": provider_name, "model": model_name,
            "used_mock": False, "latency_ms": int((time.time() - t0) * 1000),
            # 上下文额度留痕（2026-09-20）：让「天花板到底是多少、本次有没有被截」可从响应侧审计，
            # 不必再去猜 DB 配置。纯新增键，不改任何既有键的语义。
            "context_window": cw, "max_tokens_effective": mt,
            "est_input_tokens": _est_in, "context_guard": _guard,
        }
        return data

    def _stream_api(self, url, payload, headers, provider_name="", model_name="", t0=None):
        """SSE 流式调用：逐行透传 `data: ...` 帧。

        P0-a（2026-10-02）两处改动，都是"把静默失败暴露出来"：
        ① **非 200 立刻抛错**。此前不分状态码直接 `iter_lines()` —— 上游返回 400
           （余额不足 / 参数错 / 鉴权失败）时错误正文不带 `data:` 前缀 ⇒ **零帧输出且不抛异常**
           ⇒ 生成器"正常结束" ⇒ 上层落一条**成功**记录、用户看到**空回复**。
           这正是「表面成功、实际为空」的静默形态（P1-26 的 402 就走过这条路）。
        ② `stream_options.include_usage` 改为**可配置**（`llm.stream_include_usage`，默认关）。
           实测 DeepSeek 流式**默认就发 usage 帧**，故默认无需该字段；部分 OpenAI 兼容端点
           （如百炼）不带则只能拿字符估算值 —— 需要精确 token 时打开此开关即可。
        """
        import httpx
        try:
            from core import config as _cfg
            _include_usage = bool(_cfg.as_bool("llm", "stream_include_usage", False))
        except Exception:
            _include_usage = False
        if _include_usage:
            payload = dict(payload)
            payload["stream_options"] = {"include_usage": True}
        try:
            with httpx.stream("POST", url, json=payload, headers=headers, timeout=120) as resp:
                if resp.status_code != 200:
                    _body = ""
                    try:
                        _body = resp.read().decode("utf-8", "ignore")[:300]
                    except Exception:
                        pass
                    raise ValueError(f"LLM 流式 API {resp.status_code}: {_body}")
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
