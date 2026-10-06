# Agent 生产化 Harness 对照核查 — 2026-10-06

> **基准文档**：《为什么 90% 的 Agent 止步于 Demo？企业级Agent 生产化落地的 10 个实战细节》
> （原创"智能体AI"，2026-10-05发布）
> **核查对象**：`mbse_system` @ `b0882d4`（工作区干净，2026-10-06 11:07）
> **方法**：文章 10 条 Harness 逐条回库取证。判定纪律见 `mbse-optimization-drift-audit` §7.3：
> 「代码存在 ≠ 能力可用」「数据 0 行要分'没做'与'没跑过'」「结构完备 ≠ 语义可达」。

---

## 0. 结论摘要

**一句话判断**：该平台已把文章 10 条里的 **6 条半**做成了真能力（幂等、检查点、循环护栏、重试分类、审计哈希链、模型回退），
但**生产链路上仍有 4 处"代码在、没接线"的空转**，其中 **1 处是安全缺口（P0）**、**3 处是成本/可靠性缺口（P1）**。

| 等级 | 条目 | 性质 | 一句话 |
|---|---|---|---|
| 🔴 P0 | Agent 工具链零权限门 | **安全** | 文章第 1 条的核心，平台 0 覆盖 |
| 🟠 P1 | 智能路由生产零调用 | 成本 | `route_tags` 全仓生产唯一传参点在测试文件里 |
| 🟠 P1 | 熔断器完全缺失 | 可靠性 | 文章"可靠层"明确要求，全仓零命中 |
| 🟠 P1 | 成本闸无执行器 | 成本 | 有统计列、无按用户/部门阈值，配置还是死配置 |
| 🟡 P2 | 幂等仅覆盖编排层 | 可靠性 | 无请求层幂等键（HTTP 头） |
| 🟡 P2 | 审计缺"为什么"+ 工具参数 | 合规 | 五问只答了四问，工具调用日志不在审计链内 |
| 🟡 P2 | 桌面/常驻层未覆盖 | 体验 | 无进程守护、无可见界面层 |

**最反直觉的一条**：平台**不是缺能力，而是缺"接线"**。
`orch_checkpoint.py` / `loop_guard.py` / `llm/__init__.py` 的重试分类都是质量相当高的实现，
但其中 2 项（路由、熔断）**生产路径一次都没走到过** —— 这与本工程历史上的老问题同源：**门基线绿 ≠ 能力被用**。

---

## 1. 逐条对照表（10 条 × 实测）

| # | 文章要求 | 实测结果 | 判定 | 证据 |
|---|---|---|---|---|
| 01 | 权限验证与身份传递 | **部分**：HTTP 层有会话令牌双读+防回落；**Agent 工具链零权限门** | ⚠️ 部分 | `core/deps.py:131-176`；`agent/` 全目录 `require_permission` 零命中 |
| 02 | 桌面/常驻Agent 五层跃迁 | **不适用但有缺口**：无进程守护、无心跳检测服务 | ❌ 缺 | `grep heartbeat/守护/daemon` 仅命中 `alert_evaluator`、`fs_guard` |
| 03 | 审计记录五问 + 脱敏 | **四问**：谁/做什么/何时/结果齐，**缺"为什么"**；**零脱敏** | ⚠️ 部分 | `audit_logs` 12 列含哈希链；全仓无 `redact/mask/scrub` |
| 04 | 三层幂等 | **两层半**：工具层✅ 循环层✅（`DUP_THRESHOLD=2`）**请求层❌** | ⚠️ 部分 | `agent/loop_guard.py:46`；无 `Idempotency-Key` 消费 |
| 05 | 长任务状态机+检查点 | ✅ **已落地，质量高** | ✅ 满足 | `agent/orch_checkpoint.py` 336 行；`orch_checkpoints` 表在 |
| 06 | 重试分类（可试/不可试） | ✅ **已落地** | ✅ 满足 | `llm/__init__.py:471` 参数类 4xx 不重试 |
| 07 | 模型路由三步走 | **模块在，生产零调用** | ❌ 空转 | `llm/__init__.py:714-722` 自述"传参处只有一个测试文件" |
| 08 | 成本/预算闸 | **有统计、无闸门** | ⚠️ 部分 | `llm_usage_stats.estimated_cost` 在；`total_time_budget_s` 自述死配置 |
| 09 | 价值度量（效率/质量/采纳率/ROI） | **全缺** | ❌ 缺 | `grep roi/adoption/留存/dau` 零命中 |
| 10 | 四层系统（接入/编排/工具/可靠） | **三层齐、可靠层缺熔断与资源隔离** | ⚠️ 部分 | 有 `job_queue`/`orch_supervisor`，无熔断器 |

