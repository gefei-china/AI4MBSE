# -*- coding: utf-8 -*-
# CI-OPTIONAL: [nightly] tier=nightly（见 tools/gates_manifest.json）：2026-11-24 D1/D5：推理批次有失败项时不得置 approved（防假批准），Turtle 未支持 OWL 构造须明确告警不静默丢。内存夹具，无外部依赖
"""D1/D5 验证：批次假批准修复 + Turtle 未支持构造告警。

    .venv/Scripts/python.exe tools/verify/verify_cohort_failed_and_owl_unsupported.py

判据：
  D1a approve 有失败项时不置 approved（源码断言：失败分支先于 approved 更新）
  D1b 失败分支返回 partial=True 且带失败明细
  D1c failed 为空时仍正常置 approved（不误伤成功路径）
  D2a Turtle 含 owl:unionOf 时导入回报警（不静默丢）
  D2b 无未支持构造时不产生该告警（不误报）
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


def fixture():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE ontology_types(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
        type_kind TEXT, parent_id TEXT, properties TEXT, constraints TEXT,
        description TEXT, status TEXT, updated_at TEXT);
    """)
    return c


def main():
    print("\n[1] D1 批次假批准修复（源码断言）")
    src = Path("routers/graph_workspace.py").read_text(encoding="utf-8")
    i_failed = src.find('if failed:\n        stat["failed_at"]')
    i_approved = src.find("SET status='approved', stat=?", i_failed if i_failed > 0 else 0)
    check("D1a 失败分支先于 approved 更新", i_failed > 0 and i_approved > i_failed,
          f"failed@{i_failed} approved@{i_approved}")
    check("D1b 失败返回 partial 与明细",
          '"partial": True' in src and '"failed_count": len(failed)' in src)
    check("D1c 成功路径仍置 approved",
          i_approved > 0 and "status='approved'" in src[i_approved - 40:i_approved + 40])

    print("\n[2] D5 Turtle 未支持构造告警")
    import ontology_owl as ow
    ttl_bad = '''@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://ex.org/> .
ex:A a owl:Class ; rdfs:label "甲" ; owl:unionOf ex:B .
'''
    c1 = fixture()
    r1 = ow.from_turtle(ttl_bad, c1)
    check("D2a 未支持构造被明确告警",
          any("未支持" in e for e in r1.get("errors", [])),
          str(r1.get("errors"))[:160])

    ttl_ok = '''@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://ex.org/> .
ex:A a owl:Class ; rdfs:label "乙" .
'''
    c2 = fixture()
    r2 = ow.from_turtle(ttl_ok, c2)
    check("D2b 无未支持构造时不误报",
          not any("未支持" in e for e in r2.get("errors", [])),
          str(r2.get("errors"))[:120])

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
