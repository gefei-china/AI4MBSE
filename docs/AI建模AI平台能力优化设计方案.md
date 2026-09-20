# AI 建模 · AI 平台能力优化设计方案

> 版本：v1.0 · 2026-08-13
> 来源：文章《流程编排 · 工作流 · Agent》（2026）三层概念模型 + 现有代码审计（`agent/pipeline.py` / `workflows/engine.py` / `workflows/planner.py` / `agent/intent.py` / `llm/__init__.py` / `memory_service.py`）
> 对象：mbse_system「AI 建模」页（pg-ai）——AI 建模如何利用 AI 平台能力的整体链路
> 原则：对照文章「工作流 → 流程编排 → Agent」三层递进架构，对系统现状做差距分析，产出**可落地**的优化清单（P0/P1/P2），每项含现状、详细设计、边界与降级、验收口径

---

## 1. 文章要点速览

文章核心：工作流 / 流程编排 / Agent 不是互斥概念，而是**层层递进、互为支撑**的三层：

| 层 | 回答的问题 | 核心特征 | 适用场景 |
|---|---|---|---|
| 工作流（Workflow） | 具体怎么做、按什么步骤 | 预定义、规则驱动、可重复执行 | 确定性高、流程明确 |
| 流程编排（Orchestration） | 谁和谁配合、怎么配合 | 跨系统调度、异常处理、监控可视、资源调度 | 跨系统/跨部门、端到端 |
| Agent（智能体） | 做什么、为什么做 | 感知+决策+行动、工具使用、**记忆系统**、规划能力 | 不确定/开放场景 |

**关键工程要点**（文章隐含的企业级要求）：
- 上下文边界（只传必需事实）
- 权限边界（Worker ⊆ Supervisor）
- 调度状态机（含 timeout / 异常 / 补偿）
- 终止与预算（轮数 / token / 费用 / 时间）
- 结果可信度（schema 校验 / 评审 / 规则）
- 可观测性（谁创建、收到什么、调用什么、结果怎么传、哪里失败）

**实践建议**：先标准化（工作流固化确定性业务）→ 再平台化（编排整合跨流程）→ 最后智能化（Agent 处理最后一公里智能决策）。

---

## 2. 系统现状分析：AI 建模如何使用 AI 平台能力

### 2.1 总体架构（三层已齐全）

```
┌─ Agent 层 ─────────────────────────────────────────────────────┐
│  AgentRegistry（8 个 DB 可配置 Agent：需求分析/需求质量/方案设计/  │
│  变更影响/预评审/报告生成/知识问答/通用问答）                      │
│  AgentPipeline.execute_stream()：意图识别→检索→Prompt→LLM→工具→  │
│  富卡片 + SysML 视图投影 + 产物/报告归档                          │
├─ 编排层 ────────────────────────────────────────────────────────┤
│  FlowPlannerMixin.run_planner_plan()（Planner-Executor）：      │
│  复杂度判定(_needs_orchestration) → LLM 生成子任务 DAG →         │
│  TaskQueue 并行执行(≤3) → LLM 总结(_summarize_plan)              │
├─ 工作流层 ──────────────────────────────────────────────────────┤
│  FlowExecutor DAG 引擎：17 种节点(llm/tool/agent/skill/mcp/if/   │
│  orchestrator/reflection/code/http/iteration/knowledge/pubsub/  │
│  debate/webhook) + 条件分支 + 循环 + 分层并行 + 检查点/恢复        │
│  前端 st-flow 可视化画布 + AI 生成（FlowCopilot）                │
└─ LLM 能力层 ────────────────────────────────────────────────────┘
   LLMClient → LLMRouter(标签+预算路由) → ProviderRegistry 插件化
   （openai_compat / mock）+ RAG 双引擎 + 16+ 工具 + Skill 三级匹配 + MCP + Mock 兜底
```

### 2.2 单轮建模主链路（`agent/pipeline.py::execute_stream`）

