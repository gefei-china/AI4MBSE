"""init_db：建表 + 编排迁移（migrations.py）与种子（seeds.py）。幂等。"""
import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import datetime

from core.config import DB_PATH
from .connection import get_db
from .migrations import (
    _migrate_columns,
    _migrate_agent_tool_perm,
    _migrate_branch_protection,
    _migrate_mr_status,
    _rebuild_entities_pk,
    _repair_relations_fk,
    _repair_documents_fk,
    _rename_release_branch,
    _rename_dev_branch,
    _migrate_graph_tables,
    _migrate_v2g_tables,
    _migrate_staging_tables,
    _migrate_sysml_tables,
    _migrate_project_ingest_logs,
    _migrate_glossary_changelog,
    _migrate_glossary_discoveries,
    _migrate_document_lifecycle,
    _migrate_profile_tables,
    _ensure_ontology_types,
    _backfill_pipeline_detail,
    _apply_env_keys,
    _migrate_glossary_tables,
    _migrate_domain_review_queue_orphans,  # 2026-09-19 复核队列孤儿行兜底清理
    _migrate_ontology_change_tables,
    _migrate_ontology_version_tables,
    _dedupe_ontology_types,
    _migrate_ontology_iri,
    _migrate_eval_tables,
    _migrate_kb_v2_enhance,
    _migrate_entity_temporal,  # P0-④ 时态管理（2026-09-11）
    _migrate_triple_optimization,
    _migrate_reasoning_cohorts,
    _migrate_mr_comments,
    _migrate_view_layout_checks,  # P0-2 视图布局质量存档（SRS-GN-MG-BJYH）
    _migrate_data_sources,  # P0-4 数据源注册表重建（db/api/file 三类源）
    _migrate_swrl_tables,  # P1-① SWRL 规则管理（2026-09-11）
    _migrate_ontology_instance_migrations,  # 2026-09-14 本体变更→实例迁移计划表
    _migrate_artifact_ingest,  # 2026-09-15 AI 产物收编资料库（origin 溯源/块级过滤/时效取代）
    _seed_departments,
    _seed_knowledge_categories,
    _backfill_graph_source_info,
    _backfill_commit_baseline,
    _migrate_builtin_flags,
    _migrate_tool_status,
    _migrate_builtin_tool_schemas,
    _migrate_agent_team,
    _migrate_plugin_scope,
    _migrate_share_review,
    _migrate_plugin_origin,
    _migrate_p0_capabilities,
    _migrate_file_extract_settings,
    _migrate_docs_global,
    _migrate_doc_folders,  # 2026-09-21 文档目录树 doc_folders + documents.folder_id（基于文件的管理）
    _migrate_plugin_tables,
    _migrate_plugin_dependencies,  # 2026-09-16 能力依赖索引表（P0-2）
    _migrate_intent_samples,
    _migrate_dashboard_snapshots,      # 2026-09-26 意图样本池（新增迁移须在此处**显式导入**，否则 NameError）
    _migrate_drop_agent_tools_params,  # 2026-09-30 移除 agent_tools.params 废列（全仓零消费点）
    _migrate_tool_result_offloads,  # 2026-10-02 工具结果 offload 表（P1-4 Tier1 可寻址召回）
    _migrate_audit_chain,  # 2026-10-03 审计溯源列 + 哈希链（P0-a/P0-b）
)
from .seeds import (
    _seed,
    _seed_projects,
    _seed_agents,
    _seed_builtin_tools,
    _backfill_domains,
    _seed_glossary,
    _seed_intent_rules,
)


# ══════════════════════════════════════════════════════════════════════════
# P0-3 前置：迁移安全闸（2026-10-04）
# ══════════════════════════════════════════════════════════════════════════
# 【为什么需要】`init_db()` 是**唯一**的迁移入口，而它一次要做三件事：
# 建 129 张表 + 补列 + 跑全部 `_migrate_*` 迁移 + 播种种子 —— 中间**没有闸**。
# 实测代价（本机两次踩到）：
#   ① 2026-10-04 `verify_orch_resume.py` 用 env 设库路径，但 `DB_PATH` 在 import 期
#      就被绑进模块作用域 ⇒ env 设得太晚，`init_db()` **在生产库上跑了一遍迁移**；
#   ② 三份规范入库实测 **13.4 分钟且必须停服务**（SQLite 单写者被占满）。
# 两者叠加意味着：**一次误调用 init_db 就是一次不可回滚的全库变更**。
#
# 【本模块提供什么】`safe_init_db()` 薄壳：
#   · 迁移前**自动在线热备**（`sqlite3.backup()`，不是 copy2 —— WAL 库的唯一正确方式）；
#   · `dry_run=True` 时**只报告将要做什么**（盘点表/行数），不落任何写；
#   · 备份失败即**拒绝迁移**（宁可不起动，也不做无备份的变更）；
#   · 报告里给出耗时口径，便于判断"值不值得等"。
#
# 【为什么不改 init_db 本身】它是 1155 行的巨型函数，任何内联改动都会与
# 「保持行为不变」冲突。薄壳是最小风险面：**init_db 的语义一字未动**。

def inspect_db_state(db_path: str | None = None) -> dict:
    """只读盘点当前库状态（**不落任何写操作**）。

    返回：表清单数、总行数、行数top10、以及打开库时的报错（读不到也要报出来，
    不能静默返回空 —— 否则"盘点不到"会被误读成"库是空的"）。
    """
    p = db_path or DB_PATH
    info = {"path": p, "exists": False, "size_mb": 0.0, "tables": 0, "row_total": 0,
            "top": [], "db_error": ""}
    try:
        if not os.path.exists(p):
            return info
        info["exists"] = True
        info["size_mb"] = round(os.path.getsize(p) / 1e6, 1)
    except Exception as e:
        info["db_error"] = str(e)[:200]
        return info
    con = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        tabs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        info["tables"] = len(tabs)
        rows = []
        for t in tabs:
            try:
                rows.append((int(con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]), t))
            except Exception:
                pass
        info["row_total"] = sum(n for n, _ in rows)
        info["top"] = [{"table": t, "rows": n} for n, t in sorted(rows, reverse=True)[:10]]
    except Exception as e:
        info["db_error"] = str(e)[:200]
    finally:
        if con is not None:
            con.close()
    return info


def backup_db(dest_dir: str | None = None, tag: str = "pre-migrate") -> str:
    """在线热备数据库（WAL 安全的唯一正确方式），返回备份文件路径。

    ⚠️ **不要用 `shutil.copy2`**：WAL 模式下 .db 文件可能不是最新状态（WAL 里还有
    未 checkpoint 的事务），复制出来的是**不一致快照**。必须用 `sqlite3.Connection.backup`，
    它会按页把 WAL 内容一并纳入。

    :param dest_dir: 备份目录；默认 `<仓库>/backups`
    :param tag:       文件名标签
    """
    import time as _t
    d = dest_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "%s-%s.db" % (tag, _t.strftime("%Y%m%d-%H%M%S")))
    src = sqlite3.connect(DB_PATH)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)          # 在线热备，含 WAL
        finally:
            out.close()
        # ⚠️ 备份**有效性校验**（2026-10-04 新增，实测发现的缺陷）：
        #   `src.backup(out)` 只复制内容、**不校验源库有效性** —— 若 DB_PATH
        #   指向一个刚创建的空库，backup 会"成功"并产出一个 20KB 的空壳
        #   （实测：`backups/` 里 14 份 pre-migrate-*.db 全是 4 张表的空壳）。
        #   ⇒ `safe_init_db` 的"有备份才迁移"闸会以为安全，实际留下**恢复时会炸**
        #   的假备份 —— 迁移越危险，越要命。
        #   ⚠️ 必须在 src 关闭**之前**校验（校验要读源库的表集合）。
        _verify_backup(src, dst)
    finally:
        src.close()
    return dst


