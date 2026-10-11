# -*- coding: utf-8 -*-
"""_probe_binding_audit.py —— Agent × Skill × 工具 × MCP 绑定关系全量盘点（只读）。

米爸要求（2026-11-11）：「整体盘点 agent 技能，工具的定义与关系，哪些关系需要绑定，
哪些不需要，给我个分析后的方案」。

本脚本只读生产库，输出：
  ① 四类实体的定义现状（各有多少、字段是否齐备、彼此的关联字段有没有值）
  ② 实际绑定矩阵（Agent→工具 / Skill→工具白名单 / 工具→MCP）
  ③ **按语义分级的绑定建议**（哪些该绑、哪些不该绑、依据是什么）
  ④ 三级展示（Agent→Skill→MCP/工具）的**数据可行性判定**：哪些层级有数据、
     哪些是空层级（不能凭空造）

判据不臆造：每个建议都标"依据字段"或"依据实测计数"。
"""
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.config import DB_PATH  # noqa: E402


def ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


#: 语义分类 → (是否该绑图谱检索, 理由)
#: 依据：agents.kb_required（产品配置意图）+ agent_role（角色性质）
SEMANTIC = {
    "knowledge_qa": (True, "知识问答：答案应来自知识库/图谱，检索是核心能力"),
    "design": (True, "架构设计：需引用既有实体与需求，避免重复建模"),
    "impact": (True, "影响分析：必须遍历图谱关系算影响面"),
    "review": (True, "预评审：需比对模型与规范条文"),
    "report_generation": (True, "报告生成：需汇聚多源数据"),
    "architecture_skeleton": (True, "骨架生成：需对齐已有包/部件命名"),
    "view_expansion": (True, "视图展开：需读取已有元素以正确 expose"),
    "requirement_structuring": (True, "需求结构化：需复用既有需求避免重复"),
    "requirement_analysis": (True, "需求分析：需关联既有需求"),
    "trace_verification": (True, "追溯核验：本质就是查图谱关系"),
    "model_validation_repair": (True, "校验修复：需读模型现状"),
    "change_safety_gate": (True, "变更安全：删除/重构前必须查影响面"),
    "methodology_resolver": (True, "方法论解析：需读方法论知识"),
    "model_release": (True, "模型发布：需确认发布物完整性"),
    # ↓ 以下是**不该绑**的
    "chat": (False, "闲聊：用户问「你好」也去查图谱，纯浪费（本次实测发现它 kb_required=1 且绑了 graph_retrieve）"),
    "knowledge_qa_helper": (False, "辅助角色"),
}


