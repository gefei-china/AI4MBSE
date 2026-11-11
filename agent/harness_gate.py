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
    "READONLY_VERIFY_TOOLS",
    "parse_verify_spec",
    "verify_tool_allowed",
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


#: ★ 2026-11-09（P0-2a）**verify 只读白名单**：只有这些工具允许被 harness 自动执行。
#:
#: 为什么必须有（真机事故，不是假想）：
#:   `agent_todos` 761 条的 `verify` 全是 LLM 自由书写的自然语言，其中 44%（333 条）
#:   被旧解析器按「首段是 ASCII 就当工具名」判成 tool 并**真去执行**，且 100% 带错参。
#:   工具名单里还混着写类/交互类：`save_artifact` 7、`requirement_commit` 5、
#:   `export_sysml_v2` 4、`zhiyuan_sysmlv2_import` 3、`autofix` 2、`file_write` 1、
#:   **`ask_user` 15（会在收尾时主动弹问题给用户）**。
#:   ⇒ 每拦一次就重跑一遍 ⇒ 重复写库、重复导入、莫名弹问。
#: 已经发生过的事故（conv 990463，2026-11-05 09:37）：
#:   verify 写「JSON 含 artifacts/evidence/risks/claims…」⇒ 首段 `JSON` 像工具名
#:   ⇒ 真调 `JSON` ⇒ `未知工具: JSON`（ok=0）⇒ 判定"验收未通过"⇒ 拦住不让结束
#:   ⇒ 模型只能反复 `todo_write` 改状态（它无法"修复"一个不存在的工具）⇒ 空转。
#:   **闸在拦，但拦的理由是它自己的解析错误。**
#:
#: 纪律：白名单是**允许清单**（默认拒绝），不是黑名单 —— 新增写类工具不必再来改这里。
READONLY_VERIFY_TOOLS = frozenset({
    # SysML v2 校验/检查（checker.jar，只读）
    "sysml_v2_validate", "sysml_v2_lint", "sysml_v2_project_check", "project_check",
    # 工程/模型只读查询
    "query_projects", "query_branches", "query_data_tree", "query_model_versions",
    "search_model_elements", "graph_retrieve", "sysml_ast_extract", "sysml_stdlib_meta",
    "coverage_matrix",
    # 文件/产物只读
    "file_read", "file_list", "read_attachment_section", "tool_result_fetch",
})


def verify_tool_allowed(tool: str) -> bool:
    """该工具是否允许被 harness 自动执行（只读白名单）。"""
    return str(tool or "").strip() in READONLY_VERIFY_TOOLS


def _parse_args(raw) -> tuple:
    """把结构化参数归一成 dict。返回 ``(args, err)``。

    ⚠️ 与旧实现的**关键差别**：非 JSON 参数**不再**塞成 ``{"value": "自然语言"}``
       送进工具。旧行为让"返回 verdict=pass 或 n_hard=0" 变成
       `sysml_v2_validate(value="返回 verdict=pass...")`，必然失败，
       而失败原因是参数错、不是代码错 —— 模型据此"修复"会越修越偏。
    """
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, dict):
        return dict(raw), ""
    s = str(raw).strip()
    if not s:
        return {}, ""
    try:
        import json as _json
        obj = _json.loads(s)
    except Exception:
        return {}, "参数非 JSON 对象 ⇒ 不执行（避免拿自然语言当参数）"
    if not isinstance(obj, dict):
        return {}, "参数不是 JSON 对象 ⇒ 不执行"
    return obj, ""


