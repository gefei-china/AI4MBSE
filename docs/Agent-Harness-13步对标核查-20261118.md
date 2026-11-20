# Agent Harness 13 步对标核查 —— 公众号《AI Agent 底层架构曝光》× mbse_system 现状

> 核查日期：2026-11-18 ｜ 来源文章：《AI Agent 底层架构曝光！Harness + Agent Runtime + Execution Loop，13 个关键步骤拆解！》（"智能体AI"，2026-11-18 08:00，湖南）
> 文章链接：https://mp.weixin.qq.com/s/pgceBUpsbsDfx-eMNyldwA
> 核查方式：**只读**。代码给 `文件:行号`，数字来自真库 `file:mbse.db?mode=ro` 与运行服务 `/api/system/config`；凡"代码存在但生产未接线"按本工程判据单列（全仓 grep 命中只在 tests/verify/docs ⇒ 判未接线）。
> 口径提醒：既往核查文档里的部分数字（三个 CAP=3000/4000/1200、prompt 尖峰 24744 未兜住、25 个 read 工具无门）本次复核**已过期**，本文只采用本次实测值。

---

## 0. 结论摘要（结论先行）

**文章 13 步在本平台全部有对应实现，无一步完全空白：4 步"已实现"、9 步"部分"；但 9 个"部分"里有 4 步（权限准入/HITL/受控执行/出口判定）偏弱。五项工程能力中"上下文工程"接近满分，"权限与安全""可观测审计"是当前两大短板。**

| 定级 | 步数 | 步骤 |
|---|---|---|
| ✅ 已实现 | 4 | 2 上下文装配、3 预算闸门与压缩、5 模型输出、10 连接真实系统（偏已实现） |
| 🟡 部分 | 9 | 1 任务下达、4 模型调用、6 参数校验、7 权限准入、8 HITL、9 受控执行、11 结果回注、12 出口判定、13 定稿交付（其中 7/8/9/12 偏弱） |
| 🔴 整步缺失 | 0 | 但有 4 个关键子项缺失：统一 `acceptance[]`、网络白名单、deny 硬拒档、跨层单一 trace_id |

**最需要处理的三件事（按优先级）：**
1. **P0**：认证未强制（`enforce_login=False`、`trust_user_id_header=True`、users 无口令列）+ SPARQL 端点完全匿名 + 5 个内建工具绕过权限门 ⇒ 文章 STEP 07 在企业级语境下**不成立**。
2. **P0**：`harness_decisions` 296 条中 enforce 仅 12 条、`would_block=1` 全库仅 1 条且是 shadow ⇒ **"模型说做完，系统验证"从未成为主路径**（文章最强调的 STEP 12）。
3. **P1**：无贯穿"请求→模型→工具→审批→结果"的单一 trace_id（llm 覆盖 39%、tool 62%，audit_logs/graph_edit_logs/knowledge_commits 无该列）⇒ STEP 05 能力构建不起来。

---

## 1. 文章框架与本平台映射总表

| 步骤 | 文章要求 | 本平台定级 | 一句话依据 |
|---|---|---|---|
| 01 用户下达任务 | 结构化任务描述/身份/目标/交付物/超时/工具限制/验收条件 | 🟡 部分 | goal/entities/constraints/scope + expected_output（已强制兜底）+ deadline 齐全；**无统一 `acceptance[]` 字段**，验收散在 agent_todos.verify |
| 02 装配上下文 | System+Tool+History+TaskState，稳定/动态分层，稳定前缀复用 | ✅ 已实现 | `common.py:272-380` 单一真源拼装，两路径共用；产物摘要/协议契约均注入 |
| 03 预算闸门与压缩 | token 统计、压缩历史、保任务状态、防重复副作用 | ✅ 已实现 | `history.py:1659-1743` 总闸接线（`stream.py:5549`），五档压缩，CAP 已升 8000 |
| 04 发起模型调用 | 全量上下文+工具定义+约束发 LLM，遵守预算 | 🟡 部分 | `llm/__init__.py:587-650` 主链完整、retry/fallback 落 `llm_usage_stats`；**LLMRouter 零生产调用** |
| 05 模型本轮输出 | 文本 vs 结构化 tool call；tool call 只是请求 | ✅ 已实现 | SSE 透传 + 标准/行内 tool_call 解析 + ASCII 工具名护栏（`tools.py:1017-1031`） |
| 06 解析与参数校验 | 注册/Schema/必填/类型/值域；有限次修正 | 🟡 部分 | 注册表+Schema 对齐+执行出口齐备；**内置工具执行侧无统一类型/值域校验**，`missing_args` 仅 1 处生产调用 |
| 07 权限准入网关 | 身份+操作+资源+风险分档；行/列级权限；系统判定 | 🟡 部分（偏弱） | token 排他/RBAC/Agent 授权门/SPARQL 白名单有；**认证默认未强制、SPARQL 匿名、5 工具绕过、无行列级权限** |
| 08 人工确认 HITL | 高风险暂停、审批与参数绑定、超时策略 | 🟡 部分（机制强） | 参数快照+乐观锁+统一执行器是真闭环；**无 deny 档、destructive 可批不可拒、可自批、approve 档 0 条** |
| 09 受控工作区执行 | 授权目录/工具白名单/网络白名单/超时/资源约束/日志 | 🟡 部分 | `sandbox.py` 代码沙箱扎实且接线；**文件/网络/DB 写类工具直接在服务进程跑，无网络白名单、无 CPU/内存限额** |
| 10 连接真实系统 | 文件/DB/API/MCP/第三方 | ✅ 已实现（偏） | `mcp_client.py`→`mcp_servers` 真实 JSON-RPC，智源真实端点；Fuseki 3030 未监听但默认走嵌入式 pyoxigraph（非缺陷，是选型） |
| 11 结果规范化并回注 | 统一信封、失败状态、回注、唯一 call_id 对应并行结果 | 🟡 部分 | 结构化返回+offload 回注+tool_call_id 配对；**无框架级唯一 call_id、顺序执行非真并行、日志表无调用 ID 列** |
| 12 出口判定 | Thinking→Action→Obs→Eval；四态出口；验收驱动独立验证 | 🟡 部分（代码强、实证弱） | 五道闸+指数退避+幂等+checkpoint 齐全；**无统一四态出口函数，enforce 真验证仅 13/296** |
| 13 定稿并交付 | 文件存在/可读/数据一致/无遗留失败；失败如实告知 | 🟡 部分（偏强） | 归档+validator+防虚报门禁（pass=8/fail=0）+三态聚合；**校验只查格式、门禁仅 local、无原子 preflight** |

