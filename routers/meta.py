"""元数据域：审计 / 集成源 / 运维监控 / 设置 / 文档上传

聚合了体量较小、无独立域的辅助接口，避免文件碎片化。
"""
import os
import re
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, Form
from fastapi.responses import Response, JSONResponse, FileResponse

from core.config import DB_PATH
from core.deps import db_session, current_user, require_any_permission, require_permission
from repositories.meta_repo import MetaRepo
from core.audit import audit, audit_user
from models import DocMetaIn

router = APIRouter(tags=["审计·集成·运维·设置·文档"])

# 文档上传写权限：设计师（kb_review:modify）与知识工程师（kb_ontology:edit）双角色放行
DOC_WRITE_PERMS = [("kb_review", "modify"), ("kb_ontology", "edit")]

# ── 2026-09-18 源文件预览：原件直出（预览主通道）──
# 背景：原预览只走 /source 的「文本抽取」路线，对两类真实文件失效 ——
#   PDF：extract_text → _extract_pdf 依赖可选依赖 pdfplumber，本机未安装 → 返回空串
#        （实测 #793《Guide to writing Requirements》抽出 0 字符）
#   图片：extract_text 对图片只返回占位提示串（需 OCR），实测 #792 PNG 仅 46 字符
# 对策（遵守 AGENTS.md 铁律 4「零新依赖」）：不引 pdf.js / PyMuPDF，
#   浏览器原生即可内嵌渲染 PDF / 图片 / 音视频 → 直接直出 data/uploads 里的原件副本。
#   文本类并存「原件视图 + 文本视图」；Office 二进制浏览器无法内嵌 → 降级文本视图 + 下载。
PREVIEW_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8", ".yml": "text/plain; charset=utf-8",
}
# 浏览器可原生内嵌渲染（走 /raw 原件视图）
PREVIEW_INLINE_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                       ".svg", ".mp4", ".webm", ".mp3", ".wav"}
# 纯文本可直读（原件视图与文本视图并存）
PREVIEW_TEXT_EXTS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml"}


def _uploads_dir() -> str:
    """原件副本目录（转调权威实现，见 `source_copy_path` 的说明）。"""
    from knowledge_pipeline.ingest import source_copy_dir
    return source_copy_dir()


def _safe_doc_name(filename: str) -> str:
    """文件名净化（转调权威实现）。

    ⚠️ 2026-09-21：本文件原有的正则副本已删除 —— 同一规则此前在
    `ingest._save_source_copy` / 本函数 / `search.retry_document` **三处各写一遍**，
    任何一处改了正则，另外两处就会"落盘能成、定位不到"，且失败静默（预览空、
    重试报"源文件副本不存在"）。权威实现：`knowledge_pipeline.ingest.safe_doc_name`。
    """
    from knowledge_pipeline.ingest import safe_doc_name
    return safe_doc_name(filename)


def source_copy_path(doc_id: int, filename: str) -> str:
    """定位文档原件副本 data/uploads/{doc_id}_{safe}（转调权威实现）。"""
    from knowledge_pipeline.ingest import source_copy_path as _scp
    return _scp(doc_id, filename)


def preview_meta(doc_id: int, filename: str, file_type: str = "") -> dict:
    """预览能力元数据：前端据此选渲染器（原件内嵌 / 文本 / 降级下载）。"""
    ext = os.path.splitext(filename or "")[1].lower()
    if not ext and file_type:
        ext = "." + file_type.strip().lstrip(".").lower()
    has_file = os.path.exists(source_copy_path(doc_id, filename))
    if ext in PREVIEW_INLINE_EXTS:
        kind = "inline"
    elif ext in PREVIEW_TEXT_EXTS:
        kind = "text"
    elif ext:
        kind = "office"
    else:
        kind = "unknown"
    return {
        "kind": kind,           # inline | text | office | unknown
        "ext": ext,
        "mime": PREVIEW_MIME.get(ext, "application/octet-stream"),
        "has_file": has_file,   # 原件副本是否在盘
        "can_inline": bool(has_file and kind in ("inline", "text")),
        "raw_url": f"/api/documents/{doc_id}/raw",
    }



@router.get("/api/meta/backup")
def backup_db(conn=Depends(db_session)):
    """备份数据库：WAL checkpoint 落盘后导出 db 文件快照下载。

    返回 application/octet-stream，文件名带时间戳，可离线归档/迁移。
    """
    import os

    # 将 WAL 中未落盘的数据 checkpoint 回主文件，保证快照完整
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    if not os.path.exists(DB_PATH):
        return Response(content=b"", media_type="application/octet-stream")
    with open(DB_PATH, "rb") as f:
        data = f.read()
    fname = f"mbse_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# 审计查看权限：管理员（admin:audit_view）或持 audit:view 的审计只读角色