def _verify_backup(src, dst: str) -> None:
    """校验备份与源库的表集合一致；不一致则删掉备份并抛 RuntimeError。

    :param src: **未关闭**的源连接（校验要读它的表集合）
    :param dst: 备份文件路径
    :raises RuntimeError: 表集合不一致（备份不可用于恢复）
    """
    def _tables(con):
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}

    try:
        s_tables = _tables(src)
    except Exception as e:                       # 源库都读不了 ⇒ 更该失败
        _unlink_backup(dst)
        raise RuntimeError("备份校验失败：源库不可读（%s）" % e)
    # ⚠️⚠️ 先查**源库本身**是否像一份真库（2026-10-04 实测补的第二个漏洞）。
    #   第一版只查「备份是否比源库少表」—— 实测**空库备份照样通过**：
    #   空库有 1 张表、备份复制出来也是这 1 张 ⇒ 集合一致 ⇒ 放行。
    #   而这正是实测到的 14 份假备份的成因（`init_db` 在新空库上跑完就备份）。
    #   ⇒ 判据必须是"**源库**像不像真库"，与备份无关。
    #   门槛取业务核心表：一张都没有 ⇒ 这不是本工程的库。
    CORE_TABLES = ("documents", "entities", "agent_def", "settings")
    present = [t for t in CORE_TABLES if t in s_tables]
    if not present:
        _unlink_backup(dst)
        raise RuntimeError(
            "备份校验失败：源库 %s 只有 %d 张表且**不含任何核心业务表**（%s）"
            "⇒ 这是一个空库/测试库，不是待迁移的库。"
            "常见原因：MBSE_DB_PATH 指向了新创建的空库（迁移脚本自己 init_db 出来的）。"
            % (os.path.basename(DB_PATH), len(s_tables), "/".join(CORE_TABLES)))
    try:
        d_con = sqlite3.connect(dst)
        try:
            d_tables = _tables(d_con)
        finally:
            d_con.close()
    except Exception as e:
        _unlink_backup(dst)
        raise RuntimeError("备份校验失败：备份文件不可读（%s）" % e)

    # ⚠️ 允许备份比源库「多」表（迁移已在备份之后加过表），
    #   但**不允许少**：少了 ⇒ 备份是空壳/损坏，恢复必然丢数据。
    missing = s_tables - d_tables
    if missing:
        _unlink_backup(dst)
        raise RuntimeError(
            "备份校验失败：备份缺少 %d 张表（%s…）⇒ 拒绝迁移。"
            "常见原因：DB_PATH 指向了刚创建的空库 / 测试库。"
            % (len(missing), ", ".join(sorted(missing)[:5])))
    # 关键表行数抽样（表集合相同但数据为空的情况）
    for t in ("documents", "entities"):
        if t in s_tables:
            try:
                n_src = src.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                d_con = sqlite3.connect(dst)
                try:
                    n_dst = d_con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                finally:
                    d_con.close()
            except Exception:
                continue
            if n_src > 0 and n_dst == 0:
                _unlink_backup(dst)
                raise RuntimeError(
                    "备份校验失败：%s 源库 %d 行 / 备份 0 行 ⇒ 拒绝迁移。" % (t, n_src))


def _unlink_backup(dst: str) -> None:
    """删掉校验失败的备份（含 -wal/-shm 旁文件），不留下误导性的残留。"""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(dst + suffix)
        except OSError:
            pass


def safe_init_db(dry_run: bool = False, *, backup: bool = True,
                 backup_dir: str | None = None, tag: str = "pre-migrate",
                 force: bool = False) -> dict:
    """带备份与 dry-run 的 `init_db()` 包装（**推荐在人工/运维路径使用**）。

    :param dry_run: 只盘点并返回报告，**不执行任何迁移**
    :param backup:  迁移前是否自动热备（WAL 库的正确方式）
    :param force:   备份失败时是否仍强行迁移。默认 False —— **没有备份就不迁移**。
    :returns: 报告 dict，含 `action` / `backup` / `state` / `elapsed_s`

    为什么默认不 dry_run：服务启动路径（lifespan）调的是 `init_db()`，保持原样；
    本函数面向**人工变更、批量导入前、升级前**这些真正需要止损点的场景。
    """
    import time as _t
    t0 = _t.time()
    rep = {"action": "", "backup": "", "state": {}, "elapsed_s": 0.0,
           "forced": False, "error": ""}

    # ① dry-run：只报告，绝不写
    if dry_run:
        rep["action"] = "dry-run（未落任何写操作）"
        rep["state"] = inspect_db_state()
        rep["elapsed_s"] = round(_t.time() - t0, 2)
        return rep

    # ② 备份（失败即拒绝，除非显式 force）
    if backup:
        try:
            rep["backup"] = backup_db(backup_dir, tag)
        except Exception as e:
            rep["error"] = "备份失败，已拒绝迁移：%s" % str(e)[:200]
            if not force:
                rep["action"] = "aborted（无备份不迁移）"
                rep["elapsed_s"] = round(_t.time() - t0, 2)
                return rep
            rep["forced"] = True
    rep["action"] = "migrated"
    try:
        init_db()
    except Exception as e:
        rep["error"] = str(e)[:300]
        rep["action"] = "failed"
        rep["elapsed_s"] = round(_t.time() - t0, 2)
        raise
    rep["elapsed_s"] = round(_t.time() - t0, 2)
    rep["state"] = inspect_db_state()
    return rep

