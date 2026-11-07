# -*- coding: utf-8 -*-
"""MCP 工具的副作用档位派生（2026-11-06）

## 为什么需要这个模块

`tools` 表里的 `side_effect` / `risk_level` 是**执行策略门控**的关键输入：
`agent/pipeline_parts/tools.py` 对 `side_effect in ('write','destructive')` 的工具
默认**不注入**，只有输入里出现明确写意图关键词时才放行。

但 MCP 工具从来没进过 `tools` 表（实测 `SELECT COUNT(*) FROM tools WHERE mcp_server_id=161`
= **0**，而该 MCP 实际有 71 个工具）⇒ 上述门控对 MCP **完全失效** ⇒
71 个工具（其中 42 个非只读）被**无条件全量注入**给每个执行主体，
包括「知识问答Agent」「报告生成Agent」。

## 为什么不手工标注

手工标注在 MCP 升级那一刻就会失效，而且失效方式**比不标更危险**：
旧标注写着"读"，实际已变成"写"。⇒ 定位改为派生：
**标注是 discover 的派生产物，人工改动是受保护的覆写层**（见 `side_effect_source`）。

## 判据（顺序敏感，先命中先定档）

① 破坏性命名前缀（`delete_` / `remove_` / `drop_` / `purge_`）        → destructive
② `inputSchema.properties` 含顶层 **`approvalId`**                    → write
③ 含 **`commands`**（批量强类型命令，票据嵌在每条命令内）             → write
④ 写前缀命名（`create_` / `update_` / `set_` / …）                    → write
⑤ 其余                                                                → read

**为什么 ② 比命名可靠**：`approvalId` 是**智源平台自己的审批契约**——
平台认为这个操作需要审批票据才会在 schema 里要求它。实测该 MCP 71 个工具中
38 个带 `approvalId`（`readToken` 同批 38 个），而纯命名规则只能覆盖 67/71，
剩下 4 个（`present_model_elements` / `present_model_relationship` /
`place_model_activity_nodes` / `run_modeling_automation`）命名看不出读写，
**全靠 ② 救回**。同理 `validate_model` 命名像只读，但其描述写明"保存违规记录"，
也由 ② 判为 write —— 命名判据会漏它。

## 规则版本

改判据时**必须**同时提升 `RULE_VERSION`：discover 会按版本号重推所有
`side_effect_source='auto'` 的行，而 `'manual'` 行一律不动（人工覆写优先）。
"""
from __future__ import annotations

import re

#: 派生规则版本。**改动判据必须加一**，否则存量 auto 行不会被重推。
RULE_VERSION = 1

#: 破坏性命名前缀（物理删除/清空类）
_DESTRUCTIVE_PREFIX = re.compile(r"^(delete|remove|drop|purge|clear|reset)_", re.I)

#: 写前缀命名（兜底判据，仅在平台契约缺失时使用）
_WRITE_PREFIX = re.compile(
    r"^(create|update|set|add|insert|import|apply|clone|manage|configure|annotate"
    r"|layout|generate|undo|move|rename|present|place|run|bind|enable|disable"
    r"|upload|save|commit|merge|restore|submit|publish|approve|reject)_",
    re.I,
)

#: 平台审批契约的顶层参数名。出现其一 ⇒ 平台要求审批 ⇒ 写操作。
_APPROVAL_PARAMS = ("approvalId",)

#: 批量强类型命令的顶层参数名。票证据嵌在每条命令内，故顶层没有 approvalId。
_BATCH_COMMAND_PARAMS = ("commands",)

_RISK_BY_EFFECT = {"read": "low", "write": "medium", "destructive": "high"}


def _props(tool_meta: dict) -> set:
    """取工具的顶层参数名集合（对脏 schema 完全容错）。"""
    try:
        sch = (tool_meta or {}).get("inputSchema") or {}
        if not isinstance(sch, dict):
            return set()
        pr = sch.get("properties") or {}
        if not isinstance(pr, dict):
            return set()
        return set(pr.keys())
    except Exception:
        return set()


def derive(tool_meta: dict) -> tuple:
    """派生单个 MCP 工具的 `(side_effect, risk_level, rule)`。

    `tool_meta` 为 MCP `tools/list` 返回的单条 `{name, description, inputSchema}`。
    任何字段缺失/类型异常都退回 `('read', 'low', 'default_read')` ——
    **保守方向**：读是"最不放行"的档（写类工具默认不注入，判成读反而更严格）。
    这个方向是刻意的：误判成 read 只会让工具少出现，误判成 write 会让工具消失，
    两者都会暴露，但前者不会让危险能力被注入。

    Returns: (side_effect, risk_level, rule) —— rule 记录命中哪条判据，供审计/排查。
    """
    if not isinstance(tool_meta, dict):
        return "read", "low", "invalid_meta"
    name = str(tool_meta.get("name") or "").strip()
    if not name:
        return "read", "low", "empty_name"
    props = _props(tool_meta)

    if _DESTRUCTIVE_PREFIX.match(name):
        return "destructive", "high", "name_destructive"
    if any(p in props for p in _APPROVAL_PARAMS):
        return "write", "medium", "contract_approval"
    if any(p in props for p in _BATCH_COMMAND_PARAMS):
        return "write", "medium", "contract_commands"
    if _WRITE_PREFIX.match(name):
        return "write", "medium", "name_write"
    return "read", "low", "default_read"


def derive_all(tool_meta_list) -> list:
    """批量派生。返回 `[{name, side_effect, risk_level, rule, rule_version}, ...]`。

    保持输入顺序；同名只保留第一条（MCP 规范不允许重名，这里防御脏数据）。
    """
    out, seen = [], set()
    for m in (tool_meta_list or []):
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        eff, risk, rule = derive(m)
        out.append({"name": name, "side_effect": eff, "risk_level": risk,
                    "rule": rule, "rule_version": RULE_VERSION})
    return out


def summary(derived) -> dict:
    """统计各档数量，供日志/卡片展示。"""
    s = {"read": 0, "write": 0, "destructive": 0}
    rules = {}
    for d in (derived or []):
        eff = d.get("side_effect") or "read"
        s[eff] = s.get(eff, 0) + 1
        r = d.get("rule") or "-"
        rules[r] = rules.get(r, 0) + 1
    return {"counts": s, "rules": rules, "total": sum(s.values())}
