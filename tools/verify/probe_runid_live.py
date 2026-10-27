# -*- coding: utf-8 -*-
"""run_id 归正 · 真实链路探针（一次性，非门禁）。

在真实服务上走一遍「对话 → 工具调用 → 落库」，然后**只读**读回
tool_call_logs（按 conversation_id 关联），核对主路径（单 Agent 直行）的
run_id 不再恒为 0。

纪律：不写死结果 —— 全部读回比对；绝不在此脚本里补 run_id。
用法：python tools/verify/probe_runid_live.py "问题文本"
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "mbse.db")
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
    tok = call("/api/auth/local-login", {"display_name": "runid-probe"})
    tk = tok["token"]
    hdr = {"X-Session-Token": tk}
    print("[1] login ok user=%s" % tok["user"]["display_name"])

    conv = call("/api/conversations", {"title": "runid probe %d" % int(time.time())}, headers=hdr)
    cid = conv.get("id") or conv.get("conversation", {}).get("id")
    print("[2] conversation id=%s" % cid)

    n_tool = 0
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
            if ev.get("type") == "tool" and ev.get("status") == "done":
                n_tool += 1
                print("    [tool] %-26s ok=%s %ss" % (ev.get("name"), ev.get("ok"), ev.get("latency_ms")))
    print("[3] stream done: tools=%d elapsed=%.1fs" % (n_tool, time.time() - t0))

    # ── 只读读回：该会话工具调用的 run_id 分布 ──
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT run_id, COUNT(*) n FROM tool_call_logs WHERE conversation_id=? GROUP BY run_id",
        (cid,)).fetchall()
    total = sum(r["n"] for r in rows)
    print("[4] tool_call_logs for conversation_id=%s: total=%d" % (cid, total))
    nonzero = 0
    for r in rows:
        print("     run_id=%-6s count=%d" % (r["run_id"], r["n"]))
        if int(r["run_id"] or 0) != 0:
            nonzero += r["n"]
    c.close()

    print("[5] 结论：")
    if total == 0:
        print("    ⚠ 该会话未产生工具调用（可能模型未触发工具）；run_id 归正需另择触发工具的问题验证。")
    elif nonzero == total:
        print("    ✅ 主路径 run_id 全部非 0（%d/%d）—— run_id 归正生效" % (nonzero, total))
    else:
        print("    ❌ 仍有 %d/%d 行 run_id=0 —— 归正未完全生效" % (total - nonzero, total))


if __name__ == "__main__":
    main()
