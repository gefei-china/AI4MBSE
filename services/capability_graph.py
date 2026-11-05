# -*- coding: utf-8 -*-
"""能力关系图谱：Agent ↔ 技能 / MCP / 工具 的绑定**到底有没有生效**（单一真源）。

═══════════════════════════════════════════════════════════════════════
为什么要这个模块（问题不是"缺一个页面"，是"配置与运行是两张皮"）
═══════════════════════════════════════════════════════════════════════
本仓的能力关系存在**四类实体 + 一张关系表**：

    agents ──< agent_tools >──┬── skills        （tool_type='skill'，按 name 关联）
                              ├── mcp_servers   （tool_type='mcp'  ，按 name 关联）
                              └── tools         （tool_type='tool' ，按 name 关联）
    plugins ──(manifest.runtime.legacy_table/legacy_id)──> skills / tools / mcp_servers
              （能力中心卡片走 plugins，运行时走旧表 —— 两套事实源靠同步桥连）

"绑定"只是**第一条边**。它能不能变成"AI 真的能用上"，要过四道闸，而四道闸
分散在四个文件里，**没有任何一处能回答"这条边现在还生效吗"**：

| 闸 | 判据（代码位置） | 不通过时的表现 |
|---|---|---|
| ① Agent 在路由池 | `agents.status='active'`（registry.load_from_db） | 整个 Agent 不参与，绑定全死 |
| ② 绑定行启用 | `agent_tools.enabled=1` | 静默跳过 |
| ③ 目标端可用 | 技能 `enabled`+正文非空 / MCP `status='online'`+`enabled` / 工具 `status='active'`（tools.py:425-430） | 静默跳过 |
| ④ 插件可消费 | `plugin_system.store.consumable_filter`（tools.py:515-545） | 静默剔除（有 warning 日志，页面无感知） |

第 ③④ 道还有**反向漏判**（本轮实测，见下），所以"看状态字段"并不等于"看生效"：

    · 内置工具绑定的注入走 `builtin_desc` 字典（tools.py:416），**不看 tools.status**
      ⇒ `review` 绑的 `validate`（status=disabled）仍被注入。
    · 插件「自动消费」通道（tools.py:495-512）不看 `tools.status`
      ⇒ `requirement_analysis` 绑的 `modeling_coverage`（status=disabled）仍被注入。
    · 技能的绑定注入只看 `enabled`+正文（agent_repo.bound_tools_for）
      ⇒ 插件侧已下架（`plugins.status='draft'`）的技能仍被注入（实测 3 个）。

⇒ 本模块的立场：**"生效"以运行时口径为准，不以状态字段为准**。
   状态字段只作为**给人看的原因**，用来解释"为什么生效/为什么不生效"。

## 三种"生效"必须分开说（否则永远吵不清）
1. **配置层**（绑定）：`agent_tools` 里有一行 —— 本模块的 `bindings`。
2. **装配层**（注入）：这一轮它会不会进模型上下文 —— 本模块的 `state`。
   MCP 还有第三层：绑定 ≠ 71 个工具全注入，`pick_for_injection` 会按语义挑子集
   （`min_keep`），所以 MCP 的 `effective=True` 只表示"服务器可用、工具可被发现"。
3. **运行层**（真被用）：`capability_registry.call_count / last_used_at` 与
   `tool_call_logs` —— 本模块的 `usage`。装上了但 30 天零调用，是"关系存在但不活跃"，
   与"关系失效"是两件事，界面必须分开显示（否则用户会把"没用过"当成"坏了"）。

## 状态枚举（`state`）
    effective   生效
    inactive    所属 Agent 已停用（关系保留，但不参与路由）
    dangling    目标行不存在（悬空绑定 —— 删除能力时没清关系）
    off         目标已停用（技能 enabled=0 / MCP enabled=0 / 工具 status≠active）
    offline     MCP 服务器不在线（探测失败或未测通）
    unready     工具配置未就绪（必需配置为空 ⇒ 注入了也必失败，已被注入侧跳过）
    draft       技能处于 draft_revision（**仍会被注入**，但正文是未发布修订）
    bypass      目标已停用 / 插件已下架，**但运行时仍会注入**（判据被旁路，见上）
    plugin_off  插件侧已下架/停用，但旧表仍启用（同上；单列以便治理）

`effective` 与 `state` 的关系：`effective = state in ('effective','draft','bypass','plugin_off')`
—— `draft/bypass/plugin_off` 都是"确实会进上下文，但状态不干净"，属于**该治理**而非**已失效**。
把这两类混成一个布尔，就会出现"看板说失效、AI 却真的在调"的自相矛盾（本轮修的就是这个）。

═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json

KIND_LABEL = {"skill": "技能", "mcp": "MCP 服务器", "tool": "工具"}

#: 状态 → (人话, 严重级)。`level` 只有四档，前端据此上色，不再各自发明。
STATE = {
    "effective":  ("生效", "ok"),
    "inactive":   ("所属 Agent 已停用", "muted"),
    "dangling":   ("目标已不存在（悬空绑定）", "bad"),
    "off":        ("目标已停用", "warn"),
    "offline":    ("MCP 离线", "warn"),
    "unready":    ("配置未就绪", "warn"),
    "draft":      ("草稿修订（仍会注入）", "info"),
    "bypass":     ("已停用但仍会被注入", "warn"),
    "plugin_off": ("插件侧已下架，但旧表仍启用", "warn"),
}

#: 会真正进模型上下文的状态集合（见模块 docstring 最后一段）
_EFFECTIVE_STATES = ("effective", "draft", "bypass", "plugin_off")


def _one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r is not None else None


def _plugin_of(conn, table, row_id):
    """旧表行 → 承载它的插件（能力中心卡片那条）。无映射返回 None。

    ⚠️ 必须按 `manifest_json.runtime.legacy_table/legacy_id` 反查，不能按名字：
       `plugins.name` 是 slug（中文名 ASCII 化后是 `skill-137` / `mcp`）。
    """
    try:
        r = _one(conn,
                 "SELECT plugin_id, name, type, status, scope FROM plugins "
                 "WHERE json_extract(manifest_json,'$.runtime.legacy_table')=? "
                 "  AND CAST(json_extract(manifest_json,'$.runtime.legacy_id') AS TEXT)=? "
                 "  AND status!='removed' "
                 "ORDER BY CASE status WHEN 'published' THEN 0 WHEN 'disabled' THEN 1 ELSE 2 END, id "
                 "LIMIT 1",
                 (table, str(row_id)))
        return r
    except Exception:            # noqa: BLE001  （json_extract 不可用/老库）
        return None


def _tool_readiness(config_raw):
    """工具配置就绪度。与注入侧同一个判据（`agent/tool_readiness.readiness`）。"""
    try:
        from agent.tool_readiness import readiness
        return readiness(json.loads(config_raw or "{}") if isinstance(config_raw, str) else (config_raw or {}))
    except Exception:            # noqa: BLE001
        return True, ""


def _builtin_names():
    """内置工具名集合 —— **必须取注入侧那份字典**，不能另抄一份。

    为什么：注入内置工具时 `_build_tools_def` 用的是 `builtin_desc` 的键
    （tools.py:416 `if name in builtin_desc`），而 `tools.source='builtin'` 的行
    与它**并不等价**（实测：`entity_create` 有 builtin 行，但 design 注入里没有它；
    反过来 `validate` 行被停用，仍被注入）。抄一份名单必然漂移 ⇒ 直接引用。
    """
    try:
        from agent.pipeline_parts.tools import BUILTIN_DESC
        return set(BUILTIN_DESC.keys())
    except Exception:            # noqa: BLE001
        return set()


def judge_binding(conn, kind, name, agent_status="active", bind_enabled=1,
                  with_usage=False, user=None):
    """判定**一条绑定**的生效状态。返回 dict（见模块 docstring 的状态枚举）。

    纯读，不写库；不依赖 AgentPipeline（可被 API / 门禁 / 脚本共用）。
    """
    out = {"kind": kind, "kind_label": KIND_LABEL.get(kind, kind), "name": name,
           "state": "effective", "effective": True, "reason": "", "hint": "",
           "target": None, "plugin": None}
    if not bind_enabled:
        out.update(state="off", effective=False, reason="绑定行已关闭（agent_tools.enabled=0）",
                   hint="需要就重新勾选；不需要就解绑，别留着")
        return out
    if str(agent_status or "") != "active":
        out.update(state="inactive", effective=False,
                   reason="所属 Agent 已停用 ⇒ 不进入路由池，这条绑定整条不参与",
                   hint="要么启用 Agent，要么把绑定清掉（停用的 Agent 上留着绑定只会误导）")
        return out

    if kind == "skill":
        # 与运行时**同一条解析顺序**（agent_repo.bound_tools_for 2026-10-30 起确定性排序）：
        # 否则"判的是 A 这份、注入的是 B 那份"，判定就自欺。
        rows = [dict(r) for r in conn.execute(
            "SELECT id,name,status,enabled,length(COALESCE(content,'')) clen,version "
            "FROM skills WHERE name=? "
            "ORDER BY CASE COALESCE(enabled,0) WHEN 1 THEN 0 ELSE 1 END, "
            "         CASE COALESCE(status,'') WHEN 'published' THEN 0 "
            "              WHEN 'draft_revision' THEN 1 ELSE 2 END, id", (name,))]
        row = rows[0] if rows else None
        if not row:
            out.update(state="dangling", effective=False,
                       reason="技能行已不存在（删技能时没清 agent_tools）",
                       hint="解绑该行；并让删除路径级联清理（见 verify_binding_graph D2）")
            return out
        out["target"] = {"table": "skills", "id": row["id"], "status": row["status"],
                         "enabled": row["enabled"], "version": row["version"]}
        out["plugin"] = _plugin_of(conn, "skills", row["id"])
        if len(rows) > 1:
            # 关系键 = name，而 name 不唯一 ⇒ 必须把"有几份、几份在用、正文是否不同"摆出来。
            # 只说"生效"会掩盖一个事实：**页面上那张卡和实际注入的那份可能不是同一行**。
            amb = _one(conn, "SELECT COUNT(DISTINCT content) dc FROM skills WHERE name=?", (name,))
            out["duplicates"] = {
                "count": len(rows),
                "enabled": sum(1 for r in rows if r.get("enabled")),
                "distinct_content": (amb or {}).get("dc", 0),
                "ids": [r["id"] for r in rows],
            }
            if out["duplicates"]["enabled"] > 1 and out["duplicates"]["distinct_content"] > 1:
                out["reason"] = ("同名 %d 份、其中 %d 份启用且正文不同 ⇒ 注入的是 id=%s 那份（按启用/发布/版本排序），"
                                 "**不一定是你在卡片上看到的那份**"
                                 % (len(rows), out["duplicates"]["enabled"], row["id"]))
                out["hint"] = "重名技能必须收敛成一份（改名或停用多余副本），否则绑定指向不确定"
        if not row["enabled"]:
            out.update(state="off", effective=False, reason="技能已停用（enabled=0）",
                       hint="启用技能后即恢复；或解绑")
            return out
        if not row["clen"]:
            out.update(state="off", effective=False, reason="技能正文为空 ⇒ 注入的是空内容（等于没绑）",
                       hint="补正文或重新上传技能包")
            return out
        if (row["status"] or "") == "draft_revision":
            out.update(state="draft", reason="技能有未发布的草稿修订，当前注入的就是这份草稿",
                       hint="确认草稿内容无误后发布，否则模型用的是未审内容")
        pst = (out["plugin"] or {}).get("status")
        if pst in ("draft", "disabled", "removed"):
            out.update(state="plugin_off",
                       reason="插件侧状态=%s（未发布/已下架/停用），但技能行 enabled=1 ⇒ "
                              "绑定注入**不看插件状态**，仍然生效" % pst,
                       hint="这是判据旁路：确认要下架就同时停用技能；否则请在插件侧恢复")
        return out

    if kind == "mcp":
        row = _one(conn, "SELECT id,name,status,enabled,latency_ms,last_error,last_check,"
                         "length(COALESCE(tool_meta,'')) tmlen FROM mcp_servers WHERE name=?", (name,))
        if not row:
            out.update(state="dangling", effective=False, reason="MCP 服务器行已不存在",
                       hint="解绑该行")
            return out
        out["target"] = {"table": "mcp_servers", "id": row["id"], "status": row["status"],
                         "enabled": row["enabled"], "latency_ms": row["latency_ms"],
                         "last_check": row["last_check"], "last_error": row["last_error"]}
        out["plugin"] = _plugin_of(conn, "mcp_servers", row["id"])
        if not row["enabled"]:
            out.update(state="off", effective=False, reason="MCP 服务器已停用（enabled=0）",
                       hint="启用后恢复；或解绑")
            return out
        if str(row["status"] or "") != "online":
            out.update(state="offline", effective=False,
                       reason="MCP 未在线（%s）" % (row["last_error"] or "尚未测通"),
                       hint="先「测试连通」；离线时该服务器的工具对全部 Agent 都不可见")
            return out
        out["reason"] = "在线（%sms）" % (row["latency_ms"] if row["latency_ms"] is not None else "?")
        out["hint"] = ("该服务器的 %d 个工具**不会全量注入**：每轮按语义挑子集（pick_for_injection），"
                       "所以「绑定生效」不等于「71 个工具都在上下文里」" % (row["tmlen"] and len(
                           json.loads(_one(conn, "SELECT tools FROM mcp_servers WHERE id=?",
                                           (row["id"],))["tools"] or "[]")) or 0))
        pst = (out["plugin"] or {}).get("status")
        if pst in ("draft", "disabled", "removed"):
            out.update(state="plugin_off",
                       reason="插件侧状态=%s，但绑定注入只看 status='online' ⇒ 仍然生效" % pst,
                       hint="确认要下架就同时停用服务器")
        return out

    # ---- tool ----
    row = _one(conn, "SELECT id,name,source,status,kind,config FROM tools WHERE name=? "
                     "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id LIMIT 1", (name,))
    if not row:
        out.update(state="dangling", effective=False, reason="工具行已不存在（删除时没清绑定）",
                   hint="解绑该行；或把它恢复成注册工具")
        return out
    out["target"] = {"table": "tools", "id": row["id"], "status": row["status"],
                     "source": row["source"], "kind": row["kind"]}
    out["plugin"] = _plugin_of(conn, "tools", row["id"])
    is_builtin = (row["source"] or "") == "builtin" or row["name"] in _builtin_names()

    if str(row["status"] or "") != "active":
        if is_builtin:
            out.update(state="bypass",
                       reason="工具行已停用，但**内置工具按名字从 BUILTIN_DESC 注入**（不看 status）⇒ 不会被停用挡住",
                       hint="「停用」对内置工具无效（已知判据旁路）；要真正不可用只能解绑。"
                            "另外每轮还有 JIT 语义预筛，所以「没被停用挡住」≠「本轮一定出现」")
            return out
        # 非内置：插件「自动消费」通道可能绕过 status
        pst = (out["plugin"] or {}).get("status")
        if pst == "published":
            out.update(state="bypass",
                       reason="工具行已停用，但所属插件处于 published 且已安装 ⇒ "
                              "「自动消费」通道仍注入（不看 tools.status）",
                       hint="要真正停用请停用/下架该插件，或解绑")
            return out
        out.update(state="off", effective=False, reason="工具已停用（status=%s）" % row["status"],
                   hint="启用后恢复；内置/插件来源的停用不生效，见 bypass 说明")
        return out

    if not is_builtin:
        ok, why = _tool_readiness(row["config"])
        if not ok:
            out.update(state="unready", effective=False, reason=why,
                       hint="补全配置（能力中心·工具）后恢复；在此之前注入侧会跳过它")
            return out
        if out["plugin"] is None:
            out["reason"] = "注册工具（无插件映射）"
        else:
            out["reason"] = "注册工具 · 插件 %s（%s）" % (out["plugin"]["plugin_id"],
                                                       out["plugin"]["status"])
    else:
        out["reason"] = "内置工具（按名字注入）"
    return out


def agent_bindings(conn, agent_id, only_problems=False):
    """某 Agent 的全部绑定 + 生效判定 + 汇总。"""
    a = _one(conn, "SELECT id,name,display_name,status,agent_role FROM agents WHERE id=?", (agent_id,))
    if not a:
        return None
    rows = [dict(r) for r in conn.execute(
        "SELECT id,tool_type,tool_name,enabled FROM agent_tools WHERE agent_id=? "
        "ORDER BY tool_type, tool_name", (agent_id,))]
    binds = []
    for r in rows:
        j = judge_binding(conn, r["tool_type"], r["tool_name"],
                          agent_status=a["status"], bind_enabled=r.get("enabled", 1))
        j["binding_id"] = r["id"]
        j["state_label"], j["level"] = STATE.get(j["state"], (j["state"], "muted"))
        binds.append(j)
    summary = {"total": len(binds), "effective": sum(1 for b in binds if b["effective"]),
               "problem": sum(1 for b in binds if not b["effective"])}
    by_kind = {}
    for b in binds:
        k = by_kind.setdefault(b["kind"], {"total": 0, "effective": 0})
        k["total"] += 1
        k["effective"] += 1 if b["effective"] else 0
    summary["by_kind"] = by_kind
    if only_problems:
        binds = [b for b in binds if not b["effective"]]
    return {"agent": a, "bindings": binds, "summary": summary}


def usage_of(conn, kind, name):
    """**反向关系**：谁在用这个能力 + 它到底被调过没有。

    回答两个不同问题（必须分开，否则"没人用"会被误读成"坏了"）：
      · 关系侧：哪些 Agent 绑了它、其中几条真生效；
      · 运行侧：`capability_registry` 的调用次数/最后调用时间（能力中心已有度量）。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT at.agent_id, a.name, a.display_name, a.status, at.enabled "
        "FROM agent_tools at JOIN agents a ON a.id=at.agent_id "
        "WHERE at.tool_type=? AND at.tool_name=? ORDER BY a.name", (kind, name))]
    agents = []
    for r in rows:
        j = judge_binding(conn, kind, name, agent_status=r["status"],
                          bind_enabled=r.get("enabled", 1))
        agents.append({"id": r["agent_id"], "name": r["display_name"] or r["name"],
                       "raw_name": r["name"], "agent_status": r["status"],
                       "state": j["state"], "effective": j["effective"], "reason": j["reason"]})
    reg_kind = {"skill": "skill", "tool": "tool", "mcp": "mcp_server"}.get(kind, kind)
    stat = _one(conn, "SELECT call_count,fail_count,last_used_at FROM capability_registry "
                      "WHERE kind=? AND source_id=? LIMIT 1", (reg_kind, name))
    plugin = None
    tbl_id = {"skill": "skills", "tool": "tools", "mcp": "mcp_servers"}.get(kind)
    if tbl_id:
        trow = _one(conn, "SELECT id FROM %s WHERE name=? LIMIT 1" % tbl_id, (name,))
        if trow:
            plugin = _plugin_of(conn, tbl_id, trow["id"])
    return {
        "kind": kind, "kind_label": KIND_LABEL.get(kind, kind), "name": name,
        "agents": agents,
        "active_agents": [a for a in agents if a["effective"]],
        "inactive_agents": [a for a in agents if not a["effective"]],
        "usage": stat or {"call_count": 0, "fail_count": 0, "last_used_at": ""},
        "plugin": plugin,
    }


