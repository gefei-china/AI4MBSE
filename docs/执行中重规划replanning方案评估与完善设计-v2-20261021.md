# 执行中重规划（Replanning）方案评估与完善设计 — v2

> 2026-10-21 · 基于《执行中重规划replanning调研与设计方案-20261112》（下称 **v1**）
> \+ 行业标杆调研（Manus / Claude Code / CrewAI / LangGraph 生态 / OpenAI Deep Research / Dify·Coze）
>
> **纪律同 v1：只出评估与设计，不动代码。** 现状描述以当前 HEAD（`20e24b0`）代码为准。
> v1 写于 HEAD `26c63f6`，其后两次提交（`569100b` 即 v1 本身、`20e24b0` #53）未触碰
> replanning 相关代码 —— 本文已逐项复核：`stream.py` DAG 主循环（L985-1140）、`task_queue.py`
> 原语、`services/failure_class.py` 六类、`services/subtask_protocol.py`、`services/orch_span.py`
> 六 kind 均未变动，**v1 的代码事实全部仍然有效**，本文不重复罗列，只引用。
> 所有「标杆做法」均附证据索引（附 A）；所有结论区分「代码事实」与「设计建议」。

> **决策记录（2026-10-21，米爸拍板 §8 五问）**：
> ① T1 自动补跑默认**开**；② T2 用户可见形态选 **(b) 计划修订卡**（确认卡保持默认关，
> 作为可选项保留）；③ 重画预算**从 run 级 token 预算扣**（不单开）；④ 真库基线统计
> **已执行**（见《replanning真库基线统计报告-20261021》），并据数据修正两处设计：
> **T1 排除 interrupted 类**、**M3 暂不启用**；⑤ LLM 版三动作 **replace 进 P3**，
> skip/insert 留 P4 后评估。

---

## 0. 结论摘要（TL;DR）

1. **v1 总体评价：骨架达标，可以作实施基线；差距不在"能不能做对"，而在"计划作为
   一等公民"的产品化能力。** v1 的信号源盘点（S1~S9 带证据强度）、三条不变量
   （done 复用 / key 稳定 / 重过 validate_plan）、无进展检测（同因同 key 二次即 T4）
   经标杆交叉验证**达到或超过多数开源 replanner 的工程水准**；十维对标中 6 维
   ★★★、3 维 ★★、1 维 ★★☆（评分见 §1.1）。
2. **与标杆的核心差距共 10 项（§3），Top6 按严重度**：
   G8 S1/S2 在 DAG 循环的挂点与竞态未定义（P3 实施必踩，🔴）；
   G1 计划无版本/diff/回滚数据载体（Manus 可回滚版本、CrewAI PlanEvent、🔴）；
   G2 无"逐步健康度巡检"，信号全失败后置（CrewAI observer、🔴）；
   G4 无 skip 级，失效传播=全部下游 block 过粗（LangGraph router SKIP、🔴）；
   G3 无"提前收敛"（CrewAI GoalAchievedEarly、OpenAI DR 覆盖度早停、🟡）；
   G5 重画无质量门，只有拓扑校验（deep-research-engine 严格 plan 校验、🟡）。
3. **v2 完善 = 「五加一」**（全部默认关，零行为变更纪律不变）：
   ① 分级细化（补 skip 动作与 S10 巡检信号）；② 计划版本模型与 diff（零 DDL 先行）；
   ③ 健康度巡检 + 提前收敛两个横切机制；④ 重画质量门（五门）；⑤ 决策记录
   （span kind=replan 零 DDL 先行）；**加一 = 用户交互**（计划修订卡 / 确认卡）。
4. **v2 不推翻 v1 任何一条不变量与门禁**，只在 v1 骨架上长肉；变更台账见 §5，
   逐条注明理由。实施顺序修订为四阶段（§7），P1/P2 与 v1 基本相同（+决策记录），
   P4 为新增（巡检/早停）。
5. **最大风险从 v1 的"体感与成本"细化为三条新风险**（§6）：巡检误报（把设计使然
   报成失效 —— Task #43 同款教训）、提前收敛误判、确认卡阻塞。均已设计缓解 +
   变异门禁。

---

## 1. v1 方案评估

### 1.1 十维评分（设计建议，非代码事实）

| # | 维度 | v1 | 评分 | 一句话依据 |
|---|---|---|---|---|
| D1 | 计划表示与版本 | 有 lineage 字段设计；无版本/diff/回滚 | ★★☆ | 数据半成品，前端无载体 |
| D2 | 重规划触发时机 | S1~S9 信号源盘点 + 证据强度分级 | ★★★ | 系统性超过多数 replanner；但全失败后置 |
| D3 | 分级响应动作 | T0~T4 五级 | ★★★ | 缺 skip / 提前收敛 |
| D4 | 修订粒度 | 失效子图差量重画（§4.2 算法） | ★★★ | 与 LangGraph scoped replan / LLM Compiler 子树冻结同级 |
| D5 | 用户交互 | reasoning 事件留痕 | ★★ | 无计划卡片、无确认、无可视化 diff |
| D6 | 终止与收敛 | 无进展检测（同因同 key 二次→T4） | ★★★ | 比行业"轮次上限兜底"更根本；但无"够了就停" |
| D7 | 质量门 | validate_plan 拓扑校验 | ★★ | 新计划内容质量无门 |
| D8 | 可观测与决策留痕 | span 树六类 + stream_events | ★★ | "为什么重画/为什么不重画"无结构化记录 |
| D9 | 成本策略 | 重画预算从 run 总预算扣；默认关 | ★★★ | 缺模型分层（便宜规划模型） |
| D10 | 上下文衔接 | artifact_digest + build_subtask_context 口径现成 | ★★★ | 被删 key 的 offload 引用关系待明确（G10） |

**总评：骨架 A-，产品化 B-。** v1 把"该不该重画、怎么重画不炸"想清楚了；
没想细的是"重画作为产品功能"的三个一等公民：**版本（G1）、过程（G2/G8）、
交互（G6）**。

### 1.2 v1 做对的四件事（标杆交叉验证，实施时必须保持）

1. **三条不变量**（done 永不重跑 / 新 key + lineage / 重过 validate_plan strict）：
   开源 replanner 的常见缺陷正是"重跑已完成步骤"与"静默改图"。Manus 用 todo.md
   重写 + 文件系统外化上下文达到类似效果（附 A-1），Claude Code 用 TodoWrite
   运行计划保证"进度可读"（附 A-2）；v1 用数据库不变量达到同等强度且可门禁化。
2. **无进展检测优先于轮次上限**：CrewAI `max_replans=3`、LangGraph 常见
   `max_replans=2` 都是轮次兜底；v1 的"同 error_class + 归一化 error 文本在同一
   task_key 谱系第 2 次出现 → 直接 T4"是**主判据**，与 Dify"记录每一次重试决策"
   （附 A-6）的纪律同向且更严。
