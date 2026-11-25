# -*- coding: utf-8 -*-
"""兼容垫片：`sandbox` 已于 2026-11-25 迁入 `services.sandbox`（P1 应用服务归位）。

不含业务逻辑，只把旧路径在 sys.modules 指向新模块；新代码请用 `services.sandbox`。
"""
import sys as _sys

from services import sandbox as _impl

_sys.modules[__name__] = _impl
