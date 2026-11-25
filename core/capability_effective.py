# -*- coding: utf-8 -*-
"""兼容垫片：`capability_effective` 已迁入 `services.capability_effective`（P1 应用服务归位）。不含业务逻辑。"""
import sys as _sys
from services import capability_effective as _impl
_sys.modules[__name__] = _impl
