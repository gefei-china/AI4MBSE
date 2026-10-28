"""检索结果「实质相关」判定（纯函数，零 IO、零外部依赖）。

## 为什么要有这个模块（2026-10-28 实测驱动）

用户报：「让 AI 从知识库检索 `perform action` 用法，AI 说未检索到，
但知识库明显有 OMG SysML v2 文档介绍具体用法」。

真实数据：库中 **45 个切片**含 `perform action`（doc 799 = OMG SysML v2 规范），
检索链路也确实返回了 20 条正确命中（`vec_score` = 0.57~0.72，置信度「中/高」），
**但工具对外报的是「知识库未检索到与查询实质相关的内容（词重叠 0/0）」。**

旧判定在 `agent/pipeline_parts/tools.py`：

```python
q_tokens = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", q))        # ← 只抽中文
max_score = max(h["score"] ...)                                # ← RRF 融合分
weak = (not hits) or (not q_tokens) or (ratio < 0.2 and max_score < 0.5)
```

三条独立缺陷，任何一条都足以让检索「看起来失效」：

1. **`not q_tokens`**：查询里没有 2-4 字中文 ⇒ 直接判弱。
   纯英文技术术语查询（`perform action usage`）**必然**命中这一条。
2. **`ratio < 0.2`**：`q_tokens` 只取中文，而命中的是**英文规范原文**，
   英文内容里不可能有中文 token ⇒ overlap 恒为 0 ⇒ 中文查英文文档**必然**判弱。
3. **`max_score < 0.5`**：`score` 是 RRF 融合分 `1/(k+rank)`（k=60，两路相加），
   **理论最大值 ≈ 2/61 = 0.033**，永远达不到 0.5 ⇒ 该条件**恒真**、等于没写。

⇒ 实际生效的判据退化成：「查询没有中文 token，或中英文夹生」就判弱。
   这正是「库里明明有、检索却说没有」的机制。

## 本模块的口径

**强证据优先**：只要任一条成立即判「相关」，不再看词重叠。

| 证据 | 阈值 | 与谁同源 |
|---|---|---|
| 置信度「高」 | `confidence_level == "高"` | `knowledge_engine` 已有定级（取向量/BM25 两路较强者） |
| 向量相似度 | `vec_score >= rag.confidence_mid`（默认 0.45） | 同一个配置键，不引入新数字 |
| 强 BM25 | `bm25_score >= 40` | `rag._low_conf_suppressed` 强命中豁免的同一口径 |
| 词重叠 | 中英文都抽，`ratio >= 0.2` | 原意保留（防拿无关实体硬凑），但不再单独决定 |

**全部不成立** ⇒ 判弱。此时才说「未检索到实质相关内容」，
并在文案里给出**可核对**的原因（各证据的真实取值），而不是只说「词重叠 0/0」。

## 为什么不用 `score`（RRF 分）做阈值

RRF 是**排名融合**，取值区间由 `k` 决定，与「相关程度」不是同一量纲。
把它当相似度阈值会得到一个恒真/恒假的条件 —— 旧代码就是这样坏的。
需要「相关程度」时用 `vec_score` / `bm25_score` / `confidence_level`。
"""

from __future__ import annotations

import re

__all__ = ["extract_tokens", "assess_relevance", "CONF_MID_DEFAULT",
           "BM25_STRONG_DEFAULT", "MIN_OVERLAP_DEFAULT"]

#: 向量相似度「中」档线。与 `rag.confidence_mid` 同源（调用方可传配置值覆盖）。
CONF_MID_DEFAULT = 0.45
#: 强 BM25 命中线。与 `rag._low_conf_suppressed` 的强命中豁免同口径。
BM25_STRONG_DEFAULT = 40.0
#: 词重叠弱相关线（原判据保留部分）。
MIN_OVERLAP_DEFAULT = 0.2

#: 中文 token：连续汉字 2-4 字（沿用原口径，避免改动既有召回特征）
_RE_ZH = re.compile(r"[\u4e00-\u9fa5]{2,4}")
#: 英文 token：字母开头、长度 >=2 的词（含下划线与内部数字，如 perform_action / v2）
_RE_EN = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
#: 英文停用词：高频虚词不携带检索意图，计入重叠只会稀释信号
_EN_STOP = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "at", "by", "for", "and", "or",
    "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
    "these", "those", "as", "with", "from", "not", "no", "do", "does", "did",
    "how", "what", "which", "when", "where", "why", "use", "using", "used",
    "can", "could", "should", "would", "will", "may", "might", "must",
})


