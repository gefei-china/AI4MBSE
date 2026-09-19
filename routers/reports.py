"""报告域 API：多格式导出 + 报告中心（AI 建模报告统一归档/检索/导出）。

- POST /api/report/export：body 传 Report JSON + fmt（md|docx|pdf）→ 文件流下载
  （Report = {title, sections:[{heading, body, table?}], summary, report_type}）
- 报告中心：GET/POST/PUT/DELETE /api/reports；POST /api/reports/{id}/export
  AI 对话/流程产出的结构化报告自动落库归档，支持台账检索、详情查看与二次导出。
"""
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, JSONResponse

from report_generator import report_generator
from models import ReportExportIn, ReportSaveIn
from core.deps import db_session, current_user
from core.audit import audit, audit_user
from repositories.report_repo import ReportRepo
from repositories.project_repo import resolve_project_id

router = APIRouter(tags=["报告"])

_MIME = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}

_REPORT_TYPE_LABEL = {
    "analysis": "工程分析",
    "impact": "变更影响分析",
    "review": "预评审",
    "other": "其他",
}


@router.post("/api/report/export")
def export_report(body: ReportExportIn):
    fmt = str(body.fmt or "md").lower().lstrip(".")
    if fmt not in _MIME:
        raise HTTPException(400, f"不支持的导出格式: {fmt}（仅支持 md/docx/pdf）")
    report = {
        "title": body.title,
        "sections": body.sections,
        "summary": body.summary,
        "report_type": body.report_type,
        "meta": body.meta or report_generator.build_meta(body.title, body.report_type),
    }
    try:
        data = report_generator.export(report, fmt)
    except Exception as e:
        raise HTTPException(500, f"报告导出失败: {str(e)[:200]}")
    fname = re.sub(r'[\\/:*?"<>|]', "_", (body.filename or body.title or "报告").strip()) or "报告"
    return Response(
        content=data,
        media_type=_MIME[fmt],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname + '.' + fmt)}"},
    )


# ═══════════════ 报告中心（AI 建模报告统一归档）═══════════════
@router.get("/api/reports")
def list_reports(report_type: str = "", keyword: str = "",
                 conn=Depends(db_session)):
    """报告台账：按类型 / 关键词过滤，时间倒序。"""
    rows = ReportRepo(conn).list_reports(report_type.strip(), keyword.strip())
    for r in rows:
        r["report_type_label"] = _REPORT_TYPE_LABEL.get(r.get("report_type"), "其他")
    return rows


@router.post("/api/reports")
def save_report(body: ReportSaveIn, conn=Depends(db_session), u=Depends(current_user)):
    """归档报告：AI 对话/流程产出后自动落库，或手动录入。"""
    repo = ReportRepo(conn)
    title = body.title.strip()
    if not title:
        return JSONResponse({"error": "报告标题不能为空"}, 400)
    sections = body.sections if isinstance(body.sections, list) else []
    if not sections:
        return JSONResponse({"error": "报告内容为空，无法归档"}, 400)
    created_by = body.created_by or (audit_user(u) if u else "匿名")
    rtype = body.report_type if body.report_type in _REPORT_TYPE_LABEL else "analysis"
    rid = repo.create_report(
        title, rtype, body.summary or "", sections,
        body.source or "conversation", int(body.conversation_id or 0),
        body.branch or "", body.project_id or resolve_project_id(conn),
        body.status if body.status in ("draft", "final") else "draft",
        created_by,
    )
    audit(audit_user(u) if u else "匿名", "report_save",
          f"归档报告: {title}（{rtype}）", conn=conn)
    return {"ok": True, "id": rid}


@router.get("/api/reports/{report_id}")
def get_report(report_id: int, conn=Depends(db_session)):
    """报告详情：sections 反序列化为结构化分节。"""
    r = ReportRepo(conn).get_report(report_id)
    if not r:
        raise HTTPException(404, "报告不存在")
    r["report_type_label"] = _REPORT_TYPE_LABEL.get(r.get("report_type"), "其他")
    return r


@router.put("/api/reports/{report_id}")
def update_report(report_id: int, body: ReportSaveIn, conn=Depends(db_session), u=Depends(current_user)):
    """更新报告（重命名 / 改状态 final / 修订内容）。"""
    repo = ReportRepo(conn)
    if not repo.get_report(report_id):
        return JSONResponse({"error": "报告不存在"}, 404)
    title = body.title.strip() or "报告"
    rtype = body.report_type if body.report_type in _REPORT_TYPE_LABEL else "analysis"
    sections = body.sections if isinstance(body.sections, list) else []
    repo.update_report(report_id, title, rtype, body.summary or "",
                       sections, body.status if body.status in ("draft", "final") else "draft")
    audit(audit_user(u) if u else "匿名", "report_update", f"更新报告: {title}", conn=conn)
    return {"ok": True}


@router.delete("/api/reports/{report_id}")
def delete_report(report_id: int, conn=Depends(db_session), u=Depends(current_user)):
    repo = ReportRepo(conn)
    r = repo.get_report(report_id)
    if not r:
        return JSONResponse({"error": "报告不存在"}, 404)
    repo.delete_report(report_id)
    audit(audit_user(u) if u else "匿名", "report_delete", f"删除报告: {r['title']}", conn=conn)
    return {"ok": True}


@router.post("/api/reports/{report_id}/export")
def export_stored_report(report_id: int, fmt: str = Query("md"),
                         conn=Depends(db_session)):
    """已归档报告二次导出：md / docx / pdf。"""
    fmt = str(fmt).lower().lstrip(".")
    if fmt not in _MIME:
        raise HTTPException(400, f"不支持的导出格式: {fmt}（仅支持 md/docx/pdf）")
    r = ReportRepo(conn).get_report(report_id)
    if not r:
        raise HTTPException(404, "报告不存在")
    report = {
        "title": r["title"],
        "sections": r.get("sections") or [],
        "summary": r.get("summary") or "",
        "report_type": r.get("report_type") or "analysis",
        "meta": report_generator.build_meta(r["title"], r.get("report_type") or "analysis"),
    }
    try:
        data = report_generator.export(report, fmt)
    except Exception as e:
        raise HTTPException(500, f"报告导出失败: {str(e)[:200]}")
    fname = re.sub(r'[\\/:*?"<>|]', "_", r["title"].strip()) or "报告"
    return Response(
        content=data,
        media_type=_MIME[fmt],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname + '.' + fmt)}"},
    )
