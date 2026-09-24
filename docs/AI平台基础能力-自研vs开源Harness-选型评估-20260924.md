# AI 平台基础能力：自研 vs 开源 Harness 底座 —— 选型评估与落地建议

> **时点**：2026-09-24。**对象仓库**：`mbse_system`（Python / FastAPI / SQLite 单进程）。
> **问题来源**：Agent / Skill / 工具等基础能力目前自研，能否直接基于开源 Harness 平台
> （Pi / DSH / Deep Agents 等）定制，以确保基础能力"扎实稳定"。
> **事实来源分级**：三者的定位/栈/许可均取自**官方仓库与官方文档站**（GitHub、docs.langchain.com、
> deepseekharness.io、deepseekai.works），文末列链接；中文技术文章仅作背景参考，**不作为选型依据**。

---

## 0. 结论摘要（先给判断）

**结论一句话：不要换底座，要让领域能力"接口化"。**

1. **本项目的基础能力并不薄**，薄的是**闭环度**。实测基数：`agent/` 包 **8144 行**、
   `llm/` **561 行**、`agent/intent.py` **474 行**、`agent/rag.py` **641 行**、
   `agent/pipeline_parts/tools.py` **703 行**；`tools` 34 / `skills` 13 / `agents` 20 条数据。
   **但三个"能力空转"实例**同时存在：
   - `intent_rules` 表 **0 行** —— 可配路由规则的机制建好了、没人用（代码 `intent.py:206-211` 已在消费）
   - `mcp_servers` **7 行全部 `status='offline'`** —— MCP 客户端能力完备，但没有一个真能连上的服务
   - `multi_intent` 识别出多阶段意图（本次实测 `['requirement_analysis','design']`）却**不驱动任何决策**，
     仅发 SSE 事件供展示（`stream.py:889-892`、`:914-915`）
   ⇒ **空转的根因是"没接上领域资产"，不是"底座不行"。带着空转去换底座，换完还是空转，还多了一层进程边界。**

2. **语言栈是第一硬约束**：本项目是 **Python**。三个候选里 **只有 Deep Agents 是 Python**；
   **Pi 与 DSH 都是 TypeScript/Node**（需 Node ≥22.19），只能**跨进程**集成（RPC/SDK/CLI）。
   而本项目**已实测过跨进程 stdio 集成的坑**（历史事故：`spawnSync` 被守护进程的 stdio 管道卡死）。
   再加 SQLite 单写事务边界，跨进程集成会把"稳定"这件事变难，而不是变简单。

3. **性价比排序（我的推荐）**：
   | 路线 | 判断 |
   |---|---|
   | 整体替换现有 agent 管线为 Pi / DSH | ❌ **不建议**（跨语言进程边界 + 丢掉领域路由/SSE 契约/卡片体系，收益不抵成本） |
   | **领域能力外化成 MCP server（对外供给）** | ✅✅ **最推荐**（你们已有 MCP 客户端与完整表结构，做供给方是顺路；一旦做成，**任何 harness 都能消费你们的能力**，从此不再被任何底座绑死） |
   | **组件级吸收**（LLM 适配 / 上下文压缩 / 观测 / HITL 粒度） | ✅ **推荐**（按 component 逐个换，风险最小） |
   | 引入 Deep Agents 作为**新增**编排运行时试点（不替换） | ✅ 可评估（同栈、可进程内；但 LangGraph 与现同步生成器/SQLite 模型有摩擦，需试点验证） |
   | DSH 作为独立运行时（**仅当**要做"AI 平台产品化"对外交付时） | 🟡 中期再议（preview 期，见 §4 风险） |

4. **一句话纠偏**：**"扎实稳定"的决定因素是领域契约与闭环度，不是底座来源。**
   而"换底座"本身**就是当前最大的不稳定源**——它会一次性作废你们所有绑在源码文本上的自检资产
   （`tools/verify/` 下按精确子串断言的脚本），并重写 SSE / 卡片 / 落库三套契约。

