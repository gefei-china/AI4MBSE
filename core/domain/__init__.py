"""domain：语义与元模型内核（P1 分层归位，2026-11-25）。

只允许依赖 `core.*` 与标准库/三方库；**不得** import `services` / `adapters` /
`agent` / `routers`。含：本体（OWL/语义/推理/蓝图）、SysML v2 元模型与校验器、
术语与文本归一、SHACL 入库门禁。

依赖是否越界由 `tools/verify/verify_layer_ownership.py` 断言。
"""
