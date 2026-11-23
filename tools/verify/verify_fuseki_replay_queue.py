# -*- coding: utf-8 -*-
"""P0-0 第十批门禁（2026-11-23）：写后投影失败重放队列。

## 要挡住什么

1. **只告警不补账**：Fuseki 短暂不可达 ⇒ 业务写已提交但镜像永久落后到下次重启；
2. **回放旧差量**：t1 失败(L1→L2)、t2 成功(L2→L3)后回放 t1 的 (before,after)
   会把 L2 重新插回 ⇒ 同一实体两个 label。队列必须存 before、现场重算 after；
3. **重放影响业务写**：队列写失败/重放异常不得把已提交的请求改成失败；
4. **队列不可见**：降级只打日志 ⇒ 运维无从判断"欠了多少账"；
5. **无限重试风暴**：失败必须 attempts+1 且按少试优先排序；
6. **开关失效**：默认关却产生网络/入队；
7. **DDL 漂移**：新库 schema 与老库 migration 两份 DDL 不一致。

判据 F1~F15 + 真变异 M1~M4。临时库隔离，零生产库写、零 LLM、零真实网络。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_fuseki_replay_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db                      # noqa: E402
init_db()

from core import fuseki_projection as fp                   # noqa: E402
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
    c.execute("DELETE FROM fuseki_projection_replay")
    c.execute("INSERT INTO ontology_types (id,name,type_kind,parent_id,properties) "
              "VALUES (1,'需求','entity',NULL,'{}')")
    for eid, name in (("V_E2", "乙"), ("V_E3", "丙")):
        c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties,is_current) "
                  "VALUES (?,?,'dev','reviewed','需求','{}',1)", (eid, name))
    c.commit()
    fp.discard(c)


def _calls_of(fake, verb):
    return [q for q in fake.calls if (" %s DATA {" % verb) in q]


def _patch(c, replay=True, write=True):
    """返回 (orig_tuple, restore)。直接改模块全局，覆盖 flush 内部引用。"""
    orig = (fp.replay_enabled, fp.enabled)
    fp.replay_enabled = lambda: replay
    fp.enabled = lambda: write

    def restore():
        fp.replay_enabled, fp.enabled = orig
        fp.discard(c)
    return restore


def _fail_flush(c, eid, new_name):
    """真实走一次写入→flush，Fuseki 注入失败。返回写前快照。"""
    before = fp.projection_snapshot(c, "entity", eid)
    c.execute("UPDATE entities SET name=? WHERE id=? AND branch='dev'", (new_name, eid))
    c.commit()
    fp.mark(c, "entity", eid, before)
    cli = FakeCli(fail=True)
    r = fp.flush(c, cli=cli)
    return before, r, cli


def _checks(c):
    # ── F1/F2 失败入队 + 业务写不回滚 ──
    _fixture(c)
    restore = _patch(c)
    try:
        before, r, _ = _fail_flush(c, "V_E2", "乙-L2")
        rows = c.execute("SELECT * FROM fuseki_projection_replay").fetchall()
        stored = fp._json_lines(rows[0]["before_json"]) if rows else set()
        ck("F1 投影失败入队且存 before 快照",
           r["status"] == "failed" and r.get("enqueued") == 1 and len(rows) == 1
           and stored == before and any('"乙"' in ln for ln in stored),
           "status=%s enqueued=%s rows=%d stored=%s" % (
               r["status"], r.get("enqueued"), len(rows), sorted(stored)))
        cur_name = c.execute("SELECT name FROM entities WHERE id='V_E2' AND branch='dev'").fetchone()[0]
        ck("F2 业务写仍已提交（队列不改变业务事务）", cur_name == "乙-L2", cur_name)
    finally:
        restore()

    # ── F3 开关默认关：零入队、零网络 ──
    _fixture(c)
    restore = _patch(c, replay=False, write=True)
    try:
        _, r, cli = _fail_flush(c, "V_E2", "乙-off")
        n = c.execute("SELECT COUNT(*) FROM fuseki_projection_replay").fetchone()[0]
        ck("F3 重放开关关闭 ⇒ 零入队", r["status"] == "failed" and n == 0
           and r.get("enqueued", 0) == 0, "rows=%d r=%s" % (n, r))
        ck("F3b 开关关闭时 replay 返回 disabled 且零网络",
           fp.replay(c, cli=cli)["status"] == "disabled" and not cli.calls)
    finally:
        restore()

    # ── F4/F5 重放补齐 + 幂等 ──
    _fixture(c)
    restore = _patch(c)
    try:
        _, r, _ = _fail_flush(c, "V_E2", "乙-L2")
        good = FakeCli()
        rr = fp.replay(c, cli=good)
        ins = "\n".join(_calls_of(good, "INSERT"))
        ck("F4 恢复后重放真的补投影并清队", rr["status"] == "ok" and rr["replayed"] == 1
           and rr["pending"] == 0 and '"乙-L2"' in ins,
           "rr=%s ins=%s" % (rr, ins[:200]))
        n_before = len(good.calls)
        rr2 = fp.replay(c, cli=good)
        ck("F5 再次重放：no_pending 且零网络（幂等）",
           rr2["status"] == "no_pending" and len(good.calls) == n_before, rr2)
    finally:
        restore()

    # ── F6 顺序无关：t1 失败后 t2 成功，回放 t1 不得复活旧 label ──
    # 关键：t2 的 flush 要临时关掉重放，否则 flush 开头就把 t1 欠账补了，
    # 测不到"回放旧 before"这一刀。关掉 ⇒ 队列留到显式 replay 时验证。
    _fixture(c)
    restore = _patch(c)
    try:
        mid, r1, _ = _fail_flush(c, "V_E2", "乙-L2")
        fp.replay_enabled = lambda: False
        good = FakeCli()
        r2before = fp.projection_snapshot(c, "entity", "V_E2")
        c.execute("UPDATE entities SET name='乙-L3' WHERE id='V_E2' AND branch='dev'")
        c.commit()
        fp.mark(c, "entity", "V_E2", r2before)
        r2 = fp.flush(c, cli=good)
        fp.replay_enabled = lambda: True
        row = c.execute("SELECT before_json FROM fuseki_projection_replay").fetchone()
        good.calls.clear()                      # 只看显式重放产生的调用
        rr = fp.replay(c, cli=good)
        ins = "\n".join(_calls_of(good, "INSERT"))
        ck("F6 回放旧 before 不复活 L2（只按当前 DB 重算）",
           r1["status"] == "failed" and r2["status"] == "ok" and rr["status"] == "ok"
           and '"乙-L2"' not in ins and '"乙-L3"' in ins,
           "r1=%s r2=%s rr=%s ins=%s" % (r1["status"], r2["status"], rr["status"], ins[:240]))
        # 重放成功后行会被删除 ⇒ 必须在 replay 之前取证；before_json 是 JSON 串，须先解码
        orig_lines = fp._json_lines(row[0]) if row else set()
        ck("F6b before_json 仍是 t1 的原始快照（未被 t2 覆盖）",
           sum("rdf-schema#label>" in ln for ln in orig_lines) == 1
           and any('"乙"' in ln for ln in orig_lines)
           and not any('"乙-L2"' in ln for ln in orig_lines),
           "lines=%s" % sorted(orig_lines))
    finally:
        restore()

    # ── F7 同一对象多次失败合并为一行、before 取并集 ──
    _fixture(c)
    restore = _patch(c)
    try:
        _fail_flush(c, "V_E2", "乙-A")
        b2 = fp.projection_snapshot(c, "entity", "V_E2")
        c.execute("UPDATE entities SET name='乙-B' WHERE id='V_E2' AND branch='dev'")
        c.commit()
        fp.mark(c, "entity", "V_E2", b2)
        r = fp.flush(c, cli=FakeCli(fail=True))
        rows = c.execute("SELECT * FROM fuseki_projection_replay").fetchall()
        merged = fp._json_lines(rows[0]["before_json"]) if rows else set()
        labels = [ln for ln in merged if "rdf-schema#label>" in ln]
        ck("F7 同对象合并为 1 行且 before 并集保留最早快照",
           r.get("enqueued") == 1 and len(rows) == 1 and len(labels) == 2
           and any('"乙"' in ln for ln in labels) and any('"乙-A"' in ln for ln in labels),
           "rows=%d labels=%s" % (len(rows), sorted(labels)))
    finally:
        restore()

    # ── F8 重放失败：attempts+1、行保留、last_error 可见 ──
    _fixture(c)
    restore = _patch(c)
    try:
        _fail_flush(c, "V_E2", "乙-L2")
        rr = fp.replay(c, cli=FakeCli(fail=True))
        row = c.execute("SELECT attempts,last_error FROM fuseki_projection_replay").fetchone()
        ck("F8 重放失败不清队且 attempts+1",
           rr["status"] == "failed" and rr["failed"] == 1 and rr["pending"] == 1
           and row["attempts"] == 1 and "不可达" in (row["last_error"] or ""),
           "rr=%s row=%s" % (rr, tuple(row) if row else None))
    finally:
        restore()

    # ── F9 少试优先 ──
    _fixture(c)
    restore = _patch(c)
    try:
        _fail_flush(c, "V_E2", "乙-2")
        _fail_flush(c, "V_E3", "丙-3")
        c.execute("UPDATE fuseki_projection_replay SET attempts=3 WHERE obj_id='V_E2'")
        c.commit()
        good = FakeCli()
        rr = fp.replay(c, cli=good, limit=1)
        first = (rr.get("results") or [{}])[0].get("obj_id")
        ck("F9 按 attempts 少优先重放（防饥饿）", first == "V_E3", rr)
    finally:
        restore()

    # ── F10 队列写失败不改变业务结论 ──
    _fixture(c)
    restore = _patch(c)
    orig_eq = fp.enqueue
    try:
        def _boom(*a, **kw):
            raise RuntimeError("队列表写入失败（注入）")
        fp.enqueue = _boom
        _, r, _ = _fail_flush(c, "V_E2", "乙-eq")
        ck("F10 入队异常不外抛、业务写结论不变", r["status"] == "failed"
           and r.get("enqueued") == 0, r)
    finally:
        fp.enqueue = orig_eq
        restore()

    # ── F11 可见性端点数据 ──
    _fixture(c)
    restore = _patch(c)
    try:
        _fail_flush(c, "V_E2", "乙-vis")
        q = fp.queue_status(c)
        ck("F11 队列可见性：开关/条数/明细齐全",
           q["enabled"] is True and q["pending"] == 1 and len(q["entries"]) == 1
           and q["entries"][0]["obj_id"] == "V_E2"
           and q["entries"][0]["before_lines"] >= 1, q)
    finally:
        restore()

    # ── F12 静态锁：启动重放接线（在 init_db 之后、开关门内）──
    main_src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
    body = main_src[main_src.index("async def lifespan"):]
    i_init = body.index("init_db()")
    i_replay = body.index("_replay_proj(_conn_rp)")
    ck("F12 启动时重放在 init_db 之后且受开关控制",
       i_replay > i_init and 'fuseki_replay_queue' in body[i_init:i_replay],
       "i_init=%d i_replay=%d" % (i_init, i_replay))

    # ── F13 配置四处登记 ──
    cfg_src = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    ck("F13 配置四处登记（DEFAULT/ENV/BOOL/SCHEMA）",
       cfg_src.count("fuseki_replay_queue") >= 4
       and "MBSE_FUSEKI_REPLAY_QUEUE" in cfg_src,
       "count=%d" % cfg_src.count("fuseki_replay_queue"))

    # ── F14 新库/老库两份 DDL 一致且迁移已注册调用 ──
    schema_src = open(os.path.join(_ROOT, "database", "schema.py"), encoding="utf-8").read()
    mig_src = open(os.path.join(_ROOT, "database", "migrations", "ontology.py"),
                   encoding="utf-8").read()
    reg_src = open(os.path.join(_ROOT, "database", "migrations", "__init__.py"),
                   encoding="utf-8").read()
    ck("F14 DDL 双处一致 + 迁移注册/调用齐全",
       "CREATE TABLE IF NOT EXISTS fuseki_projection_replay" in schema_src
       and "CREATE TABLE IF NOT EXISTS fuseki_projection_replay" in mig_src
       and "_migrate_fuseki_replay" in reg_src
       and "_migrate_fuseki_replay(conn)" in schema_src
       and "UNIQUE(kind, obj_id)" in schema_src and "UNIQUE(kind, obj_id)" in mig_src)

    # ── F15 路由可见性 + 权限域 ──
    rtr_src = open(os.path.join(_ROOT, "routers", "sparql.py"), encoding="utf-8").read()
    ck("F15 GET/POST /replay 路由存在且复用 kb_ontology 权限",
       '@router.get("/replay")' in rtr_src and '@router.post("/replay")' in rtr_src
       and 'require_permission("kb_ontology", "view")' in rtr_src
       and 'require_permission("kb_ontology", "edit")' in rtr_src)


def _mutations(c):
    """真变异：替换被测模块函数本体，判据必须随之判红。"""
    orig_eq, orig_enabled = fp.enqueue, fp.replay_enabled
    orig_apply, orig_snap = fp.apply_delta, fp.projection_snapshot
    try:
        # M1 抹入队 ⇒ F1 判红
        _fixture(c)
        restore = _patch(c)
        try:
            fp.enqueue = lambda *a, **kw: {"ok": True}
            _, r, _ = _fail_flush(c, "V_E2", "乙-M1")
            n = c.execute("SELECT COUNT(*) FROM fuseki_projection_replay").fetchone()[0]
            _CAUGHT["m1"] = r.get("enqueued") == 1 and n == 0
            print("  [%s] M1 抹入队 ⇒ F1 判红" % ("PASS" if _CAUGHT["m1"] else "FAIL"))
        finally:
            fp.enqueue = orig_eq
            restore()

        # M2 抹重放投影（假成功、零网络）⇒ F4 判红
        _fixture(c)
        restore = _patch(c)
        try:
            _, _, _ = _fail_flush(c, "V_E2", "乙-M2")
            good = FakeCli()
            fp.apply_delta = lambda cli, a, b, timeout=30.0: {"ok": True, "calls": 0}
            rr = fp.replay(c, cli=good)
            _CAUGHT["m2"] = rr["status"] == "ok" and not good.calls
            print("  [%s] M2 抹重放投影 ⇒ F4 判红  rr=%s calls=%d"
                  % ("PASS" if _CAUGHT["m2"] else "FAIL", rr.get("status"), len(good.calls)))
        finally:
            fp.apply_delta = orig_apply
            restore()

        # M3 开关恒关 ⇒ F1/F4 判红
        _fixture(c)
        restore = _patch(c)
        try:
            fp.replay_enabled = lambda: False
            _, r, _ = _fail_flush(c, "V_E2", "乙-M3")
            rr = fp.replay(c, cli=FakeCli())
            _CAUGHT["m3"] = r.get("enqueued", 0) == 0 and rr["status"] == "disabled"
            print("  [%s] M3 开关失效 ⇒ F1/F4 判红" % ("PASS" if _CAUGHT["m3"] else "FAIL"))
        finally:
            restore()

        # M4 重放按 before 当 after（旧差量回放）⇒ F6 判红
        _fixture(c)
        restore = _patch(c)
        try:
            _, _, _ = _fail_flush(c, "V_E2", "乙-M4a")
            _stale_after = fp.projection_snapshot(c, "entity", "V_E2")
            c.execute("UPDATE entities SET name='乙-M4b' WHERE id='V_E2' AND branch='dev'")
            c.commit()
            good = FakeCli()
            fp.projection_snapshot = lambda conn, kind, oid: set(_stale_after)
            rr = fp.replay(c, cli=good)
            ins = "\n".join(_calls_of(good, "INSERT"))
            _CAUGHT["m4"] = rr["status"] == "ok" and '"乙-M4a"' in ins
            print("  [%s] M4 回放旧 after ⇒ F6 判红  rr=%s ins=%s"
                  % ("PASS" if _CAUGHT["m4"] else "FAIL", rr.get("status"), ins[:120]))
        finally:
            fp.projection_snapshot = orig_snap
            restore()
    finally:
        fp.enqueue, fp.replay_enabled = orig_eq, orig_enabled
        fp.apply_delta, fp.projection_snapshot = orig_apply, orig_snap
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
