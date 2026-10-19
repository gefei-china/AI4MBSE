# -*- coding: utf-8 -*-
"""P1-4 沙箱运行仓库：sandbox_runs 表的唯一读写面。

纪律（同其它域 repo）：
- 只做数据访问，不含业务编排（编排在 core/sandbox.py）；
- 连接注入（构造传入，路由侧由 Depends(db_session) 提供）；
- 写操作经 BaseRepo.execute（SQLite 写锁重试）；
- **每个写方法自带 commit**：BaseRepo.execute 本身不提交（路由路径靠
  db_session 兜底），但沙箱评估跑在**作业队列 handler 自开连接**上
  （handler_doc_ingest 同范式），没有 db_session 兜底 ⇒ 不自己提交
  的话整次评估结果在连接关闭时回滚（实测踩到：sandbox_runs 0 行）；
- **本 repo 只碰 sandbox_runs 一张表**——沙箱引擎的"零生产副作用"
  在数据面上可断言（门禁 E 段静态 + 行为级证明）。
"""
from typing import Optional

from repositories.base import BaseRepo


class SandboxRepo(BaseRepo):
    """sandbox_runs CRUD：一次提案的配置影子评估 = 一行运行记录。"""

    def get_run(self, run_id: int) -> Optional[dict]:
        return self.one("SELECT * FROM sandbox_runs WHERE id=?", (int(run_id),))

    def list_runs(self, proposal_id: int = 0, limit: int = 50) -> list:
        """运行历史（proposal_id 空 = 全部；按创建倒序）。"""
        lim = max(1, min(int(limit or 50), 500))
        if proposal_id:
            return self.rows(
                "SELECT * FROM sandbox_runs WHERE proposal_id=? "
                "ORDER BY id DESC LIMIT ?", (int(proposal_id), lim))
        return self.rows("SELECT * FROM sandbox_runs ORDER BY id DESC LIMIT ?", (lim,))

    def count_by_status(self) -> dict:
        rows = self.rows("SELECT status, COUNT(*) AS n FROM sandbox_runs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # ── 写 ──
    def insert_run(self, proposal_id: int, scope: str, shadow_json: str) -> dict:
        """创建运行记录（status=queued）。shadow_json = 本次评估的影子配置。"""
        run_id = self.execute(
            "INSERT INTO sandbox_runs (proposal_id, scope, shadow_json, status) "
            "VALUES (?,?,?, 'queued')",
            (int(proposal_id), str(scope or ""), str(shadow_json or "{}")))
        self.conn.commit()
        return self.get_run(int(run_id)) or {"id": int(run_id)}

    def stamp_job(self, run_id: int, job_id: int) -> None:
        """记录作业队列 job_id（提交后回填，便于从运行跳到作业）。"""
        self.execute("UPDATE sandbox_runs SET job_id=? WHERE id=?",
                     (int(job_id or 0), int(run_id)))
        self.conn.commit()

    def mark_running(self, run_id: int) -> None:
        self.execute(
            "UPDATE sandbox_runs SET status='running', started_at=CURRENT_TIMESTAMP "
            "WHERE id=?", (int(run_id),))
        self.conn.commit()

    def finish_run(self, run_id: int, status: str, verdict: str = "",
                   summary_json: str = "{}") -> None:
        """终态落账（done/failed）。failed 时 verdict 留空、error 在 summary 里。"""
        self.execute(
            "UPDATE sandbox_runs SET status=?, verdict=?, summary_json=?, "
            "finished_at=CURRENT_TIMESTAMP, duration_s="
            "CAST((julianday(CURRENT_TIMESTAMP)-julianday(COALESCE(started_at, "
            "CURRENT_TIMESTAMP)))*86400 AS INTEGER) WHERE id=?",
            (str(status), str(verdict or ""), str(summary_json or "{}"), int(run_id)))
        self.conn.commit()
