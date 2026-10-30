# -*- coding: utf-8 -*-
"""P0-2（2026-10-28）harness 级强制闸：内核判定（纯函数，零 IO）。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.3.2 / §6 第 2 步。

对齐官方 `Stop` hook：
  *"the turn doesn't end until the typecheck passes"* —— 由 **harness** 执行，
  **不依赖模型配合**。这是它与"改提示词"的本质区别。

三条不可省的设计（缺一即退化成"又一个评估器"）：
  1. **判据是 verify 的实际执行结果**，不是 LLM 判断 ⇒ 确定性
  2. **必须有上限**（对齐官方 8 次）⇒ 防 FM-1.5「该停不停」12.4%
  3. **强制放行时必须如实告知**（"未完成，卡在 X"）⇒ 不能伪装成完成

⚠️ 本模块只做**判定**（该拦还是该放、拦住时说什么）。
   实际执行 verify、计数、强制放行的落库，由 IO 层（agent/pipeline_parts/autopilot.py
   的 _harness_gate）负责 —— 与 P0-1 同样的分层纪律。
"""

from __future__ import annotations

import re

__all__ = [
    "HARNESS_MAX_BLOCKS",
    "HARNESS_FORCE_NOTICE_MARK",
    "parse_verify_spec",
    "harness_decide",
    "build_block_feedback",
    "build_force_release_notice",
]

#: 连续阻断上限（对齐官方 Stop hook 的 8 次）。
#: ⚠️ 上限是**必需**的：MAST FM-1.5「该停不停」12.4% > FM-3.1「过早终止」6.20%
#:   ⇒ 只堵"早停"会加剧"停不下来"。双向都要管。
HARNESS_MAX_BLOCKS = 8

#: 强制放行时的**必须出现**的措辞标记（门禁用它断言"如实告知"没被省掉）。
HARNESS_FORCE_NOTICE_MARK = "未完成"


#: 合法工具名形态：ASCII 字母/数字/下划线/点/冒号，且以字母开头。
#: ★ 为什么必须有这个校验（2026-10-28 真机实测暴露的核心缺陷）：
#:   全链路 A/B 跑出 B2 轮的实际工具调用记录里有这两条「工具」：
#:     · 「定位分支与版本（含草稿）后调用」 → 未知工具
#:     · 「人工确认：总结仅引用本次实际查到的工程与包树内容，字数≤200」 → 未知工具
#:   成因：verify 字段由 **LLM 自由书写**，它写的是自然语言描述，
#:   而 parse_verify_spec 原来"按空白切分、第一段当工具名" ⇒ 把中文句子当工具名去调。
#:   后果链：verify 必然失败 → 待办永远过不了验收 → 拦 2 次 → 无进展检测放行，
#:   且这些假调用污染 tool_call_logs（看起来像"工具坏了"）。
#:   ⇒ 契约不能只写在提示词里（遵循率不稳），**必须在解析层兜住**：
#:     不像工具名 ⇒ 按人工验收处理，绝不拿自然语言去调用。
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:]*$")


