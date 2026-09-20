# AI 产物收编资料库方案

> 版本：v1.0（2026-09-15）
> 关联文档：《知识管理模块对比分析与优化方案.md》（P0-3 知识分类）、《AI建模会话产物管理设计方案.md》（artifacts）
> 行业对标结论：模式与 Dify 标注回流 / Coze 人工同步 / OpenAI Vector Store"产物即知识"一致；含 model collapse 对策

---

## 1. 背景与目标

资料库（documents 管道）目前只消费**用户主动上传**的资料；AI 建模生成的报告、代码、SysML 模型沉淀在会话产物（artifacts）与报告（reports）里，**没有向量化、没有分类、AI 检索不到**——知识闭环断在"收编"这一步。

**目标**：AI 产物可一键收编入资料库（向量化 + 知识分类 + 溯源），收编后可被检索/管理；同时对"AI 生成内容回流再消费"的行业已知风险（model collapse / 自食循环）建立双重闸门。

**关键前提修正（2026-09-15 米爸确认）**：报告已无人工审核环节、报告管理模块已移除——因此**不存在"定稿"门禁**。门禁后移为**收编动作本身**（显式点按 = 人审），与 Dify/Coze 的"沉淀需人工触发"模式同构。自动收编仅保留 `sysml_versions.adopted=1`（版本采纳，系统里仅存的显式人工确认动作）一个触发点。

---

## 2. 总体架构（收编桥接，不搬家）

```
artifacts(kind=report/code/sysml/document)          ← 过程产物，留在会话侧
reports / sysml_versions                            ← 结构化归档，原样保留
        │
        │ ① 显式收编：artifacts 面板「📥 收编入资料库」（选分类）
        │ ② 自动收编：SysML 版本采纳（adopted=1）钩子
        ▼
services/artifact_ingest.py
        │  serialize_artifact（序列化为文本字节）
        │  查重（bigram 相似度 ≥0.92 → 需确认，override 可覆盖）
        ▼
knowledge_pipeline.ingest_upload_document           ← 现成管道，零改造
        │  落盘 data/uploads → 解析 → 分块 → 向量化
        ▼
documents（origin='ai_generated', source_artifact_id, knowledge_category）
document_chunks（origin='ai_generated'，块级可过滤）
        ▼
检索消费：资料库检索照常；AI 建模 RAG（GraphRAG.retrieve）
默认排除 ai_generated（kb_scope.include_ai_generated=true 才纳入）
```

**明确不做的**：artifacts 表合并进 documents（过程索引 ≠ 知识资产）；收编时自动抽取实体入图（SysML 走 `sysml_versions` 专用入库链路，文本抽取已有独立开关）。

---

## 3. 数据模型（幂等迁移）

```sql
-- documents：来源溯源
ALTER TABLE documents ADD COLUMN origin TEXT DEFAULT 'upload';            -- upload | ai_generated
ALTER TABLE documents ADD COLUMN source_artifact_id INTEGER DEFAULT 0;    -- 收编来源产物
-- document_chunks：块级冗余（检索过滤免 JOIN，命中结果可标注）
ALTER TABLE document_chunks ADD COLUMN origin TEXT DEFAULT 'upload';
-- doc_metadata：时效取代链（对齐 sysml_versions.superseded 语义）
ALTER TABLE doc_metadata ADD COLUMN superseded_by INTEGER DEFAULT 0;      -- 被哪个新文档取代
```

检索命中结果（`search_chunks` 返回）补充 `origin` 字段，供前端/Agent 标注「AI 生成」。

---

## 4. 后端设计

### 4.1 新增服务 `services/artifact_ingest.py`

```python
def serialize_artifact(conn, a: dict) -> tuple[str, str, bytes]:
    """产物 → (filename, file_type, content_bytes)。复用 download 端点的三级降级：
    ① file_path 落盘文件直读 ② report → meta.sections 渲染 markdown
    ③ code/sysml/document → preview_content（sysml 存 .sysml，其余 .md）"""

def suggest_category(conn, kind: str) -> str:
    """kind → 知识类别建议（仅取 knowledge_categories 已存在的名）：
    code/sysml → 可复用构件（设计资产）；report/document → ''（用户自选）"""

def find_similar(conn, filename, content: bytes) -> dict:
    """收编前查重：新内容首 500 字 bigram 向量 vs 存量文档 chunks 批量余弦。
    文档级取最高分；≥0.92 → {similar_doc_id, similarity}（提示已有近似版本）"""

def ingest_artifact(conn, artifact_id, knowledge_category="", actor="", override=False) -> dict:
    """收编主流程：
    ① serialize + suggest ② find_similar（未 override 且命中 → needs_confirm 返回）
    ③ ingest_upload_document 走完整管道 ④ 回写 origin/source_artifact_id/knowledge_category
       + chunks.origin ⑤ supersede：同 artifact 的旧收编文档 doc_metadata.superseded_by=新id
    ⑥ audit 留痕"""
```

