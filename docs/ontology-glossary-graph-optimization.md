# mbse_system 本体 / 词典 / 图库 设计评审与优化方案

> 评审对象：`C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system`  
> 评审依据：本体与知识图谱核心概念（ISO/IEC 21838、W3C OWL 2 / RDF 1.1 / SKOS / SHACL）、实体定义与图数据管理规范、术语词典规范（ISO 704:2022 / ISO 1087:2019 / ISO 25964-1 / ISO 30042 / SKOS）  
> 评审日期：2026-09-06

---

## 一、摘要（一页纸结论）

### 1.1 总体判定

| 维度                                                                | 现状评级   | 一句话结论                                                         |
| ----------------------------------------------------------------- | ------ | ------------------------------------------------------------- |
| 本体设计                                                              | **B+** | 类/属性/特性公理/不相交/基数都已落地并导出 OWL，缺统一命名空间、版本与项目隔离                   |
| 术语词典                                                              | **C**  | 只做到"字符串→字符串"归一化，缺概念层、状态机、语言标签、定义与审计                           |
| 图数据库                                                              | **B-** | 已是 RDF 三元组 + SPARQL + 多后端抽象，但 IRI 与本体脱节、类型谓词自建，语义推理失效         |
| 数据治理                                                              | **A-** | 审核暂存、写前融合闸、canonical 双层模型、分支命名图，这块做得比多数同类项目好                  |
| 前端                                                                | **C**  | 1.4MB / 2.1 万行单文件内联 SPA，功能齐但无法持续演进                            |
| 溯源与检索                                                             | **C-** | 三元组无结构化溯源；检索为内存 BM25 倒排 + 术语子串匹配，与标杆（Lucene/ES 连接器、概念级标注）差距最大 |
| 质量度量                                                              | **C**  | 无 KG 健康度指标体系与 Problems 面板，数据质量不可度量、不可见                        |
| **核心结论：不是"没有本体"，而是"本体、图库、词典各说各话"。工程缺的不是模块，是把三个模块缝在一起的 IRI 与概念层。** |        |                                                               |


### 1.2 问题清单

| 编号       | 级别 | 问题                                                                                                             | 影响                                                          |
| -------- | -- | -------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| **P0-1** | 致命 | 三套命名空间互不解析：本体用 `http://www.xingwang.mbse/ontology#`，图库用 `urn:mbse:*`，且图库类型谓词是自造的 `urn:mbse:type` 而非 `rdf:type` | OWL 公理对图库数据**完全无效**，无法做任何语义推理                               |
| **P0-2** | 致命 | 实体 IRI 由名称派生（`urn:mbse:ent:{loc_key(name)}`），改名即换 IRI；`make_iri` 冲突加 `_2` 依赖插入顺序                               | 违反 IRI 幂等与持久化原则，实体改名=断链                                     |
| **P0-3** | 严重 | 术语词典无概念层：`glossary` 是 `user_term → canonical_term` 字符串映射，`canonical_term` 无 ID                                 | 术语改名要全表 UPDATE；无法做同形异义检测、无法多语言                              |
| **P0-4** | 严重 | `entity_aliases`（实体别名）与 `glossary`（词表）**双轨并存**，都指向 canonical 但互不同步                                             | 同一概念两套别名，治理成本翻倍                                             |
| **P1-1** | 重要 | 推理靠 Python 硬编码：`TRANSITIVE_RELATIONS = ("包含","连接","追溯","满足","派生","执行","SATISFIES")`                            | 新增传递关系要改代码；中英混列易漏；与本体里的 `owl:TransitiveProperty` 声明重复且可能不一致 |
| **P1-2** | 重要 | 约束职责混淆：`required/unique` 被写成 `owl:Restriction` 基数；已生成的 SHACL（`shacl_export`）未接入入库门禁                            | 约束违反会让推理机判"本体不一致"，而不是报"这条数据不合规"                             |
| **P1-3** | 重要 | `ontology_types` 表**无 `project_id` / `branch` 列**（`entities`/`relations` 都有）                                   | 多项目共用一个本体，无法实现"内核本体 + 领域包"切换（与 D-1/D-2/D-4 冲突）              |
| **P1-4** | 重要 | 图库是"查询镜像"，SQLite 是权威源，双写一致性靠迁移脚本                                                                               | 存在漂移风险，缺版本号与原子切换                                            |
| **P2-1** | 一般 | 词典匹配用子串包含（`user_term in text.lower()`）                                                                         | "ODS" 等短词误命中严重                                              |
| **P2-2** | 一般 | 前端 `static/index.html` 1.4MB / 21178 行内联                                                                       | 任一模块改动都要动巨型文件，无法并行开发                                        |
| **P2-3** | 一般 | 术语无 `deprecated` 状态流转与影响分析                                                                                     | 旧术语残留在实例中无法发现（对应调研中的 G4 查询）                                 |
| **P0-5** | 致命 | 三元组无结构化溯源：只有 `source_doc` 文本，无 `chunk_id` / `extract_run_id` / `commit_id`（对标 EDG 语句级标注、W3C PROV-O）            | 审核无法回答"这条关系从哪句话来"；模型升级无法定位受影响数据                             |
| **P1-5** | 重要 | 检索是零依赖内存 BM25 倒排（仅覆盖 `document_chunks`）+ 术语子串匹配（对标 GraphDB Lucene/ES 连接器、PoolParty 10.2 精确短语匹配）                | 规模化后索引常驻内存且冷启动重建；短词（ODS/LBP）误命中                             |
| **P1-6** | 重要 | 本体无版本与发布状态：`ontology_owl.py` 无 version，`ontology_types` 无 status（对标 EDG 虚拟在制副本 + 受控发布）                         | 本体变更无法灰度、无法回滚，旧实例无法按发布时语义解释                                 |
| **P1-7** | 重要 | 无 KG 健康度指标体系（对标 GraphDB 一致性规则集、EDG Problems & Suggestions 面板）                                                  | 数据质量不可度量，治理看板无客观依据                                          |
| **P1-8** | 一般 | 无统一查询网关（对标 EDG/GraphDB GraphQL、Stardog BI/SQL）                                                                 | 前端直连散装 REST，接口随页面膨胀                                         |
| **P2-4** | 一般 | LLM 未用于术语/本体构建（对标 PoolParty Taxonomy Advisor、Stardog Voicebox、EDG 8.0 向量自动打标）                                  | 术语与本体仍全靠手工，冷启动慢                                             |
| **P2-5** | 一般 | 无外部词表对齐与变更监控（对标 EDG 8.0 MTM Accelerator）                                                                       | SysML v2 / ISO 15288 / GB-T 术语升级时无法自动发现受影响概念                |

---


## 二、现状盘点（已有能力，别重复造轮子）

| 能力             | 实现位置                                                                                                                                                                                      | 评价                                   |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| 本体类型体系         | `database/schema.py` → `ontology_types`（`type_kind = entity/relation/attribute` + `parent_id` + `properties` JSON）                                                                        | 弱 TBox，够用但缺多继承与等价类                   |
| OWL 导出         | `ontology_owl.py`：`to_owl()` / `to_turtle()`，含 `owl:Class` / `ObjectProperty` / `DatatypeProperty` / `rdfs:domain,range` / `subClassOf` / `disjointWith` / 6 种特性公理 / `owl:Restriction` 基数 | **做得好**，可直接对接标准工具                    |
| 本体校验与 SHACL 导出 | `ontology_semantics.py`：`OntologyValidator.validate_node/validate_edge` + `shacl_export()`；`make_iri()` 支持 hash-name/uuid 策略                                                              | **已有 SHACL 产物，只差接入门禁**               |
| 图库抽象           | `graph_db.py`：`PyoxigraphWriter`（默认）/ `FusekiWriter` / `Neo4jWriter` / `MockWriter`，支持 SPARQL、命名图（按 branch）、N-Quads 导出                                                                    | **架构正确**，可无痛切 Jena                   |
| 三元组审核          | `triple_store.py`：SQLite `triples` 表 + `review_queue` / `batch_review` + 幂等 `triple_id`                                                                                                   | 治理链完整                                |
| 写前融合闸          | `staging_fuse.py` + `v2g_candidates`（`candidate_kind` / `normalized_key` / `_canopy_key` 分桶）+ `entity_dup_candidates` / `entity_merges`                                                   | **符合调研中的"分桶→打分→融合"范式**               |
| canonical 双层模型 | `entities.canonical_id` + `entity_aliases`                                                                                                                                                | 思路正确，但被 P0-3/P0-4 削弱                 |
| 推理             | `ontology_reasoning.py`：`classify_instances` / `transitive_closure` / `consistency_check`                                                                                                 | 手写实现，应改为读本体公理 + OWL RL / SPARQL 属性路径 |
| 术语归一化          | `glossary.py`：`GlossaryMatcher.normalize/resolve` + `query_trace` + `domain_review_queue`                                                                                                 | 检索侧完整，治理侧缺失                          |



---

## 三、行业标杆对标与差距补充

> 第二章解决"工程内部自洽"，本章解决"与外部标杆的差距"。  
> 对标口径：**只对标可直接迁移到本工程的能力**，不比拼企业级生态（连接器数量、SSO、集群规模）。

### 3.1 标杆选型与理由

| 标杆                                       | 定位                         | 为什么选它对标                                                     |
| ---------------------------------------- | -------------------------- | ----------------------------------------------------------- |
| **PoolParty / Graphwise Graph Modeling** | 术语—本体一体化建模                 | 术语/分类法管理第一梯队，SKOS 原生、LLM 辅助建模、40+ 语言；本工程"术语词典"缺的正是对标物       |
| **Ontotext GraphDB**                     | RDF 图库 + 推理 + 全文检索         | 本工程 `FusekiWriter` 的目标形态；其规则集、Lucene 连接器、RDF Rank 是图库侧的能力刻度 |
| **Stardog**                              | 企业 KG 平台（虚拟图 + 查询时推理）      | 提供"推理与校验"的另一条路线（just-in-time vs forward-chaining），对准 P1-2   |
| **TopQuadrant TopBraid EDG**             | 业务术语表 + 本体 + 数据质量 + 工作流一体化 | 唯一把"术语表/参考数据/本体/数据质量/工作流"放进同一 KG 的产品，正对本工程治理链的下一阶段          |
| **OMG SysML v2 API & Services**          | 领域契约参考                     | 不是 KG 产品，但决定本工程"对外契约"该长什么样（2025-09 正式发布，与 P0-1 后的语义主线对齐）    |

