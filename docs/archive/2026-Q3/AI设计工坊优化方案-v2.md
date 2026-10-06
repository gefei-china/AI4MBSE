# AI 设计工坊优化设计方案 v2

> 版本：v2.0 · 2026-08-17
> 范围：① 流程编排入口隐藏（含跨模块关联逻辑）② Agent 主/子团队管理 ③ Skill/MCP/工具插件模式（公共市场 + 私人空间）
> 依据：对现有 `static/index.html` / `routers/studio.py` / `database/schema.py` / `repositories/agent_repo.py` 代码审计 + 行业通用方案（Dify Plugin / Coze 插件商店 / CrewAI 多 Agent 团队 / VS Code & WordPress 插件生态）

---

## 0. 设计总原则

1. **前端隐藏、后端保留**：隐藏的是"入口与引导"，不删除后端 API 与执行引擎。AI 建模会话内的 Planner 自动编排 / 多 Agent 委派 / 流程执行能力继续由引擎支撑，只是不再暴露可视化画布入口。
2. **数据全兼容**：新增列全部走幂等迁移（`database/migrations.py`），种子与既有数据不回滚；旧 API 响应字段只增不减。
3. **校验双端**：主/子 Agent 规则、市场发布规则在后端强制校验（400 拒绝），前端表单同步拦截与提示。
4. **延续既有交互范式**：新增 UI 复用现有卡片网格 / 表单弹窗 / subtab 切换风格，不引入新设计语言。

---

## 1. 需求一：流程编排入口隐藏

### 1.1 现状入口与关联逻辑清单（审计结果）

| # | 位置 | 说明 | 处置 |
|---|------|------|------|
| 1 | 侧边栏 `L659` `<a data-page="studio" data-tab="st-flow">🕸 流程编排` | 主导航一级入口 | **删除** |
| 2 | 工坊页 `st-flow` 子页 `L1237-1267` | 画布 / 节点托盘 / AI 生成 / 运行 / 异步 / 监控 | **DOM 保留但不可达**（入口删除后自然隐藏；`st-flow` 数据面板保留供内部使用） |
| 3 | AI 建模工具栏 `L740` `#quick-flow`「⚡ 工作流」下拉 | 会话内直接运行工作流（`quickFlowGo` → `runFlowFromChat`） | **删除**（含 `quickFlowGo`、`loadQuickBar` 的 flows 分支） |
| 4 | AI 建模富卡片 `L3182-3199` | 自动编排结果「🕸 在画布中打开」/「💾 另存为流程」按钮 + 沉淀提示文案 | **删除按钮与文案**（`saveOrchFlowAsFlow`、`loadFlowFromChat` 不再被触发） |
| 5 | AI 建模附件匹配卡片 `L4685-4699` | 附件解析后「▶ 运行此流程」「载入编排」按钮 | **删除**（会话内 flow-run 入口一并移除） |
| 6 | `ST_TAB_TITLES` `L2003` | `st-flow` 标题映射 | **移除条目** |
| 7 | 运行历史 / 运行详情弹窗 `L11577` | 「🗂 任务队列」按钮（Planner 任务视图） | **保留**（属于运行结果可观测性，非编排入口；若需彻底隐藏可一并移除，见确认点） |

### 1.2 隐藏后保留的能力（后端不动）

- `/api/studio/agent-flows` CRUD、`/api/studio/flow-runs/*`、`/api/studio/planner/*`、`/api/studio/ai/generate-flow`、`/api/studio/ai/refine-flow` 等 API **全部保留**；
- AI 建模会话内 Planner 自动编排（`AgentPipeline` 分解任务 → 多 Agent 执行 → 汇总）继续运行，仅去掉"沉淀为可视化工作流"的前端引导；
- 运行详情中的步骤 / 检查点 / 事件订阅 / 时间旅行回放能力保留（由运行历史面板提供，非画布入口）。

### 1.3 交互影响与回退

- 侧边栏「AI 设计工坊」分组仅剩：Skill 模板库 / 工具与 MCP / Agent 管理 / 插件市场（新增，见 §3）；
- 如后续需要恢复，只需恢复导航链接与 `st-flow` tab 声明，数据无任何丢失。

