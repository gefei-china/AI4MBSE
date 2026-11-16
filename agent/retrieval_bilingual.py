# -*- coding: utf-8 -*-
"""中英术语桥接：为中文查询生成一路英文检索查询（纯函数，零 IO、零 LLM）。

## 为什么需要它（实测驱动，不是推测）

跨语言检索本体是好的：`Qwen3-Embedding-0.6B` 中英同义句对余弦 0.827、
错配对 0.335（差距 0.49），纯中文查询命中英文文档 recall@1=0.50 / recall@5=0.917
（`tools/eval/eval_xling.py`，12 条人工核对用例）。

但**术语密集的短查询**仍会漏。实测反例（2026-11-16）：

| 查询 | 结果 |
|---|---|
| `端口和连接器在交互视图里怎么用` | ✗ doc 799（含 port/connector/interconnection view）**未命中**，top1 是中文文档 817 |
| `How are ports and connectors used in an interconnection view` | ✓ doc 799 **rank 1，12 条** |
| `端口 port 和连接器 connector 在交互视图 interconnection view 里怎么用` | ✗ **仍 miss** |

⇒ 两条实测结论，都很反直觉，必须记住：
1. **把英文词混进中文 query 会更差**（第三行）—— 混合串的向量落在两种语言之间，
   既不贴近中文语料也不贴近英文语料，两路都变弱。
2. **纯英文查询是最好的**（第二行）—— 所以正确做法是**独立跑第二路**，
   而不是改写原查询。

## 本模块的边界

- 只用**固定领域词表**做映射（MBSE 术语集有界），不调翻译模型、不调 LLM；
- 覆盖率有限是刻意的：**宁少不错**。命中术语 < `min_terms` 就返回 ""
  （不生成第二路），避免用 1 个偶然命中的词拼出一路纯噪声查询；
- 词表命中按「长词优先」，并且**长中文词已覆盖的短中文词不再单独输出**
  （`交互视图` 已产出 `interconnection view` 时，不再额外吐一个 `view`）；
- 纯英文查询返回 ""（它本已是最优形态，再扩只会引入中文噪声）。
"""

from __future__ import annotations

import re

__all__ = ["expand_to_english", "bilingual_terms", "MIN_TERMS"]

#: 至少命中这么多个术语才生成英文查询（低于此返回 ""）。
MIN_TERMS = 2

#: (英文正则, 中文别名列表, 英文术语)。顺序 = 优先级，**长词必须在短词之前**。
_TERMS = [
    (r"\bpart definition\b|\bpart def\b",
     ["部件定义", "零件定义", "块定义", "部件"], "part definition"),
    (r"\binterconnection view\b|\binterconnection\b",
     ["交互视图", "内部块图"], "interconnection view"),
    (r"\bstate machine\b", ["状态机"], "state machine"),
    (r"\bsemantic library\b", ["语义库"], "semantic library"),
    (r"\buse case\b", ["用例"], "use case"),
    (r"\baction flow\b|\bactivity diagram\b", ["活动图"], "action flow"),
    (r"\bview usage\b|\bviewpoint\b", ["视图使用", "视点"], "view usage"),
    (r"\bsingle thought\b|\bconjunction\b", ["单一思想"], "single thought"),
    (r"\brequirement\b", ["需求"], "requirement"),
    (r"\bverif", ["验证", "可验证"], "verification"),
    (r"\btraceab", ["可追溯", "追溯"], "traceability"),
    (r"\bunambiguous|\bambigu", ["无歧义", "歧义"], "unambiguous"),
    (r"\bredefinition\b", ["重定义"], "redefinition"),
    (r"\bsubsetting\b", ["子集化", "子集"], "subsetting"),
    (r"\bspecializ", ["特化"], "specialization"),
    (r"\bclassifier\b", ["分类器"], "classifier"),
    (r"\bfeature\b", ["特征"], "feature"),
    (r"\bnamespace\b", ["命名空间"], "namespace"),
    (r"\bmultiplicit", ["多重度"], "multiplicity"),
    (r"\bconnector\b", ["连接器"], "connector"),
    (r"\bconnection\b", ["连接"], "connection"),
    (r"\bport\b", ["端口"], "port"),
    (r"\binterface\b", ["接口"], "interface"),
    (r"\battribute\b", ["属性"], "attribute"),
    (r"\bconstraint\b", ["约束"], "constraint"),
    (r"\bparameter\b|\bparametric\b", ["参数"], "parameter"),
    (r"\btransition\b", ["迁移"], "transition"),
    (r"\bguard\b", ["守护"], "guard"),
    (r"\bexpose\b|\bimport\b", ["导入", "暴露"], "expose"),
    (r"\ballocation\b", ["分配"], "allocation"),
    (r"\bmetadata\b", ["元数据"], "metadata"),
    (r"\bdependency\b", ["依赖"], "dependency"),
    (r"\bpackage\b", ["包"], "package"),
    (r"\bstate\b", ["状态"], "state"),
    (r"\baction\b", ["动作"], "action"),
    (r"\bactivity\b", ["活动"], "activity"),
    (r"\bview\b", ["视图"], "view"),
    (r"\btype\b", ["类型"], "type"),
]

