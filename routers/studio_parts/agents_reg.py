# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：Agent 注册表与团队。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/agent-registry")
def list_agent_registry(conn=Depends(db_session)):
    """Agent 注册表视图：意图 → Agent 定义（工具绑定 / HIL 分级 / 知识库依赖）。"""
    registry = AgentRegistry()
    agents = registry.list()
    # 附带每个 Agent 声明工具的解析状态（是否能在注册表消费）
    tr = ToolRegistry(conn)
    for a in agents:
        a["tools_resolved"] = [t for t in tr.available_for(a["tools"])]
        a["tools_missing"] = [t for t in a["tools"] if tr.get(t) is None]
    return agents


@router.get("/api/studio/agents")
def list_agents(conn=Depends(db_session)):
    return AgentRepo(conn).list_agents()


@router.get("/api/studio/agent-teams")
def list_agent_teams(conn=Depends(db_session)):
    """智能体团队列表（AI 会话页「工作流」下拉数据源）。

    团队按主 Agent（agent_role='main' 且启用）划分：创建几个主 Agent 即有几个团队；
    每个团队由主 Agent（负责人）+ 其子 Agent 成员组成。返回团队规模与成员摘要。
    """
    repo = AgentRepo(conn)
    teams = []
    for a in repo.list_agents():
        if a.get("agent_role") != "main" or a.get("status") != "active":
            continue
        members = repo.list_team_members(a["id"])
        teams.append({
            "id": a["id"],
            "name": a["name"],                # 主 Agent 意图名（团队模式 forced_intent 用）
            "display_name": a.get("display_name") or a["name"],
            "icon": a.get("icon", "🤖"),
            "description": a.get("description", ""),
            "team_count": len(members),
            "team_members": [{"id": m["id"], "name": m["name"],
                              "display_name": m["display_name"], "icon": m["icon"],
                              "status": m["status"]} for m in members],
        })
    return teams
