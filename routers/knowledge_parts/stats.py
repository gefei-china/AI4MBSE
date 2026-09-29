# -*- coding: utf-8 -*-
"""知识库路由分片：FTS/统计/生命周期/覆盖度/总览。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。

## P0 口径统一（2026-09-26）

本文件的 `lifecycle` / `coverage` / `overview` 三个端点此前**各自写一份 SQL**，
加上 `/api/knowledge/stats` 带分支过滤，导致看板 `#kb-a` 同一屏三套口径互相打架
（实测：已评审 61 vs 122+61、关系 83 vs 249、已废弃 2 vs 6）。

现在三个端点**全部委派 `metrics_core`**（口径唯一来源），本文件不再写任何统计 SQL。
`/api/knowledge/stats` 保留为**兼容端点**（看板已不再使用，改用 `/api/knowledge/dashboard`）。
"""
from routers.knowledge_parts.shared import *  # noqa: F401,F403

import metrics_core


@router.get("/api/knowledge/fts")
def knowledge_fts(q: str, kinds: str = "", limit: int = 50,
                  conn=Depends(db_session), user=Depends(current_user)):
    """P1-5 全文检索（FTS5 trigram）：跨实体/关系/三元组的子串匹配 + LIKE 兜底。

    kinds 可选逗号分隔子集（如 'entities,relations'）；mode=fts|like（兜底标记）。
    """
    from fts_search import fts_query
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()] or None
    return fts_query(conn, q, kinds=kind_list, limit=max(1, min(limit, 200)))


@router.get("/api/knowledge/stats")
def knowledge_stats(branch: str = "", conn=Depends(db_session)):
    """⚠️ 兼容端点（2026-09-26 起**知识看板不再使用**，新逻辑见 `/api/knowledge/dashboard`）。

    保留原语义，供旧调用方与测试使用；**请勿用于跨面板数字比较**——本端点三项指标口径不同：
    - `reviewed` / `candidate` / `raw_chunk` / `deprecated`：**分支口径**（带 branch 过滤）
    - `total_relations`：**分支口径**
    - `total_docs`：**全局口径**（文档不按分支切分，见 `KnowledgeRepo.count_documents`）

    实体/关系按分支、文档全局 → 在本端点上混排必然与全局口径的 overview/lifecycle 不等。
    """
    repo = KnowledgeRepo(conn)
    stats = {}
    for s in ["reviewed", "candidate", "raw_chunk", "deprecated"]:
        stats[s] = repo.count_by_status(s, branch)
    stats["total_relations"] = repo.count_relations(branch)
    stats["total_docs"] = repo.count_documents(branch)
    return stats


@router.get("/api/knowledge/lifecycle")
def knowledge_lifecycle(conn=Depends(db_session)):
    """P0-4 生命周期分布（FR-KG-8）——**口径唯一实现在 `metrics_core.lifecycle`**。

    scope=global（不分分支）：published 以「release 分支 + published_at 非空」判定。
    2026-09-26 起本端点只做委派（此前与 overview 各写一份 SQL，是同屏数字打架的根源之一）。
    """
    return metrics_core.lifecycle(conn)


@router.get("/api/knowledge/coverage")
def knowledge_coverage(conn=Depends(db_session)):
    """FR-KG-13 补 G6：知识完整度——**口径唯一实现在 `metrics_core.coverage`**。

    scope=global。P0 修 C4：类型覆盖率改为**两级分母**
    - `type_coverage`（兼容旧字段名）＝ **SysML v2 元素类型**口径（14 类，来自
      `sysml_ast.NODE_KINDS`），实测 2/14≈14% —— 旧口径 26/28=93% 是假绿
      （分母被 25 个领域业务类型撑大，且指标饱和、丧失指导增量抽取的区分度）
    - `type_coverage_sysml` / `type_coverage_domain`：两级明细，供看板分级展示
    P0 修 C5：`fallback_topics` 已滤除系统提示词与通用词噪声（详见 metrics_core）。

    返回保证**不引发 KeyError**：`type_coverage` 三键（total_types/covered_types/
    coverage_rate/empty_types）保持向下兼容。
    """
    return metrics_core.coverage(conn)


@router.get("/api/knowledge/overview")
def knowledge_overview(conn=Depends(db_session)):
    """FR-KG-8/11 补 G5：知识库总览一次聚合（kb-a 首屏）——委派 `metrics_core.overview`。

    scope=global。返回 kpi / review_queue / source_dist / lifecycle / graph_thumb，
    其中 lifecycle 复用同一函数（避免两处统计漂移）。
    """
    return metrics_core.overview(conn)