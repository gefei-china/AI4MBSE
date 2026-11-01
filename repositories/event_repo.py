# -*- coding: utf-8 -*-
"""事件底座 Repository（2026-11-01）—— ev_sources / ev_triggers / ev_workitems。

只做数据访问，不含业务编排（编排在 services/event_service.py）。

━━━ 为什么 id 用 TEXT 而不是 INTEGER ━━━
Trigger ID 由外部稳定 ID 派生（`trg_<sha256>`），天然是字符串且可能很长。
用 INTEGER 主键会迫使我们再开一列存外部 ID，且"去重"要靠那个额外列
⇒ 两处真源（ID 列 + 外部 ID 列）可能漂移。
⇒ 直接用派生 ID 作主键：**去重判据 = 主键唯一性**，只有一处真源。

━━━ 幂等怎么做（不是靠"先查再插"）━━━
INSERT 用 `INSERT OR IGNORE` + 主键冲突 ⇒ 并发下也只有一个赢家。
**不**用"SELECT 存在则 return"的读-判-写三步（TOCTOU：两个线程同时判不存在
⇒ 都插 ⇒ 两行）。这与本工程 `core/job_queue.py` 的 `job_key` 唯一约束同源。
"""

from __future__ import annotations

import json

from repositories.base import BaseRepo

__all__ = ["EventRepo"]


