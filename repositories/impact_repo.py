"""变更影响分析域 Repository：impact_analyses 表（FR-CIA-3 每次分析结果快照，可追溯）。

每条分析（含失败引导）落一条记录，完整保存参数与结果 JSON 快照；
报告中心与分析记录通过 report_id 双向关联。
"""
import json

from repositories.base import BaseRepo


class ImpactRepo(BaseRepo):
    """变更影响分析数据访问。"""

    def list_analyses(self, q: str = "", limit: int = 50, offset: int = 0) -> list:
        """分析记录列表（时间倒序），关键词过滤（变更源/标题）。"""
        sql = "SELECT id, title, change_source, params, status, error_code, conversation_id, message_id, report_id, created_by, created_at FROM impact_analyses"
        params = []
        if q:
            sql += " WHERE title LIKE ? OR change_source LIKE ?"
            like = f"%{q}%"
            params += [like, like]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = self.rows(sql, params)
        for r in rows:
            r["params"] = json.loads(r.get("params") or "{}")
        return rows

    def get_analysis(self, analysis_id: int) -> dict | None:
        """分析详情（含完整结果快照，供拓扑图/矩阵重绘与追溯）。"""
        row = self.one("SELECT * FROM impact_analyses WHERE id=?", (analysis_id,))
        if not row:
            return None
        for key in ("params", "result"):
            try:
                row[key] = json.loads(row.get(key) or "{}")
            except Exception:
                row[key] = {}
        return row

    def create_analysis(self, title: str = "", change_source: str = "", params: dict | None = None,
                        result: dict | None = None, status: str = "ok", error_code: str = "",
                        conversation_id: int = 0, message_id: int = 0, report_id: int = 0,
                        created_by: str = "") -> int:
        """落一条分析记录（含失败引导记录，FR-CIA-3 追溯）。"""
        return self.execute(
            """INSERT INTO impact_analyses
               (title, change_source, params, result, status, error_code,
                conversation_id, message_id, report_id, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (title, change_source, json.dumps(params or {}, ensure_ascii=False),
             json.dumps(result or {}, ensure_ascii=False), status, error_code,
             conversation_id, message_id, report_id, created_by))

    def bind_report(self, analysis_id: int, report_id: int) -> None:
        """绑定归档报告（报告中心 ↔ 分析记录双向追溯）。"""
        self.execute("UPDATE impact_analyses SET report_id=? WHERE id=?", (report_id, analysis_id))

    def delete_analysis(self, analysis_id: int) -> int:
        """删除记录（仅删除记录，不影响模型与报告）。"""
        return self.execute("DELETE FROM impact_analyses WHERE id=?", (analysis_id,))

    # ── FR-CIA-4：变更模拟记录 ──

    def list_simulations(self, q: str = "", limit: int = 50, offset: int = 0) -> list:
        """模拟记录列表（时间倒序），关键词过滤（标题/场景）。"""
        sql = ("SELECT id, title, scene, changes, comparison, status, created_by, created_at "
               "FROM impact_simulations")
        params = []
        if q:
            sql += " WHERE title LIKE ? OR scene LIKE ?"
            like = f"%{q}%"
            params += [like, like]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = self.rows(sql, params)
        for r in rows:
            r["changes"] = json.loads(r.get("changes") or "[]")
            try:
                r["comparison"] = json.loads(r.get("comparison") or "{}")
            except Exception:
                r["comparison"] = {}
        return rows

    def get_simulation(self, sim_id: int) -> dict | None:
        """模拟详情（含完整 before/after 快照，供双图谱重绘与对比报告）。"""
        row = self.one("SELECT * FROM impact_simulations WHERE id=?", (sim_id,))
        if not row:
            return None
        for key in ("baseline_snapshot", "changes", "before_result", "after_result", "comparison"):
            try:
                row[key] = json.loads(row.get(key) or ("[]" if key == "changes" else "{}"))
            except Exception:
                row[key] = [] if key == "changes" else {}
        return row

    def create_simulation(self, title: str = "", scene: str = "", baseline_snapshot: dict | None = None,
                          changes: list | None = None, before_result: dict | None = None,
                          after_result: dict | None = None, comparison: dict | None = None,
                          status: str = "completed", created_by: str = "") -> int:
        """落一条沙箱模拟记录（FR-CIA-4 追溯）。"""
        return self.execute(
            """INSERT INTO impact_simulations
               (title, scene, baseline_snapshot, changes, before_result, after_result,
                comparison, status, created_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (title, scene, json.dumps(baseline_snapshot or {}, ensure_ascii=False),
             json.dumps(changes or [], ensure_ascii=False),
             json.dumps(before_result or {}, ensure_ascii=False),
             json.dumps(after_result or {}, ensure_ascii=False),
             json.dumps(comparison or {}, ensure_ascii=False), status, created_by))

    def delete_simulation(self, sim_id: int) -> int:
        """删除模拟记录（仅删除记录）。"""
        return self.execute("DELETE FROM impact_simulations WHERE id=?", (sim_id,))
