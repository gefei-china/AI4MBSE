"""项目域：/api/projects/*（P0-1 平台化：项目 / 场景模板 / 本体 Profile 上下文隔离）。

解除星网领域固化：新领域接入 = 新建场景模板 + 新建项目，不改代码。
前端在项目切换后，将 project_id 透传给知识图谱/实体查询即可获得项目内数据。
"""
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session
from repositories.project_repo import ProjectRepo
from core.audit import audit
from models import ProjectIn

router = APIRouter(tags=["项目"])


# ── 项目 CRUD ──
@router.get("/api/projects")
def list_projects(conn=Depends(db_session)):
    return ProjectRepo(conn).list_projects()


@router.get("/api/projects/default")
def get_default_project(conn=Depends(db_session)):
    """取默认项目（会话未显式关联项目时的兜底指针）。

    2026-09-20：默认项目改为**允许未设置** —— settings.default_project_id 置空即表示
    「无显式项目关联时不注入项目宪法」（配套 agent/pipeline_parts/memory.py 的
    `if not pid: return ""` 分支）。「未设置」是合法状态而非错误，用 404 表达会让调用方
    无法区分「从未配置」与「指向的项目已被删/归档」，故统一 200 + 空态对象。
    前端沿用既有判定即可：13-reports.js 用 `if(p && p.name)`、30-agents.js 用 `def.id` 兜底。
    """
    repo = ProjectRepo(conn)
    pid = (repo.get_default_project_id() or "").strip()
    proj = repo.get_project(pid) if pid else None
    if not proj:
        return {"id": "", "name": "", "code": "", "unset": True}
    return proj


@router.post("/api/projects/default")
def set_default_project(body: dict, conn=Depends(db_session)):
    pid = (body or {}).get("project_id", "")
    repo = ProjectRepo(conn)
    if not repo.get_project(pid):
        return JSONResponse({"error": "Project not found"}, 404)
    repo.set_default_project(pid)
    audit("王工", "project_switch", f"切换默认项目: {pid}", conn=conn)
    return {"ok": True, "default_project_id": pid}


@router.get("/api/projects/{project_id}")
def get_project(project_id: str, conn=Depends(db_session)):
    proj = ProjectRepo(conn).get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Not found"}, 404)
    return proj


@router.post("/api/projects")
def create_project(p: ProjectIn, conn=Depends(db_session)):
    repo = ProjectRepo(conn)
    if repo.get_project(p.id) or repo.get_project_by_code(p.code):
        return JSONResponse({"error": "id/code already exists"}, 409)
    repo.create_project(p.id, p.name, p.code, p.domain, p.description,
                        p.scenario_template_id, p.ontology_profile_id)
    audit("王工", "project_create", f"创建项目: {p.name}({p.id})", conn=conn)
    return {"ok": True}


@router.put("/api/projects/{project_id}")
def update_project(project_id: str, body: dict, conn=Depends(db_session)):
    repo = ProjectRepo(conn)
    if not repo.get_project(project_id):
        return JSONResponse({"error": "Not found"}, 404)
    repo.update_project(
        project_id,
        name=(body or {}).get("name"),
        domain=(body or {}).get("domain"),
        description=(body or {}).get("description"),
        scenario_template_id=(body or {}).get("scenario_template_id"),
        ontology_profile_id=(body or {}).get("ontology_profile_id"),
        status=(body or {}).get("status"),
    )
    return {"ok": True}


# ── 项目隔离查询（实体/图谱按 project_id 过滤）──
@router.get("/api/projects/{project_id}/entities")
def project_entities(project_id: str, status: Optional[str] = None,
                     search: Optional[str] = None, conn=Depends(db_session)):
    return ProjectRepo(conn).project_entities(project_id, status, search)


@router.get("/api/projects/{project_id}/graph")
def project_graph(project_id: str, branch: Optional[str] = None, conn=Depends(db_session)):
    return ProjectRepo(conn).project_graph(project_id, branch or "dev")


