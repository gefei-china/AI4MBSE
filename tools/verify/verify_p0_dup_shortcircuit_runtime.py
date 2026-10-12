"""verify_p0_dup_shortcircuit_runtime — 在**真实 pipeline 路径**上复现并验证修复。

## 为什么 AST 判据不够
`verify_p0_tool_results_var.py` 只证明「变量名不再写错」，
**不能证明「原来会崩的那条路径现在能跑通」**。
⇒ 本模块**真的把 AgentLoop 跑起来**，用「同参数重复调用」把`_dup` 分支顶出来，
   看是否还抛 `NameError`。

## 为什么这条分支最容易命中
`_guard.probe_dup`（同工具+同参数重复短路）最容易触发：
模型陷入循环时最典型的表现就是同参数反复调同一工具 —— 每次都真执行一遍。

## 做法（不依赖网络）
用**假 LLM provider**（monkeypatch `llm.llm_client.chat`），
让第2 轮**重复调用与第 1 轮完全相同**的工具与参数 ⇒ 必然走 `probe_dup` 分支。
不调真实模型 ⇒ 无额度/网络依赖、可反复跑。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p0_dup_shortcircuit_runtime.py
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


#: 实测**在 `_build_tools_def` 返回清单内**的工具（首版误用 sysml_stdlib_meta，
#:   它按需注入时不在清单里⇒ 分支走不到 ⇒ 判据空转）。
PROBE_TOOL = "sysml_v2_validate"


def main() -> int:
    import llm
    from agent.pipeline import AgentPipeline
    from database import get_db

    calls: list = []
    seen_tools: list = []          # 记录每次调用**实际收到**的工具清单

    def fake_chat(messages, provider_id=None, stream=False, tools=None,
                  thinking=False, **kw):
        """第 1 轮：给一个 tool_call。第 2 轮：**同参数重复** ⇒ 必然触发 probe_dup。
        第 3 轮：返回最终文本。"""
        calls.append(1)
        n = len(calls)
        seen_tools.append([t.get("function", {}).get("name")
                           for t in (tools or []) if isinstance(t, dict)])
        msg = {"role": "assistant", "content": ""}
        if n <= 2:
            # ★ 必须挑一个**确实会被注入**的工具。
            #   首版用 `sysml_stdlib_meta`，实测 `_build_tools_def` 返回的工具清单里
            #   **没有它**（按需注入：工具还要过相关性，见 `_build_tools_def`）
            #   ⇒ 分支根本没走到，而我的判据写成 `dup_hit <= 1`，
            #   "0 ≤ 1" 也成立 ⇒ **空转断言**。
            #   现在改用实测在清单内的 `sysml_v2_validate`，并加下界判据防再次空转。
            msg["tool_calls"] = [{
                "id": f"call_{n}",
                "type": "function",
                "function": {"name": PROBE_TOOL,
                             "arguments": json.dumps({"code": "package P { }"})},
            }]
            msg["content"] = ""
        else:
            msg["content"] = "最终回答：已完成。"
        return {
            "choices": [{"message": msg, "finish_reason":
                         "tool_calls" if n <= 2 else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "_meta": {"provider": "fake", "model": "fake",
                      "used_mock": False, "latency_ms": 1},
        }

    orig_chat = llm.llm_client.chat
    llm.llm_client.chat = fake_chat
    # ⚠️ `common.py:20` 是 `from llm import llm_client`（导入**实例**），
    #   而 `execute.py` 用 `from .common import *` ⇒ 拿到的是**同一个实例**，
    #   改实例方法即可生效。这里再补一刀"按类打桩"，
    #   防止它其实是按类绑定或另有一份实例（首版就栽在这：判据全绿但工具调用数=0，
    #   典型的**空转断言**）。
    orig_cls_chat = llm.LLMClient.chat
    llm.LLMClient.chat = lambda self, *a, **kw: fake_chat(*a, **kw)

    conn = get_db()
    conn.row_factory = __import__("sqlite3").Row
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # ⚠️ 用**具名占位符 + 显式列名**：首版按位置写占位符，结果 10 个 VALUES
    #   对 12 列表（漏了 last_slots / pending_clarify）⇒ sqlite3 直接报
    #   "10 values for 11 columns"。位置写法在表结构变动时必错，具名不会。
    cur = conn.execute(
        "INSERT INTO conversations (title, intent, status, user_id, phase,"
        " created_at, updated_at, project_id, current_intent, last_slots,"
        " pending_clarify) VALUES (:title, :intent, :status, NULL, :phase,"
        " :ts, :ts, :pid, :cur_intent, :slots, :clarify)",
        {"title": "verify_p0_dup", "intent": "view_expansion",
         "status": "active", "phase": "requirement", "ts": ts, "pid": "",
         "cur_intent": "view_expansion", "slots": "{}", "clarify": ""})
    conn.commit()
    conv = cur.lastrowid

    err = ""
    try:
        pipe = AgentPipeline()
        pipe._load_db_agents()
        pipe.execute("请基于架构骨架生成 sequence 视图，"
                     "并调用 sysml_v2_validate 校验产出",
                     conversation_id=conv, branch="dev",
                     forced_intent="view_expansion", provider_id=104)
    except Exception as exc:                # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    finally:
        llm.llm_client.chat = orig_chat
        llm.LLMClient.chat = orig_cls_chat

    # ── 前置判据：打桩是否真的生效（否则后面全是空转） ──
    checks: list[tuple[str, bool]] = []
    checks.append((
        f"假 provider 被调用（{len(calls)} 次，期望 ≥3：两轮 tool_calls + 一轮收尾）",
        len(calls) >= 3))
    # ★ 再加一条：**工具清单是否真的传给了模型**。
    #   首版工具没被执行时，若只看"调用次数=3"会误以为一切正常；
    #   实测第一轮 `tools` 可能为 None（技能走 offload，工具未注入）。
    tools_seen = seen_tools[0] if seen_tools else []
    checks.append((
        f"第 1 轮确实拿到了工具清单（{tools_seen[:4]}{'…' if len(tools_seen) > 4 else ''}，"
        f"共 {len(tools_seen)} 个；空清单⇒ 后面必然执行 0 次）",
        bool(tools_seen)))

    # ── 核心判据 ──
    checks.append((
        "重复调用路径未抛 NameError",
        "_tool_results" not in err))
    checks.append((
        f"pipeline 整体无异常（err={err[:80] or '无'}）", not err))

    n_msgs = conn.execute(
        "SELECT count(*) FROM messages WHERE conversation_id=?",
        (conv,)).fetchone()[0]
    checks.append((f"结果已落库（messages={n_msgs} 行，期望 >0）", n_msgs > 0))

    # ★ 反空转：**必须确实发生 1 次真实执行**。
    #   首版写的是 `dup_hit <= 1`，而 dup_hit 实测=0 —— "0 ≤ 1" 也成立，
    #   于是「分支根本没走到」被当成通过。这正是"永远为真的断言"的典型。
    #   正确写法：**下界**判据（真的执行过 ≥1 次）+ **上界**判据（没执行第 2 次）。
    dup_hit = conn.execute(
        "SELECT count(*) FROM tool_call_logs WHERE conversation_id=? "
        "AND tool_name=?", (conv, PROBE_TOOL)).fetchone()[0]
    checks.append((
        f"工具确被执行过（{PROBE_TOOL} 实际执行 {dup_hit} 次，期望 =1）",
        dup_hit == 1))
    checks.append((
        f"第 2 次同参数调用被短路（执行 {dup_hit} 次 < 未短路时的 2 次）",
        dup_hit < 2))

    ok = True
    print("=" * 64)
    print("P0 修复运行时验证 · 重复调用短路路径")
    print("=" * 64)
    for name, passed in checks:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("\n判据结论：" + ("全部通过" if ok else "存在未通过项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())