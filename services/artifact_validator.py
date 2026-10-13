# -*- coding: utf-8 -*-
"""交付物结构校验（批次三 #17）：给 report / requirement / doc 三类**可判定的**体检。

## 要解决的真缺口（2026-10-14 取证）
`subtask_protocol.KIND_VALUES` 声明了 `sysml/requirement/report/doc` 四类，
但**只有 `sysml` 真的走校验**（`cards.py::_check_generated_sysml` → `checker.jar`）。
⇒ 报告/需求/文档类交付物**格式对不对全凭模型自觉** —— 用户收到的是一段文字，
   至于它是"需求条目"还是"随手一段散文"，系统不判、也不提示。

## 为什么这些校验必须是**确定性**的（不调 LLM）
与 #15 冲突检测同一条取舍，但这里更硬：
  · 物化是**每个子任务每轮都跑**的路径，调 LLM = 每轮多一次费用；
  · "有没有章节标题""条目有没有编号""是否含 RFC-2119 关键词"这些判据
    **完全可判定**，用 LLM 判反而引入不可复现性；
  · 校验器的产出要能**指导用户改**，所以必须能指出"缺哪一节、哪一条没编号"。
⇒ 只做字面/结构判据。**判不出的明确不报**（不猜、不用模型补）。

## 三类各自的判据（全部可复核）
### report（报告）
  R1 非空且有实质长度（< 80 字判为"过短"—— 这类通常是模型的敷衍产物）
  R2 **有分节**：至少 1 个 markdown 标题（`#`~`######`）或"第X章/一、"
  R3 有**结论段**：出现"结论/总结/建议/小结"之一（报告没有结论 = 无法行动）
  R4 声明了**待确认/风险**（若全文出现"假设/预计/大概"却无风险标注 ⇒ 提示）

### requirement（需求条目）
  Q1 有条目标识：编号（REQ-xxx / 3.1 / 1) / Q1）或至少一条"应当/必须/不得"
  Q2 **可验证性**：至少一条含 RFC-2119 关键词（应当/必须/不得/禁止/可以）
     ⚠️ 没有关键词的需求**不可测** —— 这是需求质量的最低门槛
  Q3 有**验收/验证**口径（"验收/验证/判据/测试"之一）—— 否则无法确认做完了
  Q4 单条不宜过长（> 600 字判为"未条目化"，一条需求写成一个段落 = 没法测）

### doc（文档）
  D1 非空且有实质长度
  D2 有**标题层级**（markdown 标题）
  D3 分节数 ≥ 2（单段文档通常说明它其实是个回答而不是文档）

## 三条共用的元判据（★防假绿，本仓反复栽过的地方）
  V1 **空内容恒不判绿**：content 为空 ⇒ `ok=False`，且理由必须写明"内容为空"
  V2 返回值必须带 `checked`（判据条数）—— 调用方可据此判断"是不是什么都没查"
  V3 任何异常 ⇒ `ok=True` + `degraded=True`（**绝不因校验器自身故障阻断交付**），
     且此时 `checked` 必须为 0，让调用方能分辨"校验通过"与"没校验成"

纯函数模块：不 import DB / 网络 / LLM（仅标准库 re），可被门禁与验证脚本复用。
"""
from __future__ import annotations

import re

# ── 三类各自的门槛（可配置在此，但**必须有下限**，见下方 clamp）──────────────
MIN_CONTENT_CHARS = {"report": 80, "requirement": 20, "doc": 80}
MAX_REQ_ITEM_CHARS = 600

# RFC-2119 关键词（★ 刻意**不含**「需要/应该」—— 这两个在中文需求里是弱表述，
#   几乎每句话都能写「系统需要…」而什么都没约束住；
#   RFC-2119 规范里对应的规范词是 shall/must/should，
#   中文工程实践的对应惯例是「应当/必须/不得/禁止/可以」。
#   把弱词收进来会让"有 RFC-2119 关键词"这条判据几乎恒真 ⇒ 变成假绿。）
_RFC2119 = ("应当", "必须", "不得", "禁止", "可以", "shall", "must", "may")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)
_CN_SECT = re.compile(r"^\s*(?:第[一二三四五六七八九十百\d]+[章节部分]|"
                      r"[一二三四五六七八九十]+[、.．)）]\s*\S)", re.M)
_REQ_ID = re.compile(r"(?:REQ[-_ ]?\w+|\b[A-Z]{2,}-\d+\b|"
                     r"\b\d+(?:\.\d+)+[、.]?\s|\bR\d+\b)", re.I)

__all__ = ["validate_artifact", "VALIDATABLE_KINDS", "MIN_CONTENT_CHARS"]

# 只有这三类走本校验器（sysml 走 checker.jar，见 cards.py::_check_generated_sysml）
VALIDATABLE_KINDS = ("report", "requirement", "doc")


def _text_of(art):
    """从交付物条目里取**待校验正文**。

    ⚠️ 取值口径必须宽：`content_json` 可能塞的是结构化字段（{title,ref}）
    而非正文 —— 那时**没有正文可校验**，此时返回空串并由 V1 判红，
    **不能**把 title 当正文去"校验通过"（那就是拿标签冒充内容）。
    """
    if isinstance(art, str):
        return art
    if not isinstance(art, dict):
        return ""
    for k in ("content", "text", "body", "markdown", "value"):
        v = art.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _sections(t):
    return len(_MD_HEAD.findall(t)) + len(_CN_SECT.findall(t))