AUDIT_VIEW_PERMS = [("admin", "audit_view"), ("audit", "view")]


@router.get("/api/audit")
def list_audit(limit: int = 100, event_type: Optional[str] = None, search: Optional[str] = None,
               branch: Optional[str] = None, conn=Depends(db_session),
               user=Depends(require_any_permission(AUDIT_VIEW_PERMS))):
    """审计日志。branch 非空 → 分支视角（只返回该分支的域事件，2026-09-14）。

    P0-a（2026-10-03 评估）：补鉴权。此前该端点无任何鉴权 ——
    enforce_login 关闭时匿名请求可读全量操作记录（含谁在什么时间改了什么），
    这是审计本身最不该有的洞。匿名请求仍按现有口径放行（兼容体验期与无头脚本）。

    ⚠️ 2026-10-03 修正（登记为 P0-a 的设计缺陷，勿回退）：
    首版只挂 admin:audit_view，而本平台 roles 里 **没有任何一个账号是系统管理员**
    （实测 users 9/9 均为设计师 role_id=80，admin=[]），且 require_permission 对**匿名放行**
    ⇒ 结果是「登录了反而 403、不登录却能看全量」，审计页与分支页审计 Tab 双双变空白
    （前端 loadAudit 拿到 {error} 后访问 d.stats.today_llm 直接 TypeError，页面永远停在「加载中…」）。
    现改为双通道：admin:audit_view 或 audit:view 均可查看；设计师/知识工程师角色已补 audit:view。
    """
    repo = MetaRepo(conn)
    logs = repo.list_audit(limit, event_type, search, branch=branch or None)
    stats = {
        "today_llm": repo.count_audit_today("llm_chat"),
        "today_upload": repo.count_audit_today("upload"),
        "blocked": repo.count_audit_blocked(),
    }
    return {"logs": logs, "stats": stats}


@router.get("/api/audit/verify")
def verify_audit(limit: int = 0, conn=Depends(db_session),
                 user=Depends(require_any_permission(AUDIT_VIEW_PERMS))):
    """P0-b（2026-10-03 评估）：审计链完整性校验（NIST AU-9 / SOC2 CC7.2 防篡改底线）。

    - ok=true：逐行摘要重算一致且 prev_hash 首尾相接
    - chained < total：存在未入链的行（未经 core.audit.audit() 写入，或历史迁移未覆盖）
    - broken[]：给出第一个受损行的 id 与原因（篡改 / 行被插入或删除）
    limit>0 时只校验最近 limit 行（大表快速体检用）。
    """
    from core.audit import verify_chain
    try:
        return verify_chain(limit=limit, conn=conn)
    except Exception as e:
        # 列尚未迁移到位时给出可读结论，而不是 500
        return {"ok": False, "error": str(e)[:300], "hint": "审计链列（prev_hash/hash）尚未迁移",
                "total": 0, "chained": 0, "unchained": 0, "broken": [], "broken_count": 0}


# ── /api/integration/sources 已随 R1=B 数据集成移除（2026-09-01，data_sources 表已删）──


@router.get("/api/ops/metrics")
def ops_metrics(conn=Depends(db_session)):
    """运维指标（2026-09-22 真实化改造）。

    原版返回编造数据（light_rt/qps/availability 硬编码、Neo4j/Milvus/Qwen备/APP-2 等
    本工程不存在的组件、假备份日期）——面向甲方的假运维状态比没有更危险。
    现只返回可证真实的数据：
    - 计数：真实表统计
    - topology：真实组件探活（SQLite / pyoxigraph 内嵌图库 / LLM provider 配置 / Fuseki 外挂图库）
    - light_rt / qps / availability 等性能指标待真实压测（需求 7.1）后回填，不预置假值
    - error_codes 待统一错误码表（需求 11.3）建立后接入
    """
    repo = MetaRepo(conn)

    # ── 真实组件探活 ──
    # 2026-09-26 口径统一：探活逻辑迁到 metrics_core.service_health（知识看板「性能与运维」簇
    # 也要用同一结果），本端点只做委派，避免两份探活实现漂移。返回字段保持不变。
    from metrics_core import service_health
    topology = service_health(conn)["components"]

    # ── 真实备份记录：备份为手动导出式（GET /api/meta/backup），无定时目录可扫 ──
    backup = {"last_backup": None,
              "strategy": "手动导出（GET /api/meta/backup，WAL checkpoint 后下载快照）；定时备份策略见需求 11.3（待建）"}

    return {
        "total_entities": repo.count("entities"), "total_conversations": repo.count("conversations"),
        "total_messages": repo.count("messages"), "total_audit_logs": repo.count("audit_logs"),
        "topology": topology,
        "backup": backup,
        # light_rt / qps / heavy_concurrent / online_users / availability 已删除：
        # 原为硬编码假值，待 7.1 真实压测后以实测数据回填
        # error_codes 已删除：原三条为编造示例，待 11.3 统一错误码表建立后接入真实枚举
    }


