# AI 建模会话产物管理设计方案（修订版）

> 版本：v1.3 · 2026-08-11（v1.2 起按用户反馈定稿；**P0 已实施**：artifacts 表 + API + pipeline 归档埋点 + 前端产物分栏/预览/执行过程内联）
> 按用户反馈修订：①预览面板在页面右侧、与会话同级分栏展示、支持展开/收起 ②智能体执行过程移入会话内容区随进程展示 ③暂不支持跨会话产物库 ④上传的文档资料不进入产物库
> 依据：Trae Work / Claude Artifacts / ChatGPT Canvas&Projects / 扣子(Coze)文件 / Codex 会话归档 行业方案调研 + 当前 `static/index.html` pg-ai 与 `agent/pipeline.py` / `routers/conversations.py` / `routers/reports.py` 代码审计
> 目标：为「AI 建模」会话中 **AI 生成的** 文件 / 报告 / 代码 / SysML 视图提供统一管理入口与在线预览；会话执行过程内联到会话内容区

---

## 1. 行业调研结论（优秀设计对照）

### 1.1 五大产品做法

| 产品 | 会话产物的处理方式 | 可借鉴设计 |
|------|------------------|-----------|
| **Claude Artifacts** | 生成代码/文档/可视化时**自动在对话旁打开独立面板**渲染（code/web/markdown/react 四类）；对话内直接迭代修改；可「Publish」到 Artifacts 库；版本切换；下载 | ① 产物与对话分离展示但保持关联 ② 产物自动触发而非手动上传 ③ 独立预览面板 |
| **扣子(Coze) 文件** | 对话中所有文件自动归档到统一文件库，按 Agent/项目分类；在线预览；版本留痕 | ① 会话产物自动归档 ② 分类管理 ③ 在线预览 + 版本留痕 |
| **ChatGPT** | Canvas 独立编辑窗（版本历史、精准修改）；代码解释器**生成文件列表可下载** | ① 产物**文件列表可视化 + 下载** ② 独立工作区 |
| **Trae Work** | 会话内生成视频/图片以**可播放/预览卡片**展示；执行过程随会话流展示 | ① 会话内产物卡片化 ② **执行过程内联在会话流** |
| **Codex** | 会话 `attachments/` 按会话归档 + 索引 | 会话维度产物归档 |

### 1.2 共性模式（本方案设计原则来源）

1. **自动归档**：AI 生成的报告/代码/文件/SysML 视图自动进入会话产物列表，不散落在消息流里。
2. **分类管理**：按类型（报告/代码/SysML/文档）分类、筛选。
3. **在线预览**：图片大图、Markdown 渲染、代码高亮、SysML 视图 Cytoscape 投影（既有能力复用）。
4. **统一入口**：会话内右侧「会话产物」列表 + 右侧预览分栏（与会话同级、可展开/收起）。
5. **可操作**：下载 / 重命名 / 删除 / 跳回源消息。
6. **执行过程内联**：智能体执行（思考 → 子智能体 → 工具调用）随会话进程在消息流内直接展示（Trae/Claude 式），不再占用右侧栏。

---

## 2. 现状与差距分析

### 2.1 现状（AI 建模页 pg-ai 与后端）

- 会话消息表 `messages`：`attachments`(JSON，user 上传附件)、`card_data`(JSON，含 `sysml_views` / `exec` / 报告结构化数据)、`msg_type`。
- 用户上传附件/图片 → `POST /api/upload` 落盘 `static/uploads/`，内联展示在 user 消息气泡。
- **执行过程左右重复**：流式时思考/子智能体/工具既内联渲染进 `#proc-box`（V2.4 会话内执行过程），又同时写入右侧栏三个面板（`agent-steps` / `chat-thinking` / `chat-tools`）。
- AI 生成 SysML 代码 → `card_data.sysml_views` 投影为缩略图卡片（`cardSysmlViews` + Cytoscape），**仅存在消息流内**。
- AI 生成报告 → 仅以文本/富卡片内联展示；`reports` 表（报告中心）存在但 **pipeline 未自动归档**（`routers/reports.py` 只有手动 POST /api/reports 接口，`report_tools.exec_report_tool` 只落盘不登记）。
- AI 生成/工具落盘文件（`report_export` → `data/outputs/*.docx/pdf/md`）→ **会话内完全不可见**。

### 2.2 差距清单

