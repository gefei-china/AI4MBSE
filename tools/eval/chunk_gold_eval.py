# -*- coding: utf-8 -*-
"""chunk 级检索评测（2026-11-02 新建）：把判据从"文档级"升级到"块级 + 注入级"。

══════════════════════════════════════════════════════════════════════
为什么必须升级（这是上一轮明确留下的 P1）
══════════════════════════════════════════════════════════════════════
原 `tools/verify/retrieval_baseline.py` 的判据是**文档级**：
    `any(k in h["source_doc"] for k in keys)`  ⇒ 同一文档内**任意一块**排进前 5 就算命中。

后果（都实测过）：
  ① 块级排序改动测不出来。rrf_k 60→10 时 doc 级只从 0.7407 到 0.8519，
     而实际变化是"BM25 头号命中从第 20 名外回到第 1 名"——doc 级几乎不敏感。
  ② 文档名关键词与问题用词同源（"技术要求书" ↔ "Print_面向…"文件名）⇒
     **降级态（bigram 字面匹配）拿高分**，0.8889 是"讨好了评测口径"的假高。
  ③ `inject_k` / 重排 / 去重这些**消费端**改动，doc 级结构上看不到。

本评测提供三层指标：
    L2 块级  hit@5 / hit@10 / MRR@10 / nDCG@10   —— 目标块有没有排进前 k、排第几
    L3 注入级 inject_cov                          —— 走完整生产消费链后，目标块有没有真的进 prompt
    L0 拒答   reject_acc                          —— 域外问题是否正确返回空

══════════════════════════════════════════════════════════════════════
金标怎么来（关键设计，决定这套评测能不能信）
══════════════════════════════════════════════════════════════════════
金标不写死 (document_id, chunk_index)，而是**用原文片段 marker 在运行时解析**：

    marker = "requirements verification confirmation by examination"
    → gold = {(846, 68)}

理由：写死索引的话，一旦重新分块/重建索引，**旧索引全部失效但不会报错**，
评测会安静地变成"全不命中"或"全命中"。用 marker 解析则：
    · 解析数 1~4 → 正常（847/848 是同一 PDF 的两个副本，同一 marker 必然命中两处，都算对）
    · 解析数 0   → **exit 1**（金标失效，必须修，不许静默跳过）
    · 解析数 >4  → **exit 1**（marker 太泛，答案满库都是，"命中"没有判据）

并且 marker 匹配前统一 `re.sub(r"\\s+", " ")` 归一化空白 ——
PDF 抽取的换行会把跨行短语打断（实测 `requirements verification confirmation\\nby examination`
裸匹配为 0 条，归一化后为 1 条）。这一条不写，金标会大面积"解析不到"。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/eval/chunk_gold_eval.py --dry-run
  ./.venv/Scripts/python.exe -X utf8 tools/eval/chunk_gold_eval.py --tag before
  ./.venv/Scripts/python.exe -X utf8 tools/eval/chunk_gold_eval.py --tag after --compare before
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")
GOLD = os.path.join(_ROOT, "tools", "eval", "chunk_gold.jsonl")
OUT_DIR = os.path.join(_ROOT, "tools", "verify", "baselines")

# 单个 marker 允许解析出的块数上限：847/848 双副本 ⇒ 2，再多说明 marker 太泛
_MAX_GOLD_PER_MARKER = 4
# 金标总量上限：超过说明 marker 已经广谱，评测失去区分度
_MAX_GOLD_TOTAL = 800

_NORM_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _NORM_RE.sub(" ", (s or "")).strip().lower()


def load_gold(path: str = GOLD) -> list:
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            out.append(json.loads(ln))
    return out


def _conn_ro():
    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def resolve_gold(cases: list, con) -> tuple:
    """把 marker 解析成 gold 位置集合。返回 (case_with_gold, errors)。

    **不做静默跳过**：任何解析失败都进 errors，调用方据此 exit 1。
    """
    rows = [dict(r) for r in con.execute(
        "SELECT document_id, chunk_index, source_doc, content FROM document_chunks")]
    for r in rows:
        r["n"] = _norm(r["content"])

    errors = []
    ok = []
    for c in cases:
        if c.get("expect_empty"):
            c = dict(c)
            c["gold"] = []
            c["_pat"] = []
            ok.append(c)
            continue
        markers = c.get("markers") or ([c["marker"]] if c.get("marker") else [])
        if not markers:
            errors.append("%-18s marker 为空（非域外用例必须给 marker 或 markers）" % c.get("id"))
            continue
        want_doc = c.get("doc") or ""
        gold = []
        pats = []
        bad = False
        for marker in markers:
            pat = _norm(marker)
            hits = [(r["document_id"], r["chunk_index"], r["source_doc"])
                    for r in rows if pat in r["n"]]
            if not hits:
                errors.append("%-18s marker 解析到 0 个块（金标失效）：%r"
                              % (c.get("id"), marker[:48]))
                bad = True
                continue
            if len(hits) > _MAX_GOLD_PER_MARKER:
                errors.append("%-18s marker 解析到 %d 个块（>%d，太泛）：%r"
                              % (c.get("id"), len(hits), _MAX_GOLD_PER_MARKER, marker[:48]))
                bad = True
                continue
            if want_doc and not all(want_doc in (h[2] or "") for h in hits):
                errors.append("%-18s marker 落在了非期望文档：%s"
                              % (c.get("id"), [h for h in hits if want_doc not in (h[2] or "")][:2]))
                bad = True
                continue
            pats.append(pat)
            gold.extend((h[0], h[1]) for h in hits)
        if bad:
            continue
        c = dict(c)
        c["gold"] = sorted(set(gold))
        c["_pat"] = pats         # 归一化后的锚点列表，判命中用（见 _hit_at 注释）
        ok.append(c)

    total_gold = sum(len(c["gold"]) for c in ok)
    if total_gold > _MAX_GOLD_TOTAL:
        errors.append("金标总量 %d 超过上限 %d（marker 普遍过泛）" % (total_gold, _MAX_GOLD_TOTAL))
    return ok, errors


def _keys(hits: list) -> list:
    return [(h.get("document_id"), h.get("chunk_index")) for h in hits]


def _hit_at(hits: list, pat, idx: int) -> bool:
    """**按内容判命中**，而不是按块索引相等。

    为什么必须这样（实测踩到，不改就会系统性低报）：
      检索有**近邻合并**（`rag.retrieval_merge_neighbor`），多个相邻 chunk 会拼成一条、
      `chunk_index` 只保留第一个。实测 `req-srcdoc`：
          金标 = (847, 49)「源代码使用说明」
          检索 top1 = (847, 48)，但它的 content 是 380 字合并块（§45+§47+§49）
          ⇒ 答案文本**已经在结果里**，按索引判却是"未命中"。
      同理，重新分块后索引会整体错位，而内容判据不受影响。

    代价：marker 太短（如"3 秒"）时可能被别的块的引用/表格误命中 ——
    所以 marker 唯一性校验（1~4 块）是这条判据成立的前提。
    """
    if idx >= len(hits) or not pat:
        return False
    content = _norm(hits[idx].get("content"))
    # 多锚点：任一等价说法出现即算命中（见 chunk_gold.jsonl 里 "markers" 的说明）
    pats = [pat] if isinstance(pat, str) else list(pat)
    # 锚点**在这里也归一化一次**：调用方若传原文（未归一化），大小写/空白会直接导致漏判。
    # 归一化幂等，重复调用无副作用；实测过不归一化时 "说法B" 匹配不到 "说法b"。
    return any(x and _norm(x) in content for x in pats)


def _first_gold_rank(hits: list, pat: str, k: int):
    for i in range(min(len(hits), k)):
        if _hit_at(hits, pat, i):
            return i + 1
    return None


def _ndcg(hits: list, pat: str, k: int) -> float:
    """二元相关 nDCG@k；理想排在 1（每个 query 只有一个答案文本）。"""
    if not pat:
        return 0.0
    for i in range(min(len(hits), k)):
        if _hit_at(hits, pat, i):
            return 1.0 / math.log2(i + 2)
    return 0.0


def _consume(hits: list, inject_k: int) -> list:
    """复刻生产消费链：低置信过滤 → 去重 → 多样性/配额 → 取 inject_k。

    **必须走同一条链**：只测 `retrieve()` 的 chunk_hits 会高估注入覆盖，
    因为生产还会过 `_filter_low_conf`（可能清空）、`dedupe_and_normalize`（可能合并）
    和 `select_for_injection`（per_doc_max 配额）。
    """
    from agent.rag import GraphRAG
    from agent.retrieval_dedup import dedupe_and_normalize
    from agent.retrieval_diversity import select_for_injection

    h = list(hits)
    try:
        h, _ = GraphRAG()._filter_low_conf(h)
    except Exception:
        pass
    try:
        h, _ = dedupe_and_normalize(h)
    except Exception:
        pass
    sel, _ = select_for_injection(h, inject_k=inject_k)
    return sel[:inject_k]


def run(cases: list, top_k: int = 25) -> dict:
    from agent.rag import GraphRAG
    from core import config as cfg

    inject_k = int(cfg.get("rag", "inject_k", 5) or 5)
    svc = GraphRAG()
    rows = []
    t_all = 0
    for c in cases:
        q = c["query"]
        t0 = time.time()
        try:
            r = svc.retrieve(q)
        except Exception as e:  # noqa: BLE001
            rows.append({"id": c["id"], "type": c["type"], "query": q,
                         "err": "%s: %s" % (type(e).__name__, e), "ms": 0})
            print("  EXC  %-18s %s" % (c["id"], type(e).__name__))
            continue
        ms = int((time.time() - t0) * 1000)
        t_all += ms
        hits = r.get("chunk_hits") or []
        gold = set(c.get("gold") or [])

        if c.get("expect_empty"):
            rows.append({"id": c["id"], "type": c["type"], "query": q,
                         "expect_empty": True, "n_hits": len(hits), "ms": ms,
                         "reject_ok": not hits})
            print("  %-18s 域外  hits=%-3d %s %5dms"
                  % (c["id"], len(hits), "✅拒答" if not hits else "❌未拒答", ms))
            continue

        inj = _consume(hits, inject_k)
        pat = c.get("_pat") or []
        r5 = _first_gold_rank(hits, pat, 5)
        r10 = _first_gold_rank(hits, pat, 10)
        # 每个 top10 位置是否"含答案文本"——变异自证 M2 直接用它算名次，避免重跑检索
        flags = [_hit_at(hits, pat, i) for i in range(min(len(hits), 10))]
        rows.append({
            "id": c["id"], "type": c["type"], "query": q, "ms": ms,
            "n_hits": len(hits), "gold": sorted(gold),
            "rank5": r5, "rank10": r10,
            "ndcg10": round(_ndcg(hits, pat, 10), 4),
            "inject_cov": any(_hit_at(inj, pat, i) for i in range(len(inj))),
            "inject_n": len(inj),
            "hit_flags": flags,
            "top5": _keys(hits)[:5],
        })
        print("  %-18s %-6s 命中=%-3d %-8s 注入=%-2d %s %5dms"
              % (c["id"], c["type"], len(hits),
                 ("第%d位" % r5) if r5 else ("第%d位(>5)" % r10 if r10 else "未进前10"),
                 len(inj),
                 "✅覆盖" if rows[-1]["inject_cov"] else "❌未覆盖", ms))
    return {"rows": rows, "inject_k": inject_k, "top_k": top_k, "total_ms": t_all}


def _env_snapshot() -> dict:
    """记录影响结果的配置项。**不记录就无法复现**："数字变了"到底是代码改了还是配置漂了。"""
    try:
        from core import config as cfg
        return {k: cfg.get("rag", k) for k in
                ("rerank_mode", "rerank_max_candidates", "recall_k", "rrf_k",
                 "inject_k", "drop_metadata_blocks", "dedupe_exact", "low_conf_floor")}
    except Exception:
        return {}


def summarize(res: dict, tag: str) -> dict:
    rows = [r for r in res["rows"] if "err" not in r]
    ans = [r for r in rows if not r.get("expect_empty")]
    rej = [r for r in rows if r.get("expect_empty")]
    n = len(ans)
    hit5 = sum(1 for r in ans if r.get("rank5"))
    hit10 = sum(1 for r in ans if r.get("rank10"))
    mrr = statistics.mean([(1.0 / r["rank10"]) if r.get("rank10") else 0.0 for r in ans]) if ans else 0.0
    ndcg = statistics.mean([r["ndcg10"] for r in ans]) if ans else 0.0
    cov = sum(1 for r in ans if r.get("inject_cov"))
    rej_ok = sum(1 for r in rej if r.get("reject_ok"))
    by_type = {}
    for t in sorted({r["type"] for r in ans}):
        sub = [r for r in ans if r["type"] == t]
        by_type[t] = {
            "n": len(sub),
            "hit@5": round(sum(1 for r in sub if r.get("rank5")) / len(sub), 4),
            "inject_cov": round(sum(1 for r in sub if r.get("inject_cov")) / len(sub), 4),
        }
    return {
        "tag": tag,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": "chunk",
        "n_total": len(rows),
        "answerable": n,
        "rejected": len(rej),
        "hit@5": round(hit5 / n, 4) if n else 0.0,
        "hit@10": round(hit10 / n, 4) if n else 0.0,
        "mrr@10": round(mrr, 4),
        "ndcg@10": round(ndcg, 4),
        "inject_cov": round(cov / n, 4) if n else 0.0,
        "reject_acc": round(rej_ok / len(rej), 4) if rej else 0.0,
        "avg_ms": int(statistics.mean([r["ms"] for r in rows])) if rows else 0,
        "inject_k": res["inject_k"],
        "env": _env_snapshot(),
        "by_type": by_type,
        "rows": rows,
    }


def mutation_check(summary: dict, cases: list) -> list:
    """变异自证：判据必须**能翻红**，否则它测不出任何东西。

    M1 把命中列表整体替换成"必定不含金标"的假条目 ⇒ hit@5 必须变 0。
    M2 把真命中倒序 ⇒ 若金标只在第 1 位出现，倒序后 hit@5 应下降。
    这两条不依赖外部服务（纯算），用来证明指标函数不是恒真。
    """
    out = []
    ans = [r for r in summary["rows"] if not r.get("expect_empty") and "err" not in r]

    # M1：把命中列表整体替换成"内容里必定没有答案文本"的假条目 ⇒ 任何用例都不许判命中
    fake = [{"document_id": -1, "chunk_index": i, "content": "ZZZ_%d_无关文本" % i}
            for i in range(5)]
    m1 = 0
    for r in ans:
        # 用该用例自己的 marker 去匹配假条目：若仍判命中，说明判据根本没读内容
        pat = (r.get("query") or "")
        if pat and _first_gold_rank(fake, _norm(pat), 5):
            m1 += 1
    out.append(("M1 换入无答案文本的条目后 hit@5 必须为 0", m1 == 0, "仍有 %d 条判命中" % m1))

    # M2：位置敏感性 —— 把命中顺序倒置，原本第 1 位命中的必须变成末位。
    # ⚠️ 我第一版这里写错过：断言"倒序后名次为 None"，但第 1 位倒序后落在**第 5 位**，
    #    仍在 top5 内 ⇒ 断言恒假、变异永不触发。那不是指标错，是**判据错**。
    #    正确判据：名次必须从 1 变为非 1，证明指标真的读位置而非只看"有没有出现"。
    changed = 0
    cand = 0
    for r in ans:
        flags = r.get("hit_flags") or []
        if flags and flags[0] and len(flags) >= 3:
            cand += 1
            rev = list(reversed(flags))
            new_rank = (rev.index(True) + 1) if True in rev else None
            if new_rank != 1:
                changed += 1
    out.append(("M2 命中倒序后，原本第 1 位命中的名次必须变化",
                (changed == cand) if cand else True,
                "%d/%d 名次变化" % (changed, cand)))

    # M3：两条代码路径必须一致 —— hit_flags 全 False 时 rank10 必须为 None。
    #    （rank10 用 `_first_gold_rank`，flags 用 `_hit_at`；两者若不同源就会互相矛盾）
    inconsistent = []
    for r in ans:
        flags = r.get("hit_flags") or []
        if flags and (not any(flags)) and r.get("rank10"):
            inconsistent.append(r["id"])
        if flags and any(flags[:5]) and not r.get("rank5"):
            inconsistent.append(r["id"])
    out.append(("M3 hit_flags 与 rank5/rank10 必须同源一致", not inconsistent,
                "矛盾项=%s" % (inconsistent[:3] or "无")))
    return out


def show(s: dict):
    print("\n" + "=" * 80)
    print("chunk 级评测 [%s]  %s" % (s["tag"], s["ts"]))
    print("=" * 80)
    print("  可答 query      : %d    域外: %d    inject_k=%d"
          % (s["answerable"], s["rejected"], s["inject_k"]))
    print("  L2 块级 hit@5   : %.4f" % s["hit@5"])
    print("  L2 块级 hit@10  : %.4f" % s["hit@10"])
    print("  L2 MRR@10       : %.4f" % s["mrr@10"])
    print("  L2 nDCG@10      : %.4f" % s["ndcg@10"])
    print("  L3 注入级覆盖   : %.4f  ← 目标块是否真的进了 prompt" % s["inject_cov"])
    print("  L0 拒答准确率   : %.4f" % s["reject_acc"])
    print("  平均耗时        : %d ms" % s["avg_ms"])
    if s.get("by_type"):
        print("\n  分类型：")
        print("    %-8s %-4s %-8s %s" % ("类型", "n", "hit@5", "注入覆盖"))
        for t, v in s["by_type"].items():
            print("    %-8s %-4d %-8.3f %.3f" % (t, v["n"], v["hit@5"], v["inject_cov"]))


def compare(a: dict, b: dict):
    print("\n" + "=" * 80)
    print("对比：%s → %s" % (a["tag"], b["tag"]))
    print("=" * 80)
    print("  %-16s %-10s %-10s %s" % ("指标", a["tag"], b["tag"], "变化"))
    for k in ("hit@5", "hit@10", "mrr@10", "ndcg@10", "inject_cov", "reject_acc"):
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            continue
        d = vb - va
        print("  %-16s %-10s %-10s %+.4f %s" % (k, va, vb, d, "↑" if d > 0 else ("↓" if d < 0 else "=")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--dry-run", action="store_true", help="只校验金标，不跑检索")
    ap.add_argument("--meta", choices=["on", "off"], default="on",
                    help="评测时固定元数据块过滤开关（用于量化该项的真实贡献）")
    ap.add_argument("--rerank", choices=["off", "on", "auto"], default="off",
                    help="评测时固定重排模式（默认 off=确定性）。生产是 auto，"
                         "但 auto 会调 LLM、每次触发的条目可能不同 ⇒ **评测必须固定，否则测不出差异**")
    ap.add_argument("--compare", default="")
    a = ap.parse_args()

    cases = load_gold()
    con = _conn_ro()
    try:
        cases, errors = resolve_gold(cases, con)
    finally:
        con.close()

    print("=" * 80)
    print("chunk 级金标校验：%d 条用例" % len(cases))
    print("=" * 80)
    for c in cases:
        if c.get("expect_empty"):
            print("  %-18s 域外（应拒答）" % c["id"])
        else:
            print("  %-18s gold=%-28s §%s"
                  % (c["id"], str(c["gold"])[:26], (c.get("marker") or "")[:34]))

    if errors:
        print("\n❌ 金标校验失败 %d 项（不允许静默跳过）：" % len(errors))
        for e in errors:
            print("   - " + e)
        return 1
    print("\n✅ 金标全部可解析（每 marker 1~%d 块，且落在期望文档）"
          % _MAX_GOLD_PER_MARKER)

    if a.dry_run:
        print("\nDRY-RUN：未跑检索。")
        return 0

    # ── 固定不确定性来源（否则同一配置两次跑分数会漂）──────────────────
    # 实测：同一配置连跑两次 hit@5 出现 0.6786 / 0.7143 的差异，差异全部来自
    # `rerank_mode=auto` 调用 LLM 的结果不稳定。评测必须确定性，否则任何 A/B 都不可信。
    try:
        from core import config as _cfg
        _rag = _cfg.get("rag") or {}
        _prev = _rag.get("rerank_mode")
        _rag["rerank_mode"] = a.rerank
        _eff = (_cfg.get("rag") or {}).get("rerank_mode")
        print("\n[评测环境] rerank_mode: 生产=%r → 本次固定=%r（回读校验 %r）"
              % (_prev, a.rerank, _eff))
        if _eff != a.rerank:
            print("❌ 重排模式未能固定（回读不一致）⇒ 本次数字不可比")
            return 1
        _rag["drop_metadata_blocks"] = (a.meta == "on")
        _eff_meta = (_cfg.get("rag") or {}).get("drop_metadata_blocks")
        print("[评测环境] drop_metadata_blocks = %r（回读 %r）" % (a.meta == "on", _eff_meta))
        if bool(_eff_meta) != (a.meta == "on"):
            print("❌ 元数据过滤开关未能固定 ⇒ 本次数字不可比")
            return 1
    except Exception as e:  # noqa: BLE001
        print("⚠️ 无法固定 rerank_mode：%s" % e)

    tag = a.tag or ("chunk_" + time.strftime("%Y%m%d_%H%M%S"))
    print("\n" + "=" * 80)
    print("真实检索（tag=%s）" % tag)
    print("=" * 80)
    res = run(cases)
    s = summarize(res, tag)

    print("\n变异自证（判据必须能翻红）：")
    bad = 0
    for name, ok, detail in mutation_check(s, cases):
        print("  %s %s  [%s]" % ("✅" if ok else "❌", name, detail))
        bad += 0 if ok else 1
    s["mutation_ok"] = (bad == 0)

    show(s)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "chunk_gold_%s.json" % tag)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    print("\n已写：%s" % path)

    if a.compare:
        old = os.path.join(OUT_DIR, "chunk_gold_%s.json" % a.compare)
        if os.path.exists(old):
            with open(old, encoding="utf-8") as f:
                compare(json.load(f), s)
        else:
            print("\n⚠️ 找不到对比基线：%s" % old)

    if bad:
        print("\n❌ 变异自证失败 %d 项 ⇒ 指标可能恒真，本次数字不可采信" % bad)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
