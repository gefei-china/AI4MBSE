# -*- coding: utf-8 -*-
"""真实建模链路验证（一次手动运行，不是门禁）。

目的：**在真实服务上**走一遍「建模页面 → 对话 → 工具调用 → 落库 → 账本/追溯」，
拿到真数据核对四个环节，而不是靠夹具或打桩。

⚠️ 不写死任何结果：所有断言都是"读回来比对"，字段为空就报空，
   **绝不在验证脚本里补数据**（那等于把待验证的东西自己造出来）。
用法：python tools/verify/probe_capability_chain_live.py "问题文本"
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
MSG = sys.argv[1] if len(sys.argv) > 1 else "检索知识库中关于需求分析与需求追踪的内容，给出三条关键结论"


def call(path, body=None, method=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    t0 = time.time()
    tok = call("/api/auth/local-login", {"display_name": "capchain-probe"})
    tk = tok["token"]
    hdr = {"X-Session-Token": tk}
    print("[1] login ok user=%s" % tok["user"]["display_name"])

    conv = call("/api/conversations", {"title": "capchain probe %d" % int(time.time())}, headers=hdr)
    cid = conv.get("id") or conv.get("conversation", {}).get("id")
    print("[2] conversation id=%s" % cid)

    # ── SSE 流式对话（与建模页面同一条路径）──
    events, n_tool, n_stage, done = 0, 0, 0, None
    body = {"message": MSG, "branch": "dev"}
    req = urllib.request.Request(
        "%s/api/conversations/%d/chat/stream" % (BASE, cid),
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", **hdr}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            events += 1
            et = ev.get("type")
            if et == "tool":
                if ev.get("status") == "done":
                    n_tool += 1
                    print("    [tool] %-26s ok=%s %ss" % (
                        ev.get("name"), ev.get("ok"), ev.get("latency_ms")))
            elif et == "stage":
                n_stage += 1
            elif et == "done":
                done = ev
            elif et == "error":
                print("    [ERROR] %s" % ev.get("message"))
    print("[3] stream done: events=%d stages=%d tools=%d elapsed=%.1fs"
          % (events, n_stage, n_tool, time.time() - t0))
    if done:
        d = done.get("data") or {}
        print("    done.intent=%r agent=%r" % (d.get("intent"), d.get("agent")))
        print("    answer head: %s" % str(d.get("content") or d.get("answer") or "")[:160].replace("\n", " "))
    print("[4] conversation_id=%s（后续查库用这个）" % cid)
    return cid


if __name__ == "__main__":
    main()