3. **"四个重"语义表**（retry/resume/rerun/replan 互斥判定树）：Claude Code 把
   breakpoint resume / 重发 / 计划修订混在"错误恢复 triad"里讲（附 A-2），v1 显式
   划清互斥边界，评审成本低。
4. **保守默认（T2/T3 默认关）+ 开关打开必须留痕**：与 Dify effect 标签约束
   （read/write/destructive，写操作绝不盲目自动重试，附 A-6）、OpenAI DR
   "停止原因显式 log，静默截断会误导用户"（附 A-5）产品哲学一致。

### 1.3 v1 的五个内在局限（逻辑推演 + 代码取证）

> 以下五点不是"标杆有所以 v1 差"，而是 v1 自身逻辑推演 + 代码取证发现的缺口。

**L1 失效传播 = 全部下游，过粗。** §4.2 定义"失效传播 = F 中每个 key 的全部下游
（按当前 deps 图逆向可达）"。代码事实：`release_deps`（task_queue.py L481）只把
**全部 deps 均 done** 的 planned 任务解锁为 ready ⇒ 一条失败会让传递下游全部
冻在 planned。但工程现实中，下游的核心输入往往只是多个上游之一，另一个上游
已 done 时"跳过失效输入继续跑"优于"整支冻结"。**缺 skip 语义**（对标 LangGraph
supervisor 的 SKIP 动作，附 A-4）。

**L2 信号全失败后置，无"计划健康度"持续评估。** S3~S8 都是"事情已经坏了"才响。
CrewAI 每步一次 observer 判定"剩余计划是否仍有效"（medium 档起，附 A-3），
OpenAI DR 在搜索循环中持续判断"信息是否矛盾/不足"并改写 query（附 A-5）。
v1 缺这一层 ⇒ 缓慢漂移型失败（每一步都"成功"但整体偏离 goal） today 无信号。

**L3 无计划版本/diff，"重画必须可见"缺数据载体。** v1 §6 R4 要求"重画必须发
reasoning 事件 + 前端 subtask 行可见 retry/lineage"，但：代码事实 ——
`agent_tasks` 表（database/schema.py L883-912）无 version/lineage/batch 列
（lineage 只能塞 `config` JSON）；`stream_events` 无 plan_revised 事件类型；
前端无 diff 视图。**"可见"目前只能做到"多几行新 subtask"，做不到"计划从 v1
变成 v2，改了什么"。**

**L4 S1/S2 与 follow-up 的挂点竞态未定义。** v1 §3.2 把 S1（用户生成中补充指令）
归 T3 整图重画，但代码事实：follow-up 队列（agent/followup.py）的消费点是
**单个 Agent 的 ReAct 轮次边界**（stream.py L2758-2791，DAG worker 内的子管道），
**DAG 主循环（L985-1140）没有任何消费点**。用户指令到达时若 DAG 正在跑，
今天发生的事=指令躺在 followup 队列里被某个子 Agent 消费或 TTL 300s 过期丢弃。
P3 要接 S1/S2，必须补 DAG 级挂点 + 与 follow-up 的分工，否则设计落空。

**L5 T3 整图重画与不变量 1 的冲突只在原则层。** §4.1 规定"done 永不重跑"，
§3.2 又规定 T3 = "重新 generate plan"。LLM 产出的新图**极可能包含对已完成
任务的重复规划**（它只看到 goal + 摘要，不知道哪些 key 已 done）。v1 没有给出
"新图撞车 done 任务时怎么办"的算法层处理（过滤？转 context 引用？拒收？）——
G5 质量门要补的正是这一格。

---

## 2. 行业标杆对标（2026-10-21 调研）

### 2.1 标杆选取依据

选取标准：① 产品级 replanning 能力有公开实证；② 覆盖不同技术路线
（todo 文件式 / 计划模式式 / 观察者分级式 / 图状态机式 / RL 内化式 / 低代码式）；
③ 与 MBSE 场景（DAG 编排 + 结构化交付 + 强确定性要求）有可比维度。

### 2.2 十维对标矩阵

| 维度 | 标杆主流做法（代表） | v1 | v2 目标 |
|---|---|---|---|
| D1 计划表示与版本 | Manus：todo.md 逐步重写 + 版本回滚；CrewAI：PlanEvent(CREATED/UPDATED)；Claude Code：TodoWrite 运行计划 | lineage 字段 | **版本模型 + diff + 修订卡（G1）** |
| D2 触发时机 | CrewAI：每步 observer（medium 起）；OpenAI DR：循环内持续评估覆盖度；Manus：todo 重写时自评 | 失败后置 S1~S9 | **+ 健康度巡检（S10，默认关）（G2）** |
| D3 分级动作 | LangGraph supervisor：EXECUTE/RETRY/SKIP/REPLAN/TERMINATE 五动作；Dify：错误类型分支 | T0~T4 | **+ skip 动作（规则版仅叶子，LLM 版完整）（G4）** |
| D4 修订粒度 | deep-research-engine：scoped replan 保留已完成、只换剩余；LLM Compiler：子树冻结 + 热合并替代子 DAG；CrewAI：PlanRefinement（轻）vs PlanReplan（全） | 失效子图差量 | **保持；+ 轻/重两级显式命名** |
| D5 用户交互 | Manus Plan Mode：执行前可编辑计划 + 确认才动工 + 执行中可再规划；Claude Code plan mode：只读门禁到批准；OpenAI DR：思考轨迹可见、可中途打断 | reasoning 留痕 | **计划修订卡 + 确认卡（默认关）（G6）** |
| D6 终止收敛 | CrewAI：GoalAchievedEarly 早停；OpenAI DR：覆盖度早停（2+ 独立信源/新颖性耗尽）+ 预算硬停（20-30min、30-60 次搜索）；v1：无进展检测 | 无进展检测 | **保留 + 提前收敛（默认关）（G3）** |
| D7 质量门 | deep-research-engine：plan 输出严格校验后才进 state；PEV：validator 0-1 评分 + 纯 Python router | validate_plan 拓扑 | **五门（结构/职责/针对性/规模/依赖闭合）（G5）** |
| D8 可观测留痕 | CrewAI：五类 Plan/Observation 事件带原因与保留步数；OpenAI DR：停止原因显式 log；Dify：节点级 I/O 追踪 + 每次重试决策记录 | span 六类 | **+ replan span/decision record（G7）** |
| D9 成本策略 | CrewAI：planner 用便宜模型（haiku）+ executor 用强模型；observer 每步一次的显式成本账单；Dify：effect 标签限定自动重试范围 | run 预算统扣 | **+ 模型分层配置（G9）** |
| D10 上下文衔接 | Manus：recitation 把目标推回近期注意力；文件系统外化；Claude Code：scratchpad "done/doing/next" | artifact_digest 口径 | **保留；+ 失效任务"曾尝试方向"防重复失败** |

