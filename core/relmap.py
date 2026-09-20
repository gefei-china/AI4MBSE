# -*- coding: utf-8 -*-
"""关系名 canonical 归一（P1-2 深化，2026-09-06）。

背景：本体 relation 类型中英文并存（`包含`/CONTAINS、`满足`/SATISFIES、
`连接`/CONNECTS、`派生`/DERIVES、`追溯`/TRACE、`子类`/GENERALIZATION），
SHACL 形状生成双套约束、推理公理分裂、SysML 导入产出与本体现行约束脱节。
数据侧（relations 表 122 条）100% 使用英文标准名 → 英文为 canonical。

单一事实来源规则：
- 一切写库路径（sysml_importer / vector2graph / v2g 审批 / API 直写）在
  落 relations.relation_type 前必须经 canonical() 归一
- 归一映射由 ontology_types 迁移（migrate_rel_normalize.py）保证与数据层一致：
  中文类型 status='deprecated' + constraints.replaced_by 指向 canonical

废弃（deprecated，无映射）：属于 / 执行 / 供给电能 / 包含功能 / 地面站功能
——语义无英文对应或名称本身疑似类型误录，0 数据使用；不强行映射以免
错误召回。后续若需"执行"类语义（功能执行），建议走 REALIZES/USES 并人工确认。
"""
from __future__ import annotations

# 中文/旧名 → 英文 canonical（值 None = 无映射，仅废弃）
REL_CANONICAL: dict[str, str | None] = {
    "包含": "CONTAINS",
    "包含功能": None,       # src=卫星 tgt=功能，语义并入 CONTAINS 需人工确认，暂废弃
    "满足": "SATISFIES",
    "连接": "CONNECTS",
    "派生": "DERIVES",
    "追溯": "TRACE",
    "子类": "GENERALIZATION",
    "属于": None,           # 语义≈CONTAINS 反向，无数据使用，废弃
    "执行": None,           # 候选语义 REALIZES/USES 未定，废弃
    "供给电能": None,
    "地面站功能": None,     # 疑似类型名误录为关系
}

# canonical 白名单：relations.relation_type 允许落库的英文名（本体已声明的 15 个）
REL_ALLOWED = {
    "ALLOCATED_TO", "ASSOCIATED_WITH", "CONFLICTS", "CONNECTS", "CONTAINS",
    "DEPENDS_ON", "DERIVES", "FLOW_TO", "GENERALIZATION", "REALIZES",
    "REFERENCES", "SATISFIES", "TRACE", "USES", "VERIFIED_BY",
}


def canonical(name: str | None) -> str | None:
    """关系名 → canonical。已是 canonical/未知名原样返回（宽松策略：
    本体新增英文类型不依赖本表更新）；中文废弃名返回 None（调用方拒绝落库）。"""
    if not name:
        return name
    n = str(name).strip()
    if n in REL_CANONICAL:
        return REL_CANONICAL[n]
    return n


def is_deprecated(name: str | None) -> bool:
    """是否为已废弃（无映射）的中文关系名。"""
    return name is not None and str(name).strip() in REL_CANONICAL \
        and REL_CANONICAL[str(name).strip()] is None
