"""能力域词汇表种子（P2-3，2026-08-31 审计落地）：Skill/Tool/MCP/Plugin/Agent 术语归一。"""
import sqlite3

terms = [
    ("技能", "Skill", 2, "方法论包：skill.md 指令 + scripts + 工具白名单；Agent 按 triggers 触发注入"),
    ("Skill", "Skill", 2, "方法论包：skill.md 指令 + scripts + 工具白名单；Agent 按 triggers 触发注入"),
    ("工具", "Tool", 2, "原子能力：file_*/graph_*/sys_query_* 等，带 side_effect(读/写/破坏) 分级"),
    ("Tool", "Tool", 2, "原子能力：file_*/graph_*/sys_query_* 等，带 side_effect(读/写/破坏) 分级"),
    ("MCP", "MCP", 2, "外部连接：server.json 描述 base_url/transport/tools，streamable_http JSON-RPC"),
    ("MCP 服务器", "MCP", 2, "外部连接：server.json 描述 base_url/transport/tools，streamable_http JSON-RPC"),
    ("插件", "Plugin", 2, "市场分发单元：Plugin = Skill/MCP 统一抽象（type=skill|mcp|bundle），版本化+权限+审计"),
    ("Plugin", "Plugin", 2, "市场分发单元：Plugin = Skill/MCP 统一抽象（type=skill|mcp|bundle），版本化+权限+审计"),
    ("Agent", "Agent", 2, "编排者：绑定 Tool/Skill/MCP 三类能力，HIL L0-L2 分级，主/子团队两级"),
    ("能力中心", "能力中心", 2, "原「AI 设计工坊」：Agent / 技能 / 工具与 MCP / 插件市场 统一管理入口"),
    ("AI 设计工坊", "能力中心", 2, "原「AI 设计工坊」：Agent / 技能 / 工具与 MCP / 插件市场 统一管理入口"),
    ("插件库", "能力中心", 2, "旧导航名：现并入「能力中心」（Agent/技能/工具与MCP/插件市场）"),
    ("插件市场", "插件市场", 2, "公共市场（plugins 表 scope=public 单一事实源），支持审核/安装/置顶/审计"),
    ("技能包", "Skill 包", 2, "ZIP 技能包：skill.md + scripts/ + references/，白名单解析不自动执行"),
    ("草稿箱", "技能草稿箱", 2, "AI 自动沉淀(ai_deposit)/自改进(ai_improve) 技能草稿的审核入口"),
]

conn = sqlite3.connect("mbse.db")
cur = conn.executemany(
    "INSERT OR IGNORE INTO glossary (user_term, canonical_term, domain, intent, boost, description, kind, active, provenance) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    [(t[0], t[1], "能力中心", "能力导航", t[2], t[3], "sys", 1, "seed-capability-terms") for t in terms])
conn.commit()
print("能力域词条插入:", cur.rowcount, "条")
rows = conn.execute("SELECT user_term, canonical_term FROM glossary WHERE domain='能力中心'").fetchall()
print("现有能力域词条:", len(rows))
conn.close()
