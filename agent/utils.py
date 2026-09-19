"""AgentPipeline 纯辅助函数（P1-5 保守拆分，2026-08-31）：无类依赖/无状态的顶层工具函数。

从 agent/pipeline.py 提取（AgentPipeline 类保持原位），pipeline.py import 后 re-export，
外部既有引用（from agent.pipeline import _archive_artifacts 等）保持兼容。
"""
import json
import re


# ── 问答可解释性：检索分块（chunk_hits）→ 前端 [n] 引用链接数据 ──
def _citations_payload(hits) -> list:
    """把检索到的 chunk_hits 裁剪为前端引用数据（[n] 引用链接 → 来源段落/模型元素跳转）。

    与 _build_context 中【来源n】编号保持 1 基对齐：前端 [n] 映射 cites[n-1]。
    """
    out = []
    for h in (hits or [])[:8]:
        out.append({
            "document_id": h.get("document_id"),
            "source_doc": h.get("source_doc", ""),
            "chunk_index": h.get("chunk_index"),
            "section": h.get("section", ""),
            "score": round(float(h.get("score") or 0), 3),
            "confidence_level": h.get("confidence_level", ""),
            "content": (h.get("content") or "")[:800],
        })
    return out


def _conversation_project_id(conn, conversation_id) -> str:
    """会话归属项目（无则空串）。

    2026-09-20：报告归档此前**硬编码** `'project-satnet-broadband'`（域固化残留；该列 DDL
    默认值也是它），使报告的项目归属与会话实际归属无关。改取会话自身归属 ——
    会话的项目由「用户配置的默认项目」决定（见 repositories.project_repo.resolve_project_id）。
    """
    try:
        row = conn.execute("SELECT project_id FROM conversations WHERE id=?",
                           (conversation_id,)).fetchone()
        if not row:
            return ""
        try:
            return str(row["project_id"] or "")
        except (TypeError, IndexError):
            return str(row[0] or "")
    except Exception:
        return ""


# ── 会话产物归档（AI 生成内容 → artifacts 表 + 报告自动写 reports 表）──
def _extract_code_blocks(content: str, limit: int = 5) -> list:
    """从 markdown 文本提取 ``` 围栏代码块，返回 [{lang, code}]（最多 limit 条）。"""
    blocks = []
    for m in re.finditer(r"```([\w+-]*)\n(.*?)```", content or "", re.S):
        lang = m.group(1).strip() or "text"
        code = m.group(2).strip()
        if code and len(code) >= 20:  # 过滤过短片段
            blocks.append({"lang": lang, "code": code})
        if len(blocks) >= limit:
            break
    return blocks


def _archive_impact_analysis(conn, conversation_id: int, message_id: int,
                             card_data, user_input: str, created_by: str = "王工") -> None:
    """CIA（FR-CIA-3）：变更影响分析结果快照落库（含失败引导记录，可追溯）。

    在 execute / execute_stream 两条落库链路统一调用；失败静默（不影响会话主流程）。
    """
    try:
        from repositories.impact_repo import ImpactRepo
        _cd = json.loads(card_data) if isinstance(card_data, str) else (card_data or {})
        if _cd.get("intent") == "impact" or _cd.get("ok") is not None or _cd.get("impact_nodes") is not None:
            ImpactRepo(conn).create_analysis(
                title=(_cd.get("change_source") or {}).get("name", str(user_input)[:60]),
                change_source=(_cd.get("change_source") or {}).get("name", ""),
                params=_cd.get("params", {}),
                result=_cd,
                status="ok" if _cd.get("ok") is not False else "error",
                error_code=_cd.get("code", ""),
                conversation_id=conversation_id, message_id=message_id,
                created_by=created_by)
    except Exception:
        pass


