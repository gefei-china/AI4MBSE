# -*- coding: utf-8 -*-
"""AI4MBSE 领域能力 MCP **服务端**（P0-1，2026-09-24）。

定位
----
把本平台的三个核心**领域能力**以 MCP 原语对外供给，使任意外部 harness
（Claude Code / Pi / DeepSeek Harness / Codex 等）都能直接消费本平台的 MBSE 能力。
这是本仓「不换底座、让领域能力接口化」路线的第一刀：
**平台负责领域能力与治理，Harness 负责执行循环**，双方靠 MCP 解耦、谁也不绑谁。
（依据：docs/AI平台基础能力-自研vs开源Harness-选型评估-20260924.md §5 P0-1）

与本仓既有 MCP **客户端**（mcp_client.py / mcp_health.py）互为对端：
客户端把外部 server 的工具注入 Agent；本模块把本平台能力供给给外部客户端。
两者可用 `mcp_client.MCPClient` 自测闭环（见 tmp/fix_20260924/verify_mcp_e2e.py）。

协议
----
MCP Streamable HTTP（规范版本 2025-06-18）：JSON-RPC 2.0 over `POST /mcp`。
支持 `initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call` /
`resources/list` / `prompts/list`；**无状态**（不实现 Mcp-Session-Id）。
响应体为 `application/json`（规范允许服务端在 JSON 与 SSE 之间选择；标准客户端两者都收）。

安全
----
- 三个**检索/校验类**工具全部只读：只做检索 / 校验 / 影响推演，不写 entities / relations 正式表。
- `report_event`（2026-11-01 新增）是本网关**唯一**写类工具，但写边界极窄：
  只写 `ev_triggers` / `ev_workitems`，**不执行任何业务动作**（不改图谱、不跑抽取）。
  执行由 `core/events/executor.py` 那一层做，受「准入 + 工单 mode 快照 + 人工确认」
  三重约束 ⇒ 该工具**无法**被用来绕过授权改模型，最多让一件事出现在待办清单上。
- 总开关 `config.get("mcp", "server_enabled", True)`；关闭后 `POST /mcp` 一律 404。
- 每次工具调用落审计（`core.audit.audit`，动作 `mcp_tool_call`），供治理追溯。
- 工具参数做最小 required 校验；异常一律转 `isError:true` 的 tool 结果并**留痕**
  （本仓纪律：兜底 except 必须留痕，静默兜底会让人去追不存在的"偶发"）。
"""
import json
import time
import traceback
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter(tags=["mcp"])

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "ai4mbse-domain"
SERVER_VERSION = "1.0.0"
SERVER_INSTRUCTIONS = (
    "AI4MBSE 领域能力网关（MBSE / SysML v2 建模平台）。提供三项只读能力 + 一项事件上报：\n"
    "· graphrag_search —— GraphRAG 混合检索（图谱实体/关系 + 向量文档片段，带路由决策与置信度）\n"
    "· impact_analysis —— 变更影响传播分析（全路径枚举 + CPM 组合风险 + 处置建议）\n"
    "· ontology_validate —— 本体一致性校验（12 条规则：循环继承/悬空父类/孤立类/dom-range/重名等）\n"
    "· report_event —— 上报环境事件（模型变更/校验失败/文档入库）。"
    "**它只落事件台账与工单，不执行任何业务动作**；执行由平台观察期/授权机制决定。\n"
    "前三个工具只读，不修改模型资产。branch 参数用于选择模型空间。"
)

