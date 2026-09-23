# -*- coding: utf-8 -*-
"""P0-3 commit 内容哈希自检：结构 + 全量自检 + 篡改检测 + 覆盖边界 + 变异自证。

设计要点：
- **篡改只在临时库副本上做**（只复制 knowledge_commits 一张表），真库全程只读。
- **【4】覆盖边界是显式断言**：哈希覆盖 branch/parent_id/kind/changes/snapshot；
  `message`/`created_by` **不在**覆盖内 → 篡改 message **不应**被检出。这条是有意披露的取舍，
  写成断言以防后人以为"哈希能防一切篡改"。
- **M1~M3 变异自证**（内存猴补 content_hash_of，用 try/finally 还原）：
  M1 忽略 snapshot → 改 snapshot 的用例必须从"检出"变"漏检"；
  M2 忽略 changes  → 改 changes 的用例必须漏检；
  M3 去掉 sort_keys → "键序不同但语义等价"必须从"不误报"变"误报"。

跑法：<repo>/.venv/Scripts/python.exe -X utf8 tools/verify/verify_commit_hash.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")
TMP_DB = os.path.join(ROOT, "tmp", "p03_tamper_test.db")

from repositories.commit_repo import CommitRepo  # noqa: E402

_ok, _fail = [], []


def chk(name, cond, extra=""):
    (_ok if cond else _fail).append(name)
    print("  %s %s%s" % ("[OK]" if cond else "[FAIL]", name, ("  " + extra) if extra else ""))


def ro():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.row_factory = sqlite3.Row
    return con


def make_copy():
    """把 knowledge_commits 整表复制到临时库（只这一张表，避免复制 282MB 真库）。"""
    src = ro()
    ddl = src.execute("SELECT sql FROM sqlite_master WHERE name='knowledge_commits'").fetchone()[0]
    cur = src.execute("SELECT * FROM knowledge_commits")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    src.close()
    # ⚠️ 不用 os.remove：本机沙箱 safe-delete shim 会拦（实测 OSError），改用 DROP TABLE 原地重建
    t = sqlite3.connect(TMP_DB)
    t.row_factory = sqlite3.Row   # ⚠️ 必须：被验函数用 row["col"] 取值，缺它会 TypeError
    t.execute("DROP TABLE IF EXISTS knowledge_commits")
    t.execute(ddl)
    t.executemany("INSERT INTO knowledge_commits (%s) VALUES (%s)"
                  % (",".join(cols), ",".join("?" * len(cols))), [tuple(r) for r in rows])
    t.commit()
    return t, cols, len(rows)


def mutate(conn, cid, col, value):
    conn.execute("UPDATE knowledge_commits SET %s=? WHERE id=?" % col, (value, cid))
    conn.commit()


def patch_hash(impl):
    """猴补 content_hash_of（保存原实现，返回还原函数）。"""
    orig = CommitRepo.__dict__["content_hash_of"]
    CommitRepo.content_hash_of = staticmethod(impl)
    return lambda: setattr(CommitRepo, "content_hash_of", orig)


def main():
    print("=" * 74)
    print("P0-3 commit 内容哈希自检  |  knowledge_commits")
    print("=" * 74)

    print("\n--- [1] 结构：content_hash 列存在（schema 与真库两侧）---")
    schema_src = open(os.path.join(ROOT, "database", "schema.py"), "rb").read().decode("utf-8", "replace")
    chk("[1] schema.py DDL 含 content_hash", "content_hash TEXT DEFAULT ''" in schema_src)
    mig_src = open(os.path.join(ROOT, "database", "migrations", "columns.py"), "rb").read().decode("utf-8", "replace")
    chk("[1] columns.py 注册了幂等补列", '_add("knowledge_commits", "content_hash"' in mig_src)
    con = ro()
    cols = [r[1] for r in con.execute("PRAGMA table_info(knowledge_commits)")]
    n_total = con.execute("SELECT COUNT(*) FROM knowledge_commits").fetchone()[0]
    con.close()
    chk("[1] 真库表有 content_hash 列", "content_hash" in cols, "→ %d 列" % len(cols))
    print("      （口径：真库 knowledge_commits = %d 行，2026-09-23 实测）" % n_total)

    print("\n--- [2] 全量自检：真库应全绿 ---")
    con = ro()
    r = CommitRepo(con).verify_commits()
    con.close()
    chk("[2] missing == 0（无未回填行）", r["missing"] == 0, "→ %d" % r["missing"])
    chk("[2] mismatched == 0（无被篡改行）", len(r["mismatched"]) == 0, "→ %d" % len(r["mismatched"]))
    chk("[2] ok == True", r["ok"] is True)
    chk("[2] total == 真库行数", r["total"] == n_total, "→ %d" % r["total"])

    print("\n--- [3] 篡改检测（临时副本，真库只读）---")
    t, cols, ncopy = make_copy()
    print("      副本 knowledge_commits = %d 行" % ncopy)
    target = t.execute("SELECT id, changes, snapshot, message FROM knowledge_commits "
                       "WHERE changes NOT IN ('','{}') ORDER BY id DESC LIMIT 1").fetchone()
    cid = target["id"]
    # 3a 改 changes
    obj = json.loads(target["changes"] or "{}")
    obj["__tamper__"] = ["x"]
    mutate(t, cid, "changes", json.dumps(obj, ensure_ascii=False))
    r1 = CommitRepo(t).verify_commits()
    chk("[3] 篡改 changes → 命中该行", any(m["id"] == cid for m in r1["mismatched"]),
        "→ mismatched=%d" % len(r1["mismatched"]))
    chk("[3] 篡改后 ok=False", r1["ok"] is False)
    # 复原
    mutate(t, cid, "changes", target["changes"])
    r1b = CommitRepo(t).verify_commits()
    chk("[3] 复原后 ok=True（证明差异确实来自那次篡改）", r1b["ok"] is True)

    # 3b 改 snapshot
    obj2 = json.loads(target["snapshot"] or "{}")
    if isinstance(obj2, dict):
        obj2["__tamper__"] = 1
        mutate(t, cid, "snapshot", json.dumps(obj2, ensure_ascii=False))
        r2 = CommitRepo(t).verify_commits()
        chk("[3] 篡改 snapshot → 命中该行", any(m["id"] == cid for m in r2["mismatched"]),
            "→ mismatched=%d" % len(r2["mismatched"]))
        mutate(t, cid, "snapshot", target["snapshot"])
    else:
        chk("[3] 篡改 snapshot → 命中该行", False, "（无法构造：snapshot 非 dict）")

    print("\n--- [4] 覆盖边界（显式披露）：message 不在哈希内 ---")
    mutate(t, cid, "message", (target["message"] or "") + " [tampered]")
    r3 = CommitRepo(t).verify_commits()
    chk("[4] 篡改 message 不被检出（设计取舍，已披露）", r3["ok"] is True,
        "→ ok=%s（message 属说明字段，不改语义）" % r3["ok"])
    mutate(t, cid, "message", target["message"])

    print("\n--- [5] 键序无关性：语义等价的 JSON 不应误报 ---")
    multi = t.execute("SELECT id, snapshot FROM knowledge_commits "
                      "WHERE snapshot LIKE '%,%' ORDER BY id LIMIT 1").fetchone()
    reorder_ok = False
    if multi:
        o = json.loads(multi["snapshot"])
        if isinstance(o, dict) and len(o) >= 2:
            rev = "{" + ",".join("%s:%s" % (json.dumps(k), json.dumps(v))
                                 for k, v in reversed(list(o.items()))) + "}"
            mutate(t, multi["id"], "snapshot", rev)
            r4 = CommitRepo(t).verify_commits()
            reorder_ok = r4["ok"] is True
            chk("[5] 键序重排的等价 JSON 不误报", reorder_ok, "→ id=%s keys=%d" % (multi["id"], len(o)))
            mutate(t, multi["id"], "snapshot", multi["snapshot"])
        else:
            chk("[5] 键序重排的等价 JSON 不误报", False, "（无多键 snapshot 行可构造）")
    else:
        chk("[5] 键序重排的等价 JSON 不误报", False, "（找不到多键 snapshot 行，用例未覆盖）")

    t.close()

    print("\n" + "=" * 74)
    print("变异自证（内存猴补 content_hash_of；try/finally 还原）")
    print("=" * 74)
    base_ok, base_fail = list(_ok), list(_fail)

    def rebase(conn, impl):
        """用 impl 重算全表 content_hash —— 变异自证的必要前提。

        ⚠️ 踩过的坑：只猴补算法而不重建基线，会让**未篡改行也全部不匹配**，
        于是「篡改某列后是否漏检」根本无从判断（本脚本第一版 M1/M2 即因此假 FAIL）。
        """
        for r in conn.execute("SELECT id, branch, parent_id, kind, changes, snapshot "
                              "FROM knowledge_commits").fetchall():
            conn.execute("UPDATE knowledge_commits SET content_hash=? WHERE id=?",
                         (impl(r["branch"], r["parent_id"], r["kind"], r["changes"], r["snapshot"]),
                          r["id"]))
        conn.commit()

    t2, _, _ = make_copy()
    row2 = t2.execute("SELECT id, branch, parent_id, kind, changes, snapshot FROM knowledge_commits "
                      "WHERE changes NOT IN ('','{}') ORDER BY id DESC LIMIT 1").fetchone()
    cid2, snap2, chg2 = row2["id"], row2["snapshot"], row2["changes"]

    # M1：忽略 snapshot（用真实实现包一层，把 snapshot 置空）
    orig_impl = CommitRepo.__dict__["content_hash_of"]

    def m1_impl(branch, parent_id, kind, changes, snapshot):
        return orig_impl.__func__(branch, parent_id, kind, changes, {})

    restore = patch_hash(m1_impl)
    rebase(t2, m1_impl)
    chk("M1 变异基线自洽（重建后 ok=True）", CommitRepo(t2).verify_commits()["ok"] is True)
    obj3 = json.loads(snap2 or "{}")
    obj3["__tamper__"] = 1
    mutate(t2, cid2, "snapshot", json.dumps(obj3, ensure_ascii=False))
    r_m1 = CommitRepo(t2).verify_commits()
    m1_leaked = not any(m["id"] == cid2 for m in r_m1["mismatched"])
    restore()
    mutate(t2, cid2, "snapshot", snap2)
    chk("M1 忽略 snapshot 后，改 snapshot 被漏检（证明 [3] 的 snapshot 用例非空转）", m1_leaked,
        "→ leaked=%s" % m1_leaked)

    # M2：忽略 changes
    def m2_impl(branch, parent_id, kind, changes, snapshot):
        return orig_impl.__func__(branch, parent_id, kind, {}, snapshot)

    restore = patch_hash(m2_impl)
    rebase(t2, m2_impl)
    chk("M2 变异基线自洽（重建后 ok=True）", CommitRepo(t2).verify_commits()["ok"] is True)
    obj4 = json.loads(chg2 or "{}")
    obj4["__tamper__"] = ["x"]
    mutate(t2, cid2, "changes", json.dumps(obj4, ensure_ascii=False))
    r_m2 = CommitRepo(t2).verify_commits()
    m2_leaked = not any(m["id"] == cid2 for m in r_m2["mismatched"])
    restore()
    mutate(t2, cid2, "changes", chg2)
    chk("M2 忽略 changes 后，改 changes 被漏检（证明 [3] 的 changes 用例非空转）", m2_leaked,
        "→ leaked=%s" % m2_leaked)

    # M3：去掉 sort_keys（键序敏感）
    def m3_impl(branch, parent_id, kind, changes, snapshot):
        def _n(v):
            if isinstance(v, (str, bytes)):
                try:
                    v = json.loads(v or "{}")
                except Exception:
                    v = {}
            if not isinstance(v, dict):
                v = {}
            return json.dumps(v, separators=(",", ":"), ensure_ascii=False)  # 无 sort_keys
        raw = "|".join([str(branch or ""), "" if parent_id is None else str(parent_id),
                        str(kind or ""), _n(changes), _n(snapshot)])
        import hashlib
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    o3 = json.loads(multi["snapshot"]) if multi else None
    if isinstance(o3, dict) and len(o3) >= 2:
        t3, _, _ = make_copy()
        restore = patch_hash(m3_impl)
        rebase(t3, m3_impl)
        chk("M3 变异基线自洽（重建后 ok=True）", CommitRepo(t3).verify_commits()["ok"] is True)
        rev = "{" + ",".join("%s:%s" % (json.dumps(k), json.dumps(v))
                             for k, v in reversed(list(o3.items()))) + "}"
        mutate(t3, multi["id"], "snapshot", rev)
        r_m3 = CommitRepo(t3).verify_commits()
        restore()
        chk("M3 去掉 sort_keys 后，等价键序被误报（证明 [5] 非空转）", not r_m3["ok"],
            "→ ok=%s" % r_m3["ok"])
        t3.close()
    else:
        print("  [SKIP] M3：无多键 snapshot 行可构造（不假装通过）")
    t2.close()

    print("\n" + "=" * 74)
    print("基线（变异前）：OK=%d FAIL=%d" % (len(base_ok), len(base_fail)))
    if base_fail:
        print("基线失败项：%s" % base_fail)
    print("M1/M2/M3 = 断言非空转的证据")
    print("=" * 74)
    return 1 if base_fail else 0


if __name__ == "__main__":
    sys.exit(main())
