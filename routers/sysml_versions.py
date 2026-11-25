"""AI 建模 SysML 版本链 API：AI 生成 SysML v2 代码的版本管理与入库候选生成。

- GET  /api/sysml-versions?conversation_id=&artifact_id=   版本链列表（label/status/adopted/摘要）
- GET  /api/sysml-versions/{id}                            版本详情（源码/视图/element_summary/diff）
- POST /api/sysml-versions/{id}/import-candidates          入库 Step1/2：解析 → v2g_candidates（SYSM- 批次）+ 消歧检测

候选确认/驳回/编辑/审核复用既有 v2g 接口（/api/knowledge/v2g/*），不在此重复。

2026-09-24：POST /{id}/adopt（标记采纳版本）已随前端「📌 采纳」入口一并删除——
其唯一"后续"（自动收编产物入资料库）因 sysml_versions.artifact_id 恒为 0 从未实际触发，
工程入库（project_ingest）也不按 adopted 过滤版本；adopted 列保留作历史留痕，不再有写入方。

2026-09-24 方案A（用户拍板）：AI 建模数据链路统一为「归一确认 → 写回智源(push-zhiyuan) → 智源拉取 → 个人分支」，
工程入库直入捷径（片段不入建模工具直接进图库）从 UI 下线——本文件新增 zhiyuan_imported_id
写回状态留痕（migrations/columns.py 补列）+ push-zhiyuan 防重复闸（已写回版本拒绝再次写入）。
工程入库后端端点（/api/knowledge/project-ingest/*）暂保留供历史批次治理，前端入口已删。
"""
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user
from core.audit import audit, audit_user

router = APIRouter(tags=["SysML 版本"])


def _actor(user) -> str:
    """审计操作人：取已认证用户显示名；取不到标注「未登录」（不再编造人名）。"""
    try:
        return audit_user(user)
    except Exception:
        return "未登录"


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("content", "diff", "element_summary"):
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except Exception:
            d[k] = {}
    return d


@router.get("/api/sysml-versions")
def list_sysml_versions(conversation_id: int = Query(0, description="会话 id"),
                        artifact_id: int = Query(0, description="产物 id（可选）"),
                        conn=Depends(db_session)):
    """版本链列表：按会话（或产物）倒序，含 status/adopted/element_summary 摘要。"""
    if conversation_id:
        rows = conn.execute(
            "SELECT id, version_label, status, adopted, element_summary, imported_batch, zhiyuan_imported_id, created_by, created_at, length(code_text) AS code_len "
            "FROM sysml_versions WHERE conversation_id=? ORDER BY id DESC",
            (conversation_id,)).fetchall()
    elif artifact_id:
        rows = conn.execute(
            "SELECT id, version_label, status, adopted, element_summary, imported_batch, zhiyuan_imported_id, created_by, created_at, length(code_text) AS code_len "
            "FROM sysml_versions WHERE artifact_id=? ORDER BY id DESC",
            (artifact_id,)).fetchall()
    else:
        return JSONResponse({"error": "conversation_id 或 artifact_id 必填"}, 400)
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["element_summary"] = json.loads(d.get("element_summary") or "{}")
        except Exception:
            d["element_summary"] = {}
        out.append(d)
    return {"versions": out}


