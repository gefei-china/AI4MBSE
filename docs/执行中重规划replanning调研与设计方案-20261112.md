# 执行中重规划（Replanning）调研与设计方案

> 2026-11-12 · 米爸优先级 0「继续下一步执行中规划（replanning），先调研再实施」
> 本文档**只出调研与设计，不动代码**（用户明确要求）。所有现状描述均以当前 HEAD
> （26c63f6）代码为准，附代码索引；所有结论区分「代码事实」与「设计建议」。

---

## 0. 结论摘要（TL;DR）

1. **今天没有"执行中重规划"，只有四层相邻能力**：follow-up 微重规划（ReAct 轮内）、
   RefineGate 事后修文（汇总后）、planner_auto 流程复用（计划前）、
   resume/rerun（同图恢复/重跑）。DAG 一旦开跑，**计划即冻结**：子任务失败 →
   下游 blocked → break → partial 汇总，全程无重试、无重画（代码事实，见 §1.4）。
2. **重规划的切口已经存在且很窄**：`_stream_orchestrated_flow` 的 DAG 主循环
   （stream.py L985-1140）每次迭代都重新读 `TaskQueue.ready_tasks` ——
   在循环体内插入"重画判定 + 局部补图"即可，不需要重构执行器。
3. **触发信号今天已经结构化**：`agent_tasks.error_class`（services/failure_class.py
   六类 + 可重试判定）、subtask_protocol 的结构化交付摘要（status/missing/risks/
   claims.to_verify）、 orch_span span 树。缺的不是信号，是**消费信号的决策层**。
4. **建议分级响应，不做"万能重画"**：T0 忽略 / T1 原 key 补跑一次 / T2 失效子图
   局部重画 / T3 整图重画（仅用户补充指令触发）/ T4 升级人工。默认只开 T1，
   T2/T3 配置开关默认关（与 `intent.rule_llm_confirm` 同款的"默认零行为变更"纪律）。
5. **与 checkpoint-rerun 的边界用一句话划清**：resume/rerun 是**同一张图**的
   状态恢复/重跑（已有，LangGraph checkpointer 语义）；replan 是**换一张图**
   （新 create_plan）。二者互斥，判定树见 §5。
6. **最大风险不是算法而是体感与成本**：一次整图重画 = 全量子任务重跑 + 用户再等
   55~344s。因此终止条件（轮次/预算/无进展检测）与"重画必须可见"（reasoning 事件）
   和算法本身同等重要。

---

## 1. 现状盘点（代码级事实）

### 1.1 两条编排路径

| 路径 | 入口 | 计划生成 | 执行 | 持久化 |
|---|---|---|---|---|
| 会话流式编排 | `stream.py:_stream_orchestrated_flow`（L97） | `workflows/planner.py:_planner_core`（L185）LLM 一次生成 | 同函数内 DAG 主循环（L985-1140），实时转发子任务 token | `agent_tasks`（TaskQueue） |
| Flow 画布执行器 | `workflows/engine.py` + `persistence.py` | 画布预定义节点 | `_safe_cp` 逐节点检查点 | `flow_runs` / `flow_checkpoints` |

会话路径是 replanning 的主战场（用户对话内 AI 自主拆解任务）；画布路径是人工预设
流程，重规划需求弱（改画布即可），本文只覆盖会话路径，画布路径仅在 §5 边界处对比。

### 1.2 DAG 原语（task_queue.py，全部现成）

- `validate_plan`（L53）：Kahn 拓扑检环/自环/重复 key/空 key —— **重画后必须复用**。
- `create_plan`（L118，strict=True 校验不过抛 ValueError）：批量落库。
- `ready_tasks`（L232）/ `pending_count`（L247）：就绪集 = 依赖已 satisfied。
- `complete`（L286）/ `fail`（L297，带 `error_class` 结构化分类）/ `block`（L327）。
- `release_deps`（L481）：任务完成后释放下游 —— **只在 complete 路径调用**，
  这是"失败即冻结下游"的直接机制。
