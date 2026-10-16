"""对话域：/api/conversations, /api/messages, /api/upload"""
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from core.deps import db_session, current_user
from core.config import STATIC_DIR
from database import db_conn   # B2-11：事件落库用**独立连接**（请求级 conn 在流式循环里会抢写锁）
from repositories.conversation_repo import ConversationRepo, conv_visible_to
from repositories.studio_repo import StudioRepo
from agent import get_pipeline
from core.audit import audit, audit_user
from models import ConvIn, ChatIn, ConvProjectIn, FlowRunIn

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


def _own_conv(conn, conv_id: int, user) -> dict:
    """★ 2026-10-13（B1-9）：取会话并校验归属，**不可见即 404**（不是 403）。

    为什么是 404 而不是 403：403 等于告诉调用方"这个 id 存在、只是你没权限"，
    仍可用于**探测他人会话是否存在**（枚举 conv_id）。404 对外表达"没这个资源"，
    是归属隔离的常规做法。
    ⚠️ 口径见 `repositories/conversation_repo.py::conv_visible_to`（三条，米爸 2026-10-13 确认）：
      历史无归属会话对所有人可见 / 匿名本机模式放行全部 / 其余严格按 user_id。
    """
    row = ConversationRepo(conn).get_conversation(conv_id)
    if not conv_visible_to(row, user):
        raise HTTPException(status_code=404, detail="会话不存在")
    return row or {}


@router.get("/api/conversations")
def list_conversations(conn=Depends(db_session),
                       project_id: str | None = Query(None),
                       limit: int | None = Query(None, ge=1, le=1000),
                       offset: int = Query(0, ge=0),
                       user=Depends(current_user)):
    """★ B1-9：按 user_id 收窄 + 支持按当前工程收窄 + 游标分页（2026-10-16 P1 后端）。

    · `project_id` **不传** = 不按项目过滤（保留"未分组任务"列表的现状用法）；
      传空串 `?project_id=` = 只看未分组会话；传具体值 = 只看该项目的会话。
      前端「当前工程」切换时带上该参数即可实现"关联到项目的基于项目过滤"。
    · `limit`/`offset` 为可选游标分页（默认全量，向后兼容 renderGnavProjects）。
    """
    return ConversationRepo(conn).list_conversations(
        user=user, project_id=project_id, limit=limit, offset=offset)


@router.get("/api/conversations/search")
def search_conversations(q: str = Query("", min_length=0),
                         conn=Depends(db_session),
                         user=Depends(current_user)):
    """列表搜索（P1）：标题/意图/消息正文模糊匹配，返回未分组+项目内全部命中（置顶优先由前端再排）。"""
    if not (q or "").strip():
        return []
    return ConversationRepo(conn).search_conversations(user=user, q=(q or "").strip())


@router.post("/api/conversations")
def create_conversation(conv: ConvIn, conn=Depends(db_session),
                        user=Depends(current_user)):
    """新建会话。归属语义（2026-09-28 多工程 P0-2）：

    · 请求显式给 `project_id` → 归入该项目（左侧「项目 → ＋ 新建任务」/ 顶部新建任务带当前工程）；
    · **不给 → 空串 = 无工程会话**，这是**合法状态**：知识检索 / 问答这类会话本就不读写工程，
      不该被静默塞进某个工程（用户 2026-09-28 口径）；
    · ⚠️ **不再回落** `settings.default_project_id` —— 它是全局单行、不分标签页，
      多标签并发时会被后切换的一方覆盖，导致先开的那个标签页新建会话**错归属**（断点②）。
      「当前工程」现在是前端**页面级**状态，随请求显式带上。
    """
    repo = ConversationRepo(conn)
    # ★ B1-9：归属取当前登录用户，不再硬编码 `1`。
    #   原写法 `create_conversation(conv.title, conv.intent, 1, ...)` 把所有新会话都记到
    #   user_id=1（王工）名下 —— 归属隔离若不改这里会**当场失效**（谁建的都算他的）。
    #   匿名/本机模式（user is None）沿用 1，与改动前一致（口径②：私有化单用户不受阻）。
    _uid = int((user or {}).get("id") or 1)
    cid = repo.create_conversation(conv.title, conv.intent, _uid,
                                   project_id=conv.project_id)
    row = repo.get_conversation(cid) or {}
    _pid = str(row.get("project_id") or "")
    audit(audit_user(user), "conversation_create",
          f"新建对话: {conv.title}" + (f"（项目 {_pid}）" if _pid else "（未关联工程）"), conn=conn)
    return {"id": cid, "title": conv.title, "project_id": _pid}


@router.post("/api/conversations/{conv_id}/project")
def set_conversation_project(conv_id: int, body: ConvProjectIn,
                             conn=Depends(db_session), user=Depends(current_user)):
    """把会话归入项目（**收敛入口**）：无工程会话 → 归属某项目；传空串 = 解除归属。

    为什么单开一个端点而不是塞进 PATCH（重命名）：归属变更是**独立动作**，需要独立审计留痕，
    且它要校验项目是否存在（不能把会话挂到一个不存在的 id 上，那会变成孤儿归属）。
    ★ B1-9：先校验会话归属（越权改他人会话的归属 = 越权移动别人的资产）。
    """
    repo = ConversationRepo(conn)
    _own_conv(conn, conv_id, user)
    pid = (body.project_id or "").strip()
    if pid:
        from repositories.project_repo import ProjectRepo
        if not ProjectRepo(conn).get_project(pid):
            return JSONResponse({"error": "项目不存在"}, 400)
    if not repo.set_conversation_project(conv_id, pid):
        return JSONResponse({"error": "Conversation not found"}, 404)
    _old = str((repo.get_conversation(conv_id) or {}).get("project_id") or "")
    audit(audit_user(user), "conversation_project_set",
          f"会话#{conv_id} 归属变更: {_old or '未关联工程'} → {pid or '未关联工程'}", conn=conn)
    return {"ok": True, "id": conv_id, "project_id": pid}


