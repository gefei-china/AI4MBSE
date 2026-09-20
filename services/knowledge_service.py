"""知识库域 Service：跨表/跨引擎编排上提（P1-1 层抽象）。

上提点（涉及引擎 + Repository + 审计的多步编排）：
- v2g 半自动转化：extract / confirm / reject（跨 vector2graph 引擎 + 审计）
- 批量审核：batch_review（跨 KnowledgeRepo 循环 + 审计）
薄 CRUD（实体/图谱/本体单接口）留在 router。
"""
import json
import logging

from .base import BaseService

logger = logging.getLogger(__name__)


class KnowledgeService(BaseService):
    """知识库编排：向量→图谱转化闭环 + 批量审核。"""

    def v2g_extract(self, query: str, chunk_ids=None, top_k: int = 5, doc_id=None,
                    actor: str = "system") -> dict:
        """O-1 Step1：按查询命中 chunks 抽取候选实体/关系（LLM 受本体 schema 约束）。

        doc_id 指定时限定在该文档的 chunks 内抽取（上传后自动抽取 → chunk↔entity 溯源指向本文档）。
        """
        from vector2graph import extract_candidates
        result = extract_candidates(self.conn, query, chunk_ids or None,
                                    top_k, doc_id=doc_id)
        from core.audit import audit
        audit(actor, "v2g_extract",
              f"向量转图谱抽取: 候选 {result['node_count'] + result['edge_count']}（batch {result['batch_id']}）",
              conn=self.conn)
        return result

    def v2g_confirm(self, batch_id, selected_ids, actor: str = "system", dup_action: str = "create",
                    triple_only=None) -> dict:
        """O-1 Step2：人工确认候选 → 图谱入库（本体校验）+ chunk↔entity 溯源链接。

        P0-C 消歧前移：确认时对与已有实体重复的节点候选按 dup_action 处理
        （skip 跳过 / align 对齐合并已有实体 / create 强制新建）。
        """
        from vector2graph import confirm_candidates
        result = confirm_candidates(self.conn, batch_id, selected_ids or None,
                                    operator=actor, dup_action=dup_action, triple_only=triple_only)
        # P0 发布门禁：仅 AI 建模批次（sysml_version_id>0）在确认后生成发布合并请求
        result["gate"] = self._ingest_gate_for(batch_id, selected_ids, actor)
        from core.audit import audit
        audit(actor, "v2g_confirm",
              f"向量转图谱确认: 入库 {result['confirmed']} / 拒绝 {len(result['rejected'])}"
              + (f" / 对齐 {len(result.get('aligned', []))}" if result.get('aligned') else "")
              + (f" / 跳过 {len(result.get('skipped', []))}" if result.get('skipped') else "")
              + (f" / 发布门禁 {result['gate'].get('status', 'off')}" if result.get("gate") else ""),
              conn=self.conn)
        return result

    def _ingest_gate_for(self, batch_id, selected_ids, actor: str = "system") -> dict:
        """判断本批次是否为 AI 建模批次并触发发布门禁（非 AI 批次返回 disabled）。"""
        try:
            if not batch_id and not selected_ids:
                return {"enabled": False, "status": "off"}
            if batch_id:
                cond, args = "batch_id=?", (batch_id,)
            else:
                ph = ",".join("?" * len(selected_ids))
                cond, args = f"id IN ({ph})", list(selected_ids)
            # P0-2 回归：R1 统一闸门批次（batch_id LIKE 'SYSM-%'，无 sysml_version_id 关联）
            # 与版本关联候选（sysml_version_id>0）同属 AI 建模通道，均触发发布门禁
            n = self.conn.execute(
                f"SELECT COUNT(*) FROM v2g_candidates WHERE {cond} "
                "AND (sysml_version_id>0 OR batch_id LIKE 'SYSM-%')",
                args).fetchone()[0]
            if not n:
                return {"enabled": False, "status": "off"}
            from services.ingest_gate import submit_ingest_batch
            return submit_ingest_batch(self.conn, batch_id=batch_id or "",
                                       source_type="ai_generated", operator=actor,
                                       detail=f"AI 建模批次 {batch_id or '(勾选)'} 发布")
        except Exception as e:  # noqa: BLE001
            logger.warning("入库发布门禁未触发（向后兼容）: %s", e)
            return {"enabled": False, "status": "off", "error": str(e)}

    def v2g_reject(self, candidate_ids, reason: str = "", actor: str = "system") -> dict:
        """S4：批量驳回候选（人工确认不符合本体/业务要求，标记 rejected 保留审计）。

        reason：驳回原因（治理中心单条/批量驳回留痕）。
        """
        if not candidate_ids:
            return {"rejected": 0}
        if not (reason or "").strip():
            return {"rejected": 0, "error": "驳回必须填写原因（留痕用于追溯）"}
        n = 0
        for cid in candidate_ids:
            cur = self.conn.execute(
                "UPDATE v2g_candidates SET status='rejected', reject_reason=? WHERE id=? AND status='pending'",
                (reason[:200], cid))
            n += cur.rowcount
        self.conn.commit()
        from core.audit import audit
        audit(actor, "v2g_reject", f"向量转图谱批量驳回: {n} 条" + (f"（原因：{reason[:40]}）" if reason else ""),
              conn=self.conn)
        return {"rejected": n}

    def v2g_batches(self) -> list:
        """抽取批次聚合（治理中心批次卡：batch 维度统计 + 来源文档 + 时间）。"""
        from vector2graph import batch_stats
        return batch_stats(self.conn)

    def v2g_update(self, candidate_id: int, name: str, entity_type: str, properties: str,
                   actor: str = "system") -> dict:
        """编辑单条候选（改名称/类型/属性，重新校验与消歧打标，仍为 pending）。"""
        from vector2graph import update_candidate
        result = update_candidate(self.conn, candidate_id, name, entity_type, properties)
        if result.get("ok"):
            from core.audit import audit
            audit(actor, "v2g_update", f"编辑候选 #{candidate_id}: {name}", conn=self.conn)
        return result

    def batch_review_entities(self, entity_ids, action: str, actor: str = "system") -> dict:
        """S4：批量审核实体（批量通过/批量驳回候选实体）。"""
        if not entity_ids:
            return {"ok": False, "reviewed": 0, "error": "entity_ids 为空"}
        from repositories.knowledge_repo import KnowledgeRepo
        repo = self.repo(KnowledgeRepo)
        n = 0
        for eid in entity_ids:
            try:
                repo.review_entity(str(eid), action, operator=actor)
                n += 1
            except Exception as e:
                logger.warning("batch_review 单条失败（跳过）: entity=%s err=%s", eid, e)
        from core.audit import audit
        audit(actor, "entity_batch_review", f"批量审核 {action}: {n} 条", conn=self.conn)
        return {"ok": True, "reviewed": n, "action": action}

    # ═══════════════ Staging 写前融合闸（Task #7） ═══════════════

    def v2g_fuse(self, batch_id: str | None = None, selected_ids: list | None = None,
                 run_quality: bool = True, actor: str = "system") -> dict:
        """写前融合闸：手动触发批次/选中候选融合 + 可选质量闸。

        自动路径已在 confirm_candidates 内置（幂等，log 去重）；
        本方法供治理中心显式触发/重跑，返回完整统计。
        """
        from staging_fuse import fuse_batch, quality_gate
        result = fuse_batch(self.conn, batch_id=batch_id, selected_ids=selected_ids,
                            operator=actor)
        qg = None
        if run_quality and batch_id:
            qg = quality_gate(self.conn, batch_id)
        if result.get("auto_merged") or result.get("review_queue"):
            from core.audit import audit
            audit(actor, "v2g_fuse",
                  f"写前融合闸: 批 {batch_id or '选中'} 自动合并 {result.get('auto_merged', 0)} / "
                  f"人工队列 {result.get('review_queue', 0)}"
                  + (f" / 质量闸 {'通过' if qg and qg['pass'] else '驳回'}" if qg else ""),
                  conn=self.conn)
        if qg is not None:
            result["quality_gate"] = qg
        return result

    def v2g_fuse_status(self, batch_id: str | None = None, limit: int = 50) -> dict:
        """融合闸运行状态（汇总 + 最近审计明细）。"""
        from staging_fuse import fuse_status
        return fuse_status(self.conn, batch_id=batch_id, limit=limit)

    # ═══════════════ 全链路优化：V2 候选提交审核 + 三元组 ═══════════════

    def v2g_submit_review(self, batch_id, selected_ids, actor: str = "system") -> dict:
        """优化一/二：V2 生成的候选「提交审核」。

        将未提交的候选标记 review_status='submitted' + 时间戳（默认候选待审核闸门），
        并同步触发 write_fusion_guard 写前融合闸（聚焦去重+冲突，透明执行）。
        返回 {submitted, auto_merged, review_queue, gate}。
        """
        cands = self._cands_for(batch_id, selected_ids)
        if not cands:
            return {"submitted": 0, "auto_merged": 0, "review_queue": 0, "error": "无待提交候选"}
        submitted = 0
        for c in cands:
            cur = self.conn.execute(
                "UPDATE v2g_candidates SET review_status='submitted', review_submitted_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='pending'", (c["id"],))
            submitted += cur.rowcount
        self.conn.commit()
        # 写前融合闸（透明执行：聚焦批内去重 + 冲突消解）
        fuse = {"auto_merged": 0, "review_queue": 0}
        try:
            from staging_fuse import fuse_batch
            r = fuse_batch(self.conn, batch_id=batch_id or None,
                           selected_ids=selected_ids or None, operator=actor)
            fuse = {"auto_merged": r.get("auto_merged", 0), "review_queue": r.get("review_queue", 0)}
        except Exception as e:  # noqa: BLE001
            logger.warning("submit_review 融合闸异常（不阻断）: %s", e)
        from core.audit import audit
        audit(actor, "v2g_submit_review",
              f"V2 候选提交审核: {submitted} 条（batch {batch_id or '勾选'}）"
              + f" / 自动合并 {fuse['auto_merged']} | 人工队列 {fuse['review_queue']}",
              conn=self.conn)
        return {"submitted": submitted, "auto_merged": fuse["auto_merged"],
                "review_queue": fuse["review_queue"], "ok": submitted > 0}

    def _cands_for(self, batch_id, selected_ids) -> list:
        if selected_ids:
            ph = ",".join("?" * len(selected_ids))
            return self.conn.execute(
                f"SELECT * FROM v2g_candidates WHERE id IN ({ph}) AND status='pending'",
                selected_ids).fetchall()
        if batch_id:
            return self.conn.execute(
                "SELECT * FROM v2g_candidates WHERE batch_id=? AND status='pending'",
                (batch_id,)).fetchall()
        return []

    def triple_review_queue(self, status: str = "pending", limit: int = 200) -> dict:
        from triple_store import review_queue, stats
        return {"items": review_queue(self.conn, status=status, limit=limit),
                "stats": stats(self.conn)}

    def triple_review(self, triple_id: str, decision: str, note: str = "", actor: str = "system") -> dict:
        from triple_store import review_triple
        r = review_triple(self.conn, triple_id, decision, note, actor)
        if r["ok"]:
            from core.audit import audit
            audit(actor, "triple_review", f"三元组审核 {decision}: {triple_id}", conn=self.conn)
        return r

    def triple_commit_graph(self, actor: str = "system") -> dict:
        """三元组唯一图写入源：把已通过(approved)且未落图(graph_stored=0)的三元组反写为 entities/relations。"""
        from triple_commit import commit_approved_triples
        r = commit_approved_triples(self.conn, operator=actor)
        if r.get("entities") or r.get("relations"):
            from core.audit import audit
            audit(actor, "triple_commit_graph",
                  f"三元组落图: 实体 {r.get('entities', 0)} / 关系 {r.get('relations', 0)}", conn=self.conn)
        return r

    def triple_batch_review(self, triple_ids: list, decision: str, note: str = "", actor: str = "system") -> dict:
        from triple_store import batch_review
        r = batch_review(self.conn, triple_ids, decision, note, actor)
        if r["reviewed"]:
            from core.audit import audit
            audit(actor, "triple_batch_review", f"三元组批量审核 {decision}: {r['reviewed']}", conn=self.conn)
        return r

    def ontology_readiness(self) -> dict:
        """本体前置检查：AI 建模/抽取前是否已定义必要类型。返回 {ready, missing}。

        2026-09-02 消费治理：以最新已发布版本（active 快照）为准；无已发布回退当前表（冷启动）。
        """
        data = self._get_active_ontology()
        types = data["types"]
        entities = [r["name"] for r in types if r["type_kind"] == "entity"]
        relations = [r["name"] for r in types if r["type_kind"] == "relation"]
        ready = bool(entities) and bool(relations)
        out = {"ready": ready, "entity_types": entities, "relation_types": relations,
               "missing": {"entity": not entities, "relation": not relations}}
        if data.get("version"):
            out["ontology_version"] = data["version"]
            out["compatible"] = data.get("compatible")
        return out

    def ontology_schema_summary(self) -> dict:
        """本体 Schema 摘要（供前端建模前预览）。以最新已发布版本（active 快照）为准。"""
        data = self._get_active_ontology()
        by_type = {"entity": [], "relation": [], "attribute": []}
        for r in data["types"]:
            by_type.setdefault(r["type_kind"], []).append(dict(r))
        return {"types": by_type,
                "counts": {k: len(v) for k, v in by_type.items()},
                "ontology_version": data.get("version"),
                "source": data.get("source"),
                "compatible": data.get("compatible")}

    def _get_active_ontology(self) -> dict:
        """消费侧统一读取入口（2026-09-02 P0）：最新已发布快照（active）→ 无发布/无快照回退当前 ontology_types。

        - source=snapshot：读 ontology_version_snapshots（不可变版本工件）
        - source=live：无已发布版本，或 active 版本无快照（快照治理上线前的存量 released，避免消费空数据）→ 读当前表
        """
        v = self.conn.execute(
            "SELECT * FROM ontology_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
        if v:
            rows = self.conn.execute(
                "SELECT id, type_id, name, type_kind, parent_id, properties, constraints, "
                "description, icon, color FROM ontology_version_snapshots WHERE version_id=?",
                (v["id"],)).fetchall()
            if rows:
                return {"source": "snapshot", "version": v["version_label"],
                        "version_id": v["id"], "compatible": v["compatible"],
                        "types": [dict(r) for r in rows]}
        rows = self.conn.execute(
            "SELECT id, name, type_kind, parent_id, properties, constraints, description, icon, color "
            "FROM ontology_types").fetchall()
        return {"source": "live", "version": None, "version_id": None,
                "compatible": None, "types": [dict(r) for r in rows]}

    def v2g_fuse_config(self, enabled=None, auto_threshold=None, review_threshold=None,
                        quality_gate=None, weights=None, actor: str = "system") -> dict:
        """读写融合闸配置（阈值/权重入 settings 表，修 C7 硬编码）。"""
        from staging_fuse import set_fuse_config
        return set_fuse_config(self.conn, enabled=enabled, auto_threshold=auto_threshold,
                               review_threshold=review_threshold, quality_gate=quality_gate,
                               weights=weights, operator=actor)

    # ═══════════════ 写后调和 Nightly Job（诊断 §4.2 第 5 点） ═══════════════

    def knowledge_reconcile(self, actor: str = "system") -> dict:
        """写后调和作业（降级为定时任务，由外部 cron/平台调度触发）。

        与写前融合闸互补：融合闸管「入库前」批内去重，本作业管「入库后」
        跨批次/全图消歧与冲突检测。三引擎均已 Blocking 化（精确键/canopy 分桶），
        万级实体安全。返回各引擎统计。
        """
        from entity_resolver import run_rule_preprocess, conflict_detect
        from core.audit import audit
        rule = run_rule_preprocess(self.conn, threshold=0.62)
        conflict = conflict_detect(self.conn)
        audit(actor, "knowledge_reconcile",
              f"写后调和作业: 规则预处理 {rule.get('input_n', 0)} 入参 / "
              f"自动对齐 {rule.get('auto_aligned', 0)} / 候选 {rule.get('candidates', 0)}；"
              f"冲突 {conflict.get('detected', 0)}",
              conn=self.conn)
        return {"ok": True, "rule_preprocess": rule, "conflict_detect": conflict,
                "job": "knowledge_reconcile"}

    # ═══════════ 工程维度入库（工程归档 → 三元组 → 个人分支图库，2026-09-09） ═══════════

    def _project_versions(self, project_id: str) -> list:
        """工程关联的全部 SysML 版本（经 conversations.project_id 聚合，版本号升序）。"""
        return self.conn.execute(
            "SELECT v.id, v.version_label, v.status, v.adopted, v.content, v.imported_batch "
            "FROM sysml_versions v JOIN conversations c ON c.id = v.conversation_id "
            "WHERE c.project_id = ? ORDER BY v.id", (project_id,)).fetchall()

    def project_ingest_preview(self, project_id: str, target_branch: str = "personal") -> dict:
        """工程入库预览（只读）：聚合版本 → 解析视图统计 → 与目标分支图库重合预估。不写任何表。"""
        import json as _json
        proj = self.conn.execute("SELECT id, name FROM projects WHERE id=?",
                                 (project_id,)).fetchone()
        if not proj:
            return {"error": f"工程不存在: {project_id}"}
        versions = self._project_versions(project_id)
        names, ent_cnt, rel_cnt, attr_cnt, per_ver, failed = set(), 0, 0, 0, [], 0
        for v in versions:
            try:
                content = _json.loads(v["content"] or "{}")
                vn, ve, va = set(), 0, 0
                for vd in (content.get("views") or {}).values():
                    if not isinstance(vd, dict):
                        continue
                    for n in (vd.get("nodes") or []):
                        nm = str((n.get("name") if isinstance(n, dict) else "") or "").strip()
                        if not nm:
                            continue
                        vn.add(nm)
                        props = n.get("properties") if isinstance(n.get("properties"), dict) else {}
                        va += len(props)
                    ve += len(vd.get("edges") or [])
                ent_cnt += len(vn)
                rel_cnt += ve
                attr_cnt += va
                names |= vn
                per_ver.append({"id": v["id"], "label": v["version_label"], "status": v["status"],
                                "entities": len(vn), "relations": ve, "attributes": va})
            except Exception:
                failed += 1
                per_ver.append({"id": v["id"], "label": v["version_label"], "status": v["status"],
                                "error": "content 解析失败"})
        exist = {r["name"] for r in self.conn.execute(
            "SELECT name FROM entities WHERE branch=?", (target_branch,)).fetchall()}
        overlap = len(names & exist) if names else 0
        return {"project_id": project_id, "project_name": proj["name"],
                "target_branch": target_branch,
                "versions": per_ver, "failed_versions": failed,
                "stats": {"versions": len(versions), "unique_entities": len(names),
                          "entity_instances": ent_cnt, "relations": rel_cnt,
                          "attributes": attr_cnt, "overlap_with_branch": overlap,
                          "estimated_new": max(0, len(names) - overlap)}}

    def project_ingest_commit(self, project_id: str, actor: str = "system",
                              target_branch: str = "personal", version_ids=None) -> dict:
        """工程入库提交：逐版本 候选化 → 提交审核(含融合闸) → 确认物化入图库(个人分支)
        → 三元组随确认生成 → knowledge_commits 打点（confirm_candidates 内建）→ 批次记录。

        幂等：已 committed 版本重复入库由 dup_action=align 合并到已有实体，不产生重复节点。
        """
        import json as _json
        import time as _time
        import uuid as _uuid
        from sysml_importer import sysml_to_candidates
        from vector2graph import confirm_candidates
        from core.audit import audit
        t0 = _time.time()
        proj = self.conn.execute("SELECT id, name FROM projects WHERE id=?",
                                 (project_id,)).fetchone()
        if not proj:
            return {"error": f"工程不存在: {project_id}"}
        versions = self._project_versions(project_id)
        if version_ids:
            want = set(version_ids)
            versions = [v for v in versions if v["id"] in want]
        if not versions:
            return {"error": "该工程没有可入库的 SysML 版本"}
        batch_id = f"PINGEST-{_uuid.uuid4().hex[:8]}"
        self.conn.execute(
            "INSERT INTO project_ingest_logs (batch_id, project_id, project_name, target_branch, "
            "version_ids, stats_json, status, operator) VALUES (?,?,?,?,?,'{}','running',?)",
            (batch_id, project_id, proj["name"], target_branch,
             _json.dumps([v["id"] for v in versions]), actor))
        self.conn.commit()
        t_before = self.conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        stats = {"versions_total": len(versions), "versions_failed": 0,
                 "candidates": 0, "rejected": 0, "triples_staged": 0,
                 "entities_staged": 0, "relations_staged": 0, "auto_merged": 0,
                 "review_queue": 0, "triples_approved": 0,
                 "entities_written": 0, "relations_written": 0}
        ver_details, errors = [], []
        ok_cnt = 0
        for v in versions:
            detail = {"id": v["id"], "label": v["version_label"], "ok": False}
            try:
                content = _json.loads(v["content"] or "{}")
                st = sysml_to_candidates(self.conn, content, version_id=v["id"],
                                         model_name=f"工程入库·{proj['name']} {v['version_label']}",
                                         source="json")
                if st.get("error"):
                    raise RuntimeError(st["error"])
                stats["candidates"] += (st.get("node_count") or 0) + (st.get("edge_count") or 0)
                stats["rejected"] += len(st.get("rejected") or [])
                sr = self.v2g_submit_review(st["batch_id"], [], actor=actor)
                stats["auto_merged"] += sr.get("auto_merged") or 0
                stats["review_queue"] += sr.get("review_queue") or 0
                cf = confirm_candidates(self.conn, st["batch_id"], None,
                                        operator=actor, dup_action="align")
                # triple_only 架构：confirm 仅生成待审三元组（confirmed 字段 = 暂存三元组数）
                if cf.get("triple_only"):
                    _sg = cf.get("staged") or {}
                    stats["triples_staged"] += cf.get("confirmed") or 0
                    stats["entities_staged"] += _sg.get("entities") or 0
                    stats["relations_staged"] += _sg.get("relations") or 0
                else:
                    stats["triples_staged"] += 0
                detail.update(ok=True, batch=st["batch_id"],
                              candidates=(st.get("node_count") or 0) + (st.get("edge_count") or 0),
                              staged=cf.get("confirmed") or 0)
                ok_cnt += 1
            except Exception as e:  # noqa: BLE001
                stats["versions_failed"] += 1
                detail["error"] = str(e)[:200]
                errors.append(f"版本 {v['version_label']}: {e}")
            ver_details.append(detail)
        # 工程入库 = 用户已在向导拍板 → 本工程版本的待审三元组自动批准 → 落个人分支图库
        vids = [v["id"] for v in versions]
        if vids:
            ph = ",".join("?" * len(vids))
            cur = self.conn.execute(
                f"UPDATE triples SET status='approved', review_decision='approve', "
                f"reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP "
                f"WHERE sysml_version_id IN ({ph}) AND status='pending'",
                [actor] + vids)
            stats["triples_approved"] = cur.rowcount
            self.conn.commit()
        from triple_commit import commit_approved_triples
        wr = commit_approved_triples(self.conn, operator=actor)
        stats["entities_written"] = wr.get("entities") or 0
        stats["relations_written"] = wr.get("relations") or 0
        t_after = self.conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        stats["triples_total"] = t_after
        stats["elapsed_ms"] = int((_time.time() - t0) * 1000)
        status = "success" if ok_cnt == len(versions) else ("failed" if ok_cnt == 0 else "partial")
        self.conn.execute(
            "UPDATE project_ingest_logs SET stats_json=?, status=?, error_msg=?, finished_at=CURRENT_TIMESTAMP "
            "WHERE batch_id=?",
            (_json.dumps(stats, ensure_ascii=False), status,
             "；".join(errors)[:500], batch_id))
        self.conn.commit()
        audit(actor, "project_ingest",
              f"工程入库 {proj['name']}: 版本{ok_cnt}/{len(versions)} 三元组+{stats['triples_approved']} "
              f"落图 实体{stats['entities_written']}/关系{stats['relations_written']} → 分支 {target_branch}",
              conn=self.conn)
        return {"ok": status != "failed", "batch_id": batch_id, "status": status,
                "project_name": proj["name"], "target_branch": target_branch,
                "stats": stats, "versions": ver_details, "errors": errors}

    def content_ingest_commit(self, model_name: str, content: dict, actor: str = "system",
                              target_branch: str = "personal", version_id: int = 0) -> dict:
        """单份模型内容直通入库（2026-09-11 拉取流程）：候选化 → 融合闸 → 确认生成待审三元组
        → 自动批准 → 落个人分支图库。对话流内回执，不产生数据治理审核面板待办
        （用户在 AI 建模对话流发起拉取 = 拍板动作本身）。"""
        import time as _time
        from datetime import datetime as _dt, timedelta as _timedelta
        from sysml_importer import sysml_to_candidates
        from vector2graph import confirm_candidates
        from core.audit import audit
        t0 = _time.time()
        t0_str = (_dt.utcnow() - _timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")  # SQLite CURRENT_TIMESTAMP=UTC
        stats = {"candidates": 0, "rejected": 0, "triples_staged": 0, "auto_merged": 0,
                 "review_queue": 0, "triples_approved": 0, "entities_written": 0,
                 "relations_written": 0}
        st = sysml_to_candidates(self.conn, content, version_id=version_id,
                                 model_name=model_name, source="json")
        if st.get("error"):
            return {"error": st["error"]}
        stats["candidates"] = (st.get("node_count") or 0) + (st.get("edge_count") or 0)
        stats["rejected"] = len(st.get("rejected") or [])
        sr = self.v2g_submit_review(st["batch_id"], [], actor=actor)
        stats["auto_merged"] = sr.get("auto_merged") or 0
        stats["review_queue"] = sr.get("review_queue") or 0
        cf = confirm_candidates(self.conn, st["batch_id"], None,
                                operator=actor, dup_action="align")
        stats["triples_staged"] = cf.get("confirmed") or 0
        # 对话流发起 = 拍板 → 本批待审三元组自动批准（时间窗选择，不触碰存量 pending）
        cur = self.conn.execute(
            "UPDATE triples SET status='approved', review_decision='approve', "
            "reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP "
            "WHERE status='pending' AND created_at >= ?", [actor, t0_str])
        stats["triples_approved"] = cur.rowcount
        self.conn.commit()
        from triple_commit import commit_approved_triples
        wr = commit_approved_triples(self.conn, operator=actor)
        stats["entities_written"] = wr.get("entities") or 0
        stats["relations_written"] = wr.get("relations") or 0
        stats["elapsed_ms"] = int((_time.time() - t0) * 1000)
        audit(actor, "content_ingest",
              f"直通入库 {model_name}: 候选{stats['candidates']} 三元组+{stats['triples_approved']} "
              f"落图 实体{stats['entities_written']}/关系{stats['relations_written']} → {target_branch}",
              conn=self.conn)
        return {"ok": True, "batch_id": st["batch_id"], "status": "success",
                "model_name": model_name, "target_branch": target_branch, "stats": stats}

    def zhiyuan_pull_ingest(self, actor: str = "system", vc: str = "",
                            package_data_id=None, target_branch: str = "personal") -> dict:
        """从智源拉取建模数据 → 解析 → 直接转三元组 → 存个人分支图库（2026-09-11 拉取流程）。

        全程后端编排、对话流内回执：project_list(默认工程) → sysmlv2_gen 导出文本
        → sysml_ast.parse_strict 解析（OMG 官方解析器）→ content_ingest_commit（候选化→融合闸→自动批准→落图）。
        不产生数据治理审核面板待办——拉取动作即对话流内的拍板。
        """
        import json as _json
        from core.audit import audit
        try:
            from zhiyuan_client import ZhiyuanClient, _load_config
        except Exception as e:
            return {"error": f"智源客户端不可用: {e}"}
        cfg = _load_config()
        if not isinstance(cfg, dict) or not (cfg.get("base_url") or "").strip():
            return {"error": "智源连接未配置（ZHIYUAN_BASE_URL 缺失）"}
        client = ZhiyuanClient(base_url=cfg.get("base_url", ""), token=cfg.get("token", ""),
                               headers=cfg.get("headers"), timeout=int(cfg.get("timeout") or 15))
        # 1) vc 解析：显式 > default_vc > project_list 首个
        if not vc:
            vc = (cfg.get("default_vc") or "").strip()
        if not vc:
            try:
                pl = client.project_list("")
            except Exception as e:
                return {"error": f"智源工程列表查询失败: {e}"}
            vc = _zhiyuan_first_vc(pl)
            if not vc:
                return {"error": "智源工程列表为空或无法解析 vc（响应: "
                                 + _json.dumps(pl, ensure_ascii=False)[:200] + "）"}
        # 2) 导出建模数据文本
        try:
            gen = client.sysmlv2_gen(vc, package_data_id)
        except Exception as e:
            return {"error": f"智源导出失败（vc={vc}）: {e}"}
        text = _zhiyuan_extract_text(gen)
        if not text or not text.strip():
            return {"error": "智源未返回 SysML v2 文本（响应: "
                             + _json.dumps(gen, ensure_ascii=False)[:200] + "）"}
        # 3) 解析 → 内容形态
        # 2026-09-20：`sysml_importer.parse_text`（手写扫描器）已删除，唯一实现 = OMG 官方解析器。
        from sysml_ast import parse_strict
        try:
            parsed = parse_strict(text)
        except RuntimeError as e:
            return {"error": f"解析失败: {e}"}
        content = parsed if parsed.get("views") else {"views": {"BDD": parsed}}
        # 4) 直通入库
        model_name = f"智源拉取·vc={vc}" + (f"·包{package_data_id}" if package_data_id else "·全工程")
        r = self.content_ingest_commit(model_name, content, actor=actor, target_branch=target_branch)
        if r.get("error"):
            return r
        r.update({"vc": vc, "package_data_id": package_data_id,
                  "text_chars": len(text), "text_preview": text[:400]})
        audit(actor, "zhiyuan_pull_ingest",
              f"智源拉取入库 vc={vc}: 候选{r['stats'].get('candidates')} "
              f"三元组+{r['stats'].get('triples_approved')} 落图 "
              f"实体{r['stats'].get('entities_written')} → {target_branch}", conn=self.conn)
        return r

    def project_ingest_logs(self, project_id: str = "", limit: int = 20) -> dict:
        """工程入库历史（最新在前），stats_json 反序列化供前端直接渲染。"""
        import json as _json
        if project_id:
            rows = self.conn.execute(
                "SELECT * FROM project_ingest_logs WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM project_ingest_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["stats"] = _json.loads(d.pop("stats_json") or "{}")
            except Exception:
                d["stats"] = {}
            d["version_ids"] = _json.loads(d.get("version_ids") or "[]")
            out.append(d)
        return {"logs": out}


def _zhiyuan_first_vc(j, depth: int = 0) -> str:
    """防御式从智源 project_list 响应中提取第一个可用 vc。"""
    if depth > 6:
        return ""
    if isinstance(j, dict):
        for k in ("vc", "branchId", "branch_id", "versionContext"):
            v = j.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for k in ("data", "result", "projects", "list", "rows", "items"):
            if k in j:
                got = _zhiyuan_first_vc(j[k], depth + 1)
                if got:
                    return got
        for v in j.values():
            got = _zhiyuan_first_vc(v, depth + 1)
            if got:
                return got
    if isinstance(j, list):
        for it in j:
            got = _zhiyuan_first_vc(it, depth + 1)
            if got:
                return got
    return ""


def _zhiyuan_extract_text(j, best: str = "", depth: int = 0) -> str:
    """防御式从智源 sysmlv2/gen 响应中提取 SysML v2 文本（取最长且像模型源码的字符串）。"""
    if depth > 8:
        return best
    if isinstance(j, str):
        if len(j) > len(best) and ("package " in j or "part " in j or "def " in j or "item " in j):
            best = j
        return best
    if isinstance(j, dict):
        for k in ("text", "sysml", "content", "code", "source", "data", "result"):
            if k in j:
                best = _zhiyuan_extract_text(j[k], best, depth + 1)
        for v in j.values():
            best = _zhiyuan_extract_text(v, best, depth + 1)
    if isinstance(j, list):
        for it in j:
            best = _zhiyuan_extract_text(it, best, depth + 1)
    return best
