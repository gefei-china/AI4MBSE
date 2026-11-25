# -*- coding: utf-8 -*-
"""兼容垫片：`ontology_reasoning` 已于 2026-11-25 迁入 `core.domain.ontology_reasoning`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import ontology_reasoning` / `from ontology_reasoning import X` / `__import__("ontology_reasoning")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `core.domain.ontology_reasoning`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from core.domain import ontology_reasoning as _impl

_sys.modules[__name__] = _impl
