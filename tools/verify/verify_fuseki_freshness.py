# -*- coding: utf-8 -*-
"""P0-0 第八批门禁：Fuseki 派生镜像新鲜度判据 + 启动自动对齐接线 + 变异自证。

判据：
  F1 镜像齐全 ⇒ fresh
  F2 三元组数不足 ⇒ stale
  F3 数量够但抽样实体缺失 ⇒ stale（数量相同内容错位也要抓）
  F4 回读异常 ⇒ unknown（不是 stale，禁止自动重灌）
  F5 期望值按 RDF **集合**去重（真机教训：按行数 1262 会把 fresh 误判成 stale）
  F6 main.py 只在 status=='stale' 时自动重灌（unknown 不动）
  F7 /api/sparql/freshness 端点 + CQ 结果 freshness 字段 + summarize.stale
  F8 配置四处登记（DEFAULT/ENV_MAP/_BOOL_KEYS/SCHEMA）
  F9 run_cqs 的 sparql 结果带 freshness 字段（ping 失败时 unknown）
  M1 抹 is_stale ⇒ F2/F3 判红
  M2 抹 error_status ⇒ F4 判红
  M3 期望值不去重 ⇒ F5/F1 判红
  M4 把自动重灌条件改成 unknown ⇒ F6 判红
"""
import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_fuseki_freshness_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db              # noqa: E402
init_db()

from core import fuseki_freshness as ff           # noqa: E402
import services.sparql_sync as ss                 # noqa: E402

_RESULTS = []


def ck(name, cond, detail=""):
    _RESULTS.append((bool(cond), name, detail))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + detail) if detail else ""))


class _FakeCli:
    """可注入的 Fuseki 客户端：COUNT 返回数量，ASK 按 present 集合回答。"""

    def __init__(self, count: int, present=()):
        self.count = count
        self.present = set(present)

    def query(self, sparql):
        if "COUNT(*)" in sparql:
            return {"results": {"bindings": [{"n": {"value": str(self.count)}}]}}
        m = re.search(r"<([^>]+)>", sparql or "")
        return {"boolean": (m.group(1) in self.present) if m else True}


class _BrokenCli:
    def query(self, *a, **k):
        raise RuntimeError("新鲜度回读故障注入")


def _uri_of(eid: str) -> str:
    return ss._ttl_uri(eid).strip("<>")


def _fixture(c):
    for t in ("entities", "relations", "ontology_types"):
        c.execute("DELETE FROM " + t)
    c.execute("INSERT INTO ontology_types (name,type_kind) VALUES ('需求','entity')")
    for i in range(6):
        c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties,is_current) "
                  "VALUES (?,?,?,'reviewed','需求','{}',1)",
                  ("FF%d" % i, "探针%d" % i, "dev"))
    # 真机形态：同一 id 跨分支重复 ⇒ 生成的 RDF 三元组在 Fuseki 里会去重。
    # 没有这几行，"按行数 vs 按集合"两种口径算出同一个数，M3 就变异不出来。
    for i in range(3):
        c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties,is_current) "
                  "VALUES (?,?,?,'reviewed','需求','{}',1)",
                  ("FF%d" % i, "探针副本%d" % i, "release"))
    c.commit()


def _truth_distinct(c) -> int:
    """独立真值：直接数 TTL 的三元组集合，不经过被测的 expected_base_count。"""
    ttl = ss.build_ontology_ttl(c) + "\n" + ss.build_entities_ttl(c)
    return len({ln.strip() for ln in ttl.splitlines() if ln.strip().endswith(" .")})


