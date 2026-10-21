# replanning P4 巡检与早停阈值标定报告（2026-10-21）

> 配套文档：《执行中重规划replanning方案评估与完善设计-v2-20261021》§4.4/§7、
> 《replanning真库基线统计报告-20261021》。
> 本报告回答一个问题：**P4 的每个阈值/默认值，是拿什么数据定下来的**——
> 以及上线后拿什么数据复标。定不出来的（M3）诚实停用，不拍脑袋。

## 0. 结论速览

| 项 | 值 | 定标依据（一句话） |
|---|---|---|
| M1 partial 密度阈值 | **≥50% 且窗口 N≥3** | 全库基线 10.0%（21/209；本次复跑 19/194=9.8%），50% = 基线的 **5.1 倍**，远离噪声 |
| M2 冲突密度阈值 | **≥1 即超阈** | 强信号：无解决状态机 ⇒ 检出的冲突一律按未解决，保守口径 |
| M3 未验证 claims | **停用** | 真库 `claims.to_verify = 0 条`，无基线无法标定（数据缺口登记，不复标不开） |
| M4 预算偏离阈值 | **>120%**（严格大于） | 任务耗时中位 76s / p90 149s / max 347s、长尾 605s ⇒ 需留 20% 余量吸收抖动 |
| M4 时间预算 | **1800s/run** | 与 `reap_stuck_runs` 的 30min 孤儿收割阈值对齐（超预算不是"重画能救的"） |
| 巡检节奏 | **每 3 个任务完成 或 每 60 秒**（先到者） | run span 均值 670s ⇒ 60s 节奏在典型 run 内约 10 次；3 任务口径在中位 3 任务的 run 约 1 次，互为补充 |
| M1 窗口 | **= probe_every_tasks（3）** | 一个配置两个用途，杜绝"节奏改了窗口忘改"的漂移 |
| early_stop 判据 | **required 全 done + 零 failed + 有非必需剩余** | 保守到苛刻：无 required 标注的图**不猜**目标边界 |
| LLM 动作白名单 | **默认锁 `replace`** | 拍板⑤：skip/insert 在 P4 评估后放开；三道防线兜底（见 §5） |

## 1. M1：partial 密度（缓慢漂移的第一信号）

**口径**（`services/replan_health.py::partial_density`）：近 N 个**已完成**
任务中"自报缺东西"的占比。"自报缺东西" = 协议摘要 `status ∈ {partial, failed}`
或 `missing` 非空——与 S5 信号**同一份判据**（`_is_incomplete`），不另造第二套。

**真库数据**（`tools/_replan_decisions_stats.py` §4，只读复跑）：

```
done 任务中有结构化摘要：194 条
status 分布：full=175  partial=19
⇒ partial+failed 占比：9.8%；missing 非空占比：2.6%
```

与基线报告的 21/209=10.0% 一致（差异来自过滤条件：基线报告含失败任务）。

**阈值 50% 的推导**：

- 基线的 5.1 倍 ⇒ "每 20 个任务才 1 个 partial"的设计使然稀疏缺失**不会**
  触发巡检（防误报第一闸）；
- 窗口 N≥3：2 个任务 1 个 partial 就是 50%，那是噪声不是信号。样本不足
  **不判**（门禁 U2a 逐格钉住）；
- 窗口取**最近** N 个而非全历史：健康度是"现在进行时"，5 分钟前的 partial
  不该替现在的状态背锅（与 M4 滑动窗口同源，门禁 U2f）。

## 2. M2：冲突密度（强信号，保守口径）

复用既有事实源 `subtask_protocol.detect_conflicts`（不重造判据，与 S7 同源）。
v1 无"冲突解决"状态机 ⇒ **检出的冲突一律按未解决计**——宁可多巡检一次，
不可把未消化的矛盾当健康。阈值 ≥1 即超阈（强信号不需要比例）。

**数据缺口**：conflicts 是运行时计算、不落库 ⇒ 历史频率未知（基线报告 E1）。
本指标的行为由门禁 U3 用合成互斥 claims 逐格覆盖；密度分布待运行期
`replan_decisions.metrics` 积累后复标（§7）。

## 3. M4：预算偏离（该停，不重画）

**口径**（`budget_deviation`）：`预计剩余 = 剩余任务数 × 滑动窗口平均耗时`
（近 N 个 done 任务实际耗时，**不用全历史平均**——task span 实测长尾可达
605s，全历史平均会被早期快任务稀释，基线报告 §5 校准结论 3 同款论证）。
`hit = 预计剩余 > 剩余预算 × 120%`（严格大于，防边界抖动误报；门禁 U4e/U4f）。

**真库数据**：

