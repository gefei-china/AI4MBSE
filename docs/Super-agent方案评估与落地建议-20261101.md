# DeepSeek Super-agent 方案评估与在 mbse_system 的落地建议

> 评估对象：① 微信公众号 Datawhale《突发！DeepSeek重磅开源：超级Agent架构！》（2026-10-31 22:32）
> ② 其指向的原始出处：DeepSeek Harness 团队《Super-agent 设计探索：迈向更自主的 Agent 系统》
> ③ 开源原型 `deepseek-ai/dsh-superagent-prototype`（MIT，单 commit `6337fc8`，2026-10-31）
> 评估基准：`C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system`（HEAD `6462c78`）
> 评估时点：2026-11-01　　所有本工程数字均为**本机实测**，口径随数字标注。

---

## 0. 结论先行（TL;DR）

| # | 结论 | 置信度 |
|---|---|---|
| C1 | **这是一份"架构方向声明"，不是可用的工程资产。** 原型 README 自述"研究原型、大量工程由 coding agent 完成、可能仍有 bug、与内部版本和文章描述不完全一致"，且文章描述的多个核心机制（投机执行、Watchdog、链式召回、Inline & Spill、BYOM、存算分离）在原型的 `docs/007-architecture.md` 模块表与测试表中**均无对应实现**。 | 高（读原文 + 仓库文档交叉验证） |
| C2 | **不能作为依赖引入。** 原型是 TypeScript/Node + DeepSeek Harness bundle + GitHub/飞书 CLI；本工程是 Python/FastAPI + SQLite + **私有化离线部署 + 零外部依赖**（硬约束）。技术栈与部署形态双重冲突。 | 高 |
| C3 | **但设计思想对本工程有高价值，且是"补一层"而非"换底座"。** 把 Super-agent 的 9 个设计要点逐条映射到本工程：**6 项已有等价实现、2 项部分具备、1 项真缺失**。唯一真缺口是**事件驱动层（Trigger / Source / 跨会话工单）**。 | 高（本工程源码实测） |
| C4 | **最值得抄的三个具体设计**：`观察模式`（主动工作先只记录不执行）、`准入矩阵`（per-Source × per-sender × per-thread，而非全局静态规则）、`授权随工单版本化`（Mandate + Policy version）。这三个恰好能补本工程 `tool_hooks` 只有 4 条静态规则的短板。 | 中高 |
| C5 | **明确不抄三项**：BYOM/存算分离（违反私有化约束）、投机执行（MBSE 建模有写副作用，风险不对称）、"人不是领域概念"（与 MBSE 人在环/签审取向直接冲突）。 | 高 |
| C6 | **落地的前置条件当前不满足**：智源平台对接的 `base_url` 实测为空串（用户级 override `~/.workbuddy/mbse_config.json` 与 `core/config.py` 默认值均为 `""`），本工程内**无任何入站事件端点**、**无长驻调度器**。没有事件源，"事件驱动"无处发力。 | 高（实测） |
| C7 | **文章的性能数字不可外推到本工程。** 实验口径为"约 300 条消息/其中约 20 条工作请求、每方案跑 5 次、消息间隔与工作时长按 1/4 缩放、编码用一个只消耗时间的工具替代"——**只测调度与协作，不测单个任务做得好不好**。本工程的核心负载是"LLM 生成 SysML v2 + 校验 + 修图谱"，与该实验的负载性质不同。 | 高 |

**一句话**：值得读、值得抄模型、**不值得引入代码**；且抄之前先修智源对接，否则是"空中建楼"。

---

## 1. 这篇文章到底是什么（先把来源分层，避免把二手解读当一手结论）

| 层级 | 材料 | 性质 | 采信度 |
|---|---|---|---|
| L1 | GitHub 原型仓库 `dsh-superagent-prototype` | **一手**，MIT 协议，可读源码与 9 篇编号文档 | 高 |
| L2 | DeepSeek Harness 团队原始长文《Super-agent 设计探索》 | 一手（设计意图），但**明确声明"部分设计是未来方向，原型里还没做"** | 中高 |
| L3 | 本文（Datawhale 公众号解读） | **二手**，基本是 L2 的通俗化转述 | 中（读者为传播效果略去了"未实现"标注） |

### 1.1 原型实际实现了什么（据 `docs/007-architecture.md` 模块表与测试表）

已实现（有源码路径 + 有测试文件）：

