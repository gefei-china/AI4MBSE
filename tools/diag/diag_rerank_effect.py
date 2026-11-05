# -*- coding: utf-8 -*-
"""测试：开启 LLM 重排能否救回「排序靠后」的金标（进程内临时启用，**不写配置文件**）。

## 为什么用进程内开关而不是改配置

`~/.workbuddy/mbse_config.json` 一旦写入 `rerank_enabled`，以后改代码默认值就不生效
（本项目记过同类事故）。诊断阶段只想知道"开了会不会好"，用**进程内改内存 + finally 还原**
即可，零落盘风险。

## 被测的两条（第三条是"排序靠后"里最典型的）

  · 验收交付要求交付哪些文档   —— 融合名次 9，落在重排窗口（前 10）内 → **有希望**
  · 混动仿真关注哪些功率指标   —— 融合名次 4，落在窗口内 → **有希望**
  · 座舱空气调节需要考虑哪些负荷 —— 融合名次 >60，**根本进不了重排窗口** → 预计无效

第三条放在这里是有意的：**用它证明"重排不是万能的"**，
否则很容易得出"开了重排全好了"的错误印象。

## 判据

`gold_in_inject` —— 金标是否进入最终注入窗口（inject_k 条）。
同时记录重排耗时（这是开它的主要代价）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_rerank_effect.py
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

# 只测这三条：两条"排序靠后"（应获益）+ 一条"根本没召回"（应无变化）
TARGET_ANCHORS = ["源代码使用说明", "功率需求观测", "显热负荷"]


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
    # 按 TARGET_ANCHORS 顺序排
    sel_cases.sort(key=lambda x: TARGET_ANCHORS.index(x[1]))

    inject_k = int(cfg.get("rag", "inject_k", 5) or 5)
    top_k = int(cfg.get("rag", "top_k", 20) or 20)
    rrk = int(cfg.get("rag", "rerank_max_candidates", 10) or 10)
    svc = GraphRAG()

    print("=" * 88)
    print("开启 LLM 重排的效果测试（inject_k=%d, top_k=%d, 重排窗口=%d）"
          % (inject_k, top_k, rrk))
    print("=" * 88)
    print("※ 进程内临时启用 rerank，finally 还原；**不写配置文件**\n")

    # 记录内存中的原值，finally 还原
    rag_sec = cfg.get("rag") or {}
    orig_flag = rag_sec.get("rerank_enabled")

    def _pipeline(hits):
        h = list(hits)
        try:
            h, _ = svc._filter_low_conf(h)
        except Exception:
            pass
        try:
            h, _ = dedupe_and_normalize(h)
        except Exception:
            pass
        s, _ = select_for_injection(h, inject_k=inject_k)
        return h, s[:inject_k]

    try:
        for q, anchor, pos in sel_cases:
            print("-" * 88)
            print("Q: %s（金标=%s）" % (q, anchor))
            hy = hybrid_search(con, q, top_k=top_k, branches=None)
            hits0 = hy.get("hits") or []
            rank0 = next((i for i, h in enumerate(hits0, 1)
                          if (h.get("document_id"), h.get("chunk_index")) in pos), None)
            print("  RRF 后金标名次：%s" % (rank0 if rank0 else "❌ 不在 top%d" % top_k))
            if rank0 is None:
                print("  ⇒ 不在候选里，重排看不到它；下面结果必然是「无效」")
            if rank0 is not None and rank0 > rrk:
                print("  ⚠ 名次 %d > 重排窗口 %d ⇒ 不会被 LLM 打分" % (rank0, rrk))

            # ── 不开重排（基准）──
            _, inj0 = _pipeline(hits0)
            ok0 = bool({(h.get("document_id"), h.get("chunk_index")) for h in inj0} & pos)

            # ── 开重排 ──
            rag_sec["rerank_enabled"] = True
            t0 = time.time()
            try:
                hits1 = llm_rerank(q, list(hits0), top_k=top_k, max_candidates=rrk)
            except Exception as e:  # noqa: BLE001
                print("  重排异常：%s" % str(e)[:80])
                hits1 = hits0
            dt = time.time() - t0
            rag_sec["rerank_enabled"] = orig_flag     # 每条立即还原，避免影响下一步

            rank1 = next((i for i, h in enumerate(hits1, 1)
                          if (h.get("document_id"), h.get("chunk_index")) in pos), None)
            _, inj1 = _pipeline(hits1)
            ok1 = bool({(h.get("document_id"), h.get("chunk_index")) for h in inj1} & pos)

            print("  重排后金标名次：%s | 耗时 %.2fs" % (rank1 if rank1 else "—", dt))
            print("  注入窗口含金标： 不开重排=%s → 开重排=%s"
                  % ("✅" if ok0 else "❌", "✅" if ok1 else "❌"))
    finally:
        rag_sec["rerank_enabled"] = orig_flag
        print("\n内存开关已还原：rerank_enabled=%r（配置文件未改动）" % orig_flag)
    print("=" * 88)


if __name__ == "__main__":
    main()
