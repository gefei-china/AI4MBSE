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
        # KB-S 自愈：白名单与实际存在的文档求交（失效项剔除；全失效则退化，绝不静默 0 命中）
        scope_docs, scope_warn = self._resolve_scope_docs(conn, scope.get("docs") or [])

        # Step 0: 上传文档优先——附件文本关键词命中（首要依据）
        attachment_hits = []
        if attachment_text:
            attachment_hits = self._match_attachment(query, attachment_text)

        # Step 1: Entity linking——整句 LIKE 精确匹配，无命中时用 3-4 字 token 补充（长句 query 友好）
        # 消费侧：默认只查已发布(release)分支；Agent kb_scope.branches 可显式指定其他集合
        kb_branches = (scope.get("branches") or []) or self._release_branches(conn)
        graph_results = self._entity_link(conn, query, kb_branches, source_docs=scope_docs)

        # Step 2: Subgraph traversal - get relations
        if graph_results:
            ids = [e["id"] for e in graph_results]
            placeholders = ",".join(["?"] * len(ids))
            relations = conn.execute(
                f"SELECT r.*, e1.name as source_name, e2.name as target_name FROM relations r "
                f"JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
                f"WHERE (r.source_id IN ({placeholders}) OR r.target_id IN ({placeholders})) AND r.status != 'deprecated'",
                ids + ids
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
            if _rec_ents:
                _have = {e["id"] for e in graph_results}
                _new = [e for e in _rec_ents if e["id"] not in _have]
                if _new:
                    graph_results = graph_results + _new
                    _ids = [e["id"] for e in _new]
                    _ph = ",".join(["?"] * len(_ids))
                    _rel = conn.execute(
                        f"SELECT r.*, e1.name as source_name, e2.name as target_name FROM relations r "
                        f"JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
                        f"WHERE (r.source_id IN ({_ph}) OR r.target_id IN ({_ph})) AND r.status != 'deprecated'",
                        _ids + _ids).fetchall()
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
                              glossary_boost=boost, doc_names=scope_docs or None)
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
                                               doc_names=scope_docs or None, exclude_ai=_exclude_ai)
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
        try:
            self.router.record(conn, query, route, reason, graph_confidence,
                               len(graph_results), len(vector_results), latency_ms, branch)
        except Exception:
            pass  # 统计落库失败不影响主流程

        # P0-3 知识分类分组：设计方法知识（规范约束源）/ 设计资产（增量设计源）
        knowledge = self._group_by_category(conn, graph_results)
        conn.close()
        return {
            "source": route,  # 兼容旧字段：graph | mixed | vector
            "route": route,   # P1: 双引擎路由决策
            "route_reason": reason,
            "confidence": graph_confidence,
            "entities": graph_results,
            "relations": graph_relations,
            "vector_docs": vector_results,
            "chunk_hits": chunk_hits,  # 缺口A：分块级命中预览（含 content 片段）
            "attachment_hits": attachment_hits,   # 上传文档命中片段（首要依据）
            "attachment_used": bool(attachment_hits),
            "graph_count": len(graph_results),
            "vector_count": len(vector_results),
            "glossary_recall": glossary_recall,  # P1: 词典概念（maps_to）→ 图谱实体召回回显
            "latency_ms": latency_ms,
            "knowledge": knowledge,   # P0-3: {"method":[...规范], "assets":[...资产], "uncategorized":[...]}
            "kb_scope_warn": scope_warn,   # KB-S：白名单失效告警（None=正常）；供上层事件/卡片回显
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
    def _resolve_scope_docs(conn, raw_docs) -> tuple:
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
                f"SELECT DISTINCT filename FROM documents WHERE filename IN ({ph})", uniq).fetchall()
            have = {r["filename"] for r in rows}
        except Exception:
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


