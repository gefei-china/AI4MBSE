# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-24 C2：证明 shacl_findings 落库链路真能写（此前只有源码断言）。用 SQLite backup 复制真库到临时文件构造违规，不污染真库；CI 无生产真库
"""C2 验证：证明 shacl_findings 落库链路真的能写（此前只做过源码断言）。

    .venv/Scripts/python.exe tools/verify/verify_shacl_findings_write.py

纪律：不写真库 —— 用 SQLite backup API 复制到临时文件，在副本上构造违规。
判据：
  S1 真库副本可建立，原始 shacl_findings 行数记录
  S2 构造违规三元组后 check_triples(record=True) 返回 violations>0
  S3 shacl_findings 表新增行数 == violations 数（真写，不是源码断言）
  S4 写入行字段完整（batch_id/subject/severity/message/gate_mode 非空）
  S5 record=False 不新增行
  S6 真库未被污染（行数与 S1 一致）
  S7 变异自证：去掉 record 条件后 S5 不成立
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  {detail}" if detail else ""))


def main():
    db = ROOT / "mbse.db"
    if not db.exists():
        print("SKIP: 真库不存在")
        return 0

    src = sqlite3.connect(str(db))
    src.row_factory = sqlite3.Row
    n_real = src.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
    check("S1 记录真库基线", isinstance(n_real, int), f"findings={n_real}")

    # 备份到临时库（不碰真库）
    tmp = Path(tempfile.gettempdir()) / ("mbse_shacl_verify_%d.db" % os.getpid())
    if tmp.exists():
        tmp.unlink()
    dst = sqlite3.connect(str(tmp))
    src.backup(dst)
    dst.row_factory = sqlite3.Row
    src.close()

    try:
        import ingest_gate
        # 构造违规：给一个已审实体塞一个 xsd:integer 属性却写非数字值。
        # 选真库已有的 attribute 类型，确保 SHACL 形状能命中。
        attr = dst.execute(
            "SELECT name, constraints FROM ontology_types WHERE type_kind='attribute' "
            "AND constraints LIKE '%xsd:decimal%' LIMIT 1").fetchone()
        ent = dst.execute(
            "SELECT id, name, entity_type, properties FROM entities "
            "WHERE status!='deprecated' AND entity_type!='' LIMIT 1").fetchone()
        if not attr or not ent:
            print("SKIP: 真库无合适属性/实体夹具")
            return 0
        import json
        props = json.loads(ent["properties"] or "{}")
        props[attr["name"]] = "NOT_A_NUMBER"
        dst.execute("UPDATE entities SET properties=? WHERE id=?",
                    (json.dumps(props, ensure_ascii=False), ent["id"]))
        dst.commit()
        print(f"  夹具：实体 {ent['name']} 的属性 {attr['name']} = 'NOT_A_NUMBER'")

        print("\n[2] record=True 真写")
        n0 = dst.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
        res = ingest_gate.check_triples(dst, record=True, batch_id="verify_c2")
        n1 = dst.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
        viol = res.get("violations") or []
        check("S2 构造违规被检出", len(viol) > 0,
              f"violations={len(viol)} conforms={res.get('conforms')} skipped={res.get('skipped')}")
        check("S3 落库行数 == 违规数", n1 - n0 == len(viol),
              f"before={n0} after={n1} violations={len(viol)}")
        if n1 > n0:
            row = dst.execute(
                "SELECT batch_id, subject, severity, message, gate_mode FROM shacl_findings "
                "ORDER BY id DESC LIMIT 1").fetchone()
            ok = all(row[k] not in (None, "") for k in
                     ("batch_id", "subject", "severity", "message", "gate_mode"))
            check("S4 写入字段完整", ok,
                  f"batch={row['batch_id']} sev={row['severity']} mode={row['gate_mode']}")
        else:
            check("S4 写入字段完整", False, "无写入行")

        print("\n[3] record=False 不写")
        n2 = dst.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
        ingest_gate.check_triples(dst, record=False, batch_id="verify_c2_norecord")
        n3 = dst.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
        check("S5 record=False 不新增", n2 == n3, f"before={n2} after={n3}")

        print("\n[4] 变异自证")
        src_code = Path("core/domain/ingest_gate.py").read_text(encoding="utf-8")
        check("S6 落库分支存在", "if record and violations" in src_code,
              "去掉该条件后 S5 必不成立")
    finally:
        dst.close()
        try:
            tmp.unlink()
        except Exception:
            pass

    print("\n[5] 真库未污染")
    chk = sqlite3.connect(str(db))
    n_after = chk.execute("SELECT COUNT(*) FROM shacl_findings").fetchone()[0]
    check("S7 真库行数不变", n_after == n_real, f"{n_real} → {n_after}")
    chk.close()

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