---

## 2. 🔴 P0 · Agent 工具链零权限门（安全缺口）

### 实测

```
grep -rn "require_permission" --include=*.py routers/ | wc -l   →  51 处，覆盖 11 个路由文件
grep -rn "require_permission" --include=*.py agent/              →  0 处
```

Agent 侧唯一沾身份的是 `audit_user(user)`（`agent/pipeline_parts/execute.py:502`、`stream.py:1009/1094/1886`）——
**那是"记录谁干的"，不是"拦住谁不许干"**。

`roles.permissions` 矩阵其实早已建好（实测 3 个正式角色各含 9–10 个 domain），
`core/deps.py:94require_permission` 也现成 —— **缺的不是权限体系，是把装饰器挂到工具执行入口**。

### 为什么这条是 P0

文章的原话是"**权限必须在工具调用和资源访问的边界上，由确定性的系统强制执行**"。
当前形态是：用户提一句话 → LLM 选工具 → `_exec_tool_call()`（`tools.py:280`）**直接执行，无任何权限判定**。
⇒只要Prompt 注入成功，模型即可调用**该用户本来无权调用的写类工具**。

而平台的写类工具是**真写库**的（`tool_call_logs` 578 行、`graph_edit_logs` 190 行、
`knowledge_commits` 414 行皆为实测值）。

>对照 `sparql.py:80` 的既有注释——那里已自认"鉴权依赖可伪造的 X-User-Id（P0 鉴权落地前）"。
> 即：**HTTP 层的鉴权缺口被显式记录在案，但 Agent 工具层的同类缺口连记录都没有。**

### 建议改法（4 行量级）

在 `_exec_tool_call()` 入口加一道门，复用现成 `require_permission`：

```
① 在 roles.permissions 矩阵补 `agent_tool:{write|read}` 两个 op（矩阵本就支持 JSON 扩展）
② _exec_tool_call(name, args) 增加 user 参数，取 _tool_side_effect(name) 判读写
③ 写类工具按 agent_tools.tool_type → 映射到 domain，查权限；缺权则返回结构化拒绝
④ 让 LLM 把"无权限"如实转述给用户（与文章"Agent 只能老老实实告诉员工你没有权限"一致）
```

---

## 3. 🟠 P1 · 智能路由生产零调用（成本缺口）

### 实测

`llm/__init__.py:714-722` **自述**（原文，非我的推断）：

> `LLMClient.chat()` 只有调用方显式传 `route_tags` 时才会走 `route()`，
> 而全仓生产代码里 `route_tags` 的**传参处只有一个**：`tests/manual_verify/verify_llm_route_d10.py`

实测按模型分布（`llm_usage_stats` 5,342 行）：

| 模型 | 调用数 | 估算成本(USD) |
|---|---|---|
| `deepseek-v4-flash` | 3,209 | 2.0618 |
| `deepseek-chat` | 2,133 | 0.8995 |

→ **全部走默认 provider，路由表priority/tags/budget 从未参与过选模型。**
这正是文章第 8 条要治的病："**所有任务不分青红皂白都路由到最强模型**"。

