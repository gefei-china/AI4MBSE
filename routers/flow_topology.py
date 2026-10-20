# -*- coding: utf-8 -*-
"""P2-4 多画布流拓扑台账 API：画布结构体检 + 同构去重（只记录，不改拓扑）。

## 产品口径（2026-10-18 自进化调研路线图 P2 第四项）
路线图原文是「多画布流拓扑实验」。落地前勘察真实库，两个事实改写了口径：

① **运行证据不足以支撑拓扑实验** —— agent_flows 62 条定义vs flow_runs
   仅 11 条 / flow_run_steps 33 条，其中 9 条是同日三个同构画布各跑 3 次
   （总延迟 49–82ms，秒级），只有 run 198 是真实长时运行（94.8s / 6 步 /
   1 个 error）。在 11 条运行记录上比较两种拓扑的优劣，得到的是噪声。
   **实验的前提是先有可比较的台账**，故本期落台账与体检，不落实验。

② **同一条路线图的安全红线 #6 写着「不碰权重、不碰架构拓扑」** ——
   自动生成新拓扑并自动切换生效画布，直接违反这条自己定的红线。

**铁律：只读 agent_flows、只记录缺陷、只报同构，绝不写生效拓扑。**
拓扑台账是体检镜不是手（同 P1-4「配置回滚过人」、P2-3「档案库是记忆不是手」）。
scan 全链路没有任何 UPDATE / DELETE agent_flows 的语句（门禁 E 段静态断言）。

## 本项发现的真实问题
62 条画布里 **25 条是 (3 节点, 2 边) 完全同构**，且都命名为
「需求分析流水线<13 位时间戳>」、source=planner_auto —— 自动编排在反复
生成同一个拓扑。画布数在涨，可辨识的拓扑没有涨。这是「画布数量」这个
指标永远发现不了、只有结构台账能看见的问题。

## P2-1 教训的沿用（端点失败必须显式）
监控/提案类端点曾因采集异常裸奔 500（冷库冒烟实测）—— 本路由所有端点
失败一律 ok:False + error 可见，不 500、不静默。

端点：
    GET  /api/flow-topology                体检总览 + 台账 + 同构组
    POST /api/flow-topology/scan扫描画布 → 收敛台账（幂等）
    POST /api/flow-topology/refresh       重算运行统计（不新建行）
    GET  /api/flow-topology/isomorphic    同构冗余组清单（只报结构同构）
"""
from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from core.audit import audit, audit_user
from core.deps import current_user, db_session

router = APIRouter(tags=["画布拓扑体检"])


def _disabled_scan_payload() -> dict:
    return {"ok": True, "enabled": False, "variants": [], "groups": [],
            "overview": None, "written": {"variants_written": 0,
                                           "variants_created": 0,
                                           "variants_refreshed": 0,
                                           "groups": 0}}


#: 缺陷类别 → 人可读建议动作。**只给建议，不代改拓扑**（红线 #6）。
_DEFECT_ADVICE = {
    "dangling_edge": "检查画布里被删掉的节点，边没跟着清干净——执行期会断链",
    "unresolvable_type": "节点引用的 tool/skill/agent 不存在，换成现存目标或补上该资产",
    "isolated_node": "节点无入无出永远不会被执行，接到链上或删掉",
    "no_sink": "画布没有汇点，执行不会收敛，补一条终止路径",
    "unreachable_branch": "这段分支从任何起点都走不到，检查上游边的 source/target 是否写反",
    "linear_degenerate": "纯串行链没有并行度，独立无依赖的节点可以并行以缩短耗时",
}


def _advice(defect: str) -> str:
    return _DEFECT_ADVICE.get(defect, "结构可疑，人工核对该节点定义")


@router.get("/api/flow-topology")
def flow_topology_overview(evidence_state: str = "", fingerprint: str = "",
                           limit: int = 200, conn=Depends(db_session)):
    """画布结构体检总览 + 台账列表。

    overview.summary.verdict 三态：clean / defective / insufficient_data。
    **分母不足即insufficient_data，不回落0**（P2-1/P2-2 同型坑：
    没跑过不是跑了零次，前者无信息、后者是坏消息）。
    """
    from core import flow_topology as _ft
    from repositories.flow_topology_repo import FlowTopologyRepo
    try:
        st = _ft.get_settings()
        if not st.get("enabled", True):
            return {"ok": True, "enabled": False, "overview": None,
                    "variants": [], "groups": [], "settings": st}
        repo = FlowTopologyRepo(conn)
        variants = repo.list_variants(evidence_state=evidence_state,
                                      fingerprint=fingerprint, limit=limit)
        groups = _ft.group_isomorphic(variants)
        return {"ok": True, "enabled": True,
                "settings": st,
                "overview": _ft.summarize(
                    variants, groups,
                    min_denominator=st["min_denominator"]),
                "variants": variants,
                "groups": groups,
                "evidence_counts": repo.count_by_evidence(),
                "defective_canvases": repo.count_defective(),
                "rules": [{"name": r.name, "label": r.label, "desc": r.desc,
                           "severity": _ft.DEFECT_SEVERITY.get(r.name, "warn")}
                          for r in sorted(_ft.DEFECT_RULES.values(),
                                          key=lambda x: x.name)]}
    except Exception as e:                                   # noqa: BLE001
        # P2-1 教训（冷库冒烟实测 500）：读端点失败必须显式
        return {"ok": False,
                "error": "画布拓扑体检读取失败: %s: %s" % (type(e).__name__, e)}


