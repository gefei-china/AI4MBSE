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

    # ══════════════════════════════════════════════════════════════════════
    # ★ 2026-11-15 补：概念层两张表此前**产品代码里根本没有建表语句**
    #
    # 取证：全仓 `CREATE TABLE ... glossary_concepts|glossary_terms` 只在
    #   `tools/verify/verify_glossary_prefix_adjacency.py` 的**门禁自建夹具**里出现过；
    #   本迁移函数与其它任何产品代码都不建。本地真库之所以有这两张表，是历史遗留
    #   （早期某次外部脚本/手工建的，建表语句未随代码提交）。
    #
    # 后果（在任何**新部署**上，含 CI 与客户现场）：
    #   `GlossaryMatcher._load_concepts()` 查询失败 → `except` 兜底成 `[]`
    #   ⇒ 概念层（P0-3 术语召回、R1 意图/boost 汇总、maps_to_* 图谱召回）
    #     **永久为空且不报错**。这不是"数据没填"，是**表都不存在**。
    #   实测：只用本迁移建内存库，`_load_concepts()` 返回 0 条、
    #   `_load()` 也返回 0 条（glossary 表虽建了但无种子）。
    #   CI 上 `verify_glossary_prefix_adjacency` 报 `no such table: glossary_terms`
    #   正是这条缺陷的外部症状。
    #
    # 口径：一次建全列（而不是照抄真库里被 ALTER 滚出来的列序），
    #   再对老库做幂等补列（`PRAGMA table_info` 探测），两条路都安全。
    # ══════════════════════════════════════════════════════════════════════
    c.execute("""CREATE TABLE IF NOT EXISTS glossary_concepts (
        concept_id     TEXT PRIMARY KEY,
        pref_label     TEXT NOT NULL,
        definition     TEXT NOT NULL DEFAULT '',
        domain         TEXT DEFAULT 'unknown',
        concept_status TEXT DEFAULT 'candidate',   -- candidate|approved|deprecated|retired
        maps_to_class  TEXT DEFAULT '',            -- 本体映射：类
        maps_to_prop   TEXT DEFAULT '',            -- 本体映射：属性
        maps_to_inst   TEXT DEFAULT '',            -- 本体映射：实例
        replaced_by    TEXT DEFAULT '',            -- deprecated 时的替代概念
        version        INTEGER DEFAULT 1,
        created_by     TEXT DEFAULT 'migration',
        created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at     TEXT DEFAULT CURRENT_TIMESTAMP,
        intent         TEXT DEFAULT '',            -- R1：命中即路由到该 Agent
        boost          REAL DEFAULT 1.0,           -- R1：检索加权
        context        TEXT DEFAULT '',
        note           TEXT DEFAULT '',
        source         TEXT DEFAULT '',
        broader        TEXT DEFAULT '',            -- SKOS 上位
        related        TEXT DEFAULT ''             -- SKOS 关联
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gc_status ON glossary_concepts(concept_status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gc_domain ON glossary_concepts(domain)")

    c.execute("""CREATE TABLE IF NOT EXISTS glossary_terms (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        concept_id      TEXT NOT NULL REFERENCES glossary_concepts(concept_id),
        term            TEXT NOT NULL,
        lang            TEXT DEFAULT 'zh',
        term_kind       TEXT DEFAULT 'synonym',    -- pref|synonym|abbr|variant
        term_status     TEXT DEFAULT 'admitted',   -- admitted|deprecated|proposed
        source          TEXT DEFAULT '',
        reliability     INTEGER DEFAULT 5,
        created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
        note            TEXT DEFAULT '',
        part_of_speech  TEXT DEFAULT '',
        geography       TEXT DEFAULT '',
        -- NULL 有含义：未声明（沿用默认参与匹配）；0 才是显式排除
        use_in_matching INTEGER,
        UNIQUE(term, lang, concept_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gt_term ON glossary_terms(term)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gt_concept ON glossary_terms(concept_id)")

    # 老库补列（幂等）：真库里的这两张表是被 ALTER 逐列滚出来的，
    # 列序与上面的新建口径不同；只补"缺的那几列"，不动既有列与数据。
    for _tab, _cols in (
        ("glossary_concepts", (
            ("intent", "TEXT DEFAULT ''"), ("boost", "REAL DEFAULT 1.0"),
            ("context", "TEXT DEFAULT ''"), ("note", "TEXT DEFAULT ''"),
            ("source", "TEXT DEFAULT ''"), ("broader", "TEXT DEFAULT ''"),
            ("related", "TEXT DEFAULT ''"),
        )),
        ("glossary_terms", (
            ("note", "TEXT DEFAULT ''"), ("part_of_speech", "TEXT DEFAULT ''"),
            ("geography", "TEXT DEFAULT ''"), ("use_in_matching", "INTEGER"),
        )),
    ):
        _have = [r[1] for r in c.execute("PRAGMA table_info(%s)" % _tab).fetchall()]
        for _col, _decl in _cols:
            if _col not in _have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (_tab, _col, _decl))

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
        -- ⚠️ 2026-11-08 更正注释（原写"原始用户输入"，与实现不符）：
        --   `query` 存的是**实际送进检索的查询串** —— 调用方在 `user_input` 后
        --   拼了 kb_hint / 槽位实体 / 附件前 600 字，`agent/rag.py` 取到后又用
        --   Glossary 归一化覆盖了一次。所以它既不是用户原话，也不是模型的输入原文。
        --   用户原话另存 `query_raw`（见下），二者**不可互相替代**：
        --     query_raw —— 用户这一轮真正输入的文字（标注/样本复核要看这个）
        --     normalized —— query 经 Glossary 替换后的规范形（检索侧口径）
        query TEXT NOT NULL,
        normalized TEXT DEFAULT '',            -- 归一化后（Glossary 替换）
        -- ★ 2026-11-08：用户原话。**NULL 有确切含义**：该行写入时还没有这一列
        --   （2026-11-08 之前的历史行），原话**不可恢复**——历史行的 `query`
        --   早已被归一化与上下文增强污染，没有任何字段能还原用户输入。
        --   故：NULL = 未记录（不是空串、不是"用户没说话"）；
        --       新行由 `glossary.trace_query` 统一写值，它把「键缺失 / None / 空白」
        --       都回退成本次检索串 —— 否则"调用方漏传"会伪装成"历史行"，
        --       两种不同原因又挤进同一个取值。
        query_raw TEXT,
        intent TEXT DEFAULT '',                -- 路由意图
        route TEXT DEFAULT '',                 -- graph | vector | mixed
        domain TEXT DEFAULT '',                -- 检索域
        hit_docs TEXT DEFAULT '[]',            -- JSON: 命中文档名列表
        hit_count INTEGER DEFAULT 0,
        top_score REAL DEFAULT 0,
        -- ★ 2026-11-08：分数**来源**与其三路原值。
        --   `top_score` 原义保留（兼容既有消费方），但它是"按 route 择一"的结果，
        --   不同 route 的量纲完全不同（图置信度 0~1，融合检索分实测 0~0.63），
        --   **不可跨 route 比较**。故新增：
        --   score_kind —— 这个数是哪种分：graph_conf / chunk_score / vec_score / ''
        --                 （'' = 本次没有可用的分数，**不是** 0 分）
        --   top_* 三列 —— 三路各自的首条分数，**都记不择一**（择一即信息丢失）
        --   ⚠️ top_* 允许 NULL 且**刻意不给 DEFAULT 0**：
        --      NULL = 该路本次无结果，0 = 有结果但分数为 0。二者必须可区分，
        --      否则"没取到"会被当成"最差"，正是旧实现把 mixed 路由写成 0 的形态。
        score_kind TEXT DEFAULT '',
        top_graph REAL,
        top_chunk REAL,
        top_vec REAL,
        latency_ms INTEGER DEFAULT 0,
        detail TEXT DEFAULT '{}',              -- JSON: 各环节详情（归一化/置信度/召回原因）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    # 旧库补列：`CREATE TABLE IF NOT EXISTS` 对已存在的表是**空操作**，不会新增列。
    # 不补的话，上面的 INSERT 会在存量 6530 行的老库上报 "no such column: score_kind"，
    # 而该异常又会被 trace 的兜底吞掉 ⇒ 表现为"trace 从某次升级起整体消失"。
    _trace_cols = [r[1] for r in c.execute("PRAGMA table_info(query_trace)").fetchall()]
    for _col, _decl in (("score_kind", "TEXT DEFAULT ''"),
                        ("top_graph", "REAL"),
                        ("top_chunk", "REAL"),
                        ("top_vec", "REAL"),
                        # ★ 2026-11-08：用户原话。**刻意不给 DEFAULT** ——
                        #   老库存量行必须是 NULL（= 当时没记），给 '' 会让
                        #   "没记录"和"记了个空串"混为一谈，而消费方无法区分。
                        #   注：ALTER 只能追加到表尾，故升级库的物理列序与
                        #   全新建库（query_raw 紧跟 normalized）不同；SELECT *
                        #   的字段顺序因此有差异，但读取一律按列名，无影响。
                        ("query_raw", "TEXT")):
        if _col not in _trace_cols:
            c.execute("ALTER TABLE query_trace ADD COLUMN %s %s" % (_col, _decl))
    c.execute("CREATE INDEX IF NOT EXISTS idx_trace_time ON query_trace(created_at)")
    # 低置信采样按 (kind, score) 分组取分位（见 core/online_eval._cand_low_conf），
    # 复合索引让"组内 ORDER BY score ASC"能走索引而不是全表扫。
    c.execute("CREATE INDEX IF NOT EXISTS idx_trace_kind_score "
              "ON query_trace(score_kind, top_score)")
    # ── 历史行分数来源回填（2026-11-08，幂等）───────────────────────────────
    # 为什么要回填：新消费方（core/online_eval 低置信采样）按 `score_kind` 分组，
    # 若历史 6541 行全是 `''`，采样源会瞬间"无数据"——从"统计不全"变成"统计为零"，
    # 看起来像业务量消失。回填让新旧行口径连续。
    #
    # 三条规则，全部由**已存的 route/top_score 反推**，不猜、不补数：
    #   ① route='vector' 且 top>0 —— 融合检索分（`_hit_src` 取 chunk_hits，
    #      无 chunk 时取 vector_results）。历史行**无法区分**这两者，
    #      故记为 `legacy_fusion` 而不是冒充 `chunk_score`：同量级，但来源不确定。
    #   ② route='mixed' 且 top=0 —— 是**旧 bug 的产物**（图路取不到元素上的
    #      score/conf 字段），0 不是"最低分"而是"没取到"⇒ 记 `legacy_zero`，
    #      明确排除在低置信判断之外。若不排除，这 27 行会永远排在采样榜首。
    #   ③ 其余 top=0 —— 零命中，同样记 `legacy_zero`（没有结果 ≠ 结果很差）。
    c.execute("UPDATE query_trace SET score_kind='legacy_fusion' "
              "WHERE COALESCE(score_kind,'')='' AND route='vector' AND COALESCE(top_score,0)>0")
    c.execute("UPDATE query_trace SET score_kind='legacy_zero' "
              "WHERE COALESCE(score_kind,'')='' AND COALESCE(top_score,0)=0")
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


def _migrate_glossary_canonical_align(conn) -> dict:
    """R2（2026-11-11）：把 `C-语法规范` 概念的规范词与首选术语对齐。

    ## 修的是什么
    `语法规范` / `代码规范` 的 canonical 曾是 `SysML_V2 语法规范`（概念 pref_label
    自带限定前缀）。它与 `SysML V2 → SysML_V2` 在同一句里相邻命中时，两个
    canonical 各带一次 `SysML_V2`，拼接后出现 `SysML_V2 SysML_V2 语法规范`。
    这不是替换逻辑的错——两条词条各自都对——而是**规范名把消歧前缀写进了值里**。

    ## 为什么改数据而不是改代码（R2 的取舍）
    R1 那种「拼接时省略重复前缀」是启发式，得为每种 canonical 形状写例外；
    R2 直接让 canonical 与 preferred term 等值，归一化变成 no-op，前缀不复存在。
    真库核查：这两个 term 各自只属于一个概念（无同形异义），故不存在消歧损失。

    ## 幂等与边界
    - 只在**精确等于旧值**时 UPDATE：用户后来手工改过的规范名不会被本迁移冲掉；
    - `concept_id` 保持不变（本仓概念改名不改 ID）；
    - 旧 `glossary` 表同款两行一并改，否则前端术语列表仍显示旧串；
    - 写一条 `glossary_changelog` 留痕（按 concept_id+action+detail 去重）；
    - 表不存在时**明确记入 skipped 并返回**，不静默吞异常（夹具/干净库会走这里）。
    """
    out = {"concept_updated": 0, "legacy_updated": 0, "changelog": 0,
           "skipped": [], "before": {}, "after": {}}
    c = conn.cursor()
    exists = lambda t: c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None

    old = "SysML_V2 语法规范"
    new = "语法规范"
    cid = "C-语法规范"

    if not exists("glossary_concepts"):
        out["skipped"].append("glossary_concepts 不存在")
    else:
        row = c.execute(
            "SELECT pref_label, version FROM glossary_concepts WHERE concept_id=?", (cid,)
        ).fetchone()
        if row is not None:
            out["before"] = {"pref_label": row[0], "version": row[1]}
            if row[0] == old:
                c.execute("UPDATE glossary_concepts SET pref_label=?, version=COALESCE(version,0)+1 "
                          "WHERE concept_id=? AND pref_label=?", (new, cid, old))
                out["concept_updated"] = c.rowcount
        r2 = c.execute("SELECT pref_label, version FROM glossary_concepts WHERE concept_id=?",
                       (cid,)).fetchone()
        if r2:
            out["after"] = {"pref_label": r2[0], "version": r2[1]}

    if not exists("glossary"):
        out["skipped"].append("glossary 不存在")
    else:
        cur = c.execute(
            "UPDATE glossary SET canonical_term=? "
            "WHERE user_term IN ('语法规范','代码规范') AND canonical_term=?", (new, old))
        out["legacy_updated"] = cur.rowcount

    if out["concept_updated"] or out["legacy_updated"]:
        if not exists("glossary_changelog"):
            out["skipped"].append("glossary_changelog 不存在，未留痕")
        else:
            import json as _json
            detail = _json.dumps({"field": "pref_label", "old": old, "new": new,
                                  "reason": "R2 消除相邻 canonical 的重复限定前缀",
                                  "legacy_updated": out["legacy_updated"]},
                                 ensure_ascii=False, sort_keys=True)
            got = c.execute(
                "SELECT 1 FROM glossary_changelog WHERE concept_id=? AND action='rename' "
                "AND detail=?", (cid, detail)).fetchone()
            if not got:
                c.execute(
                    "INSERT INTO glossary_changelog "
                    "(concept_id, action, detail, reason, operator, created_at) "
                    "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (cid, "rename", detail, "R2 规范名对齐", "migration"))
                out["changelog"] = 1
    conn.commit()
    return out