def parse_verify_spec(verify: str) -> dict:
    """解析 `verify` 字段为可执行规格。

    格式（容错解析，**不因格式问题抛异常**）：
      · `人工确认` ⇒ 人工验收（不可自动执行）
      · `工具名` ⇒ 调用该工具，参数为空
      · `工具名 {json}` / `工具名 参数` ⇒ 调用该工具并带参数
      · **其他任何自然语言描述** ⇒ 人工验收（见下方 _TOOL_NAME_RE 说明）

    :returns: ``{"kind": "manual"|"tool", "tool": str, "args": dict,
                 "raw": str, "parse_error": str}``
    """
    raw = str(verify or "").strip()
    if not raw:
        return {"kind": "manual", "tool": "", "args": {}, "raw": raw,
                "parse_error": "verify 为空"}
    if raw in ("人工确认", "manual", "人工"):
        return {"kind": "manual", "tool": "", "args": {}, "raw": raw,
                "parse_error": ""}
    # 找第一个空白：前面是工具名，后面是参数
    parts = raw.split(None, 1)
    tool = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not tool:
        return {"kind": "manual", "tool": "", "args": {}, "raw": raw,
                "parse_error": "解析不出工具名"}
    # ★ 关键：工具名形态校验。不像工具名 ⇒ 人工验收，**不拿自然语言去调工具**。
    if not _TOOL_NAME_RE.match(tool):
        return {"kind": "manual", "tool": "", "args": {}, "raw": raw,
                "parse_error": ("首段 %r 不像工具名（含中文/空格/标点或过长）"
                                "⇒ 按人工验收处理" % (tool[:30],))}
    args = {}
    perr = ""
    if rest:
        # 优先按 JSON 解析
        try:
            import json as _json
            obj = _json.loads(rest)
            if isinstance(obj, dict):
                args = obj
            else:
                args = {"value": obj}
        except Exception:
            # 非 JSON ⇒ 当作位置参数
            args = {"value": rest}
            perr = "参数非 JSON，按 value 传"
    return {"kind": "tool", "tool": tool, "args": args, "raw": raw,
            "parse_error": perr}


def harness_decide(gate: dict, blocks_used: int, max_blocks: int = HARNESS_MAX_BLOCKS,
                   *, harness_enabled: bool = True, stall_streak: int = 0,
                   max_stall: int = 2) -> dict:
    """决定这一轮**要不要拦住不让结束**。

    :param gate: `todo_gate()` 的结果（来自 agent_todo.todo_gate）
    :param blocks_used: 已经连续阻断过几次
    :param max_blocks: 连续阻断上限（0 = 不阻断）
    :param harness_enabled: 闸总开关（默认关 ⇒ 永不拦）
    :param stall_streak: **连续无进展**的阻断次数（remaining 与上轮完全相同）
    :param max_stall: 无进展多少次就提前放行（默认 2）
    :returns: ``{"block": bool, "reason": str, "force_release": bool,
                 "remaining": [...], "blocks_used": int, "max_blocks": int,
                 "stalled": bool}``

    ★ stall_streak 是**真机实测驱动**加的参数（2026-10-28）：
      首版只有"次数上限 8"，真机跑出来是这样 ——
        轮1 评估器：任务未完成 → 续跑
        轮1 harness：待办未过 → 拦
        轮2 评估器：空转（next_step 与上轮相同）→ 停续跑
        轮2 harness：待办仍未过 → 拦
        …重复 8 次，每次≈17 秒（重新生成），共≈2.3 分钟，最后强制放行
      ⇒ **结果和"一开始就放行"一样，只是慢了 2 分钟**。
      这是 MAST FM-1.5「该停不停」的活样本：光有次数上限不够，
      还要有**无进展检测**——同一批 remaining 连续 N 次没变，说明模型卡住了，
      提前放行并如实告知"卡在 X"，比空转到上限更有用。
    """
    blocks_used = max(0, int(blocks_used or 0))
    max_blocks = max(0, int(max_blocks or 0))
    stall_streak = max(0, int(stall_streak or 0))
    max_stall = max(1, int(max_stall or 2))
    remaining = list((gate or {}).get("remaining") or [])
    if not harness_enabled:
        return {"block": False, "reason": "harness 闸关闭", "force_release": False,
                "remaining": remaining, "blocks_used": blocks_used,
                "max_blocks": max_blocks, "stalled": False}
    if max_blocks == 0:
        return {"block": False, "reason": "阻断上限为 0 ⇒ 不阻断",
                "force_release": False, "remaining": remaining,
                "blocks_used": blocks_used, "max_blocks": max_blocks,
                "stalled": False}
    # 无待办 ⇒ 无从验收 ⇒ 不拦（D-3 兼容）
    if (gate or {}).get("fallback"):
        return {"block": False, "reason": "无待办 ⇒ 无验收条件，不拦",
                "force_release": False, "remaining": remaining,
                "blocks_used": blocks_used, "max_blocks": max_blocks,
                "stalled": False}
    # 全部完成 ⇒ 放行
    if (gate or {}).get("done"):
        return {"block": False, "reason": "待办全部通过验收 ⇒ 放行",
                "force_release": False, "remaining": [],
                "blocks_used": blocks_used, "max_blocks": max_blocks,
                "stalled": False}
    # ★ 无进展检测：同一批 remaining 连续 max_stall 次没变 ⇒ 提前放行
    #   这条**优先于**次数上限判定（先判放行，少走 6 次空转）
    if stall_streak >= max_stall:
        return {"block": False,
                "reason": (f"连续 {stall_streak} 次阻断后待办无任何变化 ⇒ 判定卡死，"
                           f"提前放行（未到次数上限 {max_blocks}）"),
                "force_release": True, "remaining": remaining,
                "blocks_used": blocks_used, "max_blocks": max_blocks,
                "stalled": True}
    # 有未完成 ⇒ 该拦 —— 但先看上上限
    if blocks_used >= max_blocks:
        return {"block": False,
                "reason": f"已连续阻断 {blocks_used} 次（上限 {max_blocks}）⇒ 强制放行",
                "force_release": True, "remaining": remaining,
                "blocks_used": blocks_used, "max_blocks": max_blocks,
                "stalled": False}
    return {"block": True,
            "reason": f"有 {len(remaining)} 项待办未通过验收（第 {blocks_used + 1}/{max_blocks} 次阻断）",
            "force_release": False, "remaining": remaining,
            "blocks_used": blocks_used, "max_blocks": max_blocks,
            "stalled": False}


