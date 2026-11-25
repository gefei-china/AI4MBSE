# -*- coding: utf-8 -*-
"""兼容垫片：`norm_apply` 已于 2026-11-25 迁入 `services.norm_apply`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import norm_apply` / `from norm_apply import X` / `__import__("norm_apply")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `services.norm_apply`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from services import norm_apply as _impl

_sys.modules[__name__] = _impl
