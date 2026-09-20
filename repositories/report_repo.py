"""报告中心域 Repository：reports 表（AI 建模报告统一归档/检索/导出）。

报告存储契约与 report_generator 保持一致：
Report = {title, sections:[{heading, body, table?}], summary, report_type}
sections 落库为 JSON 字符串，查询时按需反序列化。
"""
import json

from repositories.base import BaseRepo


class ReportRepo(BaseRepo):
    """报告台账数据访问。"""

    def create_report(self, title: str, report_type: str, summary: str,
                      sections: list, source: str, conversation_id: int,
                      branch: str, project_id: str, status: str,
                      created_by: str) -> int:
        return self.execute(
            """INSERT INTO reports (title, report_type, summary, sections, source,
               conversation_id, branch, project_id, status, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (title, report_type, summary, json.dumps(sections, ensure_ascii=False),
             source, conversation_id, branch, project_id, status, created_by),
        )

    def list_reports(self, report_type: str = "", keyword: str = "") -> list:
        """报告台账（按时间倒序），sections 以 JSON 字符串透传（列表页无需展开）。"""
        sql = "SELECT * FROM reports WHERE 1=1"
        params = []
        if report_type:
            sql += " AND report_type=?"
            params.append(report_type)
        if keyword:
            sql += " AND (title LIKE ? OR summary LIKE ?)"
            like = f"%{keyword}%"
            params += [like, like]
        sql += " ORDER BY created_at DESC, id DESC"
        return self.rows(sql, params)

    def get_report(self, report_id: int) -> dict | None:
        row = self.one("SELECT * FROM reports WHERE id=?", (report_id,))
        if not row:
            return None
        try:
            row["sections"] = json.loads(row.get("sections") or "[]")
        except Exception:
            row["sections"] = []
        return row

    def update_report(self, report_id: int, title: str, report_type: str,
                      summary: str, sections: list, status: str) -> None:
        self.execute(
            """UPDATE reports SET title=?, report_type=?, summary=?, sections=?,
               status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (title, report_type, summary, json.dumps(sections, ensure_ascii=False),
             status, report_id),
        )

    def delete_report(self, report_id: int) -> None:
        self.execute("DELETE FROM reports WHERE id=?", (report_id,))

    def count_reports(self, report_type: str = "") -> int:
        if report_type:
            return self.count("reports", "report_type=?", (report_type,))
        return self.count("reports")