1. **意图识别**：三层混合路由（规则快筛 → 语义 bigram → LLM 精排）+ 置信度三级决策（高置信直行 / 弱置信澄清提示 / 低置信降级）+ 会话 DST 意图保持 + 意图缓存（`intent.py::IntentRouter.detect`）
2. **自动编排判定**：`_needs_orchestration`（规则信号「先…再…/并/分别」+ LLM 复杂度判定）→ 触发 `run_planner_plan`
3. **知识检索**：GraphRAG 双引擎（图谱 graph + 向量 vector，`QueryRouter` 路由 graph/vector/mixed），`#标签` / Agent kb_required 配置 / 类型化报告素材 触发
4. **Prompt 组装**：角色化 System Prompt（Agent 角色块 + HIL 分级 + 工具清单 + Skill 渐进披露 + 提示词模板 + 本体提示 + 任务拆解槽位 + 用户上下文 + 附件 + 检索上下文 + 报告模板 + 三层会话历史 + 上下文预算裁剪）
5. **LLM 调用**：`llm_client.chat` → 显式 provider_id > Agent 绑定 > LLMRouter（能力标签 + 优先级 + Token 预算）> 默认模型；无 key / 失败静默降级 Mock
6. **工具循环**：function calling 最多 3 轮 ReAct；JIT 工具选择（候选 >8 语义预筛 top6）；写工具 HIL L2 人工确认
7. **输出**：SysML v2 代码 → 视图投影（Cytoscape）+ 富卡片 + `_archive_artifacts` 产物/报告归档

### 2.3 AI 平台能力的利用点清单

| 能力 | 现状利用 | 位置 |
|---|---|---|
| 多模型插件化 | OpenAI-compatible 统一协议，DB `llm_providers` 配置即新增模型 | `llm/providers/openai_compat.py` |
| 多角色 LLM 调用 | 意图分类 / 复杂度判定 / 计划生成 / 任务执行 / 总结 / 记忆评估 均走 `_intent` 区分 | `intent.py`、`planner.py`、`pipeline.py` |
| Function Calling | graph_retrieve / entity_create / impact_analyze / validate / file_* / report_export | `workflows/tools.py`、`file_tools.py` |
| Skill 渐进披露 | triggers → bigram → LLM 三级匹配，命中注入元数据+正文摘要 | `pipeline.py::_build_skill_prompt` |
| RAG 双引擎 | 图谱 + 向量双召回，QueryRouter 消费观测 | `knowledge_engine.py` |
| 结构化输出 | Planner JSON / DelegateResult / 报告分节模板 | `planner.py`、`report_generator.py` |
| 智能路由 | 能力标签 + 优先级 + Token 预算 | `llm/__init__.py::LLMRouter` |
| Mock 兜底 | 无 key / API 失败静默降级，保证可回归 | `llm/providers/mock.py` |

---

## 3. 差距分析（对照文章）

| 文章要点 | mbse_system 现状 | 差距等级 |
|---|---|---|
| 三层架构齐备 | ✅ 工作流 DAG + 编排 Planner-Executor + Agent 管线并存 | 形态齐全 |
| 编排结果可复用（平台化） | ❌ 自动编排 plan 执行完即丢弃，**不沉淀为可复用工作流**；每次重新 LLM 规划 | 🔴 P0 |
| 编排端到端监控可视 | ❌ Planner-Executor 无节点级执行轨迹图（仅有卡片 plan 列表） | 🔴 P0 |
| Agent 记忆系统（文章核心构成） | ❌ `MemoryService` 仅工作流 memory 节点调用，**Agent 主管线未接入** | 🔴 P0 |
| 任务难度分级模型路由 | ⚠️ LLMRouter 只按标签/预算，无「轻任务小模型 / 重任务大模型」维度 | 🟠 P1 |
| 编排异常补偿 | ⚠️ 子任务失败仅 blocked 停止，无重试/降级/补偿策略 | 🟠 P1 |
| Agent 自主循环（行动→评估→改进） | ⚠️ 单轮管线；reflection 节点仅工作流层有，会话层无反思闭环 | 🟠 P1 |
| 工作流 ↔ 编排融合 | ⚠️ 两体系独立：编排不产工作流、工作流不消费编排产物 | 🟡 P2 |
| 编排级共享黑板 | ⚠️ blackboard 仅工作流层；planner 子任务仅 context 传参 | 🟡 P2 |
| 委派级 Trace / 成本核算 | ⚠️ 有 tool_call_logs / flow_runs，无「编排委派链」视图 | 🟡 P2 |

---

## 4. P0 · 近期（核心收益，工作量可控）

### P0-1 自动编排结果沉淀为可复用工作流

**现状**：`planner.py::_planner_core` 中 LLM 生成的 `plan`（key/title/agent/deps/context/expected_output/tools/skills）直接进 `TaskQueue` 执行，执行完只保留结果，**plan 本身被丢弃**。同一类复杂任务（如「需求分析 + 方案设计 + 报告」）每次都要重新规划，耗时且结果不稳定。

**目标**：编排产物可沉淀、可复用、可编辑——形成「先跑通 → 另存为工作流 → 复用/微调」的平台化闭环（对齐文章「再平台化」阶段）。

**详细设计**：

