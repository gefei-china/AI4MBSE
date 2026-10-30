# -*- coding: utf-8 -*-
"""检索注入多样性约束（P0-3 · 2026-10-26）—— 让 N 个注入槽真正换来 N 份信息。

## 问题（实测，生产库快照 6 个典型 query）

`agent/pipeline_parts/context.py` 注入检索结果时**只取前 `rag.inject_k`（默认 3）条**，
**无任何多样性约束**（全仓 grep `MMR|diversif|near_dup` 零命中）。实测结果：

| query | top3 文档数 | 槽间最大重叠 | 有效条数 |
|---|---|---|---|
| 如何定义 SysML v2 的活动图 | 3 | 0.21 | 2.48 |
| 需求追溯矩阵怎么构建 | 2 | 0.81 | 1.65 |
| **parameter 属性定义方式** | **1（同文档）** | 0.74 | **1.14** |
| ISO 29148 需求质量准则 | 2 | 0.47 | 1.75 |
| **block definition 端口定义** | **1（同文档）** | 0.53 | **1.57** |
| 状态机 状态 转换 定义 | 2 | 0.32 | 2.44 |

⇒ **3 个注入槽实际只换来 1.14~2.48 条信息**，且挤掉的是"换个文档交叉验证"的机会。

## 根因（语料侧，实测）

- `document_chunks` 共 **7,177 块 / 仅 14 个文档**，**top2 文档占 64.3%**
  （`OMG SysML V2.pdf` 2,606 + `KerML.pdf` 2,011）
- **相邻 chunk 字符集 Jaccard 中位数 0.659、p90 0.816**（7,144 对样本）
  ⇒ 切片重叠 90 字符（`chunk_text(size=600, overlap=90)`）导致相邻块语义高度重复

## 方案（无损去重 —— 这是关键）

借鉴 Deep Agents 的排序原则「**先无损、后有损**」：
- 卸载/去重 = 无损（数据完整保留，只是换个位置 / 剔除重复）
- 摘要 = 有损（"三个接口异常"塌缩成"接口问题已修复"）

本模块做的是**无损去重**，不是压缩：
1. **per-doc 配额**：同一 `source_doc` 最多占 `per_doc_max` 槽（3 槽时默认 ≤2）
2. **近邻合并**：同文档且 `|Δchunk_index| ≤ 2` ⇒ 合并为一条，标注 `§a-b` 区间
   （合并是**无损**的：两段内容都在，只是放在同一条里；且保留区间坐标供回溯）
3. **补位**：多样性过滤后槽位不足 ⇒ 用 RRF 池里落选的条目补齐（`rag.top_k=20` 池中还有 17 条）

## 硬约束：不得破坏引用编号对齐

`【来源n】` 编号与前端 `citations` 数组**1 基对齐**（`context.py:187` 明确）。
本模块只做**条目的筛选与合并**，**不改变编号语义**：
- 合并后的条目仍占**一个**编号位（内容里含两段，标注 `§a-b`）
- 编号顺序 = 最终注入顺序 ⇒ 与前端 citations 数组顺序一致

## 开关与回滚

`context_audit.retrieval_per_doc_max`（默认 2）/ `context_audit.retrieval_merge_neighbor`（默认 True）。
置 `per_doc_max=0`（或 ≥inject_k）即退回改动前行为 ⇒ **可一键回滚，无需改代码**。
"""

from __future__ import annotations


def _doc_of(hit: dict) -> str:
    return str(hit.get("source_doc") or "").strip()


def _idx_of(hit: dict):
    try:
        return int(hit.get("chunk_index") or 0)
    except Exception:
        return 0


def _text_of(hit: dict) -> str:
    return str(hit.get("content") or "")


