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

# ── 建模工具适配层（2026-09-24 泛化：对接不限于智源，也支持 MagicDraw）──
# tool_ref 语义：zhiyuan = vc(branchId,queryType)；magicdraw = TWC 工程ID/工程名/本地工程文件标识
MODELING_TOOLS = {"zhiyuan": "智源", "magicdraw": "MagicDraw"}


def build_tool_binding(tool: str, ref: str, name: str) -> str:
    """构建 projects.tool_binding JSON；空 tool = 本地建模 → 返回空串。非法 tool/缺 ref 抛 ValueError。"""
    import json as _json
    tool = (tool or "").strip().lower()
    ref = (ref or "").strip()
    if not tool:
        return ""
    if tool not in MODELING_TOOLS:
        raise ValueError("不支持的建模工具: %s（当前支持：%s）"
                         % (tool, " / ".join(f"{v}({k})" for k, v in MODELING_TOOLS.items())))
    if not ref:
        raise ValueError("工具侧工程标识（ref）不能为空")
    return _json.dumps({"tool": tool, "ref": ref, "name": (name or "").strip()}, ensure_ascii=False)


# ── 项目数据来源（2026-09-24）：本地工作空间 / 远端 SSH 连接 ──
DATA_SOURCES = {"local": "本地", "remote": "远端"}
REMOTE_AUTH_MODES = {"none": "无身份验证", "key": "身份文件"}


def build_data_source(source: str, workspace: str, remote: dict) -> tuple[str, str, str]:
    """校验并构建 (source, workspace, remote_json)。

    · source='local'  → workspace 必填（本地工作空间绝对路径，「选本地文件夹」写入）；
    · source='remote' → remote 必填 host（显示名称缺省取 host）；auth_mode='key' 时必填身份文件路径。
    非法输入抛 ValueError（router 转 400），不静默兜底。
    """
    import json as _json
    src = (source or "").strip().lower() or "local"
    if src not in DATA_SOURCES:
        raise ValueError("不支持的数据来源: %s（当前支持：local 本地 / remote 远端）" % src)
    if src == "local":
        ws = (workspace or "").strip()
        if not ws:
            raise ValueError("本地项目必须指定本地工作空间（选本地文件夹）")
        return "local", ws, ""
    r = remote or {}
    host = (r.get("host") or "").strip()
    if not host:
        raise ValueError("远端连接必须填写主机名（host.com 或 user@host.com）")
    port = str(r.get("port") or "").strip()
    if port and not port.isdigit():
        raise ValueError("SSH 端口必须是数字（或不填）")
    mode = (r.get("auth_mode") or "").strip().lower() or "none"
    if mode not in REMOTE_AUTH_MODES:
        raise ValueError("不支持的认证方式: %s（当前支持：none 无身份验证 / key 身份文件）" % mode)
    ident = (r.get("identity_file") or "").strip()
    if mode == "key" and not ident:
        raise ValueError("认证方式为「身份文件」时必须填写身份文件路径")
    payload = {"display_name": (r.get("display_name") or "").strip() or host,
               "host": host, "port": port, "auth_mode": mode,
               "identity_file": ident if mode == "key" else ""}
    return "remote", "", _json.dumps(payload, ensure_ascii=False)


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


