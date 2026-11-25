# -*- coding: utf-8 -*-
"""兼容垫片：`hil_service` 已于 2026-11-25 迁入 `services.hil_service`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import hil_service` / `from hil_service import X` / `__import__("hil_service")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `services.hil_service`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from services import hil_service as _impl

_sys.modules[__name__] = _impl
