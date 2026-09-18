# -*- coding: utf-8 -*-
"""诊断：hybrid_search 返回 + detect 兜底命中源"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json

from database import get_db
conn = get_db()

print("=== hybrid_search 直调 ===")
try:
    from knowledge_engine import hybrid_search
    r = hybrid_search(conn, "需求工程 方法", top_k=8)
    print("hits:", len(r.get("hits") or []), "| bm25_count:", r.get("bm25_count"), "| vec_count:", r.get("vec_count"), "| mode:", r.get("mode"))
    for h in (r.get("hits") or [])[:3]:
        print("  -", h.get("source_doc"), "|", h.get("section"), "| score:", round(h.get("score", 0), 3))
except Exception as e:
    print("hybrid_search 异常:", repr(e)[:200])

print()
print("=== detect 兜底命中源 ===")
from agent.intent import IntentRouter
router = IntentRouter()
t = "这个功能的质量怎么样"
# 逐层探测（复刻 detect 顺序）
for k in ("需求质量", "质量评审", "需求质量评审", "质量分析", "模糊词", "不可验证"):
    if k in t:
        print("强信号命中:", k)
print("intent_rules:", router._rules)
print("db_intents:", {k: v for k, v in (router._db_intents or {}).items() if "质量" in str(v)})
hits = [(intent, [kw for kw in kws if kw in t]) for intent, kws in getattr(router, "INTENTS", {}).items() if any(kw in t for kw in kws)]
print("INTENTS 兜底命中:", hits)
conn.close()
