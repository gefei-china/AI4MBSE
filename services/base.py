"""Service 基类（P1-1 层抽象）。

约定：只上提「跨表编排」（涉及 ≥2 个 Repository / 引擎的编排逻辑）；
薄 CRUD 留在 router，防止为抽象而抽象。router 内轻量实例化 Service(conn)。
"""
from typing import Any, Dict


class BaseService:
    """统一：repo 缓存注入。不引入 DI 框架，router 内 Service(conn) 轻量实例化。"""

    def __init__(self, conn):
        self.conn = conn
        self._repos: Dict[type, Any] = {}

    def repo(self, repo_cls, *args, **kwargs):
        """按类缓存 Repository 实例（同 Service 生命周期内复用）。"""
        if repo_cls not in self._repos:
            self._repos[repo_cls] = repo_cls(self.conn, *args, **kwargs)
        return self._repos[repo_cls]

    def commit(self):
        self.conn.commit()