| # | 差距 | 行业基准 |
|---|------|---------|
| G1 | 会话内 AI 生成的报告/代码/SysML/落盘文件无统一管理入口 | Claude Artifacts / Coze 文件 |
| G2 | 无在线预览：docx/pdf 只能下载、代码无独立渲染、报告无独立预览面板 | 全线产品 |
| G3 | 工具/流程落盘文件不回流会话（`report_export` 落盘后会话无感知） | Trae Work 产物卡片 |
| G4 | 报告中心「自动归档」承诺未实现（pipeline 未写 reports 表） | Coze 文件自动归档 |
| G5 | 执行过程左右重复展示，右侧栏信息密度低（执行状态/思考/工具三面板） | Trae Work 内联执行流 |
| G6 | 产物与源消息无双向跳转 | ChatGPT Projects / Codex session_index |

---

## 3. 设计目标与原则

### 目标
把「AI 建模」会话升级为「**消息流内联执行 + 会话产物管理**」双能力：智能体执行过程随会话流直接展示；AI 生成的报告/代码/SysML 视图/落盘文件自动归档、分类管理、在线预览、可下载可跳转。

### 明确边界（用户确认）
1. **预览面板**：位于页面右侧，**与会话同级分栏展示**（非独立滑出遮罩面板），支持展开/收起。
2. **执行过程**：右侧智能体执行过程（状态/思考/工具）移入会话内容区域，随会话进程直接展示。
3. **不做跨会话产物库**：产物列表仅作用于当前会话。
4. **上传的文档资料不进产物库**：用户上传附件维持消息内联展示，产物库只收录 **AI 生成的** 内容。

### 原则
1. **零侵入既有数据**：不动 `messages` / `reports` / `generation_history` 结构；新增 `artifacts` 表作为**会话内产物索引**，与消息通过 conversation_id/message_id 关联。
2. **自动采集、幂等归档**：产物登记埋点在既有写入链路上（消息落库、工具落盘），同源同型不重复登记。
3. **预览能力复用**：Markdown 渲染、SysML Cytoscape 投影、报告导出（md/docx/pdf）均为既有能力，预览面板直接复用。
4. **渐进交付**：P0（会话内产物面板 + 右侧预览面板 + 执行过程内联 + 报告自动归档）→ P1（落盘回流 + 报告中心联动跳转 + 消息流产物入口）→ P2（docx/pdf 在线预览、版本留痕）。

---

## 4. 总体架构

```
┌─ 展示层（前端 static/index.html，pg-ai 改造）──────────────────┐
│ 左栏：会话列表（不变）                                          │
│ 中栏：会话内容区                                                │
│   用户消息（附件内联，不进产物库）                               │
│   AI 消息：内联执行过程（思考→子智能体→工具，proc-box 强化）    │
│           + 内容 + 富卡片 + 「📎 生成产物 N 项」入口             │
│ 右栏：🗂 会话产物（当前会话列表 + 分类 chips）· 引用来源 · 本次会话│
│ 右栏预览分栏（与会话同级分栏，可展开/收起）：图片/Markdown/代码/SysML│
├─ 服务层（routers）────────────────────────────────────────────┤
│ /api/artifacts（会话内列表/统计/详情/下载/删除/重命名）          │
│ 复用 /api/reports 导出                                          │
├─ 采集层（自动归档埋点，仅 AI 生成）─────────────────────────────┤
│ ① pipeline 消息落库 → 从 card_data 抽取 sysml/report/code/doc   │
│ ② 报告类产物 → 同时写 reports 表（打通报告中心自动归档）         │
│ ③ report_export / file_write 工具落盘 → 登记产物（P1）          │
├─ 存储层────────────────────────────────────────────────────────┤
│ artifacts 表（会话内产物索引）· data/outputs/ · static/uploads/  │
│ messages.card_data / reports（关联不迁移）                      │
└────────────────────────────────────────────────────────────────┘
```

---

## 5. 后端实现方案

### 5.1 新表 `artifacts`（database/schema.py + migrations 幂等）

```sql
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER REFERENCES conversations(id),  -- 来源会话（必填，会话内产物库）
    message_id INTEGER DEFAULT 0,                          -- 来源消息（可空）
    kind TEXT NOT NULL,        -- report|code|sysml|document|other（仅 AI 生成）
    title TEXT DEFAULT '',     -- 显示名（自动生成，可重命名）
    filename TEXT DEFAULT '',  -- 导出文件名 / 落盘文件名
    file_path TEXT DEFAULT '', -- 相对落盘路径（data/outputs/...）
    file_url TEXT DEFAULT '',  -- 可访问 URL（图片直接内联）
    mime TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    preview_type TEXT DEFAULT 'none', -- none|image|markdown|code|sysml|html
    preview_content TEXT DEFAULT '',  -- 内联预览文本（markdown/code/sysml 源码）
    meta TEXT DEFAULT '{}',           -- JSON：{report_type, sections, sysml_views, version, source_msg_type}
    source TEXT DEFAULT 'conversation', -- conversation|flow|tool|report|manual
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_art_conv ON artifacts(conversation_id);
CREATE INDEX IF NOT EXISTS idx_art_kind ON artifacts(kind);
```

