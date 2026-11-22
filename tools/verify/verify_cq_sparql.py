#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：CQ SPARQL 档（P1-2，2026-11-21 新增）。

## 要挡住的是什么

`ontology_cq` 新增 `kind='sparql'` 档（走 Fuseki）。这一档有四个容易在后续改动中
悄悄坏掉的点，全是真机验证抓出来的：

  S1  白名单必须拦住 SPARQL Update 的写动词 —— 包括不叫 update 但会改图的
      LOAD / CLEAR / MOVE / COPY / ADD。CQ 是用户可登记的输入，等价于受控查询面。
  S2  **Fuseki 不可达 ⇒ skip，绝不是 pass**。降级伪装成通过 = "图库断了"被
      报成"图查询通过"。这是 P1-2 的第一红线。
  S3  `PREFIX` 前导必须放行 —— 首版白名单只认 SELECT/ASK 开头，带 PREFIX 的
      合法 CQ 全被拒（真机抓到，症状是"查询有数据却 skip"）。
  S4  通过项的 detail 必须为空串 —— 成功时输出 `'A' != 'A'` 这种反向文案
      会把排障方向引歪（与写入口门禁 G5d 同源教训）。

## 判据（每条都能判红，见末尾变异自证）

  S1  白名单 8 判例（4 过 4 拒）
  S2  端点指向一个必然不通的端口 ⇒ status 必须 skip
  S3  PREFIX 前导 + 主句 SELECT ⇒ 放行；主句 INSERT ⇒ 拒
  S4  通过项无 detail；失败项有 detail
  S5  kind 分派：sparql 走 _run_sparql_cq、sql 走 conn.execute、未知 kind ⇒ skip
      （防"sparql 失败静默回落 sql"—— 回落会把口径变化藏起来）
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []
PASS, FAIL = "PASS", "FAIL"


def _add(ok, name, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:300]))


def check_s1():
    from services.ontology_cq import validate_sparql
    cases = [
        ("SELECT ?s WHERE { ?s ?p ?o } LIMIT 1", True),
        ("ASK { ?s <http://x/a> ?o }", True),
        ("PREFIX o: <http://mbse/ontology/>\nSELECT ?s WHERE { ?s o:SATISFIES ?o }", True),
        ("BASE <http://mbse/>\nSELECT ?s WHERE { ?s ?p ?o }", True),
        ("INSERT DATA { <a> <b> <c> }", False),
        ("DELETE WHERE { ?s ?p ?o }", False),
        ("LOAD <http://example.org/data>", False),
        ("CLEAR GRAPH <http://g>", False),
        ("MOVE DEFAULT TO GRAPH <http://g>", False),
        ("SELECT ?s WHERE { ?s ?p ?o }; DROP ALL", False),   # 多语句
        ("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", False),  # 返回图，CQ 判不了
    ]
    bad = []
    for q, want in cases:
        ok, _ = validate_sparql(q)
        if ok != want:
            bad.append(f"{q[:36]!r} 期望{want}实得{ok}")
    _add(not bad, f"S1 SPARQL 白名单判例（{len(cases)} 例）", "; ".join(bad[:4]))


def check_s2():
    """Fuseki 不可达 ⇒ skip。用一个必然不通的端口模拟。"""
    import sqlite3
    from services import ontology_cq as m
    orig = m._sparql_endpoint
    try:
        # 占用概率极低的端口 + 无服务 ⇒ ping 必败
        m._sparql_endpoint = lambda: "http://127.0.0.1:39999/mbse"
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE ontology_questions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT, kind TEXT, query TEXT, expected TEXT, enabled INT,
            source TEXT, created_by TEXT)""")
        c.execute("""CREATE TABLE ontology_cq_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INT, source TEXT, status TEXT, row_count INT, message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("INSERT INTO ontology_questions (question,kind,query,expected,enabled,source) "
                  "VALUES ('探针','sparql','SELECT ?s WHERE { ?s ?p ?o } LIMIT 1','nonempty',1,'probe')")
        c.commit()
        out = m.run_cqs(c, record=False)
        x = out[0]
        ok = x["status"] == "skip"
        _add(ok, "S2 Fuseki 不可达 ⇒ skip（降级不是通过）",
             f"status={x['status']} msg={x['message'][:120]}")
        c.close()
    finally:
        m._sparql_endpoint = orig


