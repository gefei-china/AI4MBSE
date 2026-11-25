# -*- coding: utf-8 -*-
"""仓库根的**单点**解析（2026-11-25 P1 分层归位）。

## 为什么需要它

P1 把 75 个顶层模块搬进 `core/domain` / `adapters` / `services` / `services/tools`
之后，原来那种

    ROOT = os.path.dirname(os.path.abspath(__file__))

就**不再等于仓库根**（搬进 `core/domain/` 后它等于 `.../core/domain`）。
实测这类写法在本仓有 **11 个模块 / 13 处**，而它们拼的都是**仓库根下的资产**：

| 资产 | 消费方 | 写错的后果 |
|---|---|---|
| `checker.jar` / `sysml.library` / `java-runtime` | `sysml_v2_check` / `sysml_ast` / `sysml_stdlib_tools` | **校验器静默不可用**（返回 `unavailable` 被放行） |
| `sysml_models/` | `sysml_check_tools` / `sysml_project_check_tools` | 样例模型读不到 |
| `mbse.db` | `methodology_profile_tools` | 连到不存在的库（`no such table`） |
| `integrations/*.json` | `register_http_tools` | 外部集成配置扫 0 条 |
| `tmp/vec_cache` | `vector_index` | 缓存写不出（静默降级） |

⇒ **不能靠"记得改"**：这类错全部是"路径拼错→文件不存在→被上层 `except` 吞掉"，
属本仓最典型的静默兜底形态。故收敛到一处，并配门禁
`tools/verify/verify_layer_ownership.py`（断言产品代码里不再出现 `dirname(abspath(__file__))`
当仓库根用）。
"""
from __future__ import annotations

import os

__all__ = ["repo_root", "repo_path"]

#: `core/paths.py` → 上两级即仓库根（`core/` 就在根下，不做可安装包的假设）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_root() -> str:
    """仓库根绝对路径（本进程内恒定）。"""
    return _ROOT


def repo_path(*parts: str) -> str:
    """拼仓库根下的路径：`repo_path("sysml.library")`。"""
    return os.path.join(_ROOT, *parts)