要点：
- **kind 分类**（仅 AI 生成）：`report`（结构化报告，关联 reports 表）、`code`（代码块，含 SysML V2 源码）、`sysml`（SysML 视图投影，关联 card_data.sysml_views）、`document`（AI 生成的长文本/markdown 产物）、`other`。**不含**用户上传附件（维持消息内联）。
- **preview_type** 与 kind 解耦：report 可 markdown 渲染，sysml 走 Cytoscape，image 走 URL 内联。

### 5.2 新增 API（routers/artifacts.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/artifacts?conversation_id=&kind=&q=` | 产物列表：**会话内**（conversation_id 必填），分类/关键词过滤，时间倒序 |
| GET | `/api/artifacts/stats?conversation_id=` | 分类统计（chips 计数：全部/报告/代码/SysML/文档），对齐 v2g 类型 chips 模式 |
| GET | `/api/artifacts/{id}` | 详情：含 preview_content / meta（报告带 sections、sysml 带 views） |
| PATCH | `/api/artifacts/{id}` | 重命名 title |
| DELETE | `/api/artifacts/{id}` | 删除产物索引（物理文件保留；写审计 `artifact_delete`） |
| GET | `/api/artifacts/{id}/download` | 下载：file_path 存在直接返回文件流（Content-Disposition UTF-8 文件名）；report 产物转调报告导出 |

`/api/artifacts` 路由注册进 `main.py`（对齐 reports_router 现有装配方式）。

### 5.3 自动归档埋点（采集层，幂等）

1. **AI 消息落库**：`agent/pipeline.py` 三处 `INSERT INTO messages ... card_data` 后（`execute` 串行、`execute_stream`、`_try_orchestrate` 编排路径），新增 `_archive_artifacts(conn, conversation_id, message_id, card_data, content)`：
   - `card_data.sysml_views` 存在 → `kind=sysml, preview_type=sysml, meta={sysml_views}`；
   - 报告类（intent=report_generation 或 card 含 sections）→ `kind=report`，**同时写 reports 表**（title/sections/summary/conversation_id/source=conversation，落实报告中心「自动归档」承诺）；
   - 长文本含代码块 → `kind=code`（提取 ``` 块，多条代码块各登记一条）；
   - 长 Markdown 文本（无结构化 card）→ `kind=document, preview_type=markdown`；
   - 幂等键 `(conversation_id, message_id, kind, title)`，重复落库 UPDATE 不新增。
2. **工具/流程落盘回流（P1）**：`report_tools.exec_report_tool` 导出成功后登记 `kind=report|document`（path/title/source=tool）；`file_tools.exec_file_tool` 的 file_write 登记 `kind=document`（source=tool）。工具执行无会话上下文时由前端在收到产物事件时回填 conversation_id。
3. **清理语义**：删除会话（`delete_conversation`）级联删除 `artifacts` 索引（对齐现有级联清理）。

### 5.4 预览能力映射

| preview_type | 渲染方式 | 数据来源 |
|--------------|---------|---------|
| image | `<img src=file_url>` 大图（TRAE APP 缩略图+全屏模式） | file_url |
| markdown | 复用前端 `renderMarkdown` | preview_content |
| code | `<pre><code>` + 复制/下载 | preview_content |
| sysml | 复用 `cardSysmlViews` / Cytoscape 投影（既有能力） | meta.sysml_views |
| docx/pdf（P2） | 后端轻量解析 docx→html / pdf.js 预览 | file_path |

---

## 6. 前端实现方案（static/index.html，pg-ai 改造）

### 6.1 执行过程移入会话内容区（布局重构）

- **移除右侧栏三个执行面板**：`智能体执行状态(agent-steps)` / `思考过程(chat-thinking)` / `工具调用结果(chat-tools)` 面板及其渲染调用。
- **内联执行过程强化**（现有 `#proc-box` / `procRender` 已具备，保留并增强）：
  - 思考流（reasoning 事件）→ `procAddThinking` → 折叠块「💭 思考过程」；
  - 子智能体（agent 事件）→ `procAddAgent` →「🤖 子智能体：xxx ✓/执行中」；
  - 工具调用（tool 事件）→ `procAddTool` →「🛠 调用 xxx ✓/✗」+ 参数/结果折叠；
  - **管线阶段（stage 事件）内联**：意图识别 → 知识库检索 → 生成与校验 → 写入会话，渲染为 proc-box 顶部「🔄 管线阶段」行（原来写入 agent-steps），随进程实时更新；
  - 历史消息还原：`card_data.exec` → `procBlocksHtml`（既有，零改动）。
