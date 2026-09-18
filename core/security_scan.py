"""P0-6 记忆安全扫描：MemoryScanner（写入前威胁检测）。

对齐 Hermes Agent 的记忆写入前威胁扫描，拦截三类风险：
- 提示注入（改写 Agent 行为）
- 凭据外泄（密钥/口令写入记忆被检索带出）
- 角色劫持 / 越权操作 / 数据外传

只读检测（不执行任何改写/执行），命中仅拦截「写」，可审计可追溯。
"""
import re

# (rule_key, 正则, 风险类型, 说明)
_BUILTIN_RULES = [
    # ── 提示注入 ──
    ("prompt_inject_cn", r"忽略(之前|先前|以上|所有|系统).{0,12}(指令|提示|规则|要求|限制)", "prompt_injection", "提示注入：试图无视既有指令"),
    ("prompt_inject_en", r"ignore (all )?(previous|prior|above).{0,20}(instruction|prompt|rule)", "prompt_injection", "提示注入（英文）"),
    ("role_takeover", r"(你现在是|你不再是|act as|pretend (to be|you are))", "role_takeover", "角色劫持：试图改写 Agent 身份"),
    ("soul_override", r"(覆盖|删除|忽略)\s*(你的|自己的|系统).{0,6}(人格|身份|角色)", "role_takeover", "人格覆盖指令"),
    # ── 凭据外泄 ──
    ("credential_kv", r"(api[_-]?key|secret|password|passwd|token|access[_-]?key|auth)\s*[:=]\s*[\w\-./]{12,}", "credential", "凭据键值对"),
    ("credential_sk", r"\bsk-[A-Za-z0-9]{16,}\b", "credential", "OpenAI 风格密钥"),
    ("credential_aws", r"\bAKIA[0-9A-Z]{16}\b", "credential", "AWS 访问密钥"),
    ("credential_private_key", r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "credential", "私钥块"),
    # ── 越权 / 数据外传 ──
    ("exfil_curl", r"(curl|wget|nc\b|Invoke-WebRequest|powershell).{0,40}https?://", "exfiltration", "命令外传数据到外部 URL"),
    ("privilege_esc", r"(删除|导出|下载|获取|查看).{0,8}(全部|所有|他人).{0,8}(用户|权限|密码|密钥|数据库)", "privilege_escalation", "越权操作描述"),
]


class MemoryScanner:
    """记忆写入威胁扫描器。"""

    RULES = list(_BUILTIN_RULES)

    @staticmethod
    def _compile_rules() -> list:
        """返回 [(rule_key, compiled, type, desc)]；支持 settings security.memory_scan_rules 扩展。"""
        try:
            from core import config as _cfg
            extra = _cfg.get("security", "memory_scan_rules", None)
        except Exception:
            extra = None
        out = [(k, re.compile(p, re.IGNORECASE), t, d) for k, p, t, d in MemoryScanner.RULES]
        if extra:
            try:
                import json
                for e in (json.loads(extra) if isinstance(extra, str) else extra):
                    if isinstance(e, dict) and e.get("pattern"):
                        try:
                            out.append((str(e.get("key", "custom")),
                                        re.compile(str(e["pattern"]), re.IGNORECASE),
                                        str(e.get("type", "custom")),
                                        str(e.get("desc", ""))))
                        except re.error:
                            pass
            except Exception:
                pass
        return out

    @staticmethod
    def scan(text: str) -> dict:
        """扫描文本，返回 {safe: bool, matched: [{rule, type, snippet, desc}]}。

        snippet 截取命中位置 ±20 字符（供审计，避免敏感明文完整入库）。
        """
        if not text:
            return {"safe": True, "matched": []}
        s = str(text)
        matched = []
        for key, rx, rtype, desc in MemoryScanner._compile_rules():
            m = rx.search(s)
            if m:
                start = max(m.start() - 20, 0)
                end = min(m.end() + 20, len(s))
                matched.append({"rule": key, "type": rtype, "desc": desc,
                                "snippet": s[start:end]})
        return {"safe": not matched, "matched": matched}

    @staticmethod
    def enabled() -> bool:
        try:
            from core import config as _cfg
            return _cfg.as_bool("security", "memory_scan_enabled", True)
        except Exception:
            return True