@router.get("/api/sysml-versions/{version_id}")
def get_sysml_version(version_id: int, conn=Depends(db_session)):
    """版本详情：视图/源码 content + element_summary + diff。"""
    row = conn.execute("SELECT * FROM sysml_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "版本不存在"}, 404)
    return _row_to_dict(row)


@router.post("/api/sysml-versions/{version_id}/import-candidates")
def sysml_import_candidates(version_id: int, body: dict = None,
                            conn=Depends(db_session), user=Depends(current_user)):
    """入库 Step1/2：解析 SysML → 候选写入 v2g_candidates（SYSM- 批次，不入库）+ 消歧检测。

    body 可选：{content, source: "text|json", model_name}；缺省用版本快照 content（sysml_views JSON）。
    """
    from services.sysml_importer import sysml_to_candidates
    body = body or {}
    row = conn.execute("SELECT * FROM sysml_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "版本不存在"}, 404)
    content = body.get("content")
    source = body.get("source") or "text"
    model_name = str(body.get("model_name") or f"会话{row['conversation_id']}")[:60]
    if content is None:
        # 缺省：用版本快照（sysml_views 结构化 JSON）
        try:
            content = json.loads(row["content"] or "{}")
        except Exception:
            content = None
        if content:
            source = "json"
    if content is None or (isinstance(content, str) and not content.strip()):
        return JSONResponse({"error": "候选生成失败：无 SysML 内容（content 或版本快照为空）"}, 400)
    # 幂等：该版本已生成过候选批次则直接返回既有批次（不重复写候选）
    dup = conn.execute("SELECT batch_id FROM v2g_candidates WHERE sysml_version_id=? LIMIT 1",
                       (version_id,)).fetchone()
    if dup:
        crows = conn.execute(
            "SELECT id, entity_name, entity_type, status, confidence, matching_status, match_entity_id, "
            "rel_matching_status, rel_match_rel_id, rel_type, rel_source, rel_target, errors, properties "
            "FROM v2g_candidates WHERE batch_id=?", (dup["batch_id"],)).fetchall()
        return {"ok": True, "batch_id": dup["batch_id"], "dup": True,
                "candidates": [dict(r) for r in crows]}
    result = sysml_to_candidates(conn, content, version_id=version_id,
                                 model_name=model_name, source=source)
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, 400)
    # 回填版本入库批次标记（draft 状态 → 生成候选后可入库）
    conn.execute("UPDATE sysml_versions SET imported_batch=? WHERE id=?",
                 (result["batch_id"], version_id))
    conn.commit()
    audit(_actor(user), "sysml_import_candidates",
          f"AI 建模 SysML v{version_id} 候选化: {result['node_count']} 实体/{result['edge_count']} 关系"
          f"（batch {result['batch_id']}）", conn=conn)
    return {"ok": True, **result}


def resolve_push_tool_gate(conn, version_id: int) -> dict:
    """写回智源前的「工程归属 + 工具绑定」闸（2026-09-28 多工程 P0-2 抽成纯函数，便于夹具断言）。

    返回 `{"ok": True/False, "code":..., "error":..., "tool":...}`。

    为什么必须先判「会话有没有归属工程」而不是只看绑定（此前是**静默跳过**）：
    旧写法用 `JOIN projects p ON p.id=c.project_id`，会话 `project_id` 为空时 join 不到任何行
    → `_pj is None` → **整段工具闸被跳过**，版本照样往下写。这与「未绑定/不支持的工具明确报
    TOOL_NOT_READY」自相矛盾：真正没归属的那批（实测 31 个会话里 20 个 project_id 为空）
    反而一路放行。
    现在按用户口径「允许不关联工程的会话存在（知识检索/问答），一旦基于工程的会话则需收敛进项目」
    分开两种状态：
      · 会话**未关联工程** → 400 `PROJECT_NOT_BOUND`（写回是工程操作，先归入项目再说）；
      · 会话关联了工程、但该工程绑定的是别的工具 → 400 `TOOL_NOT_READY`（原语义保留）。

    P1-1（2026-09-28）归属取数改为「版本自身定格优先」：`sysml_versions.project_id`
    非空即用之，为空（存量历史行，**不回填**）才回退到会话当前归属。
    """
    # 归属取数顺序（P1-1，2026-09-28）：**版本自身定格的 project_id** → 会话当前归属（旧行兜底）。
    # 旧行 project_id 为空（存量不回填），走会话推导，与修复前完全一致；
    # 新行按产生时所属工程定格 —— 会话事后改归属，历史版本的写回目标不跟着漂移。
    row = conn.execute(
        "SELECT v.id AS vid, c.id AS cid, "
        "COALESCE(NULLIF(TRIM(COALESCE(v.project_id,'')),''), TRIM(COALESCE(c.project_id,''))) AS pid "
        "FROM sysml_versions v LEFT JOIN conversations c ON c.id=v.conversation_id "
        "WHERE v.id=?", (version_id,)).fetchone()
    if row is None:
        return {"ok": False, "code": "VERSION_NOT_FOUND", "error": "版本不存在", "tool": ""}
    pid = str(row["pid"] if hasattr(row, "keys") else row[2] or "")
    if not pid:
        return {"ok": False, "code": "PROJECT_NOT_BOUND",
                "error": "该版本所属会话未关联工程，无法写回建模工具"
                         "（写回是工程操作：先把会话归入项目，或在本会话内选择工程后再试）",
                "tool": ""}
    pj = conn.execute("SELECT id, tool_binding FROM projects WHERE id=?", (pid,)).fetchone()
    if not pj:
        # 孤儿归属：指向的工程已被移除。不能等价于"已绑定"，更不能放行。
        # ⚠️ 2026-11-07：code 从 PROJECT_NOT_BOUND 拆出 `PROJECT_ORPHANED` ——
        #   两种情况的**自救路径不同**（前者是"还没归入工程"，后者是"归入的工程没了"），
        #   用同一个 code 会让前端只能给一句笼统提示。实测 7 条版本挂在已删除的
        #   `project-satnet-broadband` 上，此前用户**没有任何入口**改回来。
        return {"ok": False, "code": "PROJECT_ORPHANED",
                "error": f"该版本归属的工程（{pid}）已不存在，无法写回建模工具"
                         "（调用 /api/sysml-versions/{id}/reattribute 重新归属到现有工程）",
                "tool": "", "stale_project_id": pid}
    try:
        _b = json.loads((pj["tool_binding"] if hasattr(pj, "keys") else pj[1]) or "{}")
    except Exception:
        _b = {}
    _tool = (_b.get("tool") or "").strip()
    if _tool and _tool != "zhiyuan":
        return {"ok": False, "code": "TOOL_NOT_READY", "tool": _tool,
                "error": f"本工程绑定的建模工具为 {_tool}，智源写回不可用"
                         "（MagicDraw 通道：文件导入 / SysML v2 API / Cameo 插件，接入后自动启用）"}
    return {"ok": True, "code": "", "error": "", "tool": _tool}


@router.post("/api/sysml-versions/{version_id}/push-zhiyuan")
def push_zhiyuan(version_id: int, body: dict = None,
                 conn=Depends(db_session), user=Depends(current_user)):
    """闸口②：把该版本的 V2 源码写入智源建模软件（先语法检测，通过后覆盖导入）。

    body: {vc: "branchId,queryType[,versionNumber]", target_package_data_id?: int}
    外系统写操作 —— 只能由用户点击触发；语法检测未通过则不写入。

    2026-09-24 方案A：工程入库直入捷径下线后，本端点成为 AI 建模数据进智源的唯一写入口。
    防重复闸（暂定规则，用户拍板）：zhiyuan_imported_id 非空的版本不允许再次写回——
    写回即覆盖导入，重复点击只会产生重复覆盖风险；如确需重写，先清 zhiyuan_imported_id 标记。

    2026-09-24 建模工具适配层：工程绑定 tool=magicdraw 等非智源工具时拒绝写回（TOOL_NOT_READY）——
    绑定了什么工具就走什么工具的连接器，MagicDraw 通道（文件导入/SysML v2 API/Cameo 插件）接入前明确报错。
    """
    from services.norm_apply import push_version_to_zhiyuan
    b = body or {}
    actor = (user or {}).get("display_name") or (user or {}).get("username") or "未登录"
    prior = conn.execute("SELECT zhiyuan_imported_id FROM sysml_versions WHERE id=?",
                         (version_id,)).fetchone()
    if not prior:
        return JSONResponse({"error": "版本不存在"}, 404)
    # 工具闸：版本 → 会话 → 工程绑定；非智源工具不可走智源写回
    _g = resolve_push_tool_gate(conn, version_id)
    if not _g["ok"]:
        return JSONResponse({"error": _g["error"], "code": _g["code"],
                             **({"tool": _g["tool"]} if _g.get("tool") else {})}, 400)
    if str(prior["zhiyuan_imported_id"] or "").strip():
        return JSONResponse({"error": f"该版本已写回智源（记录 {prior['zhiyuan_imported_id']}），"
                                      "暂定规则：已写回的版本不可重复写回", "code": "ALREADY_PUSHED"}, 400)
    res = push_version_to_zhiyuan(
        conn, version_id, vc=str(b.get("vc") or "")[:40],
        target_package_data_id=(int(b["target_package_data_id"])
                                if b.get("target_package_data_id") not in (None, "") else None),
        actor=actor)
    if res.get("error") and not res.get("ok"):
        code = 400 if res.get("code") in ("VC_REQUIRED", "CONFIG_MISSING") else 502
        return JSONResponse(res, code)
    audit(actor, "sysml_push_zhiyuan",
          f"SysML v{version_id} 写入智源（vc={b.get('vc') or '-'}）: "
          f"{'成功' if res.get('ok') else '失败'}", conn=conn)
    return res


@router.get("/api/sysml-versions/{version_id}/closure")
def version_closure(version_id: int, conn=Depends(db_session)):
    """闭环进度：一个版本走到哪一步、卡在哪、下一步该做什么（只读）。

    ## 为什么需要它
    2026-11-07 实测：`sysml_versions` 128 条里 **120 条有代码但 `imported_batch` 为空**、
    `committed` 0 条、`zhiyuan_imported_id` 0 条。也就是说"生成→入库→写回"这条路
    **从未被走完过**，但从界面上看每一条都是"正常版本"，看不出卡在哪。
    真实链路是四段（每段都有独立闸，跳过哪段都不算闭环）：

        ① 候选化   import-candidates  → v2g_candidates(SYSM-*) / imported_batch
        ② 提交审核  review_status=submitted（V2 候选强制待审闸）
        ③ 三元组   triples(pending) → 审核通过 → approved
        ④ 图库     commit_approved_triples → entities/relations（committed）
        ⑤ 写回智源 push-zhiyuan → zhiyuan_imported_id（外部系统，需 vc/配置）

    `adopted` 列**不参与**判定：它 2026-09-24 已随「📌 采纳」入口一并下线，
    仅作历史留痕、无写入方（见本文件头注）。
    """
    row = conn.execute("SELECT id, project_id, status, code_text, imported_batch, "
                       "zhiyuan_imported_id FROM sysml_versions WHERE id=?",
                       (version_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "版本不存在"}, 404)
    pid = str(row["project_id"] or "")
    has_code = bool(str(row["code_text"] or "").strip())
    batch = str(row["imported_batch"] or "")
    stages = []

    # ① 候选化
    cand_n = sub_n = 0
    if batch:
        cand_n = conn.execute("SELECT COUNT(*) FROM v2g_candidates WHERE batch_id=?",
                              (batch,)).fetchone()[0]
        sub_n = conn.execute("SELECT COUNT(*) FROM v2g_candidates WHERE batch_id=? "
                             "AND review_status='submitted'", (batch,)).fetchone()[0]
    stages.append({"stage": "candidates", "name": "候选化（解析→v2g 候选）",
                   "done": bool(batch and cand_n > 0),
                   "detail": f"批次 {batch or '—'}／候选 {cand_n} 条",
                   "action": "" if batch else "POST /import-candidates"})

    # ② 提交审核
    stages.append({"stage": "review", "name": "提交审核（V2 候选强制待审）",
                   "done": bool(cand_n and sub_n == cand_n),
                   "detail": f"已提交 {sub_n}/{cand_n}",
                   "action": "" if (cand_n and sub_n == cand_n) else "POST /api/knowledge/v2g/submit-review"})

    # ③ 三元组
    t_all = conn.execute("SELECT COUNT(*) FROM triples WHERE sysml_version_id=?",
                         (version_id,)).fetchone()[0]
    t_ok = conn.execute("SELECT COUNT(*) FROM triples WHERE sysml_version_id=? AND status='approved'",
                        (version_id,)).fetchone()[0]
    stages.append({"stage": "triples", "name": "三元组审核",
                   "done": bool(t_all and t_ok == t_all),
                   "detail": f"已通过 {t_ok}/{t_all}",
                   "action": "" if (t_all and t_ok == t_all) else "确认候选（v2g/confirm）后审核三元组"})

    # ④ 图库
    ent_n = conn.execute("SELECT COUNT(DISTINCT graph_entity_id) FROM triples "
                         "WHERE sysml_version_id=? AND coalesce(graph_entity_id,'')<>''",
                         (version_id,)).fetchone()[0]
    stages.append({"stage": "graph", "name": "图库入库（entities/relations）",
                   "done": bool(ent_n),
                   "detail": f"落地实体 {ent_n} 条／版本状态 {row['status']}",
                   "action": "" if ent_n else "commit_approved_triples（审核通过后自动）"})

    # ⑤ 写回智源
    zid = str(row["zhiyuan_imported_id"] or "")
    gate = resolve_push_tool_gate(conn, version_id)
    # 闸门通过 ≠ 能写回：还需要 base_url + vc（实测这台机器 base_url 为空 ⇒ push 必报
    # CONFIG_MISSING，但 closure 若只报"闸门通过"会让人以为差一步点击，属误导）。
    cfg_ok, cfg_why = True, ""
    if not zid:
        try:
            from adapters.zhiyuan_client import _load_config
            _cfg = _load_config()
            if not str(_cfg.get("base_url") or "").strip():
                cfg_ok, cfg_why = False, "智源 base_url 未配置（ZHIYUAN_BASE_URL）"
            elif not str(_cfg.get("default_vc") or "").strip():
                cfg_ok, cfg_why = False, "智源默认版本上下文 vc 未配置"
        except Exception as exc:                                # noqa: BLE001
            cfg_ok, cfg_why = False, f"智源配置读取失败：{type(exc).__name__}"
    _zy_action = ""
    if not zid:
        _zy_action = (gate["error"] if not gate["ok"]
                      else ("POST /push-zhiyuan（缺配置：" + cfg_why + "）" if not cfg_ok
                            else "POST /push-zhiyuan"))
    stages.append({"stage": "zhiyuan", "name": "写回智源（外部系统）",
                   "done": bool(zid),
                   "detail": (f"写回记录 {zid}" if zid else
                              ("闸门：通过" if gate["ok"] else f"闸门：{gate['code']}")
                              + ("" if cfg_ok else f"；配置：{cfg_why}")),
                   "action": _zy_action})

    blocked = [s for s in stages if not s["done"]]
    return {"version_id": version_id, "project_id": pid, "has_code": has_code,
            "stages": stages,
            "closed": not blocked,
            "next": (blocked[0]["stage"] if blocked else ""),
            "next_action": (blocked[0]["action"] if blocked else ""),
            "note": "adopted 列已于 2026-09-24 下线，不参与闭环判定"}


def reattribute_version(version_id: int, body: dict = None, conn=Depends(db_session),
                        user=Depends(current_user)):
    """把版本**重新归属**到一个现有工程（修「孤儿归属无法自救」）。

    ## 为什么必须有这个端点
    `resolve_push_tool_gate` 对「版本归属的工程已不存在」只给一句「请先把会话归入有效项目」——
    但 `sysml_versions.project_id` 是**写入时定格**的（P1-1，2026-09-28：故意不让它随会话漂移），
    所以改会话归属**不会**改变历史版本，用户手里没有任何可执行的动作。
    2026-11-07 实测：7 条版本挂在已删除的 `project-satnet-broadband` 上、113 条版本 project_id
    为空 —— 这批版本**永远无法写回**，且从界面上看只是"写回失败"。

    ## 边界（不能变成绕过闸门的后门）
    · 只改 `project_id`，**不动** `zhiyuan_imported_id` / `imported_batch` / `status`；
    · 目标工程必须存在（否则又造一个孤儿）；
    · `body.project_id=""` 表示"退出工程"（允许，写回会回到 PROJECT_NOT_BOUND，属显式选择）；
    · 全量审计：`sysml_reattribute` 记录前后值。
    """
    b = body or {}
    if "project_id" not in b:
        return JSONResponse({"error": "缺少 project_id（空串表示退出工程）"}, 400)
    target = str(b.get("project_id") or "").strip()
    row = conn.execute("SELECT id, project_id FROM sysml_versions WHERE id=?",
                       (version_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "版本不存在"}, 404)
    if target:
        pj = conn.execute("SELECT id FROM projects WHERE id=?", (target,)).fetchone()
        if not pj:
            return JSONResponse({"error": f"目标工程不存在: {target}", "code": "PROJECT_NOT_FOUND"}, 400)
    before = str(row["project_id"] or "")
    if before == target:
        return {"ok": True, "changed": False, "project_id": target}
    conn.execute("UPDATE sysml_versions SET project_id=? WHERE id=?", (target, version_id))
    conn.commit()
    audit(_actor(user), "sysml_reattribute",
          f"SysML v{version_id} 归属 {before or '（空）'} → {target or '（空）'}", conn=conn)
    return {"ok": True, "changed": True, "before": before, "project_id": target}


@router.post("/api/sysml-versions/{version_id}/reattribute")
def reattribute_version_post(version_id: int, body: dict = None, conn=Depends(db_session),
                             user=Depends(current_user)):
    """POST 入口（与函数体同一实现，供前端与门禁脚本调用）。"""
    return reattribute_version(version_id, body, conn, user)


@router.delete("/api/sysml-versions/{version_id}/import-candidates")
def sysml_import_candidates_cancel(version_id: int, conn=Depends(db_session),
                                   user=Depends(current_user)):
    """放弃候选（P1-6）：删除该版本已生成但未确认的候选批次 + 回滚 imported_batch。

    已确认（confirmed）候选与已入库实体/关系保留（历史留痕）；
    已 committed 的版本不允许放弃（入库已完成，直接走版本历史）。
    """
    row = conn.execute("SELECT status, imported_batch FROM sysml_versions WHERE id=?",
                       (version_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "版本不存在"}, 404)
    if row["status"] == "committed":
        return JSONResponse({"error": "该版本已入库（committed），无法放弃候选"}, 400)
    batch = row["imported_batch"] or ""
    deleted = 0
    if batch:
        deleted = conn.execute(
            "DELETE FROM v2g_candidates WHERE batch_id=? AND status='pending'", (batch,)).rowcount
    conn.execute("UPDATE sysml_versions SET imported_batch='' WHERE id=?", (version_id,))
    conn.commit()
    audit(_actor(user), "sysml_import_candidates_cancel",
          f"放弃 AI 建模 SysML v{version_id} 候选: 删除 {deleted} 条 pending（batch {batch or '-'}）",
          conn=conn)
    return {"ok": True, "deleted": deleted, "batch_id": batch or ""}
