"""对话域：/api/conversations, /api/messages, /api/upload"""
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse

from core.deps import db_session, current_user
from core.config import STATIC_DIR
from repositories.conversation_repo import ConversationRepo
from repositories.studio_repo import StudioRepo
from agent import agent
from core.audit import audit, audit_user
from models import ConvIn, ChatIn, RenameIn, FlowRunIn

router = APIRouter(tags=["对话"])

# 附件上传：允许类型与大小限制（V2.3 优化：会话窗口富输入）
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                ".txt", ".md", ".csv", ".json", ".xml", ".zip"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB


def _resolve_work_branch(conn, branch: str | None) -> str:
    """AI 建模输出侧工作分支：默认 dev；校验分支存在且为可写分支。

    已发布(release)分支或分支不存在 → 回落 dev（输出侧安全默认，杜绝写入 release）。
    """
    want = (branch or "dev").strip() or "dev"
    try:
        row = conn.execute("SELECT branch_type FROM branches WHERE name=?", (want,)).fetchone()
        if row is None or row["branch_type"] == "release":
            return "dev"
    except Exception:
        return "dev"
    return want


@router.get("/api/conversations")
def list_conversations(conn=Depends(db_session)):
    return ConversationRepo(conn).list_conversations()


@router.post("/api/conversations")
def create_conversation(conv: ConvIn, conn=Depends(db_session)):
    cid = ConversationRepo(conn).create_conversation(conv.title, conv.intent, 1)
    audit("王工", "conversation_create", f"新建对话: {conv.title}", conn=conn)
    return {"id": cid, "title": conv.title}


@router.patch("/api/conversations/{conv_id}")
def rename_conversation(conv_id: int, body: RenameIn, conn=Depends(db_session)):
    """会话重命名（V2.3 优化：会话管理）。"""
    repo = ConversationRepo(conn)
    if not repo.get_conversation(conv_id):
        return JSONResponse({"error": "Conversation not found"}, 404)
    repo.rename_conversation(conv_id, body.title)
    audit("王工", "conversation_rename", f"重命名对话#{conv_id}: {body.title}", conn=conn)
    return {"id": conv_id, "title": body.title}


@router.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int, conn=Depends(db_session)):
    """删除会话：级联清理消息与反馈（V2.3 优化：会话管理）。"""
    repo = ConversationRepo(conn)
    if not repo.get_conversation(conv_id):
        return JSONResponse({"error": "Conversation not found"}, 404)
    repo.delete_conversation(conv_id)
    audit("王工", "conversation_delete", f"删除对话#{conv_id}", conn=conn)
    return {"ok": True}