- `retry`（L462）/ `cancel`（L365）/ `reap_stuck_runs`（L422）：已有重试/取消/收割，
  但 `retry` 目前只有工作台手工触发（routers/studio_parts/flows.py:447），
  **编排主循环不调用**。

### 1.3 计划生成：一次成型，无二次规划

`_planner_core`（planner.py L185）流程：LLM 生成 plan（L240-286）→ 解析失败降级
单 Agent 直跑（L289-302）→ 技能校验（L304-313）→ `create_plan` → while 循环执行
（L330-369）→ 汇总（L373-381）→ RefineGate（L382-394）。

prompt 已支持两类"增量约束"输入（重画 prompt 可直接复用）：
- `artifact_digest`（会话既有产物摘要，L232-238 + L260-265）："必须在既有产物上做增量，
  禁止规划从零分析类任务"；
- `team_prompt`（团队拆解规则 + 成员名册，L268-269）。

### 1.4 失败语义现状：冻结，不是适配

流式路径 DAG 主循环（stream.py L985-1140）实测行为：

```
ready = ready_tasks(run_id)
if not ready:                      # 下游被 fail 阻塞后就落到这里
    for tk in tasks: if planned/ready: block(tk, "依赖任务失败，无法推进")
    break                          # ← 整批冻结，进入 partial 汇总
```

- 任务失败 → `TaskQueue.fail`（L1126）→ `release_deps` 不释放 → 下一轮 ready 空 →
  全部 block → break。**没有任何"换个方式再试一次"的分支**。
- 超时有两层：worker_timeout_s（默认 120s，planner.py L341）与 reap_stuck_runs
  （30 分钟收割，只标记不重跑）。
- 汇总三态：`full / partial / failed`（stream.py L142、L1261-1285，
  services/subtask_protocol.py:585 `summarize_status`）—— 今天 partial 只是
  **如实汇报**，不驱动任何执行决策。

真库实证（conv 9571，msg 4692 前缀「编排状态：partial——1 个子任务自报部分完成」）：
用户看到 partial 后只能手动重发/补充，系统不会自适应。

### 1.5 已有的四层"适应性"机制（重规划的左邻右舍）

| 层 | 机制 | 位置 | 语义 | 局限 |
|---|---|---|---|---|
| L1 微重规划 | follow-up 队列 | stream.py L2758-2791、agent/followup.py | 生成中收到补充指令 → 重置 ReAct 轮次预算（≤3 次） | 只在**单个 Agent 执行内**，不改 DAG |
| L2 事后修文 | RefineGate | workflows/refine.py:131 | 汇总后质量评审 → 未达标修订**正文** | 不重跑任何子任务，不改 DAG |
| L3 计划前复用 | planner_auto | stream.py L2355 `_try_reuse_planner_flow` | 命中已发布沉淀流程 → 跳过重新规划 | 发生在计划生成**之前**，非执行中 |
| L4 同图恢复 | resume/rerun | stream.py L114（resume_run_id 语义注释） | resume=从检查点恢复（done 复用）；rerun=clear_run 同图重跑 | **同一张图**，不换图 |

结论：**"执行中根据证据改图"这一格是空的** —— 这正是 replanning 的定位。

### 1.6 观测底座（重规划的可观测性依赖，均已就绪）

- `services/orch_span.py`：span 树（run/plan/task/summary/reflection/views 六类），
  内存收集 → 流尾 flush；`run_trace(conn, run_id)` 可按 run 回放每段耗时/token。
- `orch_stream_events`：stage/tool 事件流（回放"发生了什么"）。
- `services/subtask_protocol.py`：子任务结构化交付摘要（status/summary/artifacts/
  evidence.basis/risks/claims.kind/confidence）+ `detect_conflicts`（多 Agent 同对象
  互斥取值检出）—— **claims.to_verify 与 conflicts 是天然的"该重画"信号**。

---

## 2. 术语与边界：四个"重"的语义表

