# -*- coding: utf-8 -*-
"""replanning 真库基线统计（只读，★ 2026-10-21，v2 §8.4 用户已拍板"按建议执行"）。

**用途**：用数据定 replanning 的 T1/T2 默认值与 M1~M4 巡检阈值，替代拍脑袋。
对应文档：《执行中重规划replanning方案评估与完善设计-v2-20261021》§4.4/§8.4。

**纪律**：
- 只读打开（`file:...?mode=ro` URI），绝不写生产库；无临时表、无 WAL 检查点。
- 所有查询 try 包裹：老库缺列时该指标记 "n/a（缺列）" 而不是崩。
- 输出纯文本报告到 stdout，由人工/Agent 整理进 docs/ 基线报告。

**运行**：python tools/_replan_baseline_stats.py [db_path]
"""
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

DB = sys.argv[1] if len(sys.argv) > 1 else "mbse.db"

# failure_class.py 的口径（不重造第二份词表，直接 import 事实源）
RETRYABLE = {"timeout", "transient", "interrupted", "internal"}
NON_RETRYABLE = {"permission", "input_invalid", "output_rejected", "business"}


def pct(a, b):
    return f"{100.0 * a / b:.1f}%" if b else "n/a"


def dist_str(d):
    return "  ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda x: -x[1])) or "（空）"


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = []
    P = out.append

    P("=" * 72)
    P("replanning 真库基线统计（只读）")
    P("=" * 72)

    # ── 0. 总量 ──
    total = conn.execute("SELECT COUNT(*) c FROM agent_tasks").fetchone()["c"]
    runs = conn.execute("SELECT COUNT(DISTINCT run_id) c FROM agent_tasks").fetchone()["c"]
    status_d = dict(conn.execute(
        "SELECT status, COUNT(*) FROM agent_tasks GROUP BY status").fetchall())
    P(f"\n【0. 总量】任务 {total} 条 / run {runs} 个")
    P(f"  状态分布：{dist_str(status_d)}")

    # ── 1. 失败分类分布 → T1「多少失败本可自动补跑」 ──
    P("\n【1. 失败分类（error_class）→ T1 自动补跑收益面】")
    rows = conn.execute(
        "SELECT error_class, COUNT(*) c FROM agent_tasks WHERE status='failed' "
        "GROUP BY error_class").fetchall()
    ec_d = {r["error_class"] or "(未分类/空)": r["c"] for r in rows}
    P(f"  {dist_str(ec_d)}")
    retry_n = sum(v for k, v in ec_d.items() if k in RETRYABLE)
    nonretry_n = sum(v for k, v in ec_d.items() if k in NON_RETRYABLE)
    unclassified = ec_d.get("(未分类/空)", 0)
    failed_total = sum(ec_d.values())
    P(f"  ⇒ 可重试类（timeout/transient/interrupted/internal）：{retry_n}/{failed_total}"
      f"（{pct(retry_n, failed_total)}）——T1 自动补跑的候选面")
    P(f"  ⇒ 不可重试类（permission/input_invalid/output_rejected/business）："
      f"{nonretry_n}（{pct(nonretry_n, failed_total)}）——该重画不该重试")
    P(f"  ⇒ 未分类（老数据/极老库无 error_class 写入）：{unclassified}")

    # 未分类失败若走实时分类器能否覆盖（重放 classify，只读文本）
    try:
        sys.path.insert(0, ".")
        from services.failure_class import classify
        cov = Counter()
        for r in conn.execute(
                "SELECT error FROM agent_tasks WHERE status='failed' "
                "AND (error_class IS NULL OR error_class='')").fetchall():
            cov[classify(r["error"] or "", ok=False) or "(空文本→internal)"] += 1
        if cov:
            P(f"  ⇒ 未分类失败重放 classify() 后的分布：{dist_str(dict(cov))}")
    except Exception as e:
        P(f"  （classify 重放跳过：{e}）")

    # ── 2. partial run 基线 → 失效传播代价 ──
    P("\n【2. partial run 基线与失效传播代价】")
    per_run = defaultdict(Counter)
    for r in conn.execute("SELECT run_id, status FROM agent_tasks").fetchall():
        per_run[r["run_id"]][r["status"]] += 1
    partial_runs = {rid: c for rid, c in per_run.items()
                    if c.get("failed", 0) > 0 or c.get("blocked", 0) > 0}
    P(f"  run 总数 {len(per_run)}；含 failed/blocked 的 run {len(partial_runs)}"
      f"（{pct(len(partial_runs), len(per_run))}）")
    if partial_runs:
        failed_in_pr = sum(c.get("failed", 0) for c in partial_runs.values())
        blocked_in_pr = sum(c.get("blocked", 0) for c in partial_runs.values())
        P(f"  这些 run 内：failed {failed_in_pr} 条 / blocked {blocked_in_pr} 条")
        if failed_in_pr:
            P(f"  ⇒ 失效传播比（blocked/failed）：{blocked_in_pr / failed_in_pr:.2f}"
              f" —— 每 1 条失败平均冻结 {blocked_in_pr / failed_in_pr:.2f} 条下游")
        # 单 run 任务数分布（重画规模上限 redraw_expand_ratio 的参照）
        sizes = [sum(c.values()) for c in per_run.values()]
        P(f"  run 任务数：min={min(sizes)} 中位={statistics.median(sizes):.0f} "
          f"max={max(sizes)}（planner max_tasks 上限 12）")
        # blocked 任务的 deps 里 failed 的数量（传播路径深度）
        deep = Counter()
        for r in conn.execute(
                "SELECT deps FROM agent_tasks WHERE status='blocked'").fetchall():
            try:
                deep[len(json.loads(r["deps"] or "[]"))] += 1
            except Exception:
                deep["?"] += 1
        if deep:
            P(f"  blocked 任务的依赖数分布：{dist_str({str(k): v for k, v in deep.items()})}")

    # ── 3. summary_json：S5 信号（status=partial / missing 非空）的真实密度 ──
    P("\n【3. 结构化交付摘要（summary_json）→ S5/S6/S7 信号密度】")
    has_col = True
    try:
        rows = conn.execute(
            "SELECT task_key, summary_json FROM agent_tasks "
            "WHERE summary_json IS NOT NULL AND summary_json NOT IN ('', '{}')"
        ).fetchall()
    except sqlite3.OperationalError:
        has_col = False
        rows = []
    if not has_col:
        P("  n/a（老库无 summary_json 列）")
    else:
        st_d, miss_n, claims_n, conflict_n = Counter(), 0, 0, 0
        for r in rows:
            try:
                sj = json.loads(r["summary_json"])
            except Exception:
                continue
            if not isinstance(sj, dict):
                continue
            st_d[sj.get("status") or "(无status)"] += 1
            if sj.get("missing"):
                miss_n += 1
            cl = sj.get("claims") or []
            if isinstance(cl, list):
                claims_n += sum(1 for c in cl
                                if isinstance(c, dict) and c.get("to_verify"))
        P(f"  有结构化摘要的任务：{len(rows)} 条")
        P(f"  status 分布：{dist_str(dict(st_d))}")
        P(f"  missing 非空：{miss_n}（{pct(miss_n, len(rows))}）——S5 触发面")
        P(f"  claims.to_verify 非空：{claims_n} 条 —— S6 触发面")
        P("  conflicts（detect_conflicts）：运行时计算不落库 ⇒ 本指标 n/a，"
          "需 P1 信号探针补（数据缺口已登记）")

    # ── 4. 成本数据 → 重画成本预估校准 ──
    P("\n【4. 成本数据（latency_ms / token_count）→ 重画成本预估】")
    for col in ("latency_ms", "token_count"):
        try:
            vals = [r[0] for r in conn.execute(
                f"SELECT {col} FROM agent_tasks WHERE {col} > 0").fetchall()]
        except sqlite3.OperationalError:
            P(f"  {col}：n/a（缺列）")
            continue
        if not vals:
            P(f"  {col}：无正值数据")
            continue
        vals.sort()
        P(f"  {col}：n={len(vals)} min={vals[0]:.0f} 中位={statistics.median(vals):.0f} "
          f"p90={vals[int(len(vals) * 0.9)]:.0f} max={vals[-1]:.0f}")
    # 分状态看 latency（failed 的耗时 = 白烧的成本）
    try:
        for st in ("done", "failed"):
            vals = [r[0] for r in conn.execute(
                "SELECT latency_ms FROM agent_tasks WHERE status=? AND latency_ms > 0",
                (st,)).fetchall()]
            if vals:
                P(f"  {st} 任务 latency 中位：{statistics.median(vals):.0f}ms"
                  f"（n={len(vals)}）")
    except sqlite3.OperationalError:
        pass

    # ── 5. retry_count 分布（佐证「流式不重试恒 0」） ──
    P("\n【5. retry_count 分布】")
    try:
        rc = dict(conn.execute(
            "SELECT retry_count, COUNT(*) FROM agent_tasks GROUP BY retry_count").fetchall())
        P(f"  {dist_str({str(k): v for k, v in rc.items()})}")
        P("  ⇒ 非 0 占比若≈0，证实「流式路径从不自动重试」——T1 补跑是净增量能力")
    except sqlite3.OperationalError:
        P("  n/a（缺列）")

    # ── 6. orch_run_spans：决策延迟与重画预算的运行时参照 ──
    P("\n【6. orch_run_spans（Task #43 观测数据）→ 巡检节奏/预算参照】")
    try:
        kinds = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM orch_run_spans GROUP BY kind").fetchall())
        P(f"  span 分布：{dist_str(kinds)}")
        for kind in ("plan", "task", "run"):
            row = conn.execute(
                "SELECT COUNT(*) n, AVG(latency_ms) avg_ms, MAX(latency_ms) max_ms "
                "FROM orch_run_spans WHERE kind=?", (kind,)).fetchone()
            if row["n"]:
                P(f"  {kind} span：n={row['n']} 平均={row['avg_ms']:.0f}ms "
                  f"max={row['max_ms']:.0f}ms")
        # run 级硬超时参照：run span max ≈ 单 run 墙上限（600s 硬超时口径见 stream.py）
    except sqlite3.OperationalError as e:
        P(f"  n/a（{e}）")

    # ── 7. 失败文本 Top 聚类（运维视角：高频失败形态） ──
    P("\n【7. 失败文本 Top10（截断 60 字）】")
    txt = Counter()
    for r in conn.execute(
            "SELECT error FROM agent_tasks WHERE status='failed' AND error != ''"
    ).fetchall():
        txt[(r["error"] or "")[:60]] += 1
    for t, c in txt.most_common(10):
        P(f"  {c:3d}× {t}")

    P("\n" + "=" * 72)
    P("统计口径说明：本报告全部只读；conflicts 不落库（运行时计算），"
      "S7 密度需 P1 信号探针补齐；partial 密度以 summary_json.status 为准。")
    print("\n".join(out))


if __name__ == "__main__":
    main()