@router.post("/api/upload")
def upload_file(file: UploadFile = File(...), conn=Depends(db_session)):
    """通用附件/图片上传（V2.3 优化：会话窗口富输入）。落盘 static/uploads/,返回可访问 URL。
    AI 建模：文档类附件自动进入文档管理（解析→分块→向量化→落库），逻辑同文件管理上传；
    响应携带 doc_id/parse_status/chunk_count/kb，前端据此展示入库状态。
    注意：必须为同步 def —— db_session 连接在线程池创建,async 端点跨线程使用会抛 sqlite3.ProgrammingError。"""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTS:
        return JSONResponse({"error": f"不支持的文件类型: {ext or 'unknown'}"}, 400)
    try:
        content = file.file.read()
    except Exception as e:
        return JSONResponse({"error": f"读取文件失败: {e}"}, 400)
    if len(content) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "文件超过 20MB 上限"}, 413)
    if not content:
        return JSONResponse({"error": "空文件"}, 400)
    name = f"{uuid.uuid4().hex[:12]}{ext}"
    upload_dir = os.path.join(STATIC_DIR, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    with open(os.path.join(upload_dir, name), "wb") as f:
        f.write(content)

    resp = {
        "url": f"/static/uploads/{name}",
        "filename": file.filename,
        "size": len(content),
        "is_image": ext in IMAGE_EXTS,
    }
    # ── AI 建模：文档类附件自动进入文档管理（解析→分块→向量化→落库）──
    # 逻辑与「文件管理上传」完全一致（ingest_upload_document 共用入口）；
    # 图片附件不参与（无文本可抽取，保持会话内联展示）。失败不阻断附件主流程。
    if ext not in IMAGE_EXTS:
        try:
            from knowledge_pipeline import ingest_upload_document
            file_type = file.filename.split(".")[-1] if "." in file.filename else ""
            kb = ingest_upload_document(conn, file.filename, file_type, content,
                                        uploaded_by="王工", branch="global")
            resp["doc_id"] = kb.get("doc_id")
            resp["parse_status"] = kb.get("parse_status")
            resp["chunk_count"] = kb.get("chunk_count", 0)
            resp["kb"] = kb
            if kb.get("parse_status") == "failed":
                audit("王工", "file_upload",
                      f"附件入库文档管理失败: {file.filename} → {kb.get('error', '')[:120]}", conn=conn)
        except Exception as e:
            audit("王工", "file_upload",
                  f"附件入库文档管理异常(不阻断附件): {file.filename} → {str(e)[:120]}", conn=conn)
    audit("王工", "file_upload", f"上传附件: {file.filename} ({len(content)}B)", conn=conn)
    return resp


@router.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: int, limit: int | None = None, before_id: int | None = None, conn=Depends(db_session)):
    """会话消息列表。

    性能分页（P0 修复：长会话全量返回导致前端一次性渲染数千条消息、页面卡死）：
    - limit：只返回最近 limit 条（默认不传=全量，兼容旧调用）
    - before_id：向上翻页游标，只返回 id < before_id 的消息，配合 limit 逐批加载更早消息
    返回体：{messages: [...], total: 会话消息总数}（结构升级，前端已同步适配）
    """
    repo = ConversationRepo(conn)
    msgs = repo.list_messages(conv_id, limit=limit, before_id=before_id)
    total = repo.count_messages(conv_id)
    return {"messages": msgs, "total": total}


@router.post("/api/conversations/{conv_id}/chat")
def chat(conv_id: int, body: ChatIn, conn=Depends(db_session), user=Depends(current_user)):
    conv = ConversationRepo(conn).get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Conversation not found"}, 404)

    branch = _resolve_work_branch(conn, body.branch)
    result = agent.execute(body.message, conv_id, branch, body.provider_id, body.attachments or [],
                           forced_intent=body.forced_intent or None, skill_name=body.skill_name or None,
                           team=body.team or None,
                           scope_id=body.scope_id, scope_ids=body.scope_ids, scope=body.scope,
                           user=user)
    return result


@router.post("/api/conversations/{conv_id}/chat/stream")
def chat_stream(conv_id: int, body: ChatIn, conn=Depends(db_session), user=Depends(current_user)):
    """AI 输出流式（V2.3.2 优化）。SSE 事件流：stage 阶段 / token 增量 / done 结果 / error。

    必须用同步 def + 同步生成器：db_session 连接在线程池创建，流式生成器在
    StreamingResponse 线程中执行 SQLite 读写，同步路径无跨线程问题。
    """
    conv = ConversationRepo(conn).get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Conversation not found"}, 404)

    import json as _json
    work_branch = _resolve_work_branch(conn, body.branch)

    def event_stream():
        # P1-1 停止生成：客户端断开（AbortController）→ Starlette 关闭生成器 → GeneratorExit
        # 在 yield 点抛出，这里放行让连接干净关闭；其余异常转为 error 事件（不中断会话）
        try:
            for ev in agent.execute_stream(body.message, conv_id, work_branch,
                                           body.provider_id, body.attachments or [],
                                           forced_intent=body.forced_intent or None,
                                           skill_name=body.skill_name or None,
                                           team=body.team or None,
                                           scope_id=body.scope_id, scope_ids=body.scope_ids, scope=body.scope,
                                           user=user):
                yield f"event: {ev['type']}\ndata: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except GeneratorExit:
            raise
        except Exception as _e:
            yield f"event: error\ndata: {_json.dumps({'message': str(_e)[:200]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/conversations/{conv_id}/clarify-answer")