| 概念 | 图是否变 | 状态是否复用 | 现状 | 本文归属 |
|---|---|---|---|---|
| **retry**（补跑） | 不变 | 失败任务自身重跑 | 有原语，主循环不调 | replanning 的 T1 级 |
| **resume**（恢复） | 不变 | done 结果全部复用，只补未完成 | 已有（resume_run_id） | 不动 |
| **rerun**（重跑） | 不变 | 全部重跑（clear_run 清空） | 已有（Flow 节点重跑/消息重发） | 不动 |
| **replan**（重画） | **变**（差量或整图） | done 结果复用；失效子图重画 | **空缺** | 本文主题 |
| **refine**（修文） | 不变 | 只改汇总正文 | 已有（RefineGate） | 不动，可作触发信号源 |

判定树（任一成立即判 replan，否则走左支）：

```
执行中收到新证据（任务失败/partial/冲突/用户补充）
├─ 证据只影响"单个任务的产出质量"，图结构仍成立？
│   ├─ 是 → retry（T1，同 key 补跑一次；error_class 可重试才自动补跑）
│   └─ 否 → 图的某部分（依赖假设/任务划分/输入前提）被证据推翻？
│       ├─ 否（只是没跑完）→ resume（T0，什么都不做）
│       └─ 是 → replan（T2 局部重画失效子图；用户补充指令 → T3 整图重画）
```

---

## 3. 触发条件设计（分级响应矩阵）

### 3.1 信号源盘点（全部已存在，按"证据强度"排序）

| # | 信号 | 来源 | 强度 | 说明 |
|---|---|---|---|---|
| S1 | 用户生成中补充指令 | followup 队列 | 最强 | 用户明示纠偏，今天只重置 ReAct 轮次 |
| S2 | 新附件到达/附件解析状态变化 | attachments（msg 带 doc_id/parse_status） | 强 | 附件可能改写任务输入前提 |
| S3 | 任务 fail 且 error_class 可重试 | agent_tasks.error_class + failure_class.is_retryable | 强 | timeout/transient/internal 属之 |
| S4 | 任务 fail 且不可重试 | permission/input_invalid/business | 中 | **不该重试**，但可能该重画（换输入/换 Agent） |
| S5 | 交付摘要 status=partial/failed 或 missing 非空 | subtask_protocol | 中 | 子任务自报"缺东西" |
| S6 | claims 含 to_verify 且被下游依赖 | subtask_protocol.claims | 中 | 下游在未验证结论上建房 |
| S7 | detect_conflicts 检出互斥取值 | subtask_protocol | 中 | 两个 Agent 对同一对象给互斥值 |
| S8 | RefineGate 未达标且 advice 指向结构问题 | workflows/refine.py | 弱 | 事后信号，此时已接近尾声 |
| S9 | 预算/超时/取消 | run_budget、worker_timeout、cancel 探针 | 弱 | 通常是该停不是该重画 |

### 3.2 分级响应矩阵（设计建议）

| 级别 | 触发 | 动作 | 默认 | 用户可见性 |
|---|---|---|---|---|
| T0 忽略 | S9 / 无信号 | 维持现状（partial 如实汇总） | 开 | 已有 partial 前缀 |
| T1 补跑 | S3 | 同 key `retry` 一次（error_class 可重试才自动） | **开**（建议） | subtask 事件 retry_count+1 |
| T2 局部重画 | S4/S5/S6/S7 且失效子图 ≤ 半数任务 | 差量重画失效子图（§4） | 关（配置开） | reasoning 事件「计划修订」+ 新 subtask 行 |
| T3 整图重画 | S1/S2 | 带"修订原因+已完成产物"重新 generate plan | 关（配置开） | reasoning 事件 + 计划对比 |
| T4 升级人工 | T2/T3 失败、或无进展检测触发、或重画预算耗尽 | 停手，blocked + 澄清卡（复用既有澄清机制） | 开 | 澄清卡/blocked 原因 |

