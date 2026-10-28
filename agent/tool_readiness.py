# -*- coding: utf-8 -*-
"""工具「配置就绪度」判定 —— 连接参数没配好的工具**不应注入给模型**。

## 为什么要有（2026-10-28，conv 9736~9740 真机实证）

`tools` 表里 `source='zhiyuan'` 的 5 个 HTTP 工具，其 `config.url` 形如
`{cfg:zhiyuan.base_url}/api/project/ai/sysmlv2/gen`，而运行配置里
`zhiyuan.base_url = ""`、`zhiyuan.token = ""` —— 即**这些工具在当前部署下必然失败**。
实测证据（`tool_call_logs`）：这 5 个工具累计被调用 **16 次，成功 0 次**
（`timed out` / `[WinError 10054] 远程主机强迫关闭了一个现有的连接` /
`缺少必填参数` 三类）。更要命的是它们还在 `tool_jit.core_keep_extra` 里被
**常驻注入**（每轮固定占 3 个名额），于是：

1. **模型有"看起来能用"的写入工具**，就不会去用真正能落库的 MCP
   `import_sysml_text`（2026-10-28 真机：注入 39 个工具里两种都在，
   模型选了自建那个 → 连超时）；
2. 失败信息是"网络超时"，模型据此判断"平台不可用"，而不是"我没配 base_url"
   ⇒ **排障方向被彻底带偏**（用户看到的结论是"平台连不上"，实际是配置空串）。

⇒ 通用规则：**必需配置项为空 ⇒ 该工具不注入**（并在日志/卡片留痕）。
这不是针对智源的特例：任何 `kind='http'` 的配置驱动工具都走同一条判据。

## 口径

只校验**必需项**：`config.url` 里引用的 `{cfg:...}`。
`headers` / `body` 里的空值可能是可选的（网关不要求 token 的部署也存在），
把它们也算作"未就绪"会造成误杀 —— 只认 URL，因为 URL 空着**必然**失败。
"""

import json
import re

_RE_CFG = re.compile(r"\{cfg:([A-Za-z0-9_.]+)\}")


def _cfg_value(path: str) -> str:
    """`section.key` → `core.config` 读取；未配置/异常 ⇒ 空串。"""
    try:
        from core import config as _c
        if "." in path:
            sec, key = path.split(".", 1)
            v = _c.get(sec, key, "")
        else:
            v = _c.get(path, "")
        return "" if v is None else str(v).strip()
    except Exception:
        return ""


def required_cfg_paths(config) -> list:
    """从工具 config 里取出**必需**的配置路径（只认 url 里的占位符，去重保序）。"""
    try:
        if isinstance(config, str):
            config = json.loads(config or "{}")
        if not isinstance(config, dict):
            return []
        url = str(config.get("url") or "")
    except Exception:
        return []
    out, seen = [], set()
    for m in _RE_CFG.finditer(url):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def missing_required(config) -> list:
    """返回**为空**的必需配置路径（空列表 = 就绪）。"""
    return [p for p in required_cfg_paths(config) if not _cfg_value(p)]


def readiness(config):
    """``(ready: bool, reason: str)`` —— `reason` 供日志与卡片留痕。"""
    miss = missing_required(config)
    if not miss:
        return True, ""
    return False, ("必需配置为空：%s ⇒ 当前部署下该工具必然失败，已跳过注入"
                   % "、".join(miss))


def is_ready(config) -> bool:
    return not missing_required(config)


# ══════════════════════════════════════════════════════════════════════
# 调用**参数**就绪度：必需参数缺失 ⇒ 不该进入"人工确认队列"
# ══════════════════════════════════════════════════════════════════════

def missing_args(schema, arguments) -> list:
    """按工具的真实参数契约（`inputSchema.required`）列出**缺失的必需参数**。

    ## 为什么必须有（2026-10-28 conv 9741 实证）
    模型把**待办的验收文案**当成工具入参发了出去：
        import_sysml_text  {"value": "回执成功 + query_data_tree 能看到新元素"}
    （同一轮还有 query_projects/query_data_tree/validate 各带一条待办 verify 文案）
    而 MCP 工具**没有必填参数校验** —— 校验只写在 `http_tool_executor.py:216`
    （仅 HTTP 集成工具）。后果有两层：
      ① 这笔畸形调用**进了写前确认队列**（确认单 #38，预览「导入（未命名对象）」）；
      ② 用户若点了「批准」，执行的是一笔**没有包名、没有正文**的导入
         ⇒ 必然不生效，用户看到的就是"我确认了却没写进去"。
    ⇒ 参数不全时**根本不该入队**：要在"要不要人点头"之前就退回给模型，
      并明确告诉它缺哪些参数（它才有机会自我修正）。

    ## 口径
    只认 `schema.required`（平台自己声明的必需项），不额外发明必填规则；
    `arguments` 里键存在但值为空串也算缺（空串等价于没给）。
    """
    try:
        if isinstance(schema, str):
            import json as _json
            schema = _json.loads(schema or "{}")
        if not isinstance(schema, dict):
            return []
        req = schema.get("required") or []
        if not isinstance(req, (list, tuple)):
            return []
        args = arguments if isinstance(arguments, dict) else {}
        out = []
        for k in req:
            k = str(k)
            v = args.get(k, None)
            if v is None or (isinstance(v, str) and not v.strip()):
                out.append(k)
        return out
    except Exception:
        return []
