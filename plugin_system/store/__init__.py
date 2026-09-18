"""插件数据访问层：plugins 8 表 CRUD + 状态机 + 授权过滤（对齐设计方案 §4.4/§5.3）。

状态机（对齐 §4.4）：
    draft → private           个人发布（仅自己可见可用）
    draft/private → submitted 提交共享（进审核流）
    submitted → published     审核通过（公共市场）
    submitted → rejected      审核拒绝（退回 draft）
    published → disabled → enabled   管理员停用/恢复
    published/private → deprecated → removed  下架/软删除（审计保留）
"""

from plugin_system.store.base import (
    EDITABLE_STATUSES,
    PLUGIN_DIR,
    SCOPES,
    SCOPE_LABELS,
    SHARE_TRANSITIONS,
    STATUS_LABELS,
    TRANSITIONS,
    _SELF_PUBLISHABLE_FROM,
    _ensure_plugin_dir,
    _is_owner,
    _now,
    can_transition,
    is_admin,
    is_market_admin,
)

from plugin_system.store.queries import (
    _sync_install_count,
    get_plugin,
    get_plugin_by_name,
    grant_visible,
    install_counts,
    list_market,
    list_mine,
    plugin_dto,
)

from plugin_system.store.crud import (
    _category_of,
    create_plugin,
    update_plugin,
)

from plugin_system.store.dependencies import (
    _sync_deps_safe,
    _sync_legacy_safe,
    dependency_impact,
    list_dependencies,
    list_dependents,
    sync_dependencies,
)

from plugin_system.store.lifecycle import (
    apply_share,
    cancel_share,
    publish_self,
    review_share,
    transition,
    unpublish_self,
    withdraw_share,
)

from plugin_system.store.installs import (
    grant,
    install,
    set_enabled,
    set_install_enabled,
    soft_delete,
    uninstall,
)

from plugin_system.store.audit import (
    list_audit,
    list_call_logs,
    list_grants,
    list_reviews,
    list_versions,
    log_audit,
    log_call,
    pending_reviews,
)

from plugin_system.store.files import (
    read_server_json,
    read_skill_md,
    save_server_json,
    save_skill_md,
)

from plugin_system.store.visibility import (
    _LEGACY_TABLE_ALIAS,
    consumable_filter,
    consumable_plugin_ids,
    legacy_mapping,
)

from plugin_system.store.projection import (
    _SKILL_JSON_FIELDS,
    agent_entry_from_plugin,
    mcp_entry_from_plugin,
    plugin_ids_of_types,
    plugin_manifest,
    prompt_entry_from_plugin,
    skill_entry_from_plugin,
    tool_entry_from_plugin,
)

from plugin_system.store.integrity import (
    count_install_drift,
    reconcile_integrity,
)

__all__ = [
    "EDITABLE_STATUSES",
    "PLUGIN_DIR",
    "SCOPES",
    "SCOPE_LABELS",
    "SHARE_TRANSITIONS",
    "STATUS_LABELS",
    "TRANSITIONS",
    "_SELF_PUBLISHABLE_FROM",
    "_ensure_plugin_dir",
    "_is_owner",
    "_now",
    "can_transition",
    "is_admin",
    "is_market_admin",
    "_sync_install_count",
    "get_plugin",
    "get_plugin_by_name",
    "grant_visible",
    "install_counts",
    "list_market",
    "list_mine",
    "plugin_dto",
    "_category_of",
    "create_plugin",
    "update_plugin",
    "_sync_deps_safe",
    "_sync_legacy_safe",
    "dependency_impact",
    "list_dependencies",
    "list_dependents",
    "sync_dependencies",
    "apply_share",
    "cancel_share",
    "publish_self",
    "review_share",
    "transition",
    "unpublish_self",
    "withdraw_share",
    "grant",
    "install",
    "set_enabled",
    "set_install_enabled",
    "soft_delete",
    "uninstall",
    "list_audit",
    "list_call_logs",
    "list_grants",
    "list_reviews",
    "list_versions",
    "log_audit",
    "log_call",
    "pending_reviews",
    "read_server_json",
    "read_skill_md",
    "save_server_json",
    "save_skill_md",
    "_LEGACY_TABLE_ALIAS",
    "consumable_filter",
    "consumable_plugin_ids",
    "legacy_mapping",
    "_SKILL_JSON_FIELDS",
    "agent_entry_from_plugin",
    "mcp_entry_from_plugin",
    "plugin_ids_of_types",
    "plugin_manifest",
    "prompt_entry_from_plugin",
    "skill_entry_from_plugin",
    "tool_entry_from_plugin",
    "count_install_drift",
    "reconcile_integrity",
]
