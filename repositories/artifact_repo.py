"""会话产物域 Repository：artifacts 表（AI 建模会话内 AI 生成内容索引与管理）。

产物仅收录 AI 生成内容（报告/代码/SysML 视图/文档），用户上传附件不进入产物库。
查询按 conversation_id 作用域（当前会话内产物库），不做跨会话检索。
"""
import json

from repositories.base import BaseRepo

# kind → 展示分类（前端 chips 与预览徽章复用）
KIND_LABELS = {
    "report": "报告",
    "code": "代码",
    "sysml": "SysML",
    "document": "文档",
    "other": "其他",
}


class ArtifactRepo(BaseRepo):
    """会话产物数据访问。"""

    # 产物列表仅返回展示所需轻量列——绝不下发每行巨大 preview_content / meta，
    # 否则会话内产物多、内容大时「打开产物下拉 / 列表」会因拉取解析 MB 级 JSON 而卡顿。
    _LIST_COLS = ("id", "conversation_id", "message_id", "kind", "title", "filename",
                  "preview_type", "file_url", "size", "source", "created_by", "created_at")

    def list_artifacts(self, conversation_id: int, kind: str = "", q: str = "") -> list:
        """会话内产物列表（时间倒序），分类/关键词过滤。仅返回列表所需字段 + LIMIT。"""
        sql = ("SELECT " + ", ".join(self._LIST_COLS) + " FROM artifacts WHERE conversation_id=?")
        params = [conversation_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if q:
            sql += " AND (title LIKE ? OR filename LIKE ?)"
            like = f"%{q}%"
            params += [like, like]
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1000"
        rows = self.rows(sql, params)
        for r in rows:
            r["kind_label"] = KIND_LABELS.get(r.get("kind"), "其他")
        return rows

    def stats(self, conversation_id: int) -> dict:
        """分类统计（chips 计数）：全部 + 各 kind 数量。"""
        rows = self.rows(
            "SELECT kind, COUNT(*) n FROM artifacts WHERE conversation_id=? GROUP BY kind",
            (conversation_id,))
        stats = {"total": 0}
        for r in rows:
            stats[r["kind"]] = r["n"]
            stats["total"] += r["n"]
        return stats

    def get_artifact(self, artifact_id: int) -> dict | None:
        row = self.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if not row:
            return None
        row["kind_label"] = KIND_LABELS.get(row.get("kind"), "其他")
        try:
            row["meta"] = json.loads(row.get("meta") or "{}")
        except Exception:
            row["meta"] = {}
        return row

    def create_artifact(self, conversation_id: int, message_id: int, kind: str, title: str,
                        preview_type: str, preview_content: str, meta: dict,
                        filename: str = "", file_path: str = "", file_url: str = "",
                        mime: str = "", size: int = 0, source: str = "conversation",
                        created_by: str = "", project_id: str | None = None) -> int:
        """登记产物。幂等由调用方控制（同 (conversation_id, message_id, kind, title) 先查后插）。

        P1-1（2026-09-28）：`project_id` 缺省时按**会话归属定格**（conversation_project_id），
        显式传入则以传入值为准。空串是合法状态（无工程会话），**不回落到平台默认工程** ——
        回落会让别的标签页切了默认工程后，本会话的产物被算到别人工程下（P0-2 已修的同类问题）。
        """
        if project_id is None:
            from repositories.project_repo import conversation_project_id
            project_id = conversation_project_id(self.conn, conversation_id)
        return self.execute(
            """INSERT INTO artifacts (conversation_id, message_id, project_id, kind, title, filename,
               file_path, file_url, mime, size, preview_type, preview_content, meta, source, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (conversation_id, message_id, project_id or "", kind, title, filename,
             file_path, file_url, mime, size, preview_type, preview_content,
             json.dumps(meta, ensure_ascii=False), source, created_by),
        )

    def exists(self, conversation_id: int, message_id: int, kind: str, title: str) -> bool:
        """幂等判定：同源同型同标题不重复登记。"""
        return self.scalar(
            "SELECT COUNT(*) FROM artifacts WHERE conversation_id=? AND message_id=? AND kind=? AND title=?",
            (conversation_id, message_id, kind, title), default=0) > 0

    def rename_artifact(self, artifact_id: int, title: str) -> None:
        self.execute("UPDATE artifacts SET title=? WHERE id=?", (title, artifact_id))

    def delete_artifact(self, artifact_id: int) -> None:
        """删除产物索引（物理文件保留，由清理任务管理）。"""
        self.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))

    def delete_by_conversation(self, conversation_id: int) -> None:
        """级联清理：删除会话时清理其产物索引。"""
        self.execute("DELETE FROM artifacts WHERE conversation_id=?", (conversation_id,))
