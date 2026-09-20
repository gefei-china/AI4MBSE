"""表结构重建/外键修复/分支重命名（entities PK、relations/documents FK、release/dev 分支）。"""
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
        project_id TEXT DEFAULT '',
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
        project_id TEXT DEFAULT '',
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
