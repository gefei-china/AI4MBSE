# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：Agent CRUD/测试/运行/工具绑定。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


@router.post("/api/studio/agents")
def create_agent(body: AgentIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = AgentRepo(conn)
    if repo.get_agent_by_name(body.name):
        return JSONResponse({"error": f"Agent 名称已存在: {body.name}"}, 400)
    data = body.model_dump()
    sanitized = _sanitize_agent_meta(data)
    aid = repo.create_agent(data)
    audit(audit_user(user), "agent_create", f"创建Agent: {body.display_name} ({body.name})", conn=conn)
    # Task 12：能力字段/描述实际发生清洗时写元数据审计
    if sanitized:
        audit(audit_user(user), "agent_meta_sanitize", f"Agent {body.name} 能力/描述已清洗", conn=conn)
    return {"ok": True, "id": aid}


@router.get("/api/studio/agents/sub-candidates")
def list_sub_candidates(exclude: int = 0, conn=Depends(db_session)):
    """主 Agent 团队候选：agent_role='sub' 且启用的 Agent（exclude=当前编辑的 Agent id）。"""
    return AgentRepo(conn).sub_candidates(exclude)


@router.get("/api/studio/agents/{aid}/team")
def get_agent_team(aid: int, conn=Depends(db_session)):
    """主 Agent 团队成员列表（编辑回填用）。"""
    repo = AgentRepo(conn)
    a = repo.get_agent(aid)
    if not a:
        return JSONResponse({"error": "Agent not found"}, 404)
    return {"agent_id": aid, "agent_role": a.get("agent_role", "sub"),
            "team_members": repo.list_team_members(aid)}


@router.put("/api/studio/agents/{aid}/team")
def update_agent_team(aid: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """保存主 Agent 团队成员（全量覆盖）。

    规则：① 仅主 Agent（agent_role='main'）可设置成员；② 成员必须是子 Agent
    （不可把主 Agent 加为成员）；③ 不可自加；④ 成员必须存在且启用。
    """
    repo = AgentRepo(conn)
    a = repo.get_agent(aid)
    if not a:
        return JSONResponse({"error": "Agent not found"}, 404)
    if a.get("agent_role") != "main":
        return JSONResponse({"error": "子 Agent 不支持添加子 Agent（仅主 Agent 可设置团队成员）"}, 400)
    sub_ids = [int(x) for x in (body or {}).get("sub_agent_ids", [])]
    if len(set(sub_ids)) != len(sub_ids):
        return JSONResponse({"error": "团队成员存在重复"}, 400)
    for sid in sub_ids:
        if sid == aid:
            return JSONResponse({"error": "不能把自己添加为团队成员"}, 400)
        member = repo.get_agent(sid)
        if not member:
            return JSONResponse({"error": f"团队成员 #{sid} 不存在"}, 400)
        if member.get("status") != "active":
            return JSONResponse({"error": f"团队成员「{member.get('display_name')}」已停用"}, 400)
        if member.get("agent_role") != "sub":
            return JSONResponse({"error": f"「{member.get('display_name')}」是主 Agent，不能作为子 Agent 添加"}, 400)
    repo.set_team(aid, sub_ids)
    audit(audit_user(user), "agent_team_update", f"Agent#{aid} 设置团队成员 {len(sub_ids)} 个", conn=conn)
    return {"ok": True, "count": len(sub_ids)}


@router.get("/api/studio/agents/{aid}")
def get_agent(aid: int, conn=Depends(db_session)):
    a = AgentRepo(conn).get_agent(aid)
    if not a:
        return JSONResponse({"error": "Agent not found"}, 404)
    return a


def _rename_warnings(old_row: dict, new_name: str, new_disp: str) -> list:
    """改名影像提示：哪些东西**不会**被本次级联覆盖（代码级硬编码/内置身份）。

    数据库侧的软外键（agent_tasks / agent_memory / tool_call_logs）由
    `AgentRepo.sync_agent_name_refs` 同步掉了，这里只提示它**同步不到**的部分 ——
    不提示的话，用户以为改完就万事大吉，实际意图路由已经悄悄失配。
    """
    old_name = (old_row or {}).get("name") or ""
    if old_name == new_name:
        return []
    warns = []
    try:
        from agent.code_level_names import code_level_agent_names
        if old_name in code_level_agent_names():
            warns.append(
                f"「{old_name}」被 Python 代码硬引用（意图路由表 / 内置编排回退池），"
                f"这部分**无法自动同步** —— 改名后相关意图可能回落默认编排，请一并更新 agent/intent.py、agent/registry.py")
    except Exception:      # noqa: BLE001  提示失败不阻断改名
        pass
    if (old_row or {}).get("builtin"):
        warns.append("内置 Agent 改名风险较高：除上述代码清单外，一次性注册脚本（tools/register_*.py 的 BINDINGS）也可能按名字绑定工具")
    return warns


@router.put("/api/studio/agents/{aid}")
def update_agent(aid: int, body: AgentIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = AgentRepo(conn)
    old = repo.get_agent(aid)
    if not old:
        return JSONResponse({"error": "Agent not found"}, 404)
    data = body.model_dump()
    sanitized = _sanitize_agent_meta(data)

    # ── 名称口径统一（2026-09-30 用户反馈 2）：name 可编辑 → 改名须联动历史软外键 ──
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return JSONResponse({"error": "标识名（name）不能为空"}, 400)
    old_name = (old.get("name") or "").strip()
    if new_name != old_name:
        clash = repo.one("SELECT id, display_name FROM agents WHERE name=? AND id<>?", (new_name, aid))
        if clash:
            return JSONResponse(
                {"error": f"标识名「{new_name}」已被 Agent#{clash['id']}（{clash.get('display_name') or ''}）占用"}, 409)
    new_disp = (data.get("display_name") or "").strip() or new_name
    data["name"], data["display_name"] = new_name, new_disp

    repo.update_agent(aid, data)
    # 级联：把历史表里当软外键存的旧名字改写为新名字（改名不同步 = 历史行全变孤儿）
    synced = repo.sync_agent_name_refs(old, new_name, new_disp)
    renamed = bool(new_name != old_name or new_disp != (old.get("display_name") or "").strip())
    if renamed:
        audit(audit_user(user), "agent_rename",
              f"Agent#{aid} 改名 {old_name or old.get('display_name')}→{new_name}"
              f"（已同步历史引用 {sum(synced.values())} 行：{synced}）", conn=conn)
    audit(audit_user(user), "agent_update", f"更新Agent#{aid}: {body.display_name}", conn=conn)
    # Task 12：能力字段/描述实际发生清洗时写元数据审计
    if sanitized:
        audit(audit_user(user), "agent_meta_sanitize", f"Agent {new_name} 能力/描述已清洗", conn=conn)
    return {"ok": True, "renamed": renamed, "synced": synced,
            "synced_total": sum(synced.values()),
            "impact": repo.count_name_refs(new_name),
            "warnings": _rename_warnings(old, new_name, new_disp)}


@router.delete("/api/studio/agents/{aid}")
def delete_agent(aid: int, conn=Depends(db_session), user=Depends(current_user)):
    repo = AgentRepo(conn)
    a = repo.get_agent(aid)
    if not a:
        return JSONResponse({"error": "Agent not found"}, 404)
    if a.get("builtin"):
        return JSONResponse({"error": "内置 Agent 禁止删除，可「编辑」修改配置"}, 400)
    repo.delete_agent(aid)
    audit(audit_user(user), "agent_delete", f"删除Agent#{aid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/agents/{aid}/enable")
def enable_agent(aid: int, conn=Depends(db_session), user=Depends(current_user)):
    """启用 Agent（恢复参与意图路由，状态 active）。"""
    repo = AgentRepo(conn)
    if not repo.get_agent(aid):
        return JSONResponse({"error": "Agent not found"}, 404)
    repo.execute("UPDATE agents SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?", (aid,))
    repo._sync("agents", aid)      # 2026-09-16 同步桥：旧表状态变化 → 同步 plugins
    audit(audit_user(user), "agent_enable", f"启用Agent#{aid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/agents/{aid}/disable")
def disable_agent(aid: int, conn=Depends(db_session), user=Depends(current_user)):
    """停用 Agent（不再参与意图路由，状态 disabled）。"""
    repo = AgentRepo(conn)
    if not repo.get_agent(aid):
        return JSONResponse({"error": "Agent not found"}, 404)
    repo.execute("UPDATE agents SET status='disabled', updated_at=CURRENT_TIMESTAMP WHERE id=?", (aid,))
    repo._sync("agents", aid)      # 2026-09-16 同步桥
    audit(audit_user(user), "agent_disable", f"停用Agent#{aid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/agents/{aid}/tools")
def add_agent_tool(aid: int, body: AgentToolIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = AgentRepo(conn)
    if not repo.get_agent(aid):
        return JSONResponse({"error": "Agent not found"}, 404)
    # 2026-09-30（用户第 10 轮）：移除 "plugin" —— Agent 绑定能力收敛为 技能/MCP/工具 三类。
    if body.tool_type not in ("skill", "mcp", "tool"):
        return JSONResponse({"error": "tool_type 必须是 skill|mcp|tool"}, 400)
    # ── P0-2（2026-09-30）：绑定前校验「绑的东西真的存在且可用」 ──
    #   此前这里什么都不查：绑不存在的名字、绑已停用的工具、绑非法名（中文名）全都 200 成功，
    #   前台照单全收、后台静默丢弃 —— 真库里攒下的死绑定就是这么来的。
    #   校验口径取**运行时两条消费路径的交集**：
    #     ① builtin 工具经 registry 的 agent_def.tools 注入（pipeline_parts/tools.py:66-69）
    #     ② 非 builtin 经 tools.py:76 的 SQL 注入（要求 status='active'）
    #   两条都要求「名字在 tools 表里」，且②硬要求 active；builtin 同样按「停用即停用」处理，不再例外。
    _nm = (body.tool_name or "").strip()
    _nerr = check_tool_name(_nm)
    if _nerr:
        # 非法名（如中文名）绑上去等于白绑：运行时会被剔除且不报错，UI 却显示已绑定
        return JSONResponse({"error": _nerr}, 400)
    if body.tool_type == "tool":
        _row = conn.execute("SELECT name, status FROM tools WHERE name=?", (_nm,)).fetchone()
        if not _row:
            return JSONResponse({"error": f"工具「{_nm}」不在工具注册表里，请先在「能力中心 · 工具与MCP」注册"}, 400)
        if (_row["status"] or "") != "active":
            return JSONResponse({"error": f"工具「{_nm}」当前为「{_row['status']}」状态，启用后才能绑定"}, 409)
    tid = repo.add_tool(aid, body.tool_type, body.tool_name)
    audit(audit_user(user), "agent_tool_bind", f"Agent#{aid} 绑定 {body.tool_type}:{body.tool_name}", conn=conn)
    return {"ok": True, "id": tid}


@router.delete("/api/studio/agents/{aid}/tools/{tid}")
def remove_agent_tool(aid: int, tid: int, conn=Depends(db_session), user=Depends(current_user)):
    AgentRepo(conn).remove_tool(aid, tid)
    audit(audit_user(user), "agent_tool_unbind", f"Agent#{aid} 解绑工具#{tid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/agents/{aid}/test")
def test_agent(aid: int, body: dict | None = None, conn=Depends(db_session)):
    """Agent 试运行（P1 完整版）：给定 query → 模拟意图识别 + 绑定解析 + Skill 触发检测 + prompt 预览（不调 LLM）。"""
    repo = AgentRepo(conn)
    a = repo.get_agent(aid)
    if not a:
        return JSONResponse({"error": "Agent not found"}, 404)
    query = (body or {}).get("query", "")
    bound = repo.bound_tools_for(aid)
    tools_resolved = [{"type": t["type"], "name": t["name"]} for t in bound]

    # P1: 意图识别模拟（用全局 router + DB agents 关键词）
    from agent import AgentPipeline
    pipe = AgentPipeline()
    pipe._load_db_agents()
    routed_intent = pipe.router.detect(query) if query else a["name"]

    # P1: Skill 触发检测（命中 triggers → 注入完整指令）
    skill_hits, skill_lists = [], []
    for t in bound:
        if t.get("type") != "skill":
            continue
        trg = [str(x).lower() for x in (t.get("triggers") or [])]
        hit = bool(query) and any(x and x in query.lower() for x in trg)
        if hit:
            skill_hits.append(t["name"])
            # D4：命中技能追加渐进披露清单（references/examples/scripts）与工具白名单，与 agent._build_skill_prompt 对齐
            _prog = ""
            _rr = [str(x) if isinstance(x, str) else str(x.get("title") or x.get("path") or x)
                   for x in (t.get("references") or [])]
            _ee = [str(x) if isinstance(x, str) else str(x.get("title") or x.get("path") or x)
                   for x in (t.get("examples") or [])]
            _ss = [str(x) for x in (t.get("scripts") or [])]
            if _rr:
                _prog += f"\n📄 参考文档（需要时按需读取）：{'；'.join(_rr[:5])}"
            if _ee:
                _prog += f"\n📝 示例（需要时按需读取）：{'；'.join(_ee[:5])}"
            if _ss:
                _prog += f"\n⚙ 脚本（需要时执行）：{'；'.join(_ss[:5])}"
            _at = [str(x) for x in (t.get("allowed_tools") or [])]
            if _at:
                _prog += f"\n🔒 工具白名单（仅可调用）：{', '.join(_at)}"
            skill_lists.append(f"【Skill 已触发：{t['name']}】\n{t.get('content','')[:200]}{_prog}")
        else:
            skill_lists.append(f"- {t['name']}：{t.get('desc','') or t.get('content','')[:60]}")

    # SP-O：预览用角色块（DB system_prompt 优先；空则内置角色化提示词）
    from agent import AgentRegistry as _AR
    _sp = (a.get("system_prompt") or "").strip()
    if not _sp:
        _b = _AR.DEFINITIONS.get(a["name"])
        _sp = _b.system_prompt if _b else "你是网络总体MBSE设计助手。"
    prompt_preview = (
        f"{_sp}\n"
        f"当前意图：{routed_intent}（Agent: {a.get('display_name')}，"
        f"HIL 人机协作级别：{a.get('hil_level','L0')}）。\n"
        f"可用工具：{', '.join(t['tool_name'] for t in a.get('tools',[])) or '无（纯问答直出）'}。\n"
        + ("\n绑定技能：\n" + "\n".join(skill_lists) + "\n" if skill_lists else "")
        + f"输出规范：正文一律用自然语言描述，不要展示任何需求/实体编号。"
    )
    return {
        "agent": a["display_name"],
        "routed_intent": routed_intent,
        "hil_level": a["hil_level"],
        "tools": tools_resolved,
        "tool_count": len(tools_resolved),
        "skill_hits": skill_hits,
        # D4：渐进披露清单在 prompt 后部，放宽截断确保 📄📝⚙🔒 在试运行预览可见
        "system_prompt_preview": prompt_preview[:2000],
        "hint": "绑定工具将按此列表注入 LLM 工具集；命中 triggers 的 Skill 注入完整指令",
    }


@router.post("/api/studio/agents/{aid}/run")
def run_agent(aid: int, body: dict | None = None, conn=Depends(db_session)):
    """优化3：Agent 真实运行（可观测验证）。给定 query → 完整执行管线（意图识别+检索+LLM+工具），
    返回运行结果（content/llm 元信息/工具调用/耗时），供验证 Agent 效果。不落会话消息。"""
    import time as _time
    from agent import AgentPipeline
    repo = AgentRepo(conn)
    a = repo.get_agent(aid)
    if not a:
        return JSONResponse({"error": "Agent not found"}, 404)
    query = (body or {}).get("query", "")
    if not query:
        return JSONResponse({"error": "query 必填"}, 400)

    pipe = AgentPipeline()
    pipe._load_db_agents()
    t0 = _time.time()
    # 复用 execute 管线（execute 内部会 detect 意图并路由；provider 用 Agent 指定模型）
    # dry_run=True：验证运行不落会话消息/审计，仅返回结果供效果验证
    effective_provider = a.get("model_provider_id") or None
    result = pipe.execute(query, 0, "dev", effective_provider, [], dry_run=True)
    latency = int((_time.time() - t0) * 1000)
    # 工具调用明细（本次运行期间落库的最新日志）
    logs = AgentRepo(conn).rows(
        "SELECT tool_name, tool_type, ok, latency_ms, result FROM tool_call_logs "
        "WHERE intent=? ORDER BY id DESC LIMIT 8", (a["name"],)
    )
    return {
        "agent": a["display_name"],
        "routed_intent": result.get("intent"),
        "hil_level": result.get("hil_level"),
        "content": result.get("content", ""),
        "llm": result.get("llm", {}),
        "tool_calls": logs,
        "retrieval": result.get("retrieval", {}),
        "total_latency_ms": latency,
    }


@router.get("/api/studio/tool-logs")
def list_tool_logs(agent: str = "", call_kind: str = "", tool_type: str = "",
                   ok: int = -1, limit: int = 20, page: int = 0, conn=Depends(db_session)):
    """优化1：工具调用可观测——最近调用日志（时间/Agent/工具/参数/结果/耗时/成败）。

    支持筛选：agent（按 Agent 展示名精确匹配）、call_kind（tool/skill）、
    tool_type（builtin/mcp/http/zhiyuan）、ok（1=成功 0=失败）。
    page>0 时返回 {items,total,page,page_size} 分页结构（与 v2g candidates 对齐），
    page=0 保持向后兼容返回纯列表（limit 截断）。
    """
    limit = max(1, min(limit, 100))
    where, args = [], []
    if agent:
        where.append("agent_name=?"); args.append(agent)
    if call_kind:
        where.append("call_kind=?"); args.append(call_kind)
    if tool_type:
        where.append("tool_type=?"); args.append(tool_type)
    if ok in (0, 1):
        where.append("ok=?"); args.append(ok)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    if page > 0:
        total = AgentRepo(conn).scalar("SELECT COUNT(*) FROM tool_call_logs" + cond, tuple(args)) or 0
        rows = AgentRepo(conn).rows(
            "SELECT * FROM tool_call_logs" + cond + " ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(args + [limit, (page - 1) * limit]))
        return {"items": rows, "total": total, "page": page, "page_size": limit}
    rows = AgentRepo(conn).rows(
        "SELECT * FROM tool_call_logs" + cond + " ORDER BY id DESC LIMIT ?",
        tuple(args + [limit]))
    return rows


@router.get("/api/studio/agent-tools")
def list_agent_tools(conn=Depends(db_session)):
    """工具注册表视图：内置工具 + MCP/Tool/Skill 可配置工具合并。"""
    return ToolRegistry(conn).list()


@router.get("/api/studio/mcp-tools/catalog")
def mcp_tools_catalog(conn=Depends(db_session)):
    """Layer 1 · Catalog：所有在线 MCP 工具名 + 一句话描述（模型用轻量 search_tools 检索）。"""
    return AgentRepo(conn).mcp_catalog()


@router.get("/api/studio/mcp-tools/{tool_name}/inspect")
def mcp_tools_inspect(tool_name: str, conn=Depends(db_session)):
    """Layer 2 · Inspect：单工具完整定义（按需加载，进 context 前先查，避免工具膨胀）。"""
    info = AgentRepo(conn).mcp_inspect(tool_name)
    if not info:
        return JSONResponse({"error": f"工具未找到: {tool_name}"}, 404)
    return info
