# -*- coding: utf-8 -*-
"""probe_partial_persist — 复现「中途失败后已生成内容全部消失」。

场景（真库 conv 9629，2026-10-27 15:34:39 用户发「写入工程」）：
  · `llm_usage_stats` 有 4 条调用（18545/18546/18547/18548，最后一条 22,707 prompt、929 completion）
  · 但 `messages` 里**没有任何 assistant 消息**、`orch_stream_events` 也**一条都没有**
  ⇒ 模型确实生成了内容（用户当时看得到），失败后既没落库、前端也拿不到。

本探针只做一件事：把 /chat/stream 的**原始帧**逐条打出来（类型 + error 文案 + token 累计长度），
据此判断内容究竟在哪一层丢的：
  · 有 token 帧但无 error 帧  ⇒ 后端吞了异常（前端永远转圈）
  · 有 error 帧但 token 累计=0 ⇒ 内容没流出去（是"没生成"而非"丢了"）
  · 有 token 帧 + error 帧 + 库里 0 条 ⇒ **断点保护没兜住**（本次要害）

用法：python tools/verify/probe_partial_persist.py "写入工程" [conv_id]
"""
import json
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
    msg = sys.argv[1] if len(sys.argv) > 1 else "写入工程"
    cid = int(sys.argv[2]) if len(sys.argv) > 2 else 9629

    c = db()
    before_msgs = c.execute("SELECT COALESCE(MAX(id),0) m FROM messages WHERE conversation_id=?",
                            (cid,)).fetchone()["m"]
    before_use = c.execute("SELECT COALESCE(MAX(id),0) u FROM llm_usage_stats WHERE conversation_id=?",
                           (cid,)).fetchone()["u"]
    c.close()
    print("[基线] 会话 %d 当前最大 msg_id=%d / usage_id=%d" % (cid, before_msgs, before_use))
    print("[发送] %r" % msg)

    # ⚠️ 必须用**会话属主身份**登录：_own_conv 对非属主返回 404（不可见即 404，
    #   防枚举）。用新用户跑会得到 "HTTPError 404"，被误读成"接口不存在"。
    import sqlite3 as _s3
    _c = _s3.connect("file:mbse.db?mode=ro", uri=True)
    _c.row_factory = _s3.Row
    _uid = _c.execute("SELECT user_id FROM conversations WHERE id=?", (cid,)).fetchone()["user_id"]
    _dn = _c.execute("SELECT display_name FROM users WHERE id=?", (_uid,)).fetchone()["display_name"]
    _c.close()
    print("[身份] 会话属主 display_name=%r" % _dn)
    tok = call("/api/auth/local-login", {"display_name": _dn})["token"]
    req = urllib.request.Request(
        "%s/api/conversations/%d/chat/stream" % (BASE, cid),
        data=json.dumps({"message": msg, "branch": "dev"}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Session-Token": tok},
        method="POST")

    counts, tok_len, err_msg, done = {}, 0, "", False
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            ev_type = ""
            for raw in r:
                line = raw.decode("utf-8", "ignore").rstrip("\r\n")
                if line.startswith("event:"):
                    ev_type = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                counts[ev_type] = counts.get(ev_type, 0) + 1
                payload = line[5:].strip()
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                if ev_type == "token":
                    tok_len += len(ev.get("delta") or "")
                elif ev_type == "error":
                    err_msg = str(ev.get("message") or "")
                    print("[ERROR 帧] %s" % err_msg[:400])
                elif ev_type == "done":
                    done = True
                    print("[DONE 帧] 内容长度=%d" % len(str((ev.get("data") or {}).get("content") or "")))
    except Exception as e:
        print("[传输层异常] %r" % (e,))
    dur = time.time() - t0

    print("\n[帧统计] %s" % counts)
    print("[token 累计] %d 字符，耗时 %.1fs" % (tok_len, dur))

    time.sleep(1.5)
    c = db()
    new_msgs = list(c.execute(
        "SELECT id, role, length(content) L, substr(content,-60) tail FROM messages "
        "WHERE conversation_id=? AND id>? ORDER BY id", (cid, before_msgs)))
    usage = list(c.execute(
        "SELECT id, prompt_tokens, completion_tokens FROM llm_usage_stats "
        "WHERE conversation_id=? AND id>? ORDER BY id", (cid, before_use)))
    c.close()

    print("\n[落库] 新增消息 %d 条" % len(new_msgs))
    for m in new_msgs:
        tail = (m["tail"] or "").replace("\n", " ")
        print("   id=%s %-9s %6d 字 | 尾: %s" % (m["id"], m["role"], m["L"], tail))
    print("[LLM] 新增调用 %d 次：%s" % (len(usage),
          [(u["prompt_tokens"], u["completion_tokens"]) for u in usage]))

    # ── 判定 ──
    has_asst = any(m["role"] == "assistant" for m in new_msgs)
    print("\n=== 判定 ===")
    if tok_len > 0 and not has_asst:
        print("  ❌ 已流出 %d 字内容，但库里 0 条 assistant ⇒ 断点保护失效" % tok_len)
        return 1
    if tok_len == 0 and not has_asst:
        print("  ⚠️ 本次没流出任何内容（error=%s）⇒ 复现的是'一开始就失败'，不是'中途失败'" % (err_msg[:80] or "无"))
        return 2
    print("  ✅ 内容已落库（%d 条 assistant）" % sum(1 for m in new_msgs if m["role"] == "assistant"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
