# AI 设计工坊 Copilot：AI 辅助维护设计方案

> 版本：v1.0 · 日期：2026-08-07 · 状态：设计评审稿
> 定位：把 AI 设计工坊从"人工维护的平台"升级为"AI 自助维护的 AI 原生应用"

---

## 一、现状与痛点

当前 AI 设计工坊已具备完整的**人工维护链路**（可视化画布、FlowExecutor 真实执行、Skill/MCP/Agent 管理、提示词实验室、运行轨迹落库），但**所有维护动作都依赖人工**：

| 维护对象 | 当前方式 | 痛点 |
|---------|---------|------|
| 工作流编排 | 拖拽画布 + JSON 手工配置 | 从 0 搭流程慢；不知道有哪些节点/工具可用；条件分支表达式易写错 |
| Skill | 手写 SKILL.md / 上传 ZIP | 格式不规范、triggers 不全、frontmatter 字段易漏 |
| MCP | 手工填 endpoint + 工具名 | 不知道端点提供哪些工具；工具 schema 要手工抄 |
| Agent | 手填意图关键词/工具绑定 | 关键词靠拍脑袋；不知道绑什么工具合适；提示词模板不匹配 |
| 运行优化 | 看运行历史逐条分析 | 失败根因靠人肉归因；无参数调优建议 |

**核心洞察**：工具都有了，缺一个"帮你用好工具"的 AI 层——即 AI 原生应用最关键的 **Copilot 反馈闭环**。

## 二、行业调研结论（2025-10 起的新战场）

| 厂商/产品 | 做法 | 关键机制 |
|----------|------|---------|
| **OpenAI AgentKit**（DevDay 2025） | 低代码工作流编排工具 | NL→工作流 |
| **n8n AI Workflow Builder**（2025-10） | 自然语言直接生成 n8n 工作流 JSON | **模板检索 + 微调**（System Prompt 明确要求"将这些范例用作类似用例的模板；复制节点结构、参数格式、连接模式"）；可追加指令迭代优化 |
| **Zapier**（2025-10） | 自然语言描述触发条件+动作 → 生成草稿大纲 | "当 X 发生 → 做 Y → 再做 Z" |
| **Make** | 目标语句 → 场景骨架 + 自动解释 + 排错 | 骨架生成 + 解释 |
| **Dify** | Prompt IDE（多模型对比调试）+ LLMOps 日志追踪 + 600+ 插件市场 | 调试/观测/复用 |
| **Coze** | 零代码 + 插件市场 + 多渠道发布 | 上手门槛最低，但 MCP 支持缺失 |
| **钉钉 AI 表格助理** | NL 对话 → 生成表格/自动化工作流/仪表盘 | 领域模板复用 |

**共识模式（本方案遵循）**：
1. **自然语言 → 结构化 JSON**（工作流本质是 JSON：节点列表 + 连接关系 + 节点参数）
2. **模板检索复用优先于纯原创生成**——用已验证的流程/模式做范例，AI 按相似用例复制组装，保证稳定性、防幻觉节点
3. **迭代追加指令**：生成 → 预览 → 追加指令微调（n8n Builder 的对话式迭代）
4. **LLMOps 反馈闭环**：运行日志/轨迹 → AI 分析 → 优化建议 → 迭代（Dify 的观测哲学）
5. **人机协作确认**：AI 生成 → 人工预览确认 → 生效（对齐本系统 HIL 分级）

## 三、总体设计

### 3.1 架构（四层 + 反馈闭环）

