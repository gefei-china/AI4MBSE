# 执行中重规划 — 真库基线统计报告（2026-10-21）

> 依据：《执行中重规划replanning方案评估与完善设计-v2-20261021》§8 开放问题第 4 项，
> 米爸拍板「按建议执行」——现在跑一版真库 partial run 基线，用数据定 T1/T2 默认值
> 与 M1~M4 巡检阈值。
>
> **数据来源**：`mbse.db` 真库**只读**统计（`tools/_replan_baseline_stats.py`，
> `file:...?mode=ro` URI 打开，无任何写操作）。统计时点 2026-10-21，
> 快照：253 条 agent_tasks / 70 个 run / orch_run_spans 19 条（Task #43 于
> 2026-10-20 上线，span 仅覆盖最近 3 个 run）。

---

## 0. 结论先行（数据驱动的五个决策）

| # | 决策点 | 数据 | 结论 |
|---|---|---|---|
| D1 | T1 自动补跑默认**开**（已拍板） | 历史 58 条失败重放 classify()：50 interrupted + 8 timeout，**可重试类覆盖率 100%**；但 50 interrupted 是 `reap_stuck_runs` 的孤儿标记（run 已终止） | 开，**但规则细化：T1 只接 timeout/transient/internal，排除 interrupted**（进程级死亡应走 resume，补跑无意义）⇒ T1 活场景收益面 = 8/58（13.8%） |
| D2 | T2 默认**关**（v1 既定，数据再确认） | 58 条失败中 blocked=0——**真实数据中失效传播代价为 0**（失败全是叶子，从未冻结下游） | 关的依据被数据加强；失效传播代码路径靠 G2 合成 DAG 门禁覆盖（真库无样本） |
| D3 | M1 partial 密度阈值 ≥50% 且 N≥3 | 基线 partial 率 21/209 = **10.0%**（full=188 / partial=21）；missing 非空 5 条（2.4%） | 阈值维持：50% 是基线的 5 倍，远离噪声；S5 触发面 ≈ 12.4% |
| D4 | M3 未验证 claims 阈值 | 历史 claims.to_verify = **0 条** | **M3 暂不启用**（无基线无法标定，诚实登记数据缺口）；待 P1 信号探针积累后复标 |
| D5 | 重画成本预估口径 | 任务 latency：min 7.5s / 中位 **76s** / p90 149s / max 347s；token：中位 1666 / p90 4899 / max 9929 | v1「55~344s」口径被证实；一次 T2（≤1.5× 剩余，中位 3 → ≤4 任务）≈ **中位 5min / p90 10min**，预算从 run 总预算扣（已拍板按建议） |

---

## 1. 总量与状态分布

```
任务 253 条 / run 70 个（planner max_tasks 上限 12）
状态分布：done=195  failed=58
run 任务数：min=1  中位=3  max=6      ← DAG 普遍很小，重画规模上限有充足余量
```

## 2. 失败分类与 T1 收益面（§4.2 D1）

| error_class（重放 classify()） | 条数 | 占比 | 归属 |
|---|---|---|---|
| interrupted（孤儿运行：30 分钟无进展被收割） | 50 | 86.2% | **resume/rerun 语义，非 T1**（run 已终止，"已保留上下文可重跑"） |
| timeout | 8 | 13.8% | **T1 候选**（7× 'timeout' + 1× 'timeout（240s 无产出）'） |

两点说明：

1. 真库 58 条失败的 `error_class` 列**全部为空**——它们是 B1-6 分类上线
   （2026-10-13）之前的历史数据。重放 `services/failure_class.classify()` 后
   100% 落入可重试类，说明**分类器对存量失败零漏判**（与 failure_class.py
   模块头"判据对存量 100% 覆盖"的声明互相印证）。
2. interrupted 占 86% 是本仓**进程重启/长任务停滞**的历史形态，不是"活着的
   run 内子任务失败"。⇒ v2 §3.2 的 T1 触发条件应从 v1 的 S3（可重试类）
   细化为 **S3'：timeout/transient/internal**（显式排除 interrupted），
   判定树里 interrupted → resume 分支。**这是本次基线数据对 v1 设计的第一处
   实质修正。**

## 3. partial run 基线与失效传播代价（§4.2 D2）

```
run 总数 70；含 failed/blocked 的 run 22（31.4%）
这些 run 内：failed 58 条 / blocked 0 条
⇒ 失效传播比（blocked/failed）= 0.00
```

**解读**：

- 历史上**从未发生**"一条失败冻结多条下游"——全部失败都是叶子失败
  （无下游依赖其产物）。这与 DAG 规模（中位 3、max 6 任务）互相印证：
  图太小，传播链根本没形成。
- 对设计的含义：① T2 局部重画 today 的核心价值是**重跑失效子图本身**，
  不是"解冻下游"；失效传播算法是面向大 DAG 的理论防御，**必须靠合成 DAG
  门禁（G2：菱形依赖+一跳失效）覆盖**，真库无样本可回放；
  ② T2 默认关的保守取向被数据再次支持（误重画代价 > 漏重画代价，
  而漏重画的现实发生率为 0）。
