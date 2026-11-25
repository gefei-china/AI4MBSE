# -*- coding: utf-8 -*-
"""兼容垫片：`knowledge_reflow` 已于 2026-11-25 迁入 `services.knowledge_reflow`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import knowledge_reflow` / `from knowledge_reflow import X` / `__import__("knowledge_reflow")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `services.knowledge_reflow`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from services import knowledge_reflow as _impl

_sys.modules[__name__] = _impl
