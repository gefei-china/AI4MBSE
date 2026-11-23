# -*- coding: utf-8 -*-
"""P0-0 第九批③门禁：新鲜度精确档（base 三元组集合逐条包含）。

## 要挡住什么

第八批判据是"数量下界 + 抽样 label 命中"：如果把一条 base 三元组换成
**另一条等量垃圾三元组**，数量不变、抽样没抽到那条实体 ⇒ 假 fresh。
真机取证：Fuseki 506 条 base 与本地 TTL **逐条精确匹配**（额外 1217 条全是
W3C Time 时态三元组），所以可以按集合精确比对，用包含关系取代数量下界。

判据 F1~F7 + 源码真变异 M1~M3。临时库 + 注入客户端，零生产库写、零真实网络。
"""
from __future__ import annotations

import inspect
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_exact_fresh_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db                      # noqa: E402
init_db()

from core import fuseki_freshness as ff                    # noqa: E402
from services import sparql_sync as ss                     # noqa: E402

_RESULTS = []
_CAUGHT = {"m1": False, "m2": False, "m3": False}


def ck(name, cond, detail=""):
    _RESULTS.append((bool(cond), name, str(detail)[:300] if not cond else ""))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)[:300]) if not cond else ""))


def _uri(v):
    return {"type": "uri", "value": v}


def _lit(v, dt=None):
    b = {"type": "literal", "value": v}
    if dt:
        b["datatype"] = dt
    return b


def _bnode(v):
    return {"type": "bnode", "value": v}


def _bindings(lines, extra=()):
    """把 N-Triples 行还原成 SPARQL JSON bindings（只覆盖本仓生成的行形态）。"""
    out = []
    import re as _re
    for ln in list(lines) + list(extra):
        # 必须以「末尾 .」为界整段取 object —— 字面量（如 rdfs:comment 的 JSON）
        # 内部含空格，split(' ',2) 会把它截断成假缺失。
        m = _re.match(r"^(\S+)\s+(\S+)\s+(.+?)\s*\.\s*$", ln.strip())
        if not m:
            continue
        parts = [m.group(1), m.group(2), m.group(3)]
        cells = []
        for p in parts:
            if p.startswith("<") and p.endswith(">"):
                cells.append(_uri(p[1:-1]))
            elif p.startswith("_:"):
                cells.append(_bnode(p[2:]))
            elif p.startswith('"'):
                body = p[1:]
                dt = None
                if "^^<" in body:
                    body, rest = body.split("^^<", 1)
                    dt = rest.rstrip(">")
                if body.endswith('"'):
                    body = body[:-1]
                cells.append(_lit(body.replace('\\"', '"'), dt))
            else:
                cells.append(_lit(p))
        out.append({"s": cells[0], "p": cells[1], "o": cells[2]})
    return out


class FakeCli:
    def __init__(self, lines=(), extra=(), broken=False):
        self.lines = set(lines)
        self.extra = list(extra)
        self.broken = broken

    def query(self, q, timeout=None):
        if self.broken:
            raise RuntimeError("精确新鲜度回读故障注入")
        return {"results": {"bindings": _bindings(self.lines, self.extra)}}


def _fixture(c):
    c.execute("DELETE FROM relations")
    c.execute("DELETE FROM entities")
    c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties,is_current) "
              "VALUES ('EX_E1','甲','dev','reviewed','需求','{}',1), "
                     "('EX_E2','乙','dev','reviewed','需求','{}',1)")
    c.execute("INSERT INTO relations (id,source_id,target_id,relation_type,branch,properties,status) "
              "VALUES (201,'EX_E1','EX_E2','R','dev','{}','reviewed')")
    c.commit()