def _v_report(t, add):
    if len(t) < MIN_CONTENT_CHARS["report"]:
        add("内容过短（%d 字 < %d）：报告通常应是成段的分析，此长度更像敷衍产物"
            % (len(t), MIN_CONTENT_CHARS["report"]))
    if _sections(t) < 1:
        add("**无分节结构**：建议用 markdown 标题分节，读者无法快速定位结论")
    if not re.search(r"结论|总结|建议|小结|展望", t):
        add("**无结论段**：报告读不出「所以该怎么做」")
    if re.search(r"假设|预计|大概|可能|疑似|暂估", t) and \
            not re.search(r"风险|待确认|待核实|局限|不确定", t):
        add("出现推测性表述（假设/预计/可能等）但**未标注风险或待确认**")


def _v_requirement(t, add):
    if len(t) < MIN_CONTENT_CHARS["requirement"]:
        add("内容过短（%d 字）：需求条目至少应写清「谁-在什么条件下-做什么」"
            % len(t))
    if not _REQ_ID.search(t):
        add("**无条目标识**（如 REQ-001 / 3.1）：需求无法被追溯与逐条确认")
    if not any(k in t for k in _RFC2119):
        add("**无 RFC-2119 关键词（应当/必须/不得/禁止）**："
            "这样的句子不可测 —— 无法判定「做完了没有」")
    if not re.search(r"验收|验证|判据|测试|准则|通过条件", t):
        add("**无验收/验证口径**：需求完成与否无客观判据")
    if len(t) > MAX_REQ_ITEM_CHARS:
        add("内容过长（%d 字 > %d）：一条需求写成一个段落 = 无法逐条验证，"
            "建议拆成多条" % (len(t), MAX_REQ_ITEM_CHARS))


def _v_doc(t, add):
    if len(t) < MIN_CONTENT_CHARS["doc"]:
        add("内容过短（%d 字 < %d）" % (len(t), MIN_CONTENT_CHARS["doc"]))
    if _sections(t) < 1:
        add("**无标题层级**：文档无法导航")
    if _sections(t) < 2:
        add("分节数 < 2：单段文档通常其实是个回答，不是文档"
            "（若确实是短说明，请把 kind 改成 report）")


_VALIDATORS = {"report": _v_report, "requirement": _v_requirement, "doc": _v_doc}

# 各类实际执行的判据条数（**必须与上方函数里的 add(...) 数量一致** ——
# 不一致就等于又造了一个"数字与事实不符"的字段，门禁 X1 会当场把它抓出来）。
# ⚠️ 这不是"总判据数"，而是"**最坏情况下最多能报几条**"——
#    部分判据是**条件触发**（如 report 的"有推测表述但无风险标注"需两个条件同时满足），
#    所以具体某次校验的 issues 条数会**少于**它。
_CHECKED_COUNT = {"report": 4, "requirement": 5, "doc": 3}


def validate_artifact(kind, artifact):
    """校验单个交付物的结构，返回 `{ok, checked, issues, degraded}`。

    · `ok`      —— 结构体检是否通过（**只反映结构，不代表内容正确**）
    · `checked` —— 实际执行的判据条数；**== 0 表示没校验成**（调用方必须读它）
    · `degraded`—— 校验器自身异常（此时 ok 恒 True，绝不阻断交付）
    · `issues`  —— 逐条可执行的改进提示（不是打分，是"缺什么"）

    ⚠️ `sysml` 不走这里（它有 checker.jar，见 `cards.py::_check_generated_sysml`），
       本函数对未知 kind 返回 `checked=0, degraded=True`，**不假装校验过**。
    """
    issues = []
    try:
        k = str(kind or "").strip().lower()
        fn = _VALIDATORS.get(k)
        if fn is None:
            return {"ok": True, "checked": 0, "issues": [], "degraded": True,
                    "kind": k or "-",
                    "note": "该 kind 无结构校验器（sysml 走 checker.jar；未知 kind 不判）"}
        t = _text_of(artifact)
        if not str(t or "").strip():
            # ★ V1：空内容**恒判红** —— 绝不"因为没内容所以没错"
            return {"ok": False, "checked": 1, "degraded": False, "kind": k,
                    "issues": ["**内容为空**（或只有标题/引用而无正文）："
                               "无法校验结构。kind=%s 需要可校验的正文，"
                               "不要只给 {title, ref} 这类定位信息" % k]}
        # ★ checked 必须报**真实判据条数**：三类各 4 条（report/requirement/doc）。
        #   写死 1 会让"校验通过"与"只跑了 1 条判据"不可区分（回到本仓的老坑）。
        _n = _CHECKED_COUNT.get(k, 0)
        fn(str(t), issues.append)
        return {"ok": not issues, "checked": _n, "issues": issues,
                "degraded": False, "kind": k, "chars": len(str(t))}
    except Exception as e:                                       # noqa: BLE001
        return {"ok": True, "checked": 0, "issues": [], "degraded": True,
                "kind": str(kind or "-"), "note": "校验器异常：%r" % (e,)}
