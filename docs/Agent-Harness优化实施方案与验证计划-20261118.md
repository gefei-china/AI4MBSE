# Agent Harness 短板 · 优化实施方案与验证计划（2026-11-18）

> 上位文件：`docs/Agent-Harness-13步对标核查-20261118.md`（13 步对标，4 已实现 / 9 部分）
> 本文职责：把核查缺口转成**可派工计划**——每项给 `改动落点(文件:行) + 验收判据 + 前置依赖 + 风险 + 回滚`。
> 纪律（基线 §1k/1m/1p）：① 改动面先量化，不拍脑袋；② 索引必须建在补列之后；③ op 常量不得写成 `"domain:op"`；④ 加门必须与授权同批；⑤ 写完报告回扫摘要与交叉引用。
> 状态：**方案文档，尚未实施；除本文与台账外不改生产代码**。所有引用行号已机器抽验（见附录 A）。

---

## 0. 决策摘要（米爸要的是拍板，不是清单）

| 决策 | 事项 | 推荐方案 | 关键量化对比 | 结论 |
|---|---|---|---|---|
| **D1** | 认证运行态闭合 | **B：保留匿名兼容 + 对高危端点单独加门 + 上线 checklist**，不动全局 `enforce_login` | A 全局强制：650 个 `Depends(db_session)` 端点中 313 个已挂 `current_user`，剩 ~337 个要逐一加鉴权，且 87 处 `X-User-Id` 引用全要改；B 精准加门：SPARQL 5 端点 + 5 个绕过工具 = **10 个落点** | ✅ B，A 不做（见"明确不做"） |
| **D2** | harness 独立验证成主路径 | **B：安全侧默认值 + 验收规格质量前置整治**，非直接翻 `enforce` | 直接翻默认：761 条 verify 中 44%（333 条）曾被误当工具名，110 条 manual 直接 `ok=True`，翻默认会大批假拦；B 先修规格再翻：`harness_decisions` enforce 仅 12/296，整改目标是 enforce 成为主路径 | ✅ B，分两批 |
| **D3** | 跨层 trace_id | **A：给 7 张无 trace_id 的表补同一列 + 索引**，不回填历史 | A 补列：7 表 × 1 列 + 7 索引，新数据即串起全链；B 侧车映射表：多一次 JOIN、无既有 `begin_trace` 同源保证；历史回填 2.9 万行 llm + 7 千行 audit 无可靠关联键 | ✅ A，只对新数据，不回填 |
| **D4** | 受控执行边界 | **A：网络域名白名单 + 文件工作区根约束**，沙箱只扩到 `web_fetch` | 当前 `web_tools.py` urllib 直连任意 URL、无 SSRF 判定；`file_tools.py:89` 只做 abspath 无根约束；A 两处纯函数闸门 ≈ 2 文件；B 全工具进沙箱要改 file/db/MCP/HTTP 五类执行器，风险高 | ✅ A，先网络后文件 |
| **D5** | HITL 硬拒与自批 | **B：加 `deny` 档 + 审批人≠执行人校验（仅 destructive）**，不做全局四眼 | A 全局四眼：3 个角色都持 `hil:approve` + `agent_tool:write`，要新建审批角色并改全部流程；B 最小闭环：`LEVELS` 加一档 + 一处校验，destructive 4 个工具受影响（真库 112 active 中 4 个） | ✅ B |

**总批次**：B0 基线固化 → B1 可观测（D3）→ B2 权限与执行（D1+D4+D5）→ B3 harness 主路径（D2）→ B4 上线闸。B0/B1 零行为变更，B2 起有用户可见变化。

---

## 1. 实施基线与纪律

| 项 | 值 / 口径 |
|---|---|
| 仓库 | `C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system`，分支 `master`，HEAD `e192ebd` |
| 工作区现状 | 有 1 份未提交核查文档 + 1 份门禁刷新快照（`eval_freshness_latest.json`）；**动手前先确认这两项的归属**，不混进本方案提交 |
| Python | `<repo>\.venv\Scripts\python.exe`（绝对路径） |
| 数据库 | `<repo>\mbse.db`（真库）；只读连接 `file:...?mode=ro`；写库只在备份副本上做 PoC |
| 换行 | 本仓 `core.autocrlf=true`，改动文件全部 CRLF；批量补丁用字节级 + 命中计数断言 |
| 数字口径 | 本文所有数字为 2026-11-18 实测；旧文档中的 CAP=3000/4000/1200、24744 未兜住、25 read 无门**已过期，禁止引用** |
| 提交纪律 | 每批一个提交；小而频；`check-ignore` 反查；`-F` 提交信息；不 `--no-verify` |
| 验证固定动作 | `git status` 干净 → `py_compile` → 相关 `tools/verify/*.py` → 真库副本 PoC → 三信号确认服务（若涉及重启） |

