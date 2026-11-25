# -*- coding: utf-8 -*-
"""本体标准映射回填（定位选项 A 的落地件，2026-11-23）。

## 背景

定位体检发现：`ontology_types.profile_source` / `profile_ref`（"外部标准映射"）
**0/54 全空** —— 这两个字段是"这个类型在 SysML v2 / ISO 15288 里对应什么"的唯一载体，
空着就等于**没有对外语义锚点**：别人拿到本工程的「系统需求」，无从判断它等于
`RequirementDefinition` 还是某个自定义概念。

## 为什么用脚本而不是手填

41 个类型 × 两个字段；手填必然漏、且下次评审无法复现"当初依据什么填的"。
脚本把**映射依据**写成常量表（可审阅、可 diff、可回滚），并要求：
  · 幂等：默认只填**空值**，不覆盖已有（`--force` 才覆盖）
  · 可预演：默认 `--dry` 只打印将要写什么，`--apply` 才落库
  · 自证：落库后回读核对，并打印覆盖率

## 映射口径（不是自由发挥）

  · SysML v2 侧：取 **KerML/SysML v2 的元类名**（`PartDefinition` / `RequirementDefinition` /
    `ActionDefinition` / `VerificationCaseDefinition` / `ConnectionDefinition` 等），
    关系取对应关键字语义（`satisfy` / `verify` / `allocate` / `connect` / `flow` / `perform`）；
    **拿不准的不硬编**：标 `Dependency (approx.)` 并在 `NOTE` 里写明是近似。
  · ISO 侧：取 **ISO/IEC/IEEE 15288:2015 §6.4.x Technical Processes**
    （6.4.2 利益相关方需求、6.4.3 系统需求、6.4.4 架构定义、6.4.5 设计定义、
      6.4.9 验证、6.4.11 确认）。
  · 字段写法：`profile_source = "SysML v2"`；`profile_ref = "<MetaClass> | ISO/IEC/IEEE 15288:2015 §6.4.x"`。
    OWL 导出时两者拼成 `rdfs:seeAlso`（`core/domain/ontology_owl.py:449`），所以填了就能在导出件里看到。

用法：
    python tools/migrate_profile_mapping.py            # 预演（不改库）
    python tools/migrate_profile_mapping.py --apply    # 落库
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SRC = "SysML v2"

#: 实体类型 → (SysML v2 元类, ISO 15288 条款)
ENTITY_MAP = {
    "需求":            ("RequirementDefinition", "§6.4.3 系统需求定义"),
    "利益相关方需求":   ("RequirementDefinition", "§6.4.2 利益相关方需求定义"),
    "系统需求":         ("RequirementDefinition", "§6.4.3 系统需求定义"),
    "子系统需求":       ("RequirementDefinition", "§6.4.3 系统需求定义"),
    "单元需求":         ("RequirementDefinition", "§6.4.3 系统需求定义"),
    "功能":            ("ActionDefinition", "§6.4.4 架构定义（功能架构）"),
    "验证用例":         ("VerificationCaseDefinition", "§6.4.9 验证"),
    "利益相关方":       ("PartDefinition", "§6.4.2 利益相关方需求定义（stakeholder）"),
    "系统元素":         ("PartDefinition", "§6.4.4 架构定义"),
    "部件":            ("PartDefinition", "§6.4.5 设计定义"),
    "外部系统":         ("PartDefinition", "§6.4.1 业务/任务分析（系统上下文）"),
}

#: 星网领域类型（同一元类，条款按所处过程区分）
ENTITY_MAP.update({
    "通信卫星系统":     ("PartDefinition", "§6.4.4 架构定义（系统级）"),
    "卫星平台":         ("PartDefinition", "§6.4.4 架构定义"),
    "通信载荷分系统":   ("PartDefinition", "§6.4.4 架构定义"),
    "转发器":          ("PartDefinition", "§6.4.5 设计定义"),
    "功率放大器":       ("PartDefinition", "§6.4.5 设计定义"),
    "天线分系统":       ("PartDefinition", "§6.4.4 架构定义"),
    "天线":            ("PartDefinition", "§6.4.5 设计定义"),
    "数传分系统":       ("PartDefinition", "§6.4.4 架构定义"),
    "测控分系统":       ("PartDefinition", "§6.4.4 架构定义"),
    "电源分系统":       ("PartDefinition", "§6.4.4 架构定义"),
    "姿轨控分系统":     ("PartDefinition", "§6.4.4 架构定义"),
    "星座":            ("PartDefinition", "§6.4.4 架构定义（体系/SoS 构型）"),
    "星地链路":         ("ConnectionDefinition", "§6.4.4 架构定义（接口/连接）"),
    "地面段":          ("PartDefinition", "§6.4.4 架构定义"),
    "地面站":          ("PartDefinition", "§6.4.5 设计定义"),
    "测控站":          ("PartDefinition", "§6.4.5 设计定义"),
    "用户终端":         ("PartDefinition", "§6.4.5 设计定义"),
})

#: 关系类型 → SysML v2 语义（关键字或元类）。``approx`` 表示最接近但非一一对应。
RELATION_MAP = {
    "CONTAINS":    ("PartUsage（结构归属，近似）", "approx"),
    "COMPOSED_OF": ("PartUsage（组合，近似）", "approx"),
    "DERIVES":     ("Subsetting（需求派生，近似）", "approx"),
    "SATISFIES":   ("SatisfyRequirementUsage（satisfy 关键字）", ""),
    "VERIFIED_BY": ("VerificationCaseUsage（verify 关键字）", ""),
    "ALLOCATED_TO": ("AllocationUsage（allocate 关键字）", ""),
    "FLOW_TO":     ("FlowUsage（flow 连接）", ""),
    "CONNECTS":    ("ConnectionUsage（connect 关键字）", ""),
    "DEPENDS_ON":  ("Dependency", ""),
    "TRACE":       ("Dependency（trace 近似）", "approx"),
    "REALIZES":    ("Subsetting（realize 近似）", "approx"),
    "连接":         ("ConnectionUsage（connect 关键字）", ""),
    "执行":         ("Perform（perform 关键字，近似）", "approx"),
}


def main() -> int:
    apply = "--apply" in sys.argv
    force = "--force" in sys.argv
    import sqlite3
    from core.config import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r for r in conn.execute(
        "SELECT id,name,type_kind,profile_source,profile_ref FROM ontology_types")}

    plan = []
    for name, (meta, clause) in ENTITY_MAP.items():
        if name not in rows:
            print("[warn] 库里没有实体类型：%s（跳过）" % name)
            continue
        ref = "%s | ISO/IEC/IEEE 15288:2015 %s" % (meta, clause)
        plan.append((rows[name], SRC, ref))
    for name, (meta, approx) in RELATION_MAP.items():
        if name not in rows:
            print("[warn] 库里没有关系类型：%s（跳过）" % name)
            continue
        ref = "%s%s" % (meta, "  [近似]" if approx else "")
        plan.append((rows[name], SRC, ref))

    todo, skip = [], []
    for r, src, ref in plan:
        if (r["profile_source"] or "") or (r["profile_ref"] or ""):
            if not force:
                skip.append((r["name"], r["profile_ref"]))
                continue
        todo.append((r["id"], r["name"], src, ref))

    print("=" * 78)
    print("本体标准映射回填（%s）" % ("APPLY" if apply else "DRY-RUN"))
    print("=" * 78)
    print("计划写入 %d 条，跳过已有值 %d 条" % (len(todo), len(skip)))
    for i, (_id, name, src, ref) in enumerate(todo[:12], 1):
        print("  %2d. %-14s → %s :: %s" % (i, name, src, ref[:70]))
    if len(todo) > 12:
        print("  … 其余 %d 条同格式" % (len(todo) - 12))
    if skip:
        print("  跳过（已有值，--force 可覆盖）：%s" % ", ".join(n for n, _ in skip[:8]))

    if not apply:
        print("\n（预演结束，未改库。加 --apply 落库）")
        return 0

    n = 0
    try:
        conn.execute("BEGIN")
        for _id, _name, src, ref in todo:
            conn.execute("UPDATE ontology_types SET profile_source=?, profile_ref=?, "
                         "updated_at=datetime('now','localtime') WHERE id=?",
                         (src, ref, _id))
            n += 1
        conn.commit()
    except Exception as ex:
        conn.rollback()
        print("[FAIL] 写入失败已回滚：%s: %s" % (type(ex).__name__, str(ex)[:160]))
        return 1

    # 回读自证（不只看 UPDATE 影响行数 —— "命令成功≠操作生效"）
    filled_e = conn.execute(
        "SELECT COUNT(*) FROM ontology_types WHERE type_kind='entity' "
        "AND COALESCE(profile_ref,'')<>''").fetchone()[0]
    filled_r = conn.execute(
        "SELECT COUNT(*) FROM ontology_types WHERE type_kind='relation' "
        "AND COALESCE(profile_ref,'')<>''").fetchone()[0]
    total_e = conn.execute(
        "SELECT COUNT(*) FROM ontology_types WHERE type_kind='entity'").fetchone()[0]
    total_r = conn.execute(
        "SELECT COUNT(*) FROM ontology_types WHERE type_kind='relation'").fetchone()[0]
    print("\n写入 %d 条。回读覆盖率：entity %d/%d、relation %d/%d"
          % (n, filled_e, total_e, filled_r, total_r))
    sample = conn.execute("SELECT name, profile_source, profile_ref FROM ontology_types "
                          "WHERE name IN ('系统需求','功能','星地链路','SATISFIES')").fetchall()
    for r in sample:
        print("  · %-8s %s :: %s" % (r["name"], r["profile_source"], r["profile_ref"]))
    conn.close()
    return 0 if (filled_e == total_e and filled_r >= total_r - 2) else 1


if __name__ == "__main__":
    sys.exit(main())