class EventRepo(BaseRepo):
    """事件域数据访问。"""

    # ── sources ──
    def upsert_source(self, row: dict) -> None:
        """写入/更新来源配置（幂等）。

        ⚠️ `kind` 是稳定标识：它会出现在 Trigger.source 与策略键里，
        一旦有历史数据就**不能改语义**（改了等于换了个来源，历史对不上）。
        """
        self.conn.execute(
            """INSERT INTO ev_sources
                 (kind, title, listen, allowed_json, scope_keys_json, mode, policy_version, enabled)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(kind) DO UPDATE SET
                 title=excluded.title, listen=excluded.listen,
                 allowed_json=excluded.allowed_json,
                 scope_keys_json=excluded.scope_keys_json,
                 mode=excluded.mode, policy_version=excluded.policy_version,
                 enabled=excluded.enabled""",
            (row.get("kind", ""), row.get("title", ""), row.get("listen", "mentions"),
             json.dumps(row.get("allowed", []), ensure_ascii=False),
             json.dumps(row.get("scope_keys", []), ensure_ascii=False),
             row.get("mode", "observe"), row.get("policy_version", "v1"),
             1 if row.get("enabled", True) else 0),
        )
        self.conn.commit()

    def list_sources(self) -> list:
        return self.rows("SELECT * FROM ev_sources ORDER BY kind")

    def get_source(self, kind: str):
        return self.one("SELECT * FROM ev_sources WHERE kind=?", (kind,))

    def set_source_mode(self, kind: str, mode: str) -> int:
        """改来源模式（observe ⇄ execute）。

        ⚠️ 这是**放开自动执行**的唯一开关 ⇒ 必须留审计痕迹。
        改动本身不递增 policy_version —— 版本由调用方在改策略时显式传，
        否则"同一个版本号对应两种内容"，授权快照就失去意义了。
        """
        cur = self.conn.execute("UPDATE ev_sources SET mode=? WHERE kind=?", (mode, kind))
        self.conn.commit()
        return cur.rowcount

    # ── triggers ──
    def insert_trigger(self, row: dict) -> bool:
        """落 Trigger。返回 True=新写入，False=已存在（幂等命中）。"""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ev_triggers
                 (id, source, external_id, thread_key, actor, actor_type,
                  addressed, text, payload_json, at, disposition, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["source"], row["external_id"], row["thread_key"],
             row["actor"], row.get("actor_type", "human"), row.get("addressed"),
             row.get("text", ""), row.get("payload", "{}"), row.get("at", ""),
             row.get("disposition", "received"), row.get("reason", "")),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def get_trigger(self, tid: str):
        return self.one("SELECT * FROM ev_triggers WHERE id=?", (tid,))

    def trigger_exists(self, tid: str) -> bool:
        return self.count("ev_triggers", "id=?", (tid,)) > 0

    def set_trigger_disposition(self, tid: str, disposition: str, reason: str = "") -> None:
        """记录事件的处置结果（received/admitted/filtered/duplicate）。

        为什么必须落 disposition 而不是只返回：
        本工程最痛的一类问题是"静默丢弃不留痕 ⇒ 看起来像没发起"
        （见用户记忆里「上传附件静默丢弃」那次定位）。
        ⇒ 任何被过滤的事件都要**留下原因**，让人能问"为什么这条没动"。

        ⚠️ 只在**首次判定**时调用。幂等命中走 `bump_trigger_hit`（不改写本字段）——
        否则"admitted 后被重投"会把首次结论抹成 duplicate，事后无法追溯。
        """
        self.conn.execute(
            "UPDATE ev_triggers SET disposition=?, reason=? WHERE id=?",
            (disposition, reason, tid))
        self.conn.commit()

    def bump_trigger_hit(self, tid: str) -> None:
        """幂等命中计数 +1（**不改写首次 disposition/reason**）。

        为什么单独一个字段：首次处置结论是**事实**（不可变），
        重投次数是**观测**（可累加）。混在一列里 ⇒ 重投即抹掉事实。
        """
        self.conn.execute(
            "UPDATE ev_triggers SET hit_count = COALESCE(hit_count,0) + 1 WHERE id=?",
            (tid,))
        self.conn.commit()

    def list_triggers(self, source: str = "", thread_key: str = "",
                      limit: int = 50) -> list:
        sql = "SELECT * FROM ev_triggers"
        where, params = [], []
        if source:
            where.append("source=?"); params.append(source)
        if thread_key:
            where.append("thread_key=?"); params.append(thread_key)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self.rows(sql, params)

    # ── workitems ──
    def insert_workitem(self, row: dict) -> bool:
        """落工单。返回 True=新写入，False=已存在（幂等命中）。"""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ev_workitems
                 (id, trigger_id, source, thread_key, scope, title, action,
                  actor, actor_type, policy_version, strength, mode, state,
                  result, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (row["id"], row["trigger_id"], row["source"], row["thread_key"],
             row.get("scope", ""), row.get("title", ""), row.get("action", ""),
             row.get("actor", ""), row.get("actor_type", "human"),
             row.get("policy_version", "v1"), row.get("strength", "none"),
             row.get("mode", "observe"), row.get("state", "open"),
             row.get("result", ""), row.get("error", "")),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def get_workitem(self, wid: str):
        return self.one("SELECT * FROM ev_workitems WHERE id=?", (wid,))

    def list_workitems(self, source: str = "", state: str = "",
                       mode: str = "", limit: int = 50) -> list:
        sql = "SELECT * FROM ev_workitems"
        where, params = [], []
        if source:
            where.append("source=?"); params.append(source)
        if state:
            where.append("state=?"); params.append(state)
        if mode:
            where.append("mode=?"); params.append(mode)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self.rows(sql, params)

    def set_workitem_state(self, wid: str, state: str, result: str = "",
                           error: str = "") -> int:
        """改工单状态。**状态机判定在 service 层做**，这里只落库。"""
        cur = self.conn.execute(
            "UPDATE ev_workitems SET state=?, result=?, error=? WHERE id=?",
            (state, result, error, wid))
        self.conn.commit()
        return cur.rowcount

    # ── 统计 ──
    def stats(self) -> dict:
        """事件域概览（供监控面板）。"""
        return {
            "sources": self.count("ev_sources"),
            "triggers": self.count("ev_triggers"),
            "workitems": self.count("ev_workitems"),
            "workitems_open": self.count("ev_workitems", "state='open'"),
            "workitems_running": self.count("ev_workitems", "state='running'"),
            "workitems_observe": self.count("ev_workitems", "mode='observe'"),
            "triggers_filtered": self.count("ev_triggers", "disposition='filtered'"),
        }
