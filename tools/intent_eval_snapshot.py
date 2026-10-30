# -*- coding: utf-8 -*-
"""意图评测跑批器 → 产出 checked-in 快照（P0-3，2026-10-30）。

## 为什么要"跑批器 + 快照"而不是"CI 里直接跑"
与 `verify_rag_eval_gate.py` 同因：CI 跑在干净的 checkout 上，
**`mbse.db` 不入 git** ⇒ 样本池、意图缓存、Agent 配置都不在，评测无从跑起（跑出来是 0，不是"质量差"）。
所以分工固定为：
- **本脚本（nightly / 手动）**：跑真实路由，产出 `docs/评测/nightly/latest-intent-report.json`；
- **门禁（`verify_intent_eval_ci.py`）**：只做判定，消费快照，PR 上零成本。

## 三个集合，**分开报**，永不混算
| 集合 | 来源 | 标签怎么来的 | 能回答什么 | 不能回答什么 |
|---|---|---|---|---|
| `gold` | 人工确认（`status='confirmed'`） | 人 | 绝对水平（区间宽） | 细粒度回归（n 太小） |
| `silver` | 生产语句 + 双视图一致 | 两个视图的 LLM 独立标注取一致 | 回归检测（n 大、区间窄） | 绝对水平（与路由器同源 ⇒ 系统性高估） |
| `synth` | 手写改写（`source='aug'`） | 由构造决定（写这句话就是为了表达某意图） | 边界覆盖 / 改写鲁棒性 | 生产分布（措辞比真实输入规整） |

**为什么禁止混算**：三者的难度分布显著不同（实测 synth 准确率远低于 gold）。
混成一个数会得到一个"既不代表生产、也看不出回归"的指标 —— 那种数字最容易被当成成绩单。

## 用法
    python tools/intent_eval_snapshot.py                    # 跑全部集合
    python tools/intent_eval_snapshot.py --sets gold,silver # 只跑指定集合
    python tools/intent_eval_snapshot.py --limit 20         # 每集合只跑前 N 条（快速体检）
"""
import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "docs", "评测", "nightly")
OUT = os.path.join(OUT_DIR, "latest-intent-report.json")

SETS = {
    # 金标：只有人能给的标签
    "gold": "SELECT text, intent FROM intent_samples "
            "WHERE status='confirmed' AND intent<>'' AND COALESCE(text,'')<>'' ORDER BY id",
    # 银标：生产语句 + 双视图一致（排除 aug，避免与 synth 集重叠）
    "silver": "SELECT text, intent FROM intent_samples "
              "WHERE created_by='dual_label_v1' AND intent<>'' AND COALESCE(source,'')<>'aug' "
              "AND COALESCE(text,'')<>'' ORDER BY id",
    # 合成：手写改写，标签由构造决定（用它原始的 label，不用双视图结论 —— 见 --report）
    "synth": "SELECT text, intent FROM intent_samples "
             "WHERE COALESCE(source,'')='aug' AND intent<>'' AND COALESCE(text,'')<>'' ORDER BY id",
}


def _h(t):
    return hashlib.sha1((t or "").encode("utf-8")).hexdigest()[:12]


def _macro_f1(pairs):
    by = collections.defaultdict(lambda: [0, 0, 0])   # want -> [tp, fp, fn]
    labels = {w for w, _ in pairs} | {g for _, g in pairs}
    for w, g in pairs:
        if w == g:
            by[w][0] += 1
        else:
            by[g][1] += 1
            by[w][2] += 1
    f1s = []
    for lb in labels:
        tp, fp, fn = by[lb]
        if tp == 0 and fp == 0 and fn == 0:
            continue
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1s.append(2 * p * r / (p + r) if (p + r) else 0.0)
    return round(sum(f1s) / len(f1s), 4) if f1s else 0.0


def run_set(name, sql, limit=0):
    from agent.intent import IntentRouter
    from core.stats import wilson_ci
    from database import get_db

    c = get_db()
    rows = [(r[0], r[1]) for r in c.execute(sql).fetchall()]
    c.close()
    if limit:
        rows = rows[:limit]

    # ★ 必须清意图缓存：`intent_cache` 是**持久化 DB 表**（不是进程内存），
    #   上一次跑批/上一次线上请求留下的结论会直接短路 detect ⇒
    #   "改了代码但评测分数纹丝不动"或"改了但看不出变化"都可能源于它。
    #   清它是安全的：缓存可从原文重建（宁可多花一次调用，也不让评测读到旧结论）。
    try:
        _cc = get_db()
        try:
            _n = _cc.execute("DELETE FROM intent_cache").rowcount
            _cc.commit()
        finally:
            _cc.close()
        print("    已清意图缓存 %d 行（防旧结论短路）" % (_n or 0))
    except Exception as _e:      # noqa: BLE001
        print("    [warn] 清缓存失败（不影响评测，但可能读到旧结论）：%s" % str(_e)[:80])

    rt = IntentRouter()
    pairs, outs, t0 = [], [], time.time()
    for txt, want in rows:
        try:
            c = get_db()
            got = rt.detect(txt, conn=c)
            m = rt.get_last_meta() or {}
            c.close()
        except Exception as e:      # noqa: BLE001
            got, m = "", {"route": "exc", "confidence": 0.0, "err": str(e)[:80]}
        pairs.append((want, got))
        outs.append({"h": _h(txt), "want": want, "got": got,
                     "ok": bool(want == got), "route": m.get("route"),
                     "conf": round(float(m.get("confidence") or 0), 3)})
        if len(outs) % 25 == 0:
            print("    … %s %d/%d（%.0fs）" % (name, len(outs), len(rows), time.time() - t0))

    n = len(pairs)
    k = sum(1 for w, g in pairs if w == g)
    lo, hi = wilson_ci(k, n)
    return {
        "n": n, "correct": k,
        "accuracy": round(k / n, 4) if n else 0.0,
        "ci95": [round(lo, 4), round(hi, 4)],
        "width_pts": round((hi - lo) * 100, 2) if n else 0.0,
        "macro_f1": _macro_f1(pairs),
        "confusion": {w: dict(collections.Counter(g for ww, g in pairs if ww == w))
                      for w in sorted({w for w, _ in pairs})},
        "elapsed_s": round(time.time() - t0, 1),
        "outcomes": outs,
    }


def _commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=ROOT, text=True).strip()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="gold,silver,synth")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()

    names = [x.strip() for x in a.sets.split(",") if x.strip()]
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "commit": _commit(), "db": "mbse.db", "sets": {}}
    try:
        from core import config as _c
        report["default_chat_provider"] = str(_c.get("llm", "default_chat_provider", "") or "")
    except Exception:
        pass

    for nm in names:
        if nm not in SETS:
            print("跳过未知集合：%s" % nm)
            continue
        print("\n=== 集合 %s ===" % nm)
        report["sets"][nm] = run_set(nm, SETS[nm], a.limit)
        s = report["sets"][nm]
        print("  n=%d acc=%.4f CI95=[%.1f%%, %.1f%%] 宽 %.1f 点 macro-F1=%.4f  %.1fs"
              % (s["n"], s["accuracy"], s["ci95"][0] * 100, s["ci95"][1] * 100,
                 s["width_pts"], s["macro_f1"], s["elapsed_s"]))

    report["corpus_n"] = sum(v["n"] for v in report["sets"].values())
    print("\n语料合计 %d 条（%s）" % (report["corpus_n"], ", ".join(
        "%s=%d" % (k, v["n"]) for k, v in report["sets"].items())))

    if a.no_write:
        print("[--no-write] 未落盘")
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("已写 %s" % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
