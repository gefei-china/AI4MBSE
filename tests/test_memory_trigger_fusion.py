# -*- coding: utf-8 -*-
"""P0#1 记忆触发器 + P0#2 融合排序 + 噪音闸门 —— 回归测试（2026-10-28）。

覆盖范围（每条都对应一个**曾经可能静默失效**的接缝）：
  1. 触发器判据本身（`should_recall_memory` / `should_deposit_memory`）；
  2. **接入点真的生效**：`_build_memory_hint` / `_deposit_session_memory` 被触发器拦截时
     **必须提前返回、且不触碰记忆后端** —— 用 spy 证明，而不是只看返回值。
     （动机：接入点用 `try/except: pass` 包裹，import 写错会静默失效，
       "看起来接入了、实际没生效"，返回值测不出来，必须看后端有没有被调用。）
  3. 五维融合（relevance × source_credibility）**真的影响排序**，且默认值中性不漂移；
  4. `record_access` 噪音闸门：噪音命中不积累使用证据。

全部使用临时库/内存库，不读写生产 `mbse.db`（遵守 conftest 的隔离纪律）。
"""
import sqlite3

import pytest

from agent.memory_recall import (
    _is_greeting,
    should_deposit_memory,
    should_recall_memory,
)
from memory_service import MemoryService


# ══════════════════════════════════════════════════════════════════════
# 1. 触发器判据
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("intent,query,expected,why", [
    ("chat", "你好", False, "纯问候 → 跳过召回"),
    ("chat", "您好，在吗", False, "短纯问候 → 跳过召回"),
    ("chat", "画活动图", True, "★短但有效的建模诉求 → 必须放行（不得被长度门误杀）"),
    ("chat", "SysML v2 包结构怎么设计", True, "正常诉求 → 放行"),
    ("chat", "", True, "空 query → 兜底放行（退化为最近 top_k）"),
    ("chat", "（Mock 回答）已收到你的消息：你好", False, "占位回声 → 跳过"),
    ("chat", "我是MBSE平台的通用助手", False, "平台话术 → 跳过"),
    ("chat", "你好，请帮我生成活动图", True, "★问候+真实诉求 → 保留召回，不得整句判死"),
])
def test_should_recall_memory(intent, query, expected, why):
    assert should_recall_memory(intent, query) is expected, why


def test_is_greeting_does_not_use_length_gate():
    """触发器不得复用 `_is_noise` 的 <12 字长度门（否则会误杀短有效诉求）。"""
    assert _is_greeting("画活动图") is False
    assert _is_greeting("你好") is True


def test_should_deposit_memory_default_passthrough():
    """默认配置下写触发一律放行（零行为漂移）。"""
    assert should_deposit_memory("chat", "任意内容") is True
    assert should_deposit_memory("retrieval_only", "任意内容") is True


# ══════════════════════════════════════════════════════════════════════
# 2. 接入点：必须证明「后端有没有被调用」，不能只看返回值
# ══════════════════════════════════════════════════════════════════════
class _SpyBackend:
    """记录记忆后端被调用的次数（用于证明接入点是否真的提前退出）。"""

    def __init__(self):
        self.search_core_calls = 0
        self.search_calls = 0
        self.maybe_deposit_calls = 0

    def search_core(self, *a, **kw):
        self.search_core_calls += 1
        return [{"id": 1, "content": "核心偏好样例：未会签指标一律 TBD 占位", "mem_type": "preference"}]

    def search(self, *a, **kw):
        self.search_calls += 1
        return []

    def maybe_deposit(self, *a, **kw):
        self.maybe_deposit_calls += 1
        return 123


def _make_mixin():
    from agent.pipeline_parts.memory import MemoryMixin
    obj = MemoryMixin()
    obj._mem_ctx = None
    return obj


def test_recall_hook_actually_short_circuits(monkeypatch):
    """★接入点铁证：触发器返回 False 时，`_build_memory_hint` 必须**不碰后端**。"""
    import agent.memory_recall as mr

    obj = _make_mixin()
    spy = _SpyBackend()
    monkeypatch.setattr("memory_backend.get_memory_backend", lambda: spy)
    monkeypatch.setattr(mr, "should_recall_memory", lambda *a, **kw: False)

    got = obj._build_memory_hint("你好", "chat")
    assert got == "", "触发器拦截时应返回空串"
    assert spy.search_core_calls == 0, (
        "★★ 后端仍被调用 ⇒ 接入未生效（try/except 把 import 错误吞掉了）")