- **领域模型层**：`Source / Trigger / Thread / Eval / Policy / Mandate / Ticket / Assignment / Subscription / Correlation / Channel / Outbox / Decision`（`src/core/model.ts`、`src/core/engine.ts`）
- **Engine 为唯一写入者**：`Intake → Evals → Tickets → Assignments → Replies`（`src/core/{intake,evals,tickets,assignments,replies}.ts`）
- **授权体系**：Policy 版本化 + admission table（准入表）+ Mandate 再验证（`src/policy.ts`、`src/admission.ts`）
- **两个 Source 实现**：GitHub、飞书（`src/sources/{github,feishu}.ts`）
- **可靠副作用队列**：Effects（事务提交后执行、并发限制、指数退避、取消、shutdown drain，`src/app/effects.ts`）
- **Outbox 可靠发消息**：回执 + 顺序保证（`src/app/outbox.ts`）
- **面板**：概览 / Source / Trigger / Eval / Execution / Ticket / Thread / 设置 / Prompt 配置（`src/client/*.tsx`）
- **持久化**：SQLite schema 11 + migration
- 测试规模：约 60 个 spec 文件（`tests/`），含变异自证式的 `docs.spec.ts`（校验文档索引完整）

**在原型中查无实据**（文章反复强调、但 `007` 全文未出现对应机制）：

| 文章概念 | 原型现状 | 近似物（不等价） |
|---|---|---|
| 投机执行（speculative execution） | **未出现** | 无 |
| Watchdog / 外部看门狗 | **未出现** | `HealthBoard` + Effect 连续失败三次后报错 + `RetryHealth`（健康上报，非独立监控进程） |
| 链式召回（chain of recall） | **未出现** | `reevaluate(triggerId)`、hand-back 重新排队 |
| Inline & Spill | **未出现** | 有界上下文：Trigger 截 4000 字符、Thread 窗口首尾各 5 条、邻侧消息 400/1200 字符、其余 50 字符 stub |
| 异步 `start()` / `stop()` | **未以该机制存在** | Runtime 生命周期 + `Effects` 异步队列/取消/drain |
| BYOM / 存算分离 / 云端大脑 | **未出现** | 无 |
| "订阅直通"（同 Thread 后续事件跳过 Router） | 术语不存在 | 实现了订阅 / Thread 跟随 / Context-only follow / 取消订阅 / hand-back |

> ⚠️ 这张表本身就是本次评估最硬的一条结论：**文章 L2 里最亮眼的那几条机制，在开源原型里没有实现。** 把它们当作"DeepSeek 已做到"写进汇报材料，会被原文直接打脸。

### 1.2 实验数据的完整口径（不可只摘有利数字）

| 方案 | 任务完成时间 p50/p95（分钟） | 单次实验读缓存 Token（全部 Session） |
|---|---|---|
| Super-agent | 12.4 / 15.7 | **2323 万** |
| Super-agent，去掉订阅直通 | 12.6 / 15.5 | **3177 万**（+37%），Router 决策次数 +约 30% |
| 单 Session + Subagent 工具，中性提示 | 20.4 / 29.5 | 605 万 |
| 单 Session + Subagent，"优先委派"提示 | 13.8 / 29.0 | 1082 万 |

实验条件（原文）：约 300 条消息、其中约 20 条工作请求；**消息间隔与工作时长按 1/4 缩放，模型调用耗时为真实值**；**每种方案跑 5 次**；**编码工作用一个只消耗时间的工具代替**（故只评估调度与协作，不衡量任务本身做得好不好）。

Router 单独压测：Trigger 到达速率 1 条/分 → 40 条/分时，平均 batch 1.0 → 2.8，单请求 2.5s → 3.9s，单个决定分摊推理时间 6.4s → 1.6s（Prefill 阶段并行计算摊薄）。

**这组数字的正确读法**（三条，缺一即误读）：

1. **它用 2~4 倍 Token 换了时间与稳定性**，不是"免费提速"。本工程对此敏感——P1-22 刚因"召回零收益、慢 7 倍"关掉两级 LLM 重排（省 ~2585 tok/轮）。
2. **"中性提示的对照组 5 次一次都没调用 Subagent"** 说明模型不会主动用工具——这恰恰是本工程 P1-15/P1-16 反复处理过的同一问题（关键词路由必须下沉到 db 层），说明**该坑不是 DeepSeek 独有**，不能指望"换个框架就自动解决"。
3. **合成负载 + 简化环境**，作者自己标注"仅作初步参考"。**不能外推**到本工程的"生成 SysML v2 + 校验 + 修图谱"负载。

