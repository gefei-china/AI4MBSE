# -*- coding: utf-8 -*-
"""Agent Loop 协议护栏（2026-10-07）—— 并行 tool_calls 配对 + 截断参数拒绝。

对标文档：`docs/AgentLoop优化方案-对标行业标杆-20261007.md` §1（并行配对）/ §2（截断拒绝）。

## 为什么要有这个模块（而不是在两处 loop 里各写一段）

这两个缺陷都**同时存在于 `stream.py`（SSE，生产主路径）与 `execute.py`（非流式，冷路径）**。
2026-10-04的 `loop_guard.py` 已立过规矩：「两条路径是同一套 ReAct 语义的两种实现，
护栏必须同源 —— 否则今天补了 A 明天忘了 B，又会出现新的不对称」。
本次两个缺陷都是**协议层硬约束**（配对缺失 = HTTP 400），漏一条路径就是线上报错。

---

## 缺陷①：并行 tool_calls 的 `tool_use` / `tool_result` 配对缺口

### 现象
```python
for _ti, tc in enumerate(ptool_calls[:3]):     # ★ 只执行前 3 笔
    results.append({"tool_call_id": tc["id"], ...})
messages.append(pmsg)      # ★ pmsg 携带【全部】ptool_calls
messages.extend(results)   # ★ 只有 3 笔结果
```
⇒ 第 4 笔起 **永久悬空** ⇒ 下一轮请求 **HTTP 400**
（`tool_use ids were found without tool_result blocks immediately after`）。

### 为什么必须补而不是删切片
`[:3]` 是**失控护栏**（限制单轮工具数）。直接删掉会放开上限——
与 `loop_guard` 的三道闸冲突。⇒ 正确做法是**限执行 + 补尾部结果**。

### 真实触发频率（2026-10-07 实测）
`tools` 表 active = **33 个** ⇒ 一次给模型 33 个工具时并行 4~6 笔是常态；
且 `tool_call_logs` 实测**69 个会话里有 67 次出现「1 秒内连续多笔调用」**。

---

## 缺陷②：截断（max_tokens）时静默丢参

原实现 `except: targs = {}` 后**照常以空参数执行工具** ⇒
① 工具误用默认行为（比报错更危险）② 模型收不到"参数被截断"信号，不会重发
③ 空参数看起来像"用户没传"，掩盖了"模型输出不完整"这个真因。

⚠️ **但不能只看"解析失败"** —— 见 `is_truncated_args` 的注释。
"""

import json

__all__ = ["MAX_TOOLS_PER_ROUND", "split_calls", "skipped_results",
           "is_truncated_args", "truncated_result"]

#: 单轮最多**执行**几个工具。⚠️ 这是失控护栏，不是性能参数——
#: 未执行的尾部**必须**由 `skipped_results` 补齐结果（见模块 docstring 缺陷①）。
MAX_TOOLS_PER_ROUND = 3


def split_calls(tool_calls, limit: int = MAX_TOOLS_PER_ROUND):
    """切成 `(执行队列, 未执行队列)`。两队列都是原列表的切片，不改原对象。"""
    lst = list(tool_calls or [])
    n = max(0, int(limit))
    return lst[:n], lst[n:]


def skipped_results(tool_calls) -> list:
    """为**每个未执行**的 tool_call 生成一条"未执行"结果（协议配对用）。

    ⚠️ 三处不能省：
    1. `tool_call_id` **逐字回显** `tc["id"]`——协议靠它配对，重新生成会配不上；
    2. 提示语必须写明「**不要重试同一调用**」——否则模型很可能下一轮再发一次，
       变成真正的死循环（与`loop_guard.dedup` 的短路语义一致）；
    3. 标记 `skipped` / `skip_reason` 让上层可观测（前端要显示"未执行"而非"失败"）。
    """
    out = []
    for tc in (tool_calls or []):
        fn = (tc or {}).get("function") or {}
        name = str(fn.get("name") or "")
        out.append({
            "tool_call_id": (tc or {}).get("id", ""),
            "role": "tool",
            "name": name,
            "content": json.dumps({
                "ok": False,
                "result": (f"本轮工具并发数已达上限（{MAX_TOOLS_PER_ROUND} 个），"
                           f"「{name}」未被执行。"
                           f"**请勿重试同一个调用**——请基于已获得的信息回答，"
                           f"或下一轮改用其他工具。"),
                "skipped": True,
                "skip_reason": "round_tool_limit",
            }, ensure_ascii=False),
        })
    return out