---

## 2. 五项工程能力评分

| 能力 | 文章定义 | 本平台成熟度 | 主要证据 / 缺口 |
|---|---|---|---|
| 1 Context Engineering | System Prompt/工具定义/历史/状态、长上下文压缩、状态保存恢复 | **4/5** | 分层单一真源、五档压缩、offload、session digest 均落地；扣在缓存顺序门禁 backlog 未进 CI、`materialize_to_conversation` 生产未接线 |
| 2 Tool Orchestration | Tool Registry/Schema/Router/Executor/Result Handler，顺序并行超时错误 | **3.5/5** | 112 active 工具 100% 有 input_schema、统一执行出口 `_exec_tool_call`、retry_exec；扣在参数校验覆盖不全、顺序执行无真并行、曾发生"注册未接线致 46 次未知工具" |
| 3 Permission & Security | 身份认证、RBAC/ABAC、数据范围、白名单、沙箱、人工审批 | **2/5** | 代码组件齐（59 处 require_permission），但运行态漏洞集中：enforce_login=False、users 无口令、SPARQL 全匿名、5 工具绕过、无行列级权限、harness 真实拦截 0 次 |
| 4 Execution Loop | 状态管理、调度、观察、异常、有限重试、终止判断、断点恢复 | **4/5** | ReAct 双路径、五道终止闸、指数退避、幂等键、checkpoint/resume/supervisor 完整；扣在无四态统一出口、验收独立验证未成主路径 |
| 5 Observability & Audit | Tracing/Logging/Metrics/Audit/Evaluation，一条 Trace 串起全链 | **3/5** | audit_logs 7001 行哈希链 0 broken、llm_usage 29665 行成本延迟 >95% 非空；扣在 trace_id 覆盖 39%/62%、审批与图编辑/知识提交无 trace 列、三套 id（request/trace/run）并存 |

---

## 3. 不足清单（按处置优先级）

### P0 · 直接影响"企业级可上线"判定

**G1 认证与权限运行态未闭合（对应 STEP 07）**
- `/api/system/config` 实测 `/static/auth/enforce_login=False`、`trust_user_id_header=True`；users 表 9 列无口令列。
- `routers/sparql.py:46-57,115-116` 端点只挂 `db_session` 不挂鉴权；`main.py` 仅两个中间件（AuditRequestContext + RateLimit），无全局认证。
- `agent/pipeline_parts/tools.py:1716/1737/1769/1796/1816` 五个内建工具（read_attachment_section / memory_fetch / tool_result_fetch / todo_write / ask_user）在 RBAC 闸（:1858）之前 return，不受 `agent_tool:read` 约束。
- **建议**：上线 checklist 固化三条（enforce_login=True、SPARQL 挂鉴权、内建工具补门）；无口令列等真实认证方案落地前，至少把 trust_user_id_header 与 enforce 联动。

**G2 独立验收未成为主路径（对应 STEP 12）**
- 真库 `harness_decisions` 296 行：shadow 202 / subtask 76 / enforce 12 / orch_enforce 1 / orch_notice 5；全库 `would_block=1` 仅 1 条且是 shadow。enforce 12 条的 reason 全是"无待办⇒不拦"或"待办全过⇒放行"。
- `agent/harness_gate.py:58-72` 自述 761 条 verify 中 44%（333 条）曾被当工具名误执行；只读白名单仅 16 个工具；`autopilot.py:434-438` 对 `manual` 类验收直接 `ok=True`（等价于"模型说完成就放行"）。
- 代码侧已接线（`stream.py:3871-3885` orch_enforce_run），但生产配置默认 `harness_enabled=False, harness_mode=""`，当前靠 override 才为 enforce。
- **建议**：把 harness 默认值改为安全侧；把"待办验收通过率/独立验证覆盖率"作为发布门禁指标，而非只统计判定条数。

