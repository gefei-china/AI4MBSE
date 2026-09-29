# -*- coding: utf-8 -*-
"""知识看板指标口径唯一来源（P0 口径统一，2026-09-26）。

## 为什么单独成模块

`#kb-a` 此前由前端拼三个端点 —— `/api/knowledge/stats`（带分支）+ `overview`（全局）
+ `lifecycle`（全局）—— 同一屏三套口径互相打架。2026-09-26 实测：

    stats?branch=personal → 已评审 61 / 关系 83 / 已废弃 2
    lifecycle(全局)       → 已评审(未发布)122 + 已发布 61 / 已废弃 6
    overview(全局)        → 实体 183 / 关系 249

**不是 bug，是两套作用域被摆在同一屏却都没标注。** 本模块把看板需要的全部聚合收口到
一处；各端点与 `/api/knowledge/dashboard` 一律委派到这里，保证
「同一 scope 下同一指标在任何位置都相等」。

## 作用域（scope）三值，看板上每个指标必须显式带其一

- `release`  权威基线：`branch_type='release'` 的分支。**AI 建模消费侧默认只查它**
  （agent/rag.py `_release_branches`），因此它决定「AI 看到的知识有多准」。
- `branch`   工作分支（personal/dev）：知识工程师作业面，决定「今天该审什么」。
- `global`   全局资产：documents / document_chunks **不按分支切分**
  （见 KnowledgeRepo.count_documents 的四处一致声明），故选其口径恒为全局。

⚠️ 纪律：**禁止在别处再写一遍本模块的 SQL**。新增看板指标必须先在此定义口径，
再经 `/api/knowledge/dashboard` 暴露；前端只渲染不计算。
"""
import json
from functools import lru_cache

# 阈值三色（ok/warn/alert）唯一实现：直接复用治理模块，禁止第二份
from governance import _status  # noqa: F401

# 无 release 分支时的历史兼容回退名（与 agent/rag.py::_release_branches 同口径）
RELEASE_FALLBACK = "release"

# ── C5 修复：fallback 主题词的噪声过滤 ──
# 实测污染样本（2026-09-26）：sysml 553 / v2 264 / bdd 81 / 块定义图 74 / 代码 55 /
#   禁止虚构 50 / 交付规范 48 / 团队目标 48 / 任务上下文快照 47
#
# **根因**（已取证）：`query_routing_stats` 表**没有区分"用户提问"与"系统内部编排查询"的列**，
# 而 agent pipeline 会把内部任务上下文当检索 query 打库，例如：
#   `[任务上下文快照]\n- 任务定义：key=t2 / title=梳理气动/结构/…全链路设计原理…`
#   `先做需求分析，再生成方案设计\n子任务：方案设计\n任务上下文（只读）：…\n期望输出：…`
# 这些不是"知识缺口主题"，而是内部编排流量 → **整条 query 丢弃**（不是只丢词）。
_ORCHESTRATION_MARKERS = (
    "[任务上下文快照]", "[任务定义]", "任务上下文快照", "任务上下文（只读）",
    "子任务：", "期望输出：", "期望产出：", "上游交付物摘要", "输出需自包含",
    "待确认项需显式标注",
)

# 词级噪声（子串命中即滤：正则按最大连续串切词，"任务上下文快照" 需被 "任务上下文" 覆盖）
_INTERNAL_PROMPT_NOISE = (
    "禁止虚构", "交付规范", "团队目标", "任务上下文", "期望输出", "子任务",
    "只读", "请简要", "请说明", "简要说明", "生成方案", "先做需求分析",
    "基于需求", "上下文", "回答", "提问", "输出格式", "要求如下", "任务定义",
    "任务", "风险", "命名规范", "本项目", "一句话",
)
_GENERIC_TERM_NOISE = (
    "sysml", "kerml", "mbse", "bdd", "ibd", "uc", "act", "seq", "stm", "par",
    "v1", "v2", "v3", "代码", "块定义图", "内部块图", "用例图", "活动图",
    "模型", "系统", "数据", "文档", "知识", "生成", "设计", "分析",
    "介绍", "说明", "描述", "方法", "内容", "相关", "包括", "哪些", "通常",
    "视图", "元素", "关系", "实体", "功能", "需求", "过程", "步骤", "示例",
    "key", "title", "def", "port", "true", "false", "none", "null", "output", "input",
    "omg", "rdf", "owl", "xmi", "api", "json", "sql", "sdk", "llm",
)

# 语法碎片：以虚词/助词/标点结尾或开头的 token 不是业务主题
# （实测残留："规范里" / "的写法" / "问题：" —— 都是被标点切断的语法片段）
_NOISE_TAIL = tuple("的了里中之与及为是在有不就还者和呢吧吗啊哦嗯、，。；：（）()")

# 中文/英文停用词（保留原 shared._FALLBACK_STOP_WORDS 的全部条目，向后兼容）
STOP_WORDS = {
    "的", "了", "是", "和", "与", "在", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这", "那", "一个", "这个", "那个", "什么", "怎么", "如何", "为什么", "请", "帮我", "帮",
    "一下", "有", "没有", "吗", "呢", "吧", "啊", "哦", "嗯", "还", "就", "都", "很", "也",
    "及", "或", "等", "对", "从", "向", "把", "被", "让", "要", "会", "能", "可以", "不能",
    "检索", "查询", "搜索", "知识", "文档", "库",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "of", "for", "to", "in",
    "on", "at", "with", "by", "from", "what", "how", "why", "when", "where", "which", "who",
    "please", "help", "me", "my", "you", "your", "it", "this", "that", "and", "or", "not",
    "do", "does", "did", "can", "could", "should", "would", "about", "into", "over", "under",
    "between", "i", "we", "they", "he", "she",
}


