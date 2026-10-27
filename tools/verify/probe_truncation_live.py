# -*- coding: utf-8 -*-
"""probe_truncation_live — 真实链路验证「长用户输入进得去 + 工具日志截断可观测」。

两问只在**真实请求**上能答：
  ① 长用户输入（>2000 字）是否真的进了 prompt？—— 对比同一会话「短消息轮」与
     「长消息轮」的 `llm_usage_stats.prompt_tokens` 差值（≈ 长文 token 数）。
     修前模板段有 `str(user_input)[:2000]` 硬切；修后应无此上限。
  ② 工具结果落库是否可观测？—— 新增 `result_len` / `result_truncated` 两列必须有值。

判据（缺一即失败）：
  · 长轮 prompt_tokens − 短轮 prompt_tokens ≥ 3000（6000 字中文 ≈ 4000 token）
  · 该轮至少 1 条 tool_call_logs 且 result_len > 0、result_truncated ∈ {0,1}
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = "."
BASE = "http://127.0.0.1:8000"


def call(path, body=None, headers=None, method=None, timeout=300):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip().startswith(("{", "[")) else {}


def max_prompt_tokens_after(cid, after_id):
    """取该轮**主调用**的 prompt_tokens。

    ⚠️ 不能取 MAX(id)：一轮里有多个子调用（意图复核 / 技能反馈 / 收尾），
    最后一条往往是最小的那个（实测 1,072），会把"长输入没进去"误判成事实。
    主 ReAct 调用是 prompt_tokens **最大**的那条（实测 5,406 → 13,431）。
    """
    c = db()
    row = c.execute(
        "SELECT MAX(prompt_tokens) p FROM llm_usage_stats WHERE conversation_id=? AND id>?",
        (cid, after_id)).fetchone()
    c.close()
    return (row["p"] if row else 0) or 0


def last_usage_id(cid):
    c = db()
    row = c.execute("SELECT COALESCE(MAX(id),0) i FROM llm_usage_stats WHERE conversation_id=?",
                    (cid,)).fetchone()
    c.close()
    return row["i"] if row else 0


def stream_chat(tok, cid, msg, team=None):
    """驱动 /api/conversations/{id}/chat/stream，收集 SSE 事件直到 done。"""
    body = {"message": msg, "branch": "dev"}
    if team:
        body["team"] = team
    req = urllib.request.Request(
        "%s/api/conversations/%d/chat/stream" % (BASE, cid),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Session-Token": tok},
        method="POST")
    stages, done = [], False
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            if ev.get("type") in ("stage", "tool", "tool_call"):
                stages.append((ev.get("type"), str(ev.get("name") or ev.get("label") or "")[:30]))
            if ev.get("type") in ("done", "final", "end"):
                done = True
    return stages, done


def db():
    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main():
    tok = call("/api/auth/local-login", {"display_name": "truncation-probe"})["token"]
    conv = call("/api/conversations",
                {"title": "truncation probe %d" % int(time.time())},
                headers={"X-Session-Token": tok})
    cid = conv.get("id") or (conv.get("conversation") or {}).get("id")
    print("[会话] id=%s" % cid)

    # ── 第 1 轮：短消息（基准）──
    mark0 = last_usage_id(cid)
    stream_chat(tok, cid, "检索知识库中关于需求分析的内容，给出三条关键结论")
    p1 = max_prompt_tokens_after(cid, mark0)
    print("[短轮] 主调用 prompt_tokens = %d" % p1)

    # ── 第 2 轮：长消息（约 4000 字填充 + 同样请求）──
    filler = "以下是需求文档片段，仅作上下文背景，无需复述。\n" + "".join(
        "第%d条：系统应在整车动力管理场景下保证功能安全与可追溯性。\n" % i for i in range(1, 121))
    print("[长轮] 用户输入 %d 字" % len(filler))
    mark1 = last_usage_id(cid)
    stream_chat(tok, cid, filler + "\n\n检索知识库中关于需求分析的内容，给出三条关键结论")
    p2 = max_prompt_tokens_after(cid, mark1)
    print("[长轮] 主调用 prompt_tokens = %d   Δ = %d" % (p2, p2 - p1))

    c = db()
    rows = list(c.execute(
        "SELECT id, tool_name, length(result) rl, result_len, result_truncated "
        "FROM tool_call_logs WHERE conversation_id=? ORDER BY id", (cid,)))
    print("[工具调用] %d 条" % len(rows))
    for x in rows:
        print("   id=%s %-18s stored=%s result_len=%s truncated=%s"
              % (x["id"], x["tool_name"], x["rl"], x["result_len"], x["result_truncated"]))
    c.close()

    ok_len = (p2 - p1) >= 3000
    ok_log = any((x["result_len"] or 0) > 0 for x in rows) and \
             all((x["result_truncated"] in (0, 1)) for x in rows)

    print("\n=== 判定 ===")
    print("  长用户输入进入 prompt（Δ≥3000 token）：%s" % ("✅" if ok_len else "❌ Δ=%d" % (p2 - p1)))
    print("  工具日志 result_len 已写、truncated 合法：%s" % ("✅" if ok_log else "❌"))
    return 0 if (ok_len and ok_log) else 1


if __name__ == "__main__":
    sys.exit(main())