**明确不对标**：Neo4j（LPG 路线，与 P0-1 之后的 RDF 主线冲突，保留为导出适配器）；通用数据目录产品（Collibra / Alation，治理对象不同）。


### 3.2 能力对标矩阵

| 能力维度               | 标杆水位                                                                                            | mbse_system 现状                                                                                    | 判定          | 对应项                 |
| ------------------ | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ----------- | ------------------- |
| 术语/本体一体化建模（概念层）    | PoolParty：SKOS 原生、概念树拖拽、多语言 40+、状态机、全量审计                                                        | `glossary` 是字符串映射，无概念层                                                                            | **差距大**     | P0-3 / P0-4（已有）     |
| 三元组级溯源（Provenance） | EDG 6.3 起支持对 KG 语句加注（来源、生效日期）；W3C PROV-O（REC 2013-04-30）                                        | 仅 `source_doc` 文本字段，无 `chunk_id` / `extract_run_id` / `commit_id` 结构化关联                           | **缺失**      | **P0-5（新增）**        |
| 全文检索与概念检索          | GraphDB：Lucene / Solr / ES / OpenSearch 四类连接器；PoolParty 10.2：原生 Lucene 重做标注引擎，40+ 语言、精确短语匹配     | `BM25Engine` 为**零依赖内存倒排**（bigram 切分），仅覆盖 `document_chunks`；术语匹配是 `user_term in text.lower()` 子串包含 | **差距大**     | **P1-5（新增）**        |
| 本体版本与发布状态          | EDG：资产集合虚拟在制副本 + 受控发布/评审/审批工作流；PoolParty：逐变更历史与审计轨迹                                             | `ontology_owl.py` 无 version 字段，`ontology_types` 无 status                                          | **缺失**      | **P1-6（新增）**        |
| 数据质量度量             | GraphDB：自定义一致性检查规则集；EDG：数据质量规则 + Problems & Suggestions 面板（8.0 起表格化、可过滤排序）；PoolParty：质量设置与内置校验器 | 有零散 `metric`，无 KG 健康度指标体系                                                                         | **缺失**      | **P1-7（新增）**        |
| 统一查询网关             | EDG / GraphDB：GraphQL；Stardog：BI/SQL Server；GraphDB：JDBC                                        | 纯 REST，字段散落，前端 1.4MB 内联直连                                                                         | **缺失（但暂缓）** | **P1-8（新增）**        |
| 推理方式               | GraphDB：前向链全物化（OWL 2 RL / QL / RDFS-Plus）；Stardog：查询时推理 + 可解释约束                                 | Python 硬编码传递闭包                                                                                    | 路线已定，实现待改   | P1-2（已有）            |
| 约束校验               | Stardog：SHACL + ICV 可解释；GraphDB：RDF4J SHACL                                                     | SHACL 已能导出，未接入门禁                                                                                  | **只差接线**    | P1-1（已有）            |
| 数据虚拟化              | Stardog Virtual Graph（30+ 连接器）；GraphDB + Ontop（OBDA，支持 Dremio / Databricks / Snowflake）         | 无                                                                                                 | **当前不必要**   | P2-6（观察）            |
| LLM 辅助建模           | PoolParty Taxonomy Advisor + Corpus Analysis；Stardog Voicebox；EDG 8.0 内置向量库一次性自动打标              | 已有 LLM 接入与审核队列，但未用于术语/本体构建                                                                        | **低成本高收益**  | **P2-4（新增）**        |
| 外部词表对齐与变更监控        | EDG 8.0 MTM Accelerator：自动检测外部词表变化并生成对齐变更                                                       | 无                                                                                                 | **缺失**      | **P2-5（新增）**        |
| 可视化交互              | EDG：NeighborGram 邻域图、3D 图面板、Problems 面板、可配置布局；PoolParty：颜色编码 + 拖拽分类树                            | 图谱着色依赖类型数据（被 P0-1 卡住）；无 Problems 面板、无概念树拖拽                                                        | 部分差距        | **见 3.5 / 6.5（新增）** |


### 3.3 新增差距项与落地方案

#### P0-5　三元组级溯源（Provenance）缺失 —— 致命（合规关键）

**现状**：`relations` 表有 `source_doc TEXT`（文档名字符串）与 `confidence`，但没有 `chunk_id`、`extract_run_id`、`commit_id`。三个后果：

1. 审核员问"这条 `R-101 satisfiedBy F-204` 是从哪句话抽出来的" → **答不出来**；
2. 抽取模型升级后，无法定位受影响的旧三元组做重跑或降级；
3. `confidence` 缺少来源上下文，无法做置信度阈值回放与审计。

**标杆做法**：EDG 6.3 起支持"用额外语句标注知识图谱中的事实"（例：术语定义的来源、任意值的生效日期）；W3C PROV-O（REC 2013-04-30）提供标准词汇。

**落地**（分两段，先关系库、后 RDF）：

1. 新增 `triple_provenance(triple_id, source_doc_id, chunk_id, extract_run_id, commit_id, confidence, created_at)`，写入点统一收敛到 `triple_store.add_triples()`，禁止业务代码各写各的；
2. RDF 侧**先用命名图方案**（`urn:mbse:prov:{extract_run_id}` 承载溯源断言）——**不要**上 RDF 1.2 reification：RDF 1.2 Concepts 目前是 CR（2026-04-07），SHACL 1.2 的 `sh:reifierShape` 仍是 WD（2026-07-20），均未定稿。

#### P1-5　检索层：从"内存倒排 + 子串匹配"到"统一检索"

**现状**：`knowledge_engine.py::BM25Engine` 是零依赖内存倒排索引（bigram 切分 + 停用词），只覆盖 `document_chunks`；`glossary.py::GlossaryMatcher` 用子串包含匹配。

**风险**：内存 BM25 在 chunk 达百万级时索引常驻内存、冷启动需重建；子串匹配对短词（ODS、LBP、CPDS）误命中严重（P2-1）。

**标杆做法**：GraphDB 提供 Lucene / Solr / Elasticsearch / OpenSearch 四类 FTS 连接器；PoolParty 10.2 用原生 Lucene 重做概念标注引擎（FST 替代外部词形服务，40+ 语言，零手工刷新索引），并提供"精确短语匹配"（Mandatory 过滤 / Ranking Boost 两档），专门解决 `TAK-123` 这类标识符被拆碎的问题。

**落地**：

- 本地/私有化降级：**SQLite FTS5**（`unicode61` + `trigram`），把 `bm25_text` 落到 FTS 虚表替换内存倒排，零外部依赖；
- 图库侧：Fuseki / GraphDB 用 Lucene 连接器索引 `rdfs:label` / `skos:altLabel` / `skos:definition`；
- 术语匹配改为**先召回概念（`concept_id`）再匹配实例**（依赖 P0-3 概念层），禁止对长文本做全词表子串扫描；
- 标识符类查询（需求号、零件号）走**精确匹配通道**——这正是 PoolParty 10.2 Exact Phrase Matching 解决的问题。

#### P1-6　本体版本与发布状态机

**现状**：`ontology_owl.py` 无版本字段；`ontology_types` 无 `version` / `status`。

**标杆做法**：EDG 提供"资产集合的虚拟在制副本"，支持并行开发多版本 + 受控发布、评审、审批工作流；PoolParty 提供逐变更的历史与审计轨迹（enterprise governance）。

**落地**：与 P1-3（项目隔离）合并实施 —— `ontology_types` 增加 `project_id` / `branch` / `version` / `status(draft|review|published|deprecated)`；**发布即生成不可变快照**（`ontology_snapshots` 表 + OWL 文件导出），实例侧记录 `ontology_version`，保证旧数据仍可按发布时的语义解释。

#### P1-7　知识图谱健康度指标体系

**标杆做法**：GraphDB 支持自定义一致性检查规则集；EDG 提供数据质量规则与 Problems & Suggestions 面板；PoolParty 提供质量设置与内置数据校验器（import quality）。

**建议指标（直接写进治理看板）**：

| 指标         | 定义                                 | 目标    |
| ---------- | ---------------------------------- | ----- |
| SHACL 违规密度 | 违规数 / 千条三元组                        | < 1   |
| 需求追溯覆盖度    | 有 `verifies` 的需求数 / 需求总数           | ≥ 95% |
| 孤立实体率      | 无任何关系的实体 / 实体总数                    | < 5%  |
| 术语映射覆盖率    | 已 `mapsToClass` 的概念 / Approved 概念数 | ≥ 80% |
| 未决融合候选     | `v2g_candidates` 未处理数              | 持续收敛  |
| 低置信三元组占比   | `confidence < 0.7` 且已入库 / 总数       | < 10% |
| 弃用术语残留     | 仍使用 Deprecated 术语的实例数              | 0     |

#### P1-8　统一查询网关（GraphQL）—— 建议暂缓

**标杆做法**：EDG 6.3 起提供 GraphQL 读写本体与变更历史；GraphDB 支持 JDBC / GraphQL；Stardog 提供 BI/SQL Server。

**判断：现在不要上 GraphQL。** 前端仍是 1.4MB 单文件，同时承担"拆分模块"与"更换接口"双重风险不划算。顺序应是：先完成 6.2 的模块化 → 再在模块边界引入薄 GraphQL 层（strawberry-graphql 映射现有 repository）。当前性价比最高的替代是给现有 REST 补 OpenAPI 契约并生成 TS 类型。

#### P2-4　LLM 辅助术语与本体构建

**标杆做法**：PoolParty Taxonomy Advisor —— LLM 以企业文档为 grounding 推荐 narrower / related 概念与定义，专家接受/编辑/拒绝，支持单条或批量；Corpus Analysis 用 NLP 从语料抽取候选术语并按相关度打分（官方称单次可抽百万级）；Stardog Voicebox；EDG 8.0 内置向量库做一次性自动打标（zero-shot 分类）。

**落地（本工程条件最好、成本最低）**：已有 LLM 接入 + `domain_review_queue` 审核队列，只需三步：

1. 从 `document_chunks` 抽取候选术语（LLM + 语料相关度打分）；
2. 用现有词典与本体做 grounding（候选 → 命中已有概念 / 判定为新概念）；
3. 进 `domain_review_queue` 人审后写入概念层。

**硬约束**：候选必须走人工审核，禁止自动入库——与存量"写前融合闸"的治理原则保持一致。

