# -*- coding: utf-8 -*-
"""探针/验证噪声在**决策消费方**的过滤一致性（2026-10-29）。

## 背景（生产库实测）

`core/probe_mark.py` 早已建好口径真源（`probe_where()` / `sane_latency_where()`），
`core/misevolution.py` 也接了。但**五个核心消费方全部没接**：

| 消费方 | 后果 |
|---|---|
| `attribution` | `fake_tool`（126 次 / 112 失败）成为"高频首坏步" |
| `config_proposal` | 经 attribution 间接取数 ⇒ 会针对假工具提议配置 |
| `evolution_report` | 周报「能力调用」虚高 |
| `alert_evaluator` | 成本告警虚高（chat_probe 占 7 天成本 51%）、p95 延迟被 56 年脏行拉爆 |
| `capability_effective` | 账本 orphan_calls 里 `fake_tool` 排第 2，假工具被当成"在跑的能力" |

⇒ 同一形态：**标记建好了、消费方没接**。本测试锁死"必须接"。

## 一个必须同时锁住的例外

`alert_evaluator` 的 `llm_fail_1h` / `llm_fail_rate_1h` **刻意保留探测**——
活体探针失败正是 provider 故障的第一手信号，排除等于捂上警报器。
本文件用**反向断言**保护它：若有人"统一口径"把这里也加上过滤，测试会红。
"""
import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════════════
# 夹具：一个只含两张决策表的最小库（探针行 + 业务行各一对）