@router.patch("/api/conversations/{conv_id}")
def rename_conversation(conv_id: int, body: dict = None, conn=Depends(db_session),
                        user=Depends(current_user)):
    """会话重命名 / 置顶（V2.3 优化：会话管理 + 2026-10-16 P2 收藏）。

    ★ B1-9：校验归属（此前任何 conv_id 都能改名）。body 支持：
      · {"title": "..."}            重命名
      · {"pinned": true|false}      置顶 / 取消置顶
      · 二者可同时给（dict 解析，与文件内多数端点一致）。
    """
    repo = ConversationRepo(conn)
    _own_conv(conn, conv_id, user)
    body = body or {}
    out: dict = {"id": conv_id}
    if body.get("title"):
        repo.rename_conversation(conv_id, str(body["title"]))
        out["title"] = str(body["title"])
    if body.get("pinned") is not None:
        repo.set_pinned(conv_id, bool(body["pinned"]))
        out["pinned"] = bool(body["pinned"])
    if len(out) <= 1:   # 只含 id，无任何有效字段
        return JSONResponse({"error": "无可更新字段（title / pinned）"}, 400)
    if "title" in out:
        audit(audit_user(user), "conversation_rename", f"重命名对话#{conv_id}: {out['title']}", conn=conn)
    if "pinned" in out:
        audit(audit_user(user), "conversation_pin",
              f"对话#{conv_id} {'置顶' if out['pinned'] else '取消置顶'}", conn=conn)
    return out


