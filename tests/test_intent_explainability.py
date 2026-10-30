# -*- coding: utf-8 -*-
"""意图路由与可解释性修复（2026-10-29，基于 conv 9751 实测）。

═══════════════════════════════════════════════════════════════════════
五个真实缺陷（全部有生产数据证据）
═══════════════════════════════════════════════════════════════════════
P0 平台操作类请求无 Agent 承接
   智源对接agent(735) intent_keywords=[] ⇒ 完全不参与关键词路由；
   zhiyuan_mgmt(275) status=disabled ⇒ 被 `WHERE status='active'` 滤掉。
   ⇒ 「查工程…的 projectId」判 knowledge_qa@0.80，知识问答Agent 空答不调工具。
   实测同型误判在 query_trace 重复 4 次（3828/3691/3680/3675）。

P0b explain 前置被专有名词误触发
   「查工程「广汽**方法论**建模1022」的 projectId」命中说明信号「方法论」
   —— 而它是**工程名的一部分**，不是提问意图 ⇒ 短路判 knowledge_qa。

P1 检索零命中却声称有依据
   exec.reasoning 无条件写"将基于检索到的互联数据组织回答"，
   实测该轮 graph=0 / vector=0 / citations=0。

P2 confidence 双口径前端误导
   card_data.confidence 是**检索**置信度(0.00)，意图置信度是 0.80；
   前端把它渲染在「意图：xxx」旁边 ⇒ 读成"意图置信度 0"。

P3 conversations.intent 字段污染
   存过主 Agent 名（25/60 近期会话 = 智源对接agent），意图统计失真。

P4 绑定与执行不一致无说明
   main_agent=智源对接agent、实际执行知识问答Agent，exec.notices=null。
   根因：团队路由定向只 yield reasoning，没 append 到 exec_notices。
"""
import os
import sqlite3

import pytest

from agent.intent import IntentRouter
from agent.pipeline_parts.session import SessionMixin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════════════
# P0b / explain 前置豁免
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture()
def router():
    return IntentRouter()


@pytest.mark.parametrize("text", [
    "查工程「广汽建模方法论1022」的 projectId，只回答这一项",
    "用智源接口查一下工程「广汽方法论建模1022」：通信子系统下有个包叫 SD",
    "智源工程列表 工程 projectId",
    "获取工程的 projectId 和分支列表",
    "导出该工程包的源码",
])
def test_platform_operation_detected(router, text):
    """P0b：平台专有标识必须被识别为"操作平台"。"""
    assert router._is_platform_operation(text) is True, text


@pytest.mark.parametrize("text", [
    "介绍一下智源平台的能力",
    "介绍一下需求分析的流程",
    "这份附件里讲的流程是什么",
    "SysML v2 的 succession 是什么意思",
    "MBSE 的建模方法论是什么",
    "SysML v2 的标准规范有哪些",
])
def test_platform_operation_not_triggered_for_concept_questions(router, text):
    """★ 反向保护：真 explain 正例不得被误判为平台操作（否则会掉到 chat）。"""
    assert router._is_platform_operation(text) is False, text


def test_explain_short_circuit_exempted_for_platform_ops(router):
    """★ P0b 核心：「方法论」在工程名里 ≠ 提问意图 ⇒ 不得触发 explain 短路。"""
    q = "查工程「广汽建模方法论1022」的 projectId，只回答这一项"
    # 该句确实含说明信号「方法论」，旧实现会判 explain=True
    assert any(k in q for k in IntentRouter._EXPLAIN_SIGNALS)
    assert router._is_explain_ask(q) is False, "平台操作仍被 explain 短路"


def test_explain_still_works_for_real_questions(router):
    """真 explain 正例必须保住（前置短路行为不变）。"""
    for q in ("介绍一下需求分析的流程", "MBSE 的建模方法论是什么",
              "SysML v2 的标准规范有哪些"):
        assert router._is_explain_ask(q) is True, q


def test_generic_kw_covers_spec_name_fragments():
    """工程名常见片段必须归入泛词（不得单凭它们决定路由）。"""
    for w in ("方法论", "流程", "规范", "标准", "体系", "框架"):
        assert w in IntentRouter._GENERIC_KW, f"{w} 未归入泛词"


# ══════════════════════════════════════════════════════════════════════
# P3 / conversations.intent 只存意图标签
# ══════════════════════════════════════════════════════════════════════
class _Stub(SessionMixin):
    pass


def test_intent_for_storage_keeps_standard_label():
    s = _Stub()
    assert s._intent_for_storage("knowledge_qa") == "knowledge_qa"
    assert s._intent_for_storage("design") == "design"
    assert s._intent_for_storage("chat") == "chat"


