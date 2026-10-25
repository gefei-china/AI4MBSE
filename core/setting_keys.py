# -*- coding: utf-8 -*-
"""settings 表动态配置键的**唯一权威登记处**（白名单）。

背景（2026-10-23 设置模块数据审计）：
  settings 表曾有 5 个"零消费点"键（query_route / graph_confidence_threshold /
  max_conversation_context / legacy_migrate / system_name）—— 界面能改、改了不生效。
  数据与种子已清（见 docs/设置模块数据审计报告-2026-10-23.md），但
  `PUT /api/settings/{key}` 与 `PUT /api/system/config/runtime` 两个通用写入入口
  **原本无任何校验**，随时能把无效键写回来。

本模块收口"哪些键允许经通用入口写入"：
  · 收录标准 = 该键在代码中有**真实消费点**（读取表达式），经审计脚本逐键确认；
  · staging_fuse.* 为前缀读取（LIKE 'staging_fuse.%'），整前缀放行；
  · memory.* 两个键有消费点、DB 暂无行（走代码默认值），放行即开放界面调节能力。

防腐烂（与本工程"同一语义多处各写一份必然漂移"教训同源）：
  审计脚本 tools/verify/verify_settings_data_audit.py 的 --strict 模式会反向校验
  本白名单 —— 白名单中的键若失去消费点，门禁报错；DB 中出现白名单外的键，同样报错。
  ⇒ 新增 settings 键的开发者会立刻被提醒"来本模块登记"。
"""
from __future__ import annotations

# 精确键：有真实消费点（审计确认）
EXACT_KEYS = frozenset({
    "default_branch",             # routers/graph_workspace.py _cohort_default_branch
    "default_project_id",         # repositories/project_repo.py 写入归属唯一来源
    "entity_candidate_source",    # vector2graph.py _entity_candidate_source
    "file_auto_extract_enabled",  # knowledge_pipeline/ingest.py 上传自动抽取开关
    "plugin_review_policy",       # routers/plugins.py 公共插件审核策略
    "shacl_gate_mode",            # ingest_gate.py GATE_MODE_KEY
    "triple_only_writer",         # vector2graph.py 三元组唯一写入源
    "memory.project_memory_enabled",  # agent/pipeline_parts/memory.py 项目记忆注入开关
    "memory.project_memory_chars",    # agent/pipeline_parts/memory.py 项目记忆注入预算
})

# 前缀键：staging_fuse 融合闸配置（staging_fuse.py get/set_fuse_config，
# 读取形态 LIKE 'staging_fuse.%'；静态扫描不到变量拼接，故整前缀放行）
KEY_PREFIXES = ("staging_fuse.",)


def is_valid_setting_key(key: str) -> bool:
    """通用写入入口（/api/settings/{key}、/api/system/config/runtime）的准入判据。"""
    if not key or not isinstance(key, str):
        return False
    if key in EXACT_KEYS:
        return True
    return any(key.startswith(p) for p in KEY_PREFIXES)


def valid_keys_hint() -> list:
    """错误信息用：合法键清单（精确键 + 前缀说明），便于调用方自查。"""
    return sorted(EXACT_KEYS) + [p + "*（前缀放行）" for p in KEY_PREFIXES]
