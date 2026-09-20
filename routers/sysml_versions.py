"""AI 建模 SysML 版本链 API：AI 生成 SysML v2 代码的版本管理与入库候选生成。

- GET  /api/sysml-versions?conversation_id=&artifact_id=   版本链列表（label/status/adopted/摘要）
- GET  /api/sysml-versions/{id}                            版本详情（源码/视图/element_summary/diff）
- POST /api/sysml-versions/{id}/adopt                      标记为采纳版本（同会话后标记覆盖）
- POST /api/sysml-versions/{id}/import-candidates          入库 Step1/2：解析 → v2g_candidates（SYSM- 批次）+ 消歧检测

候选确认/驳回/编辑/审核复用既有 v2g 接口（/api/knowledge/v2g/*），不在此重复。
"""
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user
from core.audit import audit

router = APIRouter(tags=["SysML 版本"])


def _actor(user) -> str:
    try:
        return (user or {}).get("name") or "王工"
    except Exception:
        return "王工"


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
            "SELECT id, version_label, status, adopted, element_summary, imported_batch, created_by, created_at, length(code_text) AS code_len "
            "FROM sysml_versions WHERE conversation_id=? ORDER BY id DESC",
            (conversation_id,)).fetchall()
    elif artifact_id:
        rows = conn.execute(
            "SELECT id, version_label, status, adopted, element_summary, imported_batch, created_by, created_at, length(code_text) AS code_len "
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


@router.post("/api/sysml-versions/{version_id}/adopt")
def adopt_sysml_version(version_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """标记为采纳版本（幂等）：同会话其他 adopted 置 0，本版本 adopted=1。

    2026-09-15：采纳 = 显式人工确认动作 → 自动收编其产物入资料库（best-effort，
    失败不阻断采纳；查重走 override=True——同版本再采纳属版本升级，直接走取代链）。
    """
    row = conn.execute("SELECT id, conversation_id, artifact_id FROM sysml_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "版本不存在"}, 404)
    if not row["conversation_id"]:
        return JSONResponse({"error": "该版本无会话上下文，无法标记采纳"}, 400)
    conn.execute("UPDATE sysml_versions SET adopted=0 WHERE conversation_id=? AND adopted=1",
                 (row["conversation_id"],))
    conn.execute("UPDATE sysml_versions SET adopted=1 WHERE id=?", (version_id,))
    conn.commit()
    audit(_actor(user), "sysml_adopt", f"标记 SysML 版本 #{version_id} 为采纳版本", conn=conn)
    # 自动收编（唯一保留的自动触发点：采纳本身即人审；报告无定稿环节，报告类不自动收编）
    auto_ingest = {"attempted": False}
    try:
        artifact_id = int(row["artifact_id"] or 0)
        if artifact_id and conn.execute(
                "SELECT kind FROM artifacts WHERE id=?", (artifact_id,)).fetchone():
            from services.artifact_ingest import ingest_artifact
            r = ingest_artifact(conn, artifact_id, actor=_actor(user), override=True)
            auto_ingest = {"attempted": True, **{k: r.get(k) for k in ("ok", "doc_id", "chunk_count", "category", "error")}}
    except Exception as e:
        auto_ingest = {"attempted": True, "ok": False, "error": str(e)[:200]}
    return {"ok": True, "version_id": version_id, "auto_ingest": auto_ingest}


@router.post("/api/sysml-versions/{version_id}/import-candidates")
def sysml_import_candidates(version_id: int, body: dict = None,
                            conn=Depends(db_session), user=Depends(current_user)):
    """入库 Step1/2：解析 SysML → 候选写入 v2g_candidates（SYSM- 批次，不入库）+ 消歧检测。

    body 可选：{content, source: "text|json", model_name}；缺省用版本快照 content（sysml_views JSON）。
    """
    from sysml_importer import sysml_to_candidates
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


@router.post("/api/sysml-versions/{version_id}/push-zhiyuan")
def push_zhiyuan(version_id: int, body: dict = None,
                 conn=Depends(db_session), user=Depends(current_user)):
    """闸口②：把该版本的 V2 源码写入智源建模软件（先语法检测，通过后覆盖导入）。

    body: {vc: "branchId,queryType[,versionNumber]", target_package_data_id?: int}
    外系统写操作 —— 只能由用户点击触发；语法检测未通过则不写入。
    """
    from norm_apply import push_version_to_zhiyuan
    b = body or {}
    actor = (user or {}).get("display_name") or (user or {}).get("username") or "未登录"
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
