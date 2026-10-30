# -*- coding: utf-8 -*-
"""自进化三引擎的作业接线（2026-10-29）。

背景（生产库实证）：`config_proposals` 0 行、`online_eval_queue` 0 行、
`skills.source` 无一条 'ai_improve' ⇒ **进化闭环"感知层完备、决策/行动层空转"**。
典型断链：SkillImprover 候选条件是"失败次数 ≥ improve_min_fail(3)"，而生产库里
Requirement_Analysis_Pre-check 失败 17 次、智源平台管理失败 16 次 ——
**素材一直在，引擎从未被叫醒**（无触发点）。

本测试锁定三件事：
  A. 三个引擎**注册进作业队列**（有触发点）且分组正确（前端能动态渲染）；
  B. 三个 handler **能跑通**（不抛、返回结构完整、源级失败可见）；
  C. 「只提议、不自动应用」纪律：handler **不得**写生产配置、不得发布技能。
"""
import os
import sqlite3

import pytest

from core import job_handlers as jh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVOLUTION_KINDS = ("skill_improve", "config_proposal", "online_eval_sample")


def _job(job_id: int = 1) -> dict:
    """构造最小 job dict（_report / LeaseKeeper 的失败都被容错，不依赖真实作业行）。"""
    return {"id": job_id, "kind": "test", "status": "running"}


def _count(db, sql) -> int:
    c = sqlite3.connect(db)
    try:
        return int(c.execute(sql).fetchone()[0] or 0)
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# A. 注册与分组
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("kind", EVOLUTION_KINDS)
def test_evolution_kind_registered(kind):
    assert kind in jh.KINDS, f"{kind} 未注册进作业队列 ⇒ 无触发点，永远不会跑"
    assert callable(jh.KINDS[kind])


def test_describe_exposes_group_for_frontend():
    """describe() 必须给每个 kind group ⇒ 前端可按 group 动态渲染（不再硬编码）。"""
    d = jh.describe()
    assert set(jh.KINDS) <= set(d), "有 kind 未在 describe 中登记"
    missing = [k for k, v in d.items() if not v.get("group")]
    assert not missing, f"缺 group 的 kind：{missing}"
    for k in EVOLUTION_KINDS:
        assert d[k]["group"] == "evolution", f"{k} 分组错误：{d[k].get('group')}"


def test_frontend_renders_evolution_group_dynamically():
    """前端必须按 group='evolution' 动态取，而不是再写一份硬编码列表。"""
    js = open(os.path.join(ROOT, "static", "js", "mods", "48-eval.js"),
              encoding="utf-8").read()
    assert "group === 'evolution'" in js, "前端未按 group 动态渲染自进化作业"
    assert "/api/jobs/kinds" in js, "前端未从后端拉取 kind 列表"


# ══════════════════════════════════════════════════════════════════════
# B. handler 能跑通
# ══════════════════════════════════════════════════════════════════════
def test_handler_config_proposal_runs(test_db_path):
    """提案生成 handler：跑通且返回结构完整。"""
    res = jh.handler_config_proposal({}, _job())
    assert isinstance(res, dict)
    assert "created" in res and "skipped" in res
    assert isinstance(res.get("patterns_summary"), dict)


def test_handler_config_proposal_actually_persists(test_db_path, monkeypatch):
    """★ 提案 handler 必须**真落库** —— 不能只看返回值说 created。

    实测踩到的坑（2026-10-29，首次真跑即复现）：
      `generate()` 经 `ProposalRepo.insert_proposal` → `BaseRepo.execute`，
      该层**只执行不提交**（事务边界由调用方定）。Web 路径有 db_session 兜底提交
      所以 API 上"一直正常"；但 handler 自开连接、finally close() ⇒
      **未提交事务回滚** ⇒ 返回值说 `created: 5`，`SELECT COUNT(*)` 恒为 0。

    ⚠️ 本测试**必须注入确定性命中的模式**，不能用测试库的真实数据：
      测试库没有历史失败模式 ⇒ created=0 ⇒ 软断言被跳过 ⇒ 测试变空转
      （实测：去掉 commit 后本测试仍 passed，即是此因）。
      断言锚定**持久化事实**（库里有行），而不是函数自报。
    """
    from core import attribution as _attr

    # ★ 干净起点：指纹去重会让残留数据把 created 压成 0，
    #   那样"没提交"与"已存在"就无法区分（实测踩到：变异后报错理由变成了
    #   "created: 0, skipped: 2" —— 是残留而非缺陷）。故先清空。
    c = sqlite3.connect(test_db_path)
    try:
        c.execute("DELETE FROM config_proposals")
        c.commit()
    finally:
        c.close()

    # 构造一个必定命中 llm_transient_retry 的模式（conn 类失败，count 远超阈值）
    fake_pattern = {
        "source": "llm_call", "intent": "chat", "kind": "conn",
        "actor": "fake-model", "count": 9, "conversations": 3,
        "first_seen": "2026-10-01 00:00:00", "last_seen": "2026-10-28 00:00:00",
        "sample_steps": [],
    }
    monkeypatch.setattr(_attr, "collect_patterns",
                        lambda conn, settings=None: {
                            "enabled": True, "sessions_scanned": 1,
                            "sessions_with_first_bad": 1,
                            "first_bad_patterns": [fake_pattern],
                            "kind_coverage": {}, "unattributed_sessions": {"count": 0},
                        })

    res = jh.handler_config_proposal({}, _job())
    created = int(res.get("created") or 0)
    assert created > 0, f"★ 注入命中模式后仍无提案（规则未生效）：{res}"
    rows = _count(test_db_path, "SELECT COUNT(*) FROM config_proposals")
    assert rows > 0, (
        f"★ handler 回报 created={created}，但库里 0 行 —— 事务未提交（已回滚）")
    # 不得出现非 pending（自动化不可越过人工裁决）
    bad = _count(test_db_path,
                 "SELECT COUNT(*) FROM config_proposals WHERE status!='pending'")
    assert bad == 0, "★ 出现了非 pending 提案（自动化越过了人工裁决）"