```
latency_ms：n=195 min=7481ms 中位=76380ms p90=149404ms max=347163ms
run 任务数中位=3 ⇒ 按中位耗时全跑完约 229s（预算 1800s ⇒ 0.13 倍）
```

**读法**：典型 run（中位 3 任务）全跑完约 229s，只占 1800s 预算的 13%
⇒ M4 在典型负载下**不会误报**；它抓的是长尾场景（p90 单任务 149s、
max 347s，叠加重画/等待后的真实超支）。这与"超预算不是重画能救的，
是该停了"（S9 语义）的设计定位一致：宁可漏报，不可把健康 run 停掉。

**输入不足时 hit=False 且 ratio=None**（算不出 ≠ 健康，调用方按 n/a 记录；
门禁 U4a~U4c 逐格）。

## 4. 巡检节奏与 M3 停用

**节奏**：`probe_every_tasks=3`（兼作 M1 窗口）或 `probe_every_sec=60`，
取先到者。基线报告 §5 结论 4 的论证维持不变：run span 均值 670s ⇒ 60s
节奏约 10 次/run；3 任务口径在中位 3 任务的 run 约 1 次。两者覆盖
"任务密集"与"任务稀疏但单任务长"两种形态。

**双闸纪律**：两个上限都 ≤0 ⇒ 永不到期（门禁 U1e + 变异 M6）——
配置写 0 想关掉节奏时，行为必须是"关节奏"而不是"每次必检"。

**M3（未验证 claims 密度）停用**：真库 `claims.to_verify = 0 条`（基线报告
D4）。没有基线就无法区分"阈值松"与"没数据"——按需不触发 ≠ 失效的教训
（Task #43）反向同样成立：**无基线不开阈值**。复标条件见 §7。

## 5. early_stop 判据与 LLM 三动作放开评估

### 5.1 提前收敛（保守到苛刻）

判据三条**全满足**才动作（`early_stop_plan`，门禁 U6 逐格 + 变异 M2）：

1. 图上有 `config.required` 为真的任务——**无标注不猜**目标边界
   （planner 建图时标注，见 `workflows/planner.py` 的 `config.required`
   字段声明与两套 prompt 对称）；
2. 这些 required 任务**全部 done**；
3. **零 failed**（有失败 ⇒ 走 T4/blocked，不与"目标达成"混流）。

动作 = cancel 剩余非 required 的非终态任务。汇总状态记 **full** 而非
partial：剩余任务是**主动战略放弃**（没必要做），不是没做成。用户可见性
三件套：subtask `skipped` 事件（前端灰徽章"主动放弃"）、`plan_revised`
修订卡带 `skipped` 清单、汇总内容显式标注（且在反思修订后**仍存活**——
2026-10-21 实施期修的真 bug：reflection 整体重写 orch_content 会把标注
揉掉，已在 `stream.py` reflection 之后幂等重追加，门禁 S4i 钉住）。

### 5.2 LLM 三动作（skip/insert）放开评估

拍板⑤：P3 只做 replace；P4 评估后放开 skip/insert，**默认仍锁 replace**
（`llm_actions="replace"`）。放开不是放任——三道防线：

| 层 | 防线 | 门禁 |
|---|---|---|
| 解析层 | `allow` 外动作**整份拒收**（T4，不"尽力而为"） | U9e + 变异 M3 |
| 计划层 | skip 目标须 ⊆ 失效传播集（不得跳无人失败的健康上游）；insert 必须挂 `lineage.directive`（指令原文） | U10f/U10g |
| 质量门 | Q3 双轨：replace ⇒ `from ∈ failed`；insert ⇒ `action=insert` 且 directive 非空 | U11e + 变异 M4 |

**边界语义**：skip 一个无失效上游的任务 = 把下游的输入前提静默抽掉，
比 block 更危险 ⇒ 只许跳"其产出对剩余任务非关键"的失效传播集内任务；
insert 一个与指令无关的任务 = 借机塞私货 ⇒ 必须能说清"为达成指令还缺
什么"（提示词约束 + directive 留痕双证）。

**行为兼容**：`allow_actions` 默认 `("replace",)` 时，解析走 P3 旧函数
`parse_llm_plan`（逐字未动），提示词输出与 P3 逐字一致（门禁 U11b/U11c）
⇒ P3 的 124 项回归一夜之间不会因 P4 全红。

## 6. P4 实施与验证记录

