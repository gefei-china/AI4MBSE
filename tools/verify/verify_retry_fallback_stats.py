# -*- coding: utf-8 -*-
"""P1-1c 验证：LLM 重试/回退观测落库（llm_usage_stats 三列）。

验证四件事：
1. 契约：init_db 建库后 llm_usage_stats 含 retry_count / fallback_used / fallback_provider_id 三列；
2. 幂等：_migrate_columns 跑两遍不报错、不加重复列；
3. 功能：_record_usage 从响应 _meta 取值落库（retry_count=2 / fallback_used=1 / fallback_provider_id=7）；
4. 变异自证（数据驱动断言）：**不传** _meta 观测键 → 三列必须全 0 ——
   若实现是硬编码值或没接 _meta，此断言必挂。

用法：直接跑（零网络、零真库 —— MBSE_DB_PATH 指向临时库）。
"""
import os
import sys
import tempfile

# ⚠️ 必须在任何 repo import 之前：config.py 在 import 时解析 database.path
_TMP = tempfile.mkdtemp(prefix="verify_retry_fallback_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "verify.db")

sys.path.insert(0, r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")
os.chdir(r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")

import sqlite3

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ── 1. 契约：init_db 后三列存在 ──────────────────────────────────────────
from database import init_db, db_conn, migrations
init_db()

con = sqlite3.connect(os.environ["MBSE_DB_PATH"])
cols = [r[1] for r in con.execute("PRAGMA table_info(llm_usage_stats)").fetchall()]
con.close()
for c in ("retry_count", "fallback_used", "fallback_provider_id"):
    check(f"契约：llm_usage_stats 含列 {c}", c in cols, f"现有列={cols}")

# ── 2. 幂等：迁移跑两遍 ─────────────────────────────────────────────────
try:
    with db_conn() as c2:
        migrations.columns._migrate_columns(c2)
        migrations.columns._migrate_columns(c2)  # 第二遍应全部跳过
    check("幂等：_migrate_columns 重复执行不报错", True)
except Exception as e:  # noqa: BLE001
    check("幂等：_migrate_columns 重复执行不报错", False, repr(e))
con = sqlite3.connect(os.environ["MBSE_DB_PATH"])
cols2 = [r[1] for r in con.execute("PRAGMA table_info(llm_usage_stats)").fetchall()]
con.close()
check("幂等：重复迁移后无重复列", cols2 == cols, f"{cols2}")

# ── 3+4. 功能 + 变异自证：_record_usage 从 _meta 取值 ────────────────────
from llm import LLMClient

client = LLMClient()
PROVIDER = {"id": 3, "name": "主provider", "model_name": "m-1"}


def _fake_resp(meta_keys: dict) -> dict:
    resp = {"usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    if meta_keys is not None:
        resp["_meta"] = dict(meta_keys)
    return resp


def _last_row() -> tuple:
    con = sqlite3.connect(os.environ["MBSE_DB_PATH"])
    try:
        row = con.execute(
            "SELECT retry_count, fallback_used, fallback_provider_id FROM llm_usage_stats "
            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        con.close()
    return row


# 用例 A：带观测键（模拟 _chat_resilient 重试 2 次后回退到 id=7 的备选）
try:
    client._record_usage(PROVIDER, _fake_resp({
        "retry_count": 2, "fallback_used": True, "fallback_provider_id": 7}),
        False, "test_retry", 123)
    row = _last_row()
    check("功能：_meta 观测值正确落库", row == (2, 1, 7), f"实际={row}")
except Exception as e:  # noqa: BLE001
    check("功能：_meta 观测值正确落库", False, repr(e))

# 用例 B（变异自证）：不带观测键 → 必须全 0（若实现硬编码或没接 _meta，此处 FAIL）
try:
    client._record_usage(PROVIDER, _fake_resp({}), False, "test_no_meta", 123)
    row = _last_row()
    check("变异自证：无观测键 → 三列全 0", row == (0, 0, 0), f"实际={row}")
except Exception as e:  # noqa: BLE001
    check("变异自证：无观测键 → 三列全 0", False, repr(e))

# 用例 C：resp 完全无 _meta（旧调用点/直接构造的响应）→ 同样全 0
try:
    client._record_usage(PROVIDER, _fake_resp(None), False, "test_legacy", 123)
    row = _last_row()
    check("兼容：无 _meta 的旧响应 → 三列全 0", row == (0, 0, 0), f"实际={row}")
except Exception as e:  # noqa: BLE001
    check("兼容：无 _meta 的旧响应 → 三列全 0", False, repr(e))

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("✅ ALL PASS —— 三列契约/幂等/取值/自证全部通过")
