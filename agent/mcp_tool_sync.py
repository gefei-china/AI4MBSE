# -*- coding: utf-8 -*-
"""把 MCP 工具清单同步进 `tools` 表（2026-11-06）

## 为什么需要这一步

`mcp_health.discover_server()` 只把工具清单写进 `mcp_servers.tool_meta`
（name + description + inputSchema），**完全不碰 `tools` 表**。
而执行策略门控读的是 `tools.side_effect` ⇒ MCP 工具永远读不到 ⇒ 门控失效。
本模块补上这一跳。

## 三条不变量（每条都对应一次真实事故）

1. **人工覆写优先**：`side_effect_source='manual'` 的行，`side_effect` / `risk_level`
   一律不动。反面教材：`register_http_tools._upsert_tool` 用
   `ON CONFLICT DO UPDATE side_effect=excluded.side_effect` ⇒ 跑一次脚本就把人工标注冲掉。

2. **消失的工具不物理删除**：标 `status='deprecated'`。因为 `agent_tools` 绑定靠
   `name` 引用，删行会让绑定变悬空（平台上就会出现"绑了但查不到"的死条目）；
   而且工具回来时还能按原名恢复标注、人工覆写也不丢。

3. **必须带 `mcp_server_id` 归属**：不按裸 `name` 更新。`tools.name` 是 UNIQUE
   （架构债，接第二个智源环境前需改复合唯一），按 name 更新会把 A 环境的标注写到 B 环境。
"""
from __future__ import annotations

import json

from agent import tool_side_effect as _tse


