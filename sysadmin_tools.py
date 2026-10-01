"""系统管理域 Agent 工具（2026-08-31 P0-4）：只读查询工具。

对话式系统管理（Conversational System Management）——默认只读、安全优先：
- sys_query_users    用户列表（按角色/部门/状态过滤）
- sys_query_roles    角色与权限概览
- sys_query_perms    权限域与角色授权
- sys_query_audit    审计日志（最近操作轨迹）
- sys_query_monitor  系统运行统计（本体/实体/文档/会话/工具调用）
- sys_query_convs    会话统计

安全模型：全部只读（SELECT），写操作由后续 P1 授权流程（双重确认 + RBAC）承载。
返回 markdown 表格文本，由 LLM 转述或直接呈现。
"""
import sqlite3
from database import db_conn


def _conn():
    return db_conn()


def _table(rows, headers, keys=None):
    """渲染 markdown 表格。rows: dict 列表；headers: 中文表头；keys: 与 headers 等长的字段名（None=用表头作 key）。"""
    if not rows:
        return "（无数据）"
    ks = keys or headers
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        cells = []
        for i, h in enumerate(headers):
            v = r.get(ks[i] if i < len(ks) else h, "") if isinstance(r, dict) else ""
            cells.append(str(v)[:60].replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def q_users(args):
    """查询用户列表：支持 role / status / keyword 过滤。"""
    role = (args or {}).get("role") or ""
    status = (args or {}).get("status") or ""
    kw = (args or {}).get("keyword") or ""
    sql = ("SELECT u.username, u.display_name, u.department, r.name AS role, u.status "
           "FROM users u LEFT JOIN roles r ON r.id=u.role_id WHERE 1=1")
    params = []
    if role:
        sql += " AND r.name=?"; params.append(role)
    if status:
        sql += " AND u.status=?"; params.append(status)
    if kw:
        sql += " AND (u.username LIKE ? OR u.display_name LIKE ?)"; params += [f"%{kw}%", f"%{kw}%"]
    sql += " ORDER BY u.id LIMIT 50"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    return _table(rows, ["用户名", "显示名", "部门", "角色", "状态"],
                  ["username", "display_name", "department", "role", "status"])


def q_roles(args):
    """查询角色列表与权限规模。"""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT r.name, r.type, r.description, "
            "(SELECT COUNT(*) FROM users u WHERE u.role_id=r.id) AS member_cnt, "
            "(CASE WHEN r.permissions IS NULL THEN 0 ELSE json_array_length(r.permissions) END) AS perm_cnt "
            "FROM roles r ORDER BY r.id LIMIT 50").fetchall()]
    return _table(rows, ["角色", "类型", "描述", "成员数", "权限数"],
                  ["name", "type", "description", "member_cnt", "perm_cnt"])


def q_perms(args):
    """查询权限域与角色授权（只读概览）。"""
    role = (args or {}).get("role") or ""
    with _conn() as c:
        if role:
            rows = [dict(r) for r in c.execute(
                "SELECT r.name AS role, r.permissions FROM roles r WHERE r.name=?", (role,)).fetchall()]
            out = []
            for r in rows:
                try:
                    perms = __import__("json").loads(r.get("permissions") or "[]")
                except Exception:
                    perms = []
                for p in perms:
                    out.append({"role": r["role"], "permission": str(p)})
            return _table(out[:60], ["角色", "权限"]) if out else f"角色「{role}」无显式权限记录"
        # 全部角色概览
        rows = [dict(r) for r in c.execute("SELECT name, permissions FROM roles LIMIT 50").fetchall()]
        return _table([{"角色": r["name"], "权限数": str(len(__import__("json").loads(r.get("permissions") or "[]")))} for r in rows], ["角色", "权限数"])


def q_audit(args):
    """查询审计日志（最近操作轨迹）。"""
    limit = min(int((args or {}).get("limit") or 20), 50)
    actor = (args or {}).get("actor") or ""
    with _conn() as c:
        cols = [d[1] for d in c.execute("PRAGMA table_info(audit_logs)").fetchall()]
        pick = [x for x in ("actor", "action", "entity", "detail", "created_at") if x in cols]
        sql = f"SELECT {', '.join(pick)} FROM audit_logs"
        params = []
        if actor:
            sql += " WHERE actor LIKE ?"; params.append(f"%{actor}%")
        sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    headers = {"actor": "操作人", "action": "动作", "entity": "对象", "detail": "详情", "created_at": "时间"}
    return _table(rows, [headers.get(p, p) for p in pick], pick)