def _archive_sysml_version(conn, conversation_id: int, message_id: int,
                           sysml_views: dict, created_by: str = "王工",
                           code_text: str = "") -> int | None:
    """AI 建模 SysML 版本建档：每次 AI 生成/修订 SysML → sysml_versions 新版本（v0.n 递增）。

    幂等：同一 (会话, 消息) 已建档则不重复（同一消息的 views 不变）。
    修订链：旧 current/draft → superseded，新版本 current。
    返回新版本 id；失败返回 None（不阻断归档主流程）。
    """
    try:
        if not sysml_views or not (sysml_views.get("views") or {}):
            return None
        # 幂等：同消息已建档跳过
        dup = conn.execute(
            "SELECT COUNT(*) FROM sysml_versions WHERE conversation_id=? AND message_id=?",
            (conversation_id, message_id)).fetchone()[0]
        if dup:
            return None
        # 会话版本计数 + 上一版本（修订链）
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(id) AS last_id FROM sysml_versions WHERE conversation_id=?",
            (conversation_id,)).fetchone()
        n = row["n"] if row else 0
        last_id = row["last_id"] if row else None
        label = f"v0.{n + 1}"
        # 元素摘要（结构化视图 nodes/edges 汇总）
        nodes, edges = [], []
        for vname, vdata in (sysml_views.get("views") or {}).items():
            for nd in (vdata.get("nodes") or []):
                nodes.append(nd.get("name") or nd.get("id") or "")
            for ed in (vdata.get("edges") or []):
                edges.append(f"{ed.get('source') or ''}--{ed.get('type') or ''}--{ed.get('target') or ''}")
        summary = {
            "entities": len(nodes), "relations": len(edges),
            "nodes": nodes[:200], "edges": edges[:200], "intent": sysml_views.get("intent", ""),
            "views": len((sysml_views.get("views") or {}))}
        # P3（集成指南 §2.2 接入点③）：把「生成后本地校验（checker.jar）」摘要随版本链留痕。
        # 摘要由 cards.py::_check_generated_sysml 产出、挂在 views 的兄弟键 `check`
        # （与既有 quality_check 同级）。价值：`/api/sysml-versions/{id}` 本来就会
        # json.loads(element_summary) 返回 → **前端零改动**即可显示「这个版本当时合不合法」。
        # 字段：rc/verdict(pass|report|block|unavailable)/blocked/n_error/n_syntax/n_semantic/
        #       n_lexical/n_hard/n_warn/scope/top(前 5 条诊断)/at。
        #       判据只认**硬错** = 词法 + 语法（n_hard>0 → blocked）；语义错只报告不阻断
        #       （2026-09-19 三路化：词法/语法/语义分列，词法与语法同门槛）。
        _chk = sysml_views.get("check")
        if isinstance(_chk, dict) and _chk:
            summary["check"] = _chk
        # 相对上版差异（简化：新增节点名；上版不存在则全部为新增）
        diff = {"added_nodes": nodes[:200]}
        if last_id:
            try:
                old = json.loads(conn.execute(
                    "SELECT element_summary FROM sysml_versions WHERE id=?", (last_id,)).fetchone()["element_summary"] or "{}")
                old_nodes = set(old.get("nodes") or [])
                diff["added_nodes"] = [x for x in nodes if x and x not in old_nodes][:200]
            except Exception:
                pass
        # 旧版本状态收敛：draft/current → superseded（只收敛最后一个 current/draft，避免误标 committed）
        cur = conn.execute("SELECT id, status FROM sysml_versions WHERE conversation_id=? AND status IN ('draft','current') "
                           "ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
        if cur:
            conn.execute("UPDATE sysml_versions SET status='superseded' WHERE id=?", (cur["id"],))
        cur2 = conn.execute(
            "INSERT INTO sysml_versions (artifact_id, conversation_id, message_id, version_label, content, diff, element_summary, parent_id, status, created_by, code_text) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (0, conversation_id, message_id, label,
             json.dumps(sysml_views, ensure_ascii=False), json.dumps(diff, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), last_id, "current", created_by,
             code_text or ""))
        # 不在此 commit：由调用方（db_conn 上下文）统一提交，保证与消息落库同事务
        return cur2.lastrowid
    except Exception:
        return None