def clarify_answer(conv_id: int, body: dict = None, conn=Depends(db_session),
                   user=Depends(current_user)):
    """内容级澄清回答：根据用户对澄清卡片（选择题/补充输入）的回答，构造续答输入并清空挂起。

    body: {answers: [{question_id, value}]}（value=选择题选项文本或自定义补充）
    返回 {ok, resume_text}——前端把 resume_text 作为新消息发送，走正常流式续答
    （含【澄清补充】标记，agent 侧跳过再次澄清，防循环）。
    """
    import json as _j
    body = body or {}
    row = conn.execute("SELECT pending_clarify FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "会话不存在"}, 404)
    if not (row["pending_clarify"] or ""):
        return JSONResponse({"error": "当前无待澄清问题（可能已作答或已过期）"}, 400)
    try:
        pending = _j.loads(row["pending_clarify"])
    except Exception:
        return JSONResponse({"error": "澄清状态异常，请刷新后重试"}, 400)
    questions = pending.get("questions") or []
    context = pending.get("context") or {}
    qmap = {str(q.get("id")): q for q in questions}
    lines = []
    for a in (body.get("answers") or []):
        qid = str(a.get("question_id") or "")
        val = str(a.get("value") or "").strip()
        if not val:
            continue
        q = qmap.get(qid, {})
        lines.append(f"问题「{q.get('question') or qid}」→ 回答：{val}")
    if not lines:
        return JSONResponse({"error": "请至少回答一个问题"}, 400)
    resume_text = (
        "【澄清补充】用户已补充以下建模信息（请据此继续，无需再确认）：\n"
        + "\n".join(lines)
        + f"\n\n原请求：{(context.get('input') or '')[:2000]}")
    conn.execute("UPDATE conversations SET pending_clarify='', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (conv_id,))
    conn.commit()
    audit("王工", "clarify_answer", f"会话#{conv_id} 澄清回答 {len(lines)} 条", conn=conn)
    return {"ok": True, "resume_text": resume_text, "answered": len(lines)}


@router.post("/api/messages/{msg_id}/feedback")
def message_feedback(msg_id: int, body: dict, conn=Depends(db_session),
                     user=Depends(current_user)):
    fb_type = body.get("type", "")
    context = body.get("context", "") or ""
    actor = (user or {}).get("display_name") or "王工"
    repo = ConversationRepo(conn)
    repo.update_message_feedback(msg_id, fb_type)
    repo.create_feedback(msg_id, fb_type, context, actor)
    audit(actor, "feedback", f"消息#{msg_id} 反馈:{fb_type}", conn=conn)
    # FR-KG-12 补 G13：修正类评审反馈（含修订意见）自动回流为 v2g 候选（失败不阻断反馈主流程）
    if fb_type in ("modify", "wrong", "corrected") and context.strip():
        try:
            from knowledge_reflow import reflow_from_review
            rf = reflow_from_review(conn, msg_id, context, fb_type=fb_type)
            if rf.get("candidates", 0) > 0:
                audit(actor, "reflow_review",
                      f"模型评审反馈回流 v2g 候选 {rf['candidates']} 条（batch {rf.get('batch_id', '')}）",
                      conn=conn)
        except Exception:
            pass
    return {"ok": True}


# ── P0：消息结论速览生成（资料库与AI建模优化 §2.2）──

@router.post("/api/messages/{msg_id}/summary")
def generate_message_summary(msg_id: int, conn=Depends(db_session)):
    """根据已有 card_data 生成「结论速览」摘要，patch 到 messages.card_data.summary 字段。

    前端在 done 事件后异步调用（不阻塞主流程）。失败兜底为空 summary + 返回 ok=True。
    """
    import json as _j
    row = conn.execute(
        "SELECT id, card_data, content FROM messages WHERE id=?", (msg_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "message not found"}, 404)
    cd_raw = row["card_data"] or "{}"
    try:
        cd = _j.loads(cd_raw) if cd_raw.strip() else {}
    except Exception:
        cd = {}
    try:
        from agent.pipeline_parts.cards import CardMixin
        summary = CardMixin.build_message_summary(cd, row["content"] or "")
        cd["summary"] = summary
        conn.execute(
            "UPDATE messages SET card_data=? WHERE id=?",
            (_j.dumps(cd, ensure_ascii=False), msg_id))
        conn.commit()
        return {"ok": True, "summary": summary}
    except Exception as e:
        # 兜底：返回规则法生成的 summary（不入库）；前端可显示降级版本
        try:
            from agent.pipeline_parts.cards import CardMixin
            return {"ok": True, "summary": CardMixin.build_message_summary(cd, row["content"] or ""),
                    "persist_error": str(e)[:120]}
        except Exception:
            return {"ok": True, "summary": {}, "error": str(e)[:120]}


@router.post("/api/conversations/{conv_id}/flow-run/{fid}")
def conversation_flow_run(conv_id: int, fid: int, body: FlowRunIn = None, conn=Depends(db_session),
                          user=Depends(current_user)):
    """方案 B：会话内运行匹配工作流——把流程执行结果写回会话（HIL 确认后触发）。

    闭环：会话匹配到工作流 → 用户点「运行此流程」→ 本接口执行 FlowExecutor →
    结果作为 assistant 消息落库（msg_type='flow_result'，card_data 存逐步明细）→ 前端展示。
    P1-1：跨表编排已上提至 services.conversation_service.ConversationService。
    """
    import json as _j
    body = body or FlowRunIn()
    from services.conversation_service import ConversationService
    result = ConversationService(conn).run_flow_in_conversation(
        conv_id, fid, body.payload or "", user=user)
    if result.get("error"):
        code = 404 if result["error"] in ("会话不存在", "流程不存在") else 400
        return JSONResponse({"error": result["error"]}, code)
    return result


# ── P2-1 知识回流：会话产出 → 提炼知识候选 → 人工确认入图谱 ──
@router.post("/api/conversations/{conv_id}/reflow")
def conversation_reflow(conv_id: int, body: dict = None, conn=Depends(db_session),
                        user=Depends(current_user)):
    """知识回流闭环：从会话最近消息提炼候选知识 → v2g_candidates（pending）→ 人工确认入图谱。

    body: {limit_messages?} 取最近 N 条（默认 20）。
    对齐 Siemens 数字线程 / visuresolutions「知识层是活资产」——分析结论不流失，回流知识库。
    """
    body = body or {}
    conv = ConversationRepo(conn).get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "会话不存在"}, 404)
    from knowledge_reflow import reflow_from_conversation
    result = reflow_from_conversation(conn, conv_id, int(body.get("limit_messages", 20) or 20))
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, 400)
    audit(audit_user(user), "knowledge_reflow",
          f"会话#{conv_id} 提炼知识候选 {result.get('candidates', 0)} 条（batch {result.get('batch_id', '')}）",
          conn=conn)
    result["ok"] = True
    result["hint"] = "候选已进入待确认队列，可到知识库「向量转图谱」面板人工确认入库"
    return result