# ── 工具目录（tools/list 的单一真源） ────────────────────────────────────────
TOOLS = [
    {
        "name": "graphrag_search",
        "title": "GraphRAG 领域检索",
        "description": (
            "在 MBSE 模型图谱 + 知识库上做混合检索。返回命中的实体（含类型/分支）、关系、"
            "文档分块片段，以及检索路由决策（graph/mixed/vector）与置信度。"
            "适用：查某元素/概念在模型里的定义与关联、找设计规范约束、核对术语表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题或关键词（中文优先）"},
                "branch": {"type": "string", "description": "分支名（模型空间），默认 dev"},
                "max_items": {"type": "integer", "description": "每类结果最多返回条数，默认 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "impact_analysis",
        "title": "变更影响分析",
        "description": (
            "对指定元素做变更影响传播分析：以「已发布分支」的实体/关系网络为基线，"
            "按变更类型加权传播，枚举全部无环路径并用 CPM 概率合并公式算出组合风险，"
            "返回受影响元素清单（按层级/风险）、传播路径、风险矩阵与处置建议。"
            "适用：改一个参数/接口/结构之前，回答「会波及谁」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_name": {"type": "string", "description": "变更源元素名（须存在于已发布分支 released 实体中）"},
                "depth": {"type": "integer", "description": "传播深度上限 1-4，默认 3"},
                "direction": {"type": "string", "enum": ["both", "up", "down"], "description": "传播方向，默认 both"},
                "change_type": {
                    "type": "string",
                    "enum": ["attribute", "value", "interface", "rename", "delete"],
                    "description": "变更类型（决定传播权重矩阵），默认 attribute",
                },
            },
            "required": ["change_name"],
        },
    },
    {
        "name": "ontology_validate",
        "title": "本体一致性校验",
        "description": (
            "对本体（ontology）做一致性体检，返回结构化问题清单：循环继承 / 悬空父类 / 孤立类 / "
            "关系缺 dom-range / dom-range 悬空 / 重名 / 属性缺适用类型 / 属性适用类型悬空 / "
            "一侧多声明 / 声明与存量边不一致 / 实例类型未注册 / 存量实例不满足约束。"
            "每条问题带 code + 中文 label + 严重度（high/warn/low/info）。"
            "适用：发布前门禁、模型质量巡检。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instances": {
                    "type": "boolean",
                    "description": "是否附带逐实例校验（xsd/白名单/端点/基数，较慢），默认 false",
                },
                "scope": {"type": "string", "description": "只校验指定类型名（配合 instances 使用，缩小扫描面）"},
            },
            "required": [],
        },
    },
    # ── 写类工具（2026-11-01，Super-agent 事件驱动）：外部 harness 上报环境事件 ──
    # ⚠️ 这是本网关**唯一**的写类工具，但它的"写"边界极窄：
    #    只写 ev_triggers（事件台账）+ ev_workitems（工单），**不执行任何业务动作**。
    #    真正的执行由 `core/events/executor.py` 那一层做，且受 mode（observe/execute）
    #    与来源准入双重约束 ⇒ 这个工具**不能**绕过授权去改模型。
    {
        "name": "report_event",
        "title": "上报环境事件",
        "description": (
            "把外部系统发生的事件（模型变更 / 校验失败 / 文档入库 / 需求变更）上报给 MBSE 平台，"
            "平台按来源准入规则决定是否建工单。**本工具不执行任何业务动作**："
            "它只落事件台账与工单，实际执行由平台的观察期/授权机制决定。\n"
            "典型用法：智源/Super-agent 在建模会话里发现模型变更或校验失败时调用本工具，"
            "让平台侧「不需要人再发一句『帮我做影响分析』」也能接住这件事。\n"
            "幂等：同一 source + external_id（+ version）重复上报只产生一条事件。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "事件来源种类。已注册：zhiyuan(智源模型变更) / sysml_check(校验失败) / documents(文档入库)",
                },
                "external_id": {
                    "type": "string",
                    "description": "外部稳定业务 ID（分支ID/文档ID/代码哈希）。**必须稳定**——平台按它去重，不要用时间戳或随机串",
                },
                "thread_scope": {
                    "type": "string",
                    "description": "归属线程（同一业务对象用同一个值：分支ID/文档ID/包名）。省略时取 external_id",
                },
                "actor": {"type": "string", "description": "发起者 ID（人类用户名或 system）"},
                "actor_type": {
                    "type": "string",
                    "enum": ["human", "bot", "system"],
                    "description": "发起者类型。system=系统既成事实（不需白名单，但仍只建单不执行）",
                },
                "addressed": {
                    "type": "string",
                    "enum": ["mention", "direct"],
                    "description": "是否明确请求处理（人类发起时建议填 mention）",
                },
                "text": {"type": "string", "description": "事件简述（人类可读；**不参与授权判定**）"},
                "payload": {"type": "object", "description": "结构化附加数据（如 code/n_hard/model 等）"},
                "version": {"type": "string", "description": "版本号（同一 external_id 的修订，如评论 updated_at）"},
            },
            "required": ["kind", "external_id"],
        },
    },
]


