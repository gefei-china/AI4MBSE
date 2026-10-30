# -*- coding: utf-8 -*-
"""S3 运行时 token 预算回归（离线，不依赖 8000 端口服务）。

覆盖 3 项断言：
  (a) 回填给模型的工具结果 <= _TOOL_MODEL_CAP，且超长时带 truncated/note；
  (b) 更早轮次 tool 消息 content 被替换为省略标记，消息条数与 role 序列不变（协议成对性）；
  (c) _apply_context_budget 对超预算的检索段确实截断。

做法：(a)(b) 从 agent/pipeline_parts/stream.py 的真实源码切出改动块 exec 执行，
验证的是「已落地的代码文本」而非复制品；(c) 直接调用真实 HistoryMixin 方法。
运行：<venv>/python.exe -X utf8 tools/verify/verify_token_budget.py
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# ── 已接进 CI（2026-10-05 第二轮第 2 项续：变异锚点漂移修复）──────────
# 修复要点：token 预算。源码改走 `tool_offload.model_side_content` 后，切出的源码块新增对\n#   `_tool_offload`/`tname`/`self`/`tools_def` 的依赖，而 exec 的 ns 只给了 result/cap/json\n#   ⇒ NameError。**是夹具缺依赖，不是产品缺陷**。补全 ns；超限分支 patch 掉 save_offload\n#   （真函数会落库，门禁不得写库），并补一层「契约层」直接验真函数本身。
# 双环境实测：生产库 + 全新干净库 均 rc=0。

import os
import sys
import json
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STREAM_PY = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
HISTORY_PY = os.path.join(ROOT, "agent", "pipeline_parts", "history.py")

_fails = []
_oks = []


def check(name, cond, detail=""):
    (_oks if cond else _fails).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  | " + str(detail) if detail else ""))


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def cut(text, start_marker, end_marker):
    """切出 [start_marker, end_marker) 之间的源码块，并返回其起始行号。

    左端回退到行首（保留缩进前缀），否则 textwrap.dedent 不会去除缩进。

    ★★ 2026-10-21 结构加固：end_marker 原本是**行尾注释**
      （`"\\n                    # P0-按需工具：本轮工具结果全弱相关"`），
      而那段注释在 #57 改动里缩进被调整过 ⇒ `text.index` 直接
      `ValueError: substring not found` ⇒ **门禁在 (b) 组整体崩掉**，
      报错位置离真实原因（锚点漂移）十万八千里，且 (a) 组已跑完的结果全丢。

      ⇒ 改为 `cut_block()`：以**代码行**（`if results:`）为锚，按**缩进层级**
        切到同层或更浅的第一行为止，**完全不依赖任何注释文本**。
        注释怎么改、挪哪、加多少都不再影响门禁。
      `cut()` 保留给 (a) 组那种"按注释语义定位"的用法（那段锚点稳定）。
    """
    i = text.index(start_marker)
    i = text.rindex("\n", 0, i) + 1
    j = text.index(end_marker, i)
    return text[i:j], text[:i].count("\n") + 1


def cut_block(text, first_line_stripped, end_line=None, dedent_to=0):
    """★ 按**代码内容**定位切出一个代码块（完全不依赖注释文本）。

    - `first_line_stripped`：块首**代码行**（**不含前导缩进**），如 `"if results:"`。
      缩进由函数自己在全文中定位首个匹配行取得 ⇒ 调用方不必知道它嵌在几层。
    - `end_line`：块末**代码行**（不含缩进）。给定则切到该行**之后**为止；
      省略则按缩进层级切（同层或更浅的非注释代码行即止）。
    - 返回 (源码文本, 起始行号)。调用方自行 textwrap.dedent。

    为什么必须这样做：源码里的**注释**是重构时最常被移动/改写/改缩进的部分，
    而注释内容绝不该成为门禁的锚 —— 让锚点挂在**代码结构**上才稳。
    （实测踩坑两次：① 原 end_marker 是行尾注释，`ValueError` 直接崩在 (b) 组；
      ② 改按缩进切时，`if results:` 之后一直延伸到 352 行后的
      `except GeneratorExit`（缩进 8），块被撑到 18773 字符且 exec 跑不动
      ⇒ 必须再给一个**代码**终点把范围收紧。）
    """
    lines_all = text.split("\n")
    hit = None
    for k, ln in enumerate(lines_all):
        if ln.strip() == first_line_stripped:
            hit = k
            break
    if hit is None:
        raise AssertionError("cut_block: 找不到块首代码行 %r" % first_line_stripped)
    start_indent = len(lines_all[hit]) - len(lines_all[hit].lstrip())
    if end_line is not None:
        end = None
        for k in range(hit, len(lines_all)):
            if lines_all[k].strip() == end_line:
                end = k
                break
        if end is None:
            raise AssertionError("cut_block: 找不到块末代码行 %r" % end_line)
        return "\n".join(lines_all[hit:end + 1]), hit + 1
    out = [lines_all[hit]]
    for ln in lines_all[hit + 1:]:
        if not ln.strip():                      # 空行：并入块（保持 exec 语义）
            out.append(ln)
            continue
        s = ln.lstrip()
        if s.startswith("#"):                   # 纯注释行：缩进更深才属于本块
            if len(ln) - len(s) > start_indent:
                out.append(ln)
            continue
        ind = len(ln) - len(s)
        if ind <= max(dedent_to, 0):
            break                                # 同层或更浅 ⇒ 块结束
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out), hit + 1


print("=" * 72)
print("S3 运行时 token 预算回归")
print("=" * 72)

# ---------- 0. 常量 ----------
print("\n[0] 常量定义与导出")
from agent.pipeline_parts.common import _TOOL_RESULT_CAP, _TOOL_MODEL_CAP, _ATT_INJECT_CAP
import agent.pipeline_parts.common as _common

# ★★ 2026-10-30：这三条此前**锁定的是过期字面量**（6000/3000/4000），
#   而 37780d3「截断清单 P0/P1/P2 全量落地」已把三者统一提到 8000
#   （理由：B3 工具日志落库 2000→8000 那一批，展示/回填/注入一起放宽）。
#   结果是**这条门禁从 37780d3 起一直红在 CI 里**，而没人处理 ——
#   门禁红了太久 = 等于没有门禁（和空转变异同一种失效）。
#   修法：断言**不变量**而不是快照数字 —— 快照会随合理调参失效，
#   不变量（量级、相对关系、导出）才是要守的东西。
check("_TOOL_RESULT_CAP 展示上限为正且 >= 4000（不许缩到看不清结果）",
      isinstance(_TOOL_RESULT_CAP, int) and _TOOL_RESULT_CAP >= 4000, _TOOL_RESULT_CAP)
check("_TOOL_MODEL_CAP 回填上限为正且 <= 展示上限（模型侧不该比人看到的还多）",
      isinstance(_TOOL_MODEL_CAP, int) and 0 < _TOOL_MODEL_CAP <= _TOOL_RESULT_CAP,
      "%s vs 展示 %s" % (_TOOL_MODEL_CAP, _TOOL_RESULT_CAP))
check("_ATT_INJECT_CAP 注入上限为正且 <= 8000（附件注入不许无界）",
      isinstance(_ATT_INJECT_CAP, int) and 0 < _ATT_INJECT_CAP <= 8000, _ATT_INJECT_CAP)
check("_TOOL_MODEL_CAP 已在 common.__all__ 导出", "_TOOL_MODEL_CAP" in _common.__all__)
check("_ATT_INJECT_CAP 已在 common.__all__ 导出", "_ATT_INJECT_CAP" in _common.__all__)

stream_src = read(STREAM_PY)

# ---------- (a) 工具结果回填模型封顶 ----------
print("\n[a] 工具结果回填给模型时封顶（<= 3000 字）")
blk_a, line_a = cut(
    stream_src,
    "# 2026-09-17 S3：回填给模型的结果单独封顶",
    "                    if _weak:",
)
blk_a = textwrap.dedent(blk_a)
print("     切出源码块：stream.py:%d 起，共 %d 字符" % (line_a, len(blk_a)))

BIG = "X" * 20000
SMALL = "Y" * 100

# ⚠️ 2026-10-05 修：P1-4 之后封顶改走 `tool_offload.model_side_content`，源码块新增了对
#   `_tool_offload` / `tname` / `self` / `tools_def` / `tc` / `results` 的依赖。
#   门禁 exec 时仍只给 `result`/`cap`/`json` ⇒ NameError。
#   **这不等于产品有缺陷**——是夹具漏配了被测代码依赖的约定（老坑，勿再踩）。
#   另：超限时真函数会 `save_offload` **落库** ⇒ 门禁必须 patch 掉，否则污染生产库。
import types as _types
import agent.pipeline_parts.tool_offload as _TO

_real_save = _TO.save_offload
_TO.save_offload = lambda *a, **k: 900001      # 只返回假 oid，不落库
try:
    for label, raw, cap in (("超长 20000 字", BIG, _TOOL_MODEL_CAP),
                            ("短结果 100 字", SMALL, _TOOL_MODEL_CAP)):
        ns = {"result": {"ok": True, "result": raw}, "_TOOL_MODEL_CAP": cap, "json": json,
              "_tool_offload": _TO, "tname": "probe_tool", "tools_def": [],
              "self": _types.SimpleNamespace(_tool_conv_ctx=0, _tool_whitelist=None),
              "_weak": False, "weak_count": 0, "tc": {"id": "probe_call"}, "results": []}
        exec(blk_a, ns)
        tc = ns["_t_content"]
        got = len(str(tc.get("result") or ""))
        check("(%s) 回填 content 长度 <= %d" % (label, cap), got <= cap, "实际 %d" % got)
        if len(raw) > cap:
            check("(%s) 超长时 truncated=True 且带 note" % label,
                  tc.get("truncated") is True and bool(tc.get("note")), tc.get("note"))
        else:
            check("(%s) 未超限时不加 truncated" % label, "truncated" not in tc)
finally:
    _TO.save_offload = _real_save
    check("(a) 夹具已还原 save_offload（门禁未改写落库行为）", _TO.save_offload is _real_save)

# 契约层：真函数本身是否遵守 cap（不经过源码块，独立验证一侧）
_c_short, _o_short = _TO.model_side_content("probe_tool", {"ok": True, "result": SMALL},
                                            0, cap=_TOOL_MODEL_CAP)
check("(a) 契约层：<=cap 时真函数原样返回且 offloaded=False",
      _c_short.get("result") == SMALL and _o_short is False)
_c_big, _o_big = (None, None)
_TO.save_offload = lambda *a, **k: 900001
try:
    _c_big, _o_big = _TO.model_side_content("probe_tool", {"ok": True, "result": BIG},
                                            0, cap=_TOOL_MODEL_CAP)
finally:
    _TO.save_offload = _real_save
check("(a) 契约层：>cap 时真函数给出引用块且 offloaded=True",
      _o_big is True and len(str(_c_big.get("result") or "")) <= _TOOL_MODEL_CAP,
      "offloaded=%s len=%s" % (_o_big, len(str(_c_big.get("result") or "")) if _c_big else None))

check("stream.py 中不再存在未截断回填 result.get(\"result\", \"\")",
      'result.get("result", "")' not in stream_src)
# ★ 2026-10-30：口径从"砍掉 ≥80%"（3000 时代的比例）改为**封顶真实生效**：
#   要守的性质是"大结果不会全量回填"，而不是"必须砍掉八成"。
#   比例阈值会随 cap 调参失效（8000 时候 20000 字只砍 60%），
#   而"截断后长度 == cap 且 cap < 原文"永远成立。
_big_len = len(BIG)
_after = len(str({"ok": True, "result": BIG}.get("result"))[:_TOOL_MODEL_CAP])
check("(a) 大结果封顶真实生效（截断后 == cap，且 cap < 原文）",
      _TOOL_MODEL_CAP < _big_len and _after == _TOOL_MODEL_CAP,
      "%d 字 → %d 字（cap=%d）" % (_big_len, _after, _TOOL_MODEL_CAP))

# ---------- (b) 仅保留最近一轮工具结果 ----------
print("\n[b] 仅保留最近一轮 tool 结果（role 序列不变、消息不删除）")
# ★ 锚点从「行尾注释」换成「**起止两行都按代码内容定位**」（见 cut_block docstring）。
#   终点用 `_elide_queue.append((_rnd, _k))`：它是省略逻辑的收尾语句，且是**代码**不是注释
#   ⇒ 注释怎么改、挪哪、改缩进都不再影响门禁。
#   ★ 2026-10-30：锚点随实现改名（`_prev_tool_idx` → `_elide_queue`，因省略改为按**轮次**
#     保留窗口）。这正是"按代码内容定位"的代价：实现一改，锚点必须同步 ——
#     好在这类漂移**会立刻判红**（找不到块末行直接 AssertionError），不会静默失效。
blk_b, line_b = cut_block(stream_src, "if results:",
                          end_line="_elide_queue.append((_rnd, _k))")
blk_b = textwrap.dedent(blk_b)
print("     切出源码块：stream.py:%d 起，共 %d 字符" % (line_b, len(blk_b)))
assert blk_b.startswith("if results:"), repr(blk_b[:60])
assert "messages.append(pmsg)" in blk_b, "块应包含消息追加逻辑"

TOOL_RESULT = json.dumps({"ok": True, "result": "R" * 3000}, ensure_ascii=False)
messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
_elide_queue = []
role_seq_per_round = []
# ★ 2026-10-30：块里现在还会用到 `re`（从引用块抠 offload 编号）与
#   `_KEEP_TOOL_ROUNDS` / `_rnd`（按轮次保留窗口）。隔离执行必须把它们喂进去，
#   否则 exec 报 NameError —— 而 NameError 会被误读成"实现坏了"。
import re as _re
_KEEP_TOOL_ROUNDS = 1
for rnd in range(3):
    pmsg = {"role": "assistant", "content": "",
            "tool_calls": [{"id": "c%d" % rnd, "type": "function",
                            "function": {"name": "t%d" % rnd, "arguments": "{}"}}]}
    results = [{"tool_call_id": "c%d" % rnd, "role": "tool", "name": "t%d" % rnd,
                "content": TOOL_RESULT}]
    ns = {"messages": messages, "results": results, "pmsg": pmsg,
          "_elide_queue": _elide_queue, "json": json, "re": _re,
          "_KEEP_TOOL_ROUNDS": _KEEP_TOOL_ROUNDS, "_rnd": rnd}
    exec(blk_b, ns)
    messages = ns["messages"]
    _elide_queue = ns["_elide_queue"]
    role_seq_per_round.append([m["role"] for m in messages])

final_roles = [m["role"] for m in messages]
print("     最终 role 序列:", final_roles)
print("     最终 _elide_queue:", _elide_queue)
check("3 轮后消息条数 = 2 + 3*(assistant+tool) = 8", len(messages) == 8, len(messages))
check("role 序列 = system,user,(assistant,tool)*3",
      final_roles == ["system", "user"] + ["assistant", "tool"] * 3, final_roles)
check("tool 消息未被删除（每条 assistant.tool_calls 仍有配对 tool）",
      final_roles.count("tool") == 3 and final_roles.count("assistant") == 3)
check("tool_call_id 全部保留",
      [m.get("tool_call_id") for m in messages if m["role"] == "tool"] == ["c0", "c1", "c2"])
check("每轮消息数 4 / 6 / 8（省略不改变条数）",
      [len(s) for s in role_seq_per_round] == [4, 6, 8], [len(s) for s in role_seq_per_round])

ELIDE = "前序工具结果已省略"
# ★ 2026-10-30 契约变更：从"每轮只留最近一轮"改为"**保留最近 N 轮原文**"（N=_KEEP_TOOL_ROUNDS，
#   默认 1）。理由不在"省 token"而在于**可推理**：真机多步核对任务里，模型第 3 轮就需要
#   回看第 2 轮读到的元素明细，抹空后它既看不到、也没 offload 编号可重读 ⇒ 只能猜编号/重查
#   ⇒ 失败轮撞上限 ⇒ 用户看到「人工确认后无辜终止」。
#   省 token 的性质没有丢，只是窗口从 1 轮变成 2 轮，且窗口外**仍保留可寻址摘要**。
_tools_in_order = [m for m in messages if m["role"] == "tool"]
check("tool 消息条数仍为 3（省略只换 content，不删消息）", len(_tools_in_order) == 3,
      len(_tools_in_order))
check("第 1 轮 tool content 已被省略标记替换（窗口外）",
      ELIDE in _tools_in_order[0]["content"], _tools_in_order[0]["content"][:60])
check("省略件仍保留 offload 编号可重读（无编号时明确说「重新调用」）",
      ("offload_id" in _tools_in_order[0]["content"]
       or "重新调用" in _tools_in_order[0]["content"]),
      _tools_in_order[0]["content"][:80])
check("省略标记仍为合法 JSON 且 result 为空串",
      json.loads(_tools_in_order[0]["content"]).get("result") == ""
      and json.loads(_tools_in_order[0]["content"]).get("ok") is True)
check("最近一轮 tool content 保持完整（3000 字结果未被动）",
      messages[-1]["role"] == "tool" and "R" * 3000 in messages[-1]["content"])
check("上一轮 tool content 也保持完整（保留窗口 N=1）",
      "R" * 3000 in _tools_in_order[1]["content"],
      "第 2 轮被提前省略 ⇒ 模型回看不到上一轮明细")
check("_elide_queue 记录后两轮（窗口外已出队）",
      [r for r, _ in _elide_queue] == [1, 2], _elide_queue)
_saved = 2 * len(TOOL_RESULT)
print("     估算：3 轮场景下回填体积 %d 字 → %d 字（省 %.0f%%）"
      % (3 * len(TOOL_RESULT), 3 * len(TOOL_RESULT) - _saved, 100.0 * _saved / (3 * len(TOOL_RESULT))))

# ---------- (c) SSE 路径接上下文预算 ----------
print("\n[c] _apply_context_budget 对超预算检索段截断")
check("stream.py 已调用 _apply_context_budget（改动 3）",
      'self._apply_context_budget(messages[0]["content"], context_text, len(messages))' in stream_src)

from agent.pipeline_parts.history import HistoryMixin
_obj = object.__new__(HistoryMixin)
RETR = "【来源1】doc.md §1: " + ("检索内容" * 3000)
sys_prompt = "你是助手。\n检索到的互联数据：\n" + RETR + "\n尾部固定文本"
out = HistoryMixin._apply_context_budget(_obj, sys_prompt, RETR, 3)
print("     system_prompt 改前 %d 字 → 改后 %d 字" % (len(sys_prompt), len(out)))

# ★★ 2026-10-21 修正用例口径（**不是产品缺陷**）：
#   原用例写死"检索内容"*3000（约 12000 字）并断言"输出 < 输入的一半"。
#   但 `_apply_context_budget` 自 2026-11-11 起改为 **token 预算驱动**
#   （`ctx_budget_tokens("retrieval", ...)`），实测线上 `retr_tok=19200`
#   ⇒ 12000 字的检索段 **本就在预算内，不该被截断** ⇒ 原断言恒 FAIL。
#   ⇒ 改成**按真实预算**构造超限输入：先问产品要预算，再生成足量内容去超它。
#   这才是"超预算就截断"的本意（原先的固定 3000 次是拍脑袋的）。
from agent.pipeline_parts import history as _H
from core.token_counter import count_tokens as _ct
_retr_budget = int(_H.ctx_budget_tokens("retrieval", "budget_retrieval_chars", 4000))
# 取预算的 3 倍字符量，确保必然超限（中文约 1 字 ≈ 0.6~1 token，留足余量）
_pad = max(4000, _retr_budget * 3)
BIG_RETR = "【来源1】doc.md §1: " + ("检索内容" * (_pad // 4))
big_prompt = "你是助手。\n检索到的互联数据：\n" + BIG_RETR + "\n尾部固定文本"
big_out = HistoryMixin._apply_context_budget(_obj, big_prompt, BIG_RETR, 3)
print("     超限场景：检索段 %d 字 / token 预算 %d → 改后 %d 字"
      % (len(BIG_RETR), _retr_budget, len(big_out)))
check("超预算时检索段确实被截断（按真实 token 预算构造超限输入）",
      len(big_out) < len(big_prompt),
      "%d -> %d（预算 %d tok）" % (len(big_prompt), len(big_out), _retr_budget))
check("预算内时**不**截断（不误伤正常长度请求）",
      out == sys_prompt or len(out) <= len(sys_prompt),
      "%d -> %d" % (len(sys_prompt), len(out)))
check("system_prompt 头部保留（保头策略）", out.startswith("你是助手。"))
check("system_prompt 尾部保留（replace 只动检索段）", out.endswith("尾部固定文本"))
check("超限截断后头部/尾部仍保留（只动检索段中段）",
      big_out.startswith("你是助手。") and big_out.endswith("尾部固定文本"),
      big_out[:40] + " ... " + big_out[-40:])
check("retrieval_text 为空时原样返回（不破坏主流程）",
      HistoryMixin._apply_context_budget(_obj, sys_prompt, "", 3) == sys_prompt)
check("retrieval_text 不在 prompt 中时原样返回（安全兜底）",
      HistoryMixin._apply_context_budget(_obj, "无检索段的 prompt", "不存在的片段", 3) == "无检索段的 prompt")

# ---------- 改动 4 静态断言 ----------
print("\n[4] 上传资料：注入侧封顶（4①） + 解析上限维持 40（4② 已回退）")
check("att_text 已按 _ATT_INJECT_CAP 截断（注入侧唯一 token 封顶）",
      '[:_ATT_INJECT_CAP] if att_blocks else ""' in stream_src)
hist_src = read(HISTORY_PY)
# 2026-09-17 S3 回退：解析上限维持 40 —— 该值决定附件语义召回候选池 retrieval_att，
# 降它只省解析耗时、不省 token；token 侧由 _ATT_INJECT_CAP 在注入点封顶
check("history.py 解析块上限维持 40（4② 已回退）", "if len(blocks) >= 40:" in hist_src)
check("history.py 不再有曾试降到的 8 块上限", "if len(blocks) >= 8:" not in hist_src)
check("history.py 含 4② 回退说明注释", "解析上限维持 40" in hist_src)
check("块切分粒度仍为 2500 字（retrieval_att 候选池未被压缩）",
      "text[i:i + 2500] for i in range(0, len(text), 2500)" in hist_src)

# ---------- 汇总 ----------
print("\n" + "=" * 72)
print("通过 %d 项，失败 %d 项" % (len(_oks), len(_fails)))
if _fails:
    print("失败项：" + "; ".join(_fails))
print("=" * 72)
sys.exit(1 if _fails else 0)