---

## 1. 先把"基础能力"拆开：哪些是商品，哪些是你们的护城河

选型混乱的根源是把"基础能力"当一个整体。按**商品化程度**切开，结论立刻清晰：

| 层次 | 能力 | 商品化程度 | 本项目现状 | 该不该换/外部化 |
|---|---|---|---|---|
| L1 传输 | 多 provider LLM 适配、流式事件协议、token 计费 | **极高**（人人都在做） | 自研 `llm/` 561 行 + `llm_providers` 4 条 | ✅ **值得换/收编**（LiteLLM 或对齐 OpenAI 兼容） |
| L1 传输 | 工具 Schema 校验、工具执行引擎、并行/顺序执行 | **高** | `pipeline_parts/tools.py` 703 行 | 🟡 可保留（已可用，换的收益小） |
| L2 运行时 | Agent Loop（LLM→工具→回注→下一轮） | **高** | 自研，已稳定 | 🟡 可保留 |
| L2 运行时 | 会话持久化 / 上下文压缩 / checkpoint | **中高** | 有落库与 `messages` 双轨；**压缩是弱点** | ✅ 值得吸收（Deep Agents 的 summarization + 文件卸载思路） |
| L2 运行时 | 子 Agent 隔离上下文 / 长任务拆解 | **中高** | 自研编排（分层并行，实测 345s/6 子任务） | 🟡 可借鉴 |
| L2 运行时 | 沙箱 / 权限 / 审批 / 审计 | **中** | 有 HIL 四档 + 写操作暂存 | 🟡 粒度可借鉴 Claude Code allowlist |
| L3 领域 | **意图路由（三层 + DST + 澄清 + 缓存）** | **极低**（无人替你写） | 自研 `intent.py` 474 行 | ❌ **必须自研** |
| L3 领域 | **GraphRAG + 本体 + 知识状态机 + 一致性校验** | **极低** | 自研 `rag.py` 641 行 + 本体域 | ❌ **必须自研** |
| L3 领域 | **SysML v2 生成/校验硬约束（checker.jar 链路）** | **极低** | 自研 `v2_constraints.py` 120 行 + 校验器 | ❌ **必须自研** |
| L3 领域 | 版本管理 / 分支 / 合并请求 / 实体时态历史 | **极低** | 自研 | ❌ **必须自研** |
| L3 领域 | 富卡片 7 类 + 报表规范 + 产物版本链 | **极低** | 自研 | ❌ **必须自研** |
| L4 交付 | 产物呈现 / 版本滑杆 / diff / 报告导出 | **中** | 部分具备 | 🟡 可对标吸收（Canvas/Artifacts 形态） |

> **判读**：真正商品化的只有 **L1 的模型适配** 与 **L2 的循环/压缩/沙箱**——这些占你们代码量的
> 大约**一成到两成**；**L3 全是护城河，也是你们 8000+ 行 agent 代码的绝大部分**。
> 换底座能替掉的是那不痛不痒的一两成，替不掉的是决定产品价值的那八成。

---

## 2. 三个候选底座的骨架与事实

> 三者许可**都是 MIT**（可商用/可修改/可再分发，需保留版权与许可文本）。
> **共同点**：都把"系统提示 + 思维链 + 工具调用与结果 + 子 Agent 派发 + 每次上下文注入"写进**追加式会话日志**，
> 支持 resume/fork/replay —— 这是比"基础能力"更值得抄的一条架构决策（你们的 `messages` 双轨已接近此形态）。

### 2.1 Pi（`github.com/earendil-works/pi`）—— 小内核，大扩展

- **栈**：TypeScript / Node ≥22.19。**许可**：MIT。作者 Mario Zechner（libGDX 作者），2026-04 由 Earendil Inc. 接手。
- **内核极简**：默认只给模型 4 个工具（read/write/edit/bash），系统提示 <1000 token；
  **主动砍掉** MCP / 子 Agent / Plan Mode / Todo / 权限弹窗 / 后台 Bash。
