# -*- coding: utf-8 -*-
"""验证：user 消息在流开始即落库（2026-09-29 修复「切页回来空态覆盖流式现场」的后端半边）。

修复前：user + assistant 都在流收尾 INSERT → 流式进行中 GET /messages 返回空列表
        → 前端 selectConv 渲染空态覆盖 #stream-ai。
修复后：execute_stream 入口即 INSERT user（非 dry_run）→ 流式进行中库中已有 user 消息；
        流结束后 user 恰好 1 条（收尾块不再重复插入）+ assistant 落库。

用法（需服务已跑）：
  .venv/Scripts/python.exe -X utf8 tests/manual_verify/verify_stream_user_persist.py
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
PROMPT = ("请详细解释 MBSE 中需求分析阶段的主要工作内容，分点论述，"
          "每点展开说明，整体不少于 600 字。")


def http_json(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def stream_events(conv_id, message):
    """逐行读 SSE，yield (event_type, data_dict)；读到 done/error 结束。"""
    req = urllib.request.Request(
        f"{BASE}/api/conversations/{conv_id}/chat/stream",
        data=json.dumps({"message": message, "attachments": [], "branch": "dev"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        ev_type, data_buf = None, ""
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                ev_type = line[6:].strip()
            elif line.startswith("data:"):
                data_buf += line[5:].strip()
            elif line == "" and ev_type:
                try:
                    yield ev_type, json.loads(data_buf)
                except Exception:
                    yield ev_type, {}
                if ev_type in ("done", "error"):
                    return
                ev_type, data_buf = None, ""


def main():
    results = []

    def chk(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("PASS  " if ok else "FAIL  ") + name + (f"  | {detail}" if detail else ""))

    # 1) 建会话
    conv = http_json("POST", "/api/conversations", {"title": f"VB-流式落库验证-{int(time.time())}"})
    cid = conv["id"]
    print(f"会话 id = {cid}")

    # 2) 发起流式，收 token 期间查库（修复点：此时 user 消息必须已在库）
    first_token_at = None
    mid_roles = None
    token_count = 0
    t0 = time.time()
    for ev_type, data in stream_events(cid, PROMPT):
        if ev_type == "token":
            token_count += 1
            if first_token_at is None:
                first_token_at = time.time() - t0
                # 首个 token 到达 = 流已真正开跑、收尾 INSERT 必然未执行 → 此刻查库
                time.sleep(0.5)   # 略等，确保入口事务已提交
                msgs = http_json("GET", f"/api/conversations/{cid}/messages")
                _m = msgs.get("messages") if isinstance(msgs, dict) else msgs
                mid_roles = [m["role"] for m in (_m or [])]
                print(f"  首个 token 后 {time.time() - t0:.1f}s 查库：roles = {mid_roles}")
        elif ev_type == "error":
            chk("流式执行无 error", False, str(data)[:200])
    print(f"  流结束：共 {token_count} 个 token 事件，耗时 {time.time() - t0:.1f}s")

    chk("流式产出正常（token 事件 >= 5）", token_count >= 5, f"tokens={token_count}")
    chk("首个 token 时库中已有 user 消息（修复点）",
        mid_roles is not None and "user" in mid_roles, f"中途 roles={mid_roles}")
    chk("首个 token 时库中不应已有 assistant 最终消息（尚未收尾）",
        mid_roles is not None and "assistant" not in mid_roles, f"中途 roles={mid_roles}")

    # 3) 流结束后：user 恰好 1 条（无重复）+ assistant 已落库
    msgs = http_json("GET", f"/api/conversations/{cid}/messages")
    _m = msgs.get("messages") if isinstance(msgs, dict) else msgs
    roles = [m["role"] for m in (_m or [])]
    n_user = roles.count("user")
    n_assistant = roles.count("assistant")
    chk("结束后 user 消息恰好 1 条（收尾不重复插）", n_user == 1, f"roles={roles}")
    chk("结束后 assistant 消息已落库", n_assistant >= 1, f"assistant={n_assistant}")

    # 4) 清理验证会话
    req = urllib.request.Request(f"{BASE}/api/conversations/{cid}", method="DELETE")
    urllib.request.urlopen(req, timeout=10).read()
    print(f"已清理验证会话 {cid}")

    fails = [r for r in results if not r[1]]
    print(f"\n== 汇总：{len(results) - len(fails)}/{len(results)} PASS ==")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
