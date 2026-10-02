# -*- coding: utf-8 -*-
"""AI 记忆管理仓储（2026-10-02）：agent_memory 的用户侧列表 + 删除。

## 为什么要有它（产品口径）
`agent_memory` 已积累上百条（preference / fact / experience…），三层 tier（core/recall/archival）
**真在用**（实测 58% 被访问过、单条最高 133 次），但**用户完全不可见、不可删除** ——
对齐标杆产品（ChatGPT「设置 → 个性化 → 记忆」、Claude「展示可编辑的记忆摘要」）的合规与信任
落点缺失。本模块提供「看得见 + 删得掉」的最小能力，不改动召回/注入链路。

## 两条删除语义（**刻意区分，别合并**）
- `hard_delete(id)` = 用户行使「被遗忘权」→ **真删**（`DELETE`），不可恢复。
- `soft_delete(id)` = 交给遗忘引擎的软删（`forgotten=1`，检索跳过、可 `restore`）。
处置纪律：**用户主动点删除 → 走 hard_delete**（合规语义：用户要求删除即真删）；
自动/渐进式淘汰 → 走 `soft_delete`（即 `MemoryService.forget` 的既有行为）。

## ⚠️ 老库/夹具降级（**别写死列名**）
`agent_memory` 的多数列（source / relevance / activation / access_count / forgotten / tier…）
是 `database/migrations/columns.py` **迁移补的**，老库或测试夹具可能只有最小基表。
本模块因此**按实际存在的列动态拼 SQL**，缺列时走等价降级（缺 forgotten = 全部存活；
缺 tier = 按 P1-18 规则推断展示值）。硬列列名会让老库直接 `no such column` 崩掉 —— 实测踩过。

## ⚠️ 当前无用户维度隔离（已知缺口，登记在案）
`agent_memory` 按 `agent_id`（'design'/'impact'/…）归属，**没有 user 列**（实测 `scope_type`
仅 `''` / `'project'`）。故本模块列的是**全局记忆**。多用户部署前必须补 user 维度，
否则 A 用户能看到并删除 B 用户的记忆。见 `docs/上下文-记忆-意图识别-能力评估-20261002.md` §4。
"""
import json

# 用户可见的 tier 语义（与 P1-18 分层一致）
TIER_LABELS = {
    "core": "常驻（偏好类，每轮注入）",
    "recall": "按需召回（与当前问题相关才注入）",
    "archival": "已归档（软删，检索跳过）",
}
TIER_ORDER = ("core", "recall", "archival")

# 列表/导出**希望**返回的列（按实际存在性取交集，缺列不报错）
_LIST_COLS = ("id", "agent_id", "mem_type", "content", "created_at", "source", "relevance",
              "activation", "access_count", "last_accessed_at", "forgotten", "mem_topic",
              "scope_type", "scope_id")