def q_monitor(args):
    """系统运行统计：本体/实体/文档/会话/工具调用。"""
    with _conn() as c:
        stats = {}
        for t in ("ontology_types", "entities", "relations", "documents", "conversations", "chunks", "integration_triples"):
            if [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()].__contains__(t):
                stats[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        try:
            stats["tool_calls"] = c.execute("SELECT COUNT(*) FROM tool_call_logs").fetchone()[0]
        except Exception:
            stats["tool_calls"] = 0
        try:
            stats["glossary"] = c.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
        except Exception:
            stats["glossary"] = 0
    label = {"ontology_types": "本体类型", "entities": "知识实体", "relations": "关系", "documents": "文档",
             "conversations": "会话", "chunks": "知识分块", "integration_triples": "集成三元组",
             "tool_calls": "工具调用", "glossary": "术语词条"}
    return _table([{"指标": label.get(k, k), "数量": str(v)} for k, v in stats.items()], ["指标", "数量"])


def q_convs(args):
    """会话统计（最近会话列表）。"""
    limit = min(int((args or {}).get("limit") or 15), 50)
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, title, intent, status, created_at FROM conversations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    return _table(rows, ["ID", "标题", "意图", "状态", "创建时间"],
                  ["id", "title", "intent", "status", "created_at"])


TOOLS = {
    "sys_query_users": {"desc": "查询用户列表（支持 role/status/keyword 过滤），只读", "fn": q_users},
    "sys_query_roles": {"desc": "查询角色列表与成员数、权限数", "fn": q_roles},
    "sys_query_perms": {"desc": "查询权限域与角色授权（传 role 查单个角色）", "fn": q_perms},
    "sys_query_audit": {"desc": "查询审计日志（最近操作轨迹，支持 actor 过滤）", "fn": q_audit},
    "sys_query_monitor": {"desc": "系统运行统计（本体/实体/文档/会话/工具调用数量）", "fn": q_monitor},
    "sys_query_convs": {"desc": "会话统计与最近会话列表", "fn": q_convs},
}


def exec_tool(name: str, arguments: dict) -> dict:
    """Agent 工具执行入口（只读）。"""
    t = TOOLS.get(name)
    if not t:
        return {"ok": False, "result": f"未知系统管理工具：{name}"}
    try:
        text = t["fn"](arguments or {})
        return {"ok": True, "result": text, "readonly": True}
    except Exception as e:
        return {"ok": False, "result": f"查询失败：{e}"}


def seed(conn: sqlite3.Connection):
    """幂等注册：tools 表 + agents 表 + agent_tools 绑定；意图关键词由 agents.intent_keywords 承载（intent.py 动态读 db）。"""
    descs = {k: v["desc"] for k, v in TOOLS.items()}
    for name, desc in descs.items():
        conn.execute(
            "INSERT OR IGNORE INTO tools (name, source, description, status, kind, side_effect, risk_level, builtin, scope, origin) "
            "VALUES (?, 'builtin', ?, 'active', 'builtin', 'read', 'low', 1, 'global', 'system-mgmt')",
            (name, desc))
    # system_mgmt Agent（只读查询，L0 无写能力）
    conn.execute(
        "INSERT OR IGNORE INTO agents (name, display_name, description, system_prompt, hil_level, kb_required, intent_keywords, icon, status, builtin, agent_role) "
        "VALUES ('system_mgmt', '系统管理Agent', '查询用户/角色/权限/审计/运行监控/会话（只读，不执行任何写操作）', "
        "'你是系统管理助手，职责是查询与回答系统数据问题（用户/角色/权限/审计/运行监控/会话统计）。\\n规则：只读查询，禁止任何写操作；数据以表格呈现；信息不足时追问。', "
        "'L0', 0, '[\"用户\", \"角色\", \"权限\", \"审计\", \"监控\", \"账号\", \"会话统计\"]', '🛡', 'active', 1, 'sub')")
    # 绑定工具到 Agent
    ag = conn.execute("SELECT id FROM agents WHERE name='system_mgmt'").fetchone()
    if ag:
        for name in descs:
            conn.execute(
                "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                "SELECT ?, 'tool', ?, 1 WHERE NOT EXISTS (SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_name=?)",
                (ag["id"], name, ag["id"], name))
    conn.commit()
