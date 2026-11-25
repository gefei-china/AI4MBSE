# -*- coding: utf-8 -*-
"""兼容垫片：`sysml_profile` 已于 2026-11-25 迁入 `core.domain.sysml_profile`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import sysml_profile` / `from sysml_profile import X` / `__import__("sysml_profile")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `core.domain.sysml_profile`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from core.domain import sysml_profile as _impl

_sys.modules[__name__] = _impl
