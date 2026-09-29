# -*- coding: utf-8 -*-
"""多意图识别**评测**（2026-09-26）：把"多意图准不准"变成可复跑的数字。

## 报什么
- **二分指标**（是否多意图）：accuracy / precision / recall / F1 —— 漏判与误判各有代价：
  漏判 → 用户说了三件事只干一件；误判 → 单件事被拆成多步编排（慢且乱）。
- **阶段序列完全匹配率**：不仅"判成多意图"，还要**顺序与内容都对**（`sequence` 保序契约）。
- 按 tag 分组（L1/L2/隐式并列/负例/边界）—— 定位短板在哪一类。
- 逐例明细（期望 vs 实得），错例直接可读。

用法（真实 AgentPipeline，含 DB Agent 关键词与语义索引）：
    .venv/Scripts/python.exe -X utf8 tests/manual_verify/eval_multi_intent.py
"""
import os
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from agent.pipeline import AgentPipeline  # noqa: E402
from multi_intent_cases import CASES, TAG_DESC  # noqa: E402

pipe = AgentPipeline()
pipe._load_db_agents()          # 真实请求路径同款注入（否则路由池是退化态）
rt = pipe.router

rows = []
for text, want, tag in CASES:
    m = rt.detect_multi(text)
    got = (m or {}).get("sequence") or None
    rows.append({"text": text, "want": want, "got": got, "tag": tag,
                 "want_multi": want is not None, "got_multi": got is not None})

tp = sum(1 for r in rows if r["want_multi"] and r["got_multi"])
fp = sum(1 for r in rows if not r["want_multi"] and r["got_multi"])
fn = sum(1 for r in rows if r["want_multi"] and not r["got_multi"])
tn = sum(1 for r in rows if not r["want_multi"] and not r["got_multi"])
prec = tp / (tp + fp) if (tp + fp) else 0.0
rec = tp / (tp + fn) if (tp + fn) else 0.0
f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
acc = (tp + tn) / len(rows)

print("== 逐例 ==")
for r in rows:
    ok = r["got"] == r["want"]
    mark = "  " if ok else "❌"
    print("%s 「%s」" % (mark, r["text"]))
    print("     期望=%s\n     实得=%s   [%s]" % (r["want"], r["got"], r["tag"]))

print("\n== 二分指标（是否判为多意图）==")
print("  TP=%d FP=%d FN=%d TN=%d" % (tp, fp, fn, tn))
print("  accuracy=%.3f  precision=%.3f  recall=%.3f  F1=%.3f" % (acc, prec, rec, f1))

pos = [r for r in rows if r["want_multi"]]
seq_ok = sum(1 for r in pos if r["got"] == r["want"])
print("\n== 阶段序列完全匹配（仅正例）==")
print("  %d/%d = %.3f" % (seq_ok, len(pos), seq_ok / len(pos) if pos else 0.0))

print("\n== 分组 ==")
for tag in TAG_DESC:
    g = [r for r in rows if r["tag"] == tag]
    if not g:
        continue
    ok = sum(1 for r in g if r["got"] == r["want"])
    print("  %-12s %d/%d   （%s）" % (tag, ok, len(g), TAG_DESC[tag]))

print("\n== 错例清单（希望数字提升时改这里面的）==")
bad = [r for r in rows if r["got"] != r["want"]]
for r in bad:
    kind = ("漏判" if (r["want_multi"] and not r["got_multi"])
            else "误判" if (not r["want_multi"] and r["got_multi"]) else "序列不符")
    print("  [%s][%s] 「%s」 期望=%s 实得=%s" % (kind, r["tag"], r["text"], r["want"], r["got"]))

print("\n[RESULT]" + __import__("json").dumps(
    {"accuracy": round(acc, 4), "precision": round(prec, 4), "recall": round(rec, 4),
     "f1": round(f1, 4), "seq_match": round(seq_ok / len(pos), 4) if pos else 0.0,
     "n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn}, ensure_ascii=False))
sys.exit(0)