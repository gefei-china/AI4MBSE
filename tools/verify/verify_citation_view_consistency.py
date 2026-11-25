# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：引用视图一致性门禁：守「引用条数 == 正文可点数 == 列表条数」且编号连续。既有三条引用门禁守注入侧/锚点/注册表，都不查「模型只引用其中一部分」这种输出侧情况（用户实测：显示 5 条、页面只见 1,2,3,5、缺第 4 条）。node 跑 05-markdown.js 真函数：V1 真机连续无断号、V1b 列表==视图、V1c 双数标题、V2 全引用不重排、V3 无标记退回全量、V4 越界留纯文本、V5 表格内引用也可点；S1~S
"""引用视图一致性常驻门禁 —— 守「引用条数 == 正文可点数 == 列表条数」且编号连续。

为什么必须单独一条（既有三条引用门禁都守不住这一层）：
  · `verify_citation_alignment.py` 守**注入侧**（prompt 对照表 1..N 连续）；
  · `verify_citation_clause_link.py` 守 §x.y 锚点链接；
  · `verify_citation_registry.py` 守注册表。
  三者都不检查**模型只引用了其中一部分**这种输出侧情况 —— 正是用户报的现象：
  「显示引用了 5 条，页面只找到 4 条，1,2,3,5，第 4 条没看到」。

判据（node 跑 05-markdown.js 的**真函数体**，不复刻）：
  V1 真机样本：视图后 k < n，正文编号连续 1..k，无断号
  V2 全部被引用：k == n，不重排（历史渲染不变）
  V3 正文无任何标记：退回全量（k == n），不把检索面藏起来
  V4 越界编号（[9]）：原样留纯文本，不生成链接
  V5 ★ 表格单元格内的 [n] 与 §x.y 也被替换（本轮第二半根因）
  S1 静态：`_msgCites` 必须存**视图后**的 list（否则 openCitation 按新编号索引会错位）
  M1~M3 变异自证（判据有牙齿）

零真实 LLM / 零服务 / 零写库；无 node 或无真库时相应组 SKIP 并写明。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_citation_view_consistency.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
MD = os.path.join(ROOT, "static", "js", "mods", "05-markdown.js")
DB = os.environ.get("MBSE_GATE_DB") or os.path.join(ROOT, "mbse.db")

PASS, FAIL, SKIP = [], [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("　— " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def grab_fn(src, name):
    i = src.find("function " + name)
    if i < 0:
        return None
    j = src.find("{", i)
    d = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            d += 1
        elif src[k] == "}":
            d -= 1
            if d == 0:
                return src[i:k + 1]
    return None


def find_node():
    return (os.environ.get("MBSE_NODE") or shutil.which("node")
            or shutil.which("node.exe") or "")


def run_node(node, js, timeout=60):
    td = tempfile.mkdtemp()
    p = os.path.join(td, "p.js")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run([node, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    finally:
        shutil.rmtree(td, ignore_errors=True)


def real_sample():
    """取一条带 citations 的真机消息（用于 V1）。取不到返回 None。"""
    if not os.path.exists(DB):
        return None
    try:
        con = sqlite3.connect(DB, timeout=15)
        try:
            r = con.execute(
                "SELECT id, content, card_data FROM messages "
                "WHERE card_data LIKE '%\"citations\"%' "
                "AND card_data NOT LIKE '%\"citations\": []%' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not r:
        return None
    try:
        cd = json.loads(r[2] or "{}")
    except Exception:
        return None
    cits = cd.get("citations") or []
    if not cits:
        return None
    return {"id": r[0], "body": r[1] or "", "cites": cits}


NEED = ("escMd", "_mdCells", "_citeSub", "renderTable", "renderMdLine",
        "_citeClauseIndex", "_citeView", "inlineMd", "renderMarkdown")


def main():
    print("=" * 78)
    print("引用视图一致性门禁（条数一致 + 编号连续 + 表格内也可点）")
    print("=" * 78)

    src = open(MD, encoding="utf-8").read()

    # ── S 静态：接线不可丢 ─────────────────────────────────────────────
    print("\n=== [S] 静态接线 ===")
    chk("S1 _msgCites 必须存**视图后**的 list（否则 openCitation 按新编号索引会错位）",
        ("_msgCites = window._msgCites || {})[m.id] = _cv ? _cv.list : cites"
         in src) or ("[m.id] = _cv ? _cv.list : cites" in src),
        "renderMessage 里应写 _cv.list 而非原始 cites")
    chk("S2 引用替换是单一真源 _citeSub（inlineMd 与 renderTable 共用，不写两套）",
        "function _citeSub(o, cites, renum){" in src
        and "o = _citeSub(o, cites, renum);" in src
        and "? _citeSub(escMd(c), cites, renum) : escMd(c)" in src)
    chk("S3 renderMarkdown 把 cites/_renum 传给表格（否则表格内引用点不动）",
        "renderTable(rows, cites, _renum)" in src)
    chk("S4 [n] 编号解析只认 renum 一个口径（不得再用视图后的长度判范围）",
        "const _pickCite = n =>" in src and "renum[n] || 0" in src)

    node = find_node()
    if not node:
        for n in ("V1", "V2", "V3", "V4", "V5", "M1", "M2", "M3"):
            skip(n, "环境无 node")
        return report()

    parts = [grab_fn(src, n) for n in NEED]
    if any(p is None for p in parts):
        chk("V0 从 05-markdown.js 抽到全部依赖", False,
            "缺: %s" % [n for n, p in zip(NEED, parts) if p is None])
        return report()
    chk("V0 从 05-markdown.js 抽到全部依赖", True, "")
    fns = "\n".join(parts)

    helper = """
