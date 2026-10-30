# -*- coding: utf-8 -*-
"""链路治理四项修复的回归锁定（2026-10-29）。

## 背景

对照 Claude Code（subagent 的 `tools` 是硬 allowlist，且**无一条能解析时报错而非空跑**）
与 OpenAI Codex（`sandbox` 技术边界 + `approval_policy` 何时问人，两层正交）后，
对上一轮链路盘点的 6 项建议逐项落地。本文件锁住其中四项，每项都对应一个
**"坏了与没事发生长得一样"**的实测缺陷：

| 项 | 缺陷 | 判据 |
|---|---|---|
| P0-1 | 未知 Agent 名静默回落「通用问答Agent」(tools=0)，任务仍标 done | resolve() 要能说"没解析到"；create_plan 要 fail-fast |
| P0-2 | 技能失败分支 `run_id` 未定义 → NameError 被吞，use_stats/feedback_notes 不写 | 构造失败调用，三个计数必须都变 |
| P0-3 | 技能白名单被当工具集上限 → 裁掉 Agent 已绑定工具（10-16 事故根因） | 默认不裁减，且开关可回退 |
| P1-4 | triggers 把清单塞进一个元素 → 逐元素子串匹配永不命中 | 校验器识别并拆分 |
"""
import json
import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════════════
# P0-1 未知 Agent 名：必须能分辨"没解析到"

def _pipeline():
    from agent import get_pipeline
    p = get_pipeline()
    p._load_db_agents()
    return p


def test_resolve_reports_true_for_active_agent():
    """已知的 active Agent → resolved=True，且不记未解析台账。"""
    p = _pipeline()
    _a, ok, hint = p.registry.resolve("design")
    assert ok is True
    assert hint == ""


def test_resolve_reports_false_for_unknown_name():
    """★ 核心：未知名字必须 resolved=False 且给出原因。

    修复前只返回定义、无法分辨 —— 调用方（编排）因此把"降级到无工具 Agent"
    当成正常执行。
    """
    p = _pipeline()
    a, ok, hint = p.registry.resolve("绝不存在的Agent名_zzz")
    assert ok is False
    assert a.name, "必须仍返回可用定义（否则调用方会 None 崩）"
    assert hint and "不存在" in hint, f"原因不可读：{hint!r}"


def test_resolve_distinguishes_disabled_agent(test_db_path):
    """「存在但非 active」与「根本不存在」必须给不同原因（处置方式完全不同）。

    ⚠️ 测试库只造了 active 的 Agent（conftest 的 seed 如此），所以这里**临时插入**
    一条 disabled 的来验证，finally 里删掉 —— 不依赖生产库内容，也不污染后续用例。
    """
    p = _pipeline()
    c = sqlite3.connect(test_db_path)
    try:
        c.execute("INSERT OR REPLACE INTO agents (name, display_name, status, agent_role, "
                  "intent_keywords) VALUES ('zzz_disabled_probe','停用探针','disabled','sub','[]')")
        c.commit()
        _a, ok, hint = p.registry.resolve("zzz_disabled_probe")
        assert ok is False
        assert ("status" in hint or "停用" in hint or "非 active" in hint), (
            f"应指出该 Agent 存在但非 active，实际：{hint!r}")
        # 对照组：完全不存在要给另一种原因
        _b, ok2, hint2 = p.registry.resolve("zzz_never_existed")
        assert ok2 is False
        assert "不存在" in hint2, f"不存在的名字应说「不存在」：{hint2!r}"
        assert hint != hint2, "两种情形的原因必须不同（否则运维无法判断该怎么处置）"
    finally:
        try:
            c.execute("DELETE FROM agents WHERE name='zzz_disabled_probe'")
            c.commit()
        except Exception:
            pass
        c.close()


def test_unresolved_ledger_records_and_counts():
    """未解析台账要累加计数（供诊断出口读）。"""
    p = _pipeline()
    p.registry._unresolved.clear()
    p.registry.resolve("坏名字A_zzz")
    p.registry.resolve("坏名字A_zzz")
    stats = {r["agent"]: r["count"] for r in p.registry.unresolved_stats()}
    assert stats.get("坏名字A_zzz", 0) >= 2, f"台账未累加：{stats}"


def test_get_still_returns_usable_definition():
    """向后兼容：get() 必须仍返回可用定义（30+ 调用点依赖）。"""
    p = _pipeline()
    a = p.registry.get("绝不存在的Agent名_zzz")
    assert a is not None and a.name


