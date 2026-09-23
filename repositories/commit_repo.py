"""分支版本管理域 Repository：knowledge_commits 表（提交打点 / 提交查询）。

对应「分支版本管理」Spec Task2（写路径埋点）+ Task3（提交查询 API）：
- create_commit：统一提交写入（INSERT 提交 + 前移 branches.head_commit），不 commit，
  由调用方业务事务统一提交；打点失败仅打印、不阻断业务。
- get_commits / get_commit / get_branch_history：提交查询（读路径）。
"""
import hashlib
import json

from repositories.base import BaseRepo


class CommitRepo(BaseRepo):
    """知识提交（分支版本管理）数据访问。"""

    # ── 内容哈希（P0-3，对标 GitHub commit SHA：防改库篡改）──
    @staticmethod
    def content_hash_of(branch, parent_id, kind, changes, snapshot) -> str:
        """提交内容哈希：sha256(branch|parent_id|kind|规范化changes|规范化snapshot)。

        - changes/snapshot 可传 dict（写入路径）或 JSON 字符串（回填/自检路径）；
          内部统一 parse → **规范化 JSON**（sort_keys=True + 紧凑分隔符）后再拼接，
          键序变化不误报，且写入与自检**复用同一函数**（防两套算法漂移）。
        - ensure_ascii=False：中文实体名不转义，便于人工核对（两侧同函数，不影响一致性）。
        - parent_id 为 None（分支首个提交）→ 归一为空串，与库内 NULL 对拍一致。
        """
        def _norm(v):
            if isinstance(v, (str, bytes)):
                try:
                    v = json.loads(v or "{}")
                except Exception:
                    v = {}
            if not isinstance(v, dict):
                v = {}
            return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        raw = "|".join([str(branch or ""),
                        "" if parent_id is None else str(parent_id),
                        str(kind or ""), _norm(changes), _norm(snapshot)])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ── 写路径：提交打点 ──
    def create_commit(self, branch: str, kind: str, message: str,
                      changes: dict, snapshot: dict, actor: str = "系统",
                      source_branch: str = "", source_head_commit=None) -> int:
        """写入一条提交并前移该分支 head_commit，返回提交 id。

        - parent_id = 该分支当前 head_commit（Git 式链）
        - source_branch/source_head_commit：merge 提交双父指针（P1-2，供 ahead/behind 与三点式 diff）
        - 不 conn.commit()：由调用方业务事务统一提交（跟随调用方事务原子性）
        - changes 结构：{entities:[id], relations:[id], documents:[id], chunks:[id]}（无则省略键）
        - content_hash（P0-3）：随行写入内容哈希，供 GET /api/branches/commits/verify 校验篡改
        """
        head = self.scalar("SELECT head_commit FROM branches WHERE name=?", (branch,), default=None)
        chash = self.content_hash_of(branch, head, kind, changes, snapshot)
        cid = self.execute(
            "INSERT INTO knowledge_commits (branch, parent_id, kind, message, changes, snapshot, "
            "created_by, source_branch, source_head_commit, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (branch, head, kind, message,
             json.dumps(changes or {}, ensure_ascii=False),
             json.dumps(snapshot or {}, ensure_ascii=False), actor,
             source_branch, source_head_commit, chash))
        self.execute("UPDATE branches SET head_commit=? WHERE name=?", (cid, branch))
        return cid

    # ── 读路径：提交查询 ──
    def verify_commits(self, limit: int = 0) -> dict:
        """全量重算 content_hash 与库值比对（审计：提交记录是否被改库篡改）。

        - limit>0 只校验最近 limit 条（按 id DESC）
        - 返回 {total, checked, missing, mismatched:[...], ok}
          missing = content_hash 为空的行（迁移前未回填的历史数据）
        - 只读，不写库
        """
        sql = ("SELECT id, branch, parent_id, kind, changes, snapshot, content_hash "
               "FROM knowledge_commits ORDER BY id DESC")
        if limit and limit > 0:
            sql += " LIMIT %d" % int(limit)
        rows = [dict(r) for r in self.conn.execute(sql).fetchall()]
        mismatched, missing = [], []
        for r in rows:
            stored = (r.get("content_hash") or "").strip()
            if not stored:
                missing.append({"id": r["id"], "branch": r.get("branch") or ""})
                continue
            expected = self.content_hash_of(r.get("branch"), r.get("parent_id"), r.get("kind"),
                                            r.get("changes"), r.get("snapshot"))
            if expected != stored:
                mismatched.append({"id": r["id"], "branch": r.get("branch") or "",
                                   "kind": r.get("kind") or "",
                                   "stored": stored[:16] + "...", "expected": expected[:16] + "..."})
        return {"total": len(rows), "checked": len(rows) - len(missing),
                "missing": len(missing), "mismatched": mismatched,
                "ok": not mismatched and not missing}

    @staticmethod
    def _changes_count(row: dict) -> int:
        """变更对象总数（entities/relations/documents/chunks 各清单长度之和）。"""
        try:
            ch = json.loads(row.get("changes") or "{}")
        except Exception:
            ch = {}
        if not isinstance(ch, dict):
            return 0
        return sum(len(v) for v in ch.values() if isinstance(v, list))

    def _parse_row(self, row: dict) -> dict:
        """列表行：changes/snapshot 解析为 dict + changes_count。"""
        for k in ("changes", "snapshot"):
            try:
                row[k] = json.loads(row.get(k) or "{}")
            except Exception:
                row[k] = {}
        row["changes_count"] = self._changes_count(row)
        return row

    def get_commits(self, branch: str = "", kind: str = "", page: int = 1, size: int = 15):
        """提交列表：按 branch/kind 过滤，id DESC。

        page>0 → {items, total, page, limit}（分页）；page=0 → 纯列表（向后兼容）。
        """
        conds, params = [], []
        if branch:
            conds.append("branch=?")
            params.append(branch)
        if kind:
            conds.append("kind=?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = self.scalar(f"SELECT COUNT(*) FROM knowledge_commits{where}", params)
        limit = max(1, min(size or 15, 100))
        offset = (page - 1) * limit if page > 0 else 0
        items = [self._parse_row(dict(r)) for r in self.conn.execute(
            f"SELECT * FROM knowledge_commits{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset)).fetchall()]
        if page > 0:
            return {"items": items, "total": total, "page": page, "limit": limit}
        return items

    def get_commit(self, cid: int) -> dict | None:
        """单条提交详情：changes/snapshot 解析，并附变更对象名称
        （entities.name / relations source_id→target_id / documents.filename，查不到显示 id）。"""
        row = self.one("SELECT * FROM knowledge_commits WHERE id=?", (cid,))
        if not row:
            return None
        row = self._parse_row(row)
        changes = row["changes"] if isinstance(row["changes"], dict) else {}
        ents = changes.get("entities") or []
        if ents:
            names = {}
            ph = ",".join("?" * len(ents))
            for r in self.rows(f"SELECT id, name FROM entities WHERE id IN ({ph})", ents):
                names[r["id"]] = r["name"]
            row["entity_names"] = {e: names.get(e, e) for e in ents}
        rels = changes.get("relations") or []
        if rels:
            rel_names = {}
            ph = ",".join("?" * len(rels))
            for r in self.rows(f"SELECT id, source_id, target_id FROM relations WHERE id IN ({ph})", rels):
                rel_names[r["id"]] = f"{r['source_id']}→{r['target_id']}"
            row["relation_names"] = {rid: rel_names.get(rid, rid) for rid in rels}
        docs = changes.get("documents") or []
        if docs:
            doc_names = {}
            ph = ",".join("?" * len(docs))
            for r in self.rows(f"SELECT id, filename FROM documents WHERE id IN ({ph})", docs):
                doc_names[r["id"]] = r["filename"]
            row["document_names"] = {str(d): doc_names.get(int(d), d) for d in docs}
        # 快照明细（GitHub 单 commit changed files）：数组形态 snapshot（merge/import）→
        # 实体状态清单 {id,name,entity_type,status} + 与 parent 提交数组快照对比的
        # action(added/modified/unchanged/removed) 与字段级 changes。
        # 仅在有数据支撑时输出（manual 单对象快照 / 快照缺失时不产出，前端回退名称清单）。
        snap = row.get("snapshot") or {}
        if isinstance(snap, dict) and isinstance(snap.get("entities"), list):
            cur = {e.get("id"): e for e in snap["entities"]
                   if isinstance(e, dict) and e.get("id")}
            prev = {}
            if row.get("parent_id"):
                prow = self.one("SELECT snapshot FROM knowledge_commits WHERE id=?",
                                (row["parent_id"],))
                if prow:
                    try:
                        ps = prow.get("snapshot") if isinstance(prow, dict) else None
                        ps = json.loads(ps) if isinstance(ps, str) else ps
                    except Exception:
                        ps = {}
                    if isinstance(ps, dict) and isinstance(ps.get("entities"), list):
                        prev = {e.get("id"): e for e in ps["entities"]
                                if isinstance(e, dict) and e.get("id")}
            items = []
            for eid, e in cur.items():
                item = {"id": eid, "name": e.get("name", ""),
                        "entity_type": e.get("entity_type", ""),
                        "status": e.get("status", "")}
                p = prev.get(eid)
                if p is None:
                    item["action"] = "added" if prev else "unchanged"
                else:
                    diffs = []
                    for f in ("name", "entity_type", "status"):
                        if (p.get(f) or "") != (e.get(f) or ""):
                            diffs.append({"field": f, "base_value": p.get(f, ""),
                                          "head_value": e.get(f, "")})
                    item["action"] = "modified" if diffs else "unchanged"
                    if diffs:
                        item["changes"] = diffs
                items.append(item)
            if prev:
                for eid, p in prev.items():
                    if eid not in cur:
                        items.append({"id": eid, "name": p.get("name", ""),
                                      "entity_type": p.get("entity_type", ""),
                                      "status": p.get("status", ""), "action": "removed"})
            row["entity_snapshots"] = items
        return row

    def get_branch_history(self, branch: str) -> list:
        """分支提交时间线：该分支全部提交（id DESC），每条约 {id,kind,message,created_at,created_by,changes_count}。"""
        return [self._parse_row(dict(r)) for r in self.conn.execute(
            "SELECT * FROM knowledge_commits WHERE branch=? ORDER BY id DESC", (branch,)).fetchall()]

    def get_entity_commit_history(self, entity_id) -> list:
        """实体修改历史（FR-KG-11）：筛选 changes JSON 中含该实体 id 的提交，id DESC。

        每项 {id, kind, message, created_at, created_by, fields}：
        - fields：从该提交 snapshot.entities 数组里找该 id 的那项，取 {name, entity_type, status}；
          兼容单对象快照（snapshot={id,name,status,...}）；找不到则为 {}。
        """
        entity_id = str(entity_id)
        out = []
        for r in self.conn.execute(
                "SELECT id, kind, message, changes, snapshot, created_at, created_by "
                "FROM knowledge_commits ORDER BY id DESC").fetchall():
            row = dict(r)
            try:
                changes = json.loads(row.get("changes") or "{}")
            except Exception:
                changes = {}
            if not isinstance(changes, dict):
                changes = {}
            if entity_id not in [str(e) for e in (changes.get("entities") or [])]:
                continue
            try:
                snapshot = json.loads(row.get("snapshot") or "{}")
            except Exception:
                snapshot = {}
            fields = {}
            if isinstance(snapshot, dict):
                item = None
                for it in snapshot.get("entities") or []:
                    if isinstance(it, dict) and str(it.get("id")) == entity_id:
                        item = it
                        break
                if item is None and str(snapshot.get("id")) == entity_id:
                    item = snapshot  # 单对象快照兼容（review/manual 打点结构）
                if item:
                    fields = {k: item.get(k) for k in ("name", "entity_type", "status")
                              if item.get(k) is not None}
            out.append({
                "id": row["id"], "kind": row["kind"], "message": row["message"],
                "created_at": row["created_at"], "created_by": row["created_by"],
                "fields": fields,
            })
        return out

    # ── 写路径：提交级回滚（Task5）──
    def revert_commit(self, cid: int, preview: bool = False, actor: str = "系统") -> dict:
        """提交级回滚：基于 commit.changes + commit.snapshot 反向操作，作用于提交所在分支。

        仅 release 类型分支的提交可回滚（回滚是发布级操作）：
        - 实体/关系（changes.entities / changes.relations）：
          * snapshot 有该 id 且当前 status='deprecated' 且原 status 非 deprecated
            → 恢复为 snapshot 的 status/name/entity_type/properties（缺失键保留当前值）
          * snapshot 无该 id（提交时新增/来自源分支）→ 置 deprecated（软删，留审计）
        - snapshot 兼容数组（{entities:[...], relations:[...]}）与单对象（{id,...}）两种打点结构
        - preview=True 只计算预览不执行；执行时生成 kind='rollback' 提交并前移 head_commit
        - 不 conn.commit()：由调用方事务（db_session）统一提交，保证回滚操作与
          rollback 提交打点原子
        """
        row = self.one("SELECT * FROM knowledge_commits WHERE id=?", (cid,))
        if not row:
            return {"ok": False, "code": 404, "error": f"提交 #{cid} 不存在"}
        branch = row.get("branch") or ""
        b = self.one("SELECT branch_type FROM branches WHERE name=?", (branch,))
        if not b or b.get("branch_type") != "release":
            return {"ok": False, "code": 400,
                    "error": "仅发布(release)分支的提交可回滚（回滚是发布级操作）"}
        try:
            changes = json.loads(row.get("changes") or "{}")
        except Exception:
            changes = {}
        try:
            snapshot = json.loads(row.get("snapshot") or "{}")
        except Exception:
            snapshot = {}
        if not isinstance(changes, dict):
            changes = {}
        if not isinstance(snapshot, dict):
            snapshot = {}

        def _index(key: str) -> dict:
            idx = {}
            for it in snapshot.get(key) or []:
                if isinstance(it, dict) and it.get("id") is not None:
                    idx[str(it["id"])] = it
            return idx

        ent_snap, rel_snap = _index("entities"), _index("relations")
        # 单对象快照兼容（review/manual 打点：snapshot={id,name,status,...} 而非数组）
        if not ent_snap and not rel_snap and snapshot.get("id") is not None:
            sid = str(snapshot["id"])
            if changes.get("entities"):
                ent_snap[sid] = snapshot
            if changes.get("relations"):
                rel_snap[sid] = snapshot

        preview_items = {"entities": [], "relations": []}
        op_ent_ids, op_rel_ids = [], []
        # 实体：计算恢复/软删清单
        for eid in changes.get("entities") or []:
            eid = str(eid)
            cur = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (eid, branch))
            if not cur:
                continue
            snap = ent_snap.get(eid)
            if snap is not None:
                if cur["status"] == "deprecated" and (snap.get("status") or "reviewed") != "deprecated":
                    preview_items["entities"].append(
                        {"id": eid, "action": "restore", "name": cur["name"], "status": cur["status"]})
                    op_ent_ids.append(eid)
            elif cur["status"] != "deprecated":
                preview_items["entities"].append(
                    {"id": eid, "action": "soft_delete", "name": cur["name"], "status": cur["status"]})
                op_ent_ids.append(eid)
        # 关系：同样处理（注意 release 合并复制关系时重新分配 id，需按 snapshot 三元组
        # (source_id,target_id,relation_type) 跨分支定位 release 行，id 直查仅兜底同分支行）
        for rid in changes.get("relations") or []:
            rid = str(rid)
            snap = rel_snap.get(rid)
            cur = None
            if snap is not None and snap.get("source_id"):
                cur = self.one(
                    "SELECT * FROM relations WHERE branch=? AND source_id=? AND target_id=? AND relation_type=?",
                    (branch, snap["source_id"], snap["target_id"], snap.get("relation_type") or ""))
            if cur is None:
                cur = self.one("SELECT * FROM relations WHERE id=?", (rid,))
                if cur and cur.get("branch") != branch:
                    cur = None  # id 匹配到的行不在提交所在分支（merge 打点记录的是源分支 id）→ 跳过
            if not cur:
                continue
            if snap is not None:
                if cur["status"] == "deprecated" and (snap.get("status") or "reviewed") != "deprecated":
                    preview_items["relations"].append(
                        {"id": rid, "row_id": cur["id"], "action": "restore",
                         "source_id": cur["source_id"], "target_id": cur["target_id"]})
                    op_rel_ids.append(cur["id"])
            elif cur["status"] != "deprecated":
                preview_items["relations"].append(
                    {"id": rid, "row_id": cur["id"], "action": "soft_delete",
                     "source_id": cur["source_id"], "target_id": cur["target_id"]})
                op_rel_ids.append(cur["id"])

        if preview:
            return {"ok": True, "preview_mode": True,
                    "preview": {"entities": preview_items["entities"],
                                "relations": preview_items["relations"]}}

        # 执行反向操作（关系按 release 行实际 id 操作，rollback 打点亦记录 release 行）
        for item in preview_items["entities"]:
            if item["action"] == "restore":
                snap = ent_snap[item["id"]]
                cur = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (item["id"], branch))
                self.execute(
                    "UPDATE entities SET name=?, entity_type=?, status=?, properties=? "
                    "WHERE id=? AND branch=?",
                    (snap.get("name", cur["name"]), snap.get("entity_type", cur["entity_type"]),
                     snap.get("status", cur["status"]), snap.get("properties", cur["properties"]),
                     item["id"], branch))
            else:
                self.execute("UPDATE entities SET status='deprecated' WHERE id=? AND branch=?",
                             (item["id"], branch))
        for item in preview_items["relations"]:
            if item["action"] == "restore":
                snap = rel_snap.get(item["id"]) or {}
                self.execute("UPDATE relations SET status=? WHERE id=?",
                             (snap.get("status") or "reviewed", item["row_id"]))
            else:
                self.execute("UPDATE relations SET status='deprecated' WHERE id=?", (item["row_id"],))

        # 回滚提交打点（kind=rollback，branch=提交所在分支，前移 head_commit）
        rollback_commit_id = None
        rb_changes, rb_snap = {}, {}
        if op_ent_ids:
            rb_changes["entities"] = op_ent_ids
            ph = ",".join("?" * len(op_ent_ids))
            rb_snap["entities"] = [dict(r) for r in self.rows(
                f"SELECT id, name, entity_type, status FROM entities WHERE id IN ({ph}) AND branch=?",
                op_ent_ids + [branch])]
        if op_rel_ids:
            rb_changes["relations"] = op_rel_ids
            ph = ",".join("?" * len(op_rel_ids))
            rb_snap["relations"] = [dict(r) for r in self.rows(
                f"SELECT id, source_id, target_id, relation_type, status FROM relations "
                "WHERE id IN ({ph})".format(ph=ph), op_rel_ids)]
        msg = f"回滚提交 #{cid}（{len(op_ent_ids)} 实体/{len(op_rel_ids)} 关系）"
        try:
            rollback_commit_id = self.create_commit(branch, "rollback", msg, rb_changes, rb_snap, actor=actor)
        except Exception as e:
            print(f"[commit_repo] rollback 提交打点失败（不阻断业务）: {e}")
        return {"ok": True, "preview_mode": False,
                "reverted": {"entities": len(op_ent_ids), "relations": len(op_rel_ids)},
                "rollback_commit_id": rollback_commit_id}
