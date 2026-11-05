# -*- coding: utf-8 -*-
"""role 块内「工具手册段」按需裁剪（P0-7 · 2026-10-26）。

## 问题（实测，生产库快照 + 真实 execute 链路）

`agents.system_prompt`（= system prompt 的 `role` 块）存在一个**极端个案**：

| Agent | system_prompt | 其中工具手册 | 手册占比 | 非工具叙述 |
|---|---|---|---|---|
| **智源对接agent** | **14,538 字符 / 8,995 tok** | **14,531 字符 / 8,987 tok** | **100.0%** | **仅 7 字符** |
| 其余 16 个 Agent | 144 ~ 2,583 字符 | 0 | **0.0%** | 全部 |

即：`智源对接agent` 的 system_prompt **实质上是一份 58 个 MCP 工具的使用手册容器**，
没有任何 Agent 身份/职责描述（非工具叙述 7 字符）。

## 为什么这是问题（不只是"长"）

1. **与工具暴露粒度脱节**：上一轮已把 MCP 工具按需暴露到 8 个（`mcp_min_keep=8`），
   但 role 里仍全文注入 58 个工具的说明 ⇒ 其中 **50 个工具的用法对模型本轮根本不可用**
   （对应工具未进 tools payload），是纯噪音。
2. **MCP server 离线时 100% 死重**：生产库该 server `status='offline'` ⇒
   一个 MCP 工具都不可用 ⇒ 这 8,987 tok **完全无用**，却每轮照付。
3. **挤占上下文预算**：8,995 tok 比 `chat` 的 858 tok 大 10 倍；
   实测 system 占整包 98%（4,401 tok vs messages 89 tok），而这一项就占绝大部分。

## 做法（保守：只裁"能可靠识别"的段）

1. **识别**：工具名**独占行首**才算段落起点（与人工书写的「工具名换行接说明」一致）。
   只对**登记在 `mcp_servers.tools` 里的工具名**做匹配 —— 名字来源确定，不靠猜。
2. **保留**：非手册段（`__prose__`）**永远保留**；手册段仅保留「本轮暴露工具」对应的。
3. **兜底**：以下任一情形 ⇒ **原样返回，零影响**
   · 开关关闭（默认）
   · 未识别到任何手册段（其余 16 个 Agent 全属此类）
   · 本轮暴露集为空（此时**不裁** —— 不能因为"没算出暴露集"就把手册全删，
     那是把"信息缺失"变成"指令缺失"）
   · 裁剪后非手册段变空且手册段全被删（异常，保守回退）

## 为什么默认关闭

这是**改模型可见内容**的改动，风险等级高于前面几项（那些只改注入量/去重）。
必须先在生产上看到 `role` 块 token 实际下降、且任务正确率不掉，才逐步打开。
开关：`context_audit.role_manual_filter`（默认 False）。
"""

from __future__ import annotations

import re

#: 段落标记：非手册的正文（永远保留）
PROSE = "__prose__"


def split_by_tool_names(text: str, tool_names):
    """按工具名切分文本 → `[(tool_name|PROSE, segment_text)]`。

    切分规则（**宁可少切，不可切错**）：
      工具名必须**独占一行**（行首匹配、行尾只允许空白）才作为段落起点。
      正文里"用 query_projects 查询"这类提及**不会**被当成段落起点。

    返回顺序 = 原文顺序；所有段拼起来 == 原文（**无损**，可逐字节校验）。
    """
    if not text or not tool_names:
        return [(PROSE, text)] if text else []
    names = sorted((n for n in tool_names if n), key=len, reverse=True)
    if not names:
        return [(PROSE, text)]
    pat = re.compile(r"(?m)^(" + "|".join(re.escape(n) for n in names) + r")[ \t]*$")
    marks = [(m.start(), m.group(1)) for m in pat.finditer(text)]
    if not marks:
        return [(PROSE, text)]
    out = []
    if marks[0][0] > 0:
        out.append((PROSE, text[:marks[0][0]]))
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((name, text[pos:end]))
    return out


