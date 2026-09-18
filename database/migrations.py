"""数据库迁移：_rebuild_entities_pk/_repair_relations_fk/_rename_release_branch/_migrate_columns/_ensure_ontology_types/_backfill_pipeline_detail/_migrate_graph_tables/_migrate_v2g_tables/_migrate_sysml_tables/_apply_env_keys/_migrate_glossary_tables/_migrate_data_source_tables/_migrate_ontology_change_tables/_migrate_eval_tables。"""
import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import datetime

from core.config import DB_PATH

def _rebuild_entities_pk(conn):
    """实体版本化迁移：entities 由 id 单列主键 → (id, branch) 复合主键。

    SQLite 不支持 ALTER 修改主键，采用「重建表 + 拷数据 + 改名」：
    - 幂等：检测到已是 id+branch 两列主键则跳过
    - 老库单列主键时重建（relations 的 REFERENCES 未启用外键约束，安全）
    - 必须在 _migrate_columns 之后执行（graph_source/graph_x/graph_y 等列已补齐）
    """
    cols = conn.execute("PRAGMA table_info(entities)").fetchall()
    pk_cols = [c["name"] for c in cols if c["pk"] > 0]
    if len(pk_cols) == 2:
        return  # 已是复合主键
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE entities RENAME TO entities_old")
    conn.execute("""CREATE TABLE entities (
        id TEXT NOT NULL,
        name TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        properties TEXT DEFAULT '{}',
        status TEXT DEFAULT 'candidate',
        branch TEXT DEFAULT 'dev',
        project_id TEXT DEFAULT 'project-satnet-broadband',
        source_doc TEXT DEFAULT '',
        source_type TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        created_by TEXT DEFAULT '',
        reviewed_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TEXT DEFAULT '',
        graph_source TEXT DEFAULT 'manual',
        graph_x REAL DEFAULT 0,
        graph_y REAL DEFAULT 0,
        sysml_import_id TEXT DEFAULT '',
        PRIMARY KEY (id, branch)
    )""")
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id, "
        "source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at, "
        "graph_source, graph_x, graph_y, sysml_import_id) "
        "SELECT id, name, entity_type, properties, status, COALESCE(branch,'dev'), project_id, "
        "source_doc, source_type, confidence, created_by, reviewed_by, created_at, reviewed_at, "
        "COALESCE(graph_source,'manual'), COALESCE(graph_x,0), COALESCE(graph_y,0), "
        "COALESCE(sysml_import_id,'') FROM entities_old")
    conn.execute("DROP TABLE entities_old")
    conn.commit()
    print("[init_db] 迁移: entities 重建为复合主键 (id, branch)（实体版本化）")


def _repair_relations_fk(conn):
    """修复 relations 表外键失效问题。

    根因：_rebuild_entities_pk 执行 `ALTER TABLE entities RENAME TO entities_old` 时，
    SQLite（≥3.25）会自动把 relations 表内 `REFERENCES entities(id)` 改写为
    `REFERENCES "entities_old"(id)`；随后 DROP entities_old，FK 目标表消失。
    连接开启 PRAGMA foreign_keys=ON 时，任何 INSERT INTO relations 都会报
    `no such table: main.entities_old`。
    修复：检测到 schema 含 entities_old 引用则重建 relations 表（去掉 FK）——
    实体版本化后 entities 主键为 (id, branch) 复合键，单列 FK REFERENCES entities(id)
    本就不能成立，直接移除。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='relations'").fetchone()
    if not row or "entities_old" not in (row[0] or ""):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE relations RENAME TO relations_old")
    conn.execute("""CREATE TABLE relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        properties TEXT DEFAULT '{}',
        status TEXT DEFAULT 'candidate',
        branch TEXT DEFAULT 'dev',
        project_id TEXT DEFAULT 'project-satnet-broadband',
        confidence REAL DEFAULT 1.0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        props TEXT DEFAULT '{}'
    )""")
    conn.execute(
        "INSERT INTO relations (id, source_id, target_id, relation_type, properties, status, branch,"
        " project_id, confidence, created_at, props) "
        "SELECT id, source_id, target_id, relation_type, properties, status, branch,"
        " project_id, confidence, created_at, props FROM relations_old")
    conn.execute("DROP TABLE relations_old")
    conn.commit()
    print("[init_db] 迁移: relations 重建（移除失效的 entities_old 外键引用）")


def _rename_release_branch(conn):
    """分支改名迁移：release/v1.2 → release（统一发布分支名）。

    幂等：branches 表无 release/v1.2 则跳过。
    级联改名所有引用旧名的行：branches(name/parent_branch)、entities、relations、
    documents、document_chunks、merge_requests(source/target)。先改 branches.name
    （UNIQUE 约束，旧名行先改走避免与已存在的 release 行冲突）。
    """
    if not conn.execute("SELECT 1 FROM branches WHERE name='release/v1.2'").fetchone():
        return
    conn.execute("UPDATE branches SET name='release' WHERE name='release/v1.2'")
    conn.execute("UPDATE branches SET parent_branch='release' WHERE parent_branch='release/v1.2'")
    conn.execute("UPDATE entities SET branch='release' WHERE branch='release/v1.2'")
    conn.execute("UPDATE relations SET branch='release' WHERE branch='release/v1.2'")
    conn.execute("UPDATE documents SET branch='release' WHERE branch='release/v1.2'")
    conn.execute("UPDATE document_chunks SET branch='release' WHERE branch='release/v1.2'")
    conn.execute("UPDATE merge_requests SET source_branch='release' WHERE source_branch='release/v1.2'")
    conn.execute("UPDATE merge_requests SET target_branch='release' WHERE target_branch='release/v1.2'")
    conn.commit()
    print("[init_db] 迁移: 发布分支 release/v1.2 → release（级联改名）")


def _repair_documents_fk(conn):
    """修复 documents 表失效外键（data_sources_old 引用）。

    根因：_migrate_data_source_tables 重建 data_sources 时执行
    `ALTER TABLE data_sources RENAME TO data_sources_old`，SQLite 会自动把
    documents 表的 `REFERENCES data_sources(id)` 改写为
    `REFERENCES data_sources_old(id)`；随后 DROP data_sources_old，FK 目标表消失。
    连接开启 PRAGMA foreign_keys=ON 时，任何 documents 写操作（文档上传、
    发布快照 snapshot_documents 的 DELETE/INSERT）都会报
    `no such table: main.data_sources_old`。
    修复：检测到该坏外键则重建 documents/document_chunks/doc_metadata 三表
    （source_id 保留为普通列；chunks/metadata 的 document_id 外键重新指向新
    documents 表，保持 ON DELETE CASCADE 语义），模式同 _repair_relations_fk。
    """
    fks = conn.execute("PRAGMA foreign_key_list(documents)").fetchall()
    if not any(r["table"] == "data_sources_old" for r in fks):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    # ① 重建 documents（去掉失效 FK，source_id 为普通列）
    conn.execute("ALTER TABLE documents RENAME TO documents_old")
    conn.execute("""CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        file_type TEXT DEFAULT '',
        file_size INTEGER DEFAULT 0,
        parse_status TEXT DEFAULT 'pending',  -- pending | parsing | completed | failed
        chunk_count INTEGER DEFAULT 0,
        entity_count INTEGER DEFAULT 0,
        quality_score REAL DEFAULT 0,
        source_id INTEGER,                    -- 数据源 id（普通列，不设外键）
        uploaded_by TEXT DEFAULT '',
        branch TEXT DEFAULT 'dev',
        knowledge_category TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        pipeline_detail TEXT DEFAULT '{}',
        error_msg TEXT DEFAULT '',
        domain TEXT DEFAULT 'unknown',
        domain_confidence REAL DEFAULT 0
    )""")
    conn.execute(
        "INSERT INTO documents (id, filename, file_type, file_size, parse_status, chunk_count,"
        " entity_count, quality_score, source_id, uploaded_by, branch, knowledge_category, created_at,"
        " pipeline_detail, error_msg, domain, domain_confidence) "
        "SELECT id, filename, file_type, file_size, parse_status, chunk_count, entity_count,"
        " quality_score, source_id, uploaded_by, COALESCE(branch,'dev'), COALESCE(knowledge_category,''),"
        " created_at, COALESCE(pipeline_detail,'{}'), COALESCE(error_msg,''),"
        " COALESCE(domain,'unknown'), COALESCE(domain_confidence,0) FROM documents_old")
    conn.execute("DROP TABLE documents_old")
    # ② 重建 document_chunks（document_id 外键重新指向新 documents 表，级联保留）
    conn.execute("ALTER TABLE document_chunks RENAME TO document_chunks_old")
    conn.execute("""CREATE TABLE document_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        chunk_index INTEGER DEFAULT 0,
        content TEXT NOT NULL,
        embedding TEXT DEFAULT '[]',
        embed_version TEXT DEFAULT 'bigram-tf',
        source_doc TEXT DEFAULT '',
        branch TEXT DEFAULT 'dev',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        bm25_text TEXT DEFAULT '',
        section TEXT DEFAULT '',
        linked_entity_ids TEXT DEFAULT '[]',
        domain TEXT DEFAULT 'unknown',
        hyde_questions TEXT DEFAULT '[]',
        hyde_embedding TEXT DEFAULT '[]'
    )""")
    conn.execute(
        "INSERT INTO document_chunks (id, document_id, chunk_index, content, embedding,"
        " embed_version, source_doc, branch, created_at, bm25_text, section, linked_entity_ids,"
        " domain, hyde_questions, hyde_embedding) "
        "SELECT id, document_id, chunk_index, content, COALESCE(embedding,'[]'),"
        " COALESCE(embed_version,'bigram-tf'), COALESCE(source_doc,''), COALESCE(branch,'dev'),"
        " created_at, COALESCE(bm25_text,''), COALESCE(section,''), COALESCE(linked_entity_ids,'[]'),"
        " COALESCE(domain,'unknown'), COALESCE(hyde_questions,'[]'), COALESCE(hyde_embedding,'[]')"
        " FROM document_chunks_old")
    conn.execute("DROP TABLE document_chunks_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(document_id)")
    # ③ 重建 doc_metadata（document_id 外键重新指向新 documents 表，级联保留）
    conn.execute("ALTER TABLE doc_metadata RENAME TO doc_metadata_old")
    conn.execute("""CREATE TABLE doc_metadata (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
        title TEXT DEFAULT '',
        author TEXT DEFAULT '',
        version TEXT DEFAULT 'v1.0',
        tags TEXT DEFAULT '[]',
        source TEXT DEFAULT 'upload',
        extra TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute(
        "INSERT INTO doc_metadata (id, document_id, title, author, version, tags, source, extra, created_at) "
        "SELECT id, document_id, COALESCE(title,''), COALESCE(author,''), COALESCE(version,'v1.0'),"
        " COALESCE(tags,'[]'), COALESCE(source,'upload'), COALESCE(extra,'{}'), created_at"
        " FROM doc_metadata_old")
    conn.execute("DROP TABLE doc_metadata_old")
    # 自增序列修正：显式拷贝 id 后同步 sqlite_sequence，避免后续 INSERT 主键冲突
    for table in ("documents", "document_chunks", "doc_metadata"):
        conn.execute(
            "UPDATE sqlite_sequence SET seq=(SELECT COALESCE(MAX(id),0) FROM " + table + ") WHERE name=?",
            (table,))
    conn.commit()
    print("[init_db] 迁移: documents 重建（移除失效的 data_sources_old 外键引用）")


def _rename_dev_branch(conn):
    """主开发分支改名：dev → dev（默认三分支 release / dev / personal）。

    幂等：branches 表无 dev 则跳过。级联改名所有引用旧名的行：
    branches(name/parent_branch)、entities、relations、documents、document_chunks、
    merge_requests(source/target)、settings(default_branch)。
    同时清理种子示例分支（dev/knowledge-jul，含其数据），并补默认 personal 分支。
    """
    if not conn.execute("SELECT 1 FROM branches WHERE name='dev/main'").fetchone():
        return
    conn.execute("UPDATE branches SET name='dev' WHERE name='dev/main'")
    conn.execute("UPDATE branches SET parent_branch='dev' WHERE parent_branch='dev/main'")
    conn.execute("UPDATE entities SET branch='dev' WHERE branch='dev/main'")
    conn.execute("UPDATE relations SET branch='dev' WHERE branch='dev/main'")
    conn.execute("UPDATE documents SET branch='dev' WHERE branch='dev/main'")
    conn.execute("UPDATE document_chunks SET branch='dev' WHERE branch='dev/main'")
    conn.execute("UPDATE merge_requests SET source_branch='dev' WHERE source_branch='dev/main'")
    conn.execute("UPDATE merge_requests SET target_branch='dev' WHERE target_branch='dev/main'")
    conn.execute("UPDATE settings SET value='dev' WHERE key='default_branch' AND value='dev/main'")
    # 默认三分支：清理种子示例分支 dev/knowledge-jul（含其数据）并补 personal
    conn.execute("DELETE FROM document_chunks WHERE branch='dev/knowledge-jul'")
    conn.execute("DELETE FROM documents WHERE branch='dev/knowledge-jul'")
    conn.execute("DELETE FROM relations WHERE branch='dev/knowledge-jul'")
    conn.execute("DELETE FROM entities WHERE branch='dev/knowledge-jul'")
    conn.execute("DELETE FROM branches WHERE name='dev/knowledge-jul'")
    conn.execute("INSERT OR IGNORE INTO branches (name, branch_type, parent_branch, description) "
                 "VALUES ('personal', 'personal', 'dev', '个人工作分支')")
    conn.commit()
    print("[init_db] 迁移: 主开发分支 dev/main → dev（级联改名 + 默认三分支 release/dev/personal）")


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

    _add("conversations", "project_id", "TEXT DEFAULT 'project-satnet-broadband'")
    _add("entities", "project_id", "TEXT DEFAULT 'project-satnet-broadband'")
    _add("relations", "project_id", "TEXT DEFAULT 'project-satnet-broadband'")
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
    conn.commit()


def _migrate_mr_status(conn):
    """分支管理 GitHub 对标（P1-1）：merge_requests.status 旧枚举迁移为新状态机。

    - pending → open、approved → merged、rejected → closed
    - 幂等：已为新枚举值（draft/open/merged/closed）的行不重复处理
    - 旧调用方 / 历史数据兼容：后续写路径以 _norm_mr_status 归一化，读路径
      （dashboard / ingest_gate）统一按新枚举 + draft/open 未处理语义查询
    """
    c = conn.cursor()
    mapping = {"pending": "open", "approved": "merged", "rejected": "closed"}
    for old, new in mapping.items():
        cur = c.execute(
            "UPDATE merge_requests SET status=? WHERE status=?", (new, old))
        if cur.rowcount:
            print(f"[init_db] 迁移: merge_requests.status {old} → {new}（{cur.rowcount} 行）")
    conn.commit()


def _ensure_ontology_types(conn):
    """O-3：老库幂等补齐本体类型（_seed 仅在空库执行，老库需增量补 O-3 新增类型）。"""
    c = conn.cursor()
    for name, kind in [("需求", "entity"), ("部件", "entity"), ("功能", "entity"),
                        ("SATISFIES", "relation"), ("连接", "relation"), ("执行", "relation")]:
        c.execute("INSERT OR IGNORE INTO ontology_types (name, type_kind) VALUES (?,?)", (name, kind))
    conn.commit()


def _backfill_pipeline_detail(conn):
    """回填老文档 pipeline_detail：completed 且空 → 四阶段 done；failed 且空 → 标记 parse failed。"""
    import json as _j
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, parse_status, chunk_count, error_msg FROM documents WHERE pipeline_detail='{}' OR pipeline_detail=''"
    ).fetchall()
    for r in rows:
        if r["parse_status"] == "completed" and r["chunk_count"]:
            detail = {"parse": "done", "chunk": "done", "embed": "done", "insert": "done"}
        elif r["parse_status"] == "failed":
            detail = {"parse": "failed"}
            if not r["error_msg"]:
                c.execute("UPDATE documents SET error_msg='处理失败（历史记录无明细）' WHERE id=?", (r["id"],))
        else:
            detail = {}
        if detail:
            c.execute("UPDATE documents SET pipeline_detail=? WHERE id=?",
                      (_j.dumps(detail, ensure_ascii=False), r["id"]))
    conn.commit()


