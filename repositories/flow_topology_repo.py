# -*- coding: utf-8 -*-
"""P2-4 画布拓扑台账仓库：flow_topology_variants 的唯一读写面。

纪律（同其它域 repo）：
- 只做数据访问，不含业务编排（编排在 core/flow_topology.py）；
- 连接注入（构造传入，路由侧由 Depends(db_session) 提供）；
- 写操作经 BaseRepo.execute（SQLite 写锁重试）；**BaseRepo.execute 不
  commit**（P1-4 实测踩坑），故写方法自带 commit；
- **本 repo 只碰flow_topology_variants 一张表**——没有UPDATE /
  DELETE agent_flows 的语句。拓扑台账是体检镜，不是手：红线 #6
  「不碰架构拓扑」在这里落成一条可静态断言的事实（门禁 E 段）。
"""
import json
from typing import Optional

from repositories.base import BaseRepo

#: 台账可写列（upsert 用），顺序即 INSERT 列序
_COLUMNS = (
    "flow_id", "flow_name", "fingerprint", "node_count", "edge_count",
    "status", "source", "blocker_count", "warn_count", "defects_json",
    "runs", "failed_runs", "evidence_state",
)


class FlowTopologyRepo(BaseRepo):
    """flow_topology_variants：一幅画布一行，按 flow_id 唯一。"""

    # ── 读 ──
    def get_variant(self, flow_id: int) -> Optional[dict]:
        """按 flow_id 取一条（defects_json 解析为缺陷字典）。"""
        row = self.one("SELECT * FROM flow_topology_variants WHERE flow_id=?",
                       (int(flow_id),))
        return self._with_defects(row)

    def list_variants(self, *, evidence_state: str = "",
                      fingerprint: str = "", limit: int = 200) -> list:
        """台账列表（evidence_state / fingerprint 可选过滤，按 flow_id 升序）。"""
        lim = max(1, min(int(limit or 200), 5000))
        where, params = [], []
        if evidence_state:
            where.append("evidence_state=?")
            params.append(str(evidence_state))
        if fingerprint:
            where.append("fingerprint=?")
            params.append(str(fingerprint))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(lim)
        return self._with_defects_all(self.rows(
            "SELECT * FROM flow_topology_variants%s ORDER BY flow_id ASC LIMIT ?"
            % clause, tuple(params)))

    def count_by_evidence(self) -> dict:
        rows = self.rows("SELECT evidence_state, COUNT(*) AS n "
                         "FROM flow_topology_variants GROUP BY evidence_state")
        return {r["evidence_state"]: r["n"] for r in rows}

    def count_defective(self) -> int:
        return int(self.scalar(
            "SELECT COUNT(*) FROM flow_topology_variants WHERE blocker_count > 0",
            default=0))

    # ── 写（方法自带 commit）──
    def upsert_variant(self, v: dict) -> bool:
        """按 flow_id upsert 一条台账。**新建返回 True**，已存在只刷新不新建。

        「是否新建」用**插入前查存在性**判定，不用时间窗也不用 changes()：
        时间窗在同秒重扫时会把刷新误判成新建（台账虚报新建数）；
        changes() 只能判整体受影响行数、区分不了"插入"与"更新了同样行数"。
        """
        if not v or not v.get("flow_id"):
            return False
        fid = int(v["flow_id"])
        existed = int(self.scalar(
            "SELECT COUNT(*) FROM flow_topology_variants WHERE flow_id=?",
            (fid,), default=0)) > 0
        row = {k: v.get(k) for k in _COLUMNS}
        row["defects_json"] = row.get("defects_json") or "{}"
        cols = ", ".join(_COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join("%s=excluded.%s" % (k, k)
                            for k in _COLUMNS if k != "flow_id")
        sql = ("INSERT INTO flow_topology_variants (%s) VALUES (%s) "
               "ON CONFLICT(flow_id) DO UPDATE SET %s WHERE flow_id > 0"
               % (cols, marks, updates))
        self.execute(sql, tuple(row[k] for k in _COLUMNS))
        self.conn.commit()
        return not existed

    def refresh_all(self, variants: list) -> int:
        """批量刷新已有台账的运行统计（不新建）。

        用途：扫描发现的画布入册后，日后 flow_runs 涨了要把runs /
        failed_runs / evidence_state 补上——这是**刷新**，不是新变体
        （与 P2-3「指纹不含内容否则台账注水」同源纪律）。
        """
        n = 0
        for v in variants or []:
            fid = int(v.get("flow_id") or 0)
            if not fid:
                continue
            self.execute(
                "UPDATE flow_topology_variants SET runs=?, failed_runs=?, "
                "evidence_state=? WHERE flow_id=?",
                (int(v.get("runs") or 0), int(v.get("failed_runs") or 0),
                 str(v.get("evidence_state") or "uncovered"), fid))
            n += 1
        self.conn.commit()
        return n

    # ── 内部：JSON 字段还原 ──
    @staticmethod
    def _with_defects(row: Optional[dict]) -> Optional[dict]:
        if not row:
            return None
        row = dict(row)
        row["defects"] = FlowTopologyRepo._parse_defects(row.get("defects_json"))
        return row

    @staticmethod
    def _with_defects_all(rows: list) -> list:
        return [FlowTopologyRepo._with_defects(r) for r in rows]

    @staticmethod
    def _parse_defects(raw) -> dict:
        try:
            v = json.loads(raw or "{}")
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}