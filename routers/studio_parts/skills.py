# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：技能 CRUD/上传/发布。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


def _bump_version(v):
    """技能版本 patch 级递增（P2-3）。容忍 'v' 前缀与非标写法，绝不返回空。"""
    import re
    v = (v or "").strip() or "1.0.0"
    _m = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)$", v)
    if _m:
        return "%s%s.%s.%d" % (_m.group(1), _m.group(2), _m.group(3), int(_m.group(4)) + 1)
    return v + ".1"


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
        actor=audit_user(user), source="api",
    )
    # P2-3（2026-10-27）：编辑技能即 bump 版本（patch 级），让"哪个版本表现如何"可观测
    #   （文章 7 项可观测要求之一：Skill 及版本）。registry 已投影 skills.version。
    try:
        conn.execute("UPDATE skills SET version=? WHERE id=?", (_bump_version(s.get("version")), sid))
    except Exception:
        pass
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
    # P1-5（2026-10-27）：删技能**级联清绑定** —— 此前只删 skills 行，agent_tools 里的
    #   技能绑定变孤儿（当前孤儿为 0 靠清理脚本，非机制保证）。删前先清，让"无孤儿绑定"
    #   成为机制保证，而非依赖一次性脚本。用原名匹配（绑定存的是技能 name）。
    try:
        _cleared = conn.execute(
            "DELETE FROM agent_tools WHERE tool_type='skill' AND tool_name=?", (s.get("name"),)).rowcount
    except Exception as e:
        return JSONResponse({"error": f"清理技能绑定失败：{e}"}, 500)
    repo.delete_skill(sid)
    audit(audit_user(user), "skill_delete", f"删除Skill#{sid}（级联清绑定 {_cleared} 条）", conn=conn)
    return {"ok": True}


@router.post("/api/studio/skills/{sid}/enable")
def enable_skill(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """启用 Skill（停用的技能不参与触发匹配与绑定注入）。"""
    repo = StudioRepo(conn)
    s = repo.get_skill(sid)
    if not s:
        return JSONResponse({"error": "Skill not found"}, 404)
    # P2-1（2026-10-27）：启用语义自洽。全局技能池准入 = status='published' AND enabled=1；
    #   一个 status='draft' 却 enabled=1 的技能是矛盾态（开不了却显示已启用，走查记 P2-1）。
    #   故启用时若仍是 draft，自动置为 published（启用即视为可消费）；非 draft 不动 status。
    #   （注：不重跑发布依赖校验 —— 它本就 enabled=1，系统中已按"活跃"对待，这里只是对齐数据。）
    if (s.get("status") or "") == "draft":
        try:
            repo.publish_skill(sid)
        except Exception:
            pass
    repo.set_skill_enabled(sid, 1)
    audit(audit_user(user), "skill_enable", f"启用Skill#{sid}"
          + ("（draft→published 自动对齐）" if (s.get("status") or "") == "draft" else ""), conn=conn)
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
