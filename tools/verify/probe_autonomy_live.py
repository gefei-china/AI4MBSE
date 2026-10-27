# -*- coding: utf-8 -*-
"""probe_autonomy_live — 真实链路：用户一句「继续写入工程」，系统是否自己跑下去。

复现用户现场（conv 9629，2026-10-27 16:34）：
  用户发「继续写入工程」→ 该轮 `tool_call_logs` **零条 tool**、评估器**没跑**、
  模型只写一段"下一轮我先用名称检索「测试D」"就收手 ⇒ 用户必须自己再发一句。

本探针只发**一句话**，然后看：
  · 是否出现「任务完成度评估」stage（= 评估器真的跑了）
  · 是否出现「自主续跑」stage（= 决定继续）
  · 实际工具调用里有没有 `search_model_elements`（= 它真的去定位父包了，而不是又写一段说明）
  · 若需要拍板，是否发出 `clarify_ask` 卡

用法：python tools/verify/probe_autonomy_live.py [conv_id]
"""
import json
import sqlite3
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
MSG = "继续写入工程"


def call(path, body=None, headers=None, method=None, timeout=120):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip().startswith(("{", "[")) else {}


def db():
    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main():
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 9629
    c = db()
    uid = c.execute("SELECT user_id FROM conversations WHERE id=?", (cid,)).fetchone()["user_id"]
    dn = c.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone()["display_name"]
    m0 = c.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE conversation_id=?",
                   (cid,)).fetchone()[0]
    t0 = c.execute("SELECT COALESCE(MAX(id),0) FROM tool_call_logs WHERE conversation_id=?",
                   (cid,)).fetchone()[0]
    c.close()
    print("[会话] %s（属主 %r）基线 msg=%d tool=%d" % (cid, dn, m0, t0))
    print("[发送] %r" % MSG)

    tok = call("/api/auth/local-login", {"display_name": dn})["token"]
    # ⚠️ local-login 对「库里已存在的 display_name」会**新建一个用户**（id 不同），
    #   于是 `_own_conv` 判非属主 ⇒ 404（不可见即 404）。本机模式（不带 token）放行全部，
    #   探针用这条路更稳、也更贴近"本机单用户"的真实使用形态。
    hdrs = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        "%s/api/conversations/%d/chat/stream" % (BASE, cid),
        data=json.dumps({"message": MSG, "branch": "dev"}, ensure_ascii=False).encode("utf-8"),
        headers=hdrs, method="POST")

    stages, tok_len, clarify = [], 0, None
    t_start = time.time()
    with urllib.request.urlopen(req, timeout=2400) as r:
        ev_type = ""
        for raw in r:
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if line.startswith("event:"):
                ev_type = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev_type == "stage":
                nm = str(ev.get("name") or "")
                if ev.get("status") == "done" and nm:
                    stages.append((nm, str(ev.get("detail") or "")[:70]))
            elif ev_type == "token":
                tok_len += len(ev.get("delta") or "")
            elif ev_type == "clarify_ask":
                clarify = ev
    dur = time.time() - t_start

    print("\n[耗时] %.1fs  [正文] %d 字" % (dur, tok_len))
    print("[环节]")
    for nm, d in stages:
        print("   - %-16s %s" % (nm, d))

    time.sleep(2)
    c = db()
    tools = list(c.execute(
        "SELECT id, tool_name, ok, substr(arguments,1,80) a FROM tool_call_logs "
        "WHERE conversation_id=? AND id>? AND call_kind='tool' ORDER BY id", (cid, t0)))
    c.close()
    print("\n[新增工具调用] %d 条" % len(tools))
    for t in tools:
        print("   id=%s %-28s ok=%s | %s" % (t["id"], t["tool_name"], t["ok"],
                                             (t["a"] or "").replace("\n", " ")))

    names = [s[0] for s in stages]
    evaluated = any("评估" in n for n in names)
    continued = any("续跑" in n for n in names)
    located = any(t["tool_name"] == "search_model_elements" for t in tools)
    any_tool = len(tools) > 0

    print("\n=== 判定 ===")
    print("  评估器已运行（不再被闸门挡掉）：%s" % ("✅" if evaluated else "❌"))
    print("  出现自主续跑环节：%s" % ("✅" if continued else "（未续跑，可能评估为已完成/需拍板）"))
    print("  本轮真的调了工具（不是又只写一段说明）：%s  %d 条" % ("✅" if any_tool else "❌", len(tools)))
    print("  去定位父包（search_model_elements）：%s" % ("✅" if located else "—"))
    print("  需要拍板时弹了澄清卡：%s" % ("✅" if clarify else "（本轮无需拍板）"))

    ok = evaluated and (any_tool or clarify)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