@router.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int, conn=Depends(db_session),
                        user=Depends(current_user)):
    """删除会话：级联清理消息与反馈（V2.3 优化：会话管理）。

    ★ B1-9：删除是**破坏性且不可逆**的操作（messages/feedback/artifacts 级联清理），
    此前只要猜到 conv_id 就能删别人的会话 —— 本次加归属校验（不可见即 404）。
    """
    repo = ConversationRepo(conn)
    _own_conv(conn, conv_id, user)
    repo.delete_conversation(conv_id)
    # P1-4（2026-10-02）：级联清理该会话的工具结果 offload（会话没了，全文引用失去意义）
    try:
        from agent.pipeline_parts import tool_offload as _to
        _to.cleanup_conversation(conv_id)
    except Exception:
        pass  # 级联清理失败不阻断删除主流程（offload 由 TTL 兜底）
    audit(audit_user(user), "conversation_delete", f"删除对话#{conv_id}", conn=conn)
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
def get_messages(conv_id: int, limit: int | None = None, before_id: int | None = None,
                 conn=Depends(db_session), user=Depends(current_user)):
    """会话消息列表。

    性能分页（P0 修复：长会话全量返回导致前端一次性渲染数千条消息、页面卡死）：
    - limit：只返回最近 limit 条（默认不传=全量，兼容旧调用）
    - before_id：向上翻页游标，只返回 id < before_id 的消息，配合 limit 逐批加载更早消息
    返回体：{messages: [...], total: 会话消息总数}（结构升级，前端已同步适配）

    ★ B1-9：加归属校验 —— 此前任何 conv_id 都能读到**别人会话的全部消息内容**，
    这是本次修复里数据暴露面最大的一个端点。
    """
    _own_conv(conn, conv_id, user)
    repo = ConversationRepo(conn)
    msgs = repo.list_messages(conv_id, limit=limit, before_id=before_id)
    total = repo.count_messages(conv_id)
    # 2026-09-25：带上「是否仍有待澄清」——前端据此决定历史澄清卡是**可作答**还是只读摘要。
    # 此前只读：刷新/重开会话后澄清卡变成一行文字、没有任何作答入口（用户反馈"没有输入入口"），
    # 而服务端 pending_clarify 还挂着 → 用户只能靠打新消息继续，且挂起状态永不清除。
    pending = None
    row = conn.execute("SELECT pending_clarify FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if row and (row["pending_clarify"] or ""):
        try:
            import json as _pj
            pending = _pj.loads(row["pending_clarify"])
        except Exception:
            pending = None
    # 2026-10-16 R1：带上会话级绑定的主 Agent（main_agent），前端 selectConv 据此回填
    #   `window._convMainAgent`，使"这个会话一直由同一个主 Agent 执行"在刷新/切换会话后也成立
    #   （此前只靠 localStorage，与库里真实绑定易漂移）。空串=本会话从未绑定（待用户在发起时选）。
    main_agent = ""
    try:
        _ma = conn.execute("SELECT main_agent FROM conversations WHERE id=?", (conv_id,)).fetchone()
        main_agent = (_ma["main_agent"] or "") if _ma else ""
    except Exception:
        pass
    return {"messages": msgs, "total": total, "pending_clarify": pending,
            "main_agent": main_agent}


@router.post("/api/conversations/{conv_id}/chat")
def chat(conv_id: int, body: ChatIn, conn=Depends(db_session), user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    conv = ConversationRepo(conn).get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Conversation not found"}, 404)

    branch = _resolve_work_branch(conn, body.branch)
    # P1-31（2026-10-09，多用户并发）：**每请求新建 Pipeline**，不用模块级单例。
    # 实测依据（`tools/verify/verify_pipeline_concurrency.py`）：
    #   8 线程并发下共享单例的请求级字段被覆盖 **7/8**；
    #   每请求新建为 **0/8**。构造成本仅 ≈0.001 ms，可忽略。
    # ⚠️ 必须在**函数体内**取实例，不能提到模块级（那就又变回单例了）。
    _pipe = get_pipeline()
    result = _pipe.execute(body.message, conv_id, branch, body.provider_id, body.attachments or [],
                           forced_intent=body.intent_override or None, skill_name=body.skill_name or None,
                           team=body.team or None,
                           scope_id=body.scope_id, scope_ids=body.scope_ids, scope=body.scope,
                           user=user)
    return result


@router.post("/api/conversations/{conv_id}/chat/stream")
async def chat_stream(conv_id: int, body: ChatIn, conn=Depends(db_session),
                      user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    """AI 输出流式（V2.3.2 优化）。SSE 事件流：stage 阶段 / token 增量 / done 结果 / error。

    ⚠️ **2026-10-04（P0-1）由 `def` 改为 `async def`** —— 此处原有一段注释断言
    "必须用同步 def + 同步生成器：db_session 连接在线程池创建，流式生成器在
    StreamingResponse 线程中执行 SQLite 读写，同步路径无跨线程问题"。
    **该论断的结论是错的**，已更正：
      · 同步 `def` 端点返回同步生成器时，Starlette 用 `iterate_in_threadpool` 消费，
        **每产出一帧都要占一次线程池线程**；而单轮编排实测 344~1155 s、
        anyio 线程池上限 40 ⇒ 约 30 个并发 SSE 就会打满线程池，
        **连与 SSE 无关的普通读接口也一起排队**。
      · 同步 SQLite 并不是理由：内部阻塞由 `iter_with_heartbeat_async` 的
        **自建 daemon 线程**承担，主协程只在 `await queue.get()` 处让出 ——
        SQLite 读写仍发生在 worker 线程（与 `check_same_thread=False` 的既有设置一致），
        并没有跑到 event loop 上。
    保留同步版 `iter_with_heartbeat`（flows.py 在用），两者语义一致、并存。
    """
    conv = ConversationRepo(conn).get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Conversation not found"}, 404)

    import json as _json
    work_branch = _resolve_work_branch(conn, body.branch)

    # ── P1-3（2026-10-03 降级显式化）：任务开始前快照 LLM 降级累计计数 ──
    # 判据为什么用**累计数差值**而不是瞬时值 `stats.last_used_mock`：
    # 编排链路的子任务跑在 ThreadPoolExecutor 里、`llm_client` 是**进程单例**，
    # 瞬时值会被并发批次互相覆盖（最后一个子任务的状态冒充整批的状态）。
    # 累计计数只增不减 ⇒ 差值即"本轮实际发生过几次降级"，语义严格。
    # ⚠️ 已知局限：单例计数与多会话并发共存时会互相计入（偏保守，宁可多报不可漏报）；
    #    彻底隔离需 request-scoped 上下文（依赖入口带 conversation_id），留作后续。
    from core.sse import snapshot as _snap, degrade_delta as _delta
    from llm import llm_client as _llm_singleton   # 延迟导入：避免路由加载期即连库

    def _degrade_snapshot() -> dict:
        return _snap(getattr(_llm_singleton, "stats", {}) or {})

    _degrade_base = _degrade_snapshot()

    async def event_stream():
        # P1-2：首帧声明重连间隔（SSE 协议字段），并立刻产一帧心跳，
        # 让 nginx/网关在本轮第一个 LLM 请求发出前就看到"连接活跃"。
        yield "retry: 3000\n\n"
        yield ": connected\n\n"
        # P1-31（2026-10-09，多用户并发）：**每请求新建 Pipeline**（同 chat 端点）。
        # ⚠️ 建在生成器**体内**而非模块级/外层—— 建在外层等于每次请求建一个，
        #    但若提到模块级就退回单例了；建在体内保证「一个 SSE 连接 = 一个实例」。
        _pipe = get_pipeline()
        # ⚠️ 客户端已断开标志：GeneratorExit 期间**禁止再 yield**
        # （CPython 会抛 "generator ignored GeneratorExit"，且该异常发生在收尾路径，
        #  比断连本身更难排查）。下面的 finally 必须先判这个标志才能补发降级告知。
        _client_gone = False
        _frames = None
        # ── B2-11 事件落库用的缓冲与状态（建在生成器体内，每请求独立）──
        # `_evbuf` 收 stage/tool 帧；`_orch_run_id[0]` 收到 done 后才拿到真批次号；
        # `_ev_flushing` 防同一次流里重复刷库（done 之后还有 token 帧时会再进循环）。
        _evbuf = []
        _evbuf_max = 200
        _orch_run_id = [0]
        _ev_flushing = False
        _et = None
        _maxr = 0
        _SER_TYPES = ("stage", "tool")
        _terminal = False   # P1-32：本流是否已发过 done/error（终态哨兵）
        # P1-1 停止生成：客户端断开（AbortController）→ Starlette 关闭生成器 → GeneratorExit
        # 在 yield 点抛出，这里放行让连接干净关闭；其余异常转为 error 事件（不中断会话）
        try:
            # P0-1：用**异步**包装器 —— 消费侧不占 anyio 线程池线程（见端点 docstring 的更正说明）
            from core.sse import iter_with_heartbeat_async
            _frames = iter_with_heartbeat_async(
                _pipe.execute_stream(body.message, conv_id, work_branch,
                                     body.provider_id, body.attachments or [],
                                     # ★ 2026-10-16：前端不再发 forced_intent；澄清改选走 intent_override，
                                     #   且后端 team 分支会让主 Agent 覆盖它（用户所选主 Agent 为最高优先）。
                                     forced_intent=body.intent_override or None,
                                     skill_name=body.skill_name or None,
                                     team=body.team or None,
                                     scope_id=body.scope_id, scope_ids=body.scope_ids, scope=body.scope,
                                     user=user),
                # P1-2：15 s —— 链路上最小 idle timeout 通常是 nginx 默认 60 s，取 1/4 留余量
                interval_s=15.0,
                # P1-33（2026-10-10）：空闲心跳改为可见 ping 事件（带当前阶段+已耗时）。
                # 实测背景：LLM 网关/embedding 故障时单步重试链达数分钟，注释帧 `: ping`
                # 前端无法展示 ⇒ 用户只看到无限转圈，以为系统死了。
                visible_ping=True,
            )
            async for raw in _frames:
                # P1-32（2026-10-10）：终态哨兵 —— 记录是否发过 done/error。
                # 实测事故（会话 9336 msg 3895/3896）：pipeline 内部吞掉 LLM 异常后
                # 生成器**正常 return**（不抛异常）⇒ 这里 except 抓不到、done 也没有
                # ⇒ 前端 _streaming 锁死、转圈到天荒地老；用户随后发的「重试」被当成
                # followup 投进已死的轮次，**永远不会被消费** —— 即"重试无任何响应"。
                # （本哨兵只认帧前缀，不解析 JSON —— 解析失败也不能影响转发。）
                if isinstance(raw, str) and raw.startswith(("event: done", "event: error")):
                    _terminal = True
                # ── B2-11（2026-10-13）：编排关键事件落库 ──
                # 落点选这里，因为 `async for raw in _frames` 是**真正唯一的收口**：
                # 生成器内部 88 处 yield 分散、无收口（编排子任务的 _evq 队列只带子任务事件，
                # 主路径事件不经它）⇒ 改一处即可覆盖全部事件，不碰生成器语义。
                #
                # ⚠️ **run_id 只在最后一个事件（done）里才带**（`data.orch_run_id`，
                #    本次一并补上）⇒ 流中途拿不到批次号。**不缓冲的话只能落 run_id=0 的孤岛行**，
                #    那正是本项要修的"数据无法归到某次编排"的同一种毛病。
                #    故：**内存缓冲 → 收到 done 后带真 run_id 一次性刷入**。
                #    缓冲有上限（`_evbuf_max`），超了丢弃最旧的（防异常流把内存吃满）。
                # 只落 stage/tool（services/stream_event_recorder.PERSIST_TYPES）：
                # token 与 reasoning 是**逐字增量**，一次真实建模可达数百上千帧。
                # ⚠️ 落库用**独立连接**：请求级 `conn` 在流式循环里写会与主流程抢写锁。
                try:
                    from core.config import get as _cfg_get
                    if _cfg_get("stream_event_log", "enabled", True):
                        _maxr = int(_cfg_get("stream_event_log", "max_per_run", 200) or 0)
                        from services.stream_event_recorder import (
                            parse_frame as _ser_parse, record as _ser_record)
                        _et, _pl = _ser_parse(raw)
                        if _et == "done":
                            try:
                                _orch_run_id[0] = int((_pl.get("data") or {}).get("orch_run_id") or 0)
                            except Exception:
                                _orch_run_id[0] = 0
                        elif _et in _SER_TYPES:
                            _evbuf.append(raw)
                            if len(_evbuf) > _evbuf_max:
                                del _evbuf[:len(_evbuf) - _evbuf_max]
                except Exception:
                    pass  # 观测收集失败绝不影响用户拿结果
                # ── 缓冲刷库：收到 done/error 时执行一次 ──
                #   放在 yield **之前**：yield 之后代码要等下一次 __anext__ 才继续跑，
                #   而客户端断连（GeneratorExit）会让生成器停在 yield 处 ⇒ 缓冲永不清空。
                if _evbuf and _et in ("done", "error"):
                    _ev_flushing = True
                    try:
                        with db_conn() as _evc:
                            for _f in _evbuf:
                                _ser_record(_evc, conv_id, _f,
                                            run_id=int(_orch_run_id[0]),
                                            task_key="", max_per_run=_maxr)
                            _evc.commit()
                    except Exception:
                        pass
                    _evbuf[:] = []
                    _ev_flushing = False
                yield raw
        except GeneratorExit:
            _client_gone = True
            # 断连止损：显式关闭心跳包装器 → 其内部会连带 close 事件源
            #（core/sse.py 的收尾逻辑；源生成器此时正跑在 worker 线程里，
            #  CPython 会抛 ValueError: generator already executing —— 属预期，吞掉即可）。
            try:
                if _frames is not None:
                    await _frames.aclose()
            except Exception:
                pass
            raise
        except Exception as _e:
            # P1-32：本分支也要置终态 —— 否则 finally 的兜底会再补一条 error，
            # 同一条流里出现**两条互相矛盾的错误提示**（门禁 I11e 实证抓到：
            # 除错分支那条 + 兜底那条）。兜底只在"从未发过终态"时才该出现。
            _terminal = True
            yield f"event: error\ndata: {_json.dumps({'message': str(_e)[:200]}, ensure_ascii=False)}\n\n"
        finally:
            if _client_gone:
                return
            # P1-32：终态兜底 —— 流结束却从未发过 done/error ⇒ 必须补一条 error，
            # 让前端解锁（_streaming=false、停止按钮复位、失败横条出现）。
            # 没有这条，pipeline 内部任何"吞异常后正常返回"的路径都会让前端永久转圈。
            if not _terminal:
                # P1-36（2026-10-10）：本轮异常终止 ⇒ **生成期间投进来的补充指令不会被执行**
                #（follow-up 只在轮次边界消费，见 stream.py:1807）。用户点「重试」时看到的是
                # "已作为补充指令提交，本轮结束后并入处理"，而本轮已死 ⇒ 队列永久滞留
                # ⇒ 这正是"重发重试未有任何响应"的最后一块拼图：不告诉用户，他不知道要重发。
                _pending_fu = 0
                try:
                    from agent import followup as _fup
                    _pending_fu = int(_fup.pending_count(conv_id) or 0)
                except Exception:
                    _pending_fu = 0
                _msg = ("执行流程异常终止（未收到完成事件）。本次未产出结果，请直接重新发送消息重试；"
                        "若反复出现，请到「AI 对话流」查看服务日志或联系管理员。")
                if _pending_fu:
                    _msg += ("另检测到 %d 条生成期间提交的补充指令未被执行（本轮已终止），"
                             "请重新发送。" % _pending_fu)
                yield ("event: error\ndata: " + _json.dumps(
                    {"message": _msg}, ensure_ascii=False) + "\n\n")
            # P1-3：无论成功/失败，只要本轮发生过降级，**必须在流里显式说清楚**。
            # 对标 Manus/Dify：模型不可用或回退到非预期实现时必须在事件流里可见，
            # 而不是让用户拿着一段看起来正常的文字，误以为是目标模型的产出。
            try:
                d = _delta(_degrade_base, _degrade_snapshot())
                if d["mock_calls"] or d["fallback_calls"]:
                    yield ("event: degraded\ndata: " + _json.dumps({
                        "mock_calls": d["mock_calls"], "real_calls": d["real_calls"],
                        "fallback_calls": d["fallback_calls"],
                        "message": ("本次回答中有 %d 次调用未真实到达模型、由本地兜底（Mock）生成%s"
                                    % (d["mock_calls"],
                                       ("；另有 %d 次发生 Provider 回退" % d["fallback_calls"])
                                       if d["fallback_calls"] else "")),
                    }, ensure_ascii=False) + "\n\n")
            except Exception:
                pass   # 降级告知失败绝不阻断已完成的主流程

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/conversations/{conv_id}/summary")
def conversation_summary(conv_id: int, conn=Depends(db_session),
                         user=Depends(current_user)):
    """会话摘要（左侧任务列表 hover 信息卡用）。

    body: {summary, source, msg_count, updated_at, intent}
      source = "llm"（模型概括）/ "structured"（结构化概述降级）/ "empty"（无内容）

    2026-09-28 新增：此前前端 hover 卡是「取最后 1 条消息 → 去符号 → slice(0,140)」的
    **原文截断**，用户反馈"感觉像是直接截取了一段会话内容"。改为服务端真正概括，
    并带进程内缓存（按 msg_count 失效）。

    同时回传 msg_count / updated_at / intent：hover 卡的这几行此前依赖 DOM 上的
    `data-*` 属性，而**不同入口（未分组任务 / 项目分组）挂的属性并不一致**（用户反馈
    "项目下的会话 hover 展示与非项目下不一致"）。接口作为唯一真源后，前端可在属性缺失时回填，
    从机制上消除不一致。
    """
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    row = conn.execute(
        "SELECT updated_at, intent FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "会话不存在"}, 404)
    repo = ConversationRepo(conn)
    # 取全量消息送摘要：模块内部只消费首尾若干条（首=需求背景，尾=结论），
    # 故这里不必传 limit —— 传了反而可能拿不到"首条用户请求"。
    msgs = repo.list_messages(conv_id)
    from services.conv_summary import summarize
    out = summarize(conn, conv_id, msgs)
    out["updated_at"] = row["updated_at"] or ""
    out["intent"] = row["intent"] or ""
    return out


@router.post("/api/conversations/{conv_id}/clarify-answer")
def clarify_answer(conv_id: int, body: dict = None, conn=Depends(db_session),
                   user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    """内容级澄清回答：根据用户对澄清卡片（选择题/补充输入）的回答，构造续答输入并清空挂起。

    body: {answers: [{question_id, value}], note: "..."}（value=选择题选项文本或自定义补充；
          note=卡片末步「补充说明（选填）」的多行文本，**与 answers 同时生效**）
          或 {free_text: "..."}（**自由文本作答**：用户直接在主输入框打的那段话）
    返回 {ok, resume_text}——前端把 resume_text 作为新消息发送，走正常流式续答
    （含【澄清补充】标记，agent 侧跳过再次澄清，防循环）。

    2026-09-28 新增 note 字段：澄清卡改为「一次一题」向导后，末步是「补充说明（选填）」多行输入。
    此前只有 answers 一条路，note 若塞进 free_text 会把 answers **整体丢弃**（free_text 分支
    不吃 answers），故单独成字段追加在选项回答之后。

    2026-09-25 新增 free_text 分支：此前只有「选择题 answers」一条路，用户在**主输入框**
    打字提交并不构成澄清回答（既不构造 resume_text、也不清空挂起）→ 表现为"没有输入入口"
    且挂起状态永不清除（AI 会反复追问）。现支持把主输入框内容直接当作澄清补充。
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
    free_text = str(body.get("free_text") or "").strip()
    if free_text:
        resume_text = (
            "【澄清补充】用户在待澄清状态下直接补充了以下信息（请据此继续，无需再确认）：\n"
            + free_text[:4000]
            + f"\n\n原请求：{(context.get('input') or '')[:2000]}"
        )
    else:
        qmap = {str(q.get("id")): q for q in questions}
        lines = []
        for a in (body.get("answers") or []):
            qid = str(a.get("question_id") or "")
            val = str(a.get("value") or "").strip()
            if not val:
                continue
            q = qmap.get(qid, {})
            lines.append(f"问题「{q.get('question') or qid}」→ 回答：{val}")
        # 2026-09-28：末步「补充说明（选填）」—— 只有 note、没选任何选项也算有效作答
        note = str(body.get("note") or "").strip()
        if note:
            lines.append("补充说明：" + note[:2000])
        if not lines:
            return JSONResponse({"error": "请至少回答一个问题或填写补充说明"}, 400)
        resume_text = (
            "【澄清补充】用户已补充以下建模信息（请据此继续，无需再确认）：\n"
            + "\n".join(lines)
            + f"\n\n原请求：{(context.get('input') or '')[:2000]}"
        )
    conn.execute("UPDATE conversations SET pending_clarify='', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (conv_id,))
    conn.commit()
    _n = len((body.get("answers") or []))
    _tail = '自由文本' if free_text else (f"{_n} 条选项" + (f" + 补充说明" if (body.get("note") or "").strip() else ""))
    audit("王工", "clarify_answer", f"会话#{conv_id} 澄清回答（{_tail}）", conn=conn)
    return {"ok": True, "resume_text": resume_text, "free_text": bool(free_text)}


@router.post("/api/conversations/{conv_id}/messages/partial")
def commit_partial_message(conv_id: int, body: dict = None, conn=Depends(db_session),
                           user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    """固化「已生成但未完成」的 AI 输出（2026-09-25）。

    触发场景：用户点「停止生成」，或流式输出中直接提交了新消息（后者会 abort 上一轮）。
    此前两条路径都**只在浏览器 DOM 里渲染**（`renderMessage({id:0})`，无落库），
    后端在客户端断开时 `raise GeneratorExit` 直接放行 → 刷新即丢。
    现由前端在收口时调用本端点，把已生成内容写成一条 assistant 消息并返回 message_id，
    前端用真实 id 重渲染，保证「刷新后仍在」。

    body: {content: "已生成正文", stopped: true}——content 为空直接 400（不写空消息）。
    """
    body = body or {}
    content = str(body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "无可固化内容（content 为空）"}, 400)
    repo = ConversationRepo(conn)
    if not repo.get_conversation(conv_id):
        return JSONResponse({"error": "Conversation not found"}, 404)
    suffix = "\n\n> ⏹ 已停止生成（以上为已生成内容）" if body.get("stopped", True) else ""
    # ── 去重（2026-10-10 修，实测会话 9335 出现两条几乎相同的 assistant 消息）──
    # ★根因：用户点停止时**正常完成路径可能已落过一条**，本端点又写一条
    #   ⇒ 库里两条内容相同（id=3884 len=2832 / id=3885 len=2853）。
    #   后端 `_persist_partial_stream` 已按内容去重，本端点是**另一条写入路径**，
    #   同样要防 —— 否则修了后端、前端这条路径仍会双写。
    #判据：最后一条 assistant 去掉中断后缀后，与本次内容相同 ⇒ 不重复写。
    # ⚠️2026-10-10 P0 修复（settrace 实测取证，勿回退）：
    #   ① SELECT 必须**带上 id 列** —— 原写法只 SELECT role, content，
    #      而下面 return 要取 `_last["id"]` ⇒ 命中去重时抛
    #      `IndexError: No item with that key` ⇒ 被下面 except 吞掉
    #      ⇒ **去重从未真正生效过**（会话 9336 实测：正常落库 + 停止固化 = 2 条）。
    #   ② 查重失败必须**留痕**（原来 `pass` 静默，与"兜底必须可见"相悖）。
    #      判红口径见 tools/verify/verify_dedup_and_finalize_hint.py。
    try:
        _last = conn.execute(
            "SELECT id, role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
            (conv_id,)).fetchone()
        if _last and _last["role"] == "assistant":
            _lc = (_last["content"] or "").strip()
            _marker = "以上为已生成内容"
            if _marker in _lc:
                # 已是固化消息：本次内容若已被它覆盖则跳过
                _lc_body = _lc.split("> ⏹", 1)[0].strip()
                if _lc_body == content.strip() or _lc_body.startswith(content.strip()[:200]):
                    return {"ok": True, "id": _last["id"], "deduped": True}
            elif _lc == content.strip() or (len(_lc) >= 200
                                            and content.strip().startswith(_lc[:200])):
                return {"ok": True, "id": _last["id"], "deduped": True}
    except Exception as _dedup_err:
        # 查重失败**不阻断固化**（宁可多写一条，也不能丢内容），但必须留痕：
        # 静默 pass 会让"去重失效"这类缺陷完全不可见（2026-10-10 就是这么漏了一整天）。
        print(f"[commit_partial] 去重判据异常，会退化为写入一条：{_dedup_err!r}")
    mid = repo.add_assistant_message(conv_id, content + suffix, msg_type="text")
    audit("王工", "conversation_partial_commit", f"会话#{conv_id} 固化已生成内容（{len(content)} 字）", conn=conn)
    return {"ok": True, "id": mid}


@router.post("/api/conversations/{conv_id}/followup")
def post_followup(conv_id: int, body: dict = None, conn=Depends(db_session),
                  user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    """投递 follow-up（2026-10-07，P1-② 阶段 1）：**在生成过程中**追加指令。

    场景：Agent 还在跑（正在调工具/正在流式输出），用户发现漏说了什么，
    直接补一句 —— 不用等这一轮跑完，也不用打断它。
    当前轮结束后，ReAct 循环会在**轮次边界**取走队列并重启一轮。

    body: {message: "补充指令"}

    ⚠️ **为什么必须校验会话归属**：`agent` 是**模块级单例**、follow-up 队列按
    `conv_id` 分槽。若不校验归属，任何登录用户都能往**别人**的会话投指令，
    而那轮对话**会把这段指令当作用户本人说的**执行 ⇒ 越权注入。
    （与 MEMORY「服务端强制过滤，不信前端传参」同族。）
    """
    from agent import followup as _fup
    body = body or {}
    msg = str(body.get("message") or "").strip()
    if not msg:
        return JSONResponse({"error": "补充指令不能为空"}, 400)
    uid = None
    if isinstance(user, dict):
        uid = user.get("id")
    row = conn.execute("SELECT user_id FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "Conversation not found"}, 404)
    owner = row["user_id"] if row["user_id"] not in (None, 0) else None
    # 口径：会话无 owner（历史数据/系统建）⇒ 不拦；有owner ⇒ 必须匹配。
    # ⚠️ `uid` 为 None（匿名/enforce_login 关闭）时也要求匹配 —— 这是安全默认：
    #    宁可"匿名投不进去"，也不可"匿名能往他人会话注入指令"。
    if owner is not None and uid != owner:
        return JSONResponse({"error": "无权向该会话投递指令"}, 403)
    if not _fup.push(conv_id, msg):
        return JSONResponse({"error": "投递失败（指令为空或会话号非法）"}, 400)
    pending = _fup.pending_count(conv_id)
    audit(audit_user(user), "conversation_followup",
          "会话#%d 投递 follow-up（%d 字，待处理 %d 条）" % (conv_id, len(msg), pending), conn=conn)
    return {"ok": True, "pending": pending}


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
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
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
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
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


def _msg_code_text(conn, mid: int) -> str:
    """V2 源码兜底：从消息正文抽 ```sysml 代码块（与前端 extractSysmlCode 对齐）。"""
    import re as _re
    t = conn.execute("SELECT content FROM messages WHERE id=?", (mid,)).fetchone()
    if not (t and t["content"]):
        return ""
    m = _re.search(r"```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)?\s*\n"
                   r"([\s\S]*?)```", t["content"] or "", _re.IGNORECASE)
    return (m.group(1).strip() if m else "")


def _load_norm_report(conn, conv_id: int, msg_id: int):
    """归一报告的**取数与构建**（2026-10-10 从 normalize_report 主体**纯搬运**抽出，行为逐字不变）。

    来源策略：优先 sysml_versions.content（带 view 元数据），回退 messages.card_data.sysml_views。
    返回 (report, None) 成功 / (None, JSONResponse) 失败。

    ⚠️ 本函数**不做任何写操作**。发现池登记（_register_discoveries）交由**调用方**决定：
       · POST /normalize-report → 登记（P2-10 AI 建议流）
       · GET  /normalize-summary → **不登记**（只读统计，会被卡片渲染反复调用）
    """
    from sysml_importer import build_normalize_report
    import json as _json

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
        _code = (ver["code_text"] or "").strip() or _msg_code_text(conn, msg_id)
        report = build_normalize_report(
            conn, content, code_text=_code,
            version_id=ver["id"], message_id=msg_id)
        return report, None
    # 2) 回退 messages.card_data.sysml_views（UI 渲染用的视图）
    msg = conn.execute(
        "SELECT id, card_data FROM messages "
        "WHERE conversation_id=? AND id=? LIMIT 1",
        (conv_id, msg_id)).fetchone()
    if not msg:
        return None, JSONResponse({"error": "Message not found"}, 404)
    try:
        cd = _json.loads(msg["card_data"] or "{}")
    except Exception:
        cd = {}
    sysml_views = cd.get("sysml_views") or {}
    if not sysml_views.get("views"):
        return None, JSONResponse(
            {"error": "该消息不含 SysML 视图（card_data.sysml_views.views 为空）"}, 400)
    # V2 源码从 messages.content 中抽取（与前端 extractSysmlCode 逻辑对齐）
    code_text = _msg_code_text(conn, msg_id)
    report = build_normalize_report(
        conn, {"views": sysml_views.get("views") or {}}, code_text=code_text,
        version_id=0, message_id=msg_id)
    return report, None


@router.post("/api/conversations/{conv_id}/messages/{msg_id}/normalize-report")
def normalize_report(conv_id: int, msg_id: int, conn=Depends(db_session),
                     user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    """对一条 AI 消息生成的 V2 产物做归一/消歧/校验报告。

    来源策略：优先取 sysml_versions.content（带 view 元数据），回退取 messages.card_data.sysml_views。
    返回结构见 sysml_importer.build_normalize_report 注释。
    副作用：把未命中（matching_status=none）的实体名登记进发现池（P2-10 AI 建议流）。
    """
    report, err = _load_norm_report(conn, conv_id, msg_id)
    if err:
        return err
    _register_discoveries(conn, report, source_ref=f"msg:{msg_id}")   # P2-10：AI 建议流·自动收集（非阻断）
    return report


# 前端 `_NR_BLOCKING`（static/js/mods/06-cards.js）的**后端镜像** —— 两边必须逐项一致，
# 否则按钮徽标上的数字会与清单卡头的「待确认 N 项」对不上。
# 校验见 tools/verify/verify_norm_summary.py（D3 用例：grep 两端常量比对）。
# ⚠️ 这是与前端 `_NR_BLOCKING`（static/js/mods/06-cards.js）的**镜像常量**，改一处必须改另一处
#    （`tools/verify/verify_norm_summary.py` 的 D3 断言按**运行时属性**比对，漏改立刻判红）。
# ⚠️ 本行的字面量还被 verify_norm_summary 的变异自证当**锚点**用（ANCHOR），
#    它的变异体正是"故意漏掉 dup_suspect"那个 5 项元组。
#    ⇒ **跑该脚本时若被强杀（SIGTERM/超时），变异体会残留在本文件里**，
#      表现为 D3/D4 判红且 `assert ANCHOR in ORIG` 报「锚点未找到，自证中止」。
#      2026-10-12 实测踩过：沙箱里被 SIGTERM 打断后文件留在变异态，
#      一度被误判成"加 related 时误删了 dup_suspect"的产品缺陷。
_NR_BLOCKING_STATUSES = ('matched_low', 'dup_high', 'dup_suspect', 'none', 'related', 'rejected')


@router.get("/api/conversations/{conv_id}/messages/{msg_id}/normalize-summary")
def normalize_summary(conv_id: int, msg_id: int, conn=Depends(db_session),
                      user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
    """归一校验的**只读统计**：供代码卡「归一校验」按钮在**点开前**显示待确认数量与构成。

    与 POST /normalize-report 的三点区别（重要）：
      ① 只返回统计数字，**不返回 rows**（传输量小，够按钮徽标用）；
      ② **不调 _register_discoveries** —— 那会把未命中词写进发现池（freq+1）。
         本接口由卡片渲染触发、会**被反复调用**，必须严格只读；
      ③ 判定口径与前端 `_nrIsBlocking` 完全一致（见 _NR_BLOCKING_STATUSES）。
    """
    report, err = _load_norm_report(conn, conv_id, msg_id)
    if err:
        try:
            detail = (getattr(err, "body", b"") or b"").decode("utf-8", "ignore")
        except Exception:
            detail = ""
        return {"ok": False, "msg_id": msg_id, "reason": detail or "无可用 SysML 视图"}
    rows = report.get("rows") or []
    by_kind, need_by_kind = {}, {}
    for r in rows:
        k = r.get("kind") or "entity"
        by_kind[k] = by_kind.get(k, 0) + 1
        if (r.get("matching_status") or "") in _NR_BLOCKING_STATUSES:
            need_by_kind[k] = need_by_kind.get(k, 0) + 1
    cons = report.get("consistency") or {}
    return {"ok": True, "msg_id": msg_id,
            "total": len(rows),
            "need_decide": sum(need_by_kind.values()),
            "by_kind": by_kind,
            "need_by_kind": need_by_kind,
            "consistency": {"batch_dups": len(cons.get("batch_dups") or []),
                            "dangling": len(cons.get("dangling") or [])},
            "error": report.get("error") or ""}


# ── P0-2 归一确认应用（2026-09-08 闸口①）：人工决策 → 更新 V2 代码与视图（新版本，不覆盖）──
@router.post("/api/conversations/{conv_id}/messages/{msg_id}/normalize-apply")
def normalize_apply(conv_id: int, msg_id: int, body: dict = None,
                    conn=Depends(db_session), user=Depends(current_user)):
    _own_conv(conn, conv_id, user)   # ★ B1-9 归属校验
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
