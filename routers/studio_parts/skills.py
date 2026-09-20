# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：技能 CRUD/上传/发布。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/skills")
def list_skills(status: str = "", conn=Depends(db_session)):
    """技能列表（P1-3 草稿箱：status=draft|draft_revision 筛选 AI 沉淀/自改进草稿）。"""
    items = StudioRepo(conn).list_skills()
    if status:
        items = [x for x in items if (x.get("status") or "") == status]
    return items


@router.post("/api/studio/skills")
def create_skill(body: SkillIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    if repo.get_skill_by_name(body.name):
        return JSONResponse({"error": f"Skill 名称已存在: {body.name}"}, 400)
    sid = repo.create_skill(
        body.name, body.description, body.skill_type,
        json.dumps(body.triggers, ensure_ascii=False), body.category,
        body.content, body.frontmatter, "",
        dependencies=json.dumps(body.dependencies or [], ensure_ascii=False),
        allowed_roles=json.dumps(body.allowed_roles or [], ensure_ascii=False),
        allowed_tools=json.dumps(body.allowed_tools or [], ensure_ascii=False),
        references=json.dumps(body.references or [], ensure_ascii=False),
        examples=json.dumps(body.examples or [], ensure_ascii=False),
        scripts=json.dumps(body.scripts or [], ensure_ascii=False),
    )
    audit(audit_user(user), "skill_create", f"创建Skill: {body.name}", conn=conn)
    return {"ok": True, "id": sid}


@router.put("/api/studio/skills/{sid}")
def update_skill(sid: int, body: SkillIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    if not repo.get_skill(sid):
        return JSONResponse({"error": "Skill not found"}, 404)
    repo.update_skill(
        sid, body.name, body.description, body.skill_type,
        json.dumps(body.triggers, ensure_ascii=False), body.category,
        body.content, body.frontmatter,
        dependencies=json.dumps(body.dependencies or [], ensure_ascii=False),
        allowed_roles=json.dumps(body.allowed_roles or [], ensure_ascii=False),
        allowed_tools=json.dumps(body.allowed_tools or [], ensure_ascii=False),
        references=json.dumps(body.references or [], ensure_ascii=False),
        examples=json.dumps(body.examples or [], ensure_ascii=False),
        scripts=json.dumps(body.scripts or [], ensure_ascii=False),
    )
    audit(audit_user(user), "skill_update", f"更新Skill#{sid}: {body.name}", conn=conn)
    return {"ok": True}


@router.delete("/api/studio/skills/{sid}")
def delete_skill(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    s = repo.get_skill(sid)
    if not s:
        return JSONResponse({"error": "Skill not found"}, 404)
    if s.get("builtin"):
        return JSONResponse({"error": "内置 Skill 禁止删除，可「编辑」修改配置"}, 400)
    repo.delete_skill(sid)
    audit(audit_user(user), "skill_delete", f"删除Skill#{sid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/skills/{sid}/enable")
def enable_skill(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """启用 Skill（停用的技能不参与触发匹配与绑定注入）。"""
    repo = StudioRepo(conn)
    if not repo.get_skill(sid):
        return JSONResponse({"error": "Skill not found"}, 404)
    repo.set_skill_enabled(sid, 1)
    audit(audit_user(user), "skill_enable", f"启用Skill#{sid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/skills/{sid}/disable")
def disable_skill(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """停用 Skill（停用的技能不参与触发匹配与绑定注入）。"""
    repo = StudioRepo(conn)
    if not repo.get_skill(sid):
        return JSONResponse({"error": "Skill not found"}, 404)
    repo.set_skill_enabled(sid, 0)
    audit(audit_user(user), "skill_disable", f"停用Skill#{sid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/skills/{sid}/publish")
def publish_skill(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    skill = repo.get_skill(sid)
    if not skill:
        return JSONResponse({"error": "Skill not found"}, 404)
    # SK-RL：发布前依赖校验——依赖技能必须存在且已发布（版本满足 min_version）
    missing, unpub, ver_low = [], [], []
    for dep in skill.get("dependencies") or []:
        dname = dep.get("name") if isinstance(dep, dict) else dep
        dmin = dep.get("min_version") if isinstance(dep, dict) else ""
        target = repo.get_skill_by_name(dname)
        if not target:
            missing.append(dname); continue
        if target.get("status") != "published":
            unpub.append(dname); continue
        if dmin and not _version_ge(target.get("version", ""), dmin):
            ver_low.append(f"{dname}（{target.get('version')} < {dmin}）")
    if missing or unpub or ver_low:
        err = []
        if missing: err.append(f"依赖不存在: {', '.join(missing)}")
        if unpub: err.append(f"依赖未发布: {', '.join(unpub)}")
        if ver_low: err.append(f"依赖版本过低: {', '.join(ver_low)}")
        return JSONResponse({"error": "发布校验失败：" + "；".join(err)}, 400)
    repo.publish_skill(sid)
    audit(audit_user(user), "skill_publish", f"发布Skill#{sid}: {skill['name']}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/skills/upload")
def upload_skill(file: UploadFile = File(...), conn=Depends(db_session), user=Depends(current_user)):
    """上传 ZIP 技能包（P0 平台化）：解析 skill.md frontmatter + 解压 scripts 到 static/skill_packages/。

    返回解析预览（name/triggers/files...），前端确认后由 POST /api/studio/skills 入库（package_path 已落盘）。
    """
    repo = StudioRepo(conn)
    try:
        data = file.file.read()
    except Exception as e:
        return JSONResponse({"error": f"读取文件失败: {e}"}, 400)
    if len(data) > 20 * 1024 * 1024:
        return JSONResponse({"error": "技能包超过 20MB 上限"}, 413)
    try:
        parsed = parse_skill_zip(data, file.filename or "skill.zip")
    except SkillParseError as e:
        return JSONResponse({"error": str(e)}, 400)

    # 解压到 static/skill_packages/{uuid}/
    pkg_dir = os.path.join(STATIC_DIR, "skill_packages", uuid.uuid4().hex[:12])
    saved = extract_skill_package(data, pkg_dir)
    parsed["package_path"] = pkg_dir
    parsed["saved_files"] = saved
    parsed["preview"] = {
        "name": parsed["name"],
        "description": parsed["description"],
        "triggers": parsed["triggers"],
        "category": parsed["category"],
        "author": parsed["author"],
        "version": parsed["version"],
        "file_count": len(saved),
    }
    audit(audit_user(user), "skill_upload", f"上传技能包: {parsed['name']} ({len(saved)} 文件)", conn=conn)
    return parsed
