# -*- coding: utf-8 -*-
"""P0-0 第九批④门禁：全量重灌保留手工三元组。

## 要挡住什么

1. **重灌吞掉用户数据**：`CLEAR DEFAULT` 会清掉 `POST /api/sparql/update`
   写入的三元组；自动重灌/启动对齐无声毁数据；
2. **把时态三元组当手工重插**：Fuseki 会重命名 bnode label
   （`_:safe_ba`→`_:b318`），按整行文本做差会把旧时态全当手工，
   重插后出现两套时态图 ⇒ 必须按谓词识别并排除；
3. **把 base 三元组当手工**：同一条被重插无害但会掩盖分类错误；
4. **开关失效**：默认关的开关被绕过，每次重灌都打额外查询；
5. **只改一个同步入口**：`sync_to_fuseki` 与 `sync_to_fuseki_with_time`
   两条重灌路径必须都保留。

判据 F1~F7 + 变异 M1~M3。临时库 + 注入客户端，零生产库写、零真实网络。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_manual_preserve_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db                      # noqa: E402
init_db()

from services import fuseki_manual as fm                   # noqa: E402
from services import sparql_sync as ss                     # noqa: E402
from services import temporal_sparql as ts                 # noqa: E402

_RESULTS = []
_CAUGHT = {"m1": False, "m2": False, "m3": False}
_MANUAL = '<http://example.org/manual/P9D> <http://example.org/p> "手工保留" .'
_TEMPORAL = ('<http://mbse/ontology/E1/interval> '
             '<http://www.w3.org/2006/time#hasBeginning> _:b318 .')


def ck(name, cond, detail=""):
    _RESULTS.append((bool(cond), name, str(detail)[:300] if not cond else ""))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)[:300]) if not cond else ""))


def _bindings(lines):
    out = []
    for ln in lines:
        m = re.match(r"^(\S+)\s+(\S+)\s+(.+?)\s*\.\s*$", ln.strip())
        if not m:
            continue
        cells = []
        for p in (m.group(1), m.group(2), m.group(3)):
            if p.startswith("<") and p.endswith(">"):
                cells.append({"type": "uri", "value": p[1:-1]})
            elif p.startswith("_:"):
                cells.append({"type": "bnode", "value": p[2:]})
            else:
                body = p[1:-1].replace('\\"', '"') if p.endswith('"') else p
                cells.append({"type": "literal", "value": body})
        out.append({"s": cells[0], "p": cells[1], "o": cells[2]})
    return out


class FakeCli:
    def __init__(self, triples=()):
        self.triples = set(triples)
        self.calls = []

    def ping(self, timeout=None):
        return True

    def dataset_exists(self, timeout=None):
        return True

    def create_dataset(self, timeout=None):
        return True

    def query(self, q, timeout=None):
        return {"results": {"bindings": _bindings(sorted(self.triples))}}

    def update(self, q, timeout=None):
        self.calls.append(q)
        return 200


def _fixture(c):
    c.execute("DELETE FROM relations")
    c.execute("DELETE FROM entities")
    c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties,is_current) "
              "VALUES ('E1','甲','dev','reviewed','需求','{}',1)")
    c.commit()
    base = ss.build_ontology_ttl(c) + "\n" + ss.build_entities_ttl(c)
    return fm.normalize_lines(base)


def _switch(on):
    orig = fm.preserve_enabled
    fm.preserve_enabled = lambda: on
    return orig


def _checks(c):
    base = _fixture(c)
    one_base = sorted(base)[0]

    # F1 手工识别：只留非 base、非时态
    cli = FakeCli(base | {_MANUAL, _TEMPORAL})
    got = fm.collect_manual(cli, "\n".join(base))
    ck("F1 只识别手工三元组", got == {_MANUAL}, "got=%s" % sorted(got)[:3])

    # F2 时态三元组（bnode label 已变）不得当手工
    ck("F2 时态三元组被排除（防双份时态图）", fm.is_temporal(_TEMPORAL)
       and _TEMPORAL not in got, fm.is_temporal(_TEMPORAL))

    # F3 base 三元组不得当手工
    ck("F3 base 三元组被排除", one_base not in got and fm.normalize_lines(one_base) == {one_base})

    # F4 真跑 with_time 重灌：CLEAR 之后手工被重新 INSERT
    orig_cli = ss.FusekiClient
    orig_on = _switch(True)
    try:
        ss.FusekiClient = lambda **kw: cli
        r = ts.sync_to_fuseki_with_time(c)
    finally:
        ss.FusekiClient = orig_cli
        fm.preserve_enabled = orig_on
    clear_at = next((i for i, q in enumerate(cli.calls) if "CLEAR DEFAULT" in q), -1)
    manual_at = next((i for i, q in enumerate(cli.calls) if "手工保留" in q), -1)
    ck("F4 带时态重灌保留手工且顺序在 CLEAR 之后",
       r.get("ok") and r.get("preserved_manual") == 1
       and clear_at >= 0 and manual_at > clear_at,
       "ok=%s preserved=%s clear=%d manual=%d" % (r.get("ok"), r.get("preserved_manual"),
                                                   clear_at, manual_at))

    # F5 开关关闭：零保留（旧行为）
    cli2 = FakeCli(base | {_MANUAL})
    orig_on = _switch(False)
    try:
        ss.FusekiClient = lambda **kw: cli2
        r2 = ss.sync_to_fuseki(c)
    finally:
        ss.FusekiClient = orig_cli
        fm.preserve_enabled = orig_on
    ck("F5 开关关闭 ⇒ preserved_manual=0 且不重插",
       r2.get("preserved_manual") == 0 and not any("手工保留" in q for q in cli2.calls), r2)

    # F6 配置四处登记
    cfg = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    ck("F6 配置四处登记", cfg.count("fuseki_preserve_manual") >= 4
       and "MBSE_FUSEKI_PRESERVE_MANUAL" in cfg,
       "count=%d" % cfg.count("fuseki_preserve_manual"))

    # F7 空集合零网络
    f7 = FakeCli()
    ck("F7 空手工集合 ⇒ reinsert 零网络",
       fm.reinsert_manual(f7, set()) == 0 and not f7.calls)

    # F8 两条重灌路径都接线（静态锁）
    s1 = open(os.path.join(_ROOT, "services", "sparql_sync.py"), encoding="utf-8").read()
    s2 = open(os.path.join(_ROOT, "services", "temporal_sparql.py"), encoding="utf-8").read()
    ck("F8 两条重灌路径都接线",
       "reinsert_manual(cli, preserved" in s1 and "reinsert_manual(cli, preserved" in s2)


def _mutations(c):
    base = _fixture(c)
    orig_temporal, orig_collect = fm.is_temporal, fm.collect_manual
    s1_path = os.path.join(_ROOT, "services", "sparql_sync.py")
    s1_orig = open(s1_path, encoding="utf-8").read()
    try:
        # M1 抹时态识别 ⇒ F2 判红
        fm.is_temporal = lambda line: False
        got = fm.collect_manual(FakeCli(base | {_MANUAL, _TEMPORAL}), "\n".join(base))
        _CAUGHT["m1"] = _TEMPORAL in got
        print("  [%s] M1 抹时态识别 ⇒ F2 判红" % ("PASS" if _CAUGHT["m1"] else "FAIL"))

        # M2 不减 base（把 base 也当手工）⇒ F3 判红
        fm.collect_manual = lambda cli, ttl, timeout=30.0: fm_patched_actual(cli)
        def fm_patched_actual(cli):
            from core.fuseki_freshness import actual_triples
            return actual_triples(cli)
        got = fm.collect_manual(FakeCli(base | {_MANUAL}), "\n".join(base))
        _CAUGHT["m2"] = bool(got & base)
        print("  [%s] M2 不减 base ⇒ F3 判红" % ("PASS" if _CAUGHT["m2"] else "FAIL"))

        # M3 抹掉一条路径的重插接线 ⇒ F8 判红
        mutated = s1_orig.replace("reinsert_manual(cli, preserved", "noop_reinsert(cli, preserved")
        _CAUGHT["m3"] = "reinsert_manual(cli, preserved" not in mutated
        print("  [%s] M3 抹重插接线 ⇒ F8 判红" % ("PASS" if _CAUGHT["m3"] else "FAIL"))
    finally:
        fm.is_temporal, fm.collect_manual = orig_temporal, orig_collect


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
