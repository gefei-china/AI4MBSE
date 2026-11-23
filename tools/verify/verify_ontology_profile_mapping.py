# -*- coding: utf-8 -*-
"""本体标准映射门禁：`profile_source` / `profile_ref` 必须覆盖且元类合法（2026-11-23）。

## 为什么有这道门禁

定位选项 A（"语义契约层"）的**唯一硬判据**是：本工程的类型能不能对上外部标准。
体检前实测 `profile_source` / `profile_ref` **0/54 全空** —— 等于没有对外语义锚点。

回填后必须锁死三件事，否则会慢慢退回去：
  P1 **覆盖率**：entity 与 relation 类型都必须有映射（新增类型漏填即红）
  P2 **元类合法**：`profile_ref` 的首个词必须是**已知的 SysML v2 元类/关键字**
     （防手填拼错，如 `PartDefination` —— 拼错比不填更糟：导出件里看着有、实际对不上）
  P3 **ISO 条款格式**：出现 `§6.4.x` 时，x 必须在 1–14（15288:2015 Technical Processes 范围）
  P4 **真链路**：`ontology_owl.to_turtle()` 里 `rdfs:seeAlso` 条数必须 ≥ 已映射类型数
     （防"库里有值、导出不带"这种配了不生效）

## 变异（必须判红）

  M1 内存里清空一条 entity 的 profile_ref ⇒ P1 红
  M2 把某条改成 `PartDefination`（拼错）     ⇒ P2 红
  M3 把条款改成 §6.4.99                    ⇒ P3 红
  （全部只改内存副本，不写库）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_p, _f = [], []

#: SysML v2 / KerML 已知元类与关键字（回填脚本用到的那一组 + 常见备选）
KNOWN_META = {
    # 定义类
    "PartDefinition", "RequirementDefinition", "ActionDefinition", "StateDefinition",
    "UseCaseDefinition", "VerificationCaseDefinition", "ItemDefinition", "PortDefinition",
    "InterfaceDefinition", "ConnectionDefinition", "ConstraintDefinition",
    "CalculationDefinition", "AllocationDefinition", "ConcernDefinition", "MetadataDefinition",
    # 使用/关键字类
    "PartUsage", "RequirementUsage", "ActionUsage", "StateUsage", "UseCaseUsage",
    "VerificationCaseUsage", "ItemUsage", "PortUsage", "ConnectionUsage",
    "SatisfyRequirementUsage", "AllocationUsage", "FlowUsage", "Perform",
    # 关系/依赖
    "Dependency", "Subsetting", "Specialization", "Realization", "Transition",
}


def ck(cond, msg, detail=""):
    (_p if cond else _f).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg + (("  «%s»" % detail) if detail and not cond else ""))


def first_meta(ref: str) -> str:
    s = str(ref or "").split("|")[0]
    for sep in ("（", "("):
        s = s.split(sep)[0]
    return s.strip()


def check_coverage(rows) -> list[str]:
    miss = [r["name"] for r in rows
            if r["type_kind"] in ("entity", "relation")
            and not (str(r["profile_ref"] or "").strip())]
    return miss


def check_meta(rows) -> list[str]:
    bad = []
    for r in rows:
        if r["type_kind"] not in ("entity", "relation"):
            continue
        ref = str(r["profile_ref"] or "").strip()
        if not ref:
            continue
        m = first_meta(ref)
        if m and m not in KNOWN_META:
            bad.append("%s→%s" % (r["name"], m))
    return bad


def check_clause(rows) -> list[str]:
    bad = []
    for r in rows:
        ref = str(r["profile_ref"] or "")
        for m in re.finditer(r"§6\.4\.(\d+)", ref):
            if not (1 <= int(m.group(1)) <= 14):
                bad.append("%s→§6.4.%s" % (r["name"], m.group(1)))
    return bad


def main() -> int:
    print("=" * 78)
    print("本体标准映射门禁（覆盖率 / 元类 / 条款 / 导出链路）")
    print("=" * 78)
    import sqlite3
    from core.config import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id,name,type_kind,profile_source,profile_ref FROM ontology_types")]

    if not rows:
        conn.close()
        print("  [SKIP] 库里没有类型数据（CI 常态）—— **不计入通过**")
        print("结论：SKIP")
        return 0

    # P1
    miss = check_coverage(rows)
    ck(not miss, "P1 entity/relation 类型全部有标准映射", "缺 %d：%s" % (len(miss), miss[:6]))
    n_map = sum(1 for r in rows if str(r["profile_ref"] or "").strip())
    print("  · 已映射 %d / %d（entity+relation）" % (n_map, sum(1 for r in rows if r["type_kind"] in ("entity", "relation"))))

    # P2
    bad_meta = check_meta(rows)
    ck(not bad_meta, "P2 元类名都在已知 SysML v2 词表内（防拼错）", str(bad_meta[:5]))

    # P3
    bad_clause = check_clause(rows)
    ck(not bad_clause, "P3 ISO 15288 条款号在 §6.4.1–§6.4.14 范围内", str(bad_clause[:5]))

    # P4 真链路：填了必须能导出
    # ⚠️ 必须用 `database.get_db()`（产品同源入口），不要用裸 `sqlite3.connect`：
    #    `to_turtle` 内部按 active 快照取行，裸连接与产品连接的 row_factory/pragma 不一致时
    #    会取到空集 ⇒ seeAlso=0，看起来像"导出不生效"，实为**探针连错了口**（首版踩过）。
    try:
        from database import get_db
        import ontology_owl as O
        c2 = get_db()
        try:
            # ⚠️ `to_turtle` 返回 **(ttl, warns) 元组**，不是字符串。
            #    首版直接对返回值 count("rdfs:seeAlso") ⇒ 对 tuple 计数恒 0，
            #    差一点误判成"导出不带映射"（实测 41 条）。类型假设必须先自证。
            out = O.to_turtle(c2)
            s = out[0] if isinstance(out, (tuple, list)) else str(out)
            ck(isinstance(s, str) and s.lstrip().startswith(("@prefix", "PREFIX", ":")),
               "P4a to_turtle 返回 TTL 文本（已处理元组解包）", str(type(out).__name__))
        finally:
            c2.close()
        n_see = s.count("rdfs:seeAlso")
        ck(n_see >= n_map, "P4 导出件 rdfs:seeAlso 条数 ≥ 已映射类型数（填了就带出去）",
           "seeAlso=%d mapped=%d" % (n_see, n_map))
    except Exception as ex:
        ck(False, "P4 导出链路可执行", "%s: %s" % (type(ex).__name__, str(ex)[:100]))

    # ── 变异（全在内存副本上，不写库） ──
    print("\n-- 变异（必须判红） --")
    import copy
    m1 = copy.deepcopy(rows)
    tgt = next((r for r in m1 if r["type_kind"] == "entity"), None)
    if tgt:
        tgt["profile_ref"] = ""
    ck(bool(check_coverage(m1)), "M1 清空一条 entity 映射 ⇒ P1 判红",
       "miss=%s" % check_coverage(m1)[:3])

    m2 = copy.deepcopy(rows)
    tgt2 = next((r for r in m2 if r["type_kind"] == "entity"), None)
    if tgt2:
        tgt2["profile_ref"] = "PartDefination | ISO/IEC/IEEE 15288:2015 §6.4.4"
    ck(bool(check_meta(m2)), "M2 元类拼错 ⇒ P2 判红", str(check_meta(m2)[:3]))

    m3 = copy.deepcopy(rows)
    tgt3 = next((r for r in m3 if r["type_kind"] == "entity"), None)
    if tgt3:
        tgt3["profile_ref"] = "PartDefinition | ISO/IEC/IEEE 15288:2015 §6.4.99"
    ck(bool(check_clause(m3)), "M3 条款号越界 ⇒ P3 判红", str(check_clause(m3)[:3]))

    conn.close()
    print("\n" + "=" * 78)
    print("结论：%s（PASS %d / FAIL %d）" % ("OK" if not _f else "FAIL", len(_p), len(_f)))
    for m in _f:
        print("  ✗ " + m)
    print("=" * 78)
    return 0 if not _f else 1


if __name__ == "__main__":
    sys.exit(main())