@pytest.fixture()
def noise_db(tmp_path):
    """最小决策库：llm_usage_stats / tool_call_logs 各放入探针行与业务行。"""
    p = tmp_path / "noise.db"
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE llm_usage_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER DEFAULT 0, intent TEXT DEFAULT '',
        provider_name TEXT DEFAULT '', model_name TEXT DEFAULT '',
        call_failed INTEGER DEFAULT 0, failure_kind TEXT DEFAULT '',
        failure_msg TEXT DEFAULT '', finish_reason TEXT DEFAULT '',
        retry_count INTEGER DEFAULT 0, fallback_used INTEGER DEFAULT 0,
        used_mock INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,
        estimated_cost REAL DEFAULT 0, run_id INTEGER DEFAULT 0,
        sub_task_key TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        is_probe INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE tool_call_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intent TEXT DEFAULT '', agent_name TEXT DEFAULT '', tool_name TEXT DEFAULT '',
        tool_type TEXT DEFAULT '', arguments TEXT DEFAULT '{}', result TEXT DEFAULT '',
        ok INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,
        conversation_id INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        call_kind TEXT DEFAULT 'tool', user_name TEXT DEFAULT '', run_id INTEGER DEFAULT 0,
        task_key TEXT DEFAULT '', failure_kind TEXT DEFAULT '',
        capability_id TEXT DEFAULT '', trace_id TEXT DEFAULT '',
        result_len INTEGER DEFAULT 0, result_truncated INTEGER DEFAULT 0,
        is_probe INTEGER DEFAULT 0)""")

    # 业务行：1 次成功 LLM、1 次成功工具
    c.execute("INSERT INTO llm_usage_stats (intent,estimated_cost,latency_ms,is_probe) "
              "VALUES ('design',0.010,1200,0)")
    c.execute("INSERT INTO tool_call_logs (tool_name,ok,capability_id,is_probe) "
              "VALUES ('query_projects',1,'tool:query_projects',0)")
    # 探针行：1 次失败 LLM（chat_probe）+ 1 次失败假工具
    c.execute("INSERT INTO llm_usage_stats (intent,call_failed,estimated_cost,is_probe) "
              "VALUES ('chat_probe',1,0.050,1)")
    c.execute("INSERT INTO tool_call_logs (tool_name,ok,capability_id,is_probe) "
              "VALUES ('fake_tool',0,'tool:fake_tool',1)")
    c.commit()
    yield c
    c.close()


# ══════════════════════════════════════════════════════════════════════
# 1. attribution：取材 SQL 必须排除探针（真行为断言）

def test_attribution_tool_spec_excludes_probe(noise_db):
    """`_SPECS` 的 tool_call 取材 SQL 执行后，不得出现 fake_tool。

    这是**行为**断言而非文本断言：谁删掉 `+ _PROBE`，这里立刻红。
    """
    from core import attribution as A

    spec = next(s for s in A._SPECS if s.name == "tool_call")
    rows = noise_db.execute(spec.sql, (0,)).fetchall()
    names = [str((r["actor"] if hasattr(r, "keys") else r[4]) or "") for r in rows]
    assert "fake_tool" not in names, "★ 归因取材仍含探针 fake_tool（决策链被污染）"
    assert "query_projects" in names, "业务调用被误过滤（过滤条件写错了）"


def test_attribution_llm_spec_excludes_probe(noise_db):
    """LLM 取材同样必须排除探针（chat_probe 占成本 51%）。"""
    from core import attribution as A

    spec = next(s for s in A._SPECS if s.name == "llm_call")
    rows = noise_db.execute(spec.sql, (0,)).fetchall()
    intents = [str((r["intent"] if hasattr(r, "keys") else r[5]) or "") for r in rows]
    assert "chat_probe" not in intents, "★ 归因 LLM 取材仍含探针 chat_probe"
    assert "design" in intents, "业务 LLM 调用被误过滤"


def test_attribution_enum_sql_excludes_probe(noise_db):
    """会话枚举 SQL 也必须过滤——否则"只在探针会话里出现"的会话会被纳入归因。"""
    from core import attribution as A

    spec = next(s for s in A._SPECS if s.name == "tool_call")
    rows = noise_db.execute(spec.enum_sql, ("2020-01-01",)).fetchall()
    # 业务行与探针行都在 conv=0，枚举按 conversation_id>0 过滤 ⇒ 空集
    assert rows == [], "枚举 SQL 未按预期过滤（检查 conversation_id>0 与探针条件）"


# ══════════════════════════════════════════════════════════════════════
# 2. evolution_report：周报能力调用量排除探针

def test_evolution_report_probe_not_counted(noise_db):
    """周报「能力调用」的过滤片段必须真能排除探针行。"""
    from core import evolution_report as ER

    n_all = noise_db.execute("SELECT COUNT(*) FROM tool_call_logs").fetchone()[0]
    n_biz = noise_db.execute(
        "SELECT COUNT(*) FROM tool_call_logs WHERE 1=1" + ER._PROBE).fetchone()[0]
    assert n_all == 2 and n_biz == 1, f"探针未被排除：all={n_all} biz={n_biz}"


# ══════════════════════════════════════════════════════════════════════
# 3. capability_effective：账本调用集排除探针 + 取数失败必须可见

def test_capability_ledger_excludes_probe(noise_db):
    """账本不得把 fake_tool 列进 orphan_calls（假工具被当成在跑的能力）。"""
    from core.capability_effective import build_ledger

    # build_ledger 依赖 capability_registry；缺失时 collect_declared 自行降级
    led = build_ledger(noise_db)
    names = [o["tool_name"] for o in led["orphan_calls"]]
    assert "fake_tool" not in names, f"★ 账本 orphan 仍含探针：{names}"
    assert "query_projects" in names, f"业务工具被误排除：{names}"


def test_capability_ledger_reports_usage_error():
    """调用集取数失败必须带出 `usage_error`（原实现是 `except: pass`）。

    为什么这条重要：取数坏掉 ⇒ usage 全空 ⇒ 账本把**全部能力**判成"未使用"，
    而界面上这与"真的没人用"一模一样。必须让人看见。
    """
    from core.capability_effective import build_ledger

    # 建一个**没有 tool_call_logs 表**的库 ⇒ 取数必然失败
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        led = build_ledger(c)
        assert led["stats"].get("usage_error"), (
            "★ 取数失败被静默吞掉（stats 中没有 usage_error）")
    finally:
        c.close()


def test_capability_ledger_marks_probe_filtered(noise_db):
    """stats 要能自证"这一份账本已排除探针"（口径可核对）。"""
    from core.capability_effective import build_ledger

    led = build_ledger(noise_db)
    assert led["stats"].get("probe_filtered") is True


# ══════════════════════════════════════════════════════════════════════
# 4. alert_evaluator：两族口径**必须**分开（含反向断言保护例外）

def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_alert_business_metrics_exclude_probe():
    """成本/延迟/调用量三条查询必须拼 `_PROBE`。

    用文本定位每段 SQL 的**所在行**再断言含 `_PROBE`——比"文件里出现过"
    更严：能抓住"只改了其中一条"这类半截修复。
    """
    src = _src("core/alert_evaluator.py")
    lines = src.splitlines()

    def _block_has_probe(start_kw: str, span: int = 8) -> bool:
        for i, l in enumerate(lines):
            if start_kw in l:
                seg = "\n".join(lines[i:i + span])
                return "_PROBE" in seg
        raise AssertionError(f"找不到锚点：{start_kw}")

    assert _block_has_probe("base = (\"SELECT COUNT(*) c"), \
        "★ 成本/调用量口径未拼 _PROBE"
    assert _block_has_probe("SELECT latency_ms FROM llm_usage_stats"), \
        "★ 延迟 p95 未拼 _PROBE"
    assert _block_has_probe("WHERE date(created_at)=date('now','localtime')"), \
        "★ 今日成本未拼 _PROBE"


def test_alert_fail_rate_keeps_probe_on_purpose():
    """★ 反向断言：失败率**必须保留探测**（provider 健康的第一手信号）。

    若有人把这里也"统一"加上 `_PROBE`，等于把警报器捂上 ⇒ 本用例红。
    """
    src = _src("core/alert_evaluator.py")
    lines = src.splitlines()
    for i, l in enumerate(lines):
        if "SELECT COUNT(*) c, COALESCE(SUM(call_failed),0) f FROM llm_usage_stats" in l:
            seg = "\n".join(lines[i:i + 3])
            assert "_PROBE" not in seg, (
                "★ 失败率被加上了探针过滤 —— 这是刻意保留的例外"
                "（活体探针失败即 provider 故障信号），请勿统一口径")
            return
    raise AssertionError("找不到失败率查询（结构变了，需同步本用例）")


def test_alert_latency_uses_sane_latency_guard():
    """延迟 p95 还要过 `sane_latency_where`（实测 2 行 file_read 记了 ≈56.8 年）。"""
    src = _src("core/alert_evaluator.py")
    assert "_sane_lat()" in src, "★ 延迟统计缺少量纲守卫（56 年脏行会拉爆 p95）"


# ══════════════════════════════════════════════════════════════════════
# 5. 口径真源独占：五个消费方都不得自己写一份过滤字符串

def test_consumers_use_probe_mark_singleton():
    """五个消费方必须 import 真源，不得各自硬编码探针过滤字符串。

    为什么单独一条：本仓已多次出现"同一常量抄多份必然漂移"。

    ⚠️ 判据必须用**正则**而不是字面子串：第一版写成
       `"COALESCE(is_probe,0)=0" not in src`，结果带别名的写法
       `COALESCE(l.is_probe,0)=0` 正好绕过（变异 8 因此空转一轮）。
       凡"检查某种写法"的断言，要把**等价变体**一起覆盖。
    """
    import re

    # 可选表别名 + 可选空格，覆盖 `COALESCE(l.is_probe,0)=0` 等写法
    hand_rolled = re.compile(r"COALESCE\(\s*\w*\s*\.?\s*is_probe\s*,\s*0\s*\)\s*=\s*0")
    for rel in ("core/attribution.py", "core/evolution_report.py",
                "core/capability_effective.py", "core/capability_registry.py",
                "core/alert_evaluator.py"):
        src = _src(rel)
        assert "probe_mark import" in src, f"★ {rel} 未引用口径真源 probe_mark"
        m = hand_rolled.search(src)
        assert m is None, (
            f"★ {rel} 自己硬编码了探针过滤字符串 {m.group(0)!r}"
            f"（应走 probe_where()，否则将来改口径会漏掉这一处）")
