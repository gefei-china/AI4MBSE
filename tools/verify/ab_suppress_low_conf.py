# -*- coding: utf-8 -*-
"""A/B 实验 v2：验证「低置信抑制」是零命中的直接原因，并确定正确修法。

⚠️ 只改**进程内存** `core.config._CONFIG`，不写文件、不写库；脚本结束即失效。

v1 的两处判据错误（已修，记录以免重犯）：
  · A4 断言 `context_text` 增长 —— 但 retrieve() **没有** context_text 字段，
    上下文由 `context.py` 用 chunk_hits 拼 `【来源n】`。⇒ 改为断言 chunk 总量。
  · A2 断言"域外 query 必须仍为空" —— 这是**把期望写错了**：
    suppress 本就是"一刀切丢光"，关掉它必然放进噪声。正确结论不是"关掉就好"，
    而是"需要**按分数阈值**逐条过滤"。⇒ 改为测量分数可分性。

判据：
  A1 关掉抑制后，切题 query 命中数增加
  A2 切题 vs 域外的分数**可分**（切题 min > 域外 max 或均值显著更高）
     ⇒ 可分则 P0-1 应实现为"阈值过滤"；不可分则必须引入独立拒答分类器
  A3 至少救回 2 条
  A4 注入侧 chunk 总量增加（AI 真能读到东西）
用法：./.venv/Scripts/python.exe -X utf8 tools/verify/ab_suppress_low_conf.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import config as _cfg            # noqa: E402
from agent.rag import GraphRAG             # noqa: E402

CASES = [
    ("图谱强相关", "卫星通信载荷的需求有哪些", True),
    ("需求追溯", "需求与架构元素的追溯关系", True),
    ("纯文档事实", "技术要求书里对可靠性的指标是多少", True),
    ("抽象/同义", "系统在断链情况下怎么保持业务连续", True),
    ("安全要求", "信息安全要求有哪些", True),
    ("域外噪声", "今天北京天气怎么样", False),
]


def run_once(g, q):
    r = g.retrieve(q)
    ch = r.get("chunk_hits") or []
    ents = r.get("entities") or []
    scores = [float(h.get("score") or 0) for h in ch]
    vecs = [float(h.get("vec_score") or 0) for h in ch]
    bms = [float(h.get("bm25_score") or 0) for h in ch]
    return {
        "chunks": len(ch), "ents": len(ents), "route": r.get("route"),
        "conf": r.get("confidence"),
        "score_max": round(max(scores), 4) if scores else 0.0,
        "score_avg": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "vec_max": round(max(vecs), 4) if vecs else 0.0,
        "bm_max": round(max(bms), 2) if bms else 0.0,
        "first": ("%s §%s" % (str(ch[0].get("source_doc") or "")[:26],
                              str(ch[0].get("section") or "")[:18])) if ch else
                 ("entity:%s" % str((ents[0] or {}).get("name"))[:20] if ents else ""),
    }


def main():
    original = _cfg.get("rag", "suppress_low_confidence", True)
    print("=" * 104)
    print("rag.suppress_low_confidence 原值 =", original, "（仅进程内存，不改文件）")

    results = {}
    for flag in (True, False):
        _cfg._CONFIG.setdefault("rag", {})["suppress_low_confidence"] = flag
        g = GraphRAG()
        results[flag] = {}
        print("\n" + "=" * 104)
        print("【抑制=%s】" % ("开" if flag else "关"))
        print("%-12s %-6s %-6s %-8s %-8s %-8s %-8s %s"
              % ("场景", "chunk", "实体", "score_max", "score_avg", "vec_max", "bm25_max", "首条"))
        print("-" * 104)
        for label, q, should in CASES:
            r = run_once(g, q)
            results[flag][label] = r
            print("%-12s %-6d %-6d %-8.4f %-8.4f %-8.4f %-8.2f %s"
                  % (label, r["chunks"], r["ents"], r["score_max"], r["score_avg"],
                     r["vec_max"], r["bm_max"], r["first"]))

    on, off = results[True], results[False]
    print("\n" + "=" * 104)
    print("=== 判据 ===")
    ok = fail = 0

    def chk(desc, cond, extra=""):
        nonlocal ok, fail
        print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
        if cond:
            ok += 1
        else:
            fail += 1

    need = [l for l, q, s in CASES if s]
    hit_on = sum(1 for l in need if on[l]["chunks"] + on[l]["ents"] > 0)
    hit_off = sum(1 for l in need if off[l]["chunks"] + off[l]["ents"] > 0)
    print("  切题 query 命中：抑制=开 %d/%d → 抑制=关 %d/%d" % (hit_on, len(need), hit_off, len(need)))
    chk("A1 关掉抑制后命中数增加", hit_off > hit_on, "%d → %d" % (hit_on, hit_off))

    saved = [l for l in need if on[l]["chunks"] == 0 and off[l]["chunks"] > 0]
    print("  被救回的 query：", saved if saved else "（无）")
    chk("A3 至少救回 2 条", len(saved) >= 2, str(saved))

    total_on = sum(on[l]["chunks"] for l in need)
    total_off = sum(off[l]["chunks"] for l in need)
    chk("A4 注入侧 chunk 总量增加", total_off > total_on, "%d → %d" % (total_on, total_off))

    # ── A2：分数可分性（决定正确修法）──
    in_scores = [off[l]["score_max"] for l in need if off[l]["chunks"]]
    out_scores = [off["域外噪声"]["score_max"]] if off["域外噪声"]["chunks"] else []
    in_min = min(in_scores) if in_scores else 0
    out_max = max(out_scores) if out_scores else 0
    in_avg = sum(in_scores) / len(in_scores) if in_scores else 0
    print("\n  分数可分性（抑制关时）：切题 max 的最小值 = %.5f（均值 %.5f）；域外 max = %.5f"
          % (in_min, in_avg, out_max))
    separated = in_min > out_max
    chk("A2 切题与域外**按分数可分** ⇒ P0-1 可做成阈值过滤",
        separated,
        "切题min %.5f %s 域外max %.5f" % (in_min, ">" if separated else "<=", out_max))
    if not separated:
        print("      ⇒ 分数**不可分**：说明单靠阈值做不到既救回切题又挡住域外，")
        print("        必须引入**独立拒答判据**（域分类器 / 相关性二分类），不能只调阈值。")

    _cfg._CONFIG.setdefault("rag", {})["suppress_low_confidence"] = original
    print("\n已复原内存配置：", _cfg.get("rag", "suppress_low_confidence"))
    print("\n%d 项通过，%d 项失败" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