---

## 2. 批次依赖图

```
B0 基线固化（零行为变更）
 ├─ B0-1 门禁口径过期修正（verify_token_budget / verify_s4_prompt / verify_orch_summary_budget）
 ├─ B0-2 config schema 补登记（total_budget_tokens / total_budget_guard）
 └─ B0-3 核查文档与快照先提交或明确归属
        │
        ▼
B1 可观测（D3，零行为变更，纯补列）
 ├─ B1-1 七表补 trace_id 列（索引在补列之后）
 ├─ B1-2 写入点接 current_trace_id()
 └─ B1-3 trace 覆盖率门禁
        │
        ▼
B2 权限与执行（D1+D4+D5，用户可见）
 ├─ B2-1 SPARQL 5 端点挂鉴权  ─┐
 ├─ B2-2 5 个绕过工具补门      ─┼─ 同批：加门 + 授权（避免全员 403）
 ├─ B2-3 web_fetch 域名白名单  │
 ├─ B2-4 file 工作区根约束     │
 └─ B2-5 HIL deny 档 + 自批闸  ─┘
        │
        ▼
B3 harness 主路径（D2，依赖 B1 trace + B2 权限）
 ├─ B3-1 verify 规格质量整治（44% 误当工具名 → 降到 ≤5%）
 ├─ B3-2 manual 验收不得直接 ok=True
 ├─ B3-3 安全侧默认值（harness_mode=enforce 进 DEFAULT_CONFIG）
 └─ B3-4 独立验证覆盖率门禁（enforce 成为主路径）
        │
        ▼
B4 上线闸
 └─ 上线 checklist：enforce_login 决策回填 / 认证方案 / 全局 trace 覆盖率 ≥95%
```

**关键路径**：B0 → B1 → B3（B2 与 B1 可并行，但 B3 必须等两者完成）。

---

## 3. 逐项实施方案

### B0-1 门禁口径过期修正（P1，零行为变更）

- **落点**：`tools/verify/verify_token_budget.py:140/142`、`tools/verify/verify_s4_prompt.py:74`、`tools/verify/verify_orch_summary_budget.py:264/266`
- **现状**：断言旧 CAP 3000/4000/1200/24000；生产已升 8000/48000（`agent/utils.py` 对应常量 + `core/config.py:295`）
- **动作**：把断言值改为从 `core.config` 读取（单一真源），不再写死字面量；保持变异自证有效
- **验收 DoD**：三个门禁在现行配置下全绿；变异自证（把配置临时改旧值）能判红
- **风险**：低（只改门禁）；**回滚**：单文件 `git checkout --`
- **依赖**：无，可立即做

### B0-2 config schema 补登记（P2，零行为变更）

- **落点**：`core/config.py` 的 `context` schema 段（约 :2184-2220，有 `total_budget_window_ratio` 但缺 `total_budget_tokens` / `total_budget_guard`），代码在 `history.py:1603/1684` 读取
- **动作**：DEFAULT_CONFIG 与 CONFIG_SCHEMA **两处同时补**（基线 §1k：漏一处则 override 抛"未知配置项"）
- **验收**：`verify_config_switches` PASS 18/0；通过 `/api/system/config` 能读能写这两键
- **风险**：低；**回滚**：单文件 revert

### B1-1 七表补 trace_id 列（P1，零行为变更）

- **落点**：`database/migrations/columns.py`（`_add` 定义 :136；既有 `_add("audit_logs",...)` :380、`_add("knowledge_commits",...)` :683-686 可作范式）
- **目标表**（本次实测无 trace_id 列，共 7 张）：`audit_logs`、`graph_edit_logs`、`knowledge_commits`、`harness_decisions`、`orch_run_spans`、`query_trace`、`hil_confirmations`
- **动作**：每表 `_add(t, "trace_id", "TEXT DEFAULT ''")`；**索引必须紧跟 `_add` 之后建立，不得放在建表段**（基线 §1m：老库启动会因 `no such column` 挂掉，`database/schema.py:1343/1790` 已有注释仍踩过）
- **验收**：
  - 备份副本上跑 `init_db()` 不报 `no such column`
  - `PRAGMA table_info` 7 表均有 trace_id
  - 新写入行 trace_id 非空率 ≥95%（7 天窗口）
