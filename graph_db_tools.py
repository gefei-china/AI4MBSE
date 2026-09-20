# -*- coding: utf-8 -*-
"""图数据库只读查询工具（P2 图查询消费侧打通，2026-09-01）。

- graph_db_query : SPARQL 只读查询（仅 SELECT，防注入；结果截断防打爆 LLM context）
- graph_db_stats : 图库规模统计（三元组/命名图/后端）

安全：写操作（INSERT/UPDATE/DELETE/CLEAR/LOAD）一律拒绝——图库数据由治理链
（审核→物化消费链）写入，AI 只读消费（对齐星网 FR-KG-2 已评审/未评审分区 + 人在回路）。
"""
import logging
import re

logger = logging.getLogger(__name__)

MAX_ROWS = 20   # 单次返回行数上限（防 context 打爆）
MAX_QUERY = 400  # SPARQL 长度上限（防异常输入）


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    """工具执行入口（pipeline._exec_tool_call 按 graph_db_ 前缀路由）。"""
    if name == "graph_db_query":
        return _query(arguments or {})
    if name == "graph_db_nlquery":
        return _nlquery(arguments or {})
    if name == "graph_db_stats":
        return _stats()
    return {"error": f"未知图数据库工具: {name}", "ok": False}


def _writer():
    from core import config
    from graph_db import get_writer
    return get_writer(
        enabled=True,
        backend=config.get("graph_db", "backend", "pyoxigraph"),
        store_path=config.get("graph_db", "path", ""),
        use_memory=config.get("graph_db", "use_memory", False),
        endpoint=config.get("graph_db", "endpoint", ""),
        username=config.get("graph_db", "username", ""),
        password=config.get("graph_db", "password", ""),
        neo4j_uri=config.get("graph_db", "neo4j_uri", ""),
        neo4j_user=config.get("graph_db", "neo4j_user", ""),
        neo4j_password=config.get("graph_db", "neo4j_password", ""),
    )


def _query(args: dict) -> dict:
    from core import config
    if not config.as_bool("graph_db", "enabled"):
        return {"ok": False, "error": "图数据库未启用（graph_db.enabled=false）",
                "hint": "图谱问答暂不可用，可基于知识库问答或提示管理员启用图数据库"}
    q = str(args.get("query") or args.get("sparql") or "").strip()
    if not q:
        return {"ok": False, "error": "query 不能为空",
                "example": 'SELECT ?s ?t WHERE { GRAPH ?g { ?s <urn:mbse:type> ?t } } LIMIT 5'}
    if len(q) > MAX_QUERY:
        return {"ok": False, "error": f"SPARQL 过长（> {MAX_QUERY} 字符）"}
    if not q.upper().startswith("SELECT"):
        return {"ok": False, "error": "仅允许 SELECT 查询（图库为只读消费）"}
    limit = max(1, min(int(args.get("limit") or MAX_ROWS), MAX_ROWS))
    writer = _writer()
    try:
        rows = writer.query_sparql(q, limit=limit)
        return {"ok": True, "count": len(rows), "rows": rows[:MAX_ROWS],
                "note": "来源：设计知识库（图数据库）" if rows else "图谱无命中"}
    except Exception as e:
        logger.warning("graph_db_tools._query 失败: %s", e)
        return {"ok": False, "error": f"SPARQL 执行失败: {e}"}
    finally:
        writer.close()


def _stats() -> dict:
    from core import config
    if not config.as_bool("graph_db", "enabled"):
        return {"ok": False, "error": "图数据库未启用（graph_db.enabled=false）"}
    writer = _writer()
    try:
        return {"ok": True, "stats": writer.stats()}
    finally:
        writer.close()


# ── 图问答：自然语言 → SPARQL 自动翻译（2026-09-01 遗留①收官）──────
_NL_SYSTEM_PROMPT = (
    "你是 MBSE 设计知识库（图数据库）的 SPARQL 翻译器，把用户自然语言问题翻译成 SPARQL SELECT 查询。\n"
    "硬约束：\n"
    "1. 只输出 SPARQL 查询本身（可用 ```sparql 代码块包裹），不要任何解释\n"
    "2. 只允许 SELECT（只读），禁止 INSERT/UPDATE/DELETE/DROP/CLEAR/LOAD 等写操作\n"
    "3. URI 约定：实体 <urn:mbse:ent:名称:id>；类型谓词 <urn:mbse:type>；"
    "关系谓词 <urn:mbse:rel:类型>；属性谓词 <urn:mbse:prop:名>；命名图 <urn:mbse:graph:分支>（默认 release/dev）\n"
    "4. 中文名称不能直接写在 <...> 里（SPARQL IRI 语法不支持非 ASCII），用已提供的实体 URI，"
    "或 FILTER(contains(str(?s), \"名称\")) 方式匹配\n"
    "5. 已匹配候选实体 URI：{entities}——优先直接使用这些精确 URI\n"
    "6. 结果变量用可读命名（如 ?name ?type ?relation）\n"
    "示例：问「转发器有哪些关系」→ SELECT ?p ?o WHERE {{ GRAPH ?g {{ "
    "<urn:mbse:ent:转发器:ENT-002> ?p ?o . FILTER(strstarts(str(?p), \"urn:mbse:rel:\")) }} }} LIMIT 10"
)