class ToolArgError(ValueError):
    """工具入参校验失败（转 isError 的 tool 结果，而非 JSON-RPC 协议错误）。"""


# ═══════════════ 工具实现（全部只读） ═══════════════

def _tool_graphrag(args: dict) -> dict:
    """GraphRAG 混合检索——复用 `agent.rag.GraphRAG.retrieve`（与 AI 会话同一实现）。"""
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolArgError("query 不能为空")
    branch = (args.get("branch") or "dev").strip() or "dev"
    max_items = int(args.get("max_items") or 8)
    max_items = max(1, min(max_items, 50))

    from agent.rag import GraphRAG
    res = GraphRAG().retrieve(query, branch)

    entities = (res.get("entities") or [])[:max_items]
    relations = (res.get("relations") or [])[:max_items]
    chunks = (res.get("chunk_hits") or [])[:max_items]
    return {
        "query": query,
        "branch": branch,
        "route": res.get("route") or res.get("source"),
        "route_reason": res.get("route_reason"),
        "confidence": res.get("confidence"),
        "latency_ms": res.get("latency_ms"),
        "counts": {
            "entities": res.get("graph_count", len(res.get("entities") or [])),
            "relations": len(res.get("relations") or []),
            "vector_docs": res.get("vector_count", len(res.get("vector_docs") or [])),
        },
        "entities": [
            {k: e.get(k) for k in ("id", "name", "entity_type", "branch", "description") if e.get(k) is not None}
            for e in entities if isinstance(e, dict)
        ],
        "relations": [
            {k: r.get(k) for k in ("source_id", "target_id", "src_name", "tgt_name", "relation_type", "branch")
             if r.get(k) is not None}
            for r in relations if isinstance(r, dict)
        ],
        "chunks": [
            {"doc": c.get("doc_name") or c.get("source_doc"), "text": (c.get("content") or "")[:400]}
            for c in chunks if isinstance(c, dict)
        ],
        "glossary_recall": res.get("glossary_recall") or [],
        "kb_scope_warn": res.get("kb_scope_warn"),
    }


def _tool_impact(args: dict) -> dict:
    """变更影响分析——复用 `services.impact_engine` 的既有引擎（与影响分析卡同口径）。"""
    change_name = (args.get("change_name") or "").strip()
    if not change_name:
        raise ToolArgError("change_name 不能为空")
    depth = max(1, min(int(args.get("depth") or 3), 4))
    direction = args.get("direction") or "both"
    if direction not in ("both", "up", "down"):
        raise ToolArgError("direction 必须为 both / up / down")
    change_type = args.get("change_type") or "attribute"

    from database import get_db
    from services.impact_engine import (
        CHANGE_MATRIX, analyze_graph_v2, build_baseline, decide_recommendation,
    )
    if change_type not in CHANGE_MATRIX:
        raise ToolArgError("change_type 必须属于 " + ", ".join(sorted(CHANGE_MATRIX)))

    conn = get_db()
    try:
        base = build_baseline(conn, change_name, depth=depth, direction=direction)
    finally:
        conn.close()
    if not base.get("ok"):
        # 变更源解析失败（歧义/不存在）——如实回传候选，供调用方改名重试
        return {
            "ok": False, "change_name": change_name,
            "code": base.get("code"), "reason": base.get("reason"),
            "candidates": base.get("candidates") or [],
        }

    graph, source = base.get("graph") or {}, base.get("source") or {}
    card = analyze_graph_v2(graph.get("nodes") or [], graph.get("edges") or [], source,
                            depth=depth, direction=direction, change_type=change_type)
    try:
        recommendation = decide_recommendation(card)
    except Exception:  # noqa: BLE001 —— 建议属增强项，失败不影响主结论；但必须留痕
        traceback.print_exc()
        recommendation = {"error": "decide_recommendation 失败（详见服务端日志）"}

    nodes = card.get("impact_nodes") or []
    top = max(1, min(len(nodes), 10))
    return {
        "ok": True,
        "change_name": change_name,
        "source": {k: source.get(k) for k in ("id", "name", "entity_type") if source.get(k) is not None},
        "depth": depth,
        "direction": direction,
        "change_type": change_type,
        "counts": card.get("counts") or {},
        "levels": card.get("levels") or {},
        "impact_nodes": [
            {k: n.get(k) for k in ("id", "name", "entity_type", "level", "risk", "score", "hops")
             if n.get(k) is not None}
            for n in nodes[:top] if isinstance(n, dict)
        ],
        "path_list": (card.get("path_list") or [])[:8],
        "risk_matrix": card.get("risk_matrix") or [],
        "recommendation": recommendation,
    }