def _data_checks(c, add):
    _fixture(c)
    truth = _truth_distinct(c)          # 独立真值（Fuseki 侧真实会存的数量）
    exp = ff.expected_base_count(c)     # 产品函数（可能与真值漂移，正是要抓的）
    samples = ff.sample_entity_ids(c, 8)
    present = {_uri_of(e) for e in samples}
    # F1 必须用独立真值当 actual：若产品期望值虚高（不去重），这里立刻判 stale。
    add("F1 镜像齐全 ⇒ fresh",
        ff.check_freshness(c, _FakeCli(truth, present))["status"] == "fresh",
        "truth=%d expected=%d samples=%d" % (truth, exp, len(samples)))
    add("F2 三元组数不足 ⇒ stale",
        ff.check_freshness(c, _FakeCli(max(0, truth - 1), present))["status"] == "stale")
    add("F3 数量够但抽样缺失 ⇒ stale",
        ff.check_freshness(c, _FakeCli(truth, set()))["status"] == "stale")
    add("F4 回读异常 ⇒ unknown（不自动重灌）",
        ff.check_freshness(c, _BrokenCli())["status"] == "unknown")
    # F5：同一 TTL 人为重复 10 倍，集合口径的期望值必须不变
    orig_build = ss.build_entities_ttl
    try:
        ss.build_entities_ttl = lambda conn, pid=None: "\n".join(
            [orig_build(conn, pid)] * 10)
        duplicated = ff.expected_base_count(c)
    finally:
        ss.build_entities_ttl = orig_build
    add("F5 期望值按集合去重（重复行不抬期望）", duplicated == truth,
        "truth=%d expected=%d duplicated=%d" % (truth, exp, duplicated))


def _src(path):
    with open(os.path.join(_ROOT, path), encoding="utf-8") as f:
        return f.read()


def _static_checks(add, main_src=None, sparql_src=None, cq_src=None, cfg_src=None):
    main_src = main_src if main_src is not None else _src("main.py")
    sparql_src = sparql_src if sparql_src is not None else _src("routers/sparql.py")
    cq_src = cq_src if cq_src is not None else _src("services/ontology_cq.py")
    cfg_src = cfg_src if cfg_src is not None else _src("core/config.py")
    add("F6 仅 stale 才自动重灌（unknown 不动）",
        'if _fr.get("status") == "stale":' in main_src
        and "sync_to_fuseki_with_time" in main_src)
    add("F7 freshness 端点 + CQ 元数据 + summarize",
        '@router.get("/freshness")' in sparql_src
        and '"freshness"' in cq_src and '"stale": sum' in cq_src)
    add("F10 ping 传 base URL（不是完整端点）",
        "endpoint_parts(_fuseki_rt.resolve_endpoint())" in cq_src
        and "if _fuseki_rt.ping(_base):" in cq_src)
    add("F8 配置四处登记",
        cfg_src.count("fuseki_autosync") >= 4,
        "出现 %d 次" % cfg_src.count("fuseki_autosync"))