#### P2-5　外部词表对齐与变更监控

**标杆做法**：EDG 8.0 Medical Term Management Accelerator 自动检测 SNOMED / MedDRA / ICD / RxNorm 等外部词表的变更并驱动对齐变更管理。

**MBSE 落地**：把 SysML v2 规范术语、ISO/IEC/IEEE 15288、ISO/IEC/IEEE 42010、GB/T 国标术语作为外部词表，用 `skos:closeMatch` / `skos:exactMatch` 对齐；外部词表版本升级时自动生成"受影响概念清单"——复用 P0-3 中弃用影响查询的写法即可。

#### P2-6　数据虚拟化（观察项，暂不做）

Stardog Virtual Graph（30+ 连接器）、GraphDB + Ontop（OBDA）。**当前不需要**：本工程数据源以文档为主，规模在千万三元组以内，ETL 成本低于虚拟化运维成本。**触发条件**：接入 PLM / 需求管理系统的关系库且不允许复制数据时再评估。

### 3.4 明确不做 / 标准观察项

| 项                   | 结论     | 依据                                                                                                         |
| ------------------- | ------ | ---------------------------------------------------------------------------------------------------------- |
| SHACL 1.2 作为基线      | **不做** | SHACL 1.2 Core 仍为 WD（2026-07-20），含 `sh:reifierShape`、`sh:ShapeClass` 等未定稿特性；以 SHACL 1.0（REC 2017-07-20）为基线 |
| RDF 1.2 Reification | **观察** | RDF 1.2 Concepts 为 CR（2026-04-07）；暂用命名图承载溯源                                                                |
| SPARQL 1.2          | **观察** | SPARQL 1.2 Query 为 WD（2026-06-25）；以 SPARQL 1.1 为基线                                                         |
| Neo4j 作为主存          | **不做** | 与 P0-1 之后的 RDF 主线冲突；`Neo4jWriter` 保留为导出适配器                                                                 |
| 企业级 SSO / 集群        | **不做** | 已有角色权限体系，非当前主要矛盾                                                                                           |

### 3.5 前端对标补充（承接 6.3 / 6.4）

| 标杆交互                                 | 出处               | 本工程落地建议                              |
| ------------------------------------ | ---------------- | ------------------------------------ |
| Problems & Suggestions 面板（表格化、可过滤排序） | EDG 8.0          | 治理看板单列"问题"Tab，违规与建议合并展示，按严重度/类型/模块过滤 |
| 概念树拖拽 + 颜色编码                         | PoolParty        | 术语词典与本体共用同一棵概念树组件，拖拽调层级，按状态着色        |
| NeighborGram 邻域图 / 3D 图面板            | EDG              | 图谱工作区增加"邻域展开 N 跳"；3D 面板列为可选          |
| 变更历史时间线 + 评审工作流                      | EDG / WebProtégé | 复用现有合并请求评审时间线组件，覆盖术语状态流转与本体版本发布      |
| 精确短语匹配开关                             | PoolParty 10.2   | 检索框提供"精确/模糊"切换，专门解决需求号/零件号检索被拆碎的问题   |

---


## 四、目标架构

```
                     ┌─────────────────────────────────────────┐
  术语层 (可变)       │  glossary_terms                          │
  语言相关，可增删改  │  term / lang / kind / status / reliability│
                     └──────────────────┬──────────────────────┘
                                        │ n:1  concept_id
                     ┌──────────────────▼──────────────────────┐
  概念层 (稳定)       │  glossary_concepts                       │
  语言无关，IRI 永不变│  concept_id / pref_label / definition     │
                     │  status / maps_to_class / version         │
                     └───────┬───────────────────────┬─────────┘
                             │ maps_to_class          │ maps_to_instance
              ┌──────────────▼──────────┐   ┌────────▼──────────┐
  本体层 TBox │  ontology_types → OWL   │   │  entities (ABox)  │ 实例层
  类/属性/公理│  owl:Class, domain/range│   │  rdf:type → 类 IRI│
              └──────────────┬──────────┘   └────────┬──────────┘
                             │                       │ 物化
                             │      ┌────────────────▼──────────┐
                             └─────►│  graph_db (RDF, SPARQL)    │
                                    │  Pyoxigraph / Fuseki       │
                                    └────────────────────────────┘

  统一命名空间：https://{host}/mbse/ont#  →  所有层共用，全程可解析
  统一类型谓词：rdf:type                →  客体必须是本体类 IRI（不是字符串）
  统一 IRI 规则：{ns}ent/{id}           →  只依赖 id，永不依赖 name
```

三条铁律（与调研结论一致）：

1. **IRI 与名称解耦**：`ent/{id}`，改名不动 IRI
2. **类型必须是 IRI**：`?s rdf:type <.../cls/需求>`，不是 `urn:mbse:type "需求"`
3. **词变了不算变，义变了才是变**：词形变更只动术语层，定义/拆分/合并走本体影响分析

---

## 五、后端优化方案


### 5.1 P0-1 统一命名空间与类型谓词

**改动点**：`graph_db.py` 的 URI 常量与 `pred_uri`。

```python
# 现状（问题：URN、自造 type 谓词、IRI 含 name）
NS_ENT = "urn:mbse:ent:"
NS_PRED_TYPE = "urn:mbse:type"
def ent_uri(name, eid=""):
    base = NS_ENT + loc_key(name)
    return f"{base}:{eid}" if eid else base

# 目标：HTTP 命名空间 + rdf:type + IRI 只依赖 id
NS = "https://mbse.example.org/ont/"          # 与 ontology_owl.BASE 同源
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

def ent_iri(entity_id: str) -> str:
    """实体 IRI：只依赖 id。改名/归并都不影响，满足持久化标识。"""
    return f"{NS}ent/{entity_id}"

def class_iri(type_name: str) -> str:
    """类 IRI：与 ontology_owl 导出的 owl:Class IRI 完全一致。"""
    return f"{NS}cls/{quote(type_name, safe='')}"

def rel_iri(predicate: str) -> str:
    return f"{NS}rel/{quote(predicate, safe='')}"    # 对应 owl:ObjectProperty

def prop_iri(predicate: str) -> str:
    return f"{NS}prop/{quote(predicate, safe='')}"   # 对应 owl:DatatypeProperty
```

**类型三元组改写**（这是让 OWL 公理生效的关键一步）：

```python
# 现状：类型当字面量 → 推理机看不见
(urn:mbse:ent:乘员存在检测, urn:mbse:type, "功能")

# 目标：rdf:type + 类 IRI → 公理立即生效
(<.../ent/F-204>, rdf:type, <.../cls/功能>)
```

**存量迁移**（`data/rdf_export/graph_export.nq` 与 SQLite `triples` 表一并重写）：

```python
def migrate_type_triples(conn, writer):
    """把 urn:mbse:type 字面量三元组重写为 rdf:type + 类 IRI。幂等，可重跑。"""
    rows = conn.execute(
        "SELECT subject_id, subject_name, object_value FROM triples WHERE predicate='type'"
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "subject": ent_iri(r["subject_id"]),
            "predicate": "rdf:type",
            "object": class_iri(r["object_value"]),
            "object_type": "entity",
        })
    writer.delete_subjects([ent_iri(r["subject_id"]) for r in rows])
    return writer.write_triples(out)
```


### 5.2 P0-2 概念层（术语治理的地基）

新增两张表，与调研中的"概念层/术语层/管理层"三层结构对齐：

```sql
-- 概念层：语言无关，IRI 永不变
CREATE TABLE IF NOT EXISTS glossary_concepts (
    concept_id     TEXT PRIMARY KEY,        -- 稳定 ID（IRI 尾段），永不变更
    pref_label     TEXT NOT NULL,           -- 规范词（可改，不影响 concept_id）
    definition     TEXT NOT NULL DEFAULT '',-- ISO 704 定义：描述并区分于相邻概念，入表门槛
    domain         TEXT DEFAULT 'unknown',  -- 受控词表：sysml_norm/satellite_comms/thermal_mgmt/generic
    concept_status TEXT DEFAULT 'candidate',-- candidate|approved|deprecated|retired
    maps_to_class  TEXT DEFAULT '',         -- → 本体类 IRI
    maps_to_prop   TEXT DEFAULT '',         -- → 本体属性 IRI
    maps_to_inst   TEXT DEFAULT '',         -- → 本体实例 IRI
    replaced_by    TEXT DEFAULT '',         -- 弃用/合并时的替代概念
    version        INTEGER DEFAULT 1,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gc_status ON glossary_concepts(concept_status);
CREATE INDEX IF NOT EXISTS idx_gc_domain ON glossary_concepts(domain);

-- 术语层：语言相关，可增删改
CREATE TABLE IF NOT EXISTS glossary_terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id  TEXT NOT NULL REFERENCES glossary_concepts(concept_id),
    term        TEXT      NOT NULL,
    lang        TEXT      DEFAULT 'zh',     -- 语言标签（现状完全缺失）
    term_kind   TEXT      DEFAULT 'synonym',-- preferred|synonym|hidden|abbr
    term_status TEXT      DEFAULT 'admitted', -- preferred|admitted|deprecated
    source      TEXT      DEFAULT '',       -- 来源（标准/项目/LLM 建议）
    reliability INTEGER   DEFAULT 5,        -- TBX reliabilityCode 1-10
    created_at  TEXT      DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gt_term   ON glossary_terms(term, lang, concept_id);
CREATE INDEX IF NOT EXISTS idx_gt_concept       ON glossary_terms(concept_id);
-- 同形异义检测：同一 (term, lang) 指向多个概念 = 需要消歧
```

**术语改名不再动数据**：

```sql
-- 现状：改名要 UPDATE 所有引用行
UPDATE glossary SET canonical_term='乘员存在检测' WHERE canonical_term='占位检测';

-- 目标：只改概念的一个 label，所有别名、映射、检索自动跟随
UPDATE glossary_concepts SET pref_label='乘员存在检测', version=version+1
 WHERE concept_id='C-ODS';
UPDATE glossary_terms SET term_kind='synonym', term_status='deprecated'
 WHERE concept_id='C-ODS' AND term='占位检测';
```

### 5.3 P0-3 / P0-4 双轨合并：`glossary` + `entity_aliases` → 概念层

现状两套别名机制：

