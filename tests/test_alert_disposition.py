# -*- coding: utf-8 -*-
"""告警可处置化：折叠 / 状态流转 / 自动建档（2026-10-29）。

背景（生产库实证）：328 条告警 status **全是 'open'**，一条没被处理；
同三条规则各累积 81 次 ⇒ 告警疲劳。本模块覆盖三条能力：

  A. **折叠**：同一规则的未终结告警累加 hit_count，而非每次 INSERT 新行
  B. **状态流转**：open → acked → triaged → fixed → verified（只前进不回退）
  C. **自动建档**：未处置的 high/critical 告警进入 badcase 主题 ⇒ 告警变待办

全部用**测试库**（conftest 隔离），不读写生产 mbse.db。
"""
import os
import sqlite3

import pytest

from core.alert_state import (
    ALERT_STATUSES,
    TERMINAL_STATUSES,
    can_transition,
    is_valid_status,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rows(test_db_path, sql, args=()):
    c = sqlite3.connect(test_db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _exec(test_db_path, sql, args=()):
    c = sqlite3.connect(test_db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


@pytest.fixture()
def rule(test_db_path):
    """建一条必定触发的告警规则（阈值 0，实际值给 999）。"""
    _exec(test_db_path,
          "DELETE FROM alert_rules WHERE name='pytest 折叠规则'")
    _exec(test_db_path,
          "INSERT INTO alert_rules (name, metric, operator, threshold, level, status) "
          "VALUES ('pytest 折叠规则','badcase_open_count','>',0,'warning','active')")
    rows = _rows(test_db_path, "SELECT id FROM alert_rules WHERE name='pytest 折叠规则'")
    rid = rows[0]["id"]
    yield rid
    _exec(test_db_path, "DELETE FROM alert_rules WHERE id=?", (rid,))
    _exec(test_db_path, "DELETE FROM alert_events WHERE rule_id=?", (rid,))


# ══════════════════════════════════════════════════════════════════════
# A. 折叠
# ══════════════════════════════════════════════════════════════════════
def test_repeat_hit_folds_into_one_row(test_db_path, rule, monkeypatch):
    """★ 同一规则反复命中 ⇒ 只有 1 行，hit_count 累加（不再刷屏）。"""
    import core.alert_evaluator as ae

    monkeypatch.setattr(ae, "collect_global_metrics",
                        lambda c: {"badcase_open_count": 999.0})
    from database import get_db
    conn = get_db()
    try:
        ae.evaluate_once(conn, cooldown_min=0, notify=False)
        ae.evaluate_once(conn, cooldown_min=0, notify=False)
        ae.evaluate_once(conn, cooldown_min=0, notify=False)
    finally:
        conn.close()

    rows = _rows(test_db_path,
                 "SELECT id, hit_count FROM alert_events WHERE rule_id=?", (rule,))
    assert len(rows) == 1, f"★ 未折叠：产生 {len(rows)} 行（应为 1）"
    assert int(rows[0]["hit_count"] or 0) == 3, (
        f"命中计数错误：{rows[0]['hit_count']}（期望 3）")


def test_terminal_alert_is_not_folded(test_db_path, rule, monkeypatch):
    """★ 已终结(fixed/verified)的告警不参与折叠 ⇒ 问题复发开**新事件**。"""
    import core.alert_evaluator as ae

    monkeypatch.setattr(ae, "collect_global_metrics",
                        lambda c: {"badcase_open_count": 999.0})
    from database import get_db
    conn = get_db()
    try:
        ae.evaluate_once(conn, cooldown_min=0, notify=False)
    finally:
        conn.close()
    rows = _rows(test_db_path, "SELECT id FROM alert_events WHERE rule_id=?", (rule,))
    aid = rows[0]["id"]
    _exec(test_db_path, "UPDATE alert_events SET status='verified' WHERE id=?", (aid,))

    conn = get_db()
    try:
        ae.evaluate_once(conn, cooldown_min=0, notify=False)
    finally:
        conn.close()
    rows2 = _rows(test_db_path, "SELECT id FROM alert_events WHERE rule_id=?", (rule,))
    assert len(rows2) == 2, "★ 已终结的告警被继续折叠 ⇒ 复发问题不会开新事件"


# ══════════════════════════════════════════════════════════════════════
# B. 状态流转
# ══════════════════════════════════════════════════════════════════════
def test_state_machine_allows_forward_only():
    assert can_transition("open", "acked")
    assert can_transition("open", "triaged")          # 允许跳过中间态
    assert can_transition("acked", "triaged")
    assert can_transition("triaged", "fixed")
    assert can_transition("fixed", "verified")
    # 回退必须禁止
    assert not can_transition("verified", "open")
    assert not can_transition("fixed", "acked")
    assert not can_transition("triaged", "open")
    # 非法状态
    assert not can_transition("open", "banana")
    assert not can_transition("banana", "open")
    assert not can_transition("verified", "verified")


def test_terminal_statuses_are_subset():
    assert set(TERMINAL_STATUSES) <= set(ALERT_STATUSES)
    assert "fixed" in TERMINAL_STATUSES and "verified" in TERMINAL_STATUSES


def test_status_api_flows_forward_and_rejects_backward(client, test_db_path, rule):
    """★ API 层：推进合法、回退被拒（400）、非法状态被拒（400）。"""
    _exec(test_db_path,
          "INSERT INTO alert_events (rule_id, rule_name, run_id, flow_name, metric, actual, "
          "threshold, operator, level, status, hit_count) "
          "VALUES (?,?,0,'(pytest)','badcase_open_count',999,0,'>','warning','open',1)",
          (rule, 'pytest 折叠规则'))
    rows = _rows(test_db_path, "SELECT id FROM alert_events WHERE rule_id=?", (rule,))
    aid = rows[0]["id"]

    r = client.post(f"/api/monitor/alerts/{aid}/status",
                    json={"status": "triaged", "note": "LLM 配额耗尽"},
                    headers={"X-User-Id": "tester"})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "triaged"

    row = _rows(test_db_path,
                "SELECT status, handled_by, note FROM alert_events WHERE id=?", (aid,))[0]
    assert row["status"] == "triaged"
    assert row["handled_by"], "处置人未留痕"
    assert row["note"] == "LLM 配额耗尽", "归因备注未留痕"

    # 回退必须 400
    r2 = client.post(f"/api/monitor/alerts/{aid}/status", json={"status": "open"},
                     headers={"X-User-Id": "tester"})
    assert r2.status_code == 400, f"★ 回退未被拒：{r2.status_code} {r2.text}"

    # 非法状态必须 400
    r3 = client.post(f"/api/monitor/alerts/{aid}/status", json={"status": "banana"},
                     headers={"X-User-Id": "tester"})
    assert r3.status_code == 400, f"★ 非法状态未被拒：{r3.status_code}"


def test_summary_endpoint_reports_unhandled(client, test_db_path, rule):
    """汇总接口要能区分「事件总数」与「未处置数」。"""
    _exec(test_db_path,
          "INSERT INTO alert_events (rule_id, rule_name, run_id, flow_name, metric, actual, "
          "threshold, operator, level, status, hit_count) "
          "VALUES (?,?,0,'(pytest)','badcase_open_count',999,0,'>','warning','open',5)",
          (rule, 'pytest 折叠规则'))
    r = client.get("/api/monitor/alerts/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["by_status"]["open"] >= 1
    assert body["unhandled"] >= 1
    assert body["hit_total"] >= 5, "折叠计数未汇总"


# ══════════════════════════════════════════════════════════════════════
# C. 自动建档
# ══════════════════════════════════════════════════════════════════════
def test_unhandled_alert_becomes_badcase(client, test_db_path, rule):
    """★ 未处置的 high/critical 告警 ⇒ 进入 badcase 主题（告警变待办）。"""
    _exec(test_db_path,
          "INSERT INTO alert_events (rule_id, rule_name, run_id, flow_name, metric, actual, "
          "threshold, operator, level, status, hit_count) "
          "VALUES (?,?,0,'(pytest)','badcase_open_count',999,0,'>','critical','open',1)",
          (rule, 'pytest 折叠规则'))

    from core.badcase import collect_bad_cases
    from database import get_db
    conn = get_db()
    try:
        res = collect_bad_cases(conn, apply=False, kinds=["alert_unhandled"])
    finally:
        conn.close()
    cands = res.get("candidates") or []
    hit = [c for c in cands if str(rule) in str(c.get("subject") or "")]
    assert hit, f"★ 未处置告警未进入 badcase：candidates={len(cands)} 条"


def test_handled_alert_not_in_badcase(client, test_db_path, rule):
    """★ 已终结告警不再算坏例（否则看板噪声不降）。"""
    _exec(test_db_path,
          "INSERT INTO alert_events (rule_id, rule_name, run_id, flow_name, metric, actual, "
          "threshold, operator, level, status, hit_count) "
          "VALUES (?,?,0,'(pytest)','badcase_open_count',999,0,'>','critical','verified',1)",
          (rule, 'pytest 折叠规则'))

    from core.badcase import collect_bad_cases
    from database import get_db
    conn = get_db()
    try:
        res = collect_bad_cases(conn, apply=False, kinds=["alert_unhandled"])
    finally:
        conn.close()
    cands = res.get("candidates") or []
    hit = [c for c in cands if str(rule) in str(c.get("subject") or "")]
    assert not hit, f"★ 已处置告警仍被当成坏例：{hit}"


def test_frontend_alert_status_labels_cover_all_states():
    """前端状态标签必须覆盖后端全部五态（否则新状态在前端不可见）。"""
    js = open(os.path.join(ROOT, "static", "js", "mods", "31-admin.js"),
              encoding="utf-8").read()
    assert "ALERT_STATUS_LABEL" in js, "前端缺少告警状态标签表"
    for st in ALERT_STATUSES:
        assert f"{st}:" in js, f"前端状态标签缺少 {st}"


def test_badcase_detector_failure_is_reported_not_swallowed(test_db_path):
    """★ 判据面 SQL 出错必须进 failures —— 绝不能静默消失。

    为什么这条重要：原实现 `except: continue` ⇒ 某个判据面瘫了，坏例数凭空变少，
    看板/周报会把它当成"最近没有坏例、变好了"。**静默失败比失败本身更危险**。
    （同仓 core/online_eval.py 早有此纪律，badcase 此前未统一。）
    """
    from core import badcase as bc
    from database import get_db

    orig = bc._detectors
    bc._detectors = lambda p: [
        bc.DetectorSpec("pytest_broken", "故意损坏的判据", "high",
                        "SELECT id FROM table_that_does_not_exist_xyz WHERE 1=1")]
    conn = get_db()
    try:
        res = bc.collect_bad_cases(conn, apply=False)
    finally:
        conn.close()
        bc._detectors = orig

    fails = res.get("failures") or []
    assert fails, "★ 判据面失败被静默吞掉（failures 为空）"
    assert any("pytest_broken" in f for f in fails), fails