- **风险**：中（迁移）；**回滚**：备份库 + 迁移幂等标记；**前置**：先备份 `mbse.db`（含 wal）

### B1-2 trace 写入点接线（P1，零行为变更）

- **落点**：复用 `core/audit.py:97 current_trace_id()` / `:169 begin_trace()`（已是单一真源，`_TRACE_KEYS` 在 :77）
- **非流式**：`agent/pipeline_parts/execute.py:97-98` 已 `begin_trace`
- **流式**：`agent/pipeline_parts/stream.py:4466-4478` 已 `begin_trace` + `bind_trace`；注意 :964/:3518 注释警告 `begin_trace(0)` 会新开链（一次编排碎成 N+1 条），不要在这些位置重复开链
- **写入**：`tool_call_logs` 落库 `tools.py:2708` 已有 trace_id；其余 6 表按各自 INSERT 点补 `current_trace_id()`（MCP 网关等无 begin_trace 入口用 `current_trace_id(fallback_request=True)`，见 `core/audit.py:106`）
- **验收**：真库副本上跑一次真实对话，7 表新行 trace_id 一致且等于 `llm_usage_stats.trace_id`
- **风险**：中（写点分散）；**回滚**：逐文件 revert

### B1-3 trace 覆盖率门禁（P1）

- **新增** `tools/verify/verify_trace_coverage.py`（tier=local，读真库）
- **判据**：T1 七表 trace_id 列存在；T2 7 天新行非空率 ≥95%；T3 llm 与 tool 共享 trace_id 数 >0；T4 变异自证（去掉某表写入 → 判红）
- **登记**：`tools/gates_manifest.json` 加条目（G5 覆盖完整，否则门禁自红）
- **验收**：新门禁全绿；`verify_gate_manifest` 全绿

### B2-1 SPARQL 五端点挂鉴权（P0）

- **落点**：`routers/sparql.py` — `/dependency`(:21)、`/sync`(:27)、`/query`(:46)、`/query`(:51)、`/update`(:115)
- **现状**：全部只有 `conn=Depends(db_session)`，无 `current_user`；语句白名单 `:78-84/:115-128` 只管"什么语句"，不管"谁能执行"
- **动作**：按操作分级挂门——query/dependency/sync 挂 `current_user`；`/update` 挂 `require_permission("graph_db","write")`（**op 常量必须是裸 `"write"`，不是 `"graph_db:write"`**，基线 §1l）；roles 矩阵先补 `graph_db:["read","write"]` 给现有三个角色（**加门与授权同批**，否则全员 403）
- **验收**：
  - 匿名请求 5 端点均 401
  - 设计师角色 read 通过、无 write 者 update 403
  - 白名单仍生效（DROP 被拒）
  - `verify_sparql*` 相关门禁全绿
- **风险**：**高**（可能打断现有匿名调用方）；**回滚**：路由装饰器逐条 revert + roles 矩阵 revert；**前置**：先查 7 天内 `/api/sparql/*` 调用者是否带身份

### B2-2 五个绕过工具补权限门（P0）

- **落点**：`agent/pipeline_parts/tools.py:1716`（read_attachment_section）、`:1737`（memory_fetch）、`:1769`（tool_result_fetch）、`:1796`（todo_write）、`:1816`（ask_user）——均在 RBAC 闸 `:1858/:1865` **之前** return
- **动作**：把权限判定提前到这 5 个早退分支之前，复用 `core/deps.py:284 check_tool_perm(user, tool_name, side_effect)`；对 read 类工具统一用 `agent_tool:read`
- **注意**：`ask_user` / `todo_write` 是"对话级能力"，若加门要确认所有角色都有 `agent_tool:read`（真库三角色都有，边缘角色 `hil=None` 需核）
- **验收**：
  - 无 `agent_tool:read` 的用户调这 5 个工具返回结构化 `permission_denied`
  - 现有 112 active 工具（read 58/write 50/destructive 4）全部经过权限闸，无早退
  - A/B：开关关闭时行为逐字节同改动前
- **风险**：中；**回滚**：配置开关 `_tool_perm_gate_disabled` 已有，可直接关

