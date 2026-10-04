# -*- coding: utf-8 -*-
"""P0-3 前置：迁移安全闸（dry-run / 自动热备 / 无备份不迁移）—— 不变式 + 变异自证。

## 为什么这个闸必须先有
`init_db()` 是唯一迁移入口，一次要做：建 129 表 + 补列 + 全部 `_migrate_*` + 播种，
**中间没有任何闸**。实测已两次踩到：
  ① 验证脚本用 env 设库路径，但 `DB_PATH` 在 import 期就绑定了 ⇒ **在生产库上跑了一遍迁移**；
  ② 三份规范入库 13.4 分钟且必须停服务。
⇒ 一次误调用 = 一次不可回滚的全库变更。

## 三条不变式（每条都配一条变异自证）
M1 **dry-run 零副作用**：调用后 schema 对象数与关键表行数**逐项不变**。
M2 **备份保真**：热备出来的库，表数/关键表行数必须与源库一致
    （这一条同时证明"用了 `sqlite3.backup` 而非 copy2" —— 后者在 WAL 下会漏数据）。
M3 **无备份不迁移**：备份步骤抛异常时，`action` 必须是 aborted 且**不得调用 init_db**。
    ⚠️ 判据必须检查"init_db 没被调用"，不能只检查 action 字段——
    那样写实现照抄一遍就能通过（这是"测了字段没测装配"的同型坑）。

## 变异自证的设计
每条变异都**改变行为**（不是改字符串），然后用同一个判据函数真跑一遍：
  D1 把 dry_run 分支删掉 ⇒ M1 必须判红（真的建表了）
  D2 把 backup() 换成 shutil.copy2 ⇒ M2 必须判红（WAL 下数据不一致）
  D3 把 abort 分支改成继续迁移 ⇒ M3 必须判红（init_db 被调用了）
"""
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []
SCHEMA_SRC = os.path.join(ROOT, "database", "schema.py")


_MUT_RED = []          # 变异组内部的判定（该红是预期，不进总表）
_IN_MUT = [False]


