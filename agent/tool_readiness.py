# -*- coding: utf-8 -*-
"""tool_readiness 已下沉到 `core.domain.tool_readiness`（P1 收敛，2026-11-25）。

本文件只做 re-export：旧 import 路径拿到的仍是同一批函数对象，
无需改动 agent / 门禁里的任何调用方。新代码请用 `core.domain.tool_readiness`。
"""
from core.domain.tool_readiness import (
    _RE_CFG,
    _cfg_value,
    required_cfg_paths,
    missing_required,
    readiness,
    is_ready,
    missing_args,
)  # noqa: F401
