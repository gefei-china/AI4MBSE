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
