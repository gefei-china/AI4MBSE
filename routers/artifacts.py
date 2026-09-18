"""会话产物 API：AI 建模会话内 AI 生成内容（报告/代码/SysML 视图/文档）的管理与预览。

- GET  /api/artifacts?conversation_id=&kind=&q=    会话内产物列表（分类/关键词过滤）
- GET  /api/artifacts/stats?conversation_id=       分类统计（chips 计数）
- GET  /api/artifacts/{id}                         详情（含 preview_content / meta）
- PATCH /api/artifacts/{id}                        重命名
- DELETE /api/artifacts/{id}                       删除产物索引
- GET  /api/artifacts/{id}/download                下载（file_path 落盘文件 / 报告二次导出）

产物仅收录 AI 生成内容；用户上传附件不进入产物库（维持消息内联展示）。
"""
import json
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, JSONResponse

from core.deps import db_session, current_user
from core.audit import audit
from repositories.artifact_repo import ArtifactRepo, KIND_LABELS

router = APIRouter(tags=["会话产物"])

_MIME = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _safe_fname(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip()) or "产物"


@router.get("/api/artifacts")
def list_artifacts(conversation_id: int = Query(..., description="会话 id（会话内产物库）"),
                   kind: str = "", q: str = "", conn=Depends(db_session)):
    """会话内产物列表（时间倒序）。conversation_id 必填，不做跨会话检索。"""
    if kind and kind not in KIND_LABELS:
        return JSONResponse({"error": f"未知产物类型: {kind}"}, 400)
    return ArtifactRepo(conn).list_artifacts(conversation_id, kind.strip(), q.strip())


@router.post("/api/artifacts")
def register_artifact(body: dict, conn=Depends(db_session), u=Depends(current_user)):
    """显式登记会话产物（2026-09-15：报告中心入口移除后，AI 生成的报告/文件
    统一以会话产物为展示出口——前端影响分析「保存为会话产物」等场景消费）。

    body: {conversation_id, kind(report|code|sysml|document|other), title,
           preview_type?, preview_content?, meta?, filename?}
    """
    from repositories.artifact_repo import KIND_LABELS as _KL
    conversation_id = int((body or {}).get("conversation_id") or 0)
    kind = str((body or {}).get("kind") or "").strip()
    title = str((body or {}).get("title") or "").strip()
    if not conversation_id:
        raise HTTPException(400, "conversation_id 必填")
    if kind not in _KL:
        raise HTTPException(400, f"未知产物类型: {kind}")
    if not title:
        raise HTTPException(400, "title 必填")
    aid = ArtifactRepo(conn).create_artifact(
        conversation_id, 0, kind, title,
        str(body.get("preview_type") or ("markdown" if kind in ("report", "document") else "none")),
        str(body.get("preview_content") or ""),
        body.get("meta") if isinstance(body.get("meta"), dict) else {},
        filename=str(body.get("filename") or ""),
        source="manual", created_by=(u or {}).get("name", "王工"))
    audit((u or {}).get("name", "王工"), "artifact_register",
          f"登记会话产物 #{aid}: {title}（{kind}，会话 #{conversation_id}）", conn=conn)
    return {"ok": True, "id": aid}


@router.get("/api/artifacts/stats")
def artifacts_stats(conversation_id: int = Query(...), conn=Depends(db_session)):
    """分类统计：全部 + 各 kind 计数（前端 chips）。"""
    return ArtifactRepo(conn).stats(conversation_id)


@router.get("/api/artifacts/{artifact_id}")
def get_artifact(artifact_id: int, conn=Depends(db_session)):
    a = ArtifactRepo(conn).get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, "产物不存在")
    return a


@router.patch("/api/artifacts/{artifact_id}")
def rename_artifact(artifact_id: int, body: dict, conn=Depends(db_session),
                    u=Depends(current_user)):
    repo = ArtifactRepo(conn)
    a = repo.get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, "产物不存在")
    title = str((body or {}).get("title") or "").strip() or a["title"]
    repo.rename_artifact(artifact_id, title)
    audit("王工", "artifact_rename", f"重命名会话产物#{artifact_id}: {title}", conn=conn)
    return {"ok": True, "title": title}


@router.delete("/api/artifacts/{artifact_id}")
def delete_artifact(artifact_id: int, conn=Depends(db_session), u=Depends(current_user)):
    repo = ArtifactRepo(conn)
    a = repo.get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, "产物不存在")
    repo.delete_artifact(artifact_id)
    audit("王工", "artifact_delete", f"删除会话产物#{artifact_id}: {a['title']}", conn=conn)
    return {"ok": True}


@router.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: int, conn=Depends(db_session)):
    """下载产物：file_path 落盘文件直接返回；报告产物（meta.sections）二次导出 md/docx/pdf。"""
    a = ArtifactRepo(conn).get_artifact(artifact_id)
    if not a:
        raise HTTPException(404, "产物不存在")

    # 1) 落盘文件优先（report_export / file_write 产物）
    if a.get("file_path"):
        abs_path = a["file_path"] if os.path.isabs(a["file_path"]) else os.path.join(_BASE_DIR, a["file_path"])
        if os.path.exists(abs_path) and os.path.isfile(abs_path):
            ext = os.path.splitext(abs_path)[1].lstrip(".").lower()
            media = _MIME.get(ext, "application/octet-stream")
            with open(abs_path, "rb") as f:
                data = f.read()
            fname = _safe_fname(a.get("filename") or a.get("title"))
            return Response(
                content=data, media_type=media,
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname + '.' + ext)}"},
            )

    # 2) 报告产物 → 复用报告导出链路（sections → md/docx/pdf）
    if a.get("kind") == "report" and a.get("meta", {}).get("sections"):
        from report_generator import report_generator as _rg
        _am = a.get("meta", {})
        report = {
            "title": a.get("title") or "报告",
            "sections": _am["sections"],
            "summary": _am.get("summary") or "",
            "report_type": _am.get("report_type") or "analysis",
            "meta": _am.get("meta") or _rg.build_meta(a.get("title") or "报告", _am.get("report_type") or "analysis"),
        }
        try:
            data = _rg.export(report, "md")
        except Exception as e:
            raise HTTPException(500, f"报告导出失败: {str(e)[:200]}")
        fname = _safe_fname(a.get("title") or "报告")
        return Response(
            content=data, media_type=_MIME["md"],
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname + '.md')}"},
        )

    # 3) 代码/文档产物 → 内联文本下载
    if a.get("preview_content"):
        ext = "sysml" if a.get("kind") == "sysml" else "md"
        fname = _safe_fname(a.get("title"))
        return Response(
            content=a["preview_content"].encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname + '.' + ext)}"},
        )

    raise HTTPException(404, "该产物无可用下载内容")
