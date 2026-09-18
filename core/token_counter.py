"""P0-2 上下文 Token 精确管理：TokenCounter。

计数策略（按可用性降级）：
1. 优先模型真实 usage（llm_usage_stats 已落库 / chat._meta.usage 透传）
2. tiktoken（若已安装，按 model_hint 选编码）
3. 加权估算：中文 1 字 ≈ 1.2 token，英文 1 词 ≈ 1.3 token（比旧 1.5 贴近实测，仍为估算）

Mock 路径返回估算值（不依赖外部网络）。
"""
import re
import unicodedata

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

_tiktoken_enc = None
_tiktoken_ok = False


def _load_tiktoken():
    global _tiktoken_enc, _tiktoken_ok
    if _tiktoken_ok or _tiktoken_enc is not None:
        return _tiktoken_enc
    try:
        import tiktoken  # type: ignore
        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        _tiktoken_ok = True
    except Exception:
        _tiktoken_ok = True  # 标记已尝试，避免每次重试
        _tiktoken_enc = None
    return _tiktoken_enc


def count_tokens(text: str, model_hint: str | None = None) -> int:
    """估算文本 token 数。空/None → 0。"""
    if not text:
        return 0
    s = str(text)
    enc = _load_tiktoken()
    if enc is not None:
        try:
            return len(enc.encode(s))
        except Exception:
            pass
    # 加权估算：中文按字、英文按词、其他符号按字符
    cjk = len(_CJK_RE.findall(s))
    words = len(_WORD_RE.findall(s))
    rest = len(re.sub(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9_]+", "", s))
    return int(round(cjk * 1.2 + words * 1.3 + rest * 0.6))


def count_messages_tokens(messages: list, model_hint: str | None = None) -> int:
    """估算 messages（[{role, content}] 或 [{role, content, name}]）的 token 数。"""
    total = 0
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else ""
        if isinstance(c, list):  # 多模态 content 块
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    total += count_tokens(blk.get("text", ""), model_hint)
        else:
            total += count_tokens(c, model_hint)
        total += 4  # role 开销
    total += 2  # 首尾标记
    return total


def input_budget(context_window: int = 0, max_tokens: int = 0, safety: int = 512) -> int:
    """输入预算 = context_window - max_tokens - safety；参数无效按默认 8192/4096 兜底。"""
    cw = int(context_window or 0) or 8192
    mt = int(max_tokens or 0) or 4096
    safe = max(int(safety or 0), 0)
    return max(cw - mt - safe, 1024)


def partition(budget: int, weights: dict) -> dict:
    """按权重分区预算（自动归一；不足项给最小 64 token 保底）。"""
    total_w = sum(max(float(v), 0.0) for v in weights.values()) or 1.0
    out = {}
    for k, w in weights.items():
        out[k] = max(int(budget * max(float(w), 0.0) / total_w), 64)
    return out
