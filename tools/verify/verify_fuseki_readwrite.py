# CI-OPTIONAL: A 需运行中的 Fuseki SPARQL 服务（2026-10-18 干净库实测 C1 ping 即 HTTPError——服务不在）；CI 无此外部依赖
"""门禁：Fuseki 图库「可写 + 持久化」必须同时成立。

## 为什么要有这个门禁

Fuseki 的 `--loc` **默认以只读方式打开数据集**。只加 `--loc` 不加 `--update` 时：
  - `POST /{ds}/update` 返回 **HTTP 405**；
  - 但 `GET /{ds}/query`、`$/ping` 全部正常，**日志里也看不到任何 ERROR**。
⇒ 典型的「看起来一切正常、但写不进去」。业务侧 `graph_db.write_triples` 会
   抛/吞掉失败，图谱**静默停止更新**，而所有健康检查都是绿的。

**可写与持久化不是二选一**：`--update --loc=<dir>` 同时满足。
本门禁就是守住这一条，且用**行为验**（真写真读）而不是读启动参数。

## 判定
  C1 实例可达（ping）
  C2 SPARQL Update 可写（204）——漏 --update 时这里必红
  C3 写入的数据能按命名图读回
  C4 写的是类 IRI 而非字面量（type 谓词的前提；承 verify_graph_db_enabled.py）
  C5 删除需传**规范化后**的 id（`ent_uri` 把 `-` 归一为 `_`，
     `delete_subjects` 不做同样归一 ⇒ 传原名会静默删 0 条）

⚠️ C5 是 2026-10-12 实测踩到的：写入 `PROBE-SUBJ` 落库为 `ent/PROBE_SUBJ`，
   而 `delete_subjects(['PROBE-SUBJ'])` 返回 `{'deleted': 0}` 且不报错。
"""
import json
import os
import sys
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import graph_db as _gdb  # noqa: E402  （必须在 sys.path 注入之后）

_RESULTS = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    _RESULTS.append((bool(cond), label, detail))
    print(("  [PASS] " if cond else "  [FAIL] ") + label + ("  " + detail if detail else ""))
    return bool(cond)


def _endpoint() -> str:
    """从本地配置读 endpoint；读不到就退回默认端口组合。"""
    try:
        p = os.path.expanduser("~/.workbuddy/mbse_config.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                ep = (json.load(f).get("graph_db") or {}).get("endpoint")
            if ep:
                return ep.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:3030/mbse"


def _post(url: str, body: str, ctype: str) -> int:
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                 headers={"Content-Type": ctype,
                                          "Accept": "application/sparql-results+json, */*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ⚠️ Fuseki 的 /query **不带 Accept 时默认回 XML**，直接 json.loads 会抛
#    JSONDecodeError。曾把这个异常 `except: return []` 吞掉 ⇒ 判据失明，
#    「查询失败」被误报成「真的 0 条」，一次把三个用例判红。
#    ⇒ 这里返回 (rows, err)：查询失败必须**显式失败**，绝不能等价为 0 条。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _query(ep: str, q: str):
    req = urllib.request.Request(ep + "/query", data=q.encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/sparql-query",
                                          "Accept": "application/sparql-results+json"})
    try:
        with _OPENER.open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))["results"]["bindings"], ""
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])


def _count(ep: str) -> int:
    rows, err = _query(ep, "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }")
    if rows is None:
        raise RuntimeError("查询失败，无法读取条数 —— " + err)
    return int(rows[0]["n"]["value"]) if rows else 0


