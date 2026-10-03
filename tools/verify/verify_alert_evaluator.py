# -*- coding: utf-8 -*-
"""P0-d 验证：周期告警评估器（全局时间窗指标 + 冷却去重 + 两套评估器互不重复）。"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import core.config as _cfg  # noqa: E402

_db_fd, _DB = tempfile.mkstemp(suffix=".alert.db")
os.close(_db_fd)
_cfg.DB_PATH = _DB

from database.connection import db_conn  # noqa: E402
from database.migrations.columns import _migrate_columns  # noqa: E402
from database.migrations.misc import _migrate_audit_chain  # noqa: E402
from core.alert_evaluator import (GLOBAL_METRICS, collect_global_metrics,  # noqa: E402
                                  evaluate_once, notify_alert_webhook)
from core.audit import audit, bind_trace, _row_hash, _GENESIS  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {extra}")


with db_conn() as c:
    c.execute("""CREATE TABLE alert_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, rule_type TEXT DEFAULT 'threshold',
        metric TEXT NOT NULL, operator TEXT DEFAULT '>', threshold REAL DEFAULT 0,
        level TEXT DEFAULT 'warning', notify_url TEXT DEFAULT '', secret TEXT DEFAULT '',
        status TEXT DEFAULT 'active', last_fired_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE alert_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER DEFAULT 0, rule_name TEXT DEFAULT '',
        run_id INTEGER DEFAULT 0, flow_name TEXT DEFAULT '', metric TEXT DEFAULT '',
        actual REAL DEFAULT 0, threshold REAL DEFAULT 0, operator TEXT DEFAULT '',
        level TEXT DEFAULT 'warning', status TEXT DEFAULT 'open',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
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
    c.execute("""CREATE TABLE audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_name TEXT DEFAULT '', event_type TEXT NOT NULL,
        detail TEXT DEFAULT '', result TEXT DEFAULT 'success', ip_address TEXT DEFAULT '',
        branch TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _migrate_columns(c)
    _migrate_audit_chain(c)

_NOW = datetime.utcnow()
_H1 = (_NOW - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
_YDAY = (_NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

with db_conn() as c:
    # 近 1h：10 次调用 —— 4 次 mock、2 次回退、3 次截断；延迟含长尾
    for i in range(10):
        c.execute(
            "INSERT INTO llm_usage_stats (intent, used_mock, fallback_used, finish_reason, "
            "estimated_cost, latency_ms, created_at) VALUES (?,?,?,?,?,?,?)",
            ("design", 1 if i < 4 else 0, 1 if i < 2 else 0, "length" if i < 3 else "stop",
             0.01, [100, 110, 120, 130, 140, 150, 160, 170, 180, 3000][i], _H1))
    # 昨日同期：成本 0.05 → 今日累计应显著高于它（突增检测）
    c.execute("INSERT INTO llm_usage_stats (intent, estimated_cost, created_at) VALUES (?,?,?)",
              ("design", 0.05, _YDAY))
    # 认证失败 3 次 + 被拦截 2 次
    for i in range(3):
        audit("攻击者", "auth_login", f"登录失败{i}", result="failed", conn=c)
    for i in range(2):
        audit("张三", "delete_entity", f"被权限门拦截{i}", result="blocked", conn=c)
    # 一条正常审计（保证链本身是完好的）
    audit("李四", "normal_op", "正常操作", conn=c)

print("══ M1 全局指标采集 ══")
with db_conn() as c:
    m = collect_global_metrics(c)
check("M1a 近 1h 调用数", m["llm_calls_1h"] == 10.0, m["llm_calls_1h"])
check("M1b Mock 降级率 40%", m["llm_mock_rate_1h"] == 40.0, m["llm_mock_rate_1h"])
check("M1c 回退率 20%", m["llm_fallback_rate_1h"] == 20.0, m["llm_fallback_rate_1h"])
check("M1d 截断率 30%", m["llm_trunc_rate_1h"] == 30.0, m["llm_trunc_rate_1h"])
check("M1e P95 延迟取长尾（3000ms）而非均值", m["llm_p95_latency_1h"] == 3000.0, m["llm_p95_latency_1h"])
check("M1f 近 1h 成本 0.1", round(m["cost_1h_usd"], 3) == 0.1, m["cost_1h_usd"])
check("M1g 今日/昨日同期成本倍数 = 2.0", m["cost_today_vs_yday"] == 2.0, m["cost_today_vs_yday"])
check("M1h 认证失败计数（消费 P0-a 认证审计）", m["auth_failed_1h"] == 3.0, m["auth_failed_1h"])
check("M1i 被拦截计数", m["audit_blocked_1h"] == 2.0, m["audit_blocked_1h"])
check("M1j 审计链完好 = 1.0", m["audit_chain_ok"] == 1.0, m["audit_chain_ok"])

print("══ M2 两套评估器集合零重叠（结构性保障）══")
_RUN_METRICS = {"success_rate", "avg_latency", "error_count", "mock_rate"}
check("M2a run 级与全局级 metric 无交集", not (_RUN_METRICS & set(GLOBAL_METRICS)),
      sorted(_RUN_METRICS & set(GLOBAL_METRICS)))

print("══ M3 周期评估：命中 → 落 open 事件 ══")
with db_conn() as c:
    c.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level) "
              "VALUES ('Mock 降级率过高','llm_mock_rate_1h','>',20,'critical')")
    c.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level) "
              "VALUES ('登录失败突增','auth_failed_1h','>',2,'warning')")
    c.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level) "
              "VALUES ('run 成功率','success_rate','<',90,'warning')")   # run 级 → 不应被周期评估
    c.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level) "
              "VALUES ('阈值未命中','llm_calls_1h','>',9999,'info')")
res = evaluate_once(cooldown_min=30, notify=False)
check("M3a 只评估全局规则（两条命中 + 一条未命中 = 3）", res["evaluated"] == 3, res)
check("M3b 触发 2 条（渐变那条未命中）", res["fired"] == 2, res)
with db_conn() as c:
    evs = c.execute("SELECT * FROM alert_events ORDER BY id").fetchall()
    none_hit = c.execute("SELECT COUNT(*) n FROM alert_events WHERE rule_name='阈值未命中'").fetchone()["n"]
    run_hit = c.execute("SELECT COUNT(*) n FROM alert_events WHERE rule_name='run 成功率'").fetchone()["n"]
check("M3c 落 2 条事件", len(evs) == 2, len(evs))
check("M3d 未命中的规则不落事件", none_hit == 0, none_hit)
check("M3e run 级规则不被周期评估器重复处理", run_hit == 0, run_hit)
check("M3f 事件标记 run_id=0 且来源为 (periodic)",
      all(e["run_id"] == 0 and e["flow_name"] == "(periodic)" for e in evs),
      [(e["run_id"], e["flow_name"]) for e in evs])
check("M3g 事件记录真实值与阈值", any(e["metric"] == "llm_mock_rate_1h" and e["actual"] == 40.0
                                 for e in evs), [(e["metric"], e["actual"]) for e in evs])

print("══ M4 冷却去重（否则每轮刷屏）══")
res2 = evaluate_once(cooldown_min=30, notify=False)
check("M4a 二次评估全部被冷却跳过", res2["fired"] == 0 and res2["skipped_cooldown"] == 2, res2)
with db_conn() as c:
    n_before = c.execute("SELECT COUNT(*) n FROM alert_events").fetchone()["n"]
evaluate_once(cooldown_min=30, notify=False)
with db_conn() as c:
    n_after = c.execute("SELECT COUNT(*) n FROM alert_events").fetchone()["n"]
check("M4b 冷却期内不新增事件", n_after == n_before, (n_before, n_after))
res3 = evaluate_once(cooldown_min=0, notify=False)
check("M4c cooldown=0（手动立即评估）可绕过冷却", res3["fired"] == 2, res3)

print("══ M5 审计链破损可被告警 ══")
with db_conn() as c:
    c.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level) "
              "VALUES ('审计链破损','audit_chain_ok','<',1,'critical')")
    last = c.execute("SELECT id, detail FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
    c.execute("UPDATE audit_logs SET detail=? WHERE id=?", ("被篡改!!!", last["id"]))   # 破坏链
res4 = evaluate_once(cooldown_min=0, notify=False)
check("M5a 篡改后 audit_chain_ok = 0", res4["metrics"]["audit_chain_ok"] == 0.0, res4["metrics"])
with db_conn() as c:
    chain_alert = c.execute("SELECT COUNT(*) n FROM alert_events WHERE rule_name='审计链破损'").fetchone()["n"]
check("M5b 破损可被规则捕获并落事件", chain_alert >= 1, chain_alert)

print("══ M6 webhook 通知失败不阻断 ══")
try:
    ok = notify_alert_webhook({"id": 1, "name": "x", "notify_url": "http://127.0.0.1:1/nope"},
                              "llm_mock_rate_1h", 40.0, 20.0, ">")
    check("M6a 不可达 webhook 返回 False 而非抛异常", ok is False, ok)
except Exception as e:
    check("M6a 不可达 webhook 返回 False 而非抛异常", False, str(e)[:160])

os.remove(_DB)
print(f"\n══ {PASS} PASS / {len(FAIL)} FAIL ══")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
sys.exit(1 if FAIL else 0)
