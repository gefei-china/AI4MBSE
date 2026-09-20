"""分支管理域 Repository：branches / merge_requests 表。

对应 routers/branches.py 的全部数据访问。
"""
import json

from repositories.base import BaseRepo


# ── MR 状态机（对标 GitHub PR：draft → open → merged / closed，closed 可 reopen）──
MR_DRAFT = "draft"      # 草稿（不可审批，需先发布评审）
MR_OPEN = "open"        # 待评审
MR_MERGED = "merged"    # 已合并
MR_CLOSED = "closed"    # 已关闭（驳回后可 reopen）
# 旧枚举 → 新枚举（存量数据 / 旧调用方兼容映射）
LEGACY_MR_STATUS = {"pending": MR_OPEN, "approved": MR_MERGED, "rejected": MR_CLOSED}


def _norm_mr_status(status: str) -> str:
    """MR 状态归一化：旧枚举（pending/approved/rejected）映射为新状态机。"""
    return LEGACY_MR_STATUS.get(status, status)


class BranchRepo(BaseRepo):
    """分支与合并请求数据访问。"""

    # ── branches ──
    def list_branches(self) -> list:
        return self.rows("SELECT * FROM branches ORDER BY branch_type, name")

    def get_branch(self, name: str) -> dict | None:
        return self.one("SELECT * FROM branches WHERE name=?", (name,))

    # ── ahead/behind（P1-2 双父指针，对标 GitHub 分支列表）──
    def _commits_since(self, branch: str, since_id) -> int:
        """沿 parent 链从分支 head 往回数到 since_id（不含）的提交数。"""
        head = self.scalar("SELECT head_commit FROM branches WHERE name=?",
                           (branch,), default=None)
        if not head:
            return 0
        n, cur, seen = 0, head, set()
        while cur and cur != since_id and cur not in seen:
            seen.add(cur)
            n += 1
            cur = self.scalar("SELECT parent_id FROM knowledge_commits WHERE id=?",
                              (cur,), default=None)
        return n

    def ahead_behind(self, branch: str, baseline: str) -> dict:
        """分支相对基线的 ahead/behind（GitHub 分支列表语义）。

        以最近一次 branch→baseline 的 merge 提交为分界：
        - ahead = branch 自那次 merge（source_head_commit）之后新增提交数
        - behind = baseline 自那次 merge 之后新增提交数
        """
        merge = self.one(
            "SELECT id, source_head_commit FROM knowledge_commits "
            "WHERE kind='merge' AND branch=? AND source_branch=? ORDER BY id DESC LIMIT 1",
            (baseline, branch))
        if not merge:
            return {"ahead": self._commits_since(branch, None), "behind": 0,
                    "last_merge_commit": None}
        return {
            "ahead": self._commits_since(branch, merge.get("source_head_commit")),
            "behind": self._commits_since(baseline, merge.get("id")),
            "last_merge_commit": merge["id"],
        }

    def count_entities_by_branch(self, branch_name: str) -> int:
        return self.count("entities", "branch=? AND status!='deprecated'", (branch_name,))

    def create_branch(self, name: str, branch_type: str, parent_branch: str, description: str) -> None:
        """创建分支。个人分支创建时 fork 基线分支（dev 或 release）的实体/关系到新分支：
        实体版本化下同 id 不同分支各有版本行，个人分支从基线复制一份独立可编辑的数据。"""
        self.execute(
            "INSERT INTO branches (name, branch_type, parent_branch, description) VALUES (?,?,?,?)",
            (name, branch_type, parent_branch, description),
        )
        if parent_branch and branch_type == "personal":
            # fork 基线实体（排除已废弃，保留完整版本信息）
            self.execute(
                """INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id,
                        source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at,
                        graph_source, graph_x, graph_y, sysml_import_id)
                   SELECT id, name, entity_type, properties, status, ?, project_id,
                        source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at,
                        graph_source, graph_x, graph_y, sysml_import_id
                   FROM entities WHERE branch=? AND status!='deprecated'""",
                (name, parent_branch))
            # fork 基线关系（防御过滤：仅复制两端实体均在已复制实体集内的关系，
            # 避免把基线的悬空/引用已废弃实体的死行带入新分支）
            self.execute(
                """INSERT INTO relations (source_id, target_id, relation_type, status, branch, project_id,
                        confidence, properties, created_at)
                   SELECT r.source_id, r.target_id, r.relation_type, r.status, ?, r.project_id,
                        r.confidence, r.properties, r.created_at
                   FROM relations r WHERE r.branch=?
                     AND r.source_id IN (SELECT id FROM entities WHERE branch=? AND status!='deprecated')
                     AND r.target_id IN (SELECT id FROM entities WHERE branch=? AND status!='deprecated')""",
                (name, parent_branch, parent_branch, parent_branch))
            # P1-2：fork 基线提交（kind=import，head_commit 初始化）——保证合并双父指针
            # source_head_commit 自首次提交起可用（与存量分支迁移基线提交同构）
            try:
                from repositories.commit_repo import CommitRepo
                CommitRepo(self.conn).create_commit(
                    name, "import", f"fork 基线（自 {parent_branch} 复制）", {}, {})
            except Exception as e:
                print(f"[branch_repo] fork 基线提交失败（不阻断）: {e}")

    def update_branch(self, old_name: str, name: str, branch_type: str,
                      parent_branch: str, description: str, status: str) -> None:
        """编辑分支。若改名：级联同步 entities/relations/merge_requests/parent_branch 引用。"""
        self.execute(
            "UPDATE branches SET name=?, branch_type=?, parent_branch=?, description=?, status=? WHERE name=?",
            (name, branch_type, parent_branch, description, status, old_name))
        if name != old_name:
            self.execute("UPDATE entities SET branch=? WHERE branch=?", (name, old_name))
            self.execute("UPDATE relations SET branch=? WHERE branch=?", (name, old_name))
            self.execute("UPDATE merge_requests SET source_branch=? WHERE source_branch=?",
                         (name, old_name))
            self.execute("UPDATE merge_requests SET target_branch=? WHERE target_branch=?",
                         (name, old_name))
            self.execute("UPDATE branches SET parent_branch=? WHERE parent_branch=?",
                         (name, old_name))
            self.execute("UPDATE documents SET branch=? WHERE branch=?", (name, old_name))
            self.execute("UPDATE document_chunks SET branch=? WHERE branch=?", (name, old_name))

    def delete_branch(self, name: str) -> dict:
        """删除分支。引用校验：
        - 分支不存在 → error
        - 有子分支（parent_branch 引用）→ error
        - 有实体/关系 → error（需先清空或迁移）
        - 有未决合并请求 → error
        """
        b = self.get_branch(name)
        if not b:
            return {"ok": False, "error": f"分支 {name} 不存在"}
        children = self.scalar("SELECT COUNT(*) FROM branches WHERE parent_branch=? AND name!=?",
                               (name, name))
        if children:
            return {"ok": False, "error": f"有 {children} 个子分支引用该分支，无法删除"}
        ents = self.count("entities", "branch=?", (name,))
        rels = self.count("relations", "branch=?", (name,))
        if ents or rels:
            return {"ok": False, "error": f"分支内还有 {ents} 实体 / {rels} 关系，请先清空或合并"}
        pending = self.scalar(
            "SELECT COUNT(*) FROM merge_requests WHERE (source_branch=? OR target_branch=?) AND status IN ('draft','open')",
            (name, name))
        if pending:
            return {"ok": False, "error": f"有 {pending} 个未处理合并请求引用该分支，无法删除"}
        self.execute("DELETE FROM merge_requests WHERE source_branch=? OR target_branch=?",
                     (name, name))
        self.execute("DELETE FROM documents WHERE branch=?", (name,))
        self.execute("DELETE FROM document_chunks WHERE branch=?", (name,))
        self.execute("DELETE FROM branches WHERE name=?", (name,))
        return {"ok": True, "deleted": name}

    # ── 分支差异（diff 视图）──
    def diff_branches(self, base: str, head: str, mode: str = "full") -> dict:
        """两个分支的实体/关系差异（只读，无副作用）。

        实体按 id 匹配（id 全局唯一）：added=仅 head 有，removed=仅 base 有，
        modified=两边都有但 name/entity_type/status/properties 任一不同（逐字段 diff）。
        关系按 (source_id, target_id, relation_type) 三元组匹配。

        mode=merge-base（三点式，P1-2）：只返回 head 自上次 merge 到 base 以来
        涉及实体（增量）的差异，对齐 GitHub "Files changed since last merge"。
        """
        def _ents(branch):
            return {r["id"]: dict(r) for r in self.rows(
                "SELECT * FROM entities WHERE branch=? AND status!='deprecated'", (branch,))}

        base_ents, head_ents = _ents(base), _ents(head)
        added, modified, removed = [], [], []

        def _prov(e: dict) -> dict:
            """provenance 元数据（来源文档/来源类型/创建人/置信度）——diff 载荷可追溯。"""
            return {"source_doc": e.get("source_doc", ""),
                    "source_type": e.get("source_type", ""),
                    "created_by": e.get("created_by", ""),
                    "confidence": e.get("confidence")}

        for eid, he in head_ents.items():
            if eid not in base_ents:
                added.append({"id": eid, "name": he.get("name", ""),
                              "entity_type": he.get("entity_type", ""),
                              "status": he.get("status", ""),
                              "properties": he.get("properties", "{}"), **_prov(he)})
                continue
            be = base_ents[eid]
            changes = []
            if be.get("name") != he.get("name"):
                changes.append({"field": "name", "base_value": be.get("name", ""),
                                "head_value": he.get("name", "")})
            if be.get("entity_type") != he.get("entity_type"):
                changes.append({"field": "entity_type", "base_value": be.get("entity_type", ""),
                                "head_value": he.get("entity_type", "")})
            if (be.get("status") or "") != (he.get("status") or ""):
                changes.append({"field": "status", "base_value": be.get("status", ""),
                                "head_value": he.get("status", "")})
            # 置信度比对（数值列，None 归一为空串后比对）
            _bc, _hc = be.get("confidence"), he.get("confidence")
            if (_bc if _bc is not None else "") != (_hc if _hc is not None else ""):
                changes.append({"field": "confidence",
                                "base_value": _bc if _bc is not None else "",
                                "head_value": _hc if _hc is not None else ""})
            try:
                bp = json.loads(be.get("properties") or "{}")
            except Exception:
                bp = {}
            try:
                hp = json.loads(he.get("properties") or "{}")
            except Exception:
                hp = {}
            for k in set(bp) | set(hp):
                if bp.get(k) != hp.get(k):
                    changes.append({"field": k, "base_value": bp.get(k, ""),
                                    "head_value": hp.get(k, "")})
            if changes:
                modified.append({"id": eid, "name": he.get("name", ""),
                                 "entity_type": he.get("entity_type", ""), "changes": changes,
                                 **_prov(he)})
        for eid, be in base_ents.items():
            if eid not in head_ents:
                removed.append({"id": eid, "name": be.get("name", ""),
                                "entity_type": be.get("entity_type", ""),
                                "status": be.get("status", ""), **_prov(be)})

        def _rel_key(r):
            return (r["source_id"], r["target_id"], r["relation_type"])

        base_rels = {_rel_key(r): dict(r) for r in self.rows(
            "SELECT * FROM relations WHERE branch=?", (base,))}
        head_rels = {_rel_key(r): dict(r) for r in self.rows(
            "SELECT * FROM relations WHERE branch=?", (head,))}
        rel_added = [{"source_id": k[0], "target_id": k[1], "relation_type": k[2]}
                     for k in head_rels if k not in base_rels]
        rel_removed = [{"source_id": k[0], "target_id": k[1], "relation_type": k[2]}
                       for k in base_rels if k not in head_rels]
        # 修改关系：三元组身份键相同，但 properties/confidence/status 任一不同（逐字段 diff）
        rel_modified = []
        for k in head_rels:
            if k not in base_rels:
                continue
            br, hr = base_rels[k], head_rels[k]
            rchanges = []
            if (br.get("status") or "") != (hr.get("status") or ""):
                rchanges.append({"field": "status", "base_value": br.get("status", ""),
                                 "head_value": hr.get("status", "")})
            if (br.get("confidence") or "") != (hr.get("confidence") or ""):
                rchanges.append({"field": "confidence", "base_value": br.get("confidence", ""),
                                 "head_value": hr.get("confidence", "")})
            try:
                bp = json.loads(br.get("properties") or "{}")
            except Exception:
                bp = {}
            try:
                hp = json.loads(hr.get("properties") or "{}")
            except Exception:
                hp = {}
            for pk in set(bp) | set(hp):
                if bp.get(pk) != hp.get(pk):
                    rchanges.append({"field": pk, "base_value": bp.get(pk, ""),
                                     "head_value": hp.get(pk, "")})
            if rchanges:
                rel_modified.append({"source_id": k[0], "target_id": k[1],
                                     "relation_type": k[2], "changes": rchanges})
        result = {
            "summary": {"ent_added": len(added), "ent_modified": len(modified),
                        "ent_removed": len(removed), "rel_added": len(rel_added),
                        "rel_modified": len(rel_modified),
                        "rel_removed": len(rel_removed)},
            "entities": {"added": added, "modified": modified, "removed": removed},
            "relations": {"added": rel_added, "modified": rel_modified,
                          "removed": rel_removed},
        }
        # 三点式 diff（merge-base，P1-2）：只保留 head 自上次 merge 以来涉及的实体
        if mode == "merge-base":
            mb = self.one(
                "SELECT id, source_head_commit FROM knowledge_commits "
                "WHERE kind='merge' AND branch=? AND source_branch=? ORDER BY id DESC LIMIT 1",
                (base, head))
            if mb and mb.get("source_head_commit"):
                changed = self._changed_since(head, mb["source_head_commit"])
                result["entities"]["added"] = [e for e in added if e.get("id") in changed]
                result["entities"]["modified"] = [e for e in modified if e.get("id") in changed]
                result["entities"]["removed"] = [e for e in removed if e.get("id") in changed]
                result["summary"]["ent_added"] = len(result["entities"]["added"])
                result["summary"]["ent_modified"] = len(result["entities"]["modified"])
                result["summary"]["ent_removed"] = len(result["entities"]["removed"])
                result["merge_base_commit"] = mb["id"]
            # 关系无版本快照，保持全量（前端可标注）；语义上关系受实体过滤约束
        result["mode"] = mode
        return result

    def _changed_since(self, branch: str, since_id) -> set:
        """沿 parent 链收集 branch 自 since_id（不含）以来各提交涉及的实体 id 并集。"""
        ids = set()
        cur = self.scalar("SELECT head_commit FROM branches WHERE name=?",
                          (branch,), default=None)
        seen = set()
        while cur and cur != since_id and cur not in seen:
            seen.add(cur)
            row = self.one("SELECT changes, parent_id FROM knowledge_commits WHERE id=?", (cur,))
            if not row:
                break
            try:
                ch = json.loads(row.get("changes") or "{}")
            except Exception:
                ch = {}
            if isinstance(ch, dict):
                for e in ch.get("entities") or []:
                    ids.add(str(e))
            cur = row.get("parent_id")
        return ids

    # ── 合并请求：冲突解决 ──
    def _detect_conflicts(self, src: str, tgt: str) -> list:
        """两分支实体级冲突检测（P0-2 完整性，对标 GitHub mergeability）。

        - 属性级：同名实体 name + 属性 key 并集（源/目标新增 key 均比对，不再漏检）；
        - delete_modify：一方 deprecated（软删）、另一方非 deprecated（删除 vs 修改）；
        - 每条冲突含 conflict_type：property | delete_modify。
        """
        src_entities = {r["id"]: dict(r) for r in self.get_entities_by_branch(src)
                        if r["status"] != "deprecated"}
        tgt_entities = {r["id"]: dict(r) for r in self.get_entities_by_branch(tgt)
                        if r["status"] != "deprecated"}
        conflicts = []
        for eid, ent in src_entities.items():
            if eid not in tgt_entities:
                continue
            tgt_ent = tgt_entities[eid]
            try:
                src_props = json.loads(ent["properties"]) if ent["properties"] else {}
            except Exception:
                src_props = {}
            try:
                tgt_props = json.loads(tgt_ent["properties"]) if tgt_ent["properties"] else {}
            except Exception:
                tgt_props = {}
            ent_name = ent.get("name", eid)
            if ent.get("name") != tgt_ent.get("name"):
                conflicts.append({
                    "entity_id": eid, "entity_name": ent_name, "field": "name",
                    "source_value": ent.get("name", ""), "target_value": tgt_ent.get("name", ""),
                    "conflict_type": "property",
                })
            for key in set(src_props) | set(tgt_props):  # 并集：源新增 key 也报冲突
                if src_props.get(key) != tgt_props.get(key):
                    conflicts.append({
                        "entity_id": eid, "entity_name": ent_name, "field": key,
                        "source_value": src_props.get(key, ""), "target_value": tgt_props.get(key, ""),
                        "conflict_type": "property",
                    })
        # 删除 vs 修改（双向）：源删目标留 / 源留目标删
        src_all = {r["id"]: dict(r) for r in self.get_entities_by_branch(src)}
        tgt_all = {r["id"]: dict(r) for r in self.get_entities_by_branch(tgt)}
        for eid, ent in src_all.items():
            if ent["status"] != "deprecated":
                continue
            tgt_ent = tgt_all.get(eid)
            if tgt_ent and tgt_ent["status"] != "deprecated":
                conflicts.append({
                    "entity_id": eid, "entity_name": ent.get("name", eid), "field": "__delete__",
                    "source_value": "deprecated", "target_value": tgt_ent["status"],
                    "conflict_type": "delete_modify",
                })
        for eid, tgt_ent in tgt_all.items():
            if tgt_ent["status"] != "deprecated":
                continue
            ent = src_all.get(eid)
            if ent and ent["status"] != "deprecated":
                conflicts.append({
                    "entity_id": eid, "entity_name": ent.get("name", eid), "field": "__delete__",
                    "source_value": ent["status"], "target_value": "deprecated",
                    "conflict_type": "delete_modify",
                })
        return conflicts

    def get_conflict_status(self, mr: dict) -> list:
        """每个冲突字段的解决状态：{entity_id, entity_name, field,
        source_value, target_value, resolved, pick, value}。"""
        try:
            conflicts = json.loads(mr.get("conflicts") or "[]")
        except Exception:
            conflicts = []
        try:
            resolutions = json.loads(mr.get("resolutions") or "{}")
        except Exception:
            resolutions = {}
        out = []
        for c in conflicts:
            ent_res = resolutions.get(c.get("entity_id"), {}) if isinstance(resolutions, dict) else {}
            r = ent_res.get(c.get("field")) if isinstance(ent_res, dict) else None
            out.append({
                "entity_id": c.get("entity_id"),
                "entity_name": c.get("entity_name", c.get("entity_id")),
                "field": c.get("field"),
                "source_value": c.get("source_value"),
                "target_value": c.get("target_value"),
                "conflict_type": c.get("conflict_type", "property"),
                "resolved": bool(r),
                "pick": (r or {}).get("pick", ""),
                "value": (r or {}).get("value", ""),
            })
        return out

    def pending_conflicts(self, mr: dict) -> list:
        """未解决的冲突列表（approve 门禁用）。"""
        return [c for c in self.get_conflict_status(mr) if not c["resolved"]]

    def _recompute_conflicts(self, mr: dict) -> dict:
        """P0-1：approve 前重算冲突，与 MR 存量清单比对（GitHub mergeability 持续计算）。

        返回 {changed, conflicts, added, removed}。changed=True 时同步刷新 MR 冲突清单
        与 conflict_updated_at（保留仍命中的 resolutions，新冲突待解决、已消失自动消解）。
        """
        fresh = self._detect_conflicts(mr["source_branch"], mr["target_branch"])
        try:
            old = json.loads(mr.get("conflicts") or "[]")
        except Exception:
            old = []
        old_keys = {(c.get("entity_id"), c.get("field")) for c in old}
        fresh_keys = {(c.get("entity_id"), c.get("field")) for c in fresh}
        added = [c for c in fresh if (c["entity_id"], c["field"]) not in old_keys]
        removed = [c for c in old if (c["entity_id"], c["field"]) not in fresh_keys]
        changed = bool(added or removed)
        if changed:
            self.execute(
                "UPDATE merge_requests SET conflicts=?, conflict_updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(fresh, ensure_ascii=False), mr["id"]))
        return {"changed": changed, "conflicts": fresh, "added": added, "removed": removed}

    def resolve_conflict(self, mr_id: int, entity_id: str, field: str,
                         pick: str, value: str = "") -> dict:
        """记录某个冲突字段的解决决策（幂等，重复提交覆盖）。"""
        mr = self.get_merge_request(mr_id)
        if not mr:
            return {"ok": False, "error": f"合并请求 #{mr_id} 不存在"}
        if mr.get("status") not in (MR_DRAFT, MR_OPEN):
            return {"ok": False, "error": f"该合并请求已处理（{mr.get('status')}），不可再解决冲突"}
        try:
            resolutions = json.loads(mr.get("resolutions") or "{}")
        except Exception:
            resolutions = {}
        resolutions.setdefault(entity_id, {})[field] = {"pick": pick, "value": value}
        self.execute("UPDATE merge_requests SET resolutions=? WHERE id=?",
                     (json.dumps(resolutions, ensure_ascii=False), mr_id))
        return {"ok": True}

    # ── merge_requests ──
    # P0-1 发布门禁（评审×分支四象限治理）：未评审数据禁止进入 release 权威基线
    def pending_review_entities(self, branch: str) -> list:
        """源分支未评审实体列表（status 非 reviewed/deprecated）——发布门禁用。

        四象限规则「未评审 + 发布分支 = 禁止」：只有 reviewed 数据可经合并进入 release；
        deprecated 为软删除留痕，同样不进入发布。
        """
        return [dict(r) for r in self.rows(
            "SELECT id, name, entity_type, status FROM entities "
            "WHERE branch=? AND status NOT IN ('reviewed','deprecated') ORDER BY id",
            (branch,))]

    @staticmethod
    def _gate_error(blocked: list, branch: str) -> dict:
        """发布门禁错误返回：含未评审实体清单（前端可据此提示/跳转审核）。"""
        names = "、".join(f"{e['name']}({e['id']})" for e in blocked[:5])
        suffix = "…" if len(blocked) > 5 else ""
        return {"ok": False,
                "blocked_pending_review": [e["id"] for e in blocked],
                "error": f"源分支 {branch} 仍有 {len(blocked)} 条未评审实体"
                         f"（{names}{suffix}），未评审数据禁止进入发布(release)分支，"
                         f"请先到「标注审核」完成审核后再发布"}

    def create_merge_request(self, source_branch: str, target_branch: str, conflicts: str,
                             detail: str = "", release_version: str = "", actor: str = "王工",
                             title: str = "", draft: bool = False) -> dict:
        """创建合并请求。重复未处理（draft/open）拦截：同 源→目标 且未处理 → 复用返回。

        P0-1 发布门禁：目标为 release（发布）时，源分支所有实体必须已评审——
        「未评审 + 发布分支 = 禁止」（评审×分支四象限治理），未通过则在创建时即拦截。
        release_version：目标为 release 时可填发布版本号（如 v1.1），缺省自动生成 v{发布次数}。
        draft：存为草稿（draft 态不可审批）；title 缺省自动生成 source → target。
        """
        dup = self.one(
            "SELECT id FROM merge_requests WHERE source_branch=? AND target_branch=? AND status IN ('draft','open')",
            (source_branch, target_branch))
        if dup:
            return {"ok": False, "error": f"已存在未处理的合并请求（{source_branch} → {target_branch}），请先处理"}
        tgt_b = self.get_branch(target_branch)
        if tgt_b and tgt_b.get("branch_type") == "release":
            blocked = self.pending_review_entities(source_branch)
            if blocked:
                return self._gate_error(blocked, source_branch)
        status = MR_DRAFT if draft else MR_OPEN
        self.execute(
            "INSERT INTO merge_requests (source_branch, target_branch, conflicts, merge_detail, "
            "created_by, release_version, title, status) VALUES (?,?,?,?,?,?,?,?)",
            (source_branch, target_branch, conflicts, detail, actor, (release_version or "").strip(),
             (title or "").strip() or f"{source_branch} → {target_branch}", status))
        return {"ok": True, "id": self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]}

    def delete_merge_request(self, mr_id: int) -> dict:
        mr = self.one("SELECT * FROM merge_requests WHERE id=?", (mr_id,))
        if not mr:
            return {"ok": False, "error": f"合并请求 #{mr_id} 不存在"}
        if mr["status"] == MR_MERGED:
            return {"ok": False, "error": "已通过的合并请求不可删除（数据已合并）"}
        self.execute("DELETE FROM merge_requests WHERE id=?", (mr_id,))
        return {"ok": True, "deleted": mr_id}

    # ── merge_requests ──
    def get_merge_request(self, mr_id: int) -> dict | None:
        return self.one("SELECT * FROM merge_requests WHERE id=?", (mr_id,))

    def list_merge_requests(self, branch: str = "") -> list:
        """MR 列表。branch 非空 → 只返回与该分支相关（源或目标命中）的 MR；缺省全量。"""
        if branch:
            mrs = self.rows(
                "SELECT * FROM merge_requests WHERE source_branch=? OR target_branch=? "
                "ORDER BY created_at DESC", (branch, branch))
        else:
            mrs = self.rows("SELECT * FROM merge_requests ORDER BY created_at DESC")
        for m in mrs:
            m["unresolved_conflicts"] = len(self.pending_conflicts(m))
        return mrs

    def integrity_report(self) -> dict:
        """数据体检（只读巡检）：关系引用完整性——源/目标实体是否与关系同分支存在。
        背景：2026-09-04 曾清理 43 条悬空/跨分支死行，此接口用于持续巡检防复发效果。"""
        rels = self.rows(
            "SELECT id, source_id, target_id, relation_type, branch, status, created_at FROM relations")
        ek = {(r["id"], r["branch"]) for r in
              self.rows("SELECT id, branch FROM entities")}
        dangling = []
        for r in rels:
            miss = [s for s in ("source_id", "target_id")
                    if (r[s], r["branch"]) not in ek]
            if miss:
                dangling.append({
                    "id": r["id"], "branch": r["branch"],
                    "relation_type": r["relation_type"],
                    "source_id": r["source_id"], "target_id": r["target_id"],
                    "status": r["status"],
                    "missing_sides": ["源" if m == "source_id" else "目标" for m in miss],
                })
        dep_refs = self.scalar(
            """SELECT COUNT(*) FROM relations r WHERE EXISTS (
                   SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status='deprecated')
                OR EXISTS (
                   SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status='deprecated')""") or 0
        # ── 实体侧健康（信息项）──
        # ① 孤立实体：本分支内无任何关系引用（非废弃）——合法数据，供建模完整性参考
        orphan_rows = self.rows(
            """SELECT branch, COUNT(*) AS n FROM entities e
               WHERE e.status!='deprecated' AND NOT EXISTS (
                     SELECT 1 FROM relations r WHERE r.branch=e.branch
                       AND (r.source_id=e.id OR r.target_id=e.id))
               GROUP BY branch ORDER BY n DESC""")
        orphans = [{"branch": r["branch"], "count": r["n"]} for r in orphan_rows]
        orphan_total = sum(o["count"] for o in orphans)
        # ② 未注册类型：entity_type 不在本体类型表（ont_types 注册表）中
        bad_type = self.rows(
            """SELECT id, name, entity_type, branch FROM entities e
               WHERE entity_type!='' AND NOT EXISTS (
                     SELECT 1 FROM ontology_types t WHERE t.name=e.entity_type)
               ORDER BY branch, id LIMIT 50""")
        # ③ 字段缺失：name 或 entity_type 为空
        bad_field = self.rows(
            """SELECT id, name, entity_type, branch FROM entities
               WHERE name='' OR entity_type='' ORDER BY branch, id LIMIT 50""")
        # ④ 废弃引用明细：关系指向已 deprecated 实体（合法软删留痕，供清理决策）
        dep_detail = self.rows(
            """SELECT r.id, r.branch, r.relation_type, r.source_id, r.target_id
               FROM relations r WHERE EXISTS (
                   SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status='deprecated')
                OR EXISTS (
                   SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status='deprecated')
               ORDER BY r.branch, r.id LIMIT 50""")
        return {
            "total_relations": len(rels),
            "total_entities": self.count("entities"),
            "dangling_count": len(dangling),
            "dangling": dangling[:50],
            "deprecated_refs": dep_refs,
            "deprecated_detail": [dict(r) for r in dep_detail],
            "orphan_total": orphan_total,
            "orphans_by_branch": orphans,
            "bad_type_count": self.scalar(
                """SELECT COUNT(*) FROM entities e
                   WHERE entity_type!='' AND NOT EXISTS (
                         SELECT 1 FROM ontology_types t WHERE t.name=e.entity_type)""") or 0,
            "bad_type": [dict(r) for r in bad_type],
            "bad_field_count": self.scalar(
                "SELECT COUNT(*) FROM entities WHERE name='' OR entity_type=''") or 0,
            "bad_field": [dict(r) for r in bad_field],
            "healthy": len(dangling) == 0,
        }

    # ── MR 评审意见留痕（时间线，对标 GitHub PR conversation）──
    def list_mr_comments(self, mr_id: int) -> list:
        return self.rows("SELECT * FROM mr_comments WHERE mr_id=? ORDER BY created_at, id", (mr_id,))

    def add_mr_comment(self, mr_id: int, author: str = "", action: str = "comment",
                       comment: str = "") -> dict:
        self.execute(
            "INSERT INTO mr_comments (mr_id, author, action, comment) VALUES (?,?,?,?)",
            (mr_id, author, action, (comment or "").strip()))
        return self.one("SELECT * FROM mr_comments WHERE id=last_insert_rowid()")

    def get_entities_by_branch(self, branch: str) -> list:
        return self.rows("SELECT * FROM entities WHERE branch=?", (branch,))

    # ── 个人分支从基线同步（git merge 语义，2026-09-12）──
    @staticmethod
    def _ent_content_diff(a: dict, b: dict) -> bool:
        """实体内容差异判定（name/type/status/properties 任一不同即视为冲突候选）。"""
        try:
            ap = json.loads(a.get("properties") or "{}")
        except Exception:
            ap = {}
        try:
            bp = json.loads(b.get("properties") or "{}")
        except Exception:
            bp = {}
        return (a.get("name") != b.get("name")
                or a.get("entity_type") != b.get("entity_type")
                or (a.get("status") or "") != (b.get("status") or "")
                or ap != bp)

    def sync_from_branch(self, target: str, source: str, adopt_modified: list = None) -> dict:
        """personal 分支从基线分支(source∈{dev,release})同步图谱数据（git merge 语义）。

        git 分支策略映射：
        - 新增（源有、personal 无）→ 自动采纳复制进 personal（快进引入上游新增）。
        - 修改（两边都有但内容不同）→ 默认视为冲突并保留 personal（不丢本地改动），
          返回冲突清单；若 adopt_modified 指定该 id（“接受源版”）则用源覆盖。
        - 移除（源已无）→ 默认不动 personal，仅返回 removed 提示（不自动删除）。
        - 关系：源新增且两端实体均在 personal（含本条刚同步的新实体）→ 采纳复制。
        - 最后写一条 merge 提交（双父指针 source_branch=source），供 ahead/behind 与三点式 diff。
        """
        adopt = set(adopt_modified or [])
        src_ents = {r["id"]: dict(r) for r in self.rows(
            "SELECT * FROM entities WHERE branch=? AND status!='deprecated'", (source,))}
        tgt_ents = {r["id"]: dict(r) for r in self.rows(
            "SELECT * FROM entities WHERE branch=? AND status!='deprecated'", (target,))}
        added_ids = [i for i in src_ents if i not in tgt_ents]
        modified_conflicts, adopted_mod_ids = [], []
        for eid in src_ents:
            if eid in tgt_ents and self._ent_content_diff(src_ents[eid], tgt_ents[eid]):
                if eid in adopt:
                    adopted_mod_ids.append(eid)
                else:
                    modified_conflicts.append({"id": eid, "name": src_ents[eid].get("name", "")})
        removed_ents = [{"id": i, "name": tgt_ents[i].get("name", "")}
                        for i in tgt_ents if i not in src_ents]

        # 1) 采纳新增实体（保留源的完整版本信息与坐标）
        for eid in added_ids:
            e = src_ents[eid]
            self.execute(
                "INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id,"
                " source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at,"
                " graph_source, graph_x, graph_y, sysml_import_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, e["name"], e["entity_type"], e["properties"], e["status"], target,
                 e.get("project_id"), e.get("source_doc", ""), e.get("source_type", ""),
                 e.get("confidence", 1.0), e.get("created_by", ""), e.get("reviewed_by", ""),
                 e.get("created_at", ""), e.get("reviewed_at", ""), e.get("graph_source", ""),
                 e.get("graph_x", 0), e.get("graph_y", 0), e.get("sysml_import_id", "")))
        # 2) 采纳更新（接受源版 → 覆盖 personal 该实体）
        for eid in adopted_mod_ids:
            e = src_ents[eid]
            self.execute(
                "UPDATE entities SET name=?, entity_type=?, properties=?, status=?, graph_x=?, graph_y=? "
                "WHERE id=? AND branch=?", (e["name"], e["entity_type"], e["properties"],
                                             e["status"], e.get("graph_x", 0), e.get("graph_y", 0), eid, target))
        # 3) 关系：源新增且两端实体在 personal（含刚同步的新实体）→ 采纳复制
        tgt_rel_keys = {(r["source_id"], r["target_id"], r["relation_type"]) for r in
                        self.rows("SELECT * FROM relations WHERE branch=?", (target,))}
        valid_ids = set(tgt_ents) | set(added_ids)
        added_rels = []
        for r in self.rows("SELECT * FROM relations WHERE branch=?", (source,)):
            k = (r["source_id"], r["target_id"], r["relation_type"])
            if k in tgt_rel_keys or r["source_id"] not in valid_ids or r["target_id"] not in valid_ids:
                continue
            self.execute(
                "INSERT INTO relations (source_id, target_id, relation_type, status, branch, project_id,"
                " confidence, properties, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["source_id"], r["target_id"], r["relation_type"], r["status"], target,
                 r.get("project_id"), r.get("confidence"), r.get("properties", "{}"), r.get("created_at", "")))
            added_rels.append(k)
        # 4) 记账：merge 提交（双父指针 source_branch，供 ahead/behind 与三点式 diff）
        try:
            from repositories.commit_repo import CommitRepo
            CommitRepo(self.conn).create_commit(
                target, "merge",
                f"从 {source} 同步：新增实体 {len(added_ids)}、关系 {len(added_rels)}、"
                f"覆盖 {len(adopted_mod_ids)} 冲突",
                {"entities": added_ids + adopted_mod_ids,
                 "relations": [f"{k[0]}|{k[1]}|{k[2]}" for k in added_rels]},
                {"target": target, "source": source}, source_branch=source)
        except Exception as e:
            print(f"[branch_repo] sync 提交记账失败（不阻断）: {e}")
        return {
            "ok": True, "branch": target, "synced_from": source,
            "entities_added": len(added_ids), "entities_adopted": len(adopted_mod_ids),
            "entities_conflicts": modified_conflicts,
            "entities_removed_in_source": removed_ents,
            "relations_added": len(added_rels),
        }

    def resolve_merge(self, mr_id: int, status: str, actor: str = "王工",
                      review_note: str = "") -> dict:
        """审批合并请求：更新状态；approve（merged）时真正执行数据合并（闭环）。

        合并语义（git 风格分支模型）：
        - 目标为 release 类型（发布）→ 复制快照语义：dev 实体/关系/文档复制到 release，
          同 id 覆盖 target 行，源分支（dev）保留继续开发（同步）
        - 目标为 dev（个人分支合并回主开发）→ 迁移语义：无冲突实体迁入目标分支，
          冲突实体按 resolutions 决策写入目标分支后删除源行，个人分支清空可删除
        - 有冲突未解决 → 保留在源分支（路由层门禁已阻止 approve，此处兜底）
        - 目标分支为 release 类型 → 同步把源分支文档复制为发布快照（共享+发布快照）

        P0-1 冲突重算：approve（merged）前重算冲突并与存量清单比对，不一致返回
        conflict_changed（GitHub mergeability 持续计算语义），防止目标分支被静默覆盖。
        P0-1 发布硬门禁：approve 到 release 前二次校验源分支未评审实体必须为零。
        P1-1 状态机：status 归一化为 draft/open/merged/closed；驳回（closed）必填意见。
        """
        status = _norm_mr_status(status)
        mr = self.one("SELECT * FROM merge_requests WHERE id=?", (mr_id,))
        if not mr:
            return {"ok": False, "error": f"合并请求 #{mr_id} 不存在"}
        if status == MR_CLOSED and len((review_note or "").strip()) < 5:
            return {"ok": False, "error": "驳回合并请求必须填写意见（≥5 字，用于追溯）"}
        if status == MR_MERGED:
            # P0-1 冲突重算：创建 MR 后分支数据可能变化，approve 前重算比对
            recomputed = self._recompute_conflicts(mr)
            if recomputed["changed"]:
                return {"ok": False, "code": "conflict_changed",
                        "error": "冲突清单已变化，请重新确认后再通过",
                        "added": recomputed["added"], "removed": recomputed["removed"]}
            tgt_b = self.get_branch(mr["target_branch"])
            if tgt_b and tgt_b.get("branch_type") == "release":
                blocked = self.pending_review_entities(mr["source_branch"])
                if blocked:
                    return self._gate_error(blocked, mr["source_branch"])
        self.execute(
            "UPDATE merge_requests SET status=?, reviewed_by=?, review_note=?, resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, actor, (review_note or "").strip(), mr_id),
        )
        if status != MR_MERGED:
            return {"ok": True, "action": status}
        src, tgt = mr["source_branch"], mr["target_branch"]
        src_ents = {r["id"]: dict(r) for r in self.rows(
            "SELECT * FROM entities WHERE branch=? AND status!='deprecated'", (src,))}
        try:
            resolutions = json.loads(mr["resolutions"] or "{}")
        except Exception:
            resolutions = {}
        is_release = (self.get_branch(tgt) or {}).get("branch_type") == "release"
        moved, updated = 0, 0
        # 分支版本管理：合并打点用——本次合并涉及的实体/关系 id 清单
        merge_ent_ids = list(src_ents.keys())
        merge_rel_ids = []
        if is_release:
            # FR-KG-16 合并回滚：合并执行前抓取 release 当前快照（实体/关系/文档/分块/元数据），
            # 写入 merge_requests.prev_release_snapshot，供 rollback_merge 还原合并前状态
            self.execute(
                "UPDATE merge_requests SET prev_release_snapshot=? WHERE id=?",
                (json.dumps({
                    "entities": [dict(r) for r in self.rows("SELECT * FROM entities WHERE branch=?", (tgt,))],
                    "relations": [dict(r) for r in self.rows("SELECT * FROM relations WHERE branch=?", (tgt,))],
                    "documents": [dict(r) for r in self.rows("SELECT * FROM documents WHERE branch=?", (tgt,))],
                    "doc_metadata": [dict(r) for r in self.rows(
                        "SELECT m.* FROM doc_metadata m JOIN documents d ON m.document_id=d.id WHERE d.branch=?",
                        (tgt,))],
                    "chunks": [dict(r) for r in self.rows("SELECT * FROM document_chunks WHERE branch=?", (tgt,))],
                }, ensure_ascii=False), mr_id))
        if is_release:
            # ── 发布语义：复制快照到 release（源 dev 保留继续开发）──
            for eid, ent in src_ents.items():
                tgt_ent = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (eid, tgt))
                ent_res = resolutions.get(eid, {}) if isinstance(resolutions, dict) else {}
                if ent_res:
                    # 冲突实体：按字段决策生成 release 行最终值
                    try:
                        tgt_props = json.loads(tgt_ent["properties"] or "{}") if tgt_ent else {}
                    except Exception:
                        tgt_props = {}
                    try:
                        src_props = json.loads(ent["properties"] or "{}")
                    except Exception:
                        src_props = {}
                    new_props, new_name = dict(tgt_props), (tgt_ent or ent)["name"]
                    for field, r in ent_res.items():
                        pick = r.get("pick")
                        if field == "name":
                            if pick == "source":
                                new_name = ent["name"]
                            elif pick == "manual":
                                new_name = r.get("value", new_name)
                        else:
                            if pick == "source" and field in src_props:
                                new_props[field] = src_props[field]
                            elif pick == "manual":
                                new_props[field] = r.get("value", tgt_props.get(field, ""))
                    if tgt_ent:
                        self.execute(
                            "UPDATE entities SET name=?, entity_type=?, properties=?, status=? WHERE id=? AND branch=?",
                            (new_name, ent["entity_type"], json.dumps(new_props, ensure_ascii=False),
                             ent["status"], eid, tgt))
                    else:
                        self.execute(
                            "INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id,"
                            " source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at,"
                            " graph_source, graph_x, graph_y, sysml_import_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (eid, new_name, ent["entity_type"], json.dumps(new_props, ensure_ascii=False),
                             ent["status"], tgt, ent["project_id"], ent.get("source_doc", ""),
                             ent.get("source_type", ""), ent.get("confidence", 1.0),
                             ent.get("created_by", ""), ent.get("reviewed_by", ""),
                             ent.get("created_at", ""), ent.get("reviewed_at", ""),
                             ent.get("graph_source", ""), ent.get("graph_x", 0), ent.get("graph_y", 0),
                             ent.get("sysml_import_id", "")))
                    updated += 1
                else:
                    # 无冲突：dev 最新版覆盖 release（同 id），无则插入
                    if tgt_ent:
                        self.execute(
                            "UPDATE entities SET name=?, entity_type=?, properties=?, status=?,"
                            " graph_x=?, graph_y=? WHERE id=? AND branch=?",
                            (ent["name"], ent["entity_type"], ent["properties"], ent["status"],
                             ent.get("graph_x", 0), ent.get("graph_y", 0), eid, tgt))
                    else:
                        self.execute(
                            "INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id,"
                            " source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at,"
                            " graph_source, graph_x, graph_y, sysml_import_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (eid, ent["name"], ent["entity_type"], ent["properties"], ent["status"],
                             tgt, ent["project_id"], ent.get("source_doc", ""), ent.get("source_type", ""),
                             ent.get("confidence", 1.0), ent.get("created_by", ""), ent.get("reviewed_by", ""),
                             ent.get("created_at", ""), ent.get("reviewed_at", ""), ent.get("graph_source", ""),
                             ent.get("graph_x", 0), ent.get("graph_y", 0), ent.get("sysml_import_id", "")))
                    moved += 1
            # 关系复制（两端实体均已在源分支 → 复制到 release，覆盖同三元组旧行）
            for r in self.rows("SELECT * FROM relations WHERE branch=?", (src,)):
                if r["source_id"] not in src_ents or r["target_id"] not in src_ents:
                    continue
                merge_rel_ids.append(r["id"])
                self.execute(
                    "DELETE FROM relations WHERE branch=? AND source_id=? AND target_id=? AND relation_type=?",
                    (tgt, r["source_id"], r["target_id"], r["relation_type"]))
                self.execute(
                    "INSERT INTO relations (source_id, target_id, relation_type, properties, status, branch,"
                    " project_id, confidence, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["source_id"], r["target_id"], r["relation_type"], r["properties"],
                     r["status"], tgt, r["project_id"], r["confidence"], r["created_at"]))
            # P0-4 发布留痕：release 行写 published_at + 发布日志（version 递增，生命周期"发布"环节）
            # ── 分支版本管理 Task4：发布版本号 + 发布清单 ──
            # ① 先打 merge 提交拿 cid（publish_logs.commit_id 关联本次发布；release 打点提前到此处，
            #    尾部打点段仅非 release 执行，保证发布日志与 merge 提交在同一事务且 commit_id 正确）
            merge_commit_id = self._merge_commit(src, tgt, True, merge_ent_ids, merge_rel_ids)
            from datetime import datetime as _pdt
            _pnow = _pdt.now().strftime("%Y-%m-%d %H:%M:%S")
            # ② 版本号：优先 MR 人工填写（release_version），否则自动生成 v{release 已发布 distinct 版本数+1}
            _rv = (mr.get("release_version") or "").strip()
            if not _rv:
                _cnt = self.scalar(
                    "SELECT COUNT(DISTINCT version_label) FROM knowledge_publish_logs "
                    "WHERE branch=? AND action='publish' AND version_label!=''", (tgt,)) or 0
                _rv = f"v{_cnt + 1}"
            for eid, ent in src_ents.items():
                self.execute("UPDATE entities SET published_at=? WHERE id=? AND branch=?",
                             (_pnow, eid, tgt))
                _pv = self.scalar("SELECT COUNT(*)+1 FROM knowledge_publish_logs WHERE entity_id=?", (eid,)) or 1
                self.execute(
                    "INSERT INTO knowledge_publish_logs (entity_id, name, branch, published_at, version, "
                    "merged_from, version_label, commit_id) VALUES (?,?,?,?,?,?,?,?)",
                    (eid, ent.get("name", ""), tgt, _pnow, _pv, src, _rv, merge_commit_id))
        else:
            # ── 迁移语义：个人分支合并回 dev ──
            # 先摘除源分支关系（两端均随实体迁入目标分支）——否则实体迁移/删除时
            # 关系外键引用 (id, src) 会失效（新库 foreign_keys=ON 直接报错）；
            # 实体迁移完成后在目标分支重建，保留原 id，语义与原「UPDATE branch」一致
            src_ids = set(src_ents.keys())
            src_rels = [dict(r) for r in self.rows("SELECT * FROM relations WHERE branch=?", (src,))
                        if r["source_id"] in src_ids and r["target_id"] in src_ids]
            merge_rel_ids = [r["id"] for r in src_rels]
            for r in src_rels:
                self.execute("DELETE FROM relations WHERE id=?", (r["id"],))
            for eid, ent in src_ents.items():
                ent_res = resolutions.get(eid, {}) if isinstance(resolutions, dict) else {}
                if ent_res:
                    # P0-2 delete_modify 决策：keep_modify=dev 复活为源分支修改版（保留修改，
                    # 完整覆盖 name/entity_type/properties/status）；keep_delete=dev 保持软删
                    _dm = ent_res.get("__delete__") if isinstance(ent_res.get("__delete__"), dict) else None
                    if _dm and _dm.get("pick") == "keep_modify":
                        tgt_exists = self.one("SELECT 1 FROM entities WHERE id=? AND branch=?", (eid, tgt))
                        if tgt_exists:
                            self.execute(
                                "UPDATE entities SET name=?, entity_type=?, properties=?, status=?"
                                " WHERE id=? AND branch=?",
                                (ent["name"], ent["entity_type"], ent["properties"], ent["status"],
                                 eid, tgt))
                        else:
                            self.execute("UPDATE entities SET branch=? WHERE id=? AND branch=?",
                                         (tgt, eid, src))
                            moved += 1
                            continue
                        self.execute("DELETE FROM entities WHERE id=? AND branch=?", (eid, src))
                        updated += 1
                        continue
                    # 冲突实体：按字段决策应用目标分支版本行
                    tgt_ent = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (eid, tgt))
                    if tgt_ent:
                        try:
                            tgt_props = json.loads(tgt_ent["properties"] or "{}")
                        except Exception:
                            tgt_props = {}
                        try:
                            src_props = json.loads(ent["properties"] or "{}")
                        except Exception:
                            src_props = {}
                        new_props, new_name = dict(tgt_props), tgt_ent["name"]
                        for field, r in ent_res.items():
                            pick = r.get("pick")
                            if field == "name":
                                if pick == "source":
                                    new_name = ent["name"]
                                elif pick == "manual":
                                    new_name = r.get("value", new_name)
                            else:
                                if pick == "source" and field in src_props:
                                    new_props[field] = src_props[field]
                                elif pick == "manual":
                                    new_props[field] = r.get("value", tgt_props.get(field, ""))
                        self.execute(
                            "UPDATE entities SET name=?, properties=? WHERE id=? AND branch=?",
                            (new_name, json.dumps(new_props, ensure_ascii=False), eid, tgt))
                        # 合并完成：删除源分支版本行（个人副本），目标分支即为最终版本
                        self.execute("DELETE FROM entities WHERE id=? AND branch=?", (eid, src))
                        updated += 1
                    else:
                        self.execute("UPDATE entities SET branch=? WHERE id=? AND branch=?",
                                     (tgt, eid, src))
                        moved += 1
                    continue
                # 无冲突实体：迁移到目标分支；若目标分支已有同 id 版本（属性一致未判冲突）→ 删除源行
                tgt_exists = self.one("SELECT 1 FROM entities WHERE id=? AND branch=?", (eid, tgt))
                if tgt_exists:
                    self.execute("DELETE FROM entities WHERE id=? AND branch=?", (eid, src))
                else:
                    self.execute("UPDATE entities SET branch=? WHERE id=? AND branch=?",
                                 (tgt, eid, src))
                moved += 1
            # 关系边重建：目标已有同三元组 → 先删，由源行取代避免重复（保留原 id，branch 覆盖为目标分支）
            for r in src_rels:
                self.execute(
                    "DELETE FROM relations WHERE branch=? AND source_id=? AND target_id=? AND relation_type=?",
                    (tgt, r["source_id"], r["target_id"], r["relation_type"]))
                cols = [c for c in r.keys() if c not in ("id", "branch")]
                self.execute(
                    "INSERT INTO relations (id, branch, " + ",".join(f"`{c}`" for c in cols) + ") "
                    "VALUES (? , ?, " + ",".join("?" for _ in cols) + ")",
                    [r["id"], tgt] + [r.get(c) for c in cols])
        # P0-2 删除 vs 修改冲突应用：keep_delete=目标分支置 deprecated（软删留痕）；
        # keep_modify=保留修改，已在迁移合并循环中复活目标行为源分支版本（此处兜底幂等置删）
        try:
            _mr_conflicts = json.loads(mr.get("conflicts") or "[]")
        except Exception:
            _mr_conflicts = []
        for _c in _mr_conflicts:
            if _c.get("conflict_type") != "delete_modify":
                continue
            _eid = _c.get("entity_id")
            _ent_res = resolutions.get(_eid, {}) if isinstance(resolutions, dict) else {}
            _r = _ent_res.get(_c.get("field")) if isinstance(_ent_res, dict) else None
            if _r and _r.get("pick") == "keep_delete":
                self.execute("UPDATE entities SET status='deprecated' WHERE id=? AND branch=?",
                             (_eid, tgt))
        # 文档全局化：文件管理从分支体系抽离为全局资产（branch='global'），
        # 不再随分支/发布复制文档快照（实体/关系等建模数据仍按分支合并）
        snap = 0
        self.execute(
            "UPDATE merge_requests SET merge_detail=? WHERE id=?",
            (json.dumps({"moved": moved, "updated": updated, "doc_snapshot": snap},
                        ensure_ascii=False), mr_id))
        # ── 分支版本管理：合并提交打点（kind=merge，branch=目标分支）──
        # release（发布）的 merge 提交已在发布留痕前打点（publish_logs.commit_id 关联）；
        # 非 release（个人→dev 迁移）在此打点。失败仅打印、不阻断业务；跟随调用方请求事务统一提交
        if not is_release:
            self._merge_commit(src, tgt, False, merge_ent_ids, merge_rel_ids)
        return {"ok": True, "action": MR_MERGED, "moved": moved,
                "updated": updated, "doc_snapshot": snap}

    def open_merge_request(self, mr_id: int, actor: str = "王工") -> dict:
        """P1-1：草稿转正式评审（draft → open）。"""
        mr = self.get_merge_request(mr_id)
        if not mr:
            return {"ok": False, "error": f"合并请求 #{mr_id} 不存在"}
        if mr.get("status") != MR_DRAFT:
            return {"ok": False, "error": f"仅草稿（draft）合并请求可发布评审（当前状态：{mr.get('status')}）"}
        self.execute("UPDATE merge_requests SET status=?, reviewed_by=?, resolved_at='' WHERE id=?",
                     (MR_OPEN, actor, mr_id))
        return {"ok": True, "action": MR_OPEN}

    def reopen_merge_request(self, mr_id: int, actor: str = "王工") -> dict:
        """P1-1：重新打开已关闭的合并请求（closed → open）。"""
        mr = self.get_merge_request(mr_id)
        if not mr:
            return {"ok": False, "error": f"合并请求 #{mr_id} 不存在"}
        if mr.get("status") != MR_CLOSED:
            return {"ok": False, "error": f"仅已关闭（closed）合并请求可重新打开（当前状态：{mr.get('status')}）"}
        self.execute("UPDATE merge_requests SET status=?, reviewed_by=?, review_note='', resolved_at='' WHERE id=?",
                     (MR_OPEN, actor, mr_id))
        return {"ok": True, "action": MR_OPEN}

    def _merge_commit(self, src: str, tgt: str, is_release: bool,
                      merge_ent_ids: list, merge_rel_ids: list) -> int | None:
        """合并提交打点（kind=merge，branch=目标分支），返回提交 id（失败返回 None 不阻断）。

        - changes：{entities:[id], relations:[id]}（本次合并涉及的实体/关系 id 清单）
        - snapshot：提交后各对象关键字段摘要（回滚用）：entities [{id,name,entity_type,status}]
          + relations [{id,source_id,target_id,relation_type}]
        - 不 commit：由调用方业务事务统一提交（保证与业务写入原子）
        """
        try:
            from repositories.commit_repo import CommitRepo
            changes, snap = {}, {}
            if merge_ent_ids:
                changes["entities"] = merge_ent_ids
                ph = ",".join("?" * len(merge_ent_ids))
                snap["entities"] = [dict(r) for r in self.rows(
                    f"SELECT id, name, entity_type, status FROM entities "
                    "WHERE id IN ({}) AND branch=?".format(ph), merge_ent_ids + [tgt])]
            if merge_rel_ids:
                changes["relations"] = merge_rel_ids
                ph = ",".join("?" * len(merge_rel_ids))
                snap["relations"] = [dict(r) for r in self.rows(
                    f"SELECT id, source_id, target_id, relation_type FROM relations "
                    "WHERE id IN ({})".format(ph), merge_rel_ids)]
            msg = (f"发布合并：{src}→{tgt}（{len(merge_ent_ids)} 实体/{len(merge_rel_ids)} 关系）"
                   if is_release else
                   f"合并：{src}→{tgt}（{len(merge_ent_ids)} 实体/{len(merge_rel_ids)} 关系）")
            # P1-2 双父指针：记录合并时源分支 head（供 ahead/behind 计数与三点式 diff）
            src_head = self.scalar("SELECT head_commit FROM branches WHERE name=?",
                                   (src,), default=None)
            return CommitRepo(self.conn).create_commit(tgt, "merge", msg, changes, snap,
                                                       source_branch=src, source_head_commit=src_head)
        except Exception as e:
            print(f"[commit_repo] merge 提交打点失败（不阻断业务）: {e}")
            return None

    def snapshot_documents(self, src: str, tgt: str, actor: str = "王工") -> int:
        """共享+发布快照：把 src 分支的文档复制为 tgt 分支快照（覆盖 tgt 同文件名旧快照）。

        返回复制文档数。文档内容已抽取在 document_chunks（含 embedding），
        发布后 tgt 分支检索独立命中快照，dev 分支继续用最新文档。
        """
        docs = self.rows("SELECT * FROM documents WHERE branch=?", (src,))
        moved = 0
        for d in docs:
            old = self.one("SELECT id FROM documents WHERE branch=? AND filename=?",
                           (tgt, d["filename"]))
            if old:
                self.execute("DELETE FROM document_chunks WHERE document_id=?", (old["id"],))
                self.execute("DELETE FROM doc_metadata WHERE document_id=?", (old["id"],))
                self.execute("DELETE FROM documents WHERE id=?", (old["id"],))
            new_id = self.execute(
                "INSERT INTO documents (filename, file_type, file_size, parse_status, chunk_count, "
                "entity_count, quality_score, uploaded_by, pipeline_detail, error_msg, branch, "
                "domain, domain_confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["filename"], d["file_type"], d["file_size"], d["parse_status"],
                 d["chunk_count"], d["entity_count"], d["quality_score"],
                 d.get("uploaded_by", "") or "", d.get("pipeline_detail", "{}") or "{}",
                 d.get("error_msg", "") or "", tgt, d.get("domain", "unknown"),
                 d.get("domain_confidence", 0) or 0))
            md = self.one("SELECT * FROM doc_metadata WHERE document_id=?", (d["id"],))
            if md:
                try:
                    extra = json.loads(md.get("extra", "{}") or "{}")
                except Exception:
                    extra = {}
                extra["published_branch"] = tgt  # 发布标记（release 快照溯源）
                self.execute(
                    "INSERT INTO doc_metadata (document_id, title, author, version, tags, source, extra) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (new_id, md.get("title", "") or d["filename"],
                     md.get("author", "") or actor, md.get("version", "v1.0") or "v1.0",
                     md.get("tags", "[]") or "[]", md.get("source", "upload") or "upload",
                     json.dumps(extra, ensure_ascii=False)))
            else:
                # 源文档无元数据：补齐默认（title 用文件名，author 用默认上传人）
                self.execute(
                    "INSERT INTO doc_metadata (document_id, title, author, version, tags, source, extra) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (new_id, d["filename"], actor, "v1.0", "[]", "upload",
                     json.dumps({"published_branch": tgt}, ensure_ascii=False)))
            for c in self.rows("SELECT * FROM document_chunks WHERE document_id=?", (d["id"],)):
                self.execute(
                    "INSERT INTO document_chunks (document_id, chunk_index, content, embedding, "
                    "embed_version, source_doc, section, bm25_text, linked_entity_ids, branch, "
                    "domain, hyde_questions, hyde_embedding) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id, c["chunk_index"], c["content"], c["embedding"] or "[]",
                     c["embed_version"] or "bigram-tf", c.get("source_doc", ""),
                     c.get("section", ""), c.get("bm25_text", ""),
                     c.get("linked_entity_ids", "[]") or "[]", tgt,
                     c.get("domain", "unknown"), c.get("hyde_questions", "[]") or "[]",
                     c.get("hyde_embedding", "[]") or "[]"))
            moved += 1
        return moved

    # ── FR-KG-16 分支合并回滚 ──
    def _restore_rows(self, table: str, rows: list) -> int:
        """按快照逐行还原（动态列 INSERT，兼容新老库列差异）。返回还原行数。"""
        if not rows:
            return 0
        cols = list(rows[0].keys())
        if not cols:
            return 0
        # 列名加反引号：规避 SQLite 保留字（如 references）导致的语法错误
        sql = (f"INSERT INTO {table} ({','.join(f'`{c}`' for c in cols)}) "
               f"VALUES ({','.join('?' for _ in cols)})")
        for r in rows:
            self.execute(sql, [r.get(c) for c in cols])
        return len(rows)

    def rollback_merge(self, mr_id: int) -> dict:
        """回滚已合并的发布合并请求：release 分支还原到合并前快照（FR-KG-16）。

        还原语义：
        - 校验：mr 存在、status=approved、目标为 release 类型、prev_release_snapshot 非空；
        - 删除 release 当前全部 entities/relations/文档（release 为可重建快照层，物理删除）；
        - 按快照恢复 entities/relations/文档/分块（保持同 id 与原 published_at）；
        - 发布日志保留原记录，并新增 rollback 撤销记录（发布日志标记撤销）；
        - 快照一次性消费：成功后清空，防止二次回滚。
        """
        mr = self.one("SELECT * FROM merge_requests WHERE id=?", (mr_id,))
        if not mr:
            return {"ok": False, "error": f"合并请求 #{mr_id} 不存在"}
        if mr.get("status") != MR_MERGED:
            return {"ok": False, "error": f"仅已通过的合并请求可回滚（当前状态：{mr.get('status')}）"}
        tgt_b = self.get_branch(mr["target_branch"])
        if not tgt_b or tgt_b.get("branch_type") != "release":
            return {"ok": False, "error": "仅目标为 release 发布分支的合并请求可回滚（非发布合并不可回滚）"}
        try:
            snapshot = json.loads(mr.get("prev_release_snapshot") or "{}")
        except Exception:
            snapshot = {}
        if not isinstance(snapshot, dict) or "entities" not in snapshot:
            return {"ok": False, "error": "该合并请求无合并前快照，无法回滚（快照可能已消费）"}
        tgt = mr["target_branch"]
        # ① 删除 release 当前全部数据（先 relations 后 entities 满足外键；文档级联分块/元数据）
        self.execute("DELETE FROM relations WHERE branch=?", (tgt,))
        self.execute("DELETE FROM entities WHERE branch=?", (tgt,))
        self.execute("DELETE FROM documents WHERE branch=?", (tgt,))
        self.execute("DELETE FROM document_chunks WHERE branch=?", (tgt,))
        self.execute(
            "DELETE FROM doc_metadata WHERE document_id IN (SELECT id FROM documents WHERE branch=?)", (tgt,))
        # ② 按快照恢复（先实体后关系满足外键；先文档后分块/元数据）
        restored_e = self._restore_rows("entities", snapshot.get("entities", []))
        restored_r = self._restore_rows("relations", snapshot.get("relations", []))
        self._restore_rows("documents", snapshot.get("documents", []))
        self._restore_rows("doc_metadata", snapshot.get("doc_metadata", []))
        self._restore_rows("document_chunks", snapshot.get("chunks", []))
        # 自增序列修正：显式插入快照 id 后同步 sqlite_sequence，避免后续 INSERT 主键冲突
        for table in ("relations", "documents", "doc_metadata", "document_chunks"):
            self.execute(
                f"UPDATE sqlite_sequence SET seq=(SELECT COALESCE(MAX(id),0) FROM `{table}`) WHERE name=?",
                (table,))
        # ③ 发布日志撤销留痕：保留原发布日志，新增 rollback 记录（版本继续递增）
        from datetime import datetime as _rdt
        _rnow = _rdt.now().strftime("%Y-%m-%d %H:%M:%S")
        for e in snapshot.get("entities", []):
            _pv = self.scalar(
                "SELECT COUNT(*)+1 FROM knowledge_publish_logs WHERE entity_id=?", (e["id"],)) or 1
            self.execute(
                "INSERT INTO knowledge_publish_logs (entity_id, name, branch, published_at, version, merged_from, action) "
                "VALUES (?,?,?,?,?,?,?)",
                (e["id"], e.get("name", ""), tgt, _rnow, _pv, f"回滚自合并请求#{mr_id}", "rollback"))
        # 快照一次性消费：清空防止二次回滚
        self.execute("UPDATE merge_requests SET prev_release_snapshot='{}' WHERE id=?", (mr_id,))
        return {"ok": True, "restored_entities": restored_e, "restored_relations": restored_r}
