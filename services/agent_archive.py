# -*- coding: utf-8 -*-
"""P2-3 Agent 档案库/种群（DGM 式 archive）：进化历史 → 变体台账 + 死路记忆。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P2 第三项）
═══════════════════════════════════════════════════════════════
行业参照：Deep Generative Models（DGM）/ AlphaZero 谱系——每个个体是一份
可回溯的基因型，种群按适应度排序，**死掉的个体被归档记住**（不是删掉），
避免系统反复重走已知死路。

本系统此刻的进化历史散在表里彼此不通气：asset_change_logs（字段级 diff +
version_from/version_to，P0-2 落点）、config_history（配置晋升台账 P1-4）、
sandbox_runs（影子评估 P1-4）、misevolution_checks（安全复测 P2-1）、
agents.version 自动递升。缺的一环：**没有"种群"视角**——问不出
"这个 agent 试过几版？哪一版被改回去过？哪个字段在反复横跳？"于是提案器会
反复提议同一条已知会回归的路，人的时间浪费在同一面墙上。

本模块补上这一环：把进化历史**收敛成变体台账**（agent_variants，
一行 = 一个 (agent, version) 变体）+ 每变体的适应度画像 + 结局记录
（variant_outcomes，一行 = 一次外部结论）。

═══════════════════════════════════════════════════════════════
设计纪律（承 P1-3/P1-4/P2-1/P2-2 全部铁律）
═══════════════════════════════════════════════════════════════
1. **只读历史，不改历史**：引擎只 SELECT agents / asset_change_logs /
   variant_outcomes；写只经 repositories/agent_archive_repo.py 落两张自家
   新表。**绝不写 agents.version、不切换生效 agent、不回滚任何配置**
   （回滚过人的铁律同 P1-4：档案库是记忆，不是手）。
2. **画像只用真实可算的证据**（本项最重要的诚实纪律）：
   真实库里 sandbox_runs / misevolution_checks / bad_cases / config_history
   都是**提案级或会话级**，没有 agent_id + version 列。把它们硬关联到某个
   agent 变体需要一次并不存在的接线——那是 fabricate。故画像的五维全部
   从 **asset_change_logs 的字段级 diff**（P0-2 已逐字段落库，真实）+ 本模块
   自家的 variant_outcomes 推导。
   · 外部评估结论（sandbox / 安全复测）通过 **record_outcome() 显式挂载**
     提供入户口（人或上游模块明确知道"这次评估针对哪个变体"时才挂），
     挂不上就诚实地缺席——档案库不猜。
3. **核心信号：字段级震荡（oscillation）**。同一 (agent, 字段) 上出现
   A→B→A 的改回循环 = 死路的天然证据：系统试了又认输。这是从既有
   asset_change_logs 直接算得出的真事实，且比任何评分都更能指示
   "这条路走过、失败、又想走"。
4. **适应度是"画像"不是"分数"**：五维各自独立判分，方向在
   Dimension 里显式声明（direction 驱动，杜绝 P2-2 的方向/量纲坑）。
   summary.verdict 三态：
     · 有负向证据（lower_better 维度 value>0 或回归>0）⇒ regression；
     · 有 lower_better 维度且 value==0（真"无问题"的观测）⇒ pass；
     · 其余（有信息但全是探索读数 / 无 sufficient 维度）⇒
       insufficient_data，**诚实留白**。
   关键一条：**lower_better=False 的探索读数（lineage_depth）不作为 pass
   证据**——深度 0 是下界值即"没探索过"，拿没数据当好消息是假的（冒烟实测
   踩到：4/5 维 unknown 时曾误判 pass）。
5. **诚实 unknown**：维度分母不足 min_denominator ⇒ value=None、
   verdict=unknown——**绝不回落 0**（空窗口不是 0 分，P2-1 教训）。
6. **死路记忆**：某变体的负向结局数达 dead_end_repeat ⇒ 标 dead_end；
   或该变体所属 agent 的字段震荡数达 oscillation_dead_end ⇒ 该变体记
   震荡警示。判定只看**已发生的事实**，没历史的新变体永远是 unrecorded，
   不是 clean。
7. **幂等**：fingerprint = sha1(agent_id|version)，重复扫描只跳过不注水
   （同版本被显式改动是真实语义，指纹必须稳定可重复）。
8. **零副作用可证**：scan()/refresh() 除自家两张表外任何表行数零变化
   （门禁全表快照断言）。

## 诚实边界（不 fabricate，承 P2-1）
· 「敏感数据外泄」无运行时检测信号 ⇒ 不入画像；
· 「用户满意度」无量化信号 ⇒ 不入画像（不用 badcase 复发冒充满意度）；
· 画像是**证据强度的方向性读数**，不是客观能力分——没有统一基准集就宣称
  fitness 是自欺，故 API/文档一律称 fitness_profile（画像）。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

# ── 产品契约常量（API/门禁/前端三方共用，勿在调用处另写字面位）─────────────
V_ACTIVE = "active"        # 在册当前版本
V_RETIRED = "retired"      # 已被后续版本取代（血缘推得，非现行）
V_DEAD_END = "dead_end"    # 判过回归且达重复阈值 ⇒ 死路记忆
VARIANT_STATUSES = (V_ACTIVE, V_RETIRED, V_DEAD_END)

OUTCOME_PASS = "pass"
OUTCOME_REGRESSION = "regression"
OUTCOME_ERROR = "error"            # 评估环境坏，结论无效（≠ 这条路不行）
OUTCOME_CLEAN = "clean"
OUTCOME_INSUFFICIENT = "insufficient_data"
OUTCOMES = (OUTCOME_PASS, OUTCOME_CLEAN, OUTCOME_REGRESSION,
            OUTCOME_ERROR, OUTCOME_INSUFFICIENT)

#: 负向结局（死路判定只认这些）
NEGATIVE_OUTCOMES = (OUTCOME_REGRESSION,)


def is_negative(outcome: str) -> bool:
    """结局是否负向（纯函数）。error / insufficient_data **不算**——
    环境坏或样本不足不等于这条路不行（P1-4 verdict:error 同语义）。"""
    return (outcome or "") in NEGATIVE_OUTCOMES


def grade_outcomes(outcomes, *, min_count: int = 1) -> dict:
    """一组结局 → 三态画像 {count, negative, positives, verdict}（纯函数）。

    诚实分母：命中数 <min_count ⇒ verdict=unknown（**不回落 clean/pass**）。
    命中任一负向 ⇒ regression；否则 pass。error / insufficient 计入 count
    但不判负——它们进 positives 是刻意留白让人下钻（不因"多数没报错"
    就把那次回归抹掉，negative 计数始终显式返回）。
    """
    vals = [str(o or "").strip() for o in (outcomes or [])]
    vals = [v for v in vals if v]
    n, neg = len(vals), sum(1 for v in vals if is_negative(v))
    out = {"count": n, "negative": neg, "positives": n - neg, "verdict": "unknown"}
    if n >= int(min_count):
        out["verdict"] = OUTCOME_REGRESSION if neg > 0 else OUTCOME_PASS
    return out


def variant_fingerprint(agent_id: int, version: str) -> str:
    """变体指纹 = sha1(agent_id|version)。

    只用 (资产, 版本) 两元组，**不能用字段 diff 内容**：同一版本号在不同
    时间被显式改动（update_agent 不传 version 时不递升）是真实语义，
    指纹若含内容就会每次扫描造出"新变体"、台账注水。
    """
    raw = "|".join([str(agent_id), str(version or "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def dead_end_of(negative_count: int, *, threshold: int) -> bool:
    """负向结局数达阈值 ⇒ 判死路（纯函数）。

    阈值语义：**同一变体被判回归 ≥threshold 次**才记死路——一次回归可能是
    噪声（并发/环境/偶发），反复才是模式。这是"记忆"的门槛，不是"结论"。
    """
    return int(negative_count or 0) >= int(threshold)


def count_oscillations(pairs) -> int:
    """字段级震荡计数（纯函数，本模块核心信号）。

    输入：同一 (asset, field) 的 [(old, new), ...] 时序对。
    判据：存在 A→B 且后续出现 B→A ⇒ A→B→A 的改回循环，计 1。
    这是死路的真事实证据——"改了又改回去"= 系统试过、认输、又有人想再试。

    **无序对去重**：A→B 与 B→A 是同一个震荡（一次来回，不是两次）。
    用 `a < b` 只在无序对的一侧计数来去重——早期版按有序对遍历会把同一
    震荡数成 2（门禁 A5a 实测抓到），震荡数虚高会让画像显得比实际更糟。

    不比较值的内容语义：old/new 为空的对（未知前值）跳过，不猜；
    自环（old==new）不计。
    """
    edges = set()
    for old, new in (pairs or []):
        o, n = str(old or ""), str(new or "")
        if o and n and o != n:
            edges.add((o, n))
    # a < b 只在无序对的一侧计数 ⇒ 一次来回记 1，不虚高
    return sum(1 for (a, b) in edges if (b, a) in edges and a < b)


@dataclass(frozen=True)
class Dimension:
    """画像的一维（方向必须显式声明——P2-2 方向/量纲坑的教训）。"""
    name: str
    label: str
    lower_better: bool      # True=数值大者差；False=数值大者好
    desc: str


DIMENSIONS = (
    Dimension("oscillation", "字段震荡数", True,
              "该变体相关字段上 A→B→A 的改回循环数——试过又认输的直接证据"),
    Dimension("field_changes", "字段改动数", True,
              "该版本引入的字段级 diff 条数（改动面大小；改动面失控易顾此失彼）"),
    Dimension("regressions", "负向结局数", True,
              "显式挂载到该变体的回归结论数（外部评估入口 record_outcome）"),
    Dimension("lineage_depth", "血缘深度", False,
              "该版本在版本链上的深度（从首个版本起数）——探索深度的方向性读数"),
    Dimension("field_diversity", "改动字段数", True,
              "该版本改动的不同字段数（越少越集中；改得越散越可能在乱试）"),
)
DIMENSION_NAMES = tuple(d.name for d in DIMENSIONS)
_LOWER_BETTER = {d.name: d.lower_better for d in DIMENSIONS}


def _cutoff(days: int) -> str:
    """窗口下界（本地时区口径，同 P1-1/P1-2/P2-1）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() - int(days) * 86400))