**保守取向（宁漏勿误）的理由**：一次重画成本 = 全量子任务 + 55~344s 等待 + LLM
调用；误重画的代价（用户等待、重复写实体）大于漏重画（partial 如实汇报，用户可
手动重发）。因此 T2/T3 默认关，与 `intent.rule_llm_confirm` 默认 False 同款纪律：
**开关打开时行为才变，且必须留痕**。

### 3.3 无进展检测（终止条件核心）

同一失败原因（error_class + 归一化 error 文本）在同一 task_key 谱系上出现第 2 次
→ 判定无进展 → 直接 T4。防止"重画→同样方式失败→再重画"的死循环，这比轮次上限
更根本（轮次上限是兜底，无进展检测是主判据）。

---

## 4. DAG 重画算法设计（差量重画）

### 4.1 不变量（缺一条就不做）

1. **已完成任务结果复用**：`status=done` 的任务永不重跑（幂等，防重复写实体 ——
   本仓 #50/#51 附件重建的教训同源：重跑写操作必须可判定）。
2. **key 稳定性**：重画产生的新任务用**新 key**（`t4'` 或 `t4_r1`），config 带
   `lineage: {"from": "t4", "reason": "error_class=timeout", "round": 1}`。
   不用同 key 覆盖 —— 覆盖会让"为什么这条任务没了"不可追溯（validate_plan 注释
   里已确立的同款纪律：不静默改图）。
3. **图必须重新过 `validate_plan`**（环/自环/重复 key/悬空依赖），strict=True，
   不过就整个重画方案作废（T4），绝不"尽力而为落库"。
4. **重画 prompt 必须带增量约束**：复用 `_planner_core` 已有的 artifact_digest
   段落（"必须在既有产物上做增量，禁止从零分析"）+ 新增"修订原因"段（哪些任务
   失败/为什么/已完成任务的结论摘要）。

### 4.2 算法（纯函数，便于门禁单测）

```
input:  run_id, done_items(已完成key→结论摘要), failed_items(key, error_class, error),
        remaining(未执行key→title), goal, artifact_digest, round
output: {"action": "none"|"replan", "new_plan": [...], "dropped": [key], "reason": str}

1. 失效子图识别：F = failed_items 的 key 集合；
   失效传播 = F 中每个 key 的**全部下游**（按当前 deps 图逆向可达）——这些任务的
   输入前提已被推翻，留在图上只会 block。
2. 保留集 K = 全部 key - 失效传播。K 中 done 的结果进 new_plan 的 context 引用
   （不重跑）；K 中未执行的**原样保留**（key/title/deps 不变，只是 deps 中被删的
   失效 key 需要重映射到重画后的新 key —— 或让 LLM 重新表达依赖）。
3. 重画输入 prompt = goal + artifact_digest + 「已完成任务结论摘要（各≤400字，
   复用 build_subtask_context 口径）」+「失败任务及原因（error_class 归类）」+
   「修订轮次 round」。LLM 只被允许产出**失效子图的替代任务**（prompt 约束：
   不得重复已完成任务的key与职责）。
4. 新图 = K(保留) ∪ 重画任务；过 validate_plan。
5. 若新图任务数 > 原图剩余任务数 × redraw_expand_ratio（默认 1.5）或
   round > max_redraw_rounds（默认 1）→ 返回 action="none"（T4 升级人工）。
```

⚠️ 第 3 步的一个关键设计选择：**重画交给 LLM 还是规则**？建议第一版只做"规则
重画"（失效子图原样重试/换 Agent 参数），LLM 重画放 T3 且默认关 —— 因为 LLM
重画的不确定性会直接冲击"确定性保持"这条本仓铁律（Mock 环境行为漂移）。

### 4.3 与 DAG 主循环的挂点（不改循环结构）

循环体现在每次迭代都重读 ready_tasks。插入点：

```
while pending_count > 0 and guard < 200:
    ready = ready_tasks()
    if not ready:
        # 【新增】重画判定：有 failed 且非"从未开始" → 走 §4.2
        #   返回 replan → create_plan(new_plan) → continue（新一轮 ready 自然出现）
        #   返回 none   → 原逻辑：block 全部 + break
```