def _mkdb(tmp_path):
    """最小库：agents / agent_tasks 满足 create_plan 所需。"""
    from database import schema as _schema
    db = tmp_path / "t.db"
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, display_name TEXT,
        status TEXT DEFAULT 'active', agent_role TEXT DEFAULT 'sub')""")
    c.execute("""CREATE TABLE agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_key TEXT, title TEXT,
        agent_id TEXT, agent_name_snapshot TEXT, task_type TEXT, config TEXT DEFAULT '{}',
        deps TEXT DEFAULT '[]', status TEXT DEFAULT 'planned', assigned_by TEXT,
        seq INTEGER DEFAULT 0, context TEXT DEFAULT '', expected_output TEXT DEFAULT '',
        conversation_id INTEGER DEFAULT 0, error TEXT DEFAULT '', retry_count INTEGER DEFAULT 0,
        summary_json TEXT DEFAULT '', token_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("INSERT INTO agents (name, status) VALUES ('design','active')")
    c.execute("INSERT INTO agents (name, status) VALUES ('zhiyuan_mgmt','disabled')")
    c.commit()
    return c


def test_create_plan_fail_fast_on_unresolvable_agent(tmp_path):
    """★ create_plan(strict) 必须拒绝引用了不可解析 Agent 的计划。

    为什么与"依赖环"同等严格：放行之后执行期会静默降级成无工具 Agent，
    任务标 done 但什么都没做 —— 指标层面看不出来（实测降级任务 done 率 74%
    vs 正常 78%，几乎无差）。
    """
    from task_queue import TaskQueue
    c = _mkdb(tmp_path)
    try:
        with pytest.raises(ValueError) as ei:
            TaskQueue.create_plan(c, 9001, [
                {"key": "t1", "title": "A", "agent": "design"},
                {"key": "t2", "title": "B", "agent": "智源平台管理"},
            ], strict=True)
        msg = str(ei.value)
        assert "智源平台管理" in msg, f"错误信息未指出坏名字：{msg}"
        assert "通用问答Agent" in msg, "错误信息应说明后果（静默降级）"
    finally:
        c.close()


def test_create_plan_allows_empty_agent(tmp_path):
    """空 agent 名是合法用法（执行期 forced_intent=None 走意图识别），不得误拦。"""
    from task_queue import TaskQueue
    c = _mkdb(tmp_path)
    try:
        n = TaskQueue.create_plan(c, 9002, [{"key": "t1", "title": "A", "agent": ""}],
                                  strict=True)
        assert n == 1
    finally:
        c.close()


def test_create_plan_allows_good_agents(tmp_path):
    """全为 active 的名字必须正常落库（不误伤）。"""
    from task_queue import TaskQueue
    c = _mkdb(tmp_path)
    try:
        n = TaskQueue.create_plan(c, 9003, [{"key": "t1", "title": "A", "agent": "design"}],
                                  strict=True)
        assert n == 1
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# P0-2 技能失败统计：构造失败调用 → 三个计数都必须变

