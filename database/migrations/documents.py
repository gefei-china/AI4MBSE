"""文档/产物链路迁移（staging、SysML、profile、入库日志、抽取设置、全局文档、生命周期、产物收编）。"""
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