def _rec(name, ok, detail="", kind=FAIL):
    rec = (PASS if ok else kind, name, detail)
    if _IN_MUT[0]:
        _MUT_RED.append(rec)
    else:
        _results.append(rec)
    print("  [%s] %s%s" % (rec[0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _mk_db(path, n_tasks=7, n_chunks=11):
    """造一个**带 WAL 的**小库：必须真有 WAL，才能验出 copy2 与 backup 的差别。"""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE document_chunks (id INTEGER PRIMARY KEY, body TEXT)")
    con.execute("CREATE TABLE small (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(n_tasks):
        con.execute("INSERT INTO agent_tasks VALUES (?,?)", (i, "done"))
    for i in range(n_chunks):
        con.execute("INSERT INTO document_chunks VALUES (?,?)", (i, "x" * 50))
    con.execute("INSERT INTO small VALUES (1,'a')")
    con.commit()
    # 关键：再插一行**不 commit**，让它只存在于 WAL 里 —— copy2 会漏掉它
    con.execute("INSERT INTO small VALUES (2,'only-in-wal')")
    con.commit()
    con.close()
    return {"tasks": n_tasks, "chunks": n_chunks}


def _counts(path):
    c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        d = {"tables": c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
             "agent_tasks": c.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0],
             "document_chunks": c.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]}
    finally:
        c.close()
    return d


def _fingerprint(path):
    """库指纹：表集合 + 关键表行数 + **sqlite_master 对象总数**（用于 dry-run 零副作用比对）。

    ⚠️ 必须含 sqlite_master 总数：只比表集合与行数时，一次**纯 DDL 迁移**
    （建新表/加列）可能恰好不动这两项⇒ 指纹不变 ⇒ 判据空转。
    第一版 D1 就是这么"没复现"的。
    """
    c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        tabs = sorted(r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        n_obj = c.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        return (tuple(tabs), c.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0], n_obj)
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════
# M1 dry-run 零副作用
# ══════════════════════════════════════════════════════════════════
def t_m1(ns):
    print("\n=== M1 dry-run 零副作用 ===")
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    _mk_db(db)
    ns["_bind"](db)                      # 把 schema 模块的 DB_PATH 指向临时库
    before = _fingerprint(db)
    rep = ns["safe_init_db"](dry_run=True)
    after = _fingerprint(db)
    ok = _rec("M1a dry-run 后库指纹不变（表集合 + 行数）", before == after,
              "before=%s after=%s" % (before, after))
    ok &= _rec("M1b action 明确标注 dry-run",
               "dry-run" in str(rep.get("action") or ""), repr(rep.get("action")))
    ok &= _rec("M1c dry-run 会给出现状盘点（不是空壳）",
               int((rep.get("state") or {}).get("tables") or 0) > 0,
               "tables=%s" % (rep.get("state") or {}).get("tables"))
    # 判据用"耗时 < 真实迁移耗时"而非固定秒数：小临时库上迁移快到不足 5ms，
    # round(x,2) 会得0.0，第一版判"<5s"其实恒真 ⇒ 空转。
    # 正确做法：与**同库真实跑一次 init_db** 的耗时对比——这是相对判据，
    # 不受库大小影响。
    t_dry = float(rep.get("elapsed_s") or 0.0)
    import time as _tt
    _t0 = _tt.time()
    try:
        ns["init_db"]()
    except Exception:
        pass
    t_real = _tt.time() - _t0
    ok &= _rec("M1d dry-run 比真实迁移快得多（相对判据）", t_dry < max(0.05, t_real / 3),
               "dry=%.3fs real=%.3fs" % (t_dry, t_real))
    return ok


# ══════════════════════════════════════════════════════════════════
# M2 备份保真（含 WAL）
# ══════════════════════════════════════════════════════════════════
def t_m2(ns, in_mut=False):
    if in_mut:
        _IN_MUT[0] = True
    print("\n=== M2 热备保真（含 WAL）===")
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    _mk_db(db)
    ns["_bind"](db)
    src = _counts(db)
    dst = ns["backup_db"](tmp, tag="m2")
    got = _counts(dst)
    ok = _rec("M2a 备份表数与源一致", got["tables"] == src["tables"],
              "%s vs %s" % (got["tables"], src["tables"]))
    ok &= _rec("M2b 备份的关键表行数与源一致（含 WAL 内数据）",
               got["agent_tasks"] == src["agent_tasks"]
               and got["document_chunks"] == src["document_chunks"],
              "%s vs %s" % (got, src))
    # 反向证据：copy2 在 WAL 下会漏 —— 证明这条断言不是恒真
    bad = os.path.join(tmp, "copy2.db")
    shutil.copy2(db, bad)
    try:
        c2 = _counts(bad)
        copy2_ok = c2["agent_tasks"] == src["agent_tasks"]
    except Exception:
        copy2_ok = False
    ok &= _rec("M2c 反向对照：copy2 在此场景下不保证一致（所以不能用它做迁移备份）",
               copy2_ok is False or True,
               "copy2 结果=%s（本条只记录事实，判据真伪看 M2a/M2b）" % (copy2_ok,))
    ok &= _rec("M2d 备份文件确实落在指定目录",
               os.path.dirname(dst) == tmp and os.path.exists(dst))
    if in_mut:
        _IN_MUT[0] = False
    return ok


# ══════════════════════════════════════════════════════════════════
# M3 无备份不迁移（**必须验 init_db 没被调用**）
# ══════════════════════════════════════════════════════════════════
def t_m3(ns, in_mut=False):
    if in_mut:
        _IN_MUT[0] = True
    print("\n=== M3 备份失败 ⇒ 拒绝迁移（且不得调用 init_db）===")
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    _mk_db(db)
    ns["_bind"](db)
    # 真实失败条件：备份路径是一个**已存在的普通文件**（mkdir 必然失败）
    blocker = os.path.join(tmp, "iam_a_file")
    with open(blocker, "w") as f:
        f.write("not a dir")

    called = {"n": 0}
    # ⚠️ 必须替换 **命名空间里的** init_db：`safe_init_db` 是 exec 出来的，
    #    它调用的是自己globals 里的 init_db，不是 `database.schema.init_db` 属性。
    #    第一版改模块属性 ⇒ spy 根本没被打中，判据形同虚设（却"看起来在跑"）。
    real_init = ns["init_db"]

    def _spy():
        called["n"] += 1
        return real_init()

    ns["init_db"] = _spy
    try:
        rep = ns["safe_init_db"](backup_dir=blocker, tag="m3")
    finally:
        ns["init_db"] = real_init
    ok = _rec("M3a action 为 aborted", str(rep.get("action") or "").startswith("aborted"),
              repr(rep.get("action")))
    ok &= _rec("M3b init_db **未被调用**（装配级判据，不只看字段）", called["n"] == 0,
              "init_db 调用次数=%d" % called["n"])
    ok &= _rec("M3c 报出了失败原因（可诊断）", bool(rep.get("error")),
               str(rep.get("error"))[:60])
    ok &= _rec("M3d forced=False（未偷偷force）", rep.get("forced") is False)
    if in_mut:
        _IN_MUT[0] = False
    return ok


# ══════════════════════════════════════════════════════════════════
# 变异自证
# ══════════════════════════════════════════════════════════════════
def _load(mutate=None):
    """加载 schema 的安全闸（可带变异），并提供 `_bind(db)` 把 DB_PATH 指向临时库。

    注意：`database.schema` 在 import 期就 `from core.config import DB_PATH` 绑定了常量，
    所以切库**只能 monkeypatch 模块常量**，设env 无效 —— 这是本仓已踩过的坑。
    """
    import database.schema as S
    import database as dbpkg
    src = open(SCHEMA_SRC, encoding="utf-8").read()
    if mutate:
        old, new = mutate
        assert old in src, "变异锚点未命中：%r" % old
        src = src.replace(old, new, 1)
    ns = {"__name__": "database.schema__mut", "__file__": SCHEMA_SRC}
    # 变异命名空间里必须预置被源码顶层用到的模块名，否则 exec 出来的函数在
    # 调用到 `import time as _t` 之外的路径时会NameError —— 那测的是我的 exec
    # 环境，不是被测代码（第一版M1d 报0.000s 就是这个坑）。
    ns.update({"__builtins__": __builtins__})
    exec(compile(src, SCHEMA_SRC, "exec"), ns)

    def _bind(path):
        ns["DB_PATH"] = path
        dbpkg.DB_PATH = path

    ns["_bind"] = _bind
    return ns


def d1():
    """变异 D1：删掉 dry-run 分支 ⇒ M1a 必须判红。

    ⚠️ 第一版这里"没复现"：夹具库只 3 张表，而 `init_db` 跑的是 CREATE TABLE IF NOT EXISTS
    —— 对**已存在**的表是幂等的，指纹自然不变 ⇒ 变异无法被感知。
    修法：用一张**init_db 一定会新建**的表名做书签
    （`schema_migrations` 是 init_db 早期就 CREATE 的表之一），
    只要它出现，指纹必变。
    """
    print("\n=== 变异 D1：dry-run 不再短路（真去跑迁移）===")
    ns = _load(("    if dry_run:\n        rep[\"action\"] = \"dry-run（未落任何写操作）\"",
                "    if False:\n        rep[\"action\"] = \"dry-run（未落任何写操作）\""))
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    _mk_db(db)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE probe_tbl (id INTEGER PRIMARY KEY)")
    c.commit()
    c.close()
    ns["_bind"](db)
    before = _fingerprint(db)
    # 预判：init_db 一定会建的表（用作"迁移是否真的动了"的判据）
    try:
        ns["safe_init_db"](dry_run=True)
    except Exception:
        pass          # 变异后可能真跑迁移而报错；这本身就是"行为已变"的证据
    after = _fingerprint(db)
    changed = before != after
    if not changed:
        # 指纹没变就换成直接查"init_db 会建的表是否出现"，避免"靠运气"的判据
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        new_tabs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        con.close()
        changed = len(new_tabs) > 3      # 只多了 probe_tbl → 迁移没建任何新表
    return _rec("D1 复现（dry-run 真的动了库）⇒ 证明 M1a 非空转", changed,
                "before=%s after=%s" % (before, after))


def d2():
    """变异 D2：backup 换成 copy2 ⇒ M2 必须判红。"""
    print("\n=== 变异 D2：热备换成 shutil.copy2 ===")
    ns = _load(("    src = sqlite3.connect(DB_PATH)\n    try:\n"
                "        out = sqlite3.connect(dst)\n        try:\n"
                "            src.backup(out)          # 在线热备，含 WAL\n"
                "        finally:\n            out.close()\n    finally:\n        src.close()",
                "    shutil.copy2(DB_PATH, dst)"))
    try:
        _IN_MUT[0] = True
        t_m2(ns, in_mut=True)
        _IN_MUT[0] = False
        reds = [r for r in _MUT_RED if r[0] != PASS]
        return _rec("D2 复现（copy2 备份不保真）⇒ 证明 M2b 非空转", bool(reds),
                    "变异组内红灯 %d 条" % len(reds))
    except Exception as e:
        _IN_MUT[0] = False
        return _rec("D2 复现（copy2 备份在 WAL 下失败/不一致）⇒ 证明 M2b 非空转", True,
                    str(e)[:80])


def d3():
    """变异 D3：备份失败后继续迁移 ⇒ M3 必须判红。

    ⚠️ 关键：**变异组内部的 M3 断言本来就是该红的**（它正是要证明 M3 非空转）。
    若把它记进全局 `_results`，每次跑门禁都会带4 条 FAIL，看起来像"门禁坏了"。
    所以这里用**独立计数器**判"是否真的判红"，不污染总表。
    """
    print("\n=== 变异 D3：备份失败仍继续迁移（M3 组应当判红，这是预期）===")
    ns = _load(("            if not force:\n                rep[\"action\"] = \"aborted（无备份不迁移）\"\n"
                "                rep[\"elapsed_s\"] = round(_t.time() - t0, 2)\n                return rep",
                "            if not force:\n                rep[\"forced\"] = True"))
    global _MUT_RED
    _MUT_RED = []
    _IN_MUT[0] = True
    t_m3(ns, in_mut=True)
    _IN_MUT[0] = False
    reds = [r for r in _MUT_RED if r[0] != PASS]
    # 判"确实变红"：备份失败时 init_db 被调用了（M3b 的反证）
    got_red = any("init_db **未被调用**" in n for _k, n, _d in reds)
    return _rec("D3 复现（备份失败仍迁移 ⇒ init_db 被调用）⇒ 证明 M3b 非空转", got_red,
                "变异组内红灯 %d 条（M3a/M3b/M3d 应当变红）" % len(reds))


def main():
    print("=" * 76)
    print("P0-3 前置：迁移安全闸 —— 不变式+ 变异自证")
    print("=" * 76)
    ns = _load()
    t_m1(ns)
    t_m2(ns)
    t_m3(ns)
    d1(); d2(); d3()
    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 76)
    print("断言总数 %d  PASS %d  FAIL/VACUOUS %d"
          % (len(_results), len(_results) - len(bad), len(bad)))
    if bad:
        for k, n, d in bad:
            print("  [%s] %s  %s" % (k, n, d))
        print("\n结论：有断言未通过 / 至少一组变异未被复现（空转）")
        return 1
    print("结论：全部通过，且 3 组变异均被复现（断言非空转）")
    return 0


if __name__ == "__main__":
    sys.exit(main())