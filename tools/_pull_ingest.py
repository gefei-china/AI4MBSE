# -*- coding: utf-8 -*-
"""智源拉取直通入库：service 编排 + 路由 + 前端按钮/对话流回执卡"""

# ── A. services/knowledge_service.py：content_ingest_commit + zhiyuan_pull_ingest ──
p = 'services/knowledge_service.py'
src = open(p, encoding='utf-8').read()

anchor = '    def project_ingest_logs(self, project_id: str = "", limit: int = 20) -> dict:'
assert anchor in src, 'A0 anchor missing'
new_methods = '''    def content_ingest_commit(self, model_name: str, content: dict, actor: str = "system",
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
        t0_str = (_dt.now() - _timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
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

''' + anchor
src = src.replace(anchor, new_methods, 1)

# 模块级私有 helper（挂在文件顶部 import 区之后的第一个类/函数定义前不可靠 → 文件末尾追加）
src += '''

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
'''
open(p, 'w', encoding='utf-8').write(src)
print('A knowledge_service.py OK')

# ── B. 路由 ──
p = 'routers/knowledge_parts/pipeline.py'
src = open(p, encoding='utf-8').read()
src += '''

@router.post("/api/knowledge/sysml/pull-ingest")
def sysml_pull_ingest(body: dict = None, conn=Depends(db_session), user=Depends(current_user)):
    """从智源拉取建模数据 → 直接转三元组 → 存个人分支图库（2026-09-11 拉取流程）。

    后端编排全程直通（候选化→融合闸→自动批准→落图），不产生数据治理审核面板待办；
    回执由 AI 建模对话流渲染。body: {vc?, package_data_id?, target_branch?}（均可空=默认工程全量）。
    """
    from services.knowledge_service import KnowledgeService
    body = body or {}
    return KnowledgeService(conn).zhiyuan_pull_ingest(
        actor=_actor(user), vc=(body.get("vc") or "").strip(),
        package_data_id=body.get("package_data_id"),
        target_branch=(body.get("target_branch") or "personal"))
'''
open(p, 'w', encoding='utf-8').write(src)
print('B route OK')
