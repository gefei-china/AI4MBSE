# -*- coding: utf-8 -*-
"""建一套 init_db 过的干净库，供门禁的"CI 等价环境"验证使用。

用法：.venv/Scripts/python.exe tmp/mkclean.py <目标db绝对路径>
"""
import os
import sys

if len(sys.argv) < 2:
    print("usage: mkclean.py <db_path>")
    sys.exit(2)
target = os.path.abspath(sys.argv[1])
os.environ["MBSE_DB_PATH"] = target
d = os.path.dirname(target)
if d and not os.path.isdir(d):
    os.makedirs(d, exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core.config as cfg  # noqa: E402
import database.connection as conn_mod  # noqa: E402
import database.schema as schema  # noqa: E402

for m in (cfg, conn_mod, schema):
    m.DB_PATH = target

# 自证：三个模块的 DB_PATH 都指向目标（MEMORY：只设环境变量对已 import 的进程无效）
assert os.path.abspath(cfg.DB_PATH) == target, "cfg 未切换"
assert os.path.abspath(conn_mod.DB_PATH) == target, "connection 未切换"
assert os.path.abspath(schema.DB_PATH) == target, "schema 未切换"

schema.init_db()
print("OK %s" % target)