### B2-3 web_fetch 域名白名单（P1）

- **落点**：`web_tools.py` — `_get()`（约 :35-38）与 `WEB_TOOL_NAMES`（:28）
- **现状**：`urllib` 直连任意 URL，无 `allowlist/SSRF/私网 IP` 判定
- **动作**：加纯函数 `is_allowed_url(url, allowlist, denylist)`——拒绝私网段（127./10./172.16-31./192.168./169.254/::1）、拒绝非白名单域名；配置放 `web_search` 组（DEFAULT + SCHEMA 双登记）
- **验收**：
  - 单元：私网 URL / 非白名单域名 / 合法域名各断言
  - 变异：去掉私网判定 → 判红
  - 真机：`web_fetch("https://mp.weixin.qq.com/...")` 仍可用（本文抓取即真机证据）
- **风险**：中（可能挡住合法长尾域名）；**回滚**：配置开关默认放行

### B2-4 file 工具工作区根约束（P1）

- **落点**：`file_tools.py:89 _resolve()`（只 abspath）+ `:85-118 _critical_files()`（只保护平台自身文件）
- **动作**：加配置 `file_tools.allowed_roots`（默认空=不约束，保持兼容）；非空时 `_resolve` 必须落在根下
- **验收**：配置根后越界路径返回结构化错误；未配置时行为不变
- **风险**：中；**回滚**：配置默认空

### B2-5 HIL deny 档 + 自批闸（P1）

- **落点**：`services/hil_policy.py:70 LEVELS`（加 `"deny"`）、`:190 evaluate()`；`hil_service.py` 审批执行段；`static/js/mods/06-cards.js:298-312`（确认卡）
- **现状**：LEVELS 无 deny；真库 46 条确认单 level 只有 ""(10)/confirm(36)，approve 0；三角色都同时持 `hil:approve` 与 `agent_tool:write`；确认卡无前后值 / 执行人 / 审批人 / 风险等级
- **动作**：
  - LEVELS 加 `deny`（排最高），`evaluate` 返回 deny 时不入队、直接拒绝
  - destructive（真库 4 个工具）加"审批人 ≠ 发起人"校验；其余档位不变
  - 确认卡补四个字段（前后值 / 执行人 / 审批人 / 风险等级），后端已有 `decided_by` 列可消费
- **验收**：
  - 单元：deny 策略命中即拒绝；自批 destructive 被拒；他人审批通过
  - 真库副本：构造一条 destructive 确认单走完整流程
  - 前端：确认卡四字段可见（浏览器验证，本工程铁律 1）
- **风险**：中；**回滚**：polarity 加开关，默认关旧行为

### B3-1 verify 规格质量整治（P0，B3 前置）

- **落点**：`agent/harness_gate.py:58-72`（自述 761 条 verify 中 44% 被误当工具名）、`:74-88`（`READONLY_VERIFY_TOOLS` 16 个）、`:116-207`（`parse_verify_spec`）
- **动作**：把 verify 从"自由文本"收口为结构化 `{tool, args, assertion}`，旧文本按白名单+关键词解析，**解析失败一律落 manual 而非当工具名执行**
- **验收**：真库 1216 条 verify 重解析，误当工具名从 44% 降到 ≤5%；manual 110 条逐条有明确标记
- **风险**：中；**回滚**：解析函数独立可切回旧逻辑

### B3-2 manual 验收不得直接 ok=True（P0）

- **落点**：`agent/pipeline_parts/autopilot.py:434-438`
- **动作**：manual 类验收不得自动 `ok=True`，改为如实落 `deferred` 并在汇总正文告知"N 项待人工核验"
- **验收**：真库副本构造 manual 待办，收口正文出现如实告知且不报 full
- **风险**：中（用户可见变化）；**回滚**：配置开关

### B3-3 harness 安全侧默认值（P0）

- **落点**：`core/config.py:2104-2105`（`harness_enabled` 默认 False、`harness_mode` 默认 ""），生效靠 `~/.workbuddy/mbse_config.json` 的 `agent.harness_mode=enforce`（API 实测）
- **动作**：DEFAULT_CONFIG 改 `harness_mode="enforce"`；**必须在 B3-1/B3-2 完成后做**，否则大批假拦
- **验收**：干净库（无 override）启动即为 enforce；`verify_harness_real_effect` A/B 全绿
- **风险**：**高**（全局行为变更）；**回滚**：一行配置回退