def test_handler_online_eval_sample_runs(test_db_path):
    """采样 handler：跑通，且**源级失败必须可见**（failures 为空表示采样器健康）。"""
    res = jh.handler_online_eval_sample({}, _job())
    assert isinstance(res, dict)
    assert "inserted" in res and "failures" in res
    assert res["failures"] == [], f"采样源失败未可见：{res['failures']}"


def test_handler_online_eval_sample_propagates_failures(test_db_path, monkeypatch):
    """★ 源级失败必须**透传到 handler 返回值** —— 不能被吞成空列表。

    为什么单独一条：上面那条只断言 `failures == []`，它只能证明"采样器当前健康"，
    **证明不了"失败不会被吞"**（把 failures 硬置空也能通过——实测变异测试抓到过）。
    本测试构造一个必定报失败的采样器，断言 handler 把它原样带回。
    静默跳过会让"采样器坏了"长得像"没有负反馈"，这是本仓已付过学费的坑。
    """
    from core import online_eval as _oe
    monkeypatch.setattr(_oe, "sample_candidates",
                        lambda conn, apply=False, settings=None: {
                            "ok": True, "inserted": 0, "candidates": [],
                            "plan": {}, "failures": ["neg_feedback(feedback): boom"]})
    res = jh.handler_online_eval_sample({}, _job())
    assert res["failures"] == ["neg_feedback(feedback): boom"], \
        f"★ 源级失败被吞掉：{res.get('failures')}"


def test_handler_skill_improve_runs(test_db_path, monkeypatch):
    """技能改进 handler：跑通（LLM mock 下可能不产出草稿，但不能抛）。"""
    import skill_improve as si
    monkeypatch.setattr(si.SkillImprover, "run_all",
                        staticmethod(lambda conn, provider_id=None, limit=5:
                                     {"proposed": 0, "names": [], "skipped": False}))
    res = jh.handler_skill_improve({}, _job())
    assert isinstance(res, dict)
    assert "candidates" in res and "proposed" in res


def test_handler_skill_improve_reads_correct_field(test_db_path, monkeypatch):
    """★ handler 必须读 `run_all` 的真实字段名 `proposed` —— 读错字段会恒报 0。

    实测踩到的坑（2026-10-29）：`SkillImprover.run_all()` 返回
      `{proposed, names, skipped}`，而初版 handler 按 `revised` 读 ⇒
      明明产出了草稿，作业结果却报 0（"看起来没干活"）。
    本测试注入一个 proposed=3 的返回值，断言 handler **如实回报 3** ——
    字段名写错就会被抓住（读不到 → 0 ≠ 3）。
    """
    import skill_improve as si
    monkeypatch.setattr(si.SkillImprover, "run_all",
                        staticmethod(lambda conn, provider_id=None, limit=5:
                                     {"proposed": 3,
                                      "names": ["a@v2", "b@v3", "c@v4"],
                                      "skipped": False}))
    res = jh.handler_skill_improve({}, _job())
    assert int(res.get("proposed") or 0) == 3, (
        f"★ handler 未如实回报 proposed（字段名读错？）：{res}")
    assert len(res.get("names") or []) == 3, res


# ══════════════════════════════════════════════════════════════════════
# C. 「只提议、不自动应用」纪律
# ══════════════════════════════════════════════════════════════════════
def test_config_proposal_handler_does_not_write_config(test_db_path):
    """★ 提案 handler 不得写生产配置 —— 只写 pending 提案队列，等人裁决。"""
    from core import config as _cfg
    before = _cfg.get("llm", "retry_times", None)
    jh.handler_config_proposal({}, _job())
    after = _cfg.get("llm", "retry_times", None)
    assert before == after, "★ 提案 handler 改了生产配置（违反只提议不自动应用）"

    c = sqlite3.connect(test_db_path)
    c.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT status FROM config_proposals").fetchall()]
    finally:
        c.close()
    # 若有产出，必须全是 pending（不得出现 applied）
    assert all(r["status"] == "pending" for r in rows), \
        f"提案状态异常：{set(r['status'] for r in rows)}"


def test_skill_improve_handler_only_produces_draft(test_db_path, monkeypatch):
    """★ 技能改进只产 draft_revision 草稿，不得发布（status='published'）。"""
    import skill_improve as si
    monkeypatch.setattr(si.SkillImprover, "run_all",
                        staticmethod(lambda conn, provider_id=None, limit=5:
                                     {"proposed": 0, "names": [], "skipped": False}))
    jh.handler_skill_improve({}, _job())
    c = sqlite3.connect(test_db_path)
    c.row_factory = sqlite3.Row
    try:
        bad = [dict(r) for r in c.execute(
            "SELECT name, status, source FROM skills WHERE source='ai_improve' "
            "AND status NOT IN ('draft_revision')").fetchall()]
    finally:
        c.close()
    assert not bad, f"★ 技能改进产出了非草稿行：{bad}"