@router.get("/api/projects/resolve")
def resolve_project(tool: str = "", ref: str = "", project_id: str = "",
                    conn=Depends(db_session)):
    """由「建模工具侧工程」反查本地项目（多工程 P0-1，2026-09-28）。

    ⚠️ 本端点**必须注册在 `/api/projects/{project_id}` 之前** —— FastAPI 按注册顺序匹配，
    否则 `/api/projects/resolve` 会被 `{project_id}` 当成 id="resolve" 吃掉（返回 404 Not found）。

    入参（二选一）：
      · `project_id` —— 已知本地项目 id（建模工具若已保存过映射，直接带它）；
      · `tool` + `ref` —— 工具侧工程标识（tool ∈ MODELING_TOOLS，ref = 该工具内的工程标识）。
    出参恒 200（未命中是**合法状态**，不是错误）：
      `{"matched": bool, "project_id": str, "project": dict|None,
        "candidates": [同工具的已绑定项目], "reason": str}`
      reason：`project_id` / `tool_ref` / `not_bound`（该工具下无任何绑定）/
              `ref_mismatch`（绑定了同工具但 ref 不同）/ `missing_param`。
    未命中**不猜**：不回退到默认项目、不取第一个项目（「未匹配到就空着」政策）。
    """
    from repositories.project_repo import resolve_project_by_tool as _resolve
    repo = ProjectRepo(conn)
    pid = (project_id or "").strip()
    if pid:
        proj = repo.get_project(pid)
        if proj:
            return {"matched": True, "project_id": pid, "project": proj,
                    "candidates": [], "reason": "project_id"}
        return {"matched": False, "project_id": "", "project": None, "candidates": [],
                "reason": "project_not_found"}
    t = (tool or "").strip().lower()
    r = (ref or "").strip()
    if not t or not r:
        return {"matched": False, "project_id": "", "project": None, "candidates": [],
                "reason": "missing_param"}
    if t not in MODELING_TOOLS:
        return JSONResponse({"error": "不支持的建模工具: %s（当前支持：%s）"
                                      % (t, " / ".join(f"{v}({k})" for k, v in MODELING_TOOLS.items()))},
                            400)
    hit, cands = _resolve(conn, t, r)
    if hit:
        return {"matched": True, "project_id": hit, "project": repo.get_project(hit),
                "candidates": cands, "reason": "tool_ref"}
    return {"matched": False, "project_id": "", "project": None, "candidates": cands,
            "reason": "not_bound" if not cands else "ref_mismatch"}


@router.get("/api/projects/{project_id}")
def get_project(project_id: str, conn=Depends(db_session)):
    proj = ProjectRepo(conn).get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Not found"}, 404)
    return proj


