# Agent 架构优化调整方案与影响分析

> 版本：v1.2 · 2026-08-14（v1.2 按 v1.1 复审修订：修复 2 个新硬伤——① dry_run 不约束工具写操作 → 编排路径写工具统一「暂存→汇总后批量挂确认队列」；② L2 确认时机与现状不符 → 明确改汇总后统一入队 + 确认单补 run_id/task_key 归属；并补 5 项次要问题：token usage 统计、超时线程占额度语义、预算降级目标态改为 partial 汇总、subtask_artifacts 保留策略、L2 确认支持按 artifact 勾选）
> 范围：核心 Agent 架构升级——主 Agent 全生命周期能力、主子 Agent 协作机制、子 Agent 隔离与信息交互（覆盖需求点 1~3 共 13 项）
> 场景：AI 建模页（pg-ai）对话编排的完整链路：意图识别 → 主 Agent 路由 → 简单任务单 Agent 直行 / 复杂任务自动编排 → 子 Agent 执行 → 汇总输出
> 依据：现状代码审计（[agent/registry.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/registry.py)、[agent/pipeline.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py)、[task_queue.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/task_queue.py)、[workflows/planner.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/workflows/planner.py)、[hil_service.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/hil_service.py)）+ v1.1 复审意见逐条核代码
> 目标：把「固定 6 Agent 编排池 + 文本透传」的初版协作架构，升级为「注册中心 + 动态编排 + 智能分派 + 严格隔离 + 结构化摘要协议 + 交付物可追溯 + 写操作受控」的完整闭环；存量行为零破坏

---

## 1. 需求场景与目标

### 1.1 需求点概览

| # | 需求点 | 核心要求 | 需求编号 |
|---|--------|---------|---------|
| 1a | 全生命周期编辑与修改 | 配置参数 / 逻辑规则 / 功能模块均可扩展 | FR-AG-1a |
| 1b | 高级意图识别 | 解析复杂指令与潜在需求 | FR-AG-1b |
| 1c | 智能任务规划与分派 | 按任务性质 + 子 Agent 能力动态分配 | FR-AG-1c |
| 1d | 内容整合与输出 | 多源信息结构化处理、统一输出 | FR-AG-1d |
| 1e | 动态 Agent 编排 | 灵活启用任意数量子 Agent，**突破固定数量限制** | FR-AG-1e |
| 2a | 子 Agent 注册管理 | 主 Agent 自动发现 / 识别 / 管理已注册子 Agent | FR-AG-2a |
| 2b | 智能任务规划算法 | 按任务需求 + 专长 + 当前负载最优分配 | FR-AG-2b |
| 2c | 任务状态跟踪 | 实时监控子 Agent 执行进度与结果反馈 | FR-AG-2c |
| 3a | 严格上下文隔离 | 子 Agent 环境 / 数据 / 状态相互独立，防泄露与干扰 | FR-AG-3a |
| 3b | 标准化摘要模块 | 子 Agent 结果提炼为结构化摘要返回主 Agent | FR-AG-3b |
| 3c | 主子通信协议 | 信息传递准确、完整、安全 | FR-AG-3c |
| 3d | 汇总输出系统 | 整合各子 Agent 摘要生成综合结果 | FR-AG-3d |

### 1.2 场景主流程（现状基线）

```
① 用户输入  意图识别（规则+LLM+DST）→ 主 Agent（注册表路由）
        ↓
② 简单任务  单 Agent 直行：知识检索 → LLM 生成 → 工具循环 → 卡片/文本
        ↓
③ 复杂任务  自动编排（_needs_orchestration 判定）
        规划（Planner 拆解）→ TaskQueue 落库（DAG 依赖）
        ↓
④ 子 Agent  dry_run 执行（隔离命名空间 + 结构化摘要回传）
        ↓
⑤ 物化      子 Agent 产物落独立命名空间（见 6.6，FR-AG-3a/3b）
        ↓
⑥ 汇总      _summarize_plan 消费结构化摘要 + RefineGate 反思修订 → 落库 + 沉淀工作流
```

### 1.3 设计目标

1. 主 Agent 配置 / 规则 / 模块全生命周期可维护，运营期零代码扩展（FR-AG-1a）。
2. 编排子 Agent 池由注册表动态供给，数量不再硬编码封顶（FR-AG-1e）。
3. 任务分派引入「专长匹配 × 负载」打分，执行层按 per-agent 并发上限**强制限流**（FR-AG-1c/2b）。
4. 子 Agent 运行上下文严格隔离，交互仅经结构化摘要协议，**交付物可物化、可追溯**（FR-AG-3a/3b/3c）。
5. 全程任务状态可追踪、结果可汇总、编排可沉淀复用；超时/重试/预算有护栏（FR-AG-2c/1d/3d）。
6. 对存量行为零破坏：默认编排行为、历史编排产物、会话链路完全兼容。

---

## 2. 行业优秀设计对照

| 产品 / 方案 | 做法 | 可借鉴设计 |
|------------|------|-----------|
| **AutoGen / Semantic Kernel** | 多个 Agent 通过统一 runtime 注册（tool/agent 动态发现），支持多 Agent 会话与角色轮转 | ① 注册中心统一发现 ② 会话内角色轮转 |
| **LangGraph** | 图状编排：节点=Agent/工具、边=状态转移，任务 DAG 显式建模，支持条件分支与并行 | ① DAG 编排显式化（现有 TaskQueue 对齐）② 状态转移可观测 |
| **CrewAI** | 角色制 Agent（Role/Goal/Backstory）+ 任务委派（Task Delegation）+ 流程管控（Sequential/Hierarchical） | ① 角色能力元数据 ② 任务委派协议（摘要/交付物） |
| **MetaGPT** | 标准化消息协议（消息路由 + 结构化输出）+ SOP 流水线：产品经理→架构师→工程师串行协作 | ① 结构化消息 Schema ② 角色 SOP 流水线 |
| **Celery / Temporal** | 任务队列持久化 + 依赖编排 + 重试/超时/人工介入，任务状态全程可查询 | ① 状态机完备 ② 超时/重试策略 ③ 负载感知调度 |
| **多智能体负载均衡（工业实践）** | 调度器按 Agent 空闲度/并发上限分配任务，执行层强制限流（信号量/令牌桶），避免热点 Agent 过载 | ① per-agent 并发上限 ② 运行时限流（非仅规划建议） |

### 2.1 共性模式（本方案设计原则）