@lru_cache(maxsize=1)
def sysml_element_types() -> tuple:
    """SysML v2 元素类型全集（规范约束源口径的分母）。

    口径来源：`sysml_ast.NODE_KINDS` —— 本系统认可「会被接入图谱」的 OMG 元类
    （28 个元类归并为 14 个中文实体类型）。**不用 ontology_types 表做分母**：
    那张表里 28 个 entity 类型中 25 个是领域业务类型（转发器/天线分系统/…），
    拿它当分母会把 SysML 元素类型覆盖率"撑"成 93% 的假绿（实测旧口径 26/28）。
    """
    from sysml_ast import NODE_KINDS
    return tuple(sorted({v[0] for v in NODE_KINDS.values()}))


def _ph(n: int) -> str:
    return ",".join("?" * n)


def release_branches(conn) -> list:
    """已发布分支名列表（与 agent/rag.py / view_generator.py 同口径）。"""
    try:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM branches WHERE branch_type='release' ORDER BY id").fetchall()]
    except Exception:
        names = []
    return names or [RELEASE_FALLBACK]


# ────────────────────────────── 分支作用域（scope=branch） ──────────────────────────────

def entity_counts(conn, branch: str = "") -> dict:
    """指定分支（缺省=全部分支）的实体状态分布。"""
    where, params = ("branch=?", [branch]) if branch else ("1=1", [])
    out = {r["status"]: r["n"] for r in conn.execute(
        f"SELECT status, COUNT(*) n FROM entities WHERE {where} GROUP BY status", params)}
    return {"reviewed": out.get("reviewed", 0), "candidate": out.get("candidate", 0),
            "raw_chunk": out.get("raw_chunk", 0), "deprecated": out.get("deprecated", 0),
            "total": sum(out.values())}


