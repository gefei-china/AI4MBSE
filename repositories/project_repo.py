"""项目域 Repository（P0-1 平台化底座）：projects / scenario_templates / ontology_profiles 表。

对应 routers/projects.py 的全部数据访问，并提供项目上下文隔离查询
（实体/关系按 project_id 过滤，解除星网领域固化）。
"""
from repositories.base import BaseRepo


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
                       ontology_profile_id: str | None) -> None:
        self.execute(
            "INSERT INTO projects (id, name, code, domain, description, scenario_template_id, ontology_profile_id, status) "
            "VALUES (?,?,?,?,?,?,?, 'active')",
            (project_id, name, code, domain, description, scenario_template_id, ontology_profile_id),
        )

    def update_project(self, project_id: str, name: str | None = None, domain: str | None = None,
                       description: str | None = None, scenario_template_id: str | None = None,
                       ontology_profile_id: str | None = None, status: str | None = None) -> None:
        sets, params = [], []
        for col, val in [("name", name), ("domain", domain), ("description", description),
                         ("scenario_template_id", scenario_template_id),
                         ("ontology_profile_id", ontology_profile_id), ("status", status)]:
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