def filter_role_manual(role_text: str, tool_names, exposed_names) -> tuple:
    """裁剪 role 里的工具手册段，只保留 `exposed_names` 对应的。

    返回 `(filtered_text, stats)`；`stats` 用于归因：
      · `mode`      : `off` / `no_manual` / `no_exposed` / `applied`
      · `segments`  : 识别到的段落总数（含 prose）
      · `manual`    : 手册段数
      · `kept`      : 保留的手册段数
      · `dropped_chars` : 被裁掉的字符数

    ⚠️ 兜底优先于裁剪：任何不确定的情形都返回原文本（`mode` 注明原因）。
    """
    stats = {"mode": "off", "segments": 0, "manual": 0, "kept": 0, "dropped_chars": 0}
    if not role_text:
        stats["mode"] = "empty"
        return role_text, stats

    segs = split_by_tool_names(role_text, tool_names)
    stats["segments"] = len(segs)
    manual = [(n, t) for n, t in segs if n != PROSE]
    stats["manual"] = len(manual)
    if not manual:
        stats["mode"] = "no_manual"
        return role_text, stats

    # 无损性自检：切分后拼回必须与原文一致，否则不裁（防止正则吃掉字符）
    if "".join(t for _n, t in segs) != role_text:
        stats["mode"] = "split_mismatch"
        return role_text, stats

    exposed = {n for n in (exposed_names or []) if n}
    if not exposed:
        # 暴露集为空 ≠ 应删光手册。保守：不裁（无法判断"真的没暴露"还是"没算出来"）
        stats["mode"] = "no_exposed"
        return role_text, stats

    kept_parts = []
    dropped = 0
    for name, seg in segs:
        if name == PROSE:
            kept_parts.append(seg)
            continue
        if name in exposed:
            kept_parts.append(seg)
            stats["kept"] += 1
        else:
            dropped += len(seg)
    stats["dropped_chars"] = dropped
    if dropped == 0:
        stats["mode"] = "nothing_to_drop"
        return role_text, stats

    out = "".join(kept_parts)
    stats["mode"] = "applied"
    return out, stats


def collect_mcp_tool_names(bound_mcp, load_meta=None):
    """从 `registry.get_bound_mcp(intent)` 的结果收集全部工具名。

    `bound_mcp` 形如 `[{"name":..,"tools":[..],"tool_meta":"json"}]`。
    `load_meta` 可注入（测试用）；为 None 时用真实 `mcp_tool_meta.load_tool_meta`。
    """
    names = set()
    for m in (bound_mcp or []):
        try:
            for n in (m.get("tools") or []):
                if isinstance(n, str) and n.strip():
                    names.add(n.strip())
        except Exception:
            continue
    return names


def compute_exposed(bound_mcp, user_input, min_keep, top_n, load_meta=None):
    """复算「本轮会暴露哪些 MCP 工具」—— 与 `tools.py` 的注入逻辑同一套规则。

    为什么要复算：`build_prompt_blocks` 在 `execute` 里**先于** `_build_tools_def` 执行
    （`execute.py:400` vs `:469`），此时还没有 tools_def。复算成本 = 读一次 tool_meta +
    纯字符串打分（不查库、不调模型），远低于"重排主流程"的风险。

    返回 `(exposed_names:set, any_meta:bool)`；`any_meta=False` ⇒ 该 agent 的 server
    全无 tool_meta（此时 tools.py 会走"全量"分支 ⇒ 这里也应视作"全部暴露"）。
    """
    if load_meta is None:
        from agent.mcp_tool_meta import load_tool_meta as load_meta  # noqa: N813
    exposed = set()
    any_meta = False
    for m in (bound_mcp or []):
        try:
            meta = load_meta({"tool_meta": m.get("tool_meta") or ""}) or {}
        except Exception:
            meta = {}
        if not meta:
            # 无元数据 ⇒ tools.py 走全量分支 ⇒ 该 server 的工具全部暴露
            for n in (m.get("tools") or []):
                if isinstance(n, str) and n.strip():
                    exposed.add(n.strip())
            continue
        any_meta = True
        try:
            from agent.mcp_tool_meta import pick_for_injection
            picked, _st = pick_for_injection(meta, user_input or "",
                                             min_keep=int(min_keep or 0),
                                             top_n=int(top_n or 0))
            exposed |= set(picked or [])
        except Exception:
            # 打分失败 ⇒ 保守视为全暴露（不因异常而删手册）
            exposed |= set(meta.keys())
    return exposed, any_meta
