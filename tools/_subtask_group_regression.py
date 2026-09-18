# -*- coding: utf-8 -*-
"""回归：子任务内部工具事件带 key 归属（t1:xxx），reasoning 带 key 标记"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json

BASE = "http://127.0.0.1:8000"
r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-子任务归组"}, timeout=15)
cid = r.json()["id"]

tool_keyed, reasoning_keyed, subtask_n, done_ok = 0, 0, 0, False
with httpx.stream("POST", f"{BASE}/api/conversations/{cid}/chat/stream",
                  json={"message": "查询智源当前工程的包结构树，告诉我顶层有哪些包",
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
            if et == "tool" and d.get("status") == "done":
                if ":" in str(d.get("name", "")):
                    tool_keyed += 1
            elif et == "reasoning":
                if d.get("key"):
                    reasoning_keyed += 1
            elif et == "subtask" and d.get("status") == "done":
                subtask_n += 1
            elif et == "done":
                done_ok = len((d.get("data") or {}).get("content") or "") > 0

print(f"带 key 的工具事件（done）: {tool_keyed}")
print(f"带 key 的思考事件: {reasoning_keyed}")
print(f"完成子任务: {subtask_n}")
print(f"done 到达且有内容: {done_ok}")
