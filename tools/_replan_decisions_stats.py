# -*- coding: utf-8 -*-
"""P4 replanning 真库统计（只读，2026-10-21）—— replan_decisions 聚合 + 巡检/早停阈值标定。

**用途**：`replan_decisions` 表（P4 新增）的管理面。回答四类问题：
    ① 重规划决策到底长什么样（source × action × level 矩阵，不是拍脑袋）；
    ② 巡检指标（M1/M4）在真实运行里的分布 —— 阈值调参的依据；
    ③ 质量门 Q1~Q5 的拦截率（放开的动作有没有被门真的拦住）；
    ④ early_stop 的收敛规模（平均放弃几个任务）与 M1/M4 阈值的真库标定值。

**为什么单独一个工具而不并进 _replan_baseline_stats.py**：基线报告是
    "实施前"的快照（定默认值），本工具是"实施后"的持续观测（调阈值）——
    两者问题不同、运行时机不同（一个一次性，一个每次调参前跑）。
    且 replan_decisions 是 P4 才有的表，老库没有 ⇒ 独立工具缺表时
    输出 n/a 而不影响基线工具的稳定输出。

**纪律**：
- 只读打开（`file:...?mode=ro` URI），绝不写生产库。
- 所有查询 try 包裹：老库无 replan_decisions 表 ⇒ 记 n/a 而不是崩。
- 输出纯文本报告到 stdout，由人工/Agent 整理进 docs/ 标定报告。

**运行**：python tools/_replan_decisions_stats.py [db_path]
"""
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

DB = sys.argv[1] if len(sys.argv) > 1 else "mbse.db"


def pct(a, b):
    return f"{100.0 * a / b:.1f}%" if b else "n/a"


def dist_str(d):
    return "  ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda x: -x[1])) or "（空）"


