"""P0-2 上下文 Token 精确管理：TokenCounter。

计数策略（按可用性降级）：
1. 优先模型真实 usage（llm_usage_stats 已落库 / chat._meta.usage 透传）
2. tiktoken（若已安装，按 model_hint 选编码）
3. 加权估算：中文 1 字 ≈ 1.2 token，英文 1 词 ≈ 1.3 token（比旧 1.5 贴近实测，仍为估算）

Mock 路径返回估算值（不依赖外部网络）。
"""
import base64
import re
import struct
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
    """估算 messages（[{role, content}] 或 [{role, content, name}]）的 token 数。

    多模态 content（list）自 2026-09-21 起**同时计入 image_url 块** ——
    此前只累加 text 块，图片的视觉 token 完全不进估算，导致 `context_window` 守卫
    对着带图的请求**漏报**（同族教训：静默低估 = 假天花板）。
    """
    total = 0
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else ""
        if isinstance(c, list):  # 多模态 content 块
            for blk in c:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text":
                    total += count_tokens(blk.get("text", ""), model_hint)
                elif blk.get("type") == "image_url":
                    total += image_block_tokens(blk)
        else:
            total += count_tokens(c, model_hint)
        total += 4  # role 开销
    total += 2  # 首尾标记
    return total


# ── P2 视觉通道（2026-09-21）：图片块的 token 估算 ─────────────────────────
# 为什么必须补这一段：`count_messages_tokens` 原实现只累加 `type=="text"` 的块，
# image_url 块被完全跳过 → 图片的视觉 token（一张图几百至几千）不进 `_est_input_tokens`
# → `openai_compat` 的 context_window 守卫看不见图片 → **漏报超窗**。
_VISION_TOKENS_MIN = 85     # 最小图（≈256px 见方及以下）也要占用的视觉 token
_VISION_TOKENS_MAX = 4000   # 单图估算上限（防超大图把估算撑爆，掩盖真正的文本超窗）


def _data_url_head(data_url: str, n: int = 8192) -> bytes:
    """取 data URL 的**字节头**（不解码整图）；失败 → b""。"""
    try:
        s = str(data_url or "")
        i = s.find(",")
        if i != -1:
            s = s[i + 1:]
        s = s[: n - (n % 4)]
        return base64.b64decode(s, validate=False)
    except Exception:
        return b""


def image_size_from_data_url(data_url: str):
    """从 data URL 解析像素尺寸 (w, h)；失败 → (0, 0)。

    只读头部字节、**不解码整图**：视觉 token 只与像素数有关、与压缩率无关，
    为估 token 去解一张 4MB 的图纯属浪费。
    覆盖视觉通道实际产出的 PNG / JPEG，以及常见上传格式 GIF / BMP / WEBP。
    """
    b = _data_url_head(data_url)
    if len(b) < 24:
        return (0, 0)
    try:
        if b[:8] == b"\x89PNG\r\n\x1a\n":            # PNG：IHDR 固定偏移 16
            w, h = struct.unpack(">II", b[16:24])
            return (int(w), int(h))
        if b[:2] == b"BM" and len(b) >= 26:          # BMP
            w, h = struct.unpack("<ii", b[18:26])
            return (abs(int(w)), abs(int(h)))
        if b[:6] in (b"GIF87a", b"GIF89a") and len(b) >= 10:   # GIF（逻辑屏幕尺寸）
            w, h = struct.unpack("<HH", b[6:10])
            return (int(w), int(h))
        if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            fourcc = b[12:16]
            if fourcc == b"VP8X" and len(b) >= 30:   # 扩展格式（24 位长宽-1）
                return (1 + int.from_bytes(b[24:27], "little"),
                        1 + int.from_bytes(b[27:30], "little"))
            if fourcc == b"VP8 " and len(b) >= 30:   # 有损（14 位长宽）
                return (int.from_bytes(b[26:28], "little") & 0x3FFF,
                        int.from_bytes(b[28:30], "little") & 0x3FFF)
            if fourcc == b"VP8L" and len(b) >= 25:   # 无损（14 位长宽-1，打包在一个 32 位字）
                bits = int.from_bytes(b[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            return (0, 0)
        if b[:2] == b"\xff\xd8":                     # JPEG：扫段找 SOF0/1/2…
            i, n = 2, len(b)
            while i + 9 < n:
                if b[i] != 0xFF:
                    i += 1
                    continue
                m = b[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                if m == 0xD9:
                    break
                seg = (b[i + 2] << 8) | b[i + 3]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    return (((b[i + 7] << 8) | b[i + 8]), ((b[i + 5] << 8) | b[i + 6]))
                i += 2 + seg
    except Exception:
        return (0, 0)
    return (0, 0)


def image_block_tokens(block) -> int:
    """单个 image_url 块的视觉 token 估算（**保守：宁高不低**）。

    公式 `w*h/750`（业界常用的像素→视觉 token 近似），夹在
    [_VISION_TOKENS_MIN, _VISION_TOKENS_MAX]。

    为什么取向保守：本函数的唯一消费者是 `_est_input_tokens` → `context_window` 守卫。
    **低估的代价是请求真的打超窗 / 上游静默截断**（工程反复踩的假天花板同族问题），
    **高估的代价只是一条 WARNING**。两侧代价不对称，故宁高不低。

    尺寸解析不出时返回中性值（上限的一半）而**不是 0** —— 返回 0 等于「这张图不存在」，
    那正是本函数要修的漏报。
    """
    if not isinstance(block, dict):
        return 0
    iu = block.get("image_url")
    url = iu.get("url") if isinstance(iu, dict) else (iu if isinstance(iu, str) else "")
    if not url:
        return 0
    w, h = image_size_from_data_url(url)
    if w <= 0 or h <= 0:
        return max(_VISION_TOKENS_MIN, _VISION_TOKENS_MAX // 2)
    return max(_VISION_TOKENS_MIN, min(_VISION_TOKENS_MAX, int(round(w * h / 750.0))))


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