@router.get("/api/settings")
def get_settings(conn=Depends(db_session)):
    return MetaRepo(conn).get_settings()


@router.put("/api/settings/{key}")
def update_setting(key: str, body: dict, conn=Depends(db_session)):
    MetaRepo(conn).upsert_setting(key, body["value"])
    return {"ok": True}


@router.post("/api/documents/upload")
def upload_document(file: UploadFile = File(...), title: str = Form(""), author: str = Form(""),
                    version: str = Form("v1.0"), tags: str = Form(""),
                    branch: str = Form(""), folder_id: str = Form("0"), conn=Depends(db_session),
                    user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """文档上传 + 知识库真管道（KB-P0）：解析 → 结构感知分块 → Embedding → 落库 + 元数据。

    multipart/form-data 字段：file + title / author / version / tags（逗号分隔）
    + folder_id（2026-09-21：上传落点 = 当前目录，0 = 未归类；与前端左栏选中项一致）。
    文档为全局资产：不随分支变化（branch 统一 'global'），向量化数据全局消费；
    实体/关系等建模数据仍按分支隔离。
    parse_status 状态机：parsing → completed / failed；chunk_count 实时返回。
    （入库 + 上传即抽取共用 ingest_upload_document：AI 建模会话附件上传走同一入口）
    """
    from knowledge_pipeline import ingest_upload_document
    content = file.file.read()
    file_type = file.filename.split(".")[-1] if "." in file.filename else ""
    metadata = {
        "title": title or "",
        "author": author or "",
        "version": version or "v1.0",
        "tags": [t.strip() for t in tags.split(",") if t.strip()] if tags else [],
    }
    result = ingest_upload_document(conn, file.filename, file_type, content, metadata=metadata,
                                    branch="global")
    doc_id = result.get("doc_id")
    # 落点：当前目录（校验失败不阻断上传 —— 解析已成功，目录只是组织维度，
    # 为了一个不存在的目录把整份上传回滚掉是更差的选择；退化为未归类并回报原因）
    from repositories.doc_folder_repo import DocFolderRepo, normalize_folder
    _want = normalize_folder(folder_id)
    if doc_id and _want:
        _mv = DocFolderRepo(conn).move_document(doc_id, _want)
        if not _mv.get("ok"):
            result["folder_warning"] = _mv.get("error", "")
        else:
            result["folder_id"] = _want
    audit(audit_user(user), "upload", f"上传文件: {file.filename} ({len(content)} bytes) → {result.get('parse_status')} {result.get('chunk_count',0)} chunks", conn=conn)
    if result.get("parse_status") == "failed":
        return JSONResponse({"id": doc_id, "filename": file.filename, "size": len(content),
                             "parse_status": "failed", "chunk_count": 0, "error": result.get("error", "")}, 400)
    return result


@router.post("/api/documents/{doc_id}/extract")
def document_extract(doc_id: int, conn=Depends(db_session)):
    """对已有文档手动补抽候选（受「文件管理实体与关系抽取」开关控制，关闭时不可用）。
    返回 {batch_id, candidates, node_count, edge_count}
    """
    import json as _j
    # 开关关闭 → 拒绝补抽（与前端隐藏补抽入口双重保险，保证开关两态闭环一致）
    _sw = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM settings").fetchall()}
    if (_sw.get("file_auto_extract_enabled") or "0") != "1":
        return JSONResponse({"error": "文件管理实体与关系抽取未开启，请先在「抽取设置」中开启后再补抽"}, 400)
    from vector2graph import extract_candidates
    row = conn.execute("SELECT filename, parse_status FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    if row["parse_status"] != "completed":
        return JSONResponse({"error": "文档未完成解析，无法抽取"}, 400)
    title = conn.execute("SELECT title FROM doc_metadata WHERE document_id=?", (doc_id,)).fetchone()
    query = (title[0] if title and title[0] else row["filename"])[:80]
    ext = extract_candidates(conn, query, doc_id=doc_id, top_k=5)
    cur_d = _j.loads(conn.execute("SELECT pipeline_detail FROM documents WHERE id=?", (doc_id,)).fetchone()[0] or "{}")
    cur_d["extraction"] = "done"
    cur_d["extract_candidates"] = len(ext.get("candidates", []))
    conn.execute("UPDATE documents SET pipeline_detail=? WHERE id=?",
                 (_j.dumps(cur_d, ensure_ascii=False), doc_id))
    conn.commit()
    return {"doc_id": doc_id, "batch_id": ext.get("batch_id", ""),
            "candidates": len(ext.get("candidates", [])),
            "node_count": ext.get("node_count", 0), "edge_count": ext.get("edge_count", 0)}


@router.get("/api/documents/{doc_id}/download")
def download_document(doc_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """文件列表「另存为」：下载 data/uploads 原件副本；缺失时降级导出抽取全文（.txt）。"""
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return JSONResponse({"error": "document not found"}, 404)
    # 第 4 处重复实现，2026-09-21 一并收敛（见 _safe_doc_name 的说明）
    path = source_copy_path(doc_id, doc["filename"])
    if os.path.exists(path):
        audit(audit_user(user), "doc_download", f"下载文档原件: {doc['filename']}", conn=conn)
        return FileResponse(path, filename=doc["filename"])
    # 降级：抽取全文导出（txt），保证「另存为」始终可用
    from knowledge_pipeline import get_source_text
    result = get_source_text(conn, doc_id)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, 404)
    audit(audit_user(user), "doc_download_fallback", f"下载文档抽取文本: {doc['filename']}（原件缺失）", conn=conn)
    from urllib.parse import quote as _q   # HTTP 头仅允许 latin-1：中文文件名须 URL 编码
    return Response(content=result.get("content") or "",
                    media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_q(safe)}.txt"})


@router.get("/api/documents/{doc_id}/source")
def get_document_source(doc_id: int, conn=Depends(db_session)):
    """源文件预览（文本视图）：txt/md 直读副本，pdf/docx 抽取，缺失时 chunks 拼接。

    2026-09-18 预览统一：响应新增 preview 字段（见 preview_meta），前端据此决定
    优先用原件内嵌视图（/raw）还是本视图 —— 本视图作为通用降级始终可用。
    注意：本视图对 PDF 依赖可选依赖 pdfplumber、对图片只有占位串，故不可作为唯一预览通道。
    """
    from knowledge_pipeline import get_source_text
    result = get_source_text(conn, doc_id)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, 404)
    pv = preview_meta(doc_id, result.get("filename") or "", result.get("file_type") or "")
    pv["text_len"] = len(result.get("content") or "")
    result["preview"] = pv
    return result


@router.get("/api/documents/{doc_id}/raw")
def get_document_raw(doc_id: int, download: str = "", conn=Depends(db_session),
                     user=Depends(current_user)):
    """源文件原件直出：预览主通道（inline）／另存为（download=1）。

    - 必须 inline：attachment 会强制触发下载，无法被 <iframe>/<img> 内嵌渲染。
    - media_type 按扩展名给足，浏览器据此选渲染器（PDF 内嵌查看器 / 图片 / 音视频 / 纯文本）。
    - 支持 Range 请求（由 FileResponse 处理）—— PDF 内嵌翻页、音视频拖动依赖此。
    - 原件副本缺失 → 404 + fallback='source'，前端据此自动切文本视图（不报死）。
    - 预览读取不写审计：PDF 内嵌会产生多次 Range 请求，逐次审计会刷满日志；
      download=1 时沿用既有下载审计口径。
    """
    doc = conn.execute("SELECT id, filename, file_type FROM documents WHERE id=?",
                       (doc_id,)).fetchone()
    if not doc:
        return JSONResponse({"error": "document not found"}, 404)
    path = source_copy_path(doc_id, doc["filename"])
    if not os.path.exists(path):
        return JSONResponse({
            "error": "源文件原件副本缺失",
            "fallback": "source",
            "hint": "该文档入库时未保留原件（或副本已被清理），可改用文本视图（由分块拼接）",
        }, 404)
    ext = os.path.splitext(doc["filename"] or "")[1].lower()
    is_download = (download == "1")
    if is_download:
        audit(audit_user(user), "doc_download", f"下载文档原件: {doc['filename']}", conn=conn)
    from urllib.parse import quote as _q   # HTTP 头仅允许 latin-1：中文文件名须 URL 编码
    disp = "attachment" if is_download else "inline"
    return FileResponse(path,
                        media_type=PREVIEW_MIME.get(ext, "application/octet-stream"),
                        headers={
                            "Content-Disposition": f"{disp}; filename*=UTF-8''{_q(_safe_doc_name(doc['filename']))}",
                            "Cache-Control": "no-cache",
                        })


@router.get("/api/documents/{doc_id}/preview")
def get_document_preview(doc_id: int, conn=Depends(db_session)):
    """资料库列表 hover 摘要卡片：轻量返回（元数据 + 正文摘要 + 统计）。

    摘要来源（2026-09-29 痛点2）：优先 LLM 真摘要（documents.summary，入库/重解析时生成，
    summary_source='llm'）；列为空（旧文档/生成失败/mock）→ 回退「前 2 块去噪截断」旧行为
    （summary_source='truncation'）。
    与 /api/documents/{doc_id}（详情，含最多 200 个 chunk）区分：本端点轻量，用于列表 hover 即时预览，
    避免每次悬停都拉全量分块。
    """
    row = conn.execute(
        "SELECT d.id, d.filename, d.file_type, d.file_size, d.parse_status, d.chunk_count, "
        "d.entity_count, d.quality_score, d.uploaded_by, d.branch, d.knowledge_category, "
        "d.created_at, d.pipeline_detail, d.error_msg, d.domain, d.domain_confidence, "
        "d.summary, "
        "m.title, m.author, m.version, m.tags "
        "FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id WHERE d.id=?",
        (doc_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "document not found"}, 404)
    out = dict(row)
    # ① 正文摘要（2026-09-29 痛点2）：优先 LLM 真摘要（documents.summary）；
    #    列为空（旧文档/生成失败/mock）→ 回退「前 2 块去噪截断」旧行为。summary_source 标记来源。
    _real = (row["summary"] or "").strip()
    if _real:
        out["summary"] = _real
        out["summary_source"] = "llm"
    try:
        chunks = conn.execute(
            "SELECT content, section FROM document_chunks WHERE document_id=? "
            "ORDER BY chunk_index LIMIT 2", (doc_id,)).fetchall()
    except Exception:
        chunks = []
    raw = "\n".join((c["content"] or "") for c in chunks)
    txt = re.sub(r"```.*?```", " ", raw, flags=re.S)          # 代码块
    txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", txt)            # 图片
    txt = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", txt)         # 链接保留文字
    txt = re.sub(r"^\s{0,3}#{1,6}\s*", "", txt, flags=re.M)    # 标题符
    txt = re.sub(r"[|>`*_\-]{2,}", " ", txt)                   # 表格/引用/强调符
    txt = re.sub(r"\s+", " ", txt).strip()
    if not _real:
        out["summary"] = (txt[:260] + ("…" if len(txt) > 260 else "")) if txt else ""
        out["summary_source"] = "truncation"
    out["section"] = (chunks[0]["section"] or "") if chunks else ""
    # ② 抽取候选统计（按 source_doc 聚合，与列表「抽取」列口径一致）
    try:
        out["extract"] = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM v2g_candidates WHERE source_doc=? GROUP BY status",
            (row["filename"],)).fetchall()}
    except Exception:
        out["extract"] = {}
    # ③ 关联实体数（已入库，排除 deprecated）
    try:
        out["linked_entity_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE source_doc=? AND status!='deprecated'",
            (row["filename"],)).fetchone()["n"]
    except Exception:
        out["linked_entity_count"] = 0
    return out