def _tool_ontology(args: dict) -> dict:
    """本体一致性校验——复用 `routers.knowledge_parts.shared._ontology_check`（与体检端点同一实现）。"""
    instances = bool(args.get("instances"))
    scope = (args.get("scope") or "").strip() or None

    from database import get_db
    from routers.knowledge_parts.shared import _ontology_check
    conn = get_db()
    try:
        rep = _ontology_check(conn, instances=instances, scope=scope)
    finally:
        conn.close()
    return rep


def _tool_report_event(args: dict) -> dict:
    """**上报环境事件**（本网关唯一写类工具）—— 让外部 harness 走标准 MCP 驱动事件底座。

    ━━━ 为什么这个工具必须存在（而不是让外部去裸调 HTTP）━━━
    原文《Super-agent》的核心主张：Agent 的输入不该只有"人逐条键入的提示词"，
    而是**环境中的半结构化事件**。
    本平台已经是 MCP server，外部 harness（智源 / DeepSeek Harness / Claude Code）
    通过 MCP 消费本平台能力 ⇒ 事件的**入口也应该在 MCP 上**，
    否则外部要同时维护两条通道（MCP 调工具 + HTTP 报事件），很容易只做了前者。

    ━━━ 写边界（这个工具**能做什么、不能做什么**）━━━
    能：写 ev_triggers（事件台账）、可能写 ev_workitems（工单）。
    不能：**不执行任何业务动作**（不改 entities/relations、不写图谱、不跑抽取）。
    ⇒ 真正的执行在 `core/events/executor.py`，受三重约束：
       ① 准入（来源白名单 / scope / 系统事件豁免）
       ② 工单 mode 快照（observe 一律拒绝执行）
       ③ 人工确认（execute-pending 由人触发，或来源显式切 execute）
    ⇒ 所以这个工具**无法**被用来"绕过授权改模型"——它最多让一件事**出现在待办清单上**。

    ━━━ 幂等 ━━━
    同一 (kind, external_id, version) 重复上报 ⇒ 同一条 Trigger ⇒ 不重复建工单。
    重复时返回 disposition='duplicate' 并带 first_disposition（首次处置结论），
    让人能区分"本来就该 duplicate"与"admitted 后被重投"。
    """
    kind = (args.get("kind") or "").strip()
    if not kind:
        raise ToolArgError("kind 不能为空")
    external_id = str(args.get("external_id") or "").strip()
    if not external_id:
        raise ToolArgError("external_id 不能为空（且必须稳定，平台按它去重）")

    payload = args.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise ToolArgError("payload 必须是对象")

    # 来源必须已注册（未注册 ⇒ 不静默吞，明确报错并列出可用来源）
    from core.events.sources import get_registry
    reg = get_registry()
    if reg.get(kind) is None:
        raise ToolArgError("未知事件来源 kind=%s；可用：%s"
                           % (kind, ", ".join(reg.kinds()) or "(无已注册来源)"))

    from database import get_db
    from repositories.event_repo import EventRepo
    from services.event_service import EventService

    conn = get_db()
    try:
        svc = EventService(EventRepo(conn), reg)
        # ⚠️ **payload 必须平铺到顶层**（2026-11-01 端到端实测修掉的真实缺陷）：
        #    各来源的 `to_trigger` 约定读**顶层字段**（如 sysml_check 读 raw["code"]、
        #    documents 读 raw["filename"]），而 MCP 的入参天然是 `arguments.payload.xxx`。
        #    只传嵌套 payload ⇒ sysml_check 判定 `empty_code` ⇒ **事件被前置校验拦下**，
        #    症状是"上报成功了但没建单"，看起来像"准入没配好"，实则是字段层级不匹配。
        #    ⇒ 平铺（顶层优先，payload 兜底），并保留 payload 原样供后续 handler 使用。
        _flat = dict(payload or {})
        for _k in ("external_id", "thread_scope", "actor", "actor_type", "addressed",
                   "text", "version"):
            if args.get(_k) is not None:
                _flat[_k] = args.get(_k)
        _flat["payload"] = payload or {}
        res = svc.receive(kind, _flat)
    finally:
        conn.close()

    # 回给调用方的语义要**恰好**（不夸大）：明确说"只建单，未执行"
    if res.get("disposition") == "admitted":
        res["note"] = ("事件已受理并建工单；**未执行任何动作**。"
                       "执行取决于来源模式（observe 只等待人工，execute 才自动跑）")
    elif res.get("disposition") == "filtered":
        res["note"] = "事件已记录但未建工单（见 reason：来源范围/白名单/数据不完备）"
    elif res.get("disposition") == "duplicate":
        res["note"] = "同一事件此前已上报（幂等），未重复建单"
    return res


