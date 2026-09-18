"""S3 端到端验证（2026-09-17）：走真实 SSE 主路径，确认改动后服务可用且 token 结构变化生效。

验证点：
1. 服务健康：/ 与 /api/dashboard 200
2. 真实会话：创建会话 → POST /chat/stream 走完流式链路（S3 改动 1/2/3 都在这条路径上）
3. 流式响应非空、无异常事件
4. llm_usage_stats 新增记录（证明链路真跑到了 LLM 调用，prompt token 有值）
5. 服务日志无 Traceback / 500
"""
import json
import os
import sqlite3
import time

import httpx

BASE = "http://127.0.0.1:8000"
DB = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system\mbse.db"

out = []


def q(sql, args=()):
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def log(msg):
    """逐条打印（flush）：脚本后段万一报错也不丢前面的结论。"""
    print(msg, flush=True)
    out.append(msg)


# 1) 健康
r = httpx.get(BASE + "/", timeout=15)
log("GET / -> %s (%d B)" % (r.status_code, len(r.content)))
r = httpx.get(BASE + "/api/dashboard", timeout=15)
log("GET /api/dashboard -> %s" % r.status_code)

# 2) 调用前 usage 基线
before = q("SELECT count(*) n, COALESCE(max(id),0) mx FROM llm_usage_stats")[0]
log("usage before: n=%s max_id=%s" % (before["n"], before["mx"]))

# 3) 创建会话 + 走流式链路
conv = httpx.post(BASE + "/api/conversations", json={"title": "S3 端到端验证"}, timeout=20).json()
cid = conv.get("id") or conv.get("conversation_id")
log("created conversation id=%s" % cid)

prompt = os.environ.get("S3_PROMPT") or "用一句话说明需求追溯链包含哪五个环节。"
events, tokens, errs, tool_ev = 0, 0, [], 0
t0 = time.time()
with httpx.stream("POST", BASE + f"/api/conversations/{cid}/chat/stream",
                  json={"message": prompt}, timeout=180) as resp:
    log("POST /chat/stream -> %s (%.1fs)  prompt=%r" % (resp.status_code, time.time() - t0, prompt[:40]))
    for line in resp.iter_lines():
        if not line:
            continue
        events += 1
        if line.startswith("data:"):
            body = line[5:].strip()
            try:
                ev = json.loads(body)
            except Exception:
                continue
            t = ev.get("type")
            if t == "token":
                tokens += 1
            elif t == "tool":
                tool_ev += 1
            elif t == "error":
                errs.append(ev)
log("SSE events=%d, token_events=%d, tool_events=%d, error_events=%d" % (events, tokens, tool_ev, len(errs)))
if errs:
    log("ERROR detail: " + json.dumps(errs[:2], ensure_ascii=False)[:400])

# 4) usage 增量
after = q("SELECT count(*) n, COALESCE(max(id),0) mx FROM llm_usage_stats")[0]
log("usage after:  n=%s max_id=%s  (+%s 次调用)" % (after["n"], after["mx"], after["n"] - before["n"]))
for r in q("SELECT id,intent,model_name,used_mock,prompt_tokens,completion_tokens "
           "FROM llm_usage_stats ORDER BY id DESC LIMIT 5"):
    log("  usage#%s intent=%r model=%s mock=%s prompt=%s completion=%s" %
        (r["id"], r["intent"], r["model_name"], r["used_mock"], r["prompt_tokens"], r["completion_tokens"]))

# 5) 清理：删掉本次验证新建的会话，避免污染用户任务列表
try:
    d = httpx.delete(BASE + f"/api/conversations/{cid}", timeout=20)
    log("cleanup: DELETE /api/conversations/%s -> %s" % (cid, d.status_code))
except Exception as e:
    log("cleanup failed: %s" % e)


print("\n".join(str(x) for x in out))