### 注意一个反直觉点

此处`deepseek-v4-flash` 占了 60%—— 说明**实际上已经在用便宜模型**，
所以"路由缺失"当前**没有造成明显浪费**。真正的损失是：
① 简单任务可能撞上高配模型（取决于默认 provider 是谁）；
② **将来接入更贵的模型（Claude/Opus 级）时，这层保护完全不存在** ⇒ 成本会线性放大。

### 建议

给编排链路与摘要/提取类环节传 `route_tags`（如 `["chinese","extract"]`）。
文章提醒的坑本项目同样适用：**切模型必须联动上下文窗口/压缩阈值/检索分块**，
本仓 `core/config.py` 已有大量按 `context_window` 联动的先例可参照。

---

## 4. 🟠 P1 · 熔断器完全缺失（可靠性缺口）

```
grep -rn -i "circuit" --include=*.py .（排除 .venv/backups）  →  仅命中 dup_short_circuits（同名不同义）
```

文章把"**熔断**"与"状态持久化、重试队列、监控告警、心跳检测、资源隔离"并列为可靠层的 5 件事。
本仓有4.5 件：

| 文章可靠层 | 本仓对应 | 状态 |
|---|---|---|
| 状态持久化 | `orch_checkpoint.py` + `orch_checkpoints` 表 | ✅ |
| 重试队列 | `llm/__init__.py:471` 分类退避 | ✅ |
| 监控告警 | `core/alert_evaluator.py` | ✅ |
| 心跳检测 | `resume_stale_s=1800` + `LeaseKeeper` | ✅ |
| **熔断** | — | ❌ **零** |
| 资源隔离 | `worker_count=1`（SQLite 单写者约束，属被动约束非隔离） | ⚠️ |

**现存的"半熔断"只有一处**：`knowledge_pipeline/embedder.py:95-107`
识别 `403/429 + quota/insufficient` → 封锁 600s。这是**针对 embedding 配额的单点熔断**，
不覆盖 LLM 调用、工具执行、MCP 调用。

**风险场景**（文章第 7 条）：provider 返回 429 时，本仓行为是"最多 3 次指数退避重试"，
**没有"连续失败 N 次后停止打这个下游"的闸** ⇒ 上游持续 429 时会持续消耗退避时间与调用配额。

### 建议

在 `llm/__init__.py` 的 provider 调用外层加 `CircuitBreaker`（三态 CLOSED/OPEN/HALF_OPEN）：
连续 N 次可重试失败 → OPEN 若干秒 → 期间直接走回退；`embedder.py:95-107` 的现有实现可作范式复用。

---

## 5. 🟠 P1 · 成本闸有统计无执行器

### 实测

**统计侧齐备**（`llm_usage_stats` 实测 12 列）：
`retry_count` / `fallback_used` / `fallback_provider_id` / `estimated_cost` / `finish_reason` /
`reasoning_tokens` / `prompt_cache_hit_tokens` / `latency_ms` / trace 三列。
实测 `estimated_cost>0` 有 5,066 行、合计 **$2.9612** —— **记账是准的**。

**闸门侧全缺**：
- `grep -E "per_user_budget|cost.*user_id|dept.*cost"` → **零命中**（文章要求"按用户、按部门统计并超预算自动降级"）
- `orchestration.total_time_budget_s = 600` —— `core/config.py:140` **自述"未接线（死配置）"**，
  `:886` 的 CONFIG_SCHEMA 也自己标了"⚠️ 未接线"。真实兜底是 `subtask_timeout_s`。

⇒ 即便把 `total_time_budget_s` 改成 3600，**也不会改变任何行为**（这正是MEMORY 里"改配置模板 ≠ 改生效值"的同类陷阱，此处工程已自我标注）。

### 建议

