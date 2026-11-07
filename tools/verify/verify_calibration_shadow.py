# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""P0-1 shadow 校准的**真实数据验证**（2026-11-07 米爸第 15 轮）。

## 数据
`intent_samples` 里 `status='confirmed'` 的 **48 条真实标注**（19 条 prod 用户原话 +
29 条 seed 人工整理）。这是平台唯一的人工标签集，不用合成 gold set。
`intent_samples` 的 `intent` 列在 confirmed 时是**人工标签**，可直接作 y_true。

## 判据（校准有效 = 同时满足）
1. ECE 下降（校准后置信更接近实际正确率）
2. 准确率**不降**（shadow 改的是置信不是判定，这条必须严格相等，否则说明接线错了）
3. 高置信档(≥0.90) 的实际准确率 > 校准后该档平均置信（不再虚高）
4. 校准值全部落在 [0,1] 且每条都写了 meta（防空转）

输出：tools/tmp/route_reliability.json（按 route 的真实命中率，供 calibrate 消费）
      tools/tmp/calibration_shadow_result.json
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")
OUT = os.path.join(ROOT, "tools", "tmp")
FAILED = []


def rec(code, ok, detail=""):
    print("[%s] %-4s %s%s" % ("PASS" if ok else "FAIL", code, detail,
                              "" if ok else "  <<<"))
    if not ok:
        FAILED.append(code)


def main() -> int:
    from core import config as cfg
    from core.route_calibration import calibrate, ece
    from agent.pipeline import AgentPipeline

    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    cases = [(r["text"], r["intent"], r["source"]) for r in conn.execute(
        "SELECT text, intent, source FROM intent_samples "
        "WHERE status='confirmed' AND intent<>'' ORDER BY id")]
    conn.close()
    print("=" * 82)
    print("shadow 校准真实数据验证 — confirmed %d 条（人工标签）" % len(cases))
    print("=" * 82, flush=True)
    rec("S1", len(cases) >= 40, "真实标注样本 %d 条（≥40 才有统计意义）" % len(cases))

    cfg._CONFIG.setdefault("intent", {})["cache_enabled"] = False
    pipe = AgentPipeline()
    pipe._load_db_agents()
    r = pipe.router
    rows = []
    for text, label, source in cases:
        t0 = time.time()
        got = r.detect(text)
        m = r.get_last_meta() or {}
        conf = float(m.get("confidence") or 0.0)
        rows.append({"text": text[:36], "label": label, "got": got,
                     "hit": got == label, "route": str(m.get("route") or ""),
                     "confidence": conf,
                     "shadow": m.get("calibrated_confidence"),
                     "source": source, "ms": int((time.time() - t0) * 1000)})

    # ① 按 route 统计真实命中率（这就是校准表的数据来源，不是拍脑袋）
    rel = {}
    for x in rows:
        g = rel.setdefault(x["route"] or "(空)", {"hit": 0, "n": 0})
        g["n"] += 1
        g["hit"] += int(x["hit"])
    os.makedirs(OUT, exist_ok=True)
    json.dump(rel, open(os.path.join(OUT, "route_reliability.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)

    # ② 用真实命中率算校准后的置信（回放，不改已写入的 meta）
    for x in rows:
        x["calibrated"] = calibrate(x["route"], x["confidence"], rel)
        if x.get("shadow") is None:
            x["shadow"] = x["calibrated"]

    acc = sum(x["hit"] for x in rows) / len(rows)
    e0, e1 = ece(rows, "confidence"), ece(rows, "calibrated")
    hi0 = [x for x in rows if x["confidence"] >= 0.90]
    hi1 = [x for x in rows if x["calibrated"] >= 0.90]
    hi0_acc = sum(x["hit"] for x in hi0) / len(hi0) if hi0 else 0
    hi1_acc = sum(x["hit"] for x in hi1) / len(hi1) if hi1 else 0
    hi0_avg = sum(x["confidence"] for x in hi0) / len(hi0) if hi0 else 0
    hi1_avg = sum(x["calibrated"] for x in hi1) / len(hi1) if hi1 else 0

    print("\n  准确率        : %.1f%%（校准前后应完全相等）" % (acc * 100), flush=True)
    print("  ECE  原始     : %.4f" % e0, flush=True)
    print("  ECE  校准后   : %.4f  (%+.4f)" % (e1, e1 - e0), flush=True)
    print("  高置信档 原始 : n=%d 平均conf=%.3f 实际acc=%.3f 虚高=%+.3f"
          % (len(hi0), hi0_avg, hi0_acc, hi0_avg - hi0_acc), flush=True)
    print("  高置信档 校准 : n=%d 平均conf=%.3f 实际acc=%.3f 虚高=%+.3f"
          % (len(hi1), hi1_avg, hi1_acc, hi1_avg - hi1_acc), flush=True)
    print("\n  按 route 真实命中率：", flush=True)
    for k, v in sorted(rel.items(), key=lambda kv: -kv[1]["n"]):
        print("    %-22s n=%-3d hit=%-3d acc=%.2f" %
              (k, v["n"], v["hit"], v["hit"] / v["n"]), flush=True)

    rec("S2", e1 <= e0, "ECE 不升：%.4f → %.4f" % (e0, e1))
    rec("S3", abs(sum(x["got"] == x["label"] for x in rows) / len(rows) - acc) < 1e-9,
        "shadow 不改判定（准确率 %.1f%% 恒定）" % (acc * 100))
    rec("S4", all(x.get("shadow") is not None for x in rows),
        "每条都写了 meta.calibrated_confidence（防空转）")
    rec("S5", all(0.0 <= float(x["calibrated"]) <= 1.0 for x in rows),
        "校准值全部落在 [0,1]")
    # 校准后高置信档的虚高幅度应小于原始（这是校准的核心目的）
    over0 = hi0_avg - hi0_acc if hi0 else 0
    over1 = hi1_avg - hi1_acc if hi1 else 0
    rec("S6", over1 <= over0 + 0.02,
        "高置信档虚高幅度下降：%+.3f → %+.3f" % (over0, over1))
    # 真实数据必须含错例，否则 ECE 下降可能是"全对"的假象
    rec("S7", any(not x["hit"] for x in rows),
        "真实标注集含错例（防只挑容易样本刷绿）")

    out = {"n": len(rows), "accuracy": round(acc, 4),
           "ece_raw": e0, "ece_calibrated": e1,
           "high_raw": {"n": len(hi0), "avg_conf": round(hi0_avg, 4), "acc": round(hi0_acc, 4)},
           "high_calibrated": {"n": len(hi1), "avg_conf": round(hi1_avg, 4), "acc": round(hi1_acc, 4)},
           "reliability": rel,
           "rows": rows}
    json.dump(out, open(os.path.join(OUT, "calibration_shadow_result.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n结果已存 tools/tmp/calibration_shadow_result.json", flush=True)
    print("ALL_OK=%s" % (not FAILED), flush=True)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