### 2.3 标杆关键证据（逐条，详见附 A 索引）

| 标杆 | 可直接借鉴的设计 | 证据要点 |
|---|---|---|
| Manus | Plan Mode：结构化可编辑计划 → 人工确认成为 source of truth → 执行中可再激活 Plan Mode 规划下一阶段；版本可回滚 | 附 A-1 |
| Claude Code | 主 Agent 监控子任务状态，某子 Agent 失败**只重派该子任务**不重启整任务；运行计划（todo）每步更新保进度可读；stop hook 检出 N 次相同调用 → 强制重规划或交还控制权；错误恢复 triad（改参重试/降级/升级人工） | 附 A-2 |
| CrewAI | reasoning_effort 三档：low（只观察不重画）/ medium（**失败才重画**，默认）/ high（每步 decide_next_action，可早停/全重画/轻精修）；`max_replans=3`；PlanRefinement（精修未来步描述，不全重画）与 PlanReplanTriggered（剩余计划根本性错误才全重画，**保留已完成步**）分立；GoalAchievedEarly 早停；observer 每步一次显式成本；planner 可用便宜模型 | 附 A-3 |
| LangGraph 生态 | Plan-Execute-Replanner：从失败点重画、保留已完成结果；supervisor 五动作 + 确定性护栏（重试/重画预算）优先于 LLM 决策、LLM 输出非法时确定性兜底；scoped replan 把被替换步的失败记录**显式清除**防陈旧错误传播；PEV：validator 评分 → 纯 Python router 重试（反馈注入 prompt）→ 重画（带失败上下文）→ failed，全审计累加器；LLM Compiler：节点失败→冻结下游子树→异步局部重画→**热合并**替代子 DAG | 附 A-4 |
| OpenAI Deep Research | 证据矛盾/不足 → 生成新 query 开新一轮（回溯死路、换策略）；覆盖度早停（每子问题 2+ 独立信源、新颖性耗尽、矛盾已解决或显式记录）；预算硬停；**未完成部分在报告里显式标注**；停止原因显式 log | 附 A-5 |
| Dify / Coze | 节点级重试次数/间隔可配；错误类型（超时/鉴权/参数）分支走不同处理路径；连续失败达阈值**暂停并保存状态**等人介入（paused = success-with-pending，非失败）；effect 标签（read/write/destructive）约束自动重试范围——**写操作绝不盲目重发**；每次重试决策留日志 | 附 A-6 |
| Google ADK（对照） | 控制室检出工具失败 → 动态创建 replanning 子 Agent 用更宽目标重试；2 次尝试后明确 FAILED | 附 A-7 |

---

## 3. 差距清单（G1~G10，按严重度）

| # | 差距 | 标杆依据 | v1 现状 | 严重度 | v2 归属 |
|---|---|---|---|---|---|
| G1 | 计划无版本/diff/回滚载体 | Manus 版本回滚；CrewAI PlanEvent | lineage 只能塞 config JSON；无事件无视图 | 🔴 高 | §4.3 |
| G2 | 无逐步健康度巡检 | CrewAI observer；OpenAI DR 覆盖度 | 信号全失败后置 | 🔴 高 | §4.4 |
| G3 | 无提前收敛 | CrewAI GoalAchievedEarly；OpenAI DR 早停 | 只有"该停不跑"（无进展），没有"够了就停" | 🟡 中 | §4.4 |
| G4 | 无 skip 级 | LangGraph supervisor SKIP | 失效传播=全部下游 block | 🔴 高 | §4.2 |
| G5 | 重画无质量门 | deep-research-engine 严格校验；PEV validator | 仅 validate_plan 拓扑 | 🟡 中 | §4.5 |
| G6 | 用户计划交互缺失 | Manus Plan Mode；Claude Code plan mode | T3 靠 follow-up 隐式触发；无卡片/确认/编辑 | 🟡 中 | §4.8 |
| G7 | 决策无结构化留痕 | CrewAI 事件；Dify 重试决策日志；OpenAI DR 停止原因 log | reasoning 事件一次性、不可聚合 | 🟡 中 | §4.6 |
| G8 | S1/S2 挂点与竞态未定义 | Claude Code 中断语义；followup 轮次边界纪律 | DAG 主循环无消费点；P3 必踩 | 🔴 高 | §4.7 |
| G9 | 重画模型/成本策略未定义 | CrewAI 便宜 planner + 显式成本账单 | 未提 | 🟢 低 | §4.9 |
| G10 | 被删 key 的 offload 引用关系未定义 | Manus recitation；防重复失败 | 新任务上游衔接口径不全 | 🟢 低 | §4.10 |

---

## 4. 完善设计（v2）

### 4.1 总纲：「五加一」

| # | 增强 | 对应差距 | 默认 | 零 DDL 先行？ |
|---|---|---|---|---|
| ① | 分级细化：skip 动作 + S10 巡检信号 + 轻/重两级命名 | G4/G2 | 关 | 是 |
| ② | 计划版本模型 + diff + 修订卡 | G1/G6 | 事件常开（仅 T2/T3 触发时）；确认卡默认关 | 是（复用 assigned_by + config.lineage） |
| ③ | 健康度巡检 + 提前收敛（横切机制） | G2/G3 | 关 | 是 |
| ④ | 重画质量门（五门） | G5 | 随 replanning.enabled | 是 |
| ⑤ | 决策记录（span kind=replan） | G7 | 随 T1 起常开 | 是（复用 orch_run_spans） |
| 加一 | 用户交互：计划修订卡 + 确认卡 | G6 | 修订卡随事件；确认卡默认关 | 是 |

**总纪律不变**：所有新增强默认关或仅观测；开关打开行为才变且必须留痕；
每阶段独立提交、独立门禁、可独立回滚。

### 4.2 分级响应矩阵 v2（§3.2 修订）

信号表沿用 v1 §3.1（S1~S9），**新增 S10**；其中 **S3 按基线数据细化为 S3'**
（v1 原文"timeout/transient/internal 属之"未含 interrupted，但 failure_class
将 interrupted 归可重试 —— 真库 58 条失败中 50 条 interrupted 全是
`reap_stuck_runs` 的孤儿标记、run 已终止，补跑无意义 ⇒ T1 显式排除，
interrupted 走 resume 分支，见基线报告 §2）：

| # | 信号 | 来源 | 强度 | 说明 |
|---|---|---|---|---|
| S10 | 健康度巡检指标超阈 | §4.4 巡检器（默认关） | 中 | 缓慢漂移型：每步都"成功"但整体偏离 |

