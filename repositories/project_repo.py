"""项目域 Repository（P0-1 平台化底座）：projects / scenario_templates / ontology_profiles 表。

对应 routers/projects.py 的全部数据访问，并提供项目上下文隔离查询
（实体/关系按 project_id 过滤，解除星网领域固化）。
"""
from repositories.base import BaseRepo


def resolve_project_id(conn) -> str:
    """当前写入应归属的项目 id —— **唯一来源**：用户配置的 `settings.default_project_id`。

    2026-09-20（「未匹配到就空着，不强制/不默认提供」）：此前 conversations / entities /
    relations / project_memories 等表的 DDL 都写着 `DEFAULT 'project-satnet-broadband'`，
    凡是没显式给 project_id 的写入都会被**静默归入这个已归档项目**（实测：51 个会话、
    图谱工作台手工写入的实体与关系全部落在它名下）。现改为由配置决定；
    **未配置返回空串 = 「不归属任何项目」** —— 不猜、不兜底。
    """
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='default_project_id'").fetchone()
        if row is None:
            return ""
        try:
            return str(row["value"] or "")
        except (TypeError, IndexError):
            return str(row[0] or "")
    except Exception:
        return ""


def conversation_project_id(conn, conversation_id) -> str:
    """会话**当前**所属工程 id（无则空串）——「写入时定格归属」的唯一取数口（P1-1，2026-09-28）。

    与 `resolve_project_id` 的区别（别混用）：
      · `resolve_project_id`   = 平台**配置**里的默认工程（全局单行，不分标签页/会话）；
      · `conversation_project_id` = **这个会话自己**的归属（conversations.project_id，P0-2 后只由
        显式指定产生，不再回落全局默认）。

    产物/版本落库要用后者：同一会话在 A 标签页归属工程 A，此时 B 标签页把平台默认切成 B，
    A 里产生的版本仍应属于 A —— 用配置口径就会被别的标签页带走（P0-2 已修的同类问题）。
    空串是**合法状态**（知识检索/问答类会话不读写工程），不是错误，调用方不得据此报错。
    """
    try:
        row = conn.execute("SELECT project_id FROM conversations WHERE id=?",
                           (conversation_id,)).fetchone()
        if not row:
            return ""
        try:
            return str(row["project_id"] or "").strip()
        except (TypeError, IndexError):
            return str(row[0] or "").strip()
    except Exception:
        import logging, traceback
        logging.getLogger(__name__).warning(
            "[conversation_project_id] 取会话归属失败（conv=%s），按无工程处理：\n%s",
            conversation_id, traceback.format_exc())
        return ""


def parse_tool_binding(raw) -> dict:
    """解析 `projects.tool_binding`（JSON: {tool, ref, name}）→ dict；空/非法一律返回 {}。

    2026-09-28（多工程 P0-1）：此前 tool_binding **只写不查** —— 写入/写回时正向读一次，
    没有任何「由工具侧工程标识反查本地项目」的实现，故「从建模工具打开 AI 平台」接不住上下文
    （用户只能打开首页后人工记得切工程）。反查的第一件事就是把解析收敛到一处：
    **空串 / 非法 JSON / 非 dict 一律 {}**，不抛、不静默吞成别的东西（调用方据此区分"未绑定"）。
    """
    if not raw:
        return {}
    import json as _json
    try:
        v = _json.loads(raw)
    except Exception:
        return {}
    return v if isinstance(v, dict) else {}