def test_skill_failure_branch_actually_writes(tmp_path, monkeypatch):
    """★ 核心：失败调用后 uses / fail / feedback_notes 三者都必须变化。

    修复前的实测结果：三个计数**全不变**（`run_id` NameError 被 `except: pass` 吞），
    而成功分支正常 +1 ⇒ 失败统计长期只写了一半。
    """
    db = tmp_path / "sk.db"
    c = sqlite3.connect(str(db))
    # ⚠️ 必须设 row_factory：`_record_skill_feedback` 内部用 `row["use_stats"]` 按名取列，
    #    默认 tuple 工厂会抛 TypeError（被单条 try 吞掉 ⇒ 表现为"没写入"）。
    #    第一版漏了这行，红在"uses 未+1"，看起来又像代码没修好。
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, status TEXT DEFAULT 'published',
        enabled INTEGER DEFAULT 1, use_stats TEXT DEFAULT '{}',
        feedback_notes TEXT DEFAULT '[]', updated_at TEXT DEFAULT '')""")
    c.execute("INSERT INTO skills (name, use_stats, feedback_notes) VALUES (?,?,?)",
              ("测试技能", json.dumps({"uses": 5, "success": 5, "fail": 0}), "[]"))
    c.commit()

    p = _pipeline()
    p._last_skill_hits = ["测试技能"]

    # ⚠️ `db_conn` 在 `_record_skill_feedback` 里是**函数内局部导入**
    #    （`from database import db_conn` 在函数体第一行），所以必须 patch
    #    `database.db_conn` 本身 —— patch `memory.db_conn` 无效（模块里没有这个属性）。
    #    第一版就是这么写错的，测试红在"uses 未+1"，看起来像代码没修好，实际是 mock 打偏。
    import database as _dbmod

    class _Ctx:
        def __enter__(self):
            return c

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_dbmod, "db_conn", lambda *a, **k: _Ctx(), raising=False)

    p._record_skill_feedback(conversation_id=1, intent="design",
                             output_content="", error="模拟失败")

    row = c.execute("SELECT use_stats, feedback_notes FROM skills WHERE name='测试技能'").fetchone()
    st = json.loads(row[0] or "{}")
    notes = json.loads(row[1] or "[]")
    c.close()
    assert int(st.get("uses", 0)) == 6, f"uses 未+1：{st}"
    assert int(st.get("fail", 0)) == 1, f"fail 未+1：{st}"
    assert len(notes) == 1, f"feedback_notes 未追加：{notes}"
    assert notes[0].get("note"), "备注内容为空"
    assert "run_id" in notes[0], "备注里应记录 run_id（供归批）"


def test_skill_feedback_has_no_bare_run_id():
    """静态哨兵：函数体内不得再出现裸 `run_id` 标识符（只允许 `_rid_note` / SQL 列名）。

    这条防的是"改回去"：`run_id` 不是本函数形参，一旦再写裸用就会 NameError。
    """
    import re
    src = open(os.path.join(ROOT, "agent", "pipeline_parts", "memory.py"),
               encoding="utf-8").read()
    m = re.search(r"def _record_skill_feedback\(.*?(?=\n    def )", src, re.S)
    assert m, "找不到 _record_skill_feedback"
    code = re.sub(r"#[^\n]*", "", m.group(0))
    code = re.sub(r"'[^']*'|\"[^\"]*\"", "", code)      # 去掉字符串字面量（含 SQL）
    bare = re.findall(r"(?<![._\w])run_id(?![\w])", code)
    assert not bare, f"仍有裸 run_id 引用（会 NameError）：{bare}"


# ══════════════════════════════════════════════════════════════════════
# P0-3 技能白名单：默认只做授权补充，不做上限

def test_skill_whitelist_not_enforced_by_default():
    """★ 默认不裁减：`config.get` 返回 False，且代码里的 fallback 字面量必须是 False。

    ⚠️ `agent` 组整体**不在** DEFAULT_CONFIG（该组 25 个键都只在 CONFIG_SCHEMA 里，
    这是既有设计），所以默认值由**代码 fallback** 提供 —— 必须直接断言这个字面量，
    否则把 fallback 改成 True 时测试不会红（第一版就是这样空转的）。
    """
    from core import config as cfg
    assert cfg.get("agent", "skill_whitelist_enforce", False) is False, (
        "默认值必须为 False（只做授权补充）")
    src = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"),
               encoding="utf-8").read()
    assert '_cfg_sw.get("agent", "skill_whitelist_enforce", False)' in src, (
        "代码 fallback 不是 False —— 默认会裁减 Agent 已绑定的工具")


def test_skill_whitelist_switch_restores_old_behavior():
    """回退开关必须存在且在 schema 登记（能被运维改）。"""
    from core import config as cfg
    assert "skill_whitelist_enforce" in cfg.CONFIG_SCHEMA.get("agent", {}), (
        "配置项未登记 ⇒ 运维看不见也改不了")


def test_execute_does_not_assign_skill_whitelist_when_disabled():
    """静态哨兵：赋值必须被 `and _enforce` 这个**条件表达式**保护。

    判据刻意落在"完整条件表达式"上，而不是"段内出现过 `_enforce`"——
    后者太弱：把 `if _skill_wl and _enforce:` 改成 `if _skill_wl:` 时，
    段内前面那几行 `_enforce = ...` 赋值仍在，弱断言照样通过（变异 5 实测空转）。
    """
    src = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"),
               encoding="utf-8").read()
    i = src.find('_skill_wl = getattr(self, "_skill_allowed_tools", None)')
    assert i > 0, "找不到技能白名单合并段"
    seg = src[i:i + 1600]
    assert "if _skill_wl and _enforce:" in seg, (
        "赋值未受 `and _enforce` 保护（回归：会无条件把技能白名单当上限）")
    assert "self._tool_whitelist = list(_skill_wl)" in seg


# ══════════════════════════════════════════════════════════════════════
# P1-4 triggers 格式校验

def test_trigger_validator_detects_list_in_one_element():
    """★ 核心：「变更、调整、更新、影响、后果、」必须被判为脏。"""
    from core.skill_triggers import analyze_triggers
    issues = analyze_triggers('["变更、调整、更新、影响、后果、"]')
    assert len(issues) == 1
    assert issues[0][1] == ["变更", "调整", "更新", "影响", "后果"]


def test_trigger_validator_keeps_clean_and_conjunctive():
    """干净数据与合取式（A+B）都不得被判脏或拆开。

    `A+B` 是平台的合取语法（两个词都要出现），拆开会把语义从"同时"变成"任一"。
    """
    from core.skill_triggers import analyze_triggers, normalize_triggers
    for raw in ('["变更","调整","影响"]', '["变更+影响"]', '["A、B"]', '[]'):
        assert analyze_triggers(raw) == [], f"误判为脏：{raw}"
    assert normalize_triggers('["变更+影响"]')[0] == ["变更+影响"]


def test_trigger_validator_strict_raises():
    """strict 模式供写入侧从源头堵住。"""
    from core.skill_triggers import normalize_triggers
    with pytest.raises(ValueError):
        normalize_triggers('["变更、调整、更新、影响"]', strict=True)


def test_skill_router_normalizes_on_write():
    """技能保存 API 必须调用规范化（否则脏数据还能再进来）。"""
    src = open(os.path.join(ROOT, "routers", "studio_parts", "skills.py"),
               encoding="utf-8").read()
    # create 与 update 各一次
    assert src.count("_normalize_trg(body.triggers)") >= 2, (
        "create/update 至少各接一次规范化")
