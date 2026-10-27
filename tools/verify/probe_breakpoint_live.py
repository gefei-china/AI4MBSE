# -*- coding: utf-8 -*-
"""probe_breakpoint_live — 真实链路：**中途掐断连接**，验证已流出内容是否落库。

复现用户报的现象（conv 9629 15:34:39「写入工程」）：
  · `llm_usage_stats` 有 4 次调用（内容确实生成过）
  · `messages` **0 条 assistant**、`orch_stream_events` **0 条**
  ⇒ 用户"明明看到输出"，刷新/切回会话却什么都没有。

本探针的做法：订阅真实 SSE，收到**第一个 token** 就把 socket 关掉
（等价于浏览器切页/关标签/网络 reset → 服务端 GeneratorExit）。
然后回查两张表：
  · `messages` 是否新增了 assistant 且含已流出文本（断点保护的正文侧）
  · `orch_stream_events` 是否新增（断点保护的**归因**侧 —— 旧代码这里也是 0 条）

用法：python tools/verify/probe_breakpoint_live.py [conv_id]
"""
import json
import socket
import sqlite3
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


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
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    # ── 身份：会话属主（_own_conv 对非属主 404，不可见即 404）──
    tok = call("/api/auth/local-login", {"display_name": "断点保护探针"})["token"]
    if not cid:
        conv = call("/api/conversations",
                    {"title": "breakpoint probe %d" % int(time.time())},
                    headers={"X-Session-Token": tok})
        cid = conv.get("id") or (conv.get("conversation") or {}).get("id")
    print("[会话] id=%s" % cid)

    c = db()
    m0 = c.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE conversation_id=?",
                   (cid,)).fetchone()[0]
    e0 = c.execute("SELECT COALESCE(MAX(id),0) FROM orch_stream_events WHERE conversation_id=?",
                   (cid,)).fetchone()[0]
    c.close()
    print("[基线] max(message_id)=%d max(event_id)=%d" % (m0, e0))

    # ── 发起真实 SSE，收到第一个 token 就硬断 ──
    # 用 `Connection: close` + 直接关 socket：绕过客户端优雅关闭，
    # 让服务端在 `yield` 处拿到真正的断连（与浏览器 AbortController 同效果）。
    payload = json.dumps({"message": "请写一段 800 字左右的技术说明，主题是模型驱动系统工程的价值",
                          "branch": "dev"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "%s/api/conversations/%d/chat/stream" % (BASE, cid),
        data=payload,
        headers={"Content-Type": "application/json", "X-Session-Token": tok,
                 "Connection": "close"},
        method="POST")
    streamed, cut = "", False
    resp = urllib.request.urlopen(req, timeout=600)
    try:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") == "token":
                streamed += ev.get("delta") or ""
                if len(streamed) >= 24:      # 用户已经"看到了一段内容"
                    cut = True
                    break
    finally:
        resp.close()
        # 再补一刀：直接关底层 socket，确保服务端看到的是断连而不是正常读完
        try:
            sock = resp.fp.raw._sock  # noqa: SLF001
            sock.close()
        except Exception:
            pass
    print("[断连] 已收到 %d 字后掐断（cut=%s）" % (len(streamed), cut))
    print("[已流出片段] %r" % streamed[:60])

    # 服务端在另一线程收尾，给足时间（persist 是同步小事务）
    time.sleep(6)

    c = db()
    new_msgs = list(c.execute(
        "SELECT id, role, length(content) L, substr(content,-80) tail FROM messages "
        "WHERE conversation_id=? AND id>? ORDER BY id", (cid, m0)))
    new_evs = c.execute(
        "SELECT COUNT(*) FROM orch_stream_events WHERE conversation_id=? AND id>?",
        (cid, e0)).fetchone()[0]
    c.close()

    print("\n[落库] 新增消息 %d 条" % len(new_msgs))
    body = ""
    for m in new_msgs:
        tail = (m["tail"] or "").replace("\n", " ")
        print("   id=%s %-9s %6d 字 | 尾: %s" % (m["id"], m["role"], m["L"], tail))
        if m["role"] == "assistant":
            body += m["tail"] or ""
    print("[事件] 新增 orch_stream_events %d 条" % new_evs)

    c = db()
    row = c.execute("SELECT content FROM messages WHERE conversation_id=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    full_body = (row["content"] if row else "") or ""
    c.close()

    ok_body = bool(streamed.strip()) and (streamed.strip()[:10] in full_body)
    ok_mark = ("中断" in full_body) or ("已生成内容" in full_body)
    ok_ev = new_evs > 0

    print("\n=== 判定 ===")
    print("  正文已保活（含已流出片段）：%s" % ("✅" if ok_body else "❌"))
    print("  带中断说明（用户能分辨状态）：%s" % ("✅" if ok_mark else "❌"))
    print("  归因事件已落库（能回答跑到哪一步）：%s  %d 条" % ("✅" if ok_ev else "❌", new_evs))
    return 0 if (ok_body and ok_mark) else 1


if __name__ == "__main__":
    sys.exit(main())