① 给 `total_time_budget_s` 接上调度处 deadline 检查（配置里已写明该怎么接）；
② 加按用户/日/月成本阈值，超限自动切低成本 provider（复用第3 条的 `route_tags` 通路，一处改动两处受益）。

---

## 6. 🟡 P2 · 其余三条

### 6.1 幂等：缺请求层（文章第 5 条三层防护）

| 文章层级 | 本仓| 证据 |
|---|---|---|
| 请求层（幂等键 + 时间窗） | ❌ **缺** | 全仓无 `Idempotency-Key` 消费；`core/job_queue.py:20` 自称"用户手抖点两下的**唯一防线**（对标 Temporal idempotency key）" |
| 工具层（天然幂等） | ✅ | `orch_checkpoint.py:218` "done 一律复用绝不重跑"；`utils.py`多处"幂等"注释 |
| 循环层（重复签名告警） | ✅ **超出文章要求** | `loop_guard.py:46DUP_THRESHOLD=2`（文章建议 3），且 `normalize_args` 做了 key 排序防绕过 |

**风险点**：`job_queue` 把自己的幂等键当成"唯一防线"，
但**HTTP 端点直调（不经队列的同步接口）不在这个防线上**。
建议：中间件层支持 `Idempotency-Key` 头，对写类端点做"键 + 保留窗"去重。

### 6.2 审计：五问答了四问，且工具调用不在链内

`audit_logs` 实测 12 列，**质量高于文章要求**（多了 `prev_hash`/`hash` **哈希链** + `branch` + UA + request_id）。
`core/audit.py:186 verify_chain()` 还有链完整性校验。

**两处不足**：
1. **缺"为什么"**：文章要求记录"为什么做"，`detail` 是自由文本，**没有结构化的 `reason`/`intent_ref` 字段**。
2. **工具调用不在审计链内**：`audit_logs.event_type` 分布实测 Top 20 里**没有任何 tool 类事件**
   （`conversation_create`252 / `conversation_delete`234 / `llm_chat`195 / `graph_node_update`154 …），
   工具调用走的是**另一张表** `tool_call_logs`（578 行）—— 而该表**没有 user 字段**：

   ```
   PRAGMA table_info(tool_call_logs) →
   id, intent, agent_name, tool_name, tool_type, arguments, result, ok, latency_ms, conversation_id, created_at, call_kind
   ```
   ⇒ 文章那句"**业务方追着问到底是谁、通过什么方式改的**"，在本平台**查不到答案**（能查到是哪个会话，但不是哪个用户）。

3. **零脱敏**：`grep redact|mask|脱敏|scrub` 在 `core/audit.py` / `agent/` 零命中。
   文章的踩坑提示原文："**审计日志里明文记录了 API Key，导致这份本来用来保护安全的日志反而成了新的泄露源**"。
   当前 `arguments` 字段**原样入库**（`tools.py:705`），工具入参里若带 token/密钥即落库。

### 6.3 常驻/桌面层：不适用，但"可见界面层"缺

文章第 3 条讲终端 Agent 升级桌面 Agent 的五层跃迁。本平台是 Web形态，交互层/工具层不适用，
但其中**运行层与可见界面层**仍有对应缺口：

| 文章层 | 本仓现状 |
|---|---|
| 运行层（常驻、进程管理、心跳） | ⚠️ 有编排心跳（`resume_stale_s`）但**无进程守护**；`auto_resume_enabled=False` 默认关（`main.py:101` 会打印"守护未启用"） |
| 可见界面层（实时知道 Agent 在干什么） | ✅ 已有 SSE 流式进度（`stream.py`） |

**`auto_resume_enabled=False` 这个默认值本身是对的**——代码注释写得很好：
自动重投会"真的调 LLM 烧额度"，且中断前可能已落一半副作用。
但它意味着**"进程重启后自动恢复"这条能力在生产上是关闭的**，需要人工在监控页触发。

---

## 7. 建议执行顺序（按 收益÷成本）