def _cols(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except Exception:
        return set()


def sync_mcp_tools(conn, server_id: int, tool_meta, *, prune_missing: bool = True,
                   dry_run: bool = False) -> dict:
    """把一个 MCP 的工具清单同步进 `tools` 表。

    参数
        conn        : 可写 sqlite 连接（调用方负责事务）
        server_id   : `mcp_servers.id`
        tool_meta   : `tools/list` 返回的 `[{name, description, inputSchema}, ...]`
        prune_missing: 服务端已不存在的工具是否标 deprecated（默认 True）
        dry_run     : True 只计算不写库（供 UI 预览/门禁使用）

    返回
        {"inserted":n, "updated":n, "kept_manual":n, "deprecated":n,
         "rules":{...}, "counts":{...}, "rule_version":v}
    """
    derived = _tse.derive_all(tool_meta)
    stats = {"inserted": 0, "updated": 0, "kept_manual": 0,
             "deprecated": 0, "unchanged": 0,
             "rules": {}, "counts": {"read": 0, "write": 0, "destructive": 0},
             "rule_version": _tse.RULE_VERSION}
    if not derived:
        return stats

    cols = _cols(conn, "tools")
    if not cols:
        return stats
    # 低版本库（未跑列迁移）⇒ 直接放弃，不抛异常打断 discover
    if "side_effect_source" not in cols or "rule_version" not in cols:
        stats["skipped"] = "missing_columns"
        return stats

    by_name = {}
    for r in conn.execute(
            "SELECT id, name, side_effect, risk_level, status, "
            "COALESCE(side_effect_source,'auto') src, COALESCE(rule_version,0) rv "
            "FROM tools WHERE mcp_server_id=?", (server_id,)).fetchall():
        by_name[r["name"]] = dict(r)

    # 元数据里带 description（落库后模型才知道工具能做什么）
    meta_by_name = {}
    for m in (tool_meta or []):
        if isinstance(m, dict) and m.get("name"):
            meta_by_name[str(m["name"]).strip()] = m

    for d in derived:
        name = d["name"]
        eff, risk = d["side_effect"], d["risk_level"]
        stats["counts"][eff] = stats["counts"].get(eff, 0) + 1
        stats["rules"][d["rule"]] = stats["rules"].get(d["rule"], 0) + 1
        meta = meta_by_name.get(name) or {}
        desc = str(meta.get("description") or "")[:1000]
        schema = json.dumps(meta.get("inputSchema") or {}, ensure_ascii=False)

        cur = by_name.get(name)
        if cur is None:
            stats["inserted"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO tools (name, source, mcp_server_id, description, status, "
                    "input_schema, version, side_effect, risk_level, side_effect_source, rule_version) "
                    "VALUES (?, 'mcp', ?, ?, 'active', ?, 'v1.0', ?, ?, 'auto', ?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "mcp_server_id=excluded.mcp_server_id, description=excluded.description, "
                    "status='active', input_schema=excluded.input_schema, "
                    # ⚠️ 冲突时**不覆盖 manual**（见不变量 1）
                    "side_effect=CASE WHEN COALESCE(tools.side_effect_source,'auto')='manual' "
                    "  THEN tools.side_effect ELSE excluded.side_effect END, "
                    "risk_level=CASE WHEN COALESCE(tools.side_effect_source,'auto')='manual' "
                    "  THEN tools.risk_level ELSE excluded.risk_level END, "
                    "rule_version=CASE WHEN COALESCE(tools.side_effect_source,'auto')='manual' "
                    "  THEN tools.rule_version ELSE excluded.rule_version END",
                    (name, server_id, desc, schema, eff, risk, _tse.RULE_VERSION))
            continue

        is_manual = (cur["src"] == "manual")
        if is_manual:
            stats["kept_manual"] += 1
        else:
            stats["updated"] += 1
        if not dry_run:
            if is_manual:
                # 人工覆写：只刷新描述/schema/状态，side_effect 与 risk_level 原样不动
                conn.execute(
                    "UPDATE tools SET description=?, input_schema=?, status='active', "
                    "mcp_server_id=? WHERE id=?",
                    (desc, schema, server_id, cur["id"]))
            else:
                conn.execute(
                    "UPDATE tools SET description=?, input_schema=?, status='active', "
                    "mcp_server_id=?, side_effect=?, risk_level=?, "
                    "side_effect_source='auto', rule_version=? WHERE id=?",
                    (desc, schema, server_id, eff, risk, _tse.RULE_VERSION, cur["id"]))

    # 服务端已消失的工具 → deprecated（不物理删除，见不变量 2）
    if prune_missing:
        alive = {d["name"] for d in derived}
        gone = [n for n, v in by_name.items() if n not in alive and v["status"] == "active"]
        stats["deprecated"] = len(gone)
        if gone and not dry_run:
            conn.executemany("UPDATE tools SET status='deprecated' WHERE id=?",
                             [(by_name[n]["id"],) for n in gone])
    return stats


def list_mcp_tools(conn, server_id: int) -> list:
    """给 UI 用的工具清单：合并 `tools` 表的副作用标注与 `mcp_servers.tool_meta` 的描述。

    未同步过的工具（`tools` 表还没有行）也会列出来，`side_effect` 用派生值兜底
    ——这样"MCP 刚发现、还没同步"时界面不会显示成空清单。
    """
    rows = {}
    try:
        for r in conn.execute(
                "SELECT name, COALESCE(side_effect,'read') eff, "
                "COALESCE(risk_level,'low') risk, COALESCE(status,'active') st, "
                "COALESCE(side_effect_source,'auto') src, COALESCE(rule_version,0) rv, "
                "description FROM tools WHERE mcp_server_id=?", (server_id,)).fetchall():
            rows[r["name"]] = {"name": r["name"], "side_effect": r["eff"],
                               "risk_level": r["risk"], "status": r["st"],
                               "side_effect_source": r["src"], "rule_version": r["rv"],
                               "description": r["description"] or ""}
    except Exception:
        pass
    try:
        raw = conn.execute("SELECT tool_meta FROM mcp_servers WHERE id=?",
                           (server_id,)).fetchone()
        meta = json.loads((raw[0] if raw else "") or "[]")
    except Exception:
        meta = []
    out = []
    for m in meta:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        nm = str(m["name"]).strip()
        e = rows.get(nm)
        if e:
            e = dict(e)
            if not e.get("description"):
                e["description"] = m.get("description") or ""
        else:
            eff, risk, rule = _tse.derive(m)
            e = {"name": nm, "side_effect": eff, "risk_level": risk,
                 "status": "active", "side_effect_source": "auto",
                 "rule_version": 0, "description": m.get("description") or "",
                 "_derived_only": True, "_rule": rule}
        out.append(e)
    return out