def _rows(conn, sql: str, params=()) -> list:
    """容错读：表未建（老库）返回空列表而非抛异常——档案库缺历史不是错误，
    但**绝不用空列表冒充"没问题"**（由各维度 unknown 语义承担诚实）。"""
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def collect_history(conn, *, cutoff: str) -> dict:
    """读齐进化历史（只读）：{agents, logs, field_pairs, version_depth}。

    field_pairs: {(asset_id, field): [(old,new), ...]} ——震荡检测的原料。
    version_depth: {(asset_id, version): 深度} ——血缘深度（按 version_from
    链回溯，带环保护：血缘可能有环或缺失，不死循环）。
    """
    agents = _rows(conn, "SELECT id, name, version, status FROM agents")
    logs = _rows(conn,
                 "SELECT asset_type, asset_id, asset_name, field, old_value, "
                 "new_value, version_from, version_to, created_at "
                 "FROM asset_change_logs WHERE created_at>=? ORDER BY id ASC",
                 (cutoff,))
    agent_logs = [r for r in logs if (r.get("asset_type") or "") == "agent"]

    field_pairs = {}
    version_of_edge = {}
    for r in agent_logs:
        key = (int(r["asset_id"]), str(r.get("field") or ""))
        field_pairs.setdefault(key, []).append(
            (r.get("old_value"), r.get("new_value")))
        if r.get("version_to"):
            version_of_edge[(int(r["asset_id"]), str(r["version_to"]))] = str(
                r.get("version_from") or "")

    # 血缘深度：version → version_from 回溯（带 visited 集合防环）
    depth = {}
    for a in agents:
        aid, ver = int(a["id"]), str(a.get("version") or "")
        if not ver:
            continue
        seen, d, cur = set(), 0, ver
        while cur and (aid, cur) in version_of_edge and (aid, cur) not in seen:
            seen.add((aid, cur))
            cur = version_of_edge[(aid, cur)]
            d += 1
            if d > 64:                    # 防御：异常深链不无限回溯
                break
        depth[(aid, ver)] = d
    # 历史版本（非现行）也登记深度
    for (aid, ver) in version_of_edge:
        if (aid, ver) in depth:
            continue
        seen, d, cur = {(aid, ver)}, 1, version_of_edge[(aid, ver)]
        while cur and (aid, cur) in version_of_edge and (aid, cur) not in seen:
            seen.add((aid, cur))
            cur = version_of_edge[(aid, cur)]
            d += 1
            if d > 64:
                break
        depth[(aid, ver)] = d
    return {"agents": agents, "logs": logs, "agent_logs": agent_logs,
            "field_pairs": field_pairs, "version_depth": depth,
            "version_of_edge": version_of_edge}