def audit(conn):
    """全量体检：所有绑定按状态归类 + 问题清单（给运维/门禁用）。"""
    agents = [dict(r) for r in conn.execute(
        "SELECT id,name,display_name,status FROM agents ORDER BY id")]
    by_state = {}
    problems = []
    total = 0
    for a in agents:
        got = agent_bindings(conn, a["id"])
        for b in got["bindings"]:
            total += 1
            by_state[b["state"]] = by_state.get(b["state"], 0) + 1
            if not b["effective"]:
                problems.append({"agent_id": a["id"], "agent": a["display_name"] or a["name"],
                                 "kind": b["kind"], "name": b["name"],
                                 "state": b["state"], "reason": b["reason"], "hint": b["hint"]})
    severe = ("dangling", "off", "offline", "unready")
    return {
        "total": total,
        "by_state": by_state,
        "effective": sum(v for k, v in by_state.items() if k in _EFFECTIVE_STATES),
        "broken": sum(by_state.get(k, 0) for k in severe),
        "broken_states": list(severe),
        "problems": problems,
        "inactive_agents": [{"id": a["id"], "name": a["display_name"] or a["name"]}
                            for a in agents if a["status"] != "active"],
    }


def usage_map(conn):
    """全量 {kind: {name: {agents, effective}}} —— 给能力中心卡片一次性批量标注。

    为什么批量：卡片是按 kinds 分页渲染的（几十张），逐卡请求 = 几十个往返；
    这里一次扫完全部绑定（179 行）成本可忽略。
    """
    out = {"skill": {}, "mcp": {}, "tool": {}}
    for r in conn.execute("SELECT tool_type,tool_name,agent_id FROM agent_tools"):
        out.setdefault(r["tool_type"], {}).setdefault(r["tool_name"], 0)
        out[r["tool_type"]][r["tool_name"]] += 1
    return out
