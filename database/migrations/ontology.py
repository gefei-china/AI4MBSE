"""本体/语义层迁移（类型、变更表、版本表、去重、IRI、时态、SWRL、推理队列、实例迁移）。"""
import sqlite3

def _ensure_ontology_types(conn):
    """O-3：老库幂等补齐本体类型（_seed 仅在空库执行，老库需增量补 O-3 新增类型）。"""
    c = conn.cursor()
    for name, kind in [("需求", "entity"), ("部件", "entity"), ("功能", "entity"),
                        ("SATISFIES", "relation"), ("连接", "relation"), ("执行", "relation")]:
        c.execute("INSERT OR IGNORE INTO ontology_types (name, type_kind) VALUES (?,?)", (name, kind))
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

    # 6) P0-1（2026-09-23）影子历史表 entity_versions
    #    此前「双时态八环」断在最后一环：主键 PRIMARY KEY (id, branch) 只允许每个键一行，
    #    使 update_with_history 的 INSERT 必然 UNIQUE 冲突 → 该函数成为死代码（0 调用），
    #    /history 恒 1 行、/at 对任意历史时刻命中当前行、时态 SPARQL 同步空转。
    #    本表把「历史行」独立存储，entities 语义不变（= 仅当前行）→ 320 处引用零改动。
    #    回滚：DROP TABLE entity_versions + revert 写入路径改动（主表未变，行为退回原地 UPDATE）。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_versions (
            id TEXT NOT NULL,
            branch TEXT NOT NULL,
            version_no INTEGER NOT NULL,        -- 该 (id,branch) 内递增序号（1,2,3…）
            valid_from TEXT NOT NULL,           -- 本版本生效时刻（业务时间）
            valid_to TEXT DEFAULT NULL,         -- 本版本失效时刻（NULL = 当前版本）
            is_current INTEGER DEFAULT 0,       -- 1=当前行镜像，0=历史
            tx_from TEXT DEFAULT '',            -- 事务时间：入版本表时刻
            tx_to TEXT DEFAULT NULL,
            change_kind TEXT DEFAULT 'update',  -- init|create|update|fork|review|category|merge
            changed_by TEXT DEFAULT '',
            name TEXT DEFAULT '',
            entity_type TEXT DEFAULT '',
            properties TEXT DEFAULT '{}',
            status TEXT DEFAULT 'candidate',
            project_id TEXT DEFAULT '',
            source_doc TEXT DEFAULT '',
            source_type TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            created_by TEXT DEFAULT '',
            reviewed_by TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            reviewed_at TEXT DEFAULT '',
            graph_source TEXT DEFAULT '',
            graph_x REAL DEFAULT 0,
            graph_y REAL DEFAULT 0,
            sysml_import_id TEXT DEFAULT '',
            knowledge_category TEXT DEFAULT '',
            published_at TEXT DEFAULT '',
            sysml_version_id INTEGER DEFAULT 0,
            canonical_id TEXT DEFAULT '',
            PRIMARY KEY (id, branch, version_no),
            UNIQUE (id, branch, valid_from)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ev_current ON entity_versions(id, branch, is_current)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ev_range ON entity_versions(id, branch, valid_from, valid_to)")

    # 初始化灌数（不可省）：现有实体各灌一条初始版本 v1，否则 /at 对「当前时刻」会走
    # fallback、语义不一致。幂等：仅当该 (id,branch) 在版本表中无任何行时插入。
    # 快照列按「entity_versions ∩ entities」动态取交集 —— 防迁移顺序差异或未来加列导致
    # no such column（本仓迁移顺序与列演进历史上多次踩到）。
    try:
        from datetime import datetime as _dt_ev
        ev_now = _dt_ev.now().isoformat(sep=' ', timespec='seconds')
        ev_cols = [r[1] for r in conn.execute("PRAGMA table_info(entity_versions)").fetchall()]
        e_cols = [r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()]
        ev_meta = ("version_no", "valid_from", "valid_to", "is_current",
                   "tx_from", "tx_to", "change_kind", "changed_by")
        ev_payload = [c for c in ev_cols if c in set(e_cols) and c not in ev_meta]
        ev_ins_cols = ["version_no", "valid_from", "valid_to", "is_current",
                       "tx_from", "tx_to", "change_kind", "changed_by"] + ev_payload
        ev_ins_sql = ("INSERT INTO entity_versions (%s) VALUES (%s)"
                      % (", ".join(ev_ins_cols), ", ".join("?" for _ in ev_ins_cols)))
        pending = conn.execute(
            "SELECT * FROM entities e WHERE NOT EXISTS ("
            "SELECT 1 FROM entity_versions v WHERE v.id = e.id AND v.branch = e.branch)"
        ).fetchall()
        ev_n = 0
        for r in pending:
            d = dict(zip(e_cols, tuple(r)))
            ev_vals = [1,
                       (d.get("valid_from") or d.get("created_at") or ev_now), None, 1,
                       (d.get("tx_from") or d.get("created_at") or ev_now), None,
                       "init", d.get("created_by") or ""]
            ev_vals += [d.get(c) for c in ev_payload]
            conn.execute(ev_ins_sql, tuple(ev_vals))
            ev_n += 1
        if ev_n:
            print(f"[init_db] 迁移: entity_versions 初始化灌入 {ev_n} 条初始版本")
    except sqlite3.OperationalError as e:
        print(f"[migrate_entity_temporal] entity_versions 初始化失败（可重入）: {e}")

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
