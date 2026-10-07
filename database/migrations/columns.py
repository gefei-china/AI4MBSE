"""通用列补齐迁移（_migrate_columns）。"""
import json
def _migrate_columns(conn):
    """幂等列迁移：CREATE TABLE IF NOT EXISTS 不会给已有表加列，
    P0-1 引入 project_id 后需对老库补列并回填默认项目（兼容既有数据）。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        # 表尚未创建（由后续建表迁移补齐，含该列）→ 跳过列迁移（PRAGMA 对不存在表返回空列表，需先查 sqlite_master）
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            # 列名加反引号：规避 SQLite 保留字（如 references）导致的 ALTER 语法错误
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    _add("conversations", "project_id", "TEXT DEFAULT ''")
    _add("entities", "project_id", "TEXT DEFAULT ''")
    _add("relations", "project_id", "TEXT DEFAULT ''")
    # ── P0-2 批次 2（2026-10-04）：编排检查点补列 ──
    # summary_written：与 assistant 消息同事务置 1。它是"能否只重做汇总"的唯一判据 ——
    #   崩在汇总阶段时子任务已全 done（重跑就是白烧 LLM），但若消息已落库又不能重做（会出重复消息）。
    _add("orch_checkpoints", "summary_written", "INTEGER DEFAULT 0")
    # ── 内容级澄清挂起（AI 建模信息不清晰 → 选择题确认，回答后续答）──
    _add("conversations", "pending_clarify", "TEXT DEFAULT ''")
    # ── P1-3：SysML Profile 导入溯源（类型 → 来源 Profile/Stereotype）──
    _add("ontology_types", "profile_source", "TEXT DEFAULT ''")   # 来源 Profile 名
    _add("ontology_types", "profile_ref", "TEXT DEFAULT ''")      # 来源 Stereotype/metadata 元素名
    # ── P0 平台化：模型参数模板 / Skill 扩展列 / MCP 扩展列 ──
    _add("llm_providers", "model_params", "TEXT DEFAULT '{}'")          # {"temperature":0.3,"max_tokens":8192,"top_p":0.9}
    # ── M1 记忆增强：溯源 + 相关度（MemoryService 消费）──
    _add("agent_memory", "source", "TEXT DEFAULT 'agent'")              # agent | llm_agent | rule_agent | push | flow
    _add("agent_memory", "relevance", "REAL DEFAULT 1.0")               # 相关度权重（时间衰减/语义分）
    # ── 记忆索引化（Auto Memory 对齐）：主题标签，检索先按主题过滤再语义排序 ──
    _add("agent_memory", "mem_topic", "TEXT DEFAULT ''")                # 主题标签（如"建模规范"/"链路预算方法论"）
    # ── P1-18 记忆分层（core 常驻 / recall 按需 / archival 归档，对齐 Letta 三层）──
    _add("agent_memory", "tier", "TEXT DEFAULT 'recall'")               # core | recall | archival
    # ── KB-S：Agent 级知识库消费范围（{"mode":"all_release"|"custom","branches":[],"docs":[]}）──
    _add("agents", "kb_scope", "TEXT DEFAULT '{}'")
    _add("llm_providers", "model_type", "TEXT DEFAULT 'chat'")          # chat 对话模型 | embedding 向量模型
    _add("llm_providers", "context_window", "INTEGER DEFAULT 8192")     # 上下文窗口（tokens）
    # ── D9 分层式多级管理：子工作流递归——父运行 id（Supervisor 树层级关联）──
    _add("flow_runs", "parent_run_id", "INTEGER DEFAULT 0")             # 0=顶层；>0=父运行 id（子流程 run）
    # ── D10 LLM 智能路由：能力标签 / 优先级 / Token 预算（8.2 模型层多模型切换与成本控制）──
    _add("llm_providers", "tags", "TEXT DEFAULT '[]'")                  # 能力标签 JSON 数组，如 ["code","chinese","fast"]
    _add("llm_providers", "priority", "INTEGER DEFAULT 0")              # 路由优先级（越大越优先，同权重按 is_default）
    _add("llm_providers", "budget_tokens", "INTEGER DEFAULT 0")         # Token 预算（0=不限；聚合 llm_usage_stats 判定耗尽）
    # ── P1-20 prompt caching 观测：DeepSeek 上下文磁盘缓存命中/未命中（自动缓存，命中≈1/10 价）──
    _add("llm_usage_stats", "prompt_cache_hit_tokens", "INTEGER DEFAULT 0")
    # ── P0-c（2026-10-03）LLM 调用 trace 关联三列（评估：无 conversation_id/run_id 无法下钻）──
    _add("llm_usage_stats", "conversation_id", "INTEGER DEFAULT 0")
    _add("llm_usage_stats", "run_id", "INTEGER DEFAULT 0")
    _add("llm_usage_stats", "trace_id", "TEXT DEFAULT ''")
    # ── P0-2（2026-10-06）工具调用日志补操作者──
    # 此前 tool_call_logs 578 行**无 user 字段** ⇒ 只能回答"哪个会话调的"，
    # 回答不了"**是谁**调的"（文章讲的「业务方追着问到底是谁改的」那个必答项）。
    # 存量 578 行无法回溯（历史未记录），统一留空——**不编造**。
    _add("tool_call_logs", "user_name", "TEXT DEFAULT ''")
    _add("llm_usage_stats", "sub_task_key", "TEXT DEFAULT ''")
    _add("llm_usage_stats", "prompt_cache_miss_tokens", "INTEGER DEFAULT 0")
    # P1-27 截断诊断（思考模型 reasoning 与正文共享 max_tokens 配额）
    _add("llm_usage_stats", "finish_reason", "TEXT DEFAULT ''")
    _add("llm_usage_stats", "reasoning_tokens", "INTEGER DEFAULT 0")
    # ── P1-1b 观测落库：LLM 重试/回退（值由各调用路径写入响应 _meta，llm/__init__.py 统一取值落库）──
    _add("llm_usage_stats", "retry_count", "INTEGER DEFAULT 0")           # 重试次数（0=首次成功；流式不重试恒 0）
    _add("llm_usage_stats", "fallback_used", "INTEGER DEFAULT 0")         # 1=本次调用发生过 provider 回退
    _add("llm_usage_stats", "fallback_provider_id", "INTEGER DEFAULT 0")  # 回退后实际使用的 provider id（未回退=0）
    # ── D11 A2A 协议互通：agent_messages 携带发送方身份卡（agent_card 字段）──
    _add("agent_messages", "sender_name", "TEXT DEFAULT ''")            # A2A 发送方 Agent 名称（agent_card.name）
    _add("agent_messages", "sender_type", "TEXT DEFAULT ''")            # A2A 发送方类型（llm | tool | flow | external）
    _add("agent_messages", "sender_role", "TEXT DEFAULT ''")            # A2A 发送方角色（agent_card.role）
    # ── 意图识别增强（P0-2）：会话级 DST——当前意图 / 上轮结构化槽位（跨轮继承与合并）──
    _add("conversations", "current_intent", "TEXT DEFAULT ''")          # 最近一轮意图（轻量 DST：无强信号新输入继承）
    _add("conversations", "last_slots", "TEXT DEFAULT '{}'")            # 最近一轮 task_decompose 槽位 JSON（跨轮合并）
    # ── 上下文组装 v2（话题感知）：消息话题标签（惰性打标回写，供分话题摘要/相关历史拉回）──
    _add("messages", "topic", "TEXT DEFAULT ''")                         # 话题标签（段首用户消息前 N 字符）
    _add("skills", "frontmatter", "TEXT DEFAULT '{}'")                   # SKILL.md frontmatter 原文（含 triggers/category/author）
    _add("skills", "content", "TEXT DEFAULT ''")                         # SKILL.md 指令正文
    _add("skills", "triggers", "TEXT DEFAULT '[]'")                      # 触发关键词 JSON
    _add("skills", "category", "TEXT DEFAULT ''")
    _add("skills", "package_path", "TEXT DEFAULT ''")                    # 上传技能包解压目录
    _add("skills", "updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
    _add("skills", "source", "TEXT DEFAULT 'manual'")                    # M6：manual | ai_deposit（AI 自动沉淀草稿）
    # ── SK-RL：Skill 注册表生命周期（依赖声明 / 角色权限）──
    _add("skills", "dependencies", "TEXT DEFAULT '[]'")   # [{"name","min_version"}] 或 ["name"]；发布时校验依赖存在且已发布
    _add("skills", "allowed_roles", "TEXT DEFAULT '[]'")  # 允许调用角色（空=全部角色）
    # ── AI 建模 SysML 入库溯源：候选关联版本 / 实体记录来源版本 ──
    _add("v2g_candidates", "sysml_version_id", "INTEGER DEFAULT 0")   # >0 = AI 建模候选（关联 sysml_versions.id）
    _add("entities", "sysml_version_id", "INTEGER DEFAULT 0")         # 入库元素来源版本（AI 建模溯源）
    # ── P0-5 三元组溯源（2026-09-06）：落图回链——三元组 ↔ 构图对象双向可追溯 ──
    _add("triples", "graph_entity_id", "TEXT DEFAULT ''")    # 落图后回填：主语实体 id（type/属性三元组）
    _add("triples", "graph_relation_id", "TEXT DEFAULT ''")  # 落图后回填：关系 id（关系三元组）
    # ── 审计分支归属（2026-09-14）：图谱/推理等域事件记录操作分支，历史 Tab 按分支过滤 ──
    _add("audit_logs", "branch", "TEXT DEFAULT ''")
    # ── P1-8（2026-09-07）：本体类型生命周期与溯源列（对齐 glossary_concepts，
    # 补齐"谁在什么时候创建/修改"；replaced_by 之前仅 relation 的 constraints 有）──
    _add("ontology_types", "created_by", "TEXT DEFAULT ''")       # 创建人
    _add("ontology_types", "updated_at", "TEXT DEFAULT ''")       # 最近修改时间
    _add("ontology_types", "updated_by", "TEXT DEFAULT ''")       # 最近修改人
    _add("ontology_types", "replaced_by", "TEXT DEFAULT ''")      # 弃用后替代类型名（entity/relation/attribute 通用）
    _add("ontology_types", "deprecated_note", "TEXT DEFAULT ''")  # 弃用原因/说明
    # ── D4 Skills 分层结构：allowed_tools 白名单 + 渐进披露资源（reference/examples/scripts）──
    _add("skills", "allowed_tools", "TEXT DEFAULT '[]'")  # 工具白名单（skill 触发时限制可调用工具，空=不限制）
    _add("skills", "references", "TEXT DEFAULT '[]'")     # 参考文档清单（渐进披露，按需加载）
    _add("skills", "examples", "TEXT DEFAULT '[]'")       # 示例清单（按需加载）
    _add("skills", "scripts", "TEXT DEFAULT '[]'")        # 脚本清单（文件名列表，按需加载）
    # ── 工坊启停：Skill / MCP 启用停用（停用不参与触发/绑定/注入）──
    _add("skills", "enabled", "INTEGER DEFAULT 1")        # 1=启用 0=停用
    _add("mcp_servers", "enabled", "INTEGER DEFAULT 1")   # 1=启用 0=停用
    # ── 工坊可观测：tool_call_logs 统一调用类别（tool/skill/mcp）──
    _add("tool_call_logs", "call_kind", "TEXT DEFAULT 'tool'")
    # ── P0-5 技能使用中自改进：使用反馈采集（命中执行后聚合统计 + 最近失败备注滚动）──
    _add("skills", "use_stats", "TEXT DEFAULT '{}'")      # {"uses":n,"success":n,"fail":n,"last_used":"..."} 聚合统计
    _add("skills", "feedback_notes", "TEXT DEFAULT '[]'") # 最近 5 条失败/改进备注 [{"ts","run_id","intent","note"}]（滚动保留）
    # ── TR-P0/P1：工具注册表完备化（行业标准 Tool Registry：schema/版本/副作用/风险/状态机）──
    _add("tools", "input_schema", "TEXT DEFAULT '{}'")    # JSON Schema（LLM function calling 参数定义）
    _add("tools", "version", "TEXT DEFAULT 'v1.0'")       # 语义版本 name@semver
    _add("tools", "side_effect", "TEXT DEFAULT 'read'")   # read | write | destructive（执行策略门控）
    _add("tools", "risk_level", "TEXT DEFAULT 'low'")     # low | medium | high
    _add("tools", "owner", "TEXT DEFAULT ''")             # 工具业务负责人
    _add("tools", "allowed_roles", "TEXT DEFAULT '[]'")   # TR-P3：允许调用角色（空=全部角色），运行时按当前用户角色过滤
    # ── 外部接口集成：通用 HTTP 工具（配置驱动，适配不同建模软件，零代码接入）──
    _add("tools", "kind", "TEXT DEFAULT ''")              # '' | http（通用 REST 执行器）
    _add("tools", "config", "TEXT DEFAULT '{}'")          # JSON：HTTP 调用描述（url/headers/body/data_path/timeout）
    # ── D2 工具容错：重试策略 + 降级链 ──
    _add("tools", "retry_policy", "TEXT DEFAULT '{}'")    # JSON：{"max_retries":2,"backoff_base_ms":500,"backoff_multiplier":2,"retry_on":[...]}
    _add("tools", "fallback_to", "TEXT DEFAULT ''")       # 降级工具名（主工具失败重试耗尽后自动切换）
    _add("mcp_servers", "transport", "TEXT DEFAULT 'sse'")               # sse | stdio | http
    _add("mcp_servers", "command", "TEXT DEFAULT ''")                    # stdio 启动命令
    # ── P0 语义升级：长期记忆真向量版本标记（同 document_chunks.embed_version 语义）──
    _add("agent_memory", "embed_version", "TEXT DEFAULT ''")             # 真 embedding 版本（''=bigram/未向量化）
    # ── P2 记忆认知化：遗忘引擎（激活度）+ 合并引擎（反思）──
    _add("agent_memory", "activation", "REAL DEFAULT 1.0")               # 记忆激活度（使用频率×最近访问×时效衰减）
    _add("agent_memory", "access_count", "INTEGER DEFAULT 0")            # 被检索命中的次数
    _add("agent_memory", "last_accessed_at", "TEXT DEFAULT ''")          # 最近访问时间
    _add("agent_memory", "forgotten", "INTEGER DEFAULT 0")               # 0=活跃 1=遗忘（软删，检索跳过，可恢复）
    # ── 多维作用域（对齐 mem0 四维 scope：global/project/user/agent + 复合过滤）──
    _add("agent_memory", "scope_type", "TEXT DEFAULT ''")               # '' | global | project | user | agent（'' = 存量行）
    _add("agent_memory", "scope_id", "TEXT DEFAULT ''")                 # project=projects.id / user=username / agent=intent / global 为空串
    # 作用域复合过滤索引：**必须在补列之后**建（先补列再建索引，老库才不会因缺列而崩）
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_memory'").fetchone():
        _cols_am = [r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()]
        if "scope_type" in _cols_am and "scope_id" in _cols_am:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_am_scope ON agent_memory(scope_type, scope_id, forgotten)")
    # ── 记忆冲突消解（D1-A影子状态列，2026-10-07）──
    # ⚠️ **为什么用影子列而不是直接 UPDATE 旧记忆**：
    #   冲突消据本质是"判断谁对谁错"，而**判断会错**。
    #   直接 UPDATE ⇒ 错误判断不可逆（文章反方："错误更新→不可逆污染"）；
    #   影子列只标记"被谁取代"，旧内容仍在库里 ⇒ 可观察、可回退、可审计。
    # 这两列**不参与检索过滤**（召回期仲裁在 memory_recall 层做），
    # 加它们对既有行为零影响（默认 0/空 = "未被取代"）。
    _add("agent_memory", "superseded_by", "INTEGER DEFAULT 0")           # 被哪条记忆取代（0=未被取代）
    _add("agent_memory", "superseded_at", "TEXT DEFAULT ''")            # 标记时间
    # 索引同样**必须在补列之后**（与上方 ix_am_scope 同纪律）
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_memory'").fetchone():
        _cols_am2 = [r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()]
        if "superseded_by" in _cols_am2:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_am_superseded ON agent_memory(superseded_by)")
    _add("mcp_servers", "args", "TEXT DEFAULT '[]'")
    _add("mcp_servers", "env", "TEXT DEFAULT '{}'")
    _add("mcp_servers", "last_check", "TEXT DEFAULT ''")
    # ── MCP-D1：协议完整性（initialize 握手 / 三原语动态发现 / 健康巡检）──
    _add("mcp_servers", "capabilities", "TEXT DEFAULT '{}'")          # initialize 返回的能力声明
    _add("mcp_servers", "protocol_version", "TEXT DEFAULT ''")        # 协商的协议版本
    _add("mcp_servers", "server_info", "TEXT DEFAULT '{}'")           # serverInfo（name/version）
    _add("mcp_servers", "resources", "TEXT DEFAULT '[]'")             # resources/list 发现的资源清单
    _add("mcp_servers", "prompts", "TEXT DEFAULT '[]'")               # prompts/list 发现的提示模板清单
    _add("mcp_servers", "last_error", "TEXT DEFAULT ''")              # 最近一次巡检失败原因
    _add("mcp_servers", "last_discover", "TEXT DEFAULT ''")           # 最近一次动态发现时间
    # ── KB-P0：分块扩展列（BM25 全文 / 章节归属）──
    _add("document_chunks", "bm25_text", "TEXT DEFAULT ''")              # 全文索引文本（含标题/章节前缀）
    _add("document_chunks", "section", "TEXT DEFAULT ''")                # 所属章节（结构感知分块）
    # ── KB-P2：图谱编辑维护（坐标持久化 / 来源标记 / 关系属性 / 编辑审计）──
    _add("entities", "graph_source", "TEXT DEFAULT 'manual'")             # manual | llm | sysml_import
    _add("entities", "graph_x", "REAL DEFAULT 0")                         # 力导向图坐标
    _add("entities", "graph_y", "REAL DEFAULT 0")
    _add("entities", "sysml_import_id", "TEXT DEFAULT ''")                # 溯源：导入批次
    _add("relations", "props", "TEXT DEFAULT '{}'")                       # 关系属性（flow 端口等）
    # ── KB-D：图谱关系元数据（审核工作流：创建人/审核人/时间/来源文档，对齐实体元数据）──
    _add("relations", "created_by", "TEXT DEFAULT ''")
    _add("relations", "reviewed_by", "TEXT DEFAULT ''")
    _add("relations", "reviewed_at", "TEXT DEFAULT ''")
    _add("relations", "source_doc", "TEXT DEFAULT ''")
    # ── KB-P4：本体约束规则（语义层：类型/关系/属性/约束 驱动图谱实例化）──
    _add("ontology_types", "constraints", "TEXT DEFAULT '{}'")            # {"required":[],"unique":[],"cardinality":"1:N","allowed_values":[]}
    _add("ontology_types", "description", "TEXT DEFAULT ''")
    _add("ontology_types", "icon", "TEXT DEFAULT ''")                      # 表情符号
    _add("ontology_types", "color", "TEXT DEFAULT '#185FA5'")              # 节点/边默认色
    _add("ontology_types", "iri", "TEXT DEFAULT ''")                       # P0-1：实体 IRI（RDF 主轴，WebProtege 对齐）
    # ── O-1：chunk↔entity 溯源链接（向量→图谱转化工作流）──
    _add("documents", "pipeline_detail", "TEXT DEFAULT '{}'")             # JSON: {parse,chunk,embed,insert} 各阶段 done/failed + error
    _add("documents", "error_msg", "TEXT DEFAULT ''")
    _add("document_chunks", "linked_entity_ids", "TEXT DEFAULT '[]'")     # JSON: 该 chunk 已入库关联实体 id
    # ── KB分支：合并请求冲突解决决策 + 文档/分块分支归属（共享+发布快照）──
    _add("merge_requests", "resolutions", "TEXT DEFAULT '{}'")            # JSON: {entity_id:{field:{pick,value}}}
    _add("merge_requests", "merge_detail", "TEXT DEFAULT '{}'")           # JSON: 合并结果（moved/updated/doc_snapshot）
    _add("documents", "branch", "TEXT DEFAULT 'dev'")
    _add("document_chunks", "branch", "TEXT DEFAULT 'dev'")
    # ── FR-KG-16 分支合并回滚：合并前 release 快照（回滚还原用）＋发布日志撤销标记 ──
    _add("merge_requests", "prev_release_snapshot", "TEXT DEFAULT '{}'")  # 合并执行前 release 实体/关系/文档快照 JSON
    _add("knowledge_publish_logs", "action", "TEXT DEFAULT 'publish'")    # publish | rollback（合并回滚留痕）
    # ── 分支版本管理 Task4：发布版本号（MR 人工填写）+ 发布日志版本标签/关联提交 ──
    _add("merge_requests", "release_version", "TEXT DEFAULT ''")          # 人工填写的发布版本号（目标为 release 时可用）
    _add("knowledge_publish_logs", "version_label", "TEXT DEFAULT ''")    # 本次发布版本号（v1/v1.1…）
    _add("knowledge_publish_logs", "commit_id", "INTEGER DEFAULT NULL")   # 关联本次发布的 merge 提交 id
    # ── 分支版本管理：分支=提交链指针（Git 式 head commit），指向该分支最新提交 id ──
    _add("branches", "head_commit", "INTEGER DEFAULT NULL")
    # ── 2026-09-29 痛点2：LLM 真摘要（空=未生成；preview 端点优先读，回退「前2块截断」旧行为）──
    _add("documents", "summary", "TEXT DEFAULT ''")
    # ── P0-2 行业对齐：chunk 域隔离（AWS Metadata Filtering / 企业分类法）──
    _add("document_chunks", "domain", "TEXT DEFAULT 'unknown'")          # sysml_norm | satellite_comms | thermal_mgmt | generic | unknown
    _add("documents", "domain", "TEXT DEFAULT 'unknown'")                # 文档级域（chunk 继承）
    _add("documents", "domain_confidence", "REAL DEFAULT 0")             # 打标置信度（<0.9 进 review 队列）
    # ── P2-1 Reverse HyDE：假设问题向量（入库生成，查询时问题对问题匹配）──
    _add("document_chunks", "hyde_questions", "TEXT DEFAULT '[]'")       # JSON: [假设问题...]
    _add("document_chunks", "hyde_embedding", "TEXT DEFAULT '[]'")       # JSON: 问题嵌入（bigram 或真向量）
    # ── P1b-2 委派协议结构化：子任务 context/expected_output（老库补列）──
    _add("agent_tasks", "context", "TEXT DEFAULT ''")
    _add("agent_tasks", "expected_output", "TEXT DEFAULT ''")
    # ── P0-7（2026-09-30）：批次归属会话。此前流式路径直接把 conversation_id 当 run_id
    #    （stream.py:34），`clear_run` 于是每轮删掉上一轮计划（实测会话 514：run=514 的 5 行被
    #    新一轮 6 行覆盖）→ 计划无历史、不可审计。现 run_id 唯一化，归属会话落本列。
    _add("agent_tasks", "conversation_id", "INTEGER DEFAULT 0")
    # ── P0 优化：自动编排结果沉淀为可复用工作流（来源标记 manual | planner_auto）──
    _add("agent_flows", "source", "TEXT DEFAULT 'manual'")
    # ── 实体抽取治理中心（抽取候选增强）：置信度 / 消歧匹配状态 / 关联实体 / 驳回原因 ──
    _add("v2g_candidates", "confidence", "REAL DEFAULT 0")                # 抽取置信度（LLM 输出或规则确定性分数，0-1）
    _add("v2g_candidates", "matching_status", "TEXT DEFAULT 'none'")      # 与已有图谱消歧匹配：none | dup_suspect | dup_high
    _add("v2g_candidates", "match_entity_id", "TEXT DEFAULT ''")          # 疑似/高度重复时关联的已有实体 ID
    _add("v2g_candidates", "reject_reason", "TEXT DEFAULT ''")            # 驳回原因（单条/批量驳回留痕）
    # ── S7 关系候选三元组：rel_source 老库补列（v2g 抽取写入依赖，新库已由建表含该列）──
    _add("v2g_candidates", "rel_source", "TEXT DEFAULT ''")               # 关系源端实体名
    # ── P0 审核队列重复治理：关系候选入库前消歧（对齐实体 P0-C 三态）──
    _add("v2g_candidates", "rel_matching_status", "TEXT DEFAULT 'none'")  # 关系候选与已有边重复匹配：none | dup_high
    _add("v2g_candidates", "rel_match_rel_id", "TEXT DEFAULT ''")         # 重复时关联的已有关系边 ID
    # ── P0-3/P0-4：知识分类（设计方法知识/设计资产）+ 生命周期发布环节 ──
    _add("entities", "knowledge_category", "TEXT DEFAULT ''")             # P0-3: 知识类别子类名（空=未分类）
    _add("entities", "published_at", "TEXT DEFAULT ''")                   # P0-4: 进入 release（发布）时间
    _add("documents", "knowledge_category", "TEXT DEFAULT ''")            # P0-3: 文档知识类别
    # ── 架构优化（Agent 编排）：agents 能力声明 / 输入输出 schema / 并发 / 版本 / 协议范围 ──
    _add("agents", "capabilities", "TEXT DEFAULT '[]'")                   # 专长标签 JSON 数组
    _add("agents", "input_schema", "TEXT DEFAULT '{}'")                   # 输入 JSON Schema
    _add("agents", "output_schema", "TEXT DEFAULT '{}'")                  # 输出 JSON Schema
    _add("agents", "max_concurrency", "INTEGER DEFAULT 2")                # 最大并发任务数
    _add("agents", "version", "TEXT DEFAULT 'v1.0.0'")                    # Agent 语义版本（老库已存在 version 则跳过）
    _add("agents", "protocol_range", "TEXT DEFAULT '>=1,<3'")             # A2A 协议版本范围
    # ── 架构优化：子任务执行快照（负载 / 分派打分 / 结构化摘要 / 重试 / token 统计）──
    _add("agent_tasks", "load_snapshot", "TEXT DEFAULT '{}'")             # 分派时各 Agent 负载快照
    _add("agent_tasks", "assign_score", "REAL DEFAULT 0")                 # 分派打分
    _add("agent_tasks", "summary_json", "TEXT DEFAULT '{}'")              # 结构化摘要协议结果（含 status）
    _add("agent_tasks", "retry_count", "INTEGER DEFAULT 0")               # 编排内自动重试次数
    _add("agent_tasks", "token_count", "INTEGER DEFAULT 0")               # 子任务 token 统计
    # ── 架构优化：HIL 确认队列补 task_key（写操作暂存绑定子任务，先查表存在再 ALTER）──
    _add("hil_confirmations", "task_key", "TEXT DEFAULT ''")
    # ── 分支管理 GitHub 对标（P0-1/P0-2/P1-1/P1-2）：冲突重算时间戳 / MR 标题 / 驳回意见 ──
    _add("merge_requests", "conflict_updated_at", "TEXT DEFAULT ''")  # 冲突清单最近重算时间（P0-1）
    _add("merge_requests", "title", "TEXT DEFAULT ''")                 # MR 标题（缺省 source → target）
    _add("merge_requests", "review_note", "TEXT DEFAULT ''")           # 驳回意见（request changes 必填）
    # ── 分支版本管理：merge 提交双父指针（P1-2，供 ahead/behind 与三点式 diff）──
    _add("knowledge_commits", "source_branch", "TEXT DEFAULT ''")      # 合并源分支名
    _add("knowledge_commits", "source_head_commit", "INTEGER DEFAULT NULL")  # 合并时源分支 head 提交 id
    # ── P0-3：提交内容哈希（对标 G9 commit SHA；防改库篡改，审计红线场景）──
    _add("knowledge_commits", "content_hash", "TEXT DEFAULT ''")       # sha256(branch|parent_id|kind|规范化changes|规范化snapshot)
    # ── P0-2：分支保护规则（按类型默认 + 分支级覆盖；解析见 core/branch_rules.py）──
    _add("branches", "protection_rules", "TEXT DEFAULT '{}'")          # JSON: {writable,deletable,renamable,required_reviews,allow_direct_push}
    # ── 方案A（2026-09-24）：AI 建模主链统一「归一 → 写回智源 → 智源拉取」，工程入库直入捷径下线 ──
    _add("sysml_versions", "zhiyuan_imported_id", "TEXT DEFAULT ''")   # 非空=该版本已写回智源（vc#package 标识）；防重复写回 + 入口条状态展示
    # ── 方案A收尾（2026-09-24）：工程↔智源vc绑定 + 入库台账来源标记 ──
    _add("projects", "zhiyuan_vc", "TEXT DEFAULT ''")                  # 本工程在智源的版本上下文（写回/拉取按此路由；绑定即落库）
    _add("projects", "zhiyuan_project_name", "TEXT DEFAULT ''")        # 绑定的智源工程名（冗余存：选择器/回执展示 + 名称核对，防"同名不同工程"）
    _add("project_ingest_logs", "source", "TEXT DEFAULT ''")           # project_ingest=工程入库(旧捷径) | zhiyuan_pull=智源拉取（台账区分来源）
    # ⚠️ 2026-09-25：这条 UPDATE 必须**自己判表存在**。`_add()` 内部有判表守卫（表不存在就跳过加列），
    # 但紧随其后的这条裸 UPDATE 没有 → 在**没有该表**的库上（如测试用临时库）init_db 直接抛
    # `no such table: project_ingest_logs`，把整个初始化打断（实测：tests/manual_verify/verify_intent_enhance_d12.py
    # 一跑就崩，且崩在 init_db 而非被测逻辑）。判据：新库初始化不报错。
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_ingest_logs'").fetchone():
        conn.execute("UPDATE project_ingest_logs SET source='project_ingest' WHERE source='' OR source IS NULL")
    # ── 建模工具适配层（2026-09-24 用户拍板：对接不限于智源，也支持 MagicDraw）──
    # tool_binding JSON = {"tool":"zhiyuan"|"magicdraw"|..., "ref":"vc/工程标识", "name":"工具侧工程名"}
    # 空 = 本地建模。对齐 08-31《多工程数据体现设计方案》tool_type: magicdraw/sysmlv2_api/cameo 设计。
    _add("projects", "tool_binding", "TEXT DEFAULT ''")
    # 旧 zhiyuan_vc 绑定回填为 tool_binding（zhiyuan_vc/zhiyuan_project_name 转遗留列，停止读写）
    # ⚠️ 2026-09-28：本条 UPDATE 必须与上面 project_ingest_logs 那条一样**自己判表存在** ——
    #  `_add()` 有判表守卫，裸 UPDATE 没有。实测（P1 自检夹具：只有 sysml_versions/artifacts 两张表的
    #  临时库）跑 `_migrate_columns` 直接抛 `no such table: projects`，把整个 init_db 打断，
    #  且崩在初始化而非被测逻辑。生产库有 projects 表所以从未暴露 —— 典型"只在最小库上才现形"。
    #  判据：无 projects 表的库上 init_db 不报错。
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
        conn.execute(
            "UPDATE projects SET tool_binding = json_object('tool','zhiyuan','ref',trim(zhiyuan_vc),'name',trim(zhiyuan_project_name)) "
            "WHERE trim(zhiyuan_vc)<>'' AND trim(coalesce(tool_binding,''))=''")
    # ── 项目数据来源（2026-09-24 用户拍板）：本地工作空间 / 远端 SSH 连接 ──
    # source='local'  → workspace = 本地工作空间绝对路径（项目根目录，前端「选本地文件夹」写入）；
    # source='remote' → remote    = SSH 连接 JSON {display_name,host,port,auth_mode,identity_file}，
    #                   auth_mode: none（无身份验证，对应弹窗左档）| key（身份文件，须填 identity_file）。
    # 老库既有项目 source 留空 → 读取侧按 'local' 兜底（无 workspace 即「未指定」），不做数据改写。
    _add("projects", "source", "TEXT DEFAULT 'local'")
    _add("projects", "workspace", "TEXT DEFAULT ''")
    _add("projects", "remote", "TEXT DEFAULT ''")
    # ── P1-1（2026-09-28 多工程）：AI 建模写入物**定格**所属工程 ──
    # sysml_versions / artifacts 此前没有 project_id，归属只能靠
    # `v.conversation_id → c.project_id` 现算 —— 会话事后改归属，历史版本/产物会**跟着漂移**
    # （写回智源的目标工程也随之改变）。新行写入时定格，旧行留空由 JOIN 兜底。
    # ⚠️ **存量不回填**（用户拍板 2026-09-28）：历史行的归属已不可考，宁可留空走兜底，
    #    也不按当前会话归属"倒推"历史的工程。
    _add("sysml_versions", "project_id", "TEXT DEFAULT ''")
    _add("artifacts", "project_id", "TEXT DEFAULT ''")
    conn.commit()


def _migrate_branch_protection(conn):
    """P0-2：内置分支（release / dev / personal）保护规则默认值回填。

    幂等 + 非破坏：仅当 protection_rules 为空串或空对象时写入显式默认值，
    管理端已改过的配置一律不覆盖；branches 表或列缺失时直接跳过。
    """
    from core.branch_rules import BUILTIN_DEFAULTS
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='branches'").fetchone()
    if not exists:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(branches)").fetchall()]
    if "protection_rules" not in cols:
        return
    filled = 0
    for name, rules in BUILTIN_DEFAULTS.items():
        row = conn.execute("SELECT protection_rules FROM branches WHERE name=?",
                           (name,)).fetchone()
        if not row:
            continue
        raw = row["protection_rules"] if hasattr(row, "keys") else row[0]
        if str(raw or "").strip() in ("", "{}"):
            conn.execute("UPDATE branches SET protection_rules=? WHERE name=?",
                         (json.dumps(rules, ensure_ascii=False), name))
            filled += 1
    if filled:
        print(f"[init_db] 迁移: branches 回填保护规则 {filled} 条")
    conn.commit()