def relation_count(conn, branch: str = "") -> int:
    if branch:
        return conn.execute("SELECT COUNT(*) FROM relations WHERE branch=?", (branch,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]


def relation_counts_by_status(conn, branch: str = "") -> dict:
    where, params = ("branch=?", [branch]) if branch else ("1=1", [])
    out = {r["status"]: r["n"] for r in conn.execute(
        f"SELECT status, COUNT(*) n FROM relations WHERE {where} GROUP BY status", params)}
    return {"reviewed": out.get("reviewed", 0), "candidate": out.get("candidate", 0),
            "deprecated": out.get("deprecated", 0), "total": sum(out.values())}


def branch_drift(conn, branch: str) -> dict:
    """跨分支漂移：同一逻辑 id 在 release 与工作分支上的存在性差异。

    这是看板此前完全没有、但解释力最强的一项——它直接量化
    「工作分支上有多少知识还没进权威基线（=AI 还看不到）」，也是
    「61 vs 183」这类困惑的正确表达方式。
    """
    rel = release_branches(conn)
    if not branch or branch in rel:
        return {"comparable": False, "reason": "当前分支即权威基线（release），无漂移可比"}
    ph = _ph(len(rel))
    r_ids = {r["id"] for r in conn.execute(
        f"SELECT DISTINCT id FROM entities WHERE branch IN ({ph})", rel)}
    b_ids = {r["id"] for r in conn.execute(
        "SELECT DISTINCT id FROM entities WHERE branch=?", (branch,))}
    return {"comparable": True, "release": rel, "branch": branch,
            "unpublished_delta": len(b_ids - r_ids),   # 工作分支有、release 无 → 未发布增量
            "missing_in_branch": len(r_ids - b_ids),   # release 有、工作分支无 → 待同步
            "release_total": len(r_ids), "branch_total": len(b_ids)}


# ────────────────────────────── 权威基线作用域（scope=release） ──────────────────────────────

def release_counts(conn) -> dict:
    """权威基线（release 分支集合）的实体/关系分布。scope=release。

    这是"AI 看到的知识"的唯一口径（AI 消费侧只查 release）。
    """
    rel = release_branches(conn)
    ph = _ph(len(rel))

    def _by(where_status, params):
        return {r["status"]: r["n"] for r in conn.execute(
            f"SELECT status, COUNT(*) n FROM {where_status} WHERE branch IN ({ph}) GROUP BY status",
            (*params, *rel))}
    ent = _by("entities", ())
    rln = _by("relations", ())
    return {
        "entities": {"reviewed": ent.get("reviewed", 0), "candidate": ent.get("candidate", 0),
                     "deprecated": ent.get("deprecated", 0), "total": sum(ent.values())},
        "relations": {"reviewed": rln.get("reviewed", 0), "candidate": rln.get("candidate", 0),
                      "deprecated": rln.get("deprecated", 0), "total": sum(rln.values())},
        "published": conn.execute(
            f"SELECT COUNT(*) FROM entities WHERE branch IN ({ph}) AND status='reviewed' "
            f"AND published_at != ''", rel).fetchone()[0],
        "branches": rel,
    }


# ────────────────────────────── 全局作用域（scope=global） ──────────────────────────────

def global_counts(conn) -> dict:
    """全局资产计数 —— **图谱数据没有"全局"口径**，故键名分工明确，禁止再暴露裸 entities/relations。

    ⚠️ 为什么不能给图谱数据一个"全局总数"（2026-09-27 实测）：
      entities 主键是 `(id, branch)`，同一逻辑实体在每个分支各存一行；
      `COUNT(*) WHERE status!='deprecated'` 得到 183，而**逻辑 id 去重只有 61**（重复 3 倍，
      因 61 个逻辑 id 恰好分布在 dev/release/personal 三个分支）。
      relations 同理：249 行 → 三元组去重 83（其 id 每行独立生成，须按
      `(source_id, relation_type, target_id)` 去重）。
      "183 / 249" 既不是"知识总量"、也不属于任何分支，是**行数累加**，对外展示会误导。

    本函数返回三组分工明确的键：
      · `documents` / `chunks`                 真全局（文档不按分支切分）
      · `entities_dedup` / `relations_dedup`   跨分支去重后的**知识总量**（对外应展示这一组）
      · `entities_rows` / `relations_rows`     跨分支行数（含分支副本，仅供对账/排查）
    """
    def one(q):
        return conn.execute(q).fetchone()[0]
    return {
        "documents": one("SELECT COUNT(*) FROM documents"),
        "chunks": one("SELECT COUNT(*) FROM document_chunks"),
        "entities_dedup": one("SELECT COUNT(DISTINCT id) FROM entities "
                              "WHERE status!='deprecated'"),
        "relations_dedup": one("SELECT COUNT(DISTINCT source_id || relation_type || target_id) "
                               "FROM relations WHERE status!='deprecated'"),
        "entities_rows": one("SELECT COUNT(*) FROM entities WHERE status!='deprecated'"),
        "relations_rows": one("SELECT COUNT(*) FROM relations WHERE status!='deprecated'"),
    }


def lifecycle(conn) -> dict:
    """生命周期分布：原始切片 → 候选 → 审校 → 入库 → 发布 → 废弃（FR-KG-8）。

    scope=global（不分分支）：published 以「release 分支 + published_at 非空」判定。
    本函数是 lifecycle 的**唯一实现**（/api/knowledge/lifecycle 与 overview 均委派至此）。
    """
    def _cnt(where, params=()):
        return conn.execute(f"SELECT COUNT(*) FROM entities WHERE {where}", params).fetchone()[0]

    dist = {
        "raw_chunk": _cnt("status='raw_chunk'"),
        "candidate": _cnt("status='candidate'"),
        "reviewed": _cnt("status='reviewed' AND NOT (branch='release' AND published_at != '')"),
        "published": _cnt("branch='release' AND status='reviewed' AND published_at != ''"),
        "deprecated": _cnt("status='deprecated'"),
    }
    # ⚠️ 本分布是**跨分支行数口径**：同一实体在 release 记 published、在 dev 记 reviewed，
    #    故 total 是行数而非实体数（2026-09-27 实测 189 行 / 61 个逻辑实体）。
    return {"lifecycle": dist, "total": sum(dist.values()),
            "scope_note": "跨分支行数口径：同一实体在 release 记「已发布」、在 dev/personal 记"
                          "「已评审(未发布)」，故 total 是行数不是实体数",
            "by_source": source_dist(conn, 6),
            "by_category": category_dist(conn, 8),
            "published_records": conn.execute(
                "SELECT COUNT(*) FROM knowledge_publish_logs").fetchone()[0]}


def source_dist(conn, limit: int = 8) -> list:
    """实体来源分布（空 source_type 归 manual）—— **跨分支去重口径**（`COUNT(DISTINCT id)`）。

    注意 GROUP BY 用表达式而非别名：表恰有 source_type 列，GROUP BY source_type 会被解析为
    原始列，导致 '' 与 'manual' 分成两组显示两个 manual。

    ⚠️ 为什么用去重而不是行数（2026-09-27 修）：`entities` 主键是 `(id, branch)`，
    `COUNT(*)` 会把同一逻辑实体按分支重复计数 —— 实测 manual 段显示 180，而正上方的
    资产 KPI 是 61（去重），同一张卡里两种口径并列会让人误读成"有 180 个 manual 知识实体"。
    改为去重后：各段之和 == 资产 KPI 的实体数（自检有断言）。
    """
    return [dict(r) for r in conn.execute(
        "SELECT COALESCE(NULLIF(source_type,''),'manual') AS source_type, "
        "COUNT(DISTINCT id) AS n FROM entities "
        "WHERE status!='deprecated' GROUP BY COALESCE(NULLIF(source_type,''),'manual') "
        "ORDER BY n DESC LIMIT ?", (limit,))]


def category_dist(conn, limit: int = 8) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT COALESCE(NULLIF(knowledge_category,''),'未分类') AS cat, COUNT(DISTINCT id) AS n "
        "FROM entities WHERE status='reviewed' GROUP BY cat ORDER BY n DESC LIMIT ?", (limit,))]


