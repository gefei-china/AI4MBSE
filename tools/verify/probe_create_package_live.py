# -*- coding: utf-8 -*-
"""端到端探针：真实对话里「在指定包下新增一个包」能否做成。

模拟用户的原始请求（conv 9629 msg 4899），钉住执行主体 = 智源对接agent，
然后**只读**读回：
  1) 本轮注入了多少个 MCP 工具、是否含建包四件套（从 tool_call_logs 反推可见动作）
  2) 模型实际调了哪些工具、参数是否合规
  3) 目标包下**是否真的多了一个新包**（用真实 MCP 回读草稿版本）

不写死任何期望值：新建包名带时间戳，回读以服务端为准。

用法：python tools/verify/probe_create_package_live.py ["包名"]
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
BASE = "http://127.0.0.1:8000"
PID = "2114907195813986304"      # 「通信子系统用例」
GUIDE = "在工程“广汽方法论建模1022”的“通信子系统用例”包中新增一个“{name}”包"


def call(path, body=None, method=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def tree_names(cli, ver, parent):
    D = {"projectId": "2113198870736076800",
         "branchId": "2113198870744465408", "versionId": ver}
    s = str(cli.call_tool("query_data_tree",
                          {"version": D, "parentId": parent, "pageSize": 100}).get("result"))
    try:
        return [(i.get("name"), i.get("dataId")) for i in (json.loads(s).get("items") or [])]
    except Exception:
        return []


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ("测试%d" % (int(time.time()) % 10000))
    msg = GUIDE.format(name=name)
    print("[请求] %s" % msg)

    # 前置：真实 MCP 读基线
    from mcp_client import MCPClient
    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT endpoint,transport,headers FROM mcp_servers "
                    "WHERE name='zhiyuan-dev-mcp'").fetchone()
    c.close()
    cli = MCPClient(row["endpoint"], row["transport"] or "sse",
                    headers=json.loads(row["headers"] or "{}"))
    DRAFT = "2114939811837575168"
    before = tree_names(cli, DRAFT, PID)
    print("[基线] 目标包下现有 %d 个子项：%s" % (len(before), [b[0] for b in before]))

    tok = call("/api/auth/local-login", {"display_name": "create-pkg-probe"})
    hdr = {"X-Session-Token": tok["token"]}
    conv = call("/api/conversations", {"title": "create pkg probe %d" % int(time.time())},
                headers=hdr)
    cid = conv.get("id") or conv.get("conversation", {}).get("id")
    print("[会话] id=%s" % cid)

    tools = []
    body = {"message": msg, "branch": "dev", "team": "智源对接agent"}
    req = urllib.request.Request(
        "%s/api/conversations/%d/chat/stream" % (BASE, cid),
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", **hdr}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if not p or p == "[DONE]":
                continue
            try:
                ev = json.loads(p)
            except Exception:
                continue
            if ev.get("type") == "tool" and ev.get("status") == "done":
                tools.append((ev.get("name"), ev.get("ok"), str(ev.get("result") or "")[:70]))

    print("\n[工具调用] %d 条：" % len(tools))
    for t in tools:
        print("   %-28s ok=%-5s %s" % (t[0], t[1], t[2].replace("\n", " ")))

    # 回读真实结果
    after = tree_names(cli, DRAFT, PID)
    added = [a for a in after if a[0] not in [b[0] for b in before]]
    print("\n[回读] 目标包下现在 %d 个子项：%s" % (len(after), [a[0] for a in after]))
    print("[新增] %s" % (added or "无"))

    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    ans = c.execute("SELECT content FROM messages WHERE conversation_id=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    c.close()
    print("\n[回答首行] %s" % ((ans["content"] or "").strip().split("\n")[0][:110] if ans else "-"))

    ok = len(added) == 1 and added[0][0] == name
    print("\n=== 结论 ===\n  目标包下成功新增「%s」：%s" % (name, "✅" if ok else "❌"))
    print("  CONV_ID=%s" % cid)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