---

## 2. 需求二：Agent 管理 —— 主 / 子 Agent 团队

### 2.1 行业方案参考

- **CrewAI Crew**：`manager agent + worker agents`，Manager 负责任务规划与委派，成员只执行；
- **AutoGen GroupChat / LangGraph Supervisor**：单一 Supervisor 分发，Worker 不得再嵌套 Worker；
- **Coze Bot / Dify**：一个"应用/Bot"作为入口（主），内部可挂多个技能型子 Agent 协作。

共性结论：**两级团队结构**（主 Agent = 团队负责人，子 Agent = 团队成员），成员可被多个团队复用；禁止递归嵌套。

### 2.2 数据模型

```sql
-- ① agents 表新增列（幂等迁移 _migrate_agent_team）
ALTER TABLE agents ADD COLUMN agent_role TEXT DEFAULT 'sub';   -- main | sub（默认 sub，兼容存量）
-- ② 团队关系表（多对多：一个子 Agent 可属多个团队）
CREATE TABLE IF NOT EXISTS agent_team_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    main_agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    sub_agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    enabled        INTEGER DEFAULT 1,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(main_agent_id, sub_agent_id)
);
CREATE INDEX IF NOT EXISTS ux_atm_main ON agent_team_members(main_agent_id);
```

### 2.3 业务规则（后端强制校验 + 前端拦截）

| 规则 | 校验点 | 返回 |
|------|--------|------|
| 子 Agent（`agent_role='sub'`）不允许设置团队成员（两级封顶，无嵌套） | 保存团队接口 | 400「子 Agent 不支持添加子 Agent」 |
| 主 Agent 只能添加 `agent_role='sub'` 的 Agent 为成员（主不可作子的子） | 保存团队接口 + 前端候选列表过滤 | 400「主 Agent 不能作为子 Agent 添加」 |
| 不允许添加自己为成员 | 保存团队接口 | 400 |
| 成员必须存在且非停用 | 保存团队接口 | 400 |
| 删除 / 停用 Agent 时级联清理团队关系 | `delete_agent` / `disable_agent` | — |
| 内置 Agent（`builtin=1`）保持可编辑，允许调整角色 | 沿用既有内置编辑许可 | — |

### 2.4 API 设计（新增/扩展）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/studio/agents` | 每项新增 `agent_role`、`team_count`、`team_members`（[{id,name,display_name,icon}]） |
| GET | `/api/studio/agents/{aid}` | 同列表字段 |
| POST/PUT | `/api/studio/agents` | `AgentIn` 新增 `agent_role`（默认 `sub`） |
| PUT | `/api/studio/agents/{aid}/team` | body `{sub_agent_ids:[...]}` 全量覆盖式保存团队；执行 §2.3 全部校验 |
| GET | `/api/studio/agents/{aid}/team` | 查询团队成员（编辑回填用） |
| GET | `/api/studio/agents/sub-candidates` | 团队候选（`agent_role='sub'` 且非当前、未停用），供主 Agent 选择器 |

### 2.5 运行时联动（轻量接入，不重写执行引擎）

- 主 Agent 试运行 / 执行时：从 `agent_team_members` 加载团队成员，将**团队名册**（成员：显示名 / 能力 / 职责）注入主 Agent system prompt（增量一段"团队协作"段落）；
- 主 Agent 执行中的 Planner / orchestrator 委派候选收敛为**团队成员白名单**（+ 自身），避免委派给团队外 Agent；
- 子 Agent 本身仍可被任意主 Agent 复用，也可被 @ 直接调用（不破坏现有意图路由）。

### 2.6 前端改造

- **Agent 表单**（`openAgentForm`/`editAgent` 弹窗）：
  - 新增「Agent 角色」单选：`主 Agent（团队负责人）` / `子 Agent（团队成员）`；
  - 角色=主时展示「👥 团队成员」多选（候选 = `sub-candidates`，可多选、支持多个），角色=子时隐藏该区并提示「子 Agent 不可再添加子 Agent」；
