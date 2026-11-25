"""tools：Agent 可调用工具的实体实现与注册器（P1 分层归位，2026-11-25）。

与 `services/` 同层（`requirement_postprocess` 这类服务会直接消费工具实现），
所以单独成包只为划分命名空间，不构成新的依赖约束。
"""