| 序 | 项 | 成本 | 收益 | 理由 |
|---|---|---|---|---|
| **1** | Agent 工具链接权限门 | 低（4 行 + 矩阵加 2 op） | **高（安全）** | 唯一的安全缺口，且权限体系已现成 |
| **2** | `tool_call_logs` 加 `user_name` + 补工具类审计事件 | 低 | 高（合规） | 文章"审计五问"的直接缺口 |
| **3** | 审计 `arguments` 脱敏 | 低 | 高（安全） | 防审计日志成泄露源 |
| **4** | LLM 侧熔断器 | 中 | 高（可靠性） | 复用 `embedder.py:95-107` 范式 |
| **5** | 接 `total_time_budget_s` + 按用户成本闸 | 中 | 中（成本） | 配置已存在，接线即可 |
| **6** | 编排链路传 `route_tags` | 低 | 中（成本） | 当前浪费不大，但**是未来接贵模型的前置** |
| **7** | 写类端点支持 `Idempotency-Key` | 中 | 中 | 补齐三层幂等的请求层 |

**先做 1–3**：都是低成本的**安全/合规**项，且改动集中、不触碰业务语义。

---

## 8. 明确不做 / 待决

- **桌面 Agent 跃迁（文章第 3 条）**：本平台是 Web 服务形态，该条不适用。不做。
- **统一服务账号改用户令牌**：平台**已经做对了方向**——`core/deps.py:131` 明确堵住了
  "token 无效回落 X-User-Id"，且 `auth.trust_user_id_header` 有显式开关。
  残留问题只是 `trust_user_id_header` 默认 `True`（兼容期）+ `enforce_login=False`，
  这是**上线前必须翻的开关**，不是设计缺陷。
- **待米爸拍板**：`agent_tools` 的写类工具是否要按"角色×工具类型"细粒度授权，
  还是先按"读/写两类粗粒度"上线（后者 4 行，前者需设计矩阵）。

---

## 附 · 可复现命令

```bash
export PATH="/c/Users/gefei/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"
R="C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system"; cd "$R"; PY="$R/.venv/Scripts/python.exe"

# P0 权限门覆盖面
grep -rn "require_permission" --include=*.py routers/ | wc -l        # 51
grep -rn "require_permission" --include=*.py agent/  | wc -l        # 0← 缺口

# P1 路由零调用（自述）
sed -n '710,725p' llm/__init__.py
grep -rn "route_tags" --include=*.py . | grep -v .venv | grep -v tmp # 仅测试文件

# P1 熔断器
grep -rn -i "circuit" --include=*.py . | grep -v .venv | grep -v backups  # 空

# P1 死配置
grep -rn "total_time_budget_s" --include=*.py . | grep -v .venv | grep -v tmp  # 仅 config.py

# P2 工具日志无user 字段
"$PY" -X utf8 -c "import sqlite3;c=sqlite3.connect('file:mbse.db?mode=ro',uri=True);
print([x[1] for x in c.execute('PRAGMA table_info(tool_call_logs)')]);
print(list(c.execute('SELECT event_type,COUNT(*) FROM audit_logs GROUP BY 1 ORDER BY 2 DESC LIMIT 8')))"

# 已落地能力的规模（别当缺口报）
wc -l agent/orch_checkpoint.py agent/loop_guard.py# 检查点 336 行 + 护栏
"$PY" -X utf8 -c "import sqlite3;c=sqlite3.connect('file:mbse.db?mode=ro',uri=True);
print('orch_checkpoints',c.execute('SELECT COUNT(*) FROM orch_checkpoints').fetchone()[0]);
print('llm_usage_stats',c.execute('SELECT COUNT(*) FROM llm_usage_stats').fetchone()[0]);
print('cost',c.execute('SELECT ROUND(SUM(estimated_cost),4) FROM llm_usage_stats').fetchone()[0])"
```