# -*- coding: utf-8 -*-
"""兼容垫片：`alternatives` 已于 2026-11-25 迁入 `services.alternatives`（P1 收敛：应用服务归位）。

本文件不含业务逻辑，只把旧路径在 `sys.modules` 指向新模块；新代码请用 `services.alternatives`。
"""
import sys as _sys

from services import alternatives as _impl

_sys.modules[__name__] = _impl