def is_truncated_args(fn) -> bool:
    """判断这次 tool_call 的参数是否因 `max_tokens` 截断而残缺。

    ⚠️⚠️ **不能只看"JSON 解析失败"** —— 那样会把**合法但为空**的参数误判成截断，
    从而误杀正常请求（如某个确实无参的工具）。三种情况必须分开：

    | `arguments` 形态 | 判定 | 理由 |
    |---|---|---|
    | 空串 / 空白 | **截断** | 输出被切断，arguments 还没写出来 |
    | **非法 JSON**（`{"a":`） | **截断** | JSON 没闭合，语义字段可能已缺失 |
    | **合法 JSON**（含 `{}`） | **不是截断** | 模型确实发了（可能真无参）⇒ 交给工具自己的参数校验 |

    为什么"合法但为空"不是截断：模型完全可能对一个无参工具发 `{}`。
    把它当截断拒绝 ⇒ **正常请求被误杀**，比原缺陷更隐蔽。
    """
    raw = (fn or {}).get("arguments")
    if raw is None:
        return True                      # 没有 arguments 字段 = 残缺
    if not isinstance(raw, str):
        # 某些 provider 直接给了 dict —— 已是结构化对象，**不是**截断
        return False
    if not raw.strip():
        return True                      # 空串 = 截断
    try:
        json.loads(raw)
        return False                     # ★ 合法 JSON（含 {}）⇒ 不算截断
    except Exception:
        return True                      # 非法 JSON ⇒ 截断


def truncated_result(tool_name: str) -> dict:
    """参数被截断时的结果体（**拒绝执行 + 明确告知重发**）。

    对标Pi 的 `failToolCallsFromTruncatedMessage`：**假装执行，实际拒绝**。
    与 Pi 的差异：Pi 对所有 toolCall 无条件补错误结果；本仓先经`is_truncated_args`
    判别（因为本仓还存在"合法空参数"的正常请求，无条件拒绝会误杀）。
    """
    return {
        "ok": False,
        "error": "args_truncated",
        "result": (f"工具「{tool_name}」的调用参数因模型输出超出长度上限而被**截断**，"
                   f"已拒绝执行（避免用不完整参数产生错误副作用）。"
                   f"请**缩短描述后重新发起一次完整调用**。"),
    }


# ══════════════════════════════════════════════════════════════════════════
# P1-①：达到轮次上限时的「无工具收尾」（2026-10-07）
# ══════════════════════════════════════════════════════════════════════════
#
# 行业依据（OpenAI SDK 官方）：
#   「When you hit the cap, do not silently error out — append a message asking for a
#   best-effort summary and **make one final call without tools**.」
#
# ★ 为什么收尾调用**必须不带 tools**：
#   带 tools ⇒ 模型看到"还能调工具"⇒ 很可能再要一轮工具⇒ 又触发上限⇒ 死循环。
#   不带 tools ⇒ 模型在协议层**物理上无法**请求工具 ⇒ 必然给出文字结论。
#
# ★ 为什么本仓 stream.py 的兜底**早就在做**这件事（但形态不对）：
#   `stream.py:1835` `if tool_injected and not llm_content:` → `chat(messages, stream=True)`
#   —— 它**不传 tools**，所以已经是「无工具收尾」了（这一点与方案 §3.1 的假设不同）。
#   真正的缺口有两个，都不是「有没有收尾」：
#     ① **execute.py 冷路径完全没有收尾**（实测轮次用尽 → content 长度 0 ⇒ 返回空）；
#     ② stream.py 收尾时**没告诉模型"工具次数已用尽"** ⇒ 模型不知道要总结，
#        可能以为对话被截断而给出残缺结论，或重复已说过的内容。

FINALIZE_HINT = (
    "本轮工具调用次数已达上限。请**基于已经获得的信息**给出完整、"
    "自洽的最终结论（说明你做了什么、结论是什么、有哪些未覆盖的部分），"
    "不要再请求任何工具。"
)


def finalize_messages(messages: list) -> list:
    """构造收尾请求的消息序列（**追加一条 user 提示**，不改原 messages）。

    - 用 `messages + [...]` 而非原地 append：调用方的 messages 后续还要复用
      （写卡片 / 落messages 表），原地改会污染主流程。
    - 只追加 user 消息：**不追加 assistant 空消息**（部分 provider 对
      "user 紧跟 assistant.tool_calls 且中间无 tool_result"的序列更敏感）。
    """
    out = list(messages)
    out.append({"role": "user", "content": FINALIZE_HINT})
    return out


def should_finalize(msg: dict, llm_content: str, rounds_used: int,
                    max_rounds: int) -> bool:
    """是否需要补一次「无工具收尾」。

    两个条件同时成立才补：
    - `rounds_used >= max_rounds`：轮次真的用尽了（而非中途 break）；
    - `not llm_content`：还没有任何正文产出（**空正文才是真缺口**——
      已有正文时用户看到的是完整回复，不需要再追加一段）。

    ⚠️ **不要用"有 tool_calls"当条件**：轮次用尽的最后一轮 `msg` 里必然还有
    tool_calls（否则循环已在 `if not tool_calls: break` 处退出），
    用它当条件会让「模型给了正文 + 仍要工具」的情况被重复收尾。
    """
    return rounds_used >= max_rounds and not (llm_content or "").strip()