def review_queue(conn, per: int = 5) -> dict:
    """三类待评审队列（各前 per 条 + 总数）。"""
    from vector2graph import list_candidates
    ent_total = conn.execute("SELECT COUNT(*) FROM entities WHERE status='candidate'").fetchone()[0]
    rel_total = conn.execute("SELECT COUNT(*) FROM relations WHERE status='candidate'").fetchone()[0]
    v2g_total = conn.execute("SELECT COUNT(*) FROM v2g_candidates WHERE status='pending'").fetchone()[0]

    v2g_all, _ = list_candidates(conn, None, 50, 0)
    v2g_items = [{"id": c["id"], "name": c.get("entity_name") or "",
                  "entity_type": c.get("entity_type") or ""}
                 for c in v2g_all if c.get("status") == "pending"][:per]
    ent_items = [dict(r) for r in conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE status='candidate' "
        "ORDER BY created_at DESC LIMIT ?", (per,))]
    rel_rows = [dict(r) for r in conn.execute(
        "SELECT id, relation_type, source_id, target_id FROM relations WHERE status='candidate' "
        "ORDER BY id DESC LIMIT ?", (per,))]
    ids = {r["source_id"] for r in rel_rows} | {r["target_id"] for r in rel_rows}
    names = {}
    if ids:
        for e in conn.execute(f"SELECT id, name FROM entities WHERE id IN ({_ph(len(ids))})", list(ids)):
            names[e["id"]] = e["name"]
    for r in rel_rows:
        r["source_name"] = names.get(r["source_id"], "")
        r["target_name"] = names.get(r["target_id"], "")
    return {"v2g": {"total": v2g_total, "items": v2g_items},
            "entities": {"total": ent_total, "items": ent_items},
            "relations": {"total": rel_total, "items": rel_rows}}


def graph_thumb(conn, limit: int = 60) -> dict:
    """图谱缩略：reviewed 实体按逻辑 id 去重（release 版本优先）+ reviewed 关系数。"""
    thumb = [dict(r) for r in conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE status='reviewed' "
        "GROUP BY id ORDER BY (branch='release') DESC, created_at DESC LIMIT ?", (limit,))]
    return {"entities": thumb,
            "relations": conn.execute(
                "SELECT COUNT(*) FROM relations WHERE status='reviewed'").fetchone()[0]}


# ────────────────────────────── 数据质量（C4/C5 修复点） ──────────────────────────────

def type_coverage(conn) -> dict:
    """本体类型覆盖率——**两级分母**（修 C4）。

    旧口径（已废弃）：分母＝ontology_types 里全部 entity 类型（28 个，其中 25 个是领域业务
    类型）→ 26/28 = 93% 假绿，且指标饱和、丧失指导增量抽取的区分度。

    新口径拆两级：
    - `sysml`  ：SysML v2 元素类型（14 类，来自 sysml_ast.NODE_KINDS）＝**规范约束源**覆盖度。
                 实测 2/14 ≈ 14% → alert，它才是指出"该补什么"的那一项。
    - `domain` ：领域扩展类型（ontology_types 中不属于 SysML 级的）＝**领域知识点**覆盖度。
    """
    sysml = set(sysml_element_types())
    etypes = [r["name"] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind='entity'")]
    covered = {r["entity_type"] for r in conn.execute(
        "SELECT DISTINCT entity_type FROM entities WHERE status!='deprecated'")}
    reviewed = {r["entity_type"] for r in conn.execute(
        "SELECT DISTINCT entity_type FROM entities WHERE status='reviewed'")}

    def _lvl(names: list) -> dict:
        names = sorted(names)
        cov = [t for t in names if t in covered]
        rev = [t for t in names if t in reviewed]
        return {"total_types": len(names), "covered_types": len(cov), "reviewed_types": len(rev),
                "coverage_rate": round(len(cov) / len(names), 4) if names else 0,
                "empty_types": [{"name": t} for t in names if t not in reviewed]}

    return {"sysml": _lvl(sysml),
            "domain": _lvl([t for t in etypes if t not in sysml])}


def chunk_trace(conn) -> dict:
    """文档分块追溯覆盖率（linked_entity_ids 非空且非 '[]' 视为已链接）。scope=global。"""
    total = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
    linked = conn.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE linked_entity_ids IS NOT NULL "
        "AND TRIM(linked_entity_ids) != '' AND linked_entity_ids != '[]'").fetchone()[0]
    return {"total_chunks": total, "linked_chunks": linked,
            "unlinked_ratio": round((total - linked) / total, 4) if total else 0,
            "coverage_rate": round(linked / total, 4) if total else 0}


def fallback_topics(conn, days: int = 30, top: int = 20) -> list:
    """近 N 天检索 fallback 的**业务主题**词频（修 C5）。

    两级过滤：
    1. **整条 query 丢弃**：命中 `_ORCHESTRATION_MARKERS` 的内部编排流量
       （agent 把任务上下文当检索 query 打库 —— 这不是"知识缺口"，是内部噪声）
    2. **词级过滤**：停用词 + 内部提示词词 + 通用词（子串命中即滤）
    """
    import re
    from collections import Counter
    rows = conn.execute(
        "SELECT query FROM query_routing_stats "
        "WHERE created_at >= datetime('now', ?) AND (route IN ('vector','mixed') OR reason LIKE '%no_hit%')",
        (f"-{days} days",)).fetchall()
    counter = Counter()
    for r in rows:
        q = (r["query"] or "").strip()
        if not q:
            continue
        if any(mk in q for mk in _ORCHESTRATION_MARKERS):
            continue  # 整条丢弃：内部编排查询
        for w in re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z0-9]{2,}", q):
            wl = w.lower()
            if wl in STOP_WORDS:
                continue
            # 子串命中即滤：正则按最大连续串切词，"任务上下文快照" 需被 "任务上下文" 覆盖
            if any(n in wl for n in _INTERNAL_PROMPT_NOISE):
                continue
            if any(n in wl for n in _GENERIC_TERM_NOISE):
                continue
            if wl[0] in _NOISE_TAIL or wl[-1] in _NOISE_TAIL:
                continue  # 语法碎片（被标点切断的虚词片段）
            counter[wl] += 1
    return [{"word": w, "count": c} for w, c in counter.most_common(top)]