|    | `glossary`   | `entity_aliases`          |
| -- | ------------ | ------------------------- |
| 粒度 | 词 → 规范词      | mention 原文 → canonical 实体 |
| 用途 | 检索归一化 + 意图路由 | 实体合并                      |
| 问题 | 无概念 ID       | 无状态、无语言、无定义               |

**合并策略（保持两者接口不变，内部统一到概念层）**：

1. `glossary_terms` 同时承载两类词：词表术语（`term_kind=preferred/synonym`）与实体别名（`term_kind=alias`，通过 `maps_to_inst` 关联）
2. `GlossaryMatcher` 改为查 `glossary_terms`（保留 `_load()` 的 `kind='intent'` 过滤逻辑不动，避免回归）
3. `entity_aliases` 保留为**写入缓冲**（v2g 管线仍在用），通过 nightly 同步写入 `glossary_terms`
4. 意图路由词（`kind='intent'`）迁到独立表 `intent_terms`，彻底解耦——现在混在一张表里已经要靠 `WHERE kind='intent'` 打补丁，说明职责越界

```python
# glossary.py 改造要点：子串匹配 → 概念级召回
def match(self, text: str) -> list:
    # 1) 先按词边界切分，避免 "ODS" 命中 "ODSERVICE"
    # 2) 命中 term → 取 concept_id → 返回概念（含 definition / status / 映射）
    # 3) deprecated 词命中时附带 replaced_by，前端提示"该词已弃用"
```


### 5.4 P1-1 SHACL 接入入库门禁

`ontology_semantics.py` 的 `shacl_export()` 已经能产出标准 SHACL，缺的是**在写入前跑它**。

接入点：现有的写前融合闸 `staging_fuse.py`（这是全链路唯一写入收口，位置正确）。

```python
# services/ingest_gate.py 或 staging_fuse 的 commit 前
from pyshacl import validate

def gate_before_commit(data_graph, shacl_text: str):
    """入库前 SHACL 门禁。不通过 → 不写权威源，只写待办队列。"""
    shacl_graph = Graph().parse(data=shacl_text, format="turtle")
    conforms, _, report = validate(
        data_graph=data_graph, shacl_graph=shacl_graph,
        inference="none", abort_on_first=False, allow_warnings=True,
    )
    return conforms, report
```

同时：**把 `required/unique` 从 `owl:Restriction` 迁移到 SHACL**。

理由（调研结论）：OWL 基数被违反 → 推理机判定"本体不一致"（整个模型废掉）；SHACL 被违反 → 报"这条数据不合规"（精确定位）。前者是核弹，后者是手术刀。

```turtle
# 不再这样（会导致不一致）
:需求 owl:Restriction [ owl:onProperty :isVerifiedBy ; owl:minCardinality 1 ] .

# 改成这样（精确定位到实例）
:RequirementShape a sh:NodeShape ;
    sh:targetClass :需求 ;
    sh:property [ sh:path :isVerifiedBy ; sh:minCount 1 ;
                  sh:message "需求缺少验证用例，追溯链断裂" ] .
```

### 5.5 P1-2 推理机替换手写逻辑

现状硬编码：

```python
TRANSITIVE_RELATIONS = ("包含", "连接", "追溯", "满足", "派生", "执行", "SATISIFIES")
```

改为**从本体读公理**（本体里已经声明了 `owl:TransitiveProperty`）：

```python
def load_transitive_properties(conn) -> set:
    """从 ontology_types.properties JSON 读 transitive 标记，而非硬编码。"""
    rows = conn.execute(
        "SELECT name, properties FROM ontology_types WHERE type_kind='relation'"
    ).fetchall()
    return {r["name"] for r in rows
            if json.loads(r["properties"] or "{}").get("transitive")}
```

查询层用 SPARQL 属性路径（Pyoxigraph 支持 1.1 属性路径）：

```sparql
-- 传递闭包：不再需要 Python 递归
SELECT ?ancestor WHERE { <.../ent/P-07> <.../rel/包含>+ ?ancestor }

-- 未覆盖需求（对应调研中的 Q1）
SELECT ?req WHERE {
    ?req rdf:type <.../cls/需求> .
    FILTER NOT EXISTS { ?v <.../rel/验证> ?req }
}
```

若需要完整 OWL RL 物化：`rdflib` + `owlrl` 在入库时物化一次，结果写回图库（避免查询时推理的延迟）。

### 5.6 P1-3 本体版本与项目隔离

```sql
-- ontology_types 对齐 entities/relations 的隔离模型
ALTER TABLE ontology_types ADD COLUMN project_id TEXT DEFAULT 'project-satnet-broadband';
ALTER TABLE ontology_types ADD COLUMN branch     TEXT DEFAULT 'dev';
ALTER TABLE ontology_types ADD COLUMN version    INTEGER DEFAULT 1;
ALTER TABLE ontology_types ADD COLUMN status     TEXT DEFAULT 'draft';  -- draft|released|deprecated
CREATE INDEX IF NOT EXISTS idx_ot_scope ON ontology_types(project_id, branch);

-- 本体版本快照（支撑"内核本体 + 领域包"）
CREATE TABLE IF NOT EXISTS ontology_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    branch     TEXT NOT NULL,
    version    TEXT NOT NULL,      -- SemVer: major=语义变更, minor=新增, patch=笔误
    owl_text   TEXT NOT NULL,      -- 冻结的 Turtle
    shacl_text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, branch, version)
);
```

对应 AI4MBSE 的 D-1/D-2/D-4：内核本体（跨领域不变）+ 领域包（SatNet / TMS / GAC）作为不同 `project_id` 的分支，切换领域包时内核不动。

### 5.7 P1-4 双库一致性

给图库镜像加版本号，避免漂移：

```python
# graph_db 写入时带权威源 commit 版本
writer.write_triples(triples, graph=graph_uri(branch))
conn.execute("UPDATE graph_sync_state SET last_commit=? WHERE branch=?", (commit_id, branch))

# 查询前校验：镜像版本落后 → 触发增量物化，而不是静默返回旧数据
```

---

## 六、前端优化方案

### 6.1 现状

`static/index.html`：**1,447,604 字节 / 21,178 行**，全部内联。`static/js/` 下只有 `perf.js`。已有页面：本体模型 / 本体类型 / 术语词典 / 图谱工作区 / 图谱实体 / 图谱来源 / 追溯 / 影响度 / 知识库总览 / 视图 / 设置 / 报告。

**判定：功能齐全，但架构不可持续。** 任何一个模块（尤其要新增的治理界面）都要在这个巨型文件里做手术。

### 6.2 模块化路线（渐进式，不动存量功能）

```
static/
├── index.html              # 壳：只保留布局骨架 + 模块加载器（目标 < 80KB）
├── js/
│   ├── core/
│   │   ├── api.js          # fetch 封装 + 错误统一处理
│   │   ├── store.js        # 轻量状态（当前是全局变量散落）
│   │   └── router.js       # 视图路由（现在靠 data-page/data-tab 手切）
│   ├── modules/
│   │   ├── ontology.js     # 本体工作台
│   │   ├── glossary.js     # 术语词典
│   │   ├── graph.js        # 图谱工作区
│   │   ├── governance.js   # 治理看板（新增）
│   │   ├── impact.js       # 影响度
│   │   └── knowledge.js    # 知识库
│   └── vendor/             # cytoscape 等，本地化，零外部依赖
└── css/  （已有）
```

用原生 ES Module（`<script type="module">`），不引入打包器——符合私有化离线部署要求。

迁移顺序（每步可独立上线、可回退）：

1. 抽出 `core/api.js`（所有 fetch 收口）→ 风险最低
2. 抽 `modules/glossary.js`（词典改造要动它，顺带拆）
3. 抽 `modules/ontology.js`（本体工作台改造）
4. 抽 `modules/graph.js`（图谱，依赖 cytoscape，最后拆）
5. 新增 `modules/governance.js`


### 6.3 四个治理界面设计

#### A. 本体工作台（改造现有"本体模型/本体类型"）

| 区域     | 内容                                                                                                      |
| ------ | ------------------------------------------------------------------------------------------------------- |
| 左：类树   | `ontology_types` 层级，支持多继承显示（现状 `parent_id` 单亲，改造后支持）                                                    |
| 中：属性面板 | 类/属性详情：`rdfs:domain` / `rdfs:range` / 特性公理（勾选 transitive / symmetric / functional）/ `disjointWith` / 基数 |
| 右：产物预览 | 实时 OWL Turtle + SHACL 预览，一键复制/下载                                                                        |
| 底部：校验  | 「一致性校验」按钮 → 调 SHACL 门禁，列违规（节点级定位）                                                                       |

新增能力：

- **IRI 预览**：编辑时实时显示生成的类 IRI，杜绝"改个名 IRI 就变"
- **作用域切换**：`project_id` / `branch` 选择器（对应 4.6）
- **版本快照**：发布 → 写入 `ontology_snapshots`，可回滚

#### B. 术语词典（改造现有"术语词典"）

从"两列映射表"升级为"概念-术语"双栏：

```
┌─ 概念列表（左）────────┬─ 概念详情（右）────────────────────┐
│ 🔍 搜索                 │ 概念 ID: C-ODS         状态: 已批准 │
│ ▸ 乘员存在检测  已批准  │ 定义: 判定车辆座舱内是否存在…      │
│ ▸ 压力坐垫检测  已弃用  │ 映射到: cls/功能 · inst/F-204      │
│ ▸ 电池包        已批准  │                                     │
│ ▸ 标定参数      候选    │ ┌ 术语表 ──────────────────────┐   │
│                         │ │ 乘员存在检测 zh 首选 标准  9 │   │
│ [+ 新建概念]            │ │ 占位检测   zh 同义 内部  5  │   │
│                         │ │ ODS       en 缩写 标准  7  │   │
│                         │ │ 坐人检测   zh 禁用 禁用  3  │   │
│                         │ └─────────────────────────────┘   │
└─────────────────────────┴─────────────────────────────────────┘
```

关键交互：

- **状态流转按钮**：候选 → 已批准 → 已弃用（弃用时**强制填替代概念**，对应 SHACL 规则 10）
- **影响分析**：点"已弃用"概念 → 显示仍在使用该词的实例清单（对应调研 G4 查询）
- **同形异义告警**：同一词指向多个概念时红色标记
- **缺定义拦截**：定义为空不允许提交审批（对应 SHACL 规则 5）

#### C. 图谱工作区（改造现有）