# ── 文档片段驱动 AI 建模：建模范围（文件管理=全局数据，不分分支）──
@router.get("/api/scopes")
def list_scopes(conn=Depends(db_session), user=Depends(current_user)):
    """列出已保存建模范围。"""
    from services.scope_service import list_scopes as _ls
    return _ls(conn)

@router.post("/api/scopes")
def add_scope(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """保存建模范围：body {name, mode, doc_names, fragment_text, chunk_ids}。同名幂等覆盖。"""
    from services.scope_service import save_scope
    actor = (user or {}).get('display_name') or '王工'
    b = body or {}
    r = save_scope(conn, b.get('name') or '', b.get('mode') or 'doc',
                    doc_names=b.get('doc_names'), fragment_text=b.get('fragment_text') or '',
                    chunk_ids=b.get('chunk_ids'), created_by=actor)
    audit(actor, 'modeling_scope', f"保存建模范围 [{r.get('name')}] (mode={r.get('mode')})", conn=conn)
    return r

@router.delete("/api/scopes/{scope_id}")
def remove_scope(scope_id: int, conn=Depends(db_session), user=Depends(current_user)):
    from services.scope_service import delete_scope
    delete_scope(conn, scope_id)
    audit((user or {}).get('display_name') or '王工', 'modeling_scope', f"删除建模范围#{scope_id}", conn=conn)
    return {"ok": True}

@router.post("/api/scopes/preview")
def preview_scope(body: dict, conn=Depends(db_session)):
    """范围预览：{scope_id?} 或内联 scope。返回文档数/片段数/样例。"""
    from services.scope_service import scope_preview
    b = body or {}
    return scope_preview(conn, scope_id=b.get('scope_id'), scope=b.get('scope'))

@router.post("/api/scopes/from-text")
def scope_from_text_api(body: dict, conn=Depends(db_session)):
    """自然语言圈定：body {text} → 草稿 scope（预览用，未入库）。"""
    from services.scope_service import scope_from_text
    return scope_from_text((body or {}).get('text') or '', conn)

@router.get("/api/scopes/docs/{doc_id}/chunks")
def doc_scope_sections(doc_id: int, conn=Depends(db_session)):
    """文档切片树（section→chunk 预览），供可视圈定。"""
    from services.scope_service import doc_sections
    return doc_sections(conn, doc_id)

# ── P0-1 归一校验报告（2026-09-08）：AI 建模消息生成 V2 后的归一/消歧报告（只读）──
def _register_discoveries(conn, report: dict, source_ref: str = ""):
    """P2-10 AI 建议流：归一报告未命中(matching_status=none)的实体名自动登记发现池。

    轻量操作：不调 LLM、不建概念；幂等（同词 freq+1）；通用词/已忽略词过滤在登记端。
    任何异常不阻断报告返回。
    """
    try:
        from routers.glossary import _register_discovery
        for r in (report.get("rows") or []):
            if r.get("matching_status") == "none" and (r.get("kind") or "entity") == "entity":
                _register_discovery(conn, r.get("name") or "",
                                    context=str(r.get("instance_display") or "")[:200],
                                    source_ref=source_ref)
        conn.commit()
    except Exception:
        pass


@router.post("/api/conversations/{conv_id}/messages/{msg_id}/normalize-report")
def normalize_report(conv_id: int, msg_id: int, conn=Depends(db_session),
                     user=Depends(current_user)):
    """对一条 AI 消息生成的 V2 产物做归一/消歧/校验报告，**只读不入库**。

    来源策略：优先取 sysml_versions.content（带 view 元数据），回退取 messages.card_data.sysml_views。
    返回结构见 sysml_importer.build_normalize_report 注释。
    """
    from sysml_importer import build_normalize_report
    import json as _json
    import re as _re

    def _msg_code_text(mid: int) -> str:
        """V2 源码兜底：从消息正文抽 ```sysml 代码块（与前端 extractSysmlCode 对齐）。"""
        t = conn.execute("SELECT content FROM messages WHERE id=?", (mid,)).fetchone()
        if not (t and t["content"]):
            return ""
        m = _re.search(r"```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)?\s*\n"
                       r"([\s\S]*?)```", t["content"] or "", _re.IGNORECASE)
        return (m.group(1).strip() if m else "")

    # 1) 优先 sysml_versions（更纯，含 V2 源码）
    ver = conn.execute(
        "SELECT id, content, code_text FROM sysml_versions "
        "WHERE conversation_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
        (conv_id, msg_id)).fetchone()
    if ver:
        try:
            content = _json.loads(ver["content"] or "{}")
        except Exception:
            content = {}
        # 版本记录未落 code_text 时回退到消息正文（否则 V2 定位/证据无源码可依）
        _code = (ver["code_text"] or "").strip() or _msg_code_text(msg_id)
        report = build_normalize_report(
            conn, content, code_text=_code,
            version_id=ver["id"], message_id=msg_id)
        _register_discoveries(conn, report, source_ref=f"msg:{msg_id}")   # P2-10：AI 建议流·自动收集（非阻断）
        return report
    # 2) 回退 messages.card_data.sysml_views（UI 渲染用的视图）
    msg = conn.execute(
        "SELECT id, card_data FROM messages "
        "WHERE conversation_id=? AND id=? LIMIT 1",
        (conv_id, msg_id)).fetchone()
    if not msg:
        return JSONResponse({"error": "Message not found"}, 404)
    import json as _json
    try:
        cd = _json.loads(msg["card_data"] or "{}")
    except Exception:
        cd = {}
    sysml_views = cd.get("sysml_views") or {}
    if not sysml_views.get("views"):
        return JSONResponse({"error": "该消息不含 SysML 视图（card_data.sysml_views.views 为空）"}, 400)
    # V2 源码从 messages.content 中抽取（与前端 extractSysmlCode 逻辑对齐）
    code_text = _msg_code_text(msg_id)
    report = build_normalize_report(
        conn, {"views": sysml_views.get("views") or {}}, code_text=code_text,
        version_id=0, message_id=msg_id)
    _register_discoveries(conn, report, source_ref=f"msg:{msg_id}")
    return report


# ── P0-2 归一确认应用（2026-09-08 闸口①）：人工决策 → 更新 V2 代码与视图（新版本，不覆盖）──
@router.post("/api/conversations/{conv_id}/messages/{msg_id}/normalize-apply")
def normalize_apply(conv_id: int, msg_id: int, body: dict = None,
                    conn=Depends(db_session), user=Depends(current_user)):
    """应用人工归一决策：改写 V2 代码与视图 → 写 sysml_versions 新版本 → 可选同步词库。

    body: {decisions: {"<row_idx>": {action: accept|reject|rename|merge, new_name?, merge_to?}},
           write_glossary: bool（默认 false，仅本次生效，不污染全局词典）,
           use_ai: bool（默认 true，提交后由 AI 续写 V2 代码；不可用时自动回退确定性替换）}
    """
    from norm_apply import apply_normalization
    b = body or {}
    decisions = b.get("decisions") or {}
    if not isinstance(decisions, dict) or not decisions:
        return JSONResponse({"error": "decisions 为空"}, 400)
    actor = audit_user(user)
    try:
        res = apply_normalization(conn, conv_id, msg_id, decisions,
                                  write_glossary=bool(b.get("write_glossary")),
                                  actor=actor,
                                  use_ai=b.get("use_ai", True) is not False)
    except Exception as e:
        return JSONResponse({"error": f"归一应用失败: {e}"}, 500)
    if res.get("error"):
        return JSONResponse({"error": res["error"], "changes": res.get("changes", {})}, 400)
    audit(actor, "normalize_apply",
          f"会话#{conv_id} 消息#{msg_id} 归一确认应用 → {res.get('version_label')} "
          f"（改名 {res['changes']['renamed']} · 合并 {res['changes']['merged']} · "
          f"接受 {res['changes']['accepted']} · 拒绝 {res['changes']['rejected']}"
          f"{' · 词库 +%d' % res['glossary'].get('written', 0) if res.get('glossary') else ''}）",
          conn=conn)
    return res
