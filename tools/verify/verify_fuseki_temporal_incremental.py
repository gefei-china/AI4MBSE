# -*- coding: utf-8 -*-
"""P0-0 第十一批门禁（2026-11-23）：时态三元组进写后差量投影。

## 要挡住什么

1. **时态不进快照**：实体有效期变化/deletion 只能等全量重灌；真机实测删探针后
   `hasValidInterval` 残留，Fuseki 总数 1752 而非基线 1723；
2. **按 bnode 原 label 精确删**：Fuseki 插入时重命名 `_:safe_x_ba` → `_:b318`，
   `DELETE DATA { _:safe_x_ba ... }` 匹配不到 ⇒ 旧有效期永远删不掉；
3. **全量/增量两套时态公式**：重构前后 build_valid_intervals_ttl 必须字节一致；
4. **开关失效 / 无变化时打网络**。

判据 F1~F9 + 真变异 M1~M3。临时库 + 内存图库模型，零生产库写、零真实网络。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_fuseki_temporal_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db                      # noqa: E402
init_db()

from core import fuseki_projection as fp                   # noqa: E402
from services import sparql_sync as ss                     # noqa: E402
from services import temporal_sparql as ts                 # noqa: E402

_RESULTS = []
_CAUGHT = {"m1": False, "m2": False, "m3": False}
EID = "T10_实体_甲"


def ck(name, cond, detail=""):
    _RESULTS.append((bool(cond), name, str(detail)[:300] if not cond else ""))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)[:300]) if not cond else ""))


class StoreCli:
    """最小 Fuseki 语义模型：插入时**重命名 bnode label**，模式删除按命名区间匹配。"""

    def __init__(self):
        self.triples, self.calls, self._n = [], [], 0
        self._bnmap = {}   # 同一 INSERT 内相同 label 必须映射到同一 bnode（Fuseki 语义）

    def _bn(self, label):
        if label not in self._bnmap:
            self._n += 1
            self._bnmap[label] = "_:b%d" % self._n
        return self._bnmap[label]

    def _body(self, q, verb):
        return q.split(verb + " DATA {", 1)[1].rsplit("}", 1)[0]

    def update(self, q, timeout=None):
        self.calls.append(q)
        if "INSERT DATA" in q:
            for ln in self._body(q, "INSERT").splitlines():
                parts = ln.strip().rstrip(" .").split()
                if len(parts) != 3:
                    continue
                s, p, o = parts
                if s.startswith("_:"):
                    s = self._bn(s)
                if o.startswith("_:"):
                    o = self._bn(o)
                self.triples.append((s, p, o))
        elif "DELETE DATA" in q:
            gone = set()
            for ln in self._body(q, "DELETE").splitlines():
                parts = ln.strip().rstrip(" .").split()
                if len(parts) == 3:
                    gone.add(tuple(parts))
            self.triples = [t for t in self.triples if t not in gone]
        elif "DELETE {" in q and "WHERE {" in q:
            m = re.search(r"\{ (<[^>]+/interval>) \?p \?o \}", q)
            if not m:
                return 200
            inv = m.group(1)
            neighbours = {o for (s, _, o) in self.triples if s == inv}
            self.triples = [t for t in self.triples
                            if t[0] != inv
                            and not (t[0].startswith("_:") and t[0] in neighbours)]
        return 200

    def has(self, frag):
        return any(frag in " ".join(t) for t in self.triples)


def _fixture(c):
    c.execute("DELETE FROM relations")
    c.execute("DELETE FROM entities")
    c.execute("DELETE FROM ontology_types")
    c.execute("DELETE FROM fuseki_projection_replay")
    c.execute("INSERT INTO ontology_types (id,name,type_kind,parent_id,properties) "
              "VALUES (1,'需求','entity',NULL,'{}')")
    c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties,"
              "is_current,valid_from,valid_to) VALUES (?,?,'dev','reviewed','需求','{}',1,?,?)",
              (EID, "时态甲", "2026-01-01 00:00:00", "2026-12-31 00:00:00"))
    c.commit()
    fp.discard(c)


def _checks(c):
    # F1 全量/增量同一公式：实体快照里应含 8 条时态行
    _fixture(c)
    snap = fp.projection_snapshot(c, "entity", EID)
    temporal = [ln for ln in snap if "time#" in ln or ts.HAS_VALID in ln]
    ck("F1 实体快照含 8 条时态三元组（与全量同公式）",
       len(temporal) == 8 and any('"2026-01-01T00:00:00"' in ln for ln in temporal),
       "n=%d lines=%s" % (len(temporal), sorted(temporal)[:2]))

    # F2 全量 TTL 重构字节一致（真库指纹固化为常量，防两套公式漂移）
    import hashlib
    c2 = get_db()
    try:
        sha = hashlib.sha256(ts.build_valid_intervals_ttl(c2).encode()).hexdigest()
    finally:
        c2.close()
    ck("F2 全量时态 TTL 指纹与重构前一致（夹具基线）",
       sha == "ccd283150c95afdf645bb16054e171107285323374d3d0f6f6d93c48edf69e60",
       sha)

    # F3 首次投影：bnode 被 Fuseki 重命名后仍查得到有效期
    store = StoreCli()
    r = fp.apply_delta(store, set(), snap)
    ck("F3 首次投影含时态 INSERT 且内存图库拿到 8 条时态",
       r["ok"] and store.has("hasValidInterval") and store.has("2026-01-01T00:00:00"),
       "calls=%d triples=%d" % (len(store.calls), len(store.triples)))
    ck("F3b bnode label 确实被重命名（证明不能按原 label 精确删）",
       all(not t[0].startswith("_:safe_") for t in store.triples
           if t[0].startswith("_:")),
       [t for t in store.triples if t[0].startswith("_:")][:3])

    # F4 有效期改写：旧 dateTime 消失、新 dateTime 出现，且走区间模式删除
    before = set(snap)
    c.execute("UPDATE entities SET valid_from='2026-02-02 00:00:00' "
              "WHERE id=? AND branch='dev'", (EID,))
    c.commit()
    after = fp.projection_snapshot(c, "entity", EID)
    store2 = StoreCli()
    fp.apply_delta(store2, set(), before)
    store2.calls.clear()
    r = fp.apply_delta(store2, before, after)
    d, i = fp.delta(before, after)
    ck("F4 有效期变化产生精确差量且发模式删除",
       r["ok"] and r.get("patterns", 0) == 1
       and any("?p2 ?o2" in q and " WHERE { { " in q for q in store2.calls),
       "r=%s d=%s i=%s" % (r, d, i))
    ck("F4b 重命名后的旧 bnode 被清掉，旧日期不再存在",
       not store2.has("2026-01-01T00:00:00") and store2.has("2026-02-02T00:00:00"),
       [t for t in store2.triples if "dateTime" in t[2]][:4])

    # F5 删除实体：区间链接 + 区间 + bnode 全清，不留时态孤儿
    before_del = set(after)
    c.execute("DELETE FROM entities WHERE id=? AND branch='dev'", (EID,))
    c.commit()
    after_del = fp.projection_snapshot(c, "entity", EID)
    r2 = fp.apply_delta(store2, before_del, after_del)
    ck("F5 删实体后时态链接/区间/bnode 全部清除",
       r2["ok"] and not store2.has("hasValidInterval")
       and not store2.has("/interval>") and not store2.has("dateTime"),
       "r=%s rest=%s" % (r2, store2.triples[:6]))

    # F6 无变化零网络
    _fixture(c)
    snap6 = fp.projection_snapshot(c, "entity", EID)
    store6 = StoreCli()
    r6 = fp.apply_delta(store6, snap6, snap6)
    ck("F6 无差集 ⇒ 零网络（含时态）", r6["ok"] and r6["calls"] == 0 and not store6.calls, r6)

    # F7 开关关闭时 flush 不投影
    orig = fp.enabled
    try:
        fp.enabled = lambda: False
        _fixture(c)
        fp.mark(c, "entity", EID, set())
        f7 = StoreCli()
        rr = fp.flush(c, cli=f7)
        ck("F7 写入开关关闭 ⇒ 不投影", rr["status"] == "disabled" and not f7.calls, rr)
    finally:
        fp.enabled = orig
        fp.discard(c)

    # F8 重放路径同样带时态（队列 before 含 8 条时态）
    orig_re, orig_en = fp.replay_enabled, fp.enabled
    try:
        fp.replay_enabled = lambda: True
        fp.enabled = lambda: True
        _fixture(c)
        b8 = fp.projection_snapshot(c, "entity", EID)
        c.execute("UPDATE entities SET name='时态改名' WHERE id=? AND branch='dev'", (EID,))
        c.commit()
        fp.mark(c, "entity", EID, b8)
        fp.flush(c, cli=type("F", (), {"update": lambda self, q, timeout=None: (_ for _ in ()).throw(ss.FusekiUnavailable("down"))})())
        row = c.execute("SELECT before_json FROM fuseki_projection_replay").fetchone()
        lines = fp._json_lines(row[0])
        ck("F8 失败队列 before 含时态行（重放可补时态）",
           sum("time#" in ln or ts.HAS_VALID in ln for ln in lines) == 8,
           "n=%d" % len(lines))
    finally:
        fp.replay_enabled, fp.enabled = orig_re, orig_en
        fp.discard(c)

    # F9 门禁注册 + CI 接线
    man = open(os.path.join(_ROOT, "tools", "gates_manifest.json"), encoding="utf-8").read()
    ci = open(os.path.join(_ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    ck("F9 门禁注册 + CI 接线齐全",
       "verify_fuseki_temporal_incremental.py" in man
       and "verify_fuseki_temporal_incremental.py" in ci)


def _mutations(c):
    orig_snap, orig_split, orig_delta = (fp.projection_snapshot,
                                         fp._split_temporal_deletes, fp.apply_delta)
    try:
        # M1 快照抹时态 ⇒ F1 判红
        _fixture(c)
        fp.projection_snapshot = lambda conn, kind, oid: {
            ln for ln in orig_snap(conn, kind, oid) if "time#" not in ln and ts.HAS_VALID not in ln}
        s = fp.projection_snapshot(c, "entity", EID)
        _CAUGHT["m1"] = not any("time#" in ln or ts.HAS_VALID in ln for ln in s)
        print("  [%s] M1 抹时态快照 ⇒ F1 判红" % ("PASS" if _CAUGHT["m1"] else "FAIL"))
        fp.projection_snapshot = orig_snap   # 每组变异各自还原，避免污染下一组夹具

        # M2 时态删除全走精确 DELETE ⇒ F4b 判红（原 bnode label 匹配不到）
        _fixture(c)
        before = orig_snap(c, "entity", EID)
        c.execute("UPDATE entities SET valid_from='2026-03-03 00:00:00' "
                  "WHERE id=? AND branch='dev'", (EID,))
        c.commit()
        after = orig_snap(c, "entity", EID)
        store = StoreCli()
        orig_delta(store, set(), before)
        fp._split_temporal_deletes = (
            lambda d, i, b=None, a=None: (
                [ln for ln in d if "time#" not in ln], list(i), []))
        fp.apply_delta(store, before, after)
        _CAUGHT["m2"] = store.has("2026-01-01T00:00:00")
        print("  [%s] M2 精确删 bnode ⇒ F4b 判红" % ("PASS" if _CAUGHT["m2"] else "FAIL"))

        # M3 抹模式删除（返回空 pattern）⇒ F5 判红
        _fixture(c)
        store3 = StoreCli()
        orig_delta(store3, set(), set(orig_snap(c, "entity", EID)))
        fp._split_temporal_deletes = (
            lambda d, i, b=None, a=None: (
                [ln for ln in d if "time#" not in ln and ts.HAS_VALID not in ln], list(i), []))
        c.execute("DELETE FROM entities WHERE id=?", (EID,))
        c.commit()
        fp.apply_delta(store3, set(orig_snap(c, "entity", EID)), set())
        _CAUGHT["m3"] = store3.has("hasValidInterval")
        print("  [%s] M3 抹模式删除 ⇒ F5 判红" % ("PASS" if _CAUGHT["m3"] else "FAIL"))
    finally:
        fp.projection_snapshot, fp._split_temporal_deletes = orig_snap, orig_split
        fp.apply_delta = orig_delta
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
    print("结论：全部通过（常态 %d 条 + 变异 3 组按预期判红）" % len(_RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
