# -*- coding: utf-8 -*-
"""拒答阈值重标定（`rag.reject_max_score` / `rag.reject_avg_score`）。

## 为什么必须重标

阈值 0.35 / 0.22 是 **2026-11-01 在降级态（bigram-tf）下标定的**，当时的分离度是：

    切题 maxrel 0.405 ~ 0.739   |   域外 maxrel 0.000 ~ 0.293   → 取 0.35 居中

但 **2026-11-02 embedding 服务恢复**后分数分布整体抬升，实测域外 5 条：

    0.299 / 0.366 / 0.374 / 0.277 / 0.261

⇒ **3 条越过或贴近 0.35** ⇒ 拒答准确率从 1.0000 掉到 **0.0000**。
后果：问"红烧肉怎么做"也返回 20 条无关内容去编答案——这是幻觉的主要来源。

## 标定方法（分位分离度，不拍脑袋）

1. 收集**切题样本**（27 条可答 query）与**域外样本**（负例）的 `relevance` 分布
2. 找"切题下沿"与"域外上沿"之间的**空档**
3. 阈值取空档中点；若两分布重叠（无空档）⇒ **如实报告无解**，不硬凑一个数

## 判据

    separation = 切题 p05 - 域外 p95
    separation > 0  ⇒ 可分，阈值取两者中点
    separation <= 0 ⇒ 重叠，**换阈值救不了**，需改判据本身（如改用绝对分/相对比）

## 为什么用 p05/p95 而不是 min/max

min/max 对单个异常样本过敏（一条离群就能把空档压没）。
p05/p95 容忍各 5% 的尾部，更稳；但**同时报告 min/max**，让你看到尾部有多脏。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/calib/calibrate_reject_threshold.py
  ./.venv/Scripts/python.exe -X utf8 tools/calib/calibrate_reject_threshold.py --apply
"""
import os
import sys
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import importlib.util

# 复用基线脚本的正例集（27 条可答 + 域外负例都在里面）
_spec = importlib.util.spec_from_file_location(
    "_rb", os.path.join(_ROOT, "tools", "verify", "retrieval_baseline.py"))
_rb = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_rb)
except SystemExit:
    raise
QUERIES = _rb.QUERIES

# 追加域外负例（基线自带 5 条，再补几条不同形态的，避免过拟合到那 5 条）
EXTRA_NEG = [
    "红烧肉需要炖多久",
    "推荐几部好看的电影",
    "明天会下雨吗",
    "如何炒股票赚钱",
    "微信怎么修改头像",
    "篮球比赛规则是什么",
]