- **四层包结构**：`pi-ai`（模型与 Provider，30+ 家）→ `pi-agent-core`（agent loop / messages / tools / state）
  → `pi-coding-agent`（coding 能力 + Extension + Skill + Session）→ `pi-tui`（终端 UI，可自研替换）。
- **扩展机制**：`ExtensionAPI` 统一注册 **工具 / 命令 / 事件钩子 / 模型 / TUI 组件**；
  另有 System Prompt / Prompt Templates / **Skills**（`agentskills.io` 标准）/ Extensions 四类资源。
- **集成方式**：TUI、Print/JSON、**RPC**、**SDK**（可嵌入自有程序）。
- **⚠️ 风险**：默认**无沙箱**，`bash` 等同当前用户权限；社区扩展非官方维护（需审源码）。
- **对本项目**：跨语言（Node），只能 RPC/SDK 集成；其"小内核 + 扩展"哲学**值得抄**，底座本身**不建议直接采用**。

### 2.2 DSH = DeepSeek Harness（`github.com/deepseek-ai/deepseek-harness`）—— 一切皆插件

- **栈**：TypeScript / Node ^22.19（pnpm 11.7）；**许可**：MIT；npm 包 `@deepseek-ai/dsh`。
- **内核**：**Cordis** 插件框架（DeepSeek 将 Cordis 源码 vendored 进 `vendor/`，9 个包 rescope 为
  `@deepseek-ai/*`）。Cordis 只负责**装载/卸载/依赖解析**，通过 `ctx` 共享能力（`ctx.tools` / `ctx.llm`）；
  **连 Agent Loop 都是插件** —— 这是它与 Pi 的本质区别（Pi 扩展的是外围，DSH 是**用插件组装出系统本身**）。
- **配套论文**：《A Programming Paradigm for Spatiotemporal Composability》（arXiv 2608.25512，
  北大 + DeepSeek-AI）—— 一个 agent 框架配一篇独立论文，工程严谨度罕见。
- **形态**：Web UI / TUI / Headless / **SDK / ACP**；四个 Preset（Standard / PTC(Code Mode) / Minimal / Create）；
  Agent Team 模式（lead agent + sub-agents）。
- **⚠️ 风险（这条最重要）**：**v0.1 developer preview**（2026-08-13 开源），官方 README 明确警告
  "**会有兼容性破坏的变更**"；**Issues 与 PR 关闭**（只走 Discussions/Discord）；
  插件**无中心注册表**（唯一索引机制 = 给自己的仓库打 `dsh-plugin` GitHub topic）⇒ **供应链与治理风险真实存在**。
  另需自查 `THIRD_PARTY_NOTICES.md`（根 LICENSE 不覆盖全部依赖）。
- **对本项目**：**跨语言**（Node）+ **preview 期** + **插件生态无审计** ⇒ 现在把它作为生产基座**时机不成熟**；
  但作为**独立运行时**做 ACP/MCP 对接（若做平台产品化）是中期可选项。

### 2.3 Deep Agents = `langchain-ai/deepagents` —— 唯一同栈候选

- **栈**：**Python 为主**（PyPI `deepagents`，也有 JS 版 `deepagents.js`）；**许可**：MIT。
- **定位**：LangChain 官方 "**batteries-included agent harness**"，`create_deep_agent(...)` 一行起手，
  返回**标准 `CompiledStateGraph`**（即任意 LangGraph 图都能作为它的 sub-agent 塞进去）。
- **特性**：Sub-agents（隔离上下文）/ Virtual filesystem（in-memory、本地盘、LangGraph store、
  组合路由等可插拔后端 + 读写权限规则）/ 上下文管理（summarize + 工具输出卸载到磁盘 + prompt caching）/
  Shell / 持久记忆（pluggable store，跨会话）/ **Human-in-the-loop**（批准/编辑/拒绝工具调用）/
  **Skills** / 工具与 **MCP**（`tools=` 可直接传 MCP server 的工具）。
