# -*- coding: utf-8 -*-
"""真实链路探针：工具取回的 SysML 全文是否**真的**进了产物库（一次性，非门禁）。

背景：会话 9629 末轮助手称「V2 代码已取回并归档」，但 artifacts 里 9629 = 0 条。
本探针在真实服务上发一轮「读取已完成导出任务的文件」请求，然后：
  1) 抓 SSE 里是否出现 stage「取回模型文件 / 已自动存入产物」（= 拦截命中的显式信号）
  2) 只读读回 artifacts（按本次 conversation_id 关联）
不写死任何期望值 —— 一切以读回为准。

用法：python tools/verify/probe_artifact_ingest_live.py [taskId]
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
TASK = sys.argv[1] if len(sys.argv) > 1 else "2114940498336481282"
MSG = ("请**必须真实调用工具** export_sysml_v2，action=READ，taskId=%s，maxBytes=32768，"
       "分三次读：offset 依次为 0、32767、65534（服务端响应里的 nextOffset 就是下一次的 offset）。"
       "三段都读完后，用一句话说明文件第一行和最后一行分别是什么。"
       "不要凭记忆回答，也不要重新 PREVIEW/EXPORT。" % TASK)


def call(path, body=None, method=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    tok = call("/api/auth/local-login", {"display_name": "artifact-probe"})
    hdr = {"X-Session-Token": tok["token"]}
    conv = call("/api/conversations", {"title": "artifact ingest probe %d" % int(time.time())},
                headers=hdr)
    cid = conv.get("id") or conv.get("conversation", {}).get("id")
    print("[1] conversation id=%s" % cid)

    stages, tools = [], []
    # team=执行主体（主 Agent）——把本轮钉在「智源对接agent」上，
    # 否则语义路由会落到通用 chat agent（没有绑定 MCP，永远不调工具，探针变成空跑）。
    body = {"message": MSG, "branch": "dev", "team": "智源对接agent"}
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
            t = ev.get("type")
            if t == "stage" and ev.get("name"):
                stages.append((ev.get("name"), ev.get("status"), str(ev.get("detail") or "")[:120]))
            elif t == "tool":
                tools.append((ev.get("name"), ev.get("ok"), str(ev.get("result") or "")[:80]))

    print("\n[2] SSE stage 事件（关键：取回模型文件）")
    for s in stages:
        mark = "  <<<" if "取回" in str(s[0]) or "产物" in str(s[2]) else ""
        print("   %s%s %s%s" % (s[0], "[" + str(s[1]) + "]", s[2], mark))
    print("\n[3] SSE tool 事件 %d 条" % len(tools))
    for t in tools:
        print("   %s ok=%s %s" % (t[0], t[1], t[2].replace("\n", " ")))

    # 只读读回
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    print("\n[4] artifacts（本次会话）")
    rows = list(c.execute("SELECT id,kind,title,length(preview_content) pl,meta,created_at "
                          "FROM artifacts WHERE conversation_id=? ORDER BY id", (cid,)))
    if not rows:
        print("   ❌ 0 条 —— 工具取回的文档没有进产物库")
    for r in rows:
        print("   id=%s kind=%s title=%s content_len=%s meta=%s"
              % (r["id"], r["kind"], r["title"], r["pl"], r["meta"]))
    print("\n[5] 本轮 tool_call_logs")
    for r in c.execute("SELECT id,tool_name,tool_type,call_kind,ok,run_id FROM tool_call_logs "
                       "WHERE conversation_id=? ORDER BY id", (cid,)):
        print("   ", dict(r))
    c.close()
    print("\nCONV_ID=%s" % cid)


if __name__ == "__main__":
    main()
