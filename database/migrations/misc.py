"""杂项迁移（MR 状态/评论、环境变量 key、评测表、知识库 v2 增强、提交基线回填）。"""
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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        eval_kind TEXT DEFAULT '',                -- 2026-10-22：评测类型 rag_retrieval|memory_quality|…
        doc_recall_at_5 REAL DEFAULT 0,            -- doc 域主指标（此前只在 JSON 里）
        doc_mrr_at_10 REAL DEFAULT 0,
        doc_ndcg_at_10 REAL DEFAULT 0,
        doc_pool_recall REAL DEFAULT 0,
        doc_rerank_drop_rate REAL DEFAULT 0,
        doc_recall_miss_rate REAL DEFAULT 0,
        gen_citation_valid_rate REAL,              -- 2026-10-22 断链③：生成质量主指标（NULL=未测，区别于 0 分）
        gen_citation_usage_rate REAL,
        gen_grounded_entity_rate REAL,
        gen_no_citation_factual_rate REAL,
        gen_faithful_rate REAL,                    -- 2026-10-22 晚 断链③语义层：LLM 忠实率（未启用评判时 NULL）
        trace_agreement_rate REAL,                -- 2026-10-22 断链②：Trace 裁决一致率（NULL=未测）
        trace_repeat_mistake_rate REAL            -- 2026-10-22 断链②：Trace 重错率（无路由纠错类负样本时为 NULL）
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_eval_reports_doc ON eval_reports(doc_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_eval_reports_time ON eval_reports(created_at)")
    # ── 2026-10-23（评估 O3 / P0-3）：指标快照表 ─────────────────────
    # 背景：指标写死成 eval_reports 的固定列（doc_*/gen_*/trace_*），新增一个
    #   指标实测要改 8 个文件 14 处；漏改前端 = 静默不显示（无报错）。
    # 语义：一行 = 一个指标快照，三色 status **落库时由后端算好**（前端只渲染，
    #   纪律 A8 不破）。固定列保留（历史兼容 + 主指标快查），快照表是扩展通道：
    #   新增指标只需在评测器侧写入一行，repo/前端自动透出，零迁移。
    c.execute("""CREATE TABLE IF NOT EXISTS eval_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER NOT NULL,            -- → eval_reports.id
        eval_kind TEXT DEFAULT '',
        metric_key TEXT NOT NULL,              -- 与固定列同名（如 gen_faithful_rate）或新键
        value REAL,                            -- NULL=本轮未测（区别于 0 分）
        direction TEXT DEFAULT 'high',         -- high=越大越好 | low=越小越好
        gate_key TEXT DEFAULT '',              -- → core.config.eval_gates 键（空=不门禁）
        threshold REAL,                        -- 落库时的阈值快照（阈值后来改了不影响历史）
        status TEXT DEFAULT '',                -- ok|alert|warn|''（后端算好）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_eval_metrics_report ON eval_metrics(report_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_eval_metrics_kind ON eval_metrics(eval_kind, metric_key)")
    # 历史行回填（幂等）：已有报告若还没有任何快照行，从固定列补写。
    # 三色沿用 metric_status_for（repo 层同一份实现，避免迁移里另写判定）。
    try:
        from repositories.eval_report_repo import write_metric_rows
        have = {r[0] for r in c.execute(
            "SELECT DISTINCT report_id FROM eval_metrics").fetchall()}
        rows = c.execute(
            "SELECT id, eval_kind, doc_recall_at_5, doc_mrr_at_10, doc_ndcg_at_10, "
            "doc_pool_recall, doc_rerank_drop_rate, doc_recall_miss_rate, "
            "gen_citation_valid_rate, gen_citation_usage_rate, "
            "gen_grounded_entity_rate, gen_no_citation_factual_rate, gen_faithful_rate, "
            "trace_agreement_rate, trace_repeat_mistake_rate, entity_f1 "
            "FROM eval_reports ORDER BY id").fetchall()
        n_back = 0
        for r in rows:
            rid, kind = r[0], r[1] or ""
            if rid in have or not kind:
                continue
            vals = {}
            if kind == "rag_retrieval":
                vals = dict(zip(("doc_recall_at_5", "doc_mrr_at_10", "doc_ndcg_at_10",
                                 "doc_pool_recall", "doc_rerank_drop_rate",
                                 "doc_recall_miss_rate"), r[2:8]))
            elif kind == "generation_quality":
                vals = dict(zip(("gen_citation_valid_rate", "gen_citation_usage_rate",
                                 "gen_grounded_entity_rate",
                                 "gen_no_citation_factual_rate", "gen_faithful_rate"),
                                r[8:13]))
            elif kind == "trace_consistency":
                vals = dict(zip(("trace_agreement_rate", "trace_repeat_mistake_rate"),
                                r[13:15]))
            elif kind == "memory_quality":
                vals = {"entity_f1": r[15]}
            n_back += write_metric_rows(c, rid, kind, vals)
        if n_back:
            print(f"[init_db] eval_metrics 历史回填: {n_back} 行（固定列 → 快照表）")
    except Exception as _e:
        print(f"[init_db] eval_metrics 历史回填跳过: {_e}")
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
    # ── 2026-10-22（评估 P0-5）：eval_kind —— 评测**类型**列 ──────────
    # 背景：`eval_reports` 此前**没有任何类型列**，而两类完全不同的评测往同一张表写：
    #   RAG 检索评测（entity_recall/relation_recall 有真实语义）
    #   记忆质量评测（把同一个 rate **写进 6 个 P/R/F1 字段**）
    # ⇒ 同一张表里entity_f1=1.0 既可能是"检索全对"，也可能是"记忆 15/15"，
    #   **只能靠 created_by 字段名反推**（而这正是脚本自己警告过的脆弱口径）。
    # 语义：rag_retrieval | memory_quality | intent_routing | doc_extract | ''
    # 旧行为：默认 ''（不回填历史行—— 历史行的类型不可靠地推断出来，
    #   猜错比空着更坏；需要区分时按 created_by 查）。
    _add("eval_reports", "eval_kind", "TEXT DEFAULT ''")
    # doc 域检索主指标（recall@5 / mrr / ndcg / pool_recall / rerank_drop_rate /
    # recall_miss_rate）—— 此前**只存在于 JSON 文件里**，库里查不到。
    # 评估指出：doc 域是检索质量的主指标，却因表只有 entity/relation 两列而无法
    # 落库 ⇒ 看板与趋势分析只能靠 docs/评测/ 下的单份 JSON 快照。
    _add("eval_reports", "doc_recall_at_5", "REAL DEFAULT 0")
    _add("eval_reports", "doc_mrr_at_10", "REAL DEFAULT 0")
    _add("eval_reports", "doc_ndcg_at_10", "REAL DEFAULT 0")
    _add("eval_reports", "doc_pool_recall", "REAL DEFAULT 0")
    _add("eval_reports", "doc_rerank_drop_rate", "REAL DEFAULT 0")
    _add("eval_reports", "doc_recall_miss_rate", "REAL DEFAULT 0")
    # ── 2026-10-22（断链③）：生成质量主指标列 ────────────────
    # 背景：faithfulness/groundedness 全仓零命中 —— 系统评测检索/路由/记忆，
    #   唯独不评测生成质量。gen_* 四列让生成质量报告进同一张表、同一套
    #   历史/趋势/看板（见 tools/eval/eval_generation_quality.py）。
    # ⚠ DEFAULT NULL 而非 0：doc_* 列的 DEFAULT 0 是历史包袱（"没测到"会被
    #   看板渲染成 0 分 = 假绿），新列不继承这个坑。
    _add("eval_reports", "gen_citation_valid_rate", "REAL")
    _add("eval_reports", "gen_citation_usage_rate", "REAL")
    _add("eval_reports", "gen_grounded_entity_rate", "REAL")
    _add("eval_reports", "gen_no_citation_factual_rate", "REAL")
    # ── 2026-10-22 晚（断链③语义层）：LLM 忠实率列 ────────────────
    # 背景：确定性指标回答不了"答案有没有曲解证据"。gen_faithful_rate
    #   让 LLM 评判结果进同一张表、同一套趋势/看板。
    # ⚠ DEFAULT NULL：未启用 --llm-judge 时**没有这个数**，填 0 会被
    #   读成"忠实率 0 分"（假红），与 gen_* 既有列语义一致。
    _add("eval_reports", "gen_faithful_rate", "REAL")
    # ── 2026-10-22（断链②）：Trace 一致性主指标列 ────────────────
    # 背景：trace2dataset 产出的生产裁决样本（intent_confirmed/
    #   intent_rejected）此前无消费方 —— 供料环通了、度量环没接。
    #   trace_* 两列让"当前路由 ↔ 人工裁决"的一致率/重错率进同一张
    #   表、同一套历史/趋势/看板（见 tools/eval/eval_trace_consistency.py）。
    # ⚠ DEFAULT NULL 而非 0：重错率在"无路由纠错类负样本"时**没有分母**，
    #   填 0 会被读成"一条旧错都没再犯"（那是假绿，与"没测到"是两件事）。
    _add("eval_reports", "trace_agreement_rate", "REAL")
    _add("eval_reports", "trace_repeat_mistake_rate", "REAL")
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

def _migrate_view_layout_checks(conn):
    """P0-2 视图布局优化（SRS-GN-MG-BJYH）：布局质量检查存档表。幂等建表。

    - 每行 = 一次「视图投影 + 布局」的质量证据：前端布局引擎算出的
      交叉数 / 总边长 / 归一化评分，随校验结果一并落库
    - 验收用途：SRS 布局优化用例要求「布局合理性检查结果可导出」，
      本表即证据来源（按 view_type+branch 查历史、看趋势）
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS view_layout_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        view_type TEXT NOT NULL,               -- BDD/IBD/REQ/UC/ACT/SEQ/STM/PAR/PKG/TRACE
        branch TEXT DEFAULT '',                -- 投影分支（''=release 默认）
        node_count INTEGER DEFAULT 0,
        edge_count INTEGER DEFAULT 0,
        missing_kinds TEXT DEFAULT '',         -- 最小输入集缺失（逗号分隔；空=覆盖完整）
        crossings INTEGER DEFAULT 0,           -- 布局边交叉数
        edge_length_sum REAL DEFAULT 0,        -- 布局总边长（px）
        score REAL DEFAULT 0,                  -- 归一化质量分 0~1（1=无交叉且覆盖完整）
        detail TEXT DEFAULT '{}',              -- JSON 预留（平均边长/交叉率等）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vlc_type ON view_layout_checks(view_type, branch)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vlc_time ON view_layout_checks(created_at)")
    conn.commit()


def _migrate_data_sources(conn):
    """数据源注册表（P0-4 2026-09-20 重建；R1=B 曾于 2026-09-01 随数据集成移除删除）。

    type: file | db | api
    config: JSON 连接配置（凭据不落库，db/api 引 env 变量名，运行时解析）
      file → {"path": "data/uploads/xxx.md"}
      db   → {"connection": "sqlite:///D:/path/x.db", "table": "t", "sql": "SELECT ...", "limit": 200}
      api  → {"url": "https://...", "method": "GET", "headers": {...}, "token_env": "XXX_TOKEN",
              "data_key": "items", "limit": 200}
    last_status: 最近一次连通性测试结果（通过/失败: 原因）
    documents.source_id 列自 R1=B 起保留，本表重建后恢复外键语义（应用层维护）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS data_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL,                    -- file | db | api
        config TEXT NOT NULL DEFAULT '{}',     -- JSON 连接配置
        enabled INTEGER DEFAULT 1,
        last_status TEXT DEFAULT '',           -- 最近连通性/抽取状态
        last_test_at TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ds_type ON data_sources(type)")
    conn.commit()


def _migrate_intent_samples(conn):
    """意图识别**样本池**表（2026-09-26）：把"意图评测集"从硬编码脚本搬进库，可在设置页维护。

    ## 为什么要它
    意图路由的阈值标定全靠 `tests/manual_verify/eval_intent_routing.py` 里**硬编码 29 例**：
    F1 分辨率只有 1/29，且线上真实说法（如"帮我看看这个系统大概是怎么设计的"）根本进不来。
    标定结论（th=0.64）的可用窗口就是被其中两句夹出来的 —— 样本一换即漂。
    样本池 + 人工确认把评测集变成**可增长、可审计**的事实源。

    ## 为什么必须有 status（本仓既有哲学）
    `tools/eval/build_evalset.py` 写明：弱标注（系统推导出的标签）**必须人工复核后才能用于关键调参**。
    意图样本同理 —— 拿系统自己的判定当标签去评测自己，是自我循环（永远 100%）。
    故：自动采集只写 'suggested'（含系统当时的判定 hit_intent/hit_route），
    **评测只消费 status='confirmed'**，两者物理隔离。

    ## 字段
    - text        ：用户原话（UNIQUE：同一句话只留一行，seen_count 记频次 → 高频优先标注）
    - intent      ：人工确认的标签（''=未标注；status='suggested' 时存放"建议标签"= 系统判定，仅供参考）
    - status      ：new（仅采集未建议）| suggested（系统已给建议，待确认）| confirmed（人工确认，进评测）| rejected（确认不是有效样本，不进评测）
    - source      ：seed（内置 29 例导入）| prod（线上实时采集）| prod_import（历史回填）| manual（页面手工新增）
    - hit_intent/hit_route/hit_conf：采集当时的系统判定（用于"标注后对比系统行为"、回归复盘）
    幂等：建表 IF NOT EXISTS + 索引；不塞种子数据（种子走 API 导入，便于在页面里看到并改）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS intent_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL UNIQUE,
        intent TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        source TEXT DEFAULT 'manual',
        hit_intent TEXT DEFAULT '',
        hit_route TEXT DEFAULT '',
        hit_conf REAL DEFAULT 0,
        seen_count INTEGER DEFAULT 1,
        note TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_intent_samples_status ON intent_samples(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_intent_samples_intent ON intent_samples(intent)")
    conn.commit()
    print("[init_db] 迁移: 意图样本池表 intent_samples 已建立（向后兼容）")


def _migrate_dashboard_snapshots(conn):
    """知识看板指标快照表（P2，2026-09-26）。幂等建表。

    ## 为什么需要它
    看板上的"值 + 目标 + 状态"是**当下**快照，无法回答"在变好还是变坏"。
    sparkline 趋势必须落在真实历史点上，因此按天存一份指标快照。

    ## 去重语义
    `UNIQUE(metric_key, snapshot_date, branch)` + `ON CONFLICT DO UPDATE`：
    同一天多次刷新只覆盖当天那一行，不会把 sparkline 画出锯齿。

    ⚠️ 只写"有值"的指标（`value IS NOT NULL`）—— 未落地指标不落快照，
    避免把 None 当 0 画出假趋势（写入逻辑见 `metrics_core.save_snapshot`）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS dashboard_metric_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_key TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        branch TEXT DEFAULT '',
        value REAL,
        unit TEXT DEFAULT '',
        status TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(metric_key, snapshot_date, branch)
    )""")
    # 索引创建须在建表之后（老库补列/补表的既有教训）
    c.execute("CREATE INDEX IF NOT EXISTS idx_dash_snap_key_date "
              "ON dashboard_metric_snapshots(metric_key, snapshot_date)")
    conn.commit()


def _migrate_drop_agent_tools_params(conn):
    """移除 agent_tools.params 废列（2026-09-30，用户四轮反馈 1）。

    ## 为什么删
    `params` 本意是"该 Agent 调用此工具时的默认入参"，但：
    - 全仓**只有一处写入**（`repositories/agent_repo.py:add_tool` 恒写 `{}`），**零处读取**；
    - UI 从不传该字段（`30-agents.js` 绑定请求体无 params）；
    - 真库实测 **61/61 行全部为 `{}`**（100% 空）。
    属挂名死列：留着只会让人误以为存在"工具默认参数"能力。

    ⚠️ **边界：删的是 `params` 列，不是绑定关系本身** —— `agent_tools` 的行
    （agent_id × tool_type × tool_name）是真正在用的：`prompt.py` 用它判定 V2 代码 Agent，
    `agent/pipeline_parts/tools.py` 用它把 DB 自定义工具注入 LLM 可见列表。本迁移不删行。

    ## SQLite 版本前提
    `ALTER TABLE ... DROP COLUMN` 需要 SQLite ≥ 3.35.0（本机 3.53.1 ✓）。
    旧版本降级为"保留列"（不再写入即等同废弃），不报错、不阻断 init_db。
    """
    c = conn.cursor()
    cols = [r["name"] for r in c.execute("PRAGMA table_info(agent_tools)").fetchall()]
    if not cols:            # 表还不存在（新库由 schema.py 创建，已无该列）
        return
    if "params" not in cols:
        return              # 已删除 → 幂等
    try:
        ver = tuple(int(x) for x in (conn.execute("SELECT sqlite_version()").fetchone()[0] or "").split(".")[:3])
    except Exception:
        ver = (0, 0, 0)
    if ver < (3, 35, 0):
        print(f"[init_db] 迁移: agent_tools.params 保留（SQLite {'.'.join(map(str, ver))} < 3.35 不支持 DROP COLUMN）")
        return
    try:
        c.execute("ALTER TABLE agent_tools DROP COLUMN params")
        conn.commit()
        print("[init_db] 迁移: agent_tools.params 废列已移除（全仓零消费点，绑定关系保持不变）")
    except Exception as e:
        conn.rollback()
        print(f"[init_db] 迁移: agent_tools.params 移除失败（沿用旧结构）: {e}")

def _migrate_tool_result_offloads(conn):
    """P1-4（2026-10-02）工具结果 offload 表：大工具响应全文落库，prompt 内只放引用块。

    Tier1 offload（评估报告 §2.1 P1）：>3000 字符的模型侧工具结果不再"截断即丢失"——
    全文入本表，模型侧注入引用块（头部摘要 + offload id），可调 tool_result_fetch 按 id
    重读全文（可寻址召回，非破坏性）。幂等：CREATE TABLE IF NOT EXISTS。
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS tool_result_offloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER DEFAULT 0,
        tool_name TEXT DEFAULT '',
        content TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tro_conv ON tool_result_offloads(conversation_id)")
    print("[init_db] 迁移: 工具结果 offload 表 tool_result_offloads 已就绪（幂等）")


def _migrate_audit_chain(conn):
    """P0-a/P0-b（2026-10-03）审计溯源列 + 哈希链：《运行监控与审计日志差距评估》P0。

    P0-a：补 user_agent / request_id 列（ip_address 列已存在但 audit() 从不写入 —— 本批随
      middleware 绑定 ContextVar 后自动落盘）。
    P0-b：补 prev_hash / hash 列，并把历史行接成链（首行 prev_hash = 64 个 0）。

    幂等与安全边界：
    - 列用 PRAGMA table_info 判定后 ALTER（表不存在则跳过，由 schema 建表带全列）
    - **仅当全表尚无任何入链行时才回填历史**（正常就是首次启动的场景）；若已存在入链行
      则该表已在正常服役，回填需级联重算后续每一行的摘要，风险大于收益 —— 不回填，
      打印提示，未入链行由 GET /api/audit/verify 持续暴露为 unchained。
    """
    def _add(column: str, ddl: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_logs'").fetchone()
        if not exists:
            return
        cols = [r[1] for r in conn.execute("PRAGMA table_info(audit_logs)").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN `{column}` {ddl}")
            print(f"[init_db] 迁移: audit_logs 增加列 {column}")

    for col, ddl in (("user_agent", "TEXT DEFAULT ''"),
                     ("request_id", "TEXT DEFAULT ''"),
                     ("prev_hash", "TEXT DEFAULT ''"),
                     ("hash", "TEXT DEFAULT ''")):
        _add(col, ddl)

    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN COALESCE(hash,'')<>'' THEN 1 ELSE 0 END) chained "
            "FROM audit_logs").fetchone()
    except Exception as e:
        print(f"[init_db] 迁移: audit 链检查跳过（表不可用）: {e}")
        return
    total, chained = int(row["n"] or 0), int(row["chained"] or 0)
    if chained:
        if total > chained:
            print(f"[init_db] 迁移: audit 链已服役（{chained}/{total} 已入链），"
                  f"剩余 {total - chained} 行不回填（级联风险），由 /api/audit/verify 暴露")
        return
    if not total:
        return

    # 延迟导入：本函数由 database.schema.init_db 调用，模块层 import core.audit 会与
    # database 包形成循环导入（core.audit → database.db_conn）。
    from core.audit import _row_hash, _GENESIS
    rows = conn.execute(
        "SELECT id, user_name, event_type, detail, result, branch, ip_address, "
        "user_agent, request_id FROM audit_logs ORDER BY id").fetchall()
    prev = _GENESIS
    for r in rows:
        h = _row_hash(prev, r["user_name"], r["event_type"], r["detail"], r["result"],
                      r["branch"] or "", r["ip_address"] or "", r["user_agent"] or "",
                      r["request_id"] or "")
        conn.execute("UPDATE audit_logs SET prev_hash=?, hash=? WHERE id=?", (prev, h, r["id"]))
        prev = h
    print(f"[init_db] 迁移: audit_logs 哈希链已回填 {len(rows)} 行（P0-b 防篡改基线）")


# ════════════════════════════════════════════════════════════════════════
# 2026-10-13：`conflict_check` 下线（假实现退役）
# ════════════════════════════════════════════════════════════════════════
def _retire_fake_conflict_check(conn):
    """把生产库里那条 `conflict_check` 工具**置为 disabled**（不删行）。

    为什么是"下线"而不是"删行"：
      · **不删行** —— `tool_call_logs` 里有 17 条历史调用留痕指向它，删掉 tools 行会让那些
        流水变成"引用了不存在的工具"，排障时反而更难看；
      · **置 disabled** —— 与本仓既有退役口径一致（`migrations/plugins.py:41` 对
        deprecated/inactive 就是这么处理的），且 `_build_tools_def` 的 DB 工具查询带
        `AND t.status='active'` ⇒ 下线后**自动不再注入**任何 Agent，无需改代码。

    ⚠️ 这里**不动 `agents.tools` / `agent_tools`**：那两处已在本 commit 里从代码侧移除
      （registry.py 与 seeds.py）；生产库的 `agent_tools` 实测**零绑定**，无需清理。

    幂等：连跑多次只会把已 disabled 的行再确认一次，无副作用。
    """
    try:
        cur = conn.execute(
            "UPDATE tools SET status='disabled' WHERE name='conflict_check' AND status!='disabled'")
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if n:
            print(f"[init_db] 迁移: conflict_check 工具已下线（{n} 行 → disabled；"
                  f"假实现，2026-10-13 移除）")
        # ⚠️⚠️ **必须同时下线那个插件，否则下线无效**（2026-10-13 实测踩到）：
        #   `plugins` 表里有一条 `com.zhiyuan.legacy.tool.conflict-check`（status=published、
        #   `tool_entry_from_plugin()` 返回 `executable: True`），而 `_build_tools_def` 的
        #   **P1-6「安装即可消费」路径只按插件状态过滤、不查 `tools.status`**
        #   ⇒ 只把 tools.status 置 disabled 时，该工具**仍会被注入 payload**（实测确认）。
        #   同一个工具名有**两条独立的进入路径**，少处理一条就等于没下线。
        cur2 = conn.execute(
            "UPDATE plugins SET status='disabled' "
            "WHERE type='tool' AND (id='com.zhiyuan.legacy.tool.conflict-check' "
            "OR manifest_json LIKE '%conflict_check%') AND status!='disabled'")
        n2 = cur2.rowcount if cur2.rowcount and cur2.rowcount > 0 else 0
        if n2:
            print(f"[init_db] 迁移: conflict-check 插件已下线（{n2} 行 → disabled；"
                  f"否则「安装即可消费」路径会把它重新注入）")
        conn.commit()
    except Exception:
        pass


def _migrate_config_proposals(conn):
    """P1-3（2026-10-18 自进化调研路线图）：配置提案队列表。

    ## 为什么需要它
    RefineGate 只评审「内容」；P1-2 归因器聚出首坏步模式后，没有任何通路把
    模式变成「该改什么」的建议。本表是 GEPA 式反思改写的**落点**：规则从
    失败轨迹提议 prompt/skill/配置修订，**只提议不自动应用**——自动改生产
    配置是 misevolution 的直接通道。

    ## 去重/状态语义
    - fingerprint UNIQUE：sha1(rule|section|key|proposed_value)。同一提案不
      重复入队；现场配置变化 ⇒ proposed 变 ⇒ 指纹变 ⇒ 可重新提议（闭环不卡死）。
    - status 三态：pending → applied | rejected（终态不重开，与 #18 状态机纪律
      一致）。applied/rejected 只记录**人的裁决**；应用是人在设置页改配置，
      本链路没有任何写配置的路径。
    - 幂等：IF NOT EXISTS + 索引；不塞种子数据（提案只能从真实失败轨迹生成）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS config_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule TEXT NOT NULL,
        target_section TEXT NOT NULL,
        target_key TEXT DEFAULT '',
        current_value TEXT DEFAULT '',
        proposed_value TEXT DEFAULT '',
        rationale TEXT DEFAULT '',
        evidence_json TEXT DEFAULT '{}',
        fingerprint TEXT NOT NULL UNIQUE,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        reviewed_by TEXT DEFAULT '',
        reviewed_at TEXT,
        review_note TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_config_proposals_status "
              "ON config_proposals(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_config_proposals_rule "
              "ON config_proposals(rule)")
    conn.commit()
    print("[init_db] 迁移: 配置提案队列表 config_proposals 已建立（P1-3，只提议不自动应用）")


def _migrate_sandbox_p14(conn):
    """P1-4（2026-10-19 自进化调研路线图）：沙箱 A/B + 配置晋升两张表。

    ## sandbox_runs —— 一次配置影子评估 = 一行
    提案（P1-3）想改某个配置键 ⇒ 先在这里跑一次双臂评估：对照组（活配置）
    vs 实验组（影子配置，只活在被评估的门禁子进程里）各跑 CI 门禁集，
    差分判回归。status: queued → running → done | failed；verdict:
    pass | regression | error（error=评估环境坏，结论无效）。
    summary_json 存逐门禁明细（按严重度排序、max_report_gates 截断）。

    ## config_history —— 配置变更台账（晋升与回滚的唯一落点）
    每次「经 HIL 批准的真实配置变更」记一行：旧值→新值、来源
    （proposal 晋升 / rollback 回滚）、谁、何时。回滚 = 以某行的
    old_value 为新值再走一次 HIL 批准的应用 ⇒ 闭环且无自动后门。
    applied=False 的行是幂等未应用记录（台账不撒谎：发生过评估，
    只是值没变）。

    幂等：IF NOT EXISTS + 索引；不塞种子（评估只能从真实提案发起，
    变更只能经人批准产生）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sandbox_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id INTEGER NOT NULL,
        scope TEXT DEFAULT 'full',
        shadow_json TEXT DEFAULT '{}',
        status TEXT DEFAULT 'queued',
        verdict TEXT DEFAULT '',
        summary_json TEXT DEFAULT '{}',
        job_id INTEGER DEFAULT 0,
        error TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        started_at TEXT,
        finished_at TEXT,
        duration_s INTEGER DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sandbox_runs_proposal "
              "ON sandbox_runs(proposal_id, id DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sandbox_runs_status "
              "ON sandbox_runs(status)")
    c.execute("""CREATE TABLE IF NOT EXISTS config_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        section TEXT NOT NULL,
        key TEXT NOT NULL,
        old_value TEXT DEFAULT '',
        new_value TEXT DEFAULT '',
        source TEXT DEFAULT 'proposal',
        source_id INTEGER DEFAULT 0,
        applied_by TEXT DEFAULT '',
        applied_at TEXT DEFAULT CURRENT_TIMESTAMP,
        note TEXT DEFAULT '',
        applied INTEGER DEFAULT 1
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_config_history_section "
              "ON config_history(section, key, id DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_config_history_source "
              "ON config_history(source, source_id)")
    conn.commit()
    print("[init_db] 迁移: 沙箱运行/配置台账表 sandbox_runs + config_history 已建立（P1-4，晋升须过人）")


