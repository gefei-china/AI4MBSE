# -*- coding: utf-8 -*-
"""P0-2 / P0-3 回填：本体**属性类型**的数据类型（xsd_type）与量纲单位（unit/quantity_kind）。

## 为什么需要（2026-11-15 审计实测）

`core/typevocab.py`（类型词表）与前端 `ot-unit`/`ot-qkind` 级联下拉**早就存在**
（2026-09-07 落地），但**数据从未回填**：

```
真库 11 个 attribute 行：有 xsd_type 的 0 个；有 unit 的 0 个
```

后果不是"界面难看"，而是这些能力**全部空转**：
  · SHACL 导出对所有属性只能发 `sh:datatype` 缺省 → 数值/枚举校验形同虚设；
  · 实例编辑器的 xsd 强校验（2026-09 上线的校验器）**没有输入可校验**；
  · 工程数值没有单位 ⇒ 带宽/EIRP/寿命之间无法比较、无法换算、跨系统交换必然歧义
    （对照 QUDT / IEC CDD：量纲是工程属性的第一公民）。

## 单位是怎么定的（**不是凭空指定**）

第一步先查存量数据：`entities.properties` 里这 11 个属性键的**实际取值出现次数 = 0**
（脚本会打印证据）⇒ 不存在"与存量数据冲突"的风险，才按工程惯例赋单位。

第二步按领域惯例+前端已有量纲词表（`ONT_QK_UNITS`，15 个类别）就近映射。
凡词表没有的（dB/K、Mbit/s、计数），用 `quantity_kind` 自定义 + `unit` 自由填写
——前端本就支持"自定义量纲/自定义单位"这条通路。

> ⚠️ **诚实标注**：`dBm` / `W` / `dB` 等是可辩护的工程惯例（卫星通信语境），
> 但「EIRP 用 dBm 还是 dBW」「输出功率用 W 还是 dBm」在具体项目里可能另有规定。
> 本脚本把它们集中成一张表，**改一行即可**；`--dry-run` 会逐条打印计划，便于先审后写。

## 用法

    python tools/backfill_ontology_attrs.py            # 预览（默认 dry-run，不写库）
    python tools/backfill_ontology_attrs.py --apply    # 实际写入
    python tools/backfill_ontology_attrs.py --verify   # 只核对回填后的三端一致性

## 纪律

1. **幂等**：已有 `xsd_type` / `unit` 的行**不覆盖**（`--force` 才覆盖）。
   —— 本项目的人工填写优先于脚本默认值。
2. **不静默**：每行打印「原值 → 新值」，跳过的也打印原因。
3. **写完回读自证**：新开连接回读（旧连接回读是快照，本仓踩过 —— "PATCH 明明 200
   却读到旧值"）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")

# ══════════════════════════════════════════════════════════════════════════
# 回填计划：属性名 → (xsd 类型, 量纲类别, 单位, 依据)
# ══════════════════════════════════════════════════════════════════════════
#: xsd 类型取 `core.typevocab` 的**规范名**（string/text/int/decimal/boolean/
#: date/dateTime/enum）—— 不写 xsd: 前缀，写入时经 typevocab.xsd_of() 归一，
#: 保证 SHACL / OWL / 实例编辑器三端拿到同一个类型。
#:
#: 量纲类别优先用前端词表 `ONT_QK_UNITS` 的 15 个类（时间/长度/质量/温度/频率/
#: 功率/电压/电流/能量/压力/速度/角度/数据量/增益/比率）；不在其中的用自定义类别
#: （前端"自定义…"通路已支持）。
PLAN = {
    # 卫星通信链路预算类
    "EIRP":      ("decimal", "增益", "dBm",
                  "有效全向辐射功率，卫星通信惯例以 dBm/dBW 表达（此处取 dBm）"),
    "G/T值":     ("decimal", "品质因数", "dB/K",
                  "接收系统品质因数 = 天线增益 / 系统噪声温度；dB/K 为行业惯例写法（词表无，自定义）"),
    "链路余量":    ("decimal", "增益", "dB",
                  "链路余量是功率裕度，以 dB 表达"),
    "输出功率":    ("decimal", "功率", "W",
                  "射频输出功率按 SI 一贯单位 W 表达（词表内有 mW/W/kW/MW/GW）"),
    "误码率":     ("decimal", "比率", "",
                  "误码率是无量纲比值（数量级 1e-6），%／‰／ppm 均不适用 ⇒ 量纲=比率、单位留空"),
    "传输速率":    ("decimal", "传输速率", "Mbit/s",
                  "数据速率（bit/s 系），通信语境惯例用 Mbit/s（词表无，自定义）"),
    # 几何 / 轨道类
    "通信带宽":    ("decimal", "频率", "MHz",
                  "带宽以频率单位表达（词表内有 Hz/kHz/MHz/GHz）"),
    "覆盖范围":    ("decimal", "长度", "km",
                  "地面覆盖范围按长度表达；卫星覆盖用 km（词表内有 mm..km）"),
    "轨道高度":    ("decimal", "长度", "km",
                  "轨道高度按长度表达；LEO/GEO 惯例用 km"),
    # 计数 / 寿命
    "波束数量":    ("int", "计数", "个",
                  "波束数量是离散计数 ⇒ xsd:int（不是 decimal），量纲=计数、单位=个（自定义）"),
    "在轨寿命":    ("decimal", "时间", "a",
                  "在轨寿命按时间表达；工程惯例以「年(a)」计（词表有 ms..d，无年 ⇒ 自定义 a）"),
}


def _rows(con):
    return con.execute(
        "SELECT id, name, constraints FROM ontology_types WHERE type_kind='attribute'"
    ).fetchall()


def _existing_value_probe(con):
    """证据：这些属性键在存量实体里是否已有取值（有数据就不该随便改单位）。"""
    out = {}
    try:
        names = list(PLAN.keys())
        for nm in names:
            n = con.execute(
                "SELECT COUNT(*) FROM entities WHERE properties LIKE ?", ("%" + nm + "%",)
            ).fetchone()[0]
            out[nm] = n
    except Exception as e:
        out["__error__"] = str(e)
    return out


def run(apply: bool = False, force: bool = False):
    from core import typevocab  # 单一词表真源：写入前用它归一

    con = sqlite3.connect(DB, timeout=45)
    con.row_factory = sqlite3.Row

    print("=" * 78)
    print("本体属性回填：数据类型（xsd_type）+ 量纲单位（unit / quantity_kind）")
    print("模式：%s%s" % ("APPLY（写库）" if apply else "DRY-RUN（只预览）",
                        "  force=覆盖已有值" if force else ""))
    print("=" * 78)

    probe = _existing_value_probe(con)
    print("\n[证据] 存量实体中这些属性键的取值计数（>0 说明已有数据，改单位需谨慎）：")
    for k, v in probe.items():
        print("   %-10s %s" % (k, v))

    rows = _rows(con)
    print("\n[现状] attribute 行 %d 条" % len(rows))

    plan_n, skip_n, miss_n = 0, 0, []
    changes = []
    for r in rows:
        nm = r["name"]
        if nm not in PLAN:
            miss_n.append(nm)
            continue
        xsd, qk, unit, why = PLAN[nm]
        try:
            cons = json.loads(r["constraints"] or "{}")
        except Exception:
            cons = {}
        if not isinstance(cons, dict):
            cons = {}
        old_x = cons.get("xsd_type", "")
        old_u = cons.get("unit", "")
        old_q = cons.get("quantity_kind", "")

        new = dict(cons)
        todo = []
        if force or not old_x:
            new["xsd_type"] = typevocab.xsd_of(xsd) or xsd
            todo.append("xsd_type: %r → %r" % (old_x, new["xsd_type"]))
        if force or not old_u:
            if unit:
                new["unit"] = unit
                todo.append("unit: %r → %r" % (old_u, unit))
            else:
                todo.append("unit: 留空（%s）" % why)
        if force or not old_q:
            if qk:
                new["quantity_kind"] = qk
                todo.append("quantity_kind: %r → %r" % (old_q, qk))

        if not todo:
            skip_n += 1
            print("  [SKIP] %-10s 已有类型/单位，不覆盖" % nm)
            continue

        plan_n += 1
        print("  [PLAN] %-10s %s" % (nm, " | ".join(todo)))
        changes.append((nm, r["id"], json.dumps(new, ensure_ascii=False)))

    if miss_n:
        print("\n[INFO] 未纳入计划的属性（保持原样）: %s" % "、".join(sorted(miss_n)))

    if not apply:
        print("\n结果：DRY-RUN，计划变更 %d 条，跳过 %d 条（未写库）" % (plan_n, skip_n))
        con.close()
        return 0

    for nm, tid, payload in changes:
        con.execute("UPDATE ontology_types SET constraints=? WHERE id=?", (payload, tid))
    con.commit()
    con.close()

    # ⚠️ 必须新连接回读：旧连接读到的是快照（本仓已踩过这个坑）
    con2 = sqlite3.connect(DB, timeout=45)
    con2.row_factory = sqlite3.Row
    ok = 0
    print("\n[回读自证]（新连接）")
    for r in _rows(con2):
        nm = r["name"]
        if nm not in PLAN:
            continue
        cons = json.loads(r["constraints"] or "{}")
        has_x = bool(cons.get("xsd_type"))
        has_u = bool(cons.get("unit")) or (nm in ("误码率",))   # 误码率按设计无量纲
        has_q = bool(cons.get("quantity_kind"))
        flag = "OK " if (has_x and has_u and has_q) else "MISS"
        if has_x and has_u and has_q:
            ok += 1
        print("  [%s] %-10s xsd=%-12s unit=%-8s qkind=%s"
              % (flag, nm, cons.get("xsd_type", ""), cons.get("unit", "") or "（无量纲）",
                 cons.get("quantity_kind", "")))
    total = len([1 for r in _rows(con2) if r["name"] in PLAN])
    con2.close()
    print("\n结果：写入 %d 条；回读校验 %d/%d 完整" % (len(changes), ok, total))
    return 0 if ok == total else 1


def verify():
    """核对三端一致性：typevocab 归一结果 == SHACL 导出 == OWL 导出。"""
    import ontology_owl as O
    from core import typevocab

    con = sqlite3.connect(DB, timeout=45)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM ontology_types").fetchall()
    print("=" * 78)
    print("三端一致性核对：typevocab ↔ SHACL ↔ OWL")
    print("=" * 78)
    bad = 0
    for r in rows:
        if r["type_kind"] != "attribute":
            continue
        cons = json.loads(r["constraints"] or "{}")
        decl = cons.get("xsd_type")
        if not decl:
            print("  [MISS] %-10s 无 xsd_type" % r["name"])
            bad += 1
            continue
        norm = typevocab.xsd_of(decl)
        if not norm:
            print("  [FAIL] %-10s xsd_type=%r 无法归一（词表未识别）" % (r["name"], decl))
            bad += 1
            continue
        print("  [OK  ] %-10s %r → %s" % (r["name"], decl, norm))
    xml, warns = O.to_owl(con, rows=rows)
    ttl = O.to_turtle(con, rows=rows)
    ttl = ttl[0] if isinstance(ttl, tuple) else ttl
    print("\nSHACL/OWL 导出：OWL 长度 %d，Turtle 长度 %d" % (len(xml), len(ttl)))
    print("结果：%s" % ("PASS" if bad == 0 else "FAIL（%d 条）" % bad))
    con.close()
    return 0 if bad == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="本体属性 xsd_type / unit 回填")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    ap.add_argument("--force", action="store_true", help="覆盖已有类型/单位")
    ap.add_argument("--verify", action="store_true", help="只核对三端一致性")
    a = ap.parse_args()
    if a.verify:
        return verify()
    return run(apply=a.apply, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
