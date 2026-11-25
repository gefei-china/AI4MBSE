# -*- coding: utf-8 -*-
"""兼容垫片：`seed_registry` 已于 2026-11-25 迁入 `services.seed_registry`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import seed_registry` / `from seed_registry import X` / `__import__("seed_registry")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `services.seed_registry`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from services import seed_registry as _impl

if __name__ == "__main__":
    # 保留 `python seed_registry.py` 的 CLI 行为（实现模块里有 __main__ 块）
    import runpy as _runpy
    _runpy.run_module("services.seed_registry", run_name="__main__", alter_sys=True)
else:
    _sys.modules[__name__] = _impl
