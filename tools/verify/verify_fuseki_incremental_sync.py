# -*- coding: utf-8 -*-
"""P0-0 第九批门禁（2026-11-23）：写后增量投影（Fuseki 派生镜像）。

## 要挡住什么

1. **全量/增量两套 TTL 公式**：增量自己写一份 IRI/转义规则，早晚漂移；
2. **按 subject 一把清**：关系三元组 subject 是源实体，会误删同主体的其它边；
3. **跨分支误删**：同 id 多分支当前行，删本分支不能把它分支的三元组删掉；
4. **未提交就投影**：写入口内直接 flush ⇒ rollback 后 Fuseki 留幽灵三元组；
5. **投影失败阻断业务写**：派生视图问题不能把已成功的请求改成失败；
6. **开关失效**：默认关的开关被绕过（每次写都打网络）；
7. **连接引用泄漏**：sqlite3.Connection 不能 setattr/weakref，pending 字典必须 pop。

判据 F1~F13 + 真变异 M1~M4。临时库隔离，零生产库写、零真实网络。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_fuseki_incr_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db                      # noqa: E402
init_db()

from core import fuseki_projection as fp                   # noqa: E402
from core.graph_write_gate import write_entity             # noqa: E402
from services import sparql_sync as ss                     # noqa: E402

_RESULTS = []
_CAUGHT = {"m1": False, "m2": False, "m3": False, "m4": False}


def ck(name, cond, detail=""):
    _RESULTS.append((bool(cond), name, str(detail)[:300] if not cond else ""))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)[:300]) if not cond else ""))


class FakeCli:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def update(self, q, timeout=None):
        if self.fail:
            raise ss.FusekiUnavailable("Fuseki 不可达（注入）")
        self.calls.append(q)
        return 200


def _fixture(c):
    c.execute("DELETE FROM relations")
    c.execute("DELETE FROM entities")
    c.execute("DELETE FROM ontology_types")
    # ontology_types / relations 的 id 是 INTEGER（临时库 PRAGMA 实测）
    c.execute("INSERT INTO ontology_types (id,name,type_kind,parent_id,properties) "
              "VALUES (1,'需求','entity',NULL,'{}')")
    for eid, name, branch in (("V_E1", "甲", "dev"), ("V_E1", "甲-发布", "rel"),
                              ("V_E2", "乙", "dev"), ("V_E3", "丙", "dev")):
        c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties,is_current) "
                  "VALUES (?,?,?,'reviewed','需求','{}',1)", (eid, name, branch))
    c.execute("INSERT INTO relations (id,source_id,target_id,relation_type,branch,properties,status) "
              "VALUES ('101','V_E1','V_E2','R_OLD','dev','{}','reviewed')")
    c.execute("INSERT INTO relations (id,source_id,target_id,relation_type,branch,properties,status) "
              "VALUES ('102','V_E1','V_E3','R_KEEP','dev','{}','reviewed')")
    c.commit()


def _calls_of(fake, verb):
    return [q for q in fake.calls if (" %s DATA {" % verb) in q]


def _checks(c):
    _fixture(c)

    # F1 全量/增量同公式：全量 TTL 行集合 == 各对象快照并集
    all_base = {ln.strip() for ln in (ss.build_ontology_ttl(c) + "\n"
                + ss.build_entities_ttl(c)).splitlines() if ln.strip().endswith(" .")}
    union = set()
    for t in c.execute("SELECT id FROM ontology_types"):
        union |= fp.projection_snapshot(c, "ontology_type", t[0])
    for e in c.execute("SELECT DISTINCT id FROM entities"):
        union |= fp.projection_snapshot(c, "entity", e[0])
    for r in c.execute("SELECT id FROM relations"):
        union |= fp.projection_snapshot(c, "edge", r[0])
    ck("F1 全量 TTL == 逐对象快照并集（单一公式）", all_base == union,
       "full=%d union=%d missing=%s extra=%s" % (len(all_base), len(union),
       sorted(all_base - union)[:2], sorted(union - all_base)[:2]))

    # F2 改名：只删旧 label、只插新 label，type 三元组不动
    before = fp.projection_snapshot(c, "entity", "V_E2")
    c.execute("UPDATE entities SET name='乙-new' WHERE id='V_E2' AND branch='dev'")
    c.commit()
    after = fp.projection_snapshot(c, "entity", "V_E2")
    d, i = fp.delta(before, after)
    ck("F2 改名只产生 label 一删一插", len(d) == 1 and len(i) == 1
       and '"乙"' in d[0] and '"乙-new"' in i[0], "delete=%s insert=%s" % (d, i))
    fake = FakeCli()
    r = fp.apply_delta(fake, before, after)
    ck("F2b 差量真的发出 DELETE+INSERT", r["ok"] and len(_calls_of(fake, "DELETE")) == 1
       and len(_calls_of(fake, "INSERT")) == 1, "calls=%d" % len(fake.calls))

    # F3 同 subject 两条边：改一条不碰另一条
    _fixture(c)
    e_before = fp.projection_snapshot(c, "edge", 101)
    keep = fp.projection_snapshot(c, "edge", 102)
    c.execute("UPDATE relations SET relation_type='R_NEW' WHERE id=101")
    c.commit()
    e_after = fp.projection_snapshot(c, "edge", 101)
    d, i = fp.delta(e_before, e_after)
    ck("F3 同 subject 边精确差量不误删另一条", len(d) == 1 and len(i) == 1
       and bool(keep) and not (set(d) & keep) and "R_OLD" in d[0] and "R_NEW" in i[0],
       "delete=%s keep=%s" % (d, keep))

    # F4 跨分支：删 dev 行后不得删除 rel 分支仍需要的 label
    _fixture(c)
    x_before = fp.projection_snapshot(c, "entity", "V_E1")
    ck("F4a 快照含跨分支两条 label", sum('rdf-schema#label>' in ln and '"甲' in ln for ln in x_before) == 2,
       sorted(x_before))
    c.execute("DELETE FROM relations")  # 先删边，避免外键阻断实体删除
    c.execute("DELETE FROM entities WHERE id='V_E1' AND branch='dev'")
    c.commit()
    x_after = fp.projection_snapshot(c, "entity", "V_E1")
    d, _ = fp.delta(x_before, x_after)
    ck("F4 删本分支不删其它分支 label", bool(d) and all('"甲-发布"' not in ln for ln in d),
       "delete=%s after=%s" % (d, sorted(x_after)))

    # F5 幂等：空差集零网络
    fake = FakeCli()
    r = fp.apply_delta(fake, set(), set())
    ck("F5 无差集 ⇒ 零网络调用", r["ok"] and r["calls"] == 0 and not fake.calls, r)

    # F6 异常归 failed、不抛出（不能阻断已提交写入）
    _fixture(c)
    b = fp.projection_snapshot(c, "entity", "V_E2")
    c.execute("UPDATE entities SET name='断点' WHERE id='V_E2' AND branch='dev'")
    c.commit()
    r = fp.apply_delta(FakeCli(fail=True), b, fp.projection_snapshot(c, "entity", "V_E2"))
    ck("F6 Fuseki 不可达 ⇒ ok=False 不抛", r["ok"] is False and "不可达" in r.get("error", ""), r)

    # F7/F8 开关：关则不投影、开则投影
    orig_enabled = fp.enabled
    try:
        fp.enabled = lambda: False
        fp.mark(c, "entity", "V_E2", set())
        f7 = FakeCli()
        r = fp.flush(c, cli=f7)
        ck("F7 开关关闭 ⇒ disabled 且零网络", r["status"] == "disabled" and not f7.calls, r)

        fp.enabled = lambda: True
        _fixture(c)
        c.execute("UPDATE entities SET name='开关探针' WHERE id='V_E2' AND branch='dev'")
        c.commit()
        fp.mark(c, "entity", "V_E2", set())
        f8 = FakeCli()
        r = fp.flush(c, cli=f8)
        ck("F8 开关开启 ⇒ 差量投影", r["status"] == "ok" and bool(_calls_of(f8, "INSERT")), r)
        ck("F13 flush 后 pending 字典清理（无连接泄漏）", c not in fp._PENDING)
        fp.mark(c, "entity", "V_E2", set())
        fp.discard(c)
        ck("F13b discard 后 pending 清理", c not in fp._PENDING)
    finally:
        fp.enabled = orig_enabled

    # F9 真跑统一写入口：write_entity 登记 → flush 投影
    _fixture(c)
    orig_enabled = fp.enabled
    try:
        fp.enabled = lambda: True
        res = write_entity(c, op="create", entity_id="V_NEW", name="投影探针",
                           entity_type="需求", branch="dev", validate=False)
        ck("F9a 写入口成功且登记 pending", bool(res.get("ok"))
           and c in fp._PENDING and ("entity", "V_NEW") in fp._PENDING[c], res)
        f9 = FakeCli()
        r = fp.flush(c, cli=f9)
        ck("F9b commit 后 flush 真的发出 INSERT", r["status"] == "ok"
           and any("投影探针" in q for q in _calls_of(f9, "INSERT")),
           "status=%s calls=%s" % (r["status"], f9.calls[:2]))
    finally:
        fp.enabled = orig_enabled
        fp.discard(c)

    # F10 事务边界静态锁：flush 在 commit 之后，写入口内不得直接 flush
    deps_src = open(os.path.join(_ROOT, "core", "deps.py"), encoding="utf-8").read()
    body = deps_src[deps_src.index("def db_session"):]
    i_commit = body.index("conn.commit()")
    i_flush = body.index("_projection_flush(conn)")
    ck("F10a db_session 在 commit 之后才 flush", i_flush > i_commit and "\n    else:" in body[i_commit:i_flush])
    gate_src = open(os.path.join(_ROOT, "core", "graph_write_gate.py"), encoding="utf-8").read()
    ck("F10b 写入口只 mark，不直接 flush", "_projection_flush" not in gate_src
       and "_projection_mark(" in gate_src and "_projection_capture(" in gate_src)

    # F11 配置四处登记 + F12 幂等文本（开关默认关、环境变量入口存在）
    cfg_src = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    ck("F11 配置四处登记（DEFAULT/ENV/BOOL/SCHEMA）",
       cfg_src.count("fuseki_writesync") >= 4 and "MBSE_FUSEKI_WRITESYNC" in cfg_src,
       "count=%d" % cfg_src.count("fuseki_writesync"))


def _mutations(c):
    """真变异：替换被测模块的函数本体，判据必须随之判红。"""
    _fixture(c)
    orig_delta, orig_apply = fp.delta, fp.apply_delta
    orig_enabled, orig_snap = fp.enabled, fp.projection_snapshot
    try:
        # M1 抹差量（永远无变化）⇒ F2 判红
        fp.delta = lambda a, b: (set(), set())
        c.execute("UPDATE entities SET name='M1' WHERE id='V_E2' AND branch='dev'")
        c.commit()
        b = orig_snap(c, "entity", "V_E2")
        fake = FakeCli()
        r = orig_apply(fake, b, orig_snap(c, "entity", "V_E2"))
        _CAUGHT["m1"] = r["calls"] == 0
        print("  [%s] M1 抹差量 ⇒ F2 判红" % ("PASS" if _CAUGHT["m1"] else "FAIL"))

        # M2 只 INSERT 不 DELETE ⇒ F2b 判红
        _fixture(c)
        c.execute("UPDATE entities SET name='M2' WHERE id='V_E2' AND branch='dev'")
        c.commit()
        b = orig_snap(c, "entity", "V_E2")
        fake = FakeCli()
        def _insert_only(cli, before, after, timeout=30.0):
            cli.update("PREFIX mbse: <http://mbse/ontology/> INSERT DATA { %s }"
                       % "\n".join(sorted(set(after or set()) - set(before or set()))))
            return {"ok": True, "calls": 1}
        fp.apply_delta = _insert_only
        fp.apply_delta(fake, b, orig_snap(c, "entity", "V_E2"))
        _CAUGHT["m2"] = len(_calls_of(fake, "DELETE")) == 0 and bool(_calls_of(fake, "INSERT"))
        print("  [%s] M2 抹 DELETE ⇒ F2b 判红" % ("PASS" if _CAUGHT["m2"] else "FAIL"))

        # M3 开关恒开 ⇒ F7 判红
        _fixture(c)
        fp.enabled = lambda: True
        f3 = FakeCli()
        fp.mark(c, "entity", "V_E2", set())
        r3 = fp.flush(c, cli=f3)
        _CAUGHT["m3"] = r3["status"] != "disabled"
        print("  [%s] M3 开关失效 ⇒ F7 判红" % ("PASS" if _CAUGHT["m3"] else "FAIL"))

        # M4 只取单分支快照（模拟按 branch 误取）⇒ F4 判红
        _fixture(c)

        def _single(conn, kind, obj_id):
            s = orig_snap(conn, kind, obj_id)
            return set(list(sorted(s))[:1])

        fp.projection_snapshot = _single
        xb = _single(c, "entity", "V_E1")
        c.execute("DELETE FROM relations")  # 先删边，避免外键阻断实体删除
        c.execute("DELETE FROM entities WHERE id='V_E1' AND branch='dev'")
        c.commit()
        xa = _single(c, "entity", "V_E1")
        d, _ = orig_delta(xb, xa)
        _CAUGHT["m4"] = not (bool(d) and all('"甲-发布"' not in ln for ln in d))
        print("  [%s] M4 单分支快照 ⇒ F4 判红" % ("PASS" if _CAUGHT["m4"] else "FAIL"))
    finally:
        fp.delta, fp.apply_delta = orig_delta, orig_apply
        fp.enabled, fp.projection_snapshot = orig_enabled, orig_snap
        fp.discard(c)


def main():
    c = get_db()
    try:
        _checks(c)
        _mutations(c)
    finally:
        c.close()
        shutil.rmtree(_TMP, ignore_errors=True)

    n_fail = sum(1 for x in _RESULTS if not x[0])
    mut_ok = all(_CAUGHT.values())
    print("=" * 70)
    if n_fail or not mut_ok:
        print("结论：未通过（常态 %d 失败 / 变异 %s）" % (n_fail, _CAUGHT))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 4 组按预期判红）" % len(_RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