def _migrate_graph_tables(conn):
    """KB-P2：图谱编辑日志表。"""
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS graph_edit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op TEXT NOT NULL,
        node_id TEXT DEFAULT '',
        payload TEXT DEFAULT '{}',
        operator TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    # E-1：消歧候选表（blocking 分块 → 候选落库 → 证据展示 → 三档阈值审阅）
    c.execute("""CREATE TABLE IF NOT EXISTS entity_dup_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keep_id TEXT NOT NULL,
        dup_id TEXT NOT NULL,
        entity_type TEXT DEFAULT '',
        score REAL DEFAULT 0,
        vec_score REAL DEFAULT 0,
        fuzzy_score REAL DEFAULT 0,
        method TEXT DEFAULT '',
        evidence TEXT DEFAULT '{}',
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        reviewed_by TEXT DEFAULT '',
        reviewed_at TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_dup_status ON entity_dup_candidates(status)")
    # E-1：合并审计表（可撤销）
    c.execute("""CREATE TABLE IF NOT EXISTS entity_merges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keep_id TEXT NOT NULL, dup_id TEXT NOT NULL,
        score REAL, method TEXT, operator TEXT,
        props_merged TEXT DEFAULT '[]',
        relations_redirected INTEGER DEFAULT 0,
        status TEXT DEFAULT 'merged',
        rollback_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()


def _migrate_v2g_tables(conn):
    """O-1：向量→图谱转化工作流表（候选实体 + 转化批次）。"""
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS v2g_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,            -- 候选批次（前端分组展示）
        chunk_id INTEGER DEFAULT 0,        -- 来源 chunk
        source_doc TEXT DEFAULT '',        -- 来源文档
        entity_name TEXT NOT NULL,         -- 候选实体名
        entity_type TEXT DEFAULT '',       -- 候选实体类型（本体约束）
        properties TEXT DEFAULT '{}',      -- 候选属性
        rel_type TEXT DEFAULT '',          -- 候选关系（可选：与其他候选/已有实体）
        rel_source TEXT DEFAULT '',        -- 关系源端实体名（S7：三元组友好展示）
        rel_target TEXT DEFAULT '',        -- 关系目标（可选）
        status TEXT DEFAULT 'pending',     -- pending | confirmed | rejected | merged
        errors TEXT DEFAULT '',            -- 校验错误（被拒原因）
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        confidence REAL DEFAULT 0,         -- 抽取置信度（0-1）
        matching_status TEXT DEFAULT 'none',  -- 消歧匹配：none | dup_suspect | dup_high
        match_entity_id TEXT DEFAULT '',   -- 关联的已有实体 ID
        rel_matching_status TEXT DEFAULT 'none',  -- 关系候选消歧：none | dup_high
        rel_match_rel_id TEXT DEFAULT '',   -- 重复时关联的已有关系边 ID
        sysml_version_id INTEGER DEFAULT 0, -- >0 = AI 建模候选（关联 sysml_versions.id，入库溯源）
        reject_reason TEXT DEFAULT '',      -- 驳回原因
        candidate_kind TEXT DEFAULT 'entity',   -- Staging 三义拆分：entity | relation | reflow
        mention_json TEXT DEFAULT '{}',         -- 原始提及快照（溯源可重放）
        source_type TEXT DEFAULT '',             -- B1: 候选来源 ai_model | doc_extract | manual
        normalized_key TEXT DEFAULT '',         -- 归一化规范键（写前融合闸消歧用）
        _canopy_key TEXT DEFAULT ''             -- Blocking 分桶键（类型+归一化名前缀）
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_v2g_batch ON v2g_candidates(batch_id)")
    conn.commit()


def _migrate_staging_tables(conn):
    """P0 Staging 写前融合改造（诊断 §4.2）：v2g_candidates 三义拆分 + Mention/Canonical 双层模型。

    目标：将 v2g_candidates 从「一表三义」（实体候选/关系候选/回流候选）显式拆分语义，
    并为写前融合闸（Task #7 staging_fuse）预置 Blocking 分桶与溯源支撑列：
    - v2g_candidates.candidate_kind：entity | relation | reflow（三义拆分）
    - v2g_candidates.mention_json：原始提及快照（mention 保留原形，溯源可重放）
    - v2g_candidates.normalized_key：归一化规范键（消歧/去重/确认定位）
    - v2g_candidates._canopy_key：Blocking 分桶键（类型+归一化名前缀，避免全表 O(n²)）
    - entities.canonical_id：canonical 黄金实体（自身为空），mention 实体指向 canonical（n:1）
    - entity_aliases：mention 原文 → canonical 的别名映射表（可重放）
    - staging_fuse_log：写前融合闸审计（auto_merge / review_queue / new_entity / batch_reject）

    幂等：老库由 _add 补列 + CREATE TABLE IF NOT EXISTS；新库列已在 _migrate_v2g_tables 建全。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    # 1) v2g_candidates 三义拆分 + 融合闸支撑列（老库补列）
    _add("v2g_candidates", "candidate_kind", "TEXT DEFAULT 'entity'")   # entity | relation | reflow
    _add("v2g_candidates", "mention_json", "TEXT DEFAULT '{}'")         # 原始提及快照（可重放）
    _add("v2g_candidates", "normalized_key", "TEXT DEFAULT ''")         # 归一化规范键
    _add("v2g_candidates", "_canopy_key", "TEXT DEFAULT ''")            # Blocking 分桶键
    # 2) entities 双层模型：canonical 黄金实体 id（自身为 canonical 时为空串）
    _add("entities", "canonical_id", "TEXT DEFAULT ''")
    # 3) entity_aliases：mention 原文 → canonical 的 n:1 别名映射
    conn.execute("""CREATE TABLE IF NOT EXISTS entity_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id TEXT NOT NULL,            -- canonical 实体 id
        alias_name TEXT NOT NULL,           -- mention 原文（别名/缩写/翻译/变体）
        branch TEXT DEFAULT 'dev',
        source_doc TEXT DEFAULT '',
        source_type TEXT DEFAULT 'vector',
        created_by TEXT DEFAULT 'system',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(entity_id, alias_name, branch)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_aliases_name ON entity_aliases(alias_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases(entity_id)")
    # 4) staging_fuse_log：写前融合闸审计（批次级/候选级分流留痕）
    conn.execute("""CREATE TABLE IF NOT EXISTS staging_fuse_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        candidate_id INTEGER DEFAULT 0,     -- v2g_candidates.id（0=批量级事件）
        action TEXT NOT NULL,               -- auto_merge | review_queue | new_entity | batch_reject | skipped
        keep_id TEXT DEFAULT '',
        dup_id TEXT DEFAULT '',
        score REAL DEFAULT 0,
        reason TEXT DEFAULT '',
        operator TEXT DEFAULT 'system',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_staging_fuse_batch ON staging_fuse_log(batch_id)")
    conn.commit()


def _migrate_sysml_tables(conn):
    """O-3：SysML 导入批次 + 双向一致性映射表。"""
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sysml_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL UNIQUE,
        model_name TEXT DEFAULT '',
        source TEXT DEFAULT '',            -- upload | json | text
        entity_count INTEGER DEFAULT 0,
        relation_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',     -- pending | done | partial | failed
        detail TEXT DEFAULT '',
        imported_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sysml_sync (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT DEFAULT '',
        sysml_ref TEXT NOT NULL,           -- 模型元素引用（如 part 名/uri）
        node_id TEXT DEFAULT '',           -- 知识库节点 id
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()


def _migrate_profile_tables(conn):
    """P1-3：SysML Profile 导入元信息表（base_metaclass_map 供导出恢复 Extension）。"""
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_profile_meta (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        version TEXT DEFAULT '1.0',
        format TEXT NOT NULL,              -- 1x | v2 | v2xml
        base_metaclass_map TEXT DEFAULT '{}',   -- JSON: {stereotype: Block|Requirement|...}
        imported_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()


def _apply_env_keys(conn):
    """部署友好：从系统配置（core.config 的 llm 分组）注入预置 provider 的 API key。

    支持配置项：llm.deepseek_api_key / llm.qwen_api_key，
    兼容环境变量 MBSE_LLM_DEEPSEEK_API_KEY / MBSE_LLM_QWEN_API_KEY（config 已映射）。
    仅在 provider 当前无 key 时写入（幂等，不覆盖用户在界面配置的密钥）。
    """
    from core import config as _cfg
    mapping = {
        "deepseek_api_key": "DeepSeek-V3",
        "qwen_api_key": "Qwen2.5-72B",
    }
    c = conn.cursor()
    for cfg_key, provider_name in mapping.items():
        key = str(_cfg.get("llm", cfg_key, "") or "").strip()
        if not key:
            continue
        c.execute(
            "UPDATE llm_providers SET api_key=? WHERE name=? AND (api_key IS NULL OR api_key='')",
            (key, provider_name),
        )
        if c.rowcount:
            print(f"[init_db] 已从系统配置 llm.{cfg_key} 注入 {provider_name} 的 API key")
    conn.commit()


def _migrate_glossary_tables(conn):
    """Glossary 术语表 + 查询 Trace + domain review 队列。幂等建表。"""
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS glossary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_term TEXT NOT NULL UNIQUE,        -- 用户口语/别名/缩写（小写存储）
        canonical_term TEXT NOT NULL,          -- 规范术语（归一化目标）
        domain TEXT DEFAULT 'unknown',         -- 归入域（受控词表）
        intent TEXT DEFAULT '',                -- 强制意图（命中时路由到该 Agent）
        boost REAL DEFAULT 1.5,                -- 检索加权（domain 过滤命中时 score×boost）
        description TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_glossary_active ON glossary(active)")
    # P0 方案 v2 / S2：词典体系统一——kind 区分映射类型，provenance 记来源（审计可追溯）
    cols = [r[1] for r in c.execute("PRAGMA table_info(glossary)").fetchall()]
    if "kind" not in cols:
        c.execute("ALTER TABLE glossary ADD COLUMN kind TEXT DEFAULT 'intent'")
        c.execute("UPDATE glossary SET kind='intent' WHERE kind IS NULL OR kind=''")
    if "suggested_by" not in cols:
        c.execute("ALTER TABLE glossary ADD COLUMN suggested_by TEXT DEFAULT ''")  # seed/manual/llm
    if "provenance" not in cols:
        c.execute("ALTER TABLE glossary ADD COLUMN provenance TEXT DEFAULT ''")    # JSON: 理由+置信度
    c.execute("""CREATE TABLE IF NOT EXISTS query_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,                   -- 原始用户输入
        normalized TEXT DEFAULT '',            -- 归一化后（Glossary 替换）
        intent TEXT DEFAULT '',                -- 路由意图
        route TEXT DEFAULT '',                 -- graph | vector | mixed
        domain TEXT DEFAULT '',                -- 检索域
        hit_docs TEXT DEFAULT '[]',            -- JSON: 命中文档名列表
        hit_count INTEGER DEFAULT 0,
        top_score REAL DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        detail TEXT DEFAULT '{}',              -- JSON: 各环节详情（归一化/置信度/召回原因）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trace_time ON query_trace(created_at)")
    c.execute("""CREATE TABLE IF NOT EXISTS domain_review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER DEFAULT 0,
        filename TEXT DEFAULT '',
        suggested_domain TEXT DEFAULT 'unknown',
        confidence REAL DEFAULT 0,
        reason TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',         -- pending | confirmed | corrected
        reviewed_by TEXT DEFAULT '',
        reviewed_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_drq_status ON domain_review_queue(status)")
    conn.commit()


def _migrate_ontology_change_tables(conn):
    """FR-KG-4 补 G7：本体类型变更留痕表（add/update/delete 全量前后快照 + 操作人）。幂等建表。

    - before/after 存变更前/后的完整行 JSON（add 时 before='{}'，delete 时 after='{}'），
      供前端「变更历史」展示 diff 摘要，实现本体类型全部变更可追溯。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_change_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_id INTEGER NOT NULL,            -- 本体类型 id
        action TEXT NOT NULL,                -- add | update | delete
        before TEXT DEFAULT '{}',            -- 变更前整行 JSON（add 时为 '{}'）
        after TEXT DEFAULT '{}',             -- 变更后整行 JSON（delete 时为 '{}'）
        operator TEXT DEFAULT '',            -- 操作人（_actor(user)）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ocl_type ON ontology_change_logs(type_id)")
    conn.commit()


def _migrate_ontology_version_tables(conn):
    """本体版本管理：本体整体 SemVer 版本链（vMAJOR.MINOR.PATCH），每次 Schema 变更自动递增。

    对齐行业实践（owl:versionIRI + SemVer）——本体是全局共享 Schema 不做分支隔离，
    用「版本 + 变更日志」治理：major=破坏性（删除/重命名）、minor=新增（类型/属性）、patch=小改。
    status=草稿 draft → 发布 released（消费侧以已发布版本为稳定基线）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_label TEXT NOT NULL,         -- v1.2.0
        major INTEGER NOT NULL DEFAULT 1,
        minor INTEGER NOT NULL DEFAULT 0,
        patch INTEGER NOT NULL DEFAULT 0,
        change_type TEXT NOT NULL,           -- add | update | delete | apply | import
        summary TEXT DEFAULT '',             -- 本次变更摘要（如「新增类型: 卫星」）
        operator TEXT DEFAULT '',            -- 操作人
        status TEXT DEFAULT 'draft',         -- draft（草稿）| released（已发布，稳定基线）
        released_at TEXT DEFAULT '',
        released_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ont_ver ON ontology_versions(created_at DESC, id DESC)")
    # 老库补列（幂等）：status/released_at/released_by
    cols = {r[1] for r in c.execute("PRAGMA table_info(ontology_versions)").fetchall()}
    if "status" not in cols:
        c.execute("ALTER TABLE ontology_versions ADD COLUMN status TEXT DEFAULT 'draft'")
    if "released_at" not in cols:
        c.execute("ALTER TABLE ontology_versions ADD COLUMN released_at TEXT DEFAULT ''")
    if "released_by" not in cols:
        c.execute("ALTER TABLE ontology_versions ADD COLUMN released_by TEXT DEFAULT ''")
    # 2026-09-02 版本快照治理（消费侧以已发布版本为准）：快照统计 / 兼容性（1=自动跟随 0=破坏性需迁移）/ active 消费指针
    for col, ddl in (
        ("snapshot_count", "INTEGER DEFAULT 0"),
        ("compatible", "INTEGER DEFAULT 1"),
        ("active", "INTEGER DEFAULT 0"),
        ("snapshot_created_at", "TEXT DEFAULT ''"),
    ):
        if col not in cols:
            c.execute(f"ALTER TABLE ontology_versions ADD COLUMN {col} {ddl}")
    # 版本快照表：发布时全量复制 ontology_types（规范化、可 SQL 查询）；只写不改（版本工件不可变）
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_version_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id INTEGER NOT NULL REFERENCES ontology_versions(id),
        type_id INTEGER,                 -- 源 ontology_types.id（追溯）
        name TEXT NOT NULL,
        type_kind TEXT NOT NULL,         -- entity | relation | attribute
        parent_id INTEGER,
        properties TEXT DEFAULT '{}',
        constraints TEXT DEFAULT '{}',
        description TEXT DEFAULT '',
        icon TEXT DEFAULT '',
        color TEXT DEFAULT '#185FA5',
        iri TEXT DEFAULT '',             -- P0-1：实体 IRI（快照固化，导出消费）
        UNIQUE(version_id, type_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_ver ON ontology_version_snapshots(version_id)")
    # P0-1：老库快照表补 iri 列（幂等）
    _scols = [r[1] for r in c.execute("PRAGMA table_info(ontology_version_snapshots)").fetchall()]
    if "iri" not in _scols:
        c.execute("ALTER TABLE ontology_version_snapshots ADD COLUMN iri TEXT DEFAULT ''")
    conn.commit()


def _dedupe_ontology_types(conn):
    """P0-1 数据治理：ontology_types.name 去重 + UNIQUE 索引。

    根因：ontology_types.name 原先无 UNIQUE 约束，_ensure_ontology_types（每次 init_db 执行）
    与 _seed（空库执行）对 需求/部件/功能/SATISFIES/连接/执行 双双插入，
    INSERT OR IGNORE 因无冲突检测而失效 → 同名重复行 → IRI 回填被迫加 _2 后缀。
    处理：按 name 分组保留 id 最小者；parent_id 引用重定向到保留行；删除重复行；
    保留行若 IRI 为 namespace+slug(name)+_N 且 base 未被占用，归一化回 base；
    最后建 UNIQUE 索引（idx_ontology_types_name），此后 INSERT OR IGNORE 真正幂等。
    幂等：无重复可去、索引已存在时均为 no-op。
    须在全部种子插入之后、_migrate_ontology_iri（IRI 回填）之前调用。
    """
    import re as _re
    c = conn.cursor()
    dups = c.execute(
        "SELECT name FROM ontology_types GROUP BY name HAVING COUNT(*) > 1"
    ).fetchall()
    for d in dups:
        name = d["name"]
        rows = c.execute(
            "SELECT id FROM ontology_types WHERE name=? ORDER BY id ASC", (name,)
        ).fetchall()
        keep_id = rows[0]["id"]
        for r in rows[1:]:
            c.execute("UPDATE ontology_types SET parent_id=? WHERE parent_id=?", (keep_id, r["id"]))
            c.execute("DELETE FROM ontology_types WHERE id=?", (r["id"],))
    # IRI 归一化：重复行删除后，保留行机器生成的 base_N 形式 IRI 可收回 base
    # （ontology_meta 可能尚未建表——_migrate_ontology_iri 在本函数之后执行，故从现有 IRI 推断 ns）
    row = c.execute("SELECT iri FROM ontology_types WHERE iri IS NOT NULL AND iri != '' LIMIT 1").fetchone()
    if row and "#" in row["iri"]:
        ns = row["iri"].split("#")[0] + "#"
        taken = {r["iri"] for r in c.execute("SELECT iri FROM ontology_types WHERE iri").fetchall()}
        for r in c.execute("SELECT id, name, iri FROM ontology_types WHERE iri IS NOT NULL AND iri != ''").fetchall():
            base = ns + _re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5]", "_", str(r["name"]))
            if r["iri"] != base and _re.fullmatch(_re.escape(base) + r"_\d+", r["iri"]) and base not in taken:
                c.execute("UPDATE ontology_types SET iri=? WHERE id=?", (base, r["id"]))
                taken.discard(r["iri"])
                taken.add(base)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ontology_types_name ON ontology_types(name)")
    conn.commit()


def _migrate_ontology_iri(conn):
    """P0-1：本体 IRI 主轴（WebProtege 对齐）——ontology_meta 单行表 + 存量类型 IRI 回填。

    ontology_meta 存命名空间 / IRI 生成策略 / 默认前缀（全局唯一，单行 id=1）。
    回填：存量类型 iri 为空时按 namespace + slugify(name) 生成；冲突追加 _2/_3（幂等）。
    须在 _migrate_columns（已补 iri 列）与 _ensure_ontology_types（已保证类型存在）之后调用。
    """
    import re as _re
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        namespace TEXT NOT NULL DEFAULT 'http://www.xingwang.mbse/ontology#',
        iri_strategy TEXT NOT NULL DEFAULT 'hash-name',   -- hash-name | uuid | user-supplied
        default_prefix TEXT DEFAULT '',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""INSERT OR IGNORE INTO ontology_meta (id, namespace, iri_strategy, default_prefix)
                 VALUES (1, 'http://www.xingwang.mbse/ontology#', 'hash-name', '')""")
    meta = c.execute("SELECT namespace FROM ontology_meta WHERE id=1").fetchone()
    ns = (meta["namespace"] if meta else "http://www.xingwang.mbse/ontology#") or "http://www.xingwang.mbse/ontology#"

    def _slug(s):
        return _re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5]", "_", str(s))

    taken = set()
    for r in c.execute("SELECT id, name, iri FROM ontology_types").fetchall():
        if r["iri"]:
            taken.add(r["iri"])
    for r in c.execute("SELECT id, name, iri FROM ontology_types").fetchall():
        if r["iri"]:
            continue
        base = ns + _slug(r["name"])
        cand, i = base, 2
        while cand in taken:
            cand = f"{base}_{i}"
            i += 1
        taken.add(cand)
        c.execute("UPDATE ontology_types SET iri=? WHERE id=?", (cand, r["id"]))
    conn.commit()


def _migrate_eval_tables(conn):
    """FR-KG-1 补 G15：抽取质量评估报告表（golden set 比对 → 精确率/召回率/F1）。幂等建表。

    - 每行 = 一次评估运行：实体/关系双维度 P/R/F1（0-1）+ detail JSON
      （extracted 抽取结果 / golden 期望 / hits 命中 / misses 未命中明细）
    - 报告按 created_at 倒序查询（治理中心「历史报告」列表）
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS eval_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL,               -- 被评估文档 id
        doc_name TEXT DEFAULT '',              -- 文档名（报告展示用）
        entity_precision REAL DEFAULT 0,
        entity_recall REAL DEFAULT 0,
        entity_f1 REAL DEFAULT 0,
        relation_precision REAL DEFAULT 0,
        relation_recall REAL DEFAULT 0,
        relation_f1 REAL DEFAULT 0,
        detail TEXT DEFAULT '{}',              -- JSON: {extracted, golden, hits, misses, metric}
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_eval_reports_doc ON eval_reports(doc_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_eval_reports_time ON eval_reports(created_at)")
    conn.commit()


def _migrate_kb_v2_enhance(conn):
    """知识图谱构建平台 v2 增强（对标 v2.0：融合四步闭环 + 评估回环 + 本体蓝图）。
    - v2g_candidates.normalized_name：抽取后表面归一（融合第一步 P0-2，作去重/消歧/确认定位的规范名）
    - eval_reports.model_version/is_golden/golden_set_id：A/B 评测与看板透视（P1-2/P2-3）
    - knowledge_conflicts：冲突消解（融合第四步 P0-1：矛盾事实 → 加权评分 → 人工裁决留痕）
    - golden_samples：Golden Set 分层建立（评估回环 P1-3）
    - ontology_drafts：LLM 辅助本体蓝图（P2-1：文档 → 本体草案 → 人工确认）
    幂等：建表 IF NOT EXISTS + 列迁移 _add。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    _add("v2g_candidates", "normalized_name", "TEXT DEFAULT ''")
    _add("v2g_candidates", "source_type", "TEXT DEFAULT ''")  # B1: 候选来源 ai_model|doc_extract|manual
    _add("v2g_candidates", "source_type", "TEXT DEFAULT ''")  # B1: 候选来源 ai_model|doc_extract|manual
    _add("eval_reports", "model_version", "TEXT DEFAULT ''")
    _add("eval_reports", "is_golden", "INTEGER DEFAULT 0")
    _add("eval_reports", "golden_set_id", "INTEGER DEFAULT 0")
    # P2-1 整合：本体蓝图统一 Profile 导入（profile 草案透传溯源列）
    _add("ontology_drafts", "profile_source", "TEXT DEFAULT ''")
    _add("ontology_drafts", "profile_ref", "TEXT DEFAULT ''")
    _add("ontology_drafts", "constraints", "TEXT DEFAULT '{}'")
    c = conn.cursor()
    # ── P0-1 冲突消解表（融合第四步）──
    c.execute("""CREATE TABLE IF NOT EXISTS knowledge_conflicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                  -- entity_attr | relation_attr
        entity_id_a TEXT DEFAULT '',         -- 冲突方 A（实体 id；relation 冲突时 ''）
        entity_id_b TEXT DEFAULT '',
        relation_id_a INTEGER DEFAULT 0,     -- 冲突方 A（关系 id；entity 冲突时 0）
        relation_id_b INTEGER DEFAULT 0,
        attr_key TEXT NOT NULL,              -- 冲突属性 key
        value_a TEXT DEFAULT '',             -- A 侧值
        value_b TEXT DEFAULT '',
        score_a REAL DEFAULT 0,              -- A 侧加权分（可信度×0.5 + 时效性×0.3 + 权威性×0.2）
        score_b REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',       -- pending | resolved | ignored
        decision TEXT DEFAULT '',            -- left | right（采纳值侧，ignore 时 ''）
        decided_by TEXT DEFAULT '',
        decided_at TEXT DEFAULT '',
        evidence TEXT DEFAULT '{}',          -- JSON：两侧来源/时间/置信度明细
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_conflicts_status ON knowledge_conflicts(status)")
    # ── P1-3 Golden Set 分层建立（评估回环基准）──
    c.execute("""CREATE TABLE IF NOT EXISTS golden_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER DEFAULT 0,            -- 基准所属文档（按 doc_name 反查）
        doc_name TEXT DEFAULT '',
        kind TEXT NOT NULL,                  -- entity | relation
        name TEXT DEFAULT '',                -- 实体名（kind=entity）
        entity_type TEXT DEFAULT '',
        source TEXT DEFAULT '',              -- 关系源端名（kind=relation）
        relation_type TEXT DEFAULT '',
        target TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_golden_doc ON golden_samples(doc_id)")
    # ── P2-1 LLM 辅助本体蓝图（文档 → 本体草案 → 人工确认应用）──
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type_kind TEXT DEFAULT 'entity',     -- entity | relation | attribute
        parent_name TEXT DEFAULT '',         -- 建议父类型（entity）
        properties TEXT DEFAULT '{}',        -- JSON: 建议属性 {key:{note,type,required}}
        relation_src TEXT DEFAULT '',        -- 关系域（type_kind=relation）
        relation_tgt TEXT DEFAULT '',
        evidence TEXT DEFAULT '',            -- 来源片段/冲突提示
        constraints TEXT DEFAULT '{}',       -- JSON: required/allowed_values 等（Profile 结构化约束）
        profile_source TEXT DEFAULT '',      -- 来源 SysML Profile 名（profile 草案溯源）
        profile_ref TEXT DEFAULT '',         -- 对应 Stereotype/metadata 元素名
        status TEXT DEFAULT 'pending',       -- pending | applied | rejected
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ont_draft_batch ON ontology_drafts(batch_id)")
    conn.commit()


def _seed_departments(conn):
    """部门设置种子：空表时插入默认部门（老库升级也能获得）。

    幂等：INSERT OR IGNORE，预置用户（seeds.py）归属的部门名保持可下拉选择。
    """
    c = conn.cursor()
    defaults = [
        ("网络系统总体设计室", "网络总体设计（预置用户归属）", 1),
        ("数字化部", "数字化建设与运维（预置用户归属）", 2),
        ("总体论证部", "系统论证与需求分析", 3),
        ("型号设计部", "型号设计与建模", 4),
        ("质量与评审部", "质量管控与预评审", 5),
    ]
    for name, desc, order in defaults:
        c.execute(
            "INSERT OR IGNORE INTO departments (name, description, sort_order) VALUES (?,?,?)",
            (name, desc, order),
        )
    conn.commit()
    print("[init_db] 迁移: 部门表种子（默认部门）")


def _seed_knowledge_categories(conn):
    """P0-3/P0-4 迁移：知识分类种子（设计方法知识/设计资产）+ 生命周期发布回填。

    - knowledge_categories 幂等种子：9 个内置子类（INSERT OR IGNORE，按 name 唯一）
    - knowledge_publish_logs 表：老库补建（新库已由 schema.py 创建）
    - 发布回填：既有 release 分支 reviewed 实体按 reviewed_at/created_at 回填 published_at，
      并写一条发布日志（version=1）——使存量已发布数据具备"发布"生命周期标记
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS knowledge_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        group_name TEXT DEFAULT '设计方法知识',
        description TEXT DEFAULT '',
        builtin INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS knowledge_publish_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id TEXT NOT NULL,
        name TEXT DEFAULT '',
        branch TEXT DEFAULT 'release',
        published_at TEXT DEFAULT CURRENT_TIMESTAMP,
        version INTEGER DEFAULT 1,
        merged_from TEXT DEFAULT '',
        created_by TEXT DEFAULT ''
    )""")
    seeds = [
        ("设计准则", "设计方法知识", "设计必须遵守的规则与约束（AI 规范约束源，强制遵守）", 1),
        ("设计流程", "设计方法知识", "标准设计流程与方法步骤", 2),
        ("最佳实践", "设计方法知识", "领域最佳实践与经验沉淀", 3),
        ("模板方法", "设计方法知识", "可复用的模板/骨架方法", 4),
        ("已有模型", "设计资产", "既有 SysML/领域模型资产", 5),
        ("可复用构件", "设计资产", "可复用的构件/模块/组件", 6),
        ("设计方案", "设计资产", "历史设计方案与备选方案", 7),
        ("案例库", "设计资产", "典型用例与案例", 8),
        ("参数设计", "设计资产", "参数化设计数据与取值", 9),
    ]
    for name, group, desc, order in seeds:
        c.execute("INSERT OR IGNORE INTO knowledge_categories (name, group_name, description, sort_order) "
                  "VALUES (?,?,?,?)", (name, group, desc, order))
    # 生命周期发布回填：存量 release reviewed 实体 → published_at + 发布日志（幂等：仅补空 published_at）
    rows = c.execute(
        "SELECT id, name, branch, reviewed_at, created_at FROM entities "
        "WHERE branch='release' AND status='reviewed' AND (published_at IS NULL OR published_at='')"
    ).fetchall()
    for r in rows:
        ts = r["reviewed_at"] or r["created_at"] or ""
        c.execute("UPDATE entities SET published_at=? WHERE id=? AND branch='release'",
                  (ts, r["id"]))
        c.execute(
            "INSERT INTO knowledge_publish_logs (entity_id, name, branch, published_at, version, merged_from) "
            "VALUES (?,?,?,?,1,'dev')",
            (r["id"], r["name"], "release", ts))
    conn.commit()
    if rows:
        print(f"[init_db] 迁移: 知识分类种子 + 发布回填 {len(rows)} 条 release 实体")
    else:
        print("[init_db] 迁移: 知识分类种子（内置 9 类）")


def _backfill_graph_source_info(conn):
    """图谱来源信息回填（幂等，只补空不覆盖）：候选确认入库链路此前未透传来源字段。

    根因：confirm_candidates 调 GraphStore.create_node/create_edge 未传 source_doc/created_by，
    导致 entities/relations.source_doc、relations.created_by 为空（候选表 v2g_candidates
    的 source_doc 是完整的）。回填策略：
    - 实体：按候选 chunk 溯源（chunk → document_chunks.linked_entity_ids → 实体）精确回填
    - 关系：按候选 rel_source/rel_target 定位两端实体，匹配 relations 行回填 source_doc/created_by
    - manual/sysml_import 等无候选来源的数据不回填（source_doc 空属合理）
    """
    c = conn.cursor()
    ent_filled = 0
    # 1) 实体：chunk 溯源优先（候选确认入库时已建立 chunk↔实体 链接）
    cands = c.execute(
        "SELECT id, entity_name, source_doc, chunk_id FROM v2g_candidates "
        "WHERE status='confirmed' AND source_doc!=''").fetchall()
    for cand in cands:
        if not cand["chunk_id"]:
            continue
        row = c.execute("SELECT linked_entity_ids FROM document_chunks WHERE id=?",
                        (cand["chunk_id"],)).fetchone()
        if not row:
            continue
        try:
            linked = json.loads(row["linked_entity_ids"] or "[]")
        except Exception:
            continue
        for eid in linked:
            cur = c.execute(
                "UPDATE entities SET source_doc=? WHERE id=? AND (source_doc IS NULL OR source_doc='')",
                (cand["source_doc"], eid))
            ent_filled += cur.rowcount
    # 2) 关系：候选 rel_source/rel_target → 两端实体 → relations 回填（只补空）
    rel_filled = 0
    rel_cands = c.execute(
        "SELECT rel_source, rel_target, rel_type, source_doc, created_by FROM v2g_candidates "
        "WHERE status='confirmed' AND entity_type='关系候选' AND source_doc!=''").fetchall()
    for rc in rel_cands:
        src = c.execute("SELECT id FROM entities WHERE name=? AND status!='deprecated' LIMIT 1",
                        (rc["rel_source"],)).fetchone()
        tgt = c.execute("SELECT id FROM entities WHERE name=? AND status!='deprecated' LIMIT 1",
                        (rc["rel_target"],)).fetchone()
        if not src or not tgt:
            continue
        cur = c.execute(
            "UPDATE relations SET source_doc=?, created_by=? "
            "WHERE source_id=? AND target_id=? AND relation_type=? "
            "AND (source_doc IS NULL OR source_doc='')",
            (rc["source_doc"], rc["created_by"] or "", src["id"], tgt["id"], rc["rel_type"]))
        rel_filled += cur.rowcount
    conn.commit()
    if ent_filled or rel_filled:
        print(f"[init_db] 迁移: 图谱来源信息回填（实体 {ent_filled} / 关系 {rel_filled}）")


def _backfill_commit_baseline(conn):
    """分支版本管理：为存量分支生成幂等基线提交（Git 式 head commit）。

    - 对 branches 表中 status!='archived' 的每个分支，若该分支在 knowledge_commits
      无任何提交，则插入一条 kind='import'、message='基线提交（存量数据）' 的提交，
      并把该提交 id 写入 branches.head_commit
    - 幂等：有提交的分支跳过，重复执行不重复生成
    - knowledge_commits 建表由 schema.py 建表区负责（本函数先于其调用时防御性跳过）
    """
    c = conn.cursor()
    if not c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_commits'"
    ).fetchone():
        return
    branches = c.execute(
        "SELECT id, name FROM branches WHERE status!='archived' ORDER BY id").fetchall()
    created = 0
    for r in branches:
        branch = r["name"]
        if c.execute(
            "SELECT 1 FROM knowledge_commits WHERE branch=? LIMIT 1", (branch,)).fetchone():
            continue  # 已有提交（含基线/后续提交）→ 跳过
        cur = c.execute(
            "INSERT INTO knowledge_commits (branch, kind, message, changes, snapshot, created_by) "
            "VALUES (?, 'import', '基线提交（存量数据）', '{}', '{}', '系统')",
            (branch,))
        c.execute("UPDATE branches SET head_commit=? WHERE id=?",
                  (cur.lastrowid, r["id"]))
        created += 1
    conn.commit()
    if created:
        print(f"[init_db] 迁移: 分支版本管理——为 {created} 个存量分支生成基线提交")


def _migrate_builtin_flags(conn):
    """AI 设计工坊四模块内置标识（skills/agents/mcp_servers/tools 加 builtin 列并回填）。

    需求：工坊各模块「类型=全部/内置/自定义」筛选；内置项禁止删除但可编辑修改。
    - tools：source='builtin' 即内置（file_*/graph_retrieve 等）
    - agents：7 个种子 Agent（requirement_analysis/design/impact/review/report_generation/knowledge_qa/chat）
    - skills：register_skills.py 平台内置技能（文件操作/行业调研/报告生成）
    - mcp_servers：当前无内置（全部自定义），列先建好以备平台预置
    幂等：_add 只补缺失列；回填 UPDATE 按内置来源/名称，重复执行无副作用。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    for t in ("skills", "agents", "mcp_servers", "tools"):
        _add(t, "builtin", "INTEGER DEFAULT 0")
    conn.execute("UPDATE tools SET builtin=1 WHERE source='builtin'")
    conn.execute(
        "UPDATE agents SET builtin=1 WHERE name IN "
        "('requirement_analysis','design','impact','review','report_generation','knowledge_qa','chat')")
    conn.execute(
        "UPDATE skills SET builtin=1 WHERE name IN ('文件操作','行业调研','报告生成')")
    conn.commit()


def _migrate_tool_status(conn):
    """工具注册表生命周期状态归一：deprecated/inactive → disabled。

    需求：工具启停改用「启用/停用」两态控制（不再使用弃用语义），
    历史 deprecated（旧弃用）与 inactive（脚本停用）统一为 disabled，
    前端按 active/disabled 渲染中文状态徽章与启停按钮。
    幂等：UPDATE 重复执行无副作用。
    """
    cur = conn.execute(
        "UPDATE tools SET status='disabled' WHERE status IN ('deprecated','inactive')")
    if cur.rowcount:
        print(f"[init_db] 迁移: 工具状态归一 {cur.rowcount} 行 deprecated/inactive → disabled")
    conn.commit()


def _migrate_agent_team(conn):
    """AI 设计工坊：主/子 Agent 团队（agents.agent_role + agent_team_members 表）。

    需求：主 Agent 可从已设置子 Agent 中选择成员组成多 Agent 团队；
    - agents.agent_role：main（主/团队负责人）| sub（子/团队成员，默认，兼容存量）
    - agent_team_members：多对多（一个子 Agent 可属多个团队），UNIQUE 防重复
    校验规则（接口层强制）：子 Agent 不可再添加子 Agent（两级封顶）；主 Agent 不可
    把主 Agent 加为成员；不可自加。删除/停用 Agent 时接口层级联清理团队关系。
    幂等：_add 只补缺失列；建表 IF NOT EXISTS。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    _add("agents", "agent_role", "TEXT DEFAULT 'sub'")  # main | sub
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_team_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        main_agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        sub_agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        enabled        INTEGER DEFAULT 1,
        created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(main_agent_id, sub_agent_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ux_atm_main ON agent_team_members(main_agent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ux_atm_sub ON agent_team_members(sub_agent_id)")
    conn.commit()


def _migrate_plugin_scope(conn):
    """AI 设计工坊：Skill/MCP/工具 插件模式（scope/source_ref/pinned/install_count）。

    需求：公共插件市场 + 私人空间区分；
    - scope：private（私人空间，默认）| public（公共市场陈列）
    - source_ref：市场来源 "kind:name:version"（安装副本记录，与源解耦）
    - pinned：市场置顶（市场管理后台）
    - install_count：安装计数（每次「安装」时源条目 +1）
    内置项（builtin=1）回填 scope='public' 作为市场种子（平台预置插件）。
    幂等：_add 只补缺失列；回填 UPDATE 重复执行无副作用。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    for t in ("skills", "mcp_servers", "tools"):
        _add(t, "scope", "TEXT DEFAULT 'private'")          # private | public
        _add(t, "source_ref", "TEXT DEFAULT ''")            # 市场来源 "kind:name:version"
        _add(t, "pinned", "INTEGER DEFAULT 0")              # 市场置顶 1=置顶
        _add(t, "install_count", "INTEGER DEFAULT 0")       # 市场安装计数
    # 内置项回填 public（平台预置插件进入市场；幂等）
    conn.execute("UPDATE tools SET scope='public' WHERE builtin=1 AND (scope IS NULL OR scope='private')")
    conn.execute("UPDATE skills SET scope='public' WHERE builtin=1 AND (scope IS NULL OR scope='private')")
    conn.commit()


def _migrate_share_review(conn):
    """个人插件分享审核：skills/mcp_servers 加 share_status + 审批记录表 plugin_review_log。

    - share_status：''（未分享）| submitted（待审核）| approved（已通过入市）| rejected（已驳回）
    - plugin_review_log：submit/approve/reject 审批留痕（操作人/备注/时间）
    幂等：_add 只补缺失列；CREATE TABLE IF NOT EXISTS 重复执行无副作用。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    for t in ("skills", "mcp_servers"):
        _add(t, "share_status", "TEXT DEFAULT ''")   # '' | submitted | approved | rejected
    conn.execute("""CREATE TABLE IF NOT EXISTS plugin_review_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,            -- skill | mcp
        item_id INTEGER NOT NULL,
        item_name TEXT NOT NULL,
        action TEXT NOT NULL,          -- submit | approve | reject
        comment TEXT DEFAULT '',
        operator TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    conn.commit()


def _migrate_plugin_origin(conn):
    """插件来源分类：origin = admin（管理员创建/直接发布）| share（个人分享审核通过）。

    存量 public 非内置条目回填 admin（历史直接发布来源）；share_review approve 时置 share。
    幂等：_add 只补缺失列；回填 UPDATE 重复执行无副作用。
    """
    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    for t in ("skills", "mcp_servers", "tools"):
        _add(t, "origin", "TEXT DEFAULT ''")   # '' | admin | share
    for t in ("skills", "mcp_servers", "tools"):
        conn.execute(f"UPDATE {t} SET origin='admin' WHERE scope='public' AND builtin=0 AND (origin IS NULL OR origin='')")
    conn.commit()


def _migrate_p0_capabilities(conn):
    """P0 能力底座：项目级持久记忆（project_memories）+ 确定性工具钩子（tool_hooks）。

    对齐 Codex durable memory / Claude Code PreToolUse：老库升级时幂等补建两表
    （新库由 schema.init_db 的 CREATE TABLE IF NOT EXISTS 直接建立，此处仅兜底旧库）。
    """
    def _ensure_table(name: str, ddl: str) -> bool:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if exists:
            return False
        conn.execute(ddl)
        print(f"[init_db] 迁移: 新建表 {name}（P0 能力底座）")
        return True

    _ensure_table("project_memories", """
        CREATE TABLE project_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL DEFAULT 'project-satnet-broadband',
            category TEXT DEFAULT '规范',
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_by TEXT DEFAULT '王工',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ux_pm_proj ON project_memories(project_id, category)")
    _ensure_table("tool_hooks", """
        CREATE TABLE tool_hooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            tool_pattern TEXT NOT NULL,
            event TEXT DEFAULT 'pre_tool_use',
            action TEXT DEFAULT 'block',
            condition_args TEXT DEFAULT '',
            message TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hooks_enabled ON tool_hooks(enabled)")
    conn.commit()


def _migrate_file_extract_settings(conn):
    """文件管理实体与关系抽取配置（老库升级幂等补插 settings，无条件执行）。

    需求：文件管理（文档库）上传后实体/关系抽取提供开关设置能力，默认不开启；
    实体候选默认从 SysML 建模数据（AI 建模 SysML 代码提交入库的元素）过来——
    文档抽取以已入库 SysML 模型元素为候选词表做确定性匹配，不降级 LLM 自由抽取；
    SysML 建模数据是进入图库的另一条路径（SYSM- 候选批次），与文件抽取互不冲突。
    幂等：INSERT OR IGNORE，重复执行无副作用。
    """
    for k, v, d in [
        ("file_auto_extract_enabled", "0", "文件管理上传后实体/关系自动抽取开关（0关/1开，默认关）"),
        ("entity_candidate_source", "sysml", "实体候选来源（sysml=AI建模SysML入库数据，默认；llm=LLM自由抽取）"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, description) VALUES (?,?,?)", (k, v, d))
    conn.commit()


def _migrate_docs_global(conn):
    """文件管理全局化：文档从分支体系抽离为全局资产（不随分支变化，向量化数据全局消费）。

    需求：文件管理（文档库）不再按分支隔离——上传统一写全局分支 'global'；
    存量 dev/release 同名发布快照（publish_release 快照复制产物）合并去重，
    保留 id 最小一份（dev 主档）；发布机制不再复制文档快照（实体/关系仍按分支）。
    幂等：已存在 branch='global' 的行则跳过（迁移一次后文档/chunks 全部为 global）。
    """
    # 已迁移判断：存在 global 文档即视为已执行（去重只针对存量非 global 行）
    n_global = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE branch='global'").fetchone()[0]
    if n_global:
        conn.commit()
        return
    # 1) 同名快照去重：按 filename 分组保留 id 最小（dev 主档先上传），其余删除（连带 chunks/元数据）
    dups = conn.execute(
        "SELECT filename FROM documents GROUP BY filename HAVING COUNT(*)>1").fetchall()
    removed = 0
    for r in dups:
        keep = conn.execute(
            "SELECT id FROM documents WHERE filename=? ORDER BY id ASC LIMIT 1",
            (r["filename"],)).fetchone()[0]
        for d in conn.execute(
                "SELECT id FROM documents WHERE filename=? AND id!=?", (r["filename"], keep)).fetchall():
            conn.execute("DELETE FROM document_chunks WHERE document_id=?", (d["id"],))
            conn.execute("DELETE FROM doc_metadata WHERE document_id=?", (d["id"],))
            conn.execute("DELETE FROM documents WHERE id=?", (d["id"],))
            removed += 1
    # 2) 分支统一为 global（documents + document_chunks）
    conn.execute("UPDATE documents SET branch='global'")
    conn.execute("UPDATE document_chunks SET branch='global'")
    print(f"[init_db] 迁移: 文件管理全局化——去重 {removed} 份同名快照, 文档/chunks 分支统一 'global'")
    conn.commit()


def _migrate_plugin_tables(conn):
    """AI 设计工坊 · 统一插件体系（P0）：Plugin = Skill + MCP 统一抽象。

    对齐设计方案 §5.3 数据模型：8 张核心表 + 审核策略设置。
    - plugins          插件主表（manifest_json 为唯一管理契约）
    - plugin_versions  版本与产物（zip 存储，P0 记录版本/清单快照）
    - plugin_reviews   审核记录（P0 三审合并为一审，review_type=combined 可配）
    - plugin_grants    公共插件可见/可用范围（all/team/role）
    - plugin_installs  用户安装与启用状态
    - plugin_credentials 凭证库（与 server URL 解耦，P0 存引用占位，P1 加密）
    - plugin_audit_logs  全量审计（谁、何时、对哪个插件、做了什么、参数快照）
    - plugin_call_logs   调用日志（可观测性）
    与存量 skills/mcp_servers/tools 三表并行（旧体系兼容不动，P1 数据迁移并入）。
    幂等：CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE，重复执行无副作用。
    """
    def _ensure(name, ddl):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if not exists:
            conn.execute(ddl)
            print(f"[init_db] 迁移: 新建表 {name}（统一插件体系 P0）")

    _ensure("plugins", """
        CREATE TABLE plugins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT UNIQUE NOT NULL,           -- 清单 id（反向域名，唯一契约）
            name TEXT NOT NULL,                       -- 小写连字符 ≤64
            namespace TEXT DEFAULT 'personal',        -- 作者命名空间（personal | org 域）
            type TEXT DEFAULT 'bundle',               -- skill | mcp | tool | bundle
                                                      -- 权威枚举见 plugin_system.manifest.PLUGIN_TYPES
            scope TEXT DEFAULT 'personal',            -- personal=个人 | public=公共市场
            status TEXT DEFAULT 'draft',              -- draft/submitted/rejected/published/private/disabled/deprecated/removed
            current_version TEXT DEFAULT '0.0.1',
            manifest_json TEXT DEFAULT '{}',          -- 完整清单（唯一管理契约）
            author_id INTEGER DEFAULT 0,
            author_name TEXT DEFAULT '',
            icon TEXT DEFAULT '',
            category TEXT DEFAULT '',
            description TEXT DEFAULT '',
            source_ref TEXT DEFAULT '',               -- 克隆来源 "plugin_id:version"
            pinned INTEGER DEFAULT 0,                 -- 市场置顶
            install_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    _ensure("plugin_versions", """
        CREATE TABLE plugin_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            version TEXT NOT NULL,
            manifest_json TEXT DEFAULT '{}',          -- 该版本清单快照
            file_path TEXT DEFAULT '',
            checksum TEXT DEFAULT '',
            changelog TEXT DEFAULT '',
            status TEXT DEFAULT 'submitted',          -- submitted/published/deprecated
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(plugin_id, version)
        )""")
    _ensure("plugin_reviews", """
        CREATE TABLE plugin_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            version TEXT NOT NULL,
            reviewer_id INTEGER DEFAULT 0,
            reviewer_name TEXT DEFAULT '',
            action TEXT DEFAULT 'approve',            -- approve | reject
            comment TEXT DEFAULT '',
            review_type TEXT DEFAULT 'combined',      -- tech/security/business/combined（P0 一审可配）
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    _ensure("plugin_grants", """
        CREATE TABLE plugin_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            target_type TEXT DEFAULT 'all',           -- all | team | role
            target_id TEXT DEFAULT '',
            permission TEXT DEFAULT 'use',            -- use | admin
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(plugin_id, target_type, target_id)
        )""")
    _ensure("plugin_installs", """
        CREATE TABLE plugin_installs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 0,
            plugin_id TEXT NOT NULL,
            version TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            config_json TEXT DEFAULT '{}',
            installed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, plugin_id)
        )""")
    _ensure("plugin_credentials", """
        CREATE TABLE plugin_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            key_ref TEXT NOT NULL,                    -- 引用键（如 cred_zy_001），与 base_url 解耦
            ciphertext TEXT DEFAULT '',               -- P0 占位，P1 引入加密
            expires_at TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    _ensure("plugin_audit_logs", """
        CREATE TABLE plugin_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 0,
            user_name TEXT DEFAULT '',
            plugin_id TEXT NOT NULL,
            action TEXT NOT NULL,                     -- create/update/submit/review/install/uninstall/enable/disable/grant/run/delete
            detail_json TEXT DEFAULT '{}',
            ip TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    _ensure("plugin_call_logs", """
        CREATE TABLE plugin_call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id TEXT NOT NULL,
            tool_name TEXT DEFAULT '',
            params_snapshot TEXT DEFAULT '{}',
            status TEXT DEFAULT 'success',            -- success | error
            latency_ms INTEGER DEFAULT 0,
            tokens INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pl_status ON plugins(status, scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pl_author ON plugins(author_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plv_plugin ON plugin_versions(plugin_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plr_plugin ON plugin_reviews(plugin_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plg_plugin ON plugin_grants(plugin_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pli_user ON plugin_installs(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pla_plugin ON plugin_audit_logs(plugin_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plc_plugin ON plugin_call_logs(plugin_id, created_at)")
    # 审核策略设置（D-2：可配置开关，默认强制审核；幂等）
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, description) VALUES (?,?,?)",
        ("plugin_review_policy", "forced",
         "公共插件审核策略（forced=强制审核，默认；auto=免审直发，内部团队可配置）"))
    _migrate_legacy_refs(conn)
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, description) VALUES (?,?,?)",
        ("legacy_migrate", "1",
         "旧三表（skills/mcp_servers）→ 统一插件模型迁移开关（1=启用，0=关闭，同步桥由 source_ref='legacy:' 判定）"))
    conn.commit()


def _migrate_plugin_dependencies(conn):
    """能力依赖索引表（P0-2，2026-09-16）：为卸载前置检查与影响面分析提供数据基础。

    背景：plugins 表的 manifest 只声明了 `capabilities`（提供什么），没有 `dependencies`
    （需要什么），因此无法回答「停用 A 会影响谁」，卸载后残留的工具/路由引用也无从回滚。

    设计（吸收 Cordis 声明式依赖思想，但保持 SQLite 扁平索引以便兼容现有 impact_engine）：
    - consumer_id → provider_id 构成有向图，双向可查
    - provider_ref 保留原样引用串，provider_id 可为空（外部工具/模型等未入库的能力）
    - required 区分硬依赖（阻断停用）与可选降级（仅提示）

    幂等：CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plugin_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consumer_id TEXT NOT NULL,               -- 依赖方 plugin_id
            provider_id TEXT DEFAULT '',             -- 被依赖方 plugin_id（外部能力为空）
            provider_ref TEXT NOT NULL,              -- 原样引用串（capability:xxx / tool:name / model:name）
            kind TEXT NOT NULL,                      -- capability | tool | model | runtime
            required INTEGER DEFAULT 1,              -- 1=硬依赖（阻断停用）0=可选降级（仅提示）
            resolved INTEGER DEFAULT 0,              -- 1=依赖已入库且可用 0=未解析
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(consumer_id, provider_ref)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pldep_provider ON plugin_dependencies(provider_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pldep_consumer ON plugin_dependencies(consumer_id)")
    conn.commit()


def _migrate_triple_optimization(conn):
    """知识治理全链路优化（三元组入库 + V2 候选待审核）。

    目标（对齐行业三元组存储与写入闸门）：
    1. triples 表：以 (subject, predicate, object) 为最小知识单元的原子存储
       —— 替代"实体/关系分离入库 + 关系等实体先落"的耦合模式，消除入库依赖；
    2. v2g_candidates.review_status：AI 生成 V2 代码候选默认进入待审核闸门
       （'' 未提交 / submitted 已提交待审 / approved 已审核放行），提交前不写主图库。

    幂等：建表 IF NOT EXISTS + 列迁移 _add。
    """
    def _add(table, column, ddl):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    c = conn.cursor()
    # 1) 三元组原子存储（S-P-O 统一格式，替代实体/关系分离入库）
    c.execute("""CREATE TABLE IF NOT EXISTS triples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        triple_id TEXT UNIQUE,               -- 唯一键: sub|pred|obj（幂等去重）
        subject_id TEXT DEFAULT '',
        subject_name TEXT DEFAULT '',
        subject_type TEXT DEFAULT '',
        predicate TEXT NOT NULL DEFAULT '',
        object_id TEXT DEFAULT '',           -- 实体型客体（关系三元组）
        object_value TEXT DEFAULT '',        -- 值型客体（属性三元组）
        object_type TEXT DEFAULT 'literal',  -- entity | attribute | literal
        confidence REAL DEFAULT 0.8,
        status TEXT DEFAULT 'pending',       -- pending | reviewed | approved | rejected | deprecated
        review_decision TEXT DEFAULT '',
        review_note TEXT DEFAULT '',
        review_status TEXT DEFAULT '',       -- '' | submitted | approved
        graph_stored INTEGER DEFAULT 0,      -- 是否已写入 entities/relations 构图
        graph_entity_id TEXT DEFAULT '',     -- P0-5 落图回链：主语实体 id
        graph_relation_id TEXT DEFAULT '',   -- P0-5 落图回链：关系 id
        source_doc TEXT DEFAULT '',
        source_chunk TEXT DEFAULT '',
        source_type TEXT DEFAULT 'vector',   -- vector | ai_generated
        sysml_version_id INTEGER DEFAULT 0,  -- AI 建模来源 V2 版本（溯源）
        created_by TEXT DEFAULT '',
        reviewed_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TEXT DEFAULT ''
    )""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_triples_triple_id ON triples(triple_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_triples_status ON triples(status)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_triples_subject ON triples(subject_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_triples_object ON triples(object_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_triples_spo ON triples(subject_id, predicate, object_id)")
    # 2) V2 候选待审核闸门列
    _add("v2g_candidates", "review_status", "TEXT DEFAULT ''")
    _add("v2g_candidates", "review_submitted_at", "TEXT DEFAULT ''")
    conn.commit()


def _migrate_legacy_refs(conn):
    """P1：旧表增加 plugin_id 引用列（幂等），支撑 legacy 迁移与同步桥。

    设计（见 P1 迁移方案，策略 B 管理同步）：
    - 旧表保持为「运行时事实源」（pipeline.py 等 12+ 处直读，零改动）
    - plugins 表为「管理事实源」，source_ref='legacy:<table>:<id>' 溯源
    - 同步桥：统一插件页启停/上架/删除 → 单向写回旧表 enabled/scope/status
    """
    for t in ("skills", "mcp_servers"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        if not exists:
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if "plugin_id" not in cols:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN plugin_id TEXT DEFAULT ''")
            print(f"[migrate] 迁移: {t} 增加列 plugin_id（legacy 插件引用）")


def _migrate_mr_comments(conn):
    """MR 评审意见留痕表：approve/reject/评论全量时间线（审计可追溯，对标 GitHub PR conversation）。幂等建表。"""
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS mr_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mr_id INTEGER NOT NULL,                -- 关联 merge_requests.id
        author TEXT DEFAULT '',                -- 评审人/评论人
        action TEXT DEFAULT 'comment',         -- approve | reject | comment | rollback | reopen
        comment TEXT DEFAULT '',               -- 意见正文
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mr_comments_mr ON mr_comments(mr_id)")
    conn.commit()





def _migrate_project_ingest_logs(conn):
    """工程维度入库批次记录表（工程归档 → 三元组 → 个人分支图库）。

    每次工程入库一行：统计信息（stats_json）供回执卡与「工程入库历史」面板共用。
    幂等建表。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS project_ingest_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,                -- 工程级批次号 PINGEST-*
        project_id TEXT NOT NULL,              -- 工程 id（projects.project_id）
        project_name TEXT DEFAULT '',          -- 工程名快照
        target_branch TEXT DEFAULT 'personal', -- 目标分支（当前固定 personal）
        version_ids TEXT DEFAULT '[]',         -- 入库版本 id 列表（json）
        stats_json TEXT DEFAULT '{}',          -- 统计信息（json，见设计文档 §4）
        status TEXT DEFAULT 'running',         -- running | success | partial | failed
        error_msg TEXT DEFAULT '',
        operator TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pil_project ON project_ingest_logs(project_id, id)")
    conn.commit()


def _migrate_glossary_changelog(conn):
    """词典概念变更留痕表（P0 治理闭环，对标 TBX 变更控制 / PoolParty 历史管理）。

    每次概念创建/更新/状态流转/合并/术语增删写一行；detail 为人读摘要。
    幂等建表。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS glossary_changelog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        concept_id TEXT NOT NULL,
        action TEXT NOT NULL,                  -- add | update | flow | merge | terms | batch
        detail TEXT DEFAULT '',                -- 人读变更摘要
        reason TEXT DEFAULT '',                -- 变更理由（approved 概念修改必填）
        operator TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gcl_cid ON glossary_changelog(concept_id, id)")
    conn.commit()


def _migrate_glossary_discoveries(conn):
    """AI 建议流·发现池表（P2-10，对标 PoolParty Taxonomy Advisor）。

    归一校验"未命中"的实体名自动登记（幂等 upsert，同词频次+1）；
    人工触发 AI 预填（suggestion_json），采纳后建概念走审批看板。
    生命周期：discovered（待处理）→ adopted（已采纳建概念）/ dismissed（已忽略）。
    幂等建表。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS glossary_discoveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term TEXT NOT NULL UNIQUE,             -- 候选词（唯一键）
        context TEXT DEFAULT '',               -- 最近出现上下文
        source_ref TEXT DEFAULT '',            -- 来源（消息id/工程/版本）
        freq INTEGER DEFAULT 1,                -- 出现频次（重复发现+1）
        status TEXT DEFAULT 'discovered',      -- discovered | adopted | dismissed
        suggestion_json TEXT DEFAULT '',       -- AI 预填结果（json，可编辑后采纳）
        dismiss_reason TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gd_status ON glossary_discoveries(status, freq DESC)")
    conn.commit()


def _migrate_document_lifecycle(conn):
    """文件生命周期管理 + 节点/边元数据补齐（资料库与AI建模优化设计方案-20260910 P0）。

    1) documents 增 7 列：lifecycle_status / deprecated_* / archived_* / lifecycle_version
    2) document_chunks 增 1 列：lifecycle_status（向量层同步状态，避免废弃文档仍命中检索）
    3) 新建 document_lifecycle_log 审计表（FR-KG-11 软删除审计要求）
    4) 索引：按 lifecycle_status 过滤 / 文档时间线查询
    5) 历史回填：parse_status='completed' → 'stored'（待入库，committed 需人工切换）；parsing → 'processing'；其他 → 'uploaded'
    6) document_chunks.lifecycle_status 与所属 documents.lifecycle_status 同步
    幂等：_add 只补缺失列；CREATE TABLE IF NOT EXISTS 重复执行无副作用。
    """
    c = conn.cursor()

    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    # 1) documents 增列（7 列）
    _add("documents", "lifecycle_status", "TEXT DEFAULT 'uploaded'")
    _add("documents", "deprecated_at", "TEXT DEFAULT ''")
    _add("documents", "deprecated_by", "TEXT DEFAULT ''")
    _add("documents", "deprecate_reason", "TEXT DEFAULT ''")
    _add("documents", "archived_at", "TEXT DEFAULT ''")
    _add("documents", "archived_by", "TEXT DEFAULT ''")
    _add("documents", "lifecycle_version", "INTEGER DEFAULT 1")

    # 2) document_chunks 增列（向量层同步状态）
    _add("document_chunks", "lifecycle_status", "TEXT DEFAULT 'stored'")

    # 3) 新建文档生命周期审计表（FR-KG-11：已废弃数据保留审计日志）
    c.execute("""CREATE TABLE IF NOT EXISTS document_lifecycle_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL,
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        operator TEXT NOT NULL,
        reason TEXT DEFAULT '',
        extra TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_dll_doc ON document_lifecycle_log(document_id, id DESC)")

    # 4) 索引（按状态过滤性能）
    c.execute("CREATE INDEX IF NOT EXISTS idx_doc_lifecycle ON documents(lifecycle_status, branch)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_lifecycle ON document_chunks(lifecycle_status, document_id)")

    # 5) 历史回填（幂等）
    # 2026-09-10 米爸澄清：入库（committed）= 内容已进图库，需人工切换；
    # 自动回填只到 stored（向量就绪/待入库）。存量已 committed 的不回退（视为已确认入库）。
    c.execute("UPDATE documents SET lifecycle_status='stored' "
              "WHERE parse_status='completed' AND (lifecycle_status IS NULL OR lifecycle_status='uploaded')")
    c.execute("UPDATE documents SET lifecycle_status='processing' "
              "WHERE parse_status='parsing' AND (lifecycle_status IS NULL OR lifecycle_status='uploaded')")
    conn.commit()
    print("[init_db] 迁移: 文件生命周期状态机 + 审计表 document_lifecycle_log 已建立（向后兼容）")

def _migrate_entity_temporal(conn):
    """P0-④（2026-09-11）时态管理：双时态列 + 索引 + 视图 + seed 回填。

    列：valid_from / valid_to / is_current / tx_from / tx_to
    - valid_from / valid_to: 业务时间（实体在业务意义上何时存在/失效）
    - tx_from / tx_to: 事务时间（数据行何时入/退库）
    - is_current: 当前版本标记（1=当前，0=历史版本）

    索引：
    - idx_ent_valid_current: 当前版本快速查询
    - idx_ent_valid_from: 时间起点
    - idx_ent_valid_range: 区间查询

    视图：
    - v_current_entities: 当前版本（is_current=1）
    - v_temporal_entities: 时态有效（valid_to IS NULL OR valid_to > now）

    W3C Time Ontology 对齐表：
    - entity_time_intervals: 业务时态间隔（Owlready2/Protégé 互操作）

    兼容性：所有列与索引使用 IF NOT EXISTS 模式，幂等可重入。
    """
    # 1) 双时态列
    columns_to_add = [
        ("valid_from", "ALTER TABLE entities ADD COLUMN valid_from TEXT DEFAULT NULL"),
        ("valid_to",   "ALTER TABLE entities ADD COLUMN valid_to TEXT DEFAULT NULL"),
        ("is_current", "ALTER TABLE entities ADD COLUMN is_current INTEGER DEFAULT 1"),
        ("tx_from",    "ALTER TABLE entities ADD COLUMN tx_from TEXT DEFAULT NULL"),
        ("tx_to",      "ALTER TABLE entities ADD COLUMN tx_to TEXT DEFAULT NULL"),
    ]
    for col_name, alter_sql in columns_to_add:
        try:
            conn.execute(alter_sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                pass  # 已存在，幂等
            else:
                raise

    # 2) 性能索引
    indexes = [
        ("idx_ent_valid_current", "CREATE INDEX IF NOT EXISTS idx_ent_valid_current ON entities(is_current, valid_to)"),
        ("idx_ent_valid_from",    "CREATE INDEX IF NOT EXISTS idx_ent_valid_from ON entities(valid_from)"),
        ("idx_ent_valid_range",   "CREATE INDEX IF NOT EXISTS idx_ent_valid_range ON entities(valid_from, valid_to)"),
    ]
    for idx_name, sql in indexes:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            print(f"[migrate_entity_temporal] {idx_name} 失败: {e}")

    # 3) 时态视图
    views = [
        ("v_current_entities",
         "CREATE VIEW IF NOT EXISTS v_current_entities AS "
         "SELECT * FROM entities WHERE is_current = 1"),
        ("v_temporal_entities",
         "CREATE VIEW IF NOT EXISTS v_temporal_entities AS "
         "SELECT * FROM entities WHERE valid_to IS NULL OR valid_to > CURRENT_TIMESTAMP"),
    ]
    for v_name, sql in views:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            print(f"[migrate_entity_temporal] {v_name} 失败: {e}")

    # 4) W3C Time Ontology 间隔对齐表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_time_intervals (
            entity_id TEXT NOT NULL,
            branch TEXT NOT NULL,
            time_instant_iri TEXT NOT NULL,
            valid_from_xsd TEXT,
            valid_to_xsd TEXT,
            PRIMARY KEY (entity_id, branch, time_instant_iri),
            FOREIGN KEY (entity_id, branch) REFERENCES entities(id, branch)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eti_instant ON entity_time_intervals(time_instant_iri)")

    # 5) Seed 回填：把历史数据的 valid_from / tx_from 设为 created_at，is_current 设为 1
    #    SQLite ALTER 不可用 CURRENT_TIMESTAMP 默认值（须非常量），故初始为 NULL；
    #    此处用本地时间（ISO8601）回填，确保 as_of 查询可比较。
    try:
        from datetime import datetime as _dt
        now_iso = _dt.now().isoformat(sep=' ', timespec='seconds')
        conn.execute("""
            UPDATE entities
            SET valid_from = COALESCE(valid_from, created_at, ?),
                tx_from    = COALESCE(tx_from,    created_at, ?),
                is_current = COALESCE(is_current, 1)
            WHERE valid_from IS NULL OR tx_from IS NULL OR is_current IS NULL
        """, (now_iso, now_iso))
    except sqlite3.OperationalError as e:
        print(f"[migrate_entity_temporal] seed 回填失败（可重入）: {e}")

    conn.commit()

def _migrate_swrl_tables(conn):
    """P1-①（2026-09-11）SWRL 规则管理表：swrl_rules + inferred_facts。

    swrl_rules：
    - name: 规则名（业务可读）
    - body / head: SWRL Manchester 语法主体/头
    - is_active: 是否启用
    - last_executed_at / last_inferred_count: 推理监控

    inferred_facts:
    - 推理产出事实暂存表（is_accepted=0 不入主图）
    - fact_type: rdf:type / ObjectPropertyAssertion / DataPropertyAssertion
    - confidence: 推理置信度（SWRL 暂为 1.0；后续可扩展为 0~1）
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swrl_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            comment TEXT,
            body TEXT NOT NULL,
            head TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_executed_at TEXT,
            last_inferred_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inferred_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER REFERENCES swrl_rules(id) ON DELETE CASCADE,
            fact_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            inferred_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_accepted INTEGER DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_swrl_active ON swrl_rules(is_active)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inferred_pending ON inferred_facts(is_accepted)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inferred_rule ON inferred_facts(rule_id)")
    conn.commit()


def _migrate_reasoning_cohorts(conn):
    """P2 推理物化→审核门禁：批次表 + 批次条目表。

    reasoning_cohorts（物化批次）：
    - status: pending（待审核）| approved（已确认并入）| rejected（已驳回）
    - stat: JSON 快照—approve 后回填 {entity_count, relation_count,
      entities:[{id,name,entity_type}], relations:[{source,predicate,target,
      source_id,target_id,relation_type}]}
    - note: 驳回原因（驳回时必填 ≥2 字）；decided_by/decided_at 审核人/时间

    reasoning_cohort_items（批次条目，物化时落一条）：
    - item_kind: entity（类型声明）| relation（关系推断）
    - s/p/o: 主语 id / 谓词 / 宾语（id 或类型名）
    - inferred_json: 归一化推断快照（含名称/推理类型/依据）

    与三张推理暂存表（triples.status='inferred' / inferred_facts /
    swrl_rules）解耦：批次是审核门禁的编排单位，approve 时才据此幂等落
    entities/relations 主图。幂等建表。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS reasoning_cohorts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
        note TEXT DEFAULT '',                      -- 驳回原因
        stat TEXT DEFAULT '{}',                    -- JSON：approve 后并入明细快照
        created_by TEXT DEFAULT '',
        decided_by TEXT DEFAULT '',
        decided_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS reasoning_cohort_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cohort_id INTEGER NOT NULL REFERENCES reasoning_cohorts(id) ON DELETE CASCADE,
        item_kind TEXT NOT NULL,                   -- entity | relation
        s TEXT DEFAULT '',                         -- 主语 id
        p TEXT DEFAULT '',                         -- 谓词（relation_type / type）
        o TEXT DEFAULT '',                         -- 宾语（关系 id 或类型名）
        inferred_json TEXT DEFAULT '{}'            -- 归一化推断快照
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rci_cohort ON reasoning_cohort_items(cohort_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rc_status ON reasoning_cohorts(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rc_created ON reasoning_cohorts(created_at)")
    # 2026-09-14 单条审核：条目级状态（pending|approved|rejected）+ 处理备注；
    # 老库幂等补列（列已存在时忽略），批次 approve 时只并入未 rejected 的条目
    for _stmt in (
        "ALTER TABLE reasoning_cohort_items ADD COLUMN status TEXT DEFAULT 'pending'",
        "ALTER TABLE reasoning_cohort_items ADD COLUMN note TEXT DEFAULT ''",
    ):
        try:
            c.execute(_stmt)
        except Exception:
            pass  # 列已存在
    conn.commit()


def _migrate_ontology_instance_migrations(conn):
    """2026-09-14 本体变更 → 实例迁移计划表（见 docs/本体变更实例影响分析与自动迁移方案.md）。

    - plan_id: 一次发布（或手动补迁）生成的迁移计划批次号（同批多 op）
    - op_type: migrate_instances（按名批量替换）| deprecate_instances（删除类型 → 实例弃用）
      | drop_prop_key（清理实例 properties 死键）| convert_prop_values（属性值类型转换）
      | flag_violations（约束收紧 → 违例清单，只出清单不改数据）| flag_dangling（历史悬空实例扫描）
    - payload: op 参数 JSON（{from,to,type_kind,branch,...}）
    - status: pending（待 dry-run/确认）| dry_run（已预演）| applied（已执行）| failed | dismissed（已忽略）
    - 幂等：UNIQUE(plan_id, change_log_id, op_type) 防同一留痕生成重复 op；所有执行 SQL 可重入
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_instance_migrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        change_log_id INTEGER DEFAULT 0,        -- 关联 ontology_change_logs.id（悬空扫描=0）
        version_id INTEGER DEFAULT 0,           -- 触发发布的本体版本（手动补迁=0）
        op_type TEXT NOT NULL,
        target TEXT DEFAULT '',                 -- 操作对象（类型名）
        payload TEXT DEFAULT '{}',              -- op 参数 JSON
        affected INTEGER DEFAULT 0,             -- dry-run 预计行数
        sample TEXT DEFAULT '[]',               -- dry-run 抽样（≤10 条实例摘要）
        status TEXT DEFAULT 'pending',          -- pending | dry_run | applied | failed | dismissed
        note TEXT DEFAULT '',                   -- 执行结果/失败原因
        branch TEXT DEFAULT '*',                -- 执行分支范围（*=全分支）
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        applied_at TEXT DEFAULT '',
        applied_by TEXT DEFAULT '',
        UNIQUE(plan_id, change_log_id, op_type)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_oim_plan ON ontology_instance_migrations(plan_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_oim_status ON ontology_instance_migrations(status)")
    # 实例类型列索引：L1 内联迁移（rename 批量 UPDATE）与悬空扫描走列索引
    c.execute("CREATE INDEX IF NOT EXISTS idx_entities_entity_type ON entities(entity_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_relations_relation_type ON relations(relation_type)")
    conn.commit()


def _migrate_artifact_ingest(conn):
    """2026-09-15 AI 产物收编资料库（见 docs/AI产物收编资料库方案.md）。幂等补列。

    - documents.origin / source_artifact_id：来源溯源（upload | ai_generated + 产物 id）
    - document_chunks.origin：块级冗余（检索过滤免 JOIN，命中结果可标注「AI 生成」）
    - doc_metadata.superseded_by：时效取代链（同产物再收编 → 旧文档标记被取代）
    """
    c = conn.cursor()
    for stmt in (
        "ALTER TABLE documents ADD COLUMN origin TEXT DEFAULT 'upload'",
        "ALTER TABLE documents ADD COLUMN source_artifact_id INTEGER DEFAULT 0",
        "ALTER TABLE document_chunks ADD COLUMN origin TEXT DEFAULT 'upload'",
        "ALTER TABLE doc_metadata ADD COLUMN superseded_by INTEGER DEFAULT 0",
    ):
        try:
            c.execute(stmt)
        except Exception:
            pass  # 列已存在
    # 存量空值归一（旧库行为兼容：NULL 视为 upload）
    c.execute("UPDATE documents SET origin='upload' WHERE origin IS NULL")
    c.execute("UPDATE document_chunks SET origin='upload' WHERE origin IS NULL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_documents_origin ON documents(origin)")
    conn.commit()

