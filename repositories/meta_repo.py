"""元数据域 Repository：audit_logs / data_sources / settings / documents 表。

对应 routers/meta.py 的全部数据访问。
"""
from repositories.base import BaseRepo


class MetaRepo(BaseRepo):
    """审计/数据源/设置/文档 数据访问。"""

    # ── audit_logs ──
    def list_audit(self, limit: int = 100, event_type: str | None = None,
                   search: str | None = None, branch: str | None = None) -> list:
        q = "SELECT * FROM audit_logs WHERE 1=1"
        params = []
        if event_type:
            q += " AND event_type=?"
            params.append(event_type)
        if search:
            q += " AND (user_name LIKE ? OR detail LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        if branch:
            # 分支视角：只看该分支的域事件（带 branch 归属的记录）；全局事件（登录/LLM 等）不混入
            q += " AND branch=?"
            params.append(branch)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self.rows(q, params)

    def count_audit_today(self, event_type: str) -> int:
        return self.count("audit_logs", "event_type=? AND date(created_at)=date('now')", (event_type,))

    def count_audit_blocked(self) -> int:
        return self.count("audit_logs", "result='blocked'")

    # ── data_sources：已随 R1=B 数据集成移除（2026-09-01，表已删）──

    # ── settings ──
    def get_settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.rows("SELECT * FROM settings")}

    def upsert_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, value),
        )

    # ── documents ──
    def create_document(self, filename: str, file_type: str, file_size: int, uploaded_by: str = "王工") -> int:
        return self.execute(
            "INSERT INTO documents (filename, file_type, file_size, parse_status, uploaded_by) VALUES (?,?,?,?,?)",
            (filename, file_type, file_size, "pending", uploaded_by),
        )

    def update_document_parse(self, doc_id: int, parse_status: str, chunk_count: int = 0) -> None:
        self.execute(
            "UPDATE documents SET parse_status=?, chunk_count=? WHERE id=?",
            (parse_status, chunk_count, doc_id),
        )

    def list_documents(self) -> list:
        return self.rows("SELECT * FROM documents ORDER BY created_at DESC")

    # ── KB-P0：文档元数据 + 追溯视图（文档为全局资产：不按分支过滤，全分支共享）──
    def list_documents_with_meta(self, search: str = "", status: str = "", branch: str = "",
                                 uploaded_by: str = "", file_type: str = "",
                                 date_from: str = "", date_to: str = "",
                                 lifecycle_status: str = "", include_deprecated: bool = True,
                                 state: str = "", origin: str = "") -> list:
        q = """SELECT d.*, m.title, m.author, m.version, m.tags, m.source AS meta_source, m.extra
            FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id WHERE 1=1"""
        params = []
        # 注意：branch 参数保留接收但不再过滤——文档全局化后不分分支（避免历史前端传 branch）
        if search:
            q += " AND (d.filename LIKE ? OR m.title LIKE ? OR m.author LIKE ? OR m.tags LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like, like])
        # 统一状态过滤（2026-09-10 米爸：解析×生命周期合一为单一「状态」下拉，列与筛选项完全一致）
        # committed/stored/deprecated/archived → lifecycle 直匹配；
        # processing → 上传/解析中（lifecycle IN uploaded,processing 或 parse_status=parsing）；
        # failed → parse_status=failed
        if state == "failed":
            q += " AND d.parse_status='failed'"
        elif state == "processing":
            q += " AND (d.lifecycle_status IN ('uploaded','processing') OR d.parse_status='parsing')"
        elif state:
            q += " AND d.lifecycle_status=?"
            params.append(state)
        if status:
            q += " AND d.parse_status=?"
            params.append(status)
        # P0 生命周期过滤（默认包含已废弃，避免误删历史可见性；提供 include_deprecated=False 隐藏）
        if lifecycle_status:
            q += " AND d.lifecycle_status=?"
            params.append(lifecycle_status)
        elif not include_deprecated:
            q += " AND d.lifecycle_status != 'deprecated'"
        if uploaded_by:  # 上传人筛选
            q += " AND d.uploaded_by=?"
            params.append(uploaded_by)
        if origin:  # 2026-09-15 来源筛选（upload | ai_generated）
            q += " AND d.origin=?"
            params.append(origin)
        if file_type:  # 文件格式筛选（忽略大小写）
            q += " AND LOWER(d.file_type)=LOWER(?)"
            params.append(file_type)
        if date_from:  # 上传时间范围（日期格式 YYYY-MM-DD）
            q += " AND date(d.created_at) >= date(?)"
            params.append(date_from)
        if date_to:
            q += " AND date(d.created_at) <= date(?)"
            params.append(date_to)
        q += " ORDER BY d.created_at DESC"
        return self.rows(q, params)

    def get_document_detail(self, doc_id: int) -> dict | None:
        row = self.one("""
            SELECT d.*, m.title, m.author, m.version, m.tags, m.source AS meta_source, m.extra
            FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id WHERE d.id=?""", (doc_id,))
        if not row:
            return None
        row["chunks"] = self.rows(
            "SELECT id, chunk_index, content, section, source_doc, embed_version, bm25_text, linked_entity_ids "
            "FROM document_chunks WHERE document_id=? ORDER BY chunk_index LIMIT 200", (doc_id,))
        # 关联实体：基于 chunk↔entity 溯源链接（S1 修复：不依赖 source_doc 匹配）
        import json as _json
        eids = set()
        for c in row["chunks"]:
            try:
                ids = _json.loads(c.get("linked_entity_ids") or "[]")
            except Exception:
                ids = []
            eids.update(ids)
        if eids:
            ph = ",".join("?" * len(eids))
            row["linked_entities"] = self.rows(
                f"SELECT id, name, entity_type, status FROM entities WHERE id IN ({ph}) "
                f"AND status != 'deprecated' LIMIT 50", list(eids))
        else:
            # 兜底：仍按 source_doc 匹配（旧数据兼容）
            row["linked_entities"] = self.rows(
                "SELECT id, name, entity_type, status FROM entities WHERE source_doc=? "
                "AND status != 'deprecated' LIMIT 50", (row.get("filename", ""),))
        return row

    def update_doc_metadata(self, doc_id: int, title: str, author: str, version: str,
                            tags: str, extra: str) -> None:
        self.execute(
            "INSERT INTO doc_metadata (document_id, title, author, version, tags, source, extra) "
            "VALUES (?,?,?,?,?, 'upload', ?) "
            "ON CONFLICT(document_id) DO UPDATE SET title=excluded.title, author=excluded.author, "
            "version=excluded.version, tags=excluded.tags, extra=excluded.extra",
            (doc_id, title, author, version, tags, extra),
        )

    def delete_document(self, doc_id: int) -> None:
        """删除文档。

        ⚠️ `domain_review_queue.document_id` 是**无外键**的普通列
        （建表见 `database/migrations/glossary.py::_migrate_glossary_tables`），
        不随 `documents` 级联删除 —— 只删 documents 会留下"指向已删文档"的孤儿行，
        而 `dashboard_repo` 会把它们计入看板「知识评审待办」→ **计数虚高**
        （2026-09-19 实测：显示 52 / 真实 38）。

        所以这里必须**显式清理**该文档在复核队列里的行（与删除同事务）。
        兜底见 `database/migrations/glossary.py::_migrate_domain_review_queue_orphans`。
        """
        self.execute("DELETE FROM domain_review_queue WHERE document_id=?", (doc_id,))
        self.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    # ── P0：文档生命周期管理（FR-KG-8 / ArcR-5）──
    def get_document_for_lifecycle(self, doc_id: int) -> dict | None:
        """取文档用于生命周期变更前校验（含 lifecycle_status）。"""
        return self.one(
            "SELECT id, filename, lifecycle_status, deprecated_at, archived_at, lifecycle_version "
            "FROM documents WHERE id=?", (doc_id,))

    def transition_lifecycle(self, doc_id: int, from_status: str, to_status: str,
                              operator: str, reason: str = "", extra: dict | None = None,
                              chunk_sync: bool = False) -> dict:
        """事务内完成：状态机转移 + 审计日志 + 可选 chunks 同步。

        from_status 校验：若当前状态不等于 from_status，返回 {ok:False, reason:"concurrent_change"}；
        chunk_sync=True 时同步 document_chunks.lifecycle_status='deprecated'（避免向量层命中）。
        """
        cur = self.get_document_for_lifecycle(doc_id)
        if not cur:
            return {"ok": False, "reason": "not_found"}
        if cur["lifecycle_status"] != from_status:
            return {"ok": False, "reason": f"状态不符：期望 {from_status}，实际 {cur['lifecycle_status']}"}
        import json as _json
        if to_status == "deprecated":
            self.execute(
                "UPDATE documents SET lifecycle_status=?, deprecated_at=CURRENT_TIMESTAMP, "
                "deprecated_by=?, deprecate_reason=?, lifecycle_version=lifecycle_version+1 "
                "WHERE id=?", (to_status, operator, reason[:500] if reason else "", doc_id))
            if chunk_sync:
                self.execute(
                    "UPDATE document_chunks SET lifecycle_status='deprecated' WHERE document_id=?",
                    (doc_id,))
        elif to_status == "archived":
            self.execute(
                "UPDATE documents SET lifecycle_status=?, archived_at=CURRENT_TIMESTAMP, "
                "archived_by=?, lifecycle_version=lifecycle_version+1 "
                "WHERE id=?", (to_status, operator, doc_id))
        elif to_status == "committed" and from_status == "deprecated":
            # 撤销废弃 → 回到 committed（清空 deprecated_* 字段）
            self.execute(
                "UPDATE documents SET lifecycle_status='committed', "
                "deprecated_at='', deprecated_by='', deprecate_reason='', "
                "lifecycle_version=lifecycle_version+1 "
                "WHERE id=?", (doc_id,))
            if chunk_sync:
                self.execute(
                    "UPDATE document_chunks SET lifecycle_status='stored' WHERE document_id=?",
                    (doc_id,))
            to_status = "committed (restored)"
        elif to_status == "committed":
            # 人工确认入库（stored → committed，2026-09-10）：不清 deprecated_*（本就为空）
            self.execute(
                "UPDATE documents SET lifecycle_status='committed', "
                "lifecycle_version=lifecycle_version+1 WHERE id=?", (doc_id,))
        elif to_status == "stored":
            self.execute(
                "UPDATE documents SET lifecycle_status='stored', "
                "lifecycle_version=lifecycle_version+1 WHERE id=?", (doc_id,))
        else:
            self.execute(
                "UPDATE documents SET lifecycle_status=?, lifecycle_version=lifecycle_version+1 "
                "WHERE id=?", (to_status, doc_id))
        self.execute(
            "INSERT INTO document_lifecycle_log (document_id, from_status, to_status, operator, reason, extra) "
            "VALUES (?,?,?,?,?,?)",
            (doc_id, from_status, to_status, operator, reason[:500] if reason else "",
             _json.dumps(extra or {}, ensure_ascii=False)))
        return {"ok": True, "doc_id": doc_id, "from": from_status, "to": to_status}

    def get_lifecycle_log(self, doc_id: int, limit: int = 50) -> list:
        return self.rows(
            "SELECT id, from_status, to_status, operator, reason, extra, created_at "
            "FROM document_lifecycle_log WHERE document_id=? ORDER BY id DESC LIMIT ?",
            (doc_id, limit))

    def batch_transition(self, doc_ids: list[int], to_status: str, operator: str,
                          reason: str = "", allow_from: list[str] | None = None) -> dict:
        """批量状态机转移（废弃 / 归档 / 入库）。每个文档独立校验，任一失败不影响其他。

        allow_from：限定允许的来源状态（如人工入库只允许 stored → committed）；
        None 表示不限。不符合来源的计入 skipped（带 reason）。
        """
        results = {"ok": 0, "skipped": 0, "failed": []}
        for did in doc_ids:
            cur = self.get_document_for_lifecycle(did)
            if not cur:
                results["failed"].append({"doc_id": did, "reason": "not_found"})
                continue
            if cur["lifecycle_status"] == to_status:
                results["skipped"] += 1
                continue
            if allow_from and cur["lifecycle_status"] not in allow_from:
                results["skipped"] += 1
                results["failed"].append({"doc_id": did,
                                          "reason": f"来源状态不符：{cur['lifecycle_status']}（仅允许 {','.join(allow_from)}）"})
                continue
            r = self.transition_lifecycle(did, cur["lifecycle_status"], to_status,
                                          operator, reason, extra={"batch": True},
                                          chunk_sync=(to_status == "deprecated"))
            if r["ok"]:
                results["ok"] += 1
            else:
                results["failed"].append({"doc_id": did, "reason": r["reason"]})
        return results
