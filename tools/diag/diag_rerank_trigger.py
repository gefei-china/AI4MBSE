# -*- coding: utf-8 -*-
"""条件重排的**触发信号**选型：用数据决定"什么时候才需要花那 1.46s"。

## 要解决的问题

方案 2（条件重排）的核心是：**大部分查询不需要重排，只有"排序可能已经坏了"时才重排**。
于是唯一的问题是——**怎么判"排序坏了"**？

这个判据不能凭直觉（"感觉分数低就该重排"），因为：
  · 触发太宽 ⇒ 等于 always，白付 1.46s/次，方案 2 退化成方案 1
  · 触发太窄 ⇒ 该救的没救，白改

⇒ 必须量化：对每条用例算信号，看**信号与"金标没进注入窗口"的实际相关性**。

## 候选信号（都刻意选"有明确机理"的，不选拟合出来的）

| 信号 | 机理 | 为什么它可能预测失败 |
|---|---|---|
| `单文档垄断` | 前 5 名 ≥4 条同文档 | `per_doc_max` 会截掉该文档第 3 条起，等于把候选池砍掉一半 |
| `元数据块占位` | top3 里有"正文 <120 字且含 编号/版本/日期"的块 | 这种块靠**文档标题**匹配空降，白占注入槽（实测 `815/0`） |
| `top1 低置信` | 首位 confidence=低 | 排序信号弱，谁都能排第一 |
| `分数区分度低` | top1 分数 / top5 分数 < 1.15 | 前几名挤在一起，RRF 排不出真实相关性 |
| `注入窗口含低置信` | 选中的 5 条里有 ≥2 条「低」 | 上下文整体证据质量差 |

## 判据

对每个信号，输出：
  · `命中失败数 / 全部失败数`（**召回率**——漏掉的失败就救不回来）
  · `命中成功数 / 全部成功数`（**误触发率**——白花的钱）
  · 组合后的触发率与净效果

理想信号：召回率 100%（所有失败都触发），误触发率尽量低。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_rerank_trigger.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import importlib.util
import sqlite3
import statistics

_spec = importlib.util.spec_from_file_location(
    "_ab", os.path.join(_ROOT, "tools", "eval", "eval_inject_k_chat_ab.py"))
_ab = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_ab)
except SystemExit:
    raise
CASES = _ab.CASES

# 元数据/标题型块的特征词（来自实测 815/0 的真实内容）
META_PAT = re.compile(r"(文档编号|版本号|编制日期|页\s*数|代\s*号|文档性质|受控信息)")


def _signals(hits, inj):
    """对一条 query 的候选池与注入结果，算出各信号是否触发。"""
    top5 = hits[:5]
    s = {}

    # ① 单文档垄断：前 5 名里同文档最多占几条
    docs = [(h.get("source_doc") or "") for h in top5]
    if docs:
        mx = max(docs.count(d) for d in set(docs) if d)
    else:
        mx = 0
    s["单文档垄断"] = mx >= 4

    # ② 元数据块占位：top3 里有正文短且含编号/版本特征的
    meta = False
    for h in hits[:3]:
        body = h.get("content") or ""
        if len(body) < 120 and META_PAT.search(body):
            meta = True
            break
    s["元数据块占位"] = meta

    # ③ top1 低置信
    s["top1低置信"] = bool(top5) and str(top5[0].get("confidence_level") or "") == "低"

    # ④ 分数区分度低
    try:
        f1 = float(top5[0].get("score") or 0)
        f5 = float(top5[4].get("score") or 0)
        s["分数区分度低"] = bool(top5) and len(top5) >= 5 and f5 > 0 and (f1 / f5) < 1.15
    except Exception:
        s["分数区分度低"] = False

    # ⑤ 注入窗口含低置信
    low_n = sum(1 for h in inj if str(h.get("confidence_level") or "") == "低")
    s["注入窗口低置信"] = low_n >= 2

    # ⑥ 噪声混入（★ 专为「验收交付」这条设计，机理：首位清晰但后续灌噪声）
    #    top1 是「高」置信（说明首位确实匹配），但 top3~top5 **全是「低」**
    #    ⇒ 排序在后半段已经失控，注入进去的就是噪声。
    #    实测结构：rank1=§10交付要求(高) → rank3~5=SysML文档"已知局限"(低)
    if len(top5) >= 5:
        c1 = str(top5[0].get("confidence_level") or "")
        rest_low = all(str(h.get("confidence_level") or "") == "低" for h in top5[2:5])
        s["噪声混入"] = (c1 == "高" and rest_low)
    else:
        s["噪声混入"] = False

    # ⑦ 头部文档漂移：top1 的文档与 top3~5 主体文档不同
    #    机理：问的是 A 文档的事，排上来的却是 B 文档 ⇒ 词面相似而非语义相关
    if len(top5) >= 5:
        d1 = (top5[0].get("source_doc") or "")
        later = [(h.get("source_doc") or "") for h in top5[1:5]]
        if d1 and later:
            s["头部文档漂移"] = sum(1 for d in later if d and d != d1) >= 3
        else:
            s["头部文档漂移"] = False
    else:
        s["头部文档漂移"] = False
    return s


def main():
    from knowledge_engine import hybrid_search
    from agent.retrieval_diversity import select_for_injection
    from agent.retrieval_dedup import dedupe_and_normalize
    from agent.rag import GraphRAG
    from core import config as cfg

    con = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    con.row_factory = sqlite3.Row
    all_chunks = [dict(r) for r in con.execute(
        "SELECT document_id, chunk_index, content FROM document_chunks")]

    golds = []
    for q, anchor, doc, keys in CASES:
        pos = {(c["document_id"], c["chunk_index"]) for c in all_chunks
               if anchor.lower() in (c["content"] or "").lower()}
        if 0 < len(pos) <= 8:
            golds.append((q, anchor, pos))

    inject_k = int(cfg.get("rag", "inject_k", 5) or 5)
    top_k = int(cfg.get("rag", "top_k", 20) or 20)
    svc = GraphRAG()

    def _pipe(hits):
        h = list(hits)
        for f in (lambda x: svc._filter_low_conf(x)[0],
                  lambda x: dedupe_and_normalize(x)[0]):
            try:
                h = f(h)
            except Exception:
                pass
        sel, _ = select_for_injection(h, inject_k=inject_k)
        return sel[:inject_k]

    print("=" * 96)
    print("条件重排的触发信号选型（inject_k=%d, top_k=%d, 用例 %d 条）"
          % (inject_k, top_k, len(golds)))
    print("=" * 96)

    rows = []
    for q, anchor, pos in golds:
        hits = hybrid_search(con, q, top_k=top_k, branches=None).get("hits") or []
        inj = _pipe(hits)
        ok = bool({(h.get("document_id"), h.get("chunk_index")) for h in inj} & pos)
        sig = _signals(hits, inj)
        rows.append((q, anchor, ok, sig))

    n_ok = sum(1 for r in rows if r[2])
    n_bad = len(rows) - n_ok
    print("基线：成功 %d / 失败 %d（共 %d）\n" % (n_ok, n_bad, len(rows)))

    # 逐条信号矩阵
    keys = ["单文档垄断", "元数据块占位", "top1低置信", "分数区分度低", "噪声混入", "头部文档漂移"]
    print("%-34s %-6s %s" % ("问题", "结果", "  ".join("%-6s" % k[:6] for k in keys)))
    print("-" * 96)
    for q, anchor, ok, sig in rows:
        cells = "  ".join("%-6s" % ("✔" if sig[k] else "·") for k in keys)
        print("%-34s %-6s %s" % (q[:34], "✅" if ok else "❌", cells))

    # 单信号统计
    print("\n" + "-" * 96)
    print("%-16s %-14s %-14s %-12s" % ("信号", "召回(覆盖失败)", "误触发(命中成功)", "触发总数"))
    print("-" * 96)
    stats = {}
    for k in keys:
        hit_bad = sum(1 for r in rows if not r[2] and r[3][k])
        hit_ok = sum(1 for r in rows if r[2] and r[3][k])
        stats[k] = (hit_bad, hit_ok)
        print("%-16s %-14s %-14s %-12s"
              % (k, "%d/%d" % (hit_bad, n_bad),
                 "%d/%d" % (hit_ok, n_ok), hit_bad + hit_ok))

    # 组合统计（并集）
    print("\n" + "-" * 96)
    print("组合（任一信号触发即重排）")
    combos = [
        ["单文档垄断", "元数据块占位"],
        ["单文档垄断", "元数据块占位", "噪声混入"],
        ["单文档垄断", "元数据块占位", "头部文档漂移"],
        ["单文档垄断", "元数据块占位", "噪声混入", "头部文档漂移"],
    ]
    print("%-46s %-14s %-14s %-10s" % ("组合", "召回", "误触发", "触发率"))
    print("-" * 96)
    for combo in combos:
        hb = sum(1 for r in rows if not r[2] and any(r[3][k] for k in combo))
        ho = sum(1 for r in rows if r[2] and any(r[3][k] for k in combo))
        trig = hb + ho
        print("%-46s %-14s %-14s %-10s"
              % (" + ".join(combo), "%d/%d" % (hb, n_bad),
                 "%d/%d" % (ho, n_ok), "%d/%d" % (trig, len(rows))))

    print("\n[判读]")
    print("  · 目标：召回 = %d/%d（错的都要触发），误触发尽量少（少花时间）" % (n_bad, n_bad))
    print("  · 若某信号「召回 0」，说明它对本批失败无预测力，纳入只会白花钱")
    print("=" * 96)


if __name__ == "__main__":
    main()
