# -*- coding: utf-8 -*-
"""tool_side_effect 已下沉到 `core.domain.tool_side_effect`（P1 收敛，2026-11-25）。

本文件只做 re-export：旧 import 路径拿到的仍是同一批函数对象，
无需改动 agent / 门禁里的任何调用方。新代码请用 `core.domain.tool_side_effect`。
"""
from core.domain.tool_side_effect import (
    RULE_VERSION,
    _DESTRUCTIVE_PREFIX,
    _WRITE_PREFIX,
    _APPROVAL_PARAMS,
    _BATCH_COMMAND_PARAMS,
    _RISK_BY_EFFECT,
    _props,
    derive,
    derive_all,
    summary,
)  # noqa: F401