```
┌─────────────────────────────────────────────────────┐
│ 交互层：工坊内嵌 Copilot 对话面板 + 每资源「✨AI辅助」入口 │
├─────────────────────────────────────────────────────┤
│ AI 辅助服务层（Copilot Agent，5 大能力）                │
│   ① NL→DAG 生成器  ② Skill 生成/审计  ③ MCP 自动接入    │
│   ④ Agent 维护     ⑤ 运行轨迹智能分析                   │
├─────────────────────────────────────────────────────┤
│ 工坊核心层（现有）：画布/FlowExecutor/Skill/MCP/Agent/   │
│                   提示词实验室/ToolExecutor/HIL         │
├─────────────────────────────────────────────────────┤
│ 数据底座：现有 6 表 + 新增 flow_templates / ai_suggestions│
└─────────────────────────────────────────────────────┘
          ↺ 反馈闭环：运行轨迹 → AI 分析 → 优化建议 → 迭代
```

### 3.2 五大能力设计

#### 能力 ①：NL→DAG 流程生成器（核心，P0）
- **输入**：自然语言任务描述（如"从需求文本抽取条目，做冲突检测，再生成影响分析报告"）
- **输出**：`{name, description, nodes[], edges[]}`（与现有 agent_flows 格式完全一致，可直接导入画布）
- **生成策略（模板检索复用）**：
  1. 检索 `flow_templates` + 已有 `agent_flows`（余弦/关键词相似度）找最相似范例
  2. System Prompt 注入：工坊 schema（6 种节点类型及字段）+ 当前可用工具清单（ToolRegistry）+ 可用 Agent 清单（agents 表）+ 最相似范例 JSON
  3. LLM 结构化输出（function calling / JSON mode）→ 前端预览卡片
- **校验器**（生成后自动执行，防幻觉）：节点类型 ∈ 6 种；tool 节点引用的工具 ∈ ToolRegistry；agent 节点引用的 intent ∈ agents；边两端节点存在；无孤儿节点
- **迭代**：追加指令（"把影响分析改成变更影响 Agent 节点"）→ 在原 JSON 上局部修改
- **沉淀**：人工确认导入后，可选存入 `flow_templates`（带标签：场景/节点构成/验证状态）

#### 能力 ②：Skill 辅助生成/审计
- **生成**：描述 → SKILL.md（frontmatter: name/description/triggers/category + 正文指令），预览确认入库；生成后自动过 **技能安全审计**（对齐 skills-security-check：P0/P1/P2 分级）
- **审计**：已有 Skill → AI 检查触发词覆盖度（对照历史对话/工具调用日志中的高频意图词）、正文结构规范性、给出优化建议（对比 Dify Prompt IDE 的调试思路）

#### 能力 ③：MCP 自动接入
- 用户只给 endpoint → AI 调用 `tools/list`（复用现有 mcp-tools catalog/inspect 机制）→ 自动生成：服务器配置 + 工具清单 + 每个工具的入参说明 → 确认入库
- 工具调用失败 → AI 归因（404/参数错/schema 不匹配）→ 修复建议（对齐 MCP Client Best Practices：catalog → inspect → execute）

#### 能力 ④：Agent 意图与工具推荐
- **意图关键词自学习**：聚合 `tool_call_logs` + 对话消息中的高频意图词 → 建议补入 `intent_keywords`
- **工具推荐**：按 Agent 的 description + 历史绑定 + 工具描述语义匹配 → 推荐绑定（含理由）
- **提示词模板建议**：为 Agent 生成/优化其 system_prompt（对齐提示词实验室模板）

#### 能力 ⑤：运行轨迹智能分析（LLMOps 闭环）
- 输入：`flow_runs` + `flow_run_steps`（含每节点 content/data/latency/status）
- AI 分析产出：失败节点根因（error 文本归因）、耗时瓶颈（latency 排序 + 建议：换 provider / 精简 prompt）、条件分支合理性（skipped 统计）、参数调优建议
- 输出：优化建议卡片（每条：问题/证据/建议/一键应用），落 `ai_suggestions` 审计

### 3.3 数据模型