| 级别 | 触发 | 动作 | 默认 | 用户可见性 |
|---|---|---|---|---|
| T0 忽略 | S9 / 无信号 / S10 未超阈 | 维持现状（partial 如实汇总） | 开 | 已有 partial 前缀 |
| T1 补跑 | S3'（timeout/transient/internal，**排除 interrupted**） | 同 key `retry` 一次 | **开**（已拍板） | subtask 事件 retry_count+1 + decision record |
| T1.5 跳过 | **叶子失效任务**（无下游，规则版）；非关键输入失效（LLM 版） | `cancel` + reason="输入非关键/无下游"；下游不阻塞 | 关 | subtask 行置灰 + 删除线 + reason |
| T2 局部重画（轻） | S10 超阈 且 失效子图 ≤ 半数任务 | PlanRefinement 式：只精修/替换失效子图 | 关（配置开） | plan_revised 事件 + diff 卡 |
| T2 局部重画（重） | S4/S5/S6/S7 且 失效子图 ≤ 半数任务 | 差量重画失效子图（v1 §4.2 + §4.10） | 关（配置开） | 同上 |
| T3 整图重画 | S1/S2 经影响评估确需 | 带"修订原因+已完成产物"重新 generate plan | 关（配置开） | plan_revised + 计划对比 |
| T4 升级人工 | T2/T3 失败、无进展检测触发、重画预算耗尽、质量门不过 | 停手，blocked + 澄清卡（复用既有澄清机制） | 开 | 澄清卡/blocked 原因 |

**轻/重两级命名**（借鉴 CrewAI PlanRefinement vs PlanReplanTriggered）：T2-轻只改
失效子图内任务的 title/context/expected_output/agent（不动 key 与 deps 拓扑），
T2-重才换 key 与拓扑。轻级可用规则拼装（把失败原因注入原任务 context 重派），
**这是规则版 T2 的最小可用形态**，比 v1 §4.2 的"失效子图原样重试/换 Agent 参数"
更细且不动图拓扑，确定性风险更低。

**skip 的规则版边界**（设计建议）：规则版只认一种 —— 失效任务在 deps 图中
**无下游**（叶子）。叶子失败今天已经不会冻结别人（ready 不会恒空），但会在汇总里
记 partial；skip 的增量价值仅是"明确标记为主动跳过而非失败"，为 T2 省一次
重画判定。**真正有价值的 skip（下游只依赖失效任务的部分输出）需要 LLM 判断
输入关键性，归 T2-LLM/T3，默认关。**

### 4.3 计划版本模型与 diff（G1）

**数据基础（零 DDL，设计建议）**：代码事实 —— `agent_tasks` 已有
`assigned_by`（planner | orchestrator | manual）与 `config`（JSON）列、`created_at`。
v2 约定：

1. replan 产出的新任务一律 `assigned_by='replanner'`，`config.lineage =
   {"from": ["t4"], "reason": "...", "round": 1, "action": "replace|insert"}`；
2. **版本分段算法**（纯函数，便于门禁单测）：同 run_id 内按 `created_at` 排序，
   每遇 `assigned_by` 从 'planner' 跳变为 'replanner' 即开启一个新版本段；
   每段含 {version, action, reason, added_keys, removed_keys}；
3. 被移除的旧任务：`cancel` + error=「计划修订 R{n}：{reason}」（复用既有
   cancel 原语，task_queue.py L365；**不物理删行** —— 删行会让"为什么这条任务
   没了"不可追溯，v1 不变量 2 同款纪律）。

**diff 结构**（plan_revised 事件 payload，设计建议）：

```json
{
  "type": "plan_revised", "run_id": 9573, "round": 1, "level": "T2",
  "reason": "t4 超时（error_class=timeout），其下游 t6/t7 输入前提失效",
  "diff": {
    "added":   [{"key": "t4_r1", "title": "…", "agent": "…", "action": "replace"}],
    "removed": [{"key": "t6", "title": "…", "reason": "输入前提失效"}],
    "remapped":[{"key": "t7", "deps": ["t3", "t4_r1"]}],
    "kept": 3
  },
  "budget": {"tasks_before": 6, "tasks_after": 6, "tokens_used": 12345}
}
```

**回滚：v2 明确不做**（列入 §4.12 不做清单）。理由：Manus 的版本回滚是"站点
产物"语境；DAG 语境的"回到上一版计划"语义 = 取消 replanner 任务 + 复活被 cancel
的旧任务（`retry` 原语可复活 blocked/failed → ready，但 canceled 复活要新原语）
+ 重跑 —— 这已经落入 rerun/resume 语义交叉区，收益不抵复杂度。用户侧对抗误重画
的手段是：确认卡（G6）+ 手动重发（现状已有）。

### 4.4 健康度巡检与提前收敛（G2/G3，横切机制，均默认关）

**A. 计划健康度巡检（产出 S10）**

规则版指标（纯函数，无 LLM，设计建议）：

| 指标 | 计算 | 超阈动作 |
|---|---|---|
| M1 partial 密度 | 近 N 个完成任务中 summary.status=partial 的占比 | ≥ 50% 且 N≥3 → S10（**已标定**：全库基线 partial 率 10.0%，50%=基线 5 倍远离噪声，见基线报告 §4） |
| M2 冲突密度 | detect_conflicts 累计命中数 / 完成任务数 | ≥ 1 且未解决 → S10（强；⚠️ 真库不落库、频率未知，E1 缺口，P1 探针补） |
| M3 未验证 claims 密度 | 下游任务 deps 中含 claims.to_verify 的任务数 / 剩余任务数 | **暂不启用**（真库历史 to_verify=0 条，无基线无法标定；P1 探针积累 2~4 周后复标，E2 缺口） |
| M4 预算偏离 | 剩余任务数 × **滑动窗口平均耗时**（近 N 个 done 实际耗时，不用全历史平均） vs 剩余预算 | > 120% → S9 语义（该停，不重画）（已标定：任务耗时中位 76s/p90 149s/max 347s，长尾可达 605s） |

巡检节奏：每 `probe_every_tasks`（默认 3）个任务完成，或每 `probe_every_sec`
（默认 60）秒，取先到者。**巡检只产信号，不直接动作** —— S10 仍要过 §4.2 矩阵
（T2-轻/重），这是防误报的第一道闸。LLM 版巡检（`health_probe_llm`，默认关）：
轻量模型评估"剩余计划是否仍匹配 goal + artifact_digest"（CrewAI observer 同款），
成本 +1 次轻量调用/巡检，计入 run_tokens。

**B. 提前收敛（early_stop，替代 T0 的一个终止分支）**

