# -*- coding: utf-8 -*-
"""元能力工具：`capability_query`（只读）/ `capability_apply`（写，走人工确认）。

2026-11-07。回答用户问题「是否可以实现 agent / skill / 工具等的新增修改、绑定、
差选、使用、修改、移除」。

## 为什么拆两个工具

HIL 写前确认闸**按工具名取档**（`tools.side_effect`）。若合成一个带 `action` 参数
的工具并标 write，则 `action=list`（"查一下我有几个技能"）也会进确认队列
⇒ 每查一次点一次批准 ⇒ **确认疲劳**（OWASP ASI09 明确点名的失效模式：
人一旦被逼着频繁点通过，审批就退化成形式）。
与既有 `sys_query_*`（只读）和写工具分离的惯例一致。

## 覆盖矩阵（这就是"能不能做"的答案）

| kind | list | get/diff | create | update | bind/unbind | enable/disable | delete |
|---|---|---|---|---|---|---|---|
| agent | ✅ | ✅ | ✅ | ✅ | ✅（反向：给 Agent 挂能力） | ✅ | ✅（内置拒绝） |
| skill | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅（内置拒绝） |
| tool | ✅ | ✅ | ✅（HTTP） | ✅ | ✅ | ✅ | ✅（内置拒绝） |
| capability | ✅ | ✅ | ✅（代码，先审查） | ✅ | ✅ | ✅ | ✅（同时删沙箱目录） |

## 「使用」的定义（别混为一谈）

- `bind` / `enable` = 让能力**对某个 Agent 生效**（配置动作）
- 想验证真能跑：下一轮会话让它自己调；或在 Studio 里用工具试跑
- 本工具**不会**在绑定时自动执行目标能力（"绑定即执行"会把"批准一次配置"
  变成"批准一次副作用"，两者不是一回事）
"""
from __future__ import annotations

__all__ = ["META_TOOL_DEFS", "META_QUERY_NAMES", "META_APPLY_NAMES",
           "exec_capability_tool"]

META_QUERY_NAMES = ("capability_query",)
META_APPLY_NAMES = ("capability_apply",)

_KIND_DESC = "目标类型：agent / skill / tool / capability"

META_TOOL_DEFS = [
    {
        "name": "capability_query",
        "description": (
            "查询平台的能力资产：Agent / 技能 / 工具 / AI自注册能力，以及它们的绑定关系。"
            "动作：list（按关键词列出）、get（看某个的详情）、diff（看某能力绑到了谁、谁还没绑）。"
            "用途：回答「我有哪些 Agent / 技能」「这个工具绑给了谁」「谁还没绑上」这类问题；"
            "在做增删改之前先用它确认现状。只读，不需要确认。"
        ),
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["agent", "skill", "tool", "capability"],
                         "description": _KIND_DESC},
                "action": {"type": "string", "enum": ["list", "get", "diff"],
                           "description": "list=列清单；get=看详情；diff=看绑定差异"},
                "target": {"type": "string",
                           "description": "get/diff 的目标（名字或 id）"},
                "keyword": {"type": "string", "description": "list 时的关键词过滤"},
                "limit": {"type": "integer", "description": "list 返回上限，默认 100"},
            },
            "required": ["kind", "action"],
        },
    },
    {
        "name": "capability_apply",
        "description": (
            "新增/修改/绑定/解绑/启停/删除平台能力资产（Agent、技能、工具、AI自注册能力）。"
            "**所有写动作都会提交人工确认，在你点批准前不会改动任何东西。**"
            "动作：create / update / bind / unbind / enable / disable / delete。"
            "kind=capability 的 create 需要 script（Python，定义 run(**kwargs)），"
            "会先做静态安全审查、落到工作沙箱、按代码自动派生副作用档位，再注册成可调用工具。"
            "用途：用户说「给我加个能算 XX 的工具」「把某技能绑给某 Agent」「停用某工具」时使用。"
        ),
        # ★ 必须是 write：它会改配置（含新建可执行代码）。标 read 会绕过写前确认闸。
        "side_effect": "write",
        "risk_level": "medium",
        "version": "v1.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["agent", "skill", "tool", "capability"],
                         "description": _KIND_DESC},
                "action": {"type": "string",
                           "enum": ["create", "update", "bind", "unbind",
                                    "enable", "disable", "delete"],
                           "description": "要做的写动作（全部走人工确认）"},
                "target": {"type": "string",
                           "description": "update/bind/unbind/enable/disable/delete 的目标（名字或 id）；"
                                          "kind=agent 时表示「被挂能力的那个 Agent」"},
                "spec": {
                    "type": "object",
                    "description": (
                        "动作参数。create: {name, description, ...}。"
                        "capability 额外要 script 与 input_schema，可选 agents=[...]；"
                        "tool 额外要 url/method/headers/body/query；"
                        "skill 额外要 content/triggers；agent 额外要 display_name/agent_role/"
                        "system_prompt/tools/skills。"
                        "update: {fields:{字段:新值}}（字段白名单见工具说明）。"
                        "bind/unbind: {agents:[Agent名...]}；kind=agent 时用 "
                        "{tools:[...], skills:[...]} 反向挂载。"
                    ),
                },
                "reason": {"type": "string",
                           "description": "为什么要做这件事（写进确认卡，帮助人快速判断）"},
            },
            "required": ["kind", "action"],
        },
    },
]