- **Agent 卡片**：主 Agent 显示 `👑 主` 徽章 + `团队 N 人`；子 Agent 显示 `🧩 子` 徽章；
- **Agent 列表筛选**：新增「角色」筛选（全部 / 主 Agent / 子 Agent）；
- **Skill/MCP/工具绑定**保持现状（所有 Agent 均可绑定多个，无角色差异）。

---

## 3. 需求三：Skill / MCP / 工具 插件模式 —— 公共市场 + 私人空间

### 3.1 行业方案参考

- **Coze 插件商店**：官方/个人插件两类，用户"安装"到个人空间后独立使用，商店更新不自动覆盖已装副本；
- **Dify Plugin**：插件市场（Marketplace）浏览 → install 拉取为本地插件，版本固定；
- **VS Code / WordPress 插件生态**：发布 → 商店陈列 → 用户安装（版本快照）。

共性结论：**市场 = 只读陈列（public），私人空间 = 可编辑实例（private）**；"安装"= 从市场复制一条私有实例（版本固定、与源解耦）。

### 3.2 数据模型

```sql
-- skills / mcp_servers / tools 三表各新增两列（幂等迁移 _migrate_plugin_scope）
ALTER TABLE skills      ADD COLUMN scope      TEXT DEFAULT 'private';  -- private | public
ALTER TABLE skills      ADD COLUMN source_ref TEXT DEFAULT '';         -- 市场来源 "kind:name:version"
ALTER TABLE mcp_servers ADD COLUMN scope      TEXT DEFAULT 'private';
ALTER TABLE mcp_servers ADD COLUMN source_ref TEXT DEFAULT '';
ALTER TABLE tools       ADD COLUMN scope      TEXT DEFAULT 'private';
ALTER TABLE tools       ADD COLUMN source_ref TEXT DEFAULT '';
```

- **市场（Marketplace）** = 三表中 `scope='public'` 的条目聚合视图（只读陈列）；
- **私人空间（Private）** = `scope='private'` 的条目（现有管理页）；
- **内置项回填**：`builtin=1` 的 skills / tools 回填 `scope='public'`，作为市场种子（平台预置插件）；
- 分类复用现有字段：skill→`category`/`skill_type`，mcp→`transport`，tool→`source`/`side_effect`。

### 3.3 业务规则

| 规则 | 说明 |
|------|------|
| 市场条目只读 | 市场视图不可直接编辑/删除；编辑须先"安装"到私人空间 |
| 安装 = 复制 | 复制 public 条目为 private 新行，`source_ref=源标识`；同名私人条目已存在时 400 提示 |
| 安装后独立 | 私人副本可编辑 / 停用 / 删除，市场更新不自动覆盖（版本快照语义） |
| 发布到市场 | 私人条目 → `scope='public'`（`source_ref` 清空）；内置项不可下架（400） |
| 下架 | 自定义 public 条目可下架回 private（已安装副本不受影响） |
| 绑定不受 scope 影响 | Agent 绑定 skill/mcp/tool 继续按名称匹配，市场与私人条目同名时以私人空间为准 |

### 3.4 API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/studio/market?kind=&category=&q=` | 市场聚合列表（kind=skill\|mcp\|tool\|all；含类型徽章/名称/描述/版本/分类/内置/下载口径） |
| POST | `/api/studio/market/install` | body `{kind, name}` → 复制到私人空间，返回新条目 id |
| POST | `/api/studio/{kind}/{id}/publish` | 私人 → 公开（kind=skill\|mcp\|tool；内置项 400） |
| POST | `/api/studio/{kind}/{id}/unpublish` | 公开 → 私人（自定义 public 项） |
| GET | `/api/studio/{kind}`（现有列表） | 默认只返回私人空间条目；`?scope=public` 可查市场（兼容） |

> `{kind}` 与现有路由前缀对齐：skills / mcp-servers / tools（后端统一参数映射）。

### 3.5 前端改造