规则版判据（保守，设计建议）：planner 建图时在 task config 标注
`required: true` 的产出型任务**全部 done**、且无 failed 传播链 ⇒ 取消剩余
`required` 非真的任务（cancel + reason="目标已达成提前收敛"）→ 进汇总。
汇总状态记 `full`（非 partial —— 剩余任务是主动战略放弃，不是失败；
这与 v1 partial 语义的区分要在汇总三态里显式实现）。
LLM 版判据：goal 覆盖度评分 ≥ 阈值且每个 required 产出有 evidence.basis。
对标：CrewAI GoalAchievedEarlyEvent、OpenAI DR 覆盖度早停。

**为什么两个都默认关**：M1/M3 判据在 MBSE 场景有已知的"设计使然"陷阱 ——
Task #43 取证教训（2026-10-20 日志）：按需消费语义下"未触发"≠"失效"，摘要够用时
下游不读全文是设计使然。同理，partial 密度高可能因为任务本身是探索型。
**门禁必须用探针法证明"该行为在此场景必然发生"**（见 §6 R8）。

### 4.5 重画质量门（G5，五门，任一不过 ⇒ 整个重画方案作废 → T4）

| 门 | 断言 | 手法 |
|---|---|---|
| Q1 结构门 | 过 `validate_plan`（strict）——环/自环/重复 key/悬空依赖 | 已有原语 |
| Q2 职责门 | 新任务 key 不与任何 done/failed/canceled 任务的 key 相同；**LLM 版新增后校验**：新任务 title/expected_output 与 done 任务相似度 > 阈值 ⇒ 拒收或降级为 context 引用（防 LLM 重画撞车，补 L5） | 规则 + 可选相似度 |
| Q3 针对性门 | T2 每个新任务的 `config.lineage.from` 必须指向失效 key 集合；**无 lineage 的孤儿新任务 ⇒ 拒收**（防 LLM 借机塞私货/扩地盘） | 纯函数 |
| Q4 规模门 | 新图任务数 ≤ 原图剩余任务数 × `redraw_expand_ratio`（默认 1.5） | 纯函数（v1 已有，升格为门） |
| Q5 依赖闭合门 | 新任务 deps ⊆ {保留 key} ∪ {新 key}；引用被删 key ⇒ 整个方案作废（create_plan 的 deps 过滤会静默丢弃脏依赖 —— 重画场景必须显式断言而非静默） | 纯函数 |

Q2 的相似度判据可选 LLM embedding 或简单 Jaccard；第一版建议 Jaccard ≥ 0.6 即
拒收入工审查（宁可漏重画，不可重复建房 —— 与 v1 保守取向一致）。

### 4.6 决策记录与可观测（G7）

**零 DDL 先行（设计建议）**：`orch_run_spans` 表已存在（Task #43），新增
`kind='replan'` span（parent=run span），meta_json 带：

```json
{"signals": ["S3:t4:timeout"], "level": "T2", "action": "replan",
 "quality_gates": {"Q1": "pass", "Q3": "pass"},
 "round": 1, "added": ["t4_r1"], "dropped": ["t6"], "tokens": 830, "latency_ms": 4100}
```

**每一次 §4.2 判定都落一条 replan span，包括 action="none" 的决策**
（"为什么不重画"与"为什么重画"同等重要 —— OpenAI DR 停止原因显式 log 同款）。
stream_events 同步 `plan_revised`（§4.3 payload）；前端 trace 面板渲染 ⓘ 节点。

**实施注意（代码事实）**：Task #43 门禁 `verify_orch_span.py` S3 系列按 kind
计数/种类断言（当前六类），加第七类 kind 必须同步门禁，否则 CI 红 —— 列入实施
清单（§7 P1）。P4 若要做真库统计（partial→full 转化率、误重画率）再建
`replan_decisions` 表（run_id, round, signals, level, action, reason, ...），
SQL 聚合比 span JSON 扒取方便。

### 4.7 S1/S2 的 DAG 挂点与竞态（G8）

**分工原则（设计建议）**：follow-up 管"单 Agent 轮内"（现状不动）；**DAG 级
用户指令 = replan 信号源，新挂点**。与 followup.py "只在轮次边界消费、不打断
tool_call"同款纪律：DAG 级指令也只在**主循环迭代边界**消费（v1 §4.3 的插入点
正是迭代边界，天然兼容）。

**竞态三况处理**：

| 况 | 场景 | 处理 |
|---|---|---|
| C1 | 用户在 DAG 执行中发消息 | 消息仍走现有消息通道（入库 + SSE）；主循环每次迭代开始检查 `conversation_id` 的 pending 指令（复用 followup 队列或新开 DAG 级队列 —— 建议复用，followup 已有分槽/锁/TTL 300s/2000 字上限，DAG 级消费只是多一个消费方）⇒ 有则进「影响评估」 |
| C2 | 用户在汇总后发消息 | 新一轮编排（现状，不动） |
| C3 | resume 路径 | 不消费（v1 R6：resume 本身即"图仍成立"的断言） |

**影响评估（S1/S2 的必经之路，防"一句话修正"误触发 T3）**：轻量 LLM 调用
（`replanning.llm_model`，默认继承）判断补充指令/新附件影响哪些**未执行**任务：
- 只影响单个任务的产出要求 → T1/T2-轻（改该任务 context 重派）；
- 推翻任务划分/输入前提 → T2-重/T3；
- 纯信息类（"辛苦了"）/ 已被 done 任务覆盖 → T0 忽略。
评估结论 + 依据写 decision record。**没有影响评估的 S1/S2 不得直接进 T3**
（v1 §3.2 把 S1 直接归 T3 是缺口，v2 修正）。

**附件（S2）特例**：msg 带 doc_id/parse_status 变化的处理：附件解析完成事件
也走影响评估；附件改写输入前提 → T3 时新图任务的 context 必须带附件引用
（artifact_digest 已含附件产物，P0-6 机制现成）。

### 4.8 用户交互：计划修订卡与确认卡（G6）

**计划修订卡（随 plan_revised 事件，无需新配置）**：
编排卡内新增版本徽章「计划 v2 · 重画 1 轮」；点击展开 diff 面板：
+ 新增（绿）/ - 删除（灰+删除线+reason）/ ~ 重映射（deps 变化）/ 保留 n。
subtask 行：replanner 行加徽章「↻ 重画·R1」，hover 显 lineage
（from/reason）；removed 行置灰。trace 面板 replan span ⓘ 可展开
signals/质量门结果。**这就是 v1 §6 R4"重画必须可见"的完整落地形态。**

**确认卡（`user_confirm_redraw=True` 时启用，默认关）**：T2/T3 执行前暂停
DAG（复用澄清卡机制），卡片 = 新旧计划对比（diff）+ 三个动作：
[批准重画] [维持原计划] [我来补充指令]。TTL 默认 60s，超时**维持原计划继续**
（R10：确认卡不得成为新阻塞点）。分工原则（沿用 v1 §8.2 建议）：澄清卡用于
信息不足，重画用于证据充分 —— 确认卡是给"想要控制权"的用户的可选项，
不是默认流程。

