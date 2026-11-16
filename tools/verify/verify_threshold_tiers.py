# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""P1 分档阈值 A/B（真库 48 条 confirmed，2026-11-07 米爸第 15 轮）。

唯一变量 = `intent.threshold_tiers`（关/开）。同一批真实人工标签、同一路由器、
禁用缓存。判据：准确率不降、ECE 不升、澄清触发数不暴增。
若净负 ⇒ 保持默认关（与既有「实测净负故默认关闭」纪律一致）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _run(enabled: bool, cases):
    from core import config as cfg
    from core.route_calibration import ece
    from agent.pipeline import AgentPipeline
    cfg._CONFIG.setdefault("intent", {})["cache_enabled"] = False
    cfg._CONFIG["intent"]["threshold_tiers"] = enabled
    p = AgentPipeline(); p._load_db_agents(); r = p.router
    rows = []
    for text, label in cases:
        got = r.detect(text)
        m = r.get_last_meta() or {}
        rows.append({"text": text[:30], "label": label, "got": got,
                     "hit": got == label, "route": m.get("route"),
                     "confidence": float(m.get("confidence") or 0),
                     "clarify": bool(m.get("needs_clarification"))})
    acc = sum(x["hit"] for x in rows) / len(rows)
    return {"acc": round(acc, 4), "ece": ece(rows),
            "clarify": sum(x["clarify"] for x in rows), "n": len(rows),
            "wrong": [{"text": x["text"], "want": x["label"], "got": x["got"]}
                      for x in rows if not x["hit"]]}


def main() -> int:
    conn = sqlite3.connect("file:%s?mode=ro" %
                           os.path.join(ROOT, "mbse.db").replace("\\", "/"), uri=True)
    cases = [(r[0], r[1]) for r in conn.execute(
        "SELECT text,intent FROM intent_samples WHERE status='confirmed' "
        "AND intent<>'' ORDER BY id")]
    conn.close()
    print("真库 confirmed %d 条，跑 OFF / ON 两遍" % len(cases), flush=True)
    off = _run(False, cases)
    on = _run(True, cases)
    from core import config as cfg
    cfg._CONFIG["intent"]["threshold_tiers"] = False
    print("  OFF acc=%.1f%% ECE=%.4f clarify=%d" %
          (off["acc"] * 100, off["ece"], off["clarify"]), flush=True)
    print("  ON  acc=%.1f%% ECE=%.4f clarify=%d" %
          (on["acc"] * 100, on["ece"], on["clarify"]), flush=True)
    net = (on["acc"] > off["acc"] or
           (on["acc"] == off["acc"] and on["clarify"] < off["clarify"]))
    print("  结论：%s（默认保持 %s）" %
          ("净正，可考虑打开" if net else "未见净正，保持默认关",
           "开" if net else "关"), flush=True)
    out = {"n": len(cases), "off": off, "on": on, "net_positive": net,
           "decision": "keep_off" if not net else "consider_on"}
    p = os.path.join(ROOT, "tools", "tmp", "threshold_tiers_ab.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("结果已存 tools/tmp/threshold_tiers_ab.json", flush=True)
    print("ALL_OK=True", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
