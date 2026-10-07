"""敏感信息脱敏（P0-3，2026-10-06）。

**为什么必须有（依据 docs/Agent生产化Harness对照核查-20261006.md §3.2）**：
文章原话——「工具参数里经常会带 Token、API Key 这类敏感信息，写入日志之前必须做脱敏处理……
我们 就见过因为审计日志里明文记录了 API Key，导致这份本来用来『保护安全』的日志反而成了新的泄露源」。

**本工程的实际暴露面（实测 2026-10-06）**：
- `tool_call_logs.arguments` —— `_log_tool_call`（agent/pipeline_parts/tools.py:705）
  把工具入参**原样JSON 落库**；
- `audit_logs.detail` —— 多个调用点把参数字符串拼进 detail
  （如 HIL 预览 `str(arguments)[:200]`）。

工具入参里出现密钥不是假设：`tools` 表有http 集成工具（配置驱动执行器），
其配置本身就含鉴权头；MCP 服务器配置含 token。**只要用户配一次，密钥就进库了。**

**脱敏口径（三条硬约束）**：
1. **只改可辨识的密钥值，不动键名** —— 审计要能回答「调用了哪个参数」，键名是证据；
2. **不追求完美召回，追求零漏出** —— 宁可误伤（把普通长字符串也遮一部分），
   也不放过真密钥。审计是安全设施，不是数据分析设施；
3. **幂等且便宜** —— 纯正则，无IO、无网络；审计是高频路径。

⚠️ **哈希链一致性**（本实现最易踩的坑）：
`audit_logs.hash = sha256(prev_hash |各字段)`，所以**脱敏必须发生在算哈希之前**，
否则会出现「库里是明文、哈希却是脱敏后算的」⇒ 链校验 `verify_chain()` 直接判失败。
故脱敏函数被调用在 `audit()` 内部、`_row_hash()` 之前，而不是由调用方各自处理。
"""
import json
import re

__all__ = ["redact_text", "redact_obj", "REDACTED"]

#: 脱敏后的占位符。刻意保留可grep 的固定串，便于审计时识别"此处曾有敏感值"。
REDACTED = "***REDACTED***"

#: 敏感键名（不区分大小写）。命中即整体替换该值。
#: 覆盖：密钥/令牌/口令/凭据/签名 + 常见变体（_key/-key/Key 后缀、secret、pwd）。
_SENSITIVE_KEY_RE = re.compile(
    r"""(?ix)
    (                                   # ① 键名
      [ "'\[]?                          # 可选的引号/方括号
      (?: [a-z0-9_.\-]* )?              # 可选前缀（deepseek_api / x- / my）
      (?: api[_\-]?key | apikey | secret[_\-]?key | secret | access[_\-]?key
        | access[_\-]?token | auth[_\-]?token | token | bearer | authorization
        | password | passwd | pwd | passphrase
        | private[_\-]?key | credential[s]? | session[_\-]?key | cookie )
      [ "'\]]?                          # 可选收尾
      \s* [:=]                          # 键值分隔符
    )
    (                                     # ② 值
      " [^"]* " | ' [^']* '              # 带引号：整体替换（含引号）
      | [^\s,;&}\]]+                     # 裸值：吃到分隔符为止
    )
    """
)

#: 裸密钥形态（无键名时的兜底）：sk-xxx / ghp_xxx / Bearer xxx / 长 base64 串。
#: 命中即替换整个 token。**必须带长度下限**，否则会误伤大量普通短词。
_BARE_TOKEN_RES = (
    # OpenAI/Anthropic/DeepSeek 风格：sk- 前缀 + ≥16 字符
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), REDACTED),
    # GitHub PAT
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    # 裸 Bearer 头
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer " + REDACTED),
    # JWT（三段 base64.urlsafe，中间两段非空）
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), REDACTED),
)

#: 超长不含空白/引号的串：疑似密钥体。阈值取 32（短于此的几乎都是正常标识符）。
_LONG_BLOB_RE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-+/=]{32,}(?![A-Za-z0-9_\-])")


def redact_text(text):
    """把一段文本里的敏感值替换为 `***REDACTED***`。非字符串原样返回。"""
    if not text or not isinstance(text, str):
        return text
    out = _SENSITIVE_KEY_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    for rx, rep in _BARE_TOKEN_RES:
        out = rx.sub(rep, out)
    # 兜底：长blob。**放在最后**，避免把已替换的占位符再次处理。
    out = _LONG_BLOB_RE.sub(REDACTED, out)
    return out


def redact_obj(obj, _depth: int = 0):
    """递归脱敏 dict/list/tuple/str。用于工具入参这类结构化字段。

    - `dict` 的键**原样保留**（键名是审计证据，见文件头「硬约束 1」）；
    - 值按类型处理：str 走 `redact_text`，容器递归；
    - 深度上限 12，防止自引用结构导致无限递归。
    """
    if _depth > 12:
        return "<...深度超限...>"
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, _depth + 1) for v in obj]
    return obj


def redact_json_str(raw, _depth: int = 0):
    """脱敏一段 JSON 字符串。解析失败则按纯文本脱敏（不丢内容）。"""
    if not raw or not isinstance(raw, str):
        return raw
    try:
        obj = json.loads(raw)
    except Exception:
        return redact_text(raw)          # 不是合法 JSON → 当纯文本处理，**不吞内容**
    return json.dumps(redact_obj(obj), ensure_ascii=False, sort_keys=True)