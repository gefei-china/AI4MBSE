"""词表与种子数据（术语表、变更日志、发现项、部门、知识分类）。"""
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

def _migrate_domain_review_queue_orphans(conn):
    """清理 `domain_review_queue` 中指向已删文档的孤儿行。幂等，每次 init_db 扫一遍。

    根因：`domain_review_queue.document_id` 是**无外键**的普通列（见上方建表语句），
    删 `documents` 时不级联。历史删除路径（2026-09-14/15 删 doc 761–778、
    2026-09-19 删 792/796/797）曾静默留下孤儿行，而 `dashboard_repo` 会把它们
    计入看板「知识评审待办」→ 计数长期虚高（2026-09-19 实测 14 条孤儿 / 显示 52 真实 38）。

    预防侧已在 `repositories/meta_repo.py::MetaRepo.delete_document` 显式清理本表；
    本迁移作为兜底，覆盖其它删除路径（分支删除/回滚外的场景）与历史残留。

    注意 `document_id` 允许 NULL（列定义只有 DEFAULT 0，无 NOT NULL），
    NULL NOT IN (...) 结果为 NULL 不会命中，故显式带上 `IS NULL`。
    """
    n = conn.execute(
        "DELETE FROM domain_review_queue "
        "WHERE document_id IS NULL OR document_id NOT IN (SELECT id FROM documents)"
    ).rowcount
    if n:
        print(f"[init_db] 迁移: 清理 domain_review_queue 孤儿行 {n} 条（指向已删文档）")
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