- **右侧栏保留并精简**：`🔗 引用来源` + `🗂 本次会话`（状态/意图/Agent/HIL）+ 新增「🗂 会话产物」。

### 6.2 会话产物列表（右栏）

- **入口**：右栏「🗂 会话产物」块；`selectConv` 时并行拉 `/api/artifacts?conversation_id=` 与 `/api/artifacts/stats`。
- **展示**：分类 chips（全部 N / 📑 报告 M / 💻 代码 M / 🕸 SysML M / 📄 文档 M）+ 产物列表（icon + 标题 + 时间，hover 操作：👁 预览 / ⬇ 下载 / ✏️ 重命名 / 🗑 删除）。
- **增量刷新**：`handleSSE` done 事件后刷新当前会话产物列表（不整页重拉）；切换会话时隔离加载。

### 6.3 产物预览分栏（页面右侧，与会话同级，可展开/收起）

- **布局形态（分栏，非滑出遮罩）**：pg-ai 保持 `.cols` 多列 flex 布局，右栏即**产物预览分栏**，与会话内容区同级并列（同一列容器内第三栏），不覆盖会话、不做固定定位遮罩：
  ```
  .cols（flex，height:calc(100vh - 190px)）
  ├─ aside：会话列表（固定宽，不变）
  ├─ div  ：会话内容区（flex:1）
  ├─ div  ：#art-divider 分界线（可拖拽调宽，280-760px，localStorage 记忆）
  └─ aside：#art-panel 产物预览分栏（宽可拖，可展开/收起）
  ```
- **分栏内部结构**（flex column，min-height:0）：
  - 头部：`🗂 会话产物 <toggle 收起/展开按钮>`；
  - 产物列表（flex:none，max-height 可滚动）：分类 chips + 产物条目，点击条目在下方预览；
  - **产物下拉选择**（WorkBuddy 式）：`#art-select` 下拉列出全部产物（icon+标题+类型），选中即预览；与列表选中双向联动（列表点击同步下拉值，下拉选择同步列表高亮）；
  - 预览内容区（flex:1，overflow-y:auto，固定位置）：按 preview_type 渲染选中产物。
- **展开/收起**：
  - 收起：点击头部 toggle（或页面空白区 Ctrl+/），`#art-panel` 宽度置 0 / display:none，会话内容区自动占满；
  - 展开：点击头部 toggle / 消息流「📎 生成产物 N 项」chip / 产物条目；展开时自动加载当前会话产物并渲染最新选中项；
  - 收起状态记忆（localStorage），切会话保持。
- **分界线拖拽**：`#art-divider`（8px，cursor:col-resize）mousedown 开始拖拽 → document mousemove 实时改宽（拖拽期间 `transition:none` 保证实时跟随与精确落点）→ mouseup 落盘 localStorage `mbse_art_width`；收起时隐藏分界线，重开恢复宽度。
- **内容**（按 preview_type 渲染，见 5.4）：
  - 头部：类型徽章 + 标题 + 时间 + 操作（下载 / 复制 / 重命名 / 删除 / 跳转源消息）；
  - 报告产物：渲染 sections（`renderMarkdown`）+「导出 Word/PDF/Markdown」按钮（复用 `downloadReport(fmt)` 链路）；
  - SysML 产物：Cytoscape 投影（复用 `cardSysmlViews`）+ 视图清单；
  - 代码产物：高亮展示 + 复制/下载；
  - 底部：来源消息链接，点击 `scrollToMessage(msg_id)` 定位消息流。

### 6.4 消息流内增强

- AI 消息渲染 `renderMessage` 时，若该消息有归档产物（card_data 含 sysml_views / 报告 / 代码），卡片底部追加「📎 生成产物 N 项」chip，点击直接打开右侧预览面板。

### 6.5 交互细节

- 删除/重命名沿用 `confirmDialog` / `promptDialog`（居中卡片 #lbx），禁止原生 confirm/prompt。
- 分栏可主动收起/展开（头部 toggle + 消息流 chip 触发），触发预览时自动展开；收起后会话内容区占满。