def _has_table(conn, name):
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = []
    P = out.append

    P("=" * 72)
    P("P4 replanning 真库统计（只读）：replan_decisions 聚合 + 巡检/早停标定")
    P("=" * 72)

    if not _has_table(conn, "replan_decisions"):
        P("\n（库中无 replan_decisions 表 —— P4 上线前的老库，或尚未产生任何")
        P(" 重规划决策。本工具其余章节仍可输出阈值标定数据。）")
        rows = []
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM replan_decisions ORDER BY id").fetchall()]

    # ── 0. 总量与 source × action 矩阵 ──
    P(f"\n【0. 决策总量】{len(rows)} 条")
    if rows:
        src_d = Counter(r["source"] or "(空)" for r in rows)
        P(f"  source 分布：{dist_str(dict(src_d))}")
        act_d = Counter(r["action"] or "(空)" for r in rows)
        P(f"  action 分布：{dist_str(dict(act_d))}")
        P("  source × action 矩阵：")
        mat = defaultdict(Counter)
        for r in rows:
            mat[r["source"] or "(空)"][r["action"] or "(空)"] += 1
        for s in sorted(mat):
            P(f"    {s:12s} → {dist_str(dict(mat[s]))}")
        lvl_d = Counter(r["level"] or "(空)" for r in rows)
        P(f"  level 分布：{dist_str(dict(lvl_d))}")
        convs = len(set(r["conversation_id"] for r in rows))
        P(f"  覆盖会话数：{convs}；决策/会话 = "
          f"{len(rows) / convs:.1f}" if convs else "")

    # ── 1. 巡检指标分布（health 行的 metrics JSON）────────────────────────
    P("\n【1. 巡检指标（health 决策的 metrics）→ M1/M4 阈值标定】")
    hp = [r for r in rows if r["source"] == "health"]
    if not hp:
        P("  n/a（无 health 决策行 —— health_probe 未开或未到巡检节奏）")
    else:
        m1_ratios, m4_ratios, acts = [], [], Counter()
        for r in hp:
            acts[r["action"]] += 1
            try:
                mt = json.loads(r["metrics"] or "{}")
            except Exception:
                continue
            m1 = (mt.get("M1_partial") or {})
            if m1.get("ratio") is not None and m1.get("n"):
                m1_ratios.append((m1["ratio"], m1["n"]))
            m4 = (mt.get("M4_budget") or {})
            if m4.get("ratio") is not None:
                m4_ratios.append(m4["ratio"])
        P(f"  health 决策动作分布：{dist_str(dict(acts))}")
        if m1_ratios:
            rs = sorted(x[0] for x in m1_ratios)
            P(f"  M1 partial 密度：n={len(rs)} min={rs[0]:.2f} 中位="
              f"{statistics.median(rs):.2f} max={rs[-1]:.2f}（阈值 0.50）")
            over = sum(1 for x in rs if x >= 0.5)
            P(f"    ⇒ 超阈（≥0.50）占比：{pct(over, len(rs))} —— 若显著 >0 说明"
              f"阈值偏松（误报多），若恒 0 说明偏紧（漏报）")
        else:
            P("  M1：无可比样本（窗口 <3 或无 partial）")
        if m4_ratios:
            rs = sorted(m4_ratios)
            P(f"  M4 预算偏离比：n={len(rs)} min={rs[0]:.2f} 中位="
              f"{statistics.median(rs):.2f} max={rs[-1]:.2f}（阈值 1.20）")
        else:
            P("  M4：n/a（预算 ≤0 或无耗时样本 ⇒ 指标缺失 ≠ 健康）")

    # ── 2. 质量门拦截率（gates JSON）──────────────────────────────────────
    P("\n【2. 质量门 Q1~Q5 拦截率（redraw/t3_redraw 决策）→ 放开动作的防线实证】")
    gr = [r for r in rows if r["source"] in ("redraw", "t3_redraw")]
    if not gr:
        P("  n/a（无重画类决策行）")
    else:
        gate_d = Counter()
        n_gate = 0
        for r in gr:
            try:
                g = json.loads(r["gates"] or "{}")
            except Exception:
                continue
            if not g:
                continue
            n_gate += 1
            for k, v in g.items():
                if k in ("Q1", "Q2", "Q3", "Q4", "Q5") and not v:
                    gate_d[k] += 1
        P(f"  重画决策 {len(gr)} 条，其中带 gates 明细 {n_gate} 条")
        P(f"  质量门拦截计数：{dist_str(dict(gate_d)) or '（无拦截）'}")
        t4 = sum(1 for r in gr if r["level"] == "T4")
        P(f"  T4（停手升级）占比：{t4}/{len(gr)}（{pct(t4, len(gr))}）")

    # ── 3. 提前收敛规模（early_stop 决策 + early-stop span meta）──────────
    P("\n【3. 提前收敛规模 → early_stop 收益面】")
    es = [r for r in rows if r["source"] == "early_stop"]
    if not es:
        P("  n/a（无 early_stop 决策行 —— early_stop 未开或无图标注 required）")
    else:
        P(f"  early_stop 决策 {len(es)} 条（会话 "
          f"{len(set(r['conversation_id'] for r in es))} 个）")
        cancels = []
        try:
            for r in conn.execute(
                    "SELECT meta_json FROM orch_run_spans WHERE kind='replan' "
                    "AND name='early-stop'").fetchall():
                try:
                    cancels.append(int((json.loads(r["meta_json"] or "{}"))
                                       .get("cancel") or 0))
                except Exception:
                    pass
        except sqlite3.OperationalError:
            pass
        if cancels:
            P(f"  收敛放弃任务数：n={len(cancels)} 中位={statistics.median(cancels):.0f} "
              f"max={max(cancels)}（每次收敛省下的可选任务执行量）")
        else:
            P("  （early-stop span meta 不可读或未落 span）")

    # ── 4. M1 阈值真库标定（summary_json partial 密度）────────────────────
    P("\n【4. M1 阈值标定：全库 partial 密度（summary_json.status）】")
    try:
        rows_sj = conn.execute(
            "SELECT status, summary_json FROM agent_tasks WHERE status='done' "
            "AND summary_json IS NOT NULL AND summary_json NOT IN ('', '{}')"
        ).fetchall()
    except sqlite3.OperationalError:
        rows_sj = []
        P("  n/a（老库无 summary_json 列）")
    if rows_sj:
        st_d, miss_n = Counter(), 0
        for r in rows_sj:
            try:
                sj = json.loads(r["summary_json"])
            except Exception:
                continue
            if not isinstance(sj, dict):
                continue
            st_d[sj.get("status") or "(无status)"] += 1
            if sj.get("missing"):
                miss_n += 1
        n = len(rows_sj)
        part_n = st_d.get("partial", 0) + st_d.get("failed", 0)
        P(f"  done 任务中有结构化摘要：{n} 条")
        P(f"  status 分布：{dist_str(dict(st_d))}")
        P(f"  ⇒ partial+failed 占比：{pct(part_n, n)}；missing 非空占比："
          f"{pct(miss_n, n)}")
        P(f"  ⇒ M1 阈值 0.50 相当于该基线的 "
          f"{0.5 / (part_n / n):.1f} 倍（远离噪声的判据；<2 倍则阈值偏松）")

    # ── 5. M4 阈值真库标定（done 任务耗时分布）────────────────────────────
    P("\n【5. M4 阈值标定：done 任务耗时（latency_ms）→ 滑动窗口预估】")
    try:
        vals = sorted(r[0] for r in conn.execute(
            "SELECT latency_ms FROM agent_tasks WHERE status='done' "
            "AND latency_ms > 0").fetchall())
    except sqlite3.OperationalError:
        vals = []
    if not vals:
        P("  n/a（无正值耗时数据）")
    else:
        P(f"  n={len(vals)} min={vals[0]:.0f}ms 中位={statistics.median(vals):.0f}ms "
          f"p90={vals[int(len(vals) * 0.9)]:.0f}ms max={vals[-1]:.0f}ms")
        # 每 run 任务数 ⇒ 全跑完的预计耗时 vs 1800s 预算
        per_run = Counter(dict(conn.execute(
            "SELECT run_id, COUNT(*) c FROM agent_tasks WHERE status='done' "
            "GROUP BY run_id").fetchall()).values())
        sizes = []
        for r in conn.execute(
                "SELECT run_id, COUNT(*) c FROM agent_tasks "
                "GROUP BY run_id").fetchall():
            sizes.append(r["c"])
        if sizes:
            med_size = statistics.median(sizes)
            proj = med_size * statistics.median(vals) / 1000.0
            P(f"  run 任务数中位={med_size:.0f} ⇒ 按中位耗时全跑完约 {proj:.0f}s"
              f"（预算默认 1800s ⇒ M4 阈值 1.2 下 {proj / 1800:.2f} 倍）")

    # ── 6. 决策成本（tokens/latency）──────────────────────────────────────
    P("\n【6. 决策成本（LLM 类判定的 token/latency）】")
    if rows:
        tok = [r["tokens"] for r in rows if (r["tokens"] or 0) > 0]
        lat = [r["latency_ms"] for r in rows if (r["latency_ms"] or 0) > 0]
        P(f"  tokens>0 的决策：{len(tok)}/{len(rows)}"
          + (f"（合计 {sum(tok)} tok，中位 {statistics.median(tok):.0f}）"
             if tok else ""))
        P(f"  latency>0 的决策：{len(lat)}/{len(rows)}"
          + (f"（中位 {statistics.median(lat):.0f}ms，max {max(lat)}ms）"
             if lat else ""))

    P("\n" + "=" * 72)
    P("口径说明：全部只读；M1 基线以 done 任务 summary_json.status 为准；"
      "M4 用滑动窗口（近 N 个 done 耗时平均），全历史平均会被早期快任务稀释；"
      "conflicts 不落库（运行时计算）⇒ M2 密度需运行期信号探针补。")
    print("\n".join(out))


if __name__ == "__main__":
    main()
