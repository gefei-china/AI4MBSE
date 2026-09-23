"""知识库域 Repository：entities / relations / ontology_types 表。

对应 routers/knowledge.py 的全部数据访问。
"""
from repositories.base import BaseRepo


class KnowledgeRepo(BaseRepo):
    """知识库（实体/关系/本体）数据访问。"""

    # ── entities ──
    def list_entities(self, status: str | None = None, branch: str | None = None,
                      search: str | None = None, project_id: str | None = None) -> list:
        q = "SELECT * FROM entities WHERE 1=1"
        params = []
        if project_id:  # P0-1: 项目上下文隔离（缺省不过滤，向后兼容）
            q += " AND project_id=?"
            params.append(project_id)
        if status:
            q += " AND status=?"
            params.append(status)
        if branch:
            q += " AND branch=?"
            params.append(branch)
        if search:
            q += " AND (name LIKE ? OR id LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        q += " ORDER BY created_at DESC LIMIT 200"
        rows = self.rows(q, params)
        if status:  # P0 审核队列重复治理：同名同类型实体聚合重复簇
            self._annotate_dup_clusters(rows)
        return rows

    # ── P0-④（2026-09-11）时态管理 ────────────────────────────────
    def list_entities_temporal(self, status, branch, search, project_id,
                               extra_where, extra_params,
                               as_of=None, range_start=None, range_end=None):
        """时态感知的实体列表（带 valid_from/valid_to 过滤）。

        extra_where / extra_params 由 ontology_semantics.temporal_query_filter() 生成。
        - as_of 单时点查询
        - range_start/range_end 时段查询
        - 旧调用不传 → extra_where 为空，本函数不被调用
        """
        q = "SELECT * FROM entities WHERE 1=1"
        params = []
        if project_id:
            q += " AND project_id=?"
            params.append(project_id)
        if status:
            q += " AND status=?"
            params.append(status)
        if branch:
            q += " AND branch=?"
            params.append(branch)
        if search:
            q += " AND (name LIKE ? OR id LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        if extra_where:
            q += " AND " + extra_where
            params.extend(extra_params)
        q += " ORDER BY valid_from DESC LIMIT 200"
        rows = self.rows(q, params)
        # 时态查询附 as_of 标记（前端可显示「查询时刻」）
        for r in rows:
            r["temporal_as_of"] = as_of
            r["temporal_range"] = [range_start, range_end] if (range_start or range_end) else None
        if status:
            self._annotate_dup_clusters(rows)
        return rows

    def list_entity_history(self, entity_id: str, branch: str = "dev") -> list:
        """实体历史版本（P0-1 起读影子历史表 entity_versions）。

        按 version_no DESC 排序——最新在前（比 valid_from 排序更稳：同秒变更不歧义）。

        无版本行时**回退主表**（保持旧行为）：merge/sync/图谱等旁路写入产生的实体行
        尚未落版本行，回退可保证 /history 既不空也不变形。
        """
        rows = self.rows(
            "SELECT * FROM entity_versions WHERE id=? AND branch=? "
            "ORDER BY version_no DESC",
            (entity_id, branch or "dev"))
        if rows:
            return rows
        return self.rows(
            "SELECT * FROM entities WHERE id=? AND branch=? "
            "ORDER BY valid_from DESC",
            (entity_id, branch or "dev"))

    def get_entity_as_of(self, entity_id: str, as_of: str,
                         branch: str | None = None) -> dict | None:
        """时点查询：as_of 时刻该实体是什么状态（P0-1 起读影子历史表）。

        - branch=None → 跨分支查（按 release 优先）
        - branch 指定 → 限定该分支
        优先命中 entity_versions 的版本区间；无命中则**回退主表旧逻辑**（兼容无版本行场景）。
        """
        sql = ("SELECT * FROM entity_versions WHERE id=? "
               "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) ")
        params: list = [entity_id, as_of, as_of]
        if branch:
            sql += "AND branch=? "
            params.append(branch)
        sql += "ORDER BY (branch='release') DESC, version_no DESC LIMIT 1"
        row = self.one(sql, tuple(params))
        if row:
            return row
        sql2 = ("SELECT * FROM entities WHERE id=? "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) ")
        params2: list = [entity_id, as_of, as_of]
        if branch:
            sql2 += "AND branch=? "
            params2.append(branch)
        sql2 += "ORDER BY (branch='release') DESC, valid_from DESC LIMIT 1"
        return self.one(sql2, tuple(params2))

    # ── P0-1 版本写入侧（影子历史表 entity_versions）──
    # 版本元数据列（不属于从 entities 快照过来的业务字段）
    _EV_META = ("version_no", "valid_from", "valid_to", "is_current",
                "tx_from", "tx_to", "change_kind", "changed_by")

    @staticmethod
    def _vnow() -> str:
        """版本时间戳：**微秒精度**。

        秒精度会让同一秒内的两次变更撞上 UNIQUE(id, branch, valid_from)（可复现风险），
        且会毁掉「退役时刻 == 新版本生效时刻」这一半开区间语义。
        """
        from datetime import datetime as _dt
        return _dt.now().isoformat(sep=' ', timespec='microseconds')

    def _ev_ctx(self):
        """返回 (payload_cols, insert_sql)：快照列按「entity_versions ∩ entities」动态取交集。

        动态而非硬编码：防迁移顺序差异或未来给 entities 加列导致插入报 no such column。
        实例内缓存，避免每次写入都查 PRAGMA。
        """
        if not hasattr(self, "_ev_cache"):
            ev = [r[1] for r in self.conn.execute(
                "PRAGMA table_info(entity_versions)").fetchall()]
            en = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(entities)").fetchall()}
            payload = [c for c in ev if c in en and c not in self._EV_META]
            cols = ["version_no", "valid_from", "valid_to", "is_current",
                    "tx_from", "tx_to", "change_kind", "changed_by"] + payload
            sql = ("INSERT INTO entity_versions (%s) VALUES (%s)"
                   % (", ".join(cols), ", ".join("?" for _ in cols)))
            self._ev_cache = (payload, sql)
        return self._ev_cache

    def _insert_version(self, entity_id: str, branch: str, version_no: int, valid_from: str,
                        valid_to, is_current: int, change_kind: str, changed_by: str) -> None:
        """把 entities 当前行快照写入 entity_versions（主表须已是该版本的状态）。"""
        payload, sql = self._ev_ctx()
        row = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (entity_id, branch))
        if not row:
            return
        vals = [version_no, valid_from, valid_to, is_current,
                valid_from, None, change_kind, changed_by or ""]
        vals += [row.get(c) for c in payload]
        self.execute(sql, tuple(vals))

    def _ensure_initial_version(self, entity_id: str, branch: str) -> bool:
        """保证 (id,branch) 至少有一条版本行；无则把主表当前行灌为 v1（幂等）。

        ⚠️ 必须在主表被修改**之前**调用 —— 否则「初始版本」会捕获新值而非原值。
        """
        if self.scalar("SELECT COUNT(*) FROM entity_versions WHERE id=? AND branch=?",
                       (entity_id, branch)):
            return False
        row = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (entity_id, branch))
        if not row:
            return False
        vf = row.get("valid_from") or row.get("created_at") or self._vnow()
        self._insert_version(entity_id, branch, 1, vf, None, 1, "init", "")
        return True

    def _append_version_snapshot(self, entity_id: str, branch: str, change_kind: str,
                                 changed_by: str = "") -> int:
        """主表已就地变更后，补落一条版本行（退役旧版本 + 追加新版本）。

        供 update_with_history 与不经过它的状态流转路径（review/category）复用。
        返回新版本 version_no；实体在该分支不存在时返回 0。
        """
        if not self.one("SELECT 1 FROM entities WHERE id=? AND branch=?", (entity_id, branch)):
            return 0
        self._ensure_initial_version(entity_id, branch)
        now = self._vnow()
        self.execute(
            "UPDATE entity_versions SET valid_to=?, is_current=0, tx_to=? "
            "WHERE id=? AND branch=? AND is_current=1",
            (now, now, entity_id, branch))
        vno = self.scalar(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM entity_versions "
            "WHERE id=? AND branch=?", (entity_id, branch))
        self._insert_version(entity_id, branch, vno, now, None, 1, change_kind, changed_by)
        return vno

    def update_with_history(self, entity_id: str, branch: str, new_data: dict,
                            changed_by: str = "", change_kind: str = "update") -> int:
        """带历史版本的更新（P0-1 落地：影子历史表 entity_versions）。

        ① 确保初始版本行存在（必须在主表被改前）
        → ② **主表原地更新**（entities 语义不变 = 仅当前行，320 处引用不受影响）
        → ③ 退役旧版本 + 追加新版本行（valid_from=now, is_current=1）
        返回新版本 version_no；实体在该分支不存在时返回 0。

        依赖：entity_versions 由 init_db 的 _migrate_entity_temporal 建表（未建则抛错，
        不静默降级 —— 避免重演「历史记录空转」）。
        """
        row = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (entity_id, branch))
        if not row:
            return 0
        self._ensure_initial_version(entity_id, branch)
        self.execute(
            "UPDATE entities SET name=?, entity_type=?, properties=?, knowledge_category=? "
            "WHERE id=? AND branch=?",
            (new_data.get("name", row["name"]),
             new_data.get("entity_type", row["entity_type"]),
             new_data.get("properties", row["properties"]),
             new_data.get("knowledge_category", row["knowledge_category"] or ""),
             entity_id, branch))
        return self._append_version_snapshot(entity_id, branch, change_kind, changed_by)

    def _annotate_dup_clusters(self, rows: list) -> list:
        """P0-B 实体审核队列重复簇聚合：同名同类型实体标记同簇成员（含非 deprecated 全状态）。

        为每条候选附加：
        - dup_count：同簇成员总数（>1 表示存在重复）
        - dup_ids：同簇全部成员 id（含自身）
        - dup_keep_id：keep 建议（reviewed 优先，否则簇内第一个）
        """
        if not rows:
            return rows
        all_ents = self.rows(
            "SELECT id, name, entity_type, status FROM entities WHERE status!='deprecated'")
        by_key: dict = {}
        for e in all_ents:
            key = (str(e["name"] or "").strip(), e["entity_type"])
            by_key.setdefault(key, []).append(e)
        for r in rows:
            key = (str(r["name"] or "").strip(), r["entity_type"])
            grp = by_key.get(key, [])
            r["dup_count"] = len(grp)
            r["dup_ids"] = [e["id"] for e in grp]
            keep = next((e["id"] for e in grp if e["status"] == "reviewed"), None)
            r["dup_keep_id"] = keep if keep else (grp[0]["id"] if grp else "")
        return rows

    def get_entity(self, entity_id: str, branch: str | None = None) -> dict | None:
        """按 id 查实体（版本化：同 id 可在多分支各有版本行）。

        - branch 指定 → 精确查该分支版本
        - branch 缺省 → 返回该 id 的代表行（release 优先，其次最早创建）
        """
        if branch:
            return self.one("SELECT * FROM entities WHERE id=? AND branch=?", (entity_id, branch))
        return self.one(
            "SELECT * FROM entities WHERE id=? "
            "ORDER BY (branch='release') DESC, created_at LIMIT 1", (entity_id,))

    def list_relations_of(self, entity_id: str) -> list:
        # 版本化后同一逻辑 id 可有多个分支版本行，JOIN 会产生重复行 → GROUP BY r.id 去重
        return self.rows(
            "SELECT r.*, MAX(e1.name) as source_name, MAX(e2.name) as target_name FROM relations r "
            "JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
            "WHERE r.source_id=? OR r.target_id=? GROUP BY r.id",
            (entity_id, entity_id),
        )

    def create_entity(self, entity_id: str, name: str, entity_type: str, properties: str, branch: str,
                      knowledge_category: str = "", source_doc: str = "",
                      source_type: str = "manual", created_by: str = "system",
                      status: str = "candidate", project_id: str | None = None) -> None:
        """F6：默认创建人由硬编码人名「王工」改为中性「system」，真实身份由路由层透传 current_user。

        2026-09-10 状态机收口：status 默认 candidate（抽取/AI 写入走审核队列）；
        图库内手动创建（GraphStore.create_node）显式传 status='reviewed' —— 创建即确认，入图即已审核。
        """
        if project_id is None:
            from repositories.project_repo import resolve_project_id
            project_id = resolve_project_id(self.conn)
        self.execute(
            "INSERT INTO entities (id, name, entity_type, properties, status, branch, source_type, source_doc, created_by, reviewed_by, reviewed_at, knowledge_category, project_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,?)",
            (entity_id, name, entity_type, properties, status, branch, source_type, source_doc,
             created_by, created_by if status == "reviewed" else "", knowledge_category or "",
             project_id),
        )
        # P0-1：创建即写首版本 v1（幂等）；不吞异常 —— 宁可报错也不静默无历史
        self._ensure_initial_version(entity_id, branch)

    def update_entity(self, entity_id: str, name: str, entity_type: str, properties: str,
                      branch: str = "dev", knowledge_category: str = "",
                      changed_by: str = "") -> dict:
        """更新实体（版本化 fork 语义）：优先更新指定分支版本行。

        - (id, branch) 行存在 → **委托 update_with_history**（P0-1：主表原地更新 + 落版本行，
          两处写路径合一，避免「一条写路径有历史、另一条没有」）
        - 仅其他分支有该 id → fork：复制现有版本行到目标分支再应用新值
          （支撑「dev 修改已发布实体」：release 保留旧值，dev 产生新版本）
        - id 完全不存在 → 返回错误（由路由层转 404/400）
        """
        row = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (entity_id, branch))
        if row:
            self.update_with_history(
                entity_id, branch,
                {"name": name, "entity_type": entity_type, "properties": properties,
                 "knowledge_category": knowledge_category or row["knowledge_category"] or ""},
                changed_by=changed_by, change_kind="update")
            return {"ok": True, "forked": False}
        src = self.one(
            "SELECT * FROM entities WHERE id=? "
            "ORDER BY (branch='release') DESC, created_at LIMIT 1", (entity_id,))
        if not src:
            return {"ok": False, "error": f"实体 {entity_id} 不存在"}
        self.execute(
            "INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id, "
            "source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at, "
            "graph_source, graph_x, graph_y, sysml_import_id, knowledge_category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entity_id, name, entity_type, properties, src["status"], branch, src["project_id"],
             src["source_doc"], src["source_type"], src["confidence"], src["created_by"],
             src["reviewed_by"], src["created_at"], src["reviewed_at"], src["graph_source"],
             src["graph_x"], src["graph_y"], src["sysml_import_id"],
             knowledge_category or src["knowledge_category"] or ""))
        # P0-1：fork 出的分支版本同样落 v1，保证该分支的 /history 也可追溯
        self._ensure_initial_version(entity_id, branch)
        return {"ok": True, "forked": True}

    def set_entity_category(self, entity_id: str, category: str, branch: str = "dev") -> int:
        """P0-3：给实体打知识类别标签（按全局 id 更新所有分支版本行，返回影响行数）。

        用原生 conn.execute 取 rowcount（BaseRepo.execute 返回 lastrowid or rowcount，
        对 UPDATE 语义不准确）。
        """
        cur = self.conn.execute(
            "UPDATE entities SET knowledge_category=? WHERE id=?", (category or "", entity_id))
        return cur.rowcount

    def review_entity(self, entity_id: str, action: str, branch: str = "personal",
                      operator: str | None = None) -> None:
        """confirm → reviewed；reject → deprecated。

        标注审核：数据来自上游候选（抽取/SysML建模），审核后进入个人图数据库——
        仅更新 personal 分支版本行（不触碰 dev/release 的建模数据版本）。

        F6：审核人由调用方（路由 current_user / 批量审核 actor）透传，缺省中性「system」。
        """
        actor = operator or "system"
        if action == "confirm":
            self.execute(
                "UPDATE entities SET status='reviewed', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND branch=?",
                (actor, entity_id, branch),
            )
        elif action == "reject":
            self.execute("UPDATE entities SET status='deprecated' WHERE id=? AND branch=?",
                         (entity_id, branch))
        else:
            return
        # P0-1：状态流转同样落版本行（change_kind='review'），使「谁在何时把谁改为 reviewed/deprecated」可回溯
        self._append_version_snapshot(entity_id, branch, "review", actor)
        self._commit_review(entity_id, action, is_entity=True, operator=actor)

    def review_relation(self, relation_id: int, action: str, branch: str = "personal",
                        operator: str | None = None) -> None:
        """关系审核：confirm → reviewed（审核人/时间留痕）；reject → deprecated（限定 personal 分支）。

        F6：审核人由调用方透传，缺省中性「system」。
        """
        actor = operator or "system"
        if action == "confirm":
            self.execute(
                "UPDATE relations SET status='reviewed', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND branch=?",
                (actor, relation_id, branch),
            )
        elif action == "reject":
            self.execute("UPDATE relations SET status='deprecated' WHERE id=? AND branch=?",
                         (relation_id, branch))
        else:
            return
        self._commit_review(relation_id, action, is_entity=False, operator=actor)

    def _commit_review(self, obj_id, action: str, is_entity: bool,
                       operator: str = "system") -> None:
        """分支版本管理：审核动作后打点（kind=review，branch 取被审核对象分支）。
        失败仅打印、不阻断业务；跟随调用方请求事务统一提交。"""
        try:
            from repositories.commit_repo import CommitRepo
            if is_entity:
                row = self.one(
                    "SELECT name, branch, status FROM entities WHERE id=? "
                    "ORDER BY (branch='release') DESC, created_at LIMIT 1", (obj_id,))
                if not row:
                    return
                label = f"实体 {row['name'] or obj_id}"
                changes = {"entities": [obj_id]}
                snapshot = {"id": obj_id, "name": row["name"], "status": row["status"]}
            else:
                row = self.one(
                    "SELECT source_id, target_id, status, branch FROM relations WHERE id=?",
                    (obj_id,))
                if not row:
                    return
                label = f"关系 {row['source_id']}→{row['target_id']}"
                changes = {"relations": [obj_id]}
                snapshot = {"id": obj_id, "source_id": row["source_id"],
                            "target_id": row["target_id"], "status": row["status"]}
            verdict = "通过" if action == "confirm" else ("驳回" if action == "reject" else action)
            CommitRepo(self.conn).create_commit(
                row["branch"] or "dev", "review",
                f"审核：{label}（{verdict}）", changes, snapshot, actor=operator)
        except Exception as e:
            print(f"[commit_repo] review 提交打点失败（不阻断业务）: {e}")

    def count_by_status(self, status: str, branch: str = "") -> int:
        if branch:  # KB分支隔离：统计当前工作分支
            return self.count("entities", "status=? AND branch=?", (status, branch))
        return self.count("entities", "status=?", (status,))

    def count_graph_entities(self, branch: str, project_id: str | None = None, status: str | None = None) -> int:
        """图谱实体总数（与 graph_entities 相同的 WHERE 条件，不含 LIMIT，用于大图上限提示条）。"""
        q = "SELECT COUNT(*) AS n FROM entities WHERE status!='deprecated' AND branch=?"
        params = [branch]
        if status and status != "all":
            q += " AND status=?"
            params.append(status)
        if project_id:
            q += " AND project_id=?"
            params.append(project_id)
        r = self.rows(q, params)
        return r[0]["n"] if r else 0



    # ── relations / graph ──
    def graph_entities(self, branch: str, project_id: str | None = None, status: str | None = None) -> list:
        # 2026-09-12：口径统一为本分支（原 IN (?, 'release') 叠加发布基线导致分支列表数字 26 vs 图谱 80 不一致）
        q = "SELECT id, name, entity_type, status, branch, graph_x, graph_y, graph_source, properties, source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at FROM entities WHERE status NOT IN ('deprecated','raw_chunk') AND branch=?"
        params = [branch]
        if status == "active":  # P0-②：草稿+已发布聚合，满足「审核通过后图谱即见」
            q += " AND status IN ('candidate','reviewed')"
        elif status and status != "all":  # 大图按状态分页加载：reviewed | candidate | all（避免一次性全量）
            q += " AND status=?"
            params.append(status)
        elif status == "all":
            # 2026-09-10 状态机收口：all = 图库可见态（candidate+reviewed），raw_chunk 是抽取管道中间态、不属于图库
            q += " AND status IN ('candidate','reviewed')"
        if project_id:  # P0-1: 项目上下文隔离
            q += " AND project_id=?"
            params.append(project_id)
        q += " LIMIT 200"
        rows = self.rows(q, params)
        # 版本化去重：同一逻辑 id 在 当前分支+release 各有版本行时，仅展示当前分支版本（最新）
        seen = {}
        for r in rows:
            if r["id"] not in seen or r["branch"] == branch:
                seen[r["id"]] = r
        return list(seen.values())

    def graph_relations(self, branch: str, project_id: str | None = None, status: str = "reviewed") -> list:
        # 实体版本化后 JOIN 会产生重复行 → 改 Python 映射端点名（名字取自去重后的实体）
        # 2026-09-10 状态机收口：图库关系默认仅 reviewed（候选关系在审核队列确认后入图）
        q = "SELECT r.* FROM relations r WHERE r.status=? AND r.branch=?"
        params = [status, branch]
        if project_id:  # P0-1: 项目上下文隔离
            q += " AND r.project_id=?"
            params.append(project_id)
        q += " LIMIT 300"
        rows = self.rows(q, params)
        ents = {e["id"]: e["name"] for e in self.graph_entities(branch, project_id)}
        for r in rows:
            r["source_name"] = ents.get(r["source_id"], "")
            r["target_name"] = ents.get(r["target_id"], "")
        return rows

    def count_relations(self, branch: str = "") -> int:
        if branch:  # KB分支隔离：统计当前工作分支
            return self.count("relations", "branch=?", (branch,))
        return self.count("relations")

    def count_relations_by_status(self, status: str, branch: str = "") -> int:
        """P0-A：按审核状态统计关系数（关系审核队列待办计数；branch 非空时按分支隔离）。"""
        if branch:
            return self.scalar("SELECT COUNT(*) FROM relations WHERE status=? AND branch=?",
                               (status, branch))
        return self.scalar("SELECT COUNT(*) FROM relations WHERE status=?", (status,))

    def list_relations_for_review(self, status: str = "candidate", limit: int = 100,
                                  offset: int = 0, branch: str = "") -> list:
        """P0-A：关系审核队列列表——按状态查询 + Python 映射端点名/端点状态（对齐 graph_relations 做法）。

        P0-B 扩展：按 (source_id, relation_type, target_id) 聚合重复簇，
        附加 dup_count/dup_ids/dup_keep_id（>1 表示存在重复三元组）。
        branch 非空时列表与重复簇聚合均按分支隔离（KB分支：关系审核队列按当前工作分支加载）。
        """
        q = "SELECT r.* FROM relations r WHERE r.status=?"
        params: list = [status]
        if branch:
            q += " AND r.branch=?"
            params.append(branch)
        q += " ORDER BY r.id DESC LIMIT ? OFFSET ?"
        rows = self.rows(q, params + [limit, offset])
        ids = set()
        for r in rows:
            ids.add(r["source_id"])
            ids.add(r["target_id"])
        ents = {}
        if ids:
            ph = ",".join("?" * len(ids))
            for e in self.rows(f"SELECT id, name, status FROM entities WHERE id IN ({ph})", list(ids)):
                ents.setdefault(e["id"], e)
        for r in rows:
            s, t = ents.get(r["source_id"]) or {}, ents.get(r["target_id"]) or {}
            r["source_name"] = s.get("name", "")
            r["target_name"] = t.get("name", "")
            r["src_status"] = s.get("status", "")
            r["tgt_status"] = t.get("status", "")
        # P0-B 重复簇聚合：同三元组（非 deprecated）分组（与列表同分支隔离）
        agg_q = "SELECT id, source_id, target_id, relation_type, status FROM relations WHERE status!='deprecated'"
        agg_params: list = []
        if branch:
            agg_q += " AND branch=?"
            agg_params.append(branch)
        all_rels = self.rows(agg_q, agg_params)
        rel_groups: dict = {}
        for x in all_rels:
            key = (x["source_id"], x["relation_type"], x["target_id"])
            rel_groups.setdefault(key, []).append(x)
        for r in rows:
            key = (r["source_id"], r["relation_type"], r["target_id"])
            grp = rel_groups.get(key, [])
            r["dup_count"] = len(grp)
            r["dup_ids"] = [x["id"] for x in grp]
            keep = next((x["id"] for x in grp if x["status"] == "reviewed"), None)
            r["dup_keep_id"] = keep if keep else (grp[0]["id"] if grp else "")
        return rows

    def create_relation(self, source_id: str, target_id: str, relation_type: str,
                        props: str, branch: str = "dev", source_doc: str = "",
                        created_by: str = "", project_id: str | None = None) -> int:
        # P1-2 归一闸门（2026-09-06）：关系名 canonical 化（core/relmap.py 单点事实来源）。
        # 中文可映射名（包含/满足/…）静默归一为英文标准名落库；废弃名（属于/执行/…）
        # 拒绝创建（与下方引用完整性闸门同风格）。
        from core.relmap import canonical as _rel_canon
        _c = _rel_canon(relation_type)
        if _c is None:
            raise ValueError(
                f"关系创建被拒绝：关系类型 '{relation_type}' 已废弃且无 canonical 映射"
                "（见 core/relmap.py；如需对应语义请改用英文标准关系名）")
        relation_type = _c
        # 引用完整性闸门：源/目标实体必须与本关系同分支存在（2026-09-04 清理 43 条
        # 悬空/跨分支死行后加固；fork/merge/rollback 走各自 SQL 不经此处）
        for side, eid in (("源", source_id), ("目标", target_id)):
            if not self.get_entity(eid, branch):
                raise ValueError(
                    f"关系创建被拒绝：{side}实体 {eid} 在分支 {branch} 不存在"
                    "（禁止创建跨分支/悬空关系）")
        if project_id is None:
            from repositories.project_repo import resolve_project_id
            project_id = resolve_project_id(self.conn)
        return self.execute(
            "INSERT INTO relations (source_id, target_id, relation_type, properties, status, branch, source_doc, created_by, project_id) "
            "VALUES (?,?,?,?, 'reviewed', ?,?,?,?)",
            (source_id, target_id, relation_type, props, branch, source_doc, created_by, project_id),
        )

    def count_documents(self, branch: str = "") -> int:
        """文档总数——**全局资产，不按分支切分**（branch 参数保留接收、不参与过滤）。

        设计依据（同一条声明，四处一致）：
          · `ingest_document(branch='global')` —— 上传即写全局分支（docstring: 文档从分支体系抽离）
          · `database/migrations/documents.py` —— 存量 documents/document_chunks 统一为 'global'
          · `MetaRepo.list_documents_with_meta` —— "branch 参数保留接收但不再过滤——文档全局化"
          · `/api/knowledge/chunks/search` —— "检索不按分支过滤"
        注意与本类 `count_by_status`/`count_relations` 的区别：**实体/关系仍按分支**（图谱按分支），
        只有文档是全局的 —— 故本函数是整个 stats 端点里唯一不该带分支口径的一项。

        历史缺陷：本函数曾按 branch 过滤，而前端 `15-kb.js` 恰会带当前分支调用
        → `/api/knowledge/stats?branch=release` 的 total_docs 恒为 0（不传分支才是真值）。
        """
        return self.count("documents")

    # ── ontology ──
    def list_ontology_types(self) -> list:
        return self.rows("SELECT * FROM ontology_types ORDER BY type_kind, name")
