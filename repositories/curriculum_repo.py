# -*- coding: utf-8 -*-
"""P2-2 任务提案仓库：curriculum_items 表的唯一读写面。

纪律（同其它域 repo）：
- 只做数据访问，不含业务编排（编排在 core/curriculum.py）；
- 连接注入（构造传入，路由侧由 Depends(db_session) 提供）；
- 写操作经 BaseRepo.execute（SQLite 写锁重试）；**BaseRepo.execute 不
  commit**（P1-4 实测踩坑：作业 handler 无提交者 ⇒ 四个写方法的数据全丢），
  故两个写方法自带 commit；
- **本 repo 没有任何创建任务的路径**——decide 只记录人的裁决
  （accepted=人认领），任务的创建/执行走人所在链路。这是 P2-2 的铁律：
  只提议，不自动执行（自动造任务并自动跑 = 内部循环自我强化）。
"""
import json
import time
from typing import Optional

from repositories.base import BaseRepo

#: 落库列（引擎草案里下划线开头的字段是排序用的，不入库）
_COLS = ("rule", "task_kind", "title", "weakness", "suggestion", "difficulty",
         "evidence_json", "fingerprint", "status")


class CurriculumRepo(BaseRepo):
    """curriculum_items CRUD + 指纹去重 + 三态裁决。"""

    # ── 读 ──
    def get_proposal(self, item_id: int) -> Optional[dict]:
        return self._with_evidence(
            self.one("SELECT * FROM curriculum_items WHERE id=?", (item_id,)))

    def list_proposals(self, status: str = "", limit: int = 100) -> list:
        """任务提案队列（status 空 = 全部；按创建倒序，limit 封顶防长尾淹掉看板）。"""
        lim = max(1, min(int(limit or 100), 500))
        if status:
            return self._with_evidence_all(self.rows(
                "SELECT * FROM curriculum_items WHERE status=? "
                "ORDER BY id DESC LIMIT ?", (status, lim)))
        return self._with_evidence_all(self.rows(
            "SELECT * FROM curriculum_items ORDER BY id DESC LIMIT ?", (lim,)))

    def count_by_status(self) -> dict:
        rows = self.rows("SELECT status, COUNT(*) AS n FROM curriculum_items "
                         "GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # ── 写 ──
    def insert_proposal(self, draft: dict) -> tuple:
        """入队一条任务提案。返回 (row, is_new)。

        指纹去重：`ON CONFLICT(fingerprint) DO NOTHING` + `SELECT changes()`——
        changes() 精确反映「本次 INSERT 是否真插入」（1=新入队，0=指纹冲突），
        无时间窗、无时区口径、并发下也准（P1-3 的教训：曾用 created_at 5 秒
        内算新的启发式，连续 generate 时间隔 <5 秒 ⇒ created 虚高）。
        """
        evidence = draft.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        fp = draft.get("fingerprint") or ""
        self.execute(
            "INSERT INTO curriculum_items (rule, task_kind, title, weakness, "
            "suggestion, difficulty, evidence_json, fingerprint, status) "
            "VALUES (?,?,?,?,?,?,?,?,'pending') "
            "ON CONFLICT(fingerprint) DO NOTHING",
            (draft.get("rule", ""), draft.get("task_kind", ""),
             draft.get("title", ""), draft.get("weakness", ""),
             draft.get("suggestion", ""), draft.get("difficulty", "low"),
             json.dumps(evidence, ensure_ascii=False), fp))
        self.conn.commit()                # BaseRepo.execute 不 commit（P1-4 踩坑）：第二连接可见性靠它
        inserted = bool(self.conn.execute("SELECT changes()").fetchone()[0])
        row = self.one("SELECT * FROM curriculum_items WHERE fingerprint=?", (fp,))
        if not row:                                    # 理论上不可达（INSERT 即成功或冲突）
            return {"fingerprint": fp}, False
        return self._with_evidence(row), inserted

    def decide_proposal(self, item_id: int, decision: str, note: str = "",
                        decided_by: str = "manual") -> dict:
        """裁决（pending → accepted | rejected）。终态不重开由引擎层校验，
        这里再兜底一层：WHERE status='pending' 保证并发下也不会双判。
        **不创建任何任务**——只记录人的裁决。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        n = self.execute(
            "UPDATE curriculum_items SET status=?, reviewed_by=?, reviewed_at=?, "
            "review_note=? WHERE id=? AND status='pending'",
            (decision, decided_by or "manual", now, note or "", item_id))
        self.conn.commit()                # 同上：自带 commit，不依赖调用方提交
        if not n:
            return {"ok": False, "error": "任务提案不存在或已决（终态不重开）"}
        return {"ok": True, "proposal": self.get_proposal(item_id)}

    # ── 内部 ──
    def _with_evidence(self, row: Optional[dict]) -> Optional[dict]:
        """读路径解析 evidence_json → evidence（dict）：消费侧（看板/门禁）
        不该自己再 json.loads 一遍。evidence_json 原样保留供溯源。"""
        if not row or not isinstance(row.get("evidence_json"), str):
            return row
        try:
            return dict(row, evidence=json.loads(row["evidence_json"]))
        except Exception:
            return dict(row, evidence={})

    def _with_evidence_all(self, rows: list) -> list:
        return [self._with_evidence(r) for r in rows]
