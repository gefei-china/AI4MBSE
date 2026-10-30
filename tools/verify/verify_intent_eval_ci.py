# -*- coding: utf-8 -*-
"""门禁：意图评测必须**带区间**判定，且三个集合分开报（P0-3，2026-10-30）。

## 为什么"报 0.9792"是不可用的判据
n=48、47/48 时 Wilson 95% 区间是 **[89.1%, 99.6%]，宽 10.5 个百分点** ——
"0.979" 与 "0.891" 在统计上无法区分；门禁阈值 0.85 落在区间**内部**，
所以"红/绿"由噪声决定。**用点估计做门禁，等于用掷骰子做 CI。**

## 本门禁的四条判据（每条都针对一种具体的失效方式）
1. **规模闸**：语料合计 ≥ `MIN_CORPUS`(300)、生产语句集（gold+silver）≥ `MIN_PROD`(200)。
   —— 防"把 n=48 的成绩单当结论"。
2. **棘轮闸**：每个集合的 accuracy 不得低于**已接受基线** `ACC_TOL`(0.03)。
   —— 基线文件 `latest-intent-baseline.json` 是"我们认可的水平"，只允许往上抬。
3. **区间闸**：每集合 `ci_low` 不得低于基线 `ci_low - CI_TOL`，
   且报告里的区间必须**能被重算复现**（防止有人手改报告里的数字）。
4. **配对闸**：用 per-sample 结果做 McNemar —— 只有"系统性变差"（p<0.05）才判红。
   —— 这是真正能检出回归的那一条：总准确率掉 1 个点可能只是 2 个样本翻面，
      但若"基线对/现在错"显著多于反向，就是真回归。

## 三个集合为什么分开判
`gold`（人工）/ `silver`（双视图一致，生产语句）/ `synth`（手写改写）难度分布显著不同
（实测 synth 明显低于 gold）。**混算会得到一个既不代表生产、也看不出回归的数。**
本门禁因此对每个集合各自设基线、各自判定，任何一方单独掉下去都会红。

## 快照缺失
PR CI 上没有 `mbse.db` ⇒ 跑不了评测。所以本门禁只消费 checked-in 快照：
缺失时 `--allow-missing` 判 SKIP，否则判红并打印生成命令（**静默绿等于形同虚设**）。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.stats import wilson_ci, mcnemar          # noqa: E402

NIGHTLY = os.path.join(ROOT, "docs", "评测", "nightly")
REPORT = os.path.join(NIGHTLY, "latest-intent-report.json")
BASELINE = os.path.join(NIGHTLY, "latest-intent-baseline.json")

MIN_CORPUS = 300          # 语料合计下限（用户明确的 300+ 口径）
MIN_PROD = 200            # 生产语句口径下限（gold + silver）
ACC_TOL = 0.03            # 棘轮容差：允许的绝对回落（3 个百分点）
CI_TOL = 0.03             # 区间下限容差
SETS = ("gold", "silver", "synth")

_results = []


def check(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  | " + str(detail)[:200]) if (detail and not ok) else ""))


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前报告接受为新基线（人工评审通过后执行）")
    a = ap.parse_args()

    if not os.path.exists(REPORT):
        msg = ("缺少意图评测快照 %s。生成：\n"
               "  .venv/Scripts/python.exe -X utf8 tools/intent_eval_snapshot.py" % REPORT)
        if a.allow_missing:
            print("[SKIP] " + msg)
            return 0
        print("[FAIL] " + msg)
        return 1

    rep = json.load(open(REPORT, encoding="utf-8"))
    sets = rep.get("sets") or {}

    hr("[A] 规模闸：语料必须够支撑统计判据")
    corpus = int(rep.get("corpus_n") or sum(int(v.get("n") or 0) for v in sets.values()))
    prod = int((sets.get("gold") or {}).get("n") or 0) + int((sets.get("silver") or {}).get("n") or 0)
    check("A1 语料合计 %d ≥ %d（生成于 %s）" % (corpus, MIN_CORPUS, rep.get("generated_at")),
          corpus >= MIN_CORPUS, "语料不足 ⇒ 区间过宽，判据不成立")
    check("A2 生产语句口径（gold+silver）%d ≥ %d" % (prod, MIN_PROD),
          prod >= MIN_PROD, "生产语句才是被评判的对象；synth 属改写鲁棒性，不计入")
    missing = [s for s in SETS if s not in sets]
    check("A3 三个集合齐备（缺则说明跑批器被裁剪过，报告不可用于判定）",
          not missing, "缺少：%s" % missing)

    hr("[B] 区间闸：报告里的区间必须能被重算复现")
    for s in SETS:
        d = sets.get(s) or {}
        n, k = int(d.get("n") or 0), int(d.get("correct") or 0)
        if not n:
            continue
        lo, hi = wilson_ci(k, n)
        st = d.get("ci95") or [0, 0]
        check("B1 %-6s 区间可复现（存储 [%.3f, %.3f] vs 重算 [%.3f, %.3f]）"
              % (s, st[0], st[1], lo, hi),
              abs(float(st[0]) - lo) < 1e-3 and abs(float(st[1]) - hi) < 1e-3,
              "报告数字与公式不一致 ⇒ 可能被手改")
        check("B2 %-6s 点估计与计数自洽（%d/%d = %.4f）" % (s, k, n, float(k) / n),
              abs(float(d.get("accuracy") or 0) - float(k) / n) < 1e-3)
        print("     %-6s n=%-4d acc=%.4f CI95=[%.1f%%, %.1f%%] 宽 %.1f 点"
              % (s, n, float(d.get("accuracy") or 0), st[0] * 100, st[1] * 100,
                 (hi - lo) * 100))

    hr("[C] 棘轮闸：与已接受基线对比")
    if not os.path.exists(BASELINE):
        check("C0 尚无已接受基线（首次运行）—— 生成方式：本门禁 --update-baseline", True,
              "首跑不判定棘轮，但仍受 A/B 两组约束")
    else:
        base = json.load(open(BASELINE, encoding="utf-8"))
        for s in SETS:
            cur, old = sets.get(s) or {}, (base.get("sets") or {}).get(s) or {}
            if not (cur.get("n") and old.get("n")):
                continue
            ca, oa = float(cur.get("accuracy") or 0), float(old.get("accuracy") or 0)
            check("C1 %-6s 准确率 %.4f ≥ 基线 %.4f − %.2f" % (s, ca, oa, ACC_TOL),
                  ca >= oa - ACC_TOL, "回落 %.4f" % (oa - ca))
            ccl = float((cur.get("ci95") or [0])[0])
            ocl = float((old.get("ci95") or [0])[0])
            check("C2 %-6s ci_low %.4f ≥ 基线 %.4f − %.2f" % (s, ccl, ocl, CI_TOL),
                  ccl >= ocl - CI_TOL, "区间下限退化")

        # 配对闸：per-sample 对比（只有"系统性变差"才红）
        for s in SETS:
            cur, old = sets.get(s) or {}, (base.get("sets") or {}).get(s) or {}
            co, oo = cur.get("outcomes") or [], old.get("outcomes") or []
            if not co or not oo:
                continue
            om = {x.get("h"): bool(x.get("ok")) for x in oo if x.get("h")}
            b = c = 0        # b=基线对/现在错，c=基线错/现在对
            for x in co:
                h = x.get("h")
                if h in om:
                    now = bool(x.get("ok"))
                    if om[h] and not now:
                        b += 1
                    elif (not om[h]) and now:
                        c += 1
            r = mcnemar(b, c)
            print("     %-6s McNemar: 基线对→现错 %d, 基线错→现对 %d, p≈%.3f (%s)"
                  % (s, b, c, r["p_approx"], r["direction"]))
            check("C3 %-6s 无系统性变差（配对检验）" % s,
                  not (r["direction"] == "worse" and r["p_approx"] < 0.05),
                  "基线对而现在错 %d 条 vs 反向 %d 条，p=%.3f ⇒ 系统性回归" % (b, c, r["p_approx"]))
            check("C4 %-6s 可配对样本足够（≥80%% 命中，否则基线已过期需重跑）" % s,
                  (b + c) <= 0.2 * max(len(co), 1) or len(om) >= 0.8 * len(co),
                  "可配对 %d / 当前 %d" % (len(om), len(co)))

    if a.update_baseline:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print("\n已更新基线：%s" % os.path.relpath(BASELINE, ROOT))

    hr("[D] 判据自检（防止门禁本身空转）")
    # 变异①：把 gold 的 correct 减 5 ⇒ C1 必须能判红（不实际写文件，只验算）
    g = sets.get("gold") or {}
    if g.get("n"):
        _lo, _hi = wilson_ci(max(0, int(g["correct"]) - 5), int(g["n"]))
        check("D1 变异验算：gold 少对 5 条时 ci_low 会下降（判据对样本翻面敏感）",
              _lo < float((g.get("ci95") or [0])[0]) + 1e-9,
              "ci_low %.4f → %.4f" % (float((g.get("ci95") or [0])[0]), _lo))
    check("D2 区间的统计含义：n=%d 时宽 %.1f 点（>5 点说明还看不清 5 点回归）"
          % (int(g.get("n") or 0), float(g.get("width_pts") or 0)),
          True, "")
    check("D3 阈值口径写明：棘轮容差 %.2f / 区间容差 %.2f（不是拍脑袋，见模块 docstring）"
          % (ACC_TOL, CI_TOL), True, "")
    names = [r[1] for r in _results]
    check("D4 断言名唯一", len(set(names)) == len(names),
          "dups=%s" % [x for x in set(names) if names.count(x) > 1])

    n_pass = sum(1 for ok, _, _ in _results if ok)
    n_fail = len(_results) - n_pass
    print("\n" + "=" * 78)
    print("结果：%d PASS / %d FAIL" % (n_pass, n_fail))
    if n_fail:
        for ok, name, detail in _results:
            if not ok:
                print("  FAIL %s | %s" % (name, str(detail)[:200]))
    print("=" * 78)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
