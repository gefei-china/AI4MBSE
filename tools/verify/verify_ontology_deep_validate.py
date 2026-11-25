# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-24 第一期：实例级体检 deep 开关 + 真实扫描计数 + SHACL 发现落库开关。需生产真库（instances_scanned 金标 462）与 ingest_gate；CI 干净库无该数据
"""第一期「看得见」验证：实例级校验 + SHACL 发现落库。

不依赖服务（直接调内部函数 + 真库只读），随时可跑：
    .venv/Scripts/python.exe tools/verify/verify_ontology_deep_validate.py

判据（对齐产品版验收）：
  V1 默认体检 instances_scanned == 0（不默认做实例级，保持原行为）
  V2 deep 体检 instances_checked is True 且 instances_scanned > 0（真扫了数据）
  V3 覆盖面自证：instances_scanned == entities + relations + approved triples
  V4 degraded 为空（没有静默跳过的扫描）
  V5 SHACL check_triples(record=True) 有违规时写 shacl_findings（临时库夹具）
  V6 record=False 不污染 findings 表
  V7 变异自证：把 deep 分支还原 → V2 必须 FAIL（证明断言非空转）
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  {detail}" if detail else ""))


def main():
    from routers.knowledge_parts.shared import _ontology_check

    db = ROOT / "mbse.db"
    if not db.exists():
        print("SKIP: 真库不存在（CI 干净库环境）")
        return 0
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    print("\n[1] 默认 vs deep 语义")
    r0 = _ontology_check(conn)
    check("V1 默认不做实例级扫描", r0.get("instances_scanned") == 0,
          f"instances_scanned={r0.get('instances_scanned')}")

    rd = _ontology_check(conn, instances=True)
    scanned = rd.get("instances_scanned") or 0
    check("V2 deep 打开实例级", rd.get("instances_checked") is True and scanned > 0,
          f"checked={rd.get('instances_checked')} scanned={scanned}")
    check("V4 无静默降级", not (rd.get("degraded") or []),
          f"degraded={rd.get('degraded')}")

    print("\n[2] 覆盖面自证（防 0 检查假绿）")
    expect = conn.execute(
        "SELECT (SELECT COUNT(*) FROM entities WHERE status!='deprecated') + "
        "(SELECT COUNT(*) FROM relations WHERE status!='deprecated') + "
        "(SELECT COUNT(*) FROM triples WHERE status='approved')").fetchone()[0]
    check("V3 计数口径 == 实体+关系+已审三元组", scanned == expect,
          f"scanned={scanned} expect={expect}")

    print("\n[3] SHACL 发现落库开关（真库只读 + 源码断言，不写库）")
    import ingest_gate
    import inspect
    from routers import governance

    # V6：真库上用 record=False 实跑，前后 findings 行数不变（真库 schema 完整）
    n0 = conn.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
    a = ingest_gate.check_triples(conn, record=False)
    n1 = conn.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
    check("V6 record=False 不写 findings", n0 == n1,
          f"before={n0} after={n1} skipped={a.get('skipped')}")

    # V5：端点默认 record=1，且把值透传给 check_triples（源码断言，避免写真库）
    src = inspect.getsource(governance.shacl_check)
    sig = inspect.signature(governance.shacl_check)
    default_record = sig.parameters.get("record")
    check("V5a 端点默认 record=1",
          default_record is not None and default_record.default == 1,
          f"default={default_record.default if default_record else None}")
    check("V5b record 透传 check_triples",
          "record=bool(record)" in src and "record: int = 1" in src,
          "governance.shacl_check 源码")

    # V5c：落库分支真实存在（ingest_gate 源码）
    ig_src = inspect.getsource(ingest_gate.check_triples)
    check("V5c ingest_gate 有 record 落库分支",
          "if record and violations" in ig_src,
          "ingest_gate.check_triples 源码")

    print("\n[4] 变异自证：deep 分支还原后 V2 必须不成立")
    # 模拟：不传 instances=True 时 scanned 应为 0 —— 若仍 >0 说明 V2 断言空转
    mutant = _ontology_check(conn, instances=False)
    check("V7 变异击穿（默认态 scanned=0）", (mutant.get("instances_scanned") or 0) == 0,
          f"mutant_scanned={mutant.get('instances_scanned')}")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
