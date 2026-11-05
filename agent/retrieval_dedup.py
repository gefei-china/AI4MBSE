# -*- coding: utf-8 -*-
"""检索结果去重与来源名归一化（2026-11-01，P0-a）。

## 要解决的问题（实测，不是推测）

`mbse.db` 里 id=847 与 id=848 是**同一份 PDF 被导入两次**：
同名、同 `file_size=513027`、55 个 chunk **逐字完全相同（逐条比对，0 处差异）**。

但两者的 `source_doc` 形态不同 —— 一个带 document_id 前缀，一个不带：

    847 → `847_Print_面向网络总体的...技术要求.pdf`
    848 → `Print_面向网络总体的...技术要求.pdf`

后果（实测 query `自主可控 部署 数据存储`）：

    注入 5 条，文档分布 = {847_xxx: 2, xxx: 2, 热管理.md: 1}
    ⇒ 5 个槽里 **4 个是同一份物理文件**，界面看着引了 3 个文档，实际只有 2 个真实来源。

根因在 `retrieval_diversity.select_diverse`：`per_doc_max` 按 `source_doc`
**字符串**计数，两个副本名字不同 ⇒ 被当成两个独立文档，**各占 2 槽**。

## 两层修复（都不删任何数据，纯读取态变换，可一键回滚）

1. `dedupe_exact` —— **正文完全相同且来自不同 `document_id`** ⇒ 判为副本，只留第一条。
   判据刻意用"完全相等"而不是相似度：同一文档的相邻 chunk 有重叠但绝不会逐字相同
   （项目内实测相邻 chunk Jaccard 中位数 0.659），所以不会误伤真证据。

2. `normalize_doc_names` —— `{id}_名称` → `名称`，**但仅当 `名称` 也在本批命中中出现**。
   为什么判据要收窄：无条件剥前缀会把合法的 `2024_年度报告.pdf` 也改掉；
   只有"去前缀后确实存在另一份同名文档"才说明这是同一份文件的两个副本。

## 顺序
先 `dedupe_exact`（去掉重复条目），再 `normalize_doc_names`（让剩余条目的配额计数正确）。
两者都返回 `(hits, stats)`，`stats` 供 `context_audit` 观测 —— **不做任何静默处理**。
"""

from __future__ import annotations

import re

_PREFIX_RE = re.compile(r"^(\d{1,8})_(.+)$")
_WS_RE = re.compile(r"\s+")

# 正文短于此长度不参与去重：短片段相同更可能是套话/标题，删了会丢信息
_MIN_DEDUP_CHARS = 80


def _text_key(hit: dict) -> str:
    """正文归一化键（去掉所有空白后逐字比较）。"""
    return _WS_RE.sub("", str(hit.get("content") or ""))


def _doc_key(hit: dict) -> str:
    """文档身份键：优先 document_id，缺失时回落到 source_doc。"""
    v = hit.get("document_id")
    if v not in (None, ""):
        return "id:%s" % v
    return "doc:%s" % str(hit.get("source_doc") or "").strip()


def dedupe_exact(hits, min_chars: int = _MIN_DEDUP_CHARS) -> tuple:
    """去掉**跨文档**的完全重复副本。返回 `(kept, stats)`。

    只删"不同 document_id 但正文逐字相同"的条目：
      · 同文档内正文相同（文档里真有两处一样的话）→ **保留**，那是两个不同位置；
      · 正文过短（< `min_chars`）→ **保留**，避免把套话类短片段误合并。
    """
    hits = list(hits or [])
    stats = {"dropped": 0, "kept": len(hits)}
    if len(hits) < 2:
        return hits, stats

    seen = {}          # 正文键 → 文档键
    kept = []
    for h in hits:
        txt = _text_key(h)
        if len(txt) < max(0, int(min_chars or 0)):
            kept.append(h)
            continue
        doc = _doc_key(h)
        prev_doc = seen.get(txt)
        if prev_doc is None:
            seen[txt] = doc
            kept.append(h)
        elif prev_doc == doc:
            # 同一文档内正文相同 ⇒ 是两个不同 chunk 位置，都是证据，保留
            kept.append(h)
        else:
            # 跨文档正文逐字相同 ⇒ 副本，丢弃
            stats["dropped"] += 1
    stats["kept"] = len(kept)
    return kept, stats


def normalize_doc_names(hits) -> tuple:
    """把 `{document_id}_名称` 归一化为 `名称`。返回 `(hits, stats)`。

    判据（零误伤的硬条件）：**前缀数字必须等于该条目的 `document_id`**。
    这直接证明前缀是"建库时加的 document_id"，而不是文件名的一部分：
      · `847_Print_xxx.pdf` 且 document_id=847 ⇒ 剥 ✅
      · `2024_年度报告.pdf` 且 document_id=900 ⇒ 前缀 2024 ≠ 900 ⇒ 不剥 ✅

    ⚠️ 不要改回"去前缀后的名字也在本批命中里才剥"那种写法 ——
       第一步 `dedupe_exact` 已经把副本删掉了，本批里根本不存在对照物，
       该判据会让归一化**永远不生效**（实测踩过：去重后 `847_` 前缀仍然显示）。
    """
    hits = list(hits or [])
    stats = {"renamed": 0}
    out = []
    for h in hits:
        s = str(h.get("source_doc") or "").strip()
        m = _PREFIX_RE.match(s)
        if m:
            did = h.get("document_id")
            if did is not None and str(did).strip() == m.group(1):
                h = dict(h)
                h["source_doc"] = m.group(2)
                stats["renamed"] += 1
        out.append(h)
    return out, stats


def dedupe_and_normalize(hits, min_chars: int = _MIN_DEDUP_CHARS) -> tuple:
    """组合入口：先去重复副本，再归一化来源名。返回 `(hits, stats)`。

    顺序说明：去重在前（按 `document_id` 判定，与名字无关），归一化在后
    （不依赖本批对照物），因此两步互不依赖。
    """
    kept, s1 = dedupe_exact(hits, min_chars=min_chars)
    kept, s2 = normalize_doc_names(kept)
    return kept, {
        "dup_dropped": s1.get("dropped", 0),
        "doc_renamed": s2.get("renamed", 0),
        "input": s1.get("kept", 0) + s1.get("dropped", 0),
        "output": len(kept),
    }
