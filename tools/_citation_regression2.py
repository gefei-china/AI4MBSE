# -*- coding: utf-8 -*-
"""回归：意图收紧 + citations 完整链路（card_data.citations）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json

BASE = "http://127.0.0.1:8000"

print("=== 1) 意图收紧验证 ===")
from agent.intent import IntentRouter
router = IntentRouter()
for text in ("这个功能的质量怎么样", "帮我做需求质量评审", "系统响应要快速稳定，做需求质量分析"):
    intent = router.detect(text)
    meta = getattr(router, "_last_meta", {})
    print(f"  「{text[:18]}」→ {intent}（route={meta.get('route')}, conf={meta.get('confidence')}）")

print()
print("=== 2) RAG citations 链路（#标签直行）===")
r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-证据链路2"}, timeout=15)
cid = r.json()["id"]
route, card_cites, done_ok = None, None, False
with httpx.stream("POST", f"{BASE}/api/conversations/{cid}/chat/stream",
                  json={"message": "#知识库 知识库里关于需求工程的方法有哪些？"},
                  headers={"X-User-Id": "1"}, timeout=180) as resp:
    for line in resp.iter_lines():
        if line.startswith("event:"):
            et = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            if et == "stage" and d.get("name") == "知识库检索" and d.get("status") == "done":
                route = d.get("route")
            elif et == "done":
                dd = d.get("data") or {}
                done_ok = bool(dd.get("content"))
                card = dd.get("card") or {}
                card_cites = card.get("citations")
                if card_cites is None:
                    # 兼容：citations 可能在 data 顶层
                    card_cites = dd.get("citations")
print(f"检索路由: {route} | done: {done_ok}")
cc = card_cites or []
print(f"citations 条数: {len(cc)}")
if cc:
    c0 = cc[0]
    print("citations[0]:", json.dumps({k: c0.get(k) for k in ("source_doc", "chunk_index", "section", "score")}, ensure_ascii=False))
    print("chunk 内容长度:", len(c0.get("content") or ""))