| 新增能力       | 说明                                                        |
| ---------- | --------------------------------------------------------- |
| SPARQL 控制台 | 预置模板：未覆盖需求 / 追溯链 / 传递闭包 / 悬挂引用 / 弃用术语残留                   |
| 命名图切换      | 按 branch 切换 `urn:mbse:graph:{branch}`（现状已有能力，暴露到 UI）      |
| **类型着色**   | 节点颜色由 `rdf:type` 指向的本体类决定（改造 P0-1 后才能实现——现状类型在字面量里，前端拿不到） |
| 节点详情       | 点击显示：本体类型路径（含 `subClassOf` 链）、关联术语、SHACL 违规               |

#### D. 治理看板（新增）

四个卡片，全部来自已实现的后端能力，只差可视化：

| 卡片       | 数据来源                                 | 告警条件                     |
| -------- | ------------------------------------ | ------------------------ |
| SHACL 违规 | `shacl_export` + pySHACL             | Violation > 0 阻断发布       |
| 映射覆盖率    | `glossary_concepts.maps_to_class` 为空 | 已批准概念未映射 > 10% 告警        |
| 弃用术语残留   | G4 查询                                | 实例仍用 deprecated 词 > 0 告警 |
| 追溯覆盖度    | Q1 查询（未覆盖需求）                         | 覆盖率 < 90% 告警             |

### 6.4 关键交互：术语选择器（跨模块复用组件）

知识检索、图谱搜索、建模输入统一使用：

- 输入即联想，按 `preferred → synonym → hidden` 优先级排序
- 每条候选显示：**概念名 + 定义摘要 + 状态徽标**
- 命中 deprecated 词时：显示"已弃用，建议使用 XXX"并支持一键替换
- 选中后提交的是 `concept_id`，不是字符串——**这是解决 P0-3 的前端关键**

### 6.5 标杆交互补强（承接 3.5 对标表）

在四个治理界面之外，前端补三项——均来自可直接迁移的标杆交互，不引入新依赖：

1. **Problems & Suggestions 面板**（对标 EDG 8.0）：治理看板单列"问题"Tab，SHACL 违规与 LLM 候选建议**合并展示**，表格化、可按严重度/类型/模块过滤排序；点击违规直接跳转到对应实例编辑。表格组件复用现有审核队列实现。
2. **概念树统一组件**（对标 PoolParty 拖拽 + 颜色编码）：术语词典与本体**共用同一棵概念树**（概念层落地后可复用），支持拖拽调整层级，按 `conceptStatus` 着色（Approved 绿 / Candidate 黄 / Deprecated 灰）。
3. **检索"精确 / 模糊"开关**（对标 PoolParty 10.2 Exact Phrase Matching）：精确模式走标识符通道（需求号 / 零件号 / VIN），模糊模式走概念级召回；默认模糊，输入含连字符的数字串时自动提示切精确。

**依赖**：① 与 ② 依赖 P0-1（类型数据）与 P0-3（概念层）；③ 可与 P1-5 检索改造同步上线，先做前端开关、后端精确通道随后。

---

## 七、数据迁移方案（可回退）

| 步骤 | 动作                                                                                                        | 回退方式                             |
| -- | --------------------------------------------------------------------------------------------------------- | -------------------------------- |
| M0 | 全库备份 + `data/rdf_export` 快照                                                                               | 恢复文件                             |
| M1 | 建 `glossary_concepts` / `glossary_terms`（只增不删）                                                            | DROP 新表                          |
| M2 | 从 `glossary.canonical_term` 去重生成概念（每个 distinct canonical 一个 `concept_id`），`user_term` 写入 `glossary_terms` | 清空新表重跑                           |
| M3 | `entity_aliases` 同步写入 `glossary_terms`（`term_kind='alias'`）                                               | 删除该批                             |
| M4 | 图库类型三元组重写：`urn:mbse:type "X"` → `rdf:type <cls/X>`（见 4.1 脚本，幂等）                                           | 反向重写                             |
| M5 | 实体 IRI 迁移：`urn:mbse:ent:{name}` → `{ns}ent/{id}`，**保留旧 IRI 作为 `skos:altLabel` 式别名 3 个月**                  | 旧 IRI 仍在图库中可查                    |
| M6 | 切换 `graph_db.py` 常量，灰度：先 Fuseki 副本验证，再切主库                                                                 | 改回常量                             |
| M7 | 前端模块拆分（按 6.2 顺序）                                                                                          | 每个模块独立 `index.html.bak-*` 已存在此习惯 |

**幂等要求**（与调研结论一致）：M2/M4/M5 的脚本必须可重复执行，IRI 生成必须由 `id` 确定性派生，禁止随机 UUID 参与业务 IRI。

---

## 八、验收标准（可量化）

| #  | 指标          | 验收方式                                            | 目标                |
| -- | ----------- | ----------------------------------------------- | ----------------- |
| 1  | 类型三元组合规率    | SPARQL 查 `?s rdf:type ?c` 且 `?c` 是本体类 IRI       | 100%              |
| 2  | IRI 与名称解耦   | 回归测试：把某实体 `name` 改掉，重跑物化                        | IRI 不变，关系不丢       |
| 3  | 概念层覆盖       | 统计有 `concept_id` 的活跃术语占比                        | Top 500 术语 100%   |
| 4  | 双轨合并        | `glossary` 与 `entity_aliases` 指向同一 canonical 的词 | 完全一致，无孤儿          |
| 5  | SHACL 门禁有效性 | 注入 5 类故意缺陷（缺定义/编号错/状态非法/弃用无替代/同形异义）             | 100% 被拦截          |
| 6  | 传递闭包无需改码    | 在本体里新增一条 `transitive` 关系，不碰 Python              | 闭包查询正确            |
| 7  | 弃用术语残留可查    | G4 查询                                           | 返回残留实例清单          |
| 8  | 前端首屏体积      | 拆分后 `index.html` + 首屏模块                         | < 300KB（现 1.4MB）  |
| 9  | 追溯覆盖度可查     | Q1 查询                                           | 需求覆盖率可计算且 < 200ms |
| 10 | 三元组溯源可回答    | 随机抽 20 条关系，要求给出 `chunk_id` + `extract_run_id`   | 20 / 20           |
| 11 | 术语检索准确率     | Top 100 术语做概念级召回                                | P@1 ≥ 90%         |
| 12 | 标识符精确检索     | 需求号/零件号查询在精确模式下                                 | 0 误报              |
| 13 | 本体版本可回放     | 发布一次新版本后，旧版本实例仍能按发布时语义解释                        | 通过                |
| 14 | 健康度看板       | 第三章 3.3 中 7 项指标                                 | 全部可计算并自动刷新        |
| 15 | LLM 候选术语可用率 | P2-4 抽取候选经人审后采纳比例                               | ≥ 40%（首批基线，后续校准）  |

---

## 九、路线图

| 阶段          | 周期  | 交付                                                                          | 依赖      |
| ----------- | --- | --------------------------------------------------------------------------- | ------- |
| **P0 止血**   | 2 周 | 命名空间统一、`rdf:type` 改造、IRI 与名称解耦、存量三元组迁移（M1-M6）；**P0-5 溯源（关系库段）**             | 无       |
| **P1 概念层**  | 3 周 | `glossary_concepts` + 双轨合并 + 前端术语词典改造 + 术语选择器；**P1-6 本体版本与状态机（并入 P1-3）**    | P0      |
| **P2 语义闭环** | 3 周 | SHACL 门禁接入、推理机替换、治理看板、**P1-7 健康度指标**、图谱类型着色、**P1-5 检索统一（SQLite FTS5 降级通道）** | P0      |
| **P3 多项目**  | 4 周 | 本体版本/项目隔离、领域包切换、前端全量模块化、**P1-8 查询网关（可选）**                                   | P1 + P2 |
| **P4 智能化**  | 3 周 | **P2-4 LLM 辅助术语/本体构建**、**P2-5 外部词表对齐与变更监控**                                 | P1（概念层） |

**建议先做 P0-1（统一命名空间 + rdf:type）**：工作量最小（改 `graph_db.py` 常量 + 一个迁移脚本），却是解锁后面所有能力的前提——不做这一步，SHACL 门禁和推理机都无从谈起。

---

## 十、风险与回退

| 风险             | 影响                  | 缓解                                                          |
| -------------- | ------------------- | ----------------------------------------------------------- |
| IRI 迁移导致外部引用断链 | 外部系统/导出的 N-Quads 失效 | 旧 IRI 保留 3 个月作为别名；`graph_export.nq` 双写                      |
| 图库镜像与权威源漂移     | 查询结果陈旧              | 加 `graph_sync_state` 版本号，查询前校验                              |
| 词典改造影响检索效果     | 召回率波动               | `GlossaryMatcher._load()` 接口不变，加 A/B 开关，保留 `query_trace` 对比 |
| 前端拆分引入回归       | 功能回退                | 按模块拆，每次一个；沿用现有 `index.html.bak-*` 备份习惯                      |
| SHACL 门禁过严阻断入库 | 数据进不来               | 先 Warning 运行 2 周，收集违规分布后再转 Violation                        |

---

## 十一、实施记录（2026-09-06）

> P0-1 / P0-2 已落地并通过验收。以下为实际改动，全部带备份与回退路径。

### 12.1 核心改动：命名空间收敛到单一事实来源

新增 **`core/ns.py`** —— 全工程 IRI 规则的唯一定义处（BASE / NS\_* / ent_uri / class_uri / pred_uri / parse\_\*）。  
三个模块此前各有一套命名空间，现全部改为引用它：

| 模块                                               | 改动前                                            | 改动后                                             |
| ------------------------------------------------ | ---------------------------------------------- | ----------------------------------------------- |
| `graph_db.py`                                    | `urn:mbse:*`（自造）+ 类型谓词 `urn:mbse:type` 且客体是字面量 | 引用 `core.ns`；类型三元组 = `rdf:type` + **类 IRI**     |
| `ontology_owl.py`                                | `BASE = ".../ontology#"`，局部名中文原样               | `BASE = _ns.NS_ONTOLOGY`；`_slug()` 委托 `core.ns` |
| `ontology_semantics.py`                          | `slugify()` 中文原样；`make_iri` 冲突追加 `_2/_3`（不幂等）  | 委托 `core.ns`；hash-name 策略按名称确定性派生，**不再加后缀**     |
| `ontology_reasoning.py`  
`routers/knowledge.py` | 第三套 `http://www.xingwang.mbse/inst#`           | 统一为 `_ns.NS_ENT`                                |

