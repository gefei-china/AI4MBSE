# -*- coding: utf-8 -*-
"""零召回的**成因分型**（shadow，2026-11-08）。

## 它解决的是一个「不可区分」问题
`query_trace` 6530 条里有 473 条零命中（7.24%）。这些零命中的**成因完全不同**，
但在指标上长得一模一样：

    今天北京天气怎么样 / 红烧肉怎么做   → 本来就不该检索（闲聊/实时/通用知识）
    宽带载荷 / 卫星太阳能板的清洗周期   → 知识库确实没有（0 chunk）
    错误 3.1：混淆 part 和 ref part     → **库里有 17 个 chunk，只是没召回**

第三类最危险：把它算成"没有知识"会得到**一个合理的产品解释**，
于是再没人去查召回是不是坏了。这与「0 违规」与「门禁没跑」不可区分是同族问题。

## 为什么必须四态（三态不够）
"词在库里存在"并不等于"该召回"。实测反例：
    `如何用 Python 写一个网页爬虫` → `Python` 在 OMG 规范里出现 17 次
    （原文是 "The basic notation for successions…"，只是顺带提及）
`query_keywords.py` 里也记了同一个坑：用 `Python` 重试会把域外问题救成"有答案"
（gate 拒答准确率 1.000 → 0.800）。

⇒ 判据必须区分**证据强度**：
    `recall_miss`     连续短语在库内且文档频次低  ← 强证据（ref part 命中 17 个 chunk）
    `grounding_miss`  词在本体/词表里，但没有正文依据
    `knowledge_gap`   短语与词都不在库
    `indeterminate`   只有**孤立单词**在库（可能是顺带提及，不足以断言"召回失败"）

第四态是**诚实**：它在说"目前分不出来"。硬把它归进前三态，
就等于用一个猜出来的结论去掩盖我们还没有的区分能力。

## 这个模块**不参与任何决策**
它只产出一个 dict，被写进 `query_trace.detail` 供事后统计。
- 不阻断生成、不丢弃回答、不触发反问、不改 route、不改 hits。
- 门禁 `verify_retrieval_gap_shadow.py` 会**源码级**断言这一点
  （判据反向写：一旦有人把 kind 接进 if 分支，门禁必须变红）。

## 开关
`rag.gap_classify_shadow`（默认 True）。关掉只影响打标，不影响检索。
只在 `hit_count == 0` 时被调用，正常情况下开销可忽略（全库零命中率 7.24%）。
"""
from __future__ import annotations

import re

# ── 四态 ────────────────────────────────────────────────────────────────
KIND_OK = "ok"                      # 有命中，不需要分型
KIND_RECALL_MISS = "recall_miss"    # 库里有、没召回 → 该查检索，不该补知识
KIND_GROUNDING_MISS = "grounding_miss"  # 本体/词表认识它，但无正文依据
KIND_GAP = "knowledge_gap"          # 库里确实没有
KIND_INDETERMINATE = "indeterminate"  # 只有孤立单词命中，证据不足

_ALL_KINDS = (KIND_OK, KIND_RECALL_MISS, KIND_GROUNDING_MISS, KIND_GAP, KIND_INDETERMINATE)

# 短语/词的最大探测数：零命中是窄条件，但仍要给个上界，
# 否则长问句会退化成十几次全表 LIKE。
#
# ⚠️ **短语与词必须各有独立配额**（实测踩过两次）：原先共用 5 个名额时，
#    `错误 3.1：混淆 part 和 ref part` 切出 6 个 2-gram，`ref part` 排第 6，
#    被名额截断 → 明明库里有 17 个 chunk，却判成 indeterminate。
#    短语探针才是 recall_miss 的强证据来源，不该被词探针挤掉。
_MAX_PHRASE_PROBES = 8
_MAX_WORD_PROBES = 6
# 文档频次上限：超过即视为"泛词"，不能作为 recall_miss 的证据。
# 50 的依据：真库 7179 个 chunk，`ref part` = 17、`part` 上千。
# 这个数只决定"证据算不算强"，不决定"要不要回答"。
_DF_CAP = 50
# 单次 LIKE 的扫描上界（比 COUNT(*) 全表快，命中够多就早停）
_SCAN_HINT = 200