- **依赖**：**强依赖 LangGraph**（durable execution / streaming / HITL / checkpointing）；
  **LangSmith 非必需**但官方推荐用于 tracing / eval / 监控（**注意：观测数据出境需评估**）。
- **对本项目**：**唯一能进程内集成**的候选（Python）。但**摩擦点明确**：
  ① LangGraph 是 **async graph + checkpointer** 体系，你们是**同步生成器 + SQLite 单写事务**
  —— 两者的执行/持久化模型不同，硬接会打乱现有 SSE 与落库契约；
  ② 它替的是 L2（loop/子 Agent/上下文），**替不掉 L3**。
- **结论**：**适合作为"新增编排运行时"做试点，不适合替换现有管线。**

### 2.4 补充候选（本次一并核过，供对照）

- **Claude Agent SDK / Claude Code**：hooks / skills / sub-agents / 逐命令 allowlist，
  是**权限粒度与 skills 规范**的最佳参照（但不是开源底座）。
- **Codex Harness（OpenAI，Rust 内核）**：开放 `AGENTS.md` / Skills / Hooks / MCP / Plugin 五类标准扩展，
  内核稳定、治理能力齐全（沙箱/审批/权限）。**适合"外围扩展"，不适合深度改装**；语言 Rust，跨进程。
- **OpenCode / OpenClaw**：同赛道的 harness（OpenClaw 以 Pi 为底层引擎之一）。

---

## 3. 本项目实测约束（决定选型的硬条件）

这些不是理论风险，是**本仓已经发生过/已测到**的事实：

| # | 约束 | 事实依据 | 对选型的影响 |
|---|---|---|---|
| 1 | **语言栈 Python，单进程** | FastAPI + SQLite + 同步生成器（`stream.py` 内**无 `async def`**） | 排除 Pi/DSH 的进程内集成；只剩 Deep Agents 可选 |
| 2 | **跨进程 stdio 集成踩过坑** | 历史事故：`spawnSync` 被守护进程 stdio 管道卡死（已入 skill） | Pi/DSH 的 RPC/CLI 集成**风险已被实证**，不是臆测 |
| 3 | **SSE 契约已成体系** | 13 类事件（`reasoning/stage/agent/token/done/tool/subtask/skill/clarify/clarify_ask/multi_intent/error`），前端逐类渲染 | 换底座 = 重写这 13 类的产出与消费 |
| 4 | **卡片 + 落库双轨** | `msg_type` + `card_data`，7 类富卡片，历史回放走同一渲染函数 | 换底座 = 全部卡片重做（含历史消息兼容） |
| 5 | **自检资产绑源码文本** | `tools/verify/` 下脚本对 `pipeline_parts/*.py` 断言**精确子串**（含局部变量名与切片写法） | **换底座 = 这类断言全废**（重构成本被严重低估的一项） |
| 6 | **SQLite 单写 + 多写者同仓** | WAL 库、并发会话在同一工作区写代码（已出过"被并发提交卷走"事故） | 引入外部运行时 = 多一个写入方，事务与归属都更复杂 |
| 7 | **已有 MCP 客户端 + 完整表结构** | `mcp_servers` 表含 transport(stdio/sse/http)/capabilities/protocol_version/resources/prompts；`routers/studio_parts/mcp.py` 185 行 9 个端点 | **做 MCP 服务端是顺路，不是从零**（见 §5 P0-1） |
| 8 | **MCP 客户端当前空转** | 7 个 server **全部 `offline`**（5 个是"测试"占位、2 个指向 docker 内网名 `mbse-mcp:4001`） | 先清理/接通，否则"接生态"只是口号 |

---

## 4. 风险清单（若要引入外部底座，必须逐条回答）

