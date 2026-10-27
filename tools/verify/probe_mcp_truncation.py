# -*- coding: utf-8 -*-
"""决定性实验：MCP 返回的 READ 信封到底有多长、能否解析、能否被归档拦截识别。

不经过 LLM，直接用**真实** MCPClient 调 export_sysml_v2 的 READ，
再喂给**真实** extract_sysml_doc_from_result —— 复现「产物库为空」的判定链。

用法：python tools/verify/probe_mcp_truncation.py [taskId] [offset]
"""
import json
import sqlite3
import sys

sys.path.insert(0, ".")

DB = "mbse.db"
TASK = sys.argv[1] if len(sys.argv) > 1 else "2114940498336481282"
OFFSET = int(sys.argv[2]) if len(sys.argv) > 2 else 0


def main():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT name,endpoint,transport,headers FROM mcp_servers "
                    "WHERE name='zhiyuan-dev-mcp'").fetchone()
    c.close()
    if not row:
        print("mcp_servers 无 zhiyuan-dev-mcp")
        return
    hdr = {}
    try:
        hdr = json.loads(row["headers"] or "{}") or {}
    except Exception:
        pass
    print("=== MCP client ===")
    print("  endpoint=%s transport=%s headers=%s" % (row["endpoint"], row["transport"], list(hdr.keys())))

    from mcp_client import MCPClient
    from core import config
    print("  config mcp.max_response = %r" % config.get("mcp", "max_response"))
    cli = MCPClient(row["endpoint"], row["transport"] or "sse", headers=hdr)
    print("  client.max_response = %r" % cli.max_response)

    args = {"action": "READ", "taskId": TASK, "offset": OFFSET, "maxBytes": 32768}
    print("\n=== call_tool(export_sysml_v2, %s) ===" % json.dumps(args, ensure_ascii=False))
    res = cli.call_tool("export_sysml_v2", args)
    txt = str(res.get("result") or "")
    print("  ok=%s  返回长度=%d  (cap=%s)" % (res.get("ok"), len(txt), cli.max_response))
    print("  head: %s" % txt[:150].replace("\n", " "))
    print("  tail: %r" % txt[-90:])
    parsed = None
    try:
        parsed = json.loads(txt)
        print("  json.loads -> OK, keys=%s" % list(parsed.keys()))
        if isinstance(parsed, dict):
            body = parsed.get("content")
            print("  content len=%s | eof=%r | sizeBytes=%r | nextOffset=%r" % (
                len(body) if isinstance(body, str) else None,
                parsed.get("eof"), parsed.get("sizeBytes"), parsed.get("nextOffset")))
    except Exception as e:
        print("  json.loads -> FAIL（%s）⇒ 结构化信封已被截断" % e)

    from agent.utils import extract_sysml_doc_from_result
    doc = extract_sysml_doc_from_result("export_sysml_v2", txt)
    print("\n=== extract_sysml_doc_from_result ===")
    print("  -> %s" % ("None（不归档）" if not doc else
                      {"title": doc["title"], "text_len": len(doc["text"])}))


if __name__ == "__main__":
    main()
