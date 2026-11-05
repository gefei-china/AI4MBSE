# -*- coding: utf-8 -*-
"""反馈类型一致性 + 实际链路验证（2026-10-29）。

═══════════════════════════════════════════════════════════════════════
背景
═══════════════════════════════════════════════════════════════════════
「反馈」一个语义曾散在**四份互不相同**的取值域里（前端 / schema / badcase /
online_eval），结果：生产库 `feedback` 表 3 条全是 approve，**显式负反馈 0 条**，
`online_eval_queue` 0 行 ⇒ 进化系统没有方向信号。

本次修复的验证分三层：
  A. **跨语言一致性**：前端 JS 的取值集合 == 后端单一真源（core/feedback_types.py）
  B. **无字面量漂移**：后端消费端不得再各写一份词表
  C. **实际链路**（真 HTTP 端到端）：点踩 → 落库 → badcase 建档 → online_eval 采到
     并额外验证两条**语义隔离**铁律：
       ① 「取消评价」不得留痕（否则"先踩后撤"被记成两次差评，信号被放大）；
       ② 「评审拒绝(reject)」不得被当成「用户差评」进 badcase（否则进化去修没坏的东西）。
"""
import json
import os
import re
import sqlite3

import pytest

from core.feedback_types import (
    NEGATIVE_QUALITY,
    NEGATIVE_REVIEW_TYPES,
    QUALITY_TYPES,
    REVIEW_TYPES,
    public_catalog,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_MD = os.path.join(ROOT, "static", "js", "mods", "05-markdown.js")


# ══════════════════════════════════════════════════════════════════════
# A. 跨语言一致性
# ══════════════════════════════════════════════════════════════════════
def test_frontend_quality_types_match_backend():
    """前端兜底常量必须与后端单一真源**完全相等**（不是包含关系）。"""
    js = open(JS_MD, encoding="utf-8").read()
    m = re.search(r"FB_QUALITY_TYPES_DEFAULT\s*=\s*\[([^\]]*)\]", js)
    assert m, "前端找不到 FB_QUALITY_TYPES_DEFAULT（取值集合被硬编码/改名了）"
    front = sorted(x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip())
    assert front == sorted(QUALITY_TYPES), (
        f"前端质量取值 {front} != 后端真源 {sorted(QUALITY_TYPES)}")


def test_frontend_review_labels_are_subset_of_backend():
    """前端 fbMap 的评审标签必须是后端 REVIEW_TYPES 的子集。"""
    js = open(JS_MD, encoding="utf-8").read()
    m = re.search(r"fbMap\s*=\s*\{([^}]*)\}", js)
    assert m, "前端找不到 fbMap"
    keys = re.findall(r"(\w+)\s*:", m.group(1))
    assert set(keys) <= set(REVIEW_TYPES), (
        f"前端 fbMap 含后端未登记的评审取值：{set(keys) - set(REVIEW_TYPES)}")


def test_public_catalog_matches_truth():
    cat = public_catalog()
    assert cat["quality_types"] == list(QUALITY_TYPES)
    assert cat["review_types"] == list(REVIEW_TYPES)
    assert cat["negative_quality"] == list(NEGATIVE_QUALITY)
    assert cat["negative_review_types"] == list(NEGATIVE_REVIEW_TYPES)


def test_quality_and_review_are_disjoint():
    """★ 语义隔离铁律：质量信号与评审动作**不得重叠**。

    重叠 ⇒ "评审拒绝"会被当成"用户差评" ⇒ 进化系统去修没坏的东西（misevolution）。
    """
    assert not (set(QUALITY_TYPES) & set(REVIEW_TYPES))


# ══════════════════════════════════════════════════════════════════════
# B. 后端消费端无字面量漂移
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("relpath", ["core/badcase.py", "core/online_eval.py"])
def test_consumers_reference_single_source(relpath):
    """消费端必须 import 单一真源，且不得内联负反馈词表字面量。"""
    src = open(os.path.join(ROOT, relpath), encoding="utf-8").read()
    assert "feedback_types" in src, f"{relpath} 未引用 core.feedback_types"
    # 内联的旧字面量（原文：WHERE feedback_type IN ('modify', 'wrong', 'corrected')）
    assert not re.search(r"IN\s*\(\s*'modify'\s*,\s*'wrong'", src), (
        f"{relpath} 仍内联负反馈词表字面量（与单一真源会漂移）")


# ══════════════════════════════════════════════════════════════════════
# C. 实际链路（真 HTTP 端到端）
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture()
def conv_and_msg(client, test_db_path):
    """建一个会话 + 一条助手消息，返回 (conv_id, msg_id)。

    会话走真实 API（POST /api/conversations）；**助手消息直接写测试库** ——
    消息本来由 chat 产生（依赖 LLM），而本测试要验的是
    「反馈信号 → 落库 → badcase 建档 → online_eval 采到」这条链路，
    不该被 chat/LLM 的可用性带偏（否则验的是 LLM 不是链路）。
    """
    r = client.post("/api/conversations", json={"title": "反馈链路验证"},
                    headers={"X-User-Id": "tester"})
    assert r.status_code in (200, 201), r.text
    cid = r.json().get("id") or r.json().get("conversation_id")
    assert cid, r.text

    c = sqlite3.connect(test_db_path)
    try:
        cur = c.execute(
            "INSERT INTO messages (conversation_id, role, content, msg_type, created_at) "
            "VALUES (?, 'assistant', ?, 'text', datetime('now','localtime'))",
            (int(cid), "这是一条用于验证反馈链路的回答"))
        c.commit()
        mid = int(cur.lastrowid)
    finally:
        c.close()
    return int(cid), mid


def _rows(test_db_path, sql, args=()):
    c = sqlite3.connect(test_db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def test_quality_endpoint_rejects_illegal_value(client, conv_and_msg):
    """★ fail-closed：未知取值必须 400 拒绝（原先零校验，脏值可落库）。"""
    _, mid = conv_and_msg
    r = client.post(f"/api/messages/{mid}/quality", json={"quality": "garbage"},
                    headers={"X-User-Id": "tester"})
    assert r.status_code == 400, f"非法取值未被拒绝：{r.status_code} {r.text}"


def test_thumbs_down_persists_and_is_sampled(client, conv_and_msg, test_db_path):
    """★ 主链路：点踩 → messages.quality + feedback 留痕 → badcase 建档 → online_eval 采到。"""
    cid, mid = conv_and_msg

    r = client.post(f"/api/messages/{mid}/quality", json={"quality": "down"},
                    headers={"X-User-Id": "tester"})
    assert r.status_code == 200, r.text
    assert r.json().get("quality") == "down"

    # ① messages.quality 落库
    rows = _rows(test_db_path, "SELECT quality FROM messages WHERE id=?", (mid,))
    assert rows and rows[0]["quality"] == "down", "messages.quality 未落库"

    # ② feedback 留痕（signal_kind='quality'，语义可区分）
    rows = _rows(test_db_path,
                 "SELECT feedback_type, quality, signal_kind FROM feedback WHERE message_id=?",
                 (mid,))
    assert len(rows) == 1, f"feedback 留痕条数异常：{rows}"
    assert rows[0]["quality"] == "down" and rows[0]["signal_kind"] == "quality", rows[0]

    # ③ badcase 能建档（显式负反馈真正进入判据面）
    from core.badcase import collect_bad_cases
    from database import get_db
    conn = get_db()
    try:
        res = collect_bad_cases(conn, apply=False, kinds=["user_negative_feedback"])
    finally:
        conn.close()
    cands = res.get("candidates") or []
    hit = [c for c in cands if str(mid) in str(c.get("subject") or "")]
    assert hit, f"badcase 未捕获该负反馈：candidates={cands[:3]}"

    # ④ online_eval 能采到（采样队列不再是空的）
    from core.online_eval import sample_candidates
    conn = get_db()
    try:
        res2 = sample_candidates(conn, apply=False)
    finally:
        conn.close()
    assert res2.get("failures") == [], f"采样器报失败：{res2.get('failures')}"
    cands2 = res2.get("candidates") or []
    hit2 = [c for c in cands2 if int(c.get("message_id") or 0) == mid]
    assert hit2, f"online_eval 未采到该负反馈：n={len(cands2)}"


def test_clearing_quality_leaves_no_trace(client, conv_and_msg, test_db_path):
    """★ 铁律：取消评价**不得留痕** —— 否则"先踩后撤"会被采样成两次差评。"""
    cid, mid = conv_and_msg
    client.post(f"/api/messages/{mid}/quality", json={"quality": "down"},
                headers={"X-User-Id": "tester"})
    n_before = len(_rows(test_db_path, "SELECT id FROM feedback WHERE message_id=?", (mid,)))

    r = client.post(f"/api/messages/{mid}/quality", json={"quality": ""},
                    headers={"X-User-Id": "tester"})
    assert r.status_code == 200, r.text
    assert r.json().get("quality") == ""

    rows = _rows(test_db_path, "SELECT quality FROM messages WHERE id=?", (mid,))
    assert rows and rows[0]["quality"] == "", "messages.quality 未被清除"

    n_after = len(_rows(test_db_path, "SELECT id FROM feedback WHERE message_id=?", (mid,)))
    assert n_after == n_before, (
        f"★ 取消评价留下了痕迹（{n_before} → {n_after}），会被采样成额外差评")


def test_review_reject_does_not_become_badcase(client, conv_and_msg, test_db_path):
    """★ 语义隔离：评审拒绝(reject) 是流程裁决，**不得**被当成用户差评。"""
    cid, mid = conv_and_msg
    r = client.post(f"/api/messages/{mid}/feedback", json={"type": "reject"},
                    headers={"X-User-Id": "tester"})
    assert r.status_code == 200, r.text

    rows = _rows(test_db_path, "SELECT feedback_type, quality FROM feedback WHERE message_id=?",
                 (mid,))
    assert rows and rows[0]["feedback_type"] == "reject"
    assert not rows[0]["quality"], "评审行不应带质量信号"

    from core.badcase import collect_bad_cases
    from database import get_db
    conn = get_db()
    try:
        res = collect_bad_cases(conn, apply=False, kinds=["user_negative_feedback"])
    finally:
        conn.close()
    cands = res.get("candidates") or []
    hit = [c for c in cands if str(mid) in str(c.get("subject") or "")]
    assert not hit, f"★ 评审拒绝被误当用户差评建档：{hit}"


def test_feedback_types_endpoint_serves_catalog(client):
    """取值目录接口：前端据此不硬编码。"""
    r = client.get("/api/feedback-types")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quality_types"] == list(QUALITY_TYPES)
    assert body["review_types"] == list(REVIEW_TYPES)