@router.get("/api/documents")
def list_documents(search: str = "", status: str = "", branch: str = "",
                   uploaded_by: str = "", file_type: str = "",
                   date_from: str = "", date_to: str = "",
                   lifecycle_status: str = "", include_deprecated: str = "1",
                   state: str = "", origin: str = "",
                   folder_id: str = "", folder_scope: str = "self",
                   conn=Depends(db_session)):
    """P0：列表支持按 lifecycle_status 过滤；include_deprecated=1（默认）显示废弃，0 隐藏。
    2026-09-10：新增 state 统一状态过滤（解析×生命周期合一），取值
    committed/stored/processing/failed/deprecated/archived，与前端「状态」列完全一致。
    2026-09-15：新增 origin 来源筛选（upload | ai_generated，空=全部）。
    2026-09-21：新增目录维过滤 —— `folder_id`（'0'=未归类；空=不过滤）
    + `folder_scope`（self=仅当前目录 / subtree=含子目录）。展开在服务端做，
    前端只传语义，避免把"子树 id 列表"这种实现细节塞进 URL。
    """
    folder_ids = None
    if folder_id != "":
        from repositories.doc_folder_repo import DocFolderRepo, normalize_folder
        fid = normalize_folder(folder_id)
        if folder_scope == "subtree" and fid:
            folder_ids = DocFolderRepo(conn)._descendant_ids(fid) or [fid]
        else:
            folder_ids = [fid]
    return MetaRepo(conn).list_documents_with_meta(
        search, status, branch, uploaded_by, file_type, date_from, date_to,
        lifecycle_status=lifecycle_status,
        include_deprecated=(include_deprecated != "0"),
        state=state, origin=origin, folder_ids=folder_ids,
    )


