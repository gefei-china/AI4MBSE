# -*- coding: utf-8 -*-
"""P2-4 多画布流拓扑台账与结构体检：画布缺陷检测 + 同构去重（只记录，不改拓扑）。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P2 第四项）
═══════════════════════════════════════════════════════════════
路线图 P2 第四项原文「多画布流拓扑实验」。落地前先勘察了真实库，得到两个
决定性事实，它们直接改写了这一项的口径：

**事实一：运行证据不足以支撑拓扑实验。**
  agent_flows 62 条画布定义，flow_runs 只有 11 条、flow_run_steps 33 条，
  其中 9 条是同日三个同构画布各跑 3 次（总延迟 49–82ms，秒级/mock 形态），
  只有 run 198 是真实长时运行（94.8s / 6 步 / 1 个 error）。
  在 11 条运行记录上比较两种拓扑的优劣，得到的是噪声不是结论。
  **实验的前提是先有可比较的台账**，故本期做台账与体检，不做实验。

**事实二：同一条路线图的安全红线 #6 写着「不碰权重、不碰架构拓扑」。**
  自动生成新拓扑并自动切换生效画布，直接违反这条自己定的红线。
  故本期的铁律是：**只读画布、只记录缺陷、只建议去重，绝不写agent_flows**
  （与 P1-4「配置回滚过人」、P2-3「档案库是记忆不是手」同族纪律）。

真实库同时暴露了一个此前没人看过的现象，正是本项的价值所在：
  62 条画布里 **25 条是 (3 节点, 2 边) 完全同构**，且都命名为
  「需求分析流水线<13 位时间戳>」，source=planner_auto ——自动编排在
  反复生成同一个拓扑，画布数在涨、可辨识的拓扑没有涨。这是台账看不见、
  只看"画布数量"这个指标的看板永远发现不了的问题。

═══════════════════════════════════════════════════════════════
设计纪律（与 core/agent_archive、core/curriculum 同构）
═══════════════════════════════════════════════════════════════
1. 拓扑指纹 = sha1(节点 id 集合 + 有向边集合 + 节点类型计数)，**不含
   name/version/label/时间戳**。改个名字或版本号不该造出新拓扑
   （与 P2-3 变体指纹同型问题：指纹含易变字段会让台账注水）。
2. 结构缺陷规则注册表：每条规则一个纯函数 detect(flow) → 0..n 个缺陷，
   **规则内零 I/O**（画布字典由scan 侧一次读齐后传入）。
   六条规则按危害分档：
   · dangling_edge        边指向不存在的节点 ⇒ 执行期必然断链
   · isolated_node        入度=0 且出度=0 的节点（除单节点画布）⇒ 永不被执行
   · no_sink              多节点画布没有任何汇点 ⇒ 可能无限串行下去
   · unresolvable_type节点 config 引用了不存在的 tool/skill/agent ⇒ 运行期失败
   · unreachable_branch   拓扑上不可达的节点（从入度 0 出发走不到）
   · linear_degenerate    ≥linear_degenerate_min 节点却是纯链⇒ 无并行度
3. 同构分组只报**结构同构**（指纹相同），不报"相似"——相似度需要阈值，
   阈值是我拍的，拍出来的相似度不作数。
4. 诚实三态：defect 率、run 覆盖率的**分母不足即 unknown**，
   **不回落 0**（与 P2-1/P2-2 同型坑：空窗口是"没数据"，不是"没问题"）。
   summary.verdict 的 pass 证据只允许来自 lower_better 维度 value==0。
5. 缺陷严重度只做两档（blocker / warn），不做可配阈值打分 ——
   没有先验支撑"3 个 warn 比 2 个 blocker+1 个 warn 更糟"。
6. **只读 agent_flows / flow_runs，绝不写**。写只经
   repositories/flow_topology_repo.py 的两张自有表。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

# ── 产品契约常量（API/门禁/前端三方共用，勿在调用处另写字面量）─────────────

#: 结构缺陷类别 → 严重度。blocker = 执行期必然出问题；warn = 结构性可疑。
DEFECT_SEVERITY = {
    "dangling_edge": "blocker",
    "unresolvable_type": "blocker",
    "isolated_node": "blocker",
    "no_sink": "warn",
    "unreachable_branch": "warn",
    "linear_degenerate": "warn",
}

#: verdict 三态取值
VERDICT_CLEAN = "clean"
VERDICT_DEFECTIVE = "defective"
VERDICT_UNKNOWN = "insufficient_data"

#: 配置缺省值（缺配置回落这些，**不是第二套真值**——真值在 core/config.py
#: 的 DEFAULT_CONFIG + CONFIG_SCHEMA 双登记里，此处只做类型与容错兜底）
_SETTINGS_DEFAULTS = {
    "enabled": True,
    "min_denominator": 2,
    "linear_degenerate_min": 4,
    "max_canvases": 2000,
}


@dataclass(frozen=True)
class DefectRule:
    """一条结构缺陷规则的注册条目。"""
    name: str
    label: str
    desc: str
    detect: Callable[[dict], list]


# ── 纯函数层：拓扑解析 ──────────────────────────────────────────────────────

def parse_flow(row: dict) -> dict:
    """把 agent_flows 一行解析成 {id,name,nodes,edges,...} 规范结构。

    容错：nodes/edges 可能是 None、非法 JSON、JSON 但不是 list ——
    一律回落空列表并在 parse_errors 里留痕（不留痕的话"画布损坏"会被
    当成"画布是空的"，两者对体检结论完全不同）。
    """
    parse_errors: list = []
    nodes = _load_list(row.get("nodes"), parse_errors, "nodes")
    edges = _load_list(row.get("edges"), parse_errors, "edges")
    norm_nodes = []
    for n in nodes:
        if not isinstance(n, dict):
            parse_errors.append("节点不是对象: %r" % (type(n).__name__,))
            continue
        norm_nodes.append({
            "id": str(n.get("id") or "").strip(),
            "type": str(n.get("type") or "").strip(),
            "label": str(n.get("label") or ""),
            "config": n.get("config") if isinstance(n.get("config"), dict) else {},
        })
    norm_edges = []
    for e in edges:
        if not isinstance(e, dict):
            parse_errors.append("边不是对象: %r" % (type(e).__name__,))
            continue
        norm_edges.append({
            "source": str(e.get("source") or "").strip(),
            "target": str(e.get("target") or "").strip(),
        })
    return {
        "id": int(row.get("id") or 0),
        "name": str(row.get("name") or ""),
        "version": str(row.get("version") or ""),
        "status": str(row.get("status") or ""),
        "source": str(row.get("source") or ""),
        "nodes": norm_nodes,
        "edges": norm_edges,
        "parse_errors": parse_errors,
    }


def _load_list(raw, parse_errors: list, field: str) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            v = json.loads(s)
        except (ValueError, TypeError):
            parse_errors.append("%s 不是合法 JSON" % field)
            return []
        if isinstance(v, list):
            return v
        parse_errors.append("%s JSON 顶层不是数组" % field)
        return []
    parse_errors.append("%s 类型异常: %r" % (field, type(raw).__name__))
    return []


def structure_of(flow: dict) -> dict:
    """提取拓扑结构：节点 id 集合、有向边集合、类型计数、入出度。

    这是所有缺陷规则与指纹的唯一共同底座——规则之间不允许各自解析，
    否则同一份 nodes/edges 会被不同规则读出不同结构（规则间口径分裂）。
    """
    node_ids = [n["id"] for n in flow.get("nodes") or [] if n["id"]]
    id_set = set(node_ids)
    edges = [{"source": e["source"], "target": e["target"]}
             for e in flow.get("edges") or []]
    # 自环保留（自环是真实缺陷信号），但统计边数时算进去
    indeg: dict = {nid: 0 for nid in node_ids}
    outdeg: dict = {nid: 0 for nid in node_ids}
    for e in edges:
        if e["source"] in outdeg:
            outdeg[e["source"]] += 1
        if e["target"] in indeg:
            indeg[e["target"]] += 1
    type_counts: dict = {}
    for n in flow.get("nodes") or []:
        if n["id"]:
            type_counts[n["type"] or "(空类型)"] = type_counts.get(n["type"] or "(空类型)", 0) + 1
    return {
        "node_ids": node_ids,
        "id_set": id_set,
        "edges": edges,
        "indeg": indeg,
        "outdeg": outdeg,
        "type_counts": type_counts,
        "node_count": len(node_ids),
        "edge_count": len(edges),
    }


def topology_fingerprint(flow: dict) -> str:
    """结构指纹 = sha1(排序后的节点 id + 排序后的有向边 + 排序后的类型计数)。

    **不含** name / version / label / 时间戳 / source：这些字段变了但拓扑
    没变时，指纹必须相同——否则每改一次名字就造出一个"新拓扑"，台账注水
    （与 P2-3 变体指纹同型坑）。
    """
    st = structure_of(flow)
    parts = [
        "nodes=" + ",".join(sorted(st["node_ids"])),
        "edges=" + ",".join(sorted("%s>%s" % (e["source"], e["target"])
                                   for e in st["edges"])),
        "types=" + ",".join("%s:%d" % (k, st["type_counts"][k])
                            for k in sorted(st["type_counts"])),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def reachable_from_sources(flow: dict) -> set:
    """从入度 0 的节点出发沿边可达的节点集合（不跟随悬空边）。"""
    st = structure_of(flow)
    adj: dict = {nid: [] for nid in st["node_ids"]}
    for e in st["edges"]:
        if e["source"] in adj and e["target"] in st["id_set"]:
            adj[e["source"]].append(e["target"])
    frontier = [nid for nid in st["node_ids"] if st["indeg"].get(nid, 0) == 0]
    seen = set(frontier)
    stack = list(frontier)
    while stack:
        cur = stack.pop()
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ── 缺陷规则层（纯函数，零 I/O）────────────────────────────────────────────

def _d_dangling_edge(flow: dict) -> list:
    st = structure_of(flow)
    out = []
    for e in st["edges"]:
        for side in ("source", "target"):
            if e[side] not in st["id_set"]:
                out.append({"defect": "dangling_edge", "detail": "边 %s 引用了不存在的节点 %s"
                            % ("%s>%s" % (e["source"], e["target"]), e[side])})
    return out


def _d_isolated_node(flow: dict) -> list:
    st = structure_of(flow)
    if st["node_count"] <= 1:
        return []          # 单节点画布没有边，入出度都是 0 不是缺陷
    return [{"defect": "isolated_node", "detail": "节点 %s 无入边也无出边，永不被执行" % nid}
            for nid in st["node_ids"]
            if st["indeg"].get(nid, 0) == 0 and st["outdeg"].get(nid, 0) == 0]


def _d_no_sink(flow: dict) -> list:
    st = structure_of(flow)
    if st["node_count"] <= 1:
        return []
    if any(st["outdeg"].get(nid, 0) == 0 for nid in st["node_ids"]):
        return []          # 有汇点，通过
    return [{"defect": "no_sink", "detail": "画布没有任何汇点（每节点都有出边）"}]


def _d_unresolvable_type(flow: dict) -> list:
    """节点 config 引用了不存在的 tool / skill / agent。

    可解析性由调用方注入 available（库里实际存在的 id 集合）——规则本身
    不知道库长什么样，硬编码 id 列表就是 fabricate。
    """
    available = flow.get("_available") or {}
    out = []
    for n in flow.get("nodes") or []:
        if not n["id"]:
            continue
        for key, bucket in (("tool", "tools"), ("skill", "skills"),
                            ("agent", "agents")):
            ref = (n.get("config") or {}).get(key)
            # bucket 才是 available 里的集合名（key 是 config 字段名）——
            # 两个都用 key 会让规则对每一类都静默跳过（探针实测漏检）。
            if not ref or bucket not in available:
                continue    # 该类不做可解析性核对（如库表无对应概念）
            pool = available.get(bucket) or set()
            if ref not in pool:
                out.append({"defect": "unresolvable_type",
                            "detail": "节点 %s 引用 %s=%s 但目标不存在"
                                      % (n["id"], key, ref)})
    return out


def _d_unreachable_branch(flow: dict) -> list:
    st = structure_of(flow)
    reach = reachable_from_sources(flow)
    # 单节点画布无入度 0 之外的可达问题；孤立节点已由 isolated_node 负责
    return [{"defect": "unreachable_branch", "detail": "节点 %s 从任何起点都走不到" % nid}
            for nid in st["node_ids"]
            if nid not in reach and not (st["indeg"].get(nid, 0) == 0
                                         and st["outdeg"].get(nid, 0) == 0)]


def _d_linear_degenerate(flow: dict) -> list:
    st = structure_of(flow)
    floor = int(flow.get("_linear_floor") or 4)
    if st["node_count"] < floor:
        return []
    if st["edge_count"] == st["node_count"] - 1 and max(
            (st["outdeg"].get(n, 0) for n in st["node_ids"]), default=0) <= 1:
        return [{"defect": "linear_degenerate",
                 "detail": "%d 个节点是纯串行链，无并行度" % st["node_count"]}]
    return []


DEFECT_RULES: dict = {}
for _r in (DefectRule("dangling_edge", "悬空边", "边引用不存在的节点",
                      _d_dangling_edge),
           DefectRule("isolated_node", "孤立节点", "无入无出，永不被执行",
                      _d_isolated_node),
           DefectRule("no_sink", "无汇点", "每节点都有出边，可能不收敛",
                      _d_no_sink),
           DefectRule("unresolvable_type", "引用不可解析",
                      "config 引用的 tool/skill/agent 不存在", _d_unresolvable_type),
           DefectRule("unreachable_branch", "不可达分支", "从起点走不到的节点",
                      _d_unreachable_branch),
           DefectRule("linear_degenerate", "串行退化", "纯链无并行",
                      _d_linear_degenerate)):
    DEFECT_RULES[_r.name] = _r


def detect_defects(flow: dict) -> list:
    """对一幅画布跑全部缺陷规则，返回带严重度的缺陷列表（按类别+节点排序）。

    parse_errors 里的坏数据也会变成一条 blocker 缺陷：画布本身读不出来
    是最严重的问题，静默当空画布会把它伪装成健康画布。
    """
    out: list = []
    for err in flow.get("parse_errors") or []:
        out.append({"defect": "dangling_edge", "severity": "blocker",
                    "detail": "画布定义损坏: %s" % err})
    for name in sorted(DEFECT_RULES):
        for d in DEFECT_RULES[name].detect(flow) or []:
            out.append({
                "defect": name,
                "severity": DEFECT_SEVERITY.get(name, "warn"),
                "detail": d.get("detail", ""),
            })
    out.sort(key=lambda d: (d["defect"], d["detail"]))
    return out


# ── 台账收敛层 ─────────────────────────────────────────────────────────────

def variant_row(flow: dict, defects: list, run_stats: dict) -> dict:
    """把一幅画布的体检结果收敛成一行台账记录。

    run_stats 由调用方从 flow_runs 统计注入（runs / failed_runs /
    covered：至少跑过一次的画布才算有运行证据）。
    """
    fingerprint = topology_fingerprint(flow)
    by_cat: dict = {}
    for d in defects:
        by_cat[d["defect"]] = by_cat.get(d["defect"], 0) + 1
    return {
        "flow_id": int(flow.get("id") or 0),
        "flow_name": str(flow.get("name") or ""),
        "fingerprint": fingerprint,
        "node_count": structure_of(flow)["node_count"],
        "edge_count": structure_of(flow)["edge_count"],
        "status": str(flow.get("status") or ""),
        "source": str(flow.get("source") or ""),
        "blocker_count": sum(1 for d in defects if d["severity"] == "blocker"),
        "warn_count": sum(1 for d in defects if d["severity"] == "warn"),
        "defects_json": json.dumps(
            {"items": defects, "by_category": by_cat},
            ensure_ascii=False, sort_keys=True),
        "runs": int(run_stats.get("runs") or 0),
        "failed_runs": int(run_stats.get("failed_runs") or 0),
        "evidence_state": "covered" if int(run_stats.get("runs") or 0) > 0 else "uncovered",
    }


def group_isomorphic(variants: list) -> list:
    """按结构指纹分组，返回同构组（只含≥2 条的组）。

    **只报结构同构，不报"相似"**：相似度要阈值，阈值是我拍的，拍出来的
    相似度不作数（同P2-3「不猜」纪律的延伸）。
    """
    buckets: dict = {}
    for v in variants:
        buckets.setdefault(v.get("fingerprint") or "", []).append(v)
    groups = []
    for fp, members in buckets.items():
        if not fp or len(members) < 2:
            continue
        members = sorted(members, key=lambda x: (x.get("flow_id") or 0))
        groups.append({
            "fingerprint": fp,
            "size": len(members),
            "shape": "%d节点/%d边" % (members[0].get("node_count") or 0,
                                       members[0].get("edge_count") or 0),
            "flow_ids": [m.get("flow_id") for m in members],
            "flow_names": [m.get("flow_name") for m in members][:10],
            "sources": sorted({m.get("source") or "" for m in members}),
        })
    groups.sort(key=lambda g: (-g["size"], g["fingerprint"]))
    return groups


def summarize(variants: list, groups: list, *,
              min_denominator: int = 2) -> dict:
    """体检总览：三态verdict + 六个维度画像。

    ★诚实判据（P2-3 冒烟抓出的同型缺陷）：pass 证据**只允许**来自
    lower_better 维度且value==0（真的一个都没有）；分母不足的维度记
    unknown **不回落 0**——「没跑过」不是「跑了零次」，前者无信息、
    后者是坏消息，混为一谈会让看板把未知显示成健康。
    """
    total = len(variants)
    floor = max(1, int(min_denominator or 1))

    blocker = sum(int(v.get("blocker_count") or 0) for v in variants)
    warn = sum(int(v.get("warn_count") or 0) for v in variants)
    duplicate_members = sum(g["size"] - 1 for g in groups)
    covered = sum(1 for v in variants if v.get("evidence_state") == "covered")
    failed = sum(int(v.get("failed_runs") or 0) for v in variants)

    dims = [
        {"name": "blocker_defects", "value": blocker, "lower_better": True,
         "desc": "执行期必然出问题的结构缺陷总数"},
        {"name": "warn_defects", "value": warn, "lower_better": True,
         "desc": "结构性可疑缺陷总数"},
        {"name": "duplicate_canvases", "value": duplicate_members, "lower_better": True,
         "desc": "结构完全同构的冗余画布数（组内除首个外）"},
        {"name": "isomorphic_groups", "value": len(groups), "lower_better": True,
         "desc": "同构组数"},
        {"name": "run_coverage", "value": (round(covered * 100.0 / total, 1)
                                          if total else None),
         "lower_better": False,
         "desc": "有运行证据的画布占比%%（%d/%d）" % (covered, total)},
        {"name": "failed_runs", "value": failed if covered >= floor else None,
         "lower_better": True,
         "desc": "失败运行数（有运行证据的画布不足 %d 幅时记unknown）" % floor},
    ]

    known = [d for d in dims if d["value"] is not None]
    zero_evidence = [d for d in known
                     if d["lower_better"] and d["value"] == 0]
    if total == 0:
        #★ 冒烟实测抓到的真缺陷（承 P2-1/P2-2 同型坑）：没有任何画布时
        #   blocker/warn/dup 三维都是 0，zero_evidence 非空 ⇒ 旧逻辑判
        #   「clean」。**一幅画布都没有不是"零个缺陷"，是没有可体检的对象**
        #   ——空窗口不是 0 分。必须在算零证据之前拦掉。
        verdict = VERDICT_UNKNOWN
        reason = "没有任何画布定义，无可体检对象（空窗口不是 0 分）"
    elif blocker > 0:
        # ★判据顺序也实测修正过：blocker>0 是**已确知的坏**，必须直接判
        #   defective；若先问「有没有维度归零」，blocker 一旦 >0 就没有
        #   任何 lower_better 维度归零，会掉进 unknown 分支——把「明确有
        #   缺陷」显示成「数据不足」，方向完全反了（探针实测：2 blocker
        #   的库曾报insufficient_data）。
        verdict = VERDICT_DEFECTIVE
        reason = "存在 %d 个 blocker 级结构缺陷" % blocker
    elif not known or not zero_evidence:
        verdict = VERDICT_UNKNOWN
        reason = ("分母不足，无维度可作 pass 证据（空窗口不是 0 分）"
                  if not known else "无任何 lower_better 维度归零，不足以判 clean")
    elif any(d["lower_better"] and (d["value"] or 0) > 0 for d in known):
        verdict = VERDICT_DEFECTIVE
        reason = "存在 %d 组同构冗余或 %d 个 warn 缺陷" % (
            duplicate_members, warn)
    else:
        verdict = VERDICT_CLEAN
        reason = "全部 lower_better 维度归零"

    return {
        "canvases": total,
        "isomorphic_groups": len(groups),
        "duplicate_canvases": duplicate_members,
        "covered_canvases": covered,
        "uncovered_canvases": total - covered,
        "dimensions": dims,
        "summary": {"verdict": verdict, "reason": reason},
    }


# ── 采集层（只读）──────────────────────────────────────────────────────────

def collect_flows(conn: sqlite3.Connection, *, limit: int = 2000) -> list:
    """读全部画布定义（只读 agent_flows）。"""
    lim = max(1, min(int(limit or 2000), 10000))
    rows = conn.execute(
        "SELECT id,name,version,status,source,nodes,edges FROM agent_flows "
        "ORDER BY id ASC LIMIT ?", (lim,)).fetchall()
    out = []
    for r in rows:
        out.append(parse_flow({
            "id": r[0], "name": r[1], "version": r[2], "status": r[3],
            "source": r[4], "nodes": r[5], "edges": r[6],
        }))
    return out


def collect_run_stats(conn: sqlite3.Connection) -> dict:
    """按 flow_id 统计运行次数/失败次数（只读 flow_runs）。

    失败判据：status !='completed'（表注释只有 completed | partial，
    running 是真实库里出现过的第三态——归入非完成即失败，不装作没看见）。
    """
    stats: dict = {}
    for r in conn.execute(
            "SELECT flow_id, status, COUNT(*) FROM flow_runs GROUP BY flow_id, status"):
        fid = int(r[0] or 0)
        n = int(r[2] or 0)
        s = stats.setdefault(fid, {"runs": 0, "failed_runs": 0})
        s["runs"] += n
        if str(r[1] or "") != "completed":
            s["failed_runs"] += n
    return stats


def collect_available_refs(conn: sqlite3.Connection) -> dict:
    """采集可解析引用的 id 集合（tool/skill/agent），供 unresolvable_type 用。

    某一类表不存在时该类返回 None → 规则跳过（不做无据推断）。
    """
    out: dict = {}
    probes = {
        "tools": "SELECT DISTINCT name FROM tools",
        "skills": "SELECT name FROM skills",
        "agents": "SELECT name FROM agents",
    }
    for bucket, sql in probes.items():
        try:
            out[bucket] = {str(x[0]) for x in conn.execute(sql) if x and x[0]}
        except sqlite3.Error:
            out[bucket] = None
    return out


def get_settings(cfg: Optional[dict] = None) -> dict:
    """取材口径。缺配置回落 _SETTINGS_DEFAULTS（与 archive 同范式）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        if cfg is not None:
            raw = cfg.get(key, default)
            out[key] = (bool(raw) if key == "enabled"
                        else type(default)(raw))
            continue
        if key == "enabled":
            out[key] = _config.as_bool("flow_topology", key, bool(default))
            continue
        try:
            out[key] = type(default)(_config.get("flow_topology", key, default))
        except Exception:
            out[key] = default
    return out