# ── 场景模板 ──
@router.get("/api/scenario-templates")
def list_templates(conn=Depends(db_session)):
    return ProjectRepo(conn).list_scenario_templates()


@router.get("/api/scenario-templates/{template_id}")
def get_template(template_id: str, conn=Depends(db_session)):
    t = ProjectRepo(conn).get_scenario_template(template_id)
    if not t:
        return JSONResponse({"error": "Not found"}, 404)
    return t


# ── 本体 Profile ──
@router.get("/api/ontology-profiles")
def list_profiles(scenario_template_id: Optional[str] = None, conn=Depends(db_session)):
    return ProjectRepo(conn).list_ontology_profiles(scenario_template_id)


@router.get("/api/ontology-profiles/{profile_id}")
def get_profile(profile_id: str, conn=Depends(db_session)):
    p = ProjectRepo(conn).get_ontology_profile(profile_id)
    if not p:
        return JSONResponse({"error": "Not found"}, 404)
    return p


# ── 项目级持久记忆（Project Constitution）：规范/基线/决策/经验，AI 会话每次注入防漂移 ──
@router.get("/api/projects/{project_id}/memories")
def list_project_memories(project_id: str, category: str | None = None,
                          all: int = 0, conn=Depends(db_session)):
    """项目记忆列表：默认仅启用（注入用）；all=1 返回全部（管理页含停用）。"""
    repo = ProjectRepo(conn)
    if not repo.get_project(project_id):
        return JSONResponse({"error": "项目不存在"}, 404)
    return repo.list_project_memories(project_id, category, only_enabled=not all)


@router.post("/api/projects/{project_id}/memories")
def create_project_memory(project_id: str, body: dict, conn=Depends(db_session)):
    repo = ProjectRepo(conn)
    if not repo.get_project(project_id):
        return JSONResponse({"error": "项目不存在"}, 404)
    title = ((body or {}).get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "标题必填"}, 400)
    category = ((body or {}).get("category") or "规范").strip()
    if category not in ("规范", "基线", "决策", "经验"):
        return JSONResponse({"error": "类别必须为 规范/基线/决策/经验"}, 400)
    mem_id = repo.create_project_memory(
        project_id, category, title[:120], str((body or {}).get("content") or "")[:4000],
        created_by=(body or {}).get("created_by") or "王工")
    audit("王工", "project_memory_create",
          f"项目{project_id} 新增记忆[{category}]: {title[:30]}", conn=conn)
    return {"ok": True, "id": mem_id}


@router.put("/api/projects/{project_id}/memories/{mem_id}")
def update_project_memory(project_id: str, mem_id: int, body: dict, conn=Depends(db_session)):
    repo = ProjectRepo(conn)
    mem = repo.get_project_memory(mem_id)
    if not mem:
        return JSONResponse({"error": "记忆不存在"}, 404)
    title = ((body or {}).get("title") or "").strip()
    if "title" in (body or {}) and not title:
        return JSONResponse({"error": "标题不能为空"}, 400)
    category = ((body or {}).get("category") or "").strip()
    if category and category not in ("规范", "基线", "决策", "经验"):
        return JSONResponse({"error": "类别必须为 规范/基线/决策/经验"}, 400)
    repo.update_project_memory(
        mem_id,
        category=category or None,
        title=title or None,
        content=str((body or {}).get("content")).strip() if (body or {}).get("content") is not None else None,
        enabled=int((body or {}).get("enabled")) if (body or {}).get("enabled") is not None else None,
    )
    audit("王工", "project_memory_update", f"项目{project_id} 更新记忆#{mem_id}", conn=conn)
    return {"ok": True}


@router.delete("/api/projects/{project_id}/memories/{mem_id}")
def delete_project_memory(project_id: str, mem_id: int, conn=Depends(db_session)):
    repo = ProjectRepo(conn)
    if not repo.get_project_memory(mem_id):
        return JSONResponse({"error": "记忆不存在"}, 404)
    repo.delete_project_memory(mem_id)
    audit("王工", "project_memory_delete", f"项目{project_id} 删除记忆#{mem_id}", conn=conn)
    return {"ok": True}