@router.post("/api/projects")
def create_project(p: ProjectIn, conn=Depends(db_session)):
    repo = ProjectRepo(conn)
    # 2026-09-24：id/code 可缺省自动生成 —— 前端创建项目弹窗（Codex 风格）只要求填名称
    import time as _time
    pid = (p.id or "").strip() or ("p" + format(int(_time.time() * 1000), "x"))
    code = (p.code or "").strip() or p.name
    if repo.get_project(pid) or repo.get_project_by_code(code):
        return JSONResponse({"error": "id/code already exists"}, 409)
    try:
        binding = build_tool_binding(p.tool, p.tool_ref, p.tool_name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    try:
        source, workspace, remote = build_data_source(
            p.source, p.workspace,
            {"display_name": p.remote_display_name, "host": p.remote_host, "port": p.remote_port,
             "auth_mode": p.remote_auth_mode, "identity_file": p.remote_identity_file})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    repo.create_project(pid, p.name, code, p.domain, p.description,
                        p.scenario_template_id, p.ontology_profile_id,
                        tool_binding=binding, source=source, workspace=workspace, remote=remote)
    _src_desc = ("本地工作空间 " + workspace) if source == "local" else (
        "远端 SSH " + (p.remote_display_name or p.remote_host))
    audit("王工", "project_create", f"创建项目: {p.name}({pid})（{_src_desc}）"
          + (f" 绑定建模工具 {p.tool} 工程 {p.tool_name}(ref={p.tool_ref})" if binding else "（未绑定建模工具）"),
          conn=conn)
    return {"ok": True, "id": pid, "code": code, "source": source}


@router.put("/api/projects/{project_id}")
def update_project(project_id: str, body: dict, conn=Depends(db_session)):
    repo = ProjectRepo(conn)
    cur = repo.get_project(project_id)
    if not cur:
        return JSONResponse({"error": "Not found"}, 404)
    b = (body or {})
    try:
        binding = build_tool_binding(b.get("tool") or "", b.get("tool_ref") or "",
                                     b.get("tool_name") or "") \
            if any(k in b for k in ("tool", "tool_ref", "tool_name")) else None
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    # 数据来源：仅当请求显式带了 source / workspace / remote_* 时才校验并写回，
    # 避免「只改名称」这类局部更新被数据来源校验拦下（老项目无 workspace 是合法现状）。
    source = workspace = remote = None
    if any(k in b for k in ("source", "workspace", "remote", "remote_host")):
        try:
            source, workspace, remote = build_data_source(
                b.get("source") or cur.get("source") or "local", b.get("workspace"),
                b.get("remote") if isinstance(b.get("remote"), dict) else {
                    "display_name": b.get("remote_display_name"), "host": b.get("remote_host"),
                    "port": b.get("remote_port"), "auth_mode": b.get("remote_auth_mode"),
                    "identity_file": b.get("remote_identity_file")})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)
    repo.update_project(
        project_id,
        name=b.get("name"),
        domain=b.get("domain"),
        description=b.get("description"),
        scenario_template_id=b.get("scenario_template_id"),
        ontology_profile_id=b.get("ontology_profile_id"),
        status=b.get("status"),
        tool_binding=binding,   # 建模工具绑定（{tool,ref,name} JSON；空 tool=解绑）
        source=source, workspace=workspace, remote=remote,   # 数据来源（本地工作空间 / 远端 SSH）
    )
    return {"ok": True}


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: str, conn=Depends(db_session)):
    """移除项目。**只移除容器，不删内容**：其下会话解绑（project_id 置空）后仍留在「任务」列表。

    「项目 = 任务的容器」是本次左侧项目管理优化的既定语义（2026-09-24 用户拍板），
    级联删会话会让用户误删几十轮建模历史，故不做级联，只解绑并回报影响面。

    另：若被移除的正是「当前工程」（settings.default_project_id），必须**一并清空该指针**——
    否则默认项目会悬空指向已不存在的项目，表现为顶栏「⚠ 未匹配工程」、且前端「当前工程」
    高亮/自动展开全部失效（实测踩到：删项目后 /api/projects/default 返回 unset，
    项目组不再自动展开）。置空是既有合法状态（见 get_default_project 的 unset 语义）。
    """
    repo = ProjectRepo(conn)
    proj = repo.get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Not found"}, 404)
    detached = repo.unassign_project_tasks(project_id)
    cleared_default = (repo.get_default_project_id() or "").strip() == project_id
    if cleared_default:
        repo.set_default_project("")
    repo.delete_project(project_id)
    audit("王工", "project_delete",
          f"移除项目: {proj.get('name')}({project_id})（解绑任务 {detached} 个，任务与消息保留"
          + ("；该项目原为当前工程，已清除当前工程指针" if cleared_default else "") + "）",
          conn=conn)
    return {"ok": True, "detached_tasks": detached, "cleared_default": cleared_default}


@router.get("/api/projects/{project_id}/task-count")
def project_task_count(project_id: str, conn=Depends(db_session)):
    """项目下任务数——「移除项目」确认框的影响面提示（移除前先告知会解绑几个任务）。"""
    repo = ProjectRepo(conn)
    if not repo.get_project(project_id):
        return JSONResponse({"error": "Not found"}, 404)
    return {"project_id": project_id, "tasks": repo.count_project_tasks(project_id)}


@router.post("/api/projects/pick-folder")
def pick_local_folder(body: dict = None):
    """弹出本机「选择文件夹」对话框，返回绝对路径（供项目「本地工作空间」选择）。

    私有化离线部署 + 单用户本机运行，故用 Windows 原生 FolderBrowserDialog（PowerShell -STA）
    而非浏览器沙箱里的 webkitdirectory —— 后者只能拿到相对路径，拿不到可落库的绝对路径。
    用户在对话框点取消 → ok=false + cancelled=true（不是错误，前端静默即可）；
    对话框不可用（非 Windows / 无 GUI）→ ok=false + error，前端保留手填路径兜底。
    """
    import platform
    import subprocess
    if platform.system() != "Windows":
        return {"ok": False, "error": "当前系统不支持原生文件夹选择，请手动输入路径"}
    initial = ((body or {}).get("initial") or "").strip().replace('"', "")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '选择本地工作空间';"
        "$d.ShowNewFolderButton = $true;"
        + (f"$d.SelectedPath = '{initial}';" if initial else "")
        + "$r = $d.ShowDialog();"
        "if($r -eq [System.Windows.Forms.DialogResult]::OK){"
        "  [Console]::Out.Write('OK|' + $d.SelectedPath)"
        "} else { [Console]::Out.Write('CANCEL') }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, timeout=300)
        txt = (out.stdout or b"").decode("utf-8", "replace").strip()
    except Exception as e:
        return {"ok": False, "error": f"文件夹选择对话框不可用：{type(e).__name__}: {e}"}
    if txt.startswith("OK|"):
        path = txt[3:].strip()
        return {"ok": True, "path": path} if path else {"ok": False, "cancelled": True}
    if "CANCEL" in txt:
        return {"ok": False, "cancelled": True}
    return {"ok": False, "error": "文件夹选择未返回结果，请手动输入路径"}