def resolve_project_by_tool(conn, tool: str, ref: str) -> tuple[str, list]:
    """由建模工具侧工程标识**反查**本地项目：`(命中的 project_id, 同工具候选项目清单)`。

    匹配口径：**tool 与 ref 同时精确相等**（大小写不敏感、两端去空白）。
    为什么不用 LIKE/模糊匹配：ref 是工具侧工程标识（智源 vc=`branchId,queryType`、
    MagicDraw = 工程 ID/名），**相似 ≠ 同一个工程**；模糊匹配会把「同工具另一个工程」错当成命中，
    那比查不到更糟（写回会写进错误的工程）。

    返回 `("", [...])` = 未命中；候选项 = 已绑定**同一 tool** 的项目（供前端提示「未绑定，请绑定」）。
    未命中时**绝不猜**（不回退到默认项目、不取第一个项目）—— 见「未匹配到就空着」政策。
    """
    t = (tool or "").strip().lower()
    r = (ref or "").strip()
    if not t or not r:
        return "", []
    hit, cands = "", []
    try:
        # ⚠️ 这里的 except **必须留痕**：它一旦静默，反查会退化成「永远查不到」，
        # 而症状与「该工具侧工程确实没绑定」完全一样 —— 无法区分，极难定位（2026-09-28 实测：
        # 夹具缺 created_at 列时全部断言以「未命中」的假象失败，靠 traceback 才现形）。
        for row in conn.execute("SELECT id, name, tool_binding FROM projects ORDER BY created_at"):
            b = parse_tool_binding(row["tool_binding"] if hasattr(row, "keys") else row[2])
            _pid = row["id"] if hasattr(row, "keys") else row[0]
            _name = row["name"] if hasattr(row, "keys") else row[1]
            if (b.get("tool") or "").strip().lower() != t:
                continue
            cands.append({"id": _pid, "name": _name, "ref": (b.get("ref") or "").strip()})
            if (b.get("ref") or "").strip() == r:
                hit = _pid or ""
    except Exception:
        import logging
        import traceback
        logging.getLogger(__name__).warning(
            "[resolve_project_by_tool] 反查失败（tool=%s ref=%s），按未命中处理：\n%s",
            t, r, traceback.format_exc())
        return "", []
    return hit, cands