| 表 | 变更 | 说明 |
|----|------|------|
| agent_flows / flow_runs / skills / mcp_servers / agents / prompts | 复用 | 不变更结构 |
| **flow_templates**（新增） | 场景标签/节点构成/验证状态/来源(AI生成+人工确认) | 模板检索复用的语料库 |
| **ai_suggestions**（新增） | target_type/target_id/建议内容/证据/状态(待采纳/已采纳/忽略)/LLM 元信息 | AI 建议全审计可追溯 |

### 3.4 API 设计（全部走现有 llm_client，OpenAI 兼容多平台）

```
POST /api/studio/ai/generate-flow     {prompt}            → {name, nodes, edges, explanation, validations}
POST /api/studio/ai/refine-flow       {prompt, definition}→ {name, nodes, edges, explanation}
POST /api/studio/ai/generate-skill    {description}       → {frontmatter, content, audit:P0/P1/P2}
POST /api/studio/ai/audit-skill       {skill_id}          → {issues[], suggestions[]}
POST /api/studio/ai/probe-mcp         {endpoint}          → {tools[], server_config, bind_suggestions}
POST /api/studio/ai/agent-suggest     {agent_id?}         → {keywords[], tool_recommendations[], prompt_suggestion}
POST /api/studio/ai/analyze-runs      {flow_id?, run_id?} → {analysis, suggestions[]}
GET  /api/studio/ai/suggestions       {status?}           → 审计记录
```

### 3.5 前端交互

1. 工坊顶部新增「✨ AI 辅助」Copilot 对话面板（可停靠右侧）——全局入口
2. 各资源 tab 头部「✨ AI 辅助」按钮：
   - 流程编排 → 「AI 生成流程」对话框（描述 → 预览卡片 → 一键导入画布）
   - Skill 模板库 → 「AI 生成 Skill」「AI 审计」
   - 工具与 MCP → 「AI 探测接入」
   - Agent 管理 → 「AI 建议」
   - 运行历史 → 「AI 分析本次运行」
3. 生成预览统一为"AI 建议卡片"：方案内容 + 校验结果 + 说明 + [导入/采纳] [重新生成] [忽略]（HIL 人工确认，AI 不直接写库）

## 四、实施路线（4 阶段）

| 阶段 | 内容 | 价值 | 依赖 |
|------|------|------|------|
| **P0（优先）** | NL→DAG 生成器 + 校验器 + 画布一键导入 + 迭代微调 | 编排效率最大提升（对齐 n8n Builder） | 已有画布/执行/llm_client |
| **P1** | 运行轨迹智能分析 + 建议卡片 + ai_suggestions 审计 | 闭环生效（对齐 Dify LLMOps） | 已有 flow_runs |
| **P2** | Skill 生成/审计 + MCP 自动探测接入 | 资源维护提效 | 已有 skills/mcp + catalog 机制 |
| **P3** | Agent 意图自学习/工具推荐 + flow_templates 沉淀 + 多模型对比 | 智能化最高（对齐 Prompt IDE） | P0-P1 数据积累 |

## 五、风险与边界

| 风险 | 缓解 |
|------|------|
| 生成流程含"幻觉节点"（不存在的工具/Agent/错误连线） | **校验器硬校验**（工具/Agent/节点类型白名单）+ 模板检索复用（对齐 n8n Builder 策略） |
| SKILL.md 生成内容安全 | 生成后强制走**技能安全审计**（P0/P1/P2 分级，P0 阻断） |
| 建议质量依赖 LLM | 全部建议落 `ai_suggestions` 审计 + HIL 人工确认后才生效；Mock 降级时明确标注 |
| 生成稳定性 | 结构化输出（function calling/JSON mode）+ 失败重试 + 降级为纯文本建议 |

## 六、一句话总结

> **把"编排工具"变成"编排 Copilot"**：用户用自然语言描述目标，AI 检索已验证模板组装出可校验的流程/资源方案，人工一键确认后生效；每次运行轨迹回流 AI 分析，持续产出优化建议——形成"描述→生成→确认→运行→分析→优化"的 AI 原生闭环。
