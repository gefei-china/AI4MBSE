# -*- coding: utf-8 -*-
"""P2-3 Agent 档案库仓库：agent_variants + variant_outcomes 的唯一读写面。

纪律（同其它域 repo）：
- 只做数据访问，不含业务编排（编排在 core/agent_archive.py）；
- 连接注入（构造传入，路由侧由 Depends(db_session) 提供）；
- 写操作经 BaseRepo.execute（SQLite 写锁重试）；**BaseRepo.execute 不
  commit**（P1-4 实测踩坑），故三个写方法自带 commit；
- **本 repo 只碰这两张表**——没有 UPDATE agents / config_history 的语句，
  档案库是记忆不是手（回滚过人的铁律同 P1-4）。门禁 E 段静态断言这一点。
"""
import json
import time
from typing import Optional

from repositories.base import BaseRepo


class AgentArchiveRepo(BaseRepo):
    """agent_variants 变体台账 + variant_outcomes 结局记录。"""

    # ── 读 ──
    def get_variant(self, *, variant_id: Optional[int] = None,
                    agent_id: Optional[int] = None,
                    version: Optional[str] = None) -> Optional[dict]:
        """按 id 或 (agent_id, version) 取一个变体（画像解析为 profile 字典）。"""
        if variant_id is not None:
            row = self.one("SELECT * FROM agent_variants WHERE id=?", (variant_id,))
        elif agent_id is not None:
            row = self.one("SELECT * FROM agent_variants WHERE agent_id=? AND version=?",
                           (int(agent_id), str(version or "")))
        else:
            return None
        return self._with_profile(row)

    def list_variants(self, *, agent_id: Optional[int] = None,
                      status: str = "", limit: int = 200) -> list:
        """变体台账（agent_id/status 可选过滤；按 agent_id + id 升序，稳定可复现）。"""
        lim = max(1, min(int(limit or 200), 5000))
        where, params = [], []
        if agent_id is not None:
            where.append("agent_id=?")
            params.append(int(agent_id))
        if status:
            where.append("status=?")
            params.append(str(status))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(lim)
        return self._with_profile_all(self.rows(
            "SELECT * FROM agent_variants%s ORDER BY agent_id ASC, id ASC LIMIT ?"
            % clause, tuple(params)))

    def count_by_status(self) -> dict:
        rows = self.rows("SELECT status, COUNT(*) AS n FROM agent_variants "
                         "GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def list_outcomes(self, *, variant_id: int, limit: int = 200) -> list:
        """某变体的结局记录（最新在前）。"""
        lim = max(1, min(int(limit or 200), 2000))
        return self.rows(
            "SELECT * FROM variant_outcomes WHERE variant_id=? "
            "ORDER BY id DESC LIMIT ?", (int(variant_id), lim))

    def count_negative_outcomes(self, variant_id: int) -> int:
        """某变体的负向结局数（死路判定的唯一依据）。

        SQL 侧硬编码两个负向取值——**不信任调用方传入的取值列表**：那会让
        "什么算回归"变成可被参数漂移的口径，判据必须是库里的常量。
        """
        try:
            row = self.one(
                "SELECT COUNT(*) AS n FROM variant_outcomes WHERE variant_id=? "
                "AND outcome='regression'", (int(variant_id),))
            return int(row["n"])
        except Exception:
            return 0

    # ── 写 ──
    def register_variant(self, *, agent_id: int, version: str,
                         parent_version: str = "", origin: str = "",
                         status: str = "active",
                         agent_name: str = "") -> tuple:
        """入册一个变体。返回 (row, is_new)。

        指纹去重：`ON CONFLICT(fingerprint) DO NOTHING` + `SELECT changes()`
        精确反映本次是否真插入（1=新入册，0=指纹冲突）；已入册时只刷新
        last_seen_at，**不改 status**——status 的推进由 refresh() 依证据决定，
        不由"又扫了一遍"决定（否则每次扫描都会把 dead_end 打回 active）。
        """
        fp = fingerprint_of(agent_id, version)
        self.execute(
            "INSERT INTO agent_variants (agent_id, agent_name, version, "
            "parent_version, origin, status, fingerprint) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(fingerprint) DO NOTHING",
            (int(agent_id), agent_name or "", str(version or ""),
             str(parent_version or ""), origin or "", status or "active", fp))
        self.conn.commit()                # BaseRepo.execute 不 commit（P1-4 踩坑）
        inserted = bool(self.conn.execute("SELECT changes()").fetchone()[0])
        if not inserted:
            self.execute("UPDATE agent_variants SET last_seen_at=CURRENT_TIMESTAMP "
                         "WHERE fingerprint=?", (fp,))
            self.conn.commit()
        row = self.one("SELECT * FROM agent_variants WHERE fingerprint=?", (fp,))
        return (self._with_profile(row) if row else {"fingerprint": fp}, inserted)

    def add_outcome(self, *, variant_id: int, outcome: str, source: str = "manual",
                    detail: str = "", recorded_by: str = "") -> Optional[dict]:
        """记一条结局（显式挂载；变体不存在时返回 None——不凭空建行）。"""
        self.execute(
            "INSERT INTO variant_outcomes (variant_id, outcome, source, detail, "
            "recorded_by) VALUES (?,?,?,?,?)",
            (int(variant_id), str(outcome or ""), source or "manual",
             detail or "", recorded_by or ""))
        self.conn.commit()
        return self.one("SELECT * FROM variant_outcomes WHERE variant_id=? "
                        "ORDER BY id DESC LIMIT 1", (int(variant_id),))

    def update_profile(self, variant_id: int, profile: dict) -> int:
        """写回画像（refresh 的落点）。返回受影响行数。"""
        n = self.execute(
            "UPDATE agent_variants SET profile_json=?, last_seen_at=CURRENT_TIMESTAMP "
            "WHERE id=?", (json.dumps(profile or {}, ensure_ascii=False),
                           int(variant_id)))
        self.conn.commit()
        return n

    def mark_dead_end(self, variant_id: int, reason: str = "") -> bool:
        """标记死路（终态）。返回是否**本次**发生转移（已是 dead_end 则 False）。

        `WHERE status!='dead_end'` 兜底并发：重复 refresh 不会重复盖时间戳，
        也不会把已判死路的变体改回别的状态（死路是记忆，不许撤销式遗忘）。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        n = self.execute(
            "UPDATE agent_variants SET status='dead_end', dead_end_reason=?, "
            "dead_end_at=? WHERE id=? AND status!='dead_end'",
            (reason or "回归结论达重复阈值", now, int(variant_id)))
        self.conn.commit()
        return bool(n)

    # ── 内部 ──
    def _with_profile(self, row: Optional[dict]) -> Optional[dict]:
        """读路径解析 profile_json → profile（消费侧不自己 json.loads）。"""
        if not row or not isinstance(row.get("profile_json"), str):
            return row
        try:
            return dict(row, profile=json.loads(row["profile_json"]))
        except Exception:
            return dict(row, profile={})

    def _with_profile_all(self, rows: list) -> list:
        return [self._with_profile(r) for r in rows]


#: 与 core.agent_archive.variant_fingerprint 同算法 —— repo 不 import 引擎
#: （引擎 import repo，反向 import 成环）。**两侧必须同算法**，故两侧各有一
#: 条门禁断言锁住指纹相等（同一 (agent_id, version) 必须算出同一 sha1 前缀）。
def fingerprint_of(agent_id: int, version: str) -> str:
    import hashlib
    raw = "|".join([str(agent_id), str(version or "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]