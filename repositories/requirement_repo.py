# -*- coding: utf-8 -*-
"""需求条目仓储（2026-11-07）：`requirement_items` 的唯一读写入口。

## 为什么现在才有这张表
`requirement_itemize` 自 2026-10-08 上线起就是**纯函数**（`requirement_itemize_tools.itemize()`
返回 items[] 后随即消失），没有落库、也没有实体表。2026-11-07 实测：全库 `%req%` 只匹配到
`merge_requests` / `query_trace` ⇒ 五元素追溯链的第一环（Requirement）**在地基上是空的**，
且该工具被 `tools.status='disabled'` 静默停用、46 次调用全部返回「未知工具」。

## 三条不可动摇的约束
1. **幂等靠指纹，不靠文本相等**（`fingerprint = sha1(归一化文本)`）：
   同一条需求重复条目化（模型改一轮措辞、用户重贴一段）**不得产生第二条资产**，
   否则追溯矩阵会出现"同一句话两个 REQ 编号且各自连着不同的设计元素"。
2. **`req_id` 首次分配后永不变**：追溯引用（refine/derive/satisfy）记的是 req_id，
   重排/重跑不得改变既有编号 —— 所以只在 INSERT 时分配，UPDATE 分支不碰。
3. **gap 也是资产**：不可验证/待澄清的句子必须与条目同表存放（`kind='gap'`），
   它们的 `reason` 是需求质量结论的证据；只留条目会让"为什么这条不能建模"无据可查。
"""
import hashlib
import re
from datetime import datetime

CAT_CODES = ("F", "P", "I", "C")
_CAT_CODE = {"functional": "F", "performance": "P", "interface": "I", "constraint": "C"}
_WS = re.compile(r"\s+")


def norm_text(text: str) -> str:
    """归一化：全角标点折半角、压空白、去首尾。**只做等价变换，不改语义**。

    刻意**不**去掉标点/数字：`温度 15℃` 与 `温度 25℃` 是两条需求，
    去掉数字会把它们折叠成同一条（这类"过度归一"是幂等键最常见的错误用法）。
    """
    s = str(text or "")
    table = {"，": ",", "。": ".", "；": ";", "：": ":", "（": "(", "）": ")",
             "、": ",", "！": "!", "？": "?", "～": "~", "％": "%"}
    for a, b in table.items():
        s = s.replace(a, b)
    return _WS.sub(" ", s).strip().lower()


def fingerprint(text: str) -> str:
    return hashlib.sha1(norm_text(text).encode("utf-8")).hexdigest()


