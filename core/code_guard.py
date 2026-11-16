# -*- coding: utf-8 -*-
"""自注册能力的**静态审查器**（AST 扫描，2026-11-07 P2）。

## 为什么需要它（以及它不能替代什么）

`capability_propose` 允许模型提交一段 Python 脚本并在批准后执行。
执行时用的是**真解释器**（与 `skill_script_run` 同通道，因为这些能力要发 HTTP、
要读配置，塞不进 `sandbox.py` 的受限 builtins）。

⇒ 真解释器 + 模型写的代码 = 一个直白的 `open("D:/x","w")` 就能写到沙箱外。
运行时的 `cwd` 限制与路径前缀检查**都拦不住绝对路径**。

所以：**注册时审一次**（本模块），**运行时靠 cwd + 环境变量收窄**（`core/workspace`）。
两层都不是"安全沙箱"，只是"防直白越权"。刻意在文档与 UI 里写明这一点，
避免让人误以为拿到的是容器级隔离。

## 判据（阻断 vs 告警）

**阻断（BLOCK）—— 出现即拒绝注册：**
| 模式 | 理由 |
|---|---|
| `subprocess` / `os.system` / `os.popen` / `os.exec*` / `os.spawn*` | 任意命令执行 = 沙箱形同虚设 |
| `eval` / `exec` / `compile` / `__import__` | 动态代码执行绕过所有静态判据 |
| `socket` / `ftplib` / `telnetlib` / `smtplib` | 裸 socket（业务 HTTP 走 requests/urllib）|
| `shutil.rmtree` 作用于根外常量路径 | 递归删除 |
| `os.remove`/`os.unlink`/`open(...,'w')` + **绝对路径字面量** | 直白越界写 |
| `sys.modules[...]` 篡改 / `setattr(builtins,...)` | 运行时打补丁 |
| `ctypes` / `cffi` | 原生调用绕过一切 |

**告警（WARN）—— 允许但要看得见：**
| 模式 | 理由 |
|---|---|
| `open(` 任何形式 | 文件读写是能力正常需求，但要提示审批人 |
| `requests` / `urllib` / `httpx` | 对外发请求（可能泄露数据、可能改外部系统）|
| `import` 非标准库 | 依赖可能不存在 |
| 绝对路径**字面量**（非写模式） | 说明它在读沙箱外的东西 |

## 设计取舍：为什么按 AST 而不是正则

正则会被三件事骗过：注释里写 `subprocess`、字符串里写 `"subprocess"`、
变量名 `my_subprocess`。而 AST 只看**真实的调用点与 import**。
（本仓在门禁里已被正则坑过多次：`\b` 在中文失效、括号计数被嵌套引号骗。）

## 明确的能力边界（写在这里，因为不写就会被当成"安全"）

拦不住：
  · 字符串拼接/编码后的路径（`open("D:" + "/x", "w")`）
  · 通过第三方库间接执行（装了 `plumbum` 就能起进程）
  · getattr 派发（`getattr(os, "sys" + "tem")`）
⇒ 本模块的定位是**提高门槛 + 让审批人看见风险**，不是隔离。
"""
from __future__ import annotations

import ast

__all__ = ["audit_code", "BLOCK_RULES", "WARN_RULES"]

#: 阻断：`模块名` → 理由（前缀匹配，`os.system` 命中 `os` 下的 system）
_BLOCK_CALLS = {
    "subprocess": "任意命令执行（subprocess）",
    "eval": "动态代码执行（eval）",
    "exec": "动态代码执行（exec）",
    "compile": "动态编译（compile）",
    "__import__": "动态导入（__import__）",
    "os.system": "任意命令执行（os.system）",
    "os.popen": "任意命令执行（os.popen）",
    "os.execv": "进程替换（os.exec*）",
    "os.execl": "进程替换（os.exec*）",
    "os.execve": "进程替换（os.exec*）",
    "os.spawn": "起子进程（os.spawn*）",
    "os.fork": "fork 进程",
    "os.kill": "发信号（os.kill）",
    "shutil.rmtree": "递归删除目录",
    "ctypes": "原生调用（ctypes）",
    "cffi": "原生调用（cffi）",
}

_BLOCK_IMPORTS = {
    "subprocess": "任意命令执行",
    "ctypes": "原生调用",
    "cffi": "原生调用",
    "socket": "裸 socket（业务 HTTP 请用 requests/urllib）",
    "ftplib": "FTP（裸网络）",
    "telnetlib": "Telnet（裸网络）",
    "smtplib": "SMTP（裸网络）",
    "multiprocessing": "起进程",
    "pty": "伪终端",
}

#: 告警：看得见就行
_WARN_CALLS = {
    "open": "文件读写（能力正常需求，但审批人需留意路径）",
    "os.remove": "删除文件",
    "os.unlink": "删除文件",
    "os.rename": "移动/改名",
    "os.makedirs": "建目录",
    "shutil.copy": "复制文件",
    "shutil.move": "移动文件",
}
_WARN_MODULES = {
    "requests": "对外发 HTTP 请求（可能改外部系统 / 外传数据）",
    "urllib": "对外发 HTTP 请求",
    "httpx": "对外发 HTTP 请求",
    "aiohttp": "对外发 HTTP 请求",
    "paramiko": "SSH 连接",
    "psycopg2": "直连数据库",
    "pymysql": "直连数据库",
    "sqlalchemy": "直连数据库",
}

#: 写模式标记
_WRITE_MODES = {"w", "a", "x", "+", "wb", "ab", "xb", "w+", "a+", "r+"}