1. **数据模型扩展**：`flows` 表（经 `/api/studio/agent-flows` 持久化）的 definition 增加 `source` 标记字段：
   ```json
   {
     "source": "planner_auto",          // planner_auto=自动编排沉淀 | manual=手绘
     "planner_meta": {"goal": "…", "orchestrated_by": "session", "intent": "design"},
     "nodes": [{"id": "t1", "type": "agent", "label": "需求分析",
                "config": {"forced_intent": "requirement_analysis",
                           "query": "{{payload.input}}…", "context": "…", "expected_output": "…",
                           "tools": ["graph_retrieve"], "skills": []}}],
     "edges": [{"source": "t1", "target": "t2", "condition": ""}],
     "summary_node": {"enabled": true}   // 复用 _summarize_plan 作为汇聚 LLM 节点
   }
   ```
2. **后端落库**：`_planner_core` 执行成功后（`done_items ≥ 1`），把 `plan + goal + deps` 归一化为标准节点/边结构，写入 `flows` 表（`status='draft'`，`name = f"自动编排-{goal 前 20 字}"`）；失败或降级（`degraded=True`）不落库，避免污染模板库。
3. **前端入口**：AI 建模对话卡片新增「💾 另存为流程」按钮（仅 `card_data.orchestrated=true` 时显示）→ 调 `POST /api/studio/agent-flows` → 成功后跳转 `st-flow` 画布并 `flowSelect(id)`，用户可继续拖拽微调（改节点 prompt / 加 if 分支 / 改工具白名单）。
4. **复用匹配**：`pipeline.py` 在 `_needs_orchestration` 命中后、LLM 再规划前，先按「意图 + 目标关键词」检索已发布且 `source='planner_auto'` 的 flows（复用 `_match_flows` 语义匹配）；命中 → 直接 `FlowExecutor.run` 该流程（输入 payload 为当前用户输入），未命中 → 走原 LLM 规划路径。

**边界与降级**：
- 沉淀失败（落库异常）不影响本轮编排结果（静默，返回时标记 `saved_flow=false`）；
- 复用执行失败（节点配置过期）→ 捕获异常回退原 LLM 规划路径，并在卡片标注「复用流程失败，已重新规划」。

**涉及模块**：`workflows/planner.py`、`workflows/persistence.py`、`workflows/engine.py`、`agent/pipeline.py`、`routers/studio.py`、`static/index.html`（卡片按钮 + st-flow 跳转）

**验收口径**：
1. 前端输入「先做需求分析，再生成方案设计，最后输出报告」触发自动编排，执行完成后卡片出现「另存为流程」；
2. 点击后跳转 st-flow 画布，节点/连线/工具白名单与 plan 一致；
3. 发布该流程后再次输入同类任务，接口命中复用流程（卡片标注「复用流程：xxx」），不再重新规划；运行轨迹正常。

### P0-2 编排执行轨迹可视化

**现状**：Planner-Executor 执行时，前端只看到 `done` 事件里的 `plan` 列表（task key/title/status），无节点级执行轨迹（谁在跑、耗时、失败在哪一步）。文章强调「端到端流程状态的实时追踪」。

**目标**：自动编排执行过程在 AI 建模对话内以 DAG 轨迹图实时呈现，失败节点可定位原因。

**详细设计**：

1. **事件流扩展**（`agent/pipeline.py::_stream_orchestrated_flow`）：
   - 子任务开始：`{"type":"subtask","key":"t2","status":"run","agent":"design","deps":["t1"]}`
   - 子任务完成：`{"type":"subtask","key":"t2","status":"done","latency_ms":3200}`
   - 子任务失败：`{"type":"subtask","key":"t2","status":"failed","error":"…前120字"}`
   - 并行执行（ready>1）时同一批子任务连续产出 run 事件，前端按 deps 关系分层布局
2. **卡片渲染**：复用前端已有 DAG 渲染能力（st-flow 画布 `flowCanvasToDef`/节点样式），在对话卡片内渲染轻量轨迹图：
   - 节点 = 子任务（agent 图标 + title），有向边 = deps；
   - 状态着色：run（蓝脉冲）/ done（绿勾 + 耗时）/ failed（红叉，点击展开 error）；
   - 全部完成 → 折叠为「n 子任务 ✓ m 完成 / k 失败」摘要行，可展开回顾。
3. **轨迹持久化**：`card_data.exec` 已累计 reasoning/tools；本次再累计 `subtasks:[{key,title,agent,status,latency_ms,error}]`，历史消息回看可还原轨迹。