def main() -> int:
    ep = _endpoint()
    base = ep.rsplit("/", 1)[0]
    print("=== 门禁：Fuseki 可写 + 持久化 ===")
    print("endpoint =", ep)

    # C1 可达
    try:
        with urllib.request.urlopen(base + "/$/ping", timeout=15) as r:
            check(r.status == 200, "C1 实例可达（ping）")
    except Exception as e:
        check(False, "C1 实例可达（ping）", type(e).__name__)
        _summary()
        return 1

    probe_graph = "urn:g:verify_rw"
    try:
        n_before = _count(ep)
    except RuntimeError as e:
        check(False, "C0 查询通道可用（Accept: sparql-results+json）", str(e))
        _summary()
        return 1

    # C2 可写：漏 --update 时这里必红（405）
    _post(ep + "/update", "DROP GRAPH <%s>" % probe_graph, "application/sparql-update")
    st = _post(ep + "/update",
               "PREFIX : <urn:rw:> INSERT DATA { GRAPH <%s> { :s :p :o } }" % probe_graph,
               "application/sparql-update")
    check(st == 204, "C2 SPARQL Update 可写", "HTTP %s（405 ⇒ 启动漏了 --update）" % st)
    if st != 204:
        _summary()
        return 1

    # C3 按命名图读回（写入立即可读 ⇒ 排除"异步落盘"造成的假阴性）
    got, err = _query(ep, "SELECT ?s WHERE { GRAPH <%s> { ?s ?p ?o } }" % probe_graph)
    check(got is not None and len(got) == 1,
          "C3 写入数据能按命名图读回",
          ("查询失败: " + err) if got is None else "回读 %d 条" % len(got))

    # C4 type 谓词必须写成**类 IRI**（OWL 公理/推理机前提），不是字面量
    # ⚠️ 判据本身也踩过坑：曾把 class_uri 放在 ?s 位置查，而写入时 ?s 是 entity IRI
    #    ⇒ 恒查不到。正确做法是按**写入时用的 IRI 构造**去查。
    w = _gdb.FusekiWriter(endpoint=ep)
    w.write_triples([{"s": "V2G_PROBE_C", "p": "type",
                      "o_value": "部件", "o_type": "class", "graph": "dev"}])
    q4 = ("SELECT ?o WHERE { GRAPH ?g { <%s> <%s> ?o } }"
          % (_gdb._iri_encode(_gdb.ent_uri("V2G_PROBE_C")),
             _gdb._iri_encode(_gdb.pred_uri("type", "class"))))
    rows4, err4 = _query(ep, q4)
    ok4 = (rows4 is not None and len(rows4) == 1
           and rows4[0]["o"]["type"] == "uri"
           and rows4[0]["o"]["value"].endswith(_gdb.class_uri("部件")))
    check(ok4, "C4 type 谓词写的是类 IRI 而非字面量",
          ("查询失败: " + err4) if rows4 is None else
          ("取到 %r" % (rows4[0]["o"] if rows4 else None)))
    w.delete_subjects(["V2G_PROBE_C"], graph="dev")

    # C5 删除须传**规范化后**的 id（ent_uri 把 `-` 归一为 `_`，而 delete_subjects
    #    不做同样归一 ⇒ 传原名返回 deleted:0 且不报错）
    # ⚠️ 判据也踩过坑：曾用「主语含连字符」来定位，但写入时连字符**已被规范化掉**，
    #    该条件恒为假 ⇒ 假阴性。改为按规范化后的真实 IRI 查。
    raw_id, norm_id = "V2G-PROBE-D", "V2G_PROBE_D"
    w.write_triples([{"s": raw_id, "p": "p1",
                      "o_value": "x", "o_type": "literal", "graph": "dev"}])
    q5 = ("SELECT ?s WHERE { GRAPH ?g { <%s> ?p ?o } }"
          % _gdb._iri_encode(_gdb.ent_uri(norm_id)))
    rows5, _ = _query(ep, q5)
    check(bool(rows5), "C5a 连字符 id 写入后落在规范化 IRI 上",
          "写入 %r ⇒ 实际存 %s" % (raw_id, _gdb.ent_uri(norm_id)))
    if rows5:
        d_raw = w.delete_subjects([raw_id], graph="dev")
        still, _ = _query(ep, q5)
        check(int((d_raw or {}).get("deleted", 0)) == 0 and bool(still),
              "C5b 传未规范化 id 删除静默失效（已知坑）",
              "deleted=%s，残留=%s" % ((d_raw or {}).get("deleted"), bool(still)))
        d_ok = w.delete_subjects([norm_id], graph="dev")
        check(int((d_ok or {}).get("deleted", 0)) >= 1,
              "C5c 传规范化 id 可正常删除", "deleted=%s" % (d_ok or {}).get("deleted"))

    # 复原
    _post(ep + "/update", "DROP GRAPH <%s>" % probe_graph, "application/sparql-update")
    n_after = _count(ep)
    check(n_after == n_before, "C6 探针已清理干净", "%d -> %d" % (n_before, n_after))

    _summary()
    return 0 if all(c for c, _, _ in _RESULTS) else 1


def _summary() -> None:
    ok = sum(1 for c, _, _ in _RESULTS if c)
    bad = [l for c, l, _ in _RESULTS if not c]
    print("=" * 60)
    print("结果：%d pass / %d fail" % (ok, len(_RESULTS) - ok))
    if bad:
        print("失败项：", "；".join(bad))
    print("=" * 60)
    print("提示：405 ⇒ Fuseki 启动缺 --update（--loc 默认只读）；")
    print("      「写入返回成功但宾语为空」⇒ literal 客体要用 o_value 而非 o。")


if __name__ == "__main__":
    sys.exit(main())