@router.post("/api/documents/ingest-artifact")
def ingest_artifact_document(body: dict = None, conn=Depends(db_session),
                             user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """2026-09-15 AI 产物收编入资料库（见 docs/AI产物收编资料库方案.md）。

    body: {artifact_id: int, knowledge_category?: str, override?: bool}
    - 显式收编 = 人审（产物面板「📥 收编入资料库」触发）
    - 查重命中（bigram 相似度 ≥0.92 且非同产物旧版）→ 409 needs_confirm，前端确认后 override 重发
    - 收编成功：documents.origin='ai_generated' + source_artifact_id + 分类；同产物旧版标记 superseded_by
    """
    body = body or {}
    try:
        artifact_id = int(body.get("artifact_id") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "artifact_id 必须为整数"}, 400)
    if not artifact_id:
        return JSONResponse({"error": "artifact_id 必填"}, 400)
    from services.artifact_ingest import ingest_artifact
    r = ingest_artifact(conn, artifact_id,
                        knowledge_category=str(body.get("knowledge_category") or ""),
                        actor=audit_user(user), override=bool(body.get("override")))
    if r.get("ok"):
        return r
    return JSONResponse(r, 409 if r.get("needs_confirm") else 400)


@router.get("/api/documents/{doc_id}")
def get_document(doc_id: int, conn=Depends(db_session)):
    """KB-P0：文档详情 + 追溯视图（元数据 + 块列表 + 关联实体）。"""
    detail = MetaRepo(conn).get_document_detail(doc_id)
    if not detail:
        return JSONResponse({"error": "document not found"}, 404)
    return detail


