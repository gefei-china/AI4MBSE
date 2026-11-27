# -*- coding: utf-8 -*-
"""记忆周报统计（P2，2026-11-27）。

## 为什么单写一个模块
方案 P2 的原话是「记忆周报（重复率/命中率/tier 流动，聚合 API 已具备、缺统计）」。
复核后要修正一句：**聚合 API 其实不具备** —— `agent_memory` 只存**当前状态**
（tier / forgotten / archived_at），答不了"这周升了几条 core、降了几条冷层、复活了几条"，
那些是**流动**。所以本轮先补 `memory_events`（append-only 埋点，见 migration），
统计才有原始数据可聚合。

## 三个指标的口径（写清是为了不被误读）
| 指标 | 口径 | 常见误读 |
|---|---|---|
| **重复率** | `可召回行` 里 ≥`dup_sim` 的近重复**对数 ÷ 可召回行数** | 不是"重复行占比"：一对重复贡献 1，两行贡献 2 ⇒ 只会低估不会高估 |
| **命中率** | 可召回行里 `access_count>0` 的比例 | 它是**覆盖率**，不是"召回准确率"；`access_count` 会被噪音闸门挡掉计入，故偏保守 |
| **tier 流动** | `memory_events` 窗口内按 kind 计数 | 埋点上线**之前**的事件不存在 ⇒ 首次跑窗口内可能为 0，那是"没数据"不是"没流动" |

## 纪律
- **只读**：本模块不写任何记忆状态（唯一写入是 `record_event`，不在本模块里）。
- **样本上限显式化**：近重复是 O(n²) 向量比较，行数超 `pair_sample_cap`（默认 400）时
  **只算最近的 N 行并在返回里标 `sampled=True`** —— 静默截断会让"重复率下降"变成假象。
- **降级不猜**：无向量的行不参与重复率（跨版本向量物理不可比），`vector_covered` 显式给出覆盖率。
"""
from __future__ import annotations

import json
import logging
import math
import time

logger = logging.getLogger("mbse.memory")


