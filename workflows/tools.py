"""工具注册表（ToolRegistry）+ 真实工具执行器（ToolExecutor）。内置工具 + DB 可配置工具（MCP/Tool/Skill）合并视图。"""
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES  # 基础通用文件操作工具
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES  # 基础通用报告导出工具


class ToolRegistry:
    """工具注册表：内置工具 + DB 可配置工具（MCP/Tool/Skill）合并视图。"""

    BUILTINS = {
        "graph_retrieve": {
            "desc": "知识图谱检索（GraphRAG：实体链接 + 子图遍历 + 向量降级）",
            "handler": "rag.retrieve",
        },
        "conflict_check": {
            "desc": "冲突检测（FR-MG-3：新元素与既有元素属性冲突）",
            "handler": "conflict.detect",
        },
        "impact_analyze": {
            "desc": "变更影响分析（BFS 三层遍历，产出影响图）",
            "handler": "agent._card_impact",
        },
        "validate": {
            "desc": "模型预评审校验（规范性/一致性/合理性）",
            "handler": "agent._card_review",
        },
        "entity_create": {
            "desc": "创建知识实体（人工确认后入库）",
            "handler": "knowledge_repo.create_entity",
        },
        # 基础通用文件操作工具（系统文件读写/增删，定义见 file_tools.py，双链路共用执行器）
        "file_list": {"desc": "列出目录下的文件与子目录（参数 path:str，recursive:bool 可选）", "handler": "file_tools.exec_file_tool"},
        "file_read": {"desc": "读取文本文件内容（UTF-8，参数 path:str，max_chars:int 可选）", "handler": "file_tools.exec_file_tool"},
        "file_write": {"desc": "创建或覆盖写入文本文件（UTF-8 自动建父目录，参数 path:str, content:str；写操作）", "handler": "file_tools.exec_file_tool"},
        "file_append": {"desc": "追加文本到文件末尾（不存在则创建，参数 path:str, content:str；写操作）", "handler": "file_tools.exec_file_tool"},
        "file_mkdir": {"desc": "创建目录（参数 path:str；写操作）", "handler": "file_tools.exec_file_tool"},
        "file_delete": {"desc": "删除文件或空目录（参数 path:str，recursive:bool 可选；destructive 高风险需人工确认）", "handler": "file_tools.exec_file_tool"},
        "report_export": {"desc": "把报告内容导出为指定格式文档并落盘（参数 path/title/content 或 sections/fmt:md|docx|pdf；写操作）", "handler": "report_tools.exec_report_tool"},
    }

    def __init__(self, conn=None):
        self._db_tools: list = []
        self._builtin_db_status: dict = {}  # TR-P2a：DB 中内置工具行的状态（停用/弃用生效）
        if conn is not None:
            self._load_from_db(conn)

    def _load_from_db(self, conn) -> None:
        """从 tools / mcp_servers / skills 三表加载可配置工具。"""
        try:
            for row in conn.execute("SELECT * FROM tools WHERE status='active'").fetchall():
                _schema = row["input_schema"]
                try:
                    _schema = json.loads(_schema or "{}")
                except Exception:
                    _schema = {}
                self._db_tools.append({
                    "name": row["name"],
                    "desc": row["description"],
                    "source": row["source"],
                    "mcp_server_id": row["mcp_server_id"],
                    "type": "tool",
                    "version": row["version"],
                    "side_effect": row["side_effect"],
                    "risk_level": row["risk_level"],
                    "owner": row["owner"],
                    "input_schema": _schema,
                    "health": "ok",
                })
        except Exception:
            pass  # 表结构缺失时降级为仅内置工具
        try:
            # 内置工具全状态跟踪（TR-P2a：停用/弃用的内置工具从注册表消失）
            for row in conn.execute("SELECT name, status FROM tools WHERE source='builtin'").fetchall():
                self._builtin_db_status[row["name"]] = row["status"]
        except Exception:
            pass
        try:
            for row in conn.execute("SELECT * FROM mcp_servers WHERE status='online'").fetchall():
                try:
                    tool_names = json.loads(row["tools"] or "[]")
                except Exception:
                    tool_names = []
                for t in tool_names:
                    self._db_tools.append({
                        "name": t,
                        "desc": f"MCP 工具（{row['name']}）",
                        "source": "mcp",
                        "mcp_server_id": row["id"],
                        "type": "mcp",
                        "endpoint": row["endpoint"],
                        "transport": row["transport"] or "sse",
                        "health": "ok",  # TR-P2b：MCP 在线状态即健康检查结果
                    })
        except Exception:
            pass
        try:
            for row in conn.execute("SELECT * FROM skills WHERE status!='draft'").fetchall():
                self._db_tools.append({
                    "name": row["name"],
                    "desc": row["description"],
                    "source": "skill",
                    "type": "skill",
                    "skill_type": row["skill_type"],
                    "version": row["version"],
                    "content": row["content"] or "",
                })
        except Exception:
            pass

    def list(self) -> list:
        """合并内置 + DB 工具（TR-P2a：DB builtin 行优先，停用/弃用生效；无 DB 行才用 BUILTINS 兜底）。"""
        db_by_name = {t["name"]: t for t in self._db_tools if t["source"] == "builtin"}
        merged = []
        seen = set()
        for name, meta in self.BUILTINS.items():
            if name in db_by_name:
                merged.append(db_by_name[name])  # DB active 行优先（带 schema/副作用/风险/版本）
            elif self._builtin_db_status.get(name, "active") == "active":
                merged.append({
                    "name": name, "desc": meta["desc"], "source": "builtin", "type": "builtin",
                    "version": "v1.0", "side_effect": "read", "risk_level": "low",
                    "input_schema": {}, "health": "ok",
                })  # 未迁库兜底
            seen.add(name)
        for t in self._db_tools:
            if t["source"] != "builtin" and t["name"] not in seen:
                merged.append(t)
        return merged

    def get(self, name: str) -> Optional[dict]:
        for t in self.list():
            if t["name"] == name:
                return t
        return None

    def available_for(self, declared_tools: list) -> list:
        """按 Agent 声明过滤可用工具（未声明的内置工具不暴露）。"""
        available = self.list()
        if not declared_tools:
            return []
        return [t for t in available if t["name"] in declared_tools]