---

## 2. 与本工程的对照：哪些已有、哪些部分具备、哪些真缺

> 本节所有"本工程"结论均来自 2026-11-01 对 HEAD `6462c78` 源码与 `mbse.db`（只读）的实测。

### 2.1 九个设计要点的逐条映射

| Super-agent 设计要点 | 本工程等价物 | 状态 | 证据（实测坐标） |
|---|---|---|---|
| **Router 只分派不干活（轻量模型即可）** | `agent/intent.py` 意图路由 + `agent/team_router.py` 四档路由决策 + `agent/pipeline_parts/orchestration.py` 编排入口 | ✅ **已有** | intent.py 1736 行 / team_router.py 385 行 / orchestration.py 739 行 |
| **Worker 上下文隔离、挂全工具、最强模型** | 子任务走 `execute_stream(query, 0, ...)`（conversation_id=0 ⇒ fresh context）+ `build_subtask_context` 只注入任务定义/上游摘要/交付规范/产物摘要 | ✅ **已有** | 已固化为 `tools/verify/verify_subtask_isolation.py`（10 PASS + 变异） |
| **Worker 不对外说话，统一由上层回复** | `orch_tasks` 聚合 + `orchestrated_status` + 质量门禁统一出口 | ✅ **已有** | `stream.py` 聚合段（`orchestrated_status` 只降不升） |
| **异步化：长任务后台跑、前台秒回** | `agent/async_jobs.py`（跨轮托管 taskId）+ `routers/jobs.py`（作业队列 21 行 job_jobs）+ `agent/orch_checkpoint.py`（检查点/断点续跑） | ✅ **已有** | async_jobs.py 444 行 / orch_checkpoint.py 536 行 |
| **上下文当 Cache 管理（Inline & Spill 等价物）** | `agent/pipeline_parts/history.py` 四通道预算：近因窗口 + 相关性拉回 + 分话题摘要 + 会话产物摘要 | ✅ **已有（形态不同，机制等价）** | history.py 1779 行；`verify_multiturn_context.py` 76 PASS |
| **看门狗：外部监控 Worker 实质进展** | `agent/loop_guard.py`（token 预算/重复调用/工具超时三闸）+ `agent/harness_gate.py`（harness 级强制闸，判据是 verify **实际执行结果**）+ `agent/auto_continue.py`（自主续跑四闸，fail-safe 向停） | ✅ **已有且更严谨** | 531 / 223 / 572 行；`verify_orch_reliability.py` 45 PASS + 变异自证 |
| **两级过滤：程序 Fast Path → 模型 Slow Path** | `intent.py` 三层（规则/关键词 → 语义 → LLM），P1-16 后**唯一真源 = agents.intent_keywords** | ✅ **已有（且已做过一轮"去写死"改造）** | P1-13/P1-15/P1-16 系列；`verify_intent_kw_layers.py` |
| **授权：准入判定 + 授权随工单版本化** | `core/deps.py::require_permission`（72 处）+ `services/tool_hook_service.py`（99 行）+ `hil_service.py`（692 行）+ `tool_hooks` 表 | ⚠️ **部分具备**：门控够用，但**规则是全局静态 4 条**，不是 per-场景 × per-发起人 × per-线程的矩阵；**无"授权快照随工单走"** | `tool_hooks` 实测 4 行（`file_*`+`..`=block、`zhiyuan_sysmlv2_import`=require_confirm、`mbse_pull_ingest`=require_confirm、`entity_create`=warn）；`hil_confirmations` 21 行 |
| **事件驱动：Trigger / Thread / Source / 跨会话工单** | **无** | ❌ **真缺失** | 全仓无入站事件/webhook/trigger 端点（`routers/` 零命中）；无长驻调度器（`while True` 全在 ReAct 循环内，非调度器）；无 Trigger/Thread/Ticket/Policy 概念 |

### 2.2 关键判断：本工程**不是**缺"超级 Agent 底座"，是缺"事件驱动入口"

Super-agent 的 8 个模块里，本工程已经用另外的名字实现了 6 个。更准确的表述是：

> **本工程有"手"（Worker 执行层、门控、看门狗、上下文预算），有"脑"（意图路由、编排、planner），唯独没有"眼睛和耳朵"（入站事件）。**