| 风险 | 具体内容 | 缓解 |
|---|---|---|
| **Preview 期兼容性** | DSH 明确警告"会有兼容性破坏变更"（v0.1，2026-08-13） | 锁定精确版本；只在非关键路径试点；不将其作为唯一基座 |
| **供应链** | Pi / DSH 社区插件均非官方维护；DSH 无中心注册表（靠 GitHub topic），**无法审计"有多少插件、谁在维护"**；Pi 默认无沙箱、bash 等同当前用户 | 引入前**逐插件审源码**（与本仓既有 skill 安全审计纪律一致）；生产环境加沙箱 |
| **许可** | 三者 MIT 均可商用；但 **DSH 根 LICENSE 不覆盖全部依赖**，需查 `THIRD_PARTY_NOTICES.md` | 出交付物前做一次许可清点（可复用 `docs/开源许可证清单-20260922.md` 的口径） |
| **观测数据出境** | Deep Agents 官方推荐 LangSmith（SaaS）；企业内网/合规场景可能不允许 | 用可自托管的 Langfuse 替代；或只本地 tracing |
| **执行模型错配** | LangGraph 是 async + checkpointer；本仓是同步生成器 + SQLite 单写 | 试点时**只在新链路上用**，不改现有 SSE/落库契约 |
| **不可逆点** | 一旦按某底座重写 SSE/卡片/落库契约，回退成本极高 | 所有对接**走接口层**（MCP / HTTP），不让底座类型穿透到领域层 |

---

## 5. 建议与落地路径（每步带验收判据）

> 原则：**先接闭环，再换组件；领域能力接口化优先于底座替换。**

### P0（立即可做，低风险高收益）

| # | 动作 | 为什么现在做 | 验收判据 |
|---|---|---|---|
| **P0-1** | **把 3 个核心领域能力暴露为 MCP 服务端**（候选：本体/一致性校验、GraphRAG 检索、变更影响分析） | 你们**已有 MCP 客户端与完整表结构**，缺的只是"供给方"。做成后**任何 harness（Claude Code / Pi / DSH / Codex）都能直接消费你们的 MBSE 能力**，从此不再被任何底座绑死 —— 这才是"用开源生态"的正解 | 用任一外部 MCP 客户端 `tools/list` 能看到这 3 个工具，并成功调用 1 次（返回真实领域数据） |
| **P0-2** | **清理/接通 7 个 offline 的 MCP server** | 5 个是"测试"占位、2 个指向 docker 内网名 —— 现状是"能力在、全空转"，会误导后续所有"接生态"的判断 | `mcp_servers` 里**每条**要么 `online` 且能 `discover` 出工具，要么删除；不允许长期 offline 占位 |
| **P0-3** | **`intent_rules` 补种子规则**（即 AI 会话文档 §4 B5） | 可配路由规则的表建好了、代码也在消费（`intent.py:206-211`），**实测 0 行** —— 把"路由可调"从"改代码"变成"配数据" | 新增 1 条规则后**不重启**即命中（注意 `load_rules` 缓存） |
| **P0-4** | **`multi_intent` 二选一**：驱动分阶段计划 / 明确标注"仅展示" | 实测它识别出多阶段意图却**不影响任何决策**，属"建了半截" | 若选驱动：多阶段请求的 `plan` 直接复用 `sequence`（可省一次 planner LLM 调用）；若选展示：代码注释与文档明确写"仅展示" |

### P1（1～2 个月内，体验与成本）