def main():
    caught1 = caught2 = caught3 = caught4 = False
    # 本门禁守的是**数量下界 + 抽样**旧判据（F1~F5）；第九批③新增的精确集合档
    # 由 verify_fuseki_exact_freshness.py 单独守。部署配置一旦打开精确档，
    # 这里的假客户端就收不到它认识的两类查询 ⇒ 全变 unknown、门禁测不到自己要测的东西。
    # 显式锁定旧路径，两个判据各测各的，不靠部署配置碰运气。
    orig_exact = ff._exact_enabled
    ff._exact_enabled = lambda: False
    try:
        c = get_db()
        data = []
        _data_checks(c, lambda n, ok, d="": data.append((ok, n, d)))
        stat = []
        _static_checks(lambda n, ok, d="": stat.append((ok, n, d)))

        # F9：不依赖真机 Fuseki（ping 强制 False），验证 CQ 结果带 freshness 字段
        import services.ontology_cq as ocq
        import core.fuseki_runtime as frt
        c.execute("INSERT INTO ontology_questions (question,kind,query,expected,enabled,source,created_by) "
                  "VALUES ('F9探针','sparql','SELECT ?s WHERE { ?s ?p ?o } LIMIT 1','nonempty',1,'gate','system')")
        c.commit()
        orig_ping = frt.ping
        orig_cli_ping = ss.FusekiClient.ping
        try:
            # 两处 ping 都要拦：run_cqs 判新鲜度用 frt.ping，CQ 执行用客户端 ping。
            # 只拦前者会真的查到线上 Fuseki ⇒ 判据依赖真机状态，假绿/假红都不稳定。
            frt.ping = lambda *a, **k: False
            ss.FusekiClient.ping = lambda self, *a, **k: False
            rs = ocq.run_cqs(c, source="F9", record=False)
        finally:
            frt.ping = orig_ping
            ss.FusekiClient.ping = orig_cli_ping
        f9 = rs[-1]
        data.append((f9.get("freshness") == "unknown" and f9.get("status") == "skip",
                     "F9 CQ 结果带 freshness 字段", str(f9)[:150]))

        for ok, name, detail in data + stat:
            ck(name, ok, detail)

        print("\n--- 变异自证 ---")
        # M1：抹陈旧判据 ⇒ F2/F3 必须判红
        orig_stale = ff.is_stale
        try:
            ff.is_stale = lambda e, a, m: False
            m1 = []
            _data_checks(c, lambda n, ok, d="": m1.append((n, ok, d)))
            pair = [x for x in m1 if "F2" in x[0] or "F3" in x[0]]
            caught1 = len(pair) == 2 and all(not x[1] for x in pair)
            print("  [%s] M1 抹 is_stale ⇒ F2/F3 判红" % ("PASS" if caught1 else "FAIL"))
        finally:
            ff.is_stale = orig_stale

        # M2：异常误归 stale ⇒ F4 判红
        orig_err = ff.error_status
        try:
            ff.error_status = lambda ex: "stale"
            m2 = []
            _data_checks(c, lambda n, ok, d="": m2.append((n, ok, d)))
            caught2 = not next(x[1] for x in m2 if "F4" in x[0])
            print("  [%s] M2 抹 error_status ⇒ F4 判红" % ("PASS" if caught2 else "FAIL"))
        finally:
            ff.error_status = orig_err

        # M3：期望值回到"按行数不去重" ⇒ F5/F1 判红
        orig_exp = ff.expected_base_count
        try:
            def raw_count(conn, project_id=None):
                ttl = ss.build_ontology_ttl(conn) + "\n" + ss.build_entities_ttl(conn, project_id)
                return sum(1 for ln in ttl.splitlines() if ln.strip().endswith(" ."))
            ff.expected_base_count = raw_count
            m3 = []
            _data_checks(c, lambda n, ok, d="": m3.append((n, ok, d)))
            pair = [x for x in m3 if "F5" in x[0] or "F1" in x[0]]
            caught3 = len(pair) == 2 and all(not x[1] for x in pair)
            print("  [%s] M3 期望值不去重 ⇒ F5/F1 判红" % ("PASS" if caught3 else "FAIL"))
        finally:
            ff.expected_base_count = orig_exp

        # M4：把自动重灌条件改成 unknown ⇒ F6 判红（静态源码变异）
        m4 = []
        mutated_main = _src("main.py").replace(
            'if _fr.get("status") == "stale":', 'if _fr.get("status") == "unknown":')
        _static_checks(lambda n, ok, d="": m4.append((n, ok, d)), main_src=mutated_main)
        caught4 = not next(x[1] for x in m4 if "F6" in x[0])
        print("  [%s] M4 unknown 也重灌 ⇒ F6 判红" % ("PASS" if caught4 else "FAIL"))
        c.close()
    finally:
        ff._exact_enabled = orig_exact
        shutil.rmtree(_TMP, ignore_errors=True)

    n_fail = sum(1 for x in _RESULTS if not x[0])
    mut_ok = caught1 and caught2 and caught3 and caught4
    print("=" * 70)
    if n_fail or not mut_ok:
        print("结论：未通过（常态 %d 失败 / 变异 %s）" % (n_fail, mut_ok))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 4 组按预期判红）" % len(_RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
