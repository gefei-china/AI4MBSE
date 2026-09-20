# -*- coding: utf-8 -*-
"""单元测试：图谱命名子图模块分组与计数（tests/test_graph_subgraph.py）

覆盖（对齐前端「🗂 子图」下拉的模块/来源文档分组）：
1. 模块结构：顶层根 + 二级类型模块，子树类型集合正确（卫星系统 → 卫星平台/有效载荷/通信载荷/转发器…）
2. 计数准确：模块 count = 该模块类型集合命中的实体数
3. 数据量变化：新增实体 → count+1；删除实体 → count-1；实体改类型 → 计数迁移到新模块
4. 空模块剔除：有类型无实体 → 不返回该模块
5. 同类型多实体计数累计（不按类型去重）
6. 来源文档分组：source_doc 计数、空来源归「未标注来源」、降序排列、数据变化实时反映

用法（无 pytest 依赖，直接运行）：
    python tests/test_graph_subgraph.py
环境有 pytest 时亦可：pytest tests/test_graph_subgraph.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from subgraph_views import module_groups, doc_groups  # noqa: E402

RESULTS = []


def _t(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, "EXC %r" % (e,)))


# ── 测试数据：卫星通信本体 entity 类型树（含 relation/attribute 混排，验证过滤） ──
def _ont():
    return [
        {"id": 1, "name": "系统元素", "type_kind": "entity", "parent_id": None},
        {"id": 2, "name": "卫星系统", "type_kind": "entity", "parent_id": 1},
        {"id": 3, "name": "卫星平台", "type_kind": "entity", "parent_id": 2},
        {"id": 4, "name": "有效载荷", "type_kind": "entity", "parent_id": 2},
        {"id": 5, "name": "通信载荷", "type_kind": "entity", "parent_id": 4},
        {"id": 6, "name": "转发器", "type_kind": "entity", "parent_id": 5},
        {"id": 7, "name": "天线", "type_kind": "entity", "parent_id": 5},
        {"id": 8, "name": "地面段", "type_kind": "entity", "parent_id": 1},
        {"id": 9, "name": "信关站", "type_kind": "entity", "parent_id": 8},
        {"id": 10, "name": "需求", "type_kind": "entity", "parent_id": None},
        {"id": 11, "name": "系统需求", "type_kind": "entity", "parent_id": 10},
        {"id": 12, "name": "单元需求", "type_kind": "entity", "parent_id": 10},
        {"id": 13, "name": "功能", "type_kind": "entity", "parent_id": None},
        {"id": 14, "name": "用例", "type_kind": "entity", "parent_id": None},
        {"id": 15, "name": "验证活动", "type_kind": "entity", "parent_id": None},
        {"id": 16, "name": "CONTAINS", "type_kind": "relation", "parent_id": None},
        {"id": 17, "name": "EIRP", "type_kind": "attribute", "parent_id": None},
    ]


def _m(ents):
    """模块名 → 模块 dict。"""
    return {g["name"]: g for g in module_groups(_ont(), ents)}


def test_empty_entities_returns_no_modules():
    """无实体时模块列表为空（全部被 count>0 剔除）。"""
    assert module_groups(_ont(), []) == []


def test_module_tree_structure():
    """模块结构：顶层根 + 二级模块，卫星系统模块含完整子树类型集合。"""
    ents = ["卫星系统", "卫星平台", "转发器", "转发器", "系统需求", "信关站"]
    g = _m(ents)
    # 卫星系统模块 = 卫星系统 + 全部子孙类型
    assert set(g["卫星系统"]["types"]) == {"卫星系统", "卫星平台", "有效载荷",
                                            "通信载荷", "转发器", "天线"}
    # 二级模块（地面段）存在
    assert set(g["地面段"]["types"]) == {"地面段", "信关站"}
    # 顶层根模块（系统元素）覆盖全部子孙
    assert set(g["系统元素"]["types"]) == {"系统元素", "卫星系统", "卫星平台", "有效载荷",
                                            "通信载荷", "转发器", "天线", "地面段", "信关站"}
    # relation / attribute 类型不参与
    assert "CONTAINS" not in g and "EIRP" not in g


def test_count_accurate():
    """计数准确：count = 模块类型集合命中的实体数（同类型多次计数累计）。"""
    ents = ["卫星系统", "卫星平台", "转发器", "转发器", "系统需求"]
    g = _m(ents)
    assert g["卫星系统"]["count"] == 4      # 卫星系统+卫星平台+2×转发器
    assert g["系统元素"]["count"] == 4      # 顶层根覆盖全部子孙
    assert g["需求"]["count"] == 1
    assert g["系统需求"]["count"] == 1      # 二级模块
    assert "单元需求" not in g              # 无单元需求实体 → 二级空模块剔除
    assert "通信载荷" not in g              # 三级类型不独立成模块（实体归入卫星系统）


def test_empty_module_excluded():
    """空模块剔除：子树无实体的类型不返回。"""
    ents = ["卫星平台"]
    names = {g["name"] for g in module_groups(_ont(), ents)}
    assert "卫星系统" in names              # 卫星平台属于卫星系统子树
    assert "有效载荷" not in names          # 有效载荷子树无实体
    assert "需求" not in names
    assert "验证活动" not in names
    assert "功能" not in names


def test_count_add_when_entities_added():
    """数据量增加：新增实体 → 对应模块计数 +N。"""
    ents = ["卫星系统", "卫星平台", "转发器"]
    before = _m(ents)["卫星系统"]["count"]          # 3
    ents2 = ents + ["通信载荷", "通信载荷"]          # 新增 2 个实体
    g2 = _m(ents2)
    assert g2["卫星系统"]["count"] == before + 2     # 5（新增实体计入所在子树模块）
    assert "通信载荷" not in g2                       # 三级类型不独立成模块


def test_count_delete_when_entities_removed():
    """数据量减少：删除实体 → 对应模块计数 -N。"""
    ents = ["卫星系统", "卫星平台", "转发器", "转发器"]
    before = _m(ents)["卫星系统"]["count"]
    ents2 = ["卫星系统", "卫星平台", "转发器"]    # 删除 1 个转发器
    after = _m(ents2)["卫星系统"]["count"]
    assert after == before - 1


def test_count_moves_when_type_changed():
    """实体改类型 → 计数从原模块迁移到新模块（跨根迁移最明显）。"""
    ents = ["系统需求", "系统需求", "卫星平台"]
    g1 = _m(ents)
    assert g1["需求"]["count"] == 2 and g1["卫星系统"]["count"] == 1
    ents2 = ["系统需求", "卫星平台", "卫星平台"]   # 1 个系统需求改为卫星平台
    g2 = _m(ents2)
    assert g2["需求"]["count"] == 1 and g2["卫星系统"]["count"] == 2


def test_duplicate_types_count_accumulates():
    """同类型多实体计数累计（不按类型去重）。"""
    ents = ["转发器", "转发器", "转发器", "天线"]
    g = _m(ents)
    assert g["卫星系统"]["count"] == 4
    assert "通信载荷" not in g          # 三级类型实体统一归入卫星系统模块


def test_doc_groups_counts_and_order():
    """来源文档分组：计数正确、空来源归「未标注来源」、降序排列。"""
    rows = [{"source_doc": "a.md"}, {"source_doc": "a.md"},
            {"source_doc": "b.md"}, {"source_doc": ""}]
    g = doc_groups(rows)
    m = {x["name"]: x["count"] for x in g}
    assert m["a.md"] == 2 and m["b.md"] == 1 and m["未标注来源"] == 1
    assert g[0]["name"] == "a.md" and g[0]["count"] == 2   # 降序首位


def test_doc_groups_change_when_data_changes():
    """来源文档计数随数据量变化实时反映。"""
    rows = [{"source_doc": "a.md"}]
    g1 = {x["name"]: x["count"] for x in doc_groups(rows)}
    rows2 = [{"source_doc": "a.md"}, {"source_doc": "a.md"}, {"source_doc": "b.md"}]
    g2 = {x["name"]: x["count"] for x in doc_groups(rows2)}
    assert g2["a.md"] == g1["a.md"] + 1
    assert g2["b.md"] == 1


def main():
    tests = [
        test_empty_entities_returns_no_modules,
        test_module_tree_structure,
        test_count_accurate,
        test_empty_module_excluded,
        test_count_add_when_entities_added,
        test_count_delete_when_entities_removed,
        test_count_moves_when_type_changed,
        test_duplicate_types_count_accumulates,
        test_doc_groups_counts_and_order,
        test_doc_groups_change_when_data_changes,
    ]
    for fn in tests:
        _t(fn.__name__, fn)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n=== 图谱子图单元测试 ===")
    for name, ok, detail in RESULTS:
        print(("  ✓" if ok else "  ✗ FAIL"), name, ("- " + detail if detail else ""))
    print(f"\n通过 {passed}/{len(RESULTS)}")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