def _migrate_misevolution_p21(conn):
    """P2-1（2026-10-19 自进化调研路线图 P2 首项）：misevolution 监控两张表。

    ## misevolution_baselines —— 冻结的安全信号基线（一行 = 一次冻结）
    红线 #5 的「相对滑坡」判定需要过去某个可信时刻的快照：由人显式冻结
    （POST /api/monitor/misevolution/baseline，epoch 标注理由，如
    'pre_p2_promotions'）。signals_json 存 collect_signals 完整快照
    （**含分母**——复测判 sufficient 要用；只存 rate 的话事后无法判断
    当时样本量够不够）。latest_baseline 取最新一条：基线是「当前状态」
    不是流水，同一时刻只认一个当前基线。

    ## misevolution_checks —— 复测记录（一行 = 一次复测）
    红线 #5「自进化后必须复测」的落点：execute_confirmation（P1-4 晋升/
    回滚）成功后记一行——晋升时刻信号快照 + 与当时基线比较的 verdict。
    verdict=regression 时**不自动回滚**（回滚过人的铁律同 P1-4）：check
    行本身留痕「这次晋升的安全复测结论」，回滚由人从台账发起。
    source: promotion（晋升复测）/ manual / ci。

    幂等：IF NOT EXISTS + 索引；不塞种子（基线只能由人显式冻结，复测只能
    由真实晋升/手动触发产生——没有"演示数据"能冒充安全基线）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS misevolution_baselines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        epoch TEXT NOT NULL,
        signals_json TEXT DEFAULT '{}',
        note TEXT DEFAULT '',
        frozen_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_misevolution_baselines_epoch "
              "ON misevolution_baselines(epoch, id DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS misevolution_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        epoch TEXT NOT NULL,
        source TEXT DEFAULT 'manual',
        signals_json TEXT DEFAULT '{}',
        comparison_json TEXT DEFAULT '{}',
        verdict TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_misevolution_checks_verdict "
              "ON misevolution_checks(verdict, id DESC)")
    conn.commit()
    print("[init_db] 迁移: misevolution 基线/复测表 misevolution_baselines + misevolution_checks 已建立（P2-1，红线 #5）")


def _migrate_curriculum_p22(conn):
    """P2-2（2026-10-19 自进化调研路线图 P2 第二项）：失败驱动的任务提案队列。

    ## curriculum_items —— 一条「该练什么」的任务提案 = 一行
    行业参照 EnvBuilder / AgentGym / AgentEvolver Self-Questioning（agent 自己
    造课程）。证据来源全部是既有失败面：P1-2 首坏步模式（attribution）、
    P0-3 坏样本台账（bad_cases）、失败类型覆盖率（kind_coverage）。铁律同
    P1-3：**只提议，不自动执行**——自动造任务并自动执行是内部循环自我强化
    （misevolution 滑坡形态），任务必须经人裁决。

    ## 去重/状态语义
    - fingerprint UNIQUE：sha1(rule|task_kind|weakness)。弱点身份必须进指纹
      （P1-3 教训：模式身份不进指纹 ⇒ 不同弱点互相当重复，证据凭空丢失）。
    - status 三态：pending → accepted | rejected（终态不重开，与 #18 状态机
      纪律一致）。accepted/rejected 只记录**人的裁决**；任务的创建/执行走人
      所在链路，本链路没有任何创建任务的路径。
    - 幂等：IF NOT EXISTS + 索引；不塞种子数据（任务只能从真实失败证据生成——
      空窗口不是「没问题」，更不是「造点任务凑数」）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS curriculum_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule TEXT NOT NULL,
        task_kind TEXT DEFAULT '',
        title TEXT DEFAULT '',
        weakness TEXT DEFAULT '',
        suggestion TEXT DEFAULT '',
        difficulty TEXT DEFAULT 'low',
        evidence_json TEXT DEFAULT '{}',
        fingerprint TEXT NOT NULL UNIQUE,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        reviewed_by TEXT DEFAULT '',
        reviewed_at TEXT,
        review_note TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_curriculum_items_status "
              "ON curriculum_items(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_curriculum_items_rule "
              "ON curriculum_items(rule)")
    conn.commit()
    print("[init_db] 迁移: 任务提案队列表 curriculum_items 已建立（P2-2，只提议不自动执行）")