### 4.2 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/documents/ingest-artifact` | POST | body `{artifact_id, knowledge_category?, override?}`；返回 `{ok, doc_id, chunk_count, category, superseded[], similar?}`；needs_confirm 时 HTTP 409 |
| `GET /api/documents` | GET（修改） | 新增 `origin` 筛选参数（`upload`/`ai_generated`/空=全部） |
| `POST /api/sysml-versions/{id}/adopt` | POST（修改） | 采纳成功后 best-effort 自动收编其 artifact（`override=True`，失败不阻断采纳，结果附 `auto_ingest` 字段） |
| `search_chunks` | 函数（修改） | 新增 `exclude_ai: bool = False`；两个内部检索 SQL 增加 `AND (origin IS NULL OR origin!='ai_generated')`；命中 dict 补 `origin` |
| `agent/rag.py::GraphRAG.retrieve` | 函数（修改） | 向量兜底调用传 `exclude_ai = not kb_scope.get("include_ai_generated")`——**AI 建模 RAG 默认不消费 AI 生成文档** |

### 4.5 权限与审计

- 收编端点挂 `DOC_WRITE_PERMS`；自动收编（采纳钩子）继承采纳者身份。
- 每次收编写 audit：`artifact_ingest`（产物 #id → 文档 #id，N chunks，分类）。

---

## 5. 前端设计

| 位置 | 改造 |
|---|---|
| 会话产物右键菜单（`artContextMenu`） | 新增「📥 收编入资料库」：promptDialog 选/填知识类别（message 中列出现有类别 + 按类型建议），409 时 confirmDialog「已存在相似文档（相似度 0.9x），覆盖收编？」 |
| 资料库列表（`20-docs.js`） | 筛选栏加「来源」下拉（全部/用户上传/AI 生成）；文件名旁 `origin==='ai_generated'` 加 🤖 徽章 |
| 检索命中 | 命中预览带 `origin` 时标注「AI 生成」（有 origin 字段即渲染，渐进增强） |

---

## 6. 对标落地的三道防线（model collapse 对策）

| 防线 | 机制 | 状态 |
|---|---|---|
| ① 收编门禁 | 显式收编 = 人审；无"自动入池"旁路（采纳钩子也是人工动作的延续） | 本方案 |
| ② 消费隔离 | AI 建模 RAG 默认排除 ai_generated；kb_scope 显式开启才纳入 | 本方案 |
| ③ 来源可溯 | documents.origin + source_artifact_id + chunk.origin + 命中标注 | 本方案 |

配套治理项：查重防近重复污染（≥0.92 需确认）、`superseded_by` 防旧版本误导（检索侧后续可在 rerank 时降权 superseded 文档，V2）。

**遗留项（不在本方案）**：检索质量天花板升级（真 embedding 重嵌 → 双路召回 RRF 融合 → 头部 reranker），单独立项。

---

## 7. 实施清单与验证

| 文件 | 改动 |
|---|---|
| `database/migrations.py` + `schema.py` | `_migrate_artifact_ingest`：3 组幂等补列 |
| `services/artifact_ingest.py` | 新建（serialize/suggest/find_similar/ingest_artifact） |
| `routers/meta.py` | `POST /documents/ingest-artifact` + 列表 origin 筛选 |
| `repositories/meta_repo.py` | `list_documents_with_meta` 加 origin 参数 |
| `knowledge_pipeline/search.py` | exclude_ai 参数 + SQL 过滤 + 命中带 origin |
| `agent/rag.py` | retrieve 向量兜底默认排除 |
| `routers/sysml_versions.py` | adopt 钩子自动收编 |
| `static/js/mods/09-impact.js` / `20-docs.js` / `index.html` | 收编入口 / 来源筛选与徽章 |
| `tests/manual_verify/verify_artifact_ingest.py` | 全链路验证 |

验证口径：收编后 `documents.origin='ai_generated'` 且 chunk_count>0、分类正确；重复收编触发 409 查重确认、override 后旧文档 `superseded_by` 正确；`exclude_ai=True` 时检索结果不含 ai_generated chunks；采纳 SysML 版本自动产生收编文档。
