# -*- coding: utf-8 -*-
"""fingerprint_gate 的判据级验证 + 变异自证。

核心：**门禁必须真的会拦**。
若门禁只是"打个警告"，它对批量脚本等于不存在 —— 所以变异测试要把
「拒绝」退化成「告警」，断言必须抓住。
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, _ROOT)

spec = importlib.util.spec_from_file_location(
    "fpg", os.path.join(_ROOT, "tools", "eval", "fingerprint_gate.py"))
fpg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fpg)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + detail) if (detail and not cond) else ""))


def mkdb(entities=3, sections=("A", "B")):
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(p)
    c.executescript("""
      CREATE TABLE documents(id INTEGER PRIMARY KEY, filename TEXT, parse_status TEXT);
      CREATE TABLE document_chunks(id INTEGER PRIMARY KEY, document_id INTEGER, chunk_index INTEGER,
        source_doc TEXT, section TEXT);
      CREATE TABLE entities(id INTEGER PRIMARY KEY, name TEXT, entity_type TEXT, branch TEXT, status TEXT);
      CREATE TABLE relations(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
        relation_type TEXT, branch TEXT, status TEXT);
      INSERT INTO documents VALUES (1,'a.md','completed');
    """)
    for i, s in enumerate(sections):
        c.execute("INSERT INTO document_chunks VALUES (?,?,?,?,?)", (i + 1, 1, i, 'a.md', s))
    for i in range(entities):
        c.execute("INSERT INTO entities VALUES (?,?,?,?,?)", (i + 1, 'E%d' % i, 'T', 'release', 'active'))
    c.execute("INSERT INTO relations VALUES (1,1,2,'R','release','active')")
    c.commit()
    return c, p


print("=== A. 指纹结构与内容哈希 ===")
conn, path = mkdb()
fp = fpg.fingerprint(conn)
check("A1 四张表行数都在", all(fp.get(t) is not None for t in
                        ("documents", "document_chunks", "entities", "relations")),
      repr(fp))
check("A2 内容哈希非空且是 16 位十六进制",
      len(fp.get("release_entity_names_sha1", "")) == 16 and
      all(ch in "0123456789abcdef" for ch in fp["release_entity_names_sha1"]), repr(fp))
check("A3 schema_version 已置", fp.get("schema_version") == 2)
fp_self = fpg.fingerprint(conn)
check("A4 同库两次指纹完全一致（确定性）", fp == fp_self)

print("\n=== B. 门禁：一致放行 / 漂移拦截 ===")
diffs = fpg.compare(fp, fp_self)
check("B1 一致时无差异", diffs == [], repr(diffs))
d2 = dict(fp); d2["document_chunks"] = fp["document_chunks"] + 1
diffs2 = fpg.compare(d2, fp_self)
check("B2 行数漂移被抓出", len(diffs2) == 1 and "document_chunks" in diffs2[0], repr(diffs2))
# 行数不变但内容变（删A加A'）—— 只比行数会漏判
conn.execute("UPDATE entities SET name='ZZZ' WHERE id=1"); conn.commit()
fp_changed = fpg.fingerprint(conn)
check("B3 行数不变但内容变 → 哈希抓到", fp["entities"] == fp_changed["entities"] and
      fp["release_entity_names_sha1"] != fp_changed["release_entity_names_sha1"],
      "行数同=%s 哈希 %s vs %s" % (fp["entities"], fp["release_entity_names_sha1"],
                                   fp_changed["release_entity_names_sha1"]))
check("B4 指纹结构版本不同 → 明确要求重建",
      any("指纹结构已变" in x for x in fpg.compare({"schema_version": 1}, {"schema_version": 2})))

print("\n=== C. strict 门禁真的 exit（变异：退回告警必须被抓住）===")
import subprocess
# ⚠️ 模板里已用 %s 占位 strict 实参，故路径/root 等一律用 %r/拼接预先填好；
#    不能在替换后再对整串做 % 格式化—— 门禁消息里含 %s 会二次解析炸掉。
script_tpl = (
    "import sys; sys.path.insert(0, r'" + _ROOT + "')\n"
    "import importlib.util, sqlite3\n"
    "spec=importlib.util.spec_from_file_location('fpg', r'" +
    os.path.join(_ROOT, "tools", "eval", "fingerprint_gate.py") + "')\n"
    "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "c=sqlite3.connect(r'" + path + "')\n"
    "m.check_gate({'document_chunks': -999}, c, strict=__STRICT__)\n"
    "print('GATE_PASSED_THROUGH')\n"
)


def _run_src(strict_expr: str):
    src = script_tpl.replace("__STRICT__", strict_expr)
    return subprocess.run([sys.executable, "-X", "utf8", "-c", src],
                          capture_output=True, text=True, encoding="utf-8")


r_strict = _run_src("True")
check("C1 strict=True 漂移时退出码非0", r_strict.returncode == 3,
      "rc=%d out=%r" % (r_strict.returncode, r_strict.stdout[-80:]))
check("C2 strict=True 时未放行（无 GATE_PASSED_THROUGH）",
      "GATE_PASSED_THROUGH" not in r_strict.stdout, r_strict.stdout[-120:])
check("C3 strict=True 时 stderr 有门禁说明", "指纹不匹配" in r_strict.stderr,
      r_strict.stderr[-120:])

r_loose = _run_src("False")
check("C4 strict=False 才放行且返回 False", r_loose.returncode == 0 and
      "GATE_PASSED_THROUGH" in r_loose.stdout, "rc=%d" % r_loose.returncode)

# ★变异自证：把调用点的 strict 改成 False（=门禁退化成告警）⇒ C1/C2 的判据必须 FAIL
r_mut = _run_src("False")
caught = (r_mut.returncode == 0) and ("GATE_PASSED_THROUGH" in r_mut.stdout)
check("C5 变异自证：strict 退化成告警 ⇒ C1/C2 判据会 FAIL", caught,
      "rc=%d out=%r" % (r_mut.returncode, r_mut.stdout[-80:]))

conn.close()
os.unlink(path)

print("\n=== D. 真实库指纹（只读，供记录）===")
rc = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
rfp = fpg.fingerprint(rc)
print("  documents=%s chunks=%s entities=%s relations=%s" %
      (rfp["documents"], rfp["document_chunks"], rfp["entities"], rfp["relations"]))
print("  release_entity_names_sha1=%s" % rfp["release_entity_names_sha1"])
print("  chunk_sections_sha1=%s" % rfp["chunk_sections_sha1"])
rc.close()

print("\n%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)