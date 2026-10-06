"""GraphRAG（实体链接 + 子图遍历 + 向量降级）+ ConflictDetector（冲突检测）。"""
import os
import re
import json
import time
import uuid
from database import get_db, db_conn
from knowledge_engine import VectorEngine, QueryRouter  # P1: 双引擎底座
from llm import llm_client
from core.config import STATIC_DIR
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES  # 基础通用文件操作工具
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES  # 基础通用报告导出工具


def _cfg_get(group, key, default=None):
    """读配置；异常/缺组回默认（检索链路不因配置层问题中断）。"""
    try:
        from core import config as _cfg
        return _cfg.get(group, key, default)
    except Exception:
        return default


def _cfg_bool(value, default=False) -> bool:
    """配置布尔归一：配置可能以 str/int 形式落库（"1"/"true"/True 均可）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ══════════════════════════════════════════════════════════════════════════
# P0-4 多租户隔离（2026-10-04）——⚠️ **已冻结：禁止开启**（2026-10-04 架构决策）
# ══════════════════════════════════════════════════════════════════════════
# 【决策】米爸拍板：「文档向量库、图库**不需要基于项目隔离，是统一使用的数据底座**」。
#
# 【为什么必须冻结，而不是"反正默认 off 就没事"】
# 一旦有人把 `rag.tenant_isolation` 置 on，后果是**有害**的：它会把统一底座里的
# 共享资产（标准构件、领域术语映射、通用模型）从检索结果里滤掉 ——
# 不是"多看到别人的数据"，而是"**看不到本该看到的公共资产**"，直接损害建模质量。
# 默认 off 只是运气；靠注释提醒不够，故由 `tools/verify/verify_tenant_isolation.py`
# 的 D 组断言把"默认值恒为 off + 本决策声明存在"锁成 CI 门禁（改配置即判红）。
#
# 【本系统"隔离"的真实位置在哪（别再找错地方）】
#   隔离实际发生在 **artifacts / messages**，按 `conversation_id` 而非 project_id
#   —— 粒度比"按项目"更细，本来就安全。
#   `project_id` 在本系统的角色是**组织/视图标签**（前端分组用），不是安全边界。
#   数据自证：`entities` 189 行只有 1 个 project_id 值、`_release_branches` 返回
#   **所有** release 分支（分支也不构成项目边界）⇒ 图谱从未按项目分区过。
#
# 【仍然有效的部分】
#   · `GET /api/monitor/tenant-readiness` 仍有用 —— 但它的价值已从"开启隔离的体检"
#     变成"**数据健康探测**"：它报出的 `orphan_project`（悬空 project_id 引用）
#     是真缺陷的信号，成因见 `ProjectRepo.delete_project` / `detach_project_references`。
#   · 下面的过滤机制**保留**（不再删除）：客户级私有化部署若真需要隔离，机制已就绪，
#     届时再开。删除它等于毁掉将来的选项，而留着它成本为零。
#
# 【历史记录，勿当结论读】
#   本块初版（2026-10-04 凌晨）曾把"检索链路缺 project_id 维度"列为 P0 缺陷并实现过滤，
#   评估报告 §6 也据此写过。**该判断已被上述决策推翻**，报告已同步更正。
#   保留这段历史是因为它记录了一个真实的推理错误：
#   **从字段名反推数据归属**（看到 entities.project_id 就以为数据"属于某个项目"），
#   而没有先确认数据模型意图 —— 字段存在 ≠ 该字段承担语义。
#
# ── 以下为保留的过滤机制实现（冻结状态，勿改、勿开）─────────────────────────
TENANT_OFF, TENANT_ON = "off", "on"


def tenant_scope(kb_scope: dict | None) -> dict:
    """由 kb_scope 解出本次请求的租户上下文。

    返回 {"enabled": bool, "pid": str, "unresolved": "passthrough"|"empty"}
    —— `kb_scope["project_id"]` 由 pipeline 侧按**项目真源链**算好传入
    （显式 → 会话 project_id → settings.default_project_id → 空，见
    `pipeline_parts/memory._resolve_mem_project_id`），检索侧**不自己再解析一遍**
    （两处解析必然漂移，这条口径已在 memory.py 注释里立过规矩）。
    """
    scope = kb_scope or {}
    pid = str(scope.get("project_id") or "").strip()
    mode = str(_cfg_get("rag", "tenant_isolation", TENANT_OFF) or TENANT_OFF).strip().lower()
    unres = str(_cfg_get("rag", "tenant_unresolved", "passthrough") or "passthrough").strip().lower()
    if unres not in ("passthrough", "empty"):
        unres = "passthrough"      # 脏配置按宽松档处理：宁可少隔离，不可全量 0 命中
    enabled = (mode == TENANT_ON)
    if enabled and not pid:
        # 开了隔离但解析不到项目：按配置决定是"放行全部"还是"直接 0 命中"
        enabled = (unres == "empty")
    return {"enabled": enabled, "pid": pid, "unresolved": unres, "mode": mode}


def tenant_filter_rows(rows: list, ts: dict) -> list:
    """按租户过滤**已物化成 dict 的行**（实体命中 / 词典桥接召回共用）。

    为什么在 Python 侧过滤而不是一律加 SQL 条件：
      图谱有两条取数路径 —— SQLite LIKE 回退 与 TDB(pyoxigraph) 命名图查询 ——
      在 SQL 层加条件要改两处且 TDB 侧还要改 graph_db 签名；而两条路径**最终都回填
      SQLite 的实体行**（属性以 SQLite 为治理权威源）。故在"物化点"一刀切，
      覆盖面完整且不漏任何一条路径。
    """
    if not ts or not ts.get("enabled"):
        return rows
    pid = ts.get("pid") or ""
    return [r for r in (rows or []) if str((r or {}).get("project_id") or "") == pid]


class GraphRAG:
    """ArcR-5: Graph-first retrieval with vector fallback.

    P1 升级为双引擎底座：
    - 图引擎：实体链接 + 子图遍历（精确匹配）
    - 向量引擎：VectorEngine 对已完成解析文档做真实相似度排序（替代原 LIMIT 5 直取）
    - QueryRouter 决策路由并写入 query_routing_stats（查询路由统计）
    """

    def __init__(self):
        # P1-4：路由阈值配置化（rag.route_threshold，默认 0.75 与改动前一致）
        try:
            from core import config as _cfg
            self.confidence_threshold = float(_cfg.get("rag", "route_threshold", 0.75))
        except Exception:
            self.confidence_threshold = 0.75
        self.vector = VectorEngine()
        self.router = QueryRouter(threshold=self.confidence_threshold)

    def retrieve(self, query, branch="dev", attachment_text=None, kb_scope=None):
        """混合检索路由：上传文档优先 → GraphRAG 优先 → 向量兜底。

        - attachment_text: 用户上传文档全文（前 4000 字符）。非空时先做附件关键词命中，
          命中片段作为「上传资料」首要依据（attachment_hits）。
        - kb_scope: Agent 级知识库消费范围 {"mode","branches","docs"}（KB-S）：
          branches 非空 → 替代默认 release 分支集合；docs 非空 → 实体/分块/文档均按 source_doc 过滤。
        - 知识库检索：**真路由**——GraphRAG 命中且置信足够 → 不再跑向量；
          图谱未命中/置信不足 → 才执行向量检索（chunk 级优先，文档粗匹配兜底）。
        - route ∈ {graph, mixed, vector}；统计落库 query_routing_stats。
        """
        conn = get_db()
        t0 = time.time()
        scope = kb_scope or {}
        # P0-4：租户上下文只解一次（下面实体/关系/词典桥接三处共用）
        _ts = tenant_scope(scope)
        # 2026-09-21 G1：生命周期过滤的统一开关（默认排除「已下线」文档）。
        # 与 include_ai_generated 同款语义：默认 must-not-include，显式开启才纳入。
        # 之所以要在 retrieve() 入口就先算出来：下面 _resolve_scope_docs（白名单自愈）也要用同一个口径，
        # 否则「已废弃文档」会被自愈判成「白名单有效」而继续参与过滤 → 自愈形同虚设（G2）。
        _inc_dep = bool(scope.get("include_deprecated") or False)
        # KB-S 自愈：白名单与实际存在的文档求交（失效项剔除；全失效则退化，绝不静默 0 命中）
        scope_docs, scope_warn = self._resolve_scope_docs(conn, scope.get("docs") or [],
                                                         include_deprecated=_inc_dep)

        # Step 0: 上传文档优先——附件文本关键词命中（首要依据）
        attachment_hits = []
        if attachment_text:
            attachment_hits = self._match_attachment(query, attachment_text)

        # Step 1: Entity linking——整句 LIKE 精确匹配，无命中时用 3-4 字 token 补充（长句 query 友好）
        # 消费侧：默认只查已发布(release)分支；Agent kb_scope.branches 可显式指定其他集合
        kb_branches = (scope.get("branches") or []) or self._release_branches(conn)
        graph_results = self._entity_link(conn, query, kb_branches, source_docs=scope_docs)
        # P0-4：图谱实体按项目隔离（在物化点过滤，覆盖 SQLite 与 TDB 两条取数路径）
        _ent_before = len(graph_results)
        graph_results = tenant_filter_rows(graph_results, _ts)
        _tenant_dropped = _ent_before - len(graph_results)

        # Step 2: Subgraph traversal - get relations
        if graph_results:
            ids = [e["id"] for e in graph_results]
            placeholders = ",".join(["?"] * len(ids))
            # P0-4：关系侧同样按项目隔离 —— 只过滤 r 侧不够：一条 relation 可能连着
            #   本项目实体与**他项目实体**，e1/e2 的 JOIN 会把对方名字带进上下文（信息泄露面
            #   比实体本身更大：名字+关系一起就勾勒出了对方的模型结构）。
            _rel_where = ""
            _rel_args = []
            if _ts.get("enabled"):
                _rel_where = " AND COALESCE(r.project_id,'') = ?"
                _rel_args = [_ts["pid"]]
            relations = conn.execute(
                f"SELECT r.*, e1.name as source_name, e2.name as target_name FROM relations r "
                f"JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
                f"WHERE (r.source_id IN ({placeholders}) OR r.target_id IN ({placeholders})) "
                f"AND r.status != 'deprecated'{_rel_where}",
                ids + ids + _rel_args
            ).fetchall()
            graph_relations = [dict(r) for r in relations]
        else:
            graph_relations = []

        # 多因子置信度（ArcR-5 路由增强）：
        #   0.50 * 命中实体覆盖度(min(n/5,1)) + 0.30 * 关系连接性(min(rels/3,1)) + 0.20 * 类型匹配度
        graph_confidence = self._graph_confidence(graph_results, graph_relations)

        # Step 3: 真路由——GraphRAG 命中且置信足够 → 跳过向量；否则向量兜底（chunk 优先，文档粗匹配兜底）
        vector_results = []
        chunk_hits = []
        # P0-1/P0-2/P2-2：Glossary 归一化 + domain 过滤 + Trace
        _glossary_res = {}
        if conn is not None:
            try:
                from glossary import GlossaryMatcher, trace_query
                _glossary_res = GlossaryMatcher(conn).resolve(query)
                query = _glossary_res["normalized"] or query  # 归一化后检索（v2→SysML_V2）
            except Exception:
                pass
        # P1（2026-09-07）maps_to 消费：词典概念命中且带本体映射时，按映射类/实例
        # 直查图谱实体并入 graph_results——词典→图谱的桥接通道（此前 maps_to 仅登记无消费）。
        glossary_recall = {"concepts": [], "entities": []}
        if _glossary_res:
            try:
                glossary_recall = self._glossary_class_link(
                    conn, _glossary_res, kb_branches, source_docs=scope_docs)
            except Exception:
                glossary_recall = {"concepts": [], "entities": []}
            _rec_ents = glossary_recall.get("entities") or []
            # P0-4：词典桥接是**第三条实体入口**（maps_to 直查图谱），同样要过租户过滤，
            #   否则"用领域词典绕开项目隔离"会成为一个现成的漏洞。
            _rec_ents = tenant_filter_rows(_rec_ents, _ts)
            if _rec_ents:
                _have = {e["id"] for e in graph_results}
                _new = [e for e in _rec_ents if e["id"] not in _have]
                if _new:
                    graph_results = graph_results + _new
                    _ids = [e["id"] for e in _new]
                    _ph = ",".join(["?"] * len(_ids))
                    _rel_extra = ""
                    _rel_args_extra = []
                    if _ts.get("enabled"):
                        _rel_extra = " AND COALESCE(r.project_id,'') = ?"
                        _rel_args_extra = [_ts["pid"]]
                    _rel = conn.execute(
                        f"SELECT r.*, e1.name as source_name, e2.name as target_name FROM relations r "
                        f"JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
                        f"WHERE (r.source_id IN ({_ph}) OR r.target_id IN ({_ph})) "
                        f"AND r.status != 'deprecated'{_rel_extra}",
                        _ids + _ids + _rel_args_extra).fetchall()
                    graph_relations = graph_relations + [dict(x) for x in _rel]
                    graph_confidence = self._graph_confidence(graph_results, graph_relations)
        # 2026-09-16 修复（对话接口 500 的根因）：
        # `_exclude_ai` 原先只在「向量检索兜底分支」内赋值，当 hybrid_search 正常返回
        # 但命中 0 条时，控制流会落到下方 else 分支引用该变量，抛 UnboundLocalError
        # → 整个 POST /api/conversations/{id}/chat 返回 500，AI 能力消费链路完全不可用。
        # 现统一在分支外先求值，口径与兜底分支保持一致（消费隔离：默认排除 AI 收编文档）。
        _exclude_ai = not (scope.get("include_ai_generated") or False)
        # P1-4：检索条数配置化（rag.top_k / rag.fallback_top_k，默认 4/5 与改动前一致）
        try:
            from core import config as _cfg
            _rag_top_k = int(_cfg.get("rag", "top_k", 4))
            _rag_fb_top_k = int(_cfg.get("rag", "fallback_top_k", 5))
        except Exception:
            _rag_top_k, _rag_fb_top_k = 4, 5
        if graph_confidence < self.confidence_threshold:
            try:
                # 文档全局化：向量检索不分分支（文件管理全局资产，向量化数据全局消费）；
                # docs 非空按来源文档过滤；图谱实体检索仍按 kb_branches（建模数据按分支）
                # P0-2 行业对齐：Glossary 命中时强制 domain 过滤（sysml_norm 等）+ 术语加权
                force_domain = (_glossary_res.get("force_domain") or "").strip() or None
                boost = _glossary_res.get("boost") or 1.0
                from knowledge_engine import hybrid_search as _hybrid
                # S4：top_k 配置化（P1-4，默认 4），与消费侧 context_text 的 chunk_hits[:3] 对齐
                # （此前实取 8 条、仅消费 3 条，多算的 5 条白白走向量检索与 RRF 融合）
                _hy = _hybrid(conn, query, top_k=_rag_top_k, branches=None,
                              domain=force_domain if force_domain and force_domain != "unknown" else None,
                              glossary_boost=boost, doc_names=scope_docs or None,
                              include_deprecated=_inc_dep)
                chunk_hits = _hy["hits"]
                # P2-2：查询 Trace（含归一化/域/命中文档/召回原因）
                if conn is not None:
                    try:
                        from glossary import trace_query
                        trace_query(conn, _glossary_res.get("original") or query, {
                            "normalized": query,
                            "intent": "knowledge_qa",
                            "route": "vector",
                            "domain": force_domain or "all",
                            "hit_docs": [h.get("source_doc", "") for h in chunk_hits[:5]],
                            "hit_count": len(chunk_hits),
                            "top_score": chunk_hits[0]["score"] if chunk_hits else 0,
                            "latency_ms": int((time.time() - t0) * 1000),
                            "detail": {"glossary": [h.get("user_term") for h in _glossary_res.get("hits", [])],
                                       "glossary_recall": glossary_recall.get("concepts", []),
                                       "recall_reasons": [h.get("recall_reason", "") for h in chunk_hits[:3]]},
                        })
                    except Exception:
                        pass
            except Exception:
                try:
                    from knowledge_pipeline import search_chunks
                    # 2026-09-15 消费隔离：AI 建模 RAG 默认排除 AI 收编文档（origin='ai_generated'，
                    # model collapse 对策）；kb_scope.include_ai_generated=true 显式开启才纳入
                    _exclude_ai = not (scope.get("include_ai_generated") or False)
                    chunk_hits = search_chunks(conn, query, top_k=_rag_fb_top_k, branches=None,
                                               doc_names=scope_docs or None, exclude_ai=_exclude_ai,
                                               include_deprecated=_inc_dep)
                except Exception:
                    chunk_hits = []
            if chunk_hits:
                vector_results = chunk_hits
            else:
                doc_sql, doc_params = "", []
                if scope_docs:
                    doc_sql = " AND filename IN ({})".format(",".join("?" * len(scope_docs)))
                    doc_params = list(scope_docs)
                if _exclude_ai:  # 消费隔离：文档粗匹配兜底同步排除 AI 收编文档
                    doc_sql += " AND (origin IS NULL OR origin != 'ai_generated')"
                if not _inc_dep:  # G1：文档粗匹配兜底同步排除已下线文档
                    doc_sql += " AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('deprecated'))"
                docs = conn.execute(
                    "SELECT * FROM documents WHERE parse_status='completed'" + doc_sql,
                    doc_params
                ).fetchall()
                if docs:
                    doc_dicts = [dict(d) for d in docs]
                    for d in doc_dicts:
                        linked = conn.execute(
                            "SELECT name FROM entities WHERE source_doc=? AND status != 'deprecated' LIMIT 8",
                            (d["filename"],)
                        ).fetchall()
                        d["_text"] = d["filename"] + " " + " ".join(e["name"] for e in linked)
                    scored = self.vector.search(query, doc_dicts, top_k=5, key="_text")
                    for _, d in scored:
                        d.pop("_text", None)
                    vector_results = [d for _, d in scored]

        # Step 4: 路由决策（graph | mixed | vector）+ 统计落库
        route, reason = self.router.decide(graph_results, graph_confidence, vector_results)
        latency_ms = int((time.time() - t0) * 1000)
        # P1-4 修（2026-10-06）：`confidence` 此前**恒等于 graph_confidence**，而
        #   `QueryRouter.decide` 在图谱未命中时给出 route='vector'/'mixed' 且 graph 置信度=0
        #   ⇒ **vector 路由下 confidence 恒为 0**。实测（embedding 恢复后）：
        #   `tools/eval/preflight.py` 5 条查询里 4 条 route=vector / conf=0.0，
        #   而同一次 hybrid_search 的 vec_score 明明是 0.44~0.84（召回正常、命中文档正确）。
        #   后果：① 前端展示"零置信"；② `query_routing_stats` 里 vector 路平均置信度恒 0，
        #   统计口径失真、无法用它评估检索质量。
        #   修法：`confidence` 改为**按路由取值**（vector 路用向量相似度），
        #   口径**复用 knowledge_engine 算 confidence_level 的同一算法**（vs>0 取相似度，
        #   否则 bm25/50 归一）——不另造一套量纲。
        #   ⚠️ `QueryRouter.decide` 的路由判定**仍用 graph_confidence**（未改），
        #   图谱侧语义与既有行为完全不变；另**新增** `graph_confidence` 字段保留原值可观测。
        retrieval_confidence = self._retrieval_confidence(
            route, graph_confidence, chunk_hits or vector_results)
        try:
            self.router.record(conn, query, route, reason, retrieval_confidence,
                               len(graph_results), len(vector_results), latency_ms, branch)
        except Exception:
            pass  # 统计落库失败不影响主流程

        # Step 5: P0 记忆召回（2026-09-29）——把「已沉淀的经验/决策」作为一路独立召回源。
        # 详见 agent/memory_recall.py 的模块 docstring。此处只做编排：
        #   ① 开关 rag.memory_enabled（关 = 逐字回到「无此路」旧行为）
        #   ② project_id 取项目真源（与记忆注入同一口径，见 pipeline_parts.memory._resolve_mem_project_id），
        #      为空则 project_memories 路自动跳过（防测试数据冒充项目知识）
        #   ③ 异常静默 → memory_hits=[]，**绝不阻断检索主链路**
        memory_hits = []
        if _cfg_bool(_cfg_get("rag", "memory_enabled", True), True):
            try:
                from agent.memory_recall import recall_memories
                # 作用域槽位：优先让调用方经 kb_scope 显式给定（便于定向单测与精确控制）；
                # 未给定 → None，由 MemoryService 沿用「仅 agent_id」的旧行为，行为可预测、不引入隐式串扰
                memory_hits = recall_memories(
                    conn, query, agent_id=scope.get("intent") or "chat",
                    scopes=scope.get("memory_scopes"), project_id=scope.get("project_id") or "")
            except Exception:
                memory_hits = []

        # P0-3 知识分类分组：设计方法知识（规范约束源）/ 设计资产（增量设计源）
        knowledge = self._group_by_category(conn, graph_results)
        conn.close()
        return {
            "source": route,  # 兼容旧字段：graph | mixed | vector
            "route": route,   # P1: 双引擎路由决策
            "route_reason": reason,
            "confidence": retrieval_confidence,
            "graph_confidence": graph_confidence,   # 原值保留可观测（此前被当作 confidence 透出）
            "entities": graph_results,
            "relations": graph_relations,
            "vector_docs": vector_results,
            "chunk_hits": chunk_hits,  # 缺口A：分块级命中预览（含 content 片段）
            "attachment_hits": attachment_hits,   # 上传文档命中片段（首要依据）
            "attachment_used": bool(attachment_hits),
            "graph_count": len(graph_results),
            "vector_count": len(vector_results),
            "memory_hits": memory_hits,   # P0（2026-09-29）：已沉淀记忆命中（跨会话结论，仅供对齐）
            "memory_count": len(memory_hits),
            "glossary_recall": glossary_recall,  # P1: 词典概念（maps_to）→ 图谱实体召回回显
            "latency_ms": latency_ms,
            "knowledge": knowledge,   # P0-3: {"method":[...规范], "assets":[...资产], "uncategorized":[...]}
            "kb_scope_warn": scope_warn,   # KB-S：白名单失效告警（None=正常）；供上层事件/卡片回显
            # P0-4 租户上下文回显：**可观测是隔离的前提** —— 出了问题要能一眼看出
            # 「这次到底按哪个项目过滤的、滤掉了多少条」，而不是黑盒。
            # ⚠️ 冻结状态下这里恒为 enabled=False（决策：图谱是统一底座，不隔离）。
            # 保留回显是为了将来真开隔离时不用重新加观测字段。
            "tenant": {"mode": _ts.get("mode"), "enabled": bool(_ts.get("enabled")),
                       "project_id": _ts.get("pid") or "",
                       "unresolved_policy": _ts.get("unresolved"),
                       "entities_dropped": int(_tenant_dropped),
                       "frozen": True,   # 标记：本链路按架构决策不做项目隔离（勿据此判断"已隔离"）
                       },
        }

    @staticmethod
    def _group_by_category(conn, entities: list) -> dict:
        """P0-3：图谱命中实体按知识类别分组——设计方法知识（AI 必须遵守的规范约束源）
        与设计资产（可参考复用的增量设计源），供消费侧注入 system prompt 时区分对待。"""
        if not entities:
            return {"method": [], "assets": [], "uncategorized": []}
        gmap = {}
        try:
            gmap = {r["name"]: r["group_name"] for r in conn.execute(
                "SELECT name, group_name FROM knowledge_categories").fetchall()}
        except Exception:
            pass
        method, assets, other = [], [], []
        for e in entities:
            cat = e.get("knowledge_category") or ""
            if gmap.get(cat) == "设计方法知识":
                method.append(e)
            elif gmap.get(cat) == "设计资产":
                assets.append(e)
            else:
                other.append(e)
        return {"method": method, "assets": assets, "uncategorized": other}

    @staticmethod
    def _retrieval_confidence(route: str, graph_confidence: float, hits: list) -> float:
        """本次检索的置信度（0~1），**按路由取值**。

        - `graph` / `mixed` 且图谱有置信度 → 用 graph_confidence（图谱侧强度）；
        - `vector` 路由（图谱未命中，graph_confidence 恒 0）→ 用**向量相似度**；
          向量缺失时回退 bm25 归一（`min(bs/50, 1)`，与 knowledge_engine 的
          `confidence_level` 同一算法，不另造量纲）；
        - 都没命中 → 0.0。

        纯函数、不触碰数据库；调用方负责把它与 `graph_confidence` 一起落库/透传。
        """
        try:
            if route in ("graph", "mixed") and float(graph_confidence or 0.0) > 0:
                return round(float(graph_confidence), 3)
            best = 0.0
            for h in (hits or [])[:1]:
                if not isinstance(h, dict):
                    continue
                vs = float(h.get("vec_score") or 0.0)
                bs = float(h.get("bm25_score") or 0.0)
                if vs > 0:
                    best = vs
                elif bs > 0:
                    best = min(bs / 50.0, 1.0)
            return round(best, 3)
        except Exception:
            return 0.0

    @staticmethod
    def _graph_confidence(graph_results: list, graph_relations: list) -> float:
        """多因子图谱置信度（2026-09-01 路由增强；P1-4 权重配置化）。

        w_coverage * 命中实体覆盖度(min(n/5,1)) + w_relations * 关系连接性(min(rels/3,1))
        + w_typing * 类型匹配度 —— 默认 0.50/0.30/0.20（改动前硬编码值）。
        相比原 min(n/5,1) 单因子，带关系的强命中不会被弱命中稀释；有类型标注的实体更可信。
        """
        if not graph_results:
            return 0.0
        try:
            from core import config as _cfg
            w_cov = float(_cfg.get("rag", "w_coverage", 0.50))
            w_rel = float(_cfg.get("rag", "w_relations", 0.30))
            w_typ = float(_cfg.get("rag", "w_typing", 0.20))
        except Exception:
            w_cov, w_rel, w_typ = 0.50, 0.30, 0.20
        hit = min(len(graph_results) / 5.0, 1.0)
        rels = min(len(graph_relations or []) / 3.0, 1.0)
        typed = sum(1 for e in graph_results
                    if str(e.get("entity_type") or "").strip() and str(e.get("entity_type")) != str(e.get("name")))
        type_match = typed / len(graph_results) if graph_results else 0.0
        return min(w_cov * hit + w_rel * rels + w_typ * type_match, 1.0)

    @staticmethod
    def _resolve_scope_docs(conn, raw_docs, include_deprecated: bool = False) -> tuple:
        """KB-S 消费范围自愈：把 `kb_scope.docs` 白名单与**实际存在的文档**求交。

        为什么需要：docs 白名单是**硬锁**——命中即同时按 source_doc（实体）/ doc_names（分块）/
        filename IN（文档粗匹配）三路过滤。白名单里的文档名一旦不存在（重命名 / 删库 /
        手配错字 / 迁移漏改），三路会**同时**归零且全程静默：调用方只看到「检索为空」，
        无法区分「库里确实没有」与「白名单写错了」。实测 2026-09-19：design agent 白名单
        2 条（还是重复项）全不存在 → 4887 块 SysML 规范恒不可见，模型只能凭空编造。

        口径：
        - 去重保序（重复项是常见脏数据：不改变语义，但会让日志与判定难读）；
        - 失效项一律剔除（不让它参与过滤）；
        - **全部失效** → 返回空列表 = 不做文档过滤（退化为该 Agent 的 branches 口径）。
          取舍理由：放宽的代价是「范围略大」，归零的代价是「功能整体失效」——后者更不可接受；
        - 任何异常 → 原样返回（不改变既有行为，不阻断主链路）。

        `kb_scope.docs_missing_fallback=False` 时不放宽（回到改动前行为，仅告警）。
        ⚠️ 2026-09-21（G2）：求交时必须**排除已下线（deprecated）文档**。自愈的语义是
        「剔除失效项」，而 `lifecycle_status='deprecated'` 的文档在检索侧已被 G1 挡掉
        —— 若这里仍把它算作「有效」，白名单就会被判成全有效而零成本直通，
        最终三路过滤**同时归零且全程静默**，正是本方法当初要消灭的那个失败形态。
        返回 `(有效docs, warn|None)`；warn = {requested, missing, effective, unfiltered}。
        """
        docs = [str(d).strip() for d in (raw_docs or []) if str(d).strip()]
        if not docs:
            return [], None
        uniq = list(dict.fromkeys(docs))
        try:
            from core import config as _cfg
            fallback_on = _cfg.get("kb_scope", "docs_missing_fallback", True)
            warn_on = _cfg.get("kb_scope", "docs_missing_warn", True)
        except Exception:
            fallback_on, warn_on = True, True
        try:
            ph = ",".join("?" * len(uniq))
            rows = conn.execute(
                f"SELECT DISTINCT filename FROM documents WHERE filename IN ({ph})"
                + ("" if include_deprecated else
                   " AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('deprecated'))"),
                uniq).fetchall()
            have = {r["filename"] for r in rows}
        except Exception as _e:
            # 查不动就不动（保全现状）—— 不阻断主链路的降级路径。
            # ⚠️ 必须留痕（2026-09-23）：这处静默兜底曾把「夹具 documents 表缺
            # lifecycle_status 列」伪装成「白名单自愈逻辑没生效」，排查时极易误判。
            # 降级路径一律打日志，是本仓库的既有纪律。
            try:
                print(f"[kb_scope] 白名单求交失败，本次跳过自愈（白名单原样返回）："
                      f"{type(_e).__name__}: {_e}", flush=True)
            except Exception:
                pass
            return uniq, None                # 查不动就不动（保全现状）
        missing = [d for d in uniq if d not in have]
        ok = [d for d in uniq if d in have]
        if not missing:
            return ok, None                  # 白名单全有效 → 零成本直通
        if fallback_on:
            effective, unfiltered = ok, (not ok)
        else:
            effective, unfiltered = uniq, False
        if warn_on:
            try:
                print(f"[kb_scope] 文档白名单 {len(missing)}/{len(uniq)} 篇不存在，已剔除：{missing[:5]}"
                      f"{'（全部失效 → 放宽为不限文档）' if unfiltered else f'；有效 {len(effective)} 篇'}",
                      flush=True)
            except Exception:
                pass
        return effective, {"requested": uniq, "missing": missing,
                           "effective": effective, "unfiltered": unfiltered}

    @staticmethod
    def _release_branches(conn) -> list:
        """已发布(release)分支列表：AI 建模消费侧默认只查这些分支（需求：默认只消费已发布分支数据）。

        无 release 分支时回退字面 'release'（历史兼容）；再失败返回空（不检索，避免越权消费 dev 数据）。
        """
        try:
            rows = conn.execute(
                "SELECT name FROM branches WHERE branch_type='release' ORDER BY id").fetchall()
            return [r["name"] for r in rows] or ["release"]
        except Exception:
            return ["release"]

    @staticmethod
    def _tokens(text):
        """中文 2-4 字滑动窗口 + 英文词 + 分隔后的「整词」（用于图谱实体链接与附件命中）。

        2026-09-21 修复（P1-6 评测集暴露，见 docs §8）：
          原实现返回 `set`，而 Python 字符串哈希受 PYTHONHASHSEED 随机化 →
          消费点 `toks[:8]` 每个进程取到**不同的** 8 个 token，叠加 SQL `LIMIT 10`
          截断，同一 query 的实体链接结果**跨进程不一致**。实测同一 query
          「电源分系统 由哪些 蓄电池组 组成」在两个进程里，一个命中「蓄电池组」、
          另一个漏掉 → 路由决策随之改变（同一分钟内 graph/0.88 → vector/0.0），
          使"改动前后对比"与"评测分数"都不可复现。
          现改为 **有序去重列表**（生成顺序确定），并把「按分隔符切出的整词」优先入列：
          整词比滑窗碎片更贴近实体名，同时修掉「长句里第二个实体名被挤出前 8 个」。

        返回：list[str]（有序去重）。**不再是 set** —— 三个调用点均只做遍历/切片，
        无集合运算（`_match_attachment` 另有 sorted，本就与顺序无关）。
        """
        ordered, seen = [], set()

        def _add(w):
            if w and w not in seen:
                seen.add(w)
                ordered.append(w)

        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{1,}", (text or "").lower()):
            _add(w)
        # 整词优先：按「非字母数字/非汉字」切分，段本身即候选
        for seg in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text or ""):
            if len(seg) >= 2:
                _add(seg)
        cjk = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
        stop = set("的了是在和与及或把被让对从向为以于就都也很而但并且如果因为所以这些那些我们您请帮我")
        n = len(cjk)
        for i in range(n):
            for L in (2, 3, 4):
                w = cjk[i:i + L]
                if len(w) == L and not all(ch in stop for ch in w):
                    _add(w)
        return ordered

    @staticmethod
    def _entity_link(conn, query, branches, source_docs=None):
        """图谱实体链接（P1 2026-09-01：graph_db.enabled 时优先 TDB SPARQL 命名图查询）。

        - 图库优先：按分支命名图过滤，整句→token 两级匹配，命中后从 SQLite 回填属性
          （TDB 为查询镜像，SQLite 为治理权威源）。
        - 回退：SPARQL 无命中 / 未启用 / 异常 → 原 SQLite LIKE 路径（可开关，保全现状）。
        branches: 分支列表（消费侧传已发布(release)分支）；source_docs: 来源文档过滤（SQLite 侧）。
        """
        # P1：真图数据库优先（分支命名图）
        try:
            from core import config
            if config.as_bool("graph_db", "enabled"):
                from graph_db import get_writer, query_entities_by_name
                _w = get_writer(True, config.get("graph_db", "backend", "pyoxigraph"),
                                config.get("graph_db", "path", ""),
                                config.get("graph_db", "use_memory", False))
                try:
                    hits = query_entities_by_name(_w, query, branches=branches, limit=10)
                    if hits:
                        out = []
                        ids = [h["id"] for h in hits if h.get("id")]
                        if ids:
                            ph = ",".join("?" * len(ids))
                            rows = conn.execute(
                                f"SELECT * FROM entities WHERE id IN ({ph}) "
                                f"AND status != 'deprecated'", ids).fetchall()
                            out = [dict(r) for r in rows]
                        if not out:
                            out = [{"id": h["id"] or h["name"], "name": h["name"],
                                    "entity_type": h.get("entity_type") or "",
                                    "status": "reviewed", "branch": h.get("branch", ""),
                                    "source_doc": ""} for h in hits[:10]]
                        return out
                finally:
                    _w.close()
        except Exception:
            pass  # 图库异常 → 回退 SQLite（保全现状）

        in_sql = ",".join("?" * len(branches))
        doc_sql, doc_params = "", []
        if source_docs:
            doc_sql = " AND source_doc IN ({})".format(",".join("?" * len(source_docs)))
            doc_params = list(source_docs)
        rows = conn.execute(
            f"SELECT * FROM entities WHERE (name LIKE ? OR id LIKE ?) AND status != 'deprecated' "
            f"AND branch IN ({in_sql})" + doc_sql + " LIMIT 10",
            (f"%{query}%", f"%{query}%", *branches, *doc_params)
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
        # P2（2026-09-07）entity_aliases 消费：mention 原文（别名/缩写/融合前名称）
        # 在实体名 LIKE 未命中时按别名召回——写入侧见 entity_resolver.write_entity_alias。
        # 注意占位符顺序：branch IN 在 ON 子句中先于 WHERE 的 alias LIKE，参数需按 SQL 文本序绑定。
        alias_rows = []
        try:
            a_cond, a_params = ["(a.alias_name LIKE ? OR a.alias_name LIKE ?)"], [f"%{query}%", f"%{query}%"]
            _a_toks = [t for t in GraphRAG._tokens(query) if len(t) >= 2][:8]
            if _a_toks:
                a_cond.append("(" + " OR ".join(["a.alias_name LIKE ?"] * len(_a_toks)) + ")")
                a_params += [f"%{t}%" for t in _a_toks]
            alias_rows = conn.execute(
                f"SELECT DISTINCT e.* FROM entity_aliases a "
                f"JOIN entities e ON e.id=a.entity_id AND e.status != 'deprecated' "
                f"AND e.branch IN ({in_sql})" + doc_sql.replace("source_doc", "e.source_doc") +
                f" WHERE " + " OR ".join(a_cond) + " LIMIT 10",
                tuple(list(branches) + doc_params + a_params)).fetchall()
        except Exception:
            alias_rows = []
        if alias_rows:
            out = []
            for r in alias_rows:
                d = dict(r)
                d["_via_alias"] = True  # 召回原因可解释：经 mention 别名命中
                out.append(d)
            return out
        toks = [t for t in GraphRAG._tokens(query) if len(t) >= 3]
        if toks:
            conds, params = [], []
            for t in toks[:8]:
                conds.append("(name LIKE ? OR id LIKE ?)")
                params += [f"%{t}%", f"%{t}%"]
            params += branches
            params += doc_params
            try:
                rows = conn.execute(
                    f"SELECT * FROM entities WHERE ({' OR '.join(conds)}) AND status != 'deprecated' "
                    f"AND branch IN ({in_sql})" + doc_sql + " LIMIT 10",
                    tuple(params)
                ).fetchall()
            except Exception:
                rows = []
            return [dict(r) for r in rows]
        return []

    @staticmethod
    def _glossary_class_link(conn, gres: dict, branches, source_docs=None) -> dict:
        """P1（2026-09-07）词典→图谱桥接：maps_to 消费通道。

        归一化命中的概念若带本体映射（glossary_concepts.maps_to_class / maps_to_inst），
        按映射直查图谱实体并并入 GraphRAG 结果：
        - maps_to_inst：实例 IRI → 取 localname 直查 entities.id（精确 1:1）
        - maps_to_class：类 IRI/类名 → 取 localname 匹配 entities.entity_type（1:N 召回，LIMIT 10）

        返回 {concepts: [{concept_id, pref_label, maps_to_class, maps_to_inst}],
              entities: [{...entity, via_concept}]}
        """
        out = {"concepts": [], "entities": []}
        if not gres or not branches:
            return out
        seen_concepts, class_names, inst_ids = set(), set(), set()
        for h in gres.get("hits") or []:
            cls_iri = (h.get("maps_to_class") or "").strip()
            inst_iri = (h.get("maps_to_inst") or "").strip()
            if not cls_iri and not inst_iri:
                continue
            cid = h.get("concept_id") or ""
            if cid and cid not in seen_concepts:
                seen_concepts.add(cid)
                out["concepts"].append({
                    "concept_id": cid, "pref_label": h.get("canonical_term", ""),
                    "maps_to_class": cls_iri, "maps_to_inst": inst_iri,
                    "via_term": h.get("user_term", "")})
            if inst_iri:
                local = re.split(r"[#/]", inst_iri)[-1].strip()
                if local:
                    inst_ids.add(local)
            if cls_iri:
                local = re.split(r"[#/]", cls_iri)[-1].strip()
                if local:
                    class_names.add(local)
        in_sql = ",".join("?" * len(branches))
        doc_sql, doc_params = "", []
        if source_docs:
            doc_sql = " AND source_doc IN ({})".format(",".join("?" * len(source_docs)))
            doc_params = list(source_docs)
        found = {}
        # 实例级：直查 id
        for iid in list(inst_ids)[:10]:
            try:
                row = conn.execute(
                    "SELECT * FROM entities WHERE id=? AND status != 'deprecated' "
                    f"AND branch IN ({in_sql})" + doc_sql,
                    (iid, *branches, *doc_params)).fetchone()
            except Exception:
                row = None
            if row:
                found[row["id"]] = dict(row)
        # 类级：entity_type 匹配（类名可能带命名空间前缀，localname 已剥离）
        if class_names:
            ph = ",".join("?" * len(class_names))
            try:
                rows = conn.execute(
                    f"SELECT * FROM entities WHERE entity_type IN ({ph}) "
                    f"AND status != 'deprecated' AND branch IN ({in_sql})" + doc_sql + " LIMIT 10",
                    tuple(list(class_names) + list(branches) + doc_params)).fetchall()
            except Exception:
                rows = []
            for r in rows:
                found[r["id"]] = dict(r)
        for eid, e in found.items():
            e["via_concept"] = True  # 召回原因可解释：经词典概念 maps_to 映射召回
            out["entities"].append(e)
        return out

    @classmethod
    def _match_attachment(cls, query, attachment_text, top_k=5):
        """上传文档命中：词法（2-4 字滑窗）+ 语义（bigram 向量）双路召回，返回 top_k 片段。

        返回 [{content, matched, mode: lexical|semantic, score}]——作为「上传资料」首要检索依据。
        语义路用轻量 bigram 余弦（VectorEngine），弥补提问措辞与文档用词不同导致的漏召回。
        """
        q_tokens = cls._tokens(query)
        if not q_tokens or not attachment_text:
            return []
        paras = [p.strip() for p in re.split(r"[\n\r。！？!?；;]", attachment_text) if len(p.strip()) >= 10]
        if not paras:
            return []
        # 词法命中：2-4 字滑动窗口 + 英文词
        lexical = []
        for p in paras:
            matched = [t for t in q_tokens if t in p]
            if matched:
                lexical.append({"content": p[:300], "matched": sorted(matched)[:6],
                                "mode": "lexical", "score": len(matched)})
        # 语义召回：真 embedding 优先（bigram 降级），补充词法未命中的段落
        seen = {h["content"] for h in lexical}
        semantic = []
        try:
            from semantic import SemanticSearch
            for score, p in SemanticSearch().rank(query, paras, top_k=top_k * 3, threshold=0):
                seg = p[:300]
                if seg in seen:
                    continue
                seen.add(seg)
                semantic.append({"content": seg, "matched": [], "mode": "semantic", "score": round(score, 3)})
        except Exception:
            pass  # 语义召回失败不阻塞词法命中
        # 合并排序：词法优先，语义按相似度降序
        hits = lexical + semantic
        hits.sort(key=lambda h: (h["mode"] != "lexical", -h["score"]))
        return hits[:top_k]


class ConflictDetector:
    """FR-MG-3: Conflict detection between new and existing elements."""

    def detect(self, new_entities, branch="dev"):
        conn = get_db()
        conflicts = []
        for ent in new_entities:
            existing = conn.execute(
                "SELECT * FROM entities WHERE entity_type=? AND status='reviewed' AND branch!=? AND id!=?",
                (ent.get("entity_type", ""), branch, ent.get("id", ""))
            ).fetchall()
            for ex in existing:
                ex_props = json.loads(ex["properties"]) if ex["properties"] else {}
                new_props = ent.get("properties", {})
                # Simple conflict: same type, overlapping key attributes with different values
                for key in set(ex_props) & set(new_props):
                    if ex_props[key] != new_props[key]:
                        conflicts.append({
                            "new_id": ent.get("id"),
                            "existing_id": ex["id"],
                            "existing_name": ex["name"],
                            "field": key,
                            "existing_value": ex_props[key],
                            "new_value": new_props[key],
                        })
        conn.close()
        return conflicts