这决定了落地形态：**不是引入一个框架，是在现有骨架上补一层事件驱动。** 引入框架反而会与已有实现打架（两套 loop guard、两套预算、两套授权）。

### 2.3 本工程里"事件驱动"能干什么（真实场景映射，不是想象）

| 事件源 | Trigger | 本工程已有的执行能力（可直接复用） | 产出物 |
|---|---|---|---|
| 智源平台模型变更（`/api/project/...`） | 某分支新版本/实体变更 | `zhiyuan_project_tree` + `sysml_importer` + 影响分析 `services/impact_engine.py` | 影响面报告 + 追溯缺口清单 |
| 文档入库（资料库） | 新文档解析完成 | `knowledge_pipeline.ingest` + `vector2graph` 实体抽取 + 本体校验 | 图谱增量 + 术语/本体候选进 `domain_review_queue` |
| SysML v2 校验失败 | `verdict=block` | `sysml_check_tools.py` + 技能「SysML v2 校验与修复」+ `harness_gate` | 自动修复 → 重校验 → 留痕 |
| 需求变更 | 需求条目变更事件 | `workflows/planner.py` 编排 + `refine` 质量门 | 受影响视图集 + 回归校验报告 |

对照 Super-agent 的"主动"特征：这正是它说的"**不等指令，环境事件当输入**"。而本工程当前**全部触发都来自人点一次**。

---

## 3. 落地建议（分三期，含"不做什么"）

### P0 —— 先解前置，别急着建框架（约 1~2 个迭代）

| # | 动作 | 落点 | 验收判据（可自动化） |
|---|---|---|---|
| P0-1 | **打通智源对接**：填 `zhiyuan.base_url` 与 `token`（用户级 override `~/.workbuddy/mbse_config.json` 或 `core/config.py`），并**先跑通 `zhiyuan_project_tree`** | 已有工具 `tools.id` 为 zhiyuan_* 那批 | `agent/tool_readiness.py` 自检全绿；`zhiyuan_project_tree` 实测返回 200 且非空（**注意历史坑：曾实测 HTTP 502 + 43.8s 空等**） |
| P0-2 | **建事件底座三件套**（**不复用 dsh 代码，只借它的模型**）：`Trigger`（不可变事件 + 稳定 ID + actor + human 标记）、`Thread`（事件时间序 + 归属）、`WorkItem`（授权工作单元，替代裸 `agent_tasks`） | 新表 + 新模块（建议 `core/events/`） | 幂等：同 Trigger 重复投递只产生 1 个 WorkItem（**参照 dsh：Trigger ID 必须由稳定外部 ID 派生，游标只在 receive 之后推进**） |
| P0-3 | **观察模式（observe-only）落地**：主动工作**先只记录不执行**，产出一份"本应做什么"的记录，人确认后才放开执行 | 复用 `tool_hooks` 的 `warn` 档 + `domain_review_queue` 模式 | 观察期内：主动触发的写操作 **实际写库条数 = 0**，记录条数 > 0 |
| P0-4 | **把"授权快照"挂到 WorkItem 上**：当前 `tool_hooks` 是全局静态规则，改成 per-WorkItem 的策略版本快照（对应 dsh 的 Mandate + Policy version） | `tool_hooks` 扩展或新增 `workitem_policy` | 工单结束/恢复时，能回答"这条写操作当时依据哪条规则、哪一版策略" |

> **P0-3 是全部落地里性价比最高的一条。** 它把"主动"的风险从"写入图谱"降到"写一行记录"，让后续所有自动化都有一个安全的试验场。这也正好契合本工程既有政策口径——"不强制、不默认提供不合理内容，来源为空时优雅降级"，主动工作同理：**先可观测，再可执行。**

### P1 —— 把 Router 做成 Router，Worker 做成 Worker（约 2~3 个迭代）

