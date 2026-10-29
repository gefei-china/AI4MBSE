# -*- coding: utf-8 -*-
"""P0-1（2026-10-27）「实际可用集」—— 让治理视图说实话。

## 为什么需要这一层

走查发现一个**制度性矛盾**（实测，生产库）：

    tool:file_read   注册 ✅   绑定 ⚠️ 0 个 Agent   消费 ✅ 25 次

而能力中心的绑定面板只显示 `agent_tools`。于是从界面看 `file_read` "没人用"，
事实是它是被大量调用的基础能力。**治理视图与事实相反** —— 这正是治理视图最不该出错的地方。

根因不是"少写了个字段"，而是**"可用"没有唯一定义**：本仓实际有 **4 条声明入口**
（外加 1 条运行时旁路），分散在 `agent/pipeline_parts/tools.py::_build_tools_def` 的不同分支里：

| # | 入口 | 数据源 | 落点（唯一真源） |
|---|---|---|---|
| 1 | Agent 绑定 | `agent_tools`（tool_type ∈ tool/skill/mcp，enabled=1） | `tools.py` 三处：内置声明 / MCP 块 / DB 工具块 |
| 2 | Agent 内置声明 | 代码 `AgentRegistry.DEFINITIONS[*].tools` | `tools.py:273`（`agent_def.tools` ∩ `builtin_desc`） |
| 3 | 技能工具白名单 | `skills.allowed_tools`（published + enabled） | `tools.py:373-386`（`_sk_wl`） |
| 4 | 无条件注入 | 本模块 `UNCONDITIONAL_TOOLS` | `tools.py:648-675`（ask_user / read_attachment_section / tool_result_fetch） |
| — | **运行时旁路** | `tool_call_logs` 里"有调用但无任何声明"的名字 | `_exec_tool_call` 的前缀分派（无白名单时不拦） |

第 5 条（旁路）**不是要修掉的缺陷，而是必须如实公示的事实**：
把"未声明却被调用"显示成"可用"，等于再把视图做假一次。
所以本模块的口径是 —— **声明集**与**调用集**分开算，两者不一致的地方**照实标注**。

## 纪律

- **只读**：本模块全部是 SELECT，不写任何表。
- **不造假**：查不到的留空/标 `unknown`，不补占位行。
- **单一真源**：4 条入口的定义只写在这一份里，端点与门禁都从这里取，
  避免"界面一套、门禁一套"必然漂移（本仓老问题）。
"""

from __future__ import annotations

import json

# ★ 2026-10-29：账本的"调用集"必须排除探针/验证噪声。
#   为什么这里是**最要紧**的一处：账本直接回答"这个能力到底有没有被用"，
#   探针混入会让 `fake_tool`（126 次）出现在"运行时旁路"并排到第 2 位，
#   把一个假工具当成"实际在跑的能力"。实测（过滤前）正是如此。
#   口径真源：core/probe_mark.probe_where()（勿另写一份）。
from core.probe_mark import probe_where as _probe_where

_PROBE = _probe_where()

#: 无条件注入的运行时工具（第 4 条入口）。
#:
#: ⚠️ 这是一份**必须与代码同步**的清单 —— 它们不由任何表声明，只由
#: `agent/pipeline_parts/tools.py::_build_tools_def` 的尾部无条件追加。
#: 因此配了一条门禁断言（`tools/verify/verify_capability_chain.py` 的 C4）：
#: **本清单里每个名字都必须在该文件的源码里出现**，且该文件新出现的
#: `ensure_*_tool(` / 无条件 append 目标也必须登记进来 —— 否则清单一改就静默过期，
#: 界面会把"实际无条件可用"的工具报成"无声明入口"。
UNCONDITIONAL_TOOLS = ("ask_user", "read_attachment_section", "tool_result_fetch")

