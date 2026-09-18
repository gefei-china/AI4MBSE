"""Services 层（P1-1 层抽象）。

只上提「跨表编排」：涉及 ≥2 个 Repository / 引擎的编排逻辑；
薄 CRUD 留在 router，防止为抽象而抽象。
"""
from .base import BaseService

__all__ = ["BaseService"]
