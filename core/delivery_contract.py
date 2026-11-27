# -*- coding: utf-8 -*-
"""通用交付契约（delivery contract）—— 把「声称完成」与「实际交付」分开判定。

为什么需要这个模块（2026-11-27 会话 1000000251 真机事故）
--------------------------------------------------------
用户要「变更影响分析报告」，答复里声称「十节正文 + 附录 A~F 已交付」，
但落库的 artifact/report 只有一个 section（547 字符的一段自述）。
根因不是「数据缺失」，而是三层都没有机器判据：
  ① 验收层：待办 #1783/#1784 的 verify 是自然语言、verify_tool 为空，
     LLM 自己在 verify_result 里写「十节已输出」⇒ status=completed ⇒ gate 判放行；
  ② 模板层：报告类型模板（REPORT_TYPES）只存在 prompt 里，落库时不校验；
  ③ 交付层：`_archive_artifacts` 只要 intent=report_generation 就把任意正文
     物化成 report，`structure()` 对无标题段落产出单节「正文」也照收。

本模块提供**通用**判据，不绑定任何具体报告类型、工具或 Agent：
  · `evidence_tier()` —— 待办验收证据分级（machine / claim / manual / unknown），
    用于把「自己说完成」从「有机器回执」中分离；不修改既有状态机。
  · `expected_sections()` —— 从任意「模板/契约声明」提取期望章节数，
    来源可是 REPORT_TYPES、技能 frontmatter、调用方显式契约，**不硬编码 10**。
  · `check_section_contract()` —— 校验实际 sections 是否达到契约；
    明确区分「未声明契约（checked=False）」与「声明了且通过（checked=True, ok=True）」，
    避免「没契约」伪装成「验收通过」。

设计纪律（对齐工程既有铁律）：
  · 纯函数、零 IO、不依赖报告模块 —— 任何产物类型都能复用；
  · fail-safe：契约解析失败 ⇒ checked=False 而非 ok=True；
  · 判据跨边界：结论落到产物 meta，用户读的地方能看到。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

__all__ = [
    "EV_MACHINE", "EV_CLAIM", "EV_MANUAL", "EV_UNKNOWN",
    "evidence_tier", "claim_evidence_summary",
    "expected_sections", "check_section_contract", "contract_from_card",
]

EV_MACHINE = "machine"
EV_CLAIM = "claim"
EV_MANUAL = "manual"
EV_UNKNOWN = "unknown"

#: 显式人工验收的标记：这些 verify 文本允许「仅自述」，不冒充机器验收。
_MANUAL_MARKS = ("人工确认", "需人工", "manual", "用户确认", "人工验收")


def evidence_tier(verify: str = "", verify_tool: str = "",
                  evidence: Any = None) -> str:
    """给一条待办的验收证据分级（纯函数）。

    级别语义：
      · machine —— 有机器回执（`evidence.kind == 'machine'`）：真验过；
      · manual  —— 契约本身就声明为人工确认：允许自述，但要如实标注；
      · claim   —— 写了可机器验收的 verify，却只有自述：这是本次事故形态；
      · unknown —— 契约或回执无法解析：不当作通过，也不武断说模型撒谎。

    `verify_tool` 非空但 evidence 缺失/非 machine 时判 claim —— 这正是
    「本该机器验、却只写了段话」的形状，必须能被门禁识别。
    """
    v = str(verify or "").strip()
    vt = str(verify_tool or "").strip()
    ev = evidence
    if isinstance(ev, str):
        try:
            ev = json.loads(ev or "{}")
        except Exception:
            ev = None
    if not isinstance(ev, dict):
        ev = {}
    kind = str(ev.get("kind") or "").strip().lower()
    if kind == EV_MACHINE:
        return EV_MACHINE
    if kind in (EV_UNKNOWN, "error"):
        return EV_UNKNOWN
    if any(m in v for m in _MANUAL_MARKS):
        return EV_MANUAL
    if vt or v:
        return EV_CLAIM
    return EV_UNKNOWN


def claim_evidence_summary(items) -> dict:
    """统计一组待办（dict/TodoItem 序列）里的自述验收，供 gate 如实告知。

    返回 ``{"total", "machine", "claim", "manual", "unknown", "claim_subjects"}``。
    `claim_subjects` 是可打印证据（用户能核对是哪几条），不是总数口径。
    """
    out = {"total": 0, "machine": 0, "claim": 0,
           "manual": 0, "unknown": 0, "claim_subjects": []}
    for it in (items or []):
        if isinstance(it, dict):
            g = lambda k, d="": it.get(k, d)
        else:
            g = lambda k, d="": getattr(it, k, d)
        if str(g("status", "")) == "deleted":
            continue
        out["total"] += 1
        tier = evidence_tier(g("verify", ""), g("verify_tool", ""), g("evidence"))
        out[tier] += 1
        if tier == EV_CLAIM:
            out["claim_subjects"].append(str(g("subject", ""))[:120])
    return out


def expected_sections(contract: Any = None, *, template: Any = None) -> dict:
    """从契约声明/类型模板提取期望章节（通用，不写死 10）。

    :param contract: 调用方显式契约。接受三种形态：
        · int                         —— 最少章节数；
        · {"min_sections": n, ...}    —— 字典；
        · {"required_sections": [...]} —— 必须出现的章节标题列表；
        · list[str] / list[dict]       —— 章节清单（取其长度）。
    :param template: 类型模板（如 ``REPORT_TYPES[type]["sections"]``），
        仅当 contract 未提供时兜底。
    :returns: ``{"checked": bool, "min": int, "required": [str], "source": str}``。
        `checked=False` 表示**没有契约**——绝不能当作「验收通过」。
    """
    src = ""
    if contract is None:
        if template is None:
            return {"checked": False, "min": 0, "required": [], "source": ""}
        contract, src = template, "type_template"
    elif isinstance(contract, dict) and contract.get("sections") is not None:
        src = str(contract.get("source") or "caller")
        contract = contract["sections"]

    required: list = []
    if isinstance(contract, bool):
        return {"checked": False, "min": 0, "required": [], "source": src}
    if isinstance(contract, int):
        return {"checked": True, "min": max(0, contract), "required": [], "source": src or "caller"}
    if isinstance(contract, str):
        try:
            return expected_sections(json.loads(contract), template=template)
        except Exception:
            return {"checked": False, "min": 0, "required": [], "source": src}
    if isinstance(contract, (list, tuple)):
        for s in contract:
            if isinstance(s, dict):
                h = str(s.get("heading") or s.get("title") or "").strip()
            else:
                h = str(s).strip()
            if h:
                required.append(h)
        # 显式清单按「至少这些节」校验；模板清单按「至少这么多节」校验
        return {"checked": True,
                "min": len(required) if src != "type_template" else len(required),
                "required": required if src != "type_template" else [],
                "source": src or "caller"}
    if isinstance(contract, dict):
        req = contract.get("required_sections") or contract.get("required_headings") or []
        if isinstance(req, str):
            req = [x for x in re.split(r"[/、,，|]", req) if x.strip()]
        req = [str(x).strip() for x in (req or []) if str(x).strip()]
        mn = contract.get("min_sections") or contract.get("min") or 0
        try:
            mn = int(mn)
        except (TypeError, ValueError):
            mn = 0
        if mn <= 0 and req:
            mn = len(req)
        if mn <= 0 and not req:
            return {"checked": False, "min": 0, "required": [], "source": src}
        return {"checked": True, "min": max(0, mn), "required": req,
                "source": str(contract.get("source") or src or "caller")}
    return {"checked": False, "min": 0, "required": [], "source": src}


def _norm_head(s) -> str:
    """章节标题归一化：去 markdown 标记、空白与标点，用于宽松匹配。"""
    s = str(s or "").strip()
    s = re.sub(r"^#{1,6}\s*", "", s)
    return re.sub(r"[\s\u3000、,，.。:：\-—_()（）\[\]【】]", "", s).lower()


def check_section_contract(sections: Any, *, contract: Any = None,
                           template: Any = None) -> dict:
    """校验实际章节是否满足契约（纯函数，零 IO）。

    :returns: ``{"checked", "ok", "min", "actual", "missing", "source", "reason"}``
      · `checked=False` —— 无契约可校验，**不是通过**；
      · `ok=True`       —— 有契约且达标；
      · `ok=False`      —— 有契约且未达标，`missing` 列出缺哪些标题。
    """
    exp = expected_sections(contract, template=template)
    secs = sections if isinstance(sections, (list, tuple)) else []
    actual = len(secs)
    heads = []
    for s in secs:
        if isinstance(s, dict):
            heads.append(_norm_head(s.get("heading") or s.get("title") or ""))
        else:
            heads.append(_norm_head(s))
    joined = "|".join(h for h in heads if h)
    missing = [r for r in exp["required"]
               if _norm_head(r) and _norm_head(r) not in joined]
    base = {"checked": exp["checked"], "min": exp["min"], "actual": actual,
            "missing": missing, "source": exp["source"]}
    if not exp["checked"]:
        return dict(base, ok=False, reason="未声明交付契约（checked=False，不等于验收通过）")
    if missing:
        return dict(base, ok=False,
                    reason="缺少必需章节：%s" % "、".join(missing[:8]))
    if actual < exp["min"]:
        return dict(base, ok=False,
                    reason="章节数不足：期望 ≥%d，实际 %d" % (exp["min"], actual))
    return dict(base, ok=True, reason="章节契约满足（%d ≥ %d）" % (actual, exp["min"]))


def contract_from_card(card: Any = None) -> Any:
    """从 card_data 取显式契约（不改 call_data 结构，向后兼容）。

    优先级：``delivery_contract`` > ``report_contract`` > ``sections`` 的显式声明；
    没有就返回 None（= 无契约，落库方按类型模板兜底）。
    """
    cd = card if isinstance(card, dict) else {}
    for key in ("delivery_contract", "report_contract", "section_contract"):
        v = cd.get(key)
        if v not in (None, "", {}, []):
            return v
    return None
