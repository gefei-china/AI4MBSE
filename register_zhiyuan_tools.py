"""智源 SysMLv2 AI 工具注册脚本（幂等，可重复执行）。

- 将 zhiyuan_client.TOOL_DEFS 中 5 个工具注册进 tools 表（source='zhiyuan'）
- 绑定到 Agent：
  * design（方案设计）：全量 5 个（生成源码/语法检测/覆盖导入/工程列表/包结构树）
  * review（预评审）：zhiyuan_sysmlv2_check + zhiyuan_project_tree（评审前校验语法、查结构）
- 与 database.init_db 的种子逻辑兼容（INSERT OR IGNORE，重复执行不产生重复行）

用法：python register_zhiyuan_tools.py
"""
import json
import sqlite3

from core.config import DB_PATH
from zhiyuan_client import TOOL_DEFS

# Agent 绑定映射：agent name → 智源工具名列表
# 2026-09-11 起 zhiyuan 工具职责收敛到专用主 Agent zhiyuan_mgmt；design/review 不再绑定智源工具。
# 2026-09-17 C2 决策：原 register_zhiyuan_agent.py（定义 zhiyuan_mgmt 的一次性脚本）已删除——
#   它从未接入 seed_all，且其效果早已落库（agents id=275 / 5 个工具绑定齐全）。
#   该 Agent 的权威定义已留档：docs/_archive/zhiyuan_mgmt-agent-snapshot-20260917.json。
#   工具侧由本脚本（经 seed_registry.seed_all 编排）负责注册与绑定，幂等可重复执行。
BINDINGS = {
    "zhiyuan_mgmt": [t["name"] for t in TOOL_DEFS],
}


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # 1) 注册工具（幂等）
        inserted = 0
        for t in TOOL_DEFS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tools "
                "(name, source, description, status, input_schema, version, side_effect, risk_level) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (t["name"], "zhiyuan", t["description"], "active",
                 json.dumps(t["input_schema"], ensure_ascii=False),
                 t["version"], t["side_effect"], t["risk_level"]),
            )
            inserted += cur.rowcount
        conn.commit()
        print(f"[register] tools 新增 {inserted} 条（其余已存在）")

        # 2) 绑定 Agent（幂等；Agent 不存在时跳过并提示）
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

        # 3) 回显结果
        print("\n=== tools 表（source='zhiyuan'）===")
        for r in conn.execute(
            "SELECT id, name, source, status, side_effect, version FROM tools WHERE source='zhiyuan'"
        ).fetchall():
            print(f"  #{r['id']} {r['name']} [{r['status']}/{r['side_effect']}/{r['version']}]")
        print("\n=== agent_tools 绑定 ===")
        for r in conn.execute(
            "SELECT a.name AS agent, at.tool_name FROM agent_tools at "
            "JOIN agents a ON a.id=at.agent_id "
            "WHERE at.tool_type='tool' AND at.tool_name LIKE 'zhiyuan_%' ORDER BY a.name"
        ).fetchall():
            print(f"  {r['agent']} → {r['tool_name']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
