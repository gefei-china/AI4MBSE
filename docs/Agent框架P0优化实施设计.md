# Agent 框架 P0 优化实施设计（对齐 DeepAgent / Hermes Agent 能力差距）

> 版本：v1.1（2026-08-13）
> 范围：反思闭环 / 上下文 Token 精确管理 / 子 Agent 隔离上下文注入 / 流式编排并行化 / 技能自改进 / 记忆安全扫描 六项 P0
> 依据：DeepAgent（langchain-ai/deepagents）与 Hermes Agent（NousResearch）框架对比结论 + 本仓库真实代码（agent/pipeline.py、workflows/engine.py、workflows/planner.py、workflows/nodes.py、llm/__init__.py、memory_service.py、skill_deposit.py）

---

## 1. 背景与目标

与 DeepAgent / Hermes Agent 对比后，本系统在**意图路由、长期记忆、技能渐进披露、编排沉淀复用**上已领先；主要差距集中在八项能力。其中 P0 六项为性价比最高、可直接在当前 `AgentPipeline` / `FlowExecutor` 上落地的改进：

| 编号 | 能力 | 现状差距 | 借鉴对象 |
|---|---|---|---|
| P0-1 | 反思闭环 | 汇总后不校验、不自评，偏差不回溯 | DeepAgent 感知-规划-行动-记忆-反思闭环 |
| P0-2 | 上下文 Token 精确管理 | 字符粗估（1 字≈1.5 token），固定预算，无真实 token 计数 | DeepAgent SummarizationMiddleware 自适应触发 |
| P0-3 | 子 Agent 隔离上下文 | 子任务仅注入 title+goal，无任务上下文快照 | DeepAgent subagent fresh context + 单一报告回传 |
| P0-4 | 流式编排并行化 | `_stream_orchestrated_flow` 固定 `ready[:1]` 串行 | DeepAgent 子 Agent 并行（隔离上下文窗口） |
| P0-5 | 技能使用中自改进 | 技能一次性沉淀、人工发布，**无使用反馈采集与周期修订** | Hermes Agent 学习闭环（技能在使用中自我改进） |
| P0-6 | 记忆安全扫描 | 记忆沉淀直接落库，无注入/凭据外泄/角色劫持扫描 | Hermes Agent 记忆写入前威胁扫描 |

## 2. 现状代码映射

| 能力点 | 现有实现（文件:函数） | 差距描述 |
|---|---|---|
| 子任务汇总 | `workflows/planner.py:_summarize_plan` | 只整合交付物，无质量校验/自评/修订 |
| 反思评估 | `workflows/nodes.py:_exec_reflection` | 已有 score/passed/issues/advice 完整实现，**仅画布流程可用**，会话自动编排未接入 |
| 上下文预算 | `agent/pipeline.py:_apply_context_budget` / `_truncate_budget` / `_load_history` | 字符级裁剪（`budget_retrieval_chars=4000`、`budget_history_chars=3000`），无真实 token 计数 |
| usage 数据 | `llm/__init__.py:LLMClient._record_usage` | 已把 `prompt_tokens/completion_tokens/total_tokens` 落库 `llm_usage_stats`（真实 token 数据已具备） |
| 上下文窗口 | `llm_providers.context_window`（前端表单可配 512~1000000） | 已有字段，但未参与预算计算 |
| 子任务执行 | `workflows/nodes.py:_exec_agent`、`agent/pipeline.py:_stream_orchestrated_flow` | 子管道 `conversation_id=0`，query 仅「订阅黑板 + 任务文案」，无任务上下文快照 |
| 委派结果 | `workflows/planner.py:_run_task` → DelegateResult(status/conclusion/evidence/risks/missing_info/next_steps) | 结构化报告已存在，但流式编排路径未采用 |
| 并行执行 | `workflows/engine.py:_run_parallel`（4 线程分层并行）、`workflows/planner.py:_planner_core`（≤3 线程） | 非流式已有并行；**流式编排路径串行** |
| 技能沉淀 | `skill_deposit.py:SkillDepositor.maybe_deposit`（M6：产出 → skills draft，source='ai_deposit'） | 一次性沉淀、人工发布；**无使用反馈记录、无周期修订、无版本演进** |
| 技能命中 | `agent/pipeline.py:_build_skill_prompt`（命中 → `_last_skill_hits` → card_data.skill_hits） | 命中即注入，**不采集执行结果质量反馈** |
| 记忆沉淀 | `memory_service.py:MemoryService.deposit / maybe_deposit / push` | 三入口均直接落库，**无写入前威胁扫描**（注入/凭据外泄/角色劫持） |

