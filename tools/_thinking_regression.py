# -*- coding: utf-8 -*-
"""回归：LLM 真实思考（reasoning_content）是否透传到前端 reasoning 事件"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json

BASE = "http://127.0.0.1:8000"
r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-思考透传"}, timeout=15)
cid = r.json()["id"]

think_chars, think_events, done_ok, err = 0, 0, False, None
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
            if et == "reasoning" and d.get("phase") in ("probe", "llm"):
                think_events += 1
                think_chars += len(d.get("delta") or "")
            elif et == "error":
                err = str(d)[:120]
            elif et == "done":
                dd = d.get("data") or {}
                done_ok = len(dd.get("content") or "") > 0

print(f"LLM 思考事件（probe/llm）: {think_events} 次，共 {think_chars} 字符")
print(f"done 到达且有内容: {done_ok}")
print(f"错误: {err or '无'}")