| # | 动作 | 说明 |
|---|---|---|
| P1-1 | **入站事件入口 + 准入矩阵**：`POST /api/events`（带幂等键）+ per-Source × per-sender × per-thread 的准入表，替代"全局 4 条 hook"的粗放形态 | dsh 的 admission table 三格（`listedUnaddressed` / `unlistedAddressed` / `unlistedUnaddressed`）可直接借 |
| P1-2 | **Router 批处理**：入站事件在短窗口内合并成批再决策（Prefill 并行摊薄） | 本工程当前是"一次请求一次意图识别"，**批处理对单用户场景收益极小**——dsh 的批处理收益来自 40 条/分洪峰，本工程没有这个负载。**建议只做"事件去重 + 抖动合并"，不做通用批处理框架**（避免为不存在的问题写代码） |
| P1-3 | **跨会话工单**：一个 WorkItem 可跨会话/跨通道推进，接续时**自动带上下文**（等价 dsh 的 Ticket 跟随 Thread） | 解决本工程"跨轮上下文掉链子"的已知痛点；已有 `orch_checkpoint.py` 可作底座 |
| P1-4 | **链式召回的等价实现**：按对象 ID 引用图逐跳检索（Trigger → Thread → WorkItem → 上游交付物 → 会话产物） | 本工程已有 GraphRAG 实体链接 + `_entity_link` 基础，可复用 |

### P2 —— 观察期后按需（不预先承诺）

- 投机执行：**只在纯只读/可回滚动作上开**（如"只算影响面，先不写"），**建模写图谱绝不开**——MBSE 的写副作用（建实体/建关系/合并分支）不对称风险太高。
- 独立 Watchdog 进程：本工程已有 `loop_guard` + `harness_gate` + 心跳 + 检查点，**外部独立看门狗的边际价值有限**，可先不做。
- BYOM / 云端大脑：**与私有化离线硬约束直接冲突，明确不做。**
- 面板（Panel）：本工程已有前端（`static/js/mods/*`），用现有 UI 承载"待人工处理"清单即可，不必复刻 dsh 的 React 面板。

---

## 4. 价值与风险清单

### 4.1 价值（按可兑现度排序）

| 价值项 | 具体形态 | 可兑现度 | 兑现证据/前置 |
|---|---|---|---|
| V1 **"主动"能力补齐** | 从"人点一次"变成"环境事件触发"，覆盖影响分析、实体抽取、校验自愈三类场景 | 高 | P0-1 打通智源 |
| V2 **注意力经济学** | "少占人注意力"直接命中本工程痛点：多轮会话、跨轮上下文掉链子、报告堆到没人看 | 高 | P1-3 |
| V3 **授权模型精细化** | 从"全局 4 条静态 hook"到"per-工单授权快照 + 准入矩阵"，为后续放开自动写操作提供可控底座 | 高 | P0-4 / P1-1 |
| V4 **架构叙事对齐** | 智源"Agent 级协同"路线与 DeepSeek 官方"超级 Agent 底座"同向，可用于产品规划与对外沟通 | 中高 | 无技术前置 |
| V5 **领域模型借鉴** | Ticket / Mandate / Policy version / Subscription 这套概念可直接用于"跨会话工作单元"的建模 | 中 | — |
| V6 **团队协作形态** | GitHub/飞书双通道的"跨信道连续性" | **低** | 本工程无飞书/企微集成，私有化内网场景下 IM 通道价值有限 |
| V7 **7×24 云端常驻** | — | **不适用** | 与私有化离线部署硬冲突 |

### 4.2 风险（按严重度排序）