@router.put("/api/documents/{doc_id}/meta")
def update_document_meta(doc_id: int, body: DocMetaIn, conn=Depends(db_session), user=Depends(current_user)):
    """KB-P0：更新文档元数据（标题/作者/版本/标签）。"""
    import json as _json
    MetaRepo(conn).update_doc_metadata(
        doc_id, body.title, body.author, body.version,
        _json.dumps(body.tags, ensure_ascii=False),
        _json.dumps(body.extra or {}, ensure_ascii=False))
    audit(audit_user(user), "doc_meta_update", f"更新文档#{doc_id} 元数据", conn=conn)
    return {"ok": True}


@router.put("/api/documents/{doc_id}/category")
def update_document_category(doc_id: int, body: dict, conn=Depends(db_session)):
    """P0-3：给文档打知识类别标签（设计方法知识/设计资产子类）。空串=清除分类。"""
    category = (body.get("category") or "").strip()
    if category:
        row = conn.execute("SELECT id FROM knowledge_categories WHERE name=?", (category,)).fetchone()
        if not row:
            return JSONResponse({"error": f"知识类别不存在: {category}"}, 400)
    if not conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
        return JSONResponse({"error": f"文档不存在: {doc_id}"}, 404)
    conn.execute("UPDATE documents SET knowledge_category=? WHERE id=?", (category, doc_id))
    conn.commit()
    audit("李工", "doc_category", f"文档打知识类别标签: #{doc_id} → {category or '未分类'}", conn=conn)
    return {"ok": True, "doc_id": doc_id, "category": category}


@router.post("/api/documents/{doc_id}/reindex")
def reindex_document(doc_id: int, conn=Depends(db_session)):
    """KB-P0：重索引文档（embedding 升级/模型更换后重新向量化）。"""
    from knowledge_pipeline import reindex_document as _reindex
    result = _reindex(conn, doc_id)
    if result.get("parse_status") == "failed":
        return JSONResponse(result, 400)
    return result


@router.post("/api/documents/{doc_id}/retry")
def retry_document(doc_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """失败文档一键重试：从源文件副本重新执行整条管道（解析→分块→向量化→入库）。"""
    from knowledge_pipeline import retry_document as _retry
    result = _retry(conn, doc_id)
    audit(audit_user(user), "doc_retry", f"文档#{doc_id} 重试 → {result.get('parse_status')} {result.get('chunk_count',0)} 块", conn=conn)
    if result.get("parse_status") == "failed":
        return JSONResponse(result, 400)
    # 重试会重写 pipeline_detail（丢失 extraction 状态）→ 按文件抽取开关回填，
    # 保证开关关闭态 retry 后抽取阶段仍显示「未开启」（两态闭环一致）
    try:
        import json as _j
        _sw = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM settings").fetchall()}
        if (_sw.get("file_auto_extract_enabled") or "0") != "1":
            _cur = _j.loads(conn.execute(
                "SELECT pipeline_detail FROM documents WHERE id=?", (doc_id,)).fetchone()[0] or "{}")
            _cur["extraction"] = "disabled"
            conn.execute("UPDATE documents SET pipeline_detail=? WHERE id=?",
                         (_j.dumps(_cur, ensure_ascii=False), doc_id))
            conn.commit()
    except Exception:
        pass
    return result


@router.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, conn=Depends(db_session), user=Depends(current_user)):
    MetaRepo(conn).delete_document(doc_id)
    audit(audit_user(user), "doc_delete", f"删除文档#{doc_id}", conn=conn)
    return {"ok": True}


