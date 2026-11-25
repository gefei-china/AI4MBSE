# -*- coding: utf-8 -*-
"""图谱命名子图：模块/来源文档分组的纯函数计算（单一事实源，供接口与单元测试复用）。

语义对齐前端「🗂 子图」下拉：
- 模块子图：按本体 entity 类型 parent_id 求顶层根 → 每根 DFS 收集子孙类型 = 一个语义模块；
  同时把「拥有成员的二级类型」也列为模块（对齐系统分解：卫星系统/有效载荷/地面段…），粒度更实用。
- 来源文档子图：按实体 source_doc 分组计数（空来源归「未标注来源」）。

纯函数设计：不依赖数据库连接，输入为行数据（dict/Row 均可），便于单元测试覆盖
「数据量变化时计数依然准确」的场景（新增/删除/改类型/空模块剔除）。
"""


def module_groups(ont_types, entity_types):
    """计算模块子图分组与计数。

    Args:
        ont_types: 本体类型行列表，每行需含 id / name / type_kind / parent_id。
                   通常来自 ontology_types 表（含 entity/relation/attribute 三类）。
        entity_types: 实体类型名列表（每个实体一行，允许重复），来自 entities 表。

    Returns:
        [{"name": 模块名, "types": [类型名...], "count": 实体计数}, ...]
        仅返回 count>0 的模块（有成员才展示），按顶层根排序后二级类型紧随其后。
    """
    ents = [t for t in ont_types if t["type_kind"] == "entity"]
    by_id = {t["id"]: t for t in ents}

    def is_root(t):
        pid = t["parent_id"]
        if not pid:
            return True
        p = by_id.get(pid)
        return p is None or p["type_kind"] != "entity"

    roots = sorted([t for t in ents if is_root(t)], key=lambda t: t["name"])

    def subtree(pid):
        """pid 类型及其全部子孙类型名（DFS，防环去重）。"""
        members = [by_id[pid]["name"]] if pid in by_id else []
        for t in ents:
            if t["parent_id"] == pid:
                name = t["name"]
                if name not in members:
                    members.append(name)
                for m in subtree(t["id"]):
                    if m not in members:
                        members.append(m)
        return members

    groups = []
    for r in roots:
        groups.append({"name": r["name"], "types": subtree(r["id"])})
        children = sorted(
            [t for t in ents if t["parent_id"] == r["id"]],
            key=lambda t: t["name"])
        for c in children:
            groups.append({"name": c["name"], "types": subtree(c["id"])})

    for g in groups:
        ts = set(g["types"])
        g["count"] = sum(1 for et in entity_types if et in ts)
    return [g for g in groups if g["count"] > 0]


def doc_groups(entity_rows):
    """按来源文档分组计数。

    Args:
        entity_rows: 实体行列表，每行需含 source_doc（可为空）。

    Returns:
        [{"name": 文档名（空归「未标注来源」）, "count": 实体数}, ...] 按计数降序。
    """
    m = {}
    for e in entity_rows:
        m[e["source_doc"] or "未标注来源"] = m.get(e["source_doc"] or "未标注来源", 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(m.items(), key=lambda kv: -kv[1])]