1. **注册驱动**：一切子 Agent 经注册中心被发现，主 Agent 不硬编码 Agent 池（FR-AG-1e/2a）。
2. **能力契约**：子 Agent 声明专长 + 输入输出 Schema，分派与校验均基于契约（FR-AG-2b/3c）。
3. **结构化通信**：子 Agent → 主 Agent 只传结构化摘要，不传内部上下文（FR-AG-3b）。
4. **隔离运行 + 物化可溯**：子 Agent 独立上下文命名空间，数据/状态互不可见；交付物经独立命名空间物化，ref 可追溯（FR-AG-3a/3b）。
5. **运行时限流**：负载感知不止于规划打分，执行层按 per-agent 并发上限强制限流（FR-AG-2b）。
6. **可观测与可追溯**：任务状态机 + 负载快照 + 编排轨迹持久化（FR-AG-2c）。

---

## 3. 现状与差距分析

### 3.1 现状（已具备的能力）

- **Agent 定义与注册**：[registry.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/registry.py) DB `agents` 表优先、内置 DEFINITIONS 兜底；[definition.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/definition.py) 支持 tools / hil_level（L0/L1/L2）/ kb_scope / system_prompt 配置，AI 设计工坊可 CRUD。
- **意图识别**：[agent/intent.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/intent.py) 规则信号 + LLM 判定 + 会话意图继承（DST）+ 术语归一化 + 低置信度澄清（needs_clarification）。
- **编排判定与执行**：[pipeline.py#L408](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L408) `_needs_orchestration` → `_stream_orchestrated_flow`（Planner 拆解 → 子任务 dry_run 执行 → 汇总 → 反思修订 → 沉淀工作流）。
- **任务队列**：[task_queue.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/task_queue.py) `agent_tasks` 表状态机 + 依赖 DAG 推进 + 人工重试（P2）。
- **汇总与反思**：`_summarize_plan` LLM 汇总 + [workflows/refine.py](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/workflows/refine.py) RefineGate 质量评审自动修订。
- **前端实时展示**：编排 stage/subtask 事件流实时推送，card_data.exec.subtasks 轨迹持久化。

### 3.2 关键现状约束（评审核实的硬约束）

| 约束 | 代码事实 | 对本方案的影响 |
|------|---------|---------------|
| **dry_run 不落库** | 子任务执行 `dry_run=True`（[pipeline.py#L2039](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L2039)）；`dry_run` 语义为不写 messages/audit，card 兜底空卡（[L2296](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L2296)、[L2597-2600](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L2597)） | 子 Agent 产物无正式 message id → **摘要协议 ref 不能指向正式会话**，需独立物化命名空间（6.6） |
| **执行层并发硬编码** | `ThreadPoolExecutor(max_workers=min(len(ready), 3))`（[L2074](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L2074)），全局 3 并发 | max_concurrency 若只参与打分则**不构成运行时限流**，需 per-agent 信号量（6.7） |
| **无子任务超时** | `guard < 200` 仅防死循环（[L2002](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L2002)），无 wall-clock 超时 | 需补充超时机制（6.7） |
| **共享 SQLite（WAL）** | 子 worker 与子 Pipeline 实例共享单文件库；现状**主线程统一落库**（worker 只收集事件） | 保留主线程串行落库，并发写锁风险登记（9.2/12） |
| **人工重试已存在** | `TaskQueue.retry()` 为人工 P2（[task_queue.py#L111](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/task_queue.py#L111)） | 编排路径需补自动重试（6.7） |
| **dry_run 不约束工具写操作（v1.1 复审）** | `_exec_tool_call(name, arguments)`（[pipeline.py#L1233](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L1233)）不感知 dry_run；写保护仅两道：L2 写工具→即时确认队列（[L1253-1266](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L1253)）、destructive 工具→强制确认（[L1268-1283](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L1268)）；**L0/L1 非 destructive 写工具会真实写库** | 6.6 的"不写正式表"保证必须有执行层拦截支撑（6.6.1 写操作受控） |
| **L2 确认单归属模糊（v1.1 复审）** | 现状 L2 写工具在子任务执行期间**即时入队**，确认单 `conversation_id=conv_ctx or 0`（dry_run 子任务 conv_ctx=0 → 归属模糊）；`hil_confirmations` 已有 run_id 列但 `_exec_tool_call` 未传（默认 0）、无 task_key 列 | 编排路径改「汇总后统一入队」+ 确认单补 run_id/task_key 归属（6.8.1） |

### 3.3 差距清单

| # | 差距 | 行业基准 | 对应需求 |
|---|------|---------|---------|
| G1 | 编排 Agent 池硬编码 `_ORCH_AGENTS`（6 个固定）+ `_ORCH_MAX_TASKS=6` 封顶 | AutoGen/LangGraph 动态 Agent 池 | FR-AG-1e/2a |
| G2 | 无子 Agent 能力元数据（专长/输入输出契约/并发上限/版本），分派靠 LLM 自由选 | CrewAI 角色制、注册中心能力声明 | FR-AG-1c/2b |
| G3 | 负载仅规划建议、执行层无 per-agent 限流 | Celery 负载均衡 + 运行时限流 | FR-AG-2b |
| G4 | 子 Agent 共享同一 SQLite/llm_client，记忆无独立命名空间，附件全文共享 | 多智能体隔离运行 | FR-AG-3a |
| G5 | 子 Agent 结果文本透传 + 截断，无结构化摘要协议；产物无物化通道（dry_run 不落库） | MetaGPT 消息 Schema | FR-AG-3b/3c |
| G6 | 逻辑规则更新（意图路由/工具行为）需改代码，功能模块扩展无插件式通道 | 插件化/规则引擎 | FR-AG-1a |
| G7 | 编排无超时/自动重试/预算护栏/部分失败汇总语义 | Celery/Temporal 可靠性原语 | FR-AG-2c/3d |
| G8 | 编排路径下 L1/L2 子 Agent 的人工确认环节未定义 | 行业 HIL 体系 | FR-AG-3a 一致性 |

---

## 4. 设计目标

1. **注册中心化**：子 Agent 经注册中心动态供给，编排池 = 注册表筛选结果（FR-AG-1e/2a）。
2. **能力契约化**：分派、执行、校验均基于 Agent 能力元数据（FR-AG-2b/3c）。
3. **隔离命名空间化**：子 Agent 上下文按 `run_id:task_key` 隔离，交互只留结构化摘要（FR-AG-3a/3b）。
4. **物化可溯化**：子 Agent 交付物经独立命名空间物化，ref 指向物化产物，追溯线完整（FR-AG-3b/3d）。
5. **调度智能化 + 运行时限流**：专长匹配 × 负载 × 历史成功率打分，执行层 per-agent 信号量强制限流（FR-AG-1c/2b）。
6. **可靠性护栏**：子任务超时 / 自动重试 / run 级预算上限 / 部分失败汇总语义（FR-AG-2c/3d）。
7. **全生命周期可运营**：参数/规则/模块可配置，运营期零代码扩展（FR-AG-1a）。
8. **存量零破坏**：默认编排行为、历史编排产物、会话链路完全兼容（设计原则）。

---

## 5. 总体架构

```
┌────────────────────────────────────────────────────────────────┐
│ 前端（pg-ai 编排面板 + AI 设计工坊 Agent 管理）                    │
│  编排实时事件流（stage/subtask）· 负载/结果反馈卡片 · 能力配置表单  │
└──────────────────────────┬─────────────────────────────────────┘
                           │ REST / SSE
┌──────────────────────────▼─────────────────────────────────────┐
│ 主 Agent（pipeline.execute_stream）                             │
│  意图识别(FR-AG-1b) → 路由主 Agent → 复杂度判定                   │
├────────────────────────────────────────────────────────────────┤
│ 编排调度层                                                     │
│  动态规划（Planner：注册表候选池 + 专长×负载打分）→ TaskQueue      │
│  子 Agent 执行器（隔离命名空间 + dry_run + per-agent 限流/超时/重试）│
│  交付物物化（独立命名空间落库，ref 可溯）→ 汇总（结构化消费+反思）   │
├────────────────────────────────────────────────────────────────┤
│ 子 Agent 注册中心（FR-AG-2a）                                    │
│  agents 表（能力元数据）→ registry.get / discover（动态筛选）      │
├────────────────────────────────────────────────────────────────┤
│ 数据层                                                        │
│  agents(+capabilities/input_schema/output_schema/max_concurrency)│
│  agent_tasks(+load_snapshot/assign_score/summary_json/retry/token)│
│  subtask_artifacts(独立物化命名空间) · 编排轨迹(既有)             │
└────────────────────────────────────────────────────────────────┘
```

**核心复用（不重复造轮子）**：
- 任务状态机 / DAG 推进：现有 [TaskQueue](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/task_queue.py)（新增字段，状态机不变）。
- 子 Agent 执行器：现有 `sub.execute_stream(forced_intent=agent_id, dry_run=True)`（[pipeline.py#L2035](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L2035)），外层包限流/超时/重试。
- 汇总 + 反思：现有 `_summarize_plan` + RefineGate。
- 落库串行化：现状主线程统一落库，保留（见 9.2/12）。

---

## 6. 核心模块设计（重点）

### 6.1 子 Agent 注册中心（FR-AG-2a）

`agents` 表扩展能力元数据：

| 字段 | 说明 | 示例 |
|------|------|------|
| `capabilities` | 专长标签（**枚举词表**，见 6.3.1，防注入） | `["需求拆解","追溯","冲突检测"]` |
| `input_schema` | 任务输入契约（JSON Schema） | `{goal, context}` |
| `output_schema` | 交付物契约（JSON Schema） | `{summary, artifacts[]}` |
| `max_concurrency` | **运行时并发上限**（执行层信号量，见 6.7） | 2 |
| `version` | Agent 版本（语义化，见 6.5.3） | `v2.1.0` |
| `enabled` | 运行时可启停 | true |

注册表新增发现能力：`registry.discover(intent_domain) → 可用子 Agent 列表（含专长/负载/成功率/version）`。

**降级链**（评审 #15 补边界）：
```
discover(domain)
  ├─ 结果非空 → 候选池 = 结果
  ├─ 结果为空 → 回退旧固定池（_ORCH_AGENTS 交集过滤）
  │     └─ 仍为空 → 回退单 Agent 直行（主 Agent 意图域 Agent）
  └─ 打分全部并列 → 按注册顺序（id ASC）稳定选择
```

### 6.2 动态编排机制（FR-AG-1e）

- `_ORCH_AGENTS` / `_ORCH_MAX_TASKS` 硬编码 → 配置化：
  - 编排候选池 = `registry.discover(intent_domain)`（动态生成 Planner prompt 的 Agent 池，带专长描述）。
  - 任务数上限 = 配置项（默认 6，可调），按注册表活跃子 Agent 数与 run 级预算（6.7.3）动态放缩。
- 未配置能力元数据的 Agent 回退旧固定池 → **默认行为不变**（兼容性原则）。

### 6.3 智能任务规划与分派（FR-AG-1c/2b）

#### 6.3.1 专长匹配度（评审 #7：字符串重叠过脆弱）

匹配度 = 归一化词表命中兜底 + embedding 相似度兜底：

```
match(A, task) = max(
    norm_hit(A.capabilities, task) ,      # 归一化词表：同义词归一到同一标签
    embed_sim(A.capabilities, task)       # embedding 相似度（低于阈值 0.55 视为 0）
)
```

- **归一化词表**（P0 内置，可配置扩展）：`需求拆解=需求分析=需求条目化`、`架构设计=方案设计`、`影响评估=变更影响` 等。
- **embedding 兜底**：复用现有 `VectorEngine` embedding，对 capabilities 标签与任务文本算余弦相似度。
- **P0 明确算法**，避免「智能分派」名不副实；词表未命中且无 embedding 服务时回退池内均分。

#### 6.3.2 分派打分

```
Score(A) = 0.6 × match(A, task) + 0.3 × (1 − 当前负载(A)/max_concurrency(A)) + 0.1 × 有效成功率(A)
```

#### 6.3.3 冷启动（评审 #8）

- **无历史数据（新注册 Agent）**：有效成功率 = 池内中位数（exploration 兜底），保证新 Agent 能拿到任务，不违背 FR-AG-1a「零代码扩展」。
- 可选增强：新 Agent 前 N 次任务成功率按「中位数 × 0.8」加权（试用期降权但不归零）。

#### 6.3.4 负载快照时效性（评审 #9）

- **打分粒度**：每批 `ready` 任务执行前重算负载（推荐）——即按依赖批次刷新候选池负载后打分，load_snapshot 记录的是分派时刻快照。
- 明确局限：属规划期启发式，最终并发由执行层信号量兜底（6.7.1），打分不作为运行时约束。

### 6.4 严格上下文隔离（FR-AG-3a）

| 隔离维度 | 现状 | 调整后 |
|---------|------|--------|
| 会话/记忆 | 子任务 conversation_id=0 + dry_run | 独立上下文命名空间 `run_id:task_key`，记忆按命名空间读写 |
| 知识库检索范围 | 共享全库 | 按命名空间过滤（kb_scope 叠加 run 隔离） |
| 附件 | 全文共享注入 | **按需注入**：子任务 config 声明所需附件，白名单放行 |
| 运行实例 | 新 Pipeline 实例（已隔离） | 维持（+ 独立 llm 调用上下文参数） |
| 结果 | 完整文本回传 | **仅结构化摘要回传**（见 6.5） |
| **数据库物理边界** | 共享单文件 SQLite（WAL） | **承认物理共享**：数据/状态隔离为逻辑命名空间隔离；写库统一由主线程串行执行（现状保留），见 9.2/12 |

### 6.5 标准化摘要协议（FR-AG-3b/3c）

#### 6.5.1 协议 Schema

子 Agent → 主 Agent 统一返回结构化摘要（JSON，带 `schema_version`）：

```json
{
  "schema_version": 1,
  "task_key": "t2",
  "status": "full",                    // full | partial | failed（评审 #6）
  "summary": "一句话成果摘要",
  "artifacts": [
    {"kind": "sysml", "title": "BDD 视图", "ref": "subtask://12/t2/a0"}  // 物化命名空间引用，见 6.6
  ],
  "evidence": [{"source": "graph|vector|attachment", "ref": "REQ-SYS-010"}],
  "risks": [{"level": "high", "desc": "…", "advice": "…"}],
  "confidence": 0.85,
  "meta": {"latency_ms": 3200, "model": "…", "degraded": false, "token_count": 8420}
}
```

- `status`：`full`（全部交付物就绪）/ `partial`（部分交付物缺失，缺口在 `missing[]` 字段标注）/ `failed`（无可交付物）。汇总节点据此显式标注缺口，RefineGate 据此决策重派或降级（见 6.9）。
- `meta.token_count`：供 run 级预算护栏统计（见 6.7.3）。
- 协议带 `schema_version`；历史编排产物（`card_data.exec.subtasks` 旧文本格式）双格式兼容解析。

#### 6.5.2 通信安全

- 子 Agent 返回体在主 Agent 侧按 `output_schema` 校验，超长字段截断、非法结构丢弃并标 `partial`。
- 协议内容仅经进程内传递（无跨进程网络面），注入面收敛于能力元数据（见 6.10）。

#### 6.5.3 版本语义（评审 #14）

- **Agent version**（语义化 `MAJOR.MINOR.PATCH`）：MAJOR 变更 = 能力/契约不兼容，需重新声明 capabilities 与 output_schema（旧版本任务由注册表按版本归档）；MINOR/PATCH = 兼容增强，不影响分派。
- **协议 schema_version**：由 `services/subtask_protocol.py` 统一定义；Agent 声明支持的协议版本范围（`protocol: ">=1,<3"`），不匹配时注册表标记不可用。
- 两者关系：Agent version 是实例版本，schema_version 是通信契约版本；Agent 升级不强制改协议，协议升级须向后兼容或版本并存。

### 6.6 交付物物化策略（评审 #1 · dry_run 矛盾修复）

**背景**：子任务 `dry_run=True` 不写正式会话（messages/audit），若摘要协议 ref 指向 `msg:1024` 必然悬空。

**决策：方案 (a) 独立命名空间物化**（与 6.4 隔离设计对齐，MBSE 场景交付物可追溯）：

```
dry_run 子任务执行
  └─ 产物物化到 subtask_artifacts 表（独立命名空间）
       (run_id, task_key, artifact_idx, kind, title, content_json, created_at)
       ref = subtask://{run_id}/{task_key}/a{idx}
  └─ 子 Agent 写模型元素的处理（见 6.6.1 写操作受控）：
       编排路径子任务保持 dry_run —— 只产草稿产物，不直接写 entities/relations 正式表；
       写工具请求统一暂存到 subtask_artifacts（kind=write_request），
       汇总后批量挂接人工确认队列（见 6.8.1），确认后才写入正式模型。
```

#### 6.6.1 写操作受控（v1.1 复审硬伤 1 修复：dry_run 不约束工具写操作）

**现状缺口**：`_exec_tool_call` 不感知 dry_run，L0/L1 Agent 持有的非 destructive 写工具（entity_create/relation_create/entity_update 等）在编排子任务里会**真实写库**——6.6 的"不写正式表"保证若无执行层拦截则只是声明。

**决策：编排路径强制全部写工具入确认队列（复用 `HILService.is_write_tool` 判定）**：

```
_exec_tool_call 改造（编排路径标志 _orch_subtask=True）：
  if _orch_subtask and HILService.is_write_tool(name):
      # 不即时执行、不即时入队 —— 暂存待确认写请求到 subtask_artifacts
      artifact_materializer.store_write_request(run_id, task_key, name, arguments)
      return {ok: True, result: "写操作「X」已暂存，汇总后统一人工确认"}
  if HILService.is_write_tool(name) and hil_level == L2:   # 现状分支保留（非编排路径）
      → 即时入队（L1253-1266）
  if self._tool_side_effect(name) == "destructive":         # 现状分支保留（含编排路径）
      → 强制确认（L1268-1283）
```

- **拦截点**：`_exec_tool_call` 入口（单点拦截，覆盖内置/MCP/HTTP 全部工具路径）。
- **判定复用**：`HILService.is_write_tool`（[hil_service.py#L15](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/hil_service.py#L15) `WRITE_TOOLS` 白名单），新增写工具在此登记即可，无需每处维护。
- **与 6.8 衔接**：暂存的写请求（subtask_artifacts kind=write_request）在汇总后批量挂确认队列（6.8.1），一次确认覆盖多个交付物，天然规避并发子 Agent 写同一模型冲突。

- **物化时机**：子任务完成时（主线程统一落库段内，保持 SQLite 串行写）。
- **ref 解析**：主 Agent 汇总时可按需将 `subtask://` 引用二次物化为正式会话产物（写正式 messages/artifacts），或保留独立命名空间引用（历史可溯）。
- **保留策略**（v1.1 复审次要问题 ④）：subtask_artifacts 保留**最近 N 个 run（默认 20）**，超出按 run_id 清理（汇总完成后物化产物已二次物化到正式会话，独立命名空间仅为追溯留痕，可安全清理）。
- **MBSE 场景建议**：子 Agent 产出 SysML 草稿/需求条目 → 物化为 subtask_artifacts → 汇总后进入 L2 人工确认队列 → 确认后才写入正式模型。交付物追溯线完整：`子任务 → subtask_artifacts → 汇总产物 → 正式模型`。

### 6.7 执行层并发控制与可靠性（评审 #2/#3/#4/#5）

#### 6.7.1 per-agent 并发限流（评审 #2）

现状 `ThreadPoolExecutor(max_workers=min(len(ready), 3))` 全局硬编码，单个 Agent 并发不被约束。调整为：

```
全局 worker 池：max_workers = min(配置项(默认3), len(ready))     # 配置化
per-agent 信号量：Semaphore(agent.max_concurrency) per agent_id
任务执行前：agent_sem.acquire() → 执行 → 完成后 release()
```

- **运行时保证**：同一 Agent 并发任务数 ≤ max_concurrency（打分只是规划建议，信号量是强制约束）。
- 全局 worker 数与 per-agent 上限均可配置；热点 Agent 不再被并发拉起。

#### 6.7.2 子任务超时与自动重试（评审 #3/#4）

- **超时**：每子任务 wall-clock 超时（默认 120s，可配置）。实现：`concurrent.futures` Future + `wait(timeout)`，超时未完成 → 任务置 `failed`（`error="timeout"`），不阻塞后续批次。
- **超时线程语义**（v1.1 复审次要问题 ②）：`ThreadPoolExecutor` 的 future 超时后线程杀不掉，子 pipeline 会在后台继续跑，`finally: agent_sem.release()` 会在线程自然结束后才执行——**接受该语义：超时任务仍占 per-agent 信号量额度直到其自然结束**（简单安全，避免超限释放导致并发数突破上限；文档明确不承诺强杀）。
- **自动重试**：编排路径默认 1 次自动重试 + 指数退避（1s/2s）。瞬时失败（LLM 超时/工具偶发）直接重试；重试后仍失败 → `failed`，转 RefineGate 或人工接管（复用 `TaskQueue.block/retry`，`retry` 现为人工 P2，编排内自动重试为新增原语）。
- **guard 语义**：`guard < 200` 保留为批次推进护栏，超时由独立机制负责，两者不混淆。

#### 6.7.3 run 级预算护栏（评审 #5）

| 护栏 | 默认值 | 触发行为 |
|------|--------|---------|
| 子任务数上限 | 6（配置化） | 规划阶段截断，超出提示用户 |
| run 级 token 预算 | 200K（配置化） | 汇总 `meta.token_count` 累计，超预算 → **停止派发新子任务**，用已完成子任务的摘要直接进入 `_summarize_plan` 汇总（partial 语义，见 6.5.1 status），并提示用户 |
| 单子任务工具轮数 | 10 | 超限中断该子任务（partial） |

- **降级目标态**（v1.1 复审次要问题 ③）：超预算**不退回单 Agent 直行**（编排中途退回语义不通）——已执行部分摘要仍有效，直接汇总产出 partial 报告；RefineGate 依据 status=partial 决策补做或降级。
- **token 统计来源**（v1.1 复审次要问题 ①）：`llm/` 层补 token usage 记账——`llm_client.chat` 响应体含 usage（prompt_tokens/completion_tokens）时写入 `llm_usage_stats`（现有表）并回填 `_log_tool_call`/子任务 `meta.token_count`；Mock 或无 usage 字段时按 `len(text)/2` 估算兜底（见 7.1 文件清单）。

### 6.8 HIL 交互语义（评审 #11 · v1.1 复审硬伤 2 对齐现状）

现状 `hil_level`（L0 直出 / L1 预览 / L2 确认）。编排路径（dry_run）下定义：

| 子 Agent HIL | 编排路径行为 | 说明 |
|-------------|-------------|------|
| L0 | 正常执行，产物直接物化 | 无人工环节 |
| L1 | 产物物化为草稿 + 预览标记，**不阻塞编排** | 编排为自动预演，草稿汇入汇总报告供主 Agent 展示 |
| L2 | 写操作暂存（subtask_artifacts kind=write_request），**不阻塞编排** | 见 6.8.1：汇总后统一入确认队列，一次确认覆盖多个子 Agent 交付物 |

- **原则**：编排路径 = 自动预演（不阻塞），人工确认集中在**汇总级**（一次确认覆盖多个子 Agent 交付物），不打断编排流程。
- **一致性**：与现有「人在回路（BR-2）」体系一致——子任务级不强行挂确认（否则编排会被人工阻塞），汇总级保留强制确认（写正式模型必须人工批准）。

#### 6.8.1 确认时机与归属（v1.1 复审硬伤 2 修复）

**现状**：L2 写工具在 `_exec_tool_call` 内**即时入队**（[L1253-1266](file:///c:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L1253)），确认单 `conversation_id=conv_ctx or 0`（dry_run 子任务为 0，归属模糊）；`hil_confirmations` 已有 run_id 列但调用方未传，无 task_key 列。

**决策：编排路径改「汇总后统一入队」**（方案描述的体验——一次确认覆盖多交付物）：

```
编排路径（_orch_subtask=True）：
  L2/写工具 → 6.6.1 暂存到 subtask_artifacts(kind=write_request, 含 run_id/task_key)
  主 Agent 汇总阶段 → artifact_materializer.batch_queue_confirmations(run_id)
     遍历该 run 全部 write_request → 逐条 queue_confirmation(
         agent_id, action, payload, run_id=run_id, task_key=task_key)   # 补归属
  → 确认单按 run 聚合展示（前端确认队列按 run_id 分组）
```

- **归属字段**：`hil_confirmations` 补 `task_key` 列（幂等迁移）；编排路径 `queue_confirmation` 显式传 `run_id`（现状列已存在但未传）。
- **前端聚合**：确认队列面板按 run_id/task_key 分组展示（可展开查看该 run 的全部待确认写操作）。
- **部分拒绝**（v1.1 复审次要问题 ⑤）：确认队列支持**按 artifact 勾选**——勾选项 approve、未勾选 reject；至少 P1 落地勾选，P0 记录该限制（全批确认）为已知限制。

- **一致性**：非编排路径（会话内单 Agent 直行）保持现状即时入队语义，零改动。

### 6.9 意图识别增强（FR-AG-1b · 评审 #16 补设计）

现状已具备规则+LLM+DST+归一化+低置信度澄清。编排场景下增强：

1. **多意图分解**：一条输入含多个子意图（如「先做需求分析，再出方案设计」）→ 识别为多意图序列，直接进入动态编排（与 `_needs_orchestration` 规则信号衔接）。
2. **潜在需求挖掘**：槽位缺失（无变更源/无约束条件）时，在澄清提示中给出**建议补充项**（复用现有 needs_clarification 结构扩展 `suggested_slots`）。
3. **意图置信度分级路由**：高置信 → 直行；中置信 → 澄清（现有）；低置信且匹配编排信号 → 编排池按意图域候选渲染候选列表。

改动面：`agent/intent.py`（多意图识别 + suggested_slots）+ `pipeline.py`（意图序列驱动编排）。

### 6.10 安全与治理（评审 #10/#12）

| 项 | 措施 |
|----|------|
| **能力元数据注入清洗** | capabilities 限定**枚举词表**（DB 层校验，非任意文本）；description 注入 Planner prompt 前做长度截断（≤200 字符）+ 字符白名单清洗（去控制字符/脚本标记），防脏数据劫持规划器 |
| **SQLite 并发** | 保留主线程统一落库（worker 只收集事件不写库）；风险表登记写锁竞争，监控 `database is locked` 频次，超阈降级编排并发度 |
| **元数据审计** | Agent 能力字段变更写入审计日志（复用 audit） |

---

## 7. 后端实现设计

### 7.1 新增/修改文件

| 文件 | 职责 |
|------|------|
| `agent/registry.py`（改） | 能力元数据加载 + `discover()` 动态筛选 + 负载/成功率统计 + 版本/协议校验 |
| `agent/pipeline.py`（改） | 编排池动态化（6.2）、分派打分（6.3）、隔离命名空间（6.4）、摘要协议收发（6.5）、**执行层信号量/超时/重试/预算（6.7）**、**`_exec_tool_call` 编排写操作拦截（6.6.1，`_orch_subtask` 标志：写工具暂存不即时执行/不即时入队）**、HIL 汇总级确认（6.8.1） |
| `llm/`（base/registry）（改） | **token usage 记账**（6.7.3 次要问题 ①）：chat 响应 usage 写入 llm_usage_stats 并回填 meta.token_count；Mock 无 usage 按 `len(text)/2` 估算 |
| `hil_service.py`（改） | `queue_confirmation` 支持 `task_key` 参数（6.8.1 归属）；`WRITE_TOOLS` 登记新增写工具 |
| `agent/intent.py`（改） | 多意图分解 + suggested_slots（6.9） |
| `workflows/planner.py`（改） | 候选池注入 + 打分排序提示词 |
| `task_queue.py`（改） | 负载/打分/摘要/重试字段读写 + 负载查询 + 自动重试原语 |
| `services/subtask_protocol.py`（新） | 摘要协议 Schema 校验 / 序列化 / 旧格式兼容 / 版本范围 |
| `services/artifact_materializer.py`（新） | 交付物物化（subtask_artifacts 写读 + 二次物化到正式会话 + **`store_write_request`/`batch_queue_confirmations`（6.6.1/6.8.1）** + **保留策略清理（最近 N run）**） |
| `routers/studio.py`（改） | Agent 管理接口扩展能力字段 + 枚举词表校验 |
| `database/schema.py`（改） | 幂等列迁移 + subtask_artifacts 表 + hil_confirmations.task_key 列 |
| 前端（index.html + 确认队列面板） | Agent 能力表单、编排负载/结果面板、**确认队列按 run_id/task_key 分组 + 按 artifact 勾选（部分拒绝）** |

### 7.2 数据模型（FR-AG-1e/2a/2b/3b）

```sql
-- agents 表扩展（幂等迁移）
ALTER TABLE agents ADD COLUMN capabilities TEXT DEFAULT '[]';
ALTER TABLE agents ADD COLUMN input_schema TEXT DEFAULT '{}';
ALTER TABLE agents ADD COLUMN output_schema TEXT DEFAULT '{}';
ALTER TABLE agents ADD COLUMN max_concurrency INTEGER DEFAULT 2;
ALTER TABLE agents ADD COLUMN version TEXT DEFAULT 'v1.0.0';
ALTER TABLE agents ADD COLUMN protocol_range TEXT DEFAULT '>=1,<3';

-- agent_tasks 表扩展
ALTER TABLE agent_tasks ADD COLUMN load_snapshot TEXT DEFAULT '{}';  -- 分派时各 Agent 负载快照
ALTER TABLE agent_tasks ADD COLUMN assign_score REAL DEFAULT 0;      -- 分派打分
ALTER TABLE agent_tasks ADD COLUMN summary_json TEXT DEFAULT '{}';   -- 结构化摘要协议结果（含 status）
ALTER TABLE agent_tasks ADD COLUMN retry_count INTEGER DEFAULT 0;    -- 编排内自动重试次数
ALTER TABLE agent_tasks ADD COLUMN token_count INTEGER DEFAULT 0;    -- 子任务 token 统计（预算护栏）

-- 交付物物化命名空间（评审 #1）
CREATE TABLE IF NOT EXISTS subtask_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,              -- = conversation_id（run 命名空间）
  task_key TEXT NOT NULL,               -- 子任务 key
  artifact_idx INTEGER DEFAULT 0,       -- 物化序号（ref=subtask://{run_id}/{task_key}/a{idx}）
  kind TEXT DEFAULT '',                 -- sysml | requirement | report | doc | ...
  title TEXT DEFAULT '',
  content_json TEXT DEFAULT '{}',
  materialized INTEGER DEFAULT 0,       -- 是否已二次物化到正式会话
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sa_run ON subtask_artifacts(run_id, task_key);

-- 编排写请求与 L2 确认单归属（v1.1 复审硬伤 2：hil_confirmations 已有 run_id 列，补 task_key）
ALTER TABLE hil_confirmations ADD COLUMN task_key TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_hc_run ON hil_confirmations(run_id);
```

> 注：subtask_artifacts.kind 取值含 `sysml|requirement|report|doc`（物化产物）与 `write_request`（编排路径暂存的写工具请求，payload=工具名+参数，汇总后经 batch_queue_confirmations 挂确认队列，见 6.6.1/6.8.1）。

### 7.3 关键算法

```
discover(domain):   # 见 6.1 降级链
  enabled 子 Agent → capabilities/domain 过滤 → 附加负载/成功率/版本 → 返回候选

assign(task, candidates):   # 每批 ready 前重算（6.3.4）
  load = 实时 running+ready 数（按 agent_id）
  for A in candidates: Score(A) = 0.6·match + 0.3·(1−load/max_concurrency) + 0.1·eff_success
  排序 → 写 task.assign_score → Planner 按序委派

execute_subtask(tk):        # 6.7 执行层
  agent_sem[agent_id].acquire()          # per-agent 限流
  future = pool.submit(sub.execute_stream, ..., dry_run=True, orch_subtask=True)  # 6.6.1 写拦截
  try: wait(future, timeout=120s)        # wall-clock 超时（超时任务仍占额度直至自然结束）
  except TimeoutError: → retry（1 次，退避 1s）→ 仍超时 → failed(timeout)
  finally: agent_sem[agent_id].release()
  产物 → artifact_materializer.store(run_id, task_key, artifacts)      # 6.6 物化
  写请求 → artifact_materializer.store_write_request(...)              # 6.6.1（_exec_tool_call 内）
  摘要 → subtask_protocol.validate(summary_json) → task.summary_json   # 6.5
  token 累计 → run 级预算检查（超预算停止派新任务 → partial 汇总）      # 6.7.3

summarize(run_id):          # 汇总阶段（6.8.1）
  batch_queue_confirmations(run_id)   # 该 run 全部 write_request → 确认队列（补 run_id/task_key）
  → 汇总消费结构化摘要（status=partial 时显式标注缺口）
```

### 7.4 与现有管线的衔接

- 单 Agent 直行路径**零改动**（隔离/摘要/物化仅作用于编排子任务）。
- 编排骨架沿用 `_stream_orchestrated_flow`，替换「池来源、分派、子任务收发（摘要+物化）、执行包装（限流/超时/重试/预算）、HIL 汇总级确认」五处。
- 存量编排产物（历史 subtasks 文本）旧格式解析兜底，不迁移历史数据。

---

## 8. 前端设计（增量）

1. **AI 设计工坊 Agent 管理**：能力表单（专长**枚举下拉**、输入/输出 Schema、并发上限、版本、协议范围、启停）。
2. **编排面板**：子 Agent 卡片展示专长标签 + 负载条（当前/上限）+ 任务结果摘要（summary/artifacts/risks/status）+ 重试/超时标记。
3. **L2 确认衔接**：汇总后「确认并写入模型」按钮挂接现有确认队列（一次确认覆盖多个子 Agent 交付物）。
4. 复用既有组件与风格（rc-chip / tag / 状态徽标），与现有编排 subtask 展示对齐。

---

## 9. 对现有系统的影响与冲击

### 9.1 代码影响面

| 文件 | 改动 | 冲击等级 | 说明 |
|------|------|---------|------|
| `agent/pipeline.py` | 编排池动态化、分派打分、隔离命名空间、摘要协议、**执行包装（限流/超时/重试/预算）**、HIL 汇总级确认 | **高** | `_stream_orchestrated_flow`/`execute_stream` 编排子任务路径全链路触碰；单 Agent 直行零改动 |
| `agent/registry.py` | 能力元数据 + discover + 版本/协议校验 | 中 | `list`/`get` 签名扩展，兼容现有调用 |
| `agent/intent.py` | 多意图分解 + suggested_slots | 低 | 增量能力，现有判定不变 |
| `workflows/planner.py` | 候选池/打分注入 | 中 | 仅编排路径生效 |
| `task_queue.py` | 负载/打分/摘要/重试字段 | 低 | 新增列与原语，状态机不变 |
| `database/schema.py` | agents/agent_tasks 加列 + subtask_artifacts 表 | 低 | 幂等迁移，存量数据保留 |
| `routers/studio.py` + 前端 | 能力表单、负载/结果面板、L2 确认衔接 | 中 | 与既有 Agent 管理 UI 对齐 |

### 9.2 行为/兼容性冲击

| 冲击点 | 风险 | 规避 |
|--------|------|------|
| 动态池放开后 LLM 选中劣质/无关 Agent | 高 | 意图域过滤 + 打分排序 + 执行层限流兜底 |
| 隔离改动影响依赖附件的编排 | 中 | 6.4「按需注入」白名单，默认行为不丢附件 |
| 摘要协议改版影响历史编排产物还原 | 中 | `card_data.exec.subtasks` 双格式兼容解析 |
| **dry_run 产物追溯线（评审 #1）** | 高 | 独立物化命名空间（subtask_artifacts）+ ref 可解析，不指向正式 msg |
| **编排写工具行为变化（v1.1 复审硬伤 1）** | 高 | 编排路径 L0/L1 非 destructive 写工具从"真实执行"改为"暂存→汇总后确认"——属**安全收紧**，单 Agent 直行路径不受影响；编排语义从"自动写模型"变为"草稿+确认"，需在编排面板文案明示 |
| **L2 确认时机变化（v1.1 复审硬伤 2）** | 中 | 编排路径从"子任务即时入队"改"汇总后统一入队"；非编排路径保持即时入队零改动；确认单补 run_id/task_key 归属，前端按 run 聚合 |
| **执行层限流改动（评审 #2）** | 中 | 信号量只收紧不放松，全局并发默认 3 不变 |
| HIL 交互语义变化（评审 #11） | 中 | 子任务不阻塞、汇总级确认，与现有 BR-2 确认队列一致 |
| **SQLite 写锁竞争** | 中 | 保留主线程统一落库（worker 不写库）；监控 locked 频次，超阈降并发（登记 12） |
| 存量会话/沉淀流程（saved_flow_id） | 低 | 流程模板按 agent_id 引用，注册表补齐 |

### 9.3 数据面

- `agents` 新增 6 列、`agent_tasks` 新增 4 列、新建 `subtask_artifacts` 表，全部幂等迁移，无数据清洗/回填脚本。
- 存量 `agent_tasks.result`（旧文本）与 `summary_json` 并存，读取优先新字段、缺失回退旧字段。

### 9.4 回归验证面

- 后端：意图路由、`_needs_orchestration` 判定、单 Agent 直行、编排降级、TaskQueue 状态机、RefineGate 反思、工作流沉淀——全量回归。
- 新增用例：discover 筛选与降级链、分派打分（含冷启动）、隔离命名空间、**摘要协议解析（含旧格式）**、**物化 ref 解析闭环**、**per-agent 限流（同 Agent 并发 ≤ max_concurrency）**、超时/重试、预算护栏、HIL 汇总级确认。
- 前端：Agent 管理 CRUD（含能力字段）、编排 subtask 展示、L2 确认衔接、编排沉淀流程。

---

## 10. 分期落地

| 阶段 | 内容 | 覆盖需求 |
|------|------|---------|
| **开工前必须补** | ① 交付物物化策略（6.6，含 6.6.1 写操作受控——dry_run 矛盾 + 写穿透修复）② HIL 交互语义与确认归属（6.8/6.8.1） | FR-AG-3b/3d、FR-AG-3a 一致性 |
| **P0（核心闭环）** | ③ 注册中心能力元数据 + discover + 降级链 ④ 编排池动态化（回退兼容）⑤ 分派打分（含匹配算法 6.3.1/冷启动 6.3.3/分批重算 6.3.4）⑥ **per-agent 并发限流（6.7.1）** ⑦ run 级预算护栏 + token usage 记账（6.7.3） | FR-AG-1e/2a/2b/1c |
| **P1（隔离+协议+可靠性）** | ⑧ 上下文隔离命名空间 + 附件按需注入 ⑨ 结构化摘要协议（含 status/旧格式兼容）⑩ 汇总消费结构化摘要 ⑪ 子任务超时 + 自动重试（6.7.2）⑫ 部分失败汇总语义 ⑬ **确认队列按 run 聚合 + 按 artifact 勾选（部分拒绝，6.8.1）** | FR-AG-3a/3b/3c/1d/2c |
| **P2（运营+治理）** | ⑭ 逻辑规则配置化（意图路由/工具行为规则表）⑮ 负载/结果反馈面板 ⑯ 功能模块插件化通道 ⑰ 意图识别增强（多意图/suggested_slots，6.9）⑱ 注入清洗 + 元数据审计（6.10） | FR-AG-1a/1b/2c |

---

## 11. 测试验证方案

- **后端**：`tools/verify_agent_arch.py`（临时 DB）覆盖——注册表 discover 意图域过滤与**降级链**（空结果→旧池→单 Agent）、能力元数据 CRUD 与**枚举词表校验**、分派打分（专长匹配单调性、**冷启动中位数兜底**、**分批重算**）、**per-agent 限流（同 Agent 并发数 ≤ max_concurrency）**、隔离命名空间读写互不可见、摘要协议 Schema 校验与**旧格式回退**、**物化闭环（subtask:// ref 可解析、二次物化）**、**超时/自动重试/预算护栏**（token 超限 → partial 汇总而非降级单 Agent）、部分失败 status 汇总、HIL 汇总级确认、动态池下编排产物正常落库、兼容回归（未配置元数据 → 旧固定池行为一致）。
- **新增用例（v1.1 复审硬伤 1/2）**：
  - **写操作受控**：编排子任务（orch_subtask=True）调用 L0/L1 非 destructive 写工具 → 不真实写 entities/relations、subtask_artifacts 出现 write_request 记录、返回"已暂存"文案；非编排路径调用同工具 → 正常执行（行为不变）。
  - **确认单归属**：编排路径汇总后 batch_queue_confirmations → hil_confirmations 该 run 确认单 run_id/task_key 非空、按 run 可聚合查询；非编排路径即时入队确认单 run_id 保持 0（现状语义不变）。
  - **token 记账**：chat 响应含 usage → llm_usage_stats 落库 + meta.token_count 回填；Mock 估算兜底。
- **前端（playwright，按用户规则完成页面交互验证）**：
  1. Agent 管理：新建/编辑含能力字段（枚举下拉）→ 保存 → 列表展示。
  2. 编排：触发多 Agent 任务 → 子 Agent 卡片显示专长/负载/结果摘要/status → 汇总输出完整 → L2 确认写入模型。
  3. 隔离：两个子任务上下文互不污染（检索结果不一致可验证）。
  4. 兼容回归：既有会话重放编排历史消息（旧 subtasks 格式）正常还原。
- **性能/并发验收（评审 #13）**：`tools/bench_orchestration.py` 压测——10+ 子 Agent 并发编排：**P95 编排时延 ≤ 90s（默认超时 120s 内）**、**`database is locked` 次数 = 0（主线程串行落库）**、per-agent 并发数严格 ≤ max_concurrency（采样断言）。纳入 CI 冒烟（小规模 5 子 Agent 版）。
- **演示数据**：注册表预置 2 个带能力元数据的自定义子 Agent（如「热控分析Agent」「链路预算Agent」），验证动态池突破 6 个固定上限。

---

## 12. 风险与规避

| 风险 | 规避 |
|------|------|
| 动态池导致劣质 Agent 被选中 | 意图域过滤 + 专长打分强制排序 + 执行层限流 + 未配置回退旧池 |
| **交付物 ref 悬空（dry_run 不落库）** | 独立物化命名空间（subtask_artifacts）+ ref 走 `subtask://` 协议（评审 #1） |
| **编排写工具真实写库（dry_run 不拦截，v1.1 复审硬伤 1）** | `_exec_tool_call` 编排路径拦截全部写工具（`HILService.is_write_tool`）→ 暂存 write_request → 汇总后批量挂确认队列（6.6.1/6.8.1） |
| **L2 确认单归属模糊（v1.1 复审硬伤 2）** | 编排路径统一汇总后入队 + 确认单补 run_id/task_key（hil_confirmations 加列 + 调用方传参），前端按 run 聚合（6.8.1） |
| **同 Agent 并发过载（打分非限流）** | per-agent 信号量运行时强制限流（评审 #2） |
| **子任务卡死拖住编排** | wall-clock 超时（默认 120s）→ failed，不阻塞后续批次（评审 #3） |
| **瞬时 LLM 失败无兜底** | 编排内 1 次自动重试 + 指数退避，重复失败转 RefineGate/人工（评审 #4） |
| **动态池 token 消耗失控** | run 级任务数/token 预算护栏，超预算停止派新任务 → partial 汇总并提示（6.7.3） |
| **部分失败汇总语义不清** | 摘要协议 status: full/partial/failed，汇总显式标注缺口，RefineGate 决策重派/降级（评审 #6） |
| **能力元数据注入 Planner 劫持** | 枚举词表 + 长度/字符白名单清洗 + 元数据审计（评审 #10） |
| **SQLite 写锁竞争（WAL 单文件）** | 保留主线程统一落库（worker 不写库）；监控 locked 频次，超阈降并发（评审 #12） |
| 冷启动新 Agent 拿不到任务 | 成功率取池内中位数（exploration 兜底），试用期加权（评审 #8） |
| 负载快照时效性 | 每批 ready 前重算 + 文档明确为规划期启发式，运行时由信号量兜底（评审 #9） |
| 隔离改造破坏附件/记忆链路 | 按需注入白名单 + 回归用例覆盖附件编排 |
| 摘要协议升级破坏历史产物 | 双格式兼容解析 + schema_version 版本并存 |
| 与既有编排/沉淀流程冲突 | 全部改动限定编排子任务路径，单 Agent 直行零改动 |
| 配置化规则引擎范围失控 | 规则表仅覆盖意图路由/工具行为两类，P2 再评估插件化 |
