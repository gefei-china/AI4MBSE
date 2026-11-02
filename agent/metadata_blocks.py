# -*- coding: utf-8 -*-
"""元数据块识别：把"版权页 / 封面 / 签批表 / DOI 抬头 / 目录"从排序里摘出去。

══════════════════════════════════════════════════════════════════════
为什么要它（实测证据，2026-11-02）
══════════════════════════════════════════════════════════════════════
块级评测暴露出一类"占着名次但不可能回答任何问题"的块：

    Q「SysML v2 的 binding 和 succession 是什么」
      rank2 = [818:0] "> 所属方法论：… > 适用范围：… > 更新日期：2026-04-18---"   ← 纯文档头
      rank3 = [817:0] 同上（另一份文档的文档头）
    Q「验收交付要求交付哪些文档」
      rank4 = [847:1] "编制单位 网络系统总体设计室 会 签 综合办公室…"          ← 签批表

它们之所以能排上来，是因为**包含了文档标题/编号里的关键词**（"运行视角""视图""技术要求书"），
而查询里的领域词恰好也在标题里 —— 这就是之前记的"标题词稀释"。

后果：`inject_k=5` 的 5 个槽里被这类块占掉 1~2 个，真正承载答案的块被挤出窗口。

══════════════════════════════════════════════════════════════════════
判定原则：**宁可漏判，不可误杀**
══════════════════════════════════════════════════════════════════════
误杀一个能回答问题的块，比漏掉一个元数据块严重得多（前者让系统变哑，后者只是少占一个名次）。
所以规则全部是"强特征组合"，且：

  ① 单条命中即可判定的，只用**几乎不可能出现在正文里的**标记
     （`中国星网受控信息`、`All rights reserved`、`所属方法论：`）；
  ② 需要组合判定的，要求**两个以上特征同时出现**（如 `文档编号` + `版本号` + `编制日期`）；
  ③ **长度上限**：超过 `_MAX_LEN` 的块不判（长块里即便有版权句，也必然有正文）；
  ④ 调用方 `filter_metadata()` 有 `min_keep` 保护：过滤后不足 N 条就**放弃过滤**，
     绝不让检索结果变空。

已知局限：只覆盖中文技术文档与英文标准文档的常见形态（版权页、封面、签批表、目录）。
若换语料出现新的元数据形态，**应先补规则再开开关**，不要用"过滤掉短的"这类泛化规则凑。

══════════════════════════════════════════════════════════════════════
第一版踩的坑（必须记下来，否则改回去还会犯）
══════════════════════════════════════════════════════════════════════
第一版只要"出现 `all rights reserved` 或 `copyright ©` 且命中 ≥2 个组合词"就判元数据，
结果**误杀了真内容**：

    [846:78] "verification confirmation, through the provision of objective evidence…"
             ← 这正是 ISO 29148 里 verification 的定义，因为块尾带了页脚
               "© ISO/IEC 2011 – All rights reserved" 而被判成元数据
    [799:729] "…attribute3="http://...."}Metadata or @MetadataDef1 {…"
             ← 真实语法示例，因为含 "http://...." 的连续点被目录规则命中

⇒ 两条修正，都是"看位置/密度"而不是"看有没有出现"：
  ① 页脚类标记（`all rights reserved` 等）**只在块首 100 字内才算** ——
     封面/版权页的标记在开头，正文的页脚标记在结尾；
  ② 目录规则不只看点的次数，还要求**点占字符比例 ≥ 0.2**（目录是"点为主"的块，
     正文里 URL 的 `....` 占比极低）；
  ③ 组合词规则分两档：≥3 个直接判（正文几乎不可能同时出现 3 个结构词），
     =2 个时要求块长 ≤ 300（短块才可能是纯抬头/签批表）。
"""
from __future__ import annotations

import re

#: 超过这个长度不判为元数据（长块里即使有版权句，正文也占多数）
_MAX_LEN = 700

_NORM_RE = re.compile(r"\s+")

#: 单条即可判定（这些串几乎只出现在封面/版权/文档头）
#: ⚠️ **刻意不含 `all rights reserved`**：PDF 跨页抽取会把页脚放到**下一块的开头**，
#:    于是正文块也"块首含 all rights reserved"（实测 846:232/240/247/314/354 全部命中）。
#:    按块首判会把真内容整片误杀 —— 这是第一版最严重的问题。
_ALONE = (
    "中国星网受控信息",
    "important notice this document is a copyrighted",
    "copyright protected document",
    "> 所属方法论：",
)

#: 需要组合判定的结构词（单个可能在正文里被引用）
_COMBO = (
    "文档编号",
    "版本号",
    "编制日期",
    "文件编号",
    "版 本 号",
    "工程阶段",
    "编制单位",
    "会 签",
    "编 写",
    "校 对",
    "copyright ©",
    "isbn",
)

#: 目录特征：连续 4 个以上的点，且**点占字符比例**达标（正文里的 URL/省略号占比极低）
_LEADER_RE = re.compile(r"\.{4,}")
_LEADER_RATIO = 0.20

#: 页脚类标记只在块首这么多个字符内才算（正文的页脚在结尾）
_HEAD_CHARS = 100


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", (text or "")).strip()


def is_metadata(content: str) -> bool:
    """判断一个块是否"不可能回答业务问题"的元数据块。"""
    t = _norm(content)
    if not t or len(t) > _MAX_LEN:
        return False
    low = t.lower()
    head = low[:_HEAD_CHARS]

    # ① 单条强特征 —— **只看块首**（封面/文档头的标记在开头，正文页脚在结尾）
    if any(s in head for s in _ALONE):
        return True

    # ② 结构词密度：≥3 个直接判；=2 个时要求短块
    hits = sum(1 for s in _COMBO if s in low)
    if hits >= 3:
        return True
    if hits >= 2 and len(t) <= 300:
        return True

    # ③ 目录：点的次数 + **点占字符比例**（防止把 URL 里的 "...." 当目录）
    dots = _LEADER_RE.findall(t)
    if len(dots) >= 2 and (sum(len(d) for d in dots) / max(1, len(t))) >= _LEADER_RATIO:
        return True

    # ④ 引用块式的文档头（以 > 开头 + 极短 + 含日期说明）
    if t.startswith(">") and len(t) <= 120 and ("更新日期" in t or "整理日期" in t):
        return True

    return False


def filter_metadata(hits, min_keep: int = 3) -> tuple:
    """摘掉元数据块。返回 `(kept, stats)`。

    **保护**：若过滤后剩余 < `min_keep`，则**原样返回**（宁可留元数据，也不让结果变空）。
    这条不是防御性代码，是必须的：某些文档只有 1~2 个分块时，
    其唯一分块可能就是文档头（如 `_short_upload.txt`），过滤掉会让该文档彻底检索不到。
    """
    hits = list(hits or [])
    stats = {"total": len(hits), "dropped": 0, "kept": len(hits), "skipped": False}
    if not hits:
        return hits, stats

    kept = [h for h in hits if not is_metadata(h.get("content") or "")]
    dropped = len(hits) - len(kept)
    if dropped == 0:
        stats["dropped_reason"] = "no_metadata"
        return hits, stats
    if len(kept) < max(1, int(min_keep)):
        stats.update({"dropped": 0, "kept": len(hits), "skipped": True,
                      "would_drop": dropped})
        return hits, stats

    stats.update({"dropped": dropped, "kept": len(kept)})
    return kept, stats