---

## 3. P0-1 反思闭环（Reflection Loop）

### 3.1 目标

自动编排（含多 Agent 协作）在「子任务汇总 → 交付」之间插入**质量评审 → 修订 → 复评**闭环：交付物不达标时自动补充修订，达到「感知-规划-行动-反思」闭环；全程无人工介入（与画布 reflection 节点行为一致，但内置到会话编排）。

### 3.2 设计

新增 `workflows/refine.py` 模块，提供 `RefineGate` 组件：

```
输入：goal(原始目标) + done_items(子任务交付物) + plan(计划)
  ↓
步骤1 汇总  _summarize_plan(done_items, plan)          （复用现有）
  ↓
步骤2 评审  _evaluate(report, criteria)                （复用 _exec_reflection 的评分 prompt 与解析）
            返回 {score, passed, issues[], advice}
  ↓
步骤3 决策  passed 或 已修订 2 轮 → 输出最终报告
            否则 → 步骤4
  ↓
步骤4 修订  _refine(report, issues, advice, done_items)
            LLM 按评审意见修订报告（可引用子任务交付物补证据）
            回到步骤2（最多 2 轮）
```

**评审标准 criteria**（按意图差异化，默认）：
- 完整性：是否覆盖 `plan` 全部子任务交付物
- 一致性：是否存在内部矛盾 / 与子任务交付物冲突
- 事实性：是否混入交付物之外的虚构内容
- 风险标注：交付物中的风险/待确认项是否被显式标注（对齐 `_summarize_plan` 第 4 条约束）

**接入点（两处，行为一致）**：
1. 流式编排：`agent/pipeline.py:_stream_orchestrated_flow` 第 4 步「汇总」之后（当前 L1943-1953 `_summarize_plan` 调用处），在 `orch_content` 送 token 推送（L1959）**之前**插入 `RefineGate.run(...)`：
   - 评审/修订期间 yield `{"type":"reasoning","delta":"（自动编排）对汇总结果进行质量评审…"}` 与修订进度事件，保持 SSE 实时性
   - 若评审在首轮通过（score≥阈值），跳过修订，零额外 LLM 开销
2. 非流式编排：`workflows/planner.py:_planner_core` 内 `_summarize_plan` 调用处（汇总后、返回前）插入同一 `RefineGate.run`；`agent/pipeline.py:_finish_orchestrated` 无需改动（消费 `orch.content` 已是精修后内容）

**评分阈值**（settings 可配，默认）：
- `refine.pass_score = 70`（首轮 ≥70 直接通过）
- `refine.max_rounds = 2`（修订最多 2 轮，防止 token 无限消耗）
- `refine.enabled = true`（总开关；Mock/无 key 自动降级为拼接，见 §10）

**落库透出**（card_data 新增字段，前端运行卡展示评审轨迹）：
```json
"reflection": {
  "enabled": true,
  "rounds": 1,
  "score": 86,
  "passed": true,
  "issues": ["…"],
  "advice": "…",
  "provider": "deepseek-v4-flash"
}
```

### 3.3 关键实现细节

- 复用 `_exec_reflection` 的评审 prompt 与 JSON 解析（`workflows/nodes.py:1068-1079`），抽公共函数 `_evaluate_prompt(content, criteria)` 避免重复；`_exec_reflection` 改为调用公共函数（行为不变）
- 修订 prompt 注入：`issues + advice + 原始报告 + done_items 摘要`，要求「按评审意见修订，可引用交付物补充证据，禁止虚构」
- `RefineGate.run` 返回值统一 `{content, score, passed, rounds, issues, advice, llm}`；LLM 异常/解析失败 → 返回原始汇总内容 + `passed=False, degraded=True`（确定性保持）

---

## 4. P0-2 上下文 Token 精确管理

### 4.1 目标

把「字符粗估裁剪」升级为「token 驱动的预算分配」：以真实 `context_window` 为基准，按分区权重动态分配输入预算，超限时按重要性分级裁剪；长会话下对齐 DeepAgent 的自适应摘要触发。