class RequirementRepo:
    def __init__(self, conn):
        self.conn = conn

    # ── 编号分配 ──────────────────────────────────────────────
    def _next_seq(self, project_id: str, cat: str) -> int:
        """按 (项目, 分类) 取下一个序号。

        口径：只数**已落库的 item**（不含 gap），且按 req_id 实际最大值 +1 ——
        用 count(*)+1 会在"有条目被 retired 后重跑"时撞号（唯一约束会直接抛错）。
        """
        code = _CAT_CODE.get(cat)
        if not code:
            return 1
        pref = f"REQ-{code}"
        row = self.conn.execute(
            "SELECT req_id FROM requirement_items WHERE project_id=? AND kind='item' "
            "AND req_id LIKE ? ORDER BY req_id DESC LIMIT 1",
            (project_id, pref + "%")).fetchone()
        if not row:
            return 1
        rid = row["req_id"] if hasattr(row, "keys") else row[0]
        try:
            return int(str(rid)[len(pref):]) + 1
        except Exception:
            return 1

    # ── 写入（幂等）────────────────────────────────────────────
    def commit(self, items: list, gaps: list | None = None, *, project_id: str = "",
               conversation_id: int = 0, version_id: int = 0, source_ref: str = "",
               domain: str = "", created_by: str = "") -> dict:
        """把条目化结果落到 `requirement_items`（幂等：同文本只留一条）。

        返回 `{"inserted":n,"updated":n,"total":n,"items":[req_id...],"gaps":[req_id...]}`。
        **不做 commit**：由调用方（事务边界）统一提交，避免工具层私自提交把上层事务劈开。
        """
        items = list(items or [])
        gaps = list(gaps or [])
        ins = upd = 0
        item_ids, gap_ids = [], []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for kind, rows in (("item", items), ("gap", gaps)):
            for r in rows:
                txt = str(r.get("text") or "").strip()
                if not txt:
                    continue
                fp = fingerprint(txt)
                exist = self.conn.execute(
                    "SELECT id, req_id, status FROM requirement_items "
                    "WHERE project_id=? AND fingerprint=?", (project_id, fp)).fetchone()
                cat = str(r.get("category") or "") if kind == "item" else ""
                if exist:
                    rid = exist["req_id"] if hasattr(exist, "keys") else exist[1]
                    # 已存在：只补**可变元数据**（主体/来源/关联版本），不动 req_id 与 text
                    self.conn.execute(
                        "UPDATE requirement_items SET subject=COALESCE(NULLIF(?,''),subject), "
                        "source_ref=COALESCE(NULLIF(?,''),source_ref), "
                        "domain=COALESCE(NULLIF(?,''),domain), "
                        "version_id=CASE WHEN ?>0 THEN ? ELSE version_id END, "
                        "conversation_id=CASE WHEN ?>0 THEN ? ELSE conversation_id END, "
                        "updated_at=? WHERE id=?",
                        (str(r.get("subject") or ""), str(r.get("source_ref") or source_ref or ""),
                         str(domain or ""), int(version_id or 0), int(version_id or 0),
                         int(conversation_id or 0), int(conversation_id or 0), now,
                         exist["id"] if hasattr(exist, "keys") else exist[0]))
                    upd += 1
                else:
                    # 优先沿用条目化阶段分配的 `id` —— 需求视图骨架代码里
                    # `attribute reqId = "REQ-P003"` 用的就是它；若此处另发一号，
                    # 库里的条目与生成代码里的 reqId **对不上**，追溯立刻断。
                    rid = str(r.get("id") or "").strip()
                    if not rid or self._rid_taken(project_id, rid):
                        rid = ("REQ-%s%03d" % (_CAT_CODE.get(cat, "G"), self._next_seq(project_id, cat))
                               if kind == "item" else "GAP-%03d" % self._next_gap_seq(project_id))
                    self.conn.execute(
                        "INSERT INTO requirement_items (req_id, fingerprint, kind, text, category, "
                        "subject, sysml_name, ambiguous, reason, source_ref, domain, project_id, "
                        "conversation_id, version_id, status, created_by, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                        (rid, fp, kind, txt, cat, str(r.get("subject") or ""),
                         str(r.get("sysml_name") or ""), 1 if r.get("ambiguous") else 0,
                         str(r.get("reason") or ""), str(r.get("source_ref") or source_ref or ""),
                         str(domain or ""), project_id, int(conversation_id or 0),
                         int(version_id or 0), created_by, now, now))
                    ins += 1
                (item_ids if kind == "item" else gap_ids).append(rid)
        return {"inserted": ins, "updated": upd, "total": ins + upd,
                "items": item_ids, "gaps": gap_ids}

    def _rid_taken(self, project_id: str, req_id: str) -> bool:
        """该编号是否已被**别的**需求占用（同一条重复落库走 fingerprint 分支，不到这里）。"""
        row = self.conn.execute(
            "SELECT 1 FROM requirement_items WHERE project_id=? AND req_id=? LIMIT 1",
            (project_id, req_id)).fetchone()
        return row is not None

    def _next_gap_seq(self, project_id: str) -> int:
        row = self.conn.execute(
            "SELECT req_id FROM requirement_items WHERE project_id=? AND kind='gap' "
            "ORDER BY req_id DESC LIMIT 1", (project_id,)).fetchone()
        if not row:
            return 1
        rid = row["req_id"] if hasattr(row, "keys") else row[0]
        try:
            return int(str(rid).split("-")[-1]) + 1
        except Exception:
            return 1

    # ── 读取 ─────────────────────────────────────────────────
    def list_items(self, *, project_id: str | None = None, conversation_id: int = 0,
                   kind: str = "", status: str = "active", limit: int = 200) -> list:
        sql = ("SELECT req_id, kind, text, category, subject, sysml_name, ambiguous, reason, "
               "source_ref, project_id, conversation_id, version_id, status, merged_into, "
               "created_at, updated_at FROM requirement_items WHERE 1=1")
        args = []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if conversation_id:
            sql += " AND conversation_id=?"
            args.append(int(conversation_id))
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY kind, req_id LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def stats(self, project_id: str | None = None) -> dict:
        """统计：条目数/缺口数/分类分布/可建模率（items / (items+gaps)）。"""
        where, args = "", []
        if project_id is not None:
            where, args = " WHERE project_id=?", [project_id]
        row = self.conn.execute(
            "SELECT SUM(CASE WHEN kind='item' THEN 1 ELSE 0 END) items, "
            "SUM(CASE WHEN kind='gap' THEN 1 ELSE 0 END) gaps FROM requirement_items"
            + where, args).fetchone()
        items = int((row[0] if row else 0) or 0)
        gaps = int((row[1] if row else 0) or 0)
        by_cat = {}
        for r in self.conn.execute(
                "SELECT category, COUNT(*) n FROM requirement_items WHERE kind='item'"
                + (" AND project_id=?" if project_id is not None else "")
                + " GROUP BY category", args).fetchall():
            by_cat[r[0] or "unknown"] = r[1]
        return {"items": items, "gaps": gaps, "by_category": by_cat,
                "itemize_ratio": round(items / max(1, items + gaps), 3)}

    # ── 生命周期 ──────────────────────────────────────────────
    def retire(self, req_id: str, project_id: str = "", reason: str = "") -> int:
        """作废（留痕，不物理删除 —— 追溯链上被引用过的编号不能消失）。"""
        cur = self.conn.execute(
            "UPDATE requirement_items SET status='retired', reason=COALESCE(NULLIF(?,''),reason), "
            "updated_at=? WHERE req_id=? AND (?='' OR project_id=?)",
            (reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), req_id, project_id, project_id))
        return cur.rowcount

    def merge(self, src_req_id: str, dst_req_id: str, project_id: str = "") -> int:
        """合并（同义需求收敛）：src → merged，并记录 merged_into（可回溯，不删行）。"""
        cur = self.conn.execute(
            "UPDATE requirement_items SET status='merged', merged_into=?, updated_at=? "
            "WHERE req_id=? AND (?='' OR project_id=?)",
            (dst_req_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             src_req_id, project_id, project_id))
        return cur.rowcount
