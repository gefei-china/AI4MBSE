# -*- coding: utf-8 -*-
"""全量验证：开启 LLM 重排对 17 条用例的净效果（进程内临时启用，不写配置文件）。

## 为什么必须全量测，不能只看那 3 条失败用例

前面单点测试显示重排能救「验收交付」（名次 9→1），但这只是**收益侧**。
一个改动是否该开，还要看它会不会**弄坏原本正确的**：

    单点测「混动」时还发现 `per_doc_max=0` 能救它，
    但全量一跑发现它把「工具架构」弄坏了（✅→❌）—— 净收益为 0。

⇒ 所以任何参数/开关都必须**看全量净变化**，不能凭"救回了某条"就下结论。

## 判据

- `cover`：金标进入注入窗口的条数（改前 14/17）
- `净变化`：新增救回的 - 被弄坏的
- `耗时`：重排是同步 LLM 调用，直接加在用户等待时间里

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_rerank_full.py
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

    golds = []
    for q, anchor, doc, keys in CASES:
        al = anchor.lower()
        pos = {(c["document_id"], c["chunk_index"]) for c in all_chunks
               if al in (c["content"] or "").lower()}
        if 0 < len(pos) <= 8:
            golds.append((q, anchor, pos))

    inject_k = int(cfg.get("rag", "inject_k", 5) or 5)
    top_k = int(cfg.get("rag", "top_k", 20) or 20)
    rrk = int(cfg.get("rag", "rerank_max_candidates", 10) or 10)
    svc = GraphRAG()
    rag_sec = cfg.get("rag") or {}
    orig_flag = rag_sec.get("rerank_enabled")

    import argparse as _ap
    _p = _ap.ArgumentParser(); _p.add_argument("--maxc", type=int, default=rrk)
    _p.add_argument("--topk", type=int, default=top_k)
    _a = _p.parse_args(); rrk = _a.maxc; top_k = _a.topk
    print("=" * 90)
    print("全量：LLM 重排的净效果（inject_k=%d, top_k=%d, 重排窗口=%d, 用例 %d 条）"
          % (inject_k, top_k, rrk, len(golds)))
    print("=" * 90)

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
        return s[:inject_k]

    rows = []
    total_dt = 0.0
    try:
        for q, anchor, pos in golds:
            hy = hybrid_search(con, q, top_k=top_k, branches=None)
            hits0 = hy.get("hits") or []
            ok0 = bool({(h.get("document_id"), h.get("chunk_index"))
                        for h in _pipeline(hits0)} & pos)

            rag_sec["rerank_enabled"] = True
            t0 = time.time()
            try:
                hits1 = llm_rerank(q, list(hits0), top_k=top_k, max_candidates=rrk)
            except Exception:
                hits1 = hits0
            dt = time.time() - t0
            total_dt += dt
            rag_sec["rerank_enabled"] = orig_flag

            ok1 = bool({(h.get("document_id"), h.get("chunk_index"))
                        for h in _pipeline(hits1)} & pos)
            rows.append((q, anchor, ok0, ok1, dt))
    finally:
        rag_sec["rerank_enabled"] = orig_flag

    print("%-36s %-8s %-8s %-8s" % ("问题", "不开", "开", "重排耗时"))
    print("-" * 90)
    for q, anchor, ok0, ok1, dt in rows:
        flag = ""
        if ok0 and not ok1:
            flag = "  ⚠ 弄坏了"
        elif ok1 and not ok0:
            flag = "  ★ 救回"
        print("%-36s %-8s %-8s %-8s%s"
              % (q[:36], "✅" if ok0 else "❌", "✅" if ok1 else "❌",
                 "%.2fs" % dt, flag))

    n0 = sum(1 for r in rows if r[2])
    n1 = sum(1 for r in rows if r[3])
    gained = [r for r in rows if r[3] and not r[2]]
    lost = [r for r in rows if r[2] and not r[3]]

    print("-" * 90)
    print("不开重排：%d/%d = %.0f%%" % (n0, len(rows), 100.0 * n0 / len(rows)))
    print("开重排  ：%d/%d = %.0f%%" % (n1, len(rows), 100.0 * n1 / len(rows)))
    print("净变化  ：+%d 救回 / -%d 弄坏 = %+d"
          % (len(gained), len(lost), len(gained) - len(lost)))
    print("重排平均耗时：%.2fs（串行阻塞在用户请求里）"
          % (total_dt / len(rows) if rows else 0))

    print("\n[判读]")
    net = len(gained) - len(lost)
    if net > 0:
        print("  · 净正向 %+d ⇒ 建议开启 rerank" % net)
    elif net == 0:
        print("  · 净收益 0 ⇒ 开与不开等价，但要付 %.2fs/次延迟 ⇒ 不建议开"
              % (total_dt / len(rows) if rows else 0))
    else:
        print("  · 净负向 %+d ⇒ 不要开" % net)
    for q, a, ok0, ok1, dt in gained:
        print("    救回：%s" % q[:40])
    for q, a, ok0, ok1, dt in lost:
        print("    弄坏：%s" % q[:40])

    print("\n内存开关已还原：rerank_enabled=%r（配置文件未改动）" % orig_flag)
    print("=" * 90)


if __name__ == "__main__":
    main()