def build_block_feedback(remaining, blocks_used: int, max_blocks: int,
                          failures=None) -> str:
    """构造回灌给模型的内容（对应官方 Stop hook 的 exit code 2 ⇒ 回灌错误输出）。

    ⚠️ 必须包含"还有哪些没做完"（可执行的清单），而不只是"你还没做完" ——
       模型需要知道**下一步做什么**才能继续，否则会空转。
    """
    items = [str(x) for x in (remaining or []) if str(x).strip()]
    lines = [
        "【验收未通过 · 本轮不允许结束】",
        f"还有 {len(items)} 项待办未通过验收（连续阻断第 {blocks_used}/{max_blocks} 次）：",
    ]
    for i, it in enumerate(items, 1):
        lines.append(f"  {i}. {it}")
    if failures:
        lines.append("")
        lines.append("验收失败明细：")
        for f in failures:
            lines.append(f"  · {f}")
    lines.append("")
    lines.append("【要求】先把这些待办真正做完并通过各自验收（调用验收方式指定的工具），"
                 "再把对应待办置为 completed（必须带 verify_result 回执）。"
                 "不要只给建议、也不要直接输出完成结论。")
    return "\n".join(lines)


def build_force_release_notice(remaining, blocks_used: int, max_blocks: int) -> str:
    """构造强制放行时的**如实告知**（设计第 3 条：不得伪装成完成）。

    ⚠️ 必须含 `HARNESS_FORCE_NOTICE_MARK`（「未完成」）—— 门禁据此断言
       "没有把未完成说成完成"。
    """
    items = [str(x) for x in (remaining or []) if str(x).strip()]
    lines = [
        f"⚠️ 本任务**未完成**（已连续 {blocks_used} 次阻断未通过验收，达到上限 {max_blocks}）。",
        f"仍未通过的待办 {len(items)} 项：",
    ]
    for i, it in enumerate(items, 1):
        lines.append(f"  {i}. {it}")
    lines.append("")
    lines.append("以上项目卡在验收环节，本轮不再自动继续。")
    return "\n".join(lines)
