# -*- coding: utf-8 -*-
"""端到端回归：对话流内「从工程获取数据→转化三元组→写入分支」全链"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json

BASE = "http://127.0.0.1:8000"
r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-拉取入库全链"}, timeout=15)
cid = r.json()["id"]

tool_seen, done_content, err = None, "", None
with httpx.stream("POST", f"{BASE}/api/conversations/{cid}/chat/stream",
                  json={"message": "从智源工程获取数据，转化三元组并写入图库分支",
                        "team": "zhiyuan_mgmt"},
                  headers={"X-User-Id": "1"}, timeout=300) as resp:
    for line in resp.iter_lines():
        if line.startswith("event:"):
            et = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            if et == "tool" and d.get("name", "").endswith("mbse_pull_ingest") and d.get("status") == "done":
                tool_seen = {"ok": d.get("ok"), "result": str(d.get("result"))[:400]}
            elif et == "done":
                done_content = (d.get("data") or {}).get("content") or ""
            elif et == "error":
                err = str(d)[:150]

print("入库工具调用:", json.dumps(tool_seen, ensure_ascii=False) if tool_seen else "未调用 ❌")
print("done 有内容:", bool(done_content), "| 回答头 300 字:")
print(done_content[:300])
print("错误:", err or "无")
