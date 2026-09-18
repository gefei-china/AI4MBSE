# -*- coding: utf-8 -*-
"""回归：LLM rerank 生效（rerank_score/reranked 字段 + 排序变化）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db
from knowledge_engine import hybrid_search

conn = get_db()
try:
    r = hybrid_search(conn, "热管理系统 温度控制 策略", top_k=5)
    hits = r.get("hits") or []
    print("hits:", len(hits))
    for h in hits[:5]:
        print(f"  - {h.get('source_doc')} | {str(h.get('section'))[:24]} | rrf={h.get('score')} "
              f"| rerank={h.get('rerank_score')} | reranked={h.get('reranked')}")
    reranked_n = sum(1 for h in hits if h.get("reranked"))
    print("LLM 重排生效:", reranked_n > 0, f"（{reranked_n}/{len(hits)} 带重排分）")
finally:
    conn.close()