`create_plan` 已支持同 run_id 追加工任务（run_id 不变，agent_tasks 只是加行），
审计连续。span 树上体现为同一 run 下第二批 task span（parent=run）。

---

## 5. 与 checkpoint-rerun 的职责边界（详表）

| 维度 | resume | rerun | replan（本文） |
|---|---|---|---|
| 图 | 同一张 | 同一张 | **新的（差量）** |
| done 结果 | 复用 | 丢弃全跑 | **复用**（不变量 1） |
| 触发者 | 持久执行恢复（resume_run_id） | Flow 节点重跑/用户重发 | **执行中证据**（§3.1） |
| 计划生成 | 不调 LLM（避免二次拆分不同） | 不调 | **调**（T2 规则优先/T3 LLM） |
| 现状 | 已有（stream.py L114 注释语义明确） | 已有（clear_run） | **空缺** |
| 幂等风险 | 低（done 复用） | 中（写操作重放） | **高 → 靠不变量 1+key 稳定性兜住** |

一句话给代码评审用：**resume 回答"从哪继续"，rerun 回答"再跑一遍"，
replan 回答"计划错了该怎么改"。三者互斥，判定树见 §2。**

---

## 6. 风险登记与门禁设计

### 6.1 风险清单（按严重度）

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 无限重画（死循环烧钱） | 无进展检测（§3.3）+ max_redraw_rounds + 重画预算从 run 总预算扣 |
| R2 | 重复写实体（图谱/资产双写） | 不变量 1（done 不重跑）+ 新 key lineage；写操作工具幂等由 T8 受控上下文兜底（既有） |
| R3 | 上下文膨胀（重画 prompt 越带越大） | 已完成结论摘要 ≤400 字/个（build_subtask_context 口径）+ 总封顶 |
| R4 | 用户体感（静默重画=莫名等待） | 重画必须发 reasoning 事件（「计划修订：原因 X」）+ 前端 subtask 行可见 retry/lineage |
| R5 | 确定性回归（Mock/无 key 环境行为漂移） | T2 规则重画不调 LLM；T3 默认关；评测集 A/B 零回退（既有纪律） |
| R6 | 与 resume 打架（断点恢复后该重画还是该继续） | 判定树互斥；resume 路径不进入重画判定（resume 本身即"图仍成立"的断言） |
| R7 | 降级路径被重画吞掉 | 重画失败 → T4 原样 block + break，绝不静默吞异常 |

### 6.2 门禁方案（沿用本仓既有纪律）

| 门禁 | 断言 | 手法 |
|---|---|---|
| G1 触发判定 | §3.2 矩阵逐格：信号 → 级别（含"不可重试类不得自动补跑"） | 纯函数单测（合成 agent_tasks 行） |
| G2 差量重画算法 | 合成 DAG（菱形依赖+一跳失效）：保留集正确、失效传播正确、新图过 validate_plan、done 不被重画 | 纯函数单测 |
| G3 变异自证 | ① 关掉"done 复用" ⇒ G2 红（done 被重画）；② 关掉无进展检测 ⇒ 死循环被抓住；③ 删 `if not ready` 的重画分支 ⇒ T2 场景退化成 block（抓住"重画没接线"） | 内存孪生变异（本仓标准手法：exec 变异源码进模块全局副本，不写生产文件） |
| G4 端到端回放 | 真库历史失败批次（conv 9571 等 partial run）喂给孪生管道：重画后 partial→full 或明确 T4；**不写生产库** | 真库只读 + 临时 run_id |
| G5 接线与双口径 | CI 接线（.github/workflows/ci.yml）；真库 + 干净库（MBSE_DB_PATH 空库）双口径均绿 | 既有纪律 |

配置双登记（core/config.py DEFAULT_CONFIG + CONFIG_SCHEMA）建议键：