#: 入口类型的展示名（前端直接读，不在 JS 里再写一份中文）
ENTRY_LABELS = {
    "bound": "Agent 绑定",
    "skill_pool": "全局技能池",
    "agent_builtin": "Agent 内置声明",
    "skill_allowed": "技能工具白名单",
    "unconditional": "无条件注入",
    "runtime_only": "无声明入口（运行时旁路）",
}


def _json_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
    except Exception:
        return []
    return v if isinstance(v, list) else []


def _empty_entry() -> dict:
    return {
        "bound_agents": [],       # 第 1 条：绑定了它的 Agent（name）
        "skill_pool": [],         # 第 5 条：全局技能池（published+enabled 的技能对所有 Agent 可用）
        "agent_builtin": [],      # 第 2 条：代码里内置声明了它的 Agent（name）
        "skill_allowed": [],      # 第 3 条：把它写进 allowed_tools 的已发布技能（name）
        "unconditional": False,   # 第 4 条
    }


def collect_declared(conn) -> dict:
    """汇总 4 条**声明入口**。

    返回 `{"tools": {name: entry}, "skills": {name: entry}, "mcp_tools": {name: entry},
           "stats": {...}}`。

    - `tools`     —— 工具名（builtin / DB 工具 / MCP 工具统一按名字收口）
    - `skills`    —— 技能名（技能本身也是一种能力，其"绑定"就是 agent_tools.skill 行）
    - `mcp_tools` —— MCP 工具名（供与 `mcp_servers` 的 server 归属映射）
    """
    tools: dict = {}
    skills: dict = {}
    mcp_tools: dict = {}
    stats = {"bound_rows": 0, "builtin_agents": 0, "skill_whitelist_skills": 0}

    def _slot(bucket, name):
        name = str(name or "").strip()
        if not name:
            return None
        if name not in bucket:
            bucket[name] = _empty_entry()
        return bucket[name]

    # ── 第 1 条：agent_tools 绑定（只算 active Agent、enabled 行）──────────
    #   ⚠️ MCP 的绑定粒度是**服务器级**：`tool_type='mcp'` 的 `tool_name` 存的是
    #   **server 名**（实测：4 行都是 `zhiyuan-dev-mcp`），不是单个工具名。
    #   ⇒ 必须按 server 展开成它的工具清单，否则"绑了服务器却认不出它的工具"。
    _server_tools: dict = {}
    try:
        from core.capability_registry import extract_mcp_tool_names
        for r in conn.execute("SELECT name, tools, capabilities, server_info, tool_meta "
                              "FROM mcp_servers").fetchall():
            nm = r["name"] if hasattr(r, "keys") else r[0]
            _server_tools[nm] = extract_mcp_tool_names(
                (r["capabilities"] if hasattr(r, "keys") else r[2]),
                (r["server_info"] if hasattr(r, "keys") else r[3]),
                (r["tool_meta"] if hasattr(r, "keys") else r[4])) or _json_list(
                (r["tools"] if hasattr(r, "keys") else r[1]))
    except Exception:
        _server_tools = {}

    try:
        rows = conn.execute(
            "SELECT a.name AS agent, t.tool_type, t.tool_name "
            "FROM agent_tools t JOIN agents a ON a.id = t.agent_id "
            "WHERE COALESCE(t.enabled,1)=1 AND a.status='active'").fetchall()
    except Exception:
        rows = []
    for r in rows:
        # sqlite3.Row 与 tuple 都兼容（端点用 db_session 连接，门禁用裸连接）
        agent, ttype, tname = (r["agent"], r["tool_type"], r["tool_name"]) \
            if hasattr(r, "keys") else (r[0], r[1], r[2])
        ttype = str(ttype or "")
        stats["bound_rows"] += 1
        if ttype == "skill":
            e = _slot(skills, tname)
            if e is not None and agent not in e["bound_agents"]:
                e["bound_agents"].append(agent)
        elif ttype == "mcp":
            # server 级绑定 → 展开为该 server 的全部工具
            for _t in (_server_tools.get(tname) or [tname]):
                e = _slot(tools, _t)
                if e is not None and agent not in e["bound_agents"]:
                    e["bound_agents"].append(agent)
                em = _slot(mcp_tools, _t)
                if em is not None and agent not in em["bound_agents"]:
                    em["bound_agents"].append(agent)
        else:
            e = _slot(tools, tname)
            if e is not None and agent not in e["bound_agents"]:
                e["bound_agents"].append(agent)

    # ── 第 2 条：Agent 内置声明（代码 DEFINITIONS）─────────────────────────
    #   只有「DB 里没有同名 Agent」或「DB 定义被用作兜底」时才生效 —— 但**不可判**：
    #   `registry.get(intent)` 是运行时才决定用 DB 定义还是内置定义。
    #   因此这里**不猜**，只如实列出"代码里声明了它"这一事实，
    #   并在返回值的 `stats.builtin_note` 里说明该入口与 DB 绑定是**互斥兜底**关系。
    try:
        from agent.registry import AgentRegistry
        try:
            active_names = {(r["name"] if hasattr(r, "keys") else r[0])
                            for r in conn.execute(
                                "SELECT name FROM agents WHERE status='active'").fetchall()}
        except Exception:
            active_names = set()
        # 兜底语义：DB 里没有任何 active Agent 时（空库/夹具），内置定义就是唯一来源
        # ⇒ 全量计入；否则只算"代码里有、DB 里也 active"的（否则 `registry.get` 用不着它）。
        _only_when_absent = not active_names
        for aname, adef in (AgentRegistry.DEFINITIONS or {}).items():
            if not _only_when_absent and aname not in active_names:
                continue
            for tname in (getattr(adef, "tools", None) or []):
                e = _slot(tools, tname)
                if e is not None and aname not in e["agent_builtin"]:
                    e["agent_builtin"].append(aname)
            stats["builtin_agents"] += 1
    except Exception:
        stats["builtin_note"] = "AgentRegistry 不可用，内置声明未统计"

    # ── 第 5 条：全局技能池 + 第 3 条：技能 allowed_tools ────────────────────
    #   ⚠️ 第 5 条极易被漏掉，但它是**技能这个品类的主入口**：
    #   `skills.py::_global_skill_pool` 把所有 `status='published' AND enabled=1`
    #   的技能**无条件**纳入每个 Agent 的候选池（代码注释原文：
    #   "剔除 plugin 展开件后重跑，技能「文件操作」仍被正常触发（走全局池）"
    #   ⇒ "agent_tools 里的技能绑定完全冗余"）。
    #   漏掉它会把「覆盖性分析 / 行业调研 / 报告生成 / 智源平台管理」四个
    #   真实在用的技能误判成"无声明入口"——又一次把视图做假。
    try:
        srows = conn.execute(
            "SELECT id, name, allowed_tools FROM skills "
            "WHERE status='published' AND COALESCE(enabled,1)=1").fetchall()
    except Exception:
        srows = []
    for r in srows:
        sid, sname, raw = (r["id"], r["name"], r["allowed_tools"]) \
            if hasattr(r, "keys") else (r[0], r[1], r[2])
        se = _slot(skills, sname)
        if se is not None and "skill_pool" not in se["skill_pool"]:
            se["skill_pool"].append("全局")
        names = _json_list(raw)
        if not names:
            continue
        stats["skill_whitelist_skills"] += 1
        for tname in names:
            e = _slot(tools, tname)
            if e is not None and sname not in e["skill_allowed"]:
                e["skill_allowed"].append(sname)

    # ── 第 4 条：无条件注入 ────────────────────────────────────────────────
    for tname in UNCONDITIONAL_TOOLS:
        e = _slot(tools, tname)
        if e is not None:
            e["unconditional"] = True

    return {"tools": tools, "skills": skills, "mcp_tools": mcp_tools, "stats": stats}


