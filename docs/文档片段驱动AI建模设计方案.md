# 文档片段驱动 AI 建模设计方案

> 版本：v1.0 · 2026-08-23
> 来源：客户需求「基于指定的文档片段（文档统一存于文件管理，已支持切片与向量化）进行 AI 建模」+ 现有代码审计
> 审计对象：`knowledge_pipeline/`（切片/embedding/入库/检索）、`agent/rag.py`（GraphRAG）、`agent/pipeline.py::execute_stream`（建模主链路）、`routers/conversations.py`（SSE 入口）、`models/conversation.py::ChatIn`、`static/index.html`（AI 建模输入区 / 文件管理 kb-e）
> 结论先行：需求合理且属成熟 RAG 增强生成范式。工程已具备「文件管理全局化 + 切片 + 向量化 + 检索 + 建模注入 + 溯源」的大半能力，本方案聚焦两个待补缺口：**「怎么圈定内容」**（范围设定）与**「建模时怎么选范围」**（范围消费）。

---

## 1. 需求理解与原则

### 1.1 需求拆解

"基于指定的文档片段进行 AI 建模"拆为三件事：

1. **文档统一存文件管理**：已有 —— `documents` 表 + `data/uploads` 源副本，`document_chunks` 落分块。
2. **确定"指定"的内容**（本方案核心缺口 A）：用户要能圈定范围 —— 整篇文档，或某文档的某几节/某几段（由切片产生）。
3. **建模时选择该范围**（本方案核心缺口 B）：建模请求把该范围传给后端，作为检索**硬边界**，AI 仅在此范围内召回切片并据此建模，产物带溯源。

### 1.2 关键原则（与现状代码对齐）

- **P1 文件管理 = 全局数据，不分分支**。文件不随图谱分支管理（见现状 §2.1，`rag.py` 向量检索本就 `branches=None` 全局消费；前端文件选择器已"文档全局化"列出全部已接入文档）。**范围设定与文档选择均不经过分支概念**；分支只影响图谱实体消费，不约束文档片段。
- **P2 文档片段 = 召回证据，不是建模保证**。SysML v2 强规范领域，片段再准也可能产出非法模型。本能力只负责**输入约束与溯源**；产物合法性仍走既有 `_build_model_code_req` + 视图生成 + `sysml_versions` 版本入库链路，不重复造。
- **P3 默认安全边界不破坏**。未指定范围时维持现有默认（图谱消费 release 分支，向量全库）；一旦指定范围，文档检索在该范围硬锁，图谱实体仍按分支规则。
- **P4 溯源不丢**。任何命中切片必须保留 `source_doc + section + chunk_index`，供前端「引用来源 [n]」「一键追溯来源」核验。

---

## 2. 现状与差距

### 2.1 已具备能力（勿重复实现）

| 层 | 现状 | 位置 |
|---|---|---|
| 切片 | 结构感知分块：标题行 / 代码块 / 表格独立，标题富化 `embed_text` | `knowledge_pipeline/chunking.py::chunk_text_structured` |
| 入库 + 向量化 | BGE-M3 真向量（bigram 降级）、源副本、Reverse HyDE | `knowledge_pipeline/ingest.py`、`embedder.py` |
| 检索（按文档过滤） | `search_chunks`/`vector_search_embed` 支持 `doc_names` 硬过滤（`WHERE source_doc IN (...)`） | `knowledge_pipeline/search.py::_doc_clause` |
| GraphRAG 消费 | `retrieve(kb_scope.docs)` → `source_docs`/`doc_names` 硬锁；`attachment_text` 附件文本优先 | `agent/rag.py::retrieve` |
| 建模注入 + 溯源 | 检索命中切片注入 system prompt（`【来源i】`），前端 `[n]` 引用链接 | `agent/pipeline.py`、`static/index.html` |
| AI 建模产物 | `_build_model_code_req` + sysml_views + 版本入库 | `agent/pipeline.py::_build_model_code_req` |

### 2.2 待补缺口