### 4.2 设计

**（1）新增 `core/token_counter.py` 组件**

```python
def count_tokens(text: str, model_hint: str | None = None) -> int
def count_messages_tokens(messages: list, model_hint: str | None = None) -> int
```

计数策略（按可用性降级）：
1. 优先：模型真实 usage——`llm_usage_stats` 已落库真实 `prompt_tokens`；对 `chat` 返回的 `_meta` 增加 `input_tokens` 透传（`llm/__init__.py:_record_usage` 已有数据，扩展 `resp["_meta"]["usage"]`）
2. 估算回退：tiktoken（若已安装，按 model_hint 选编码）→ 无则中文 1 字≈1.2 token、英文 1 词≈1.3 token 的加权估算（比现行 1.5 更贴近实测，仍为估算）
3. Mock 路径返回估算值（不依赖外部）

**（2）预算分配模型（`_apply_context_budget` 改造）**

```
输入预算 input_budget = context_window(该 provider 实际窗口, 缺省 8192)
                     - max_tokens(输出预留, 缺省 4096)
                     - safety(安全余量, 默认 512)
分区权重（settings 可配）：
  retrieval 40%（检索结果，最相关）
  history   35%（会话历史：原文+拉回+摘要）
  memory    15%（长期记忆提示 + 技能渐进披露）
  system    10%（system prompt 固定部分，仅超限时截断工具清单）
分区按「头/尾保留」策略沿用现有：检索保头、历史保尾、记忆保头
```

- `_truncate_budget(text, max_chars, keep_head)` 增加 token 版本 `_truncate_tokens(text, max_tokens, keep_head, model_hint)`：先按 `count_tokens` 估算，超限时以字符近似比例截断后复测（最多 2 次迭代，防止中英混排误差）
- `_load_history` 的 `raw_cap / pull_cap`（当前 50%/75% 字符预算）改为 token 计数驱动（`budget_history_tokens`），原文/拉回/摘要三级仍在预算内竞争

**（3）工具结果回填与轨迹截断**

- 非流式工具结果回填 `result[:2000]` 改为 token 化：`result[: budget_tool_result_tokens(默认1200) ]`，且保留结果「结论尾部 + 首行」双端（首行=工具名/状态，尾部=关键结论），防截断丢证据
- 流式探测回填 `result[:300]` 保留（轨迹展示用，不影响生成上下文）

**（4）长会话自适应摘要（对齐 SummarizationMiddleware）**

- 触发条件：`_load_history` 组装后 `count_messages_tokens(history) > budget_history_tokens × 1.2` 时，对「其他话题」的摘要轮次提高优先（当前摘要已经存在 `_history_summary`，仅需把触发判断从字符改为 token）
- 可选（P2，本期仅留接口）：Anthropic 系 provider 对稳定 system prompt 前缀启用 `cache_control` 提示缓存（`llm/providers/` 实现层预留 `enable_prompt_cache` 参数）

### 4.3 配置项清单（settings 表，缺省走代码默认）

| key | 默认值 | 说明 |
|---|---|---|
| context.budget_retrieval_tokens | 1600 | 检索区 token 预算 |
| context.budget_history_tokens | 1400 | 历史区 token 预算 |
| context.budget_memory_tokens | 600 | 记忆+技能区 token 预算 |
| context.safety_tokens | 512 | 安全余量 |
| context.tool_result_tokens | 1200 | 工具结果回填上限 |
| context.part_retrieval / part_history / part_memory / part_system | 0.40 / 0.35 / 0.15 / 0.10 | 分区权重（需归一） |
| refine.pass_score / refine.max_rounds / refine.enabled | 70 / 2 / true | 见 §3.2 |

> 兼容：旧 key（budget_retrieval_chars 等）保留作为估算回退路径的兜底，不删。

---

## 5. P0-3 子 Agent 隔离上下文注入

### 5.1 目标

子任务执行时注入「任务上下文快照」，让子 Agent 在隔离窗口内获得完成任务所需的最小充分上下文；同时子任务输出收敛为结构化报告，主 Agent 只见报告摘要，控制上下文增长（对齐 DeepAgent subagent fresh context + 单一报告）。

### 5.2 设计

**（1）任务上下文组装 `_build_subtask_context`（新函数，`workflows/planner.py`）**

