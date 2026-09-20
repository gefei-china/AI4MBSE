# -*- coding: utf-8 -*-
"""回归：指定智能体（forced_intent）时意图识别仍然执行"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json

BASE = "http://127.0.0.1:8000"
r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-强制意图识别"}, timeout=15)
cid = r.json()["id"]

intent_evt, clarify_evt, done_ok = None, None, False
with httpx.stream("POST", f"{BASE}/api/conversations/{cid}/chat/stream",
                  json={"message": "帮我看看这个系统大概是怎么设计的",
                        "forced_intent": "zhiyuan_mgmt"},   # 指定智能体
                  headers={"X-User-Id": "1"}, timeout=180) as resp:
    for line in resp.iter_lines():
        if line.startswith("event:"):
            et = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            if et == "stage" and d.get("name") == "意图识别" and d.get("status") == "done":
                intent_evt = d
            elif et == "clarify":
                clarify_evt = d
            elif et == "done":
                done_ok = True

print("意图识别事件:", json.dumps({k: intent_evt.get(k) for k in ("intent", "route", "confidence")}, ensure_ascii=False) if intent_evt else "缺失 ❌")
print("澄清提示:", json.dumps({k: clarify_evt.get(k) for k in ("intent", "detected", "confidence")}, ensure_ascii=False) if clarify_evt else "未触发")
print("执行 Agent（intent）:", (intent_evt or {}).get("intent"), "| done:", done_ok)
print("结论: 指定智能体时意图识别", "已执行 ✓" if intent_evt and intent_evt.get("route") else "未执行 ❌")
