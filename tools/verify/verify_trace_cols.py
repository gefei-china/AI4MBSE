# -*- coding: utf-8 -*-
"""P0-c 验证：LLM 调用的 trace→span 关联（conversation_id / run_id / trace_id / sub_task_key）。

纪律：夹具只建最小基表，列由**真实迁移** `_migrate_columns` 补（不手抄 DDL，避免与生产 schema 漂移）。
"""
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import core.config as _cfg  # noqa: E402

_db_fd, _DB = tempfile.mkstemp(suffix=".trace.db")
os.close(_db_fd)
_cfg.DB_PATH = _DB          # get_db() 内运行时读取 → 隔离生产库

from core.audit import (begin_trace, bind_trace, trace_context, clear_trace,  # noqa: E402
                        bind_request, request_context)
from database.connection import db_conn  # noqa: E402
from database.migrations.columns import _migrate_columns  # noqa: E402
from llm import LLMClient  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {extra}")


class _ClientStub:
    def __init__(self, host): self.host = host


class _ReqStub:
    def __init__(self, headers=None, client=None):
        self.headers = headers or {}
        self.client = client


with db_conn() as c:
    c.execute("""CREATE TABLE llm_usage_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER DEFAULT 0,
        provider_name TEXT DEFAULT '', model_name TEXT DEFAULT '', intent TEXT DEFAULT '',
        used_mock INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
        prompt_cache_hit_tokens INTEGER DEFAULT 0, prompt_cache_miss_tokens INTEGER DEFAULT 0,
        finish_reason TEXT DEFAULT '', reasoning_tokens INTEGER DEFAULT 0,
        retry_count INTEGER DEFAULT 0, fallback_used INTEGER DEFAULT 0,
        fallback_provider_id INTEGER DEFAULT 0, estimated_cost REAL DEFAULT 0,
        latency_ms INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _migrate_columns(c)     # ← 真实迁移补列（P0-c 的四列也在这里）

print("══ T1 迁移补列 ══")
with db_conn() as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(llm_usage_stats)")]
for col in ("conversation_id", "run_id", "trace_id", "sub_task_key"):
    check(f"T1-{col} 列已补", col in cols, cols)

print("══ T2 链路上下文语义 ══")
bind_request(_ReqStub({"x-request-id": "req-abc123"}, _ClientStub("127.0.0.1")))
t = begin_trace(conversation_id=77)
check("T2a trace_id 沿用 request_id", t["trace_id"] == "req-abc123", t)
check("T2b conversation_id 落上下文", t["conversation_id"] == 77, t)
t2 = bind_trace(run_id=808)
check("T2c bind_trace 合并而非覆盖", t2["conversation_id"] == 77 and t2["run_id"] == 808, t2)
t3 = bind_trace(sub_task_key="t2")
check("T2d sub_task_key 追加", t3["sub_task_key"] == "t2" and t3["run_id"] == 808, t3)
bind_trace(conversation_id=None)
check("T2e None 表示不修改该字段", trace_context()["conversation_id"] == 77, trace_context())
check("T2f conversation_id=0 时不再自动开新 trace_id",
      begin_trace(conversation_id=0, trace_id=None)["trace_id"] == "req-abc123")

print("══ T3 LLM 用量落库带 trace 归属 ══")
# T2f 已把 conversation_id 置 0（验证不重开 trace_id 的副作用）—— 这里按真实入口重建链路
begin_trace(conversation_id=77)
bind_trace(run_id=808, sub_task_key="t2")
client = LLMClient()
client._record_usage({"id": 3, "name": "测试源", "model_name": "deepseek-v4-flash"},
                     {"usage": {"prompt_tokens": 100, "completion_tokens": 50}},
                     used_mock=False, intent="design_analysis", latency_ms=120)
with db_conn() as c:
    row = c.execute("SELECT * FROM llm_usage_stats ORDER BY id DESC LIMIT 1").fetchone()
check("T3a conversation_id 落盘", row["conversation_id"] == 77, row["conversation_id"])
check("T3b run_id 落盘", row["run_id"] == 808, row["run_id"])
check("T3c trace_id 落盘", row["trace_id"] == "req-abc123", row["trace_id"])
check("T3d sub_task_key 落盘", row["sub_task_key"] == "t2", row["sub_task_key"])
check("T3e 用量本体未受影响", row["prompt_tokens"] == 100 and row["total_tokens"] == 150,
      (row["prompt_tokens"], row["total_tokens"]))

print("══ T4 线程池 ContextVar 继承（编排子任务路径）══")
import contextvars  # noqa: E402


def _submit_with_ctx(pool, fn, *args):
    """与 stream.py 内同名实现保持一致：ThreadPoolExecutor 不继承 ContextVar，需显式 copy。"""
    _ctx = contextvars.copy_context()
    return pool.submit(lambda *a: _ctx.run(fn, *a), *args)


def _worker_read():
    return trace_context()


with ThreadPoolExecutor(max_workers=2) as pool:
    inherited = _submit_with_ctx(pool, _worker_read).result()
    bare = pool.submit(_worker_read).result()
check("T4a copy_context 提交后子线程可见链路上下文",
      inherited.get("trace_id") == "req-abc123" and inherited.get("run_id") == 808, inherited)
check("T4b 裸 submit 会丢失（对照组：证明修复是必要的）",
      bare.get("trace_id") != "req-abc123", bare)

print("══ T5 隔离：清理后不串号 ══")
clear_trace()
_bind = trace_context()
check("T5a clear_trace 后为空", _bind == {}, _bind)
client._record_usage({"id": 3, "name": "测试源", "model_name": "m"},
                     {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                     used_mock=True, intent="outside_session", latency_ms=1)
with db_conn() as c:
    r2 = c.execute("SELECT * FROM llm_usage_stats ORDER BY id DESC LIMIT 1").fetchone()
check("T5b 会话外调用归属为 0（不串上一轮）", r2["conversation_id"] == 0 and r2["trace_id"] == "",
      (r2["conversation_id"], r2["trace_id"]))

print("══ T6 降级：缺列时不丢用量数据 ══")
with db_conn() as c:
    c.execute("ALTER TABLE llm_usage_stats DROP COLUMN sub_task_key")   # 模拟迁移未到位
    n_before = c.execute("SELECT COUNT(*) n FROM llm_usage_stats").fetchone()["n"]
bind_trace(conversation_id=1)
client._record_usage({"id": 3, "name": "降级源", "model_name": "m"},
                     {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
                     used_mock=True, intent="degrade", latency_ms=2)
with db_conn() as c:
    n_after = c.execute("SELECT COUNT(*) n FROM llm_usage_stats").fetchone()["n"]
check("T6a 缺列时降级写入仍成功（不丢用量）", n_after == n_before + 1, (n_before, n_after))

os.remove(_DB)
print(f"\n══ {PASS} PASS / {len(FAIL)} FAIL ══")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
sys.exit(1 if FAIL else 0)
