"""图谱与向量层迁移（pipeline 明细回填、graph/v2g 表、来源信息回填、三元组优化）。"""
import json

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