function measure(body, cites){
  const v = _citeView(cites, body);
  const html = renderMarkdown(String(body), cites);
  const nums = [...new Set([...html.matchAll(/data-i="(\\d+)"/g)].map(m=>+m[1]))].sort((a,b)=>a-b);
  const secs = (html.match(/cite-sec/g)||[]).length;
  const sups = (html.match(/<sup class="cite/g)||[]).length;
  const maxN = Math.max(0, ...nums);
  const broken = Array.from({length:maxN},(_,i)=>i+1).filter(n=>!nums.includes(n));
  return {n: v.n, k: v.k, trimmed: v.trimmed, nums, broken, secs, sups,
          contiguous: (nums.length===0) || (broken.length===0 && nums[nums.length-1]===v.k),
          listLen: v.list.length};
}
function stubDom(){}
function mermaidAutoRender(){} function codeFoldCopy(){} function codeFoldToggle(){} function mermaidToggle(){}
"""

    fx = real_sample()
    stub_cites = [
        {"anchor": "§5.2.5 Characteristics of", "content": "a", "source_doc": "x.pdf"},
        {"anchor": "§5.2.6 Verifiable", "content": "b", "source_doc": "x.pdf"},
        {"anchor": "片段#7", "content": "c", "source_doc": "x.pdf"},
        {"anchor": "§4.4.1 R18", "content": "d", "source_doc": "y.pdf"},
        {"anchor": "片段#9", "content": "e", "source_doc": "z.pdf"},
    ]

    print("\n=== [V] 行为（node 跑真函数） ===")

    # V1 真机样本
    if fx:
        body = fx["body"]
        has_mark = any(("[%d]" % i) in body for i in range(1, len(fx["cites"]) + 1))
        js = fns + helper + """
const BODY = %s, CITES = %s;
console.log(JSON.stringify(measure(BODY, CITES)));
""" % (json.dumps(body, ensure_ascii=False), json.dumps(fx["cites"], ensure_ascii=False))
        rc, out, err = run_node(node, js)
        if rc != 0 or not out.strip():
            chk("V1 真机样本渲染成功", False, (err or out)[-200:])
        else:
            d = json.loads(out.strip().splitlines()[-1])
            chk("V1 真机样本：编号连续无断号（k=%d / n=%d）" % (d["k"], d["n"]),
                d["contiguous"],
                "编号=%s 断号=%s" % (d["nums"], d["broken"]))
            chk("V1b 列表条数 == 视图条数（%d == %d）" % (d["listLen"], d["k"]),
                d["listLen"] == d["k"])
            if has_mark and d["trimmed"]:
                # 源码里是 ES 模板字面量 ${_cv.k} / ${_cv.n}，不是字面数字
                chk("V1c 有裁剪时标题给出双数（引用 k / 检索 n）",
                    "引用 ${_cv.k} 条 / 本次检索 ${_cv.n} 条" in src,
                    "折叠区标题应含模板 ${_cv.k}/${_cv.n}")
    else:
        skip("V1 真机样本", "无带 citations 的真机消息")

    # V2 全部被引用 → 不重排
    js = fns + helper + """
const C = %s;
const B = C.map((_,i)=>'引'+(i+1)+'['+(i+1)+']').join('；');
console.log(JSON.stringify(measure(B, C)));
""" % json.dumps(stub_cites, ensure_ascii=False)
    rc, out, err = run_node(node, js)
    ok2 = False
    if rc == 0 and out.strip():
        d2 = json.loads(out.strip().splitlines()[-1])
        ok2 = (d2["k"] == d2["n"] == d2["listLen"] and not d2["trimmed"]
               and d2["nums"] == list(range(1, d2["n"] + 1)))
    chk("V2 全部被引用 ⇒ k==n、不重排、编号 1..n", ok2, (err or out)[-160:])

    # V3 正文无任何标记 → 退回全量
    js = fns + helper + """
const C = %s;
console.log(JSON.stringify(measure('这段回答没有任何编号也没有条款号。', C)));
""" % json.dumps(stub_cites, ensure_ascii=False)
    rc, out, err = run_node(node, js)
    ok3 = False
    if rc == 0 and out.strip():
        d3 = json.loads(out.strip().splitlines()[-1])
        ok3 = (d3["k"] == d3["n"] == d3["listLen"] and not d3["trimmed"])
    chk("V3 正文无标记 ⇒ 退回全量（不把检索面藏起来）", ok3, (err or out)[-160:])

    # V4 越界编号
    js = fns + helper + """
const C = %s;
const r = measure('正常 [1] 与越界 [99]。', C);
console.log(JSON.stringify({...r, raw: (inlineMd('越界 [99]', C)||'')}));
""" % json.dumps(stub_cites, ensure_ascii=False)
    rc, out, err = run_node(node, js)
    ok4 = False
    if rc == 0 and out.strip():
        d4 = json.loads(out.strip().splitlines()[-1])
        ok4 = (d4["nums"] == [1] and "[99]" in d4["raw"] and "data-i" not in d4["raw"])
    chk("V4 越界编号 ⇒ 原样纯文本，不造空入口", ok4, (err or out)[-160:])

    # V5 表格单元格内的引用
    tbl = "| 序 | 依据 |\n|---|---|\n| 1 | 见 §5.2.5 [1] |\n| 2 | 见 [2] |"
    js = fns + helper + """
const C = %s;
const r = measure(%s, C);
console.log(JSON.stringify(r));
""" % (json.dumps(stub_cites, ensure_ascii=False), json.dumps(tbl, ensure_ascii=False))
    rc, out, err = run_node(node, js)
    ok5 = False
    if rc == 0 and out.strip():
        d5 = json.loads(out.strip().splitlines()[-1])
        ok5 = d5["secs"] >= 1 and d5["sups"] >= 2
    chk("V5 ★ 表格单元格内的 §x.y 与 [n] 也被替换（本轮第二半根因）", ok5, (err or out)[-200:])

    # ── M 变异自证 ────────────────────────────────────────────────────
    print("\n=== [M] 变异自证 ===")
    # M1：把 renum 丢掉（退化成"按视图长度判范围"）⇒ V1 必须出现断号
    bad = fns.replace("const _pickCite = n => (renum ? (renum[n] || 0) : ((n >= 1 && n <= cites.length) ? n : 0));",
                      "const _pickCite = n => ((n >= 1 && n <= cites.length) ? n : 0);")
    if bad != fns:
        js = bad + helper + """
const C = %s;
// ⚠️ body 必须**只引用一部分**（缺 [4]）才会触发裁剪；
//    第一版写成 B+' 只引 [1][2][3][5]'，而 B 已含全部 [1..5] ⇒ k==n 不裁剪，
//    变异体也不会断号 ⇒ 判据空转（自测抓出）。
// ⚠️ 夹具文本里**不能出现** [4] 的字样（哪怕是在说明跳过它）——
//    那是真的引用标记，会把 k 拉回 5（自测第二次踩到）。故只写要引用的。
const B = '只引 [1][2][3][5] 这几条';
console.log(JSON.stringify(measure(B, C)));
""" % json.dumps(stub_cites, ensure_ascii=False)
        rc, out, err = run_node(node, js)
        mut_bad = False
        if rc == 0 and out.strip():
            dm = json.loads(out.strip().splitlines()[-1])
            mut_bad = bool(dm["broken"]) or dm["nums"] != list(range(1, dm["k"] + 1))
        chk("M1 变异：去掉 renum 重映射 ⇒ 编号必然断号（判据有牙齿）", mut_bad,
            (err or out)[-200:])
    else:
        chk("M1 变异：去掉 renum 重映射 ⇒ 编号必然断号", False, "变异锚点未命中")

    # M2：表格不调 _citeSub ⇒ V5 必须翻红
    # ⚠️ 不能只删 `? _citeSub(...) :`（会留下 `(cond) : escMd(c)` 语法错）；
    #    整句替换才是合法变异体。
    bad2 = fns.replace(
        "const cell = c => (cites && cites.length) ? _citeSub(escMd(c), cites, renum) : escMd(c);",
        "const cell = c => escMd(c);")
    if bad2 != fns:
        js = bad2 + helper + """
const C = %s;
console.log(JSON.stringify(measure(%s, C)));
""" % (json.dumps(stub_cites, ensure_ascii=False), json.dumps(tbl, ensure_ascii=False))
        rc, out, err = run_node(node, js)
        mut2 = False
        if rc == 0 and out.strip():
            dm2 = json.loads(out.strip().splitlines()[-1])
            mut2 = dm2["secs"] == 0 and dm2["sups"] == 0
        chk("M2 变异：表格退回只看 escMd ⇒ 表格内引用全丢（V5 抓得到）", mut2,
            (err or out)[-200:])
    else:
        chk("M2 变异：表格退回只看 escMd", False, "变异锚点未命中")

    # M3：_citeView 不裁（等价于"标题用 N、正文用 K"的旧口径）
    bad3 = fns.replace("  want.sort((a, b) => a - b);                        // 保持原相关度顺序",
                       "  return {list: cites.slice(), renum: {}, k: n, n: n, trimmed: false};\n"
                       "  want.sort((a, b) => a - b);")
    if bad3 != fns:
        js = bad3 + helper + """
const C = %s;
const r = measure('只引 [1]', C);
console.log(JSON.stringify(r));
""" % json.dumps(stub_cites, ensure_ascii=False)
        rc, out, err = run_node(node, js)
        mut3 = False
        if rc == 0 and out.strip():
            dm3 = json.loads(out.strip().splitlines()[-1])
            mut3 = (dm3["k"] == dm3["n"] and dm3["trimmed"] is False)
        chk("M3 变异：不裁剪（退回旧口径）⇒ k==n 且不标 trimmed（说明修复确实依赖裁剪）", mut3)
    else:
        chk("M3 变异：不裁剪", False, "变异锚点未命中")

    return report()


def report():
    print("\n" + "=" * 78)
    print("PASS %d / FAIL %d / SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("红项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