_COMPILED = [(re.compile(p, re.I), aliases, en) for p, aliases, en in _TERMS]

#: 含汉字即视为中文查询
_ZH = re.compile(r"[\u4e00-\u9fa5]")


def bilingual_terms(query: str) -> list:
    """返回查询命中的英文术语（去重、按词表优先级）。

    去重规则（2026-11-16 实测修正）：
      · 同一英文术语只出一次；
      · **长中文词已覆盖的短中文词不单独输出** —— 否则 `交互视图` 产出
        `interconnection view` 之后，`视图` 又补一个 `view`，纯属噪声
        （实测该查询因此带上 5 个词，其中 3 个是「交互/连接/视图」的同义重复）。
    """
    q = str(query or "")
    if not q.strip():
        return []
    out = []
    covered = []          # 已命中的中文别名，用于抑制更短的子串
    for rx, aliases, en in _COMPILED:
        hit_zh = next((a for a in aliases if a in q), "")
        hit_en = bool(rx.search(q))
        if not (hit_zh or hit_en):
            continue
        # ⚠️ 2026-11-16 实测修正：这里原先写成「命中长词后只跳过同英文术语」，
        #    结果 `连接器` 产出 connector 后，`连接` 又补一个 connection；
        #    `交互视图` 产出 interconnection view 后，`视图` 又补一个 view
        #    ⇒ 一条 5 词的查询里 3 个是「交互/连接/视图」的同义重复，纯噪声。
        #    正确规则：短中文词被已覆盖的长中文词包含 ⇒ **整个跳过**。
        if hit_zh and any(hit_zh != c and hit_zh in c for c in covered):
            continue
        if en not in out:
            out.append(en)
            if hit_zh:
                covered.append(hit_zh)
    return out


def expand_to_english(query: str, min_terms: int = MIN_TERMS) -> str:
    """中文查询 → 英文术语查询串；不足 min_terms 个术语时返回 ""（不生成第二路）。"""
    q = str(query or "")
    if not _ZH.search(q):
        # 纯英文查询：本已是跨语言最优形态，不扩（实测混合会更差）
        return ""
    terms = bilingual_terms(q)
    if len(terms) < max(1, int(min_terms)):
        return ""
    return " ".join(terms)


def fuse_rrf(primary, secondary, k: int = 60, cap: int | None = None):
    """把两路命中按 RRF（倒数排名融合）合并，返回新列表。

    为什么用 RRF 而不是"追加"：
      · 追加会**改变既有排序**（新结果全排在后面，等于没参与竞争）；
      · RRF 让两路按各自排名公平竞争，且**不需要分数可比**
        （中英两路的 `score` 是各自 RRF 融合分，跨查询不可比，见 `rag.py` 注释）。

    纪律：
      · 按 `id`（chunk 主键）去重；缺 id 时退化为 (document_id, chunk_index)；
      · primary 里的对象**原样复用**（下游依赖其 `vec_score`/`confidence_level` 等字段），
        secondary 独有的对象直接带入（它们来自同一个 `hybrid_search`，字段同构）；
      · 不修改入参（返回新 list，元素仍是原对象引用）。
    """
    def _key(h):
        if h.get("id") is not None:
            return ("id", int(h["id"]))
        return ("dc", h.get("document_id"), h.get("chunk_index"))

    prim = list(primary or [])
    sec = list(secondary or [])
    score = {}
    obj = {}
    for rank, h in enumerate(prim, 1):
        kk = _key(h)
        score[kk] = score.get(kk, 0.0) + 1.0 / (k + rank)
        obj.setdefault(kk, h)
    for rank, h in enumerate(sec, 1):
        kk = _key(h)
        score[kk] = score.get(kk, 0.0) + 1.0 / (k + rank)
        obj.setdefault(kk, h)
    # 排序稳定性：分数降序，同分保持「primary 先出现者优先」
    order = {kk: i for i, kk in enumerate(obj.keys())}
    keys = sorted(score.keys(), key=lambda kk: (-score[kk], order[kk]))
    if cap:
        keys = keys[:int(cap)]
    return [obj[kk] for kk in keys]