def exec_capability_tool(name: str, arguments: dict | None = None,
                         ctx: dict | None = None) -> dict:
    """统一入口。`ctx` 提供 agent/conv/run/user，用于把确认单挂到正确会话上。"""
    from services import capability_admin as CA

    args = arguments or {}
    ctx = ctx or {}
    if name in META_QUERY_NAMES:
        return CA.query(kind=args.get("kind"), action=args.get("action"),
                        target=args.get("target") or "",
                        keyword=args.get("keyword") or "",
                        limit=int(args.get("limit") or 100))
    if name in META_APPLY_NAMES:
        r = CA.propose(kind=args.get("kind"), action=args.get("action"),
                       spec=args.get("spec") or {},
                       agent_id=str(ctx.get("agent") or ""),
                       conversation_id=int(ctx.get("conversation_id") or 0),
                       run_id=int(ctx.get("run_id") or 0),
                       task_key=str(ctx.get("task_key") or ""),
                       user=ctx.get("user"))
        # ★★ 2026-11-07（真机实测修）：**必须用既有收口协议把卡发出去**。
        #
        #   背景：`capability_apply` 被 `_PROPOSE_WRITE_EXEMPT` 豁免了写前确认闸
        #   （否则会双重确认），但豁免的代价是 —— 闸不再设 `confirm_required`，
        #   于是 `stream.py:5756` 的收口分支不触发 ⇒ **确认单生成了，会话里却没有卡片**。
        #   实测（probe 990517）：`hil_confirmations` 里有单 #79，SSE 里没有 `hil_confirm`
        #   ⇒ 用户只能去别的页面找到它，与"一站式"直接冲突。
        #
        #   ⇒ 这里手动补上 `confirm_required` + `confirm`，让既有收口链路原样生效
        #   （不改 stream.py：协议只有一处真源，越少分支越不容易漏）。
        if r.get("ok") and r.get("confirmation_id"):
            _pv = str(r.get("preview") or "")
            r["confirm_required"] = True
            r["confirm"] = {
                "id": int(r["confirmation_id"]),
                "tool": "capability_apply",
                "tool_type": "builtin",
                "level": "confirm",
                "match_key": "meta_propose",
                "kind": "write",
                "preview": _pv,
                "arguments": {"kind": args.get("kind"), "action": args.get("action"),
                              "spec": args.get("spec") or {}},
                "batch_count": 1,
                "title": "能力变更待人工确认",
            }
        return r
    return {"ok": False, "result": "未知元能力工具：%s" % name}
