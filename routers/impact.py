"""变更影响分析 API：分析记录（FR-CIA-3 追溯）+ 沙箱变更模拟（FR-CIA-4 预演）。

- GET    /api/impact/history?q=&limit=&offset=    分析记录列表
- GET    /api/impact/history/{id}                  分析详情（含完整结果快照）
- DELETE /api/impact/history/{id}                  删除记录（仅删除记录，不影响模型与报告）
- POST   /api/impact/simulate                      沙箱预演：scene/自定义图 + changes → before/after/对比
- GET    /api/impact/simulations                    模拟记录列表
- GET    /api/impact/simulations/{id}               模拟详情（双图谱重绘/对比报告）
- DELETE /api/impact/simulations/{id}               删除模拟记录
- GET    /api/impact/scenes                         内置多层级演示场景列表
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import db_session, current_user
from core.audit import audit
from repositories.impact_repo import ImpactRepo
from services.impact_engine import simulate, get_scene, SCENES

router = APIRouter(tags=["变更影响分析"])


@router.get("/api/impact/history")
def list_analyses(q: str = "", limit: int = Query(50, ge=1, le=200),
                  offset: int = Query(0, ge=0), conn=Depends(db_session)):
    """分析记录列表（时间倒序），关键词过滤。"""
    return ImpactRepo(conn).list_analyses(q.strip(), limit, offset)


@router.get("/api/impact/history/{analysis_id}")
def get_analysis(analysis_id: int, conn=Depends(db_session)):
    """分析详情（完整结果快照，供追溯/重绘）。"""
    row = ImpactRepo(conn).get_analysis(analysis_id)
    if not row:
        raise HTTPException(404, "分析记录不存在")
    return row


@router.delete("/api/impact/history/{analysis_id}")
def delete_analysis(analysis_id: int, conn=Depends(db_session), user: dict = Depends(current_user)):
    """删除分析记录（仅删记录，不影响模型与报告）。"""
    if not ImpactRepo(conn).get_analysis(analysis_id):
        raise HTTPException(404, "分析记录不存在")
    ImpactRepo(conn).delete_analysis(analysis_id)
    audit((user or {}).get("name", "王工"), "impact_history_delete",
          f"删除变更影响分析记录 #{analysis_id}", conn=conn)
    return {"ok": True}


# ══════════════ FR-CIA-4：沙箱变更模拟 ══════════════


@router.post("/api/impact/baseline")
def build_impact_baseline(body: dict, conn=Depends(db_session), user: dict = Depends(current_user)):
    """构建真实图谱基线（2026-09-15 补齐"真实模型预演"缺口）。

    body: {change_source: str, depth?: int, direction?: str}
    与 _card_impact 同口径：release 已发布分支依赖网络 + 变更源解析。
    返回 {ok, graph, source, depth, direction}，直接作为 /api/impact/simulate 的
    baseline_graph+source 入参（分析卡「🛠 模拟预演此变更」按钮消费）。
    """
    from services.impact_engine import build_baseline
    body = body or {}
    r = build_baseline(conn, str(body.get("change_source") or ""),
                       int(body.get("depth", 3) or 3),
                       (body.get("direction") or "both") or "both")
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason") or "基线构建失败")
    audit((user or {}).get("name", "王工"), "impact_baseline",
          f"构建真实图谱基线：{r['source'].get('name')}（{len(r['graph'].get('nodes', []))} 节点）", conn=conn)
    return r


@router.post("/api/impact/history/{analysis_id}/link-report")
def link_impact_report(analysis_id: int, body: dict, conn=Depends(db_session),
                       user: dict = Depends(current_user)):
    """报告 ↔ 分析记录双向关联（FR-CIA-3，2026-09-15 补齐）：保存报告后回写 report_id。"""
    report_id = int((body or {}).get("report_id") or 0)
    if not report_id:
        raise HTTPException(400, "report_id 必填")
    if not ImpactRepo(conn).get_analysis(analysis_id):
        raise HTTPException(404, "分析记录不存在")
    ImpactRepo(conn).bind_report(analysis_id, report_id)
    conn.commit()
    audit((user or {}).get("name", "王工"), "impact_report_link",
          f"分析记录 #{analysis_id} 关联报告 #{report_id}", conn=conn)
    return {"ok": True, "analysis_id": analysis_id, "report_id": report_id}


@router.get("/api/impact/scenes")
def list_scenes():
    """内置多层级依赖演示场景（供沙箱预演选择）。"""
    return [{"id": k, "title": v["title"], "source_id": v.get("source_id"),
             "depth": v.get("depth", 5), "direction": v.get("direction", "both"),
             "change_desc": v.get("change_desc", ""),
             "changes": v.get("changes", []),
             "node_count": len((v.get("graph") or {}).get("nodes", [])),
             "edge_count": len((v.get("graph") or {}).get("edges", []))}
            for k, v in SCENES.items()]


@router.post("/api/impact/simulate")
def run_simulation(body: dict, conn=Depends(db_session), user: dict = Depends(current_user)):
    """沙箱预演：入参 {scene 或 (baseline_graph+source), changes, depth, direction}。

    - scene：内置演示场景 ID（GET /api/impact/scenes）；或自定义传 baseline_graph/source。
    - changes：[{op: modify|delete|add, target, new_value, node?, edge?}]。
    返回 {ok, before, after, comparison, id} 并自动落 impact_simulations（FR-CIA-4 追溯）。
    全程不写 entities/relations 正式表。
    """
    scene_id = (body or {}).get("scene", "")
    depth = int((body or {}).get("depth", 5) or 5)
    direction = (body or {}).get("direction", "both") or "both"
    changes = (body or {}).get("changes") or []
    graph = (body or {}).get("baseline_graph")
    source = (body or {}).get("source")
    title = (body or {}).get("title", "")
    # 2026-09-26：经验回流按工程隔离——可选 project_id 透传给 reflow（缺省为空=不臆造归属）
    project_id = (body or {}).get("project_id", "")
    if scene_id:
        sc = get_scene(scene_id)
        if not sc:
            raise HTTPException(400, "场景不存在")
        graph = sc["graph"]
        source = sc["source"]
        if not title:
            title = sc["title"]
        depth = depth or sc.get("depth", 5)
        direction = direction or sc.get("direction", "both")
        if not changes:
            changes = sc.get("changes", [])
    if not graph or not source:
        raise HTTPException(400, "缺少基线图谱或变更源（请传 scene 或 baseline_graph+source）")
    if not isinstance(changes, list) or not changes:
        raise HTTPException(400, "变更操作列表不能为空（changes: [{op,target,...}]）")
    result = simulate(graph, source, changes, depth, direction)
    try:
        sim_id = ImpactRepo(conn).create_simulation(
            title=title, scene=scene_id, baseline_snapshot=graph, changes=changes,
            before_result=result["before"], after_result=result["after"],
            comparison=result["comparison"], created_by=(user or {}).get("name", "王工"))
    except Exception:
        sim_id = 0
    actor = (user or {}).get("name", "王工")
    audit(actor, "impact_simulate", f"沙箱变更模拟：{title or scene_id}", conn=conn)
    # FR-KG-12 补 G13：模拟结论自动回流为 v2g 候选（失败不阻断主流程）
    if sim_id > 0:
        try:
            from knowledge_reflow import reflow_from_impact
            rf = reflow_from_impact(conn, sim_id, changes, result["comparison"], title=title,
                                    project_id=project_id)
            if rf.get("candidates", 0) > 0:
                audit(actor, "reflow_impact",
                      f"变更影响分析回流 v2g 候选 {rf['candidates']} 条（batch {rf.get('batch_id', '')}）",
                      conn=conn)
        except Exception:
            pass
    return {"ok": True, "id": sim_id, "scene": scene_id,
            "before": result["before"], "after": result["after"],
            "comparison": result["comparison"]}


@router.get("/api/impact/simulations")
def list_simulations(q: str = "", limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0), conn=Depends(db_session)):
    """模拟记录列表（时间倒序）。"""
    return ImpactRepo(conn).list_simulations(q.strip(), limit, offset)


@router.get("/api/impact/simulations/{sim_id}")
def get_simulation(sim_id: int, conn=Depends(db_session)):
    """模拟详情（完整 before/after 快照，供双图谱重绘与对比报告）。"""
    row = ImpactRepo(conn).get_simulation(sim_id)
    if not row:
        raise HTTPException(404, "模拟记录不存在")
    return row


@router.delete("/api/impact/simulations/{sim_id}")
def delete_simulation(sim_id: int, conn=Depends(db_session), user: dict = Depends(current_user)):
    """删除模拟记录（仅删记录，不影响模型与报告）。"""
    if not ImpactRepo(conn).get_simulation(sim_id):
        raise HTTPException(404, "模拟记录不存在")
    ImpactRepo(conn).delete_simulation(sim_id)
    audit((user or {}).get("name", "王工"), "impact_sim_delete",
          f"删除变更模拟记录 #{sim_id}", conn=conn)
    return {"ok": True}