_HANDLERS = {
    "graphrag_search": _tool_graphrag,
    "impact_analysis": _tool_impact,
    "ontology_validate": _tool_ontology,
    "report_event": _tool_report_event,
}


# ═══════════════ JSON-RPC / MCP 协议层 ═══════════════

def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, message: str, data=None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": e}


def _text_result(payload, is_error: bool = False) -> dict:
    """工具结果：content 为 JSON 文本（不声明 outputSchema 就不带 structuredContent，
    避免严格客户端拿 outputSchema 校验 structuredContent 时误判）。"""
    txt = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": txt}], "isError": bool(is_error)}


def _audit_call(tool: str, is_error: bool, latency_ms: int, detail: str = "") -> None:
    """每次外部调用落审计（治理追溯）；失败不阻断工具返回，但留痕。

    P0-a（2026-10-24）：同时落 tool_call_logs 统一调用日志。此前平台作为 MCP
    Server 被外部调用**只写 audit_logs**（不进能力度量）⇒「外部流量用了哪些
    能力」在能力中心完全不可见。capability_id 反查 tools 表（命中 tool:{name}），
    不命中留空——SERVER_NAME（ai4mbse-domain）不是 mcp_servers 表里的行，
    不造假映射。
    """
    try:
        from core.audit import audit
        audit("mcp-client", "mcp_tool_call",
              f"[MCP供给] {tool} {'失败' if is_error else '成功'} {latency_ms}ms {detail}".strip(),
              result="fail" if is_error else "success")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        from database import db_conn
        # ★ P0-3（2026-10-27）：外部 harness 的入站调用**没有对话轮次**
        #   ⇒ 用 request_id 作 trace（一次外部请求 = 一条链），这是如实口径。
        #   这样"MCP 供给出去的调用"也能和它自己的其它日志串起来。
        try:
            from core.audit import current_trace_id as _ctid_gw
            _tid_gw = _ctid_gw(fallback_request=True)
        except Exception:
            _tid_gw = ""
        with db_conn() as _c:
            _hit = _c.execute("SELECT 1 FROM tools WHERE name=?", (tool,)).fetchone()
            # S0（2026-10-28）探针标记（core/probe_mark）：外部 harness 会拿假工具探活
            try:
                from core.probe_mark import is_probe_tool as _ipt_gw
                _probe_gw = _ipt_gw(tool)
            except Exception:
                _probe_gw = 0
            _c.execute(
                "INSERT INTO tool_call_logs (intent, agent_name, tool_name, tool_type, call_kind, "
                "arguments, result, ok, latency_ms, capability_id, trace_id, is_probe) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("mcp_gateway", "", tool, "mcp", "tool", "{}",
                 detail[:2000], 0 if is_error else 1, latency_ms,
                 ("tool:%s" % tool) if _hit else "", _tid_gw, _probe_gw))
    except Exception:  # noqa: BLE001
        pass  # 度量落库失败不影响审计与工具返回