def _migrate_agent_archive_p23(conn):
    """P2-3（2026-10-19 自进化调研路线图 P2 第三项）：Agent 档案库两张表。

    DGM/AlphaZero 式种群的关键不是「保留最优」，而是**记住死掉的个体**——
    否则系统会反复重走已知死路。本迁移提供两个落点：

    ## agent_variants —— 变体台账（一行 = 一个 (agent, version) 变体）
    把散在 asset_change_logs（字段级 diff + version_from/version_to）、
    agents.version 自动递升、config_history（配置晋升台账）里的进化历史
    **收敛成一份种群台账**：谁、从哪个版本改来、当前是否在役、画像如何。
    - fingerprint UNIQUE：sha1(agent_id|version)。**不能用字段 diff 内容进指纹**
      ——同一版本号在不同时间被显式改动是真实语义，指纹含内容会让每次扫描
      造出「新变体」，台账注水。
    - status 三态：active（在役现行）| retired（血缘里的历史版本）|
      dead_end（判过回归且达重复阈值 ⇒ 死路记忆，终态）。
    - profile_json 存 fitness_profile（五维画像 + summary.verdict）；
      **逐维独立判 unknown**：分母不足即 null，不回落 0（空窗口不是 0 分）。
    - 本表**只记录，不切换生效 agent**——档案库是记忆，不是手（回滚过人的
      铁律同 P1-4）。

    ## variant_outcomes —— 结局记录（一行 = 一次外部评估结论）
    谁把结论挂到变体上是显式的（record_outcome 入户口）：真实库里
    sandbox_runs / misevolution_checks 都是提案级、没有 agent_id+version 列，
    档案库**不猜**这次评估针对哪个变体——猜出来的血缘比没有血缘更危险。
    - outcome ∈ pass|clean|regression|error|insufficient_data；
      **error 与 insufficient_data 不算负向**（环境坏/样本不足 ≠ 这条路不行，
      与 P1-4 verdict:error 同语义）。
    - 死路判定只看本表的负向计数 ≥dead_end_repeat。

    幂等：IF NOT EXISTS + 索引；**不塞种子**（台账只能由真实进化历史收敛出
    来——凭空塞变体等于伪造历史）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS agent_variants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL,
        agent_name TEXT DEFAULT '',
        version TEXT NOT NULL,
        parent_version TEXT DEFAULT '',
        origin TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        fingerprint TEXT NOT NULL UNIQUE,
        profile_json TEXT DEFAULT '{}',
        dead_end_reason TEXT DEFAULT '',
        dead_end_at TEXT,
        first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_av_agent "
              "ON agent_variants(agent_id, id ASC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_av_status "
              "ON agent_variants(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_av_version "
              "ON agent_variants(agent_id, version)")
    c.execute("""CREATE TABLE IF NOT EXISTS variant_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        variant_id INTEGER NOT NULL REFERENCES agent_variants(id) ON DELETE CASCADE,
        outcome TEXT NOT NULL,
        source TEXT DEFAULT 'manual',
        detail TEXT DEFAULT '',
        recorded_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vo_variant "
              "ON variant_outcomes(variant_id, id DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vo_outcome "
              "ON variant_outcomes(outcome)")
    conn.commit()
    print("[init_db] 迁移: Agent 变体台账 agent_variants + 结局表 variant_outcomes "
          "已建立（P2-3，死路记忆；只记录不切换生效版本）")


