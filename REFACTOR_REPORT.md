# mbse_system 工程解耦拆分报告（2026-09-09）

> 三项任务：① uploads 清理 ② 清理内容说明 ③ 后端大文件拆分。全部完成并回归验证通过。

## 一、data/uploads 清理（48.6 MB → 16.8 MB）

- **保留 14 个活文件**（与 `documents` 表 15 条记录一一对应，按 `{docid}_{原文件名}` 规则匹配；doc 750 无文件本就不存在）。
- **归档 154 个文件 / 31.8 MB** → `_archive/uploads-cleanup/`（只移不删，可随时恢复）。
- 数据库引用走 `document_chunks.document_id` 外键 + 原文件名，旧 id 前缀文件无任何记录引用，删除无副作用。

## 二、清理的内容是什么（问题 2 答复）

| 内容 | 数量 | 性质 |
|---|---|---|
| `*_pipe_test.txt` / `*_kbp0_doc.md` / `*_s1flow.md` | 88 | **流水线联调合成语料**：宽带载荷（V波段/相控阵/转发器）文本反复拼接的假文档，测试摄取/抽取用 |
| `*_TST-BR-发布快照.md` / `*_TST-BR-快照-*` | 17 | 分支发布快照功能的测试文档（"验证 dev→release 发布文档快照"） |
| `*_e2e_*.md` / `*_batch_*.md` / `*_fail_once.md` / `_tmp/_test` | 25 | E2E 与批量上传测试残留 |
| `广汽方法论-合稿1.docx`（24/29/36/43/47 前缀） | 5 | 同一文档的历史重复上传（6.3MB×5，现行文件 97_ 保留） |

**结论：全部是测试残留与历史重复上传，无业务数据损失。**

## 三、后端三大文件拆分（机械切分，零行为变更）

| 文件 | 拆分前 | 拆分后 | 方式 |
|---|---|---|---|
| `agent/pipeline.py` | 4551 行 / 276KB | **81 行** 入口 + `pipeline_parts/` 11 个 Mixin | 单类 God Class → Mixin 多继承 |
| `routers/knowledge.py` | 3233 行 / 168KB | **10 行** 入口 + `knowledge_parts/` 8 个路由分片 + shared | 共享 router 对象分片注册 |
| `routers/studio.py` | 2158 行 / 105KB | **13 行** 入口 + `studio_parts/` 11 个路由分片 + shared | 同上 |

**pipeline Mixin 分片**：session（会话/澄清）、skills、orchestration（团队编排）、memory、prompt、tools、execute（主流程）、stream（流式 SSE）、cards（SysML/卡片/变更影响）、history（历史/上下文预算）、context（组装/重排）+ common（公共依赖）。

**knowledge 分片**：entities / graph / glossary / graph_query / stats / ontology / ontology_version / pipeline（V2G/三元组/摄取/SysML）。**studio 分片**：prompts / skills / mcp / tools / rules / flows / agents_reg / hooks_copy / agents / a2a_events / market。模块级私有 helper 全部收敛到各包 `shared.py`。

### 验证结果（全过）

- AST 对比：pipeline 73 个方法零丢失；knowledge 120 条路由、studio 111 条路由 集合完全一致
- `py_compile` 全部通过；`router.routes` 运行时挂载数 knowledge=120、studio=123
- 服务重启后 12 个跨域接口 HTTP 全 200；agent 干跑（`test_agent`）正常路由 intent=design
- Playwright 前端全页面回归 **ALL PASS**（无 JS 错误、无失败请求）
- 备份：`_archive/bak/{pipeline,knowledge,studio}.*.pre-split`；拆分脚本 `tools/split_pipeline.py` / `split_router_knowledge.py` / `split_router_studio.py` 可复现

## 四、剩余大文件（可选后续）

- `database/migrations.py` 102KB（一次性 DDL 序列，拆分收益低）
- `sysml_importer.py` 82KB（解析器单模块，可按 解析/校验/落库 三段拆）
- `static/js/mods/21-ontology.js` 168KB（前端本体工作台，可二次拆）

**拆分后全工程最大源码文件 210KB（index.html 骨架），原 1.66MB/276KB/168KB/105KB 四个超大文件全部消失。**