def pct(vals, p):
    v = sorted(vals)
    if not v:
        return 0.0
    i = min(len(v) - 1, max(0, int(round((len(v) - 1) * p))))
    return v[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回配置（默认只报告）")
    ap.add_argument("--min-gap", type=float, default=0.0,
                    help="要求的最小分离度；不足则不写（默认 0）")
    args = ap.parse_args()

    from agent.rag import GraphRAG
    from core import config as cfg

    svc = GraphRAG()
    pos_q = [q for (label, q, _kw, empty) in
             [(x[0], x[1], x[2], x[3]) for x in QUERIES] if not empty]
    neg_q = [x[1] for x in QUERIES if x[3]] + EXTRA_NEG

    print("=" * 86)
    print("拒答阈值重标定（embedding 恢复后必须重做）")
    print("=" * 86)
    print("原阈值：reject_max_score=%s  reject_avg_score=%s"
          % (cfg.get("rag", "reject_max_score"), cfg.get("rag", "reject_avg_score")))
    print("正例 %d 条 / 负例 %d 条\n" % (len(pos_q), len(neg_q)))

    def _collect(qs, tag):
        mx, av = [], []
        for q in qs:
            try:
                r = svc.retrieve(q, branch="dev")
                hits = r.get("chunk_hits") or []
            except Exception:
                hits = []
            # ★ 必须用 `_relevance_of()` 现场算，**不能读 `hit["relevance"]`**。
            #   实测踩坑：该字段只在 `_filter_low_conf` 的**低置信分支**才写
            #   （`h["relevance"] = round(_rel, 4)`），切题条目多为「中/高」置信
            #   ⇒ 字段是 `None` ⇒ `float(None or 0)` 把它当成 0 ⇒
            #   第一版标定出现"切题 27 条全 0 分、域外 11 条都有分"的荒谬分布，
            #   进而错误地得出"两分布重叠、换阈值救不了"。
            #   ⇒ 用生产同一个函数算，量纲与阈值口径**天然同源**。
            rels = [svc._relevance_of(h) for h in hits]
            if rels:
                mx.append(max(rels))
                av.append(sum(rels) / len(rels))
            else:
                mx.append(0.0)
                av.append(0.0)
        print("  %s：%d 条（有命中的 %d 条）" % (tag, len(qs), sum(1 for x in mx if x > 0)))
        return mx, av

    pos_mx, pos_av = _collect(pos_q, "切题")
    neg_mx, neg_av = _collect(neg_q, "域外")

    def _report(name, pos, neg):
        print("\n[%s]" % name)
        print("  切题  min=%.3f p05=%.3f p50=%.3f max=%.3f"
              % (min(pos), pct(pos, 0.05), pct(pos, 0.5), max(pos)))
        print("  域外  min=%.3f p50=%.3f p95=%.3f max=%.3f"
              % (min(neg), pct(neg, 0.5), pct(neg, 0.95), max(neg)))
        sep = pct(pos, 0.05) - pct(neg, 0.95)
        print("  分离度(p05-p95) = %.3f  %s" % (sep, "✅ 可分" if sep > 0 else "❌ 重叠"))
        return sep

    sep_mx = _report("relevance 最大值（对应 reject_max_score）", pos_mx, neg_mx)
    sep_av = _report("relevance 均值（对应 reject_avg_score）", pos_av, neg_av)

    # ── 建议值 ──
    print("\n" + "=" * 86)
    print("[建议]")
    cand_mx = cand_av = None
    if sep_mx > 0:
        cand_mx = round((pct(pos_mx, 0.05) + pct(neg_mx, 0.95)) / 2, 3)
        print("  reject_max_score : %.3f  →  **%.3f**（空档中点）"
              % (float(cfg.get("rag", "reject_max_score") or 0), cand_mx))
    else:
        print("  reject_max_score : 两分布重叠，**换阈值救不了**")
    if sep_av > 0:
        cand_av = round((pct(pos_av, 0.05) + pct(neg_av, 0.95)) / 2, 3)
        print("  reject_avg_score : %.3f  →  **%.3f**（空档中点）"
              % (float(cfg.get("rag", "reject_avg_score") or 0), cand_av))
    else:
        print("  reject_avg_score : 两分布重叠，**换阈值救不了**")

    # ── 模拟新阈值的效果（用已采集的分数，不重新检索）──
    if cand_mx and cand_av:
        print("\n[模拟] 用新阈值重判（两者都低于才拒，与生产逻辑一致）")
        for tag, mx, av in (("切题", pos_mx, pos_av), ("域外", neg_mx, neg_av)):
            rej = sum(1 for m, a in zip(mx, av) if m < cand_mx and a < cand_av)
            print("  %s：拒答 %d/%d" % (tag, rej, len(mx)))
        rej_pos = sum(1 for m, a in zip(pos_mx, pos_av) if m < cand_mx and a < cand_av)
        rej_neg = sum(1 for m, a in zip(neg_mx, neg_av) if m < cand_mx and a < cand_av)
        print("  ⇒ 误拒率 %.1f%%（切题被拒）、拒答准确率 %.1f%%（域外被拒）"
              % (100.0 * rej_pos / len(pos_mx), 100.0 * rej_neg / len(neg_mx)))

    # ── 写回 ──
    if args.apply:
        if not (cand_mx and cand_av):
            print("\n❌ 无可信建议值，不写配置")
            return 1
        if min(sep_mx, sep_av) < args.min_gap:
            print("\n❌ 分离度 %.3f < 要求 %.3f，不写配置" % (min(sep_mx, sep_av), args.min_gap))
            return 1
        import json
        p = cfg.CONFIG_PATH
        d = json.load(open(p, encoding="utf-8"))
        d.setdefault("rag", {})["reject_max_score"] = cand_mx
        d.setdefault("rag", {})["reject_avg_score"] = cand_av
        json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        cfg.reload()
        print("\n✅ 已写入 %s 并 reload" % p)
        print("   生效值：max=%s avg=%s"
              % (cfg.get("rag", "reject_max_score"), cfg.get("rag", "reject_avg_score")))
    else:
        print("\n（未写配置；加 --apply 写回并 reload）")
    print("=" * 86)
    return 0


if __name__ == "__main__":
    sys.exit(main())
