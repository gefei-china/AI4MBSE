"""Repository 基类：通用 SQLite 访问工具。

所有域 repo 继承本类，统一：
- 连接注入（构造传入，路由侧由 Depends(db_session) 提供）
- 行转换（sqlite3.Row → dict，复用 core.audit.rows_to_list）
- 基础 CRUD 工具（rows/one/scalar/count/execute）

写操作内置「database is locked」重试：SQLite 偶发写锁竞争（WAL 下多线程并发写）
时等待后重试，最多 3 次，避免并发场景偶发 500。
"""
import sqlite3
import time
from typing import Any, Iterable, Optional

from core.audit import rows_to_list


class BaseRepo:
    """SQL 收拢基类：只做数据访问，不含业务编排（编排上提 services）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── 基础工具 ──
    def rows(self, sql: str, params: Iterable = ()) -> list:
        """查询多行 → list[dict]（JSON 序列化友好）。"""
        return rows_to_list(self.conn.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable = ()) -> Optional[dict]:
        """查询单行 → dict | None。"""
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable = (), default: Any = 0) -> Any:
        """查询单值（如 COUNT(*)/MAX()），无行返回 default。"""
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else default

    def count(self, table: str, where: str = "", params: Iterable = ()) -> int:
        """通用 COUNT 快捷方法。"""
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return self.scalar(sql, params)

    def execute(self, sql: str, params: Iterable = ()) -> int:
        """执行写操作，返回 lastrowid（无自增场景返回 rowcount）。

        内置「database is locked」重试（0.15s/0.3s/0.6s 退避），自愈偶发写锁竞争。
        """
        for attempt in range(3):
            try:
                cur = self.conn.execute(sql, tuple(params))
                return cur.lastrowid or cur.rowcount
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 2:
                    time.sleep(0.15 * (2 ** attempt))
                    continue
                raise