- **侧边栏新增**：`🧩 插件市场`（`data-page="studio" data-tab="st-market"`）一级入口，位于"Agent 管理"之后；
- **插件市场页**（`st-market`）：统一卡片网格（聚合 skill/mcp/tool 三类，卡片显示类型徽章 / 名称 / 描述 / 版本 / 分类 / 来源「平台内置/用户发布」/「＋ 安装」按钮），顶部搜索框 + 种类筛选（全部/Skill/MCP/工具）；
- **私人空间三页**（Skill / 工具与 MCP 子页）：
  - 列表只显示 `private` 条目；
  - 卡片操作区新增「⬆ 发布到市场」（自定义项）、内置项显示「市场内置」标识；
- **安装交互**：点击「＋ 安装」→ 确认弹窗（提示"将复制到您的私人空间，独立编辑，不受市场更新影响"）→ 成功 toast → 私人空间列表自动刷新。

---

## 4. 数据迁移与兼容

| 项 | 说明 |
|----|------|
| `_migrate_agent_team` | agents 加 `agent_role`（默认 'sub'）+ 建 `agent_team_members` 表；**必须早于任何引用该列的种子/查询**（沿用记忆经验：列迁移放在建表后、种子前） |
| `_migrate_plugin_scope` | 三表加 `scope`/`source_ref`；内置项回填 public 在 seeds 之后执行（只 UPDATE 不 INSERT，无列引用风险） |
| 旧前端兼容 | `AgentIn` 新字段全部 Optional，旧调用方不受影响；列表新增字段不影响既有渲染 |

## 5. 验证计划

- **后端脚本**（`tools/verify_agent_team.py`）：角色约束（子不可加子 / 主不可作子 / 不可自加 / 停用成员拒绝）、级联清理（删主/删子）、团队 CRUD、`sub-candidates` 过滤；
- **后端脚本**（`tools/verify_market.py`）：市场列表聚合、内置种子、安装复制（source_ref 记录、同名冲突 400）、发布/下架、私人空间隔离；
- **前端闭环**（playwright）：① 侧边栏无「流程编排」、聊天工具栏无「⚡ 工作流」、编排富卡片无「另存为流程」；② Agent 表单角色切换 → 主 Agent 显示团队成员多选 → 保存后卡片徽章/团队数正确；子 Agent 无成员区；③ 插件市场浏览 → 安装 → 私人空间出现 → 编辑/发布；
- **回归**：`verify_agent_arch` / `verify_tool_ondemand` / `verify_skills_d4` 等既有脚本不回归（API 只增字段不改行为）。

## 6. 确认结论（2026-08-17 用户确认）

1. **流程编排**：运行详情「🗂 任务队列」（Planner 子任务视图）**保留**（属运行结果可观测性）；仅隐藏编排入口与跨模块引导。
2. **主/子 Agent 运行时联动**：采用**轻量联动**——团队名册注入主 Agent system prompt + 委派候选收敛为团队成员，不重写执行引擎。
3. **插件市场发布权限**：**自由发布**（自定义项可直接发布到公共市场，暂不引入审批流）。
4. **市场管理后台**：**需要**——提供市场条目管理（编辑描述/分类/版本、置顶、上下架、安装统计）。

### 6.1 插件市场管理后台（确认项 4 补充设计）

| 功能 | 说明 |
|------|------|
| 数据列 | 三表新增 `pinned INTEGER DEFAULT 0`（置顶）、`install_count INTEGER DEFAULT 0`（安装数，安装时 +1） |
| 入口 | 插件市场页内 Tab：「🛒 浏览市场」/「⚙ 市场管理」 |
| 市场管理列表 | 聚合三表 public 条目表格：类型/名称/描述/分类/版本/内置/安装数/置顶标记 + 操作（✏️ 编辑 / 📌 置顶 / ⬇ 下架 / 🗑 移除） |
| 编辑市场条目 | 改描述/分类/版本（内置项可编辑，与既有"内置可编辑"规则一致） |
| 置顶/取消置顶 | 切换 `pinned`；市场浏览列表按 pinned DESC、install_count DESC 排序 |
| 下架 | 自定义 public 项 → scope 回 private（source_ref 清空）；**内置项禁止下架（400）** |
| 移除 | 下架的等价操作（public→private 即从市场消失），不删除私人数据 |
| 安装统计 | 每次「安装」时源条目 `install_count+1`（幂等去重仍计数） |

## 7. 待确认决策点（已确认，见 §6）