class MemoryAdminRepo:
    def __init__(self, conn):
        self.conn = conn

    # ── 内部：列存在性（老库/夹具缺列时降级，零行为漂移）──
    def _cols(self) -> set:
        try:
            return {r[1] for r in self.conn.execute("PRAGMA table_info(agent_memory)")}
        except Exception:
            return set()

    def _sel_cols(self, cols: set) -> str:
        """按实际存在的列拼 SELECT 列表（缺列不报错；至少给 id）。"""
        sel = [c for c in _LIST_COLS if c in cols]
        if "tier" in cols:
            sel.append("tier")
        return ", ".join(sel) if sel else "id"

    # ── 查询 ──
    def list(self, agent_id=None, tier=None, mem_type=None, q=None,
             include_forgotten=0, limit=200, offset=0) -> dict:
        """分页列表 + 统计。`include_forgotten=0`（默认）只看存活记忆。"""
        cols = self._cols()
        has_tier, has_fg = "tier" in cols, "forgotten" in cols
        where, args = [], []
        if not include_forgotten and has_fg:      # 无 forgotten 列 = 全部存活，不加条件
            where.append("forgotten=0")
        if agent_id and "agent_id" in cols:
            where.append("agent_id=?")
            args.append(agent_id)
        if tier and has_tier:
            where.append("tier=?")
            args.append(tier)
        if mem_type and "mem_type" in cols:
            where.append("mem_type=?")
            args.append(mem_type)
        if q and "content" in cols:
            where.append("content LIKE ?")
            args.append("%" + q + "%")
        w = ("WHERE " + " AND ".join(where)) if where else ""

        total = self.conn.execute(
            "SELECT COUNT(*) FROM agent_memory " + w, args).fetchone()[0]
        # 排序：偏好/事实类优先（对用户最有意义），再按激活度与新近（缺列则逐项退让）
        order = []
        if "mem_type" in cols:
            order.append("CASE mem_type WHEN 'preference' THEN 0 WHEN 'fact' THEN 1 ELSE 2 END")
        if "activation" in cols:
            order.append("activation DESC")
        order.append("id DESC")
        rows = self.conn.execute(
            "SELECT " + self._sel_cols(cols) + " FROM agent_memory " + w
            + " ORDER BY " + ", ".join(order) + " LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d.setdefault("mem_type", "")
            if not has_tier:
                # 缺列时按 P1-18 的分层规则**推断**展示值（不写库）：preference→core，
                # 已遗忘→archival，其余→recall
                d["tier"] = ("core" if d.get("mem_type") == "preference"
                             else ("archival" if d.get("forgotten") else "recall"))
            d["tier_label"] = TIER_LABELS.get(d.get("tier") or "", "")
            items.append(d)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "stats": self.stats()}

    def stats(self) -> dict:
        """总览计数 + 分布。逐列降级：缺 forgotten=全部存活；缺 tier 用 SQL 推断；缺 access_count 则 None。"""
        cols = self._cols()
        has_tier, has_fg, has_ac = "tier" in cols, "forgotten" in cols, "access_count" in cols
        one = lambda sql: self.conn.execute(sql).fetchone()[0]
        total = one("SELECT COUNT(*) FROM agent_memory")
        alive = one("SELECT COUNT(*) FROM agent_memory WHERE forgotten=0") if has_fg else total

        def _grp(expr, extra_where=""):
            return {r[0]: r[1] for r in self.conn.execute(
                "SELECT " + expr + " t, COUNT(*) FROM agent_memory WHERE 1=1" + extra_where
                + " GROUP BY t")}

        alive_where = " AND forgotten=0" if has_fg else ""
        tier_expr = ("COALESCE(tier,'')" if has_tier
                     else "CASE WHEN mem_type='preference' THEN 'core' "
                          "WHEN forgotten=1 THEN 'archival' ELSE 'recall' END")
        return {
            "total": total,
            "alive": alive,
            "forgotten": total - alive,
            "by_tier": _grp(tier_expr, alive_where) if ("mem_type" in cols or has_tier) else {},
            "by_agent": _grp("agent_id", alive_where) if "agent_id" in cols else {},
            "by_mem_type": _grp("mem_type", alive_where) if "mem_type" in cols else {},
            # 缺列时给 None（而不是 0）——「数据不足不许报 0」是本仓既定纪律
            "accessed": (one("SELECT COUNT(*) FROM agent_memory WHERE access_count>0"
                             + alive_where) if has_ac else None),
        }

    def get(self, mid: int):
        try:
            r = self.conn.execute("SELECT * FROM agent_memory WHERE id=?", (mid,)).fetchone()
            return dict(r) if r else None
        except Exception:
            return None

    # ── 写入（三种删除/恢复语义）──
    def hard_delete(self, mid: int) -> bool:
        """用户行使被遗忘权：**真删**。返回是否确有删除。"""
        cur = self.conn.execute("DELETE FROM agent_memory WHERE id=?", (mid,))
        self.conn.commit()
        return cur.rowcount > 0

    def soft_delete(self, mid: int) -> bool:
        """软删（forgotten=1）：检索跳过、可 restore。与遗忘引擎同语义。"""
        if "forgotten" not in self._cols():
            return False        # 无该列 → 不支持软删（老库），不静默假装成功
        cur = self.conn.execute(
            "UPDATE agent_memory SET forgotten=1 WHERE id=? AND forgotten=0", (mid,))
        self.conn.commit()
        return cur.rowcount > 0

    def restore(self, mid: int) -> bool:
        """恢复软删记忆。"""
        if "forgotten" not in self._cols():
            return False
        cur = self.conn.execute(
            "UPDATE agent_memory SET forgotten=0 WHERE id=? AND forgotten=1", (mid,))
        self.conn.commit()
        return cur.rowcount > 0

    def hard_delete_forgotten(self) -> int:
        """清除全部已软删（forgotten=1）记忆，返回条数。用于「清空已归档」。"""
        if "forgotten" not in self._cols():
            return 0
        cur = self.conn.execute("DELETE FROM agent_memory WHERE forgotten=1")
        self.conn.commit()
        return cur.rowcount

    # ── 导出（用户数据可携带，合规配套）──
    def export_all(self, agent_id=None) -> list:
        cols = self._cols()
        where, args = [], []
        if agent_id and "agent_id" in cols:
            where.append("agent_id=?")
            args.append(agent_id)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            "SELECT " + self._sel_cols(cols) + " FROM agent_memory " + w + " ORDER BY id",
            args).fetchall()
        return [dict(r) for r in rows]