### 4.9 成本与模型策略（G9）

| 项 | 设计建议 |
|---|---|
| 模型分层 | `replanning.llm_model`（默认空=继承主模型）。重画/影响评估/LLM 巡检优先用轻量档（CrewAI 便宜 planner 同款）；MBSE 场景 planner 输出是结构化 JSON，轻量模型通常足够 |
| 预算 | 所有 replan 相关 LLM 调用计入 run_tokens，从 run 总预算扣（v1 §8.3；**已拍板按建议，不单开钱包**） |
| 成本可见 | 每次重画的 tokens/latency 写 decision record；修订卡显示「本次修订成本 ≈ N tokens」—— 把 CrewAI 的"显式成本账单"做成用户可见 |
| 重画成本预估 | T2/T3 触发时若预估超预算 ⇒ 直接 T4（与 S9 预算信号合并判定）。**预估口径（基线报告 §5 标定）**：新任务数 × 近期同 Agent 任务耗时中位；真库口径 = 任务耗时中位 76s / p90 149s / max 347s ⇒ 一次 T2（≤1.5×剩余，中位剩余 3→≤4 任务）≈ 中位 5min / p90 10min |

### 4.10 算法 v2（v1 §4.2 修订）

```
input:  run_id, done_items(已完成key→结论摘要), failed_items(key, error_class, error),
        remaining(未执行key→title), goal, artifact_digest, round,
        health_signals(可选, S10), user_directive(可选, S1/S2经影响评估), allow_llm
output: {"action": "none"|"early_stop"|"replan",
         "new_plan": [...], "dropped": [key], "skipped": [key],
         "reason": str, "signals": [...], "cost": {...}}

0. （early_stop 开）required 产出任务全 done 且无 failed 传播 ⇒ action="early_stop"
1. 失效子图识别：F = failed_items 的 key 集合；失效传播 = F 的全部下游（同 v1）
2. 保留集 K = 全部 key - 失效传播；
   skip 集 = F 中无下游的 key（叶子）——T1.5；
   K 中 done 的结果进 new_plan 的 context 引用（不重跑）；
   K 中未执行的原样保留（deps 中被删的失效 key 重映射到重画后新 key）
3. 重画输入 prompt = goal + artifact_digest + 已完成任务结论摘要（≤400字/个）
   + 失效任务及原因（error_class 归类）+ **失效任务曾尝试的方向**（防重复失败，
   无进展检测的 prompt 层配合）+ 修订轮次 round + （user_directive 原文）
4. 重画：
   - 规则版（第一版）：T2-轻 = 失效任务原 context + 失败原因拼装重派（key 不变，
     retry_count+1，deps 不变）；T2-重 = 换 Agent 参数重派（新 key + lineage）
   - LLM 版（llm_redraw 开）：只允许产出失效子图的替代任务，动作限
     replace/skip/insert（skip/insert 仅 LLM 版）；不得重复已完成任务的 key 与职责
5. 质量门 Q1~Q5（§4.5），任一不过 ⇒ action="none" ⇒ T4（附质量门失败原因）
6. 新图 = K(保留) ∪ skip 集(取消) ∪ 重画任务；过 validate_plan（Q1）
7. 规模/轮次上限（同 v1）：超限 ⇒ action="none" ⇒ T4
```

**与 v1 §4.2 的差异**：步骤 0（早停）、步骤 2（skip 集）、步骤 3（曾尝试方向）、
步骤 5（质量门）、步骤 4（三动作 + 轻/重两级）。核心算法（失效传播 + 保留集 +
增量约束 prompt）不变。

**上下文口径补全（G10）**：被删 key 的 offload（共享产物工作区引用）**不**传给
新任务 —— 其产物前提已被推翻；done key 的 offload 照常传。曾尝试方向的来源：
failed_items 的 error 文本 + 该任务 result 前缀（若有）—— 新任务 prompt 里显式
列出"这条路过不通：{error 摘要}"，防 LLM 重画原样再撞。

### 4.11 配置项 v2（增量）

```
replanning.enabled             = False   # 总闸（沿用 v1）
replanning.retry_once          = True    # T1（沿用 v1）
replanning.max_redraw_rounds   = 1       # T2 轮次上限（沿用 v1）
replanning.redraw_expand_ratio = 1.5     # 规模上限（沿用 v1）
replanning.llm_redraw          = False   # T3 LLM 整图重画（沿用 v1）
replanning.llm_model           = ""      # 【新】重画/影响评估/LLM 巡检用模型（空=继承主模型）
replanning.health_probe        = False   # 【新】计划健康度巡检（规则版，产出 S10）
replanning.health_probe_llm    = False   # 【新】巡检升级 LLM 评估（轻量调用）
replanning.early_stop          = False   # 【新】目标达成提前收敛
replanning.user_confirm_redraw = False   # 【新】T2/T3 前出确认卡（TTL 后维持原计划）
replanning.probe_every_tasks   = 3       # 【新】巡检节奏：每 N 个任务完成
replanning.probe_every_sec     = 60      # 【新】巡检节奏：或每 N 秒
replanning.confirm_ttl_sec     = 60      # 【新】确认卡 TTL（超时维持原计划）
```

双登记（core/config.py DEFAULT_CONFIG + CONFIG_SCHEMA）沿用 v1 §6.2 纪律。

### 4.12 明确的"不做"清单（v1 三项 + v2 四项）

- 不做多 plan 候选投票/beam search（v1，成本与确定性双输）。
- 不做"全局最优重画"（v1，DAG ≤12 任务，局部差量足够）。
- 不在画布路径（Flow executor）做重画（v1，改画布即可，语义不同）。
- **不做计划版本回滚**（§4.3：落入 rerun/resume 交叉区，收益不抵复杂度；
  用户侧用确认卡 + 手动重发覆盖）。
- **不做 steering（即时注入）**：DAG 级用户指令只在迭代边界消费（与
  followup.py 既定纪律一致；即时注入破坏执行中的 tool_call 序列）。
- **规则版不做"非关键输入 skip"**：需要 LLM 判断输入关键性，归 LLM 版默认关；
  规则版只认叶子 skip（§4.2）。
- **不做重画前的 dry-run 预演**：内存孪生跑一遍新图再决定是否采纳 —— 成本
   double-hit，且确定性环境（Mock）预演结果不可外推；用质量门 + 真库回放（G4）
  替代。

---

## 5. v1 → v2 变更台账