def select_diverse(hits, inject_k: int = 3, per_doc_max: int = 2,
                   merge_neighbor: bool = True, neighbor_window: int = 2):
    """从 `hits` 选出 `inject_k` 条**多样性**条目。

    返回 `(selected, stats)`：
      · `selected` —— 选中的条目列表（**元素是原 dict 的浅拷贝**，多了 `merged_from` /
        `section_range` 两个标注键；不修改入参）
      · `stats` —— 诊断指标（名义条数 / 有效条数 / 同文档占槽数 / 合并次数），
        用于喂给 `context_audit` 的 `metrics` 字段 ⇒ **"名义 N 条 vs 有效 M 条"可观测**

    规则（按序执行）：
      1. per-doc 配额：同一 `source_doc` 累计命中数 > `per_doc_max` ⇒ 跳过
      2. 近邻合并：与**已选中的**同文档条目 `|Δchunk_index| ≤ neighbor_window`
         ⇒ 合并进该条目（记 `merged_from`），不占新槽
      3. 补位：若最终不足 `inject_k` 条 ⇒ 放宽 per-doc 配额再取（保证不因约束导致信息变少）
    """
    hits = list(hits or [])
    stats = {
        "candidates": len(hits),
        "nominal": min(inject_k, len(hits)),
        "merged": 0,
        "selected": 0,
        "effective": 0.0,
        "docs": 0,
        "relaxed": 0,
    }
    if not hits or inject_k <= 0:
        return [], stats

    selected = []          # 选中的条目（浅拷贝）
    doc_count = {}         # 每文档已占槽数
    used_slots = 0

    def _clone(h, **extra):
        d = dict(h)
        d.update(extra)
        return d

    for h in hits:
        if used_slots >= inject_k:
            break
        doc = _doc_of(h)
        idx = _idx_of(h)
        txt = _text_of(h)

        # ── 规则 2：近邻合并（先于配额判断，因为合并不占新槽）──
        if merge_neighbor:
            merged = False
            for s in selected:
                if _doc_of(s) != doc or not doc:
                    continue
                if abs(idx - _idx_of(s)) <= neighbor_window:
                    # ★ 合并必须**无损**：按 chunk_index 逐段留存（即便两段正文完全相同，
                    #   也要保留各段标识 —— 否则"无损去重"就变成了"有损去重"）。
                    #   判据用「已登记的 chunk_index 集合」，**不用正文相等判断**
                    #   （正文相同但 chunk 不同 = 两个不同来源位置，都是证据）。
                    #   ⚠️ 顺序要点：先取 `_seen` 再判断，最后才写回 merged_from ——
                    #   若先 append 再取 _seen，则 idx 必在集合内，正文永不追加（丢内容）。
                    _seen = set(s.get("merged_from") or [_idx_of(s)])
                    if idx not in _seen:
                        _prev = s.get("content") or ""
                        _piece = f"（续 §{idx}）" + txt
                        s["content"] = (_prev + "\n" + _piece) if _prev else txt
                    s["merged_from"] = sorted(_seen | {idx})
                    lo, hi = min(s["merged_from"]), max(s["merged_from"])
                    s["section_range"] = f"{lo}-{hi}" if lo != hi else str(lo)
                    stats["merged"] += 1
                    merged = True
                    break
            if merged:
                continue

        # ── 规则 1：per-doc 配额 ──
        if per_doc_max and per_doc_max > 0 and doc_count.get(doc, 0) >= per_doc_max:
            continue

        s = _clone(h)
        selected.append(s)
        doc_count[doc] = doc_count.get(doc, 0) + 1
        used_slots += 1

    # ── 规则 3：补位（多样性约束不应导致信息变少）──
    # ⚠️ 去重判据用 **chunk 位置**（doc + chunk_index），**不用正文相等**：
    #   同文档不同 chunk 可能正文高度相似（实测相邻 chunk Jaccard 中位数 0.659），
    #   按正文去重会把它们误判为同一条 ⇒ 候选不足时**少返回条目**（实测丢 1 条）。
    if len(selected) < inject_k:
        _picked = {(_doc_of(s), _idx_of(s)) for s in selected}
        for h in hits:
            if len(selected) >= inject_k:
                break
            _pos = (_doc_of(h), _idx_of(h))
            if _pos in _picked:
                continue
            doc = _doc_of(h)
            if doc_count.get(doc, 0) >= max(1, per_doc_max or 1):
                # 配额已满但槽位不足 ⇒ 放宽（记 relaxed 供观测）
                stats["relaxed"] += 1
            s = _clone(h, _relaxed=True)
            selected.append(s)
            doc_count[doc] = doc_count.get(doc, 0) + 1
            _picked.add(_pos)
            used_slots += 1

    # ── 诊断指标：有效条数（按 1 - 两两重叠 折算，量化"真实信息量"）──
    # ⚠️ 口径细化：**跨文档**条目不按重叠扣减。
    #   理由：不同文档即使字符集相似（如两本标准都讲"需求"），也是**两份独立证据**，
    #   对模型的增益是真实的；而同文档近邻 chunk 的重复才是真冗余。
    #   若不区分，会出现"换到跨文档条目 ⇒ 指标反降"的假象（实测 block definition
    #   端口定义：1.57→1.50，但实际文档数 1→2，多样性是提升的）。
    eff = float(len(selected))
    texts = [_text_of(s) for s in selected]
    docs = [_doc_of(s) for s in selected]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if docs[i] and docs[j] and docs[i] == docs[j]:
                eff -= _jaccard(texts[i], texts[j])
    stats["selected"] = len(selected)
    stats["effective"] = round(max(1.0, eff), 2)
    stats["docs"] = len({_doc_of(s) for s in selected if _doc_of(s)})
    return selected, stats


def _jaccard(a: str, b: str) -> float:
    """字符集 Jaccard（与调研口径一致：相邻 chunk 实测中位数 0.659、p90 0.816）。"""
    sa, sb = set(a or ""), set(b or "")
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))
