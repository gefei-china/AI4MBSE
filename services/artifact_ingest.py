# -*- coding: utf-8 -*-
"""AI 产物收编资料库服务（2026-09-15，见 docs/AI产物收编资料库方案.md）。

把会话产物（artifacts：报告/代码/SysML/文档）序列化为文本字节，复用
knowledge_pipeline.ingest_upload_document 完整管道（落盘→解析→分块→向量化），
并补写来源溯源（documents.origin/source_artifact_id + chunks.origin）、
知识分类与时效取代链（doc_metadata.superseded_by）。

门禁模型（报告管理模块已移除、无"定稿"环节的前提下）：
- 显式收编 = 人审（用户在产物面板点「收编入资料库」）；
- 自动收编仅挂在 sysml_versions.adopted=1（版本采纳）钩子上；
- 收编前查重（bigram 相似度 ≥0.92 需确认，override 可覆盖），防近重复污染检索池；
- AI 建模 RAG 默认排除 ai_generated（消费隔离，见 knowledge_pipeline/search.py）。
"""
import json
import os
import re

from core.audit import audit

# 查重阈值：文档级最高 bigram 相似度 ≥ 该值 → 提示已有近似版本（industry: Dify/FastGPT 分段管理同源需求）
DEDUP_THRESHOLD = 0.92


def _safe_fname(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip()) or "产物"


def serialize_artifact(conn, a: dict) -> tuple:
    """产物 → (filename, file_type, content_bytes)。复用 download 端点三级降级：
    ① file_path 落盘文件直读 ② report → meta.sections 渲染 markdown ③ preview_content。"""
    kind = a.get("kind") or "document"
    title = _safe_fname(a.get("title") or a.get("filename") or "产物")
    # ① 落盘文件优先
    fp = a.get("file_path") or ""
    if fp:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        abs_path = fp if os.path.isabs(fp) else os.path.join(base, fp)
        if os.path.exists(abs_path) and os.path.isfile(abs_path):
            ext = os.path.splitext(abs_path)[1].lstrip(".").lower() or "md"
            with open(abs_path, "rb") as f:
                return f"{title}.{ext}", ext, f.read()
    # ② 报告产物 → sections 渲染 markdown（复用报告导出链路）
    if kind == "report":
        meta = a.get("meta") or {}
        if meta.get("sections"):
            from report_generator import report_generator as _rg
            report = {
                "title": a.get("title") or "报告",
                "sections": meta["sections"],
                "summary": meta.get("summary") or "",
                "report_type": meta.get("report_type") or "analysis",
                "meta": meta.get("meta") or _rg.build_meta(a.get("title") or "报告",
                                                           meta.get("report_type") or "analysis"),
            }
            return f"{title}.md", "md", _rg.export(report, "md")
    # ②b SysML 视图产物：preview_content 为空，真实内容在 meta.sysml_views
    #    （投影为可读结构化文本，收编后向量检索才有语义可命中）
    text = a.get("preview_content") or ""
    if kind == "sysml" and not text:
        sv = ((a.get("meta") or {}).get("sysml_views")) or {}
        lines = []
        for vname, vdata in (sv.get("views") or {}).items():
            view = (vdata or {}).get("view") or {}
            lines.append(f"## SysML 视图：{view.get('name') or vname}（{view.get('type', '')}）")
            if view.get("desc"):
                lines.append(str(view["desc"]))
            vnodes = vdata.get("nodes") or []
            vedges = vdata.get("edges") or []
            names = {n.get("id"): (n.get("name") or n.get("id")) for n in vnodes}
            if vnodes:
                lines.append(f"元素（{len(vnodes)}）：" + "、".join(
                    filter(None, (names.get(n.get("id")) for n in vnodes))))
            for e in (vedges or [])[:200]:
                lines.append(f"- {names.get(e.get('source'), e.get('source'))} "
                             f"—[{e.get('type') or e.get('label') or '关联'}]→ "
                             f"{names.get(e.get('target'), e.get('target'))}")
        text = "\n".join(lines)
        if text:
            return f"{title}.sysml.md", "md", text.encode("utf-8")
    # ③ 内联文本（sysml / code / document）
    if kind == "sysml":
        return f"{title}.sysml", "sysml", text.encode("utf-8")
    return f"{title}.md", "md", text.encode("utf-8")


def suggest_category(conn, kind: str) -> str:
    """kind → 知识类别建议（仅返回 knowledge_categories 中已存在的类别名，无则空串）。"""
    want = "可复用构件" if kind in ("code", "sysml") else ""
    if not want:
        return ""
    row = conn.execute("SELECT name FROM knowledge_categories WHERE name=?", (want,)).fetchone()
    return row["name"] if row else ""