# ── 引擎入口 ───────────────────────────────────────────────────────────────

def scan(conn: sqlite3.Connection, *, settings: Optional[dict] = None,
         collect: bool = True) -> dict:
    """扫描全库画布：体检 + 同构分组 + 总览。

    collect=False 时只做纯函数计算不读库（门禁 A 段用）。
    """
    st = settings or get_settings()
    if collect:
        flows = collect_flows(conn, limit=st["max_canvases"])
        run_stats = collect_run_stats(conn)
        available = collect_available_refs(conn)
    else:
        flows, run_stats, available = [], {}, {}

    variants = []
    for f in flows:
        f = dict(f)
        f["_available"] = available
        f["_linear_floor"] = st["linear_degenerate_min"]
        defects = detect_defects(f)
        variants.append(variant_row(f, defects, run_stats.get(f["id"], {})))

    groups = group_isomorphic(variants)
    overview = summarize(variants, groups, min_denominator=st["min_denominator"])
    if not collect:
        overview["summary"]["reason"] = (
            "collect=False 纯函数自检，不代表真实库状况")
    return {"settings": st, "variants": variants, "groups": groups,
            "overview": overview}


def record_scan(conn: sqlite3.Connection, result: dict, *,
                repo: Optional[object] = None) -> dict:
    """把 scan 结果写入台账（写只经 repo，引擎本身不执行 SQL 写入）。"""
    from repositories.flow_topology_repo import FlowTopologyRepo
    r = repo or FlowTopologyRepo(conn)
    created = 0
    for v in result.get("variants") or []:
        if r.upsert_variant(v):
            created += 1
    touched = r.refresh_all(result.get("variants") or [])
    return {"variants_written": len(result.get("variants") or []),
            "variants_created": created,
            "variants_refreshed": touched,
            "groups": len(result.get("groups") or [])}