def entry_kinds(entry: dict) -> list:
    """一条 entry 命中的入口类型（顺序固定，供前端稳定渲染）。"""
    kinds = []
    if entry.get("bound_agents"):
        kinds.append("bound")
    if entry.get("skill_pool"):
        kinds.append("skill_pool")
    if entry.get("agent_builtin"):
        kinds.append("agent_builtin")
    if entry.get("skill_allowed"):
        kinds.append("skill_allowed")
    if entry.get("unconditional"):
        kinds.append("unconditional")
    return kinds


def build_ledger(conn, days: int = 30) -> dict:
    """把「声明集」×「调用集」拼成一张账本 —— 端点的唯一实现。

    **核心是那个 2×2 矩阵**（这是"让视图说实话"的全部内容）：

    |                 | 有调用 | 无调用 |
    |-----------------|--------|--------|
    | **有声明入口**  | 正常在用 | 已声明未用（冗余绑定，可清理） |
    | **无声明入口**  | ⚠️ 运行时旁路（视图的盲区，必须公示） | 未使用 |

    返回值：
      · `rows`      —— 每个能力的账本行（含 declared/verdict/call_count/…）
      · `matrix`    —— 上述 2×2 计数（页面顶部 KPI）
      · `orphan_calls` —— "有调用但不在注册表"的工具名（如运行时注入的 3 个、MCP 工具）
      · `stats`     —— 入口统计（口径可核对）
    """
    declared = collect_declared(conn)
    days = max(1, int(days or 30))

    # ── 调用集：窗口内按 capability_id / tool_name 聚合 ───────────────────
    usage_by_cid: dict = {}
    usage_by_name: dict = {}
    usage_error = ""
    try:
        for r in conn.execute(
                "SELECT capability_id, tool_name, COUNT(*) n, "
                "       SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) fails, MAX(created_at) last_at "
                "FROM tool_call_logs "
                "WHERE created_at >= datetime('now', ?)" + _PROBE + " "
                "GROUP BY capability_id, tool_name", ("-%d days" % days,)).fetchall():
            cid, nm, n, fails, last_at = (r["capability_id"], r["tool_name"], r["n"],
                                          r["fails"], r["last_at"]) \
                if hasattr(r, "keys") else tuple(r)
            nm = str(nm or "")
            if nm:
                agg = usage_by_name.setdefault(nm, [0, 0, ""])
                agg[0] += int(n or 0)
                agg[1] += int(fails or 0)
                if (last_at or "") > agg[2]:
                    agg[2] = last_at or ""
            if cid:
                agg = usage_by_cid.setdefault(cid, [0, 0, ""])
                agg[0] += int(n or 0)
                agg[1] += int(fails or 0)
                if (last_at or "") > agg[2]:
                    agg[2] = last_at or ""
    except Exception as e:
        # ★ 2026-10-29：**不静默**。
        #   原实现是 `except: pass` —— 代价是：一旦 SQL 出错（如拼错列名），
        #   usage 集全空 ⇒ 账本把**全部能力**判成"未使用"，而界面上
        #   这与"真的没人用"长得一模一样（本仓已记过这个坑）。
        #   现在把错误带进 stats，页面「口径」区可见。
        usage_error = "%s: %s" % (type(e).__name__, str(e)[:160])

    # ── 注册表行（统一账本的"名前"那一半）────────────────────────────────
    rows = []
    reg_names = set()
    try:
        reg_rows = conn.execute(
            "SELECT * FROM capability_registry ORDER BY kind, capability_id").fetchall()
    except Exception:
        reg_rows = []
    for r in reg_rows:
        d = dict(r) if hasattr(r, "keys") else {}
        if not d:
            continue
        cid = d.get("capability_id") or ""
        kind = d.get("kind") or ""
        name = d.get("display_name") or ""
        reg_names.add(name)
        # 声明入口：tool 类看 tools 桶；skill 类看 skills 桶；mcp_tool 看 mcp_tools+名称匹配
        if kind == "skill":
            entry = declared["skills"].get(name) or declared["tools"].get(name) or _empty_entry()
        elif kind == "mcp_tool":
            entry = declared["mcp_tools"].get(name) or declared["tools"].get(name) or _empty_entry()
        else:
            entry = declared["tools"].get(name) or _empty_entry()
        kinds = entry_kinds(entry)
        use = usage_by_cid.get(cid)
        if use is None and kind == "mcp_server":
            # MCP server 的调用记在工具级（mcp:{server}:{tool}）⇒ 按前缀汇总。
            # 与 `capability_registry.aggregate_capability_metrics` 同一口径，
            # 否则"服务在跑但账本显示 0 次"会再次制造视图与事实不符。
            _pref = cid + ":"
            _n = _f = 0
            _last = ""
            for _k, _v in usage_by_cid.items():
                if _k.startswith(_pref):
                    _n += _v[0]
                    _f += _v[1]
                    if _v[2] > _last:
                        _last = _v[2]
            if _n:
                use = [_n, _f, _last]
        if use is None:
            # capability_id 对不上（历史行可能是空/旧口径）⇒ 按名字兜底，**并标出口径**
            use = usage_by_name.get(name)
            src = "name"
        else:
            src = "capability_id"
        calls = int(use[0]) if use else 0
        fails = int(use[1]) if use else 0
        last_at = use[2] if use else ""
        if kinds and calls:
            verdict = "in_use"
        elif kinds:
            verdict = "declared_unused"
        elif calls:
            verdict = "runtime_only"
        else:
            verdict = "unused"
        rows.append({
            "capability_id": cid, "kind": kind, "display_name": name,
            "source_table": d.get("source_table") or "", "source_id": d.get("source_id") or "",
            "status": d.get("status") or "", "lifecycle": d.get("lifecycle") or "",
            "risk_level": d.get("risk_level") or "", "version": d.get("version") or "",
            "bound_agents": entry["bound_agents"][:20],
            "bound_count": len(entry["bound_agents"]),
            "agent_builtin": entry["agent_builtin"][:20],
            "skill_allowed": entry["skill_allowed"][:20],
            "unconditional": bool(entry["unconditional"]),
            "entries": kinds,
            "entry_labels": [ENTRY_LABELS[k] for k in kinds],
            "call_count": calls, "fail_count": fails, "last_used_at": last_at,
            "usage_source": src if calls else "",
            "verdict": verdict,
        })

    matrix = {"in_use": 0, "declared_unused": 0, "runtime_only": 0, "unused": 0}
    for x in rows:
        matrix[x["verdict"]] = matrix.get(x["verdict"], 0) + 1

    # ── 有调用但不在注册表的名字（运行时注入 / MCP 工具 / 已删能力）────────
    orphan = []
    for nm, agg in sorted(usage_by_name.items(), key=lambda kv: -kv[1][0]):
        if nm in reg_names:
            continue
        e = declared["tools"].get(nm) or declared["mcp_tools"].get(nm) or {}
        kinds = entry_kinds(e) if e else []
        orphan.append({
            "tool_name": nm, "call_count": agg[0], "fail_count": agg[1],
            "last_used_at": agg[2],
            "entries": kinds or ["runtime_only"],
            "entry_labels": [ENTRY_LABELS[k] for k in kinds] or [ENTRY_LABELS["runtime_only"]],
        })

    stats = dict(declared.get("stats") or {})
    # ★ 调用集取数失败必须可见（否则"取数坏了"会被读成"全部能力零调用"）
    stats["probe_filtered"] = True
    if usage_error:
        stats["usage_error"] = usage_error
    return {
        "window_days": days,
        "rows": rows,
        "matrix": matrix,
        "orphan_calls": orphan[:50],
        "stats": stats,
        "entry_labels": ENTRY_LABELS,
        "hint": ("matrix.runtime_only > 0 表示有工具在被调用但没有任何声明入口 —— "
                 "这是运行时旁路（无白名单时不拦），属**需要公示的事实**，不是可忽略的噪声。"),
    }
