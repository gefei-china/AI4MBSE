"""插件/工具/Agent 能力域迁移（内置标记、工具状态、团队、作用域、共享评审、来源、P0 能力、插件表与依赖、legacy 引用）。"""
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
            project_id TEXT NOT NULL DEFAULT '',
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
