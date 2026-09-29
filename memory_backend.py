# -*- coding: utf-8 -*-
"""记忆后端接口缝（极小实现）：为将来接 mem0 / Graphiti 留门，不引入任何外部依赖。

边界（刻意「不过度设计」）：只有 3 个方法 —— `search / deposit / maybe_deposit`，
它们是**领域层真正使用的读写面**；`forget / consolidate / maintain / record_access`
属**存储内部维护**（生命周期、去重、激活度），换后端时由其自理，不穿透到领域层。

默认后端 = 内置 sqlite（`MemoryService`，全 staticmethod，可直接当后端用）。
配置 `memory.backend` 非 sqlite 时按约定导入 `memory_backend_<name>` 模块的 `BACKEND` 对象；
导入失败 → 静默回落内置实现（不抛，不阻断主流程）。
"""


def get_memory_backend():
    """返回当前记忆后端实例（默认内置 sqlite 实现 MemoryService）。"""
    name = "sqlite"
    try:
        from core import config as _cfg
        name = str(_cfg.get("memory", "backend", "sqlite") or "sqlite").strip().lower()
    except Exception:
        name = "sqlite"
    if name and name != "sqlite":
        try:
            import importlib
            mod = importlib.import_module("memory_backend_" + name)
            backend = getattr(mod, "BACKEND", None)
            if backend is not None:
                return backend
        except Exception:
            pass
    from memory_service import MemoryService
    return MemoryService