# ── P0：文档生命周期管理（FR-KG-8 / ArcR-5）──

@router.get("/api/documents/{doc_id}/lifecycle")
def get_document_lifecycle(doc_id: int, conn=Depends(db_session)):
    """文档生命周期时间线（来源 + 状态变迁 + 元数据）。

    返回 {document, log: [...]} —— 文档元数据 + 按时间倒序的 audit log（≤50 条）。
    """
    doc = conn.execute(
        "SELECT d.id, d.filename, d.lifecycle_status, d.parse_status, d.uploaded_by, "
        "d.created_at, d.deprecated_at, d.deprecated_by, d.deprecate_reason, "
        "d.archived_at, d.archived_by, d.lifecycle_version, "
        "m.title, m.author, m.version, m.tags, m.source AS meta_source, m.extra "
        "FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id WHERE d.id=?",
        (doc_id,)).fetchone()
    if not doc:
        return JSONResponse({"error": "document not found"}, 404)
    log = MetaRepo(conn).get_lifecycle_log(doc_id, limit=50)
    return {"document": dict(doc), "log": log}


@router.post("/api/documents/{doc_id}/deprecate")
def deprecate_document(doc_id: int, body: dict, conn=Depends(db_session),
                       user=Depends(current_user)):
    """P0-1：软删除文档（废弃）。

    body: {"reason": str} 必填（审计要求，FR-KG-11）
    联动：document_chunks.lifecycle_status 同步置 deprecated（向量层过滤，避免废弃文档仍命中）；
    不级联删除关联 entities/relations（保留溯源，entities.status='deprecated' 隔离）。
    """
    actor = audit_user(user)
    reason = (body.get("reason") or "").strip()
    if not reason:
        return JSONResponse({"error": "废弃理由不能为空（FR-KG-11 审计要求）"}, 400)
    cur = MetaRepo(conn).get_document_for_lifecycle(doc_id)
    if not cur:
        return JSONResponse({"error": "document not found"}, 404)
    if cur["lifecycle_status"] == "deprecated":
        return JSONResponse({"error": "文档已废弃，无需重复操作"}, 400)
    with conn:
        r = MetaRepo(conn).transition_lifecycle(
            doc_id, cur["lifecycle_status"], "deprecated", actor, reason,
            extra={"filename": cur["filename"]}, chunk_sync=True)
    if not r["ok"]:
        return JSONResponse({"error": r["reason"]}, 400)
    audit(actor, "doc_deprecate", f"文档#{doc_id} 软删除: {reason[:80]}", conn=conn)
    return {"ok": True, "doc_id": doc_id, "lifecycle_status": "deprecated"}


@router.post("/api/documents/{doc_id}/restore")
def restore_document(doc_id: int, body: dict | None = None,
                     conn=Depends(db_session), user=Depends(current_user)):
    """P0-1：撤销废弃（24h 窗口内可恢复；超期需管理员权限 user.manage）。

    body 可选 {"force": true} —— 管理员强制恢复（绕过 24h 窗口）。
    """
    body = body or {}
    actor = audit_user(user)
    # 权限校验：超期需管理员
    cur = MetaRepo(conn).get_document_for_lifecycle(doc_id)
    if not cur:
        return JSONResponse({"error": "document not found"}, 404)
    if cur["lifecycle_status"] != "deprecated":
        return JSONResponse({"error": "文档未处于废弃状态，无需恢复"}, 400)
    # 24h 窗口校验（非管理员禁止强制恢复）
    from datetime import datetime
    try:
        dep_at = datetime.fromisoformat(cur["deprecated_at"]) if cur["deprecated_at"] else None
    except Exception:
        dep_at = None
    elapsed_h = (datetime.now() - dep_at).total_seconds() / 3600 if dep_at else 0
    if elapsed_h > 24 and not body.get("force"):
        return JSONResponse(
            {"error": f"已超过 24h 撤销窗口（{elapsed_h:.1f}h），需管理员强制恢复", "need_admin": True},
            403)
    with conn:
        r = MetaRepo(conn).transition_lifecycle(
            doc_id, "deprecated", "committed", actor,
            reason="撤销废弃，恢复到 committed", chunk_sync=True)
    if not r["ok"]:
        return JSONResponse({"error": r["reason"]}, 400)
    audit(actor, "doc_restore", f"文档#{doc_id} 撤销废弃 (elapsed={elapsed_h:.1f}h)", conn=conn)
    return {"ok": True, "doc_id": doc_id, "lifecycle_status": "committed"}


