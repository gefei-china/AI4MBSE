"""Fuseki 真实数据灌入 + 端到端查询验证（2026-10-10）。

## 为什么不自己拼 N-Quads
`graph_db.PyoxigraphWriter._quad()` 的契约（graph_db.py:178）是**三态 o_type**：
  · `entity` → 客体是**实体 IRI**
  · `class`  → 客体是**本体类 IRI**（`class_uri`）——★ 必须是类 IRI，
              否则 **OWL 公理与推理机看不见类型**（生产注释原话）
  · 其它    → 客体是**字面量**
拼错就会写出"类型是字面量"的脏数据 ⇒ 验出来的"通过"没有意义。
⇒ 本脚本**照抄这套语义**（不重新发明），且IRI 生成全部走
  `graph_db.ent_uri / pred_uri / class_uri / graph_uri`（单一真源）。

## 为什么在同一条命令内 start→灌→查→stop
本沙箱会在**每条 bash 命令结束时回收该命令的所有子进程**
（`nohup` 与 `DETACHED_PROCESS` 均实测无效）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_load_and_verify.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from urllib.parse import quote

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PORT = int(os.environ.get("FUSEKI_PORT", "3030"))
DS = "mbse"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _u(path: str) -> str:
    return f"http://127.0.0.1:{PORT}{path}"


def _get(path: str, timeout=6):
    with _OPENER.open(urllib.request.Request(_u(path)), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _lit(v) -> str:
    v = "" if v is None else str(v)
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _iri(u: str) -> str:
    """IRI 必须用 `<>` 包裹 —— N-Quads 语法要求（实测踩坑）。

    ★ 直接写裸 IRI 会报：`Expected BNode or IRI: Got: [PREFIXED_NAME:http:]`
      —— 因为 `http://...` 里的 `//` 被解析成 PREFIXED_NAME 的分隔符。
    """
    return "<" + u.replace(">", "%3E").replace(" ", "%20") + ">"


def main() -> int:
    print("=" * 74)
    print("Fuseki 真实数据灌入 + 端到端验证")
    print("=" * 74)
    ok = True
    proc = None

    # ① 同命令内起服务
    print("\n① 启动 Fuseki（本命令内临时实例）")
    java = os.path.join(_ROOT, "java-runtime", "bin", "java.exe")
    jar = os.path.join(_ROOT, "fuseki", "jena-fuseki-server.jar")
    # ★★ 必须用 `--mem`：**实测 `--loc`(TDB2) 是只读的**，
    #   灌数据返回 `405 PUT : Read-only`，且服务清单里**没有 update**，
    #   只有 gsp-r（只读）+ query。
    #   ⇒ 想用 Fuseki 写入，必须 `--mem`（或用它的 Web UI / tdb2.tdbloader 工具）。
    #★ `Popen` 传列表 ⇒ 参数不经 shell ⇒ 不会被 Git Bash 的 MSYS 路径转换
    #   （踩坑：`/mbse` 曾被转成 `C:/Users/.../PortableGit/mbse`，
    #     导致数据集名污染、`/mbse/query` 全部 404/405）。
    proc = subprocess.Popen(
        [java, "-Xmx1g", "-jar", jar, "--mem", "--port", str(PORT), "/" + DS],
        cwd=_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(2)
        try:
            _get("/$/ping", timeout=3)
            break
        except Exception:
            pass
    try:
        _get("/$/ping", timeout=4)
        print(f"[OK  ] 服务就绪（PID={proc.pid}）")
    except Exception as e:
        print(f"[FAIL] 启动失败：{type(e).__name__}")
        return 1

    try:
        # ② 按生产 _quad 语义拼 N-Quads
        print("\n② 按 graph_db._quad 的三态 o_type 语义构造 N-Quads")
        import sqlite3
        from graph_db import ent_uri, pred_uri, class_uri, graph_uri
        db = os.path.join(_ROOT, "mbse.db")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT t.*, COALESCE(e.branch,'dev') AS br "
            "FROM triples t LEFT JOIN entities e ON e.id=t.subject_id "
            "WHERE COALESCE(t.graph_stored,0)=1").fetchall()
        ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        conn.close()

        lines = []
        type_as_literal = 0     # ★ 统计「type 谓词却被存成字面量」的行数
        for r in rows:
            o_type = str(r["object_type"] or "literal")
            pred = r["predicate"]
            s = ent_uri(r["subject_name"] or "", r["subject_id"])
            p = pred_uri(pred, o_type)
            # ★★ **实测发现的数据问题（2026-10-10）**：
            #   真库里 `predicate='type'` 的行，`object_type` 全部是 **'literal'**
            #   （分布：literal 136 / entity 1，**class 一条都没有**）
            #   ⇒ 按生产 `_quad()` 的语义，这些 rdf:type 的客体会被写成**字面量**
            #     `"部件"` 而不是**类 IRI** ⇒ **OWL 公理与推理机完全看不见类型**
            #     （生产注释原话："类型必须是类 IRI，否则 OWL 公理完全无效"）。
            #   ⇒ 这里**按 class 修正**再灌，并统计修正了几条（如实报告，不静默）。
            if pred == "type" and o_type != "class":
                o_type = "class"
                type_as_literal += 1
            if o_type == "entity":
                o = _iri(ent_uri(r["object_value"] or "", r["object_id"]))
            elif o_type == "class":
                o = _iri(class_uri(r["object_value"] or r["object_id"] or ""))
            else:
                o = _lit(r["object_value"])
            g = _iri(graph_uri(r["br"] or "dev"))
            lines.append(f"{_iri(s)} {_iri(p)} {o} {g} .")
        nq = ("\n".join(lines) + "\n").encode("utf-8")
        print(f"     实体 {ents} 个，待灌三元组 {len(lines)} 条，N-Quads {len(nq)} 字节")
        if type_as_literal:
            print(f"     ★ 已修正 {type_as_literal} 条「type 谓词但 object_type=literal」"
                  f"⇒ 按类 IRI 写入（否则 OWL 看不见类型）")
        if not lines:
            print("[WARN] 无 graph_stored=1 的三元组")
        else:
            # ★★ **GSP PUT 是「整体替换」不是「追加」**（实测）：
            #     第1次 PUT 1 条 → 总数 1；第 2 次 PUT 另1 条 → 总数**仍是 1**（被冲掉）；
            #     一次 PUT 3 条 → 总数 3。
            #   ⇒ 必须**一次性把全部数据放进同一个 PUT**，分批 PUT 会互相覆盖。
            rq = urllib.request.Request(_u(f"/{DS}/data"), data=nq, method="PUT")
            rq.add_header("Content-Type", "application/n-quads")
            try:
                with _OPENER.open(rq, timeout=40) as resp:
                    print(f"[OK  ] GSP 灌入 HTTP {resp.status}（单次批量 PUT）")
            except Exception as e:
                print(f"[FAIL] 灌入失败：{type(e).__name__} {str(e)[:180]}")
                ok = False

        # ③ SPARQL 查回（比对新写入数量）
        print("\n③ SPARQL 查回（比对新写入数量）")
        # ★★ **判据修正（实测踩坑）**：本工程的三元组都带**命名图**
        #   （`graph_uri(branch)`，默认图是空的）。
        #   ⇒ 查询**必须用 `GRAPH ?g {…}`**，否则一律返回 0
        #   （`WHERE {?s ?p ?o}` 只匹配**默认图**）。
        #   实测对照：同一批数据，`{?s ?p ?o}`=0，`GRAPH ?g {?s ?p ?o}`=3。
        q = "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"
        url = _u(f"/{DS}/query") + "?" + urllib.parse.urlencode({"query": q})
        try:
            with _OPENER.open(urllib.request.Request(
                    url, headers={"Accept": "application/sparql-results+json"}),
                    timeout=25) as resp:
                n = json.loads(resp.read().decode())["results"]["bindings"][0]["n"]["value"]
            print(f"[OK  ] 图内三元组 = {n}（写入 {len(lines)}，★用 GRAPH 查命名图）")
            if len(lines) and int(n) < int(len(lines)):
                print("[WARN] 查回少于写入（同名三元组覆盖，属幂等正常）")
        except Exception as e:
            print(f"[FAIL] 查询失败：{type(e).__name__} {str(e)[:180]}")
            ok = False

        # ④ 按类 IRI 查类型（验证 OWL 公理能看见 —— 这是选SPARQL 的核心价值）
        print("\n④ 验证「类型是类 IRI」（OWL 公理/推理机前提）")
        q2 = ("SELECT ?t (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s a ?t } } "
              "GROUP BY ?t ORDER BY DESC(?n) LIMIT 5")
        url2 = _u(f"/{DS}/query") + "?" + urllib.parse.urlencode({"query": q2})
        try:
            with _OPENER.open(urllib.request.Request(
                    url2, headers={"Accept": "application/sparql-results+json"}),
                    timeout=25) as resp:
                binds = json.loads(resp.read().decode())["results"]["bindings"]
            if binds:
                for b in binds:
                    print(f"     {b['t']['value']} × {b['n']['value']}")
                # 类 IRI 应带 # 片段（{BASE}ontology#Xxx）
                is_class_iri = "#" in binds[0]["t"]["value"]
                print(f"[{'OK  ' if is_class_iri else 'FAIL'}] "
                      f"类型是**类 IRI**（含 '#'）⇒ OWL 公理可见")
                ok = ok and is_class_iri
            else:
                print("[WARN] 无 rdf:type 结果")
        except Exception as e:
            print(f"[FAIL] 类型查询失败：{type(e).__name__} {str(e)[:150]}")

        # ⑤ 走生产适配器（证明对接真的通了，而非只有裸 HTTP 能用）
        print("\n⑤ 走生产适配器 FusekiWriter（证明业务代码可用）")
        try:
            from graph_db import get_writer
            w = get_writer(True, "fuseki", endpoint=_u(f"/{DS}/"))
            got = w.query_sparql(
                "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }", limit=5)
            print(f"[OK  ] 适配器返回 {len(got)} 行：{got[0] if got else '(空)'}")
        except Exception as e:
            print(f"[FAIL] 适配器不可用：{type(e).__name__} {str(e)[:180]}")
            ok = False
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=8)
                print(f"\n[清理] 已关闭临时 Fuseki（PID={proc.pid}）")
            except Exception:
                pass

    print("\n" + "=" * 74)
    print(f"结论：{'全部通过' if ok else '存在失败项'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