class ToolExecutor:
    """真实工具执行器：内置工具映射到实际实现，MCP 工具走 JSON-RPC。"""

    BUILTINS = ToolRegistry.BUILTINS

    def __init__(self, conn=None):
        self.conn = conn
        self.rag = None  # 延迟实例化 GraphRAG
        self._tool_meta_cache = {}  # D2：工具容错元数据缓存（source/kind/retry_policy/fallback_to）

    def _get_rag(self):
        if self.rag is None:
            from agent import GraphRAG
            self.rag = GraphRAG()
        return self.rag

    # ── D2 工具容错：重试策略 + 降级链 ──
    def _tool_meta(self, name: str) -> dict:
        """读取工具容错元数据（重试策略/降级目标），实例级缓存。"""
        if name in self._tool_meta_cache:
            return self._tool_meta_cache[name]
        meta = {"source": "", "kind": "", "retry_policy": {}, "fallback_to": ""}
        try:
            from database import get_db
            conn = self.conn or get_db()
            try:
                row = conn.execute(
                    "SELECT source, kind, retry_policy, fallback_to FROM tools WHERE name=?",
                    (name,)).fetchone()
                if row:
                    from tool_resilience import parse_policy
                    meta = {
                        "source": row["source"] or "",
                        "kind": row["kind"] or "",
                        "retry_policy": parse_policy(row["retry_policy"] or "{}"),
                        "fallback_to": (row["fallback_to"] or "").strip(),
                    }
            finally:
                if conn is not self.conn:
                    conn.close()
        except Exception:
            pass
        self._tool_meta_cache[name] = meta
        return meta

    def execute(self, name: str, arguments: dict, branch: str = "dev") -> dict:
        """执行单个工具，返回标准化结果。异常不抛出（转为 result 错误）。

        D2 容错：对 MCP/HTTP 集成工具按 retry_policy 做指数退避重试（仅瞬态失败），
        重试耗尽后按 fallback_to 降级切换，结果携带 degraded_from 标记供链路追踪。
        """
        meta = self._tool_meta(name)
        external = (meta["source"] == "mcp") or (meta["kind"] == "http")
        if external and meta["retry_policy"].get("max_retries", 0) > 0:
            from tool_resilience import retry_exec
            r, retried, _did = retry_exec(
                lambda: self._safe_execute(name, arguments or {}, branch), meta["retry_policy"])
            if retried:
                r = {**r, "retried": retried}
        else:
            r = self._safe_execute(name, arguments or {}, branch)
        # 降级链：失败且配置了 fallback_to
        if not r.get("ok") and meta["fallback_to"] and meta["fallback_to"] != name:
            r2 = self._safe_execute(meta["fallback_to"], arguments or {}, branch)
            r2 = {**r2, "degraded_from": name, "fallback_to": meta["fallback_to"]}
            return r2
        return r

    def _safe_execute(self, name: str, arguments: dict, branch: str) -> dict:
        try:
            return self._do_execute(name, arguments, branch)
        except Exception as e:
            return {"ok": False, "result": f"工具执行失败: {str(e)[:200]}"}

    def _do_execute(self, name, arguments, branch) -> dict:
        # ── MCP 工具：查 DB 端点 → JSON-RPC 调用 ──
        if name not in self.BUILTINS:
            # 通用 HTTP 集成工具（tools.kind='http'，配置驱动执行器——适配建模软件等外部系统，零代码接入）
            try:
                from database import get_db
                conn = get_db()
                try:
                    zrow = conn.execute(
                        "SELECT 1 FROM tools WHERE name=? AND kind='http' AND status='active'", (name,)).fetchone()
                finally:
                    conn.close()
                if zrow:
                    from http_tool_executor import exec_http_tool
                    r = exec_http_tool(name, arguments)
                    return {"ok": r.get("ok", True), "result": r.get("result", "{}")[:2000]}
            except Exception as e:
                return {"ok": False, "result": f"HTTP 集成工具调用失败: {str(e)[:200]}"}
            try:
                from database import get_db
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT * FROM mcp_servers WHERE status='online' AND tools LIKE ?",
                        (f"%{name}%",)).fetchone()
                finally:
                    conn.close()
                if row:
                    from mcp_client import MCPClient
                    client = MCPClient(row["endpoint"], row["transport"] or "sse")
                    r = client.call_tool(name, arguments)
                    return {"ok": r.get("ok", True), "result": json.dumps(r, ensure_ascii=False)[:2000]}
            except Exception as e:
                return {"ok": False, "result": f"MCP 调用失败: {str(e)[:200]}"}
            return {"ok": False, "result": f"未知工具: {name}"}

        rag = self._get_rag()
        # ── graph_retrieve：图谱 + 向量双引擎检索 ──
        if name == "graph_retrieve":
            q = str(arguments.get("query", ""))
            r = rag.retrieve(q, branch)
            hits = r.get("chunk_hits") or r.get("entities") or []
            return {"ok": True, "result": f"图谱 {r.get('graph_count',0)} 条 / 向量 {r.get('vector_count',0)} 条："
                                          f"{[h.get('name') or str(h.get('content',''))[:60] for h in hits[:5]]}"}
        # ── impact_analyze：变更影响分析（BFS 三层）──
        if name == "impact_analyze":
            from agent import AgentPipeline
            q = str(arguments.get("query", ""))
            card = AgentPipeline()._card_impact(rag.retrieve(q, branch), branch)
            return {"ok": True, "result": f"直接影响 {card.get('direct_count',0)} 条 / 间接 {card.get('indirect_count',0)} 条"
                                          f"；关键项：{[d.get('name','') for d in (card.get('direct',[]) or [])[:3]]}"}
        # ── validate：模型预评审 ──
        if name == "validate":
            from agent import AgentPipeline
            q = str(arguments.get("query", ""))
            card = AgentPipeline()._card_review(rag.retrieve(q, branch), branch)
            return {"ok": True, "result": f"评分 {card.get('score',0)}，问题 {len(card.get('issues',[]))} 项"}
        # ── conflict_check：新元素与既有元素属性冲突检测 ──
        if name == "conflict_check":
            q = str(arguments.get("query", ""))
            r = rag.retrieve(q, branch)
            ents = r.get("entities") or []
            dup = [e.get("name") for e in ents if e.get("name") == q] or []
            return {"ok": True, "result": f"冲突检测：图谱命中 {len(ents)} 条；{'发现同名冲突: ' + str(dup[:5]) if dup else '无同名冲突（人工确认后入库）'}"}
        # ── entity_create：创建知识实体（模拟图库写入）──
        if name == "entity_create":
            from database import get_db
            eid = arguments.get("id") or "FLOW-" + uuid.uuid4().hex[:8]
            conn = get_db()
            try:
                from repositories.knowledge_repo import KnowledgeRepo
                KnowledgeRepo(conn).create_entity(
                    eid, str(arguments.get("name", "")),
                    str(arguments.get("entity_type", "部件")),
                    json.dumps(arguments.get("properties", {}), ensure_ascii=False), branch)
                conn.commit()
            finally:
                conn.close()
            return {"ok": True, "result": f"实体已创建: {eid}（{arguments.get('name','')} / {arguments.get('entity_type','部件')}）"}
        # ── file_*：基础通用文件操作（系统文件读写/增删，双链路共用 file_tools.exec_file_tool）──
        if name in _FILE_TOOL_NAMES:
            return exec_file_tool(name, arguments or {})
        # ── report_export：报告导出（内容→md/docx/pdf，双链路共用 report_tools.exec_report_tool）──
        if name in _REPORT_TOOL_NAMES:
            return exec_report_tool(name, arguments or {})
        return {"ok": False, "result": f"未知工具: {name}"}


