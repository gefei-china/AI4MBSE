"""AI 设计工坊域 Repository：prompts / skills / mcp_servers / tools / agent_flows / generate_rules 表。

对应 routers/studio.py 的全部数据访问。
"""
import json

from repositories.base import BaseRepo


class StudioRepo(BaseRepo):
    """定制化中心（提示词/技能/MCP/工具/规则/Agent流程）数据访问。"""

    # ── legacy ↔ plugins 同步桥（2026-09-16）──
    def _sync(self, table: str, row_id) -> None:
        """旧表行 → plugins 同步（管理视图随之更新）。

        失败绝不阻断主流程：能力中心的可见性不应影响业务写入。
        """
        if not row_id:
            return
        try:
            from plugin_system import legacy_sync
            legacy_sync.sync_from_legacy_safe(self.conn, table, row_id)
        except Exception:
            pass

    # ── prompts ──
    def list_prompts(self) -> list:
        return self.rows("SELECT * FROM prompts ORDER BY updated_at DESC")

    def create_prompt(self, name: str, scenario: str, content: str, variables: str, version: str, created_by: str = "王工") -> int:
        rid = self.execute(
            "INSERT INTO prompts (name, scenario, content, variables, version, created_by) VALUES (?,?,?,?,?,?)",
            (name, scenario, content, variables, version, created_by),
        )
        self._sync("prompts", rid)
        return rid

    def update_prompt(self, pid: int, name: str, scenario: str, content: str, variables: str, version: str) -> None:
        self.execute(
            "UPDATE prompts SET name=?, scenario=?, content=?, variables=?, version=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (name, scenario, content, variables, version, pid),
        )
        self._sync("prompts", pid)

    def delete_prompt(self, pid: int) -> None:
        self.execute("DELETE FROM prompts WHERE id=?", (pid,))
        self._sync("prompts", pid)

    def publish_prompt(self, pid: int) -> None:
        self.execute("UPDATE prompts SET status='published', updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
        self._sync("prompts", pid)

    # ── skills ──
    @staticmethod
    def _parse_skill(row: dict) -> dict:
        """JSON 字段解析（triggers/dependencies/allowed_roles/allowed_tools/references/examples/scripts 容错）。"""
        for k in ("triggers", "dependencies", "allowed_roles", "allowed_tools", "references", "examples", "scripts"):
            try:
                v = json.loads(row.get(k) or "[]")
                row[k] = v if isinstance(v, list) else []
            except Exception:
                row[k] = []
        return row

    def list_skills(self) -> list:
        rows = self.rows("""
            SELECT s.*, p.name as prompt_name FROM skills s
            LEFT JOIN prompts p ON s.prompt_id=p.id ORDER BY s.created_at DESC""")
        return [self._parse_skill(dict(r)) for r in rows]

    def get_skill(self, sid: int) -> dict | None:
        row = self.one("SELECT * FROM skills WHERE id=?", (sid,))
        return self._parse_skill(dict(row)) if row else None

    def get_skill_by_name(self, name: str) -> dict | None:
        row = self.one("SELECT * FROM skills WHERE name=?", (name,))
        return self._parse_skill(dict(row)) if row else None

    def create_skill(self, name: str, description: str, skill_type: str,
                     triggers: str, category: str, content: str,
                     frontmatter: str, package_path: str, status: str = "draft",
                     dependencies: str = "[]", allowed_roles: str = "[]",
                     allowed_tools: str = "[]", references: str = "[]",
                     examples: str = "[]", scripts: str = "[]") -> int:
        rid = self.execute(
            """INSERT INTO skills (name, description, skill_type, triggers, category,
               content, frontmatter, package_path, status, version, dependencies,
               allowed_roles, allowed_tools, `references`, examples, scripts)
               VALUES (?,?,?,?,?,?,?,?,?,'v1.0',?,?,?,?,?,?)""",
            (name, description, skill_type, triggers, category, content, frontmatter,
             package_path, status, dependencies, allowed_roles, allowed_tools,
             references, examples, scripts),
        )
        self._sync("skills", rid)
        return rid

    def update_skill(self, sid: int, name: str, description: str, skill_type: str,
                     triggers: str, category: str, content: str, frontmatter: str,
                     dependencies: str = "[]", allowed_roles: str = "[]",
                     allowed_tools: str = "[]", references: str = "[]",
                     examples: str = "[]", scripts: str = "[]") -> None:
        self.execute(
            """UPDATE skills SET name=?, description=?, skill_type=?, triggers=?, category=?,
               content=?, frontmatter=?, dependencies=?, allowed_roles=?,
               allowed_tools=?, `references`=?, examples=?, scripts=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (name, description, skill_type, triggers, category, content, frontmatter,
             dependencies, allowed_roles, allowed_tools, references, examples, scripts, sid),
        )
        self._sync("skills", sid)

    def delete_skill(self, sid: int) -> None:
        self.execute("DELETE FROM skills WHERE id=?", (sid,))
        self._sync("skills", sid)

    def publish_skill(self, sid: int) -> None:
        self.execute(
            "UPDATE skills SET status='published', updated_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
        self._sync("skills", sid)

    def set_skill_enabled(self, sid: int, enabled: int) -> None:
        self.execute("UPDATE skills SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (enabled, sid))

    # ── mcp_servers ──
        self._sync("skills", sid)
    def list_mcp_servers(self) -> list:
        return self.rows("SELECT * FROM mcp_servers")

    def get_mcp_server(self, sid: int) -> dict | None:
        return self.one("SELECT * FROM mcp_servers WHERE id=?", (sid,))

    def create_mcp_server(self, name: str, endpoint: str, tools: str,
                          transport: str = "sse", command: str = "",
                          args: str = "[]", env: str = "{}") -> int:
        rid = self.execute(
            """INSERT INTO mcp_servers (name, endpoint, tools, transport, command, args, env)
               VALUES (?,?,?,?,?,?,?)""",
            (name, endpoint, tools, transport, command, args, env),
        )
        self._sync("mcp_servers", rid)
        return rid

    def update_mcp_server(self, sid: int, name: str, endpoint: str, tools: str,
                          transport: str, command: str, args: str, env: str) -> None:
        self.execute(
            """UPDATE mcp_servers SET name=?, endpoint=?, tools=?, transport=?, command=?,
               args=?, env=? WHERE id=?""",
            (name, endpoint, tools, transport, command, args, env, sid),
        )
        self._sync("mcp_servers", sid)

    def update_mcp_status(self, sid: int, status: str, latency_ms: int, tools: str | None = None) -> None:
        if tools is not None:
            self.execute(
                """UPDATE mcp_servers SET status=?, latency_ms=?, tools=?,
                   last_check=CURRENT_TIMESTAMP WHERE id=?""",
                (status, latency_ms, tools, sid),
            )
        else:
            self.execute(
                """UPDATE mcp_servers SET status=?, latency_ms=?,
                   last_check=CURRENT_TIMESTAMP WHERE id=?""",
                (status, latency_ms, sid),
            )

    def delete_mcp_server(self, sid: int) -> None:
        self.execute("DELETE FROM mcp_servers WHERE id=?", (sid,))
        self._sync("mcp_servers", sid)

    def set_mcp_enabled(self, sid: int, enabled: int) -> None:
        self.execute("UPDATE mcp_servers SET enabled=? WHERE id=?", (enabled, sid))

    # ── MCP-D1：动态发现与健康巡检落库 ──
        self._sync("mcp_servers", sid)
    def update_mcp_discovery(self, sid: int, status: str, latency_ms: int,
                             tools: str, capabilities: str, protocol_version: str,
                             server_info: str, resources: str, prompts: str) -> None:
        self.execute(
            """UPDATE mcp_servers SET status=?, latency_ms=?, tools=?, capabilities=?,
               protocol_version=?, server_info=?, resources=?, prompts=?,
               last_error='', last_check=CURRENT_TIMESTAMP, last_discover=CURRENT_TIMESTAMP
               WHERE id=?""",
            (status, latency_ms, tools, capabilities, protocol_version,
             server_info, resources, prompts, sid),
        )

    def update_mcp_error(self, sid: int, error: str) -> None:
        self.execute(
            "UPDATE mcp_servers SET status='offline', last_error=?, "
            "last_check=CURRENT_TIMESTAMP WHERE id=?",
            (error[:300], sid),
        )

    # ── tools（TR-P1：工具注册表维护 CRUD）──
    def list_tools(self) -> list:
        return self.rows("SELECT * FROM tools ORDER BY source, name")

    def get_tool(self, tid: int) -> dict | None:
        return self.one("SELECT * FROM tools WHERE id=?", (tid,))

    def get_tool_by_name(self, name: str) -> dict | None:
        return self.one("SELECT * FROM tools WHERE name=?", (name,))

    def create_tool(self, name: str, description: str, source: str,
                    input_schema: str, version: str, side_effect: str,
                    risk_level: str, owner: str = "", status: str = "active",
                    allowed_roles: str = "[]", retry_policy: str = "{}",
                    fallback_to: str = "", config: str = "{}") -> int:
        rid = self.execute(
            "INSERT INTO tools (name, source, description, status, input_schema, version, side_effect, risk_level, owner, allowed_roles, retry_policy, fallback_to, config) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, source, description, status, input_schema, version, side_effect, risk_level, owner, allowed_roles,
             retry_policy, fallback_to, config),
        )
        self._sync("tools", rid)
        return rid

    def update_tool(self, tid: int, description: str, input_schema: str,
                    retry_policy: str = "{}", fallback_to: str = "", config: str = "{}") -> None:
        """仅更新可编辑字段（描述/schema/重试/降级/config）；版本/副作用/风险/负责人/角色保持注册来源值。"""
        self.execute(
            "UPDATE tools SET description=?, input_schema=?, retry_policy=?, fallback_to=?, config=?, "
            "created_at=CURRENT_TIMESTAMP WHERE id=?",
            (description, input_schema, retry_policy, fallback_to, config, tid),
        )
        self._sync("tools", tid)

    def set_tool_status(self, tid: int, status: str) -> None:
        self.execute("UPDATE tools SET status=? WHERE id=?", (status, tid))
        self._sync("tools", tid)

    def delete_tool(self, tid: int) -> None:
        self.execute("DELETE FROM tools WHERE id=?", (tid,))
        self._sync("tools", tid)

    def agent_tool_refs(self, tool_name: str) -> list:
        return self.rows("SELECT agent_id, tool_type FROM agent_tools WHERE tool_name=?", (tool_name,))

    # ── generate_rules ──
    def list_rules(self) -> list:
        return self.rows("SELECT * FROM generate_rules")

    def update_rule(self, rule_id: int, value: str) -> None:
        self.execute(
            "UPDATE generate_rules SET rule_value=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (value, rule_id),
        )

    # ── agent_flows ──
    def list_agent_flows(self) -> list:
        return self.rows("SELECT * FROM agent_flows ORDER BY created_at DESC")

    def get_agent_flow(self, flow_id: int) -> dict | None:
        return self.one("SELECT * FROM agent_flows WHERE id=?", (flow_id,))

    def create_agent_flow(self, name: str, description: str, nodes: str, edges: str, version: str = "v1", status: str = "draft", source: str = "manual") -> int:
        return self.execute(
            "INSERT INTO agent_flows (name, description, nodes, edges, version, status, source) VALUES (?,?,?,?,?,?,?)",
            (name, description, nodes, edges, version, status, source),
        )

    def update_agent_flow(self, flow_id: int, name: str, description: str, nodes: str, edges: str, version: str = "v1", status: str = "draft", source: str = "manual") -> None:
        self.execute(
            "UPDATE agent_flows SET name=?, description=?, nodes=?, edges=?, version=?, status=?, source=? WHERE id=?",
            (name, description, nodes, edges, version, status, source, flow_id),
        )

    def delete_agent_flow(self, flow_id: int) -> None:
        self.execute("DELETE FROM agent_flows WHERE id=?", (flow_id,))

    # ── 插件市场（Skill/MCP/工具 公共市场 + 私人空间；scope/source_ref/pinned/install_count）──
    # kind → 表/主键列映射
    _MARKET_TABLES = {
        "skill": ("skills", "id"),
        "mcp": ("mcp_servers", "id"),
        "tool": ("tools", "id"),
    }

    def _market_rows(self, kind: str, scope: str = "public", q: str = "", category: str = "") -> list:
        """按 kind 查 scope 范围条目，统一字段视图（kind 徽章/名称/描述/分类/版本/内置/置顶/安装数）。"""
        table, _ = self._MARKET_TABLES[kind]
        cols = [r["name"] for r in self.rows(f"PRAGMA table_info({table})")]
        desc_col = "description" if "description" in cols else "endpoint"
        cat_col = "category" if "category" in cols else ("transport" if table == "mcp_servers" else "''")
        ver_col = "version" if "version" in cols else "''"
        ori_col = "origin" if "origin" in cols else "''"
        where = f"WHERE scope=? COLLATE NOCASE"
        params: list = [scope]
        if q:
            where += f" AND (name LIKE ? OR {desc_col} LIKE ?)"
            params += [f"%{q}%", f"%{q}%"]
        if category:
            if cat_col != "''":
                where += f" AND {cat_col}=?"
                params.append(category)
            else:
                # 该种类无分类字段（如 tools）：按分类筛选时该种类不返回任何条目
                return []
        rows = self.rows(
            f"SELECT id, name, {desc_col} AS descr, {cat_col} AS category, {ver_col} AS version, "
            f"builtin, pinned, install_count, {ori_col} AS origin, created_at FROM {table} {where} "
            f"ORDER BY pinned DESC, install_count DESC, id DESC",
            params)
        return [dict(r) for r in rows]

    def market_list(self, kind: str = "all", q: str = "", category: str = "", sort: str = "hot") -> list:
        """公共市场聚合列表（scope='public'；kind=all 时三表合并）。

        - installed：当前用户是否已安装（私人副本 source_ref 前缀匹配或同名私有项存在）
        - sort：hot=置顶优先+安装数降序（默认）；new=按发布时间倒序
        """
        kinds = ["skill", "mcp", "tool"] if kind == "all" else [kind]
        out = []
        for k in kinds:
            table, _ = self._MARKET_TABLES[k]
            # 一次性取出私人空间全部条目，构建「已安装」判定集与副本 id（供前端直接编辑/删除副本）
            priv_rows = self.rows(f"SELECT id, name, source_ref FROM {table} WHERE scope='private' COLLATE NOCASE")
            installed_names = set()
            installed_ids: dict = {}
            for p in priv_rows:
                pname = str(p["name"]).lower()
                installed_names.add(pname)
                installed_ids[pname] = p["id"]
                src = str(p.get("source_ref") or "")
                if src.startswith(f"{k}:"):
                    parts = src.split(":", 2)
                    if len(parts) >= 2 and parts[1]:
                        sname = parts[1].lower()
                        installed_names.add(sname)
                        installed_ids.setdefault(sname, p["id"])
            for r in self._market_rows(k, "public", q, category):
                r["kind"] = k
                r["installed"] = bool(r["name"] and str(r["name"]).lower() in installed_names)
                r["installed_id"] = installed_ids.get(str(r["name"]).lower()) if r["installed"] else None
                out.append(r)
        if sort == "new":
            return sorted(out, key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return sorted(out, key=lambda x: (-(x.get("pinned") or 0), -(x.get("install_count") or 0)))

    def market_install(self, kind: str, name: str) -> int | None:
        """安装市场条目 → 复制为私人空间副本（scope='private'，source_ref 记录来源）。

        返回新副本 id；同名私人条目已存在返回 None（由接口层报 400）。
        注意：tools.name 全局 UNIQUE（public 源占用了原名）→ 副本名加「（市场副本）」后缀；
        skills/mcp_servers 无 name 唯一约束，副本保留原名。
        """
        table, _ = self._MARKET_TABLES[kind]
        src = self.one(f"SELECT * FROM {table} WHERE name=? AND scope='public' COLLATE NOCASE", (name,))
        if not src:
            return None
        dup = self.one(f"SELECT id FROM {table} WHERE name=? AND scope='private' COLLATE NOCASE", (name,))
        if dup:
            return None
        col_names = [r["name"] for r in self.rows(f"PRAGMA table_info({table})")]
        col_names = [c for c in col_names if c not in ("id", "created_at", "updated_at", "pinned", "install_count")]
        vals = []
        for c in col_names:
            v = src.get(c)
            # tools 副本名加后缀（原名被 public 源占用）
            if c == "name" and kind == "tool":
                cand = f"{name}（市场副本）"
                i = 2
                while self.one(f"SELECT id FROM {table} WHERE name=?", (cand,)):
                    cand = f"{name}（市场副本{i}）"
                    i += 1
                v = cand
            vals.append(v)
        cols_sql = ",".join(f"`{c}`" for c in col_names)
        ph = ",".join("?" * len(col_names))
        new_id = self.execute(f"INSERT INTO {table} ({cols_sql}) VALUES ({ph})", vals)
        ver = str(src.get("version") or "v1.0")
        self.execute(f"UPDATE {table} SET scope='private', source_ref=?, pinned=0, install_count=0 WHERE id=?",
                     (f"{kind}:{name}:{ver}", new_id))
        self.execute(f"UPDATE {table} SET install_count=install_count+1 WHERE id=?", (src["id"],))
        return new_id

    def _find_by_name(self, kind: str, name: str, scope: str | None = None) -> dict | None:
        table, _ = self._MARKET_TABLES[kind]
        if scope:
            return self.one(f"SELECT * FROM {table} WHERE name=? AND scope=? COLLATE NOCASE", (name, scope))
        return self.one(f"SELECT * FROM {table} WHERE name=? COLLATE NOCASE", (name,))

    def market_publish(self, kind: str, name: str) -> bool:
        """私人 → 公共市场（source_ref 清空；同名 public 已存在返回 False）。"""
        table, _ = self._MARKET_TABLES[kind]
        item = self._find_by_name(kind, name, "private")
        if not item:
            return False
        exists = self.one(f"SELECT id FROM {table} WHERE name=? AND scope='public' COLLATE NOCASE", (name,))
        if exists:
            return False
        self.execute(f"UPDATE {table} SET scope='public', source_ref='', origin='admin' WHERE id=?", (item["id"],))
        return True

    def market_unpublish(self, kind: str, name: str) -> bool:
        """公共（自定义项）→ 私人；内置项返回 False（禁止下架）。"""
        table, _ = self._MARKET_TABLES[kind]
        item = self._find_by_name(kind, name, "public")
        if not item:
            return False
        if item.get("builtin"):
            return False
        self.execute(f"UPDATE {table} SET scope='private', source_ref='' WHERE id=?", (item["id"],))
        return True

    def market_update(self, kind: str, name: str, fields: dict) -> bool:
        """市场管理：编辑 public 条目可编辑字段（description/category/version/pinned）。"""
        table, _ = self._MARKET_TABLES[kind]
        item = self._find_by_name(kind, name, "public")
        if not item:
            return False
        cols = [r["name"] for r in self.rows(f"PRAGMA table_info({table})")]
        sets, params = [], []
        for k, v in (fields or {}).items():
            if k in ("description", "category", "version", "pinned") and k in cols:
                sets.append(f"`{k}`=?")
                params.append(v)
        if not sets:
            return True
        params.append(item["id"])
        self.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id=?", params)
        return True

    def market_toggle_pin(self, kind: str, name: str) -> bool:
        """市场管理：置顶/取消置顶。"""
        table, _ = self._MARKET_TABLES[kind]
        item = self._find_by_name(kind, name, "public")
        if not item:
            return False
        self.execute(f"UPDATE {table} SET pinned=CASE WHEN pinned=1 THEN 0 ELSE 1 END WHERE id=?", (item["id"],))
        return True

    def private_list(self, kind: str) -> list:
        """私人空间列表（scope='private'），供前端"我的"视图。"""
        return self._market_rows(kind, "private")

    def mine_list(self, kind: str = "all", q: str = "") -> list:
        """我的插件统一列表 = 私人空间条目 + 平台内置预置（builtin 始终可用）。

        供「插件管理 → 我的插件」统一视图（skill/mcp/tool 聚合，kind 字段区分）。
        """
        kinds = ["skill", "mcp", "tool"] if kind == "all" else [kind]
        out = []
        for k in kinds:
            table, _ = self._MARKET_TABLES[k]
            cols = [r["name"] for r in self.rows(f"PRAGMA table_info({table})")]
            desc_col = "description" if "description" in cols else "endpoint"
            cat_col = "category" if "category" in cols else ("transport" if table == "mcp_servers" else "source")
            ver_col = "version" if "version" in cols else "''"
            en_col = "enabled" if "enabled" in cols else "1"
            st_col = "status" if "status" in cols else "''"
            src_col = "source_ref" if "source_ref" in cols else "''"
            share_col = "share_status" if "share_status" in cols else "''"
            where = "WHERE (scope='private' COLLATE NOCASE OR builtin=1)"
            params: list = []
            if q:
                where += f" AND (name LIKE ? OR {desc_col} LIKE ?)"
                params += [f"%{q}%", f"%{q}%"]
            rows = self.rows(
                f"SELECT id, name, {desc_col} AS descr, {cat_col} AS category, {ver_col} AS version, "
                f"builtin, pinned, install_count, {src_col} AS source_ref, {en_col} AS enabled, {st_col} AS status, "
                f"{share_col} AS share_status "
                f"FROM {table} {where} ORDER BY builtin DESC, pinned DESC, id DESC", params)
            for r in rows:
                r["kind"] = k
                out.append(r)
        return out

    def mine_toggle_pin(self, kind: str, item_id: int) -> bool | None:
        """我的空间（私人条目）置顶/取消置顶（图钉）。内置项为 scope='public'，不在置顶范围。

        返回切换后的 pinned（0/1）；条目不存在或非私人空间返回 None。
        """
        table, _ = self._MARKET_TABLES[kind]
        item = self.one(f"SELECT id, pinned FROM {table} WHERE id=? AND scope='private' COLLATE NOCASE", (item_id,))
        if not item:
            return None
        new_pin = 0 if item.get("pinned") else 1
        self.execute(f"UPDATE {table} SET pinned=? WHERE id=?", (new_pin, item_id))
        return new_pin

    # ── 个人插件分享审核（share_status + plugin_review_log）──

    def _share_log(self, kind: str, item_id: int, item_name: str, action: str, comment: str = "", operator: str = "") -> None:
        self.execute(
            "INSERT INTO plugin_review_log (kind, item_id, item_name, action, comment, operator) VALUES (?,?,?,?,?,?)",
            (kind, item_id, item_name, action, comment, operator))

    def share_submit(self, kind: str, item_id: int) -> str | None:
        """个人插件提交分享（待管理员审核）。仅限私人空间自建条目（非内置、非市场副本）。

        返回 None=成功；否则返回错误文案。
        """
        table, _ = self._MARKET_TABLES[kind]
        item = self.one(f"SELECT * FROM {table} WHERE id=? AND scope='private' COLLATE NOCASE", (item_id,))
        if not item:
            return "条目不存在或非私人空间条目"
        if item.get("builtin"):
            return "内置插件不支持分享"
        if item.get("source_ref"):
            return "市场添加的副本不支持分享（分享个人自建插件）"
        st = str(item.get("share_status") or "")
        if st == "submitted":
            return "已提交审核，请等待管理员处理"
        if st == "approved":
            return "该插件已在市场中"
        self.execute(f"UPDATE {table} SET share_status='submitted' WHERE id=?", (item_id,))
        self._share_log(kind, item_id, str(item["name"]), "submit", "提交分享申请", "王工")
        return None

    def share_review(self, kind: str, item_id: int, action: str, comment: str = "") -> str | None:
        """管理员审核：approve 通过入市 / reject 驳回。返回 None=成功，否则错误文案。"""
        table, _ = self._MARKET_TABLES[kind]
        item = self.one(f"SELECT * FROM {table} WHERE id=?", (item_id,))
        if not item:
            return "条目不存在"
        if item.get("builtin"):
            return "内置插件不支持审核"
        if str(item.get("share_status") or "") != "submitted":
            return "该条目不在待审核状态"
        if action == "approve":
            # 同名市场条目已存在则拒绝（避免覆盖市场数据）
            exists = self.one(f"SELECT id FROM {table} WHERE name=? AND scope='public' COLLATE NOCASE", (item["name"],))
            if exists:
                return f"公共市场已存在同名条目「{item['name']}」，无法通过审核（请先在个人空间重命名）"
            self.execute(f"UPDATE {table} SET scope='public', source_ref='', share_status='approved', origin='share' WHERE id=?", (item_id,))
            self._share_log(kind, item_id, str(item["name"]), "approve", comment or "审核通过，上架市场", "王工")
        else:  # reject
            self.execute(f"UPDATE {table} SET share_status='rejected' WHERE id=?", (item_id,))
            self._share_log(kind, item_id, str(item["name"]), "reject", comment or "审核驳回", "王工")
        return None

    def share_items(self, kind: str = "all", share_status: str = "") -> list:
        """分享审核列表（私人空间非内置条目，含 share_status）。供插件管理待审核/已驳回视图。"""
        kinds = ["skill", "mcp"] if kind == "all" else [kind]
        out = []
        for k in kinds:
            table, _ = self._MARKET_TABLES[k]
            cols = [r["name"] for r in self.rows(f"PRAGMA table_info({table})")]
            desc_col = "description" if "description" in cols else "endpoint"
            cat_col = "category" if "category" in cols else ("transport" if table == "mcp_servers" else "source")
            ver_col = "version" if "version" in cols else "''"
            en_col = "enabled" if "enabled" in cols else "1"
            where = "WHERE scope='private' COLLATE NOCASE AND builtin=0"
            params: list = []
            if share_status:
                where += " AND share_status=?"
                params.append(share_status)
            rows = self.rows(
                f"SELECT id, name, {desc_col} AS descr, {cat_col} AS category, {ver_col} AS version, "
                f"pinned, install_count, {en_col} AS enabled, share_status, created_at "
                f"FROM {table} {where} ORDER BY id DESC", params)
            for r in rows:
                r["kind"] = k
                out.append(r)
        return out

    def share_log(self, limit: int = 100) -> list:
        """审批记录（提交/通过/驳回留痕，倒序）。"""
        return self.rows("SELECT * FROM plugin_review_log ORDER BY id DESC LIMIT ?", (limit,))
