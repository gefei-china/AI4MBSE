# -*- coding: utf-8 -*-
"""线上 SSE 探针（临时取证，非常驻门禁）：建临时会话 → 打 /chat/stream → 逐帧打印。

取证目标（2026-10-10 会话 9336 事故复盘后的复验）：
  ① 不再出现 `NameError: CHAT_TASK_PROBE is not defined`（P1-35 修复前每次必挂）
  ② 长等待期间出现 `event: ping`（P1-33 可见心跳）
  ③ 流终止时**恰好一条** `event: error`（P1-32 终态兜底，且不二次补发）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/_probe_sse_live.py [max_seconds]
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
MAX_S = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0

# ⚠️ 本机 HTTP_PROXY=127.0.0.1:55929 会把 **127.0.0.1 的本地请求也劫持**，
# 实测直连 8000 被代理挡回 502 Bad Gateway（与外网网关故障是两回事）。
# 本地探针必须显式禁代理，否则会把"代理挡了"误判成"服务挂了"。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    conv = post("/api/conversations", {"title": "SSE探针(临时)", "type": "chat"})
    cid = conv.get("id") or (conv.get("data") or {}).get("id")
    print("[probe] conv_id=%s" % cid)
    req = urllib.request.Request(
        BASE + "/api/conversations/%s/chat/stream" % cid,
        data=json.dumps({"message": "你好，做一次自我介绍"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    t0 = time.time()
    counts = {}
    frames = []
    try:
        with _OPENER.open(req, timeout=MAX_S) as r:
            print("[probe] HTTP %s  content-type=%s" % (r.status, r.headers.get("Content-Type")))
            buf = b""
            while True:
                chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    s = raw.decode("utf-8", "replace")
                    et = "message"
                    for line in s.splitlines():
                        if line.startswith("event:"):
                            et = line[6:].strip()
                    counts[et] = counts.get(et, 0) + 1
                    frames.append((round(time.time() - t0, 1), et, s[:160]))
                if time.time() - t0 > MAX_S:
                    print("[probe] 达到上限 %.0fs，主动断开" % MAX_S)
                    break
    except Exception as e:
        print("[probe] 连接异常: %s" % type(e).__name__, e)
    print("\n=== 帧统计 ===")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print("  %-12s x%d" % (k, v))
    print("\n=== 前 40 帧 ===")
    for t, et, s in frames[:40]:
        print("  [%6.1fs] %-10s %s" % (t, et, s.replace("\n", " | ")[:140]))
    print("\n=== 关键判据 ===")
    all_text = "\n".join(f[2] for f in frames)
    print("  ① 无 CHAT_TASK_PROBE NameError :", "CHAT_TASK_PROBE" not in all_text)
    print("  ② 出现可见 ping               :", counts.get("ping", 0) > 0, "(%d 帧)" % counts.get("ping", 0))
    print("  ③ error 恰好 1 条             :", counts.get("error", 0) == 1, "(%d 条)" % counts.get("error", 0))
    print("  ④ 有终态（done 或 error）      :", (counts.get("done", 0) + counts.get("error", 0)) >= 1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