def test_recall_hook_passes_through_when_allowed(monkeypatch):
    """反向对照：触发器放行时后端**必须**被调用（证明上一条断言不是空转）。"""
    import agent.memory_recall as mr

    obj = _make_mixin()
    spy = _SpyBackend()
    monkeypatch.setattr("memory_backend.get_memory_backend", lambda: spy)
    monkeypatch.setattr(mr, "should_recall_memory", lambda *a, **kw: True)

    got = obj._build_memory_hint("SysML v2 包结构怎么设计", "chat")
    assert spy.search_core_calls >= 1, "放行时后端应被调用"
    assert "核心偏好" in got or got != ""  # 至少不应被提前拦掉


def test_deposit_hook_actually_short_circuits(monkeypatch):
    """★接入点铁证：写触发返回 False 时，`maybe_deposit` 必须**不被调用**。"""
    import agent.memory_recall as mr

    obj = _make_mixin()
    spy = _SpyBackend()
    monkeypatch.setattr("memory_backend.get_memory_backend", lambda: spy)
    monkeypatch.setattr(mr, "should_deposit_memory", lambda *a, **kw: False)

    obj._deposit_session_memory("用户诉求", "本次产出的可复用经验正文" * 3, "chat")
    assert spy.maybe_deposit_calls == 0, (
        "★★ maybe_deposit 仍被调用 ⇒ 写触发接入未生效")


# ══════════════════════════════════════════════════════════════════════
# 3. 五维融合：relevance × source_credibility
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture()
def mem_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT, mem_type TEXT DEFAULT 'fact', content TEXT DEFAULT '',
        embedding TEXT DEFAULT '[]', embed_version TEXT DEFAULT '',
        source TEXT DEFAULT 'agent', relevance REAL DEFAULT 1.0,
        activation REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
        last_accessed_at TEXT DEFAULT '', created_at TEXT DEFAULT '',
        forgotten INTEGER DEFAULT 0, tier TEXT DEFAULT 'recall',
        superseded_by INTEGER DEFAULT 0, scope_type TEXT DEFAULT '',
        scope_id TEXT DEFAULT '', mem_topic TEXT DEFAULT '')""")
    yield c
    c.close()


_VALUE = "建模时必须先定包结构再生成需求"


def _insert(c, source, relevance, content=_VALUE):
    c.execute("INSERT INTO agent_memory (agent_id,mem_type,content,source,relevance,created_at,forgotten) "
              "VALUES ('chat','experience',?,?,?,datetime('now','localtime'),0)",
              (content, source, relevance))
    c.commit()


def test_fusion_default_is_neutral(mem_conn):
    """默认配置（credibility 空 / relevance 全 1.0）⇒ 零行为漂移。"""
    for src in ("agent", "llm_agent", "reflow", "rule_agent", ""):
        assert MemoryService._source_credibility(src) == 1.0


def test_fusion_breaks_tie_by_relevance_and_credibility(mem_conn, monkeypatch):
    """两条语义**完全相同**的记忆，只有 relevance/source 不同 ⇒ 融合后必须分出胜负。

    这证明 `rel × cred` 真的进了排序键；若未生效，两者分数会完全一致、顺序不可控。
    """
    import core.config as cfgmod

    _orig = cfgmod.get

    def _patched(section, key=None, default=None):
        if key is None:
            return _orig(section, key, default)
        if section == "memory" and key == "source_credibility":
            return "llm_agent=1.0,reflow=0.5"
        return _orig(section, key, default)

    monkeypatch.setattr(cfgmod, "get", _patched)

    _insert(mem_conn, "reflow", 1.0)      # 组合 = 0.5
    _insert(mem_conn, "llm_agent", 2.0)   # 组合 = 2.0
    res = MemoryService.search(mem_conn, "chat", _VALUE, top_k=5)
    assert len(res) == 2
    assert res[0]["source"] == "llm_agent", "高 relevance × 高可信度应排前（融合未生效则会乱序）"


# ══════════════════════════════════════════════════════════════════════
# 4. record_access 噪音闸门
# ══════════════════════════════════════════════════════════════════════
_NOISE_TEXT = "你好，我是 MBSE 平台的通用助手。你发送的「123」我这边没有识别到具体意图，方便补充一下你想做什么"


def test_record_access_gate_skips_noise(mem_conn):
    """噪音命中**不得**积累使用证据（否则 activation 自锁、永远逃过遗忘）。"""
    _insert(mem_conn, "rule_agent", 1.0, content=_NOISE_TEXT)
    _insert(mem_conn, "llm_agent", 1.0, content=_VALUE)

    MemoryService.search(mem_conn, "chat", "MBSE 平台通用助手 识别到具体意图 建模 包结构 需求", top_k=5)

    rows = {r["content"]: dict(r) for r in mem_conn.execute(
        "SELECT content, access_count FROM agent_memory")}
    assert rows[_NOISE_TEXT]["access_count"] == 0, "★噪音不应被记账"
    assert rows[_VALUE]["access_count"] == 1, "有效条目应正常 +1（证明闸门非全关）"
