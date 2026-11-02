# -*- coding: utf-8 -*-
"""诊断：`per_doc_max` 配额与「单主题查询」的冲突。

## 发现的机制

`混动` 那条：金标（功率需求观测）在 RRF 里**名次 4**，重排后仍是 4，
但**进不了注入窗口**。原因是排在前 5 的全是同一个文档（814）：

    rank1 814/0  rank2 814/49  rank3 814/7  rank4 814/54(=金标)  rank5 814/43

而 `context_audit.retrieval_per_doc_max = 2` ⇒ 同一文档**最多占 2 槽**
⇒ 金标是该文档第 4 条 ⇒ 被配额挡掉。

## 这是"多样性约束 vs 单主题查询"的固有冲突

- 配额的本意：防止一份文档灌满上下文（多文档场景是对的）
- 但**当问题就是针对这一份文档时**，配额反而把唯一正确的答案挡在外面

## 本脚本测什么

对 3 条失败用例，交叉测试 `per_doc_max ∈ {2, 3, 0}` × `重排 开/关`，
看金标能否进注入窗口。`0` 表示不限制（= 关闭该约束）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_perdocmax.py
"""
import os
import sys
import time

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

TARGET_ANCHORS = ["源代码使用说明", "功率需求观测", "显热负荷"]
PD_VALUES = [2, 3, 0]      # 0 = 不限制


def main():
    from knowledge_engine import hybrid_search
    from agent.retrieval_diversity import select_for_injection
    from agent.retrieval_dedup import dedupe_and_normalize
    from agent.rag import GraphRAG
    from services.rag_rerank import llm_rerank
    from core import config as cfg

    con = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    con.row_factory = sqlite3.Row
    all_chunks = [dict(r) for r in con.execute(
        "SELECT document_id, chunk_index, content FROM document_chunks")]

    sel_cases = []
    for q, anchor, doc, keys in CASES:
        if anchor not in TARGET_ANCHORS:
            continue
        al = anchor.lower()
        pos = {(c["document_id"], c["chunk_index"]) for c in all_chunks
               if al in (c["content"] or "").lower()}
        if pos:
            sel_cases.append((q, anchor, pos))
    sel_cases.sort(key=lambda x: TARGET_ANCHORS.index(x[1]))

    inject_k = int(cfg.get("rag", "inject_k", 5) or 5)
    top_k = int(cfg.get("rag", "top_k", 20) or 20)
    rrk = int(cfg.get("rag", "rerank_max_candidates", 10) or 10)
    svc = GraphRAG()
    rag_sec = cfg.get("rag") or {}
    orig_flag = rag_sec.get("rerank_enabled")

    print("=" * 92)
    print("per_doc_max × 重排 交叉测试（inject_k=%d, top_k=%d, 重排窗口=%d）"
          % (inject_k, top_k, rrk))
    print("=" * 92)

    def _pipeline(hits, pd):
        h = list(hits)
        try:
            h, _ = svc._filter_low_conf(h)
        except Exception:
            pass
        try:
            h, _ = dedupe_and_normalize(h)
        except Exception:
            pass
        s, _ = select_for_injection(h, inject_k=inject_k, per_doc_max=pd)
        return s[:inject_k]

    try:
        for q, anchor, pos in sel_cases:
            print("-" * 92)
            print("Q: %s（金标=%s）" % (q, anchor))
            hy = hybrid_search(con, q, top_k=top_k, branches=None)
            hits0 = hy.get("hits") or []
            # 先看金标所在文档占了多少前 5 名
            top5_docs = {}
            gold_rank = None
            for i, h in enumerate(hits0[:5], 1):
                d = h.get("source_doc") or ""
                top5_docs[d] = top5_docs.get(d, 0) + 1
                if (h.get("document_id"), h.get("chunk_index")) in pos:
                    gold_rank = i
            print("  前 5 名的文档分布：%s"
                  % {k[:26]: v for k, v in top5_docs.items()})
            print("  金标在 RRF 的名次：%s" % (gold_rank or "不在前5"))

            # 重排一次，两条配置共用（省 LLM 调用）
            rag_sec["rerank_enabled"] = True
            try:
                hits1 = llm_rerank(q, list(hits0), top_k=top_k, max_candidates=rrk)
            except Exception:
                hits1 = hits0
            finally:
                rag_sec["rerank_enabled"] = orig_flag

            print("  %-14s %s" % ("", "  ".join("pd=%-3d" % p for p in PD_VALUES)))
            for tag, hits in (("不开重排", hits0), ("开重排  ", hits1)):
                cells = []
                for pd in PD_VALUES:
                    inj = _pipeline(hits, pd)
                    ok = bool({(h.get("document_id"), h.get("chunk_index"))
                               for h in inj} & pos)
                    cells.append("  ✅   " if ok else "  ❌   ")
                print("  %-14s %s" % (tag, "  ".join(cells)))
    finally:
        rag_sec["rerank_enabled"] = orig_flag
        print("\n内存开关已还原：rerank_enabled=%r（配置文件未改动）" % orig_flag)
    print("=" * 92)


if __name__ == "__main__":
    main()
