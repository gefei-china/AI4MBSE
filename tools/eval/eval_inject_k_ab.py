# -*- coding: utf-8 -*-
"""`rag.inject_k` 的 A/B 标定（3 / 5 / 8 三档）。

## 为什么必须另建一套用例集

项目既有评测集（`retrieval_baseline.py`）判定的是**文档级命中**（`source_doc` 是否含关键词），
而 `inject_k` 影响的是**文档内部的注入截断** —— 文档召回到了，但答案所在的 chunk
被截在第 k 条之外，模型依然看不到答案。所以既有评测集对 `inject_k` **天生不敏感**。

这一点早有记载（`docs/AI上下文配置项-依据与行业对标-20260921.md` §8.7）：
> **`inject_k` 未被直接测到**：doc 域 gold 是"章节内全部 chunk"（平均 6.5 条），
> 3 条注入量下 recall 对"注入截断"不敏感；要测 `inject_k` 需另建"单答案"用例集。

本脚本就是那个"单答案"用例集。

## 判据怎么设计（关键）

每条用例 = (问题, **答案锚点**, 期望文档)。锚点取该章节的**独有短语**（如 §7.3 的 `99.9%`），
且脚本会**自动校验锚点在库里只出现在 1~3 个 chunk**（否则它就不是"单答案"，用例作废并报错，
绝不静默跳过 —— 静默跳过会让门禁假绿）。

因此指标是 **anchor_hit@k**：注入的前 k 条里，是否真的出现了承载答案的那个 chunk。
这个量直接随 `inject_k` 变化，正是要测的东西。

## 三档怎么比

检索本身与 `inject_k` 无关（它只控制注入截断），所以**每条用例只检索一次**，
然后对 3/5/8 三档分别做 `select_for_injection` 截断 —— 同一批候选、同一套配置，
唯一变量就是 k。这保证对比公平，也省掉 3 倍检索开销。

## 指标

| 指标 | 含义 | 为什么看它 |
|---|---|---|
| anchor_hit@k | 答案 chunk 落在注入前 k 条的比例 | **主指标**：直接决定模型能不能看到答案 |
| avg_chars | 实际注入总字数 | 成本：`inject_k` 每 +1 约 +600 字 |
| avg_docs | 注入覆盖的不同文档数 | 多样性：多文档才有多视角 |
| noise_rate | 注入条目里不含锚点、且非期望文档的比例 | 多给是不是在灌噪声 |

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/eval/eval_inject_k_ab.py
  ./.venv/Scripts/python.exe -X utf8 tools/eval/eval_inject_k_ab.py --tag after_dedup
"""
import os
import sys
import json
import time
import argparse
import statistics

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# 让 embedding 超时不因本机代理而放大（本机确有 HTTP_PROXY，见项目约定）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

# ── "单答案"用例集 ────────────────────────────────────────────────────────
# (问题, 答案锚点, 期望文档关键词)
# 锚点全部取自**真实 chunk 原文**，且要求是特定章节独有的短语。
# 选材原则：答案能由**一个 chunk** 完整回答（不是"本章节共 6.5 个 chunk"那种散装答案），
#          这样"答案在不在注入的 k 条里"才有确定的是/否。
CASES = [
    # ── 技术要求书（847/848，55 chunk，一章一块，最适合单答案）──
    ("技术要求书里轻量级查询的响应时间要求是多少", "3 秒", "Print_面向"),
    ("技术要求书对可靠性要求中，出错时应该怎么做", "平滑降级", "Print_面向"),
    ("技术要求书要求的全年可用性是多少", "99.9%", "Print_面向"),
    ("技术要求书对网络传输数据的安全要求是什么", "网络会话加密", "Print_面向"),
    ("自主可控要求对组件部署有什么限制", "公有服务器", "Print_面向"),
    ("运行环境要求支持哪些国产操作系统", "麒麟", "Print_面向"),
    ("设计与实施约束对训练数据有什么要求", "公有云训练", "Print_面向"),
    ("工具的系统架构应包含哪些接口", "互联数据接口", "Print_面向"),
    ("验收交付要求交付哪些文档", "源代码使用说明", "Print_面向"),
    ("易用性要求对使用手册有什么规定", "使用手册", "Print_面向"),
    ("人在回路要求对 AI 生成结果提供什么", "中间结果预览", "Print_面向"),
    ("模型预评审与校验要求生成什么材料", "预评审报告", "Print_面向"),
    ("交互界面要求支持哪些交互形式", "多模态交互", "Print_面向"),
    ("设计数据管理要从哪些数据源提取数据", "多源异构数据", "Print_面向"),
    ("运行维护要求对运行中出现的错误怎么说明", "错误码", "Print_面向"),
    # ── 广汽方法论（812）：流程链路，每步的输入输出是明确的单句 ──
    ("整车需求分析以什么为输入、什么为输出", "整车需求VR为输出", "广汽方法论"),
    ("子系统架构设计以什么为输入", "子系统需求SSR为输入", "广汽方法论"),
]