子任务 query 由三段拼接（现有仅「订阅黑板 + title+goal」）：

```
[任务上下文快照]
- 任务定义：key / title / agent / 期望产出 expected_output（若计划已给）
- 前置结果摘要：deps 中已完成任务的结果（各 ≤400 字），标明「上游 t1 交付物摘要」
- 黑板相关键：cfg.subscribe 命中键值（现有逻辑保留）
- 长期记忆提示：MemoryService.search 命中（现有逻辑保留）
- 交付规范：输出需自包含、仅基于给定上下文、风险需显式标注

[任务] {title}\n团队目标：{goal}
```

**（2）结构化报告回传（复用 DelegateResult）**

- 流式编排 `_stream_orchestrated_flow` 子任务执行后，产出由纯文本升级为结构化摘要：
  - `TaskQueue.complete(conn, id, result, {"summary": …, "score": …})` 的 `result` 保持完整文本（落库）
  - 汇总阶段 `_summarize_plan` 的 `done_items` 增加 `conclusion / risks / missing_info` 字段（从子任务文本按关键词粗提取，或子 Agent 返回时携带——优先后者，`_exec_agent` 返回结构已有 `data` 可扩展）
- `_summarize_plan` prompt 增加约束：「优先采用子任务的 conclusion/risks 字段，避免对长文本的二次截断失真」

**（3）改动清单**

| 位置 | 改动 |
|---|---|
| `workflows/planner.py` | 新增 `_build_subtask_context`；`_planner_core` 的子任务循环改用其组装 query |
| `agent/pipeline.py:_stream_orchestrated_flow` | 子任务 query 组装（L1874 处）改用同一 `_build_subtask_context` |
| `workflows/nodes.py:_exec_agent` | 保持黑板/记忆注入，返回值 `data` 扩展 `conclusion/risks/missing_info`（可选） |

---

## 6. P0-4 流式编排并行化

### 6.1 目标

消除 `_stream_orchestrated_flow` 的 `ready[:1]` 串行瓶颈：无依赖的子任务并行执行，整体延迟下降；同时保持 SSE 进度实时性。

### 6.2 方案 A（推荐落地）：层级并行 + 非流式子管道 + 主线程转发

```
改造点（agent/pipeline.py:_stream_orchestrated_flow 第 3 步）：
当前：while pending: ready=TaskQueue.ready_tasks(); for tk in ready[:1]: 串行执行
改为：while pending:
        ready = TaskQueue.ready_tasks(conn, run_id)
        if not ready: 阻塞依赖处理（保留现状）; break
        # 并行执行全部就绪任务
        with ThreadPoolExecutor(max_workers=min(len(ready), 3)) as pool:
            futures = {pool.submit(_run_subtask, tk): tk for tk in ready}
            for fut in as_completed(futures):     # 主线程逐一收结果
                tk = futures[fut]
                tkey, ok, sub_res, err = fut.result()
                # 主线程统一：TaskQueue.complete/fail + release_deps + 落库轨迹
                yield {"type":"subtask", "key":tkey, "status":"done"/"failed", …}   # 完成即推
```

关键约束：
- **worker 线程只执行子管道**：`sub.execute_stream(query, 0, …, dry_run=True)` 事件流在 worker 内**收集为列表**（子任务通常 3~10 轮 LLM/工具，事件量可控），完成返回 `(tkey, ok, content, events_list)`；**主线程**统一做 `TaskQueue` 状态更新、`release_deps`、落库与 SSE 转发（避免 worker 写 SQLite 与请求级事务互斥——沿用 `_run_parallel` 的「worker 只计算、主线程落库」模式）
- 子任务内部事件实时性降级为「完成时统一转发」（一个 `subtask run → done` 事件对）；如需保留 token 级实时，见方案 B
- 失败任务：`TaskQueue.fail` + `release_deps`，其下游任务 `ready` 阶段自动 blocked（现状逻辑保留）
- 并行度上限取 `min(len(ready), 3)`（与 `_planner_core` 一致，控制 SQLite/LLM 并发）

### 6.3 方案 B（进阶，本期不落地）：事件队列保留实时流

子管道 `execute_stream` 事件写入 `queue.Queue`，主线程从队列取事件实时转发。实现复杂度高（生成器跨线程 + SSE 顺序保证 + 子管道内部 `db_conn` 跨线程），列为 P1 候选，本期仅文档留档。

