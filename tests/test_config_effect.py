# -*- coding: utf-8 -*-
"""配置变更「改后效果」判定（2026-10-29）。

补的是 P1-4 之后的空档：
    改前：沙箱影子配置跑门禁 → 差分判回归（已有）
    改后：线上指标有没有真的变好？（**此前无**）

本模块验证 `core/config_effect.py` 的判定逻辑，重点是**四类易错点**：
  A. **方向敏感**：direction=low（越低越好）与 high（越高越好）下，
     同一"数值下降"的结论相反 —— 写反了会把劣化判成改善（最危险的假绿）。
  B. **样本不足不下结论**：宁可 inconclusive，也不拿 1 个样本当统计。
  C. **无基线不硬判**：before 均值为 0 时算不出相对变化 ⇒ no_baseline，不补零。
  D. **不写库除非 apply**：dry-run 与 apply 的边界（与全仓口径一致）。
"""
import sqlite3

import pytest

from core import config_effect as CE

#: 测试用固定时间，避免"当前时间"漂移影响窗口断言
APPLIED_AT = "2026-10-15 12:00:00"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE config_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT, key TEXT,
        old_value TEXT, new_value TEXT, source TEXT, source_id INTEGER,
        applied_by TEXT, applied_at TEXT, note TEXT, applied INTEGER DEFAULT 1,
        effect_verdict TEXT DEFAULT '', effect_detail TEXT DEFAULT '',
        effect_checked_at TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE eval_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER,
        eval_kind TEXT, metric_key TEXT, value REAL, direction TEXT,
        gate_key TEXT, threshold REAL, status TEXT, created_at TEXT)""")
    c.execute(
        "INSERT INTO config_history (section, key, old_value, new_value, applied_at, applied) "
        "VALUES ('llm','retry_times','2','3',?,1)", (APPLIED_AT,))
    yield c
    c.close()


def _add_metrics(c, key, values, direction, ts_list):
    """按给定时间戳批量插入指标样本。"""
    for v, ts in zip(values, ts_list):
        c.execute("INSERT INTO eval_metrics (metric_key, value, direction, created_at) "
                  "VALUES (?,?,?,?)", (key, v, direction, ts))
    c.commit()


def _ts(days_before_applied: float) -> str:
    """相对 APPLIED_AT 偏移若干天（负=改前）。"""
    import time
    t = time.mktime(time.strptime(APPLIED_AT, "%Y-%m-%d %H:%M:%S"))
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t + days_before_applied * 86400))


# ══════════════════════════════════════════════════════════════════════
# A. 方向敏感（最容易写反的地方）
# ══════════════════════════════════════════════════════════════════════
def test_low_direction_improved_when_value_drops(conn):
    """direction=low（越低越好）：数值下降 ⇒ 改善。"""
    _add_metrics(conn, "doc_recall_miss_rate", [40, 42, 41], "low", [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "doc_recall_miss_rate", [10, 11, 9], "low", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_effect(conn, 1)
    assert res["verdict"] == CE.VERDICT_EFFECTIVE, res
    m = res["metrics"][0]
    assert m["verdict"] == "improved", m


def test_low_direction_degraded_when_value_rises(conn):
    """direction=low：数值**上升** ⇒ 劣化（方向写反会把这里判成改善）。"""
    _add_metrics(conn, "doc_recall_miss_rate", [10, 11, 9], "low", [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "doc_recall_miss_rate", [40, 42, 41], "low", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_effect(conn, 1)
    assert res["verdict"] == CE.VERDICT_INEFFECTIVE, res
    assert res["metrics"][0]["verdict"] == "degraded", res["metrics"][0]


def test_high_direction_improved_when_value_rises(conn):
    """direction=high（越高越好）：数值上升 ⇒ 改善。"""
    _add_metrics(conn, "gen_faithful_rate", [50, 52, 51], "high", [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "gen_faithful_rate", [90, 91, 89], "high", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_effect(conn, 1)
    assert res["verdict"] == CE.VERDICT_EFFECTIVE, res
    assert res["metrics"][0]["verdict"] == "improved"


def test_high_direction_degraded_when_value_drops(conn):
    """direction=high：数值下降 ⇒ 劣化。"""
    _add_metrics(conn, "gen_faithful_rate", [90, 91, 89], "high", [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "gen_faithful_rate", [50, 52, 51], "high", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_effect(conn, 1)
    assert res["verdict"] == CE.VERDICT_INEFFECTIVE, res


# ══════════════════════════════════════════════════════════════════════
# B / C. 样本不足、无基线
# ══════════════════════════════════════════════════════════════════════
def test_insufficient_samples_is_inconclusive(conn):
    """★ 样本不足不下结论（1 个样本不是统计）。"""
    _add_metrics(conn, "doc_mrr_at_10", [0.4], "high", [_ts(-1)])
    _add_metrics(conn, "doc_mrr_at_10", [0.9], "high", [_ts(1)])
    res = CE.evaluate_effect(conn, 1)
    assert res["verdict"] == CE.VERDICT_INCONCLUSIVE, res
    assert res["metrics"][0]["verdict"] == "insufficient_samples"


def test_zero_baseline_is_no_baseline_not_zero_filled(conn):
    """★ before 均值为 0 ⇒ no_baseline，**不得补零**当分母（会算出无穷大）。"""
    _add_metrics(conn, "gen_no_citation_factual_rate", [0, 0, 0], "low",
                 [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "gen_no_citation_factual_rate", [5, 6, 7], "low",
                 [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_effect(conn, 1)
    assert res["metrics"][0]["verdict"] == "no_baseline", res["metrics"][0]
    assert res["verdict"] == CE.VERDICT_INCONCLUSIVE, res


def test_no_metrics_at_all_is_no_data(conn):
    res = CE.evaluate_effect(conn, 1)
    assert res["verdict"] == CE.VERDICT_NO_DATA, res


# ══════════════════════════════════════════════════════════════════════
# 综合判定
# ══════════════════════════════════════════════════════════════════════
def test_mixed_when_both_improve_and_degrade(conn):
    """★ 有改善也有劣化 ⇒ mixed（不替人做权衡）。"""
    _add_metrics(conn, "doc_recall_miss_rate", [40, 41, 42], "low",
                 [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "doc_recall_miss_rate", [10, 11, 12], "low", [_ts(1), _ts(2), _ts(3)])
    _add_metrics(conn, "gen_faithful_rate", [90, 91, 92], "high",
                 [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "gen_faithful_rate", [50, 51, 52], "high", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_effect(conn, 1)
    assert res["verdict"] == CE.VERDICT_MIXED, res


def test_unchanged_when_delta_within_threshold(conn):
    """变化小于阈值（默认 5%）⇒ unchanged ⇒ 整体 inconclusive，不硬报"有效"。"""
    _add_metrics(conn, "doc_recall_miss_rate", [100, 100, 100], "low",
                 [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "doc_recall_miss_rate", [99, 99, 99], "low", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_effect(conn, 1)
    assert res["metrics"][0]["verdict"] == "unchanged"
    assert res["verdict"] == CE.VERDICT_INCONCLUSIVE


# ══════════════════════════════════════════════════════════════════════
# D. dry-run / apply 边界
# ══════════════════════════════════════════════════════════════════════
def test_evaluate_pending_dry_run_does_not_write(conn):
    """★ 默认 dry-run 不写库（与全仓口径一致）。"""
    _add_metrics(conn, "doc_recall_miss_rate", [40, 41, 42], "low",
                 [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "doc_recall_miss_rate", [10, 11, 12], "low", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_pending(conn)
    assert res["dry_run"] is True and res["written"] == 0
    v = conn.execute("SELECT effect_verdict FROM config_history WHERE id=1").fetchone()[0]
    assert not v, "dry-run 竟然写了库"


def test_evaluate_pending_apply_writes_verdict(conn):
    """apply=True 才落判定列。"""
    _add_metrics(conn, "doc_recall_miss_rate", [40, 41, 42], "low",
                 [_ts(-3), _ts(-2), _ts(-1)])
    _add_metrics(conn, "doc_recall_miss_rate", [10, 11, 12], "low", [_ts(1), _ts(2), _ts(3)])
    res = CE.evaluate_pending(conn, apply=True)
    assert res["written"] == 1, res
    row = conn.execute(
        "SELECT effect_verdict, effect_detail, effect_checked_at "
        "FROM config_history WHERE id=1").fetchone()
    assert row["effect_verdict"] == CE.VERDICT_EFFECTIVE, dict(row)
    assert row["effect_checked_at"], "判定时间未落库"


def test_effect_check_api_endpoint(client):
    """/api/config/effect-check 端点可达（无历史时应干净返回）。"""
    r = client.post("/api/config/effect-check", json={}, headers={"X-User-Id": "tester"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert "counts" in body


def test_frontend_shows_effect_verdict():
    """前端必须展示效果判定（否则判定结果没人看得见）。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "static", "js", "mods", "31-admin.js"),
              encoding="utf-8").read()
    assert "loadConfigHistory" in js, "前端缺少配置台账渲染"
    assert "/api/config/history" in js, "前端未接台账端点"
    assert "/api/config/effect-check" in js, "前端未接效果判定端点"
    for v in (CE.VERDICT_EFFECTIVE, CE.VERDICT_INEFFECTIVE, CE.VERDICT_MIXED):
        assert v in js, f"前端未覆盖判定态 {v}"
