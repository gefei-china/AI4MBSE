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
import re
import sys

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

#: 收尾时若存在「已offload 但模型没取回」的技能正文，附上它。
#:
#: ★ 为什么需要它（2026-10-09 实测，N3 视图展开 6/8 产出雷同）：
#:   模型把 3 轮工具预算全部花在"查标准库/graph 检索"上，
#:   第 3 轮拿到的是**「未找到包 StandardViewDefinitions」**这种失败信息；
#:   收尾轮**不给工具**⇒ 它手里没有任何视图规范正文
#:   ⇒ 只能凭记忆写，于是交出上一轮残留的代码（6 个视图产出 md5 完全相同）。
#:
#: ⚠️ 这**不是给某个视图打补丁**，而是补一个通用缺口：
#:   「正文已 offload 到库、但没进本轮 messages」⇒ 模型永远看不见它。
#:   收尾轮既然不能再调工具，就把那份正文**直接放进 messages**。
_FETCH_HINT = (
    "\n\n以下是本轮已就绪但你尚未取回的技能规范正文（**权威依据**，"
    "优先于你的记忆；若与你的印象冲突，以它为准）：\n"
)


def finalize_messages(messages: list, extra_context: str = "") -> list:
    """构造收尾请求的消息序列（**追加一条 user 提示**，不改原messages）。

    - 用 `messages + [...]` 而非原地 append：调用方的 messages 后续还要复用
      （写卡片 / 落messages 表），原地改会污染主流程。
    - 只追加 user 消息：**不追加 assistant 空消息**（部分 provider 对
      "user 紧跟 assistant.tool_calls 且中间无 tool_result"的序列更敏感）。
    - `extra_context`：调用方传入的补充材料（如未取回的技能正文）。
      为空时行为与改动前**逐字相同**。
    """
    out = list(messages)
    hint = FINALIZE_HINT
    if extra_context:
        hint += _FETCH_HINT + extra_context
    out.append({"role": "user", "content": hint})
    return out


#: 代码块判定（与 tools/verify/* 各脚本取块正则同口径）
_CODE_FENCE_RE = re.compile(r"```(?:sysml|sysmlv2)?[ \t]*\n", re.I)


def lacks_deliverable(llm_content: str, expect_code: bool = False) -> bool:
    """产出里**没有目标产物**（不是"没有文字"）。

    ★ 为什么需要它（2026-10-08 实测，N3 视图展开 25% 无产出）：
    `should_finalize` 原判据是 `not llm_content`（正文为空才补收尾）。
    实测模型在轮次耗尽前常**只写一句过程叙述**就结束：

        「我先抽取骨架结构并核实标准库中的顺序视图相关元类，再生成代码。」（31 字）
        「标准库确认 Parts::Part、Ports::Port 均存在。现在生成结构视图…」（76 字）

    ⇒ `llm_content` 非空 ⇒ **收尾被跳过** ⇒ 循环结束，一行代码都没产出。
    这正是"模型言不由衷"的机制：**一句"我现在去生成"就骗过了收尾判定**。

    ⚠️ `expect_code=True` 时才要求产出代码块；默认 `False` 保持原行为
    （纯问答场景不需要代码，用它判反而会误触发收尾）。

    代码块判定取 ``` 围栏（含 ```sysml / ```sysmlv2 / 裸``` ），
    与 `tools/verify/*` 各脚本的取块正则同口径。
    """
    txt = llm_content or ""
    if not expect_code:
        return not txt.strip()
    return not _CODE_FENCE_RE.search(txt)


def should_finalize(msg: dict, llm_content: str, rounds_used: int,
                    max_rounds: int, expect_code: bool = False) -> bool:
    """是否需要补一次「无工具收尾」。

    两个条件同时成立才补：
    - `rounds_used >= max_rounds`：轮次真的用尽了（而非中途 break）；
    - **产出里没有目标产物**：
      · `expect_code=False`（默认，纯问答）→ `llm_content` 为空才算缺口
        （**保持原行为**：已有正文时用户看到的是完整回复，不需要再追加一段）；
      · `expect_code=True`（产出代码的任务，如 N3 视图展开）→
        只要**没有代码块**就算缺口，**哪怕已有一句过程叙述**
        （2026-10-08 实测：模型常只写"现在生成…"就耗尽轮次，
          原判据因此跳过收尾 ⇒ 零代码产出）。

    ⚠️ **不要用"有 tool_calls"当条件**：轮次用尽的最后一轮 `msg` 里必然还有
    tool_calls（否则循环已在 `if not tool_calls: break` 处退出），
    用它当条件会让「模型给了正文 + 仍要工具」的情况被重复收尾。
    """
    if rounds_used < max_rounds:
        return False
    return lacks_deliverable(llm_content, expect_code=expect_code)