# ────────────────────────────── 消费侧 ──────────────────────────────

def engine_routes(conn) -> dict:
    """双引擎路由分布（复用 QueryRouter 统计，不另写一份 SQL）。"""
    from knowledge_engine import QueryRouter
    return QueryRouter().stats(conn)


def governance_items(conn, run_shacl: bool = False) -> dict:
    """治理健康度 9 项（复用 governance.compute_metrics 的既有阈值口径）。"""
    from governance import compute_metrics
    return compute_metrics(conn, run_shacl=run_shacl)


# ────────────────────────────── 红线（scope=release，不受任何切换影响） ──────────────────────────────

def redlines(conn) -> list:
    """治理红线：任一为 alert 都必须能一键下钻到修复入口。

    1. release 上未评审实体数 —— 不变量，必须 = 0（AI 只消费 release，
       所以这条为真意味着「AI 正在消费脏数据」）
    2. 指向 release 的未处理合并请求中，源分支未评审实体数 —— 闸门预警
    3. 文档分块追溯覆盖率 —— 未链接 = 抽取值不到来源，追溯链断裂
    """
    from repositories.branch_repo import BranchRepo
    repo = BranchRepo(conn)
    rel = release_branches(conn)

    ph = _ph(len(rel))
    leaked = conn.execute(
        f"SELECT COUNT(*) FROM entities WHERE branch IN ({ph}) "
        f"AND status NOT IN ('reviewed','deprecated')", rel).fetchone()[0]

    pending_mr = conn.execute(
        "SELECT source_branch, target_branch FROM merge_requests "
        "WHERE status IN ('draft','open') AND target_branch IN (%s)" % ph, rel).fetchall()
    gate_hits, gate_detail = 0, []
    for mr in pending_mr:
        blocked = repo.pending_review_entities(mr["source_branch"])
        if blocked:
            gate_hits += len(blocked)
            gate_detail.append({"source_branch": mr["source_branch"],
                                "target_branch": mr["target_branch"], "count": len(blocked)})

    ct = chunk_trace(conn)
    return [
        {"key": "release_unreviewed", "name": "权威基线未评审数据", "value": leaked, "unit": "条",
         "target": "= 0", "scope": "release", "layer": "红线",
         "threshold": "未评审数据禁止进入 release（AI 只消费 release）",
         "detail": f"release 分支上 status 非 reviewed/deprecated 的实体 {leaked} 条",
         "status": _status(leaked, lower_better=True, warn=0),
         "drill": {"page": "kb-b", "title": "去治理中心完成审核"}},
        {"key": "merge_gate_pending", "name": "发布闸门预警", "value": gate_hits, "unit": "条",
         "target": "= 0", "scope": "release", "layer": "红线",
         "threshold": "指向 release 的合并请求若源分支有未评审数据，将被闸门拦截",
         "detail": "；".join(f"{d['source_branch']}→{d['target_branch']} 待审 {d['count']} 条"
                          for d in gate_detail) or "无未处理的发布合并请求",
         "status": _status(gate_hits, lower_better=True, warn=0),
         "drill": {"page": "kb-b", "title": "去治理中心完成审核"}},
        {"key": "chunk_trace", "name": "分块追溯覆盖率",
         "value": round(ct["coverage_rate"] * 100, 1), "unit": "%",
         "target": "≥ 90%", "scope": "global", "layer": "红线",
         "threshold": "分块未链接实体 = 抽取值无来源，追溯链断裂",
         "detail": f"{ct['linked_chunks']}/{ct['total_chunks']} 个分块已链接实体",
         "status": _status(ct["coverage_rate"] * 100, ok=90, warn=70),
         "drill": {"page": "kb-e", "title": "去文档库查看未链接分块"}},
    ]


# ────────────────────────────── 向后兼容聚合（旧端点委派点） ──────────────────────────────

def overview(conn) -> dict:
    """/api/knowledge/overview 的返回体（scope=global）。"""
    q = review_queue(conn)
    gc = global_counts(conn)
    # ⚠️ kpi 用**跨分支去重**口径（此前用行数累加，把知识量虚报 3 倍）
    kpi = {"entities": gc["entities_dedup"],
           "relations": gc["relations_dedup"],
           "documents": gc["documents"],
           "pending_review": q["v2g"]["total"] + q["entities"]["total"] + q["relations"]["total"],
           "entities_rows": gc["entities_rows"],
           "relations_rows": gc["relations_rows"]}
    return {"kpi": kpi, "review_queue": q, "source_dist": source_dist(conn, 8),
            "lifecycle": lifecycle(conn), "graph_thumb": graph_thumb(conn),
            "scope": "global",
            "scope_note": "实体/关系为**跨分支去重**后的知识总量（entities_rows/"
                          "relations_rows 为含分支副本的行数，仅供对账）；文档/分块不分分支"}