def _migrate_flow_topology_p24(conn):
    """P2-4（2026-10-19 自进化调研路线图 P2 第四项）：画布拓扑台账。

    路线图原文「多画布流拓扑实验」。落地前勘察真实库，两个事实改写了口径：

    ① **运行证据不足以支撑拓扑实验**：agent_flows 62 条定义 vs
       flow_runs 仅 11 条 / flow_run_steps 33 条，其中 9 条是同日三个同构
       画布各跑 3 次（49–82ms，秒级），只有 run 198 是真实长时运行
       （94.8s / 6 步 / 1 个 error）。在 11 条记录上比较两种拓扑的优劣
       得到的是噪声。**实验的前提是先有可比较的台账**，故本期落台账与体检。

    ② **同一条路线图的安全红线 #6 写着「不碰权重、不碰架构拓扑」**：
       自动生成新拓扑并自动切换生效画布直接违反这条自己定的红线。

    ## flow_topology_variants —— 画布拓扑台账（一行 = 一幅画布）
    把「画布数量」这个看不出问题的指标，换成「结构是否健康、是否重复」：
    - flow_id UNIQUE：一幅画布一行，扫描反复跑只刷新不新增（台账不注水）。
    - fingerprint = sha1(节点 id + 有向边 + 节点类型计数)，**不含 name /
      version / label / 时间戳**——改个名字不该造出新拓扑（P2-3 变体指纹
      同型坑：指纹含易变字段会让台账注水）。
    - defects_json 存结构缺陷明细 + 按类别计数（blocker/warn 两档，
      不做可配阈值打分：没有先验支撑「3 warn 比 2 blocker+1 warn 更糟」）。
    - evidence_state = covered（至少跑过一次）| uncovered（**从未跑过**），
      逐画布独立判，**不回落 0**：没跑过不是跑了零次。
    - 本表**只记录，绝不写 agent_flows**——拓扑台账是体检镜不是手
      （门禁 E 段静态断言 repo 里没有 UPDATE/DELETE agent_flows）。

    幂等：IF NOT EXISTS + 索引；**不塞种子**（台账只能由真实画布体检
    收敛出来——凭空塞行等于伪造健康度）。
    """
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS flow_topology_variants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        flow_id INTEGER NOT NULL UNIQUE,
        flow_name TEXT DEFAULT '',
        fingerprint TEXT DEFAULT '',
        node_count INTEGER DEFAULT 0,
        edge_count INTEGER DEFAULT 0,
        status TEXT DEFAULT '',
        source TEXT DEFAULT '',
        blocker_count INTEGER DEFAULT 0,
        warn_count INTEGER DEFAULT 0,
        defects_json TEXT DEFAULT '{}',
        runs INTEGER DEFAULT 0,
        failed_runs INTEGER DEFAULT 0,
        evidence_state TEXT DEFAULT 'uncovered',
        first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ftv_fingerprint "
              "ON flow_topology_variants(fingerprint)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ftv_evidence "
              "ON flow_topology_variants(evidence_state)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ftv_blocker "
              "ON flow_topology_variants(blocker_count)")
    conn.commit()
    print("[init_db] 迁移: 画布拓扑台账 flow_topology_variants 已建立"
          "（P2-4，结构体检 + 同构去重；只记录不改拓扑，红线 #6）")