| v1 条目 | v2 变更 | 理由 |
|---|---|---|
| §3.1 信号 S1~S9 | + S10（巡检）；**S3 细化为 S3'（排除 interrupted）** | G2；基线报告 §2 |
| §3.2 矩阵 T0~T4 | + T1.5 skip；T2 分轻/重；S1/S2 必经影响评估；T1 默认**开**（已拍板） | G4；L4；CrewAI 轻/重分级；决策① |
| §3.3 无进展检测 | 不变（升格为终止主判据的地位在 §1.2 确认） | — |
| §4.1 不变量三条 | 不变；+ Q2~Q5 质量门（§4.5） | G5；L5 |
| §4.2 算法 | 步骤 0/2/3/4/5 增强（§4.10） | G3/G4/G5/G10 |
| §4.3 挂点 | 不变；+ S1/S2 竞态三况与分工（§4.7） | G8；L4 |
| §5 边界表 | 不变 | — |
| §6 风险 R1~R7 | + R8 巡检误报 / R9 早停误判 / R10 确认卡阻塞 / R11 新 kind 门禁同步 / R12 评估失败保守 T0 | §6 |
| §6.2 门禁 G1~G5 | + G6 决策记录完整性 / G7 质量门变异 / G8 diff 一致性 / G9 早停巡检变异 / G10 竞态 | §6 |
| §6.2 配置 5 键 | + 8 键（§4.11） | §4 |
| §6.3 不做 3 项 | + 4 项（§4.12） | §4 |
| §7 三阶段 | 修订为四阶段（+P4 巡检/早停；P1 +决策记录 + 基线已跑） | §7 |
| §8 开放问题 4 条 | **五问全部已决**（2026-10-21 拍板，见 §8 决策记录） | 决策①~⑤ |
| §4.4 巡检 M1~M4 | M1/M4 阈值真库标定；**M3 停用**（无基线） | 基线报告 §4 |
| §4.9 成本 | 重画成本预估口径真库标定（中位 76s/任务） | 基线报告 §5 |

---

## 6. 风险与门禁（v2 增量）

### 6.1 风险增量

| # | 风险 | 缓解 |
|---|---|---|
| R8 | 巡检误报：把"按需不触发/摘要够用"等**设计使然**报成计划失效（Task #43 同款教训：行为观测类断言要先证明"该行为在此场景必然发生"） | 巡检只产信号不过矩阵；探针法门禁（确定性复现注入逻辑）；规则版指标全部可解释（M1~M4 无黑箱）；默认关 |
| R9 | 提前收敛误判：目标未达却停 | 规则版仅认 planner 显式 `required:true` 标注；LLM 版双确认（覆盖度评分 + evidence.basis）；默认关；汇总态显式区分（非 partial） |
| R10 | 确认卡成为新阻塞点（用户不点，DAG 干等） | TTL 默认 60s 维持原计划继续；不阻塞汇总兜底；默认关 |
| R11 | 新增 span kind='replan' 破坏 Task #43 门禁 S3 系列计数/种类断言 ⇒ CI 红 | 实施清单显式同步 verify_orch_span.py（P1 第一个提交） |
| R12 | 影响评估 LLM 调用本身失败/超时 | 评估失败 ⇒ 保守按 T0 忽略（不重画）+ decision record 记"评估失败"；绝不因评估故障触发重画 |

### 6.2 门禁增量（沿用本仓纪律：纯函数单测 + 内存孪生变异 + 真库只读回放 + CI 双口径）

| 门禁 | 断言 | 手法 |
|---|---|---|
| G6 决策记录完整性 | 凡 action≠"none" 必有 replan span（含 signals/质量门/成本）；**action="none" 也必有**（为什么不重画） | 纯函数单测 + 端到端回放 |
| G7 质量门变异自证 | ① 放过无 lineage 的孤儿新任务 ⇒ Q3 红；② 放过大规模膨胀 ⇒ Q4 红；③ 关掉 Q2 相似度 ⇒ LLM 重画撞车 done 任务被抓住 | 内存孪生变异（exec 变异源码进模块全局副本） |
| G8 diff 一致性 | plan_revised 事件 payload 与库状态（agent_tasks 行 + assigned_by/config.lineage）逐字段一致 | 真库回放（同 Task #43 API 与库逐字段一致手法，不写生产库） |
| G9 巡检/早停变异 | ① 关掉"required 全 done"判据 ⇒ 早停不触发；② 巡检阈值置无穷 ⇒ S10 永不超阈；③ M1 判据在"设计使然"探针场景下不得超阈（防误报的具体断言） | 内存孪生变异 + 探针法 |
| G10 竞态 | C1 执行中用户指令只在迭代边界消费；C3 resume 路径零消费；评估失败 ⇒ T0 | 合成时序单测（线程 + 队列，followup.py 同款锁语义） |

真库回放数据集（G4/G8）：v1 §6.2 已列的 conv 9571（partial run）+ 本轮新增
取证候选：run 539（t1 撞 600s 硬超时，Task #43 e2e 已实证的失败形态）。

---

## 7. 实施顺序（v2 修订，四阶段）

| 阶段 | 内容 | 交付物 | 回滚方式 |
|---|---|---|---|
| P1 观测与信号结构化（不动执行） | S3~S7 汇成观测结构 + **replan span/decision record（含 action=none）+ plan_revised 事件**；T1 补跑（S3'，排除 interrupted） | 信号探针 + G1/G6 门禁 + **真库基线已跑完**（《replanning真库基线统计报告-20261021》：T1 收益面 100% 可重试类、排除 interrupted 后活场景 13.8%）+ R11 门禁同步 | 配置关 retry_once |
| P2 差量重画（规则版） | §4.10 算法（轻/重两级 + skip 叶子）+ 质量门 Q1~Q5 + 无进展检测；T2 默认关 | G2/G3/G7 门禁 + G4 真库回放 | replanning.enabled=False |
| P3 用户指令触发 + 可视化 | S1/S2 挂点与影响评估（§4.7）+ T3（llm_redraw 默认关）+ 修订卡/确认卡（§4.8） | G8/G10 门禁 + 评测集 A/B 零回退证明 | 配置关 |
| P4 巡检与早停（新增） | health_probe（规则版优先）+ early_stop；真库标定 M1~M4 阈值 | G9 门禁 + 阈值标定报告（同冲突检测阈值标定的既有手法） | 配置关 |

每阶段独立提交、独立门禁（与 #49~#53 批次纪律一致）。**P1 的第一个提交必须是
R11（orch_span 门禁同步）**，否则后续全红。

---

## 8. 开放问题决策记录（2026-10-21 米爸拍板，全部已决）

