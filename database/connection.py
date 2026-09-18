"""SQLite 连接管理：get_db / db_conn 上下文管理器。"""
import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import datetime

from core.config import DB_PATH

def get_db():
    # check_same_thread=False：允许跨线程使用（FastAPI 的 anyio 线程池可能在不同线程提交/回滚）。
    # 安全性由「请求级连接 + db_session 上下文管理」保证（每个请求独立连接，不跨请求共享）。
    from core import config as _cfg
    _timeout = int(_cfg.get("database", "connect_timeout", 30))
    conn = sqlite3.connect(DB_PATH, timeout=_timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=%d" % (_timeout * 1000))
    return conn


@contextmanager
def db_conn():
    """Exception-safe database connection context manager.

    Commits on success, rolls back on exception, and ALWAYS closes the
    connection. Prevents write-lock buildup when a SQL error (e.g. UNIQUE
    constraint violation) would otherwise skip conn.close().
    """
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
