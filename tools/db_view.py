# -*- coding: utf-8 -*-
"""门禁**只读**开库的统一入口（2026-11-25）。

## 为什么需要

10 个门禁写成 `sqlite3.connect("file:mbse.db?mode=ro", uri=True)` —— 路径**写死**。
后果不是"读错库"这么简单，而是**隔离机制整条失效**：

  · `gate_runner --isolate-db` 给每个门禁一份独立库副本，靠 `MBSE_DB_PATH` 传递；
  · `core/config.py` 的 `database.path` 才认这个环境变量（`DB_PATH = get(...)`）；
  · 写死路径的门禁**绕过**它 ⇒ 隔离模式下它读的仍是**真库**：
      - 它对真库做的读取断言，验的可能是别的门禁留下的状态（顺序污染假红）；
      - 更糟的是"隔离看起来生效了"（其它门禁都换了库），唯独这几个没有 ——
        这类**部分生效**比完全失效更难发现。

## 用法

```python
from tools.db_view import open_ro
con = open_ro()          # 与产品同源的库，只读
```

Windows 路径要转正斜杠：SQLite 的 URI 形式 `file:C:/x/y.db?mode=ro` 才认，
`file:C:\\x\\y.db` 会被当成不存在的文件名（**不报错、直接建空库**，典型的静默坑）。
"""

from __future__ import annotations

import sqlite3


def db_path() -> str:
    """产品同源的库路径（尊重 `MBSE_DB_PATH`）。"""
    from core.config import DB_PATH
    return str(DB_PATH)


def open_ro(timeout: float = 10.0) -> sqlite3.Connection:
    """只读连接产品库。库不存在时抛 FileNotFoundError（而不是静默建空库）。"""
    import os
    p = db_path()
    if not os.path.isfile(p):
        raise FileNotFoundError("库不存在：%s（设 MBSE_DB_PATH 或先 init_db）" % p)
    uri = "file:%s?mode=ro" % p.replace("\\", "/")
    return sqlite3.connect(uri, uri=True, timeout=timeout)