| # | 问题 | 决策 | 落地影响 |
|---|---|---|---|
| 1 | T1 自动补跑默认开还是关？ | **开** | `replanning.retry_once=True`；规则据基线数据细化为 S3'（timeout/transient/internal，**排除 interrupted**——50/58 历史失败是 reap_stuck_runs 孤儿标记、run 已终止，补跑无意义，归 resume；见基线报告 §2） |
| 2 | T2 局部重画的用户可见形态？ | **(b) 计划修订卡** | 修订卡成为 P3 标准交付（零成本可见）；确认卡 `user_confirm_redraw` 保持默认关、作为可选项保留（§4.8） |
| 3 | 重画预算从哪扣？ | **按建议**：从 run 级 token 预算扣，不单开 | §4.9 预算行确认；所有 replan LLM 调用计入 run_tokens |
| 4 | 真库基线统计现在跑？ | **按建议执行（已跑完）** | 《replanning真库基线统计报告-20261021》：58 失败 100% 可重试类；partial 率 10.0%；失效传播比 0.00；任务耗时中位 76s。⇒ 数据修正两处设计（T1 排除 interrupted、M3 停用），T2 默认关获数据加强 |
| 5 | LLM 版三动作（skip/insert）进 P3？ | **按建议**：replace 进 P3，skip/insert 留 P4 后评估 | §4.10 步骤 4 不变；P3 范围锁定 replace |

---

## 附 A：标杆证据索引（2026-10-21 检索）

| # | 来源 | 关键引文要点 |
|---|---|---|
| A-1 | Manus Plan Mode（alphasignal.ai/news/manus-ships-plan-mode…）+ Manus 工程博客《Context Engineering for AI Agents》（manus.im/blog 2025-07-18，转引自 hivebook 词条） | 结构化可编辑计划 → 人工确认成为 source of truth → 执行中可再激活 Plan Mode 规划下一阶段；版本可回滚；todo.md 逐步重写把目标推回近期注意力（recitation）；平均 50 次工具调用/任务；文件系统即外化上下文 |
| A-2 | Claude Code / Claude Agent SDK（neurals.ca/tech/claude/agentic-coding；callsphere.ai/blog/debugging-claude-code…；segmentfault 深度拆解） | plan mode 只读门禁到批准；TodoWrite 运行计划每步更新保"进度可读"防循环；主 Agent 监控子任务，某子 Agent 失败只重派该子任务不重启整任务；错误恢复 triad（改参重试/降级完成/升级人工）；stop hook 检出 N 次相同调用强制重规划或交还控制权；断点续跑；Anthropic 强调可观测性是生产级核心 |
| A-3 | CrewAI（claudeskills.info/skills/crewaiinc/skills/design-agent；docs.crewai.com event-listener） | reasoning_effort 三档（low 只观察 / medium 失败才 replan / high 每步 decide_next_action 可早停）；max_replans=3、max_steps=20；StepObservationCompletedEvent 含"剩余计划是否仍有效+建议精修"；PlanRefinementEvent（精修未来步描述）vs PlanReplanTriggeredEvent（剩余计划根本性错误，保留已完成步）；GoalAchievedEarlyEvent；observer 每步一次显式成本；planner 可用便宜模型 |
| A-4 | LangGraph 生态（github.com/AdvitR/deep-research-engine；Zane-Qbb/LLM--Agent 的 LLM Compiler 范式；dev.to PEV 模板；sotaaz Plan-Execute-Replanner） | supervisor 五动作 EXECUTE/RETRY/SKIP/REPLAN/TERMINATE + 确定性护栏（重试/重画预算、完成检查）优先，LLM 决策非法时确定性兜底；scoped replan 保留已完成步只换剩余，**被替换步的失败记录显式清除**防陈旧错误传播；plan 输出严格校验后才进 state；失败作为一等数据；LLM Compiler：节点失败→冻结下游子树→异步局部重画→热合并替代子 DAG；PEV：validator 0-1 评分+纯 Python router，重试注入反馈、重画带失败上下文、全审计累加器、haiku/sonnet 成本分层 |
| A-5 | OpenAI Deep Research（blog.promptlayer.com/how-deep-research-works；quidproquo.cc deep-research 长文） | 证据矛盾/不足→生成新 query 开新一轮，回溯死路换策略；两层停止：覆盖度早停（每子问题 2+ 独立信源、新颖性耗尽、矛盾解决或显式记录）+ 预算硬停（20-30min、30-60 次搜索、120-150 页、150-200 轮、代码 5-10 次）；未完成部分在最终报告显式标注；停止原因显式 log（静默截断误导用户）；思考轨迹对用户可见可打断 |
| A-6 | Dify / Coze（docs.dify.ai error-handling；ones.com.cn / wiz.cn 工具测评） | 节点级重试次数/间隔可配；错误类型（超时/鉴权/参数校验）分支走不同处理路径；连续失败达阈值暂停并保存状态等人介入，paused=success-with-pending 非失败，凭 token 恢复且可再次暂停；effect 标签 read/write/destructive 约束自动重试——写操作绝不盲目重发、每次重试决策记日志；节点级输入输出追踪 |
| A-7 | Google ADK Codelab（codelabs.developers.google.com/next26/scale-agents） | 控制室检出工具失败→动态创建 replanning 子 Agent 用更宽目标重试→2 次尝试后明确 FAILED（"明确失败"优于无限循环的对照样例） |

## 附 B：v1 代码索引（沿用，HEAD=20e24b0 复核状态：全部仍有效）

| 机制 | 位置 | 复核 |
|---|---|---|
| 流式编排入口 / DAG 主循环 | `agent/pipeline_parts/stream.py` L97 / L985-1140 | ✅ 未变 |
| resume 语义（同图恢复） | `agent/pipeline_parts/stream.py` L114 注释 | ✅ |
| 计划生成 | `workflows/planner.py` L185（`_planner_core`）/ L240-286 | ✅ |
| build_subtask_context（摘要口径） | `workflows/planner.py` L11 | ✅ |
| DAG 原语 | `task_queue.py` L53/L118/L286-333/L365/L462/L481 | ✅ retry 只翻转状态不增 retry_count（retry_count 列存在，schema L994 注"流式不重试恒 0"）—— v2 T1 实施时需补计数 |
| 失败分类 | `services/failure_class.py` L61 六类 + interrupted / L101 classify | ✅ |
| 结构化交付摘要 | `services/subtask_protocol.py` L585 / L817 | ✅ |
| span 树观测 | `services/orch_span.py` L34-39 六 kind / L51 SpanCollector / L138 run_trace | ✅ 加第七类 kind 需同步门禁（R11） |
| follow-up 微重规划 | `agent/pipeline_parts/stream.py` L2758-2791、`agent/followup.py` | ✅ 分槽/锁/TTL 300s/2000 字上限，DAG 级可复用为新消费方 |
| RefineGate（事后修文） | `workflows/refine.py` L131 | ✅ |
| 画布检查点（对比用） | `workflows/persistence.py` L29 / L48 | ✅ |
| agent_tasks 表结构 | `database/schema.py` L883-912 | ✅ 无 version/lineage 列 ⇒ v2 走 assigned_by + config JSON（§4.3） |
| orch_run_spans 表 | Task #43 建（2026-10-20 日志） | ✅ replan span 零 DDL 载体 |