**边界与降级**：
- 前端不支持 DAG 渲染的环境（极简模式）→ 退化为现有 plan 列表展示；
- 子任务事件丢失（连接中断）→ 以 done 事件携带的完整 `plan` 为准，轨迹仅作增强展示。

**涉及模块**：`agent/pipeline.py`、`static/index.html`（卡片渲染 + 样式）

**验收口径**：
1. 触发一次自动编排（≥3 子任务且有并行），对话内实时看到 DAG 轨迹图：并行子任务同层、串行子任务按 deps 递进；
2. 人为制造一个失败子任务（如指定不存在技能），失败节点红叉可点开查看错误；
3. 刷新页面回看历史消息，轨迹图完整还原（非空卡片）。

### P0-3 Agent 长期记忆接入主管线

**现状**：`memory_service.py::MemoryService` 已具备完整能力（`search` 语义检索+时间衰减 / `deposit` 主动沉淀 / `maybe_deposit` LLM 评估沉淀 / `push` 会话推动 / `forget`+`consolidate` 遗忘合并维护），但**只在 `workflows/nodes.py` 的 memory 节点被调用**，`AgentPipeline` 主管线完全不感知记忆。文章定义「记忆系统」是 Agent 四大核心构成之一——建模规范、领域约定、用户偏好无法跨会话复用，同一类错误反复出现。

**目标**：建模会话主链路接入记忆「读 → 注入 → 沉淀」闭环。

**详细设计**：

1. **读（检索注入）**：`execute_stream` 组装 system_prompt 时新增 `_build_memory_hint(user_input, intent, user)`：
   - 查询 = `user_input + 意图 + Agent 名`（关键词拼装），`MemoryService.search(conn, agent_id=intent, query=…, top_k=5)`；
   - 命中 → 注入 System Prompt 独立块：
     ```
     【长期记忆（跨会话经验，仅供对齐，不得虚构扩展）】
     - (score 0.87) 建模规范：SysML v2 视图必须按 BDD/UC/ACT 分独立包 …
     - (score 0.72) 用户偏好：报告正文不要展示 REQ-xxx 编号 …
     ```
   - 记忆内容与检索上下文并列但标清来源，防止 LLM 把记忆当事实检索结果引用。
2. **写（沉淀）**：`execute_stream` 产出阶段（done 前）对 `intent in (design, requirement_analysis, impact, review)` 的高质量产出调用 `MemoryService.maybe_deposit(conn, agent_id=intent, task_content=content, task_query=user_input)`：
   - 真实 LLM → LLM 提炼为 fact/preference/experience/skill 结构化记忆；
   - Mock/无 key → 规则降级（内容 >120 字且非客套语 → 沉淀为 experience），保持确定性。
   - 频率护栏：每会话最多沉淀 2 条（防噪音），且 `deposit` 内部 `content[:800]` 截断防膨胀。
3. **用户级记忆**：`user` 传入时，`agent_id` 用 `f"{intent}@{user['display_name']}"` 或全局 `"global"` 混合查询——用户偏好（report 风格等）存 `user-{display_name}` 记忆槽，跨 Agent 复用。

**边界与降级**：
- 记忆检索/沉淀任何异常静默（try/except），不影响主流程；
- `agent_memory` 表已存在（schema.py L393），无需迁移；缺 embedding 环境走 bigram 兜底（MemoryService 已内置）；
- 记忆内容只作「参考对齐」，Prompt 硬约束「不得虚构扩展」，避免记忆污染模型输出。

**涉及模块**：`agent/pipeline.py`（`_build_memory_hint` + 沉淀钩子）、`memory_service.py`（复用，无需改动）、可选 `routers/conversations.py`（传 user）

**验收口径**：
1. 对话 A：告知「报告正文不展示编号」→ 对话 B（同意图）：输入相关任务，System Prompt 注入包含该偏好记忆（调试日志可见 `_build_memory_hint` 命中）；
2. `agent_memory` 表新增记录（mem_type=preference，source=push/llm_agent），且每会话沉淀 ≤2 条；
3. 记忆检索失败（清空表）时建模功能完全不受影响（回归原链路）。

---

## 5. P1 · 中期（工程完备）

### P1-1 任务难度分级模型路由

**现状**：`LLMRouter.route` 仅按「能力标签 + 优先级 + Token 预算」选模型（`llm/__init__.py::LLMRouter`）。意图识别、复杂度判定、计划生成、正文生成、总结全部可能落到同一大模型——轻调用（意图识别）用大模型成本浪费，重调用（建模生成）用小模型质量不够。

**目标**：按调用场景难度分级路由：「轻调用 → 小模型 / 重调用 → 大模型」，且标签与预算约束保持。

