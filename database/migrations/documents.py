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

    ⚠️ 2026-09-21（G5）**去重范围必须收窄到「跨分支的同名发布快照」**：
    本迁移把「同名」定义为**重复、应当删除**；而版本链（方案 D3）把「同目录同名」定义为
    **新版本、应当保留**。两者语义相反。原实现 `GROUP BY filename HAVING COUNT(*)>1`
    只要同名就删，一旦在已有版本链的库上执行（`n_global == 0` 的全新部署 / 恢复后重跑），
    会**物理删除版本历史**（连带 chunks 与 doc_metadata）。

    ⚠️ **为什么不能只加 `branch != 'global'`**（这点必须先说清，否则后人会以为加了就安全）：
    本函数上面有早退 `if n_global: return` —— 能走到去重时，库里**必然 0 行 global**。
    所以 `WHERE branch != 'global'` 会匹配全部行，**恒等于没有条件（死代码）**。
    真正收窄语义的是分组条件：只有**同一文件名横跨多个分支**才是"发布快照对"
    （dev 主档 + publish_release 复制出来的 release 快照）——这正是 docstring 声明的本意。
    同分支内的同名行**不是发布快照**，是版本链或真实重复，**一律不动**。

    ⚠️ 与版本链的相容性声明（版本链落地时的 P2 前置检查项）：本迁移**与版本链不相容**。
    版本链若采纳 D3（历史版 = documents 行 + `version_no`/`root_document_id`），
    任何"按 filename 去重"的清理都必须显式排除"同属一条链"的行（按 `root_document_id` 判定），
    不可再用 filename 作去重键 —— 本函数已按此收窄，但**仍不是版本链安全的**（见下）。
    """
    # 已迁移判断：存在 global 文档即视为已执行（去重只针对存量非 global 行）
    n_global = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE branch='global'").fetchone()[0]
    if n_global:
        conn.commit()
        return
    # 1) 同名快照去重：**仅限「同名且跨分支」**（= 真发布快照对），按 filename 分组保留 id 最小
    #    （dev 主档先上传），其余删除（连带 chunks/元数据）。
    #    COUNT(DISTINCT branch)>1 是判断"是否发布快照对"的充要条件：
    #    单分支内的多条同名行不是快照复制产物，删了就是删用户数据（G5）。
    dups = conn.execute(
        "SELECT filename FROM documents WHERE branch != 'global' "
        "GROUP BY filename HAVING COUNT(*)>1 AND COUNT(DISTINCT branch)>1").fetchall()
    removed = 0
    for r in dups:
        keep = conn.execute(
            "SELECT id FROM documents WHERE filename=? AND branch != 'global' ORDER BY id ASC LIMIT 1",
            (r["filename"],)).fetchone()[0]
        for d in conn.execute(
                "SELECT id FROM documents WHERE filename=? AND branch != 'global' AND id!=?",
                (r["filename"], keep)).fetchall():
            conn.execute("DELETE FROM document_chunks WHERE document_id=?", (d["id"],))
            conn.execute("DELETE FROM doc_metadata WHERE document_id=?", (d["id"],))
            conn.execute("DELETE FROM documents WHERE id=?", (d["id"],))
            removed += 1
    # 2) 分支统一为 global（documents + document_chunks）
    conn.execute("UPDATE documents SET branch='global'")
    conn.execute("UPDATE document_chunks SET branch='global'")
    print(f"[init_db] 迁移: 文件管理全局化——去重 {removed} 份同名快照, 文档/chunks 分支统一 'global'")
    conn.commit()


def _migrate_doc_folders(conn):
    """文档目录树 + 文档挂目录（2026-09-21，方案 §4.1 唯一 DDL）。

    需求：文档库「基于文件的管理」——目录树左栏（人工结构）+ 文档归属目录；
    文档仍是**全局资产**，不按分支切分（故本表**不带 branch 列**，唯一约束也不含 branch）。

    设计要点（每条都对应一个已实测的坑）：
    1) **`parent_id` 用 0 表示根，不用 NULL**（D1）。SQLite 的 UNIQUE 把 NULL 视为互不相等，
       于是 `UNIQUE(parent_id, name)` 在 `parent_id IS NULL` 时**完全失效** ——
       隔离库实测：根级连插两个 ('规范') 两次都成功，同名根目录可以有无限个。
       哨兵 0 写法则被正确拦截（同一实测：第二次抛 IntegrityError）。
    2) 仍保留 `ux_doc_folders_name_parent` 表达式唯一索引作**第二道闸**：
       万一有人把 parent_id 写成 NULL（历史数据 / 手写 SQL），表达式索引仍能拦住同名
       —— 因为 `IFNULL(NULL,0)` 把两个 NULL 归一成同一个 0。
    3) `path` 是物化路径（'/规范/热管理/'），供「含子目录」前缀查询与面包屑；
       重命名/移动时由仓储层级联刷新（不由 DB 触发器维护）。
    4) `sort` 为同级手排；`domain` 是**目录级默认域**，上传时预填，不参与检索过滤（决策点 B4=A）。
    5) `documents.folder_id` 同款哨兵：**0 = 未归类**（不是 NULL）——与 parent_id 一致，
       避免再引入一处"NULL 语义"造成的过滤遗漏（`folder_id=0` 的等值查询是可靠的）。
    6) 向后兼容：老库 documents 全部落 0 = 未归类，既有的列表/检索/统计行为完全不变。

    幂等：CREATE TABLE IF NOT EXISTS + 表达式索引 IF NOT EXISTS + _add 只补缺失列。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS doc_folders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        parent_id   INTEGER NOT NULL DEFAULT 0,     -- 0 = 根（D1：不用 NULL，否则 UNIQUE 失效）
        path        TEXT NOT NULL DEFAULT '/',      -- 物化路径，供「含子目录」前缀查询与面包屑
        sort        INTEGER DEFAULT 0,
        domain      TEXT DEFAULT '',                -- 目录级默认域（上传预填；不参与检索过滤）
        created_by  TEXT DEFAULT '',
        created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(parent_id, name)                     -- ⚠️ 仅因 parent_id NOT NULL 才有效（见 1）
    )""")
    # 防御性第二道闸：即便 parent_id 被写成 NULL，同名仍不允许（见 2）
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_doc_folders_name_parent "
              "ON doc_folders(name, IFNULL(parent_id, 0))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_doc_folders_parent ON doc_folders(parent_id, sort)")

    def _add(table: str, column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: {table} 增加列 {column}")

    # 文档挂目录（0 = 未归类）——列级增量，向后兼容
    _add("documents", "folder_id", "INTEGER NOT NULL DEFAULT 0")
    c.execute("CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder_id)")
    conn.commit()
    print("[init_db] 迁移: 文档目录树 doc_folders + documents.folder_id（0=未归类）已建立")

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
