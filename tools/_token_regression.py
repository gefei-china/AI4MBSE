# -*- coding: utf-8 -*-
"""回归：done 事件带 token 统计（llm.tokens）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json

BASE = "http://127.0.0.1:8000"
r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-token统计"}, timeout=15)
cid = r.json()["id"]
llm_info, done_ok, content_len = None, False, 0
with httpx.stream("POST", f"{BASE}/api/conversations/{cid}/chat/stream",
                  json={"message": "用一句话说明MBSE中需求追溯的作用"},
                  headers={"X-User-Id": "1"}, timeout=180) as resp:
    for line in resp.iter_lines():
        if line.startswith("event:"):
            et = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            if et == "done":
                dd = d.get("data") or {}
                llm_info = dd.get("llm") or {}
                done_ok = True
                content_len = len(dd.get("content") or "")

print("done:", done_ok, "| content_len:", content_len)
print("llm:", json.dumps(llm_info, ensure_ascii=False)[:200])
tokens = (llm_info or {}).get("tokens") or {}
print("tokens:", tokens, "| 有统计:", bool(tokens.get("prompt") or tokens.get("completion")))
