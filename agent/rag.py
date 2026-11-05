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
        # P0-b M4：rag span（会话级 trace）。开在入口——AST 验证本函数体
        # （L126-428）只有 L398 一个 return ⇒ 单点闭即全覆盖；异常路径不闭
        # 也不留假象：SpanCollector.flush 对未闭 span 按 error 收口并带
        # note=unclosed_at_flush（services/orch_span.py 文件头失败语义）。
        # 无 tracer（编排子任务/非 chat/stream 路径）⇒ sid=0，闭点 no-op。
        _rag_sid = 0
        _rag_tracer = None
        try:
            from services.orch_span import current_tracer as _ct
            _rag_tracer = _ct()
            if _rag_tracer is not None:
                _rag_sid = _rag_tracer.open_rag(t0=t0)
        except Exception:
            _rag_sid = 0   # 观测装配失败绝不阻断检索主链路
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
        # 2026-10-16 修：关系连接性只数**与 query 相关的关系**（两端实体名均出现在 query 中）——
        #   否则任何提及系统级实名的域外查询都会因"命中实体+系统间有关系"拿到高置信走图谱路由
        #   （实测 neg-07「宽带通信卫星的太阳能板清洗周期」：4 实体 432 关系 → conf 0.9 → graph，
        #   而图谱根本没有"太阳能板清洗周期"的关系可答）。开关 rag.relation_relevance_gate。
        graph_confidence = self._graph_confidence(graph_results, graph_relations, query=query)

        # Step 3: 真路由——GraphRAG 命中且置信足够 → 跳过向量；否则向量兜底（chunk 优先，文档粗匹配兜底）
        vector_results = []
        chunk_hits = []
        # P0-1（2026-11-01）：低置信过滤统计，供 trace 落库（"丢了几条"必须可查）
        _lc_stats = {}
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
                    graph_confidence = self._graph_confidence(graph_results, graph_relations, query=query)
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
                # ── P1-7（2026-10-16）检索侧 Corrective：域约束致零召回 ⇒ 撤域重查一次 ──
                # 实测根因（69 例评测集 doc 43 例）：4 例金标在召回池位次 1~4、confidence_level「高」，
                # 但 retrieve() 返回 chunk_hits=0 —— **不是召回差，是被 domain 过滤整个掉**。
                # 链条：query 里的 `PA`(part 首字母缩写) / `SA` 被 Glossary 命中
                #   → force_domain=satellite_comms（卫星通信域）
                #   → 强制按该域过滤，而金标在**热管理建模**知识库 ⇒ 跨域污染，命中 0
                #   → 退化到文档粗匹配（search_chunks 对这 4 例金标一个都召不回）。
                # ⇒ 对策：**只在「带域约束 且 命中为空」这一窄条件下**撤掉 domain 重查一次。
                #   刻意不做泛化的 LLM 查询改写：评测显示这 4 例原始 query 本就能召全
                #   （金标位次 1,2,3,4），坏的是域约束而非措辞，改写只会引入新噪声。
                # 约束：最多重查 1 次（撤域后仍空即认账，走原降级路径）。
                # 开关 rag.corrective_domain_retry（默认 True，置 False 回旧行为）。
                if not chunk_hits and force_domain:
                    _retry = self._retry_without_domain(
                        conn, query, boost, scope_docs, _rag_top_k, _inc_dep)
                    if _retry:
                        chunk_hits = _retry
                # ⚠️ S4（2026-10-29）：原此处有一段 `trace_query(...)`，`"route": "vector"`
                #   **硬编码**且**只在本（向量兜底）分支内**调用 ⇒
                #   `query_trace` 表实测 3,484 条**全部是 vector**，图/混合路由零 trace。
                #   后果：我曾据此判"图路由零命中"，查 `query_routing_stats` 才发现
                #   图路由占 264/1867 —— **观测口径错误导演了一个不存在的结论**。
                #   已移到 Step 4 路由决策之后统一写（用真实 route），见下方 trace 块。
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
            # 域外查询的向量命中置信度极低（2026-10-16 标定：neg 8 例中 7 例 top5 全「低」、
            # 最大 vec_score 0.359；doc 43 例在全低判据下 0 误伤）——把这些命中喂给 LLM
            # 是幻觉温床（问"今天北京天气"也拿到 20 条"相关知识"）。判据：注入宽度内
            # （chunk_hits 全部）confidence_level 全为「低」⇒ 向量内容**整体不返回**
            # （chunk 级 + 文档粗匹配兜底）。只作用于向量兜底路径：图谱路由有独立
            # route_threshold 门禁、附件命中不拦（用户上传资料是首要依据）。
            # 开关 rag.suppress_low_confidence（默认 True）。
            # ★ 2026-11-01（P0-1）：「全批低置信 ⇒ 整批清零」改为「按分数门槛过滤」。
            #   旧行为的问题（A/B 实测 tools/verify/ab_suppress_low_conf.py）：
            #     embedding 服务 502 → 降级 bigram → vec_score 塌到 0.07~0.29 → 必判「低」
            #     → 整批丢弃 ⇒ **连切题金标（4.2.2 系统需求分析）也一起丢**，
            #     实测 6 条 query 只 1 条有结果。这是"静默清零"，最难归因。
            #   新口径：只丢"低于门槛"的，并且把"降级/丢弃"如实标注出来（不再无声无息）。
            if chunk_hits:
                # ── P3-a（2026-11-02）元数据块不参与排序 ──────────────────
                # 块级评测实测：封面/版权页/签批表/目录会靠"文档标题里的关键词"空降前 5，
                # 白占 inject_k 的槽（实例：`818:0`「> 所属方法论… 更新日期…」排在
                # succession 问题的第 2 位；`847:1` 签批表排在"交付哪些文档"的第 4 位）。
                # 位置选在 `_filter_low_conf` **之前**：先摘噪声再算置信度，
                # 否则噪声会被算进 (max, avg) 从而影响域外拒答判定。
                # 保护：`filter_metadata` 内部有 min_keep，剩太少就放弃过滤（绝不清空）。
                try:
                    from agent.metadata_blocks import filter_metadata
                    from core import config as _cfg_mb
                    if _cfg_mb.as_bool("rag", "drop_metadata_blocks", True):
                        _min_keep = int(_cfg_mb.get("rag", "metadata_min_keep", 3) or 3)
                        chunk_hits, _mb_stats = filter_metadata(chunk_hits, min_keep=_min_keep)
                        if _mb_stats.get("dropped"):
                            _mb_stats["query"] = (query or "")[:60]
                except Exception:
                    pass    # 判定失败绝不阻断检索（退化为不过滤，比丢结果安全）
                chunk_hits, _lc_stats = self._filter_low_conf(chunk_hits)
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
        # ── S4（2026-10-29）查询 Trace：**改到路由决策之后，用真实 route** ──────
        # 修的是什么（实测证据）：
        #   原实现把 `trace_query(...)` 放在**向量兜底分支内部**，且 `"route": "vector"`
        #   是**硬编码** ⇒ `query_trace` 表 3,484 条**全部 route=vector**，
        #   图/混合路由**一条 trace 都没有**。
        #   危害不是"少记几行"，而是**观测口径错了会导演出不存在的结论**：
        #   我据该表判过"图路由零命中"，查 `query_routing_stats` 才发现
        #   图路由实际占 264/1,867（14%）—— 结论完全相反。
        # 现在：
        #   · 三种路由**都写** trace（graph / mixed / vector）；
        #   · `route` 用决策结果真值，另附 `reason`（graph_no_hit / no_hit_fallback …）
        #     与 `graph_count`/`vector_count`，使"为什么走这条路"可直接按列聚合；
        #   · `hit_docs` 取**该路由实际参与结果的来源**，不再是向量分支的局部变量。

        # ── P0-1b（2026-11-01）域外整体拒答 ──────────────────────────────────
        # 与 low_conf_floor 是两层语义：
        #   low_conf_floor → 逐条过滤（去掉明显无关的，留下真命中）
        #   这里           → 整体拒答（**最好的那条都不够** ⇒ 查询不在知识域内）
        # 为什么必须有这一层：只做逐条过滤时，域外问题仍会拿到 1~20 条"字面巧合"片段
        #   （实测"Python 写爬虫"拿到 20 条、"卫星太阳能板清洗"拿到 1 条），
        #   喂给 LLM 就是幻觉温床 —— 实测拒答准确率仅 0.333。
        # 判据（**两者都低于才拒**，宁少拒不误杀）：
        #   top1 relevance < reject_max_score 且 均分 < reject_avg_score
        # 生效范围（很关键）：**只在「向量路由 且 图谱无命中」时**——
        #   · 模型域问题（"转发器 EIRP 多少"）走 graph/mixed，命中实体就算有依据，不受此限；
        #   · 附件命中优先（attachment_hits）也不受此限。
        _reject_info = {"rejected": False}
        try:
            if (route == "vector" and not graph_results and chunk_hits
                    and not attachment_hits):
                from core import config as _cfg_rj
                _rj_max = float(_cfg_rj.get("rag", "reject_max_score", 0.35) or 0)
                _rj_avg = float(_cfg_rj.get("rag", "reject_avg_score", 0.22) or 0)
                _rels = [self._relevance_of(h) for h in chunk_hits]
                _mx = max(_rels) if _rels else 0.0
                _av = (sum(_rels) / len(_rels)) if _rels else 0.0
                _reject_info = {"rejected": False, "max": round(_mx, 4),
                                "avg": round(_av, 4),
                                "max_th": _rj_max, "avg_th": _rj_avg}
                if _rj_max > 0 and _mx < _rj_max and _av < _rj_avg:
                    # ── P1-a（2026-11-01）补救：判域外之前，用高区分度关键词再确认一次 ──
                    # 为什么必须有这一步（实测，不是推测）：
                    #   `信息安全有哪些要求` 捞到的正是技术要求书 7.4 的信息安全条款
                    #   （正文为"网络会话加密/弱口令/组件安全/白名单"，内容完全正确），
                    #   但 bigram 降级态下 relevance 只有 0.195 / 0.194，双双低于
                    #   0.35 / 0.22 ⇒ 被判 out_of_domain **整批清空**，
                    #   用户看到的是"知识库里没有相关内容" —— 纯误杀。
                    # 判据：关键词重试**仍能命中**，就证明"查询携带的领域实词在库中有对应
                    #   内容"⇒ 域外的前提不成立，不该拒。真正的域外问题（红烧肉/北京天气）
                    #   在重试里召不回任何条目，仍会正常拒答。
                    _kw_kept = []
                    try:
                        _b = float(boost)      # boost 在 vector 路由下必已定义，防御性处理
                    except Exception:
                        _b = 1.0
                    try:
                        _kw = self._retry_with_keywords(
                            conn, query, _b, scope_docs, _rag_top_k, _inc_dep)
                        if _kw:
                            _kw_kept, _ = self._filter_low_conf(_kw)
                    except Exception:
                        _kw_kept = []
                    # ★★ 2026-11-02 修正：**重试结果也必须过同一套域外判据**。
                    #   旧实现只要 `_kw_kept` 非空就无条件采纳，而 `_filter_low_conf`
                    #   只按 `low_conf_floor`（0.15）过滤 —— 比拒答阈值（0.481）宽松得多
                    #   ⇒ 任何"能字面匹配一点东西"的域外问题都会被救活。
                    #   实测（embedding 恢复后阈值重标定，11/02）：
                    #     `红烧肉怎么做` → 关键词重试捞回 **20 条 KerML 英文规范**
                    #     （kw_retry_rescued=20）⇒ 拒答被完全绕过 ⇒ 基线拒答准确率
                    #     从标定预测的 11/11 跌到实际 3/5。
                    #   为什么上一轮没暴露：当时阈值 0.35 是按降级态标的，
                    #   降级态下重试命中分数也低，`_filter_low_conf` 就把它滤光了，
                    #   所以"无条件采纳"没有机会发生 —— **阈值一放宽，缺陷立刻显形**。
                    #   ⇒ 现在对重试结果再算一次 (max, avg)，同样要求"两者都低于才拒"。
                    if _kw_kept:
                        _kw_rels = [self._relevance_of(h) for h in _kw_kept]
                        _kw_mx = max(_kw_rels) if _kw_rels else 0.0
                        _kw_av = (sum(_kw_rels) / len(_kw_rels)) if _kw_rels else 0.0
                        _reject_info["kw_retry"] = {"n": len(_kw_kept),
                                                    "max": round(_kw_mx, 4),
                                                    "avg": round(_kw_av, 4)}
                        if _rj_max > 0 and _kw_mx < _rj_max and _kw_av < _rj_avg:
                            # 重试命中的同样是弱相关 ⇒ 判定域外，不采纳
                            _kw_kept = []
                    if _kw_kept:
                        _reject_info["kw_retry_rescued"] = len(_kw_kept)
                        chunk_hits = _kw_kept
                        vector_results = chunk_hits
                    else:
                        # 拒答：清空结果，但**保留判据数字**，让"为什么拒"可查（不静默）
                        _reject_info["rejected"] = True
                        _reject_info["dropped"] = len(chunk_hits)
                        chunk_hits = []
                        vector_results = []
                        route, reason = "vector", "out_of_domain"
                        # ★ 2026-11-02：拒答清空后必须同步 low_conf 统计口径。
                        #   否则 `stats.kept` 还是"拒答前"的数字（实测 L3b 转红：
                        #   统计说 11 条、实际返回 0 条）。**同一语义的量在两处各写一份，
                        #   迟早对不上** —— 这是本项目反复踩的老坑，见下方去重块的同类同步。
                        _lc_rj = locals().get("_lc_stats")
                        if isinstance(_lc_rj, dict):
                            _lc_rj["kept"] = 0
                            _lc_rj["rejected_dropped"] = _reject_info["dropped"]
        except Exception:
            pass   # 拒答判定失败绝不阻断检索（退化为不拒答，比误拒安全）

        # ── P0-a（2026-11-01）检索结果去重 + 来源名归一化 ────────────────────
        # 位置在**拒答之后**，保证对"原始命中"和"关键词重试捞回的命中"一视同仁
        # （只做一次，不会漏掉重试路径）。
        # 为什么必须在 rag 层而不是 select_diverse 里做：citations 直接用本函数的
        # `chunk_hits`（不经过 select_diverse），只在后者做会让引用区仍出现两份相同来源。
        try:
            from agent.retrieval_dedup import dedupe_and_normalize
            from core import config as _cfg_dd
            if chunk_hits and _cfg_dd.as_bool("rag", "dedupe_exact", True):
                _min_c = int(_cfg_dd.get("rag", "dedupe_min_chars", 80) or 80)
                _before_dd = len(chunk_hits)
                chunk_hits, _dd_stats = dedupe_and_normalize(chunk_hits, min_chars=_min_c)
                vector_results = chunk_hits
                if _dd_stats.get("dup_dropped") or _dd_stats.get("doc_renamed"):
                    _dd_stats["query"] = (query or "")[:60]
                # ★ 同步 low_conf 统计口径 —— 否则 `stats.kept` 会是"去重前"的数字，
                #   与调用方实际拿到的条数对不上（实测 L3b 门禁因此转红：
                #   统计说 15 条、实际返回 12 条）。**两个数字必须同源**，
                #   否则"为什么少了几条"永远查不清 —— 详见 `deduped` 字段。
                _lc = locals().get("_lc_stats")
                if isinstance(_lc, dict):
                    _after_dd = len(chunk_hits)
                    _lc["deduped"] = _before_dd - _after_dd
                    _lc["kept"] = _after_dd
                    _lc["dropped"] = max(0, int(_lc.get("nominal", _after_dd) or 0) - _after_dd)
        except Exception:
            pass    # 去重失败绝不阻断检索（退化为不去重，比丢结果安全）

        # ── P0-2（2026-11-01）文档级摘要接入 ────────────────────────────────
        # 现状：`documents.summary` 早就在生成（LLM 摘要，实测质量不错：
        #   160~209 字能准确概括文档范围），但 `rag.py` / `knowledge_engine.py`
        #   **完全不读它** —— 已建好的能力在空转。
        # 用途（刻意只做"背景"，不做"证据"）：
        #   chunk 级命中给的是**局部条文**，模型常因缺全局语境而误判
        #   （如把"技术要求书的 7.4 节"当成全部，忽略 11.1 的相关约束）。
        #   补一段文档摘要能显著改善"这段在讲什么、还有哪些相关章节"。
        # 为什么只取**命中已出现的文档**：避免把无关文档的摘要塞进上下文（那是噪声）。
        # 为什么限制条数：摘要 200 字 × 全部命中文档会挤占正文预算；取前 3 个文档足够。
        doc_summaries = []
        try:
            if chunk_hits and conn is not None:
                _seen = []
                for _h in chunk_hits:
                    _d = str(_h.get("source_doc") or "")
                    if _d and _d not in _seen:
                        _seen.append(_d)
                    if len(_seen) >= 3:
                        break
                if _seen:
                    _ph2 = ",".join(["?"] * len(_seen))
                    _rows2 = conn.execute(
                        "SELECT filename, summary FROM documents "
                        f"WHERE filename IN ({_ph2}) AND COALESCE(summary,'') <> ''",
                        _seen).fetchall()
                    _got = {}
                    for _r2 in _rows2:
                        _fn = _r2["filename"] if isinstance(_r2, dict) or hasattr(_r2, "keys") else _r2[0]
                        _sm = _r2["summary"] if isinstance(_r2, dict) or hasattr(_r2, "keys") else _r2[1]
                        if _fn and _sm and _fn not in _got:
                            _got[_fn] = str(_sm)
                    # 按命中顺序输出（与【来源n】的文档顺序一致，便于模型对照）
                    doc_summaries = [{"source_doc": _f, "summary": _got[_f]}
                                     for _f in _seen if _f in _got]
        except Exception:
            doc_summaries = []   # 摘要缺失绝不阻断检索

        try:
            if conn is not None:
                from glossary import trace_query as _tq
                _hit_src = (graph_results if route in ("graph", "mixed")
                            else (chunk_hits or vector_results)) or []
                # 命中档位只在"真的命中"时才有意义；空结果恒 0（不编造 0.0 以外的值）
                _top = 0
                if _hit_src:
                    _f0 = _hit_src[0]
                    _top = (_f0.get("score") or _f0.get("conf") or 0) if isinstance(_f0, dict) else 0
                _tq(conn, query, {
                    "normalized": query,
                    "intent": "knowledge_qa",
                    "route": route,                    # ← 真实路由（本次修复的核心）
                    "domain": force_domain or "all",
                    "hit_docs": [str((h or {}).get("source_doc") or
                                     (h or {}).get("name") or "")[:200]
                                 for h in _hit_src[:5]],
                    "hit_count": len(_hit_src),
                    "top_score": _top,
                    "latency_ms": latency_ms,
                    "detail": {
                        "reason": reason,              # 路由理由（graph_no_hit 等）
                        "graph_count": len(graph_results or []),
                        "vector_count": len(vector_results or []),
                        "glossary": [h.get("user_term") for h in (_glossary_res.get("hits") or [])],
                        "glossary_recall": glossary_recall.get("concepts", []),
                        "recall_reasons": [(h or {}).get("recall_reason", "")
                                           for h in (chunk_hits or [])[:3] if isinstance(h, dict)],
                        # P0-1（2026-11-01）：低置信过滤的可观测性。
                        # 旧实现"整批清零"时这里**什么都不留** ⇒ 无法区分
                        # "库里没内容"与"有内容但被丢了"，是本次故障难归因的直接原因。
                        "low_conf": _lc_stats,
                        # P0-1b：拒答判据数字（rejected/max/avg/阈值）。
                        # 保留数字而不是只存 true/false —— 阈值是否合适要靠这些数字回溯。
                        "out_of_domain": _reject_info,
                    },
                })
        except Exception:
            pass   # 观测失败绝不阻断检索主链路
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
            # ── S4（2026-10-29）：零命中先细分成因再落统计 ────────────────────
            # 实测构成（近 7 天 59 条 no_hit_fallback）：指令性文本 40% + 业务数据查询 15%
            #   ⇒ ≥55% 属于"检索被用在不该用的地方"，不是召回质量问题。
            #   混在一起统计会让"召回失败率"长期虚高、并把人引向错误的优化方向
            #   （去调向量阈值，而真正该改的是调用侧语义错配）。
            # 本步**只细分 reason，不改变任何检索行为**（零风险、可回退）。
            _reason_stat = reason
            try:
                from agent.retrieval_reason import refine_reason as _refine
                _reason_stat = _refine(query, route, reason)
            except Exception:
                _reason_stat = reason
            self.router.record(conn, query, route, _reason_stat, retrieval_confidence,
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
        # P0-b M4：闭 rag span。hit/top_score/latency 口径同 query_routing_stats
        # （L297-298 同源计算，不造第二份数）；rag span 无栈（单次调用顺序执行），
        # 异常路径由 flush 的 unclosed_at_flush 收口。
        if _rag_sid:
            try:
                _hit_n = (len(graph_results) + len(vector_results)
                          + len(chunk_hits) + len(attachment_hits))
                _top = chunk_hits[0]["score"] if chunk_hits else 0
                _rag_tracer.close_rag(_rag_sid, hit_count=_hit_n,
                                      top_score=_top, latency_ms=latency_ms)
            except Exception:
                pass   # 观测收口失败绝不阻断检索主链路
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
            # P0-1（2026-11-01）：低置信过滤回显。让"被丢了几条、门槛多少、是否降级"
            # 直接可见 —— 不再需要靠猜。下游可据此在 prompt 里标注"本次为降级检索"。
            "low_conf": _lc_stats,
            # P0-1b：域外拒答判据回显（rejected/max/avg/阈值）。
            # 消费侧据此给"未在知识库找到依据"的提示与建议（FR-MQ-4），
            # 而不是让模型对着空上下文硬编。
            "out_of_domain": _reject_info,
            # P0-2（2026-11-01）：命中文档的摘要（文档级背景，非证据）。
            # 消费侧（context.py）只在有值时注入一小段，帮模型建立"这段在讲什么"的语境。
            "doc_summaries": doc_summaries,
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
    def _graph_confidence(graph_results: list, graph_relations: list, query: str | None = None) -> float:
        """多因子图谱置信度（2026-09-01 路由增强；P1-4 权重配置化）。

        w_coverage * 命中实体覆盖度(min(n/5,1)) + w_relations * 关系连接性(min(rels/3,1))
        + w_typing * 类型匹配度 —— 默认 0.50/0.30/0.20（改动前硬编码值）。
        相比原 min(n/5,1) 单因子，带关系的强命中不会被弱命中稀释；有类型标注的实体更可信。

        2026-10-16（neg 域误路由修复）：关系连接性**默认只计入与 query 相关的关系**——
        两端实体名均作为子串出现在 query 中（图谱能回答该问题的前提：问题问的就是这两个
        实体之间的关系）。域外查询即便命中系统级实体、图谱里有成百上千条系统间关系，
        相关关系也为 0 → 置信度封顶 0.70 < 阈值 0.75 → 不再走高置信图谱路由。
        开关 rag.relation_relevance_gate（默认 True；置 False 回到旧行为：全量关系计数）。
        实测（2026-10-16 74 例评测集）：graph 18 例相关关系全部 ≥3（置信度不变，零回归）；
        neg-07 相关关系 0 → conf 0.90→0.60，误路由消除。
        """
        if not graph_results:
            return 0.0
        try:
            from core import config as _cfg
            w_cov = float(_cfg.get("rag", "w_coverage", 0.50))
            w_rel = float(_cfg.get("rag", "w_relations", 0.30))
            w_typ = float(_cfg.get("rag", "w_typing", 0.20))
            _rel_gate = bool(_cfg.get("rag", "relation_relevance_gate", True))
        except Exception:
            w_cov, w_rel, w_typ = 0.50, 0.30, 0.20
            _rel_gate = True
        # 关系相关性门禁：只数「两端实体名都出现在 query 里」的关系（见 docstring）
        _rels = graph_relations or []
        if _rel_gate and query:
            _q = str(query)
            _rels = [r for r in _rels
                     if str(r.get("source_name") or "") and str(r.get("target_name") or "")
                     and str(r.get("source_name")) in _q and str(r.get("target_name")) in _q]
        hit = min(len(graph_results) / 5.0, 1.0)
        rels = min(len(_rels) / 3.0, 1.0)
        typed = sum(1 for e in graph_results
                    if str(e.get("entity_type") or "").strip() and str(e.get("entity_type")) != str(e.get("name")))
        type_match = typed / len(graph_results) if graph_results else 0.0
        return min(w_cov * hit + w_rel * rels + w_typ * type_match, 1.0)

    @staticmethod
    def _retry_without_domain(conn, query: str, boost: float, scope_docs,
                              top_k: int, inc_dep: bool) -> list:
        """P1-7：撤掉 domain 约束重查一次（检索侧 Corrective 的唯一动作）。

        ## 为什么只撤域、不做 LLM 查询改写
        2026-10-16 实测（69 例评测集，doc 43 例）确认的失败形态**不是"措辞对不上"**：
        doc-22/23/30/37 这 4 例用**原始 query** 直调 hybrid_search(top_k=4) 时，
        金标位次分别是 [1,2,3,4] / [1,2,3,4] / [1,2,3,4] / [1,2,3]，
        confidence_level 全为「高」，bm25 48~91 —— 查询本身完全没问题。
        坏的是 **Glossary 把 `PA`/`SA` 误判成卫星通信术语 → force_domain=satellite_comms
        → 跨域过滤把热管理知识库的金标全部滤掉**。
        ⇒ 泛化的 HyDE/LLM 改写在这里是**纯噪声**（把已经能召全的 query 改坏）；
        真正该撤销的是那个**域约束**。

        ## 触发条件（刻意收窄）
        必须**同时**满足：① 带了 domain 约束；② 带约束时命中为 0。
        命中非空时一律不重查——那时域约束是有信息量的，不能撤。

        ## 安全边界
        - **最多重查 1 次**，无递归；撤域后仍空即返回 []，由调用方走原降级路径。
        - 撤域后仍保留 `glossary_boost` 与 `doc_names`（来源文档隔离是**租户/项目边界**，
          不是行业偏好，撤掉会有越权风险）。只撤 domain 这一个**软约束**。
        - 开关 `rag.corrective_domain_retry`（默认 True，置 False 精确回退到 P1-7 之前行为）。
        - 纯函数语义：只调 hybrid_search，不改任何状态；失败一律返回 []。
        """
        try:
            from core import config as _cfg
            if not _cfg.as_bool("rag", "corrective_domain_retry", True):
                return []
        except Exception:
            pass
        try:
            from knowledge_engine import hybrid_search as _hybrid
            _hy = _hybrid(conn, query, top_k=top_k, branches=None,
                          domain=None,                # ← 唯一被撤销的约束
                          glossary_boost=boost,
                          doc_names=scope_docs or None,
                          include_deprecated=inc_dep)
            return _hy.get("hits") or []
        except Exception:
            return []

    @staticmethod
    def _retry_with_keywords(conn, query: str, boost: float, scope_docs,
                             top_k: int, inc_dep: bool) -> list:
        """P1-a（2026-11-01）：零召回时用**高区分度关键词**重查一次。

        ## 为什么需要（实测，`agent/query_keywords.py` 有完整记录）
        embedding 不可用 → 降级 bigram-tf → 长问句极易零召回：
            `信息安全有哪些要求` → 0 条
            `信息安全`          → 2 条（内容正确：技术要求书 7.4 的信息安全条款）
        根因是问句里的"有哪些/怎么/吗"稀释了 2-gram 重叠比例，不是知识库没有。

        ## 触发条件（刻意收窄到最窄）
        必须**命中为空**才调用。实测反例：`需求应该怎么写才规范` 原本 13 条命中，
        用关键词重试反而变 0 条 ⇒ 有命中时重试是**纯粹的破坏**，故只做零召回兜底。

        ## 质量怎么保证
        不做事后过滤，而是**只让高区分度词进入重试**（实词 + 长度≥3，
        详见 `high_signal_terms`）。结果仍走下游既有的 `_filter_low_conf`
        与域外拒答判据，分数不够一样会被拒 ⇒ 不会把"字面巧合"变成"有答案"。

        ## 安全边界
        - 最多试 `len(candidates)` 次（≤5），首个非空即返回；全空返回 [] 走原降级路径。
        - 撤 domain（与 `_retry_without_domain` 同理：零召回更可能是域误判所致），
          但**保留** `glossary_boost` 与 `doc_names`（来源隔离是权限边界，不是偏好）。
        - 开关 `rag.zero_hit_retry`（默认 True，置 False 精确回退）。
        - 纯函数语义：只调 hybrid_search，不改状态；任何异常一律返回 []。
        """
        try:
            from core import config as _cfg
            if not _cfg.as_bool("rag", "zero_hit_retry", True):
                return []
        except Exception:
            pass
        try:
            from agent.query_keywords import retry_candidates
            cands = retry_candidates(query)
            if not cands:
                return []
            from knowledge_engine import hybrid_search as _hybrid
            for cand in cands:
                try:
                    _hy = _hybrid(conn, cand, top_k=top_k, branches=None,
                                  domain=None, glossary_boost=boost,
                                  doc_names=scope_docs or None,
                                  include_deprecated=inc_dep)
                    hits = _hy.get("hits") or []
                except Exception:
                    hits = []
                if hits:
                    # 标注来源，便于从结果侧分辨"这是重试捞回来的"。
                    # ⚠️ 必须用**独立字段** `recall_via`，不能用 `recall_reason`：
                    #   后者已被上游写成 `RRF 融合(vec#… + bm25#…) + HyDE`，
                    #   用 `setdefault` 根本不会覆盖 ⇒ 标记永远打不上（实测踩过，
                    #   表现为"关键词重试明明捞回了内容，却仍被判域外"）。
                    for h in hits:
                        if isinstance(h, dict):
                            h["recall_via"] = "keyword_retry"
                    return hits
            return []
        except Exception:
            return []

    @staticmethod
    def _low_conf_suppressed(chunk_hits: list) -> bool:
        """P1-5：低置信检索抑制判据——注入宽度内（chunk_hits 全部，即 rag.top_k 条）**全部**为「低」置信。

        标定（2026-10-16，69 例评测集，注入宽度 top_k=4）：
          doc 43 例：top1 低仅 doc-27 一例，且其 top2-5 全「高」（gold 在前 5）⇒ 全低判据下 **0 误伤**
          neg  8 例：7 例 top5 全「低」（最大 vec_score 0.359）⇒ 全低判据下 **全命中**
        判据从「首条低」升级为「全批低」的原因：A/B 实测「首条低」误伤 doc 2/43
        （recall@5 0.837 vs 0.884）——那些查询 top1 低但后续条高置信，整批挡掉会丢金标。
        开关 rag.suppress_low_confidence（默认 True，置 False 回旧行为）。

        ⚠️ 2026-11-01：本函数**已不再被主链路调用**（保留为兼容/门禁引用）。
        主链路改用 `_filter_low_conf()` —— 理由：本函数只能表达"丢光/不丢光"两态，
        而降级场景需要"丢一部分、留一部分"。判据逻辑（含强 BM25 豁免）已在
        `_filter_low_conf` 内原样继承，二者口径一致。
        """
        try:
            from core import config as _cfg
            if not _cfg.as_bool("rag", "suppress_low_confidence", True):
                return False
        except Exception:
            pass
        try:
            if not chunk_hits:
                return False
            # ★ 2026-10-20（会话 9565 实测）：**强 BM25 命中豁免抑制**。
            #   embedding 服务不稳（降级 bigram / 熔断）时 vec_score 全面走低，
            #   旧判据"全批低 ⇒ 整批丢弃"会把 BM25 高分（稀疏精确匹配，不依赖
            #   embedding）的真命中一起丢掉——实测广汽方法论 chunk bm25=105
            #   仍被整批抑制，工具侧只剩"词重叠 0/5"。豁免阈值 40：bm25 归一
            #   （/50 封顶）达 0.8 的强命中，任一条存在即不抑制（宁多给不误杀，
            #   下游 graph_retrieve 自带词重叠弱相关判定兜底）。
            _strong_bm25 = any(float(h.get("bm25_score") or 0) >= 40.0 for h in chunk_hits)
            if _strong_bm25:
                return False
            return all(str(h.get("confidence_level") or "") == "低" for h in chunk_hits)
        except Exception:
            return False

    # ── P0-1（2026-11-01）低置信「过滤」而非「清零」────────────────────────────
    #: 强 BM25 豁免线（与 `agent/retrieval_relevance.STRONG_BM25` 同口径）
    _STRONG_BM25 = 40.0

    @staticmethod
    def _relevance_of(hit: dict) -> float:
        """单条命中的"相关度"（0~1），**复用 knowledge_engine 的同一量纲**。

        why 不直接用 `score`：`score` 是 RRF 融合分（≈0.03~0.06，量纲不可解释）；
        而 `confidence_level` 的定级依据是 `max(vec_score, bm25/50)`，
        实测该值切题 0.273~0.632 / 域外 0.069（差 4 倍，可比 score 的 2 倍更可分）。
        用同一把尺子，阈值才有物理意义。
        """
        try:
            vs = float(hit.get("vec_score") or 0)
        except Exception:
            vs = 0.0
        try:
            bs = float(hit.get("bm25_score") or 0)
        except Exception:
            bs = 0.0
        return max(vs, min(bs / 50.0, 1.0) if bs > 0 else 0.0)

    def _filter_low_conf(self, chunk_hits: list) -> tuple:
        """全批低置信时按 `rag.low_conf_floor` 过滤，返回 `(kept, stats)`。

        设计取舍（为什么不是"直接关掉 suppress"）：
          · 关掉 suppress ⇒ 域外 query（今天天气）也能拿到 3~20 条噪声 —— 矫枉过正；
          · 保留整批清零 ⇒ 降级时切题命中全丢 —— 也就是现在的故障；
          · 按门槛过滤是唯一同时满足"救回切题"与"挡住域外"的做法（A/B 已验证可分）。

        stats 供 trace/观测使用，字段名与既有 `suppressed_count` 口径衔接：
          nominal            过滤前条数
          kept               过滤后条数
          dropped            被丢条数
          floor              本次生效门槛
          all_low            是否全批低置信（即是否触发了本逻辑）
          strong_bm25_exempt 是否因强 BM25 而豁免（豁免时不做过滤）
          degraded           是否处于 embedding 降级态
        """
        stats = {
            "nominal": len(chunk_hits or []), "kept": len(chunk_hits or []), "dropped": 0,
            "floor": 0.0, "all_low": False, "strong_bm25_exempt": False, "degraded": False,
        }
        if not chunk_hits:
            return [], stats
        try:
            from core import config as _cfg
            if not _cfg.as_bool("rag", "suppress_low_confidence", True):
                return chunk_hits, stats          # 开关关闭 ⇒ 完全不过滤（旧行为）
            floor = float(_cfg.get("rag", "low_conf_floor", 0.15) or 0.0)
        except Exception:
            floor = 0.15
        stats["floor"] = floor

        # 降级判定：优先用 knowledge_engine 如实上报的 `vector_mode`（唯一可信来源）。
        # 为什么要标：降级时"低置信"是**系统性塌陷**（实测 vec 0.07~0.29 vs 真向量 0.4~0.8）
        #   而非"内容不相关"，必须让下游知道"这次分数不可信"，否则会把降级当结论。
        # fallback：老数据/别的召回路径没有 vector_mode 时，按 embed_version 推断
        #   （出现 bigram-tf 视为降级）；两者都没有则不臆断，保持 False。
        try:
            _modes = {str(h.get("vector_mode") or "") for h in chunk_hits}
            _modes.discard("")
            if _modes:
                stats["degraded"] = "bigram" in _modes
            else:
                _vers = {str(h.get("embed_version") or "") for h in chunk_hits}
                _vers.discard("")
                stats["degraded"] = bool(_vers) and _vers.issubset({"bigram-tf", "bigram"})
        except Exception:
            stats["degraded"] = False

        # 强 BM25 豁免：稀疏精确匹配不依赖 embedding，高分是强证据（口径同 _low_conf_suppressed）
        if any(float(h.get("bm25_score") or 0) >= self._STRONG_BM25 for h in chunk_hits):
            stats["strong_bm25_exempt"] = True
            return chunk_hits, stats

        # 只有"全批低"才进入过滤；否则原样返回（高/中置信说明召回本身健康）
        _all_low = all(str(h.get("confidence_level") or "") == "低" for h in chunk_hits)
        stats["all_low"] = _all_low
        if not _all_low:
            return chunk_hits, stats

        kept = []
        for h in chunk_hits:
            _rel = self._relevance_of(h)
            if _rel >= floor:
                # 打降级标记（**不改变 content 本身**，只加元信息，下游可选择性展示）
                h = dict(h)
                h["degraded"] = stats["degraded"]
                h["relevance"] = round(_rel, 4)
                kept.append(h)
        stats["kept"] = len(kept)
        stats["dropped"] = stats["nominal"] - len(kept)
        return kept, stats

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

        ⚠️ 2026-11-05（conv 990461 实测）：query 可能是**整份粘贴的模型代码**
        （实测 94,538 字符）。此前把它原样拼成 ``%{query}%``，突破 SQLite 的
        ``SQLITE_MAX_LIKE_PATTERN_LENGTH``（默认 50,000 字节）⇒
        ``OperationalError: LIKE or GLOB pattern too complex``；异常被上层转成
        error 事件后子任务零产出 ⇒ 判 failed ⇒ 全批编排失败，用户看到「刚发就失败」。
        治理：**整句 LIKE 只取前 200 字**（实体名不可能更长）；token 路径仍用完整 query
        （实测 105K 字符分词仅 0.057s / 71 token，无需截断）。超长时打可见日志，
        不静默吞掉——静默兜底会把「我写错了」变成「查不到但没报错」。
        """
        _LIKE_MAX = 200
        _like_q = query if len(query) <= _LIKE_MAX else query[:_LIKE_MAX]
        if len(query) > _LIKE_MAX:
            try:
                print("[entity_link] query 超长 %d 字，整句 LIKE 截断为 %d 字（token 路径不受影响）"
                      % (len(query), _LIKE_MAX), flush=True)
            except Exception:
                pass
        # P1：真图数据库优先（分支命名图）
        try:
            from core import config
            if config.as_bool("graph_db", "enabled"):
                from graph_db import get_writer, query_entities_by_name
                _w = get_writer(True, config.get("graph_db", "backend", "pyoxigraph"),
                                config.get("graph_db", "path", ""),
                                config.get("graph_db", "use_memory", False))
                try:
                    hits = query_entities_by_name(_w, _like_q, branches=branches, limit=10)
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
            (f"%{_like_q}%", f"%{_like_q}%", *branches, *doc_params)
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
        # P2（2026-09-07）entity_aliases 消费：mention 原文（别名/缩写/融合前名称）
        # 在实体名 LIKE 未命中时按别名召回——写入侧见 entity_resolver.write_entity_alias。
        # 注意占位符顺序：branch IN 在 ON 子句中先于 WHERE 的 alias LIKE，参数需按 SQL 文本序绑定。
        alias_rows = []
        try:
            a_cond, a_params = ["(a.alias_name LIKE ? OR a.alias_name LIKE ?)"], [f"%{_like_q}%", f"%{_like_q}%"]
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


