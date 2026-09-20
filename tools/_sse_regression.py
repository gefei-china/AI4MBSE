# -*- coding: utf-8 -*-
"""团队模式流式回归：事件闭环检查（无未闭合 run、done 到达、content 非空）"""
import httpx, json
from collections import Counter

BASE = "http://127.0.0.1:8000"

r = httpx.post(f"{BASE}/api/conversations", json={"title": "回归-团队流式"}, timeout=15)
cid = r.json()["id"]
events = []
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
            if et == "stage":
                events.append(("stage", d.get("name"), d.get("status")))
            elif et == "agent":
                events.append(("agent", str(d.get("name"))[:14], d.get("status")))
            elif et == "tool":
                events.append(("tool", d.get("name"), d.get("status"), d.get("ok")))
            elif et == "skill":
                events.append(("skill", ",".join(d.get("names") or []), d.get("status")))
            elif et == "error":
                events.append(("ERROR", str(d)[:120], ""))
            elif et == "done":
                dd = d.get("data") or {}
                events.append(("done", "content_len=" + str(len(dd.get("content") or "")),
                               "msg_id=" + str(dd.get("message_id"))))

print("--- 主流程事件（非子任务）---")
for e in events:
    if e[0] in ("stage", "agent", "skill", "done", "ERROR") and not (e[0] == "agent" and str(e[1]).startswith("t")):
        print(e)

print("--- 未闭合检查 ---")
pairs = Counter()
for e in events:
    if e[0] in ("stage", "agent"):
        pairs[(e[0], str(e[1]), e[2])] += 1
    elif e[0] == "tool":
        pairs[(e[0], str(e[1]), e[2])] += 1
bad = 0
for (et, name, st), n in sorted(pairs.items()):
    if et == "stage" and st == "run":
        # run 与 done 配对检查
        dn = pairs.get((et, name, "done"), 0)
        if n > dn:
            print("未闭合 stage:", name); bad += 1
    if et == "agent" and st == "run":
        dn = pairs.get((et, name, "done"), 0)
        if n > dn:
            print("未闭合 agent:", name); bad += 1
    if et == "tool" and st == "run":
        dn = pairs.get((et, name, "done"), 0)
        if n > dn:
            print("未闭合 tool:", name); bad += 1
if not bad:
    print("全部事件闭环 ✓")

don = [e for e in events if e[0] == "done"]
print("done 到达:", bool(don), don)
print("错误事件:", [e for e in events if e[0] == "ERROR"] or "无")
