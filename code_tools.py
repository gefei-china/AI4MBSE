# -*- coding: utf-8 -*-
"""code_exec —— 在沙箱里执行 Python / JS 代码（2026-11-06）

## 为什么需要

`agent/tool_side_effect.py` 那轮盘点发现：**沙箱早就写好了、但没接成工具**。
`sandbox.py` 提供 `run_code(code, language, inputs, timeout, max_output)`，
支持进程隔离 / 超时强杀 / builtins 与 import 白名单，可它从未进 `tools` 表
（`SELECT COUNT(*) FROM tools WHERE name LIKE '%code%'` = 0）⇒ 模型完全不知道它存在。

后果是所有"需要算一下、转一下、验证一下"的任务都只能靠模型心算：
  · 解析/生成复杂 JSON、正则提取、单位换算、统计计算
  · 自己写的 SysML 片段做语法/结构预检
  · 数据处理（CSV 行聚合、时间序列差分）
这些用自然语言做既慢又容易错，而机器一跑就对。

## 安全边界（不是"给个 shell"）

本工具**只暴露 `sandbox.run_code`**，不提供任意 shell：
  · 无 `open` / `os` / `subprocess` / `socket` —— 沙箱 builtins 白名单已禁
  · import 仅允许 json/math/re/random/datetime/collections/itertools/statistics/
    string/functools/operator/typing
  · 超时强杀（默认 10s），输出截断
  ⚠️ 刻意**不提供** shell / eval 透传：一旦给了任意命令执行，
      `side_effect` 分级就失去判据（`rm -rf` 无法归到 read 还是 write）。
      需要文件操作请用 file_* 工具（它们有路径保护），需要联网用 web_*。

## 与「自扩展」的关系

这是自扩展闭环的第二环：模型写代码 → **本工具执行验证** → 再决定是否沉淀成工具/技能。
"""
from __future__ import annotations

__all__ = ["exec_code_tool", "CODE_TOOL_NAMES", "CODE_TOOL_DEFS"]

#: 单次执行默认超时（秒）。与 sandbox.run_code 的默认一致，此处显式化便于审计。
DEFAULT_TIMEOUT = 10.0
#: 超时上限：防止模型传一个巨大的 timeout 绕过"快速失败"的意图。
MAX_TIMEOUT = 30.0
#: 输出截断上限（字符）。
MAX_OUTPUT = 8000

CODE_TOOL_NAMES = ("code_exec",)

CODE_TOOL_DEFS = [
    {
        "name": "code_exec",
        "description": (
            "在隔离沙箱中执行一段代码并返回变量内容（Python 或 JavaScript）。"
            "用途：精确计算、JSON/正则处理、单位换算、统计聚合、数据转换、"
            "对自己产出的代码做语法/结构预检。"
            "约定：Python 用 `_in` 读取入参、往 `_out` 字典里写结果；"
            "JS 同样用 `_in` / `_out`。只有 `_out` 里的内容会返回。"
            "限制：无文件读写、无网络、无 os/subprocess；仅可 import "
            "json/math/re/random/datetime/collections/itertools/statistics/"
            "string/functools/operator/typing；超时默认 10 秒。"
        ),
        "side_effect": "read",       # 沙箱内无持久副作用（无文件/网络）
        "risk_level": "low",
        "version": "v1.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string",
                         "description": "要执行的代码。Python 示例：`_out['sum'] = _in['a'] + _in['b']`"},
                "language": {"type": "string", "enum": ["python", "js"],
                             "description": "语言，默认 python"},
                "inputs": {"type": "object",
                           "description": "注入到代码里 `_in` 的键值对（比把数据硬编码进代码更安全）"},
                "timeout": {"type": "number",
                            "description": "超时秒数，默认 10，上限 30"},
            },
            "required": ["code"],
        },
    },
]


def _clamp_timeout(v) -> float:
    """把模型给的 timeout 收进 [0.5, MAX_TIMEOUT]。

    为什么要夹取：模型可能传 999 秒来表示"这个计算慢"，但那会让一次失误
    占住执行槽十几分钟；也可能传 0 表示"不用超时"，而 0 在 subprocess 里
    等价于**无限等待**（比不传更危险）。
    """
    try:
        t = float(v)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if t <= 0:
        return DEFAULT_TIMEOUT
    return min(max(t, 0.5), MAX_TIMEOUT)


def exec_code_tool(name: str, arguments: dict | None = None) -> dict:
    """统一入口：执行 code_exec。返回 {"ok": bool, "result": str}（与 file_tools 同形态）。

    为什么返回 JSON 字符串而不是裸文本：模型需要能分清
    「执行成功但 _out 为空」与「执行失败」——前者 ok=True、后者 ok=False 且带 error。
    """
    import json as _json

    args = arguments or {}
    if name not in CODE_TOOL_NAMES:
        return {"ok": False, "result": "未知代码工具: %s" % name}

    code = args.get("code") or ""
    if not str(code).strip():
        return {"ok": False, "result": "code 不能为空"}

    lang = str(args.get("language") or "python").strip().lower()
    inputs = args.get("inputs")
    if inputs is not None and not isinstance(inputs, dict):
        return {"ok": False, "result": "inputs 必须是对象（键值对）"}
    timeout = _clamp_timeout(args.get("timeout"))

    try:
        from sandbox import run_code
    except Exception as e:                      # pragma: no cover
        return {"ok": False, "result": "沙箱不可用：%s: %s" % (type(e).__name__, str(e)[:150])}

    try:
        r = run_code(code, language=lang, inputs=inputs or {},
                     timeout=timeout, max_output=MAX_OUTPUT)
    except Exception as e:
        return {"ok": False, "result": "沙箱执行异常：%s: %s" % (type(e).__name__, str(e)[:200])}

    ok = bool(r.get("ok"))
    err = r.get("error") or ""
    outputs = r.get("outputs") or {}
    content = r.get("content") or ""

    payload = {
        "ok": ok,
        "language": lang,
        "outputs": outputs,
        # content 是沙箱 stdout 摘要（可能含用户 print），保留供排查
        "stdout": content[:MAX_OUTPUT],
    }
    if not ok:
        payload["error"] = err or "执行失败（无错误详情）"
    return {"ok": ok, "result": _json.dumps(payload, ensure_ascii=False, default=str)[:MAX_OUTPUT]}