def extract_tokens(text: str) -> set:
    """抽取用于重叠比较的 token（中英文都取）。

    规则：
      · 中文：连续汉字滑窗 2-4 字（与 `knowledge_engine` 的 bigram 切分同族）
      · 英文：小写化后长度 >=2 且不在停用词表内

    ⚠️ **必须中英文都取** —— 旧实现只取中文，导致
      「英文技术术语查询」与「中文查询命中英文文档」两种最常见场景
      的重叠率恒为 0，从而恒判弱相关。这是本轮修复的核心。
    """
    s = str(text or "")
    if not s:
        return set()
    toks = set(_RE_ZH.findall(s))
    for w in _RE_EN.findall(s):
        lw = w.lower()
        if len(lw) >= 2 and lw not in _EN_STOP:
            toks.add(lw)
    return toks


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def assess_relevance(query: str, chunk_hits,
                     *, conf_mid: float = CONF_MID_DEFAULT,
                     bm25_strong: float = BM25_STRONG_DEFAULT,
                     min_overlap: float = MIN_OVERLAP_DEFAULT) -> dict:
    """判定检索结果是否与查询「实质相关」。

    :param query: 原始查询串
    :param chunk_hits: `rag.retrieve()` 返回的 chunk_hits（dict 列表，可含
                       `confidence_level` / `vec_score` / `bm25_score` / `content`）
    :returns: ``{"weak": bool, "reason": str, "anchor": str, "evidence": dict}``

        · ``weak=True``  ⇒ 调用方应告知"未检索到实质相关内容"
        · ``anchor``     ⇒ 判**相关**时是哪条证据支撑的
                           （confidence / vector / bm25 / overlap / none）
        · ``evidence``   ⇒ 各证据的真实取值，供文案回显与事后核对

    **判据顺序（强证据优先，任一成立即判相关）**：
      ① 置信度「高」 ② `vec_score >= conf_mid` ③ `bm25_score >= bm25_strong`
      ④ 中英文词重叠 `ratio >= min_overlap`

    为什么强证据优先：`vec_score` 与 `bm25_score` 都是**内容层面的相似度证据**，
    比"字面词重叠"更能反映语义相关（尤其跨语言：中文问、英文答）。
    词重叠降为**兜底信号**，用于"分数不高但词面确实对上"的情况。
    """
    hits = list(chunk_hits or [])
    if not hits:
        return {"weak": True, "anchor": "none",
                "reason": "检索无命中（图谱与向量均未返回切片）",
                "evidence": {"hits": 0}}

    max_vec = 0.0
    max_bm25 = 0.0
    n_high = 0
    for h in hits:
        if not isinstance(h, dict):
            continue
        max_vec = max(max_vec, _f(h.get("vec_score")))
        max_bm25 = max(max_bm25, _f(h.get("bm25_score")))
        if str(h.get("confidence_level") or "") == "高":
            n_high += 1

    # ④ 词重叠（中英文都抽）
    q_tokens = extract_tokens(query)
    hit_text = " ".join(str(h.get("content") or "")[:800]
                        for h in hits if isinstance(h, dict))
    hit_tokens = extract_tokens(hit_text)
    overlap = q_tokens & hit_tokens
    ratio = (len(overlap) / len(q_tokens)) if q_tokens else 0.0

    evidence = {
        "hits": len(hits),
        "n_high_conf": n_high,
        "max_vec": round(max_vec, 4),
        "max_bm25": round(max_bm25, 2),
        "q_tokens": len(q_tokens),
        "overlap": len(overlap),
        "overlap_ratio": round(ratio, 3),
    }

    # ①②③ 强证据：任一成立即判相关
    if n_high > 0:
        return {"weak": False, "anchor": "confidence",
                "reason": "存在高置信命中", "evidence": evidence}
    if max_vec >= conf_mid:
        return {"weak": False, "anchor": "vector",
                "reason": f"向量相似度 {max_vec:.3f} ≥ {conf_mid}",
                "evidence": evidence}
    if max_bm25 >= bm25_strong:
        return {"weak": False, "anchor": "bm25",
                "reason": f"BM25 {max_bm25:.1f} ≥ {bm25_strong}",
                "evidence": evidence}
    # ④ 兜底：词面对上
    if q_tokens and ratio >= min_overlap:
        return {"weak": False, "anchor": "overlap",
                "reason": f"词重叠 {len(overlap)}/{len(q_tokens)} ≥ {min_overlap:.0%}",
                "evidence": evidence}

    # 全部不成立 ⇒ 弱相关
    if not q_tokens:
        why = "查询无可比较 token"
    else:
        why = f"词重叠 {len(overlap)}/{len(q_tokens)}"
    return {"weak": True, "anchor": "none",
            "reason": (f"{why}；最高向量 {max_vec:.3f} < {conf_mid}、"
                       f"最高 BM25 {max_bm25:.1f} < {bm25_strong}、无高置信命中"),
            "evidence": evidence}