# ── 智源工程清单（「创建项目 → 绑定智源工程」选择器数据源）──
def _extract_zhiyuan_projects(payload, depth: int = 0, out: list | None = None) -> list:
    """防御式从 project_list 响应中提取工程条目。

    ⚠️ project_list 响应结构未实测校准（智源服务 2026-09-24 仍 502），按常见键名扫描：
    dict 含「名称类」键且含「id/分支类」键之一即视为工程条目；
    vc 优先取条目自带 vc，其次 branchId+queryType 拼接（vc 格式 = branchId,queryType）。
    """
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(payload, list):
        for it in payload:
            _extract_zhiyuan_projects(it, depth + 1, out)
        return out
    if isinstance(payload, dict):
        name = next((payload[k] for k in ("name", "projectName", "project_name", "title")
                     if isinstance(payload.get(k), str) and payload[k].strip()), "")
        pid = next((str(payload[k]) for k in ("projectId", "project_id", "id", "dataId")
                    if payload.get(k) not in (None, "")), "")
        branch = next((str(payload[k]) for k in ("branchId", "branch_id")
                       if payload.get(k) not in (None, "")), "")
        vc = payload.get("vc") if isinstance(payload.get("vc"), str) and payload.get("vc").strip() else ""
        if name or pid or branch:
            if not vc and branch.strip():
                vc = f"{branch},{payload.get('queryType', 0)}"
            if vc or pid:
                item = {"name": name or pid, "project_id": pid, "branch_id": branch, "vc": vc}
                if not any(x["vc"] == item["vc"] and x["name"] == item["name"] for x in out):
                    out.append(item)
        for v in payload.values():
            _extract_zhiyuan_projects(v, depth + 1, out)
    return out


@router.get("/api/zhiyuan/projects")
def list_zhiyuan_projects():
    """智源工程清单，供「创建项目/绑定智源工程」下拉选择（替代手填 vc）。

    解析失败不报 5xx —— 返回 ok=false + error + raw（截断原文），前端据此降级为手填 vc。
    """
    import json as _json
    from zhiyuan_client import ZhiyuanClient, _load_config
    cfg = _load_config()
    if not isinstance(cfg, dict) or not (cfg.get("base_url") or "").strip():
        return {"ok": False, "error": "智源连接未配置（zhiyuan.base_url 缺失）", "projects": []}
    space_vc = (cfg.get("default_vc") or "").strip()
    if not space_vc:
        return {"ok": False, "error": "未配置智源空间上下文（zhiyuan.default_vc，格式 branchId,queryType，如 1,0）",
                "projects": []}
    client = ZhiyuanClient(base_url=cfg.get("base_url", ""), token=cfg.get("token", ""),
                           headers=cfg.get("headers"), timeout=int(cfg.get("timeout") or 15))
    try:
        pl = client.project_list(space_vc)
    except Exception as e:
        return {"ok": False, "error": f"智源接口调用失败: {e}", "projects": []}
    if not pl.get("ok"):
        return {"ok": False, "error": str(pl.get("result") or "智源工程列表查询失败")[:300], "projects": []}
    try:
        payload = _json.loads(pl.get("result") or "null")
    except Exception:
        payload = None
    projects = _extract_zhiyuan_projects(payload)
    return {"ok": True, "space_vc": space_vc, "projects": projects,
            "raw": (pl.get("result") or "")[:1200]}


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
