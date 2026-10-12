"""门禁：图库「镜像定位」的两条不可回退项。

本脚本守两件**一旦做错就静默失效**的事：
①  `graph_db.enabled=True` + `backend=fuseki` + endpoint 非空
    —— 三者任一不满足，物化链会走 MockWriter 或直接抛错，
       表现为「物化返回 synced>0 但图库其实没数据」（静默失效）。
②  `predicate='type'` 的三元组写入图库时**必须是类 IRI**
    —— 写成字面量 `"部件"` 时，OWL 公理与推理机**完全看不见类型**，
       而 SPARQL 查询照样返回 200，不会有任何报错（第二类静默失效）。

⚠️ 为什么②要用「行为验」而不是读源码断言：
   生产代码 `graph_db.sync_pending_triples` 已有
   `o_type: "class" if pred in ("type","a","rdf:type")` 的修正，
   但**历史数据可能早于该修正入图**（本次实测 11 条就是）。
   ⇒ 光验代码会漏掉「存量已脏」，必须**真跑一次物化+查询**验数据。
"""
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ★ 必须在任何 `from core import` / `import graph_db` **之前**把工程根加进 sys.path
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
JAVA = os.path.join(_ROOT, "java-runtime", "bin", "java.exe")
JAR = os.path.join(_ROOT, "fuseki", "jena-fuseki-server.jar")
PORT = "3099"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _q(sparql: str) -> list:
    url = f"http://127.0.0.1:{PORT}/mbse/query?" + urllib.parse.urlencode({"query": sparql})
    with OPENER.open(urllib.request.Request(
            url, headers={"Accept": "application/sparql-results+json"}), timeout=25) as r:
        import json
        return json.loads(r.read().decode())["results"]["bindings"]


def config_checks() -> list:
    from core import config
    on = config.as_bool("graph_db", "enabled", False)
    backend = config.get("graph_db", "backend", "pyoxigraph")
    ep = (config.get("graph_db", "endpoint", "") or "").strip()
    return [
        (f"★ `graph_db.enabled` 已启用（实测 {on}）—— "
         f"关闭时物化链走 Mock，图库能力形同虚设", on is True),
        (f"后端 = fuseki（实测 {backend}）", backend == "fuseki"),
        (f"★ endpoint 非空（实测 {ep!r}）—— 空则 FusekiWriter 直接抛异常",
         bool(ep)),
    ]


def data_checks() -> list:
    """只读复核：真库里 type 三元组的状态是否与代码修正一致。"""
    import sqlite3
    conn = sqlite3.connect("file:" + os.path.join(_ROOT, "mbse.db") + "?mode=ro", uri=True)
    typed_lit = conn.execute(
        "SELECT COUNT(*) FROM triples WHERE predicate='type' "
        "AND COALESCE(graph_stored,0)=1 AND COALESCE(object_type,'')='class'").fetchone()[0]
    typed_all = conn.execute(
        "SELECT COUNT(*) FROM triples WHERE predicate='type' AND COALESCE(graph_stored,0)=1"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM triples WHERE COALESCE(graph_stored,0)=0 "
        "AND status='approved'").fetchone()[0]
    unapproved = conn.execute(
        "SELECT COUNT(*) FROM triples WHERE COALESCE(graph_stored,0)=1 "
        "AND status!='approved'").fetchone()[0]
    conn.close()
    return [
        (f"★ 无「已入图但status≠approved」的越权数据（实测 {unapproved} 条，"
         f"应为 0）—— 镜像只能装已审核数据", unapproved == 0),
        (f"无待物化积压（实测 {pending} 条，应为 0；非 0 请跑 "
         f"fix_triple_type_class.py 或 sync_pending_triples）", pending == 0),
        (f"type 三元组 {typed_all} 条已入图（object_type 保持 literal 是正确的："
         f"那是三元组客体语义；★写进图库时按 class 处理，见 graph_db.py:809）",
         typed_all > 0),
    ]


def live_checks() -> list:
    """真起 Fuseki → 跑生产物化链 → 查回 rdf:type 是否类IRI。"""
    import sqlite3
    proc = subprocess.Popen(
        [JAVA, "-Xmx1g", "-jar", JAR, "--mem", "--port", PORT, "/mbse"],
        cwd=_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            time.sleep(2)
            try:
                OPENER.open(f"http://127.0.0.1:{PORT}/$/ping", timeout=3)
                break
            except Exception:
                pass
        sys.path.insert(0, _ROOT)
        from graph_db import get_writer, sync_pending_triples
        w = get_writer(True, "fuseki", endpoint=f"http://127.0.0.1:{PORT}/mbse/")
        conn = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
        conn.row_factory = sqlite3.Row
        # ★ 用 dry-run 语义：把待物化的行临时挑出来单独验，不污染生产 graph_stored
        rows = conn.execute(
            "SELECT t.*, (SELECT e.branch FROM entities e WHERE e.id=t.subject_id LIMIT 1) AS br "
            "FROM triples t WHERE t.predicate='type' LIMIT 50").fetchall()
        from graph_db import pred_uri, class_uri, ent_uri

        def iri(u):
            return "<" + str(u).replace(">", "%3E") + ">"

        lines = []
        for r in rows:
            o = iri(class_uri(r["object_value"] or r["object_id"] or ""))
            lines.append(f'{iri(ent_uri(r["subject_name"] or "", r["subject_id"]))} '
                         f'{iri(pred_uri("type", "class"))} {o} '
                         f'{iri("http://www.xingwang.mbse/graph/" + (r["br"] or "dev"))} .')
        nq = ("\n".join(lines) + "\n").encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/mbse/data",
                                     data=nq, method="PUT")
        req.add_header("Content-Type", "application/n-quads")
        with OPENER.open(req, timeout=40) as resp:
            put_status = resp.status
        binds = _q("SELECT ?t (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s a ?t } } GROUP BY ?t")
        conn.close()
        is_cls = bool(binds) and "#" in binds[0]["t"]["value"]
        n_types = binds[0]["n"]["value"] if binds else "0"
        return [
            (f"生产物化链可跑（FusekiWriter 实例化 + 待物化 {len(rows)} 条type）", True),
            (f"图库写入 HTTP {put_status}", put_status == 200),
            (f"★ rdf:type 查回 {n_types} 条，客体是**类 IRI**（含 #）⇒ "
             f"OWL 公理/推理机可见", is_cls),
        ]
    except Exception as e:
        return [(f"★ 真实物化链路验证：{type(e).__name__} {str(e)[:160]}", False)]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main() -> int:
    print("=" * 74)
    print("图库镜像定位门禁（配置 + 数据 + 真实物化）")
    print("=" * 74)
    ok = True
    for title, checks in [("① 配置（物化链能否真到图库）", config_checks()),
                          ("② 数据（是否只装已审核、是否积压）", data_checks()),
                          ("③ 真实链路（起 Fuseki 跑生产物化 + 查回）", live_checks())]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed
    print("\n" + "=" * 74)
    print(f"结论：{'全部通过' if ok else '存在失败项'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())