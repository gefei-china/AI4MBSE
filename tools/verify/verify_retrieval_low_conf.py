# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
# -*- coding: utf-8 -*-
"""S1 门禁：低置信「过滤」（而非清零）是否按预期工作。

背景：旧行为是"全批低置信 ⇒ chunk_hits 整段置空"。embedding 服务故障时
向量分系统性塌陷（0.07~0.29）⇒ 必判低 ⇒ **连切题金标一起丢**，
实测 6 条 query 只 1 条有结果，症状与"知识库没数据"完全一样。

本门禁守住四条语义：
  L1 切题 query 必须**有命中**（否则等于没修）
  L2 域外 query 必须**仍不返回内容**（否则是矫枉过正，比原来更糟）
  L3 过滤过程**必须留痕**（nominal/kept/dropped/floor 齐全）——不准再静默
  L4 降级状态**必须如实上报**（embedding 不可用时 degraded=True）
  L5 强 BM25 命中**豁免过滤**（稀疏精确匹配不依赖 embedding，不能连坐）
  L6 开关关闭 ⇒ 完全不过滤（保留旧行为可回退）

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_retrieval_low_conf.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import config as _cfg      # noqa: E402
from agent.rag import GraphRAG       # noqa: E402

# (标签, query, 是否应当有命中)
CASES = [
    ("图谱强相关", "卫星通信载荷的需求有哪些", True),
    ("需求追溯", "需求与架构元素的追溯关系", True),
    ("纯文档事实", "技术要求书里对可靠性的指标是多少", True),
    ("抽象/同义", "系统在断链情况下怎么保持业务连续", True),
    ("安全要求", "信息安全要求有哪些", True),
    ("域外噪声", "今天北京天气怎么样", False),
]


def main():
    ok = fail = 0

    def chk(desc, cond, extra=""):
        nonlocal ok, fail
        print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
        if cond:
            ok += 1
        else:
            fail += 1

    print("=" * 96)
    print("S1 门禁：低置信过滤")
    print("  rag.suppress_low_confidence =", _cfg.get("rag", "suppress_low_confidence"))
    print("  rag.low_conf_floor          =", _cfg.get("rag", "low_conf_floor"))
    print("=" * 96)

    g = GraphRAG()
    res = {}
    print("%-12s %-6s %-6s %-6s %-7s %-8s %s"
          % ("场景", "chunk", "naive", "drop", "floor", "degraded", "首条"))
    print("-" * 96)
    for label, q, should in CASES:
        r = g.retrieve(q)
        lc = r.get("low_conf") or {}
        ch = r.get("chunk_hits") or []
        first = ""
        if ch:
            first = "%s §%s" % (str(ch[0].get("source_doc") or "")[:22],
                                str(ch[0].get("section") or "")[:14])
        res[label] = {"chunks": len(ch), "lc": lc, "should": should}
        print("%-12s %-6d %-6s %-6s %-7s %-8s %s"
              % (label, len(ch), lc.get("nominal"), lc.get("dropped"),
                 lc.get("floor"), lc.get("degraded"), first))

    print("\n=== 判据 ===")
    need = [l for l, q, s in CASES if s]
    hit = [l for l in need if res[l]["chunks"] > 0]
    print("  切题命中：%d / %d" % (len(hit), len(need)))
    chk("L1 切题 query 有命中（>=4/5，修复前是 1/5）", len(hit) >= 4,
        "命中 %s" % hit)

    outside = res["域外噪声"]["chunks"]
    chk("L2 域外 query 不返回内容（矫枉过正检查）", outside == 0, "返回 %d 条" % outside)

    # L3 留痕完整性：每条有命中的结果都必须带 low_conf 五要素
    fields = {"nominal", "kept", "dropped", "floor", "degraded"}
    missing = []
    for l in need:
        lc = res[l]["lc"]
        if res[l]["chunks"] > 0 or lc.get("nominal"):
            miss = fields - set(lc.keys())
            if miss:
                missing.append((l, sorted(miss)))
    chk("L3 过滤过程留痕完整（nominal/kept/dropped/floor/degraded）",
        not missing, str(missing) if missing else "")

    # L3b 数量自洽：kept 必须等于实际返回条数（防止"统计说 15、实际给 0"）
    bad = [(l, res[l]["lc"].get("kept"), res[l]["chunks"])
           for l in need if res[l]["lc"].get("kept") != res[l]["chunks"]]
    chk("L3b stats.kept 与实际返回条数一致（口径不许对不上）", not bad, str(bad))

    # L4 降级上报：按 embedding **实际 provider** 探活（不能再查 embedding.base_url 的 /health）
    #
    # ★ 2026-11-02 修：原实现查 `cfg.get("embedding","base_url")` 的 `/health`，
    #   但本工程的 embedding provider 是**存在 `llm_providers` 表**里的
    #   （id=108/110，`base_url` 形如 http://x:8004/v1），配置项 `embedding.base_url`
    #   并不指向它们 ⇒ 探测恒失败 ⇒ 误判"不可用"。
    #   症状：embedding 明明已恢复（实测 108/110 均 200、dim=1024），门禁却仍要求
    #   `degraded=True`，于是判据翻红。**这是探测口径错，不是行为错。**
    _emb_down = True
    try:
        import sqlite3
        from knowledge_pipeline import Embedder
        from database import get_db
        _emb = Embedder(get_db())
        # 用一个极短文本真打一次 API：返回版本即真相（bigram-tf ⇒ 降级）
        _v, _ver = _emb.embed_with_version(["探活"])
        _emb_down = (_ver in ("bigram-tf", "bigram")) or not _v
    except Exception:
        _emb_down = True
    degraded_any = any(res[l]["lc"].get("degraded") for l in need)
    if _emb_down:
        chk("L4 embedding 不可用时如实标 degraded=True", degraded_any,
            "degraded 出现于: %s" % [l for l in need if res[l]["lc"].get("degraded")])
    else:
        chk("L4 embedding 可用时不应误标降级", not degraded_any, "degraded 误标")

    # L5 强 BM25 豁免（构造 case，不依赖真实语料）
    _fake = [
        {"vec_score": 0.02, "bm25_score": 105.0, "confidence_level": "低", "content": "x"},
        {"vec_score": 0.02, "bm25_score": 10.0, "confidence_level": "低", "content": "y"},
    ]
    kept, st = g._filter_low_conf(_fake)
    chk("L5 强 BM25（>=40）命中豁免过滤", st.get("strong_bm25_exempt") is True and len(kept) == 2,
        "kept=%d exempt=%s" % (len(kept), st.get("strong_bm25_exempt")))

    # L5b 弱 BM25 且低于门槛 ⇒ 应被过滤（不能全留）
    _fake2 = [{"vec_score": 0.01, "bm25_score": 1.0, "confidence_level": "低", "content": "z"}]
    kept2, st2 = g._filter_low_conf(_fake2)
    chk("L5b 低于门槛的条目被过滤（阈值真的生效）", len(kept2) == 0,
        "kept=%d floor=%s" % (len(kept2), st2.get("floor")))

    # L6 开关关闭 ⇒ 完全不过滤（可回退）
    origin = _cfg.get("rag", "suppress_low_confidence", True)
    try:
        _cfg._CONFIG.setdefault("rag", {})["suppress_low_confidence"] = False
        kept3, st3 = g._filter_low_conf(_fake2)
        chk("L6 关闭开关 ⇒ 不过滤（旧行为可回退）", len(kept3) == 1,
            "kept=%d" % len(kept3))
    finally:
        _cfg._CONFIG.setdefault("rag", {})["suppress_low_confidence"] = origin

    print("\n%d 项通过，%d 项失败" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