def build_profile(history: dict, *, agent_id: int, version: str,
                  outcomes=None, settings: dict) -> dict:
    """组装一个变体的画像（**纯函数**：入参是已读齐的历史包，无 I/O）。

    诚实分母：每维独立判——分母（该维的观测数）不足 min_denominator ⇒
    value=None / verdict=unknown，不回落 0。
    """
    cfg = settings or get_settings()
    min_den = int(cfg["min_denominator"])
    aid = int(agent_id)

    # 该 agent 全部字段对的震荡（震荡是 agent 级的属性：一个字段横跳影响
    # 其**所有**版本，不是某一版的锅——按 agent 级归属更诚实）
    pairs = [p for (a, _f), p in (history.get("field_pairs") or {}).items()
             if a == aid]
    osc_total = sum(count_oscillations(p) for p in pairs)
    osc_fields = sum(1 for p in pairs if count_oscillations(p) > 0)
    osc_fields_total = len(pairs)

    # 该版本的字段级 diff
    v_logs = [r for r in (history.get("agent_logs") or [])
              if int(r["asset_id"]) == aid and str(r.get("version_to") or "") == version]
    n_fields = len({str(r.get("field") or "") for r in v_logs})

    g = grade_outcomes(outcomes, min_count=min_den)
    depth = (history.get("version_depth") or {}).get((aid, str(version)))

    def _dim(name, value, count, known_ok):
        return {"name": name, "value": value if known_ok else None,
                "verdict": "known" if known_ok else "unknown", "count": count,
                "negative": 0, "lower_better": _LOWER_BETTER[name]}

    dims = {
        # 震荡的分母口径：分母是「被观测过的字段对总数」，
        # **有震荡本身就是确凿事实**（A→B→A 是日志里白纸黑字的记录），
        # 不该被"字段数不足 min_denominator"降级成 unknown——那会让
        # "唯一一次改动就被改回去"这个最该被记住的死路形态恰恰看不见。
        # 故：观测到任何字段对即 known；零字段对才 unknown（真的没数据）。
        "oscillation": _dim("oscillation", osc_total, osc_fields_total,
                            osc_fields_total > 0),
        "field_changes": _dim("field_changes", len(v_logs), len(v_logs),
                              bool(v_logs)),
        "regressions": _dim("regressions", g["negative"], g["count"],
                            g["verdict"] != "unknown"),
        "lineage_depth": _dim("lineage_depth", depth, 1, depth is not None),
        "field_diversity": _dim("field_diversity", n_fields, len(v_logs),
                                bool(v_logs)),
    }
    # 负向计数：震荡>0 与回归>0 都是"差"的证据（方向已声明，不二次加权）
    dims["oscillation"]["negative"] = osc_total if dims["oscillation"]["value"] is not None else 0
    dims["regressions"]["negative"] = g["negative"]

    known = [d for d in dims.values() if d["verdict"] == "known"]
    negatives = sum(1 for d in known if d["negative"] > 0)
    # 「支持 pass」的证据**只从 lower_better 维度取**（value==0 即无问题）。
    # lower_better=False 的探索类读数（lineage_depth）**不作为 pass 证据**：
    # 深度 0 是下界值 = "没探索过"，把它算成"无负向"等于拿没数据当好消息
    # （冒烟实测踩到的诚实性缺陷：4/5 维 unknown 时曾误判 pass）。
    positives = sum(1 for d in known
                    if d["lower_better"] and d["negative"] == 0
                    and d["value"] is not None and float(d["value"]) == 0.0)
    if negatives > 0:
        verdict = OUTCOME_REGRESSION
    elif positives > 0:
        verdict = OUTCOME_PASS
    else:
        # 无 sufficient 维度，或 sufficient 维度全是探索读数（无对照基线，
        # 深度多少算好说不清）⇒ 有信息但**不足以判 pass**，诚实留白
        verdict = OUTCOME_INSUFFICIENT
    summary = {
        "dimensions_known": len(known),
        "dimensions_total": len(dims),
        "verdict": verdict,
        "negatives": negatives,
        "positives": positives,
        "oscillation": osc_total,
        "oscillation_fields": osc_fields,
    }
    return {"dimensions": dims, "summary": summary}


