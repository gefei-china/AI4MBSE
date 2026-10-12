# -*- coding: utf-8 -*-
"""_probe_retry_context.py —— 取证：用户发「重试」时，实际下发给 LLM 的 messages 是什么。

为什么必须进程内拦截取证（2026-10-11）：
  问的是「上下文里有没有之前的输入」，而 messages 由多层拼装而成 ——
  system_prompt + 会话历史注入（_load_history）+ 本轮 user_input + 工具轮 append。
  **读代码推不出来**（预算裁剪/话题分段/产物摘要都会改内容），只能抓真调用。

做法：monkeypatch `llm.llm_client.chat`（common.py 里 stream/execute 用的就是它），
      每次调用把 messages 深拷贝存进 _CAPTURE，然后返回一个**结构合法但内容为 Mock**
      的响应，让 pipeline 继续跑完 —— 于是我们既抓到了上下文，又不依赖真实网关
      （当前 109 provider 网关已死，走真调用只会卡死）。

⚠️ 隔离：用 _tmpdb.ensure_schema 建独立库，生产库零读写。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/_probe_retry_context.py
"""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "verify"))

from _tmpdb import ensure_schema  # noqa: E402

TMP_DB = os.path.join(ROOT, "tmp", "probe_retry_ctx.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
if not ensure_schema(TMP_DB):
    print("[probe] 建临时库失败")
    sys.exit(1)

os.environ["MBSE_FORCE_MOCK"] = "1"   # 意图识别/拆解等前置 LLM 一律走 Mock，不碰网关

import database.connection as _conn_mod  # noqa: E402
_conn_mod.DB_PATH = TMP_DB

from llm import llm_client  # noqa: E402

_CAPTURE = []          # 每次 chat() 调用的快照
_real_chat = llm_client.chat


def _fake_chat(messages, provider_id=None, stream=False, tools=None, **kw):
    """结构合法的 Mock 响应：probe 轮给 tool_calls 或空 content，流式给 SSE 文本块。"""
    intent = kw.get("_intent") or ""
    _CAPTURE.append({
        "intent": intent,
        "stream": stream,
        "has_tools": bool(tools),
        "messages": copy.deepcopy(messages),
    })
    if stream:
        chunk = ('data: ' + json.dumps(
            {"choices": [{"delta": {"content": "（Mock 回答）"}}]}, ensure_ascii=False) + "\n\n")
        return iter([chunk, "data: [DONE]\n\n"])
    # 非流式：probe 轮 → 不给 tool_calls（让 pipeline 直接进入生成，避免工具循环复杂化）
    return {"choices": [{"message": {"content": "（Mock 回答）", "tool_calls": None}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


llm_client.chat = _fake_chat


def _seed(cid, pairs):
    """往临时库塞历史消息（role/content）。"""
    import sqlite3
    c = sqlite3.connect(TMP_DB)
    for role, content in pairs:
        c.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
                  (cid, role, content))
    c.commit()
    c.close()


def _digest(msgs, limit=260):
    out = []
    for i, m in enumerate(msgs):
        role = m.get("role") or "?"
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
        c = str(c or "")
        out.append({"idx": i, "role": role, "len": len(c), "head": c[:limit]})
    return out


def main():
    from agent import get_pipeline
    from agent import followup as fup

    cid = 990001

    # ── 场景 A：普通首轮（无历史）────────────────────────────────────────
    print("=" * 78)
    print("场景 A：首轮提问「帮我梳理需求」")
    _seed(cid, [])
    pipe = get_pipeline()
    n0 = len(_CAPTURE)
    for _ in pipe.execute_stream("帮我梳理需求", cid, "dev", user={"id": 1}):
        pass
    a = _CAPTURE[n0:]
    print("  LLM 调用次数=%d" % len(a))
    for k, c in enumerate(a):
        print("  --- call#%d intent=%s stream=%s tools=%s msgs=%d"
              % (k, c["intent"], c["stream"], c["has_tools"], len(c["messages"])))
        for row in _digest(c["messages"]):
            print("      [%d] %-9s len=%-6d %s"
                  % (row["idx"], row["role"], row["len"], row["head"].replace("\n", " ⏎ ")[:200]))

    # ── 场景 B：上一轮有历史，再发「重试」────────────────────────────────
    print("\n" + "=" * 78)
    print("场景 B：已有 2 轮历史后，用户发「重试」")
    import sqlite3
    c = sqlite3.connect(TMP_DB)
    c.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
              (cid, "user", "帮我梳理需求"))
    c.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
              (cid, "assistant", "好的，已为你梳理出 3 条需求：R1 取电、R2 充电、R3 上报。"))
    c.commit()
    c.close()

    n1 = len(_CAPTURE)
    for _ in pipe.execute_stream("重试", cid, "dev", user={"id": 1}):
        pass
    b = _CAPTURE[n1:]
    print("  LLM 调用次数=%d" % len(b))
    for k, c in enumerate(b):
        print("  --- call#%d intent=%s stream=%s tools=%s msgs=%d"
              % (k, c["intent"], c["stream"], c["has_tools"], len(c["messages"])))
        for row in _digest(c["messages"]):
            print("      [%d] %-9s len=%-6d %s"
                  % (row["idx"], row["role"], row["len"], row["head"].replace("\n", " ⏎ ")[:200]))
        # 关键判据
        sysm = c["messages"][0]["content"] if c["messages"] else ""
        users = [str(m.get("content")) for m in c["messages"] if m.get("role") == "user"]
        print("      ▸ system 含【续作指令】: %s" % ("【续作指令】" in sysm))
        print("      ▸ user 消息条数=%d" % len(users))
        for u in users:
            print("        · %s" % u.replace("\n", " ⏎ ")[:160])
        print("      ▸ 含上一轮用户输入「帮我梳理需求」: %s"
              % any("帮我梳理需求" in u for u in users))
        print("      ▸ 含上一轮 assistant 产出「R1 取电」: %s"
              % any("R1 取电" in str(m.get("content")) for m in c["messages"]))

    # ── 场景 C：follow-up 队列（生成中发「重试」）────────────────────────
    print("\n" + "=" * 78)
    print("场景 C：生成过程中发「重试」⇒ 进 followup 队列 ⇒ 轮次边界被消费")
    fup.push(cid, "重试")
    print("  pending_count=%d" % fup.pending_count(cid))
    n2 = len(_CAPTURE)
    for _ in pipe.execute_stream("继续刚才的任务", cid, "dev", user={"id": 1}):
        pass
    cc = _CAPTURE[n2:]
    print("  LLM 调用次数=%d" % len(cc))
    for k, c in enumerate(cc):
        print("  --- call#%d intent=%s msgs=%d" % (k, c["intent"], len(c["messages"])))
        for row in _digest(c["messages"]):
            print("      [%d] %-9s len=%-6d %s"
                  % (row["idx"], row["role"], row["len"], row["head"].replace("\n", " ⏎ ")[:200]))
        hit = [str(m.get("content")) for m in c["messages"]
               if "补充了新指令" in str(m.get("content"))]
        print("      ▸ 含 follow-up 追加块: %s" % (bool(hit)))
        for h in hit:
            print("        · %s" % h.replace("\n", " ⏎ ")[:160])

    print("\n[probe] 临时库=%s（生产库未读写）" % TMP_DB)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()