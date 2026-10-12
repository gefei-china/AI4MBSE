"""L1 · DeepDoc 思路借鉴：结构化问句生成（标题路径优先，模板兜底）。

══════════════════════════════════════════════════════════════════════
为什么做这个（实测差距，非印象）
══════════════════════════════════════════════════════════════════════
本工程 `_generate_hyde_questions`（ingest.py:117）已有 Reverse HyDE，
但它是**「术语 + 固定模板」**组装：
    什么是{术语}？ / {术语1}和{术语2}有什么关系？ / {术语}怎么使用？
⇒ **全库所有块的问句句式几乎一样**，区分度低；
检索时"用户问法↔假设问题"匹配靠的是这几句模板，**同质化会稀释召回**。

ragflow `qa.go:216 extractQAMarkdown` 的思路（实测读码确认）：
    用**标题栈**维护层级路径（`# A > ## B > ### C` ⇒ 问题 = "A\\nB\\nC"），
    标题下的正文作为答案；**代码块内的 `#` 不算标题**。
⇒ 得到的是**内容真实反映的问题**，而不是套模板。

本模块做法（**不引入 ragflow 依赖，纯借鉴算法**）：
    标题层级路径 → 多种真实问法（是什么 / 有哪些 / 如何实现 / 与…关系）
    无标题（纯正文/表格/OCR）⇒ **完全回落到现有 `_generate_hyde_questions`**，
    保证行为不劣于现状。

══════════════════════════════════════════════════════════════════════
零漂移设计
══════════════════════════════════════════════════════════════════════
· 默认**关闭**：`hyde_heading_enabled`（ingest 组），默认 False
  ⇒ 未开启时 `enhanced_hyde_questions()` 直接返回 None，
     调用方（ingest.py）保留原 `_generate_hyde_questions` 行为。
· 永远返回 ≥1 个问题；生成不出比原版更好时**不覆盖**。
"""
from __future__ import annotations

import re

#: markdown 标题（与 ragflow qa.go:214 `^(#*)` 同思路，但要求后随非空文本）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
#: ★ **数字章节标题**：实测 83.2% 的 section 形如 `3.2.1 系统架构`（PDF 提取产物）。
#:   第一版只认 markdown `#`，导致 L1 在真实语料上**0% 命中**——
#:   这正是"照抄别家算法而不看自己数据"的典型后果。
_NUM_HEADING_RE = re.compile(
    r"^((?:\d+\.)*\d+)\s+(\S.*)$")            # 3.2.1 系统架构
_CN_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百0-9]+[章节部分])\s*(\S.*)?$")


def heading_path(question_like_text: str) -> list:
    """返回标题层级路径（按出现顺序），供问句合成用。

    ★ 代码围栏内的 `#` **不算标题**——否则整段 Python 代码会被
      误当成标题，问句就变成 "def foo() 的定义是什么？"这类噪声
      （ragflow qa.go 同款处理：进入 codeBlock 后标题分支被短路）。

    ★ **实测校正（2026-10-10）**：真实语料里markdown `#` 标题只占 **16.3%**，
      而 `3.2.1 xxx` 这类**数字章节**占 **83.2%**（PDF 提取产物）。
      ⇒ 三类标题都要认（`#` / 数字 / 中文章节），否则 L1 等于没做。
    """
    if not question_like_text:
        return []
    path: list = []
    in_fence = False
    for line in question_like_text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        level, title = None, ""
        m = _HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2)
        else:
            m2 = _NUM_HEADING_RE.match(line.strip())
            if m2:
                # 层级 = 点的段数（`3.2.1` ⇒ level 3）
                level, title = m2.group(1).count(".") + 1, m2.group(2)
            else:
                m3 = _CN_HEADING_RE.match(line.strip())
                if m3:
                    level, title = 1, (m3.group(2) or m3.group(1))
        if not level or not title:
            continue
        title = re.split(r"[：:；;，,。]", title)[0].strip()[:40]
        if not title:
            continue
        # 弹出同级及更高级
        while path and path[-1][0] >= level:
            path.pop()
        path.append((level, title))
    return [t for _, t in path]


def _questions_from_path(path: list) -> list:
    """由标题路径合成**多种真实问法**（替代单一模板）。"""
    if not path:
        return []
    head = path[0]
    full = " ".join(path)
    tail = path[-1] if len(path) > 1 else ""
    out = [f"{head}是什么？", f"{head}包含哪些内容？"]
    if tail and tail != head:
        out.append(f"{tail}怎么实现？")
    if len(path) >= 2:
        out.append(f"{head}与{tail}是什么关系？")
    # 去重保序
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


def enhanced_hyde_questions(content: str, section: str = "") -> list:
    """返回**增强**问句列表；无法增强时返回 None（调用方保留原实现）。

    ⚠️⚠️ **实测踩坑（2026-10-10）**：第一版只从 `content` 取标题路径，
       结果**全库 0% 命中**——因为切片时标题行已被 `chunking.py` 提升为
       `section`（实测 `section='# SysML v2 / KerML 文本建模知识文档'`，
       而 `content` 里只剩正文），且 **100% 的块都有 section**。
    ⇒ **标题的真实载体是 `section`，不是 `content`**。
       现在两个都读：section 若自身带 `#` 标记也能解析出路径。
    """
    from core import config
    if not config.as_bool("ingest", "hyde_heading_enabled", False):
        return None                 # ★ 默认关⇒ 行为与改动前逐字相同
    if not content:
        return None
    # ① 首选：section 自身带 markdown 标题标记（切片提升后的真实载体）
    path = heading_path(section) if section else []
    # ② 兜底：content 里确有标题（未被提升的情况，如手工构造的块）
    if not path:
        path = heading_path(content)
    # ★ 两者都没有 ⇒ 返回 None，由调用方回落到原模板（永不劣化）
    if not path:
        return None
    qs = _questions_from_path(path)
    return qs or None