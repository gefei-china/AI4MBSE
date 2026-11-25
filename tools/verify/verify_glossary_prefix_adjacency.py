# -*- coding: utf-8 -*-
"""R2 回归门禁（2026-11-11）：规范词不得自带会被重复拼接的限定前缀。

## 守的是什么
`C-语法规范` 的 pref_label 曾是 `SysML_V2 语法规范`（规范词自带消歧前缀）。
它与 `SysML V2 → SysML_V2` 在同一句里相邻命中时，两个 canonical 各带一次
`SysML_V2`，单遍 span 替换后拼成 `SysML_V2 SysML_V2 语法规范要求`。
两条词条各自都正确，错在**规范名把上下文限定写进了值里**。

R2 的修法是数据治理：把 pref_label 对齐为首选术语（`语法规范`），归一化变成
等值 no-op。因此本门禁把「数据不变式」而不是「替换代码」钉住。

## 断言
  A1 结构性：所有 preferred 术语的 pref_label 必须等于 term 本身（全表 0 例外）
  A2 跨词条拼接：`SysML v2 语法规范要求` → 无紧邻重复前缀
  A3 无空格拼接：`v2代码规范` → 无紧邻重复前缀
  A4 真库全量：输出中紧邻重复前缀 = 0（存量 `SysML_SysML` 污染行不判红）
  A5 迁移幂等：同一夹具跑两次，第二次 0 更新
  A6 变异：迁移写成 no-op ⇒ A1/A2 必须翻红（证明迁移真被消费）
  A7 变异：判据改成「出现次数 ≥2」⇒ 正常长文本被误判（证明必须用紧邻口径）

A7 为什么必须有：真库里有 114 条长任务上下文，`SysML_V2` 分散出现 ≥2 次属正常；
只数次数会把它们全报成缺陷（假阳性），比漏报更消耗信任。
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL, SKIPPED = [], [], []
DUP = "SysML_V2SysML_V2"
OLD_LABEL = "SysML_V2 语法规范"
NEW_LABEL = "语法规范"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if cond:
        print("  PASS %s" % name, flush=True)
    else:
        print("  FAIL %s  <- %s" % (name, detail), flush=True)


def skip(name, why):
    SKIPPED.append(name)
    print("  SKIP %s  <- %s" % (name, why), flush=True)


def flat(s):
    """去掉空白后判断紧邻重复 —— 判据口径，别改成"出现次数"。"""
    return re.sub(r"\s+", "", s or "")


def has_adjacent_prefix(text):
    return DUP in flat(text)


def has_adjacent_prefix_mutant(text):
    """A7 用的假阳性判据：只数出现次数，不看是否紧邻。"""
    return flat(text).count("SysML_V2") >= 2


def _fixture():
    """最小可跑夹具：真迁移建 glossary/trace（含概念层），再补 changelog。

    ★ 2026-11-15：夹具的建表必须 **IF NOT EXISTS**。
      本夹具原先写 `CREATE TABLE glossary_concepts`（无 IF NOT EXISTS），
      其前提是"产品迁移**不建**概念层这两张表，由夹具补上"。
      而 2026-11-15 修掉的真缺陷正是「产品根本不建 glossary_concepts/glossary_terms」
      （⇒ 任何新部署的概念层永久为空），补进 `_migrate_glossary_tables` 之后，
      这条夹具当场 `table glossary_concepts already exists` 崩掉。
      ⇒ 夹具应当**幂等**：产品建了就复用，没建才补。假设"产品不做某件事"
        写进夹具，会在产品补上那天变成假红。
    """
    from database.migrations.glossary import _migrate_glossary_tables
    from glossary import GlossaryMatcher

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _migrate_glossary_tables(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS glossary_concepts (
            concept_id TEXT PRIMARY KEY, pref_label TEXT, definition TEXT DEFAULT '',
            domain TEXT DEFAULT '', concept_status TEXT DEFAULT 'approved',
            intent TEXT DEFAULT '', boost REAL DEFAULT 1.0,
            maps_to_class TEXT DEFAULT '', maps_to_inst TEXT DEFAULT '',
            replaced_by TEXT DEFAULT '', version INTEGER DEFAULT 1,
            note TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS glossary_terms (
            id INTEGER PRIMARY KEY, concept_id TEXT, term TEXT, lang TEXT DEFAULT 'zh',
            term_kind TEXT DEFAULT 'preferred', term_status TEXT DEFAULT 'approved',
            use_in_matching INTEGER, note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS glossary_changelog (
            id INTEGER PRIMARY KEY, concept_id TEXT, action TEXT, detail TEXT,
            reason TEXT, operator TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.execute("INSERT INTO glossary_concepts (concept_id,pref_label,domain,concept_status) "
                 "VALUES (?,?,?,'approved')",
                 ("C-语法规范", OLD_LABEL, "sysml_norm"))
    conn.execute("INSERT INTO glossary_concepts (concept_id,pref_label,domain,concept_status) "
                 "VALUES (?,?,?,'approved')",
                 ("C-SysML_V2", "SysML_V2", "sysml_norm"))
    # ⚠️ `term_status` / `concept_status` 必须**显式**写入，不能依赖表默认值。
    #   2026-11-15：产品迁移补建这两张表后，默认值来自**产品**（term_status 默认
    #   'admitted'、concept_status 默认 'candidate'），而本夹具原先假设默认值是自己
    #   DDL 里的 'approved' ⇒ 插入行变成 'admitted' ⇒ `_mismatch()` 的
    #   `term_status='approved'` 过滤查不到 ⇒ A0「初始应有 1 条不一致」变成 0 条而失败。
    #   ⇒ 夹具数据必须自带状态，与"表是谁建的"解耦。
    for cid, term, kind in (
        ("C-语法规范", "语法规范", "preferred"),
        ("C-语法规范", "代码规范", "synonym"),
        ("C-SysML_V2", "SysML_V2", "preferred"),
        ("C-SysML_V2", "SysML V2", "synonym"),
        ("C-SysML_V2", "v2", "alias"),
        ("C-SysML_V2", "sysml", "alias"),
    ):
        conn.execute("INSERT INTO glossary_terms (concept_id,term,term_kind,term_status) "
                     "VALUES (?,?,?,'approved')", (cid, term, kind))
    for term, canonical in (("语法规范", OLD_LABEL), ("代码规范", OLD_LABEL),
                            ("v2", "SysML_V2")):
        conn.execute("INSERT INTO glossary (user_term,canonical_term,kind,active) "
                     "VALUES (?,?,'intent',1)", (term, canonical))
    conn.commit()
    return conn, GlossaryMatcher


def _mismatch(conn):
    return [dict(r) for r in conn.execute(
        "SELECT t.term, c.pref_label FROM glossary_terms t "
        "JOIN glossary_concepts c ON c.concept_id=t.concept_id "
        "WHERE t.term_kind='preferred' AND t.term_status='approved' "
        "AND c.pref_label<>t.term")]


def load_migration(mutate=None):
    """加载迁移模块源码，可选文本变换后 exec —— 变异自证用。"""
    path = os.path.join(ROOT, "database", "migrations", "core/domain/glossary.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if mutate:
        src = mutate(src)
    ns = {"__name__": "glossary_migration_mut"}
    exec(compile(src, "core/domain/glossary.py", "exec"), ns)
    return ns["_migrate_glossary_canonical_align"]


def run_scenario(migrate_fn, tag):
    """在干净夹具上跑一次 R2 场景，返回 (A1 通过?, A2 通过?, A3 通过?)。"""
    conn, Matcher = _fixture()
    try:
        migrate_fn(conn)
        a1 = not _mismatch(conn)
        m = Matcher(conn)
        a2 = not has_adjacent_prefix(m.normalize("SysML v2 语法规范要求")[0])
        a3 = not has_adjacent_prefix(m.normalize("v2代码规范")[0])
        print("  · [%s] pref_label=%r / %r / %r" % (
            tag,
            conn.execute("SELECT pref_label FROM glossary_concepts WHERE concept_id='C-语法规范'")
                .fetchone()[0],
            m.normalize("SysML v2 语法规范要求")[0],
            m.normalize("v2代码规范")[0]), flush=True)
        return a1, a2, a3
    finally:
        conn.close()


def main():
    real = load_migration()

    print("\n[1] 结构性不变式（preferred 术语 ≡ 规范名）", flush=True)
    conn, Matcher = _fixture()
    try:
        before = _mismatch(conn)
        check("A0 夹具初始状态是 R2 之前（有 1 条不一致）", len(before) == 1,
              "实得 %d 条：%r" % (len(before), before))
        r1 = real(conn)
        check("A1 preferred 术语的 pref_label 全部与 term 等值", not _mismatch(conn),
              "不一致：%r" % _mismatch(conn))
        r2 = real(conn)
        check("A5 迁移幂等（第二次 0 更新，0 留痕）",
              r2["concept_updated"] == 0 and r2["legacy_updated"] == 0
              and r2["changelog"] == 0, "实得 %r" % r2)
        check("A5b 迁移首次确实改了概念层与旧表",
              r1["concept_updated"] == 1 and r1["legacy_updated"] == 2,
              "实得 %r" % r1)
        m = Matcher(conn)
        n2 = m.normalize("SysML v2 语法规范要求")[0]
        n3 = m.normalize("v2代码规范")[0]
        check("A2 跨词条拼接不出现紧邻重复前缀", not has_adjacent_prefix(n2),
              "实得 %r" % n2)
        check("A3 无空格拼接不出现紧邻重复前缀", not has_adjacent_prefix(n3),
              "实得 %r" % n3)
        check("A2b 归一化后语义仍含 SysML_V2 与语法规范各一次",
              n2 == "SysML_V2 语法规范要求", "实得 %r" % n2)
    finally:
        conn.close()

    print("\n[2] 真库全量（有 mbse.db 才跑）", flush=True)
    db = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(db):
        skip("A4 真库全量", "无 mbse.db（CI 干净库）")
    else:
        import contextlib
        c2 = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        c2.row_factory = sqlite3.Row
        with contextlib.closing(c2):
            from glossary import GlossaryMatcher as GM
            gm = GM(c2)
            qs = [r[0] for r in c2.execute(
                "SELECT COALESCE(NULLIF(TRIM(query_raw),''),query) q FROM query_trace "
                "WHERE query IS NOT NULL AND TRIM(query)<>''")]
            seen, produced, legacy = set(), [], 0
            for q in qs:
                if not q or q in seen:
                    continue
                seen.add(q)
                out, _ = gm.normalize(q)
                if has_adjacent_prefix(out):
                    produced.append((q[:40], out[:60]))
                if "SysML_SysML" in flat(q):
                    legacy += 1
            check("A4 真库全量（%d 条）归一化后紧邻重复前缀 = 0" % len(seen),
                  not produced, "实得 %d 条：%r" % (len(produced), produced[:2]))
            print("  · 存量污染行 %d 条（不判红，属历史不可逆）" % legacy, flush=True)
            real_bad = _mismatch(c2)
            check("A1b 真库 preferred 术语与规范名 0 条不一致", not real_bad,
                  "实得 %d 条：%r" % (len(real_bad), real_bad[:2]))

    print("\n[3] 变异自证（判据必须能翻红）", flush=True)

    def _mut_noop(src):
        anchor = '    new = "语法规范"'
        assert anchor in src, "M1 锚点失效（迁移常量已被改写）"
        return src.replace(anchor, '    new = old  # 变异：迁移退化成 no-op', 1)

    try:
        mut = load_migration(_mut_noop)
        v1, v2, v3 = run_scenario(mut, "M1 迁移 no-op")
        check("M1 迁移失效 ⇒ A1/A2 至少一项翻红", not (v1 and v2),
              "变异后仍全绿：%r" % ((v1, v2, v3),))
    except AssertionError as e:
        check("M1 变异可施加", False, str(e))

    # 两条判据必须作用于**同一条真实归一化输出**（而不是原始串）：
    # 原始串里写的是 `SysML v2`，次数口径连一次都数不到，会假装"无假阳性"。
    _conn, _MatcherCls = _fixture()
    try:
        real(_conn)
        normal_long = _MatcherCls(_conn).normalize(
            "先按 SysML v2 检查模型，再用 SysML v2 语法规范复核")[0]
    finally:
        _conn.close()
    check("M2 紧邻判据在正常长文本上不误报（%r）" % normal_long[:36],
          not has_adjacent_prefix(normal_long), "实得误报：%r" % normal_long)
    check("M2 变异（只数出现次数）在同一文本上假阳性 ⇒ 判据口径有牙齿",
          has_adjacent_prefix_mutant(normal_long),
          "变异体未翻红，说明两条判据等价，A7 无意义")

    print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL), len(SKIPPED)),
          flush=True)
    for f in FAIL:
        print("  ✗ %s" % f, flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