---

## 7. 与既有模块的衔接

| 既有模块 | 衔接方式 |
|---------|---------|
| 报告中心（reports 表） | 报告类产物自动写 reports 表 → 报告中心列表出现「来源会话」跳转（P1）；报告中心导出的同时登记产物 |
| 生成与反馈（generation_history） | 产物列表与其并存，互不迁移；确认/拒绝动作仍走原链路 |
| 会话消息（messages.attachments） | **用户上传附件不进产物库**，维持消息内联展示（明确边界 4） |
| 工具/流程落盘（report_export / file_write） | 落盘即登记（P1），会话内可见可预览（回流闭环） |
| SysML 视图预览（cardSysmlViews） | 产物预览面板复用其 Cytoscape 投影渲染 |
| 执行过程（proc-box / 右侧三面板） | 移除右侧三面板，执行过程全部内联会话流（明确边界 2） |

---

## 8. 分阶段实施计划

### P0 — 会话内产物闭环 + 布局重构（✅ 已实施 2026-08-11）
- 后端：artifacts 表 + `/api/artifacts` CRUD + `/api/artifacts/stats` + 埋点①（AI 消息 card_data 抽取 sysml/report/code/document）+ 报告自动写 reports 表
- 前端：
  - 布局重构：移除右侧执行三面板，执行过程全部内联 proc-box（含 stage 管线阶段内联）；右栏改为「🗂 会话产物」列表 + 产物预览分栏（与会话同级，展开/收起）
  - 右栏新增「🗂 会话产物」列表 + 分类 chips + 下载/重命名/删除
  - 产物预览分栏（同列分栏，展开/收起）：image/markdown/code/sysml + 报告导出
  - 消息流「📎 生成产物 N 项」chip
- 验证：tools/verify_artifacts.py 10/10（代码块提取/四类产物归档/幂等/报告自动归档/级联清理/分类统计）；playwright 前端闭环——产物 chips 计数、列表 4 项、报告预览分节、SysML Cytoscape 预览、代码预览+复制、chip「📎 生成产物 3 项」点击预览、分栏收起（w:0 + 重开按钮）/展开（w:370）、重命名/删除居中卡片弹窗、下载 200+UTF-8 文件名、流式执行内联（管线阶段 4/4 + 思考 + 子智能体）、产物列表 done 后增量刷新至 5 项、导航回归正常、无新增 JS 报错

### P1 — 落盘回流 + 报告中心联动
- 埋点③（report_export/file_write 登记，落盘文件回流会话）+ 报告中心「来源会话」跳转 + 跳回源消息定位（scrollToMessage）
- 验证：流程落盘 docx → 会话产物列表出现并可下载；报告中心跳回源会话高亮消息

### P2 — 高级预览与体验
- docx→html 在线预览、pdf.js 预览、产物版本留痕（对齐 Coze 自动版本）
- 验证：docx 在线预览；产物修改产生版本历史

---

## 9. 验证方案（对齐项目「前端闭环验证」惯例）

1. **后端**：`tools/verify_artifacts.py`——表结构/幂等归档（同源同型不重复）/分类统计/CRUD/下载/级联删除/报告自动归档，临时测试 DB 隔离。
2. **前端 playwright**：AI 建模页发消息生成 SysML → 执行过程内联展示（右侧三面板已移除）→ 产物计数+列表正确 → 右侧预览分栏展开 → 图片大图 → 下载触发 → 重命名/删除（居中卡片弹窗）→ 分栏收起（会话占满）→ 消息流「生成产物 N 项」chip → 切换会话产物隔离 → 报告中心出现自动归档报告 → 上传附件不出现在产物库。
3. **回归**：会话附件内联展示、SysML 缩略图卡片、报告导出、生成与反馈台账无回归。

---

## 10. 参考资料

- Trae Work / Trae changelog（会话产物卡片、执行过程内联、任务历史）：https://www.trae.ai/changelog
- Claude Artifacts 官方说明（独立面板、四类产物、Publish 库、版本切换）：https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them
- Claude Artifacts 交互解析（会话内产物卡片化）：https://claude-me.com/zh/glossary/core-concepts/claude-artifacts/
- ChatGPT Canvas / Projects（独立编辑窗、版本历史、文件列表下载）：https://juejin.cn/post/7460319992413732879
- 扣子(Coze) 文件（会话产物自动归档、分类、在线预览、版本）：https://docs.coze.cn/cozespace/files
- Codex 会话归档（attachments 目录 + session_index）：https://m.toutiao.com/group/7661841620987314729/
