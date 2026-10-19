# -*- coding: utf-8 -*-
"""P1-3 配置提案仓库：config_proposals 表的唯一读写面。

纪律（同其它域 repo）：
- 只做数据访问，不含业务编排（编排在 core/config_proposal.py）；
- 连接注入（构造传入，路由侧由 Depends(db_session) 提供）；
- 写操作经 BaseRepo.execute（SQLite 写锁重试）；
- **本 repo 没有任何写 core.config 的路径**——提案的"应用"是人在设置页
  改配置（现有配置 API），decide 只记录裁决。这是 P1-3 的铁律：
  只提议，不自动应用（自动改生产配置 = misevolution 直通道）。
"""
import json
import time
from typing import Iterable, Optional

from repositories.base import BaseRepo

#: 落库列（引擎草案里下划线开头的字段是排序/增补用的，不入库）
_COLS = ("rule", "target_section", "target_key", "current_value", "proposed_value",
         "rationale", "evidence_json", "fingerprint", "status")


class ProposalRepo(BaseRepo):
    """config_proposals CRUD + 指纹去重 + 三态裁决。"""

    # ── 读 ──
    def get_proposal(self, proposal_id: int) -> Optional[dict]:
        return self.one("SELECT * FROM config_proposals WHERE id=?", (proposal_id,))

    def list_proposals(self, status: str = "", limit: int = 100) -> list:
        """提案队列（status 空 = 全部；按创建倒序，limit 封顶防长尾淹掉看板）。"""
        lim = max(1, min(int(limit or 100), 500))
        if status:
            return self.rows(
                "SELECT * FROM config_proposals WHERE status=? "
                "ORDER BY id DESC LIMIT ?", (status, lim))
        return self.rows("SELECT * FROM config_proposals ORDER BY id DESC LIMIT ?", (lim,))

    def count_by_status(self) -> dict:
        rows = self.rows("SELECT status, COUNT(*) AS n FROM config_proposals "
                         "GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # ── 写 ──
    def insert_proposal(self, draft: dict) -> tuple:
        """入队一条提案。返回 (row, is_new)。

        指纹去重：`ON CONFLICT(fingerprint) DO NOTHING` + `SELECT changes()`——
        changes() 精确反映「本次 INSERT 是否真插入」（1=新入队，0=指纹冲突），
        无时间窗、无时区口径、并发下也准。曾用「created_at 5 秒内算新」的启发式：
        连续两次 generate 间隔 <5 秒时旧行也被判新 ⇒ created 虚高（实测踩到）。
        现场配置变化 ⇒ proposed_value 变 ⇒ 指纹变 ⇒ 可重新提议（闭环不卡死）。
        """
        evidence = self._evidence_of(draft)
        fp = draft.get("fingerprint") or ""
        self.execute(
            "INSERT INTO config_proposals (rule, target_section, target_key, "
            "current_value, proposed_value, rationale, evidence_json, fingerprint, "
            "status) VALUES (?,?,?,?,?,?,?,?,'pending') "
            "ON CONFLICT(fingerprint) DO NOTHING",
            (draft.get("rule", ""), draft.get("target_section", ""),
             draft.get("target_key", ""), str(draft.get("current_value", "")),
             str(draft.get("proposed_value", "")), draft.get("rationale", ""),
             json.dumps(evidence, ensure_ascii=False), fp))
        inserted = bool(self.conn.execute("SELECT changes()").fetchone()[0])
        row = self.one("SELECT * FROM config_proposals WHERE fingerprint=?", (fp,))
        if not row:                                    # 理论上不可达（INSERT 即成功或冲突）
            return {"fingerprint": fp}, False
        return row, inserted

    def decide_proposal(self, proposal_id: int, decision: str, note: str = "",
                        decided_by: str = "manual") -> dict:
        """裁决（pending → applied | rejected）。终态不重开由引擎层校验，
        这里再兜底一层：WHERE status='pending' 保证并发下也不会双判。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        n = self.execute(
            "UPDATE config_proposals SET status=?, reviewed_by=?, reviewed_at=?, "
            "review_note=? WHERE id=? AND status='pending'",
            (decision, decided_by or "manual", now, note or "", proposal_id))
        if not n:
            return {"ok": False, "error": "提案不存在或已决（终态不重开）"}
        return {"ok": True, "proposal": self.get_proposal(proposal_id)}

    # ── 内部 ──
    @staticmethod
    def _evidence_of(draft: dict) -> dict:
        """草案 → evidence_json（溯源：哪个模式、什么证据促成了这条提案）。

        下划线字段（_pattern/_pattern_count）是引擎内部排序/增补用的，剥掉。
        """
        pat = draft.get("_pattern") or {}
        return {
            "source": pat.get("source"), "intent": pat.get("intent"),
            "kind": pat.get("kind"), "actor": pat.get("actor"),
            "count": pat.get("count"), "conversations": pat.get("conversations"),
            "first_seen": pat.get("first_seen"), "last_seen": pat.get("last_seen"),
            "sample_steps": (pat.get("sample_steps") or [])[:3],
        }
