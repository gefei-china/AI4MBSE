"""能力簇（Capability Cluster）：把 22 个 Agent + 137 插件 + 41 技能 + 121 工具
按**能力**聚合成 8 个互斥业务族 + 1 个横切基础族，供路由收窄与能力范围裁剪使用。

## 为什么需要（2026-11-07 米爸第 2/4 问）

现状路由候选池 = `agents.status='active'` 的 22 行，三层（关键词 / 语义 / LLM）
都在这 22 行里做「22 选 1」。但候选池本身不互斥：

| 冗余 | 实证 |
|---|---|
| `design` 与 `工程建模方案设计Agent` | display_name 相同、system_prompt 逐字相同（476 字） |
| 需求域 3 个 | analysis / quality / structuring，用户语言里无法区分 |
| 设计域 4 个 | design / 工程建模方案设计Agent / 多方案设计 / 多方案生成 |
| 校验域 5 个 | review / repair / trace_verification / change_safety_gate / impact |

集合本身冗余时「最优」没有定义 ⇒ **先按能力聚合收窄，再在族内按输入形态分工**。

## 设计纪律（沿用本仓既有铁律）

1. **不删任何行**：本模块只做「逻辑聚合 + 候选池收窄」，不物理合并/禁用 Agent，
   不碰 agent_tools / agent_team_members / 历史会话。所有改动可逆。
2. **策略即数据**：族定义、成员映射、族内分工判据都放在本模块的可读常量里，
   代码只消费；新增 Agent 时 `coverage()` 会报「未归族」，不会静默漏掉。
3. **默认零回归**：路由开关 `intent.cluster_route` 默认 **False**。
   打开后 LLM 兜底层从「22 选 1」变成「8 选 1 + 族内确定性分派」，
   对外输出仍是**具体 Agent 名**（`_done` / 缓存 / 落库 / 编排契约完全不变）。
4. **族内分工不调 LLM**：纯关键词判据，可单测、可解释、零延迟。
   分类交给 LLM（擅长语义），分工交给代码（擅长确定性）—— 与 OpenAI 官方
   "把决策尽可能从 LLM 抽到显式软件" 一致。

## 实测依据（tools/verify/verify_intent_placement_ab.py，同一 gold set 15 条 × 4 组）

| 组 | 准确率 | 平均延迟 | 平均 token |
|---|---|---|---|
| 独立分类器 / 22 个 Agent | 100% | 993 ms | 635 |
| 独立分类器 / 8 个能力族 | **100%** | **931 ms** | **531（-16.4%）** |
| 主 Agent 内嵌决策 / 22 | 100% | 4736 ms | 1265 |
| 主 Agent 内嵌决策 / 8 族 | 93.3%（1 次输出截断） | 4840 ms | 1148 |

⇒ 独立轻量分类器在准确度不降的前提下，比主 Agent 内嵌**快 5 倍、省 2.4 倍 token**；
   能力族化再省 16% token。故本平台**不**单独维护「意图识别子 Agent」。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

CLUSTER_VERSION = 2

#: 8 个互斥业务族 + 1 个横切基础族。
#: `representative` = 路由只出族时代理执行者；`intra` = 族内分工判据（按顺序命中）。
CLUSTERS: Dict[str, dict] = {
    "requirement": {
        "title": "需求工程",
        "desc": "自然语言诉求 → 需求规格；已写需求的质量评审；长文本条目化",
        "members": ["requirement_analysis", "requirement_quality", "requirement_structuring"],
        "representative": "requirement_analysis",
        # 族内分工 = **输入形态**，不是"谁更懂需求"。顺序即优先级（先评审、再条目化、默认规格化）
        "intra": [
            ("requirement_quality",
             ("质量评审", "写得对不对", "写得好不好", "模糊词", "不可验证", "问题清单",
              "需求质量", "验收判据", "矛盾", "自相矛盾")),
            ("requirement_structuring",
             ("条目化", "结构化", "拆成条目", "拆条目", "批量", "分类标注", "给 ID",
              "需求清单", "长文本", "访谈记录")),
        ],
    },
    "design": {
        "title": "设计与建模",
        "desc": "架构方案设计、多方案权衡、方法论选择、骨架与八视图生成",
        # 2026-11-07：重复 Agent 已合并（只保留 `design`），旧名从成员表移除。
        "members": ["design", "多方案设计", "多方案生成",
                    "architecture_skeleton", "view_expansion", "methodology_resolver"],
        "representative": "design",
        # 设计族内是**流水线阶段**（stage_order），不是并列候选：路由只出族，
        # 阶段分派由既有编排器按 stage_order 决定，这里不重复实现。
        "intra": [],
    },
    "verify": {
        "title": "校验与评审",
        "desc": "预评审打分、语法诊断修复、需求-模型追溯核验",
        "members": ["review", "model_validation_repair", "trace_verification"],
        "representative": "review",
        "intra": [
            ("model_validation_repair",
             ("修复", "校验不通过", "语法错误", "诊断修复", "自动修复", "修复循环")),
            ("trace_verification",
             ("追溯", "覆盖", "孤儿", "满足链", "追溯矩阵")),
        ],
    },
    "change": {
        "title": "变更与影响",
        "desc": "变更波及范围分析；删除/替换前的安全门评估",
        "members": ["impact", "change_safety_gate"],
        "representative": "impact",
        "intra": [
            ("change_safety_gate",
             ("安全门", "删除前", "批量替换", "删除影响", "边界变更", "替换风险")),
        ],
    },
    "report": {
        "title": "报告与导出",
        "desc": "分析结果组装成工程报告并导出 md/docx/pdf",
        "members": ["report_generation"],
        "representative": "report_generation",
        "intra": [],
    },
    "knowledge": {
        "title": "知识与问答",
        "desc": "MBSE/SysML v2 概念解释、规范条款解读、平台操作与兜底问答",
        "members": ["knowledge_qa", "chat"],
        "representative": "knowledge_qa",
        "intra": [
            ("chat",
             ("哪个菜单", "怎么新建", "平台怎么", "按钮在哪", "怎么配置", "这个平台")),
        ],
    },
    "platform": {
        "title": "平台数据读写",
        "desc": "通过 MCP 读取/写入智源平台工程与源码；模型发布入库",
        "members": ["智源对接agent", "智源数据读写agent", "model_release"],
        "representative": "智源对接agent",
        "intra": [
            ("model_release", ("发布", "落库", "入库", "生成新版本", "图谱回灌")),
            ("智源数据读写agent", ("增删改", "修改元素", "新增元素", "删除元素")),
        ],
    },
    "orchestration": {
        "title": "编排总控",
        "desc": "跨阶段任务拆解与多 Agent 协同（主 Agent，不直接承接用户意图）",
        "members": ["MBSE建模总体负责人"],
        "representative": "MBSE建模总体负责人",
        "intra": [],
        "route_visible": False,      # 主 Agent 是执行主体，不是路由候选
    },
    "shared": {
        "title": "横切基础能力",
        "desc": "文件/代码/文档/元能力/行业调研等跨业务域复用能力，不参与路由",
        "members": [],
        "representative": "",
        "intra": [],
        "route_visible": False,
    },
}

#: 路由可见的业务族（顺序确定，供 LLM 清单与指纹使用）
ROUTE_CLUSTERS: List[str] = [k for k, v in CLUSTERS.items()
                             if v.get("route_visible", True)]

#: Agent → 族。显式映射是必要的：中文名 / 自建 Agent 无法从 task_domain 可靠推导
#: （实测 task_domain='modeling' 横跨需求/设计/校验/发布 4 个族）。
AGENT_CLUSTER: Dict[str, str] = {}
for _cid, _c in CLUSTERS.items():
    for _m in _c["members"]:
        AGENT_CLUSTER[_m] = _cid

#: 插件/技能/工具按能力聚合的**派生规则**（不能手工逐个标注）。
#: 顺序即优先级；命中第一条即归族，未命中归 shared。
ASSET_RULES: List[tuple] = [
    ("verify", ("validation", "repair", "lint", "校验", "修复", "trace_coverage",
                "release_checklist", "stdlib_reference")),
    ("change", ("change_safety", "change_impact", "变更影响", "冲突分析", "conflict")),
    ("requirement", ("requirement", "需求", "clarification")),
    ("design", ("view_generation", "skeleton_generation", "methodology", "视图",
                "多方案", "alternatives", "需求图生成")),
    ("platform", ("zhiyuan", "智源", "mcp", "平台管理")),
    ("report", ("report", "报告")),
    ("knowledge", ("知识", "行业调研", "方法论规约")),
]


def cluster_of_agent(name: str) -> str:
    """Agent 名 → 族 id；未登记返回 ''（由 coverage 门禁报警，不静默归 shared）。"""
    return AGENT_CLUSTER.get(str(name or ""), "")


def cluster_of_asset(name: str, source: str = "", category: str = "") -> str:
    """技能 / 工具 / 插件名 → 能力族（纯派生，不查库）。

    ⚠️ **来源优先于名字**：实测 71 个 MCP 工具有 70 个名字（`query_projects` /
      `export_sysml_v2` / `get_model_text`…）不含「智源/zhiyuan/mcp」字样，
      按名字匹配会把它们全判进 shared（横切），族清单失去意义。
      但它们 100% 是**智源平台读写能力** ⇒ `source='mcp'` 直接归 platform。
      同理 `category='平台集成'` 的技能（智源平台管理/智源MCP工具手册）也归 platform。
    """
    n = str(name or "").lower()
    s = str(source or "").lower()
    cat = str(category or "").lower()
    if s in ("mcp", "zhiyuan") or "平台集成" in cat or "我的能力" in cat:
        return "platform"
    if s in ("coverage",) or "覆盖性分析" in cat:
        return "verify"
    for cid, pats in ASSET_RULES:
        if any(p.lower() in n for p in pats):
            return cid
    return "shared"


def clusters_for_llm() -> str:
    """给 LLM 兜底层的族清单（替代 22 个 Agent 名）。"""
    return "\n".join(
        "- %s（%s）：%s" % (cid, CLUSTERS[cid]["title"], CLUSTERS[cid]["desc"])
        for cid in ROUTE_CLUSTERS)


def intra_pick(cid: str, text: str) -> str:
    """族内按**输入形态**确定性分工；无命中回落族代表。零 LLM、零 I/O。"""
    c = CLUSTERS.get(cid)
    if not c:
        return ""
    t = str(text or "").lower()
    for target, kws in c.get("intra", []):
        if any(k.lower() in t for k in kws):
            return target
    return c.get("representative") or (c["members"][0] if c["members"] else "")


def resolve_cluster(cid: str, text: str) -> str:
    """LLM 输出族 id → 具体 Agent 名（对外契约不变）。"""
    cid = str(cid or "").strip()
    if cid in CLUSTERS:
        return intra_pick(cid, text)
    if cid in AGENT_CLUSTER:            # 模型偶尔直接回 Agent 名，兼容
        return cid
    return ""


def narrow_candidates(names: List[str], text: str = "") -> List[str]:
    """候选池收窄：每族最多保留 1 个（族代表）；shared/未归族不动。

    用于语义索引与 JIT 注入的**可选**裁剪。默认路由层不调用它，避免零回归风险。
    """
    seen, out = set(), []
    for n in names:
        cid = cluster_of_agent(n)
        if not cid or cid == "shared" or cid not in ROUTE_CLUSTERS:
            out.append(n)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append(intra_pick(cid, text) if text else
                   CLUSTERS[cid]["representative"])
    return out


def coverage(active_agent_names: List[str]) -> dict:
    """归族覆盖率体检：返回未归族 / 已归族 / 映射指向不存在的成员。"""
    declared = set(AGENT_CLUSTER)
    known = {m for c in CLUSTERS.values() for m in c["members"]}
    return {
        "active": len(active_agent_names),
        "unmapped": sorted(set(active_agent_names) - declared),
        "stale_mapping": sorted(declared - set(active_agent_names)),
        "member_mismatch": sorted(declared - known),
        "clusters": len(ROUTE_CLUSTERS),
    }


def asset_clusters(rows: List[dict]) -> dict:
    """给一批 {name,type,source,category} 资产按能力分簇，返回 {族: [资产名]}。"""
    out: Dict[str, List[str]] = {k: [] for k in CLUSTERS}
    for r in rows:
        out[cluster_of_asset(r.get("name") or "", r.get("source") or "",
                             r.get("category") or "")].append(r.get("name") or "")
    return out


    return out


# ─────────────────────────────────────────────────────────────────────────────
# ★ 2026-11-07（米爸第 14 轮）：多簇识别 + 族内成员直选
#
# 为什么必须加这两样（**实测驱动，不是设计偏好**）：
#   `verify_cluster_cost_eval.py` 用真路由器跑 8 条流水线阶段意图，发现簇模式把
#   准确率从 62.5% 压到 37.5% —— `model_validation_repair→review`、
#   `change_safety_gate→impact`。根因：LLM 只准输出 7 个族 id，族内分工表
#   `intra` 没覆盖 8 个阶段 Agent，于是细粒度被**强制抹平**。
#   ⇒ 必须让 LLM 能同时给出"族 + 族内成员"，代码只做归属校验（防越权）。
#
# 同时这也回答「一次识别能否匹配多个簇」：能。LLM 返回 `clusters` 数组，
# 代码取第一个作主意图（**detect() 的单值契约不变**），其余写进 meta 供
# 编排层决定 team/direct，不打断既有 `_done` / 缓存 / 落库 / 澄清卡链路。
# ─────────────────────────────────────────────────────────────────────────────


def clusters_for_llm_detailed() -> str:
    """给 LLM 的族清单（带族内可选成员名）。成员名是**白名单**，代码会校验。"""
    lines = []
    for cid in ROUTE_CLUSTERS:
        c = CLUSTERS[cid]
        lines.append("- %s（%s）：%s" % (cid, c["title"], c["desc"]))
        if len(c["members"]) > 1:
            lines.append("    族内可选成员：%s" % "、".join(c["members"]))
    return "\n".join(lines)


def parse_cluster_output(raw: str, known_agents: set) -> dict:
    """解析 LLM 的多簇输出；返回 {primary, confidence, clusters:[{id,agent,confidence}]}。

    兼容三种形态（老模型/降级/新格式都能吃）：
      1. `{"clusters":[{"id":"verify","agent":"model_validation_repair",...}], "confidence":0.9}`
      2. `{"intent":"verify","confidence":0.9}`（旧族模式）
      3. 裸文本 `{"intent":"design","confidence":0.9}`（旧 Agent 模式）

    **归属校验**：`agent` 必须真的属于该族成员表，否则丢弃并回落 `intra_pick`。
    这是安全边界 —— 不能让模型凭一句话就把执行主体换成任意 Agent。
    """
    s = str(raw or "").strip()
    out = {"primary": "", "confidence": 0.0, "clusters": []}
    # 取最后一个 JSON 对象（模型可能先写分析再给结论）
    objs = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", s)
    data = None
    for o in reversed(objs):
        try:
            d = json.loads(o)
            if isinstance(d, dict) and (d.get("clusters") or d.get("intent") or d.get("id")):
                data = d
                break
        except Exception:
            continue
    if not data:
        return out

    items = []
    arr = data.get("clusters")
    if isinstance(arr, list) and arr:
        for it in arr:
            if not isinstance(it, dict):
                continue
            cid = str(it.get("id") or it.get("cluster") or "").strip()
            if cid not in CLUSTERS:
                continue
            agent = str(it.get("agent") or "").strip()
            if agent and agent not in CLUSTERS[cid]["members"]:
                agent = ""            # 越权成员 ⇒ 丢弃，由代码回落族内分工
            items.append({"id": cid, "agent": agent,
                          "confidence": float(it.get("confidence") or 0.0)})
    else:
        cid = str(data.get("intent") or data.get("id") or "").strip()
        if cid:
            items.append({"id": cid, "agent": "",
                          "confidence": float(data.get("confidence") or 0.0)})
    if not items:
        return out
    out["clusters"] = items
    out["primary"] = items[0]["id"]
    out["confidence"] = items[0]["confidence"] or float(data.get("confidence") or 0.0)
    return out


def resolve_clusters(parsed: dict, text: str, known_agents: set) -> dict:
    """多簇 → {primary_agent, clusters:[{id,agent,confidence}], extras:[agent]}。

    `primary_agent` 是**唯一**交给 `_done` 的值 ⇒ 对外契约仍是单意图。
    `extras` 是本次识别命中的其它族的代表，供编排层/前端展示，不改变执行主体。
    """
    out = {"primary_agent": "", "clusters": [], "extras": []}
    items = parsed.get("clusters") or []
    for i, it in enumerate(items):
        cid = it["id"]
        agent = it.get("agent") or ""
        if agent and agent in known_agents:
            chosen = agent
        elif cid in CLUSTERS:
            chosen = intra_pick(cid, text)
        else:
            chosen = cid if cid in known_agents else ""
        it = dict(it, agent=chosen)
        out["clusters"].append(it)
        if i == 0:
            out["primary_agent"] = chosen
        elif chosen and chosen not in out["extras"]:
            out["extras"].append(chosen)
    return out


def score_clusters(text: str, keywords_by_agent: dict, max_clusters: int = 3) -> list:
    """**确定性多簇打分**（纯代码、零 LLM）：返回 [{id, agent, confidence}]，按分降序。

    为什么必须有这一层（2026-11-07 实测）：`verify_multi_cluster.py` 首版把多簇
    只放在 LLM 兜底层，结果复合句「先需求条目化→再设计架构→最后导出报告」被
    **确定性规则层抢先**（route=fused），LLM 根本没被调用 ⇒ clusters=[]。
    标杆做法（vLLM Semantic Router 多信号 / Dify 多意图召回）都是**先多标签打分**
    再决定是否升级为编排，而不是把多意图系在兜底路径上。

    数据源仍是**单一事实源**：`keywords_by_agent` = `IntentRouter._db_intents`
    （来自 agents.intent_keywords，新建 Agent 自动生效），族描述只作补充信号。
    ⚠️ 纯旁路观测：不参与 detect() 的主返回判定，只写 meta 供编排层决定 team/direct。
    """
    t = str(text or "").lower()
    if not t:
        return []
    scores = {cid: 0.0 for cid in ROUTE_CLUSTERS}
    member_hits = {cid: {} for cid in ROUTE_CLUSTERS}
    for agent, kws in (keywords_by_agent or {}).items():
        cid = cluster_of_agent(agent)
        if cid not in scores or cid == "orchestration":
            continue
        for kw in kws or []:
            k = str(kw or "").strip().lower()
            if len(k) < 2 or k not in t:
                continue
            w = min(1.0, len(k) / 4.0)          # 长专有词权重更高，与既有打分口径一致
            scores[cid] += w
            member_hits[cid][agent] = member_hits[cid].get(agent, 0.0) + w
    # 族描述里的典型说法作补充（覆盖"没有专属关键词但语义明确"的表述）
    for cid in ROUTE_CLUSTERS:
        for frag in re.split(r"[、,，/；;]", CLUSTERS[cid].get("hint", "")):
            f = frag.strip().lower()
            if len(f) >= 3 and f in t:
                scores[cid] += 0.6
    ranked = sorted((c for c in ROUTE_CLUSTERS if scores[c] > 0),
                    key=lambda c: -scores[c])[:max_clusters]
    if not ranked:
        return []
    top = scores[ranked[0]]
    out = []
    for i, cid in enumerate(ranked):
        s = scores[cid]
        # 次族必须达到主族的 35%，且至少 1.0 分 ⇒ 防止一个弱词就触发 team 编排
        if i > 0 and (s < 1.0 or s < top * 0.35):
            continue
        best_agent = ""
        if member_hits[cid]:
            best_agent = max(member_hits[cid], key=member_hits[cid].get)
        out.append({"id": cid, "agent": best_agent or intra_pick(cid, text),
                    "confidence": round(min(0.95, 0.55 + s / 10.0), 2),
                    "score": round(s, 2), "source": "deterministic"})
    return out


def redundancy_report(conn) -> dict:
    """只读体检：列出可聚合精简项 + 影响范围。**不执行任何删除**。"""
    out: dict = {"duplicates": [], "by_cluster": {}, "stale_plugins": []}
    for cid in ROUTE_CLUSTERS:
        out["by_cluster"][cid] = {
            "title": CLUSTERS[cid]["title"],
            "agents": list(CLUSTERS[cid]["members"]),
            "count": len(CLUSTERS[cid]["members"]),
        }
    # 同名技能 / 同名 Agent 展示名
    for table, col in (("skills", "name"), ("plugins", "name")):
        try:
            rows = conn.execute(
                "SELECT %s, COUNT(*) n, GROUP_CONCAT(status) st FROM %s "
                "GROUP BY %s HAVING n>1 ORDER BY n DESC" % (col, table, col)).fetchall()
        except Exception:
            continue
        for r in rows:
            out["duplicates"].append({"table": table, "name": r[0],
                                      "copies": r[1], "statuses": r[2]})
    # 探针残留（门禁自测产生的临时插件）
    try:
        for r in conn.execute(
                "SELECT name,type,status FROM plugins WHERE name LIKE 'gate-%probe%' "
                "OR name LIKE 'hil-probe%'"):
            out["stale_plugins"].append({"name": r[0], "type": r[1], "status": r[2]})
    except Exception:
        pass
    return out


__all__ = ["CLUSTERS", "ROUTE_CLUSTERS", "AGENT_CLUSTER", "CLUSTER_VERSION",
           "cluster_of_agent", "cluster_of_asset", "clusters_for_llm",
           "clusters_for_llm_detailed", "parse_cluster_output", "resolve_clusters",
           "score_clusters", "intra_pick", "resolve_cluster", "narrow_candidates",
           "coverage", "asset_clusters", "redundancy_report"]