def parse_verify_spec(verify: str, verify_tool=None, verify_args=None,
                      tool_exists=None) -> dict:
    """解析验收规格为可执行规格（容错解析，**永不抛异常**）。

    解析优先级（前三级都可能产出 ``kind="tool"``，但都要过**只读白名单**）：
      1. **显式结构化字段** `verify_tool` + `verify_args`（2026-11-09 新增，推荐）
      2. `verify` 本身是 JSON 对象：``{"tool": "sysml_v2_validate", "args": {...}}``
      3. 旧式 ``工具名 {json}``（**参数必须是 JSON 对象**）
      4. 其余**任何**形态（含「工具名 + 自然语言参数」）⇒ **manual**

    ★ 第 3 级的收紧是本轮核心（2026-11-09）：
       旧实现只要首段是 ASCII 就当工具名，参数解析失败就 ``{"value": rest}``
       硬传 —— 生产上已造成「未知工具: JSON」空转（conv 990463）。
       现在**参数不像 JSON 就不执行**：宁可退成人工验收（漏拦），
       也不拿自然语言去调工具（误拦 + 副作用）。

    :param tool_exists: 可选的``callable(tool)->bool``，用于额外确认工具真实存在。
        纯函数层不查库，由 IO 层传入（保持本模块零 IO）。
    :returns: ``{"kind", "tool", "args", "raw", "origin", "parse_error", "blocked_reason"}``
        ``origin`` ∈ structured / json / legacy / manual，用于归因"这条验收是怎么判的"。
    """
    raw = str(verify or "").strip()
    base = {"kind": "manual", "tool": "", "args": {}, "raw": raw,
            "origin": "manual", "parse_error": "", "blocked_reason": ""}

    def _tool(tool: str, args: dict, origin: str, perr: str = "") -> dict:
        out = dict(base, kind="tool", tool=tool, args=args, origin=origin,
                   parse_error=perr)
        if not verify_tool_allowed(tool):
            out.update(kind="manual", blocked_reason="工具 %r 不在只读白名单 ⇒ 不执行" % tool)
            return out
        if tool_exists is not None:
            try:
                if not tool_exists(tool):
                    out.update(kind="manual",
                               blocked_reason="工具 %r 未注册/不可用 ⇒ 不执行" % tool)
                    return out
            except Exception:
                pass
        return out

    # ── 1) 显式结构化字段 ──
    vt = str(verify_tool or "").strip()
    if vt:
        if not _TOOL_NAME_RE.match(vt):
            return dict(base, parse_error="verify_tool %r 非法工具名 ⇒ 人工验收" % vt[:30])
        args, err = _parse_args(verify_args)
        if err:
            return dict(base, parse_error="%s（verify_args）" % err)
        return _tool(vt, args, "structured")

    if not raw:
        return dict(base, parse_error="verify 为空")
    if raw in ("人工确认", "manual", "人工"):
        return base

    # ── 2) verify 本身是 JSON 对象 ──
    if raw.startswith("{"):
        try:
            import json as _json
            obj = _json.loads(raw)
        except Exception:
            obj = None
        if isinstance(obj, dict) and str(obj.get("tool") or "").strip():
            tool = str(obj["tool"]).strip()
            if not _TOOL_NAME_RE.match(tool):
                return dict(base, parse_error="JSON.tool %r 非法工具名" % tool[:30])
            args, err = _parse_args(obj.get("args"))
            if err:
                return dict(base, parse_error="%s（JSON.args）" % err)
            return _tool(tool, args, "json")

    # ── 3) 旧式 `工具名 {json}`：参数**必须**是 JSON 对象 ──
    parts = raw.split(None, 1)
    tool = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not _TOOL_NAME_RE.match(tool):
        return dict(base, parse_error=("首段 %r 不像工具名（含中文/空格/标点或过长）"
                                      "⇒ 按人工验收处理" % (tool[:30],)))
    if not rest:
        return dict(base, parse_error=("只有工具名、未给参数 ⇒ 不执行"
                                      "（无法确定校验对象；请用 verify_tool + verify_args）"))
    try:
        import json as _json
        obj = _json.loads(rest)
    except Exception:
        return dict(base, parse_error=("参数非 JSON 对象（%r…）⇒ 不执行"
                                      "（旧行为会硬塞 value=自然语言，已在生产造成误判）"
                                      % (rest[:24],)))
    if not isinstance(obj, dict):
        return dict(base, parse_error="参数不是 JSON 对象 ⇒ 不执行")
    return _tool(tool, obj, "legacy")


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