def coverage(conn) -> dict:
    """/api/knowledge/coverage 的返回体。

    兼容旧字段（`type_coverage` 仍存在，但**分母已改为 SysML v2 元素类型**），
    并新增 `type_coverage_sysml` / `type_coverage_domain` 两级明细供看板分级展示。
    """
    tc = type_coverage(conn)
    ct = chunk_trace(conn)
    return {"type_coverage": {**tc["sysml"], "scope": "sysml",
                              "note": "分母＝SysML v2 元素类型（sysml_ast.NODE_KINDS 归并 14 类）"},
            "type_coverage_sysml": tc["sysml"],
            "type_coverage_domain": tc["domain"],
            "chunk_linked": ct,
            "fallback_topics": fallback_topics(conn),
            "scope": "global"}


# ══════════════════════════════════════════════════════════════════════════════
# P2 数据质量簇（2026-09-26）
#   ⚠️ 本节的实现纪律：**指标未落地时必须返回 value=None（前端显示 "—"、状态 warn），
#      不得报 0% 假绿/假红**。以下三项实测即为"未落地/无从评估"，它们本身就是最该暴露的信息：
#        · entity_aliases        = 0 行  → 别名覆盖率未落地
#        · entity_dup_candidates = 0 行  → 消歧准确率无评测集
#        · ontology_types.constraints.required 全空 → 属性完整度**无从评估**
# ══════════════════════════════════════════════════════════════════════════════