- 采样局限登记：22 个 partial run 全部 ≤6 任务；>6 任务的 DAG 传播行为
  无数据，P2 上线后需在真库复跑本脚本对比。

## 4. 结构化交付摘要密度：S5/S6 信号（§4.2 D3/D4）

```
有结构化摘要（summary_json）的任务：209 条
status 分布：full=188  partial=21（10.0%）
missing 非空：5（2.4%）
claims.to_verify 非空：0 条
conflicts（detect_conflicts）：运行时计算不落库 ⇒ n/a（数据缺口，P1 探针补）
```

- **S5 触发面 ≈ 12.4%**（partial 21 + missing 5，上界，两者可能有重叠）——
  每 8 个任务约 1 个自报"缺东西"，T2 若开会是不低的触发频率，
  这也是 T2 默认关 + 质量门必须严的实证依据。
- **S6（to_verify claims）历史为零** ⇒ M3 无基线，v2 §4.4 的 M3 指标
  **暂不启用**（不作为升级依据），待 P1 信号探针跑 2~4 周有数据后复标。
- **S7（conflicts）不落库** ⇒ 频率完全未知；P1 信号探针的首要任务之一
  就是给 detect_conflicts 的结果落一条观测记录（不落库的指标等于不存在）。

## 5. 成本数据：重画预算与巡检节奏（§4.2 D5）

| 指标 | n | min | 中位 | p90 | max |
|---|---|---|---|---|---|
| 任务 latency_ms | 195 | 7.5s | **76.4s** | 149.4s | 347.2s |
| 任务 token_count | 194 | 12 | **1,666** | 4,899 | 9,929 |
| plan span（Task #43） | 3 | — | 11.2s | — | 17.7s |
| task span（Task #43） | 10 | — | 194.8s | — | 604.9s |
| run span（Task #43） | 3 | — | 670.3s | — | 774.1s |

校准结论：

1. **v1「一次重画 = 全量子任务重跑 + 用户再等 55~344s」的口径被证实**
   （min 7.5s ~ max 347.2s，中位 76.4s）。
2. 一次 T2 重画（redraw_expand_ratio=1.5 × 中位剩余 3 任务 ≈ 4 任务）≈
   **中位 5.1min / p90 10.0min**。这就是 T2 确认卡（若开）要给用户看的
   「本次修订预计耗时」的计算口径：`Σ 新任务数 × 近期同 Agent 任务耗时中位`。
3. M4 预算偏离的"平均任务耗时"应取**滑动窗口**（近 N 个 done 任务实际耗时），
   不用全历史平均——task span 显示长尾可达 605s（硬超时 600s 口径）。
4. run span 平均 670s ≈ 11min ⇒ 巡检节奏 `probe_every_sec=60` 在典型 run
   内约触发 10 次，与 `probe_every_tasks=3`（中位 3 任务的 run 约 1 次）
   互为补充，节奏合理，维持 v2 默认值。

## 6. retry_count 分布：T1 是净增量

```
retry_count：0=241  1=12
```

12 条 retry_count=1 全部来自工作台手工重试（routers/studio_parts/flows.py:447）
⇒ **流式编排主循环零自动重试**证实（schema.py L994 注释同款口径）。
T1 自动补跑是净增量能力，不与任何现有机制重叠。

## 7. 失败文本 Top 聚类（运维视角）

```
50× 编排中断：超过 30 分钟无任何进展，判定为孤儿运行（进程重启或执行线程丢失），已保留上下文可重跑
 7× timeout
 1× timeout（240s 无产出）
```

与 failure_class.py 模块头 2026-10-12 快照（46 interrupted + 7 timeout +
1 timeout）相比：interrupted +4 条（进程重启类问题仍在发生，
是独立的运维议题，不属 replanning 范围，登记备查）。

---

## 8. 数据缺口登记（P1 信号探针的验收清单）

| # | 缺口 | 影响 | 补齐方式 |
|---|---|---|---|
| E1 | conflicts（S7）频率未知 | M 类巡检缺一项；T2 触发面低估 | P1 给 detect_conflicts 结果落 orch_run_spans/观测结构 |
| E2 | claims.to_verify（S6）历史为零 | M3 无法标定（已停用） | P1 探针 + 2~4 周数据后复标 |
| E3 | 大 DAG（>6 任务）失效传播无样本 | 失效传播算法只有合成门禁覆盖 | P2 上线后复跑本脚本对比（run 任务数 max 从 6 抬升即有新样本） |
| E4 | span 仅覆盖 3 个 run | 重画决策延迟无运行时参照 | 随 Task #43 自然积累 |

## 9. 复跑方式

```bash
python tools/_replan_baseline_stats.py            # 默认 mbse.db，只读
python tools/_replan_baseline_stats.py <db_path>  # 指定库（如干净库双口径）
```

脚本纪律：`mode=ro` URI 只读打开；无写无临时表；缺列指标记 n/a 不崩；
error_class 分类一律 import `services/failure_class` 事实源（不重造词表）。