**详细设计**：

1. **配置扩展**：`llm_providers` 表新增 `task_levels` 文本列（JSON 数组，如 `["light","medium","heavy"]`）；前端「模型参数设置」处可编辑。
2. **难度定义**（按 `_intent` 场景映射）：
   | 级别 | 场景（_intent） | 说明 |
   |---|---|---|
   | light | `intent_detect` / `complexity` / `memory_push` / `memory_deposit` | 判断类、短输出，用快速模型 |
   | medium | `planner` / `task_llm` / `plan_summary` | 结构化 JSON / 汇总，要求格式稳定 |
   | heavy | 主链路建模生成（`design` / `requirement_analysis` / `impact` / `review` 的正文） | 长文 + SysML 代码，要求最强模型 |
3. **路由扩展**：`LLMRouter.route` 增加 `level` 入参：
   - 候选过滤：`level ∈ task_levels` 且标签/预算满足；
   - 优先级排序保持（score 不变），无匹配 level 的 provider → 回落原路由逻辑；
   - `llm_client.chat(…, route_tags=…)` 调用点按 `_intent` 映射补充 `level`（封装进 `_intent→level` 字典，默认 medium）。
4. **降级链**：heavy 模型预算耗尽 → 依次降级 medium/light（复用现有 `exhausted` 回退逻辑）。

**边界与降级**：`task_levels` 为空（未配置）→ 等效现状（全量候选）；Mock 环境不受影响（force_mock 优先）。

**涉及模块**：`database/schema.py`（列）、`database/seeds.py`、`llm/__init__.py`（路由 + level 映射）、`static/index.html`（模型设置表单）、`routers/studio.py`（配置接口）

**验收口径**：
1. 配置两个 provider：A 大模型仅 heavy、B 小模型仅 light；调用意图识别接口，命中 B；主链路建模生成命中 A；
2. 关闭 A 的预算或标签后，heavy 调用自动降级 B（usage 落库 route_reason 含「预算耗尽」）；
3. 全部 provider 无 task_levels 配置时，行为与改造前一致（回归验证）。

### P1-2 编排异常补偿机制

**现状**：`_planner_core` 中 `TaskQueue.fail` 后子任务置 `failed`，其下游 `blocked`，**无自动重试、无降级顶替、无人工接管通道**（唯一兜底是 worker 级 `worker_timeout_s` 超时）。文章强调编排层「异常处理与补偿机制」。

**目标**：子任务失败策略化：可重试 → 可降级 → 可人工接管，失败原因随 DelegateResult 上报，总结不再把失败当成功。

**详细设计**：

1. **自动重试（幂等）**：`TaskQueue.fail` 分支增加重试判定：
   - 失败且 `retry_count < max_retries(=2)` 且失败类型为**可重试**（超时/瞬时 API 错误/JSON 解析失败）→ 状态置回 `ready`，`retry_count+1`，指数退避（1s/3s，不阻塞调度循环，用 `ready_at` 时间戳控制）；
   - 不可重试（计划错误/权限不足/模型不可用）→ 直接 failed。
   - 幂等：`run_id + task_key` 唯一，`TaskQueue.claim` 加 `AND status='ready'` 条件防并行重复认领。
2. **降级顶替**：重试仍失败且 `task_type='agent'` → 尝试同池次优 Agent 顶替（如 `impact` 失败 → `review` 降级分析；规则表 `agent_fallback`：`{from: "impact", to: "review", reason: "…"}`），顶替成功标记 `degraded_from` 供总结标注。
3. **人工接管**：重试+降级均失败 → 状态 `blocked`，写入 `hil_confirmations`（HIL 人工处理队列），卡片轨迹节点显示「⏸ 待人工处理」，用户可重试/跳过（复用现有 HIL 确认交互）。
4. **总结标注**：`_summarize_plan` 输入补充 fail_items 结构化信息（`{key, title, error, retried, degraded_from}`），Prompt 硬约束「失败/降级子任务必须在报告中对应小节显式标注，不得作为已确认事实」。

**边界与降级**：所有补偿动作 try/except 包裹，异常时保持原 failed 行为；`max_retries`/`fallback` 表由配置中心 `delegation` 分组管理（`core/config.py`）。

**涉及模块**：`workflows/task_queue.py`（重试/幂等）、`workflows/planner.py`（降级 + 总结标注）、`hil_service.py`（接管）、`database/schema.py`（`agent_tasks` 加 `retry_count` 列）