class ProjectRepo(BaseRepo):
    """项目 / 场景模板 / 本体 Profile 数据访问 + 项目隔离查询。"""

    # ── projects ──
    def list_projects(self) -> list:
        return self.rows("SELECT * FROM projects ORDER BY created_at")

    def get_project(self, project_id: str) -> dict | None:
        return self.one("SELECT * FROM projects WHERE id=?", (project_id,))

    def get_project_by_code(self, code: str) -> dict | None:
        return self.one("SELECT * FROM projects WHERE code=?", (code,))

    def create_project(self, project_id: str, name: str, code: str, domain: str,
                       description: str, scenario_template_id: str | None,
                       ontology_profile_id: str | None,
                       tool_binding: str = "",
                       source: str = "local", workspace: str = "",
                       remote: str = "") -> None:
        self.execute(
            "INSERT INTO projects (id, name, code, domain, description, scenario_template_id, ontology_profile_id, status, tool_binding, source, workspace, remote) "
            "VALUES (?,?,?,?,?,?,?, 'active', ?,?,?,?)",
            (project_id, name, code, domain, description, scenario_template_id, ontology_profile_id,
             tool_binding, source, workspace, remote),
        )

    def update_project(self, project_id: str, name: str | None = None, domain: str | None = None,
                       description: str | None = None, scenario_template_id: str | None = None,
                       ontology_profile_id: str | None = None, status: str | None = None,
                       tool_binding: str | None = None, source: str | None = None,
                       workspace: str | None = None, remote: str | None = None) -> None:
        sets, params = [], []
        for col, val in [("name", name), ("domain", domain), ("description", description),
                         ("scenario_template_id", scenario_template_id),
                         ("ontology_profile_id", ontology_profile_id), ("status", status),
                         ("tool_binding", tool_binding), ("source", source),
                         ("workspace", workspace), ("remote", remote)]:
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if not sets:
            return
        params.append(project_id)
        self.execute(
            f"UPDATE projects SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        )

    def delete_project(self, project_id: str) -> None:
        self.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def count_project_tasks(self, project_id: str) -> int:
        """该项目下会话（任务）数——删除项目前的影响面提示。"""
        return int(self.scalar(
            "SELECT COUNT(*) FROM conversations WHERE project_id=?", (project_id,), default=0) or 0)

    def unassign_project_tasks(self, project_id: str) -> int:
        """把该项目下的会话解绑（project_id 置空，会话与消息**保留**——删项目不删任务）。

        与「移除项目」的语义一致：移除的是容器，不是内容；不置空会让任务继续挂在一个
        已不存在的项目上（导航分组按 project_id 匹配，表现为"任务消失"）。
        返回受影响行数。
        """
        return int(self.execute(
            "UPDATE conversations SET project_id='' WHERE project_id=?", (project_id,)) or 0)

    # ── 默认项目（settings 持久化，前端/后端统一读取）──
    def get_default_project_id(self) -> str:
        """默认项目 id；**未设置时返回空串** = 「无显式项目关联则不注入项目宪法」。

        2026-09-20：原 default 硬编码 `'project-satnet-broadband'`。两处不妥 ——
          ① 属领域固化残留（本模块所在的项目域，文档头明示目标是「解除星网领域固化」）；
          ② 它让「把 default_project_id 置空」在 settings 行缺失时被悄悄推翻，
             使清理不可持续。故改为空串，与 routers/projects.py::get_default_project
             的空态返回、agent/pipeline_parts/memory.py 的 `if not pid: return ""` 三者一致。
        """
        return self.scalar(
            "SELECT value FROM settings WHERE key='default_project_id'",
            default="",
        ) or ""

    def set_default_project(self, project_id: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO settings (key, value, description) VALUES ('default_project_id', ?, '默认项目（P0-1）')",
            (project_id,),
        )

    # ── 项目隔离查询（实体/关系按 project_id 过滤）──
    def project_entities(self, project_id: str, status: str | None = None,
                         search: str | None = None) -> list:
        q = "SELECT * FROM entities WHERE project_id=?"
        params = [project_id]
        if status:
            q += " AND status=?"
            params.append(status)
        if search:
            q += " AND (name LIKE ? OR id LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        q += " ORDER BY created_at DESC LIMIT 200"
        return self.rows(q, params)

    def project_graph(self, project_id: str, branch: str = "dev") -> dict:
        ents = self.rows(
            "SELECT id, name, entity_type, status, branch FROM entities "
            "WHERE project_id=? AND status!='deprecated' AND branch IN (?, 'release') LIMIT 100",
            (project_id, branch),
        )
        rels = self.rows(
            "SELECT r.*, e1.name as source_name, e2.name as target_name FROM relations r "
            "JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
            "WHERE r.project_id=? AND r.status!='deprecated' AND r.branch IN (?, 'release') LIMIT 200",
            (project_id, branch),
        )
        return {"entities": ents, "relations": rels}

    # ── scenario_templates ──
    def list_scenario_templates(self) -> list:
        return self.rows("SELECT * FROM scenario_templates ORDER BY created_at")

    def get_scenario_template(self, template_id: str) -> dict | None:
        return self.one("SELECT * FROM scenario_templates WHERE id=?", (template_id,))

    # ── ontology_profiles ──
    def list_ontology_profiles(self, scenario_template_id: str | None = None) -> list:
        if scenario_template_id:
            return self.rows(
                "SELECT * FROM ontology_profiles WHERE scenario_template_id=? ORDER BY created_at",
                (scenario_template_id,),
            )
        return self.rows("SELECT * FROM ontology_profiles ORDER BY created_at")

    def get_ontology_profile(self, profile_id: str) -> dict | None:
        return self.one("SELECT * FROM ontology_profiles WHERE id=?", (profile_id,))

    # ── 项目级持久记忆（Project Constitution）：规范/基线/决策/经验，AI 会话每次注入防漂移 ──
    def list_project_memories(self, project_id: str, category: str | None = None,
                              only_enabled: bool = False) -> list:
        q = "SELECT * FROM project_memories WHERE project_id=?"
        params = [project_id]
        if only_enabled:
            q += " AND enabled=1"
        if category:
            q += " AND category=?"
            params.append(category)
        q += " ORDER BY id"
        return self.rows(q, params)

    def get_project_memory(self, mem_id: int) -> dict | None:
        return self.one("SELECT * FROM project_memories WHERE id=?", (mem_id,))

    def create_project_memory(self, project_id: str, category: str, title: str,
                              content: str, created_by: str = "王工") -> int:
        return self.execute(
            "INSERT INTO project_memories (project_id, category, title, content, created_by) "
            "VALUES (?,?,?,?,?)",
            (project_id, category, title, content, created_by),
        )

    def update_project_memory(self, mem_id: int, category: str | None = None,
                              title: str | None = None, content: str | None = None,
                              enabled: int | None = None) -> None:
        sets, params = [], []
        for col, val in (("category", category), ("title", title),
                         ("content", content), ("enabled", enabled)):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if not sets:
            return
        sets.append("updated_at=CURRENT_TIMESTAMP")
        params.append(mem_id)
        self.execute(f"UPDATE project_memories SET {', '.join(sets)} WHERE id=?", params)

    def delete_project_memory(self, mem_id: int) -> None:
        self.execute("DELETE FROM project_memories WHERE id=?", (mem_id,))