def _archive_artifacts(conn, conversation_id: int, message_id: int,
                       card_data, content: str, intent: str, created_by: str = "王工") -> None:
    """把 AI 生成内容登记为会话产物（幂等：同 (会话,消息,类型,标题) 不重复）。

    - card_data.sysml_views → kind=sysml（预览走 Cytoscape 投影）
    - 报告意图（report_generation / card 含 sections）→ kind=report，并同时写 reports 表（报告中心自动归档）
    - 正文含代码块 → kind=code（每条代码块登记一项）
    - 长 markdown 正文 → kind=document（preview_type=markdown）
    仅 AI 生成内容；用户上传附件不进入产物库。
    """
    try:
        from repositories.artifact_repo import ArtifactRepo
        from repositories.report_repo import ReportRepo
        repo = ArtifactRepo(conn)
        cd = card_data
        if isinstance(cd, str):
            try:
                cd = json.loads(cd) or {}
            except Exception:
                cd = {}
        cd = cd if isinstance(cd, dict) else {}
        content = content or ""

        # 1) SysML 视图产物 + 版本建档（sysml_versions 版本链）
        _vid = None  # P0-7：本消息 SysML 版本 id（代码文件产物关联版本）
        _code = ""   # P0-7：本消息提取的 SysML 代码文本
        sv = cd.get("sysml_views") or {}
        if sv.get("views"):
            for vname, vdata in (sv.get("views") or {}).items():
                title = f"SysML 视图：{vdata.get('view', {}).get('name', vname)}"
                if not repo.exists(conversation_id, message_id, "sysml", title):
                    repo.create_artifact(
                        conversation_id, message_id, "sysml", title, "sysml", "",
                        {"sysml_views": {"views": {vname: vdata}}}, source="conversation",
                        created_by=created_by)
            # AI 建模 SysML 版本建档（幂等；失败不阻断归档）
            _blocks = _extract_code_blocks(content)
            _code = "\n\n".join(b["code"] for b in _blocks if b.get("lang","").lower() in ("sysml","kerml","sysmlv2","kerml2"))
            if not _code and _blocks:
                _code = _blocks[0]["code"]
            _vid = _archive_sysml_version(conn, conversation_id, message_id, sv, created_by, _code)
            if _vid:
                for ar in conn.execute(
                        "SELECT id, meta FROM artifacts WHERE conversation_id=? AND message_id=? AND kind='sysml'",
                        (conversation_id, message_id)).fetchall():
                    m = {}
                    try:
                        m = json.loads(ar["meta"] or "{}")
                    except Exception:
                        m = {}
                    m["version"] = f"v{_vid}"
                    conn.execute("UPDATE artifacts SET meta=? WHERE id=?",
                                 (json.dumps(m, ensure_ascii=False), ar["id"]))

        # 2) 报告产物 + 报告中心自动归档
        is_report = intent == "report_generation" or bool(cd.get("sections")) or bool(cd.get("report"))
        if is_report:
            try:
                from report_generator import report_generator as _rg
                rtype = cd.get("report_type") or _rg.detect_report_type(str(content)[:80])
                title = str(cd.get("report_title") or cd.get("title") or "AI 生成报告")[:120]
                report_meta = cd.get("meta") or _rg.build_meta(title, rtype)
                structured = cd.get("sections") or _rg.structure("", content, report_type=rtype)["sections"]
                summary = str(cd.get("summary") or structured[-1].get("body", "") if structured else "")[:500]
                if not repo.exists(conversation_id, message_id, "report", title):
                    repo.create_artifact(
                        conversation_id, message_id, "report", title, "markdown", content,
                        {"report_type": rtype, "sections": structured, "summary": summary, "meta": report_meta},
                        source="conversation", created_by=created_by)
                    # 报告中心自动归档（幂等：同会话同标题不重复写 reports）
                    dup = conn.execute(
                        "SELECT COUNT(*) FROM reports WHERE conversation_id=? AND title=?",
                        (conversation_id, title)).fetchone()[0]
                    if not dup:
                        ReportRepo(conn).create_report(
                            title, rtype, summary, structured, "conversation",
                            conversation_id, "",
                            _conversation_project_id(conn, conversation_id), "draft", created_by)
            except Exception:
                pass

        # 3) 代码产物：SysML 版本 → 每个版本一个「代码文件 vN（SysML）」产物（产物列表并列展示演进路线）
        if _vid:
            _vlabel = f"v{_vid}"
            try:
                _vr = conn.execute("SELECT version_label FROM sysml_versions WHERE id=?", (_vid,)).fetchone()
                if _vr and _vr["version_label"]:
                    _vlabel = _vr["version_label"]
            except Exception:
                pass
            _vt = f"代码文件 {_vlabel}（SysML）"
            if _code and not repo.exists(conversation_id, message_id, "code", _vt):
                repo.create_artifact(
                    conversation_id, message_id, "code", _vt, "code", _code,
                    {"lang": "sysml", "version_id": _vid, "version_label": _vlabel},
                    source="conversation", created_by=created_by)
        else:
            # 幂等修复（2026-09-01）：重复归档时 sysml 版本幂等返回 None → _vid 分支漂移。
            # 同消息已有任一 code 产物（如首次走 if _vid 分支的「代码文件」）则跳过代码块登记。
            has_code = conn.execute(
                "SELECT 1 FROM artifacts WHERE conversation_id=? AND message_id=? AND kind='code' LIMIT 1",
                (conversation_id, message_id)).fetchone()
            if not has_code:
                for i, blk in enumerate(_extract_code_blocks(content), 1):
                    title = f"代码块 {i}（{blk['lang']}）"
                    if not repo.exists(conversation_id, message_id, "code", title):
                        repo.create_artifact(
                            conversation_id, message_id, "code", title, "code", blk["code"],
                            {"lang": blk["lang"]}, source="conversation", created_by=created_by)
        
        # 4) 长 markdown 文档产物（无结构化 card 且无明显代码块）
        if not cd.get("sysml_views") and not is_report and content and len(content) >= 200:
            title = (cd.get("title") or "AI 生成文档")[:120]
            if not repo.exists(conversation_id, message_id, "document", title):
                repo.create_artifact(
                    conversation_id, message_id, "document", title, "markdown", content,
                    {"source_msg_type": cd.get("msg_type", "text")}, source="conversation",
                    created_by=created_by)
    except Exception:
        # 归档失败不阻断主流程（对齐既有降级原则）
        pass