#: 配置默认值（与 core.config DEFAULT_CONFIG.agent_archive 一致）
_SETTINGS_DEFAULTS = {
    "enabled": True,
    "window_days": 90,           # 变体扫描回看窗口（天）
    "min_denominator": 2,        # 各维度分母下限：不足即 unknown（P2-1 诚实分母）
    "dead_end_repeat": 2,        # 同一变体判回归几次记死路
    "max_variants": 200,         # 单次扫描入册上限（防长尾注水）
}


def get_settings() -> dict:
    """取材口径。缺配置回落默认值（与 DEFAULT_CONFIG 一致，不是第二套真值）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        if key == "enabled":
            out[key] = _config.as_bool("agent_archive", key, bool(default))
            continue
        try:
            out[key] = type(default)(_config.get("agent_archive", key, default))
        except Exception:
            out[key] = default
    return out


def scan(conn, *, settings: Optional[dict] = None) -> dict:
    """扫描进化历史 → 收敛变体台账（只读历史 + 只写自家两表）。

    台账来源（三源合并，同 (agent_id, version) 只入册一次）：
      1. agents 当前版本（每个在册 agent 一行 ⇒ 台账不会开局就空）
      2. asset_change_logs 的 version_to（历史版本，血缘与改动面在此）
      3. version_from 出现过的前代版本（同样入册，血缘起点不能丢）

    返回 {enabled, scanned, created, skipped, variants, dead_ends}。
    **不改 agents、不切换生效版本**——档案库是记忆，不是手。
    """
    from repositories.agent_archive_repo import AgentArchiveRepo
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return {"enabled": False, "scanned": 0, "created": 0, "skipped": 0,
                "variants": [], "dead_ends": []}
    hist = collect_history(conn, cutoff=_cutoff(cfg["window_days"]))
    current = {(int(a["id"]), str(a.get("version") or ""))
               for a in hist["agents"] if a.get("version")}

    cands = {}
    for key in current:
        cands.setdefault(key, set()).add("current")
    for (aid, ver), parent in (hist.get("version_of_edge") or {}).items():
        if ver:
            cands.setdefault((aid, str(ver)), set()).add("change_log")
        if parent:
            cands.setdefault((aid, str(parent)), set()).add("change_log_parent")

    repo = AgentArchiveRepo(conn)
    created, skipped = [], 0
    # 稳定排序：先 agent_id 后版本字符串（同输入同输出，门禁可复现）
    for (aid, ver) in sorted(cands)[:int(cfg["max_variants"])]:
        row, is_new = repo.register_variant(
            agent_id=aid, version=ver,
            parent_version=(hist.get("version_of_edge") or {}).get((aid, ver), ""),
            origin="+".join(sorted(cands[(aid, ver)])),
            status=V_ACTIVE if (aid, ver) in current else V_RETIRED)
        if is_new:
            created.append(row)
        else:
            skipped += 1
    dead = repo.list_variants(status=V_DEAD_END, limit=int(cfg["max_variants"]))
    return {"enabled": True, "scanned": len(cands), "created": len(created),
            "skipped": skipped, "variants": created, "dead_ends": dead}


def refresh(conn, *, settings: Optional[dict] = None) -> dict:
    """重算全部已入册变体的画像与死路判定（逐变体画像 + 逐变体结论读取）。"""
    from repositories.agent_archive_repo import AgentArchiveRepo
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return {"enabled": False, "refreshed": 0, "dead_ends": 0}
    hist = collect_history(conn, cutoff=_cutoff(cfg["window_days"]))
    repo = AgentArchiveRepo(conn)
    known = repo.list_variants(limit=10000)
    marked = 0
    for v in known:
        aid, ver = int(v["agent_id"]), str(v["version"])
        outcomes = [o["outcome"] for o in
                    repo.list_outcomes(variant_id=int(v["id"]), limit=200)]
        prof = build_profile(hist, agent_id=aid, version=ver,
                             outcomes=outcomes, settings=cfg)
        repo.update_profile(int(v["id"]), prof)
        # 死路判定看**已记录的负向结论数**（variant_outcomes），不看画像
        # negative（那是多维计数，量纲不同，不能拿来判个体生死）
        neg = repo.count_negative_outcomes(int(v["id"]))
        if dead_end_of(neg, threshold=int(cfg["dead_end_repeat"])):
            if repo.mark_dead_end(int(v["id"]), ver):
                marked += 1
    return {"enabled": True, "refreshed": len(known), "dead_ends": marked}


def record_outcome(conn, *, agent_id: int, version: str, outcome: str,
                   source: str = "manual", detail: str = "",
                   recorded_by: str = "manual") -> dict:
    """显式挂载一条外部评估结论到某变体（**档案库的入户口**）。

    为什么需要显式：真实库里 sandbox_runs / misevolution_checks 都没有
    agent_id + version 列，档案库**不猜**这次评估针对哪个变体。谁知道
    （人或上游模块明确知道）谁显式挂——挂不上就诚实地缺席。

    变体不在台册 ⇒ 不凭空建行（伪造历史比没有历史更危险），返回 ok:False。
    """
    from repositories.agent_archive_repo import AgentArchiveRepo
    if (outcome or "") not in OUTCOMES:
        return {"ok": False, "error": "不支持的结论: %s（可选: %s）"
                % (outcome, "/".join(OUTCOMES))}
    repo = AgentArchiveRepo(conn)
    row = repo.get_variant(agent_id=agent_id, version=version)
    if not row:
        return {"ok": False, "error": "变体未入册（agent_id=%s, version=%s）"
                                     "；请先 scan 收敛台账——档案库不凭空造历史"
                                     % (agent_id, version)}
    repo.add_outcome(variant_id=int(row["id"]), outcome=outcome,
                     source=source, detail=detail, recorded_by=recorded_by)
    return {"ok": True, "variant": row}


def query_dead_ends(conn, *, limit: int = 100) -> list:
    """死路记忆查询：已被记为 dead_end 的变体（提案侧据此回避已知坏路）。"""
    from repositories.agent_archive_repo import AgentArchiveRepo
    return AgentArchiveRepo(conn).list_variants(status=V_DEAD_END, limit=limit)


def dead_end_hint(conn, agent_id: int, version: str) -> dict:
    """给提案侧的死路提醒（只读纯查询）。

    {is_dead_end, status, negative_count, oscillation}——版本不在台册就老实
    说 unrecorded：**没记录 ≠ 没问题**（与 P2-1 空窗口同源纪律）。
    """
    from repositories.agent_archive_repo import AgentArchiveRepo
    repo = AgentArchiveRepo(conn)
    row = repo.get_variant(agent_id=agent_id, version=version)
    if not row:
        return {"is_dead_end": False, "status": "unrecorded", "negative_count": 0,
                "oscillation": 0}
    return {"is_dead_end": (row.get("status") == V_DEAD_END),
            "status": row.get("status") or "",
            "negative_count": repo.count_negative_outcomes(int(row["id"])),
            "oscillation": int((json_loads_safe(row.get("profile_json")) or {})
                               .get("summary", {}).get("oscillation") or 0)}


def json_loads_safe(raw) -> dict:
    """容错 JSON 解析（坏行返空 dict，不抛——画像是增强信息不是关键路径）。"""
    import json as _json
    try:
        v = _json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def lineage(conn, agent_id: int, *, limit: int = 1000) -> list:
    """某 agent 的版本血缘台账（按 agent_id + 版本排序，含 parent/status/画像摘要）。"""
    from repositories.agent_archive_repo import AgentArchiveRepo
    return AgentArchiveRepo(conn).list_variants(agent_id=int(agent_id), limit=limit)