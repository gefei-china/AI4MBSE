# -*- coding: utf-8 -*-
"""图数据库工具注册脚本（幂等，可重复执行，2026-09-01 P2）。

- 将 graph_db_query / graph_db_stats 注册进 tools 表（source='graph_db'）
- 绑定到 Agent：knowledge_qa（知识问答）、design（方案设计）、impact（变更影响）、review（预评审）
- 执行路由：agent/pipeline.py::_exec_tool_call 按 graph_db_ 前缀分发到 graph_db_tools.exec_tool

用法：python register_graph_db_tools.py（或经 seed_registry.seed_all 幂等挂接）
"""
import json
import sqlite3

from core.config import DB_PATH

TOOL_DEFS = [
    {
        "name": "graph_db_query",
        "description": "设计知识库（图数据库）SPARQL 只读查询：按分支命名图检索实体/关系/属性，"
                       "如 SELECT ?s ?t WHERE { GRAPH ?g { ?s <urn:mbse:type> ?t } } LIMIT 5。"
                       "仅支持 SELECT（只读），返回结构化行；图谱无命中时建议回退知识库问答。",
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "SPARQL SELECT 查询语句（实体 URI 形如 urn:mbse:ent:名称:id）"},
                "limit": {"type": "integer", "description": "返回行数上限（默认 20，最大 20）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "graph_db_nlquery",
        "description": "图数据库自然语言问答：自动把自然语言问题翻译为 SPARQL SELECT 并执行"
                       "（实体预匹配 + LLM 翻译，只读）。用于图谱优先问答，如"
                       "「转发器有哪些关系」「哪些组件依赖电源分系统」。返回结构化行与生成语句。",
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言问题，如「转发器有哪些关系」"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "graph_db_stats",
        "description": "设计知识库（图数据库）规模统计：三元组总数、命名图（分支）列表、后端类型。"
                       "用于了解图谱覆盖度与分支数据分布。",
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.0",
        "input_schema": {"type": "object", "properties": {}},
    },
]

# Agent 绑定映射（幂等；agent 不存在时跳过）
BINDINGS = {
    "knowledge_qa": ["graph_db_query", "graph_db_nlquery", "graph_db_stats"],
    "design": ["graph_db_query", "graph_db_nlquery"],
    "impact": ["graph_db_query", "graph_db_nlquery"],
    "review": ["graph_db_query"],
}


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        inserted = 0
        for t in TOOL_DEFS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tools "
                "(name, source, description, status, input_schema, version, side_effect, risk_level) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (t["name"], "graph_db", t["description"], "active",
                 json.dumps(t["input_schema"], ensure_ascii=False),
                 t["version"], t["side_effect"], t["risk_level"]),
            )
            inserted += cur.rowcount
        conn.commit()
        print(f"[register] tools 新增 {inserted} 条（其余已存在）")

        bound = 0
        for agent_name, tool_names in BINDINGS.items():
            agent = conn.execute("SELECT id FROM agents WHERE name=?", (agent_name,)).fetchone()
            if not agent:
                print(f"[register] ⚠️ 跳过绑定：Agent '{agent_name}' 不存在")
                continue
            for tname in tool_names:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                    "VALUES (?, 'tool', ?, 1)",
                    (agent["id"], tname),
                )
                bound += cur.rowcount
        conn.commit()
        print(f"[register] agent_tools 新增绑定 {bound} 条（其余已存在）")

        print("\n=== tools 表（source='graph_db'）===")
        for r in conn.execute(
            "SELECT id, name, status, side_effect, version FROM tools WHERE source='graph_db'"
        ).fetchall():
            print(f"  #{r['id']} {r['name']} [{r['status']}/{r['side_effect']}/{r['version']}]")
    finally:
        conn.close()


def seed(conn: sqlite3.Connection) -> dict:
    """seed_registry 幂等挂接入口（接收外部连接）。"""
    inserted = 0
    for t in TOOL_DEFS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tools "
            "(name, source, description, status, input_schema, version, side_effect, risk_level) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (t["name"], "graph_db", t["description"], "active",
             json.dumps(t["input_schema"], ensure_ascii=False),
             t["version"], t["side_effect"], t["risk_level"]),
        )
        inserted += cur.rowcount
    for agent_name, tool_names in BINDINGS.items():
        agent = conn.execute("SELECT id FROM agents WHERE name=?", (agent_name,)).fetchone()
        if not agent:
            continue
        for tname in tool_names:
            conn.execute(
                "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                "VALUES (?, 'tool', ?, 1)",
                (agent["id"], tname),
            )
    return {"graph_db_tools": f"+{inserted}"}


if __name__ == "__main__":
    main()