| # | 风险 | 严重度 | 说明与证据 | 缓解 |
|---|---|---|---|---|
| R1 | **把二手解读当一手结论，写进汇报材料被原文打脸** | 高 | 文章 L3 略去了"研究原型/未实现"标注；`007` 文档证实投机执行、Watchdog、链式召回、Inline&Spill、BYOM **无实现** | 引用时一律标"文章/设计意图"vs"原型已实现"两栏；本文 §1.1 即为对照表 |
| R2 | **引入代码导致架构分裂** | 高 | 引入后会与本工程已有 `loop_guard`/`harness_gate`/上下文预算/权限体系形成双份实现。本工程历史上"同一语义在四个环节各写一份常量"已导致过引用编号对不上 | **只借模型、不引代码**（P0 全部在现有骨架内实现） |
| R3 | **Token 成本被"提速"叙事掩盖** | 中高 | dsh 自己用 2323 万 vs 605 万（2~4 倍）换 p95 15.7 vs 29.5 分钟；本工程已因成本关掉两级重排（P1-22），且 embedding 曾因百炼免费额度耗尽降级 bigram | 任何自动化方案必须同时报"Token/轮"与"耗时"两个口径，不接受只报速度 |
| R4 | **提示词注入 / 凭据泄露** | 中高 | **作者自己列为未解决问题**：云端 Agent Loop 被攻破后，所有端侧设备可能被控制；需设计"不接触凭据本身就能使用凭据"的机制。文档另一处明确 "`meta` 与 `text` 都是不可信数据，不能当作授权依据" | 事件正文一律视为不可信；授权判定只依赖结构化字段与 sender 身份，**不依赖正文语义** |
| R5 | **模型质量幻觉：以为"框架能提高任务质量"** | 中高 | dsh 实验明说"不衡量单个任务本身做得好不好"，编码被换成只耗时的工具 | 落地验收必须用**本工程真实负载**（生成 SysML v2 + 校验 + 修图谱）跑 A/B，判据用 `verdict` 双路计数与 `orchestrated_status`，不用 dsh 的时间数字 |
| R6 | **主动写操作的副作用不对称** | 中 | 建模写图谱后回滚成本远高于读；`entity_create` 目前只是 `warn` 档 | P0-3 观察模式先行；写操作沿用 `require_confirm` 不放开 |
| R7 | **领域模型过重** | 中 | Ticket/Mandate/Policy/Assignment/Eval/Correlation 全套是为"GitHub+飞书团队协作"设计的；单人 MBSE 工作台可能只需 Trigger + Thread + WorkItem 三件套 | P0-2 明确只建三件套，其余按需再加 |
| R8 | **"人不是领域概念"与 MBSE 取向冲突** | 中 | dsh 明确把"人"排除在领域外（人只是 Trigger 的 actor）；MBSE 强调人在环、需求追溯、签审责任 | 本工程实现时**保留"人"作为一等公民**：工单必须记录发起人、确认人、责任人 |
| R9 | **原型本身是未自证的研究代码** | 中 | README：大量工程由 coding agent 完成、可能仍有 bug、风格不一致；本工程 MEMORY 里已有"验证器自身可信度"的连续教训（连续 4 轮栽在验证器上） | 若将来真要读它的代码，**必须自建变异测试**，不信任它的 `tests/` |
| R10 | **前置不满足导致空中建楼** | 中 | 智源 `base_url` 实测为空；本工程无入站事件端点、无长驻调度器 | P0-1 是硬前置；未通之前不写事件层代码 |

---

## 5. 一页纸速览（给决策用）

| 维度 | 评级 | 一句话 |
|---|---|---|
| 架构思想价值 | ★★★★☆ | 事件驱动 + 授权快照 + 观察模式三点可直接补本工程短板 |
| 代码可复用性 | ★☆☆☆☆ | TS/Node + GitHub/飞书 CLI + 私有化冲突，零复用 |
| 直接引入可行性 | ☆☆☆☆☆ | 研究原型、依赖外部平台、违反离线部署约束 |
| 落地工作量（P0） | 中 | 1~2 个迭代，且**必须先通智源** |
| 落地工作量（全量） | 大 | P1+P2 涉及事件总线、调度器、跨会话工单，收益需分批验证 |
| 风险可控性 | 高（若只借模型） / 低（若引代码） | 差别全在"是否把 dsh 代码拉进来" |

**推荐动作**：**读 000/001/007 三篇文档（哲学、领域模型、架构），不 clone 代码。**
然后按 P0-1 → P0-3（观察模式）→ P0-2（事件三件套）→ P0-4（授权快照）顺序推进，每步用本工程的真实负载验证。

---

### 附：本评估引用的原文坐标

| 引用点 | 出处 |
|---|---|
| "研究原型、非正式产品、大量工程由 coding agent 完成" | 仓库 README 首段引用块 |
| "公开版本与内部版本、与文章描述的实现都不完全一样；文章中有些设计是未来方向，原型里还没做" | 仓库 README 首段引用块 |
| 领域模型（Source/Trigger/Thread/Eval/Mandate/Ticket/Assignment/Subscription…）与"被否决的概念"清单 | `docs/001-domain.md` |
| 已实现模块表与测试表 | `docs/007-architecture.md` |
| 新增一个 Source 的完整契约（稳定 ID、游标在 receive 后推进、幂等 key、`meta`/`text` 不可信） | `docs/008-cookbook.md` |
| 注意力经济学与责任条款（Start narrow / Tune for less noise / Do not use it to produce volume） | `docs/000-philosophy.md` |
| 实验口径与四组对照数字 | 原长文"初步实验"节（经 L2/L3 转述，数值一致） |
