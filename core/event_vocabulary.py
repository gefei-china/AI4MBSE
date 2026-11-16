# -*- coding: utf-8 -*-
"""封闭事件类型词表 + ignorable 前向兼容（P1-3 剩余缺口）。

对标 DSH ``dsh-session/lib/types/known-event-types.js:1-21``：读路径遇到
词表外的事件类型、且事件未带 ``ignorable`` 标记时**拒绝**，
否则「静默跳过一个必需事件」会重建出错误的会话。

词表不是编造的：下面 20 项来自本机 ``mbse.db`` 的
``SELECT DISTINCT block_name FROM context_audit``（2026-11-09 实测 18 项，
2026-11-15 补登记 ``artifact_facts`` / ``output_contract`` —— 二者是 2026-11-14
``common.py::build_prompt_blocks`` 新增的 prompt 块，`verify_prompt_blocks.py`
的 `_EXTRA_KEYS` 里早有记录，只是当时漏登记进本词表）。
门禁会断言「生产库里出现的类型 ⊆ 词表」—— 出现新类型必须显式登记，
这正是把「格式演进」从一次性回填脚本变成**写入时受约束**的机制。

⚠️ 登记纪律：新增 prompt 块时**同一批**就要把名字加进这里。
   漏登记的后果不是"少一条统计"，而是读路径 `validate_event` 直接抛
   `UnknownEventError` ⇒ 会话回放失败（本文件的 docstring 第 1 段就是讲这个）。
"""
from __future__ import annotations

KNOWN_EVENT_TYPES = frozenset({
    "attachment", "boundary", "citation_rules", "intent_line", "memory",
    "model_code_req", "model_context", "ontology", "output_rules", "report",
    "retrieval", "role", "roster", "skill_prompt", "slots", "tool_rules",
    "tools", "user_ctx",
    # ★ 2026-11-15 补登记（此前漏登 ⇒ 门禁红）：common.py 的 prompt 块
    #   `artifact_facts`（产物自动归档事实，execute 路径收不到才加到 blocks）
    #   `output_contract`（agents.output_schema → 提示词的结构化输出契约）
    "artifact_facts", "output_contract",
    # ★ 2026-11-16 补登记：`artifact_baseline`（当前产物的正文基线，让迭代是
    #   "在既有产物上增量修改"而不是从零重写）。同样 11-13 就进了 prompt 块
    #   （common.py:326 / 698 的 `_build_artifact_baseline`），当时漏登记。
    #   与上面两项同族：**新增 prompt 块必须同批登记**，否则读路径
    #   `validate_event` 会抛 UnknownEventError ⇒ 会话回放失败。
    "artifact_baseline",
})


class UnknownEventError(RuntimeError):
    pass


def validate_event(event: dict) -> dict:
    """校验单条事件；未知类型仅在显式 ignorable=True 时被接受。"""
    if not isinstance(event, dict):
        raise UnknownEventError("事件必须是 dict，收到 %s" % type(event).__name__)
    kind = event.get("type") or event.get("block_name")
    if not kind:
        raise UnknownEventError("事件缺少 type / block_name")
    if kind in KNOWN_EVENT_TYPES:
        return {"accepted": True, "ignorable": False, "type": kind}
    if event.get("ignorable") is True:
        return {"accepted": True, "ignorable": True, "type": kind}
    raise UnknownEventError(
        "未知事件类型 %r：词表外类型须显式带 ignorable=True 才可读；"
        "新类型请登记进 core/event_vocabulary.py 的 KNOWN_EVENT_TYPES" % kind)


def fold_events(events) -> dict:
    """回放：逐条校验后按类型折叠计数；被忽略的未知类型单独记账。"""
    by_type, ignored, count = {}, [], 0
    for ev in events or ():
        r = validate_event(ev)
        count += 1
        if r["ignorable"]:
            ignored.append(r["type"])
        else:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    return {"count": count, "by_type": by_type, "ignored": ignored}


def assert_production_covered(conn) -> dict:
    """读路径守卫：生产库出现的 block_name 必须全部在词表内。"""
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT block_name FROM context_audit WHERE block_name IS NOT NULL")]
    unknown = sorted(set(rows) - set(KNOWN_EVENT_TYPES))
    return {"seen": len(rows), "known": len(KNOWN_EVENT_TYPES),
            "unknown": unknown, "ok": not unknown}


__all__ = ["KNOWN_EVENT_TYPES", "UnknownEventError", "validate_event",
           "fold_events", "assert_production_covered"]