**IRI 形态变化（P0-2）**：`urn:mbse:ent:{名称}:{id}` → `http://www.xingwang.mbse/ent/{enc(id)}`，只依赖 id，改名不换 IRI。

**配套改动（不改会断功能）**：IRI 不再含名称后，按名检索失效 → 写入时同步生成 `rdfs:label`，`query_entities_by_name` 改为 label 匹配（保留 legacy 路径兜底）。

### 12.2 存量数据迁移

| 脚本                    | 作用                                                               | 回退                                 |
| --------------------- | ---------------------------------------------------------------- | ---------------------------------- |
| `migrate_ns_v2.py`    | 归一 `ontology_types.iri` 存量 73 条（55 条旧格式）到新规则；原值存入 `iri_legacy` 列 | `--rollback`；另有 DB 全量备份            |
| `migrate_graph_db.py` | 重建图库镜像（182 实体 / 122 关系 / 1896 四元组）                               | 旧目录已备份为 `.bak-p0ns-*/graph_db_old` |
| `verify_p0_ns.py`     | 7 项样例 + 2 项真实数据验收                                                | —                                  |

全部脚本默认 dry-run（`migrate_ns_v2.py` 需 `--apply` 才写入）。

### 12.3 验收结果

```
V1  命名空间同源（本体 owl:Class == 图库 rdf:type 客体）     PASS
V2  类型三元组合规率（rdf:type + 类 IRI，非字面量）  4/4     PASS
V3  IRI 与名称解耦（改名后 IRI 不变）                        PASS
V4  rdfs:label 覆盖率                              4/4     PASS
V5  按名称检索（走 rdfs:label）                             PASS
V6  追溯链可查（SPARQL 属性路径）                           PASS
V7  IRI 解析可逆 + legacy 前缀兼容                          PASS
V2b 真实数据：非法类型三元组数量                    0 条     PASS
V4b 真实数据：label 覆盖                        182/182     PASS
结果：9/9 通过
```

导出产物 `data/graph_db/graph_export.nq`：1896 条四元组，含 182 条 `rdf:type`、182 条 `rdfs:label`、**0 条 legacy 残留**。

### 12.4 本轮未做（后续按路线图推进）

P0-3/P0-4 概念层与双轨合并、P0-5 三元组级溯源、P1-* 各项。**P0-1 是它们的前置，现已解锁。**


### 12.5 P1 段实施记录（2026-09-06 下午）

**P0-3/P0-4 概念层与双轨合并**：

- 新表 `glossary_concepts`（概念层）/ `glossary_terms`（术语层），迁移脚本 `migrate_p1_semantic.py`（幂等，dry-run/apply/内置 legacy 备份列思路）。存量 glossary 迁入 5 概念 / 10 术语；**kind='intent'（意图路由）与 kind='sys'（能力导航）保留在 glossary**——它们是路由配置不是术语，误迁会丢 force_intent（实施中实测发现并修正）
- `GlossaryMatcher` 升级概念级召回：新增 `_load_concepts()` / `match_concepts()`，命中带 concept_id / concept_status / replaced_by / definition；弃用词命中返回替代建议；`match/normalize/resolve` 接口不变（已实测：归一化 `电源单元→电源模块` 正常、意图路由不回归）
- 概念 API：`/api/glossary/concepts` CRUD + 详情（含弃用影响分析、同形异义检测）+ 术语增删 + `sync-aliases`（entity_aliases 缓冲同步）

**P1-1 SHACL 门禁**（`ingest_gate.py`）：

- 三模式（settings 表 `shacl_gate_mode`）：off / **warn（默认，先观察违规分布）** / enforce
- `shacl_export()` 两处修复：① targetClass 与 property 之间缺 `;`（产物非法 Turtle）；② **形状 IRI 与数据 IRI 不同源**——形状用中文原样+ontology# 前缀、数据用编码 IRI+rel/prop 前缀，约束全部落空（假阴性）。已对齐 core.ns，全量扫描从"0 违规（假）"变为"1007 违规（真）"，含已验证真阳性（DERIVES 指向 单元需求，range 是 子系统需求，仅共享根类 需求）
- 接入 `triple_store.review_triple/batch_review`：enforce 下违规候选阻断、留在待办；**候选三元组及其同主体 pending 类型行必须并入数据图**（否则无 rdf:type 无形状命中，实测拦不住）
- 数据图并入 `rdfs:subClassOf` 公理（SHACL 规范 sh:class 的子类闭包依赖数据图内公理）；`sh:unique`（非标准词）→ `sh:maxCount 1`
- 依赖 rdflib+pyshacl 已装项目 `.venv`；全量校验 433-457 三元组约 0.8s

**P1-2 推理机读公理**（`ontology_reasoning.py`）：

- 新增 `load_transitive_properties(conn)`：从 `ontology_types.properties` 读 `transitive`/`characteristics` 标记，无标记回退 `TRANSITIVE_RELATIONS`
- **实锤发现：真实数据用英文关系名（CONTAINS/DERIVES/TRACE…），原中文硬编码白名单在真实数据上从未生效（闭包恒为 0）**。已在本体中正式声明 CONTAINS/DERIVES/TRACE transitive=True，闭包产出 25 条推断（SAT-001 ⇒ 各分系统 2 跳等）

**P1-3/P1-4 版本与一致性**：

- `ontology_types` 加列 project_id/branch/version/status（存量默认 released，不阻断）；`ontology_snapshots`（SemVer 冻结 OWL+SHACL）；`graph_sync_state` 水位（migrate_graph_db 收口记录）
- 发布 API 带门禁：有 Violation 拦截（实测 1.0.0 被 1007 条违规正确拦截）

**P1-7 治理指标**（`governance.py` + `routers/governance.py`）：

- 9 项指标实测：SHACL 违规 0（近7天）/ 追溯覆盖度 **6.1%（alert）** / 孤立率 **45.6%（alert）** / 映射覆盖率 **0%（alert，概念层刚上线属预期）** / 未决候选 27 / 低置信 0% / 弃用残留 0 / 同形异义 0 / 镜像 lag 0

**前端**（index.html，术语词典页 + 治理入口）：

- 术语页双视图：「🗂 概念层（新）」概念列表（状态/域筛选、缺定义标记、同形异义 🔴）+ 概念详情右侧滑动面板（状态流转、术语表、弃用影响、同步实体别名）；「🔗 归一映射（旧）」原样保留
- 治理看板按既定交互范式做**右侧滑动面板**（术语页「🛡 治理」按钮进入）：9 指标卡 + 门禁模式切换 + 本体发布快照。**独立治理看板页面评估结论：暂缓**——3 项 alert 的整改动作在导入侧与概念层运营，不在看板；warn 期红条目是噪音；等 P3 前端模块化时再升级为独立页

**验收**：enforce 阻断（ok=False / blocking_n=8 / 候选保持 pending）、发布拦截 422、缺定义审批 422、弃用无替代 422、归一化/意图路由回归正常、P0 回归 9/9、4 个治理端点 + 概念端点 TestClient 200、前端 4 个内联 script 语法 0 错误。

**本轮未做**：P0-5 溯源（关系库段）、P1-5 检索统一（FTS5）、图谱类型着色（需先重物化镜像消费 rdf:type——P0-1 已解锁）、P4 LLM 辅助。

---


## 十二、依据来源

**本次评审引用的标准（均已在前期调研中核实出处）**：

- ISO 704:2022《术语工作—原则与方法》第 4 版（2022-07，ISO/TC 37/SC 1）：客体/概念/定义/指称四要素
- ISO 1087:2019《术语工作与术语科学—词汇》：concept / term / designation / definition 定义
- ISO 25964-1:2011《叙词表与互操作》、ISO 30042:2019《TBX》第 2 版（conceptEntry / langSec / termSec）
- W3C OWL 2 Structural Specification（2nd ed., 2012-12-11）：六类实体、特性公理
- W3C RDF 1.1（2014-02-25）：三元组取值约束、字面量三要素
- W3C SKOS Reference（REC 2009-08-18）：公理 S18–S24，`skos:broader` 非传递
- W3C SHACL（REC 2017-07-20）：约束校验与 OWL 公理的职责分工
- ISO/IEC 11179-1:2023（MDR 框架）：数据元素概念 = 对象类 + 属性；"本体是允许应用形式逻辑的概念系统"

**本次新增对标的行业标杆（官方公开资料）**：

- Graphwise / PoolParty：Graph Modeling（SKOS 原生、W3C 标准、多语言、审计与历史、SPARQL 端点、LLM 辅助 Taxonomy Advisor 与 Corpus Analysis）；PoolParty 10.2 发布说明（原生 Lucene 概念标注引擎、40+ 语言、Exact Phrase Matching 的 Mandatory / Ranking Boost 两档）
- Ontotext GraphDB：RDF4J 兼容、RDFS / OWL 2 RL / QL 标准规则集与自定义一致性检查、前向链全物化推理、Lucene / Solr / Elasticsearch / OpenSearch 连接器、RDF Rank、Ontop OBDA 虚拟化
- Stardog：Virtual Graph 虚拟化、查询时（just-in-time）推理、SHACL + ICV 可解释约束、BI/SQL Server、Voicebox LLM 建模助手
- TopQuadrant TopBraid EDG：6.3 起支持对 KG 语句加注（provenance / 生效日期）、资产集合虚拟在制副本与受控发布、Problems & Suggestions 面板；8.0 内置向量库辅助本体对齐与实体消解、外部图库（任意 SPARQL 端点）实时集成、Medical Term Management Accelerator（外部词表变更自动检测）
- W3C 标准现状：SHACL 1.0（REC 2017-07-20，基线）；SHACL 1.2 Core（WD 2026-07-20）、RDF 1.2 Concepts（CR 2026-04-07）、SPARQL 1.2 Query（WD 2026-06-25）—— 均**未定稿**，列为观察项
- W3C PROV-O（REC 2013-04-30）：溯源建模词汇

**评审中核对的工程文件**：`database/schema.py`、`database/migrations.py`、`glossary.py`、`graph_db.py`、`triple_store.py`、`ontology_owl.py`、`ontology_semantics.py`、`ontology_reasoning.py`、`staging_fuse.py`、`static/index.html`


