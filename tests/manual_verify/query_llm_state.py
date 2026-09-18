# -*- coding: utf-8 -*-
"""查询 LLM provider 与最近调用统计（验证默认模型切换是否生效的辅助查询）。"""
import sqlite3

DB = "mbse.db"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

print("-- llm_providers --")
for r in c.execute("SELECT id, name, model_type, is_default, status, api_key != '' AS has_key, base_url FROM llm_providers ORDER BY id").fetchall():
    print(dict(r))

print("-- latest llm_usage_stats --")
for r in c.execute("SELECT id, provider_id, provider_name, model_name, used_mock, intent FROM llm_usage_stats ORDER BY id DESC LIMIT 6").fetchall():
    print(dict(r))

c.close()