| 模块 | 文件 | 内容 |
|---|---|---|
| 巡检纯函数 | `services/replan_health.py`（新，425 行） | 节奏/M1/M2/M4/主判定/early_stop/LLM 巡检/决策 meta |
| 三动作解析 | `services/replan_directive.py`（+140 行） | `parse_llm_plan_actions` + `llm_redraw_prompt(allow_actions=)` |
| 三动作计划 | `services/replan_redraw.py`（改） | `quality_gates(insert_ok=)` Q3 双轨 + `plan_llm_redraw(allow_actions=)` 三道防线 |
| 配置 | `core/config.py`（+7 键双登记） | 三闸默认关 + 节奏 + 预算 + `llm_actions="replace"` |
| 落库 | `database/schema.py`（+18 行） | `replan_decisions` 表 + 两索引（即时写，不依赖 span 开关） |
| 标注 | `workflows/planner.py` + `agent/pipeline_parts/stream.py` | 两套 planner prompt 对称加 `config.required` |
| 主链路 | `agent/pipeline_parts/stream.py`（+约 200 行） | 迭代边界巡检/早停段（总闸+分闸+try/except 旁路）、T3 透传 allow_actions、skip 应用、7 个决策点落库 |
| 前端 | `static/js/mods/11-pipeline.js`（+约 60 行） | 修订卡"跳过/插入"两节 + early_stop 人话徽章 + skipped 状态三处渲染（子任务卡/计划清单/DAG 灰虚线节点） |

**验证矩阵**（全绿）：

| 验证 | 结果 |
|---|---|
| 门禁 `verify_replan_p4.py`（静态契约 + 逐格 + 变异自证 6 项） | **135 PASS / 0 FAIL** |
| 行为探针 `_probe_replan_p4_behavior.py`（注入式假 LLM 端到端 A~G） | **39 PASS / 0 FAIL** |
| 前端探针 `_probe_replan_p4_frontend.py`（抽真函数体渲染） | **21 PASS / 0 FAIL** |
| 回归：P1/P2/P3 门禁 | 60 / 66 / **124** PASS / 0 FAIL |
| 回归：P3 行为探针（重跑） | 40 PASS / 0 FAIL |
| 回归：`verify_orch_planning`（E4 判据重标定为比率） | 25 PASS + 1 变异 |
| CI | `.github/workflows/ci.yml` P3 段后注册 P4 门禁 |

**零行为变更证明**（探针场景 A + G + 门禁 U13）：三闸默认关 ⇒ 无
health/early_stop 决策落库、无巡检 span、无 skipped 子任务、计划清单
行数不变、`llm_actions` 锁 replace ⇒ T3 通道与 P3 逐字一致。

**实施期修掉的真 bug**（都有探针/门禁钉住，防回归）：

1. 被 skip 的任务未从 kept 排除 ⇒ Q5 误杀 + cancelled 行重复派发风险
   （门禁 U10c + 变异 M5）；
2. `dropped` 口径不一致（gates-fail 与成功路径返回不同集合）
   ⇒ 统一 `sorted(failed)`，skip 独立字段；
3. 提示词 insert 规则串内 ASCII 双引号截断 Python 字符串 ⇒ 中文引号；
4. **reflection 重写 orch_content 抹掉早停标注** ⇒ reflection 后幂等
   重追加（探针 C7 + 门禁 S4i）。

## 7. 数据缺口与复标条件

| # | 缺口 | 影响 | 复标条件 |
|---|---|---|---|
| G1 | conflicts（M2）历史频率未知 | M2 只有合成门禁覆盖，无密度分布 | 运行期 `replan_decisions.metrics.M2_conflict` 积累 ≥50 条后复跑 §1 统计 |
| G2 | `claims.to_verify=0`（M3） | M3 停用中 | 协议侧开始产出 to_verify claims 且 ≥30 天后复标；**无基线不开阈值** |
| G3 | early_stop 收益面无真库数据 | "平均每次收敛省几个任务"未知 | `early_stop` 开启运行 2~4 周后复跑统计工具 §3 |
| G4 | replan_decisions 上线前的老库无此表 | 工具对老库输出 n/a | 老库下次 `init_db` 自动建表（`CREATE TABLE IF NOT EXISTS`，零迁移） |
| G5 | 大 DAG（>6 任务）巡检行为无样本 | M1 窗口=3 在大图上的表现未知 | run 任务数 max 从 6 抬升后对比复跑 |

## 8. 复跑方式

```bash
# 阈值标定数据（只读，不写生产库）
python tools/_replan_decisions_stats.py [db_path]

# P4 门禁（含 6 项变异自证）
python tools/verify/verify_replan_p4.py

# P4 行为探针（临时库 + 假 LLM，约 5 分钟）
python tools/_probe_replan_p4_behavior.py

# P4 前端探针（Node 抽真函数体）
python tools/_probe_replan_p4_frontend.py
```