### 6.4 回归风险控制

- 串行/并行结果一致性：并行只改变执行顺序与吞吐，不改变子任务语义；`guard < 200` 与 pending 判定保留
- 与 HIL：子任务内写操作仍走 HIL 预拦截（`_prequeue_hil` 主线程模式，并行路径已在 `_run_parallel` 验证过）

---

## 7. P0-5 技能使用中自改进（Skill Self-Improvement）

### 7.1 目标

把技能从「一次性沉淀、人工发布」升级为「**使用中采集反馈 → 周期评估 → 生成修订草稿 → 人工审核发布**」的学习闭环（对齐 Hermes Agent：完成任务后自我评估并复用技能、技能在使用中自我改进）。人工审核发布的安全策略不变，仅新增反馈采集与修订产出环节。

### 7.2 设计

**（1）使用反馈采集（轻量，零新表）**

`skills` 表新增两列（`database/migrations.py` 幂等 `_add`）：
- `use_stats TEXT DEFAULT '{}'`：聚合统计 `{"uses": n, "success": n, "fail": n, "last_used": "时间"}`，命中执行后 UPDATE 一次（原子累加）
- `feedback_notes TEXT DEFAULT '[]'`：最近 5 条失败/改进备注 `[{"ts": "...", "run_id": 0, "intent": "", "note": "失败原因（≤200字）"}]`（滚动保留）

埋点位置：
- `agent/pipeline.py:_build_skill_prompt`（L325）命中后返回 `self._last_skill_hits`；在 `execute_stream` 落库段（L2420 / L2486 等写 card_data 处）与 `_finish_orchestrated`（L529）统一调用新函数 `_record_skill_feedback()`：
  - `success` 判定：本次输出 `content` 非空且无 error/typed error（`out["status"]=="ok"` / 无 `error` 字段）
  - 失败 `note`：取 `str(error)[:200]` 或「无输出」/「工具失败」摘要
- `workflows/nodes.py:_exec_agent`（画布 agent 节点，L111 返回处）同样调用，覆盖画布路径

**（2）周期修订 `SkillImprover`（新增 `skill_improve.py`）**

```
扫描触发（每日/每次会话启动时惰性，settings 可配）：
  use_stats.fail >= improve_min_fail(3) 且 fail/use >= improve_fail_ratio(0.3)
  或 use_stats.uses >= improve_min_uses(10) 且 updated_at 距今 > improve_idle_days(30)
  ↓
修订（LLM）：输入「技能原文(content/triggers/description) + 最近反馈备注 + 一次命中上下文示例」
  输出修订版 JSON {content, triggers, description, changelog}
  ↓
产出：status='draft_revision' 的新版本行（version=原version+1），source='ai_improve'，保留原 published 行不动
  ↓
人工审核 → 发布：发布时原版本 status 置 'superseded'（保留审计链）
  ↓
防膨胀：每技能 improve_max_revisions(5) 次修订后停止自动提议，转人工维护
```

**（3）配置项清单**（settings，缺省走代码默认）

| key | 默认值 | 说明 |
|---|---|---|
| skill.self_improve_enabled | true | 总开关 |
| skill.improve_min_fail | 3 | 失败次数下限才触发 |
| skill.improve_fail_ratio | 0.3 | 失败/使用比例下限 |
| skill.improve_min_uses | 10 | 使用次数下限 |
| skill.improve_idle_days | 30 | 长时间未更新触发 |
| skill.improve_max_revisions | 5 | 每技能修订上限 |

**（4）降级**：LLM Mock/无 key → 跳过修订（不产出草稿）；反馈采集不依赖 LLM（规则判定，确定性）。

---

## 8. P0-6 记忆安全扫描（Memory Write Security Scan）

### 8.1 目标

记忆/技能沉淀落库前增加**威胁扫描**，拦截三类风险：提示注入（改写 Agent 行为）、凭据外泄（把密钥写进记忆再被检索带出）、角色劫持/越权操作（对齐 Hermes Agent 的记忆写入前正则扫描）。

### 8.2 设计

**（1）新增 `core/security_scan.py`：`MemoryScanner`**