def init_db():
    """建表 + 种子数据 + 环境变量 key 注入。幂等：表已存在跳过，有数据跳过 seed。"""
    # 确保数据文件父目录存在（MBSE_DB_PATH 可指向任意目录）
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir and not os.path.isdir(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = get_db()
    c = conn.cursor()

    # ── 用户与角色 ──
    c.execute("""CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        type TEXT DEFAULT 'custom',  -- preset | custom
        description TEXT DEFAULT '',
        permissions TEXT DEFAULT '{}',  -- JSON: {domain: [operations]}
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        department TEXT DEFAULT '',
        role_id INTEGER REFERENCES roles(id),
        workspace TEXT DEFAULT '',
        source TEXT DEFAULT 'local',  -- local | ldap | oauth2
        status TEXT DEFAULT 'active',  -- active | disabled
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 权限域表化（2026-09-01 权限域表化）：permission_domains + permission_ops。
    #    原 PERM_DOMAINS 代码常量降级为内置种子（建表/首次启动写入），
    #    运行时从表读，支持管理员免代码扩展权限模型（对齐 D-2 领域包差异化）。
    #    builtin=1 为内置种子（不可删除/不可改 key），custom 可增删。
    c.execute("""CREATE TABLE IF NOT EXISTS permission_domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_key TEXT UNIQUE NOT NULL,      -- 域键（如 ai_chat / kb_ontology）
        label TEXT NOT NULL,                  -- 显示名（如 AI 建模对话）
        builtin INTEGER DEFAULT 1,            -- 1=内置种子（不可删/不可改键）0=自定义
        sort_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT ''
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS permission_ops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_key TEXT NOT NULL REFERENCES permission_domains(domain_key) ON DELETE CASCADE,
        op_key TEXT NOT NULL,                 -- 操作键（如 view / publish）
        label TEXT NOT NULL,                  -- 显示名
        builtin INTEGER DEFAULT 1,            -- 1=内置种子（不可删）0=自定义
        UNIQUE(domain_key, op_key)
    )""")

    # ── 部门设置（用户归属部门受控词表，用户表单部门字段从该表下拉选择）──
    c.execute("""CREATE TABLE IF NOT EXISTS departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',  -- active | disabled
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 对话与消息 ──
    c.execute("""CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        intent TEXT DEFAULT '',
        status TEXT DEFAULT 'active',  -- active | completed | archived
        user_id INTEGER REFERENCES users(id),
        project_id TEXT DEFAULT '',  -- P0-1: 会话归属项目（2026-09-20：去硬编码默认）
        phase TEXT DEFAULT 'requirement',  -- requirement|design|change|review|merge
        pending_clarify TEXT DEFAULT '',  -- 内容级澄清挂起：{questions, context} JSON（未答清空前为空）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER REFERENCES conversations(id),
        role TEXT NOT NULL,  -- user | assistant | system
        content TEXT NOT NULL,
        msg_type TEXT DEFAULT 'text',  -- text | card_impact | card_review | card_candidates
        card_data TEXT DEFAULT '{}',  -- JSON for rich cards
        attachments TEXT DEFAULT '[]',  -- JSON array
        feedback TEXT DEFAULT '',  -- approve | reject | modify | ''
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 知识库：实体与关系（实体版本化：id 全局逻辑唯一，同 id 可在不同分支各有版本行）──
    c.execute("""CREATE TABLE IF NOT EXISTS entities (
        id TEXT NOT NULL,
        name TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        properties TEXT DEFAULT '{}',  -- JSON
        status TEXT DEFAULT 'candidate',  -- raw_chunk|candidate|reviewed|deprecated
        branch TEXT DEFAULT 'dev',
        project_id TEXT DEFAULT '',  -- P0-1: 项目上下文隔离（2026-09-20：去硬编码默认）
        source_doc TEXT DEFAULT '',
        source_type TEXT DEFAULT '',  -- graph | vector | manual | ai_generated
        confidence REAL DEFAULT 1.0,
        created_by TEXT DEFAULT '',
        reviewed_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        reviewed_at TEXT DEFAULT '',
        knowledge_category TEXT DEFAULT '',  -- P0-3: 知识类别（设计方法知识/设计资产子类，空=未分类）
        published_at TEXT DEFAULT '',        -- P0-4: 进入 release（发布）时间，非空=已发布
        sysml_version_id INTEGER DEFAULT 0,  -- AI 建模：来源 sysml_versions.id（入库溯源）
        PRIMARY KEY (id, branch)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        properties TEXT DEFAULT '{}',
        status TEXT DEFAULT 'candidate',
        branch TEXT DEFAULT 'dev',
        project_id TEXT DEFAULT '',  -- P0-1: 项目上下文隔离（2026-09-20：去硬编码默认）
        confidence REAL DEFAULT 1.0,
        created_by TEXT DEFAULT '',
        reviewed_by TEXT DEFAULT '',
        reviewed_at TEXT DEFAULT '',
        source_doc TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (source_id, branch) REFERENCES entities(id, branch),
        FOREIGN KEY (target_id, branch) REFERENCES entities(id, branch)
    )""")

    # ── 本体（领域知识模型） ──
    c.execute("""CREATE TABLE IF NOT EXISTS ontology_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type_kind TEXT NOT NULL,  -- entity | relation | attribute
        parent_id INTEGER,
        properties TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 分支管理 ──
    c.execute("""CREATE TABLE IF NOT EXISTS branches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        branch_type TEXT NOT NULL,  -- release | dev | personal | local
        parent_branch TEXT DEFAULT '',
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'active',  -- active | merged | archived
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        protection_rules TEXT DEFAULT '{}'  -- P0-2 分支保护规则 JSON（解析见 core/branch_rules.py）
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS merge_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_branch TEXT NOT NULL,
        target_branch TEXT NOT NULL,
        status TEXT DEFAULT 'open',  -- draft | open | merged | closed（P1-1 状态机；旧 pending/approved/rejected 由迁移归一）
        conflicts TEXT DEFAULT '[]',  -- JSON array of conflict details
        created_by TEXT DEFAULT '',
        reviewed_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT DEFAULT '',
        release_version TEXT DEFAULT ''  -- 分支版本管理：人工填写的发布版本号（目标为 release 时可用，如 v1.1）
    )""")
    # merge_detail/prev_release_snapshot 等列由 _migrate_columns 幂等补齐（新库也走迁移补列）

    # ── 分支版本管理：Git 式提交（commit）链（一次导入/审核/合并=一次原子变更提交）──
    c.execute("""CREATE TABLE IF NOT EXISTS knowledge_commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch TEXT NOT NULL,                 -- 提交所在分支
        parent_id INTEGER DEFAULT NULL,       -- 分支内父提交（形成链）
        kind TEXT NOT NULL DEFAULT 'import',  -- import | review | merge | manual | rollback
        message TEXT DEFAULT '',              -- 提交说明
        changes TEXT DEFAULT '{}',            -- JSON：{entities:[id], relations:[id], documents:[id], chunks:[id]} 变更对象清单
        snapshot TEXT DEFAULT '{}',           -- JSON：提交后各对象关键字段摘要（回滚用）
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        content_hash TEXT DEFAULT ''          -- P0-3 内容哈希 sha256(branch|parent_id|kind|规范化changes|规范化snapshot)，防改库篡改
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kc_branch ON knowledge_commits(branch, id)")

    # ── AI 设计工坊 ──
    c.execute("""CREATE TABLE IF NOT EXISTS prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        scenario TEXT DEFAULT '',
        content TEXT NOT NULL,
        variables TEXT DEFAULT '[]',  -- JSON array of variable names
        version TEXT DEFAULT 'v1',
        status TEXT DEFAULT 'draft',  -- draft | published
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        skill_type TEXT DEFAULT 'text2json',  -- text2json | tool_call | llm_score
        description TEXT DEFAULT '',
        prompt_id INTEGER REFERENCES prompts(id),
        tool_name TEXT DEFAULT '',
        version TEXT DEFAULT 'v1.0',
        status TEXT DEFAULT 'draft',
        enabled INTEGER DEFAULT 1,         -- 1=启用 0=停用（停用不参与触发/绑定）
        builtin INTEGER DEFAULT 0,         -- 1=内置（平台预置，禁止删除可编辑）
        scope TEXT DEFAULT 'private',      -- private=私人空间 | public=公共插件市场
        source_ref TEXT DEFAULT '',        -- 市场来源 "kind:name:version"（安装副本记录）
        pinned INTEGER DEFAULT 0,          -- 市场置顶 1=置顶（市场管理后台）
        install_count INTEGER DEFAULT 0,   -- 市场安装计数
        share_status TEXT DEFAULT '',      -- 个人分享审核：'' | submitted | approved | rejected
        origin TEXT DEFAULT '',            -- 来源：admin（管理员创建）| share（个人分享通过）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS mcp_servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        tools TEXT DEFAULT '[]',  -- JSON array
        status TEXT DEFAULT 'offline',  -- online | offline
        enabled INTEGER DEFAULT 1,  -- 1=启用 0=停用（停用不注入 Agent、不参与发现）
        builtin INTEGER DEFAULT 0,  -- 1=内置（平台预置，禁止删除可编辑）
        scope TEXT DEFAULT 'private',      -- private=私人空间 | public=公共插件市场
        source_ref TEXT DEFAULT '',        -- 市场来源 "kind:name:version"（安装副本记录）
        pinned INTEGER DEFAULT 0,          -- 市场置顶 1=置顶（市场管理后台）
        install_count INTEGER DEFAULT 0,   -- 市场安装计数
        share_status TEXT DEFAULT '',      -- 个人分享审核：'' | submitted | approved | rejected
        origin TEXT DEFAULT '',            -- 来源：admin（管理员创建）| share（个人分享通过）
        latency_ms INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS plugin_review_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,            -- skill | mcp
        item_id INTEGER NOT NULL,
        item_name TEXT NOT NULL,
        action TEXT NOT NULL,          -- submit | approve | reject
        comment TEXT DEFAULT '',
        operator TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS tools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        source TEXT DEFAULT 'local',  -- local | mcp
        mcp_server_id INTEGER REFERENCES mcp_servers(id),
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        builtin INTEGER DEFAULT 0,  -- 1=内置（平台预置，禁止删除可编辑；与 source='builtin' 等价）
        scope TEXT DEFAULT 'private',      -- private=私人空间 | public=公共插件市场
        source_ref TEXT DEFAULT '',        -- 市场来源 "kind:name:version"（安装副本记录）
        pinned INTEGER DEFAULT 0,          -- 市场置顶 1=置顶（市场管理后台）
        install_count INTEGER DEFAULT 0,   -- 市场安装计数
        origin TEXT DEFAULT '',            -- 来源：admin（管理员创建）| share（个人分享通过）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS agent_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        nodes TEXT DEFAULT '[]',  -- JSON array of nodes
        edges TEXT DEFAULT '[]',  -- JSON array of edges
        version TEXT DEFAULT 'v1',
        status TEXT DEFAULT 'draft',
        source TEXT DEFAULT 'manual',  -- manual | planner_auto（自动编排沉淀）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 编排运行轨迹（多智能体运行历史监控）──
    c.execute("""CREATE TABLE IF NOT EXISTS flow_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        flow_id INTEGER DEFAULT 0,
        flow_name TEXT DEFAULT '',
        status TEXT DEFAULT 'completed',        -- completed | partial
        order_json TEXT DEFAULT '[]',
        total_latency_ms INTEGER DEFAULT 0,
        error_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS flow_run_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        seq INTEGER DEFAULT 0,
        node_id TEXT DEFAULT '',
        node_type TEXT DEFAULT '',
        node_label TEXT DEFAULT '',
        status TEXT DEFAULT '',                 -- done | error | skipped
        content TEXT DEFAULT '',
        data TEXT DEFAULT '{}',
        latency_ms INTEGER DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_flow_steps_run ON flow_run_steps(run_id)")

    # P0 状态管理：检查点（每节点执行后 state 快照，支持中断恢复/时间旅行/循环回退）
    c.execute("""CREATE TABLE IF NOT EXISTS flow_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        node_id TEXT DEFAULT '',
        state_json TEXT DEFAULT '{}',           -- 该时刻完整 results 快照
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_fcp_run_seq ON flow_checkpoints(run_id, seq)")

    # P1 记忆系统：L1 工作记忆（共享黑板） / L2 编排级会话 / L3 长期记忆
    c.execute("""CREATE TABLE IF NOT EXISTS flow_working_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT DEFAULT '{}',
        mem_type TEXT DEFAULT 'artifacts',   -- artifacts | context | result
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_fwm_run ON flow_working_memory(run_id, key)")

    # ── P1 上下文工程：会话历史摘要缓存（三层窗口：即时原文 / 中期摘要 / 历史概览）──
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        scope TEXT NOT NULL,               -- mid（中期摘要）| hist（历史概览）
        anchor_id INTEGER DEFAULT 0,       -- 覆盖段最大消息 id（段变化则重算）
        summary TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(conversation_id, scope)
    )""")

    # ── P2 语义缓存：高频相似查询 embedding 命中直返（LLM 不参与，省时省钱）──
    c.execute("""CREATE TABLE IF NOT EXISTS semantic_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        query_embedding TEXT DEFAULT '[]',
        embed_version TEXT DEFAULT '',
        answer TEXT DEFAULT '',
        hit_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_hit_at TEXT DEFAULT ''
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sc_query ON semantic_cache(query)")

    # ── 意图识别增强（P0-3）：意图级缓存——L1 命中直接返回（省重复规则/语义/LLM 全链路）──
    # index_fp：路由索引指纹（DB Agent 关键词 + 语义索引），指纹变化自动失效
    c.execute("""CREATE TABLE IF NOT EXISTS intent_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        query_hash TEXT NOT NULL UNIQUE,
        intent TEXT DEFAULT '',
        route TEXT DEFAULT '',
        confidence REAL DEFAULT 0,
        index_fp TEXT DEFAULT '',
        hit_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ic_hash ON intent_cache(query_hash)")

    c.execute("""CREATE TABLE IF NOT EXISTS flow_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        seq INTEGER DEFAULT 0,
        role TEXT DEFAULT '',
        content TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_fcv_run ON flow_conversations(run_id)")

    # D7 多 Agent 发布-订阅：共享消息池（MetaGPT 模式——发布者写标准化消息，订阅者按 topic 认领）
    c.execute("""CREATE TABLE IF NOT EXISTS agent_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        seq INTEGER DEFAULT 0,
        topic TEXT NOT NULL,
        content_json TEXT DEFAULT '{}',          -- 发布内容（JSON，可含结构化字段）
        publisher_node TEXT DEFAULT '',
        subscriber_node TEXT DEFAULT '',
        claimed INTEGER DEFAULT 0,               -- 0 待认领 | 1 已被订阅节点认领
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_am_claim ON agent_messages(run_id, topic, claimed)")

    # D11 A2A 协议互通：事件回调订阅（8.2 API 网关层「事件回调」）——
    # 执行器在节点完成/出错/运行完成时，向订阅的 webhook_url POST 标准化 A2A 事件（HMAC-SHA256 签名）。
    c.execute("""CREATE TABLE IF NOT EXISTS flow_event_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,                 -- 0=全局订阅（任意 run 触发）；>0=仅该 run
        node_id TEXT DEFAULT '',                 -- ''=全部节点；否则仅匹配该节点 id
        event_type TEXT NOT NULL,                -- node_done | node_error | run_completed
        webhook_url TEXT NOT NULL,
        secret TEXT DEFAULT '',                  -- HMAC-SHA256 签名密钥（空=不签名）
        status TEXT DEFAULT 'active',            -- active | paused | failed
        last_event_at TEXT DEFAULT '',
        last_status_code INTEGER DEFAULT 0,
        last_error TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fes_run ON flow_event_subscriptions(run_id, event_type)")

    # D12 统一监控平台：告警规则 + 告警事件（8.2「监控与日志面板」+「监控面板和性能分析工具」）
    # 规则定义阈值指标（success_rate/avg_latency/error_count/mock_rate），运行结束后执行器评估，命中写事件（可选 webhook 通知）。
    c.execute("""CREATE TABLE IF NOT EXISTS alert_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        rule_type TEXT DEFAULT 'threshold',      -- threshold（阈值比较）
        metric TEXT NOT NULL,                    -- run 级: success_rate | avg_latency | error_count | mock_rate
                                            -- 全局级(P0-d 周期评估): llm_* | cost_* | auth_* | audit_*
                                            --   见 core/alert_evaluator.GLOBAL_METRICS
        operator TEXT DEFAULT '>',               -- > | >= | < | <=
        threshold REAL DEFAULT 0,
        level TEXT DEFAULT 'warning',            -- info | warning | critical
        notify_url TEXT DEFAULT '',              -- 告警 webhook（A2A 事件结构，空=仅记录）
        secret TEXT DEFAULT '',                  -- HMAC-SHA256 签名密钥
        status TEXT DEFAULT 'active',            -- active | paused
        last_fired_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ar_status ON alert_rules(status)")
    c.execute("""CREATE TABLE IF NOT EXISTS alert_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id INTEGER DEFAULT 0,
        rule_name TEXT DEFAULT '',
        run_id INTEGER DEFAULT 0,
        flow_name TEXT DEFAULT '',
        metric TEXT DEFAULT '',
        actual REAL DEFAULT 0,
        threshold REAL DEFAULT 0,
        operator TEXT DEFAULT '',
        level TEXT DEFAULT 'warning',
        status TEXT DEFAULT 'open',              -- open | acked
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ae_status ON alert_events(status, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ae_rule ON alert_events(rule_id)")

    # ── P0-C（2026-10-04）：通用异步作业队列（job_jobs）────────────────────────
    # 为什么需要：长任务（如工程入库实测 13.4 分钟、SysML 导入）此前只能**同步占着
    # HTTP 连接**跑完 —— 请求断开即前功尽弃，且期间该连接占一个线程池名额。
    # 此前 `batch_id` 只是"数据批次标识"，**不是**异步作业句柄（已实测确认）。
    #
    # 为什么自己建表而不是引Redis+RQ：**AGENTS.md 铁律 4「私有化离线部署，
    # 不引入外部 CDN / 构建链 / 新依赖」**。Redis 方案直接违反该铁律；
    # 且本机实测无 redis-server / 无 redis-cli / 6379 无响应。
    # ⇒ 用 SQLite 承载队列状态（本仓已有 WAL + busy_timeout，单写者够用），
    #    消费端是后台 daemon worker（与 core/orch_supervisor 同一范本）。
    c.execute("""CREATE TABLE IF NOT EXISTS job_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_key TEXT UNIQUE NOT NULL,        -- 业务键（幂等去重：同key 重复提交只返回既有 job）
        kind TEXT DEFAULT '',                 -- 作业类型（决定用哪个 handler）
        payload TEXT DEFAULT '{}',           -- JSON入参
        status TEXT DEFAULT 'queued',        -- queued | running | done | failed | canceled
        progress INTEGER DEFAULT 0,-- 0~100（由 handler 主动上报；不精确，仅供展示）
        stage TEXT DEFAULT '',               -- 当前阶段文案（"正在解析 SysML…"，给人看）
        result TEXT DEFAULT '',              -- 成功产出（JSON 文本，截断落库）
        error TEXT DEFAULT '',
        attempt INTEGER DEFAULT 0,-- 已重试次数（达上限转 failed，防无限自愈）
        max_attempts INTEGER DEFAULT 3,
        -- 租约：worker claim 时写心跳；进程崩溃后租约过期 ⇒ 其他 worker 可回收
        lease_until TEXT DEFAULT '',
        heartbeat_at TEXT DEFAULT '',
        worker_id TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    # 状态索引：worker 扫"可领取的"作业（queued + 租约已回收的 running）
    c.execute("CREATE INDEX IF NOT EXISTS ix_job_pick ON job_jobs(status, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_job_kind ON job_jobs(kind, status)")


    c.execute("""CREATE TABLE IF NOT EXISTS agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        mem_type TEXT DEFAULT 'fact',        -- profile | fact | preference | experience | skill
        content TEXT DEFAULT '',
        embedding TEXT DEFAULT '[]',
        mem_topic TEXT DEFAULT '',           -- 主题标签（Auto Memory 索引，检索先按主题过滤）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        scope_type TEXT DEFAULT '',          -- 作用域：'' | global | project | user | agent（'' = 存量行，按 agent 槽召回）
        scope_id TEXT DEFAULT ''             -- 作用域标识：project=projects.id / user=username / agent=intent / global 为空串
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_am_agent ON agent_memory(agent_id, mem_type)")
    # ⚠️ ix_am_scope 不在此处建：老库「表已存在」时 CREATE TABLE IF NOT EXISTS 不会加列，
    #    此处建索引会 `no such column: scope_type` 直接打断 init_db。统一放到补列迁移之后
    #    （database/migrations/columns.py 的 _migrate_columns 内）。

    c.execute("""CREATE TABLE IF NOT EXISTS generate_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_key TEXT UNIQUE NOT NULL,
        rule_value TEXT DEFAULT '',
        description TEXT DEFAULT '',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── LLM 配置 ──
    c.execute("""CREATE TABLE IF NOT EXISTS llm_providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        provider_type TEXT DEFAULT 'openai',  -- openai | deepseek | qwen | custom
        base_url TEXT NOT NULL,
        api_key TEXT DEFAULT '',
        model_name TEXT NOT NULL,
        model_type TEXT DEFAULT 'chat',       -- chat 对话模型 | embedding 向量模型
        max_tokens INTEGER DEFAULT 8192,
        context_window INTEGER DEFAULT 8192,  -- 上下文窗口（tokens），对话请求 max_tokens 不超过该值
        temperature REAL DEFAULT 0.3,
        is_default INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── Agent 管理（P0 平台化：DB 驱动 Agent 注册表 + skill/mcp 绑定）──
    c.execute("""CREATE TABLE IF NOT EXISTS agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,            -- 意图标识（requirement_analysis/design/...）
        display_name TEXT NOT NULL,           -- 展示名（需求分析Agent）
        description TEXT DEFAULT '',
        system_prompt TEXT DEFAULT '',        -- 自定义 system prompt（空=用默认模板）
        model_provider_id INTEGER REFERENCES llm_providers(id),
        model_params TEXT DEFAULT '{}',       -- Agent 级参数覆盖 {"temperature":0.2}
        hil_level TEXT DEFAULT 'L0',          -- L0/L1/L2
        kb_required INTEGER DEFAULT 0,
        intent_keywords TEXT DEFAULT '[]',    -- 意图关键词（并入 IntentRouter 匹配）
        icon TEXT DEFAULT '🤖',
        status TEXT DEFAULT 'active',         -- active | draft | disabled
        version TEXT DEFAULT 'v1',
        builtin INTEGER DEFAULT 0,            -- 1=内置（平台预置，禁止删除可编辑）
        agent_role TEXT DEFAULT 'sub',        -- main=主Agent（团队负责人）| sub=子Agent（团队成员）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS agent_tools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        tool_type TEXT NOT NULL,              -- skill | mcp | tool
        tool_name TEXT NOT NULL,              -- skill 名 / MCP 工具名 / 内置工具名
        enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agent_id, tool_type, tool_name)
    )""")

    # ── AI 设计工坊：主/子 Agent 团队（多对多：一个子 Agent 可属多个团队；两级封顶）──
    c.execute("""CREATE TABLE IF NOT EXISTS agent_team_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        main_agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        sub_agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        enabled        INTEGER DEFAULT 1,
        created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(main_agent_id, sub_agent_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_atm_main ON agent_team_members(main_agent_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ux_atm_sub ON agent_team_members(sub_agent_id)")

    # ── 多Agent 任务队列（Planner-Executor / Manager 编排产物，支持依赖/认领/移交/恢复）──
    c.execute("""CREATE TABLE IF NOT EXISTS agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER DEFAULT 0,             -- 所属流程运行 id（0=独立编排）
        task_key TEXT DEFAULT '',             -- 计划内唯一键（t1/t2...）
        title TEXT DEFAULT '',
        agent_id TEXT DEFAULT '',             -- 指派 Agent（intent 名）
        task_type TEXT DEFAULT 'agent',       -- agent | llm | tool | react
        config TEXT DEFAULT '{}',             -- JSON: 节点级配置（query/prompt/max_steps 等）
        deps TEXT DEFAULT '[]',               -- JSON: 前置任务 key 列表
        status TEXT DEFAULT 'planned',        -- planned|ready|running|done|blocked|failed|canceled
        result TEXT DEFAULT '',               -- 执行结果（summary/content）
        metadata TEXT DEFAULT '{}',           -- JSON: handoff metadata（score/artifacts 等）
        error TEXT DEFAULT '',
        assigned_by TEXT DEFAULT 'planner',   -- planner | orchestrator | manual
        seq INTEGER DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        context TEXT DEFAULT '',              -- P1b-2 委派协议：子任务必需事实（注入子 Agent）
        expected_output TEXT DEFAULT '',      -- P1b-2 委派协议：完成标准（可验证交付物）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_at_run ON agent_tasks(run_id, status)")

    # ── P0-2 持久执行：编排 run 的检查点（LangGraph checkpointer 的最小等价物）──────────
    # ⚠️ 设计口径（勿扩写成"整图状态快照"）：本表**不存**任务结果与 DAG 推进状态 ——
    #   那些已经在 `agent_tasks` 里逐行持久化（status/result/metadata/retry_count 每步 commit）。
    #   本表只存「重进这张图所需的坐标系」：原始用户输入、意图、分支、provider、原始计划、
    #   以及 phase（执行阶段）+ updated_at（心跳）。缺的正是这几项 —— 进程崩了之后，
    #   agent_tasks 还在，但**没人知道**这次 run 原本要跑什么、用哪个 provider、跑到哪一阶段。
    # 为什么必须显式落这句：此前编排既无 run 级 phase、也无输入/ provider 留痕，崩溃现场只能
    #   "整批标记 failed"（批次 1 孤儿回收已做），无法继续 —— 这正是缺 checkpoint 的代价。
    c.execute("""CREATE TABLE IF NOT EXISTS orch_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,              -- 编排批次号（= agent_tasks.run_id）
        conversation_id INTEGER DEFAULT 0,
        phase TEXT DEFAULT 'planned',         -- planned|executing|summarizing|done|failed|interrupted
        user_input TEXT DEFAULT '',           -- 触发本次编排的原始用户输入（恢复时用它重建 goal）
        intent TEXT DEFAULT '',               -- 主意图（决定汇总口径与 Agent 选择）
        branch TEXT DEFAULT '',               -- 工作分支（写库位置，恢复必须同源）
        provider_id INTEGER DEFAULT 0,        -- LLM provider（恢复时保持同模型，避免半程换模型）
        skill_name TEXT DEFAULT '',
        team TEXT DEFAULT '',
        plan_json TEXT DEFAULT '[]',          -- 原始 LLM 计划（保真，便于审计与离线复盘）
        params_json TEXT DEFAULT '{}',        -- 其余调用参数（scope/team_forced/stage_hint 等）
        attempt_count INTEGER DEFAULT 0,      -- resume 次数（每次恢复 +1，防无限自愈循环）
        summary_written INTEGER DEFAULT 0,    -- ① 汇总产物是否已与消息**同事务**落库
                                              --   （0=未落 ⇒ 可安全"仅重做汇总"；1=已落 ⇒ 重做会产生重复消息）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP   -- 心跳：主循环推进时刷新（存活判定依据）
    )""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ock_run ON orch_checkpoints(run_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ock_conv ON orch_checkpoints(conversation_id, updated_at)")

    # ── HIL 人机协作强制：L2 写操作确认队列（未确认前不落库/不生效）──
    c.execute("""CREATE TABLE IF NOT EXISTS hil_confirmations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT DEFAULT '',             -- 发起 Agent（intent 名）
        conversation_id INTEGER DEFAULT 0,
        run_id INTEGER DEFAULT 0,
        action TEXT NOT NULL,                 -- entity_create / relation_create / tool 名
        payload TEXT DEFAULT '{}',            -- JSON: 待执行参数
        preview TEXT DEFAULT '',              -- 展示文案（LLM/执行器生成）
        status TEXT DEFAULT 'pending',        -- pending | approved | rejected
        decided_by TEXT DEFAULT '',
        decided_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_hc_status ON hil_confirmations(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_hc_run ON hil_confirmations(run_id)")

    # ── LLM 调用统计（token 成本核算 + 可观测）──
    c.execute("""CREATE TABLE IF NOT EXISTS llm_usage_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER DEFAULT 0,
        provider_name TEXT DEFAULT '',
        model_name TEXT DEFAULT '',
        intent TEXT DEFAULT '',
        used_mock INTEGER DEFAULT 0,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        -- P1-20 prompt caching 观测：DeepSeek 上下文磁盘缓存命中/未命中 token 数
        -- （usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens，命中按 1/10 价计费）
        prompt_cache_hit_tokens INTEGER DEFAULT 0,
        prompt_cache_miss_tokens INTEGER DEFAULT 0,
        -- P1-27 截断诊断：思考模型的 reasoning 与正文**共享 max_tokens 配额**，
        -- 这两列是「输出被截断 / 为空」的直接证据（stop=正常 / length=被上限截断）。
        finish_reason TEXT DEFAULT '',
        reasoning_tokens INTEGER DEFAULT 0,
        -- P1-1b 观测落库：LLM 重试/回退（此前只在响应 _meta，DB 无记录 → 回退率/重试成功率无法统计）
        retry_count INTEGER DEFAULT 0,          -- 重试次数（0=首次成功；流式不重试恒 0）
        fallback_used INTEGER DEFAULT 0,        -- 1=本次调用发生过 provider 回退
        fallback_provider_id INTEGER DEFAULT 0, -- 回退后实际使用的 provider id（未回退=0）
        estimated_cost REAL DEFAULT 0,        -- 估算成本（美元，仅真实调用）
        latency_ms INTEGER DEFAULT 0,
        -- P0-c（2026-10-03 评估）：trace→span 关联三列。此前本表是**孤立流水** —— LLM 调用
        -- 不知道自己属于哪次会话 / 哪个编排 run，只能按 created_at 猜相邻，无法下钻。
        -- 标杆（Langfuse/LangSmith/Phoenix/Datadog LLM Observability）都以 trace 为第一等模型。
        conversation_id INTEGER DEFAULT 0,    -- 所属会话（0=会话外调用，如意图识别/后台任务）
        run_id INTEGER DEFAULT 0,             -- 所属编排 run（task_runs 表 id；0=非编排调用）
        trace_id TEXT DEFAULT '',             -- 一次请求轮次的链路号（同一请求的多次 LLM 调用同号）
        sub_task_key TEXT DEFAULT '',         -- 编排子任务 key（区分一次 run 内哪个子任务花的钱）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_lus_time ON llm_usage_stats(created_at)")

    # ── 生成历史与反馈 ──
    c.execute("""CREATE TABLE IF NOT EXISTS generation_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER REFERENCES conversations(id),
        gen_type TEXT NOT NULL,  -- requirement | design | impact | review
        title TEXT DEFAULT '',
        element_count INTEGER DEFAULT 0,
        version TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',  -- pending | confirmed | rejected | partial
        confirmed_by TEXT DEFAULT '',
        change_log TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER REFERENCES conversations(id),
        message_id INTEGER REFERENCES messages(id),
        feedback_type TEXT NOT NULL,  -- approve | reject | modify
        context TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 审计日志 ──
    c.execute("""CREATE TABLE IF NOT EXISTS graph_edit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op TEXT NOT NULL,
        node_id TEXT DEFAULT '',
        payload TEXT DEFAULT '{}',
        operator TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_name TEXT DEFAULT '',
        event_type TEXT NOT NULL,
        detail TEXT DEFAULT '',
        result TEXT DEFAULT 'success',  -- success | blocked | failed
        ip_address TEXT DEFAULT '',
        branch TEXT DEFAULT '',         -- 分支归属（图谱/推理等域事件记录操作分支；全局事件为空）
        user_agent TEXT DEFAULT '',     -- P0-a（2026-10-03）审计六要素 Source：UA / request_id
        request_id TEXT DEFAULT '',     --   ↑ 同一请求产生的多条审计可聚合（利于排查一次操作的影响面）
        prev_hash TEXT DEFAULT '',      -- P0-b（2026-10-03）哈希链：上一行摘要（链首为 64 个 0）
        hash TEXT DEFAULT '',           --   ↑ 本行摘要 sha256(prev_hash|各审计字段)，由 core.audit.audit() 写入
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 图谱命名子图（KB-D：筛选/模块/文档组合存为可复用子图视图）──
    c.execute("""CREATE TABLE IF NOT EXISTS graph_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        branch TEXT DEFAULT 'dev',
        config TEXT DEFAULT '{}',          -- JSON: {entity_types, status, hideIsolated, layout, docs}
        builtin INTEGER DEFAULT 0,         -- 内置子图（模块/文档）不可删除
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── P0-3：知识分类（设计方法知识 / 设计资产，AI 消费区分规范约束源与增量设计源）──
    c.execute("""CREATE TABLE IF NOT EXISTS knowledge_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,          -- 子类名（如 设计准则/可复用构件）
        group_name TEXT DEFAULT '设计方法知识',  -- 大类：设计方法知识 | 设计资产
        description TEXT DEFAULT '',
        builtin INTEGER DEFAULT 1,          -- 内置类别不可删除
        sort_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── P0-4：知识发布日志（实体进入 release 权威基线的留痕，生命周期"发布"环节）──
    c.execute("""CREATE TABLE IF NOT EXISTS knowledge_publish_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id TEXT NOT NULL,
        name TEXT DEFAULT '',
        branch TEXT DEFAULT 'release',
        published_at TEXT DEFAULT CURRENT_TIMESTAMP,
        version INTEGER DEFAULT 1,          -- 该实体第几次发布
        merged_from TEXT DEFAULT '',        -- 来源分支（dev）
        created_by TEXT DEFAULT '',
        action TEXT DEFAULT 'publish',       -- FR-KG-16: publish | rollback（合并回滚留痕）
        version_label TEXT DEFAULT '',       -- 分支版本管理：本次发布版本号（v1/v1.1…，人工或自动生成）
        commit_id INTEGER DEFAULT NULL       -- 分支版本管理：关联本次发布的 merge 提交 id
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kpl_entity ON knowledge_publish_logs(entity_id)")

    # ── 系统设置 ──
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        description TEXT DEFAULT '',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 文档（R1=B 2026-09-01：数据集成移除，data_sources 表已删；source_id 列保留防 documents 重构风险）──
    c.execute("""CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        file_type TEXT DEFAULT '',
        file_size INTEGER DEFAULT 0,
        parse_status TEXT DEFAULT 'pending',  -- pending | parsing | completed | failed
        chunk_count INTEGER DEFAULT 0,
        entity_count INTEGER DEFAULT 0,
        quality_score REAL DEFAULT 0,
        source_id INTEGER DEFAULT NULL,   -- R1=B：原 REFERENCES data_sources 已随表删除，SQLite FK 默认关闭无约束
        uploaded_by TEXT DEFAULT '',
        branch TEXT DEFAULT 'global',  -- KB分支：⚠️ 文档为全局资产，恒 'global'（列保留仅为历史兼容）
        knowledge_category TEXT DEFAULT '',  -- P0-3: 知识类别（设计方法知识/设计资产子类，空=未分类）
        summary TEXT DEFAULT '',  -- 2026-09-29 LLM summary (empty => preview falls back)
        -- P1-3 (2026-10-04) segmented OCR checkpoint: JSON {"resume": {"page0": text}, "total": n}
        -- Written when a large scanned PDF exceeds the OCR time budget; the next ingest
        -- resumes from this checkpoint instead of silently dropping the remaining pages
        -- (benchmark: RAGFlow v0.25 segmented parse / Unstructured batching / MinerU per-page).
        ocr_progress TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 缺口A：知识库真管道 —— 文档分块（chunk）与向量持久化 ──
    c.execute("""CREATE TABLE IF NOT EXISTS document_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        chunk_index INTEGER DEFAULT 0,
        content TEXT NOT NULL,              -- 分块文本（命中预览直接返回）
        embedding TEXT DEFAULT '[]',        -- JSON: 向量（bigram TF 或真 embedding）
        embed_version TEXT DEFAULT 'bigram-tf',  -- bigram-tf | openai-compat
        source_doc TEXT DEFAULT '',         -- 冗余文件名，检索展示用
        branch TEXT DEFAULT 'dev',     -- KB分支：分块归属分支（随文档，发布时复制快照）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(document_id)")

    # ── 文档片段驱动 AI 建模：建模范围（文件管理=全局数据，不分分支，快照固化）──
    c.execute("""CREATE TABLE IF NOT EXISTS modeling_scopes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'doc',            -- doc | fragment_group | chunk (G1/G2/G3)
        doc_ids TEXT DEFAULT '[]',                   -- documents.id 列表（JSON）
        doc_names TEXT DEFAULT '[]',                 -- source_doc/filename 清单（检索硬过滤/展示）
        fragment_text TEXT DEFAULT '',               -- G2 片段文本（快照固化）
        chunk_ids TEXT DEFAULT '[]',                 -- G3 {document_id,chunk_index,...} 谱
        meta TEXT DEFAULT '{}',                      -- 扩展（创建人/来源版本等）
        created_by TEXT DEFAULT 'system',
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scopes_name ON modeling_scopes(name)")

    # ── KB-P0：文档元数据管理（追溯 + 元数据字段）──
    c.execute("""CREATE TABLE IF NOT EXISTS doc_metadata (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
        title TEXT DEFAULT '',
        author TEXT DEFAULT '',
        version TEXT DEFAULT 'v1.0',
        tags TEXT DEFAULT '[]',            -- JSON: 标签列表
        source TEXT DEFAULT 'upload',      -- upload | sysml_import | manual
        extra TEXT DEFAULT '{}',           -- JSON: 扩展元数据
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 优化1：工具调用可观测（Agent 真实调用工具的执行日志）──
    c.execute("""CREATE TABLE IF NOT EXISTS tool_call_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intent TEXT DEFAULT '',              -- 路由到的 Agent 意图
        agent_name TEXT DEFAULT '',          -- Agent 展示名
        tool_name TEXT NOT NULL,             -- 工具名（graph_retrieve / MCP 工具名）
        tool_type TEXT DEFAULT '',           -- builtin | mcp | http | zhiyuan | skill
        call_kind TEXT DEFAULT 'tool',       -- 调用类别：tool | skill | mcp（统一可观测）
        arguments TEXT DEFAULT '{}',         -- JSON: 入参
        result TEXT DEFAULT '',              -- 结果摘要（截断）
        ok INTEGER DEFAULT 0,                -- 1=成功 0=失败
        latency_ms INTEGER DEFAULT 0,        -- 调用耗时
        conversation_id INTEGER DEFAULT 0,
        -- P0-2（2026-10-06）：操作者。此前本表**无 user 字段** ⇒ 578 行工具调用
        -- 只能回答「哪个会话调的」，回答不了「**是谁**调的」——
        -- 而后者正是文章讲的「业务方追着问到底是谁改的」那个必答项。
        -- 取值口径同audit_logs.user_name（display_name，回落 username）。
        user_name TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tool_logs_time ON tool_call_logs(created_at)")
    # ⚠️ idx_tool_logs_user（user_name）**不在这里建** —— 本段是 CREATE TABLE IF NOT EXISTS，
    # 老库走该分支时表已存在、**不会加列**，索引建在缺列的表上会直接
    # `no such column: user_name` 让 init_db 整体失败（2026-10-06 实测踩到）。
    # 建索引必须晚于 _migrate_columns 的补列 —— 已移到紧跟其后的位置。

    # ── P1 知识引擎双引擎：查询路由统计（图/向量/混合消费观测）──
    c.execute("""CREATE TABLE IF NOT EXISTS query_routing_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        route TEXT NOT NULL,              -- graph | vector | mixed
        reason TEXT DEFAULT '',           -- 路由原因（graph_confidence_ok / graph_confidence_low_vector_supplement / graph_no_hit ...）
        confidence REAL DEFAULT 0,
        graph_count INTEGER DEFAULT 0,
        vector_count INTEGER DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        branch TEXT DEFAULT 'dev',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── P0-1 平台化：场景模板 / 本体 Profile / 项目（解除星网领域固化）──
    c.execute("""CREATE TABLE IF NOT EXISTS scenario_templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        domain TEXT DEFAULT '',          -- 领域：卫星通信 | 卫星导航 | 汽车热管理 ...
        description TEXT DEFAULT '',
        entities_schema TEXT DEFAULT '[]',    -- JSON: 预置实体类型列表
        relations_schema TEXT DEFAULT '[]',   -- JSON: 预置关系类型列表
        views_schema TEXT DEFAULT '[]',       -- JSON: 预置视图集
        version TEXT DEFAULT 'v1',
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS ontology_profiles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        profile_type TEXT DEFAULT 'domain',  -- domain | project | discipline
        scenario_template_id TEXT REFERENCES scenario_templates(id),
        entities TEXT DEFAULT '[]',      -- JSON: [{type, props:[...]}]
        relations TEXT DEFAULT '[]',     -- JSON: [{type, src, tgt}]
        attributes TEXT DEFAULT '[]',    -- JSON: [attr,...]
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        domain TEXT DEFAULT '',
        description TEXT DEFAULT '',
        scenario_template_id TEXT REFERENCES scenario_templates(id),
        ontology_profile_id TEXT REFERENCES ontology_profiles(id),
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 项目级持久记忆（Project Constitution）：建模规范/设计基线/决策记录，AI 会话每次注入防漂移 ──
    c.execute("""CREATE TABLE IF NOT EXISTS project_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL DEFAULT '',
        category TEXT DEFAULT '规范',          -- 规范 | 基线 | 决策 | 经验
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1,
        created_by TEXT DEFAULT '王工',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ux_pm_proj ON project_memories(project_id, category)")

    # ── 确定性工具钩子（对齐 Claude Code PreToolUse）：工具调用前强制校验，不依赖 LLM 概率 ──
    c.execute("""CREATE TABLE IF NOT EXISTS tool_hooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        tool_pattern TEXT NOT NULL,           -- 工具名匹配：精确名 | 前缀（file_*）| 后缀（*_delete）
        event TEXT DEFAULT 'pre_tool_use',    -- 钩子事件（本轮仅 pre_tool_use）
        action TEXT DEFAULT 'block',          -- block | require_confirm | warn
        condition_args TEXT DEFAULT '',       -- 可选：参数条件 JSON {key, contains|equals}
        message TEXT DEFAULT '',              -- 命中提示文案
        enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_hooks_enabled ON tool_hooks(enabled)")

    # ── 报告中心：AI 建模产出报告（变更影响/预评审/模型分析等）统一归档、检索与导出 ──
    c.execute("""CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        report_type TEXT DEFAULT 'analysis',   -- analysis | impact | review | other
        summary TEXT DEFAULT '',
        sections TEXT DEFAULT '[]',            -- JSON: [{heading, body, table?}]
        source TEXT DEFAULT 'conversation',    -- conversation | flow | skill | upload | manual
        conversation_id INTEGER DEFAULT 0,
        branch TEXT DEFAULT '',
        project_id TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        status TEXT DEFAULT 'draft',           -- draft | final
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reports_type ON reports(report_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at)")

    # ── 会话产物：AI 建模会话内 AI 生成内容（报告/代码/SysML 视图/文档）统一索引与管理 ──
    c.execute("""CREATE TABLE IF NOT EXISTS artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER REFERENCES conversations(id),
        message_id INTEGER DEFAULT 0,
        project_id TEXT DEFAULT '',           -- P1-1（2026-09-28）：**写入时定格**所属工程（空=无工程会话，合法）
        kind TEXT NOT NULL,          -- report | code | sysml | document | other（仅 AI 生成）
        title TEXT DEFAULT '',
        filename TEXT DEFAULT '',
        file_path TEXT DEFAULT '',
        file_url TEXT DEFAULT '',
        mime TEXT DEFAULT '',
        size INTEGER DEFAULT 0,
        preview_type TEXT DEFAULT 'none',  -- none | image | markdown | code | sysml | html
        preview_content TEXT DEFAULT '',
        meta TEXT DEFAULT '{}',            -- JSON：{report_type, sections, sysml_views, version, source_msg_type}
        source TEXT DEFAULT 'conversation',-- conversation | flow | tool | report | manual
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_art_conv ON artifacts(conversation_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_art_kind ON artifacts(kind)")

    # ── AI 建模 SysML 版本链：AI 生成 SysML v2 代码每次生成/修订建档一个版本，采纳版本可入库 ──
    c.execute("""CREATE TABLE IF NOT EXISTS sysml_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artifact_id INTEGER DEFAULT 0,        -- 关联 artifacts(kind=sysml)
        conversation_id INTEGER DEFAULT 0,    -- 来源会话
        message_id INTEGER DEFAULT 0,         -- 来源消息
        project_id TEXT DEFAULT '',           -- P1-1（2026-09-28）：**写入时定格**所属工程（空=无工程会话，合法）
        version_label TEXT NOT NULL,          -- v0.1 / v0.2 / v1.0
        content TEXT DEFAULT '',              -- SysML v2 源码/视图 JSON 快照
        diff TEXT DEFAULT '{}',               -- JSON：相对上一版变更摘要（文本级）
        element_summary TEXT DEFAULT '{}',    -- JSON：{entities:N, relations:N, nodes:[...], edges:[...]}
        parent_id INTEGER DEFAULT NULL,       -- 修订链（上一版本 id）
        status TEXT DEFAULT 'draft',          -- draft | current | committed | superseded
        adopted INTEGER DEFAULT 0,            -- 1=用户标记为最终采纳版本
        imported_batch TEXT DEFAULT '',       -- 入库后回填 v2g_candidates 批次 id
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sv_conv ON sysml_versions(conversation_id, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sv_art ON sysml_versions(artifact_id)")
    # P0-6：版本跟随代码文件——code_text 存该版本 SysML v2 源码（旧库幂等迁移）
    try:
        c.execute("ALTER TABLE sysml_versions ADD COLUMN code_text TEXT DEFAULT ''")
    except Exception:
        pass

    # P1-3（2026-10-04）大文件 OCR 断点续跑：旧库幂等加列（同上写法）。
    # 为什么必须 ALTER 而不只是改 CREATE TABLE：CREATE TABLE IF NOT EXISTS 对**已存在**
    # 的表是**完全跳过**的 ⇒ 存量库（含开发机与客户私有化部署）永远拿不到这一列。
    try:
        c.execute("ALTER TABLE documents ADD COLUMN ocr_progress TEXT DEFAULT ''")
    except Exception:
        pass

    # ── CIA：变更影响分析记录（FR-CIA-3 每次分析结果快照，可追溯）──
    c.execute("""CREATE TABLE IF NOT EXISTS impact_analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT DEFAULT '',               -- 变更源描述
        change_source TEXT DEFAULT '',       -- 变更源名称
        params TEXT DEFAULT '{}',            -- {depth, direction, relation_types, reference_sources, use_vector, branch}
        result TEXT DEFAULT '{}',            -- 完整结果 JSON 快照（nodes/edges/levels/params/err）
        status TEXT DEFAULT 'ok',            -- ok | error
        error_code TEXT DEFAULT '',          -- NO_CHANGE_SOURCE | SOURCE_AMBIGUOUS | ...
        conversation_id INTEGER DEFAULT 0,
        message_id INTEGER DEFAULT 0,
        report_id INTEGER DEFAULT 0,         -- 归档报告关联（报告中心双向追溯）
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_impact_conv ON impact_analyses(conversation_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_impact_created ON impact_analyses(created_at)")

    # ── CIA：变更模拟记录（FR-CIA-4 沙箱预演结果快照，可追溯）──
    c.execute("""CREATE TABLE IF NOT EXISTS impact_simulations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT DEFAULT '',               -- 场景标题
        scene TEXT DEFAULT '',               -- 内置场景 ID / 自定义
        baseline_snapshot TEXT DEFAULT '{}', -- 基线子图快照 {nodes, edges}
        changes TEXT DEFAULT '[]',           -- 预演变更清单 [{op,target,new_value,...}]
        before_result TEXT DEFAULT '{}',
        after_result  TEXT DEFAULT '{}',
        comparison    TEXT DEFAULT '{}',     -- {added, removed, degree_changes, risk, summary}
        status TEXT DEFAULT 'completed',
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sim_created ON impact_simulations(created_at)")

    # ── 架构优化：子任务物化产物（SysML/需求/报告/文档物化产物 + write_request 写操作暂存）──
    c.execute("""CREATE TABLE IF NOT EXISTS subtask_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        task_key TEXT NOT NULL,
        artifact_idx INTEGER DEFAULT 0,
        kind TEXT DEFAULT '',               -- sysml | requirement | report | doc 物化产物 | write_request 写操作暂存
        title TEXT DEFAULT '',
        content_json TEXT DEFAULT '{}',
        materialized INTEGER DEFAULT 0,     -- 0 未物化 1 已物化
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sa_run ON subtask_artifacts(run_id, task_key)")

    # ── 架构优化：意图路由规则配置化（触发关键词 → 路由意图，权重/启停）──
    c.execute("""CREATE TABLE IF NOT EXISTS intent_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger TEXT NOT NULL,              -- 触发关键词
        intent TEXT NOT NULL,               -- 路由意图
        weight REAL DEFAULT 1.0,
        enabled INTEGER DEFAULT 1,          -- 1=启用 0=停用
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()

    # ── 幂等列迁移（老库补 P0-1 新增的 project_id 列，回填默认项目）──
    _migrate_columns(conn)
    # P0-c 追踪索引：**必须在 _migrate_columns 之后** —— 老库走 CREATE TABLE IF NOT EXISTS
    # 不会加列，索引若建在迁移前会 "no such column: conversation_id"（本次启动失败实拍）。
    conn.execute("CREATE INDEX IF NOT EXISTS ux_lus_conv ON llm_usage_stats(conversation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ux_lus_run ON llm_usage_stats(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ux_lus_trace ON llm_usage_stats(trace_id)")
    # ── P0-2（2026-10-06）工具调用日志操作者索引 ──
    # **必须紧跟 _migrate_columns 之后**：老库的 tool_call_logs 已存在，
    # CREATE TABLE IF NOT EXISTS 不加列 ⇒ 索引必须等columns.py 的 _add补完 user_name 才建。
    # （与上方 ux_lus_conv/run/trace 同一约束，此处踩过一次 no such column。）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_logs_user ON tool_call_logs(user_name)")
    # ── P0-2 分支保护规则：branches.protection_rules 内置分支默认值回填（幂等，仅填空）──
    _migrate_branch_protection(conn)
    # ── P0-6（2026-10-06）Agent 工具链 RBAC + HIL 批准授权 ──
    # 必须**早于种子执行**：新库的角色行由 _seed_* 写入，若本迁移跑在种子之前，
    # 新角色不会被授权（老库则因已落库、种子"存在即跳过"而必须靠本迁移补）。
    # 详见 database/migrations/permissions.py 顶部「加门必须与授权同批交付」的说明。
    _migrate_agent_tool_perm(conn)
    # ── 分支管理 GitHub 对标：merge_requests.status 旧枚举 → 新状态机（幂等）──
    _migrate_mr_status(conn)
    # ── 工坊四模块内置标识（skills/agents/mcp_servers/tools.builtin，内置禁删可编辑）──
    # 必须早于种子执行：_seed_builtin_tools/_seed_agents 的 INSERT 已写入 builtin 列
    _migrate_builtin_flags(conn)
    # ── AI 设计工坊：主/子 Agent 团队（agents.agent_role + agent_team_members 表）──
    # 必须早于种子执行：_seed_agents 的 INSERT 依赖 agent_role 列（DEFAULT 'sub'）
    _migrate_agent_team(conn)
    # ── 实体版本化：entities 单列主键 → (id, branch) 复合主键（老库重建）──
    _rebuild_entities_pk(conn)
    # ── 修复 relations 失效外键（RENAME 迁移把 FK 改写为已删除的 entities_old）──
    _repair_relations_fk(conn)
    # ── 发布分支改名：release/v1.2 → release（级联更新所有引用）──
    _rename_release_branch(conn)
    # ── 主开发分支改名：dev → dev（级联更新 + 默认三分支 release/dev/personal）──
    _rename_dev_branch(conn)
    # ── KB-P2：图谱编辑日志表 ──
    _migrate_graph_tables(conn)
    # ── O-1：v2g 候选表 ──
    _migrate_v2g_tables(conn)
    # ── O-3：SysML 导入批次 + 映射表 ──
    _migrate_sysml_tables(conn)
    # ── 工程维度入库批次记录表（工程归档 → 三元组 → 个人分支图库）──
    _migrate_project_ingest_logs(conn)
    # ── 词典概念变更留痕表（P0 治理闭环）──
    _migrate_glossary_changelog(conn)
    _migrate_glossary_discoveries(conn)
    # ── P0：文件生命周期管理 + 节点/边元数据（资料库与AI建模优化 2026-09-10）──
    _migrate_document_lifecycle(conn)
    # ── 文档目录树 doc_folders + documents.folder_id（基于文件的管理 2026-09-21 §4.1）──
    _migrate_doc_folders(conn)
    # ── P1-3：SysML Profile 导入元信息表 ──
    _migrate_profile_tables(conn)
    # ── O-3：老库补齐本体类型 ──
    _ensure_ontology_types(conn)
    # ── 状态回填：老文档 pipeline_detail（失败环节可观测）──
    _backfill_pipeline_detail(conn)
    # ── 种子数据（仅空库时执行，用户数据永不被 demo 覆盖）──
    _seed(conn)
    # ── 项目/场景/本体种子（projects 表空时执行，老库升级也能获得）──
    _seed_projects(conn)
    # ── Agent 注册表种子（agents 表空时执行，老库升级也能获得）──
    _seed_agents(conn)
    # ── TR-P2a：内置工具迁库（无条件执行，INSERT OR IGNORE 幂等；老库升级获得）──
    _seed_builtin_tools(conn)
    # ── 工具启停状态归一：deprecated/inactive → disabled（启用/停用两态，替代弃用语义）──
    _migrate_tool_status(conn)
    # ── 内置工具 input_schema ↔ 执行器对齐：必须晚于 _seed_builtin_tools（行已存在才能订正）──
    #    修的是「LLM 按 schema 填 A、执行器读 B」的静默错配，见 plugins.py 顶部说明
    _migrate_builtin_tool_schemas(conn)
    # ── AI 设计工坊：Skill/MCP/工具 插件模式（scope/source_ref/pinned/install_count）──
    # 必须晚于种子：_seed_builtin_tools 已建内置工具，回填 builtin → scope='public'
    _migrate_plugin_scope(conn)
    # ── 个人插件分享审核：share_status + 审批记录表（技能/MCP）──
    _migrate_share_review(conn)
    # ── 插件来源分类：admin（管理员创建/直接发布）| share（个人分享审核通过）──
    _migrate_plugin_origin(conn)
    # ── P0 能力底座：项目级持久记忆 + 确定性工具钩子（老库升级幂等补建）──
    _migrate_p0_capabilities(conn)
    # ── 环境变量注入 API key（每次启动执行，仅补无 key 的 provider）──
    _apply_env_keys(conn)
    # ── 文件管理实体/关系抽取配置（上传自动抽取开关默认关 + 实体候选来源默认 sysml）──
    _migrate_file_extract_settings(conn)
    # ── 文件管理全局化：文档从分支体系抽离为全局资产（存量快照去重 + 分支统一 global）──
    _migrate_docs_global(conn)
    # ── AI 设计工坊 · 统一插件体系（P0）：8 表 + 审核策略（与存量三表并行，旧体系兼容）──
    _migrate_plugin_tables(conn)
    # ── 能力依赖索引：consumer→provider 有向图，支撑卸载前置检查与影响面分析（P0-2）──
    _migrate_plugin_dependencies(conn)
    # ── Glossary 术语表 + 查询 Trace + domain review 队列（P0-1/P2-2/P2-3）──
    _migrate_glossary_tables(conn)
    # ── 复核队列孤儿行兜底清理（document_id 无外键，删文档不级联；幂等）──
    _migrate_domain_review_queue_orphans(conn)
    # ── 修复 documents 失效外键（data_sources 重建的 RENAME 副作用 → data_sources_old 引用）──
    _repair_documents_fk(conn)
    # ── FR-KG-4 补 G7：本体类型变更留痕表（add/update/delete 全量快照）──
    _migrate_ontology_change_tables(conn)
    # ── 本体版本管理：本体整体 SemVer 版本链（每次 Schema 变更自动递增）──
    _migrate_ontology_version_tables(conn)
    # ── P0-1 数据治理：ontology_types.name 去重 + UNIQUE 索引（须在种子之后、IRI 回填之前）──
    _dedupe_ontology_types(conn)
    # ── P0-1：本体 IRI 主轴（ontology_meta + 存量类型 IRI 回填，须在补列/建类型之后）──
    _migrate_ontology_iri(conn)
    # ── FR-KG-1 补 G15：抽取质量评估报告表（golden set 比对 P/R/F1）──
    _migrate_eval_tables(conn)
    # ── KB v2 增强：归一化/冲突消解/Golden Set/本体蓝图表（对标知识图谱平台 v2.0）──
    _migrate_kb_v2_enhance(conn)
    # ── 2026-09-26 意图识别样本池（把硬编码评测集搬进库，设置页可维护；评测只消费 confirmed）──
    _migrate_intent_samples(conn)
    # ── 2026-09-26 知识看板指标快照（P2：值+状态的历史点，供 sparkline 趋势）──
    _migrate_dashboard_snapshots(conn)
    # ── 2026-09-30 移除 agent_tools.params 废列（只有写入无读取，UI 从不传；绑定关系保留）──
    _migrate_drop_agent_tools_params(conn)
    # ── P1-4（2026-10-02）工具结果 offload 表（Tier1 大响应可寻址召回）──
    _migrate_tool_result_offloads(conn)
    # ── P0-a/P0-b（2026-10-03）审计溯源 + 哈希链（评估 P0）──
    _migrate_audit_chain(conn)
    # ── P0-④（2026-09-11）时态管理：双时态列 + 索引 + 视图 + W3C Time 对齐表 ──
    _migrate_entity_temporal(conn)
    # ── P1-①（2026-09-11）SWRL 规则管理表：swrl_rules + inferred_facts（推理产出暂存）──
    _migrate_swrl_tables(conn)
    # ── 2026-09-14 本体变更→实例迁移计划表（impact-preview / 发布挂钩 / 悬空补迁）──
    _migrate_ontology_instance_migrations(conn)
    # ── 2026-09-15 AI 产物收编资料库（origin 溯源 / 块级过滤 / superseded 时效链）──
    _migrate_artifact_ingest(conn)
    # ── P0 Staging 写前融合改造：v2g_candidates 三义拆分 + Mention/Canonical 双层模型 ──
    _migrate_staging_tables(conn)
    # ── 部门设置种子（用户归属部门受控词表，老库升级也能获得）──
    _seed_departments(conn)
    # ── P0-3/P0-4：知识分类种子 + 生命周期发布回填（幂等）──
    _seed_knowledge_categories(conn)
    # ── 图谱来源信息回填（候选确认入库链路未透传 source_doc，幂等只补空）──
    _backfill_graph_source_info(conn)
    # ── 分支版本管理：存量分支基线提交（幂等）──
    _backfill_commit_baseline(conn)
    # ── 现有文档 domain 迁移（P0-2：按文件名规则回填）──
    _backfill_domains(conn)
    # ── 知识治理全链路优化：三元组表 + V2 候选待审核闸门列 ──
    _migrate_triple_optimization(conn)
    # ── 推理物化审核门禁批次（reasoning_cohorts / reasoning_cohort_items，幂等）──
    _migrate_reasoning_cohorts(conn)
    # ── MR 评审意见留痕表（approve/reject/评论全量时间线，幂等）──
    _migrate_mr_comments(conn)
    # ── P0-2 视图布局优化：布局质量检查存档表（SRS-GN-MG-BJYH 证据链，幂等）──
    _migrate_view_layout_checks(conn)
    # ── P0-4 数据源注册表（db/api/file 三类源，幂等）──
    _migrate_data_sources(conn)
    # ── Glossary 种子（空表时插入预置术语）──
    _seed_glossary(conn)
    # 2026-10-04：意图路由规则集入库（此前只在运行库里，干净库 0 行 ⇒ CI 无法复现生产路由）
    _seed_intent_rules(conn)
    conn.close()