def _dotted(node) -> str:
    """把 `os.path.join(...)` 这类调用还原成 `os.path.join` 字符串（取不到返回 ''）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return (base + "." + node.attr) if base else node.attr
    return ""


def _is_abs_literal(node) -> bool:
    """是否绝对路径字面量（`"D:/x"` / `"/etc/passwd"` / `r"C:\\x"`）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value.strip()
        return bool(v) and (
            v.startswith("/") or v.startswith("\\\\")
            or (len(v) > 2 and v[1] == ":" and v[2] in ("/", "\\")))
    return False


def audit_code(code: str) -> dict:
    """审查一段 Python 源码。

    返回 `{"ok": bool, "blocks": [{line, rule, detail}], "warns": [...], "imports": [...]}`。
    `ok=False` 表示**不得注册**；`ok=True` 且 `warns` 非空表示可注册但要让审批人看见。

    语法错误也视为 block（不能让"跑不起来的脚本"进注册流程 —— 那样批准后必然失败，
    而失败原因要到执行时才暴露）。
    """
    blocks, warns, imports = [], [], []
    src = str(code or "")
    if not src.strip():
        return {"ok": False, "blocks": [{"line": 0, "rule": "empty", "detail": "脚本为空"}],
                "warns": [], "imports": []}
    if len(src) > 200_000:
        return {"ok": False,
                "blocks": [{"line": 0, "rule": "too_large",
                            "detail": "脚本超过 200KB（%d 字符）" % len(src)}],
                "warns": [], "imports": []}
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"ok": False,
                "blocks": [{"line": e.lineno or 0, "rule": "syntax_error",
                            "detail": "%s: %s" % (type(e).__name__, e.msg)}],
                "warns": [], "imports": []}

    # ── ① import 审查 ──
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        for m in mods:
            top = (m or "").split(".")[0]
            if not top:
                continue
            imports.append(m)
            if top in _BLOCK_IMPORTS:
                blocks.append({"line": getattr(node, "lineno", 0),
                               "rule": "import_blocked",
                               "detail": "禁止 import %s（%s）" % (m, _BLOCK_IMPORTS[top])})
            elif top in _WARN_MODULES:
                warns.append({"line": getattr(node, "lineno", 0), "rule": "import_warn",
                              "detail": "import %s：%s" % (m, _WARN_MODULES[top])})

    # ── ② 调用点审查 ──
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if not name:
            continue
        line = getattr(node, "lineno", 0)
        hit = None
        for pat, why in _BLOCK_CALLS.items():
            if name == pat or name.startswith(pat + "."):
                hit = why
                break
        if hit:
            blocks.append({"line": line, "rule": "call_blocked",
                           "detail": "禁止调用 %s（%s）" % (name, hit)})
            continue
        # 写模式 + 绝对路径字面量 ⇒ 直白越界写（阻断）
        if name == "open" and node.args:
            _mode = ""
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                _mode = str(node.args[1].value or "")
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    _mode = str(kw.value.value or "")
            _is_write = any(ch in _mode for ch in ("w", "a", "x", "+"))
            if _is_abs_literal(node.args[0]):
                blocks.append({
                    "line": line, "rule": "abs_path_write" if _is_write else "abs_path_read",
                    "detail": ("写/改绝对路径文件：%s — 自注册能力只允许在沙箱内写"
                               % ast.unparse(node.args[0])[:80]) if _is_write
                              else ("读绝对路径文件：%s — 允许但需审批人知情"
                                    % ast.unparse(node.args[0])[:80])})
                if not _is_write:
                    # 读绝对路径降级为告警（读不产生副作用）
                    blocks.pop()
                    warns.append({"line": line, "rule": "abs_path_read",
                                  "detail": "读绝对路径：%s" % ast.unparse(node.args[0])[:80]})
            elif name in _WARN_CALLS:
                warns.append({"line": line, "rule": "call_warn",
                              "detail": "%s（%s）" % (name, _WARN_CALLS[name])})
            continue
        # rmtree 的 target 是绝对路径字面量 ⇒ 多记一条（更严重）
        if name in _WARN_CALLS:
            warns.append({"line": line, "rule": "call_warn",
                          "detail": "%s（%s）" % (name, _WARN_CALLS[name])})

    # ── ③ 打补丁类：setattr(builtins, ...) / sys.modules 赋值 ──
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted(node.func) == "setattr" and node.args:
            _t = _dotted(node.args[0]) if not isinstance(node.args[0], ast.Name) else node.args[0].id
            if _t in ("builtins", "__builtins__", "sys"):
                blocks.append({"line": getattr(node, "lineno", 0), "rule": "patch_builtins",
                               "detail": "禁止运行时打补丁 setattr(%s, ...)" % _t})
        if isinstance(node, ast.Assign):
            for tg in node.targets:
                if isinstance(tg, ast.Subscript) and _dotted(tg.value) in (
                        "sys.modules", "__builtins__"):
                    blocks.append({"line": getattr(node, "lineno", 0),
                                   "rule": "patch_modules",
                                   "detail": "禁止篡改 %s" % _dotted(tg.value)})

    return {"ok": not blocks, "blocks": blocks, "warns": warns,
            "imports": sorted(set(imports))}


def summarize(res: dict) -> str:
    """一行摘要（给确认卡/接口回显用）。"""
    b = len(res.get("blocks") or [])
    w = len(res.get("warns") or [])
    if not res.get("ok"):
        return "❌ 审查未通过：%d 项阻断" % b
    return "✅ 审查通过（%d 项提示）" % w if w else "✅ 审查通过（无提示）"