def _dispatch(msg: dict):
    """处理单条 JSON-RPC 消息 → (响应体 dict 或 None, HTTP 状态码)。

    通知（无 id）返回 (None, 202)——规范要求 202 且无体。
    """
    if not isinstance(msg, dict):
        return _err(None, -32600, "Invalid Request: 消息必须是 JSON 对象"), 200

    is_notification = "id" not in msg
    rid = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}

    # ── 协议方法 ──
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        }), 200

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None, 202

    if method == "ping":
        return _ok(rid, {}), 200

    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS}), 200

    if method == "resources/list":
        return _ok(rid, {"resources": []}), 200

    if method == "prompts/list":
        return _ok(rid, {"prompts": []}), 200

    # ── 工具调用 ──
    if method == "tools/call":
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        if name not in _HANDLERS:
            return _err(rid, -32602, f"未知工具: {name}",
                        {"available": sorted(_HANDLERS)}), 200
        if not isinstance(arguments, dict):
            return _ok(rid, _text_result(
                {"error": "arguments 必须是对象"}, is_error=True)), 200

        t0 = time.time()
        try:
            out = _HANDLERS[name](arguments)
            latency = int((time.time() - t0) * 1000)
            _audit_call(name, False, latency, f"args={json.dumps(arguments, ensure_ascii=False)[:160]}")
            if isinstance(out, dict) and out.get("ok") is False:
                # 领域层的"未找到/歧义"——如实回传，但不标 isError（这是合法结论，不是调用失败）
                return _ok(rid, _text_result(out)), 200
            return _ok(rid, _text_result(out)), 200
        except ToolArgError as e:
            latency = int((time.time() - t0) * 1000)
            _audit_call(name, True, latency, f"arg_error={e}")
            return _ok(rid, _text_result({"error": str(e)}, is_error=True)), 200
        except Exception as e:  # noqa: BLE001
            latency = int((time.time() - t0) * 1000)
            traceback.print_exc()          # 留痕：绝不静默
            _audit_call(name, True, latency, f"exc={type(e).__name__}: {str(e)[:160]}")
            return _ok(rid, _text_result(
                {"error": f"{type(e).__name__}: {e}", "tool": name}, is_error=True)), 200

    # 未知通知（notifications/*）→ 202；未知请求 → 方法未找到
    if is_notification or method.startswith("notifications/"):
        return None, 202
    return _err(rid, -32601, f"Method not found: {method}"), 200


def _enabled() -> bool:
    try:
        from core import config
        return bool(config.as_bool("mcp", "server_enabled", True))
    except Exception:  # noqa: BLE001 —— 配置读取失败按"开"处理（能力可用优先），但要留痕
        traceback.print_exc()
        return True


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP Streamable HTTP 入口（JSON-RPC 2.0）。"""
    if not _enabled():
        return JSONResponse({"error": "MCP 服务端已关闭（config: mcp.server_enabled=false）"}, 404)
    try:
        raw = await request.body()
        msg = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(_err(None, -32700, f"Parse error: {e}"), 200)

    body, status = _dispatch(msg)
    if body is None:
        return Response(status_code=status)
    # 会话标识（规范里为 MAY：服务端可分配）。实测官方 Python SDK 1.x 在**没有**该头时，
    # 第三次请求（tools/call）会报 "Session terminated"；带上后 SDK 侧恢复正常。
    # 本实现仍是无状态（不记录、不校验该 id）——仅为兼容标准客户端。
    headers = {}
    try:
        if isinstance(body, dict) and "protocolVersion" in (body.get("result") or {}):
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    # 响应形态：规范允许服务端在 JSON 与 SSE 之间选择。默认返回 JSON；
    # 但当客户端在 Accept 里声明了 text/event-stream 时改用 SSE —— 实测官方 Python SDK
    # 的 streamable-http 客户端在 JSON 响应下第三次请求会报 "Session terminated"，
    # 改 SSE 后恢复正常（SDK 侧对「单次 JSON 响应」的处理有缺陷）。
    if "text/event-stream" in (request.headers.get("accept") or "").lower():
        payload = "event: message\ndata: " + json.dumps(body, ensure_ascii=False) + "\n\n"
        return Response(payload, status_code=status, media_type="text/event-stream", headers=headers)
    return JSONResponse(body, status_code=status, headers=headers)


@router.get("/mcp")
def mcp_get_not_supported():
    """本服务端为无状态 JSON 响应模式，不提供 server→client 的 SSE 长流。"""
    return JSONResponse(
        {"error": "本 MCP 服务端仅支持 POST（无状态 JSON 响应）；不支持 GET SSE 长流"},
        405, headers={"Allow": "POST"})


@router.delete("/mcp")
def mcp_delete_not_supported():
    """无状态实现无会话可终止。"""
    return JSONResponse({"error": "无状态实现，无 Mcp-Session-Id 可终止"}, 405,
                        headers={"Allow": "POST"})