def check_s3():
    from services.ontology_cq import validate_sparql
    ok_pref, err = validate_sparql("PREFIX o: <http://x/>\nSELECT ?s WHERE { ?s o:p ?o }")
    _add(ok_pref, "S3a PREFIX 前导放行", err)
    ok_base, err = validate_sparql("BASE <http://x/>\nSELECT ?s WHERE { ?s ?p ?o }")
    _add(ok_base, "S3b BASE 前导放行", err)
    ok_ins, err = validate_sparql("PREFIX o: <http://x/>\nINSERT DATA { <a> o:b <c> }")
    _add(not ok_ins, "S3c PREFIX 掩护下的写动词仍拒", err or "未被拦截")
    ok_empty, err = validate_sparql("PREFIX o: <http://x/>")
    _add(not ok_empty, "S3d 只有声明没有主体 ⇒ 拒", err or "未被拦截")


def check_s4():
    import sqlite3
    from core.graph_write_gate import post_check_edge
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE relations (id INTEGER, source_id TEXT, target_id TEXT, "
              "relation_type TEXT, branch TEXT)")
    c.execute("INSERT INTO relations VALUES (1,'A','B','R','dev')")
    good = post_check_edge(c, 1, "dev", "A", "B", "R", op="edge_create")
    noisy = [(x["check"], x["detail"]) for x in good["checks"] if x["ok"] and x["detail"]]
    _add(not noisy, "S4 通过项 detail 为空串", f"成功却带文案: {noisy}")


def _branch_body(src: str, header: str) -> str:
    """按**缩进**取出 `header` 开始的那个分支体。

    ⚠️ 首版我用 `src.split('elif kind == "sparql"')[1].split("elif")[0]`，
    结果把分支**之后**的 `out.append(item)` 与 `conn.execute("INSERT INTO ontology_cq_logs…")`
    一起算进了分支体 ⇒ S5 误报"sparql 分支里出现了 conn.execute"。
    那是日志落库，不是回落执行。判据没错，是**切片方式错了**。
    """
    lines = src.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith(header):
            start = i
            break
    if start is None:
        return ""
    base = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for ln in lines[start + 1:]:
        if not ln.strip():
            body.append(ln)
            continue
        ind = len(ln) - len(ln.lstrip())
        if ind <= base:
            break
        body.append(ln)
    return "\n".join(body)


def check_s5():
    """kind 分派：sparql 不许静默回落 sql 档。"""
    import inspect
    from services import ontology_cq as m
    src = inspect.getsource(m.run_cqs)
    body = _branch_body(src, 'elif kind == "sparql"')
    if not body:
        _add(False, "S5 定位 sparql 分支", "源码里找不到该分支（判据失效，比判红更危险）")
        return
    has_fallback = "conn.execute" in body
    _add(not has_fallback, "S5 sparql 分支不得回落到 SQLite 执行",
         "sparql 分支里出现了 conn.execute（= 失败时悄悄换数据源）：\n      "
         + "\n      ".join(l.strip() for l in body.splitlines() if "conn.execute" in l)[:240])


