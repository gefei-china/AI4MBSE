# -*- coding: utf-8 -*-
"""回归：RAG 检索触发 + citations 证据链路（[n] 标注 + chunk 引用）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json

BASE = "http://127.0.0.1:8000"
r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-证据链路"}, timeout=15)
cid = r.json()["id"]

route, cite_n, done_ok, content_head = None, 0, False, ""
with httpx.stream("POST", f"{BASE}/api/conversations/{cid}/chat/stream",
                  json={"message": "#知识库 知识库里关于需求工程的方法有哪些？[1]"},
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
                cites = dd.get("citations") or []
                cite_n = len(cites)
                content_head = (dd.get("content") or "")[:200]
                if cites:
                    print("citations[0]:", json.dumps(cites[0], ensure_ascii=False)[:220])

print(f"检索路由: {route} | citations 条数: {cite_n} | done: {done_ok}")
print("回答头 200 字:", content_head.replace(chr(10), ' '))
