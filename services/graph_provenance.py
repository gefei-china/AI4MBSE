"""节点/边元数据一键追溯 Service（FR-KG-11 落地方案 §4）。

设计要点：
- 5 段聚合视图：节点本体 / 来源文档 / 命中切片 / 版本链路 / 审计轨迹 / 同源节点 / 废弃轨迹
- 单表轻量查询 + 索引利用：chunks 限 3 条、audit 限 20 条、同源节点限 20 条
- 节点 / 边复用同一查询构造器，差异仅在 source/target 节点引用
- 不抛异常：任何子查询失败返回 None，前端按段显示「无数据」
"""
import json
import logging
from typing import Any

from .base import BaseService

logger = logging.getLogger(__name__)


class GraphProvenanceService(BaseService):
    """节点/边数据来源追溯：聚合元数据 + 文档 + 切片 + 版本 + 审计 5 段视图。"""

    CHUNK_LIMIT = 3          # 命中切片展示上限
    AUDIT_LIMIT = 20         # 审计轨迹展示上限
    SIBLING_LIMIT = 20       # 同源节点展示上限

    def entity_provenance(self, entity_id: str, branch: str | None = None) -> dict:
        """节点一键追溯（5 段 + 同源节点 + 废弃轨迹）。"""
        ent = self._safe_one(
            "SELECT * FROM entities WHERE id=?", (entity_id,))
        if not ent:
            return {"error": "entity not found"}
        out: dict[str, Any] = {"entity": ent}

        # 1) 来源文档
        out["source_document"] = self._source_document(ent.get("source_doc"))

        # 2) 命中切片（按 source_doc + 节点名 LIKE 检索）
        out["source_chunks"] = self._source_chunks(ent.get("source_doc"), ent.get("name"))

        # 3) 版本链路（SysML 版本关联）
        out["version_chain"] = self._version_chain(ent.get("sysml_version_id"))

        # 4) 审计轨迹（graph_edit_logs + knowledge_commits 合并）
        out["audit_trail"] = self._audit_trail(entity_id=entity_id)

        # 5) 同源节点（同文档产出的其他节点）
        out["siblings"] = self._siblings(ent.get("source_doc"), exclude_id=entity_id)

        # 6) 废弃轨迹（若已废弃）
        if ent.get("status") == "deprecated":
            out["deprecated_trace"] = {
                "deprecated_at": ent.get("reviewed_at") or "",
                "deprecated_by": ent.get("reviewed_by") or "",
                "current_status": "deprecated",
                "note": "节点级废弃，关联文档可能未废弃",
            }
        else:
            out["deprecated_trace"] = None

        return out

    def relation_provenance(self, relation_id: int) -> dict:
        """边一键追溯（在节点追溯基础上 + source/target 节点引用）。"""
        rel = self._safe_one("SELECT * FROM relations WHERE id=?", (relation_id,))
        if not rel:
            return {"error": "relation not found"}
        out: dict[str, Any] = {"relation": rel}

        # source/target 节点简略引用
        out["source_entity"] = self._entity_ref(rel.get("source_id"))
        out["target_entity"] = self._entity_ref(rel.get("target_id"))

        # 来源文档 + 命中切片 + 版本 + 审计同节点逻辑
        out["source_document"] = self._source_document(rel.get("source_doc"))
        out["source_chunks"] = self._source_chunks(rel.get("source_doc"),
                                                    rel.get("relation_type") or "")
        out["version_chain"] = []
        out["audit_trail"] = self._audit_trail(relation_id=relation_id)
        out["deprecated_trace"] = (
            {"current_status": "deprecated", "note": "边级废弃"}
            if rel.get("status") == "deprecated" else None)
        out["siblings"] = []  # 边无同源概念
        return out

    # ── 子查询（任一失败返回 [] / None，不阻断主流程）──

    def _safe_one(self, sql: str, params: tuple = ()) -> dict | None:
        try:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.warning("provenance _safe_one 失败: %s | %s", sql, e)
            return None

    def _safe_rows(self, sql: str, params: tuple = ()) -> list:
        try:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        except Exception as e:
            logger.warning("provenance _safe_rows 失败: %s | %s", sql, e)
            return []

    def _source_document(self, filename: str | None) -> dict | None:
        if not filename:
            return None
        return self._safe_one(
            """SELECT d.id, d.filename, d.file_type, d.lifecycle_status, d.parse_status,
                      d.uploaded_by, d.created_at, d.deprecated_at, d.archived_at,
                      m.title, m.author, m.version, m.tags
               FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id
               WHERE d.filename=?""", (filename,))

    def _source_chunks(self, filename: str | None, hint: str) -> list:
        """按文件名 + 节点名/关系类型 LIKE 命中切片（≤3 条）。"""
        if not filename or not hint:
            return []
        return self._safe_rows(
            """SELECT dc.id, dc.chunk_index, dc.content, dc.section, dc.bm25_text,
                      dc.lifecycle_status, d.filename
               FROM document_chunks dc JOIN documents d ON d.id=dc.document_id
               WHERE d.filename=? AND dc.content LIKE ?
                     AND dc.lifecycle_status != 'deprecated'
               ORDER BY dc.chunk_index LIMIT ?""",
            (filename, f"%{hint[:60]}%", self.CHUNK_LIMIT))

    def _version_chain(self, version_id: int | None) -> list:
        if not version_id:
            return []
        return self._safe_rows(
            """SELECT id, version, sysml_version, created_by, created_at, message
               FROM sysml_versions WHERE id=? OR sysml_version_id=?
               ORDER BY sysml_version""",
            (version_id, version_id))

    def _audit_trail(self, entity_id: str | None = None,
                     relation_id: int | None = None) -> list:
        """graph_edit_logs + knowledge_commits 合并按时间倒序（≤20 条）。

        节点查询：graph_edit_logs.node_id = entity_id；
        边查询：relation_id 直接走 changes LIKE。
        """
        rows: list = []
        try:
            if entity_id:
                rows.extend([dict(r) for r in self.conn.execute(
                    "SELECT 'edit' AS kind, op AS action, operator, payload, created_at "
                    "FROM graph_edit_logs WHERE node_id=? ORDER BY created_at DESC LIMIT ?",
                    (entity_id, self.AUDIT_LIMIT)).fetchall()])
            elif relation_id:
                rows.extend([dict(r) for r in self.conn.execute(
                    "SELECT 'edit' AS kind, op AS action, operator, payload, created_at "
                    "FROM graph_edit_logs WHERE payload LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f'%relation_id":{relation_id}%', self.AUDIT_LIMIT)).fetchall()])
            rows.extend([dict(r) for r in self.conn.execute(
                "SELECT 'commit' AS kind, kind AS action, created_by AS operator, "
                "       message AS payload, created_at "
                "FROM knowledge_commits ORDER BY id DESC LIMIT ?",
                (self.AUDIT_LIMIT,)).fetchall()])
        except Exception as e:
            logger.warning("provenance _audit_trail 失败: %s", e)
            return []
        rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return rows[:self.AUDIT_LIMIT]

    def _siblings(self, filename: str | None, exclude_id: str) -> list:
        if not filename:
            return []
        return self._safe_rows(
            """SELECT id, name, entity_type, status, created_by, created_at
               FROM entities WHERE source_doc=? AND id != ?
               ORDER BY created_at DESC LIMIT ?""",
            (filename, exclude_id, self.SIBLING_LIMIT))

    def _entity_ref(self, entity_id: str | None) -> dict | None:
        """节点简略引用（id/name/type/status），用于边追溯展示两端。"""
        if not entity_id:
            return None
        return self._safe_one(
            "SELECT id, name, entity_type, status FROM entities WHERE id=?",
            (entity_id,))