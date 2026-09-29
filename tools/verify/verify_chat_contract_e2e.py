# -*- coding: utf-8 -*-
"""前后端一致性验证（2026-09-25）：用**真实一轮对话**产出真实 card_data.exec，
供浏览器端核对「执行详情」面板显示的数字/内容是否与库中一致（此前只用合成夹具验证过渲染）。

流程：建临时会话 → 真跑一次 /chat/stream（消费完整 SSE）→ 从库里读该 assistant 消息的
exec（tools/reasoning/agent）与 used_mock → 输出给驱动层做浏览器断言基准；最后删会话。

用法：.venv/Scripts/python.exe -X utf8 tools/verify/verify_chat_contract_e2e.py
"""
import json
import sqlite3
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
DB = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system\mbse.db"
# 复用模式：python verify_chat_contract_e2e.py --reuse <conv_id>
#   （上次真跑一轮后若想复核库中数据，可跳过真实 LLM 调用；--keep 保留会话不删）
REUSE = None
KEEP = "--keep" in sys.argv
if "--reuse" in sys.argv:
    REUSE = int(sys.argv[sys.argv.index("--reuse") + 1])


def post(path, body, stream=False):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=300)


def main():
    # 1) 建临时会话（或复用 --reuse 指定的会话，跳过真实 LLM 调用）
    if REUSE:
        cid = REUSE
        print(f"[1] 复用会话 #{cid}")
    else:
        with post("/api/conversations", {"title": "E2E-CONTRACT-真实执行详情"}) as r:
            cid = json.loads(r.read().decode())["id"]
        print(f"[1] 临时会话 #{cid}")

    mid = None
    used_mock = None
    if not REUSE:
        # 2) 真跑一轮（消费完整 SSE，直到 done）
        msg = "请用一句话说明：SysML v2 中需求与用例的追溯关系一般如何表达？"
        events = {}
        with post(f"/api/conversations/{cid}/chat/stream", {"message": msg, "attachments": []}) as r:
            buf = ""
            for raw in r:
                buf += raw.decode("utf-8", "replace")
                while "\n\n" in buf:
                    blk, buf = buf.split("\n\n", 1)
                    ev, data = "", ""
                    for line in blk.splitlines():
                        if line.startswith("event: "):
                            ev = line[7:].strip()
                        elif line.startswith("data: "):
                            data = line[6:]
                    if not ev:
                        continue
                    events[ev] = events.get(ev, 0) + 1
                    if ev == "done":
                        # ⚠️ SSE 的 data 是**事件信封** {"type":"done","data":{...}}（此前少剥一层，
                        #    误判成"后端没回 message_id"——实为测试脚本 bug，后端一直正常）
                        env = json.loads(data)
                        done_data = env.get("data", env)
        print(f"[2] SSE 事件统计: {events}")
        mid = done_data.get("message_id")
        used_mock = (done_data.get("llm") or {}).get("used_mock")
        print(f"[3] done: message_id={mid} used_mock={used_mock} content_len={len(done_data.get('content') or '')}")
        if not mid:
            print("  ✗ done 未携带 message_id")
            return 1
    else:
        # 复用模式：从库里取该会话最后一条 assistant 消息
        c0 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            mid = c0.execute("SELECT id FROM messages WHERE conversation_id=? AND role='assistant' "
                             "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()[0]
        finally:
            c0.close()
        print(f"[2] 复用消息 #{mid}（跳过真实 LLM 调用）")

    # 3) 直查库：该消息的 card_data.exec 真实内容（浏览器端断言以此为准）
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = c.execute("SELECT card_data, msg_type FROM messages WHERE id=?", (mid,)).fetchone()
    finally:
        c.close()
    cd = json.loads(row[0] or "{}")
    ex = cd.get("exec") or {}
    tools = ex.get("tools") or []
    real = {
        "conv_id": cid,
        "message_id": mid,
        "used_mock": bool(used_mock),
        "msg_type": row[1],
        "agent": ex.get("agent") or cd.get("agent") or "",
        "intent": cd.get("intent") or "",
        "reasoning_len": len(ex.get("reasoning") or ""),
        "tool_count": len(tools),
        "tool_names": [t.get("name") for t in tools][:5],
        "tool_ok": [t.get("ok") for t in tools][:5],
        "skill_hits": len(cd.get("skill_hits") or []),
        "source": cd.get("source") or "",
        "provider": cd.get("provider") or "",
    }
    print("[4] 库中真实执行数据: " + json.dumps(real, ensure_ascii=False))
    print("[RESULT]" + json.dumps(real, ensure_ascii=False))   # 驱动层解析这一行
    if not KEEP and not REUSE:
        urllib.request.urlopen(urllib.request.Request(
            f"{BASE}/api/conversations/{cid}", method="DELETE"), timeout=30)
        print(f"[5] 已清理临时会话 #{cid}")
    else:
        print(f"[5] 保留会话 #{cid}（供浏览器端一致性核对；核对后请手动删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())