# -*- coding: utf-8 -*-
"""兼容垫片：`orch_supervisor` 已迁入 `services.orch_supervisor`（P1 应用服务归位）。不含业务逻辑。"""
import sys as _sys
from services import orch_supervisor as _impl
_sys.modules[__name__] = _impl