def _segments(query: str) -> list:
    """按「非字母数字/非汉字」切段，保序去空。"""
    return [s for s in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", query or "") if s]


def _phrases(query: str, max_n: int = 3) -> list:
    """连续短语（**2 段优先**，再 3 段），保序去重。

    ## 为什么短短语优先（实测踩过）
    原先按"越长越靠前"排，`错误 3.1：混淆 part 和 ref part` 切出 7 段后
    先探 5 个 3-gram（`错误 3.1 混淆` / `part 和 ref` …），配额用尽，
    真正能命中的 `ref part`（第 10 个候选）**根本没被探到** → 误判成 indeterminate。

    短组合更可能真的出现在正文里，而"两词毗邻"的顺带提及概率仍远低于单词，
    所以 2-gram 是 recall_miss 最合适的证据粒度，3-gram 只作补充。

    中文长句切不开（整句就是一段）时返回空列表，自动落到单词探针。
    """
    segs = _segments(query)
    out = []
    for n in range(2, min(max_n, len(segs)) + 1):
        for i in range(len(segs) - n + 1):
            p = " ".join(segs[i:i + n])
            if p and p not in out:
                out.append(p)
    return out


def _words(query: str, max_n: int = 8) -> list:
    """单词候选：英文词 + 长度≥2 的整段（不含中文滑窗碎片）。

    刻意**不用** `GraphRAG._tokens` 的中文 2-4 字滑窗：
    那会把「错误 3.1：混淆 part…」刷成十几个碎片，探测成本翻几倍，
    而碎片（如「误混」）在库里命中反而制造假证据。
    """
    out = []
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{1,}", (query or "").lower()):
        if w not in out:
            out.append(w)
    for s in _segments(query):
        if len(s) >= 2 and s not in out:
            out.append(s)
    return out[:max_n]


def _df(conn, needle: str, cap: int = _DF_CAP + 1) -> int:
    """needle 在 document_chunks 正文里的文档频次（上限封顶，早停）。

    失败返回 0 —— 分型是**旁路观测**，任何异常都不该冒泡到检索主链。
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM document_chunks "
            "WHERE content LIKE ? LIMIT ?)",
            ("%" + needle + "%", max(cap, _SCAN_HINT)),
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _ontology_hit(conn, word: str) -> str:
    """词是否被本体/词表"认识"（不查正文，只查类型与术语）。

    两处真源：`ontology_types.name`（SysML 类型）、`intent_taxonomy`（意图词表）。
    命中说明"系统有这个概念"，但没有正文依据 ⇒ grounding_miss。
    """
    try:
        r = conn.execute(
            "SELECT name FROM ontology_types WHERE LOWER(name)=? LIMIT 1", (word.lower(),)
        ).fetchone()
        if r:
            return "ontology_types:%s" % (r[0],)
    except Exception:
        pass
    try:
        r = conn.execute(
            "SELECT intent FROM intent_taxonomy WHERE LOWER(intent)=? "
            "OR aliases LIKE ? LIMIT 1", (word.lower(), '%"' + word + '"%')
        ).fetchone()
        if r:
            return "intent_taxonomy:%s" % (r[0],)
    except Exception:
        pass
    return ""


def classify(conn, query: str, hit_count: int, *,
             max_phrase_probes: int = _MAX_PHRASE_PROBES,
             max_word_probes: int = _MAX_WORD_PROBES,
             df_cap: int = _DF_CAP) -> dict:
    """对一次检索结果做成因分型。

    参数：
      hit_count        —— 本次实际召回条数。>0 直接返回 `ok`（分型只对零命中成立）。
      max_phrase_probes / max_word_probes —— 两类探针**各自**的名额（不共用，
                         否则短语会被词挤掉，见常量处注释）。
      df_cap           —— 短语被认定为"够具体"的文档频次上限。

    返回（**只用于观测**）：
      {"kind": <四态之一>, "evidence": [{"probe":..., "df":..., "via":...}, ...],
       "checked": <探测次数>}

    绝不返回"该不该回答"这类判断 —— 那是产品层的事，不是本模块的职责。
    """
    if hit_count and hit_count > 0:
        return {"kind": KIND_OK, "evidence": [], "checked": 0}
    q = (query or "").strip()
    if not q:
        return {"kind": KIND_GAP, "evidence": [], "checked": 0}

    evidence = []
    checked = 0

    # ① 强证据：连续短语在库内且频次低 ⇒ 知识在、召回漏
    for p in _phrases(q)[:max_phrase_probes]:
        checked += 1
        df = _df(conn, p, df_cap + 1)
        if 0 < df <= df_cap:
            evidence.append({"probe": p, "df": df, "via": "phrase"})
            return {"kind": KIND_RECALL_MISS, "evidence": evidence, "checked": checked}

    # ② 本体/词表认识，但正文里找不到 ⇒ 有概念无依据
    weak = []
    for w in _words(q)[:max_word_probes]:
        checked += 1
        via = _ontology_hit(conn, w)
        if via:
            evidence.append({"probe": w, "via": via})
            return {"kind": KIND_GROUNDING_MISS, "evidence": evidence, "checked": checked}
        df = _df(conn, w, df_cap + 1)
        if df > 0:
            # 只记下来，供 indeterminate 用；**不**据此断言 recall_miss
            weak.append({"probe": w, "df": df, "via": "word"})

    # ③ 只有孤立单词在库 ⇒ 证据不足（Python 案例）
    if weak:
        return {"kind": KIND_INDETERMINATE, "evidence": weak[:3], "checked": checked}

    # ④ 短语与词都不在库
    return {"kind": KIND_GAP, "evidence": [], "checked": checked}


def is_kind(value: str) -> bool:
    """校验取值合法（门禁/统计用，防止拼写漂移出第五态）。"""
    return value in _ALL_KINDS