def _checks(c):
    _fixture(c)
    expected = ff.expected_base_triples(c)
    ck("F0 期望集合非空且来自同一 builder", len(expected) >= 5, len(expected))

    # F1 齐全 + 额外时态/手工三元组 ⇒ fresh（额外不算陈旧）
    temporal = ("_:b1 <http://www.w3.org/2006/time#inXSDDateTime> "
                '"2026-01-01T00:00:00"^^<http://www.w3.org/2001/XMLSchema#dateTime> .')
    r = ff.check_exact_freshness(c, cli=FakeCli(expected, extra=[temporal]))
    ck("F1 base 齐全且容忍额外三元组 ⇒ fresh",
       r["status"] == "fresh" and r["expected_base"] == len(expected)
       and r["actual"] == len(expected) + 1, r)

    # F2 等量不同内容：删一条 base、补一条垃圾，总数不变 —— 下界判据会漏，精确判据必抓
    victim = sorted(expected)[0]
    junk = "<http://mbse/ontology/__junk> <http://mbse/ontology/junk> \"x\" ."
    actual = (expected - {victim}) | {junk}
    r = ff.check_exact_freshness(c, cli=FakeCli(actual))
    ck("F2 等量内容错位 ⇒ stale（数量下界漏判的形态）",
       r["status"] == "stale" and victim in r["missing"]
       and r["actual"] == r["expected_base"], r)

    # F3 真缺一条 ⇒ stale
    r = ff.check_exact_freshness(c, cli=FakeCli(expected - {victim}))
    ck("F3 直接缺 base 三元组 ⇒ stale", r["status"] == "stale" and victim in r["missing"], r)

    # F4 回读异常 ⇒ unknown（不能触发自动重灌）
    r = ff.check_exact_freshness(c, cli=FakeCli(broken=True))
    ck("F4 回读异常 ⇒ unknown 不是 stale", r["status"] == "unknown" and not r["missing"], r)

    # F5 期望集合复用同步器 builder（builder 变 ⇒ 期望随之变）
    orig_build = ss.build_entities_ttl
    extra_line = "<http://mbse/ontology/__changed> <http://www.w3.org/2000/01/rdf-schema#label> \"新\" ."
    try:
        ss.build_entities_ttl = lambda conn, pid=None: orig_build(conn, pid) + "\n" + extra_line
        changed = ff.expected_base_triples(c)
    finally:
        ss.build_entities_ttl = orig_build
    ck("F5 期望值随 builder 变化（无第二套公式）",
       extra_line in changed and ff.expected_base_triples(c) == expected,
       "changed=%d base=%d" % (len(changed), len(expected)))

    # F6 配置四处登记 + check_freshness 接线
    cfg = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    ck("F6a 配置四处登记", cfg.count("fuseki_exact_freshness") >= 4
       and "MBSE_FUSEKI_EXACT_FRESHNESS" in cfg, "count=%d" % cfg.count("fuseki_exact_freshness"))
    src = inspect.getsource(ff.check_freshness)
    ck("F6b check_freshness 精确档接线", "if _exact_enabled():" in src
       and "return check_exact_freshness(conn, cli, project_id)" in src)

    # F7 兼容字段（端点/CQ 消费方零改动）
    r = ff.check_exact_freshness(c, cli=FakeCli(expected))
    ck("F7 四态字段兼容（status/expected_base/actual/missing/reason）",
       all(k in r for k in ("status", "expected_base", "actual", "missing", "reason")), r)


def _mutations(c):
    expected = ff.expected_base_triples(c)
    victim = sorted(expected)[0]
    junk = "<http://mbse/ontology/__junk> <http://mbse/ontology/junk> \"x\" ."
    actual = (expected - {victim}) | {junk}
    orig_missing, orig_fn = ff.exact_missing, ff.check_exact_freshness
    try:
        # M1 抹精确差判据 ⇒ F2 判红
        ff.exact_missing = lambda e, a: []
        r = orig_fn(c, cli=FakeCli(actual))
        _CAUGHT["m1"] = r["status"] != "stale"
        print("  [%s] M1 抹精确差 ⇒ F2 判红" % ("PASS" if _CAUGHT["m1"] else "FAIL"))

        # M2 把"额外三元组"当缺失 ⇒ F1 判红（时态三元组被误判陈旧）
        ff.exact_missing = lambda e, a: sorted(set(a or set()) - set(e or set()))
        r = orig_fn(c, cli=FakeCli(expected, extra=[junk]))
        _CAUGHT["m2"] = r["status"] != "fresh"
        print("  [%s] M2 反向差集 ⇒ F1 判红" % ("PASS" if _CAUGHT["m2"] else "FAIL"))

        # M3 异常误归 stale（会触发盲目重灌）⇒ F4 判红
        src = inspect.getsource(orig_fn)
        m3src = src.replace('out = {"status": "unknown"',
                            'out = {"status": "stale"', 1)
        exec(m3src, ff.__dict__)
        r = ff.check_exact_freshness(c, cli=FakeCli(broken=True))
        _CAUGHT["m3"] = r["status"] != "unknown"
        print("  [%s] M3 异常归 stale ⇒ F4 判红" % ("PASS" if _CAUGHT["m3"] else "FAIL"))
    finally:
        ff.exact_missing = orig_missing
        ff.check_exact_freshness = orig_fn


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
