# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：技能 CRUD/上传/发布。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


def _normalize_trg(triggers):
    """triggers 规范化 → `(列表, 警告列表, 是否变化)`。

    ★ 2026-10-29（P1-4）：接 `core/skill_triggers`。
    取 `strict=False`（自动拆分 + 告警，不阻断）：历史数据与第三方导入可能已是此形态，
    硬拦会让既有流程挂掉；从源头自动修掉更实际。
    ⚠️ 校验器不可用时**退回原值**（不因辅助模块异常挡住技能保存）。
    """
    try:
        from core.skill_triggers import normalize_triggers as _nt
        return _nt(triggers, strict=False)
    except Exception:
        try:
            return list(triggers or []), [], False
        except Exception:
            return [], [], False


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
    # ★ 2026-10-29（P1-4）：triggers 入库前规范化。
    #   匹配是**逐元素子串**，把「变更、调整、更新、影响」当**一个元素**会让该技能
    #   几乎永不命中（实测技能 id=47 因此长期未触发）。此处自动拆分并返回告警，
    #   让问题在写入时就被看见，而不是事后靠巡检发现"这个技能好像没用"。
    _trg, _trg_warn, _trg_changed = _normalize_trg(body.triggers)
    if _trg_changed:
        try:
            audit(audit_user(user), "skill_triggers_normalized",
                  f"创建Skill: {body.name}；triggers 已自动拆分（原含分隔符清单）", conn=conn)
        except Exception:
            pass
    sid = repo.create_skill(
        body.name, body.description, body.skill_type,
        json.dumps(_trg, ensure_ascii=False), body.category,
        body.content, body.frontmatter, "",
        dependencies=json.dumps(body.dependencies or [], ensure_ascii=False),
        allowed_roles=json.dumps(body.allowed_roles or [], ensure_ascii=False),
        allowed_tools=json.dumps(body.allowed_tools or [], ensure_ascii=False),
        references=json.dumps(body.references or [], ensure_ascii=False),
        examples=json.dumps(body.examples or [], ensure_ascii=False),
        scripts=json.dumps(body.scripts or [], ensure_ascii=False),
        # ★ 2026-11-20：记录创建者，供同步桥解析 plugins.author_id。
        #   未登录（兼容期匿名自检请求）不写 "未登录" 这种假归属，留空 ⇒ 维持本地创建口径。
        owner=str((user or {}).get("username") or ""),
    )
    audit(audit_user(user), "skill_create", f"创建Skill: {body.name}", conn=conn)
    return {"ok": True, "id": sid, "trigger_warnings": _trg_warn}


@router.put("/api/studio/skills/{sid}")
def update_skill(sid: int, body: SkillIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    if not repo.get_skill(sid):
        return JSONResponse({"error": "Skill not found"}, 404)
    # ★ 2026-10-29（P1-4）：同 create，编辑时也规范化（老脏数据经一次编辑即被修好）
    _trg, _trg_warn, _trg_changed = _normalize_trg(body.triggers)
    # ★ 2026-11-20 归属自愈：2026-11-20 之前创建的技能没有 owner（skills 表该列从未写入）
    #   ⇒ 同步桥只能给 plugins.author_id=0 ⇒ 创建者本人也不是「作者」（is_mine=False）
    #   ⇒ 卡片上没有编辑/发布/删除，只有一个「停用」按钮（用户本轮投诉的死锁态）。
    #   规则：**仅私有（personal/private）且当前无归属**时，由本次编辑者接管 ——
    #   私有条目本来只有他自己能看到，不存在"抢别人作品"的问题；public/pending 不动。
    #   ⚠️ 两个词表不一致：`skills.scope` 用 private，`plugins.scope` 用 personal，
    #      两边都要认（此前只写 personal，实测本表默认值是 private ⇒ 永不命中）。
    #   必须在 repo.update_skill 之前写，它内部的 _sync 才会带上新 owner。
    _who = str((user or {}).get("username") or "").strip()
    if _who:
        try:
            conn.execute(
                "UPDATE skills SET owner=? WHERE id=? AND scope IN ('personal','private') "
                "AND (owner IS NULL OR owner='')", (_who, sid))
        except Exception:
            pass
    repo.update_skill(
        sid, body.name, body.description, body.skill_type,
        json.dumps(_trg, ensure_ascii=False), body.category,
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
    #   —— 该职责由 studio_repo.update_skill 内的 asset_audit.audited_update 承担
    #   （按真实 diff 计算 version_from→version_to，无 diff 不递增）。
    # ★ 2026-11-20 修：此处原有 `_bump_version(s.get("version"))`，但 `s` 在本作用域
    #   从未定义 ⇒ 恒 NameError → 被 except 静默吞掉。后果不是"版本多跳一次"，
    #   而是"这行代码从来没跑过"，却让人误以为这里有第二处递增。删除死代码。
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
    #
    # ★★ 2026-11-06 修一个**会静默毁掉绑定**的缺陷（本次清理同名草稿时实测发现）：
    #   上面那条 `DELETE ... WHERE tool_name=?` 只按 name 清，**不看同名还剩几份**。
    #   而 name 本来就不唯一（实测「智源平台管理」有 3 份：1 份 published + 2 份
    #   draft_revision），于是**删掉一份草稿就会把正式版的绑定全部清空** ——
    #   实测：删 id=133（草稿）时清掉 3 条绑定，而 id=95（published）仍安然在库。
    #   用户视角是"我删了个重复项，怎么技能全没了"。
    #   正确语义：绑定的是**技能名**这一层关系；只要删除后同名还有任何一份存在，
    #   绑定就仍然有效（运行时按 enabled→published→id 选一份注入），不能清。
    #   仅当删完后同名**一份都不剩**，绑定才会真正悬空，此时才清理。
    try:
        _others = conn.execute(
            "SELECT COUNT(*) FROM skills WHERE name=? AND id<>?",
            (s.get("name"), sid)).fetchone()[0]
        if _others:
            _cleared = 0    # 还有同名副本（正式版/其它草稿）⇒ 绑定保持有效，一条不清
        else:
            _cleared = conn.execute(
                "DELETE FROM agent_tools WHERE tool_type='skill' AND tool_name=?",
                (s.get("name"),)).rowcount
    except Exception as e:
        return JSONResponse({"error": f"清理技能绑定失败：{e}"}, 500)
    repo.delete_skill(sid)
    audit(audit_user(user), "skill_delete",
          f"删除Skill#{sid}（同名剩余 {_others} 份；级联清绑定 {_cleared} 条）", conn=conn)
    return {"ok": True, "same_name_left": _others, "bindings_cleared": _cleared}


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
