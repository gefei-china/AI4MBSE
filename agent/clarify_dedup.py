# -*- coding: utf-8 -*-
"""通用澄清去重 —— 已回答过的问题不再弹卡（2026-11-27）。

为什么需要（真机实证）
--------------------
conv 9571 7 连问 / conv 9553 24 连问 / conv 1000000254 回答后又被问同一题：
`ask_user` 工具执行处无条件产出 `clarify_required`，而防循环护栏只守
`session._clarify_detect`（内容级探测），ask_user 路径完全绕过。
后果：用户答了等于没答，卡反复弹，流程看似「确认后没有后续」。

设计纪律
--------
· 纯函数 + 只读 conn：不写库、不改状态机；
· 判据是**问题文本指纹**（归一化后 SHA1 前 12 位），不用「答案非空」推断——
  那会把「跳过/未答」误判成「已答」；
· skip 也记指纹：否则同一题会在「跳过」后无限重弹；
· 用户在**本轮明确改主意**（输入含【澄清补充】或「还是那个问题/改主意」）时不应用指纹，
  避免误杀合法重问。
"""
from __future__ import annotations

import hashlib
import json
import re

__all__ = ["question_fingerprint", "answered_fingerprints", "filter_questions",
           "is_explicit_reask", "as_question_list"]


def as_question_list(result_or_raw) -> list:
    """把 ask_user 的产出归一成 questions list（多题/单题/旧数据三兼容）。

    统一收口在**一处**，避免 execute.py / stream.py 两处各写 `[dict(req)]`
    再次漂移（这正是多题改造最容易漏的第二个点）。
    """
    raw = result_or_raw
    if isinstance(raw, dict):
        for key in ("clarify_questions", "questions"):
            v = raw.get(key)
            if isinstance(v, list) and v:
                return [dict(q) for q in v if isinstance(q, dict) and str(q.get("question") or "").strip()]
        one = raw.get("clarify")
        if isinstance(one, dict) and str(one.get("question") or "").strip():
            return [dict(one)]
        if str(raw.get("question") or "").strip():
            return [dict(raw)]
        return []
    if isinstance(raw, list):
        return [dict(q) for q in raw if isinstance(q, dict) and str(q.get("question") or "").strip()]
    return []


_REASK_MARKS = ("澄清补充", "还是那个问题", "改主意", "重新问", "换个答案")


def question_fingerprint(text) -> str:
    """问题文本 → 归一化指纹。空白/标点/全半角差异不影响判等。"""
    s = str(text or "")
    s = re.sub(r"[\s\u3000，。？！、：:；;?,.!？【】\[\]（）()「」『』\"']+", "", s)
    s = s.lower()
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def is_explicit_reask(user_input: str) -> bool:
    """用户本轮是否显式要求重问/改答案（此时不应用去重指纹）。"""
    s = str(user_input or "")
    return any(m in s for m in _REASK_MARKS)


def answered_fingerprints(conn, conversation_id: int) -> set:
    """从本会话**已处理**的澄清卡收集问题指纹（含 answered / skipped）。

    数据源：`messages.msg_type='clarify'` 的 card_data.questions + card_data.answered。
    无 evidence 列/表异常时返回空集（fail-safe：宁可多问一次，不可吞掉真问题）。
    """
    out = set()
    if not conn or not conversation_id:
        return out
    try:
        rows = conn.execute(
            "SELECT card_data FROM messages WHERE conversation_id=? "
            "AND msg_type='clarify' ORDER BY id", (int(conversation_id),)).fetchall()
    except Exception:
        return out
    for r in rows:
        raw = r[0] if isinstance(r, (tuple, list)) else (r["card_data"] if hasattr(r, "keys") else "")
        if not raw:
            continue
        try:
            cd = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(cd, dict):
            continue
        ans = cd.get("answered")
        if not isinstance(ans, dict):
            continue                      # 未处理（或仅挂起）⇒ 不算已答
        for q in (cd.get("questions") or []):
            if isinstance(q, dict):
                out.add(question_fingerprint(q.get("question")))
    return out


def filter_questions(questions, conn=None, conversation_id: int = 0,
                     *, user_input: str = "") -> list:
    """过滤掉本会话已回答/已跳过的问题；全部已答时返回空列表。

    :param questions: [{id, question, options, ...}, ...]
    :returns: 保留的问题列表（原对象，不拷贝内部字段）。
    """
    qs = [q for q in (questions or []) if isinstance(q, dict) and str(q.get("question") or "").strip()]
    if not qs or is_explicit_reask(user_input):
        return qs
    seen = answered_fingerprints(conn, conversation_id)
    if not seen:
        return qs
    return [q for q in qs if question_fingerprint(q.get("question")) not in seen]
