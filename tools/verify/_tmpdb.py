# -*- coding: utf-8 -*-
"""tools/verify 下门禁共用的「临时库夹具」助手（2026-10-05）。

## 为什么需要

`verify_partial_persist` / `verify_continuation` 都用 `tmp/uisafe_r5.db` 当隔离库，
但**谁都不建表** —— 它们假设"这个文件已经存在且有 schema"。

本地能跑通只是因为**上一轮跑过留下了 db 文件**（历史残留）。
到 CI 上是全新 checkout ⇒ `sqlite3.connect()` 建出一个 **0 字节空库**
⇒ 第一条 SQL 就 `no such table: messages` ⇒ 门禁崩溃。

这正是 MEMORY 里那条纪律的实例：**"我这儿能跑"不等于"它会跑"** ——
本地绿是因为有脏状态，不是因为门禁写对了。

## 一条硬约束

`database/connection.py` 与 `database/schema.py` 都在 **import 期**执行
`from core.config import DB_PATH`，把值绑进自己模块作用域。
⇒ 只设环境变量对"已经 import 过 core 的进程"是废品，必须**直接改这三个模块的常量**
（MEMORY 已记）。本函数两条都做，并且**事后自证**表真的建出来了。

## 用法
    from _tmpdb import ensure_schema
    ok = ensure_schema(DB)     # False = 建库失败，调用方应判 FAIL 而不是继续崩
"""
import os
import sqlite3


def _has_messages(db_path):
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return False
    try:
        c = sqlite3.connect(db_path)
        n = c.execute("SELECT COUNT(*) FROM sqlite_master "
                      "WHERE type='table' AND name='messages'").fetchone()[0]
        c.close()
        return n > 0
    except Exception:
        return False


def ensure_schema(db_path, marker="messages"):
    """确保 `db_path` 是一套完整 schema 的库；缺则就地 init_db()。

    返回 True/False。**False 时调用方必须判 FAIL**，不能当作"没数据"继续跑下去
    （否则会退化成 `no such table` 崩溃，掩盖真正要验的东西）。
    """
    if _has_messages(db_path):
        return True

    abs_db = os.path.abspath(db_path)
    d = os.path.dirname(abs_db)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

    # 1) 环境变量（对还没 import core 的进程有效）
    os.environ["MBSE_DB_PATH"] = abs_db

    # 2) 已 import 的情况：直接改绑定后的模块常量（这条才是关键）
    try:
        import core.config as cfg
        cfg.DB_PATH = abs_db
    except Exception:
        pass
    try:
        import database.connection as conn_mod
        conn_mod.DB_PATH = abs_db
    except Exception:
        pass
    try:
        import database.schema as schema
        schema.DB_PATH = abs_db
    except Exception:
        pass

    # 3) 建库
    import database.schema as schema  # noqa: F811
    try:
        schema.init_db()
    except Exception:
        return False

    # 4) 自证：不是"调用了就当成功"，要真的看到表
    try:
        c = sqlite3.connect(abs_db)
        n = c.execute("SELECT COUNT(*) FROM sqlite_master "
                      "WHERE type='table' AND name=?", (marker,)).fetchone()[0]
        c.close()
        return n > 0
    except Exception:
        return False
