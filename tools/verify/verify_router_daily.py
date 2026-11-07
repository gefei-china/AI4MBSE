# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""P2 每日路由质量快照（2026-11-07 米爸第 15 轮）。

## 为什么
Gartner 2025（经 Plura 综述）：「**每天看置信分数分布**是路由退化的领先指标」；
44% 表现不佳的部署「从不随产品变化更新意图分类」。
`verify_router_quality.py` 是一次性门禁，缺**趋势**：今天 ECE 0.09、明天 0.15
单看都可能在阈值内，但翻倍就是退化信号。

## 做法
- 追加写 `tools/tmp/router_quality_history.jsonl`（append-only），每行含时间戳、
  准确率、ECE、高置信档准确率、confirmed 覆盖率、suggested 积压、按 route 命中率。
- 与**上一份快照**对比，报出变化方向；超过阈值给 WARN（不是 FAIL，运营项）。
- 只读业务表 + 读校准回放结果，不改任何业务数据；可被每日定时任务直接调用。

用法：python -X utf8 tools/verify/verify_router_daily.py [--snapshot]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
HIST = os.path.join(ROOT, "tools", "tmp", "router_quality_history.jsonl")
CALIB = os.path.join(ROOT, "tools", "tmp", "calibration_shadow_result.json")
DB = os.path.join(ROOT, "mbse.db")

WARNED = []


def rec(code, ok, detail="", warn_only=False):
    if ok:
        print("[PASS] %-4s %s" % (code, detail))
    elif warn_only:
        print("[WARN] %-4s %s" % (code, detail))
        WARNED.append(code)
    else:
        print("[FAIL] %-4s %s  <<<" % (code, detail))


def _read_last():
    if not os.path.exists(HIST):
        return None
    try:
        lines = [l for l in open(HIST, encoding="utf-8").read().splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def main(write_snapshot: bool = True) -> int:
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    total = conn.execute("SELECT COUNT(*) FROM intent_samples").fetchone()[0]
    conf = conn.execute("SELECT COUNT(*) FROM intent_samples WHERE status='confirmed'").fetchone()[0]
    sugg = conn.execute("SELECT COUNT(*) FROM intent_samples WHERE status='suggested'").fetchone()[0]
    n_agent = conn.execute("SELECT COUNT(*) FROM agents WHERE status='active'").fetchone()[0]
    n_override = conn.execute("SELECT COUNT(*) FROM intent_samples WHERE source='override'").fetchone()[0]
    n_override_pending = conn.execute(
        "SELECT COUNT(*) FROM intent_samples WHERE source='override' AND status!='confirmed'"
    ).fetchone()[0]
    conn.close()

    calib = {}
    if os.path.exists(CALIB):
        calib = json.load(open(CALIB, encoding="utf-8"))
    snap = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "samples_total": total, "confirmed": conf, "suggested": sugg,
        "confirmed_per_intent": round(conf / max(n_agent, 1), 2),
        "overrides": n_override, "overrides_pending": n_override_pending,
        "accuracy": calib.get("accuracy"),
        "ece_raw": calib.get("ece_raw"), "ece_calibrated": calib.get("ece_calibrated"),
        "high_raw": calib.get("high_raw"), "high_calibrated": calib.get("high_calibrated"),
        "reliability": calib.get("reliability", {}),
    }
    print("=" * 80)
    print("P2 每日路由质量快照  %s" % snap["ts"])
    print("=" * 80)
    print("  样本池 %d 条 | confirmed %d (%.2f/意图) | suggested %d | 改选待审 %d" %
          (total, conf, snap["confirmed_per_intent"], sugg, n_override_pending))
    if calib:
        print("  准确率 %.1f%% | ECE %.4f→%.4f(校准后) | 高置信档 acc %.1f%%" %
              ((calib.get("accuracy") or 0) * 100, calib.get("ece_raw") or 0,
               calib.get("ece_calibrated") or 0,
               (calib.get("high_raw") or {}).get("acc", 0) * 100))
        print("  按 route 真实命中率（前 6）：")
        for k, v in sorted((calib.get("reliability") or {}).items(),
                           key=lambda kv: -kv[1].get("n", 0))[:6]:
            print("    %-22s n=%-3d acc=%.2f" %
                  (k, v.get("n", 0), v.get("hit", 0) / max(v.get("n", 1), 1)))

    prev = _read_last()
    failed = []
    rec("D1", bool(calib), "有校准实测结果（先跑 verify_calibration_shadow.py）")
    if not calib:
        print("ALL_OK=False（缺校准结果）")
        return 1
    rec("D2", calib.get("ece_calibrated", 1) <= calib.get("ece_raw", 0),
        "校准后 ECE 不升：%.4f → %.4f" %
        (calib.get("ece_raw", 0), calib.get("ece_calibrated", 0)))
    rec("D3", n_agent > 0 and total > 0, "样本池与 Agent 池非空")

    if prev:
        pe, ae = prev.get("ece_raw"), snap.get("ece_raw")
        if isinstance(pe, (int, float)) and isinstance(ae, (int, float)) and pe > 0:
            delta = ae - pe
            rec("D4", delta <= 0.03,
                "ECE 环比 %+.4f（阈值 +0.03；上一份 %s）" % (delta, prev.get("ts")),
                warn_only=True)
        pa, aa = prev.get("accuracy"), snap.get("accuracy")
        if isinstance(pa, (int, float)) and isinstance(aa, (int, float)):
            rec("D5", aa >= pa - 0.02,
                "准确率环比 %+.1fpt（上一份 %.1f%%）" %
                ((aa - pa) * 100, pa * 100), warn_only=True)
        dc = snap["confirmed"] - int(prev.get("confirmed") or 0)
        ds = snap["suggested"] - int(prev.get("suggested") or 0)
        print("  环比：confirmed %+d / suggested %+d / 改选待审 %+d" %
              (dc, ds, n_override_pending - int(prev.get("overrides_pending") or 0)))
        rec("D6", dc >= 0, "confirmed 样本未减少（%+.0f）" % dc, warn_only=True)
    else:
        print("  （首份快照，无环比基线）")

    if write_snapshot:
        os.makedirs(os.path.dirname(HIST), exist_ok=True)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        print("  已追加快照 -> tools/tmp/router_quality_history.jsonl")
    print("=" * 80)
    if failed:
        print("FAIL %s" % failed)
        return 1
    print("ALL_OK=True  （WARN %d 项为运营改进项）" % len(WARNED))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