| # | 动作 | 要点 | 验收判据 |
|---|---|---|---|
| **P1-1** | **LLM 适配层评估 LiteLLM**（或对齐 OpenAI 兼容） | 这是**唯一真正商品化**的一层，自研 561 行不划算。**保持 `llm_client` 对外接口不变，只换内部实现** | 现有 4 个 provider 全部走通；`llm_usage_stats.used_mock` 统计不回退；`provider_id` 语义不变 |
| **P1-2** | **吸收上下文压缩**（借鉴 Deep Agents：摘要 + 工具输出卸载到磁盘） | 对应你们长任务痛点（实测单次编排 345s、`run_budget` 200k token 量级） | 构造一个超长输入（>上下文窗口）→ 会话不崩、关键信息可追溯、token 曲线可观测 |
| **P1-3** | **HITL 粒度对齐 Claude Code allowlist** | 你们已有 HIL 四档 + 写操作暂存，缺"**逐工具/逐命令** allow/ask/deny" | 三档各一条用例；`enforce` 下未确认的写请求不落库 |
| **P1-4** | **观测：自研 `usage_stats` → 引入可自托管 tracing**（Langfuse 等） | 现有 1725 条 usage 只有 token/延迟，缺**链路级**追踪（哪一步慢、哪次检索空转） | 能按一次编排回放出完整 span 树（planner → 6 子任务 → 汇总），定位到最慢子任务 |
| **P1-5** | **Deep Agents 试点（不替换）** | 用 `create_deep_agent` 跑一个**真实长任务**（如"文档库批量解析 → 建模 → 校验"），只看子 Agent 隔离上下文与压缩的实际收益 | 同一任务在"现管线 vs Deep Agents"下的 **token / 耗时 / 人工返工**三项对比数据 |

### P2（平台产品化时再议）

- 若确实要做**对外交付的 AI 平台**（多租户、多 Agent 团队、多运行环境），再评估
  **DSH 作为独立运行时** + **MCP/ACP 与本平台对接**。前提：DSH 退出 preview、插件治理成型。
- 届时你们的分工是清晰的：**平台负责领域能力与治理，Harness 负责执行循环** —— 靠 MCP 解耦，谁也不绑谁。

### ❌ 明确不建议

用 Pi 或 DSH **替换**现有 agent 管线。理由已在 §0/§1/§3 列全：
跨语言进程边界（且本机已实证 stdio 集成坑）+ 丢掉 L3 全部领域契约 + 作废 `tools/verify/` 断言资产
——**成本是一次数月级的重写，收益是替掉那一两成商品化代码**。

---

## 6. 一句话回答米爸的原问题

> **"用开源框架打底能不能让基础能力扎实稳定？"**
>
> 能——但**只能让"商品化那一层"（模型适配、循环、压缩、沙箱）更稳**，因为那层的维护者变成了社区。
> 而**决定你们系统稳不稳的，是领域契约的闭环度**：现在 `intent_rules` 0 行、MCP 7 个全 offline、
> `multi_intent` 空转 —— 这些**换任何底座都不会自动变好**，反倒会因为这些契约没有对外接口而更难迁移。
>
> **所以正确顺序是：先把领域能力接口化（MCP 供给方），再把商品化层逐组件换成开源实现。**
> 这样你们既拿到了社区维护的稳定性，又**永远保住了换底座的权利**——那时换 Pi、换 DSH、换 Deep Agents
> 都只是"换一个 MCP 客户端"，而不是"重写一个平台"。

---

## 参考（官方来源）

- **Pi**：`https://github.com/earendil-works/pi` · 官网 `https://pi.dev/`（包目录 `https://pi.dev/packages`）
- **DSH / DeepSeek Harness**：`https://github.com/deepseek-ai/deepseek-harness` · 文档站
  `https://deepseek-harness.github.io/deepseek-harness/` · 官方站 `https://deepseekharness.io/`
  （Cordis 论文：arXiv **2608.25512**，《A Programming Paradigm for Spatiotemporal Composability》）
- **Deep Agents**：`https://github.com/langchain-ai/deepagents` · 文档 `https://docs.langchain.com/oss/python/`
- **对照**：Claude Code hooks/skills/sub-agents/allowlist、Codex Harness（AGENTS.md / Skills / Hooks / MCP / Plugin）
- **本仓既有口径**：`docs/AI会话实现逻辑调研与优化方案-20260923.md`（§3 行业对标、§4 F/B 方案）、
  `docs/开源许可证清单-20260922.md`（许可清点口径）