### B3-4 独立验证覆盖率门禁（P0）

- **新增/改造** `tools/verify/verify_harness_real_effect.py`（已在 manifest :943）或新增 `verify_harness_coverage.py`
- **判据**：`harness_decisions` 中 enforce+orch_enforce 占比 ≥80%（当前 13/296=4.4%）；`would_block=1` 在 enforce 下 >0（当前 0）；变异自证
- **验收**：新门禁真库全绿；达标前持续判红
- **风险**：低；**回滚**：门禁可单独 revert

### B4 上线闸（P1）

- **动作**：把 D1 的认证决策固化为 checklist；`/api/system/config` 实测 `enforce_login=False`、`trust_user_id_header=True`；users 无口令列（9 列实测）
- **验收**：checklist 每条可勾；trace 覆盖率 ≥95%；harness enforce ≥80%
- **风险**：—（这是门禁不是改动）

---

## 4. 专项 PoC 步骤（动手前在备份副本验证）

| # | 步骤 | 验证目标 | 通过判据 |
|---|---|---|---|
| S1 | `sqlite3 backup()` 复制 mbse.db（含 wal）到 temp | 写库不碰生产 | 副本可打开、表数一致 |
| S2 | 在副本执行 B1 的 7 表补列 + 索引 | 老库迁移不挂 | `init_db()` 无 `no such column` |
| S3 | 副本上跑一次 `_archive_artifacts` 与本方案新增写入点 | trace_id 同源 | 7 表新行 trace_id 相等且非空 |
| S4 | 沙箱构造 5 个绕过工具调用（无权限用户） | 补门生效 | 5 个全部 `permission_denied` |
| S5 | 沙箱构造 SPARQL 匿名请求 | 挂鉴权生效 | 5 端点 401，白名单仍拒 DROP |
| S6 | 副本上跑 B3-1 的 1216 条 verify 重解析 | 规格质量 | 误当工具名 ≤5% |
| S7 | 全量回归 8+ 相关门禁 | 无回归 | 全绿 |

---

## 5. 验收门禁清单（DoD）

| 批次 | 必跑门禁 | 新增门禁 | 通过线 |
|---|---|---|---|
| B0 | verify_config_switches / verify_gate_manifest / verify_token_budget / verify_s4_prompt / verify_orch_summary_budget | — | 全绿 |
| B1 | verify_config_switches / verify_gate_manifest | **verify_trace_coverage** | T1-T4 全绿 |
| B2 | verify_harness_verify_safety / verify_sparql* / verify_explicit_required_guard / verify_artifact_naming | B2 各单元断言 | 全绿 + 前端确认卡浏览器验证 |
| B3 | verify_harness_real_effect / verify_harness_gate_p02 / verify_p1_prerequisite | **verify_harness_coverage** | enforce ≥80%、would_block>0 |
| B4 | 全部 local 门禁 + 上线 checklist | — | 全绿 |

**统一收尾动作**：`git status --porcelain` 干净 → 每批一个提交 → 若改后端则按 AGENTS.md 坑 31 三信号确认服务重启（PID / StartTime > 代码 mtime / 新代码专属功能证据）。

---

## 6. 风险登记册

| ID | 风险 | 等级 | 触发条件 | 缓解 | 回滚 |
|---|---|---|---|---|---|
| R1 | 加权限门后全员 403 | 高 | 只加门不同批授权（基线 §1l） | 加门与 roles 矩阵同批；先查现有调用者身份 | roles + 路由同批 revert |
| R2 | 老库迁移启动失败 | 高 | 索引建在补列之前（基线 §1m） | 索引紧跟 `_add`；副本先 PoC | 备份库恢复 |
| R3 | harness 翻默认后大批假拦 | 高 | 未先整治 verify 规格就翻 | B3-1/B3-2 为硬前置 | 一行配置回退 |
| R4 | op 常量写成 `"domain:op"` 恒判 False | 中 | 新 op 命名失误（基线 §1l） | 断言**判定结果**而非"授权已写入" | 代码 revert |
| R5 | 网络白名单挡住合法域名 | 中 | 白名单过窄 | 默认空=放行；灰度先 observe 再 enforce | 配置清空 |
| R6 | trace 补列后写入点遗漏 | 中 | 写点分散在 7 表 | T2 覆盖率门禁 ≥95% 兜底 | 逐表 revert |
| R7 | 前端确认卡加了字段但渲染失效 | 中 | 全局函数/内联 onclick（铁律 1） | 浏览器真机验证，不看代码 | 前端单文件 revert |
| R8 | 门禁写死新值再次过期 | 低 | 未来再改 CAP | B0-1 改为从 config 读取单一真源 | 门禁 revert |

