# -*- coding: utf-8 -*-
"""兼容垫片：`skill_deposit` 已于 2026-11-25 迁入 `services.skill_deposit`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import skill_deposit` / `from skill_deposit import X` / `__import__("skill_deposit")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `services.skill_deposit`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from services import skill_deposit as _impl

_sys.modules[__name__] = _impl