def main():
    conn = ro()
    print("=" * 88)
    print("Agent × Skill × 工具 × MCP 绑定关系盘点（**只读，未改任何配置**）")
    print("=" * 88)

    # ── ① 四类实体现状 ─────────────────────────────────────────
    print("\n【① 四类实体现状】")
    n_agents = conn.execute("SELECT COUNT(*) n FROM agents WHERE status='active'").fetchone()["n"]
    n_tools = conn.execute("SELECT COUNT(*) n FROM tools WHERE status='active'").fetchone()["n"]
    n_skills = conn.execute("SELECT COUNT(*) n FROM skills WHERE status='active'").fetchone()["n"]
    n_mcp = conn.execute("SELECT COUNT(*) n FROM mcp_servers WHERE status='active'").fetchone()["n"]
    n_bind = conn.execute("SELECT COUNT(*) n FROM agent_tools WHERE enabled=1").fetchone()["n"]
    print("  Agent(active)=%dTool(active)=%d  Skill(active)=%d  MCP(active)=%d"
          % (n_agents, n_tools, n_skills, n_mcp))
    print("  Agent→工具 绑定数 = %d" % n_bind)

    print("\n  关联字段的值分布（决定三级链路能否成立）：")
    checks = [
        ("tools.mcp_server_id", "工具→MCP 归属",
         "SELECT COUNT(*) n FROM tools WHERE status='active' AND mcp_server_id IS NOT NULL"),
        ("skills.allowed_tools", "技能→工具白名单",
         "SELECT COUNT(*) n FROM skills WHERE status='active' AND allowed_tools IS NOT NULL AND allowed_tools!=''"),
        ("skills.tool_name", "技能→单工具直连",
         "SELECT COUNT(*) n FROM skills WHERE status='active' AND tool_name IS NOT NULL AND tool_name!=''"),
        ("skills.triggers", "技能→触发词（隐式挂载）",
         "SELECT COUNT(*) n FROM skills WHERE status='active' AND triggers IS NOT NULL AND triggers!=''"),
        ("agents.kb_required=1", "Agent→强制检索知识库",
         "SELECT COUNT(*) n FROM agents WHERE status='active' AND kb_required=1"),
    ]
    for col, desc, sql in checks:
        n = conn.execute(sql).fetchone()["n"]
        print("    %-24s %-28s %d 条" % (col, desc, n))
    has_as = conn.execute(
        "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='agent_skills'"
    ).fetchone()["n"]
    print("    %-24s %-28s %s" % ("agent_skills 表", "Agent→技能显式绑定",
                                "**不存在**（技能靠 triggers 隐式挂载）" if not has_as else "存在"))

    # ── ② 实际绑定矩阵 ─────────────────────────────────────────
    print("\n【② 实际绑定：Agent → 工具】")
    rows = conn.execute("""
        SELECT a.name an, a.kb_required kb, a.agent_role ar, at.tool_name tn
        FROM agent_tools at JOIN agents a ON a.id=at.agent_id
        WHERE at.enabled=1 ORDER BY a.name, at.tool_name""").fetchall()
    by_agent = defaultdict(list)
    for r in rows:
        by_agent[r["an"]].append(r["tn"])
    graph_tools = ("graph_retrieve", "graph_db_query", "graph_db_nlquery", "graph_db_stats")
    for name in sorted(by_agent):
        a = conn.execute("SELECT kb_required, agent_role FROM agents WHERE name=? AND status='active'",
                         (name,)).fetchone()
        kb = a["kb_required"] if a else "?"
        has_g = [t for t in by_agent[name] if t in graph_tools]
        flag = "⚠️" if has_g and SEMANTIC.get(name, (None, ""))[0] is False else "  "
        print("  %s%-24s kb_required=%-3s 工具%2d 个%s"
              % (flag, name[:22], kb, len(by_agent[name]),
                 ("  图谱:" + ",".join(has_g)) if has_g else ""))

    print("\n【②-b 实际绑定：Skill → 工具白名单（节选前 12）】")
    srows = conn.execute("""
        SELECT name, allowed_tools, triggers FROM skills
        WHERE status='active' AND allowed_tools IS NOT NULL AND allowed_tools!=''
        ORDER BY name LIMIT 12""").fetchall()
    for r in srows:
        at = (r["allowed_tools"] or "")[:70]
        print("  %-26s → %s" % (r["name"][:24], at))
    if len(srows) < conn.execute(
            "SELECT COUNT(*) n FROM skills WHERE status='active' "
            "AND allowed_tools IS NOT NULL AND allowed_tools!=''").fetchone()["n"]:
        print("  …（其余略）")

    # ── ③ 绑定建议（按语义分级）────────────────────────────────
    print("\n【③ 绑定建议矩阵：按语义分级】")
    print("  %-26s %-9s %-7s %-7s %s" % ("Agent", "现状", "建议", "kb_required", "依据/理由"))
    print("  " + "-" * 84)
    agents = conn.execute(
        "SELECT name, kb_required, agent_role FROM agents WHERE status='active' ORDER BY name").fetchall()
    changes = {"解绑图谱": [], "改kb_required": [], "保持": []}
    for a in agents:
        name = a["name"]
        bound_graph = [t for t in by_agent.get(name, []) if t in graph_tools]
        should, why = SEMANTIC.get(name, (None, "未分类"))
        cur = "绑了" if bound_graph else "—"
        if should is None:
            sug = "待定"
        elif should and not bound_graph:
            sug = "建议绑"
        elif should and bound_graph:
            sug = "正确✓"
        else:
            sug = "★解绑"
            changes["解绑图谱"].append((name, bound_graph, why))
        # kb_required 建议
        if should is False and a["kb_required"]:
            changes["改kb_required"].append(name)
        print("  %-26s %-9s %-7s %-9s %s"
              % (name[:24], cur, sug, a["kb_required"], why[:38] if why else ""))

    # ── ④ 三级展示可行性 ───────────────────────────────────────
    print("\n【④ 三级展示（Agent → Skill → MCP/工具）数据可行性】")
    n_mcp_bound = conn.execute(
        "SELECT COUNT(*) n FROM tools WHERE status='active' AND mcp_server_id IS NOT NULL").fetchone()["n"]
    print("  L1 Agent 层：%d 个 active Agent ⇒ ✅ 有数据" % n_agents)
    print("  L2 Skill 层：%d 个 active Skill，但**无 agent_skills 显式绑定表**" % n_skills)
    print("               ⇒ ⚠️ 只能按「本轮是否命中该 skill」动态归组（运行时判定，非静态配置）")
    print("  L3 MCP/工具层：%d 个 active 工具，其中 **%d 个挂 MCP**"
          % (n_tools, n_mcp_bound))
    if n_mcp_bound == 0:
        print("               ⇒ ★ **MCP 层当前是空的**：没有任何工具归属 MCP server")
        print("               ⇒ 三级展示里第三级只能显示「本地工具」，不能凭空标 MCP")
    print("\n  结论：**结构支持三级，但 L3 的 MCP 维度当前无数据**。")
    print("       建议实现为「Agent → Skill → 工具」三层，L3 标题按")
    print("       `工具名（本地）` 或 `工具名（MCP:服务器名）` 条件渲染，")
    print("       这样将来 MCP 工具接入时**无需改协议**。")

    # ── 变更汇总 ───────────────────────────────────────────────
    print("\n【⑤ 待变更汇总（本次只盘点，未执行）】")
    print("  A. ★解绑图谱工具 %d 个 Agent：" % len(changes["解绑图谱"]))
    for n, ts, why in changes["解绑图谱"]:
        print("     %-24s 解绑 %s  ← %s" % (n[:22], ",".join(ts), why))
    print("  B. ★kb_required 建议置 0 的 %d 个：%s"
          % (len(changes["改kb_required"]), "、".join(changes["改kb_required"]) or "无"))
    print("  C. 前端层级展示：需在 tool/skill 事件补 agent 与 skill 字段（协议改造）")
    print("  D. stage「知识库检索」改为仅在should_retrieve 为真时 yield（消除幽灵环节）")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()