# ══════════════════════════════════════════════════════════════════
# 自测（直接 `python agent/loop_protocol.py` 运行）
#
# 重点验2026-10-08 的那条缺陷：**"有过程叙述但没有代码"也算缺口**。
# 这正是 N3 视图展开 25% 无产出的根因，收尾被一句
# 「现在生成结构视图…」骗过 ⇒ 轮次耗尽时零代码产出。
# ══════════════════════════════════════════════════════════════════
def _selftest():
    import os
    _r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _r not in sys.path:
        sys.path.insert(0, _r)
    from agent.loop_protocol import lacks_deliverable, should_finalize

    ok = 0
    total = 0

    def chk(desc, got, want):
        nonlocal ok, total
        total += 1
        good = got == want
        ok += good
        print(f"  [{'OK  ' if good else 'FAIL'}] {desc}")
        if not good:
            print(f"期望 {want}，实际 {got}")

    print("=" * 68)
    print("loop_protocol 收尾判据自测")
    print("=" * 68)

    NARRATIVE = "标准库确认 Parts::Port 均存在。现在生成结构视图并校验。"
    CODE = "生成完毕：\n```sysml\npackage P { part def V; }\n```"

    print("\n① lacks_deliverable（产物缺失判定）")
    chk("纯问答：空产出 → 缺", lacks_deliverable("", expect_code=False), True)
    chk("纯问答：有叙述 → 不缺（保持原行为）",
        lacks_deliverable(NARRATIVE, expect_code=False), False)
    chk("产码：只有叙述、无代码 → **缺**（本轮修复的核心）",
        lacks_deliverable(NARRATIVE, expect_code=True), True)
    chk("产码：有代码块 → 不缺",
        lacks_deliverable(CODE, expect_code=True), False)
    chk("产码：裸 ``` 围栏也算",
        lacks_deliverable("```\npackage P {}\n```", expect_code=True), False)
    chk("产码：```sysmlv2 也算",
        lacks_deliverable("```sysmlv2\npackage P {}\n```", expect_code=True), False)

    print("\n② should_finalize（轮次用尽才收尾）")
    msg_with_tools = {"tool_calls": [{"id": "1"}]}
    chk("产码 + 轮次用尽 + 只有叙述 → **补收尾**",
        should_finalize(msg_with_tools, NARRATIVE, 3, 3, expect_code=True), True)
    chk("产码 + 已有代码 → 不补（避免重复）",
        should_finalize(msg_with_tools, CODE, 3, 3, expect_code=True), False)
    chk("产码 + 轮次未尽 → 不补",
        should_finalize(msg_with_tools, NARRATIVE, 1, 3, expect_code=True), False)
    chk("纯问答 + 有正文 → 不补（原行为不变）",
        should_finalize(msg_with_tools, NARRATIVE, 3, 3, expect_code=False), False)
    chk("纯问答 + 空正文 → 补（原行为不变）",
        should_finalize(msg_with_tools, "", 3, 3, expect_code=False), True)

    print("\n③ finalize_messages 的 extra_context（2026-10-09 通用补缺）")
    base = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    m0 = finalize_messages(base)
    m1 = finalize_messages(base, extra_context="ACTUAL_BODY_TEXT")
    chk("空 extra_context ⇒ 提示与改动前逐字相同",
        m0[-1]["content"] == FINALIZE_HINT, True)
    chk("有 extra_context ⇒ 正文进入最后一条 user 消息",
        "ACTUAL_BODY_TEXT" in m1[-1]["content"], True)
    chk("有 extra_context ⇒ 仍说明不要再请求工具",
        "不要再请求任何工具" in m1[-1]["content"], True)
    chk("原messages 不被污染（不原地 append）", len(base) == 2, True)

    print("\n" + "=" * 68)
    print(f"  自测通过 {ok}/{total}")
    print("=" * 68)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(_selftest())
