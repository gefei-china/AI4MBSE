# -*- coding: utf-8 -*-
"""兼容垫片：`action_binding_runtime` 已于 2026-11-25 迁入 `services.action_binding_runtime`（P1 收敛：应用服务归位）。

本文件不含业务逻辑，只把旧路径在 `sys.modules` 指向新模块；新代码请用 `services.action_binding_runtime`。
"""
import sys as _sys

from services import action_binding_runtime as _impl

_sys.modules[__name__] = _impl
