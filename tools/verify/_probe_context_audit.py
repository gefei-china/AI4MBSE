# -*- coding: utf-8 -*-
"""_probe_context_audit.py —— 取证：真实会话的上下文组装现状体检。

回答四个问题（全部**实测**，不推断）：
  Q1 每轮实际下发给 LLM 的 messages 是什么结构、各区占多少 token？
  Q2 system 区里到底有哪些块、各占多少？（找"大头"）
  Q3 五档压缩在真实数据上能压到多少？触发面有多大？
  Q4 现有机制里**标杆有、本仓没有**的是什么？

做法：直接读生产库（只读）取真实长会话 + 复用真实 prompt 组装函数，
     不调 LLM、不改数据。
     ⚠️ 生产库只读打开（file:...?mode=ro），零写入。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/_probe_context_audit.py [conv_id]
"""
import os
import re
import sqlite3
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.config import DB_PATH  # noqa: E402
from core.token_counter import count_tokens  # noqa: E402


def ro_conn():
    """**只读**连接生产库（mode=ro，任何写操作直接抛）。"""
    uri = "file:%s?mode=ro" % DB_PATH.replace("\\", "/")
    return sqlite3.connect(uri, uri=True)


def pick_longest_conv(conn):
    """挑消息最多的会话（上下文压力最大的真实样本）。"""
    rows = conn.execute(
        "SELECT conversation_id, COUNT(*) n, "
        "       SUM(LENGTH(content)) chars "
        "FROM messages WHERE role IN ('user','assistant') "
        "GROUP BY conversation_id ORDER BY n DESC LIMIT 5").fetchall()
    return rows


def head_stats(conn, cid):
    m = conn.execute(
        "SELECT role, COUNT(*), SUM(LENGTH(content)) FROM messages "
        "WHERE conversation_id=? AND role IN ('user','assistant') GROUP BY role",
        (cid,)).fetchall()
    return {r[0]: (r[1], r[2] or 0) for r in m}


def audit_system_blocks(system_prompt):
    """把 system 区按【块标题】切开，统计各块 token 占用。"""
    parts = re.split(r"(【[^】]{1,40}】)", system_prompt)
    out = []
    i = 0
    while i < len(parts) - 1:
        title = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        out.append((title, count_tokens(title + body)))
        i += 2
    if parts and parts[-1].strip():
        out.append(("<尾部无标题段>", count_tokens(parts[-1])))
    return out