```
replanning.enabled            = False   # 总闸（默认关 = 零行为变更）
replanning.retry_once         = True    # T1：可重试类失败自动补跑一次
replanning.max_redraw_rounds  = 1       # T2 轮次上限
replanning.redraw_expand_ratio= 1.5     # 新图规模上限（防 LLM 重画膨胀）
replanning.llm_redraw         = False   # T3：LLM 整图重画（默认关）
```

### 6.3 明确的"不做"清单（防过度工程）

- 不做多 plan 候选投票/ beam search（成本与确定性双输）。
- 不做"全局最优重画"（DAG 小（≤12 任务），局部差量足够）。
- 不在画布路径（Flow executor）做重画（改画布即可，语义不同）。
- 不重写 TaskQueue（原语齐全，只加消费方）。

---

## 7. 实施顺序建议（三阶段，每阶段可独立回滚）

| 阶段 | 内容 | 交付物 | 回滚方式 |
|---|---|---|---|
| P1 观测与信号结构化（不动执行） | 把 S3~S7 信号汇成 `replan_signals` 观测结构，挂 orch_span/stream_events；T1 补跑（retry_once，仅可重试类） | 信号探针 + G1 门禁 + 真库统计"多少失败本可自动补跑" | 配置关 retry_once |
| P2 差量重画（规则版） | §4.2 算法 + `if not ready` 挂点 + 无进展检测；T2 默认关 | G2/G3 门禁 + G4 真库回放 | replanning.enabled=False |
| P3 用户补充指令触发整图重画 | S1/S2 → T3（llm_redraw 默认关）；前端「计划修订」可视化 | G4 扩展 + 评测集 A/B 零回退证明 | 配置关 |

每阶段独立提交、独立门禁，与 #49/#50/#51/#52 的批次纪律一致。

---

## 8. 待用户决策的开放问题（实施前需米爸拍板）

1. **T1 自动补跑默认开还是关？** 建议开（可重试类，成本一次子任务）；
   但"自动"二字触及「执行中自主权」边界，需确认。
2. **T2 局部重画的用户可见形态**：静默补跑（只加 subtask 行）还是先出确认卡？
   建议静默 + reasoning 留痕（与澄清卡分工：澄清卡用于信息不足，重画用于证据充分）。
3. **重画预算从哪扣**：建议从 run 级 token 预算扣（现有 run_budget 机制），
   不单开——单开预算等于给死循环开第二个钱包。
4. **真库 partial run 的基线统计**（P1 的第一步）要不要现在就跑一版，
   用数据决定 T1/T2 的默认值？

---

## 附：关键代码索引（HEAD=26c63f6）

| 机制 | 位置 |
|---|---|
| 流式编排入口/ DAG 主循环 | `agent/pipeline_parts/stream.py` L97（`_stream_orchestrated_flow`）、L985-1140 |
| resume 语义（同图恢复） | `agent/pipeline_parts/stream.py` L114 注释 |
| 计划生成 | `workflows/planner.py` L185（`_planner_core`）、L240-286（prompt） |
| 非流式编排 | `workflows/planner.py` L153（`run_planner_plan`）、L330-369 |
| DAG 原语 | `task_queue.py` L53（validate_plan）/L118（create_plan）/L286-333（complete/fail/block）/L462（retry）/L481（release_deps） |
| 失败分类 | `services/failure_class.py` L61（六类）/L101（classify）/is_retryable |
| 结构化交付摘要 | `services/subtask_protocol.py` L585（summarize_status）/L817（detect_conflicts） |
| span 树观测 | `services/orch_span.py` L51（SpanCollector）/L138（run_trace） |
| follow-up 微重规划 | `agent/pipeline_parts/stream.py` L2758-2791、`agent/followup.py` |
| RefineGate（事后修文） | `workflows/refine.py` L131（run） |
| planner_auto 复用 | `agent/pipeline_parts/stream.py` L2355 |
| 画布检查点（对比用） | `workflows/persistence.py` L29（_safe_cp）/L48（_load_checkpoints） |
| 子任务上下文快照（摘要口径） | `workflows/planner.py` L11（build_subtask_context） |
