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
    # ── P0-2（2026-10-26）：上下文注入精度旋钮（消费点 = agent/pipeline_parts/context.py
    #    检索注入多样性 / agent/pipeline_parts/memory.py 记忆单一真源 / tools.py JIT 回退）──
    # 这些是**本轮修复项的行为开关**：置回旧行为即可一键回滚，无需改代码。
    "context_audit.enabled",                 # agent/context_audit.py 分块归因总开关
    "context_audit.retrieval_per_doc_max",   # context.py 同一 source_doc 最多占几个槽
    "context_audit.retrieval_merge_neighbor",  # context.py 同文档近邻 chunk 是否合并
    "context_audit.memory_single_source",    # memory.py 是否以 rag.memory_hits 为唯一真源
    "context_audit.memory_max_chars",        # 记忆注入正文截断字符数
    "context_audit.jit_fallback_top_n",      # tools.py JIT 空回退时的 top-N（0=退回全量）
    "context_audit.mcp_meta_enabled",         # tools.py 是否用 MCP 真实描述+参数契约（tool_meta）
    "context_audit.mcp_min_keep",             # tools.py 每个 MCP server 至少暴露的工具数
    "context_audit.mcp_top_n",                # tools.py 每个 MCP server 暴露总量上限（0=不限）
    "context_audit.role_manual_filter",       # prompt.py role 块内工具手册段按需裁剪（默认关）
    "context_audit.mcp_exempt_skill_whitelist",  # tools.py MCP 绑定工具豁免技能白名单（默认开）
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