```python
class MemoryScanner:
    RULES = [  # (rule_key, 正则, 风险类型, 说明)
        # 提示注入
        ("prompt_inject_cn", r"忽略(之前|先前|以上|系统).{0,10}(指令|提示|规则|要求)", "prompt_injection", "..."),
        ("prompt_inject_en", r"ignore (all )?(previous|prior).{0,15}(instruction|prompt)", "prompt_injection", "..."),
        ("role_takeover", r"(你现在是|act as |as a )", "role_takeover", "..."),
        # 凭据外泄
        ("credential_kv", r"(api[_-]?key|secret|password|token|access[_-]?key)\s*[:=]\s*[\w\-]{12,}", "credential", "..."),
        ("credential_sk", r"\bsk-[A-Za-z0-9]{16,}\b", "credential", "..."),
        ("credential_aws", r"\bAKIA[0-9A-Z]{16}\b", "credential", "..."),
        # 越权操作 / 数据外传
        ("exfil_curl", r"(curl|wget|nc|Invoke-WebRequest).{0,30}https?://", "exfiltration", "..."),
        ("privilege_esc", r"(删除|导出|获取).{0,8}(全部|所有).{0,8}(用户|权限|密码|密钥)", "privilege_escalation", "..."),
    ]
    @staticmethod
    def scan(text: str) -> dict:
        """返回 {safe: bool, matched: [{rule, type, snippet}]}"""
```

- 规则集中管理（模块常量），全部为**只读检测**，不执行任何改写
- `snippet` 截取命中位置 ±20 字符供审计（防敏感明文完整入库）

**（2）接入点（统一收口到 `MemoryService.deposit`）**

- `memory_service.py:deposit`（L106，`maybe_deposit`/`push`/`_exec_agent` 的公共落库路径）在 `INSERT` 前调用 `MemoryScanner.scan(content)`：
  - `safe=False` → **不落库**，返回 None，并 `audit("memory_blocked", f"{matched_type}: {snippet_masked}")` 记录
  - 被拦截明细可见于审计中心（复用现有 `core/audit.py`）
- 同一扫描复用于 `skill_deposit.py:SkillDepositor.maybe_deposit` 的 `body` 写入前（技能正文同样可能被污染）

**（3）配置项清单**（settings，缺省走代码默认）

| key | 默认值 | 说明 |
|---|---|---|
| security.memory_scan_enabled | true | 总开关 |
| security.memory_scan_rules | （空=用内置 RULES） | 扩展规则 JSON：`[{"key","pattern","type"}]` |

**（4）误杀控制**：规则仅拦截强特征（sk- 前缀、`api_key=` 赋值等），普通工程内容不受影响；命中仅拦截「写」，不影响「读」与正常对话；拦截可审计、可追溯，不静默丢弃。

---

## 9. 数据与配置变更清单

- **表结构变更（小，幂等 `_add`）**：`skills` 新增 `use_stats TEXT DEFAULT '{}'`、`feedback_notes TEXT DEFAULT '[]'`（§7.2）；其余复用既有表（`llm_usage_stats`、`agent_tasks`、`card_data`）
- **settings 新增 key**：见 §3.2、§4.3、§7.2、§8.2（`refine.*`、`context.*_tokens`、`context.part_*`、`skill.*`、`security.*`）
- **card_data 新增字段**：`reflection`（§3.2）、`subtasks[].conclusion/risks`（§5.2）
- **llm/__init__.py**：`chat` 响应 `_meta` 增加 `usage`（prompt/completion tokens 透传），供 TokenCounter 复用
- **新增文件**：`core/token_counter.py`、`core/security_scan.py`、`workflows/refine.py`、`skill_improve.py`

## 10. 兼容性与降级策略

| 场景 | 行为 |
|---|---|
| LLM Mock / 无 key | `RefineGate` 返回原汇总（`degraded=True`）；TokenCounter 用估算；并行度回落串行（线程池 size=1 兜底）；技能修订跳过（不产出草稿） |
| `context_window` 未配置（0/空） | 按默认 8192 计算 |
| 子任务全部失败 | 不触发评审（无 done_items），直接走现有降级文案 |
| 修订轮次超限 | 输出最后一版修订内容 + `passed=False` 标记（不阻塞交付） |
| 记忆扫描命中 | 仅拦截「写」，返回 None + audit 记录；对话与读取不受影响 |
| 记忆扫描误杀 | 可审计追溯，audit 记录含命中规则与脱敏片段；规则表可配置增删 |
| 旧配置缺省 | `config.get(section, key, default)` 全部带代码默认值 |