def _cos(a, b):
    q = math.sqrt(sum(x * x for x in a)) or 1.0
    w = math.sqrt(sum(y * y for y in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (q * w)


def _recallable(conn) -> list:
    """可召回行的最小投影（口径与 `MemoryService.search` 对齐）。

    与评测脚本 `alive_rows` 同一套条件：forgotten=0 / 非 archival / 非 contradicted /
    未被取代 / 有效期未关。列缺失（老库/夹具）时不拼该条件。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()}
    except Exception as exc:  # noqa: BLE001
        # 读不到表结构 ⇒ 这份报告**没有分母**，必须显式说，不能返回一个"0 行"的空报告
        logger.warning("记忆周报：读 agent_memory 表结构失败：%s: %s",
                       type(exc).__name__, str(exc)[:120])
        return []
    where = ["COALESCE(forgotten,0)=0"]
    if "tier" in cols:
        where.append("(tier IS NULL OR tier != 'archival')")
    if "verify_state" in cols:
        where.append("COALESCE(verify_state,'unverifiable')!='contradicted'")
    if "superseded_by" in cols:
        where.append("COALESCE(superseded_by,0)=0")
    if "valid_to" in cols:
        where.append("(valid_to IS NULL OR TRIM(valid_to)='')")
    _sel = ["id", "content", "embedding", "embed_version", "access_count", "mem_topic",
            "created_at"]
    if {"scope_type", "scope_id"} <= cols:
        _sel += ["scope_type", "scope_id"]
    sql = ("SELECT " + ", ".join(_sel) + " FROM agent_memory WHERE " + " AND ".join(where)
           + " ORDER BY id DESC")
    out = []
    for r in conn.execute(sql):
        d = dict(r)
        try:
            d["vec"] = json.loads(d["embedding"]) if isinstance(d.get("embedding"), str) else (
                d.get("embedding") or [])
        except Exception as exc:  # noqa: BLE001
            # 单行向量坏了只影响这一行（不参与重复率），但一眼要能看出"有坏行"
            logger.warning("记忆周报：id=%s 的 embedding 解析失败（该行不参与重复率）：%s",
                           d.get("id"), type(exc).__name__)
            d["vec"] = []
        out.append(d)
    return out


def duplicate_rate(rows: list, sim: float = 0.95, cap: int = 400) -> dict:
    """近重复统计。返回 {pairs, rows, rate, sampled, vector_covered}。"""
    usable = [r for r in rows if r.get("vec")]
    sampled = len(usable) > cap
    pool = usable[:cap]          # rows 已按 id DESC ⇒ 取"最近 N 行"，不是随机抽样
    pairs = 0
    for i in range(len(pool)):
        a = pool[i]
        for j in range(i + 1, len(pool)):
            b = pool[j]
            if a.get("embed_version") != b.get("embed_version"):
                continue
            if len(a["vec"]) != len(b["vec"]):
                continue
            if _cos(a["vec"], b["vec"]) >= sim:
                pairs += 1
    n = max(1, len(rows))
    return {"pairs": pairs, "rows": len(rows), "pool": len(pool),
            "rate": round(pairs / n, 4), "sim": sim,
            "sampled": sampled,
            "vector_covered": round(len(usable) / n, 4)}


def topic_clusters(rows: list, min_n: int = 3, min_access: int = 5, limit: int = 10) -> list:
    """经验聚类候选（P2「经验聚类成技能」的**输入面**，不自动生成技能）。

    聚类的键就是 `mem_topic`（Auto Memory 的既有主题索引），不用向量聚类 —— 理由：
      · 主题是人/模型显式给的标签，语义边界比 k-means 稳；
      · k-means 的 k 需要标定，而本工程还没有"技能质量"的判据去标它（先有判据再调算法）。
    判据：同 topic 下 `mem_type='experience'`、可召回、`access_count>=min_access` 的行 ≥min_n 条。
    ⇒ 这才是"值得提炼成技能"的形状：同一主题反复被用、且有多条经验。
    """
    groups: dict = {}
    for r in rows:
        t = (r.get("mem_topic") or "").strip()
        if not t:
            continue
        dt = r.get("_mem_type") or ""
        if dt and dt != "experience":
            continue
        groups.setdefault(t, []).append(r)
    out = []
    for t, g in groups.items():
        hot = [x for x in g if int(x.get("access_count") or 0) >= min_access]
        if len(hot) >= min_n:
            out.append({"topic": t, "n_experience": len(g), "n_hot": len(hot),
                        "total_access": sum(int(x.get("access_count") or 0) for x in g)})
    out.sort(key=lambda x: (-x["n_hot"], -x["total_access"]))
    return out[:limit]


def _flow(conn, since: str, until: str) -> dict:
    """窗口内台账流动。表不存在 → 全 0 + `available=False`（不冒充"没有流动"）。"""
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*) n FROM memory_events WHERE at>=? AND at<=? GROUP BY kind",
            (since, until)).fetchall()
        flow = {r[0]: int(r[1]) for r in rows}
        total = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        return {"available": True, "by_kind": flow, "events_total": int(total)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆周报：读 memory_events 失败（按'不可用'上报）：%s: %s",
                       type(exc).__name__, str(exc)[:120])
        return {"available": False, "by_kind": {}, "events_total": 0,
                "note": "memory_events 不存在或不可读（%s）—— 流动数据不可用，不等于没有流动"
                        % type(exc).__name__}


def weekly(conn, days: int = 7, dup_sim: float = 0.95, pair_cap: int = 400) -> dict:
    """记忆周报：存量结构 + 三个指标 + 窗口流动。**只读**。"""
    days = max(1, min(int(days or 7), 365))
    now = time.time()
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - days * 86400))
    until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    try:
        total = conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆周报：agent_memory 不可读：%s: %s", type(exc).__name__, str(exc)[:120])
        return {"error": "agent_memory 不可读", "detail": "%s: %s" % (type(exc).__name__, exc)}
    rows = _recallable(conn)
    # 逐行补 mem_type（`_recallable` 的 SELECT 故意不含它——上面聚类要用，单独取一次更省）
    try:
        types = {int(r[0]): (r[1] or "") for r in conn.execute(
            "SELECT id, mem_type FROM agent_memory")}
        for r in rows:
            r["_mem_type"] = types.get(int(r["id"]), "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆周报：补 mem_type 失败（聚类会退化为不筛类型）：%s: %s",
                       type(exc).__name__, str(exc)[:120])

    try:
        struct = {r[0]: int(r[1]) for r in conn.execute(
            "SELECT tier, COUNT(*) FROM agent_memory WHERE COALESCE(forgotten,0)=0 GROUP BY tier")}
        forgotten = conn.execute(
            "SELECT COUNT(*) FROM agent_memory WHERE COALESCE(forgotten,0)!=0").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆周报：按 tier 汇总失败（结构段留空）：%s: %s",
                       type(exc).__name__, str(exc)[:120])
        struct, forgotten = {}, 0
    cold = 0
    try:
        cold = conn.execute(
            "SELECT COUNT(*) FROM agent_memory WHERE COALESCE(forgotten,0)=0 AND tier='archival'"
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆周报：统计冷层行数失败（报 0，但**不是**真的 0）：%s: %s",
                       type(exc).__name__, str(exc)[:120])

    hit = sum(1 for r in rows if int(r.get("access_count") or 0) > 0)
    dup = duplicate_rate(rows, dup_sim, pair_cap)
    clusters = topic_clusters(rows)

    # 本窗口新增（沉淀健康度：只增不用的部分就是"沉淀即死"的增量）
    try:
        created = conn.execute(
            "SELECT COUNT(*) FROM agent_memory WHERE created_at>=?", (since,)).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆周报：统计窗口内新增失败（报 0）：%s: %s",
                       type(exc).__name__, str(exc)[:120])
        created = 0

    return {
        "window": {"days": days, "since": since, "until": until},
        "stock": {"total": int(total), "recallable": len(rows),
                  "by_tier": struct, "forgotten": int(forgotten), "cold_archival": int(cold)},
        "created_in_window": int(created),
        "dup_rate": dup,
        "hit_rate": {"rows": len(rows), "with_access": hit,
                     "rate": round(hit / max(1, len(rows)), 4),
                     "note": "覆盖率口径：可召回行中有过访问证据的比例；非召回准确率"},
        "flow": _flow(conn, since, until),
        "skill_clusters": clusters,
        "reading_guide": {
            "dup_rate": "近重复对数/可召回行；sampled=true 时只算了最近 pool 行",
            "hit_rate": "低 = 沉淀即死（写了没人用）；注意噪音闸门会挡掉部分计入，故偏保守",
            "flow": "埋点自 2026-11-27 起；窗口内 0 事件可能是'刚上线没跑过'。"
                    "⚠️ 一次合并会同时产生 1 条 merged + 1 条 superseded（后者是底层原语），"
                    "算'本周合并了多少条'请只数 merged，否则会翻倍",
        },
    }