def _match_entities(nl_query: str, limit: int = 3) -> list:
    """NL 查询 → 图谱候选实体预匹配（注入 LLM 提示，提高翻译命中率）。"""
    try:
        from core import config
        if not config.as_bool("graph_db", "enabled"):
            return []
        from graph_db import get_writer, query_entities_by_name
        w = get_writer(True, config.get("graph_db", "backend", "pyoxigraph"),
                       config.get("graph_db", "path", ""),
                       config.get("graph_db", "use_memory", False),
                       endpoint=config.get("graph_db", "endpoint", ""),
                       username=config.get("graph_db", "username", ""),
                       password=config.get("graph_db", "password", ""))
        try:
            hits = query_entities_by_name(w, nl_query, branches=None, limit=limit * 2)
        finally:
            w.close()
        # 只保留与查询词有字符重叠的（防无关实体误注入）
        qchars = set(nl_query)
        picked = [h for h in hits if qchars & set(h["name"])][:limit]
        return [{"name": h["name"], "uri": h["uri"], "branch": h["branch"],
                 "type": h.get("entity_type", "")} for h in picked]
    except Exception:
        return []


def _nl_to_sparql(nl_query: str, entities: list) -> str:
    """LLM 翻译 NL → SPARQL。只做清理提取（代码块/前缀），不判定 SELECT（交 _validate_sparql）。"""
    from llm import llm_client
    hint = "; ".join(f"{e['name']} → <{e['uri']}>" for e in (entities or [])) or "无"
    resp = llm_client.chat([
        {"role": "system", "content": _NL_SYSTEM_PROMPT.format(entities=hint)},
        {"role": "user", "content": nl_query},
    ])
    text = str((resp or {}).get("content", "") or "")
    if not text.strip():
        return ""
    m = re.search(r"```sparql\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    idx = text.upper().find("SELECT")
    if idx > 0:
        text = text[idx:]
    text = text.strip().rstrip().rstrip(";")
    return text if text else ""


_WRITE_KW = ("INSERT", "UPDATE", "DELETE", "DROP", "CLEAR", "LOAD",
             "CREATE", "MOVE", "COPY", "ADD", "WITH")


def _validate_sparql(sparql: str) -> tuple:
    """只读校验：写操作关键字（剥离字面量后词边界）→ SELECT 开头。

    写关键字优先报（LLM 输出 DELETE/INSERT 时给出明确「写操作」而非笼统「非 SELECT」）；
    引号内的 insert 等字面量不影响判断。
    """
    s = (sparql or "").strip()
    if not s:
        return False, "空查询"
    stripped = re.sub(r'"[^"]*"|\'[^\']*\'', '""', s.upper())
    for kw in _WRITE_KW:
        if re.search(rf"\b{kw}\b", stripped):
            return False, f"检测到写操作关键字 {kw}（图库只读，写请走治理链审核）"
    if not s.upper().startswith("SELECT"):
        return False, "非 SELECT 查询（图库只读，仅允许 SELECT）"
    return True, ""


def _nlquery(args: dict) -> dict:
    """自然语言图问答：NL → SPARQL（LLM 翻译 + 实体预匹配）→ 校验 → 执行。"""
    from core import config
    if not config.as_bool("graph_db", "enabled"):
        return {"ok": False, "error": "图数据库未启用（graph_db.enabled=false）",
                "hint": "图谱问答暂不可用，可基于知识库问答或提示管理员启用图数据库"}
    nl = str(args.get("query") or args.get("question") or "").strip()
    if not nl:
        return {"ok": False, "error": "问题不能为空",
                "example": "转发器有哪些关系？哪些组件依赖电源分系统？"}
    entities = _match_entities(nl)
    try:
        sparql = _nl_to_sparql(nl, entities)
    except Exception as e:
        logger.warning("graph_db_tools._nlquery 翻译失败: %s", e)
        return {"ok": False, "error": f"自然语言翻译 SPARQL 失败: {e}"}
    if not sparql:
        return {"ok": False, "error": "LLM 未能生成合法 SPARQL",
                "suggestion": "可改用 graph_db_query 手写 SELECT"}
    ok, err = _validate_sparql(sparql)
    if not ok:
        return {"ok": False, "error": err, "sparql": sparql}
    writer = _writer()
    try:
        rows = writer.query_sparql(sparql, limit=MAX_ROWS)
        return {"ok": True, "count": len(rows), "rows": rows,
                "sparql": sparql, "entities": entities,
                "note": "来源：设计知识库（图数据库）· 自然语言自动翻译" if rows else "图谱无命中"}
    except Exception as e:
        logger.warning("graph_db_tools._nlquery 执行失败: %s", e)
        return {"ok": False, "error": f"SPARQL 执行失败: {e}", "sparql": sparql}
    finally:
        writer.close()
