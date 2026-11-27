# -*- coding: utf-8 -*-
"""verify_retrieval_quality —— 锁住检索侧三条修复（2026-11-27）。

## 三个被证实的坏形态（conv 1000000246 实测）

| 形态 | 实测 | 本门禁对应判据 |
|---|---|---|
| 整篇提示词（2665 字）当检索串 | 42.7s、19 条命中全是同两个文档相邻块、领域词 0 命中 | R1/R2 |
| 低分命中放行 | 19 条 relevance 低至 0.197 一条未丢（`all_low=false` ⇒ 旧逻辑根本不过滤） | R4 |
| 同一 query 19s 内重复检索 | 10:21:29 与 10:21:48 两次 hits/top 完全相同 | R3/R5 |

判据全部**离线确定性**：不依赖外部 embedding 服务、不改生产数据。
R2 是结构断言（压缩必须发生在拼槽位/附件**之前**），因为顺序反了会把串尾的
槽位实体与附件摘要截掉 —— 那是比"慢"更严重的静默丢信息。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_fails, _passes = [], []


def chk(tag, ok, detail=""):
    (_passes if ok else _fails).append(tag)
    print(("  PASS " if ok else "  FAIL ") + tag + ("  | " + detail if detail else ""))
    return ok


# ── R1 压缩函数 ────────────────────────────────────────────────────────────

def r1_compact():
    from agent.rag import compact_query
    long_text = ("我需要你帮我完成低空预警雷达的系统建模。背景：该雷达用于对低空小型目标进行"
                 "搜索、检测、跟踪与识别。") * 30
    out = compact_query(long_text, 300)
    chk("R1.1 长文本被压到上限内", 0 < len(out) <= 300, "%d 字 → %d 字" % (len(long_text), len(out)))
    chk("R1.2 保留首句意图（不是随机截断）", out.startswith("我需要你帮我完成低空预警雷达的系统建模"),
        out[:40])
    short = "SysML 活动视图语法"
    chk("R1.3 短输入原样返回", compact_query(short, 300) == short, short)
    chk("R1.4 limit=0 不压缩", compact_query(long_text, 0) == long_text.strip(), "关闭即旧行为")
    # 多段文本：每段取首句（需求文档的分节标题/首句信息密度最高）
    multi = "1. 背景：低空雷达\n2. 功能：目标搜索与航迹跟踪\n3. 约束：复杂电磁环境"
    out2 = compact_query(multi * 20, 120)
    chk("R1.5 多段时保留每段首句", "背景" in out2 and ("功能" in out2 or "约束" in out2),
        out2[:80])


# ── R2 调用点顺序 ──────────────────────────────────────────────────────────

def r2_call_order():
    for rel in ("agent/pipeline_parts/stream.py", "agent/pipeline_parts/execute.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        i_compact = src.find("_compact(_q_user")
        i_slots = src.find("retrieve_query += \" \" + \" \".join(slot_ents)")
        i_att = src.find("retrieve_query += \" \" + retrieval_att[:600]")
        ok = (i_compact > 0 and i_slots > i_compact and i_att > i_compact)
        chk("R2 %s 压缩在拼槽位/附件之前" % rel.split("/")[-1], ok,
            "compact@%d slots@%d att@%d" % (i_compact, i_slots, i_att))


# ── R4 混合批次逐条下限 ───────────────────────────────────────────────────

def _hit(rel, level="中"):
    """构造一条命中：vec_score=rel，无 bm25（relevance 即 vec_score）。"""
    return {"source_doc": "d.md", "content": "x" * 120, "vec_score": rel,
            "bm25_score": 0, "confidence_level": level, "score": 0.05}


def r4_floor_all():
    from agent.rag import GraphRAG
    rag = GraphRAG.__new__(GraphRAG)          # 不初始化向量引擎（本判据不碰检索底座）
    rag._STRONG_BM25 = getattr(GraphRAG, "_STRONG_BM25", 20.0)

    # 正常态混合批次：0.197 的垃圾 + 0.6 的切题 ⇒ 垃圾必须被丢
    mixed = [_hit(0.61, "高"), _hit(0.52, "中"), _hit(0.197, "中"), _hit(0.18, "中")]
    kept, stats = rag._filter_low_conf(mixed)
    chk("R4.1 混合批次丢弃低于 floor_all 的命中", len(kept) == 2 and stats.get("dropped") == 2,
        "kept=%d dropped=%d floor_all=%s" % (len(kept), stats.get("dropped"), stats.get("floor_all")))
    chk("R4.2 丢弃的正是 0.197/0.18 那两条",
        all(float(h.get("vec_score")) >= 0.30 for h in kept),
        str([h.get("vec_score") for h in kept]))

    # 全都不够 ⇒ **至少保留 top1**（宁少给不清空）
    low = [_hit(0.22, "中"), _hit(0.18, "中")]
    kept2, stats2 = rag._filter_low_conf(low)
    chk("R4.3 全低时至少保 top1", len(kept2) == 1 and float(kept2[0]["vec_score"]) == 0.22,
        "kept=%d" % len(kept2))

    # 全批低置信（非降级）⇒ 走旧口径 floor=0.15，0.18 应保留（不许误杀）
    all_low = [_hit(0.18, "低"), _hit(0.16, "低")]
    kept3, stats3 = rag._filter_low_conf(all_low)
    chk("R4.4 全低批次仍按 0.15 口径", stats3.get("all_low") is True and len(kept3) == 2,
        "kept=%d floor=%s" % (len(kept3), stats3.get("floor")))

    # 降级态（bigram）⇒ 不启用 floor_all（分数不可比，砍了就是静默清零）
    deg = [_hit(0.20, "中"), _hit(0.18, "中")]
    for h in deg:
        h["vector_mode"] = "bigram"
    kept4, stats4 = rag._filter_low_conf(deg)
    chk("R4.5 降级态不套用正常门槛", stats4.get("degraded") is True and len(kept4) == 2,
        "kept=%d degraded=%s" % (len(kept4), stats4.get("degraded")))

    # 强 BM25 豁免：稀疏精确命中是强证据，整批保留（口径与旧行为一致）
    strong = [_hit(0.10, "中"), dict(_hit(0.08, "中"), bm25_score=40.0)]
    kept5, stats5 = rag._filter_low_conf(strong)
    chk("R4.6 强 BM25 豁免仍然生效", stats5.get("strong_bm25_exempt") is True and len(kept5) == 2,
        "kept=%d" % len(kept5))


# ── R3/R5 缓存 ────────────────────────────────────────────────────────────

def r3_cache():
    from agent import rag as R
    R.clear_retrieval_cache()
    called = {"n": 0}
    sentinel = {"route": "vector", "chunk_hits": [_hit(0.5)], "confidence": 0.5,
                "vector_docs": [], "graph_count": 0, "vector_count": 1}

    real = R.GraphRAG.retrieve
    # 用真实 retrieve 会打外部 embedding ⇒ 只验缓存层本身（不验检索算法）
    R._cache_put(("q1", "dev", "{}"), sentinel)
    got = R._cache_get(("q1", "dev", "{}"), 120)
    chk("R3.1 命中返回缓存值", got is not None and got["route"] == "vector")
    chk("R3.2 不同 key 不命中", R._cache_get(("q2", "dev", "{}"), 120) is None)
    chk("R3.3 过期即失效", R._cache_get(("q1", "dev", "{}"), 0) is None, "ttl=0 ⇒ 等同关闭")
    R.clear_retrieval_cache()
    chk("R3.4 clear 之后不命中", R._cache_get(("q1", "dev", "{}"), 120) is None)
    # 调用方禁用缓存：ttl<=0 时 _cache_key 为 None（静态判据，避免真跑外部服务）
    src = open(os.path.join(ROOT, "agent/rag.py"), encoding="utf-8").read()
    chk("R3.5 带附件时不缓存",
        "if _cache_ttl > 0 and not attachment_text:" in src, "附件命中是请求私有")
    chk("R3.6 命中也要落 trace", '"cache_hit": True' in src, "否则重复检索在统计里消失")
    del called, real


def main() -> int:
    print("== R1 长输入压缩 ==")
    r1_compact()
    print("== R2 调用点顺序 ==")
    r2_call_order()
    print("== R3/R5 缓存 ==")
    r3_cache()
    print("== R4 混合批次下限 ==")
    r4_floor_all()
    bad = len(_fails)
    print("\n合计 %d 项，PASS %d，FAIL %d" % (len(_passes) + bad, len(_passes), bad))
    if _fails:
        print("失败项：" + "; ".join(_fails))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