---

## 7. 明确不做的事（避免范围蔓延）

1. **不做全局 `enforce_login=True`**：650 个 `Depends(db_session)` 中 337 个无 `current_user`，87 处 `X-User-Id` 引用；这是产品级认证方案决策，不是本轮工程项。
2. **不回填历史 trace_id**：llm 2.9 万行 + audit 7 千行无可靠关联键，硬回填会造出假关联；只对新数据生效。
3. **不把全工具塞进沙箱**：file/db/MCP/HTTP 五类执行器改造风险远高于"加网络白名单 + 路径约束"的收益。
4. **不做 HIL 全局四眼**：3 个角色都持 `hil:approve` + `agent_tool:write`，全局职责分离要新建审批角色并改全部流程；只对 destructive 做。
5. **不重跑 live 基线**（约 3h 真实 LLM）：G08 判据已由产品修复且 offline 12/12、真库副本 A/B 已证可达，重跑只刷新记录。
6. **不改数据集 / dataset_id**：`eb245949e0d84ed1` 保持不变，避免基线作废。
7. **不动 SysML v2 校验器、知识工程链路、审计哈希链**：这些是文章未覆盖但平台已有的能力，不在本轮优化范围。

---

## 8. 关键落点索引

| 主题 | 文件:行 | 说明 |
|---|---|---|
| 认证默认 | `core/config.py:198/207` | `enforce_login=False`、`trust_user_id_header=True` |
| SPARQL 端点 | `routers/sparql.py:21/27/46/51/115` | 5 个无鉴权端点 |
| 5 绕过工具 | `agent/pipeline_parts/tools.py:1716/1737/1769/1796/1816` | RBAC 闸 `:1858` 之前 return |
| 权限判定 | `core/deps.py:252/284` | `has_perm` / `check_tool_perm` |
| trace 单一真源 | `core/audit.py:77/97/169` | `_TRACE_KEYS` / `current_trace_id` / `begin_trace` |
| trace 已覆盖表 | llm_usage_stats / tool_call_logs / context_audit | 39% / 62% / 100% |
| trace 待补表 | audit_logs / graph_edit_logs / knowledge_commits / harness_decisions / orch_run_spans / query_trace / hil_confirmations | 7 张 |
| 迁移范式 | `database/migrations/columns.py:136/380/683-686` | `_add` 定义与既有调用 |
| 索引纪律 | `database/schema.py:1343/1790` | 索引必须在补列之后 |
| web 出口 | `web_tools.py:28/35` | `WEB_TOOL_NAMES` / `_get` urllib 直连 |
| 文件根约束 | `file_tools.py:89/85-118` | `_resolve` / `_critical_files` |
| HIL 档位 | `services/hil_policy.py:70/190` | `LEVELS` / `evaluate` |
| 确认卡 | `static/js/mods/06-cards.js:298-312` | 展示字段与批准按钮 |
| harness 默认 | `core/config.py:2104-2105` | `harness_enabled=False` / `harness_mode=""` |
| verify 解析 | `agent/harness_gate.py:58-72/74-88/116-207` | 44% 误当工具名的根因 |
| manual 放行 | `agent/pipeline_parts/autopilot.py:434-438` | `ok=True` 直接放行 |
| 门禁旧值 | `verify_token_budget.py:140/142`、`verify_s4_prompt.py:74`、`verify_orch_summary_budget.py:264/266` | 写死旧 CAP |

---

## 附录 A · 行号抽验记录

以下引用已用 `sed -n 'Np' | grep` 逐项抽验（2026-11-18）：`tools.py:1716/1737/1769/1796/1816/1858/1865`、`sparql.py:21/27/46/51/115`、`hil_policy.py:70/190/259`、`config.py:198/207/2104/2105` —— 全部命中预期符号，无偏移。其余行号来自本次只读 grep 输出，若实施时发现漂移以**符号名**为准重新定位（本工程行号会随每次改动失效，AGENTS.md 铁律 2）。

> 本方案与配套台账均为新增未跟踪文件，尚未提交；实施需另行开工单，本文不授权改生产代码。
