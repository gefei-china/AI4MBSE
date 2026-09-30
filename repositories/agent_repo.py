"""Agent 管理域 Repository：agents / agent_tools 表（P0 平台化）。

- agents 表：DB 驱动 Agent 注册表（替代 AgentRegistry 代码写死）
- agent_tools 表：Agent ↔ (skill/mcp/tool) 多对多绑定
"""
import json

from repositories.base import BaseRepo


class AgentRepo(BaseRepo):
    """Agent 注册表 + 工具绑定数据访问。"""

    # ── agents CRUD ──
    # ── legacy ↔ plugins 同步桥（2026-09-16）──
    def _sync(self, table: str, row_id) -> None:
        """旧表行 → plugins 同步（管理视图随之更新）。失败绝不阻断主流程。"""
        if not row_id:
            return
        try:
            from plugin_system import legacy_sync
            legacy_sync.sync_from_legacy_safe(self.conn, table, row_id)
        except Exception:
            pass

    def _attach_team(self, a: dict) -> dict:
        """附加团队信息：team_members（成员摘要）+ team_count；main 时有效，sub 返回空。"""
        a["team_members"] = self.list_team_members(a["id"])
        a["team_count"] = len(a["team_members"])
        return a

    def list_agents(self) -> list:
        agents = self.rows("""
            SELECT a.*, p.name AS provider_name, p.model_name AS provider_model,
                   (SELECT COUNT(*) FROM agent_tools t WHERE t.agent_id=a.id) AS tool_count
            FROM agents a LEFT JOIN llm_providers p ON a.model_provider_id=p.id
            ORDER BY a.id""")
        for a in agents:
            a["tools"] = self.list_agent_tools(a["id"])
            a["intent_keywords"] = json.loads(a.get("intent_keywords") or "[]")
            a["model_params"] = json.loads(a.get("model_params") or "{}")
            a["kb_scope"] = json.loads(a.get("kb_scope") or "{}") or {}
            # Task 14：能力元数据解析（capabilities 专长 / input/output schema / 并发 / 协议）
            a["capabilities"] = json.loads(a.get("capabilities") or "[]")
            a["input_schema"] = json.loads(a.get("input_schema") or "{}")
            a["output_schema"] = json.loads(a.get("output_schema") or "{}")
            # 主/子 Agent 团队：成员摘要 + 团队规模
            self._attach_team(a)
        return agents

    def get_agent(self, agent_id: int) -> dict | None:
        row = self.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not row:
            return None
        row["tools"] = self.list_agent_tools(agent_id)
        row["intent_keywords"] = json.loads(row.get("intent_keywords") or "[]")
        row["model_params"] = json.loads(row.get("model_params") or "{}")
        row["kb_scope"] = json.loads(row.get("kb_scope") or "{}") or {}
        # Task 14：能力元数据解析（与 list_agents 保持一致）
        row["capabilities"] = json.loads(row.get("capabilities") or "[]")
        row["input_schema"] = json.loads(row.get("input_schema") or "{}")
        row["output_schema"] = json.loads(row.get("output_schema") or "{}")
        # 主/子 Agent 团队：成员摘要 + 团队规模
        self._attach_team(row)
        return row

    def get_agent_by_name(self, name: str) -> dict | None:
        return self.one("SELECT * FROM agents WHERE name=?", (name,))

    def _warn_kb_scope_docs(self, kb_scope_json: str, where: str) -> None:
        """保存侧预防：kb_scope.docs 白名单若含**不存在的文档名** → 写 WARNING（不阻断保存）。

        为什么保存侧也要查：运行时虽有自愈（agent/rag.py::_resolve_scope_docs —— 剔除失效项、
        全失效则放宽），但白名单写错的**最早可发现点**就在此处；等到「检索恒为空」才发现，
        排查成本高得多（实测：design agent 白名单 2 条全不存在，4887 块规范长期不可见）。
        **不阻断保存**：允许「先配范围、后传文档」的正常工作流；文档名大小写/后缀写错由日志提示。
        ⚠️ 2026-09-21（G2）：预检口径必须与 `GraphRAG._resolve_scope_docs` 一致 ——
        已下线（`lifecycle_status='deprecated'`）文档在检索侧根本不可见，这里若算「存在」，
        用户会看到「保存通过、检索为空」且**没有任何告警**（自愈会把它判成有效项）。
        """
        try:
            sc = json.loads(kb_scope_json or "{}") or {}
            docs = [str(d).strip() for d in (sc.get("docs") or []) if str(d).strip()]
            if not docs:
                return
            miss = [d for d in dict.fromkeys(docs)
                    if not (self.one("SELECT 1 FROM documents WHERE filename=? "
                                     "AND (lifecycle_status IS NULL "
                                     "OR lifecycle_status NOT IN ('deprecated'))", (d,)))]
            if miss:
                print(f"[kb_scope][WARN] {where} 白名单含 {len(miss)} 篇不存在的文档：{miss[:5]}"
                      f" —— 该 Agent 检索时会被自愈剔除（全失效则放宽为不限文档），请核对文档名",
                      flush=True)
        except Exception:
            pass   # 观测失败不影响保存

    def create_agent(self, data: dict) -> int:
        self._warn_kb_scope_docs(json.dumps(data.get("kb_scope") or {}, ensure_ascii=False),
                                 f"create_agent(name={data.get('name')})")
        rid = self.execute(
            """INSERT INTO agents (name, display_name, description, system_prompt,
               model_provider_id, model_params, hil_level, kb_required, kb_scope, intent_keywords, icon, status, version,
               capabilities, input_schema, output_schema, max_concurrency, protocol_range, agent_role)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data["name"], data["display_name"], data.get("description", ""),
             data.get("system_prompt", ""), data.get("model_provider_id"),
             json.dumps(data.get("model_params", {}), ensure_ascii=False),
             data.get("hil_level", "L0"), 1 if data.get("kb_required") else 0,
             json.dumps(data.get("kb_scope") or {}, ensure_ascii=False),
             json.dumps(data.get("intent_keywords", []), ensure_ascii=False),
             data.get("icon", "🤖"), data.get("status", "active"), data.get("version", "v1.0.0"),
             json.dumps(data.get("capabilities", []), ensure_ascii=False),
             json.dumps(data.get("input_schema", {}) or {}, ensure_ascii=False),
             json.dumps(data.get("output_schema", {}) or {}, ensure_ascii=False),
             int(data.get("max_concurrency") or 2),
             data.get("protocol_range", ">=1,<3"),
             data.get("agent_role", "sub")),
        )
        self._sync("agents", rid)
        return rid


    def update_agent(self, agent_id: int, data: dict) -> None:
        # kb_scope 缺省/None 时保留既有值（防止未感知该字段的旧调用方清空范围配置）
        if data.get("kb_scope") is not None:
            kb_scope_json = json.dumps(data.get("kb_scope") or {}, ensure_ascii=False)
            # 保存侧预防：与 create_agent 对齐 —— **改范围的主路径其实在这里**（旧配置多由编辑产生）；
            # 仅在调用方显式写入 kb_scope 时告警，未感知该字段的字段级更新不产生噪音。
            self._warn_kb_scope_docs(kb_scope_json, f"update_agent(id={agent_id})")
        else:
            cur = self.one("SELECT kb_scope FROM agents WHERE id=?", (agent_id,))
            kb_scope_json = (cur or {}).get("kb_scope") or "{}"
        # Task 14：能力元数据列——显式传 None 时保留既有值（SQL COALESCE 条件更新），前端表单总带值则整体更新
        def _field_json(v, fallback):
            return json.dumps(v or fallback, ensure_ascii=False) if v is not None else None
        caps_json = _field_json(data.get("capabilities"), [])
        in_json = _field_json(data.get("input_schema"), {})
        out_json = _field_json(data.get("output_schema"), {})
        mc = data.get("max_concurrency")
        mc_val = int(mc) if mc is not None else None
        pr = data.get("protocol_range")
        pr_val = pr if pr is not None else None
        role = data.get("agent_role")
        role_val = role if role in ("main", "sub") else None  # 显式非法值忽略（保留既有）
        self.execute(
            """UPDATE agents SET name=?, display_name=?, description=?, system_prompt=?,
               model_provider_id=?, model_params=?, hil_level=?, kb_required=?, kb_scope=?, intent_keywords=?,
               icon=?, status=?, version=?, capabilities=COALESCE(?, capabilities),
               input_schema=COALESCE(?, input_schema), output_schema=COALESCE(?, output_schema),
               max_concurrency=COALESCE(?, max_concurrency), protocol_range=COALESCE(?, protocol_range),
               agent_role=COALESCE(?, agent_role),
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (data.get("name", ""), data.get("display_name", ""), data.get("description", ""),
             data.get("system_prompt", ""), data.get("model_provider_id"),
             json.dumps(data.get("model_params", {}), ensure_ascii=False),
             data.get("hil_level", "L0"), 1 if data.get("kb_required") else 0,
             kb_scope_json,
             json.dumps(data.get("intent_keywords", []), ensure_ascii=False),
             data.get("icon", "🤖"), data.get("status", "active"), data.get("version", "v1.0.0"),
             caps_json, in_json, out_json, mc_val, pr_val, role_val,
             agent_id),
        )
        self._sync("agents", agent_id)

    def delete_agent(self, agent_id: int) -> None:
        # agent_tools 有 ON DELETE CASCADE，显式删除双保险（兼容无外键开关的旧连接）
        self.execute("DELETE FROM agent_tools WHERE agent_id=?", (agent_id,))
        # 主/子团队关系双向清理（本 Agent 作主、作子均删除）
        self.execute("DELETE FROM agent_team_members WHERE main_agent_id=? OR sub_agent_id=?", (agent_id, agent_id))
        self.execute("DELETE FROM agents WHERE id=?", (agent_id,))

    # ── agent_tools 绑定 ──
        self._sync("agents", agent_id)
    def list_agent_tools(self, agent_id: int) -> list:
        return self.rows(
            "SELECT * FROM agent_tools WHERE agent_id=? ORDER BY tool_type, tool_name",
            (agent_id,),
        )

    def add_tool(self, agent_id: int, tool_type: str, tool_name: str, params: dict | None = None) -> int:
        return self.execute(
            "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, params) VALUES (?,?,?,?)",
            (agent_id, tool_type, tool_name, json.dumps(params or {}, ensure_ascii=False)),
        )

    def remove_tool(self, agent_id: int, tool_id: int) -> None:
        self.execute("DELETE FROM agent_tools WHERE id=? AND agent_id=?", (tool_id, agent_id))

    # ── 名称口径同步（2026-09-30 用户反馈 2：卡片名与编辑页不一致）──
    #
    # `agents.name`（意图标识）与 `agents.display_name`（展示名）是两个字段，
    # 但**历史表把这两个字符串当软外键存了下来**。改任一 field 不同步 = 历史行变孤儿。
    # 真库实测（2026-09-30）：
    #   · agent_tasks.agent_id      → 存 **name**（13 个不同值，12 个能命中 agents.name）
    #   · agent_memory.agent_id     → 存 **name**（9 值，8 命中；reflow 已是历史孤儿）
    #   · tool_call_logs.agent_name → **混存**：7 个值实为 display_name（356 条）+ 6 个值实为 name（33 条）
    # 所以 tool_call_logs 这一列必须**两个口径各过一遍**。
    _NAME_REF_COLS = (
        ("agent_tasks", "agent_id"),
        ("agent_memory", "agent_id"),
        ("tool_call_logs", "agent_name"),
    )

    def count_name_refs(self, name: str) -> dict:
        """某名字在软外键表里被引用多少行（改名影响面预览 / 改后复核）。"""
        out = {}
        if not name:
            return out
        for tbl, col in self._NAME_REF_COLS:
            try:
                out[f"{tbl}.{col}"] = self.scalar(f"SELECT COUNT(*) FROM {tbl} WHERE {col}=?", (name,))
            except Exception:      # 表不存在（新库）不该炸掉整个保存
                out[f"{tbl}.{col}"] = 0
        return out

    def sync_agent_name_refs(self, old_row: dict, new_name: str, new_display_name: str) -> dict:
        """把旧 name / display_name 的软外键引用改写为新值，返回 {表.列: 改写行数}。

        ⚠️ **不要用 `self.execute` 取行数**：`BaseRepo.execute` 返回 `lastrowid or rowcount`，
        UPDATE 语句的 lastrowid 会**残留上次 INSERT 的值**（非 0）→ 拿到的行数不可信。
        这里直接走 `conn.execute` 读 `cur.rowcount`。

        ⚠️ **双重口径保护**：若该 Agent 的 name 与 display_name 原本**相同**（真库 10/20 例），
        两个候选改写的旧值也相同；此时只让**第一个（name，规范标识）**生效，
        避免同一批行被先后改写两次甚至被第二个口径抢走。
        """
        old_row = old_row or {}
        plan, seen = [], set()
        for ov, nv in ((old_row.get("name"), new_name),
                       (old_row.get("display_name"), new_display_name)):
            ov = (ov or "").strip()
            nv = (nv or "").strip()
            if not ov or not nv or ov == nv or ov in seen:
                continue
            seen.add(ov)
            plan.append((ov, nv))
        stat: dict = {}
        for ov, nv in plan:
            for tbl, col in self._NAME_REF_COLS:
                try:
                    cur = self.conn.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (nv, ov))
                except Exception:
                    continue                      # 表/列不存在 → 静默跳过（新库）
                if cur.rowcount:
                    key = f"{tbl}.{col}"
                    stat[key] = stat.get(key, 0) + cur.rowcount
        return stat

    # ── 主/子 Agent 团队（agent_team_members 多对多）──
    def list_team_members(self, main_agent_id: int) -> list:
        """主 Agent 的团队成员摘要（含显示名/图标/角色/启用态）。"""
        rows = self.rows(
            """SELECT m.id AS mid, m.enabled, a.id, a.name, a.display_name, a.icon, a.status, a.agent_role
               FROM agent_team_members m JOIN agents a ON a.id=m.sub_agent_id
               WHERE m.main_agent_id=? ORDER BY m.id""", (main_agent_id,))
        return [dict(r) for r in rows]

    def set_team(self, main_agent_id: int, sub_agent_ids: list) -> None:
        """全量覆盖式保存团队成员（校验由接口层完成）。"""
        self.execute("DELETE FROM agent_team_members WHERE main_agent_id=?", (main_agent_id,))
        for sid in sub_agent_ids:
            self.execute(
                "INSERT OR IGNORE INTO agent_team_members (main_agent_id, sub_agent_id) VALUES (?,?)",
                (main_agent_id, sid))

    def sub_candidates(self, exclude_agent_id: int = 0) -> list:
        """团队候选：agent_role='sub' 且启用且非当前 Agent（主 Agent 选择器数据源）。"""
        rows = self.rows(
            """SELECT id, name, display_name, icon, status, capabilities FROM agents
               WHERE agent_role='sub' AND status='active' AND id<>? ORDER BY id""",
            (exclude_agent_id,))
        for r in rows:
            r["capabilities"] = json.loads(r.get("capabilities") or "[]")
        return rows

    def team_member_intents(self, main_agent_id: int) -> list:
        """主 Agent 团队成员的意图名列表（运行时委派候选收敛用）。"""
        return [r["name"] for r in self.list_team_members(main_agent_id) if r.get("status") == "active"]

    def tool_names_for(self, agent_id: int) -> list:
        """按绑定返回工具名列表（兼容旧 AgentDefinition.tools 语义）。"""
        return [t["tool_name"] for t in self.list_agent_tools(agent_id) if t.get("enabled", 1)]

    # ── 工具消费视图：绑定工具的全量元数据（供 ToolRegistry 注入）──
    def bound_tools_for(self, agent_id: int) -> list:
        """Agent 绑定的 skill/mcp/tool 合并视图（带 source/desc/endpoint 等消费元数据）。"""
        binds = self.list_agent_tools(agent_id)
        out = []
        for b in binds:
            if b["tool_type"] == "skill":
                row = self.one("SELECT * FROM skills WHERE name=?", (b["tool_name"],))
                if row and row.get("enabled", 1):
                    # D4：技能分层结构 JSON 字段统一解析（allowed_tools 白名单 + 渐进披露资源）
                    def _lj(v):
                        try:
                            r = json.loads(v or "[]")
                            return r if isinstance(r, list) else []
                        except Exception:
                            return []
                    out.append({
                        "type": "skill", "name": row["name"], "source": "skill",
                        "desc": row["description"], "skill_type": row["skill_type"],
                        "version": row["version"], "triggers": _lj(row.get("triggers")),
                        "content": row.get("content", ""),
                        "params": json.loads(b.get("params") or "{}"),
                        # D4：工具白名单 + 渐进披露资源
                        "allowed_tools": _lj(row.get("allowed_tools")),
                        "references": _lj(row.get("references")),
                        "examples": _lj(row.get("examples")),
                        "scripts": _lj(row.get("scripts")),
                    })
            elif b["tool_type"] == "mcp":
                row = self.one("SELECT * FROM mcp_servers WHERE name=?", (b["tool_name"],))
                if row and row.get("status") == "online" and row.get("enabled", 1):
                    out.append({
                        "type": "mcp", "name": row["name"], "source": "mcp",
                        "desc": f"MCP 工具（{row['name']}）", "endpoint": row["endpoint"],
                        "transport": row.get("transport", "sse"),
                        "tools": json.loads(row.get("tools") or "[]"),
                        "params": json.loads(b.get("params") or "{}"),
                    })
            elif b["tool_type"] == "plugin":
                # P0-5：插件市场绑定透传（plugin_id），由 AgentRegistry.load_from_db 展开为 skill/mcp 能力
                out.append({
                    "type": "plugin", "name": b["tool_name"], "tool_name": b["tool_name"],
                    "source": "plugin", "tool_type": "plugin", "desc": "插件市场能力（运行时展开）",
                    "params": json.loads(b.get("params") or "{}"),
                })
            elif b["tool_type"] == "tool":
                out.append({
                    "type": "tool", "name": b["tool_name"], "source": "builtin",
                    "desc": "内置工具", "params": json.loads(b.get("params") or "{}"),
                })
        return out

    # ── P1 渐进式工具发现（MCP 官方 Client Best Practices：catalog → inspect → execute）──
    def mcp_catalog(self) -> list:
        """Layer 1 · Catalog：所有在线 MCP 工具名 + 一句话描述（供模型 search_tools 轻量检索）。"""
        out = []
        for row in self.rows("SELECT * FROM mcp_servers WHERE status='online' AND enabled=1"):
            try:
                tool_names = json.loads(row.get("tools") or "[]")
            except Exception:
                tool_names = []
            for t in tool_names:
                out.append({
                    "name": t,
                    "server": row["name"],
                    "endpoint": row["endpoint"],
                    "transport": row.get("transport", "sse"),
                    "desc": f"MCP 工具（{row['name']}）",
                })
        return out

    def mcp_inspect(self, tool_name: str) -> dict | None:
        """Layer 2 · Inspect：单工具完整定义（按需加载，进 context 前先查）。"""
        for row in self.rows("SELECT * FROM mcp_servers WHERE status='online' AND enabled=1"):
            try:
                tool_names = json.loads(row.get("tools") or "[]")
            except Exception:
                tool_names = []
            if tool_name in tool_names:
                return {
                    "name": tool_name,
                    "server": row["name"],
                    "endpoint": row["endpoint"],
                    "transport": row.get("transport", "sse"),
                    "desc": f"MCP 工具（{row['name']}）",
                    "input_schema": {"type": "object", "properties": {}},
                    "note": "完整 schema 由 MCP server 的 tools/list 返回（本层返回注册信息供执行器解析）",
                }
        return None