def check_s6():
    """API 两层（add / update）都必须**按 kind 选白名单**。

    真机发现的两处：
      · `POST /cqs` 硬拦 `kind != "sql"` ⇒ sparql 档根本登记不进来；
      · `PUT /cqs/{qid}` 无条件用 `validate_query`（SQL 白名单）⇒
        ① sparql 行一改就被拒（PREFIX 开头不匹配 ^select|^with）；
        ② sql 与 sparql 的禁用词不同（LOAD/CLEAR/MOVE 只在 SPARQL 侧），
           校验面与执行面对不上。
    """
    src_path = os.path.join(ROOT, "routers", "knowledge_parts", "ontology_version.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()

    add_body = _branch_body(src, "def ontology_cq_add(")
    upd_body = _branch_body(src, "def ontology_cq_update(")
    # _branch_body 按 `elif` 设计，函数用 def 作 header 时同样适用（缩进边界一致）
    if not add_body or not upd_body:
        _add(False, "S6 定位两个 CQ 端点函数",
             f"add={bool(add_body)} update={bool(upd_body)}")
        return
    checks = [
        ("S6a add 端点按 kind 选白名单",
         "validate_sparql" in add_body and "validate_query" in add_body,
         "只调了一种（sparql 档会被 SQL 白名单拒，或反之）"),
        ("S6b add 端点不再硬拦 kind=sql",
         'kind != "sql"' not in add_body,
         "仍存在 `kind != \"sql\"` 硬拦 ⇒ sparql CQ 登记不进来"),
        ("S6c update 端点按 kind 选白名单",
         "validate_sparql" in upd_body and "validate_query" in upd_body,
         "无条件用 SQL 白名单 ⇒ sparql 行改不动、且校验面与实际执行面对不上"),
    ]
    for name, ok, why in checks:
        _add(ok, name, "" if ok else why)


def check_s7():
    """ASK/SELECT 判定必须**剥掉 PREFIX/BASE 前导**——执行侧独立的一份判据。

    真机 bug（2026-11-21，CQ id=47）：`_run_sparql_cq` 用
    `query.split(None,1)[0]` 取首 token，对 `PREFIX o: <…>\\nASK {…}` 取到 `prefix`
    ⇒ ASK 结果被当 SELECT 处理（`results.bindings` 为空）⇒ 判"返回 0 行" ⇒ **恒 fail**。
    这与校验侧漏放 PREFIX 是**同一族错误**：两处各写一份"取首个动词"。

    ⚠️ 本条**不连真 Fuseki**（pr 层不许有外部依赖）：用假 client 注入，
    让它对任何查询都返回 `{boolean: True}`（即"ASK 为真"）。于是：
      · 若判据正确识别为 ASK ⇒ expected=nonempty 应 pass；
      · 若误判为 SELECT ⇒ 读 `results.bindings`（不存在）⇒ 0 行 ⇒ fail。
    判据只测"分派对不对"，不测网络。
    """
    import sqlite3
    from services import ontology_cq as m

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def ping(self, timeout=4.0):
            return True

        def query(self, sparql, timeout=20.0):
            return {"boolean": True}       # 只有 ASK 才有这个键

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE ontology_questions (id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT, kind TEXT, query TEXT, expected TEXT, enabled INT,
        source TEXT, created_by TEXT)""")
    c.execute("""CREATE TABLE ontology_cq_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INT, source TEXT, status TEXT, row_count INT, message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    import services.sparql_sync as ss
    orig_cls = ss.FusekiClient
    try:
        ss.FusekiClient = _FakeClient
        cases = [
            ("ASK 无前缀", "ASK { ?s ?p ?o }"),
            ("ASK 带 PREFIX", "PREFIX o: <http://mbse/ontology/>\nASK { ?s o:p ?o }"),
            ("ASK 带 BASE", "BASE <http://mbse/>\nASK { ?s ?p ?o }"),
        ]
        bad = []
        for name, q in cases:
            c.execute("DELETE FROM ontology_questions")
            c.execute("INSERT INTO ontology_questions (question,kind,query,expected,enabled,source) "
                      "VALUES (?,?,?,?,1,'probe')", (name, "sparql", q, "nonempty"))
            c.commit()
            x = m.run_cqs(c, record=False)[0]
            if x["status"] != "pass":
                bad.append(f"{name} ⇒ {x['status']}({x['message'][:46]})")
        _add(not bad, f"S7 ASK 判定剥前导（{len(cases)} 组，假 client 注入）", "；".join(bad))
    finally:
        ss.FusekiClient = orig_cls
        c.close()


def mutations():
    out = []
    from services.ontology_cq import validate_sparql
    # M1：把 INSERT 谎报成合法查询 ⇒ S1 的判据必须不认
    ok, err = validate_sparql("INSERT { <a> <b> <c> }")
    out.append(("M1 INSERT 判红", not ok, f"ok={ok} err={err}"))

    # M2：真注入一段"sparql 失败回落 sql"的代码 ⇒ `_branch_body` + S5 必须判红。
    #     不加这组的话，S5 可能是恒绿（切片切空 ⇒ 永远找不到 conn.execute）。
    fake_src = (
        "    for r in rows:\n"
        "        if kind == \"sql\":\n"
        "            conn.execute(r[\"query\"])\n"
        "        elif kind == \"sparql\":\n"
        "            passed = _run_sparql_cq(r[\"query\"], r[\"expected\"])\n"
        "            if passed[\"status\"] == \"skip\":\n"
        "                conn.execute(r[\"query\"])   # 回落\n"
        "        else:\n"
        "            pass\n"
        "    out.append(item)\n"
        "    conn.execute(\"INSERT INTO ontology_cq_logs ...\")\n"
    )
    body = _branch_body(fake_src, 'elif kind == "sparql"')
    hit = "conn.execute" in body
    # 同时断言：日志那条 INSERT（在分支之外）不该被算进来
    leaked = 'ontology_cq_logs' in body
    out.append(("M2 注入回落代码 ⇒ S5 判红且不误连日志 INSERT",
                hit and not leaked, f"hit={hit} leaked={leaked}"))

    # M3：把"update 端点无条件用 SQL 白名单"的老写法拼成假源码 ⇒ S6c 判据必须判红。
    #     没有这组，S6 可能是恒绿（`validate_sparql` 一旦出现在文件任意处就过）。
    fake_upd = (
        'def ontology_cq_update(qid: int, body: dict = None):\n'
        '    row = conn.execute("SELECT * FROM ontology_questions WHERE id=?", (qid,)).fetchone()\n'
        '    ok, err = _ocq.validate_query(query)\n'
        '    return {"ok": True}\n'
    )
    b = _branch_body(fake_upd, "def ontology_cq_update(")
    caught = b and ("validate_sparql" not in b)
    # 反面：sparql 出现但只是另一处 —— 说明判据是"函数体内同时出现两者"，不是全文计数
    fake_upd2 = fake_upd.replace('def ontology_cq_update',
                                 'def _helper():\n    ok, err = _ocq.validate_sparql(q)\n\ndef ontology_cq_update')
    b2 = _branch_body(fake_upd2, "def ontology_cq_update(")
    still_caught = b2 and ("validate_sparql" not in b2)
    out.append(("M3 老写法拼回 ⇒ S6c 判红（且不被别处的 validate_sparql 蒙过）",
                bool(caught) and bool(still_caught),
                f"caught={bool(caught)} still={bool(still_caught)}"))

    # M4：证明 S7 有区分力 —— 同一个假 client，用"不剥前导"的等价逻辑算 head，
    #     必须得到非 ask 的错误结论。若两种算法结果一致，说明 S7 测不出这个 bug。
    q = "PREFIX o: <http://mbse/ontology/>\nASK { ?s o:p ?o }"
    head_new = ""
    _l = [ln for ln in q.splitlines()]
    while _l and re.match(r"^\s*(prefix|base)\b", _l[0], re.I):
        _l.pop(0)
    head_new = (_l[0].strip().split(None, 1)[0].lower() if _l and _l[0].strip() else "")
    head_old = q.split(None, 1)[0].lower()
    out.append(("M4 不剥前导的老算法给出不同结论（S7 有区分力）",
                head_new == "ask" and head_old != "ask",
                f"new={head_new!r} old={head_old!r}"))
    return out


def main():
    print("=" * 76)
    print("门禁：CQ SPARQL 档（P1-2）")
    print("=" * 76)
    check_s1()
    check_s2()
    check_s3()
    check_s4()
    check_s5()
    check_s6()
    check_s7()
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("\n--- 变异自证 ---")
    mut = mutations()
    for name, ok, detail in mut:
        print("  [%s] %s%s" % (PASS if ok else FAIL, name, ("  <- " + detail) if detail else ""))
    n_mut_fail = sum(1 for x in mut if not x[1])
    print("\n" + "=" * 76)
    if n_fail or n_mut_fail or not mut:
        print("结论：未通过（常态 %d 条失败 / 变异 %d 组未判红）" % (n_fail, n_mut_fail))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组按预期判红）" % (len(_results), len(mut)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
