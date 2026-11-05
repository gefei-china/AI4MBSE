# -*- coding: utf-8 -*-
"""查询侧关键词抽取（2026-11-01，P1-a）：给「零召回」兜底用。

## 为什么需要
`embedding` 服务不可用时会降级到 bigram-tf，此时**长问句极易零召回**（实测）：

    `信息安全有哪些要求`      → 0 条
    `信息安全`               → 2 条   ← 正文是技术要求书 7.4 的信息安全条款，内容正确
    `这个系统对可靠性有什么要求吗` → 0 条

原因是 bigram 只做 2-gram 重叠，问句里的"有哪些""怎么""吗"等成分把重叠比例稀释掉。
⇒ 在**零召回**这一个窄条件下，用"高区分度关键词"重查一次，是把能力救回来的最直接手段。

## 为什么判据这么严（每条都是踩过的坑）

1. **词性白名单**：只收实词（名词/动名词/名形词/地名/机构名/英文/简称）。
   - 必须排除 `l`（习用语）—— 实测 `这个系统对可靠性有什么要求吗` 被 jieba 切成
     `系统对/l`，用它会召回 **20 条噪声**（"系统"泛匹配），是本次最大的坑。
   - 排除 `r`（代词）/`p`（介词）/`y`（语气词）/`t`（时间词）——
     它们不承载领域信息，`今天` 这类词重试只会把域外问题伪装成"有结果"。
2. **长度 ≥ 3**：2 字泛词是噪声放大器。实测 `要求` 参与拼接后
   （`系统对 可靠性 要求`）召回 19 条，而单词 `可靠性` 召回 0 条 —— 差异全在"要求"。
3. **只在零召回时调用**：实测 `需求应该怎么写才规范` 原本有 13 条命中，
   用关键词重试反而变 0 条。**有命中时绝不重试**（这一点由调用方保证）。
4. **不做正文包含校验**：一开始想用"命中正文必须含关键词"当闸门，实测会**误杀真命中** ——
   `信息安全` 召回的 7.4 节条款正文里并没有连续的"信息安全"四个字（它写的是
   "网络会话加密/弱口令/组件安全"），但内容完全正确。故放弃该闸门，
   改为依赖下游既有的 `_filter_low_conf` + 域外拒答判据来兜质量。
"""

from __future__ import annotations

import re
from functools import lru_cache

# 词性白名单：实词。刻意不含 l/r/p/y/t/u/d/m/q/c/f/s/b/z 等
# ⚠️ 也**不含 eng**：纯英文词在技术文档里"顺带提及"太普遍，
#   单独用它重试必然产生字面巧合。实测反例（gate 拒答准确率 1.000 → 0.800）：
#   `如何用 Python 写一个网页爬虫` → 抽到 `Python` → 召回 **17 条** SysML 规范里
#   含 "Python" 字样的段落（正文是 "The basic notation for successions…"），
#   relevance 高达 0.34 ⇒ 越过域外拒答线，把一个域外问题救成了"有答案"。
#   代价是 `SysML`/`MBSE` 这类真领域英文词也不再单独触发重试 —— 这是**有意的取舍**：
#   误放行会直接产生幻觉答案，而少救一次只是退回"拒答"这个安全默认。
_KEEP_POS = frozenset("n nz vn v an ns nt nrt j i".split())

_MIN_TERM_CHARS = 3   # 中文按字符数；英文缩写 ≥3 字母（MBSE/SysML/KerML 均满足）
_MAX_TERMS = 4        # 拼接重试时的词数上限，过多会让 bigram 再次被稀释

# 候选词必须含中文（双保险：jieba 偶尔把英文词标成 n，光看词性拦不住）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@lru_cache(maxsize=256)
def high_signal_terms(query: str) -> tuple:
    """抽取高区分度词（保序去重，最多 `_MAX_TERMS` 个）。

    jieba 不可用时返回空元组 —— 调用方据此跳过重试（**退化为原行为，不报错**）。
    返回 tuple 而非 list，是为了能进 `lru_cache`；调用方按需转 list。
    """
    q = (query or "").strip()
    if not q:
        return ()
    try:
        import jieba.posseg as pseg
    except Exception:
        return ()

    out = []
    try:
        for word, pos in pseg.cut(q):
            w = (word or "").strip()
            if len(w) < _MIN_TERM_CHARS:
                continue
            if pos not in _KEEP_POS:
                continue
            if not _CJK_RE.search(w):
                # 纯英文/数字词不参与：技术文档里到处都有，匹配上也只是"顺带提及"
                continue
            if w not in out:
                out.append(w)
            if len(out) >= _MAX_TERMS:
                break
    except Exception:
        return ()
    return tuple(out)


def retry_candidates(query: str) -> list:
    """生成重试用的候选查询串（保序去重）。

    顺序：① 全部高区分度词拼接（保留共现约束）→ ② 逐个词单独试（最坏也能捞回一些）。
    单一词时两者等价，自动去重。
    """
    terms = list(high_signal_terms(query))
    if not terms:
        return []
    cands = [" ".join(terms)] + (terms if len(terms) > 1 else [])
    seen = set()
    out = []
    for c in cands:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out