### 12.6 P1-2 深化：SHACL 交集假阳性治理 + 首次发布闭环（2026-09-06 晚）

**背景**：上轮 P1-1 对齐命名空间后，全量校验报 1007 条 Violation（DERIVES range 错配为真阳性的结论需修正——本轮证明其主因是形状生成 bug）。

**根因 1（主要，~100% 假阳性）：SHACL 交集语义**。`shacl_export()` 把同一关系 path 的多个目标类拆成多个 `sh:property [sh:path C; sh:class T]` 块。SHACL 规范中多块同 path = **交集**（值须同时是所有 T 类的实例），而业务语义是**并集**。实测 `载荷Shape` 的 CONTAINS 重复 19 块、`部件`/`系统元素` 各 21 块（含中文关系 `包含` 与英文 `CONTAINS` 重复声明叠加）——几乎无数据能同时是 19 个类的实例，因此 457 条三元组炸出 1007 条违规。  
**修复**：`ontology_semantics.py` — 同 path 的 tgt 类合并为一个 `sh:or ( [sh:class A] [sh:class B] … )` 并集块（单 tgt 保持 `sh:class`）；`_shapes_cache` 签名不变（本体类型数），进程重启后自然生效。

**根因 2（发布链路首次暴露）：`to_turtle()` 未解包**。`to_turtle` 返回 `(text, warnings)`（方案 A3），`governance.release_snapshot` 直接把元组当文本入库 → `Error binding parameter 5: type 'tuple' is not supported`。旧路径走不到这里（总被 SHACL 拦截），违规清零后首次触发。  
**修复**：`governance.py` 解包 `owl_text, owl_warnings = to_turtle(conn)`。

**根因 3（产物级）：Turtle 续接 bug**。`ontology_owl.py` attribute 分支有两个独立续接块（subPropertyOf 与 domain/range），第二块在语句已 `" ."` 收尾后追加 `" ;"` → 产出 `rdfs:label "发射功率" . ;`（非法 Turtle，rdflib BadSyntax）；"父属性+domain" 并存时同样触发。  
**修复**：合并为单一 `cont` 列表统一收尾；entity 分支仅一处续接，安全。

**数据侧核查（全干净，无需清洗）**：悬挂引用 0、无类型实体 0、数据关系全部在本体声明范围内（12 个类型：CONTAINS 34 / SATISFIES 26 / DERIVES 16 / …）。上轮"1007 条需人工清洗"的结论撤销。

**假阳性记录清理**：warn 模式期间写入 `shacl_findings` 9071 条（全部产生于修复前 6 分钟窗口，含被拦截的 release:1.0.0 尝试）→ 备份后清空。备份：`backups/mbse.db.bak-findings-20260906-194028`（68MB 全库）。代码备份：`ontology_semantics.py.bak-shaclor-20260906`、`ontology_owl.py.bak-turtle-20260906`。

**哨兵测试（防"全绿=形状失效"）**：故意造 2 条错配（卫星系统 CONTAINS 需求、系统需求 DERIVES 功能）→ pySHACL 全部命中，正确对照边（卫星系统 CONTAINS 通信载荷）不误报——形状修复后依然有效。

**里程碑**：**本体 v1.0.0 首次发布成功**（SemVer 冻结快照：OWL 18.8KB/416 triples + SHACL 87.8KB/2953 triples、37 NodeShape、93 个 sh:or 并集块；ontology_types version+1）。冻结快照复检当前数据 conforms=True，可直接分发下游消费。

**回归**：P0 验收 9/9 通过；全量 SHACL 0 违规（457 triples）；治理指标 SHACL 项转绿（ok=6/warn=0/alert=3，剩余 alert 为真实治理欠账：追溯覆盖 6.1%、孤立率 45.6%、映射覆盖 0%）。

**新工具**：`diag_violations.py`（只读诊断：全量校验 + path 聚合 + 约束 vs 实际数据分布对照；其白名单字面标记仅供参考，真伪以 SHACL 校验为准）。

**遗留警示**：本体中中文关系（`包含`/`连接`）与英文关系（CONTAINS/CONNECTS）并存属语义重复，SHACL 形状各生成一套约束。建议后续做关系类型归一（含数据迁移），本轮未动。


### 12.7 P1-2 深化二：关系类型归一 + 本体 v1.1.0（2026-09-06 晚，接 12.6 遗留警示）

**盘点**：本体 relation 类型 26 个 = 英文标准 15 + 中文 11。**relations 数据 122 条 100% 用英文**，中文类型 0 使用；v2g pending 27 条候选中 24 条用中文关系名（审批落库会直接污染）。

**归一决策**（canonical=英文，与数据一致）：

- 确定映射 6 个：`包含`→CONTAINS、`满足`→SATISFIES、`连接`→CONNECTS、`派生`→DERIVES、`追溯`→TRACE、`子类`→GENERALIZATION
- 约束合并：`包含` 的增量并入 CONTAINS（src+部件、tgt+部件）；**tgt 的"需求"不并入**——"部件包含需求"语义不成立（该判断随后被验证，见下）
- 废弃无映射 5 个：`属于`/`执行`/`供给电能`/`包含功能`/`地面站功能`（语义未定或疑似类型误录，不强行映射以免错误召回）

**实施**：

1. `core/relmap.py`（新）：REL_CANONICAL 单点映射 + `canonical()`/`is_deprecated()` + REL_ALLOWED 白名单；宽松策略（未知名原样返回，本体新增英文类型不依赖本表）
2. `migrate_rel_normalize.py`：本体 11 类型 deprecated+replaced_by、CONTAINS 约束合并、**v2g pending 20 条归一**（`执行`4 条保留人工裁决）、relations 防御扫描；--dry-run/--apply/--rollback（快照 JSON）齐备
3. 代码路径归一：
   - `sysml_importer.py` **两条路径**都插入归一（staging 候选路径 + import_sysml 直接落库路径，后者必须在 ontology 预校验前——否则废弃中文类型的旧约束仍参与 domain/range 判定，实测踩中）；废弃名边剔除计入 rejected；归一后补一次去重（视图路径 dedup 在归一前，`包含`/`CONTAINS` 同边会重复）；落库 props 带 `rel_normalized_from` 溯源
   - `knowledge_repo.create_relation`：归一闸门（可映射静默归一，废弃名拒绝创建，与引用完整性闸门同风格）——运行时唯一外部关系写入口
   - `ontology_reasoning.TRANSITIVE_RELATIONS` 兜底表补英文（CONTAINS/CONNECTS/TRACE/SATISFIES/DERIVES）
   - `governance._VERIFY_RELS` 补 SATISFIES；**追溯覆盖度口径修正**：需求在链的 source 或 target 侧均算覆盖（此前只查 source，"功能 SATISFIES 需求"全部漏计）→ **覆盖度 6.1% → 72.7%**（24/33，还原真值）
4. **SHACL 抓到第一个真实语义错误**：`BroadbandPayloadSystem（部件）CONTAINS HighRateRequirement（需求）`（v2g 导入 candidate 数据）——正是迁移时有意不并入的"需求"目标类。已废弃该关系（id=90519，保留痕迹）。SHACL 门禁在真实数据上的价值首次闭环验证。

**E2E 验证**：JSON 通道导入 含`包含`/`满足`/`执行` 边的模型 → `包含`→CONTAINS、`满足`→SATISFIES 落库（带溯源标记）、`执行`剔除且原因可读；P0 回归 9/9；SHACL 全量 0 违规。

**发布**：**本体 v1.1.0**（OWL 18.8KB + SHACL 90KB）。备份：`backups/mbse.db.bak-relnorm-20260906-202914` + 回滚快照 `_relnorm_backup_20260906-202914.json`。

**指标现状**：ok=6 / warn=0 / alert=3（追溯覆盖 72.7% 距 90% 门槛、孤立率 45.6%、映射覆盖 0%——均为数据运营欠账，非代码问题）。


### 12.8 P0-5 三元组溯源（2026-09-06 第四轮）

**目标**：每条落库三元组可回答"从哪来、谁审的、何时审、落在图上哪个对象"。实施前 `triples` 表为空（候选从未 stage），溯源链路四处断裂。

**四项改造**：

| 缺口                           | 修复                                                                                                                                                                            |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `source_chunk` 有列但 INSERT 不写 | `_add_triple` 增加 `source_chunk` 参数（已存在三元组缺 chunk 时增量补齐）；`stage_candidates_as_triples` 传递 `c["chunk_id"]`                                                                      |
| 落图无回链                        | migration 加列 `graph_entity_id` / `graph_relation_id`（幂等 `_migrate_columns` + CREATE TABLE 同步）；`commit_approved_triples` 三处回填（type 实体 / 属性 / 关系 `create_edge` 返回的 relation id） |
| 无溯源查询入口                      | 新端点 `GET /api/knowledge/triples/{triple_id}/provenance`：来源（doc/chunk 原文预览 300 字/section/文件名）→ 审批（by/at/note）→ 落图（entity_name/relation_label）→ SysML 版本                        |
| 废弃名可经 stage 混入               | `stage_candidates_as_triples` 关系名走 `core.relmap.canonical`，废弃名跳过并计数 `skipped_deprecated`                                                                                      |

**E2E 过程中修掉两个既有真 bug**（此前 triples 从未有数据，从未暴露）：

1. **建实体时序颠倒**：Phase1 `create_node` 传空 props，必填属性在 Phase2 属性三元组才消费 → 有必填属性的本体类（如 `[天线] 缺少必填属性: 测试1`）全部落图失败。修复：Phase1 预组装同主体已审属性三元组后再建实体。
2. **跨批次关系解析缺失**：Phase2 关系三元组只查本批 `name_to_id`，两端实体在先前批次落图时关系被静默跳过且永远无法落图。修复：回退按名查 `personal` 分支非弃用实体（取最新，同名唯一性由上游判重保障）。

**E2E 验证**（候选→stage→SHACL 门禁审批→落图→溯源 API）：实体/属性/关系三类三元组全部落图且回链；溯源 API 输出完整证据链（doc=广汽方法论-合稿1.docx、chunk 6090 原文预览、reviewer、relation_id=90592 → `溯源测试载荷 —CONTAINS→ 溯源测试天线`）。测试数据已按标记精确清理。

**数据运营**：28 条 pending 关系候选物化为 27 条待审三元组（1 条 `执行` 废弃名被新防御跳过，源候选保留人工裁决）；4 条此前残留的 `执行` 三元组清理。当前 `triples`：pending 27 / approved 0——待人工审核（审核队列 API 不变）。