## 11. 验证方案

**后端回归脚本**（`tests/manual_verify/`）：
- `verify_p0_refine.py`：构造 mock 子任务交付物（含 1 份明显缺项）→ `RefineGate.run` 断言：首轮未通过 → 修订后通过 / 或 max_rounds 截断；Mock 下不抛异常
- `verify_p0_token.py`：TokenCounter 对中/英/混排文本计数合理性；`_apply_context_budget` token 版对超限输入的分区裁剪正确、不超 `input_budget`
- `verify_p0_subtask_ctx.py`：`_build_subtask_context` 对含 deps 的计划正确注入前置摘要与黑板键
- `verify_p0_parallel.py`：含 3 个无依赖子任务的计划，断言并行执行完成且顺序事件完整（回归 `run_planner_plan` 并行 + 新流式并行）
- `verify_p0_skill_improve.py`：造 1 个已发布技能 + 写入失败 use_stats → 触发修订生成 draft_revision；成功路径 use_stats 累加正确；Mock 下修订跳过
- `verify_p0_mem_scan.py`：构造注入/凭据/越权三类样本 → `MemoryScanner.scan` 命中；`MemoryService.deposit` 拦截不落库 + audit 记录；正常工程内容不误杀

**浏览器验证矩阵**（8001 服务 + playwright）：
| 场景 | 预期 |
|---|---|
| 多 Agent 复杂任务（触发编排） | 运行卡出现「质量评审」进度与 reflection 评分；最终报告完整 |
| 长会话（>20 轮）多话题 | 无超窗报错，历史按 token 预算分区展示 |
| 3 个并行子任务编排 | SSE 中并行 subtask done 事件，总耗时明显低于串行 |
| 命中已发布技能执行 | 技能卡 use_stats 更新；失败多次后管理页出现「修订草稿」 |
| 审计中心 | 记忆拦截事件 `memory_blocked` 可见（含规则与脱敏片段） |
| Mock 环境回归 | 全链路不报错，评审/修订降级为直接输出 |

## 12. 实施任务分解

| 任务 | 文件 | 依赖 |
|---|---|---|
| T1 公共评审函数抽取 | `workflows/nodes.py`（抽 `_evaluate_prompt`） | 无 |
| T2 RefineGate 组件 | 新增 `workflows/refine.py` | T1 |
| T3 流式编排接入评审 | `agent/pipeline.py:_stream_orchestrated_flow` | T2 |
| T4 非流式编排接入评审 | `workflows/planner.py:_planner_core` | T2 |
| T5 TokenCounter 组件 | 新增 `core/token_counter.py` + `llm/__init__.py` usage 透传 | 无 |
| T6 预算模型改造 | `agent/pipeline.py:_apply_context_budget` / `_truncate_budget` / `_load_history` | T5 |
| T7 子任务上下文注入 | `workflows/planner.py:_build_subtask_context` + `_stream_orchestrated_flow` | 无 |
| T8 流式编排并行化 | `agent/pipeline.py:_stream_orchestrated_flow` 第 3 步 | T7 |
| T9 前端运行卡透出 | `static/index.html`（reflection 评分 + subtasks conclusion 展示） | T3/T4/T8 |
| T10 技能反馈采集 | `database/migrations.py`（skills 加列）+ `agent/pipeline.py:_record_skill_feedback` + `workflows/nodes.py:_exec_agent` | 无 |
| T11 技能周期修订 | 新增 `skill_improve.py`（SkillImprover） | T10 |
| T12 记忆安全扫描 | 新增 `core/security_scan.py` + `memory_service.py:deposit` / `skill_deposit.py` 接入 | 无 |
| T13 回归脚本 + 浏览器验证 | `tests/manual_verify/` 6 个脚本 + 浏览器矩阵 | T2-T12 |

实施顺序：T1→T2→T3→T4（反思闭环，可独立交付验证）→ T5→T6（Token 管理）→ T7→T8（并行）→ T10→T11（技能自改进）→ T12（记忆安全扫描）→ T9→T13。每步完成后按用户规则做页面交互验证。
