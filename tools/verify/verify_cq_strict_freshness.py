# -*- coding: utf-8 -*-
"""P0-0 第九批②门禁：SPARQL CQ 严格档（stale / unknown ⇒ skip）。

## 要挡住什么

1. **拿陈旧镜像的查询结果当通过**：与 A9 同型 —— 验的不是最新版本，
   不能算结论；
2. **新鲜度没验成却报 pass**：unknown 是"没验成"，不是"验出好的"；
3. **误伤旧调用方**：`_run_sparql_cq()` 不传 freshness 表示"未判新鲜度"，
   严格档不能把这种调用全改判 skip（那会让既有门禁/报告口径全红）；
4. **配置只登记两层**：界面/环境变量改不了开关；
5. **每条 CQ 各查一次新鲜度**：run_cqs 必须只判一次再复用。

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

_TMP = tempfile.mkdtemp(prefix="verify_cq_strict_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db                              # noqa: E402
init_db()

from services import ontology_cq as ocq                    # noqa: E402
from services import sparql_sync as ss                     # noqa: E402

_RESULTS = []
_CAUGHT = {"m1": False, "m2": False, "m3": False}
_QUERY = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"


def ck(name, cond, detail=""):
    _RESULTS.append((bool(cond), name, str(detail)[:300] if not cond else ""))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)[:300]) if not cond else ""))


class FakeCli:
    def __init__(self, base_url=None, dataset=None, ok=True):
        self.ok = ok
        self.base_url = base_url
        self.dataset = dataset
        self.queries = []

    def ping(self, timeout=None):
        return self.ok

    def query(self, q, timeout=None):
        self.queries.append(q)
        return {"results": {"bindings": [{"s": {"value": "http://mbse/ontology/E1"}}]}}


def _run(freshness, strict):
    orig = ocq._strict_freshness
    orig_cli = ss.FusekiClient
    try:
        ocq._strict_freshness = lambda: strict
        ss.FusekiClient = FakeCli
        return ocq._run_sparql_cq(_QUERY, "nonempty", freshness=freshness)
    finally:
        ocq._strict_freshness = orig
        ss.FusekiClient = orig_cli


def _checks():
    # F1 严格档 + stale ⇒ skip（陈旧不是结论）
    r = _run({"status": "stale", "expected_base": 506, "actual": 12}, True)
    ck("F1 strict + stale ⇒ skip 且原因可见",
       r["status"] == "skip" and r["freshness"] == "stale" and "不可信" in r["message"], r)

    # F2 严格档 + unknown ⇒ skip（没验成不等于验过）
    r = _run({"status": "unknown", "reason": "回读异常"}, True)
    ck("F2 strict + unknown ⇒ skip",
       r["status"] == "skip" and "unknown" in r["message"] and r["row_count"] == 0, r)

    # F3 严格档不能误伤 fresh
    r = _run({"status": "fresh"}, True)
    ck("F3 strict + fresh ⇒ 正常 pass", r["status"] == "pass", r)

    # F4 历史口径零回归：开关关闭时 stale 仍按原判据执行
    r = _run({"status": "stale"}, False)
    ck("F4 strict 关闭 ⇒ stale 仍按原口径判定", r["status"] == "pass", r)

    # F5 未传 freshness 的旧调用（含既有门禁）不受严格档影响
    orig = ocq._strict_freshness
    orig_cli = ss.FusekiClient
    try:
        ocq._strict_freshness = lambda: True
        ss.FusekiClient = FakeCli
        r = ocq._run_sparql_cq(_QUERY, "nonempty")
    finally:
        ocq._strict_freshness = orig
        ss.FusekiClient = orig_cli
    ck("F5 旧调用不传 freshness ⇒ 严格档不介入", r["status"] == "pass", r)

    # F6 配置四处登记 + run_cqs 只判一次新鲜度
    cfg = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    ck("F6a 配置四处登记", cfg.count("cq_strict_freshness") >= 4
       and "MBSE_CQ_STRICT_FRESHNESS" in cfg, "count=%d" % cfg.count("cq_strict_freshness"))
    src = inspect.getsource(ocq.run_cqs)
    ck("F6b run_cqs 只判一次新鲜度并传给每条 CQ",
       src.count("check_freshness(") == 1 and "freshness=sp_fresh" in src,
       "calls=%d" % src.count("check_freshness("))

    # F7 summarize 计数：skip 与 stale 是两个独立维度，都要可见
    out = ocq.summarize([
        {"status": "skip", "freshness": "stale"},
        {"status": "skip", "freshness": "unknown"},
        {"status": "pass", "freshness": "fresh"},
    ])
    ck("F7 summarize 保留 skipped 与 stale 两维",
       out.get("skipped") == 2 and out.get("stale") == 1 and out.get("failed") == 0, out)


def _mutations():
    """源码级真变异：替换被测函数本体，判据必须随之判红。"""
    orig_fn = ocq._run_sparql_cq
    src = inspect.getsource(orig_fn)
    try:
        # M1 抹掉严格档判断 ⇒ F1 判红
        m1src = src.replace('if freshness is not None and _strict_freshness() and fs in ("stale", "unknown"):',
                            'if False:')
        exec(m1src, ocq.__dict__)  # 直接用模块字典：函数内 _strict_freshness 动态可 patch
        r = _run({"status": "stale"}, True)
        _CAUGHT["m1"] = r["status"] != "skip"
        print("  [%s] M1 抹严格档 ⇒ F1 判红" % ("PASS" if _CAUGHT["m1"] else "FAIL"))

        # M2 严格档误伤 fresh ⇒ F3 判红
        m2src = src.replace('fs in ("stale", "unknown")', 'fs in ("stale", "unknown", "fresh")')
        exec(m2src, ocq.__dict__)
        r = _run({"status": "fresh"}, True)
        _CAUGHT["m2"] = r["status"] != "pass"
        print("  [%s] M2 误伤 fresh ⇒ F3 判红" % ("PASS" if _CAUGHT["m2"] else "FAIL"))

        # M3 去掉 freshness is not None 哨兵 ⇒ F5 判红（旧调用被误伤）
        m3src = src.replace('if freshness is not None and _strict_freshness()',
                            'if _strict_freshness()')
        exec(m3src, ocq.__dict__)
        orig = ocq._strict_freshness
        orig_cli = ss.FusekiClient
        try:
            ocq._strict_freshness = lambda: True
            ss.FusekiClient = FakeCli
            r = ocq._run_sparql_cq(_QUERY, "nonempty")
        finally:
            ocq._strict_freshness = orig
            ss.FusekiClient = orig_cli
        _CAUGHT["m3"] = r["status"] == "skip"
        print("  [%s] M3 抹哨兵 ⇒ F5 判红" % ("PASS" if _CAUGHT["m3"] else "FAIL"))
    finally:
        ocq._run_sparql_cq = orig_fn


def main():
    try:
        _checks()
        _mutations()
    finally:
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