def alias_coverage(conn) -> dict:
    """别名覆盖率（PRD §2.2「中英文实体互认率 ~30% → ≥90%」）。

    实测 2026-09-26：`entity_aliases` **0 行** → `landed=False`、`rate=None`。
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status!='deprecated'").fetchone()[0]
    try:
        aliases = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
        with_alias = conn.execute(
            "SELECT COUNT(DISTINCT entity_id) FROM entity_aliases").fetchone()[0]
    except Exception:
        aliases = with_alias = 0
    return {"entities": total, "aliases": aliases, "entities_with_alias": with_alias,
            "landed": aliases > 0,
            "rate": round(with_alias / total, 4) if (total and aliases) else None}


def dedup_state(conn) -> dict:
    """消歧队列深度 + 消歧准确率（PRD §2.2 ≥85%）。

    实测：`entity_dup_candidates` **0 行** → 队列 0、**准确率无评测集（None）**。
    """
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM entity_dup_candidates GROUP BY status").fetchall()
    except Exception:
        rows = []
    by = {r["status"]: r["n"] for r in rows}
    pending = by.get("pending", 0) + by.get("candidate", 0)
    return {"queue_depth": sum(by.values()), "pending": pending, "by_status": by,
            "accuracy": None, "accuracy_note": "无评测集（entity_dup_candidates 未标注）",
            "landed": sum(by.values()) > 0}


def ontology_schema_gaps(conn) -> dict:
    """本体 Schema 缺口：定义了 `constraints.required` 的实体类型占比。

    ⚠️ 实测：52 个类型**无一**定义必填属性 ⇒ 属性完整度**无从评估**。
    这条指标就是把"无法评估"这件事本身暴露出来（不是报 100% 完整度）。
    阈值：≥50% 才算本体具备可评估的约束力。
    """
    types = conn.execute(
        "SELECT name, constraints FROM ontology_types WHERE type_kind='entity'").fetchall()
    with_req = []
    for t in types:
        try:
            cons = json.loads(t["constraints"] or "{}")
        except Exception:
            cons = {}
        if cons.get("required"):
            with_req.append(t["name"])
    tot = len(types)
    return {"total_types": tot, "types_with_required": len(with_req), "with_required": with_req,
            "rate": round(len(with_req) / tot, 4) if tot else 0}


def attr_filled_rate(conn) -> dict:
    """实体属性齐备率（properties 非空占比）——本体无必填约束时的兜底口径。"""
    tot = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status!='deprecated'").fetchone()[0]
    filled = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status!='deprecated' "
        "AND properties IS NOT NULL AND TRIM(properties) NOT IN ('','{}')").fetchone()[0]
    return {"total": tot, "filled": filled,
            "rate": round(filled / tot, 4) if tot else 0}


def staleness(conn, days: int = 90) -> dict:
    """陈旧度：超 N 天未新建/变更的实体占比。

    ⚠️ 口径限制：`entities` 表**无 `updated_at` 列**（实测 PRAGMA），
    故只能以 `created_at` 近似（即"长期未新增"而非"长期未修改"），已在 detail 注明。
    """
    tot = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status!='deprecated'").fetchone()[0]
    stale = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status!='deprecated' "
        "AND created_at < datetime('now', ?)", (f"-{days} days",)).fetchone()[0]
    return {"total": tot, "stale": stale, "days": days,
            "rate": round(stale / tot, 4) if tot else 0}


def ontology_drift(conn) -> dict:
    """本体版本漂移：孤儿类型（实体引用了本体中不存在的类型）+ 被替换/弃用类型仍被引用。"""
    orphan = [dict(r) for r in conn.execute(
        "SELECT DISTINCT e.entity_type, COUNT(*) n FROM entities e WHERE e.status!='deprecated' "
        "AND NOT EXISTS (SELECT 1 FROM ontology_types o WHERE o.type_kind='entity' "
        "AND o.name=e.entity_type) GROUP BY e.entity_type")]
    replaced = [r["name"] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE COALESCE(replaced_by,'')!='' "
        "OR status IN ('deprecated','obsolete')")]
    in_use = 0
    if replaced:
        in_use = conn.execute(
            f"SELECT COUNT(*) FROM entities WHERE status!='deprecated' "
            f"AND entity_type IN ({_ph(len(replaced))})", replaced).fetchone()[0]
    ver = conn.execute(
        "SELECT MAX(version) v, COUNT(DISTINCT version) c FROM ontology_types").fetchone()
    return {"orphan_types": orphan, "orphan_count": len(orphan),
            "replaced_types": replaced, "replaced_in_use": in_use,
            "max_version": ver["v"], "distinct_versions": ver["c"],
            "drift": len(orphan) + in_use}


def ai_review_rate(conn) -> dict:
    """AI 建模入库审核率（PRD §2.2「0% → 100%」）。

    口径：`v2g_candidates` 中 `source_type='ai_model'` 且已离开 pending 的占比。
    实测 348 条全 pending → 0%。
    """
    try:
        tot = conn.execute("SELECT COUNT(*) FROM v2g_candidates "
                           "WHERE COALESCE(source_type,'')='ai_model'").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM v2g_candidates WHERE "
                            "COALESCE(source_type,'')='ai_model' AND status!='pending'").fetchone()[0]
        alltot = conn.execute("SELECT COUNT(*) FROM v2g_candidates").fetchone()[0]
    except Exception:
        tot = done = alltot = 0
    scope = tot or alltot   # 无 ai_model 标记时退化为全部候选（口径已注明）
    return {"ai_total": tot, "ai_reviewed": done, "all_total": alltot,
            "scope_total": scope, "scoped_to_ai": bool(tot),
            "rate": round(done / scope, 4) if scope else None}


# ══════════════════════════════════════════════════════════════════════════════
# P3 消费 / 性能 / 验收对照（2026-09-26）
# ══════════════════════════════════════════════════════════════════════════════

def latency_stats(conn, days: int = 0) -> dict:
    """检索延迟分位（**在 Python 内算**：SQLite 无 percentile 函数，窗口+别名写法易错）。

    实测 2026-09-26（471 条）：全体 p50=4090ms / p95=8367ms；
    分路线：graph p50=3ms、vector p50=4516ms、mixed p50=6293ms。
    → 直接对应甲方 7.1(a)「API ≤500ms」，是当前最大的性能缺口。
    """
    where, params = ("WHERE created_at >= datetime('now', ?)", (f"-{days} days",)) if days else ("", ())
    rows = conn.execute(
        f"SELECT route, latency_ms FROM query_routing_stats {where}", params).fetchall()

    def _pct(vals, p):
        if not vals:
            return None
        v = sorted(vals)
        return v[min(len(v) - 1, int(len(v) * p))]

    allv = [r["latency_ms"] or 0 for r in rows]
    by = {}
    for r in rows:
        by.setdefault(r["route"] or "unknown", []).append(r["latency_ms"] or 0)
    return {"n": len(allv), "p50": _pct(allv, .5), "p90": _pct(allv, .9),
            "p95": _pct(allv, .95), "max": max(allv) if allv else None,
            "by_route": {k: {"n": len(v), "p50": _pct(v, .5), "p95": _pct(v, .95),
                             "max": max(v)} for k, v in sorted(by.items())}}


def slow_queries(conn, threshold: int = 2000, limit: int = 5) -> dict:
    """慢查询：超阈值条数 + 占比 + TOP N（按延迟倒序）。"""
    total = conn.execute("SELECT COUNT(*) FROM query_routing_stats").fetchone()[0]
    slow = conn.execute("SELECT COUNT(*) FROM query_routing_stats WHERE latency_ms > ?",
                        (threshold,)).fetchone()[0]
    items = [{"query": (r["query"] or "")[:60], "route": r["route"], "latency_ms": r["latency_ms"]}
             for r in conn.execute(
                 "SELECT query, route, latency_ms FROM query_routing_stats "
                 "WHERE latency_ms > ? ORDER BY latency_ms DESC LIMIT ?", (threshold, limit))]
    return {"threshold": threshold, "total": total, "slow": slow,
            "rate": round(slow / total, 4) if total else 0, "items": items}


def eval_quality(conn) -> dict:
    """知识获取质量（甲方 7.1(c~e)）——读 `eval_reports` 最新一行的 detail JSON。

    实测（2026-09-22 46 例评测，db 指纹已对账）：
      summary.graph.entity_recall = 0.972 → 甲方「结构化 ≥95%」达标
      summary.doc.recall@5        = 0.9   → 甲方「半/非结构 ≥75%」达标
      negative_control.recall@5   = 0.0   → 负对照（轮转 gold），证明指标在真测对齐关系
      entity_precision / f1 列为 0.0 **是"未计算"**，不是"精确率为 0" → 必须如实标注
    """
    row = conn.execute(
        "SELECT id, created_at, model_version, entity_precision, entity_recall, entity_f1, "
        "relation_precision, relation_recall, relation_f1, detail, is_golden "
        "FROM eval_reports ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        return {"landed": False, "note": "eval_reports 无记录"}
    try:
        detail = json.loads(row["detail"] or "{}")
    except Exception:
        detail = {}
    s = detail.get("summary") or {}
    doc = s.get("doc") or {}
    graph = s.get("graph") or {}
    neg = s.get("neg") or {}
    nc = ((detail.get("negative_control") or {}).get("recall") or {})
    age = conn.execute("SELECT CAST(julianday('now')-julianday(?) AS INT)",
                       (row["created_at"],)).fetchone()[0]
    return {
        "landed": True,
        "report_id": row["id"], "created_at": row["created_at"],
        "model_version": row["model_version"], "age_days": age,
        # 结构化（图谱侧）召回：甲方 7.1(c)
        "structured_recall": graph.get("entity_recall"),
        "relation_hit": graph.get("relation_hit"),
        "route_acc": graph.get("route_acc"),
        "graph_n": graph.get("n"),
        # 半/非结构化（文档侧）recall@5：甲方 7.1(d)
        "semi_recall5": doc.get("recall@5"),
        "doc_mrr10": doc.get("mrr@10"),
        "doc_n": doc.get("n"),
        "doc_p50_ms": doc.get("latency_p50_ms"),
        # 精确率/F1：列为 0.0 = 未计算（非"为 0"）
        "precision_computed": bool(row["entity_precision"]),
        "precision": row["entity_precision"],
        "f1": row["entity_f1"],
        # 负对照与负例
        "negative_control_recall5": nc.get("recall@5"),
        "neg_n": neg.get("n"), "neg_route_ok": neg.get("route_ok"),
        "neg_false_graph_confident": neg.get("false_graph_confident"),
        "fingerprint_drift": len(detail.get("fingerprint_drift") or []),
        "ontology_active": ((detail.get("fingerprint_evalset") or {}).get("ontology_active")),
    }


def service_health(conn, fuseki_port: int = 3030, timeout: float = 1.0) -> dict:
    """服务健康探活（与 `/api/ops/metrics` 的 topology 同源实现，看板复用，不重复写一份）。

    返回 {components:[{name,status}], total, online, rate}。
    """
    comps = [{"name": "SQLite 主库", "status": "online"}]
    try:
        import pyoxigraph  # noqa: F401
        comps.append({"name": "pyoxigraph 内嵌图库", "status": "online"})
    except Exception:
        comps.append({"name": "pyoxigraph 内嵌图库", "status": "offline"})
    try:
        import socket
        with socket.create_connection(("127.0.0.1", fuseki_port), timeout=timeout):
            comps.append({"name": "Fuseki 外挂图库", "status": "online"})
    except Exception:
        comps.append({"name": "Fuseki 外挂图库（可选，未启用）", "status": "offline"})
    try:
        for r in conn.execute("SELECT name, status FROM llm_providers").fetchall():
            comps.append({"name": f"LLM · {r['name']}", "status": r["status"] or "unknown"})
    except Exception:
        pass
    ok = ("online", "active", "enabled", "ok")
    online = sum(1 for c in comps if c["status"] in ok)
    return {"components": comps, "total": len(comps), "online": online,
            "rate": round(online / len(comps), 4) if comps else 0}


def activity_stats(conn, days: int = 7) -> dict:
    """审计与 LLM 活跃度（近 N 天 / 近 30 天）。"""
    def _c(table, d):
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table} "
                                f"WHERE created_at >= datetime('now', ?)",
                                (f"-{d} days",)).fetchone()[0]
        except Exception:
            return None
    return {"days": days, "audit_recent": _c("audit_logs", days),
            "audit_30d": _c("audit_logs", 30),
            "llm_recent": _c("llm_usage_stats", days),
            "llm_30d": _c("llm_usage_stats", 30),
            "audit_total": _c("audit_logs", 3650),
            "llm_total": _c("llm_usage_stats", 3650)}


# ── P2：指标快照与趋势（唯一写库点） ──

def save_snapshot(conn, metrics: dict, branch: str = "", commit: bool = True) -> int:
    """把指标落快照（按 `(metric_key, snapshot_date, branch)` **幂等 upsert**）。

    为什么按天去重：sparkline 需要"每天的取值"，同一天多次刷新不应产生多个点。
    """
    n = 0
    for key, m in (metrics or {}).items():
        if m.get("value") is None:
            continue  # 未落地的指标不写快照（避免把 None 当成 0 画出假趋势）
        conn.execute(
            "INSERT INTO dashboard_metric_snapshots (metric_key, snapshot_date, branch, value, unit, status) "
            "VALUES (?, date('now'), ?, ?, ?, ?) "
            "ON CONFLICT(metric_key, snapshot_date, branch) DO UPDATE SET "
            "value=excluded.value, unit=excluded.unit, status=excluded.status",
            (key, branch or "", m.get("value"), m.get("unit") or "", m.get("status") or ""))
        n += 1
    if commit:
        conn.commit()
    return n


def metric_trend(conn, key: str, branch: str = "", limit: int = 12) -> list:
    """单指标历史趋势（按日期升序，供 sparkline）。"""
    try:
        rows = conn.execute(
            "SELECT snapshot_date, value, status FROM dashboard_metric_snapshots "
            "WHERE metric_key=? AND branch=? ORDER BY snapshot_date DESC LIMIT ?",
            (key, branch or "", limit)).fetchall()
    except Exception:
        return []
    return [{"date": r["snapshot_date"], "value": r["value"], "status": r["status"]}
            for r in reversed(rows)]


def trends(conn, keys: list, branch: str = "", limit: int = 12) -> dict:
    return {k: metric_trend(conn, k, branch, limit) for k in (keys or [])}
