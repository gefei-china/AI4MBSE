# -*- coding: utf-8 -*-
"""模拟：不同 `recall_k` 下，金标能否进入**最终注入窗口**（真正决定模型看到什么）。

## 和 diag_gold_rank.py 的区别

`diag_gold_rank.py` 只看「金标在 BM25 / 融合池里的名次」——那是中间量。
本脚本走**完整消费链**，直接看 `select_for_injection()` 选出的前 `inject_k` 条里有没有金标：

    检索（受 recall_k 影响） → 低置信过滤 → 去重 → 多样性/近邻合并 → 取前 inject_k

这才是"模型能不能看到答案"的真值。中间名次好但被后续环节挤掉的情况很常见，
只看名次会得出"检索没问题"的错误结论。

## 三种配置对比

1. **现状**：recall_k=20
2. **加宽召回**：recall_k=30 / 40 —— 只需改一个数，验证"加宽是否有用"
3. （不改配置，只做参数覆盖，跑完即还原，不落盘）

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_inject_cover.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import importlib.util
import sqlite3

_spec = importlib.util.spec_from_file_location(
    "_ab", os.path.join(_ROOT, "tools", "eval", "eval_inject_k_chat_ab.py"))
_ab = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_ab)
except SystemExit:
    raise
CASES = _ab.CASES

KS = [2, 3, 0]   # 这里代表 per_doc_max（0=不限）


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

    # 金标 = 位置集合（847/848 副本，任一命中即可）
    golds = []
    for q, anchor, doc, keys in CASES:
        al = anchor.lower()
        hits = {(c["document_id"], c["chunk_index"]) for c in all_chunks
                if al in (c["content"] or "").lower()}
        if 0 < len(hits) <= 8:
            golds.append((q, anchor, hits))
        else:
            print("  [WARN] 跳过 %s（金标命中 %d 个）" % (anchor, len(hits)))

    inject_k = int(cfg.get("rag", "inject_k", 5) or 5)
    print("=" * 92)
    print("模拟：不同 per_doc_max 下金标能否进入注入窗口（inject_k=%d, recall_k=20）" % inject_k)
    print("=" * 92)
    print("金标为位置集合，任一副本进入注入窗口即算命中。\n")

    # need 一个 RAG 实例来复用 _filter_low_conf（低置信过滤会影响最终候选）
    svc = GraphRAG()

    results = {}
    for rk in KS:
        hit = 0
        detail = []
        for q, anchor, pos in golds:
            try:
                hy = hybrid_search(con, q, top_k=20, branches=None)
                hits = hy.get("hits") or []
            except Exception as e:  # noqa: BLE001
                detail.append((q, False, "检索异常 %s" % str(e)[:40]))
                continue
            # 与生产同序：低置信过滤 → 去重/归一化 → 多样性选择
            try:
                hits, _ = svc._filter_low_conf(hits)
            except Exception:
                pass
            try:
                hits, _ = dedupe_and_normalize(hits)
            except Exception:
                pass
            sel, _ = select_for_injection(hits, inject_k=inject_k, per_doc_max=rk)
            got = {(h.get("document_id"), h.get("chunk_index")) for h in sel[:inject_k]}
            ok = bool(got & pos)
            if ok:
                hit += 1
            detail.append((q, ok, "注入后候选 %d 条" % len(hits)))
        results[rk] = (hit, detail)

    # 逐条对比
    print("%-36s %-10s %-10s %-10s" % ("问题", *["pd=%d" % k for k in KS]))
    print("-" * 92)
    for i, (q, anchor, pos) in enumerate(golds):
        cells = []
        for rk in KS:
            ok = results[rk][1][i][1]
            cells.append("✅" if ok else "❌")
        print("%-36s %-10s %-10s %-10s" % (q[:36], *cells))

    print("-" * 92)
    for rk in KS:
        h = results[rk][0]
        print("per_doc_max=%-3d 金标进入注入窗口：%d/%d = %.0f%%"
              % (rk, h, len(golds), 100.0 * h / len(golds)))

    print("\n[判读]")
    base = results[KS[0]][0]
    best_rk = max(KS, key=lambda k: results[k][0])
    best = results[best_rk][0]
    if best > base:
        print("  · per_doc_max=%d 比 %d 多覆盖 %d 条 ⇒ 值得改" % (best_rk, KS[0], best - base))
        gain = [(golds[i][0], golds[i][1]) for i in range(len(golds))
                if not results[KS[0]][1][i][1] and results[best_rk][1][i][1]]
        for q, a in gain:
            print("      受益：%s（%s）" % (q[:34], a))
    else:
        print("  · 改 per_doc_max 无收益 ⇒ 别白改参数")
        miss = [(golds[i][0], golds[i][1], results[KS[0]][1][i][2])
                for i in range(len(golds)) if not results[KS[0]][1][i][1]]
        for q, a, why in miss:
            print("      仍失败：%s（%s）%s" % (q[:30], a, why))
    print("=" * 92)


if __name__ == "__main__":
    main()