**验收口径**：
1. 制造可重试失败（临时断网），子任务自动重试 2 次后成功，`agent_tasks.retry_count=2`，轨迹显示「重试 2 次」；
2. 重试仍失败且配置了 fallback → 降级顶替执行成功，总结报告标注「t2 由 变更影响Agent 降级执行」；
3. 重试+降级均失败 → blocked + HIL 确认单，前端可人工重试/跳过，报告不把失败当成功。

### P1-3 Agent 反思闭环（行动 → 评估 → 改进）

**现状**：会话层建模生成为单轮直出；`reflection` 节点仅存在于工作流层（`engine.py::_exec_reflection`），会话主链路无「生成后自查 → 修正」闭环。建模类输出（SysML 代码）常存在遗漏/不一致，需用户人工发现。

**目标**：建模类意图（design / requirement_analysis / impact / review）生成后追加轻量反思：LLM 自查 → 发现问题自动修正 1 轮（可配）。

**详细设计**：

1. **反思触发**：`execute_stream` 阶段 3（LLM 生成完成）后，对 `intent in (design, requirement_analysis)` 且正文含 ```` ```sysml ```` 的输出触发：
   - `reflection_prompt`：把生成内容 + 反思清单（完整性：需求覆盖 / 一致性：命名与关系 / 规范性：SysML v2 语法要点、视图分包规范）交给 LLM，输出 JSON `{"issues":[{"severity":"high|medium|low","problem":"…","fix":"…"}]}`；
   - 有 high 问题 → 执行第 2 次生成（注入问题清单，要求修正后重新输出）；否则跳过（不浪费 token）。
2. **修正注入**：第 2 次生成把 issues 作为独立块注入 System Prompt：「根据以下自查发现的问题修正输出，仅修正这些问题，保持其他内容不变」。
3. **护栏**：
   - 反思与修正仅对**含 sysml 代码块**的输出执行（非代码输出跳过）；
   - 修正最多 1 轮（防循环）；修正后仍失败不阻塞，卡片标注「已自查 1 轮，存在 N 个未决问题」；
   - `force_mock` / 无 key 环境：LLM 无法结构化解析 → 跳过反思（确定性保持）。
4. **沉淀**：反思发现的问题模式（如「视图未分包」）经 `MemoryService.maybe_deposit` 沉淀为 experience（与 P0-3 联动，下次生成前注入防再犯）。

**边界与降级**：反思 LLM 调用失败/超时 → 直接返回原生成内容（不降级质量）；反思仅增耗时（轻量调用可配 light 级模型，联动 P1-1）。

**涉及模块**：`agent/pipeline.py`（反思钩子 + 修正注入）、`llm/__init__.py`（level 复用）

**验收口径**：
1. 输入建模任务且输出含 sysml 代码块，后端日志出现反思调用（`_intent="reflection"`），存在 high 问题时自动修正 1 轮；
2. 修正后卡片标注反思轮次；无 high 问题时仅 1 次生成（不浪费）；
3. Mock 环境行为与改造前完全一致（反思跳过）。

---

## 6. P2 · 长期（治理强化）

### P2-1 工作流 ↔ 编排融合

**现状**：自动编排（Planner-Executor）与可视化工作流（FlowExecutor DAG）是两条独立链路——编排不产工作流（P0-1 解决沉淀）、工作流也不消费编排产物。

**目标**：双向融合——工作流节点可嵌套编排（orchestrator 节点内直接跑 `run_planner_plan`），编排 plan 可展开为画布编辑。

**详细设计**：

1. **orchestrator 节点增强**：`engine.py::_exec_orchestrator` 支持 `mode="planner"`：节点 config 透传 goal/agents/max_tasks/parallel 给 `run_planner_plan`，返回结构化结果（plan + 各子任务 DelegateResult），下游节点可用 `{{node_id.data.tasks}}` 引用；
2. **编排 ↔ 工作流互转**：
   - 编排 → 工作流：P0-1 已实现（沉淀为 flows 表）；
   - 工作流 → 编排：用户将已存 flows 的 definition 作为「编排模板」绑定到 Agent（`agents` 表加 `flow_template_id` 列），同类任务命中 Agent 时直接按模板生成 plan（模板节点 → 子任务，未命中节点才由 LLM 补规划）。
3. **执行上下文互通**：编排子任务执行走 `FlowExecutor.run`（复用检查点/黑板/事件回调），而非独立 `TaskQueue` 循环——统一调度状态机与可观测。

**边界与降级**：模板缺节点/失败 → 回退 LLM 规划；双向转换均为显式用户操作（按钮），不自动发生。

**涉及模块**：`workflows/engine.py`、`workflows/nodes.py`、`workflows/planner.py`、`database/schema.py`、`static/index.html`

**验收口径**：
1. 画布放置 orchestrator(planner) 节点，配置 goal 模板，运行后产出多子任务轨迹与总结，下游 llm 节点可引用子任务结果；
2. Agent 绑定 flow 模板后，同类任务按模板执行（卡片标注「模板执行」），未覆盖部分 LLM 补规划；
3. 双向转换互操作无数据丢失（节点 config 全量保真）。

### P2-2 编排级共享黑板升级

**现状**：blackboard（`flow_working_memory`）仅工作流层有（`engine.py`），planner 编排层子任务之间只靠 `context` 单向下传，无共享工作记忆——多个子任务重复检索相同领域事实，且并行写可能覆盖（无版本）。

**目标**：编排层引入共享黑板：子任务可读写领域事实，支持版本/写前校验；减少重复检索与上下文膨胀。

**详细设计**：

1. **编排层黑板**：`run_planner_plan` 初始化 blackboard（复用 `flow_working_memory` 表），子任务 `_run_task` 前注入黑板相关事实（`MemoryService` 同构：`黑板书 = 最近 N 条共享事实`），子任务产出经 `maybe_deposit(…, source='blackboard')` 写回（去重+截断）；
2. **版本与写前校验**：`flow_working_memory` 增加 `version` 列；写黑板带 `expect_version`，版本不匹配拒绝（防并行覆盖），冲突提示子任务重读；
3. **可选订阅**：工作流 memory 节点支持 `subscribe_keys`，黑板 key 变更触发下游节点重算（复用 D11 事件机制）。

**边界与降级**：黑板读写失败静默（回退纯 context 传参）；版本冲突仅记录不阻塞。

**涉及模块**：`workflows/planner.py`、`workflows/persistence.py`、`database/schema.py`（version 列）、`workflows/nodes.py`

**验收口径**：
1. 两个并行子任务先后读同一黑板 key，第二次读到第一次写入的版本（非覆盖）；
2. 子任务产出自动沉淀黑板，下游子任务 Prompt 出现「共享上下文」块，检索次数下降（对比日志 query_routing_stats）；
3. 模拟并发写冲突，版本校验拦截并记录，任务不阻塞。

### P2-3 委派级 Trace 与编排成本核算

**现状**：有 `tool_call_logs` / `flow_runs` / `llm_usage_stats`，但「谁把什么任务委派给哪个 Agent、传了什么上下文、给了哪些权限、花了多少 token」无关联视图。文章可观测性五问（谁创建、收到什么、调用什么、结果怎么传、哪里失败）无法完整回答。

**目标**：编排委派链路全 Trace + 单次编排成本核算，Studio「工具调用日志」旁新增「委派链路」视图。

**详细设计**：

1. **新表 `delegation_logs`**：
   ```sql
   CREATE TABLE IF NOT EXISTS delegation_logs (
     id INTEGER PRIMARY KEY AUTOINCREMENT,
     run_id INTEGER DEFAULT 0,             -- 编排运行 id（agent_tasks.run_id）
     conversation_id INTEGER DEFAULT 0,
     parent_agent TEXT DEFAULT '',          -- 委派方（planner/orchestrator/Agent 名）
     child_agent TEXT DEFAULT '',           -- 被委派 Agent（intent 名）
     task_key TEXT DEFAULT '',
     task_title TEXT DEFAULT '',
     context_snippet TEXT DEFAULT '',       -- context 摘要（前 200 字）
     tools_whitelist TEXT DEFAULT '[]',     -- JSON: 本次授予工具白名单
     expected_output TEXT DEFAULT '',
     status TEXT DEFAULT 'planned',         -- planned|running|done|failed|blocked
     retried INTEGER DEFAULT 0,
     degraded_from TEXT DEFAULT '',         -- 降级顶替来源（P1-2）
     error TEXT DEFAULT '',
     token_cost INTEGER DEFAULT 0,          -- 本次委派累计 token
     latency_ms INTEGER DEFAULT 0,
     created_at TEXT DEFAULT CURRENT_TIMESTAMP
   );
   ```
2. **写入点**：`_planner_core` 每子任务创建/完成/失败均写 `delegation_logs`（复用 `_log_tool_call` 的观测模式）；`agent_tasks.config` 已有完整委派协议，直接拷贝。
3. **成本核算**：`llm_usage_stats` 按 `run_id + task_key` 维度聚合（子任务执行时 `_intent` 带 `task_key` 前缀，如 `_intent="task:design:t2"`），单次编排结束在卡片展示「⚡ 共调用 N 次模型 / M tokens / 子任务耗时合计」。
4. **前端视图**：Studio「工具调用日志」tab 旁新增「委派链路」视图：按 run_id 分组展示 parent→child 树、上下文摘要、白名单、状态、token 成本；失败节点红标可展开 error。

**边界与降级**：Trace 写入失败不影响执行（静默）；token 统计缺失（Mock）显示「-」。

**涉及模块**：`database/schema.py`、`workflows/planner.py`、`llm/__init__.py`（intent 前缀）、`static/index.html`、`routers/studio.py`

**验收口径**：
1. 一次 4 子任务编排后，`delegation_logs` 有 4 条关联记录（parent=planner，child=各 Agent，含白名单与 context 摘要）；
2. 卡片展示本次编排 token/耗时统计；Studio「委派链路」视图树状展示且失败节点可展开 error；
3. 历史编排（无 delegation_logs）页面不报错（空态提示）。

---

## 7. 建议实施顺序与验收口径

| 阶段 | 项 | 预期收益 | 依赖 |
|---|---|---|---|
| 第一批 | P0-1 编排沉淀工作流 | 编排结果平台化复用，效率与一致性提升 | 无 |
| 第一批 | P0-2 编排轨迹可视化 | 端到端可观测，失败定位快 | 无 |
| 第一批 | P0-3 Agent 记忆接入 | 建模规范/偏好跨会话复用，错误不再重复 | 无 |
| 第二批 | P1-1 难度分级路由 | 成本优化（轻调用小模型） | 无 |
| 第二批 | P1-2 异常补偿 | 编排稳定性、失败可接管 | P0-2（轨迹展示重试/降级） |
| 第二批 | P1-3 反思闭环 | 建模输出质量提升 | P0-3（问题模式沉淀）+ P1-1（反思用轻模型） |
| 第三批 | P2-1 工作流↔编排融合 | 统一调度状态机与可观测 | P0-1 |
| 第三批 | P2-2 编排黑板 | 减少重复检索、上下文更优 | P0-3 |
| 第三批 | P2-3 委派 Trace + 成本 | 治理与观测上台阶 | P0-2、P1-1、P1-2 |

**通用验收场景**：前端输入「先做需求分析，再生成方案设计，最后输出报告」——
1. 触发自动编排（P0-2 轨迹图实时显示子任务进度）；
2. 完成后「另存为流程」→ st-flow 画布可编辑 → 发布（P0-1）；
3. 再输入同类任务命中复用流程（不重新规划）；子任务失败自动重试/降级/人工接管（P1-2）；
4. 建模输出经反思修正 1 轮（P1-3）；记忆沉淀后新会话自动注入建模规范（P0-3）；
5. Studio「委派链路」视图可回溯整条委派链与 token 成本（P2-3）。

---

## 8. 与既有模块衔接

- **配置中心**：`delegation` 分组新增 `max_retries / retry_backoff_s / agent_fallback / reflection_enabled / reflection_max_rounds`（P1-2/P1-3）——`core/config.py` + REST 配置接口自动获得管理入口；
- **工具注册表**：P0-1 沉淀的 flow 节点工具白名单复用 `tools.kind/config` 机制，不新增权限模型；
- **HIL**：P1-2 人工接管复用 `hil_confirmations`（L2 写确认同队列），不冲突；
- **Skill**：P0-1 沉淀节点带 `skills` 字段对接已发布技能池；P1-3 反思问题模式经记忆沉淀，不新增技能；
- **LLM 路由**：P1-1 在现有 `LLMRouter.route` 上扩展 `level` 过滤，不改变标签/预算/优先级语义；
- **可观测**：P0-2 复用 `card_data.exec`，P2-3 复用 `llm_usage_stats` 聚合，不新建基础设施。

---

## 9. 风险与注意事项

1. **编排沉淀模板污染**：P0-1 只沉淀 `done_items ≥ 1` 的成功编排，且模板默认 `draft` 需人工发布，避免低质量模板进入复用池；
2. **记忆误导**：P0-3 记忆注入带「仅供对齐、不得虚构扩展」硬约束，且记忆与检索上下文分区展示，防止模型把记忆当事实来源；
3. **反思成本**：P1-3 反思仅对含 sysml 代码块的输出、且仅 1 轮，配合 P1-1 light 级模型控制成本；
4. **兼容性**：所有新表/新列均 `ALTER TABLE` 幂等迁移（对齐 `database/migrations.py` 模式），旧数据不迁移不报错（空态提示）；
5. **Mock 确定性**：所有 LLM 增量能力（反思/记忆评估/难度路由）在 Mock/无 key 环境自动跳过或规则降级，保证离线可回归。