### P1 · 影响规模化与可运维性

**G3 无跨层单一 trace_id（对应文章第五节 Observability）**
- `llm_usage_stats` trace_id 非空 11,547/29,665（39%），`tool_call_logs` 6,180/9,907（62%），`context_audit` 100%；`audit_logs` 无 trace_id 列（request_id 仅 3,464/7,001=49%），`graph_edit_logs`/`knowledge_commits`/`harness_decisions`/`orch_run_spans` 均无 trace 列。
- llm 与 tool 共享的不同 trace_id 仅 426 个，同时拥有两类 trace 的会话 299 个；request_id/trace_id/run_id 三套 id 并存，需靠 conversation_id 手工拼接。
- **建议**：引入贯穿全链的 correlation_id，至少在 audit_logs / graph_edit_logs / knowledge_commits / harness_decisions 补列并写入现有 `current_trace_id()`。

**G4 受控执行边界不完整（对应 STEP 09）**
- `sandbox.py` 只覆盖 `code_exec`；`file_*`（`file_tools.py:288-297`）、`web_fetch`（`web_tools.py` urllib 直连任意 URL，无白名单/SSRF 判定）、DB/MCP/HTTP 工具全部在服务进程内直接执行。
- 无 CPU/内存限额，只有超时与输出截断。
- **建议**：给 web_fetch 加域名白名单 + 私网地址拒绝；文件工具加工作区根目录约束；高风险工具逐步迁入沙箱执行。

**G5 HITL 策略分档不足（对应 STEP 08）**
- `services/hil_policy.py:70 LEVELS=("auto","notify","approve","confirm")` — 无 deny/forbid 档；真库 `destructive=confirm`（可批后执行，非硬拒）；46 条确认单 level 只有 ""(10) 与 confirm(36)，approve 0 条。
- 审批端点只要求 `hil:approve`，设计师角色同时有 `agent_tool:write` 与 `hil:approve` ⇒ 可自批自改（无"审批人≠执行人"校验）。
- 确认卡（`06-cards.js:298-312`）展示动作/影响面/可逆性/原始参数/超时，但无前后值对比、无执行人/审批人、无风险等级字段。
- 超时靠 `tools/cleanup_expired_hil.py --apply` 手动，默认 dry-run。

### P2 · 工程债与口径治理

**G6 任务契约无统一 `acceptance[]`（STEP 01）** — goal/entities/constraints/scope 在 slots，expected_output 在 plan，deadline 在 runtime，验收在 agent_todos.verify；`request_invariant` 默认 shadow（漂移不硬失败）。

**G7 参数校验与结果信封不统一（STEP 06/11）** — `missing_args`（`agent/tool_readiness.py:91-120`）生产调用点仅 `tools.py:2078` 一处；返回信封实际是 `ok/result/latency_ms/is_error`，非文章建议的 `status/data/execution_time_ms`；无框架级唯一 call_id（`tool_call_logs` 无该列），并行调用实为顺序执行。

**G8 门禁口径过期** — `verify_token_budget.py:140/142`、`verify_s4_prompt.py:74`、`verify_orch_summary_budget.py:264/266` 仍断言旧 CAP 3000/4000/1200/24000，而生产已升 8000/48000；`verify_prompt_cache_order.py` 在 backlog 未进 CI。门禁写死旧值会在改动时给出假绿/假红。

---

## 4. 文章没提、但平台已有（避免过度自贬）

按本工程"报缺口也要报已对项"的纪律，明确列出文章未覆盖而平台已具备的能力：
- **SysML v2 官方校验器闭环**（checker.jar + 标准库，校验 verdict 分 pass/report/block，接入 artifact validator）——文章只讲通用工具执行，未涉及领域校验器。
- **知识工程全链路**（实体/关系、v2g 候选、本体快照、SPARQL/Fuseki 适配、文档生命周期、RAG 漏斗调参）——超出文章的销售分析示例范畴。
- **审计哈希链**（`core/audit.py` 7001 行、0 broken）——文章只要求 Audit 记录，平台已做到防篡改。
- **编排 checkpoint/resume/supervisor 自愈**——文章 STEP 12 只讲 loop 内重试，平台多了批次级断点与守护。

---

## 5. 一句话总评

**这个平台不是"没有 Harness"，而是 Harness 的工程组件大多已经建好：真正的差距不在"有没有代码"，而在"运行态是否强制生效"——认证默认关、验收几乎没真拦、trace 串不起来、写类工具不在沙箱里。文章里那句"模型可以说我做完了，Harness 决定这一轮能不能退出"，对本平台当前的准确表述是：退出逻辑已经写了，但默认没有真正握着方向盘。**

> 本文档为新增未跟踪文件，尚未提交；核查全程只读，未改生产代码与数据。
