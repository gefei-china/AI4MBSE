# -*- coding: utf-8 -*-
"""模块源码视图：把「兼容垫片路径」解析回真实实现文件。

## 为什么需要它

2026-11-25 的 P1 分层归位把 **94 个**顶层模块搬到 `services/` / `core/domain/` 等
新位置，旧路径保留 5 行垫片：

```python
import sys as _sys
from services.orch_supervisor import _impl as _impl   # 实际写法见下
_sys.modules[__name__] = _impl
```

门禁若继续按**旧路径**读源码，读到的是这 5 行垫片，后果分两种：
  * 断言「某内容**在**源码里」⇒ 恒红（吵，能发现 —— 本轮实测 4 个门禁就是这样红）；
  * 断言「某内容**不在**源码里」⇒ **恒绿**（静默假绿 —— 本仓 P0 正在治的病，
    而且它不会自己暴露）。

⇒ 源码断言必须与模块的**物理位置解耦**：读之前先解析到真实实现文件。
这与 `tools/stream_source.py` 是同一族设施（那个解决 stream.py 拆分，
这个解决模块搬迁），拆分/搬迁之后门禁**不改一行**仍然验的是真实代码。

## 用法

```python
from tools.module_source import read_module_source, resolve_source
src = read_module_source("core/orch_supervisor.py")   # 自动穿透垫片
path = resolve_source("core/orch_supervisor.py")      # 需要写回变异时用真实路径
```

`read_module_source()` 的换行语义与 `open(..., encoding="utf-8").read()` **完全一致**
（universal newline ⇒ 文本里只有 `\\n`）—— 门禁断言里写死了 `\\n`，两者不一致会批量假红
（stream 视图那轮实测踩过：31 个门禁假红）。
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 垫片指纹：`_sys.modules[__name__] = _impl`（允许 `sys.` 前缀与任意别名）
_SHIM_RE = re.compile(r"^\s*(?:_?sys)\.modules\[__name__\]\s*=\s*(\w+)\s*$", re.M)
# 垫片里的实现导入：`from services.x import y as _impl`
_IMPORT_RE = re.compile(
    r"^\s*from\s+([\w.]+)\s+import\s+([\w]+)(?:\s+as\s+(\w+))?\s*$", re.M)

_MAX_DEPTH = 5


def root() -> str:
    return _ROOT


def _abs(rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(_ROOT, rel)


def _module_to_file(dotted: str) -> str | None:
    """`services.orch_supervisor` → 该模块的文件路径（.py 或 包/__init__.py）。"""
    parts = dotted.split(".")
    for cand in (os.path.join(*parts) + ".py",
                 os.path.join(*parts, "__init__.py")):
        p = os.path.join(_ROOT, cand)
        if os.path.isfile(p):
            return p
    return None


def _read_text_quiet(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def shim_target(path: str) -> str | None:
    """若 path 是垫片，返回它指向的真实实现文件；否则 None。"""
    txt = _read_text_quiet(path)
    if not txt:
        return None
    m = _SHIM_RE.search(txt)
    if not m:
        return None
    alias = m.group(1)
    imported = {}
    for im in _IMPORT_RE.finditer(txt):
        mod, name, as_name = im.group(1), im.group(2), im.group(3) or im.group(2)
        imported[as_name] = "%s.%s" % (mod, name) if name != mod.split(".")[-1] else mod
    dotted = imported.get(alias)
    if not dotted:
        return None
    return _module_to_file(dotted)


def is_shim(rel_or_abs: str) -> bool:
    return shim_target(_abs(rel_or_abs)) is not None


def resolve_source(rel_or_abs: str, _depth: int = 0) -> str:
    """把（可能是垫片的）路径解析到真实实现文件；非垫片原样返回**绝对路径**。"""
    p = _abs(rel_or_abs)
    if _depth > _MAX_DEPTH or not os.path.isfile(p):
        return p
    nxt = shim_target(p)
    if not nxt:
        return p
    if os.path.abspath(nxt) == os.path.abspath(p):
        return p                      # 自指，防死循环
    return resolve_source(nxt, _depth + 1)


def read_module_source(rel_or_abs: str) -> str:
    """读真实实现源码。换行语义与 `open(..., encoding='utf-8').read()` 一致（仅 LF）。"""
    return _read_text_quiet(resolve_source(rel_or_abs))