def find_similar(conn, content: bytes) -> dict:
    """收编前查重：文档级签名对比——新内容（前 2000 字）bigram 向量 vs
    存量文档的 chunk 向量聚合（Counter 累加，重建文档级词袋）后余弦。

    注意不能用「全文 vs 单 chunk」直接比：文档分块后每块只是全文子集，
    余弦被稀释（实测 3 块文档仅 0.57），阈值形同虚设。文档级聚合后
    相同内容 ≈ 1.0，近重复才可能越过阈值。"""
    try:
        from collections import Counter
        from knowledge_engine import VectorEngine
        ve = VectorEngine()
        text = content.decode("utf-8", errors="ignore")
        qv = ve._vector(text[:2000])
        if not qv:
            return {}
        doc_vec = {}
        for r in conn.execute(
                "SELECT document_id, content FROM document_chunks WHERE content != ''").fetchall():
            dv = doc_vec.setdefault(r["document_id"], Counter())
            dv.update(ve._vector(r["content"] or ""))
        if not doc_vec:
            return {}
        best = {doc_id: ve._cosine(qv, dv) for doc_id, dv in doc_vec.items()}
        doc_id, score = max(best.items(), key=lambda kv: kv[1])
        if score >= DEDUP_THRESHOLD:
            row = conn.execute(
                "SELECT id, filename, origin, source_artifact_id FROM documents WHERE id=?",
                (doc_id,)).fetchone()
            if row:
                return {"similar_doc_id": row["id"], "similar_filename": row["filename"],
                        "similarity": round(score, 3)}
    except Exception:
        pass
    return {}


def ingest_artifact(conn, artifact_id: int, knowledge_category: str = "",
                    actor: str = "王工", override: bool = False) -> dict:
    """收编主流程。返回 {ok, doc_id, chunk_count, category, superseded[], similar?}；
    查重命中且未 override → {ok: False, needs_confirm: True, similar...}。"""
    from repositories.artifact_repo import ArtifactRepo
    repo = ArtifactRepo(conn)
    a = repo.get_artifact(artifact_id)
    if not a:
        return {"ok": False, "error": f"产物不存在: #{artifact_id}"}
    if a.get("source") == "upload":
        return {"ok": False, "error": "该产物为用户上传件，无需收编（本就在资料库）"}
    filename, file_type, content = serialize_artifact(conn, a)
    if not content or not content.strip():
        return {"ok": False, "error": "产物无可序列化内容（无落盘文件/sections/preview）"}
    # ① 查重（防近重复污染检索池；同产物再收编 = 版本升级，直接放行走取代链）
    sim = find_similar(conn, content)
    if sim and not override and sim.get("similar_doc_id") != a.get("id"):
        prev = conn.execute(
            "SELECT id FROM documents WHERE source_artifact_id=? AND id=?",
            (artifact_id, sim["similar_doc_id"])).fetchone()
        if not prev:
            return {"ok": False, "needs_confirm": True, **sim}
    # ② 分类（多选：前端「、」连接；逐个校验词表后合并存储；用户指定优先，否则按 kind 建议）
    category = (knowledge_category or "").strip()
    if category:
        parts = [p.strip() for p in re.split(r"[、,，]", category) if p.strip()]
        valid = {r["name"] for r in conn.execute("SELECT name FROM knowledge_categories").fetchall()}
        bad = [p for p in parts if p not in valid]
        if bad:
            return {"ok": False, "error": f"知识类别不存在: {'、'.join(bad)}"}
        category = "、".join(dict.fromkeys(parts))  # 去重保序
    else:
        category = suggest_category(conn, a.get("kind") or "")
    # ③ 走完整入库管道（落盘→解析→分块→向量化）
    from knowledge_pipeline import ingest_upload_document
    result = ingest_upload_document(
        conn, filename, file_type, content, uploaded_by=actor,
        metadata={"title": a.get("title") or "", "author": "AI 生成",
                  "version": "v1.0", "tags": ["AI生成", f"artifact:{artifact_id}"]},
        branch="global")
    doc_id = result.get("doc_id")
    if result.get("parse_status") == "failed" or not doc_id:
        return {"ok": False, "error": f"入库管道失败: {result.get('error', '')}"}
    # ④ 溯源回写：文档级 + 块级 origin（消费隔离与命中标注的数据基础）
    conn.execute(
        "UPDATE documents SET origin='ai_generated', source_artifact_id=?, knowledge_category=? WHERE id=?",
        (artifact_id, category, doc_id))
    conn.execute("UPDATE document_chunks SET origin='ai_generated' WHERE document_id=?", (doc_id,))
    # ⑤ 时效取代链：同产物旧收编文档标记 superseded_by（检索侧后续可降权）
    superseded = []
    for r in conn.execute(
            "SELECT id FROM documents WHERE source_artifact_id=? AND id!=?", (artifact_id, doc_id)).fetchall():
        conn.execute("UPDATE doc_metadata SET superseded_by=? WHERE document_id=?", (doc_id, r["id"]))
        superseded.append(r["id"])
    conn.commit()
    audit(actor, "artifact_ingest",
          f"产物 #{artifact_id}「{a.get('title')}」收编入资料库 → 文档 #{doc_id}"
          f"（{result.get('chunk_count', 0)} chunks，分类: {category or '未分类'}"
          f"{('，取代 ' + str(len(superseded)) + ' 旧版') if superseded else ''}）", conn=conn)
    return {"ok": True, "doc_id": doc_id, "filename": filename,
            "chunk_count": result.get("chunk_count", 0), "category": category,
            "superseded": superseded}