def _load_chunks():
    import sqlite3
    db = os.path.join(_ROOT, "mbse.db")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT document_id, source_doc, chunk_index, section, content FROM document_chunks"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _anchor_uniqueness(chunks, anchor):
    """返回锚点出现在多少个 chunk / 哪些文档。"""
    al = anchor.lower()
    docs = set()
    n = 0
    for c in chunks:
        if al in (c.get("content") or "").lower():
            n += 1
            docs.add(c.get("source_doc") or "")
    return n, docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--ks", default="3,5,8")
    args = ap.parse_args()
    ks = [int(x) for x in args.ks.split(",") if x.strip()]

    print("=" * 78)
    print("inject_k A/B 标定（单答案用例集）")
    print("=" * 78)

    chunks = _load_chunks()
    print("语料：%d chunks / %d 文档" % (len(chunks), len({c['source_doc'] for c in chunks})))

    # ── 第一步：校验锚点是否真的"单答案" ──
    print("\n[1] 锚点校验（必须存在，且只出现在少量 chunk —— 否则不是单答案）")
    valid = []
    bad = []
    for q, anchor, doc in CASES:
        n, docs = _anchor_uniqueness(chunks, anchor)
        if n == 0:
            bad.append((q, anchor, "锚点在库中不存在"))
        elif n > 3:
            bad.append((q, anchor, "锚点出现在 %d 个 chunk（>3，非单答案）" % n))
        else:
            valid.append((q, anchor, doc, n))
            print("  [OK  ] %-42s 锚点=%-16s chunks=%d" % (q[:42], anchor, n))
    for q, anchor, why in bad:
        print("  [FAIL] %-42s 锚点=%-16s %s" % (q[:42], anchor, why))
    if bad:
        print("\n❌ 有 %d 条用例锚点不合格 —— 必须先修用例，不能带着坏用例出结论" % len(bad))
        sys.exit(1)
    print("  合格用例：%d 条" % len(valid))

    # ── 第二步：每条用例只检索一次（inject_k 不影响检索）──
    from agent.rag import GraphRAG
    from agent.retrieval_diversity import select_for_injection

    svc = GraphRAG()
    print("\n[2] 检索（每条只跑一次，三档共用同一批候选）")
    records = []
    t0 = time.time()
    for q, anchor, doc, _n in valid:
        try:
            r = svc.retrieve(q, branch="dev")
            hits = r.get("chunk_hits") or []
        except Exception as e:  # noqa: BLE001
            print("  [WARN] %s → 检索异常 %s" % (q[:36], e))
            hits = []
        # ⚠️ 必须缓存 hits 本身：`inject_k` 只影响截断、不影响检索，
        #   所以三档共用同一批候选才叫公平对比；重跑检索还会引入检索侧抖动。
        records.append({"q": q, "anchor": anchor, "doc": doc, "hits": hits})
        print("  %-46s 命中=%2d" % (q[:46], len(hits)))
    print("  检索总耗时 %.1fs" % (time.time() - t0))

    # ── 第三步：三档截断对比 ──
    print("\n[3] 三档对比")
    header = "%-8s %-14s %-12s %-10s %-10s" % ("inject_k", "anchor_hit@k", "平均字数", "文档数", "噪声率")
    print(header)
    print("-" * len(header))
    summary = {}
    misses = {}          # k → 漏掉答案的用例清单（诊断用，比数字更有说服力）
    for k in ks:
        hit_n = 0
        chars = []
        doc_sets = []
        noise_num = 0
        noise_den = 0
        miss_list = []
        for rec in records:
            sel, _ = select_for_injection(rec["hits"], inject_k=k)
            got = sel[:k]
            got_hit = any(rec["anchor"].lower() in (h.get("content") or "").lower() for h in got)
            if got_hit:
                hit_n += 1
            else:
                # 答案是否在完整候选池里？用来区分「检索没召回到」与「召回到但被截断」
                in_pool = any(rec["anchor"].lower() in (h.get("content") or "").lower()
                              for h in rec["hits"])
                miss_list.append({"q": rec["q"], "anchor": rec["anchor"],
                                  "in_pool": in_pool, "pool_size": len(rec["hits"])})
            chars.append(sum(len(h.get("content") or "") for h in got))
            doc_sets.append(len({h.get("source_doc") for h in got if h.get("source_doc")}))
            for h in got:
                noise_den += 1
                body = (h.get("content") or "").lower()
                in_doc = rec["doc"].lower() in (h.get("source_doc") or "").lower()
                if rec["anchor"].lower() not in body and not in_doc:
                    noise_num += 1
        misses[k] = miss_list
        n = len(valid)
        rate = hit_n / n if n else 0
        summary[k] = {
            "anchor_hit": round(rate, 4),
            "avg_chars": round(statistics.mean(chars), 1) if chars else 0,
            "avg_docs": round(statistics.mean(doc_sets), 2) if doc_sets else 0,
            "noise_rate": round(noise_num / noise_den, 4) if noise_den else 0,
        }
        print("%-8d %-14s %-12.0f %-10.2f %-10.3f" % (
            k, "%d/%d = %.3f" % (hit_n, n, rate),
            summary[k]["avg_chars"], summary[k]["avg_docs"], summary[k]["noise_rate"]))

    # ── 漏答案明细：区分「没召回到」和「召回到但被截断」──
    print("\n[3b] 漏答案明细（区分根因，避免把截断问题误判成召回问题）")
    any_miss = False
    for k in ks:
        if not misses[k]:
            print("  inject_k=%-2d 无漏答案" % k)
            continue
        any_miss = True
        for m in misses[k]:
            why = ("候选池里有、被第 %d 条截断挡住 ← inject_k 太小" % k
                   if m["in_pool"] else "候选池里就没有 ← 召回问题，调 inject_k 无效")
            print("  inject_k=%-2d %-40s 锚点=%-14s %s（池 %d 条）"
                  % (k, m["q"][:40], m["anchor"], why, m["pool_size"]))
    if not any_miss:
        print("  三档均无漏答案 —— 说明本用例集对 inject_k 不敏感，需换更硬的用例")

    # ── 第四步：结论（按主指标 + 成本给建议，不替人拍板） ──
    print("\n[4] 判读")
    best = max(summary, key=lambda k: (summary[k]["anchor_hit"], -summary[k]["avg_chars"]))
    base = min(ks)
    print("  · 主指标 anchor_hit 最高：inject_k=%d（%.3f）" % (best, summary[best]["anchor_hit"]))
    print("  · 相对 inject_k=%d 的提升：%+.3f" % (
        base, summary[best]["anchor_hit"] - summary[base]["anchor_hit"]))
    print("  · 成本代价：平均字数 %d → %d（×%.2f）" % (
        summary[base]["avg_chars"], summary[best]["avg_chars"],
        (summary[best]["avg_chars"] / summary[base]["avg_chars"]) if summary[base]["avg_chars"] else 0))
    print("  · 噪声率变化：%.3f → %.3f" % (summary[base]["noise_rate"], summary[best]["noise_rate"]))
    print("  ※ 本脚本只给数据，不自动改配置 —— 调参是产品决策，需人确认。")

    # ── 当前生效配置的实测值（体检用：改完配置跑一遍就知道行不行） ──
    try:
        from core import config as _cfg
        _cur = int(_cfg.get("rag", "inject_k", 0) or 0)
        if _cur in summary:
            print("\n  当前生效 rag.inject_k=%d ⇒ anchor_hit=%.3f、平均注入 %d 字"
                  % (_cur, summary[_cur]["anchor_hit"], summary[_cur]["avg_chars"]))
        else:
            print("\n  当前生效 rag.inject_k=%d（不在本次扫描档位 %s 内）" % (_cur, ks))
    except Exception:
        pass

    if args.tag:
        out = os.path.join(_ROOT, "tools", "verify", "baselines", "inject_k_%s.json" % args.tag)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "ks": ks, "n_cases": len(valid),
                       "summary": {str(k): v for k, v in summary.items()}},
                      f, ensure_ascii=False, indent=2)
        print("\n已写：%s" % out)
    print("=" * 78)


if __name__ == "__main__":
    main()