@router.post("/api/flow-topology/scan")
def scan_flow_topology(conn=Depends(db_session), user=Depends(current_user)):
    """扫描全部画布 → 体检 + 收敛台账（幂等：同一 flow_id 只刷新不新增）。

    只读 agent_flows / flow_runs + 只写自家一张表——**绝不改画布拓扑、
    不删画布、不动 flow_runs**。
    """
    from core import flow_topology as _ft
    try:
        st = _ft.get_settings()
        if not st.get("enabled", True):
            return _disabled_scan_payload()
        result = _ft.scan(conn, settings=st)
        written = _ft.record_scan(conn, result)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "画布拓扑扫描失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    audit(user, "flow_topology_scan",
          "扫描画布 %d 幅：新入册 %d，刷新 %d，同构组 %d"
          % (result["overview"].get("canvases", 0),
             written.get("variants_created", 0),
             written.get("variants_refreshed", 0),
             written.get("groups", 0)), conn=conn)
    return {"ok": True,
            "enabled": True,
            "settings": result.get("settings"),
            "overview": result.get("overview"),
            "groups": result.get("groups"),
            "variants": result.get("variants"),
            "written": written}


@router.post("/api/flow-topology/refresh")
def refresh_flow_topology(conn=Depends(db_session), user=Depends(current_user)):
    """重算已入册台账的运行统计（runs / failed_runs / evidence_state）。

    画布跑过之后要把「无运行证据」升为「有运行证据」——这是**刷新**，
    不是新变体（同 P2-3「指纹不含内容否则台账注水」的同源纪律）。
    """
    from core import flow_topology as _ft
    try:
        st = _ft.get_settings()
        if not st.get("enabled", True):
            return {"ok": True, "enabled": False, "refreshed": 0}
        result = _ft.scan(conn, settings=st)
        written = _ft.record_scan(conn, result)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "画布拓扑复算失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    return {"ok": True, "enabled": True, **written,
            "overview": result.get("overview")}


@router.get("/api/flow-topology/isomorphic")
def isomorphic_groups(limit: int = 50, conn=Depends(db_session)):
    """同构冗余组清单：**只报结构完全同构**（指纹相同），不报「相似」。

    相似度需要阈值，阈值是拍出来的，拍出来的相似度不作数
    （同 P2-3「不猜」纪律的延伸）。
    """
    from core import flow_topology as _ft
    from repositories.flow_topology_repo import FlowTopologyRepo
    try:
        st = _ft.get_settings()
        if not st.get("enabled", True):
            return {"ok": True, "enabled": False, "groups": []}
        repo = FlowTopologyRepo(conn)
        lim = max(1, min(int(limit or 50), 500))
        variants = repo.list_variants(limit=5000)
        groups = _ft.group_isomorphic(variants)
        return {"ok": True, "enabled": True, "groups": groups[:lim],
                "total_groups": len(groups),
                "duplicate_canvases": sum(g["size"] - 1 for g in groups)}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False,
                "error": "同构分组读取失败: %s: %s" % (type(e).__name__, e)}


@router.post("/api/flow-topology/{flow_id}/defects")
def explain_flow_defects(flow_id: int, body: dict = Body(default={}),
                         conn=Depends(db_session),
                         user=Depends(current_user)):
    """对单幅画布重跑体检并给出人可读解释（不写库）。

    这是「体检结论怎么变成人能看懂的话」那一层：把 blocker/warn 分类
    翻成一句建议动作，但**只给建议，不代改拓扑**（红线 #6）。
    """
    from core import flow_topology as _ft
    from repositories.flow_topology_repo import FlowTopologyRepo
    try:
        row = conn.execute(
            "SELECT id,name,version,status,source,nodes,edges "
            "FROM agent_flows WHERE id=?", (int(flow_id),)).fetchone()
        if not row:
            return JSONResponse({"ok": False,
                                 "error": "画布 #%s 不存在" % flow_id}, 404)
        flow = _ft.parse_flow({"id": row[0], "name": row[1], "version": row[2],
                               "status": row[3], "source": row[4],
                               "nodes": row[5], "edges": row[6]})
        st = _ft.get_settings()
        flow["_available"] = _ft.collect_available_refs(conn)
        flow["_linear_floor"] = st.get("linear_degenerate_min", 4)
        defects = _ft.detect_defects(flow)
        repo = FlowTopologyRepo(conn)
        registered = repo.get_variant(int(flow_id))
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "画布体检失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    audit(user, "flow_topology_defects",
          "查看画布 #%s（%s）结构缺陷：%d blocker / %d warn"
          % (flow_id, row[1],
             sum(1 for d in defects if d["severity"] == "blocker"),
             sum(1 for d in defects if d["severity"] == "warn")), conn=conn)
    return {"ok": True, "flow_id": flow_id, "flow_name": row[1],
            "fingerprint": _ft.topology_fingerprint(flow),
            "defects": defects,
            "blocker_count": sum(1 for d in defects if d["severity"] == "blocker"),
            "warn_count": sum(1 for d in defects if d["severity"] == "warn"),
            "advice": [_advice(d["defect"]) for d in defects
                       if d["severity"] == "blocker"][:5],
            "registered": bool(registered)}