**缺口 A —— 没有"范围对象"**：
- 现有 `#文件名` 标签是**软命中**（`extract_kb_tags` → `kb_hint` 拼进检索词，靠语义/标题命中靠前，**其它文档仍可能被召回**），不是硬边界。（[pipeline.py](file:///C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/agent/pipeline.py#L3190-L3193)）
- 现有 `kb_scope.docs` 硬过滤**只在 Agent 定义里静态配置**，不来自建模时的用户选择。
- 没有任何"保存/复用命名范围"的数据结构，也没有"选到片段级（节/段）"的能力。

**缺口 B —— 请求链路没有范围入参**：
- `ChatIn`（[conversation.py](file:///C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/models/conversation.py#L12-L19)）与 `execute_stream` 签名均无范围字段；`chat_stream` 把自定义 scope 透传给 `execute_stream` 再传给 `rag.retrieve` 的通道不存在（只有 Agent 定义级 `kb_scope`）。

---

## 3. 总体设计

### 3.1 一句话方案

新增一个**建模范围（Modeling Scope）**概念，其粒度分三档；范围可在「文件管理」中设定并保存（带版本快照），在「AI 建模」输入时选择，经请求体传给后端，作为检索**硬边界**进入 `rag.retrieve(kb_scope=...)`；命中切片保留溯源。

### 3.2 范围粒度（三档，向下兼容）

| 档位 | 含义 | 检索实现 | 改造成本 | 适用 |
|---|---|---|---|---|
| G1 整文档 | 以文档为单位圈定 | 硬过滤 `doc_names=[filename...]`（已有搜索侧），实则无需新代码 | 低 | 需求文档集驱动建模 |
| G2 文档片段**组** | 一段可命名的 text 片段集（如"第2节+第5节"） | 片段文本作为 `attachment_text` 注入（**附件优先路径**，硬性输入非候选） | 中 | 面向"特定章节"建模 |
| G3 切片级精确 | `document_id+chunk_index` 精确锁定 | 拉取指定 chunks 作为精确证据注入（等同 G2 但用 chunk 存储） | 中 | 需要精确到句/段 |

> 设计取舍：**不强造"片段选择器直接驱动向量检索"**。因为检索是召回不是精确，若用户已明确"就用这几段"，走"证据注入"（attachment_text / 精确 chunk 文本）比"检索"更可控、更可解释。G1 用硬过滤（快、范围语义清晰），G2/G3 用证据注入（准、不误召）。

### 3.3 数据模型（新增一张表，幂等迁移）

表 `modeling_scopes`（**全局表，不随分支**）：

```
modeling_scopes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,            -- 范围名称（用户命名，如"载荷指标-方案基线"）
  mode        TEXT NOT NULL,            -- doc | fragment_group | chunk (G1/G2/G3)
  doc_ids     TEXT DEFAULT '[]',        -- 命中文档 documents.id 列表（JSON 数组）
  doc_names   TEXT DEFAULT '[]',        -- 冗余 source_doc/filename 列表（检索硬过滤 / 展示）
  fragment_text TEXT DEFAULT '',        -- G2 片段文本（多段拼接）；G3 的精确片断源
  chunk_ids   TEXT DEFAULT '[]',        -- G3：{document_id, chunk_index, section, content} 谱
  meta        TEXT DEFAULT '{}',        -- 创建人/来源版本等扩展
  created_by  TEXT DEFAULT 'system',
  created_at  TEXT DEFAULT (datetime('now', 'localtime'))
)
```

- 幂等迁移：沿用 `database/migrations.py` 的 `_add` 模式（`try/except` + 启动时建表）。
- 片段源文本在保存时已固化（快照语义），后续文档重向量化不影响已保存范围（明确"快照"）。

### 3.4 端到端链路

```
文件管理(kb-e)                     AI 建模输入区
   │ 浏览切片/选档/拖段落                │
   ▼                                   ▼
自然语言/可视化 设定范围 → 保存 modeling_scopes  选择范围(下拉) → 软`#`/硬范围
   │                                   │  POST /api/conversations/{id}/chat/stream
   └────── 复用 G1/G2/G3 任一 ─────────▶ ChatIn.scope_id (或内联 scope)
                                             │
                                             ▼
                            chat_stream → execute_stream(scope=...)
                                             │  合并 agent_def.kb_scope + 请求 scope
                                             ▼
             rag.retrieve(kb_scope={'docs':[...], 'mode':..., 'fragments':...})
                 ├─ G1：hard doc_names 过滤（search.py 已有）
                 └─ G2/G3：fragment_text 走 _match_attachment / 直接注入
                                             │
                                             ▼
             命中切片(带 source_doc/section/chunk_index) 注入 system prompt
                                             ▼
             LLM 建模 → _build_model_code_req 出 SysML → sysml_views → 版本/入库
                                             │
                                             ▼
               前端：引用来源 [n] / 一键追溯（溯源保留）
```

---

## 4. 详细设计：怎么"圈定内容"（缺口 A）

### 4.1 文件管理的"范围设定"入口

新增「文件管理 kb-e → 建模范围」面板（可视 + 自然语言双入口），风格沿用现有 kb 子页工具行/列表/弹窗样式，不破坏既有布局约定。

**方案 A2-自然语言圈定（推荐首期）**
- 用户在文件管理粘贴/编写一段"范围描述"，如《宽带载荷》第2节+第5节，取性能指标段落；或直接选中文件管理里已入库文档的某些切片勾选。
- 后端解析：整篇 → G1；仅含文档但提到节/段 → G2/G3。生成范围对象预览（命中文档清单 + 命中切片数），用户确认后命名保存到 `modeling_scopes`。

**方案 B2-可视化圈定（增强，二期）**
- 复用 `document_chunks` 的 `section` 树：文档 → 章节(section) → 切片(chunk) 三级勾选树。默认按 doc 级选（G1），展开到节可勾选节（G2），展开到切片可精勾（G3）。

**范围快照语义**：保存那一刻把 `fragment_text`（G2）与 `chunk_ids`（G3）文本固化下来。文档重解析/重向量化后，**历史范围不漂移**（证据已快照）。

### 4.2 后端范围构建器

新增 `services/scope_service.py`（或并入 `knowledge_service.py`），提供：

- `resolve_scope(scope_id|inline) -> {docs, mode, fragments, chunks}`：把范围对象展开为检索可消费的结构。
- `scope_from_text(text, conn) -> scope`：自然语言圈定解析（整篇→G1；含"节/段/参数"等词→G2/G3，用 `document_chunks.section` 匹配）。
- `save_scope(...)` / `list_scopes()` / `delete_scope()`：范围 CRUD。
- `scope_preview(scope) -> {doc_count, chunk_count, sample_snippets}`：设定后预览。

### 4.3 检索接入点（已就绪，只接数据）

`search.py::_doc_clause` / `rag.py` 已支持 `doc_names` 硬过滤。范围构建后：
- G1 → 传 `doc_names=[...]`（向量侧）+ `source_docs=[...]`（图谱实体侧，均已有）。
- G2/G3 → 取 `fragments` 文本拼为 `attachment_text` 传入 `retrieve`，`_match_attachment` 词法+语义双路召回作**首选证据**。

---

## 5. 详细设计：怎么"选范围"并传给建模（缺口 B）

### 5.1 请求体扩展（`ChatIn`）

在 [conversation.py](file:///C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/models/conversation.py#L12-L19) 增加：

```python
scope_id: Optional[int] = None          # 引用已保存范围
scope: Optional[dict] = None            # 或内联：{"mode":"doc|fragment_group|chunk",
                                        #          "doc_names":[...], "fragment_text":"...", "chunk_ids":[...]}
```

前端勾选"仅基于所选范围"时带上；不勾选则保持现状（软 `#` 标签），完全向后兼容。

### 5.2 后端透传（`chat_stream` → `execute_stream`）

- [conversations.py::chat_stream](file:///C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/routers/conversations.py#L148-L182) 把 `body.scope_id`/`body.scope` 透传。
- `execute_stream` 增加关键字参数 `scope=None`；在其内部：
  - `scope_id` → `scope_service.resolve_scope(scope_id)`；`scope` 内联 → 直接展开。
  - 合并：`effective_scope = dict(agent_def.kb_scope or {}); effective_scope.update(request_scope)`（请求级覆盖 Agent 定义级，且**明确文档范围优先**；分支语义保留 Agent 定义）。
  - 将结构化检索上下文替换/增加 prior 到 prompt 注入块：
    - G1：`rag.retrieve(retrieve_query, branch, kb_scope={'docs': doc_names, ...})`
    - G2/G3：`rag.retrieve(retrieve_query, branch, attachment_text=fragments, kb_scope={'docs': doc_names})`
  - 检索词侧：范围理解命中时，允许在 query 中注入范围名/文档名关键词辅助（可选），但**不依赖**（硬过滤已兜底）。

### 5.3 前端：AI 建模输入区

- 在输入框工具条新增「范围」按钮（复用现有工具 popover 风格），点开：
  - 选择已保存范围（`list_scopes`），或内联勾选文档（复用 `openKBPicker` 的文档全列表数据源）。
  - 选中后输入区展示范围 chip（如「📚 范围：载荷指标-方案基线」），旁边 ✕ 可取消。
- 发送时：若 chip 存在 → `body.scope_id` 带上；若只是勾选文档且要硬锁 → 升级为 `body.scope={'mode':'doc','doc_names':[...]}`（不再依赖软 `#`）。
- 保留原 `#文件名` 软行为作为"未声明硬范围"的降级。

### 5.4 溯源与展示

- 命中切片沿用现有 `【来源i】 document §chunk_index: content` 注入格式不变。
- 前端引用块沿用现有「引用来源 [n]」「一键追溯来源」；范围建模完成后可在产物摘要里回填「基于范围：{name}（N 文档 / M 片段）」，落 `messages.card_data.scope`，历史可查。

---

## 6. 详细改点清单（按文件）

| 文件 | 改动 | 说明 |
|---|---|---|
| `database/migrations.py` | 新增 `modeling_scopes` 建表 | 幂等 `_add` 模式，全局表不分分支 |
| `models/conversation.py` | `ChatIn` 加 `scope_id` / `scope` | 向后兼容 |
| `routers/conversations.py` | `chat_stream` 透传 scope | chat / chat_stream / clarify 可复用 |
| `agent/pipeline.py` | `execute_stream` 加 `scope`，合并 `effective_scope`，G1 传 `kb_scope.docs`，G2/G3 传 `attachment_text` | 主链路接入点；`execute`（非流式）同步补 |
| `agent/rag.py` | 无需核心改动（`kb_scope.docs`/`attachment_text` 已支持）；确认 G3 精确 chunk 注入路径 | 仅做兼容确认 |
| `services/scope_service.py`(新) | 范围 CRUD / 自然语言解析 / 预览 | 复用现有文档与切片查询 |
| `static/index.html` | 文件管理「建模范围」面板 + AI 建模输入「范围」chip | 沿用现有 kb 子页/AI 页样式 |

---

## 7. 边界、降级与安全

- **不指定范围**：完全回到现状（软 `#` + 默认 release 图谱）。新增逻辑零影响。
- **范围无效/文档已删除**：`resolve_scope` 失败 → 降级为"无范围"并提示，不阻断建模。
- **置空片段**（G2/G3 空文本）：降级为 G1（按 doc_names 硬过滤），不静默全库。
- **分支守卫**：`_release_guard` 等写保护不受影响（本能力只读召回）；图谱实体仍按分支，文档范围全局（P1）。
- **性能**：范围脚本化展开、检索硬过滤复用现有索引；单次建模只多一次范围解析，开销可忽略。
- **权限**：`modeling_scopes` 为全局资源，写操作走既有用户鉴权与审计（`audit`）。

---

## 8. 验收口径

1. **设定**：文件管理可新建/命名/保存范围；整篇（G1）与"某节/某段"（G2/G3）两种设定方式均可用；范围预览显示文档数与片段数。
2. **选择**：AI 建模输入可仅基于所选范围，请求体带 `scope_id`/`scope`。
3. **硬边界**：带 G1 范围时，检索结果 `source_doc` 全部命中范围内文档（抽样断言，无越界召回）。
4. **证据优先**：带 G2/G3 范围时，命中切片以所选片段为首要证据（`attachment_used`/引用块可见）。
5. **无范围回归**：不选范围 → 行为与改动前一致（`e2e.js` ALL_PASS）。
6. **溯源**：范围建模产物带 `source_doc/section/chunk_index` 引用，可点开追溯。
7. **产物链**：建模仍产出 SysML 代码 + 视图 + 版本归档，`_e2e_ai.js` 的代码/视图 tab 断言通过。

### 8.1 测试脚本建议

- `_chk_scope.py`：范围 CRUD + G1 硬过滤召回断言 + G2/G3 片段注入断言（可直接调 `scope_service` 与 `rag.retrieve`）。
- `_e2e_scope.js`：Playwright「AI 建模页选范围 → 发送 → 断言结果中含所选文档引用」。
- 回归后跑 `_e2e_ai.js`（4/4）与 `e2e.js`（ALL_PASS）。

---

> 备注：本文档为设计方案（未改任何代码）。确认方案后按 §6 清单进入实现；实现时遵循既有补丁规范（index.html / pipeline.py 按需行尾处理、幂等锚点、改后 `node _validate.js` + 服务起 8000 验证）。

> 实施状态：**v1.0 已实现（2026-08-23）**。modeling_scopes 表 / services/scope_service.py / ChatIn.scope_id+scope / conversations.py 透传+范围接口 / pipeline.py execute&execute_stream 消费 scope / 前端范围按钮+chip+发送 scope_id。文件管理=全局数据(branch=global)。验证：后端 py_compile 全过；index.html JS BAD=0；Playwright 页载+范围面板+chip 通过；pipeline `_resolve_req_scope` 双路径 PASS。
> 交互增强（2026-08-23）：范围面板新增「按文档切片圈定」(G3)——选文档→加载其分片(chunk)→章节分组勾选→「保存为范围并选中」，mode='chunk'，选中切片文本作证据(fragment_text)+ 所属文档硬锁。后端 `/api/scopes/docs/{id}/chunks` 返回分片，`save_scope` 存 chunk_ids+片段快照。