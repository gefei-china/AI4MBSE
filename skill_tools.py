# -*- coding: utf-8 -*-
"""兼容垫片：`skill_tools` 已于 2026-11-25 迁入 `services.tools.skill_tools`（P1 分层归位）。

本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：
`import skill_tools` / `from skill_tools import X` / `__import__("skill_tools")` 全部照常工作，
且**模块身份唯一**（不会出现两份模块级状态）。

新代码请用 `services.tools.skill_tools`；产品代码里的旧路径由
`tools/verify/verify_layer_ownership.py` 断言为 0。
"""
import sys as _sys

from services.tools import skill_tools as _impl

if __name__ == "__main__":
    # 保留 `python skill_tools.py` 的 CLI 行为（实现模块里有 __main__ 块）
    import runpy as _runpy
    _runpy.run_module("services.tools.skill_tools", run_name="__main__", alter_sys=True)
else:
    _sys.modules[__name__] = _impl