**回归**：P0 9/9；SHACL 全量 0 违规（456 triples）；指标 ok=5 / warn=1（未决候选 50=候选 23+三元组 27，属正常审核积压）/ alert=3（真实运营欠账不变）。

**涉及文件**：`database/migrations.py`（2 列）、`triple_store.py`（source_chunk 链路）、`triple_commit.py`（4 处：chunk 传递、回链、时序修复、跨批次解析、canonical 防御）、`routers/knowledge.py`（provenance 端点）。备份：`triple_commit.py.bak-p05-20260906`。

**剩余路线**：P1-5 FTS5、图谱类型着色、P4 LLM 辅助；数据运营三项（追溯覆盖→90%、孤立率、映射覆盖）。


### 12.9 P1-5 FTS5 全文检索（2026-09-06 第五轮）

**动机**：原检索仅 `name LIKE '%kw%'` 全表扫描，且 properties JSON 内容与三元组知识原子**完全搜不到**。

**方案**（新建 `fts_search.py`，~150 行）：

- **索引**：FTS5 虚表 `knowledge_fts(kind, ref_id UNINDEXED, title, body)`，`tokenize='trigram'`（SQLite 3.53 实测中英文**子串**匹配；unicode61 对中文是整串分词不可用）。索引三张表：entities（name+类型+properties 文本）/ relations（src—rel→tgt+props）/ triples（S—p→O）
- **同步**：SQLite 触发器（3 表 × INSERT/UPDATE/DELETE 共 9 个）挂自动同步——**不改任何写路径**；`ensure_fts` 幂等（启动时调用，空索引自动重建），`fts_rebuild` 全量重建；实测初始 596 行（448+125+23），触发器增删改同步断言通过
- **查询**：`fts_query` MATCH 短语引号包裹防语法注入；**<3 字符或零命中回退 LIKE**（trigram 最短 3 字符边界，覆盖"卫星"这类 2 字中文词）

**接入**：

- `graph_search` 升级：先 FTS（含 properties 内容命中）拿实体 id 集，异常/零命中回退原 LIKE——返回结构不变，前端零改动
- 新端点 `GET /api/knowledge/fts?q=&kinds=&limit=`：跨实体/关系/三元组聚合检索，返回附实时 status/branch
- `main.py` lifespan 挂 `ensure_fts`

**验证**：中文子串（卫星系统/转发器/高速率）、英文子串（SysML/BroadbandPayloadSystem）、属性内容命中、2 字 LIKE 兜底、触发器三态同步全部断言通过；P0 回归 9/9、SHACL 0 违规。实施中修 1 处自身笔误（entities 重建 SELECT 列数错位）+ 1 处兜底逻辑 bug（<3 字符分支只设 mode 未查询）。

**涉及文件**：`fts_search.py`（新）、`routers/knowledge.py`（graph_search 升级 + fts 端点）、`main.py`（启动初始化）。

**剩余路线**：图谱类型着色、P4 LLM 辅助；数据运营三项（追溯覆盖→90%、孤立率、映射覆盖）。

### 12.10 图谱类型着色升级（2026-09-06 第六轮）

**原有问题**（`static/index.html` `GRAPH_COLORS` 30 类硬编码 + `graphColor()` includes 顺序匹配）：

1. 未覆盖类型一律落灰（实测 3 类：地面站/系统元素/连接）——本体类型是动态的，硬编码永远滞后
2. `includes` 顺序遍历存在歧义隐患（"系统需求"先被"需求"键命中；当前数据侥幸不撞色，新类型加入即可能翻车）
3. ring 布局排序键与着色逻辑不一致（排序用原始键查表，未知类型恒 `'z'`）

**升级**（三级回退，`graphColor()` 重写）：

| 级别      | 逻辑                                                            |
| ------- | ------------------------------------------------------------- |
| 1 精确匹配  | 类型名动态注册进 `GRAPH_COLORS` 即精确命中（补 地面站→地面段青、连接→链路紫、系统元素→中性蓝灰）    |
| 2 最长子串  | 消除顺序歧义——"子系统需求"等长键优先于"需求"                                     |
| 3 动态调色板 | 未知类型走 `graphHashColor` 稳定哈希，从 12 色低饱和和谐色系取色——**同类型永远同色，不再落灰** |

**连带修正**：ring 布局排序改按 `graphColor` 结果（同色相邻更准确）；抽取结果 `typeColor()` 兜底同接动态调色板（原固定灰 `#5a6478`）。

**验证**：Node 实测 6 断言（长键优先/精确命中/动态稳定/空类型安全）全过；4 个内联 script 块 `node --check` 语法通过；节点渲染处（`renderGraph` 的 `graphColor(n.entity_type)`）自动生效，前端无需其它改动。备份 `static/index.html.bak-colors-20260906`。

**剩余路线**：P4 LLM 辅助；数据运营三项（追溯覆盖→90%、孤立率 45.6%、映射覆盖 0%）；27 条待审三元组人工裁决。


## 12.11 编辑路径本体校验补全（P1-6，2026-09-07）

**问题**：图谱工作区实例属性的"基于本体类型约束"只覆盖了一半——新建节点（POST）经 `GraphStore.create_node → validate_node`（类型合法/必填/唯一/取值白名单，含父类继承），但**编辑保存（PUT `/graph/nodes/{id}`）完全绕过校验**：可把受控属性改成白名单外的值、删必填属性、写非法键；边修正（PUT `/graph/edges/{id}`）同样绕过 `validate_edge`，且可写入废弃关系名（如"执行"）回流。前端受控渲染（必填*、下拉白名单、xsd 控件、release 只读）只是 UI 层君子协定。

**修复**（用户拍板：扩展属性保留自由扩展 + xsd 类型本轮补强）：

1. **PUT 节点**：接入 `validate_node(body.entity_type, body.properties)`，违规 400。扩展属性（未在本体定义的键）不拦，保留"高级：自定义扩展属性"能力
2. **PUT 边**：①`relmap.canonical` 防废弃名（与 sysml_importer/create_relation 同口径，`包含`→自动归一 CONTAINS、无映射废弃名拒绝）；②`validate_edge` 校验 domain/range（含子类匹配）
3. **xsd 数据类型强校验**（`OntologyValidator._xsd_type_errors`，validate_node 末尾调用）：受控属性值存在时按本体 dataProperty 声明的 type 检查格式——int/integer/long 整数、decimal/float/double 数值（含科学计数）、boolean（true/false/0/1）、date、dateTime；属性 domain 不含当前类型链则跳过（扩展键不受影响）

**E2E 七场景**（临时约束+显式清理，全部通过）：

| 场景                                  | 结果                                |
| ----------------------------------- | --------------------------------- |
| 合法更新（载荷，无约束类型）                      | ok                                |
| 删除必填属性（临时 required）                 | 400 `[载荷] 缺少必填属性: 频段`             |
| 白名单外取值（临时 allowed_values）           | 400 `属性 频段=X 不在允许值内 ['Ka','Ku']`  |
| xsd:int 写 'abc' / 写 '32'            | 400 / ok                          |
| 边修正写废弃名"执行"                         | 400 `关系类型已废弃且无 canonical 映射`      |
| 边修正写"包含"                            | 自动归一 CONTAINS 落库，状态回 candidate 重审 |
| 边修正 domain/range 错配（需求 CONTAINS 需求） | 400 `不允许来源类型 '需求'`                |

回归：P0 9/9、SHACL 全量 conforms=True（0 违规）。备份 `ontology_semantics.py.bak-editguard-20260907`、`routers/knowledge.py.bak-editguard-20260907`。

**附带发现（本体卫生，待清理）**：祖先类型"系统元素"constraints 残留测试约束 `required:["测试1"]`，经继承链污染全部子孙类（卫星系统/通信载荷/有效载荷/天线等）——新建/编辑这些类型实例都会被要求填写测试属性"测试1"。属早期测试遗留，建议删除该 required 项（会同时改变 SHACL 形状，发布新版本快照）。

### 12.11.1 遗留约束清理（2026-09-07 已执行）

全量扫描 `ontology_types` 后确认**仅一处真遗留**：`#1997 系统元素 required=["测试1"]`（`#2024 验证活动` 的 desc "测试/仿真/审查"为正当业务描述，保留）。全库**0 个实体实际存有"测试1"属性**，清理无数据副作用。

- 操作：仅移除 `required` 键（约束由 `{"required":["测试1"]}` → `{}`），不动其它字段；备份 `backups/mbse.db.bak-cleanreq-20260907-102838.db`
- 影响：继承链上 天线/通信载荷/有效载荷/卫星系统/系统元素 `validate_node(..., {})` 由"缺必填"全部转为 `[]`；**OWL 产物 416→413 triples**（证明该约束此前确实进入了发布产物）
- 发布 **v1.2.0**：OWL 413 triples / SHACL 3040 triples（37 NodeShape），快照中已无"测试1"痕迹
- 回归：P0 9/9、SHACL 全量 conforms=True（0 违规）

**另发现（未处理，待确认）**：库中存留 17 条历史测试实体（`ENT-TEST-*` / 名为"测试实体"，类型 载荷，分支 dev，创建于 2026-08-07 前后），非本轮产物。删除属数据清理动作，需确认后执行。

### 12.11.2 历史测试实体清理（2026-09-07 已执行）

- 范围：15 个不同 id / 17 行（含分支重复行），全部名为"测试实体"或 `ENT-TEST-*`/`KBP2-T1`/`TST-GATE-*`
- 关联面核查：**relations 0 条**（无悬挂风险）；仅 `graph_edit_logs` 46 行、`knowledge_publish_logs` 25 行引用，随之清理；`*_bak_20260827` 备份表与其它功能的测试数据（conversations/prompts/mcp_servers 等）不在范围
- 执行：日志先行（46 + 25）→ 删实体 17 行；FTS 索引由触发器自动同步 604 → 587（-17，与删除量一致，零残留）
- 复核：残留测试实体 0、FTS 残留 0、**悬挂关系 0**
- 回归：P0 9/9、SHACL conforms=True（0 违规，456→453 triples）；治理指标——孤立节点率 **45.6% → 44.5%**（删掉的正是无连接测试实体），追溯覆盖度 72.7% 不变
- 备份：`backups/mbse.db.bak-deltestent-20260907-1036xx.db`
