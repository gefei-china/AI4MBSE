"""SQLite database schema and initialization for MBSE AI system.

解耦拆分（2026-08）：原单体 1549 行 → 包结构，对外导入契约不变（get_db/db_conn/init_db/_seed_builtin_tools...）：
- connection.py  连接管理（get_db / db_conn）
- schema.py      init_db 建表 + 编排迁移与种子
- migrations.py  迁移函数（_migrate_*/_rebuild_*/_repair_*...）
- seeds.py       种子数据（_seed/_seed_projects/_seed_agents/_seed_builtin_tools...）
"""
from .connection import get_db, db_conn
from .schema import init_db
from .migrations import (
    _rebuild_entities_pk,
    _repair_relations_fk,
    _repair_documents_fk,
    _rename_release_branch,
    _rename_dev_branch,
    _migrate_columns,
    _ensure_ontology_types,
    _backfill_pipeline_detail,
    _migrate_graph_tables,
    _migrate_v2g_tables,
    _migrate_sysml_tables,
    _apply_env_keys,
    _migrate_glossary_tables,
    _migrate_ontology_iri,
    _dedupe_ontology_types,
    _migrate_project_ingest_logs,
    _migrate_glossary_changelog,
    _migrate_glossary_discoveries,
)
from .seeds import (
    _seed,
    _seed_projects,
    _seed_agents,
    _seed_builtin_tools,
    _backfill_domains,
    _seed_glossary,
)
from core.config import DB_PATH

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