def test_intent_for_storage_rejects_agent_name():
    """★ P3 核心：主 Agent 名不得写入 conversations.intent。"""
    s = _Stub()
    assert s._intent_for_storage("智源对接agent") == "chat"
    assert s._intent_for_storage("智源对接agent", detected="design") == "design"
    assert s._intent_for_storage("MBSE建模总体负责人", detected="requirement_analysis") \
        == "requirement_analysis"


def test_intent_for_storage_never_returns_empty():
    s = _Stub()
    assert s._intent_for_storage("") == "chat"
    assert s._intent_for_storage(None) == "chat"


def test_standard_labels_cover_db_builtin_intents():
    """标准标签集必须覆盖库里 builtin=1 的全部意图（否则会被降级成 chat）。

    ⚠️ 生产库若新增内置意图而本常量未同步，该意图会被规范化成 chat（静默丢信息）。
       本用例用真实库校对，防止这种漂移。
    """
    db = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(db):
        pytest.skip("生产库不存在")
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        names = {r[0] for r in c.execute("SELECT name FROM agents WHERE builtin=1")}
    finally:
        c.close()
    missing = names - set(SessionMixin._STANDARD_INTENT_LABELS)
    assert not missing, f"标准标签集缺内置意图：{missing}（新增内置意图需同步常量）"


# ══════════════════════════════════════════════════════════════════════
# P1 / P2 源码级断言（行为断言需要跑完整 stream，成本过高且依赖 LLM）
# ══════════════════════════════════════════════════════════════════════
def _code_only(path: str) -> str:
    """剔除注释后的源码（断言"不得再出现某写法"时必须排除说明文字）。"""
    out = []
    for line in open(os.path.join(ROOT, path), encoding="utf-8").read().splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def test_p1_reasoning_no_unconditional_retrieval_claim():
    """★ P1：检索零命中时不得再声称"基于检索到的互联数据"。"""
    src = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
    assert "未命中任何条目" in src, "缺少零命中的明确文案"
    assert "无检索依据" in src, "缺少「无检索依据」的口径声明"
    # 关键：必须按实际命中数分支，而不是无条件拼接
    assert "_g + _v > 0" in src, "reasoning 文案未按实际命中数分支"


def test_p2_card_data_has_intent_confidence():
    """★ P2：card_data 必须同时提供意图置信度与检索置信度（分列，不混用）。"""
    src = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
    assert '"intent_confidence"' in src, "card_data 缺 intent_confidence"
    assert '"intent_route"' in src, "card_data 缺 intent_route"


def test_p2_frontend_labels_both_confidences():
    """★ P2：前端必须分别标注"意图置信"与"检索置信"，不能再混成一个"置信度"。"""
    js = open(os.path.join(ROOT, "static/js/mods/05-markdown.js"), encoding="utf-8").read()
    assert "意图置信" in js, "前端未标注意图置信度"
    assert "检索置信" in js, "前端未标注检索置信度"
    assert "intent_confidence" in js, "前端未读取 intent_confidence"


def test_p4_team_direct_notice_is_persisted():
    """★ P4：团队路由定向说明必须进 exec_notices（落库可见），不能只 yield reasoning。

    实测 conv 9751：main_agent=智源对接agent、实际执行知识问答Agent，
    而 exec.notices=null ⇒ 完全无法解释"为什么换了 Agent"。

    ★ 2026-10-30 修判据（原判据锚**展示文案**，已失效）：
      原写法 `src.find('已定向到团队成员')` —— 文案是展示层，随时会为了更准确而改。
      实测：把文案从「（团队路由）已定向到团队成员「X」」
      改成「（团队路由）主 Agent「Y」将本次请求定向给成员「X」」（**更准确**：补上了
      主 Agent 是谁，正是 P4 要说清的那件事），断言立刻 `idx=-1` 判红 ——
      功能没坏，判据坏了。这类"文案耦合"判据会让**改进文案的人被判红**，是反向激励。

      ⇒ 改锚**代码结构不变量**：`_subj_note` 这个变量的赋值点，其邻近必须同时出现
        `exec_notices.append`。这才是 P4 真正要守的东西：
        **说明文本进了结构化 notices，而不只是发了一条 reasoning**。

    变异自证：删掉 `exec_notices.append({"kind": "progress", "text": _subj_note})`
    ⇒ `_subj_note` 附近再无 append ⇒ 判红（验证见下方 `_assert_invariant` 的注释）。
    """
    src = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
    idx = src.find("_subj_note = (")
    assert idx > 0, "找不到团队定向说明的赋值点（_subj_note）"
    seg = src[max(0, idx - 1200): idx + 900]
    assert "exec_notices.append" in seg, (
        "团队定向说明仍未写入 exec_notices（只 yield reasoning 会在刷新后丢失）")
    # 补充约束：说明必须同时以 reasoning 发出（前端实时可见），两者缺一都不算修好
    assert '"delta": _subj_note' in seg, "定向说明未以 reasoning 实时发出（用户当轮看不到）"
