"""database.migrations 包：原 database/migrations.py（2168 行 / 49 个顶层函数）纯搬运拆分。

对外导入契约不变：`from database.migrations import X` 与 `from .migrations import X` 照旧可用。
各子模块按功能域内聚：
- rebuild.py  表结构重建/外键修复/分支重命名（entities PK、relations/documents FK、release/dev 分支）
- columns.py  通用列补齐迁移（_migrate_columns）
- ontology.py  本体/语义层迁移（类型、变更表、版本表、去重、IRI、时态、SWRL、推理队列、实例迁移）
- graph.py  图谱与向量层迁移（pipeline 明细回填、graph/v2g 表、来源信息回填、三元组优化）
- documents.py  文档/产物链路迁移（staging、SysML、profile、入库日志、抽取设置、全局文档、生命周期、目录树、产物收编）
- glossary.py  词表与种子数据（术语表、变更日志、发现项、部门、知识分类）
- plugins.py  插件/工具/Agent 能力域迁移（内置标记、工具状态、团队、作用域、共享评审、来源、P0 能力、插件表与依赖、legacy 引用）
- misc.py  杂项迁移（MR 状态/评论、环境变量 key、评测表、知识库 v2 增强、提交基线回填）
"""

from .rebuild import (
    _rebuild_entities_pk,
    _repair_relations_fk,
    _rename_release_branch,
    _repair_documents_fk,
    _rename_dev_branch,
)
from .columns import (
    _migrate_columns,
    _migrate_branch_protection,
)
from .ontology import (
    _ensure_ontology_types,
    _migrate_ontology_change_tables,
    _migrate_ontology_version_tables,
    _dedupe_ontology_types,
    _migrate_ontology_iri,
    _migrate_entity_temporal,
    _migrate_swrl_tables,
    _migrate_reasoning_cohorts,
    _migrate_ontology_instance_migrations,
)
from .graph import (
    _backfill_pipeline_detail,
    _migrate_graph_tables,
    _migrate_v2g_tables,
    _backfill_graph_source_info,
    _migrate_triple_optimization,
)
from .documents import (
    _migrate_staging_tables,
    _migrate_sysml_tables,
    _migrate_profile_tables,
    _migrate_file_extract_settings,
    _migrate_docs_global,
    _migrate_doc_folders,
    _migrate_project_ingest_logs,
    _migrate_document_lifecycle,
    _migrate_artifact_ingest,
)
from .glossary import (
    _migrate_glossary_tables,
    _migrate_domain_review_queue_orphans,
    _seed_departments,
    _seed_knowledge_categories,
    _migrate_glossary_changelog,
    _migrate_glossary_discoveries,
)
from .plugins import (
    _migrate_builtin_flags,
    _migrate_tool_status,
    _migrate_agent_team,
    _migrate_plugin_scope,
    _migrate_share_review,
    _migrate_plugin_origin,
    _migrate_p0_capabilities,
    _migrate_plugin_tables,
    _migrate_plugin_dependencies,
    _migrate_legacy_refs,
    _migrate_builtin_tool_schemas,
)
from .misc import (
    _migrate_mr_status,
    _apply_env_keys,
    _migrate_eval_tables,
    _migrate_kb_v2_enhance,
    _backfill_commit_baseline,
    _migrate_mr_comments,
    _migrate_view_layout_checks,
    _migrate_data_sources,
    _migrate_intent_samples,
    _migrate_dashboard_snapshots,
    _migrate_drop_agent_tools_params,   # 2026-09-30 移除 agent_tools.params 废列（需 SQLite≥3.35）
    _migrate_tool_result_offloads,      # 2026-10-02 工具结果 offload 表（P1-4 Tier1 可寻址召回）
)

__all__ = [
    "_rebuild_entities_pk",
    "_repair_relations_fk",
    "_rename_release_branch",
    "_repair_documents_fk",
    "_rename_dev_branch",
    "_migrate_columns",
    "_ensure_ontology_types",
    "_migrate_ontology_change_tables",
    "_migrate_ontology_version_tables",
    "_dedupe_ontology_types",
    "_migrate_ontology_iri",
    "_migrate_entity_temporal",
    "_migrate_swrl_tables",
    "_migrate_reasoning_cohorts",
    "_migrate_ontology_instance_migrations",
    "_backfill_pipeline_detail",
    "_migrate_graph_tables",
    "_migrate_v2g_tables",
    "_backfill_graph_source_info",
    "_migrate_triple_optimization",
    "_migrate_staging_tables",
    "_migrate_sysml_tables",
    "_migrate_profile_tables",
    "_migrate_file_extract_settings",
    "_migrate_docs_global",
    "_migrate_doc_folders",
    "_migrate_project_ingest_logs",
    "_migrate_document_lifecycle",
    "_migrate_artifact_ingest",
    "_migrate_glossary_tables",
    "_migrate_domain_review_queue_orphans",
    "_seed_departments",
    "_seed_knowledge_categories",
    "_migrate_glossary_changelog",
    "_migrate_glossary_discoveries",
    "_migrate_builtin_flags",
    "_migrate_tool_status",
    "_migrate_agent_team",
    "_migrate_plugin_scope",
    "_migrate_share_review",
    "_migrate_plugin_origin",
    "_migrate_p0_capabilities",
    "_migrate_plugin_tables",
    "_migrate_plugin_dependencies",
    "_migrate_legacy_refs",
    "_migrate_builtin_tool_schemas",
    "_migrate_mr_status",
    "_apply_env_keys",
    "_migrate_eval_tables",
    "_migrate_kb_v2_enhance",
    "_backfill_commit_baseline",
    "_migrate_mr_comments",
    "_migrate_view_layout_checks",
    "_migrate_data_sources",
    "_migrate_dashboard_snapshots",
    "_migrate_drop_agent_tools_params",
    "_migrate_tool_result_offloads",
]