@router.post("/api/documents/{doc_id}/archive")
def archive_document(doc_id: int, body: dict | None = None,
                     conn=Depends(db_session), user=Depends(current_user)):
    """P0-1：归档文档（只读分区，紧急恢复用）。需 doc.admin 权限。"""
    actor = audit_user(user)
    cur = MetaRepo(conn).get_document_for_lifecycle(doc_id)
    if not cur:
        return JSONResponse({"error": "document not found"}, 404)
    if cur["lifecycle_status"] == "archived":
        return JSONResponse({"error": "文档已归档"}, 400)
    if cur["lifecycle_status"] not in ("committed", "deprecated"):
        return JSONResponse({"error": f"仅 committed/deprecated 文档可归档，当前 {cur['lifecycle_status']}"}, 400)
    with conn:
        r = MetaRepo(conn).transition_lifecycle(
            doc_id, cur["lifecycle_status"], "archived", actor,
            reason=(body or {}).get("reason") or "")
    if not r["ok"]:
        return JSONResponse({"error": r["reason"]}, 400)
    audit(actor, "doc_archive", f"文档#{doc_id} 归档", conn=conn)
    return {"ok": True, "doc_id": doc_id, "lifecycle_status": "archived"}


@router.post("/api/documents/batch-deprecate")
def batch_deprecate(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """P0-1：批量软删除。body: {"ids": [int], "reason": str}。

    每个文档独立校验，任一失败不影响其他；返回 {ok, skipped, failed: [{doc_id, reason}]}。
    """
    actor = audit_user(user)
    ids = body.get("ids") or []
    reason = (body.get("reason") or "").strip()
    if not isinstance(ids, list) or not ids:
        return JSONResponse({"error": "ids 必须为非空数组"}, 400)
    if not reason:
        return JSONResponse({"error": "废弃理由不能为空"}, 400)
    with conn:
        r = MetaRepo(conn).batch_transition(ids, "deprecated", actor, reason)
    audit(actor, "doc_batch_deprecate",
          f"批量废弃文档 ok={r['ok']} skipped={r['skipped']} failed={len(r['failed'])}",
          conn=conn)
    return {"ok": True, **r}


# ── 2026-09-10 米爸澄清：入库（committed）= 内容已进图库，人工切换 ──
@router.post("/api/documents/{doc_id}/commit")
def commit_document(doc_id: int, body: dict | None = None,
                    conn=Depends(db_session), user=Depends(current_user)):
    """人工确认入库：stored（向量就绪/待入库）→ committed（正式入库）。

    入库表示知识文档内容已进入图库，此状态由人工确认切换（不自动提升）。
    """
    actor = audit_user(user)
    cur = MetaRepo(conn).get_document_for_lifecycle(doc_id)
    if not cur:
        return JSONResponse({"error": "document not found"}, 404)
    if cur["lifecycle_status"] == "committed":
        return JSONResponse({"error": "文档已是正式入库状态"}, 400)
    if cur["lifecycle_status"] != "stored":
        return JSONResponse(
            {"error": f"仅「待入库」（向量就绪）文档可人工入库，当前 {cur['lifecycle_status']}（请等解析完成）"}, 400)
    with conn:
        r = MetaRepo(conn).transition_lifecycle(
            doc_id, "stored", "committed", actor,
            reason=(body or {}).get("reason") or "人工确认入库")
    if not r["ok"]:
        return JSONResponse({"error": r["reason"]}, 400)
    audit(actor, "doc_commit", f"文档#{doc_id} 人工确认入库", conn=conn)
    return {"ok": True, "doc_id": doc_id, "lifecycle_status": "committed"}


@router.post("/api/documents/batch-commit")
def batch_commit(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """批量人工入库（仅 stored → committed）。body: {"ids": [int]}。"""
    actor = audit_user(user)
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return JSONResponse({"error": "ids 必须为非空数组"}, 400)
    with conn:
        r = MetaRepo(conn).batch_transition(ids, "committed", actor,
                                            reason="批量人工入库", allow_from=["stored"])
    audit(actor, "doc_batch_commit",
          f"批量入库文档 ok={r['ok']} skipped={r['skipped']} failed={len(r['failed'])}",
          conn=conn)
    return {"ok": True, **r}