def main():
    argv = sys.argv[1:]
    conn = ro_conn()
    print("DB(只读) = %s" % DB_PATH)

    cands = pick_longest_conv(conn)
    print("\n=== 消息量 Top5 会话（真实上下文压力样本）===")
    for cid, n, chars in cands:
        st = head_stats(conn, cid)
        u, a = st.get("user", (0, 0)), st.get("assistant", (0, 0))
        print("  conv=%-8s 消息=%-4d 总字符=%-7d user=%d/%d字符 assistant=%d/%d字符"
              % (cid, n, chars, u[0], u[1], a[0], a[1]))

    cid = int(argv[0]) if argv else (cands[0][0] if cands else 0)
    if not cid:
        print("库里没有可用会话")
        return
    print("\n选定 conv=%d 做深度体检" % cid)

    # ── Q1/Q2：真实组装一次 messages，看结构与 token 分布 ──────────────
    from agent import get_pipeline
    pipe = get_pipeline()

    captured = {}

    def fake_chat(messages, provider_id=None, stream=False, tools=None, **kw):
        captured["intent"] = kw.get("_intent") or ""
        captured["messages"] = messages
        if stream:
            return iter(['data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', "data: [DONE]\n\n"])
        return {"choices": [{"message": {"content": "ok", "tool_calls": None}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    from llm import llm_client
    llm_client.chat = fake_chat
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND role IN "
            "('user','assistant') ORDER BY id", (cid,)).fetchall()
        last_user = ""
        for r, c in rows:
            if r == "user":
                last_user = c
        print("  用该会话最后一条用户输入（%d 字）重放一轮：%s"
              % (len(last_user), last_user[:60].replace("\n", " ")))
        for _ in pipe.execute_stream(last_user or "继续", cid, "dev", user={"id": 1}):
            pass
    except Exception as e:
        print("  重放失败：%s: %s" % (type(e).__name__, e))
    finally:
        llm_client.chat = getattr(llm_client, "chat", fake_chat)

    msgs = captured.get("messages") or []
    if not msgs:
        print("  未捕获到 LLM 调用")
        return

    print("\n=== Q1 实际下发的 messages 结构 ===")
    total = 0
    for i, m in enumerate(msgs):
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
        t = count_tokens(str(c or ""))
        total += t
        print("  [%d] %-9s tok=%-6d %s" % (i, m.get("role"), t,
                                            str(c or "")[:70].replace("\n", " ")))
    print("  合计 tok=%d（总闸上限默认 10000）" % total)

    print("\n=== Q2 system 区按块拆解（找大头）===")
    sqm = str(msgs[0].get("content") or "")
    blocks = audit_system_blocks(sqm)
    stot = count_tokens(sqm)
    for title, t in sorted(blocks, key=lambda x: -x[1])[:14]:
        bar = "#" * max(1, int(t / max(1, stot) * 40))
        print("  %-28s tok=%-6d %5.1f%% %s" % (title, t, t / max(1, stot) * 100, bar))
    print("  system 合计 tok=%d，块数=%d" % (stot, len(blocks)))

    # ── Q3：五档压缩能把多少压下来 ────────────────────────────────────
    print("\n=== Q3 五档压缩实测（同一 system 区，用 pipeline 真实原语）===")
    from agent.pipeline_parts import compression as cp
    _bud = pipe._apply_context_budget
    _trunc = pipe._truncate_tokens
    prev = sqm
    for tier in range(1, 6):
        try:
            cur = cp.apply_tier(prev, "", tier, budget_fn=_bud, truncate_fn=_trunc)
        except Exception as e:
            print("  档%d 失败：%s: %s" % (tier, type(e).__name__, e))
            break
        t = count_tokens(cur)
        print("  档%d: tok=%-6d （较上一档 %+d）" % (tier, t, t - count_tokens(prev)))
        prev = cur

    # ── Q4：本仓有 vs 标杆有 ──────────────────────────────────────────
    print("\n=== Q4 机制覆盖对照（★=本仓缺）===")
    src = ""
    for p in ("agent/pipeline_parts/history.py", "agent/pipeline_parts/compression.py",
              "agent/tool_offload.py", "agent/pipeline_parts/stream.py",
              "agent/session_artifacts.py", "agent/followup.py"):
        fp = os.path.join(ROOT, p.replace("/", os.sep))
        if os.path.exists(fp):
            src += open(fp, encoding="utf-8").read()
    checks = [
        ("系统指令在最前、工具/历史在后（Anthropic 排序）", "messages = [messages[0]] + hist"),
        ("结构化笔记/todo 复述（Manus/Claude Code）", "todo.md"),
        ("按主题分区裁剪", "topic_group_match_dense"),
        ("语义拉回（just-in-time retrieval）", "topic_retrieve_topk"),
        ("超限自动压缩（compaction）", "def apply_tier"),
        ("压缩事件透传 UI", '"type": "compact"'),
        ("大工具结果 offload 到外部存储", "offload"),
        ("召回记忆（全量历史可检索）", "recall_memory"),
        ("★ KV-cache 前缀稳定性（禁时间戳进 system）", "cache_breakpoint"),
        ("★ 上下文可观测：每轮 token 构成落库", "context_audit"),
        ("★ 压缩保底：摘要本身再压缩/降级", "digest_of_digest"),
        ("★ 结构化摘要 schema（决策/待办/失败尝试）", "failed_attempts"),
    ]
    for name, needle in checks:
        hit = needle in src
        mark = "OK  " if hit else "★缺"
        print("  [%s] %s" % (mark, name))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()