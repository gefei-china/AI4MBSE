#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：前端管道明细展示（timing + OCR 页数）—— 第 4 项，2026-10-04

## 为什么需要它

P2（阶段耗时留痕）与 P1-3（OCR 分段续跑）都已在**后端**落地并真机验证过，
但前端**零展示** ⇒ 用户看到的仍是"解析完成 / 解析中"这类三年前的状态文案。
本门禁钉死"后端字段真的被前端消费了，且口径没写反"。

## 关键设计：判据跑**真源码**，不复刻

断言若在 Python 里手写一份"我认为前端该长什么样"的逻辑，那就是**测零件没测装配**
（MEMORY 里的教训）。本门禁的做法是：
1. 从 `static/js/mods/20-docs.js` **按花括号配平抽出** `stageTable` /
   `fmtMs` / `ocrInfoTable` 的函数体；
2. 把抽出的文本拼成 `new Function(...)` **在 Node 里就地执行**；
3. 对**真实函数**喂真数据与边界数据，断言渲染结果。

⇒ 源码改了判据自动跟着改；复刻版则永远与源码脱节。

## 为什么口径必须逐字段核对

实测真机数据（doc#819）：
    ocr = {"quality": {"lines":84,"avg_score":0.696,"low_ratio":48.8},
           "elapsed": 1.395, "pages_total":1, "pages_ocr":1}
两个易错点：
- `quality` 是**对象**，不是数字（写成 `${o.quality}` 会输出 `[object Object]`）；
- `elapsed` 单位是**秒**（按毫秒处理会显示 `0.0 s`）。
这类错误**不会报错**，只会静默显示一眼假的数字 ⇒ 只能靠断言钉死。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS_JS = os.path.join(ROOT, "static", "js", "mods", "20-docs.js")
INGEST_PY = os.path.join(ROOT, "knowledge_pipeline", "ingest.py")

PASS, FAIL = "PASS", "FAIL"
_results = []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:200])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    return bool(ok)


_MUT_ROWS = []


def _src(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── JS 函数抽取（与 verify_job_ui.py 同一套手法：花括号配平）──────────────
def extract_js_fn(src, name):
    """抽出 `function name(...) { ... }` 的完整源码（按括号配平，处理字符串）。"""
    m = re.search(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), src)
    if not m:
        return None
    i = src.index("(", m.start())
    depth = 0
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    i = src.index("{", i)
    depth = 0
    quote = ""
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == quote and src[i - 1] != "\\":
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    return src[m.start():i]


def run_js(body_js: str):
    """把 JS 代码在 Node 里执行并返回 stdout。"""
    proc = subprocess.run(
        [os.environ.get("NODE_BIN", "node"), "-e", body_js],
        capture_output=True, text=True, cwd=ROOT, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def js_eval_helpers(doc_js: str, expr: str):
    """在 Node 里执行 `expr`（可访问 stageTable/fmtMs/ocrInfoTable），返回 JS 表达式的 JSON 值。"""
    parts = [extract_js_fn(doc_js, n) or "" for n in ("fmtMs", "ocrInfoTable", "stageTable")]
    stage_names = ("const STAGE_NAMES = {parse:'解析', chunk:'切片', embed:'向量化', "
                   "insert:'入库', extraction:'抽取'};")
    code = "\n".join(parts) + "\n" + stage_names + "\nconsole.log(JSON.stringify(" + expr + "));"
    rc, out, err = run_js(code)
    if rc != 0:
        raise RuntimeError("node 失败: %s" % err[-400:])
    return json.loads(out.strip().splitlines()[-1])


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


# ── T1：函数存在且可抽（抽取器本身的有效性）──────────────────────────────
def t_extract(doc_js):
    print("\n=== T1 抽取器能拿到真函数体 ===")
    ok = True
    for n in ("fmtMs", "ocrInfoTable", "stageTable"):
        body = extract_js_fn(doc_js, n)
        ok &= _rec("T1 %s 可抽出且长度合理" % n,
                   body and len(body) > 60, "len=%s" % (len(body) if body else None))
    # 抽取器有效性：截断的函数体会在 new Function 时语法错
    try:
        js_eval_helpers(doc_js, "typeof fmtMs")
        ok &= _rec("T1b 抽出后可执行（抽取器没截断）", True)
    except Exception as e:
        ok &= _rec("T1b 抽出后可执行（抽取器没截断）", False, str(e)[:160])
    return ok


# ── T2：fmtMs 口径 ────────────────────────────────────────────────────────
def t_fmtms(doc_js):
    print("\n=== T2 耗时格式化口径 ===")
    ok = True
    cases = [(0, "0 ms"), (999, "999 ms"), (1000, "1.0 s"), (59999, "60.0 s"),
             (60000, "1.0 min"), (177100, "3.0 min")]
    for ms, want in cases:
        got = js_eval_helpers(doc_js, "fmtMs(%d)" % ms)
        ok &= _rec("T2 fmtMs(%d)=%s" % (ms, want), got == want, "got=%s" % got)
    return ok


# ── T3：stageTable 有耗时列、旧文档不显示 0ms ────────────────────────────
def t_stage_table(doc_js):
    print("\n=== T3 阶段耗时列 ===")
    ok = True
    timing = {"parse": {"status": "done", "ms": 9}, "chunk": {"status": "done", "ms": 528},
              "embed": {"status": "failed", "ms": 1},
              "insert": {"status": "done", "ms": 210}, "total_ms": 748}
    detail = json.dumps({"parse": "done", "chunk": "done", "embed": "failed",
                         "insert": "done", "timing": timing}, ensure_ascii=False)
    html = js_eval_helpers(doc_js, "stageTable(%s)" % json.dumps(detail))
    txt = strip_html(html)
    ok &= _rec("T3a 表头有「耗时」列", "耗时" in txt, txt[:120])
    ok &= _rec("T3b parse 9 ms 可见", "9 ms" in txt, txt[:160])
    ok &= _rec("T3c chunk 528 ms 可见", "528 ms" in txt, txt[:200])
    ok &= _rec("T3d 显示合计", "合计" in txt, txt[:240])

    # ⚠️ 关键反证：旧文档（无timing）绝不能显示 0 ms ——
    #   那会让用户以为该阶段不耗时，而实际是"数据缺失"。
    old = json.dumps({"parse": "done", "chunk": "done", "embed": "done", "insert": "done"})
    h2 = js_eval_helpers(doc_js, "stageTable(%s)" % json.dumps(old))
    t2 = strip_html(h2)
    ok &= _rec("T3e 旧文档显示「—」而非 0 ms", "—" in t2 and "0 ms" not in t2, t2[:160])
    ok &= _rec("T3f 旧文档不显示合计", "合计" not in t2, t2[:200])

    # ⚠️ 反证：progress/进度条类的"完成"判据仍只看 status（不得引入 ms 判据）
    ok &= _rec("T3g 判定不依赖 ms（failed 阶段照常显示失败）",
               "✗" in txt or "失败" in txt, txt[:200])
    return ok


# ── T4：OCR 三态显式化 ───────────────────────────────────────────────────
def t_ocr(doc_js):
    print("\n=== T4 OCR 页数与截断显式化 ===")
    ok = True
    base = {"enabled": True, "available": True}

    # ① 部分完成（分段续跑）
    o = dict(base, pages_total=140, pages_ocr=3, pages_pending=137, truncated=True,
             elapsed=12.4, quality={"lines": 84, "avg_score": 0.696, "low_ratio": 48.8})
    h = js_eval_helpers(doc_js, "ocrInfoTable({ocr:%s})" % json.dumps(o))
    t = strip_html(h)
    ok &= _rec("T4a 部分完成：显示待识别页数", "待识别 137 页" in t, t[:200])
    ok &= _rec("T4b 部分完成：明确告知可续跑", "断点续跑" in t, t[:240])
    ok &= _rec("T4c 部分完成：显示已识别/总页数",
               "共 140 页" in t and "已识别 3 页" in t, t[:200])

    # ② 硬截断（ocr_max_pages 上限）—— 必须显式警告，不能静默
    o2 = dict(base, pages_total=200, pages_ocr=50, pages_pending=0, truncated=True,
              elapsed=300.0, quality=None)
    h2 = js_eval_helpers(doc_js, "ocrInfoTable({ocr:%s})" % json.dumps(o2))
    t2 = strip_html(h2)
    ok &= _rec("T4d 硬截断：显示被截断", "截断" in t2, t2[:200])
    ok &= _rec("T4e 硬截断：指出只索引了前 N 页", "前 50 页" in t2, t2[:240])

    # ③ 全部完成：无警告文案
    o3 = dict(base, pages_total=30, pages_ocr=30, pages_pending=0, truncated=False,
              elapsed=62.5, quality={"lines": 40, "avg_score": 0.93, "low_ratio": 2})
    h3 = js_eval_helpers(doc_js, "ocrInfoTable({ocr:%s})" % json.dumps(o3))
    t3 = strip_html(h3)
    ok &= _rec("T4f 全完成：无截断/待识别告警",
               "截断" not in t3 and "未识别" not in t3, t3[:200])

    # ④ OCR 不可用 ⇒ 整段不显示（"没发生"不是"0 页"）
    h4 = js_eval_helpers(
        doc_js, "ocrInfoTable({ocr:{enabled:true,available:false,pages_total:0,pages_ocr:0}})")
    ok &= _rec("T4g available=false 时不显示 OCR 段", not h4.strip(), h4[:120])
    # ⑤ 无 ocr 段 ⇒ 不显示
    h5 = js_eval_helpers(doc_js, "ocrInfoTable({})")
    ok &= _rec("T5h 无 ocr 段时不渲染", not h5.strip(), h5[:120])
    return ok


# ── T5：quality 是对象 / elapsed 是秒（易错口径）──────────────────────────
def t_units(doc_js):
    print("\n=== T5 字段口径（quality 是对象 / elapsed 是秒）===")
    ok = True
    o = {"enabled": True, "available": True, "pages_total": 1, "pages_ocr": 1,
         "elapsed": 1.395, "quality": {"lines": 84, "avg_score": 0.696, "low_ratio": 48.8}}
    h = js_eval_helpers(doc_js, "ocrInfoTable({ocr:%s})" % json.dumps(o))
    t = strip_html(h)
    ok &= _rec("T5a quality 渲染为数值而非 [object Object]",
               "[object Object]" not in h and "0.70" in t, t[:200])
    ok &= _rec("T5b elapsed 按秒显示（1.395 ⇒ 1.4s，不是 0.0 s）",
               "1.4s" in t and "0.0 s" not in t, t[:200])
    ok &= _rec("T5c 无 NaN / undefined 泄漏",
               "NaN" not in h and "undefined" not in h, t[:220])
    return ok


# ── T6：后端确实产出这两个字段（跨语言契约）───────────────────────────────
def t_backend_contract():
    print("\n=== T6 后端产出 timing / ocr 字段（跨语言契约）===")
    ok = True
    ing = _src(INGEST_PY)
    ok &= _rec("T6a ingest 写 pipeline_detail.timing", '["timing"]' in ing)
    ok &= _rec("T6b 返回体带 timing（异步 worker 侧也能拿到）",
               '"timing"' in ing)
    # OCR 字段名以 extract.py 为准
    ex = _src(os.path.join(ROOT, "knowledge_pipeline", "extract.py"))
    for f in ("pages_total", "pages_ocr", "pages_pending", "truncated"):
        ok &= _rec("T6c 后端产出 ocr.%s" % f, ('"%s"' % f) in ex)
    return ok


# ── 变异自证 ──────────────────────────────────────────────────────────────
def mutations(doc_js):
    print("\n--- 变异 M1：删耗时列 ⇒ T3 判红 ---")
    _IN_MUT[0] = True
    try:
        mut = doc_js.replace("<th style=\"text-align:right;\">耗时</th>", "<th>xx</th>", 1)
        ok = mut != doc_js and (not t_stage_table(mut))
    except Exception as e:
        _rec("M1 锚点命中", False, str(e)[:120])
        ok = False
    _rec("M1 删耗时列 ⇒ T3 判红", ok)
    _IN_MUT[0] = False

    print("\n--- 变异 M2：删「断点续跑」告警 ⇒ T4 判红 ---")
    _IN_MUT[0] = True
    try:
        mut = doc_js.replace("从断点续跑", "已处理", 1)
        ok = mut != doc_js and (not t_ocr(mut))
    except Exception as e:
        _rec("M2 锚点命中", False, str(e)[:120])
        ok = False
    _rec("M2 删续跑告警 ⇒ T4 判红", ok)
    _IN_MUT[0] = False

    print("\n--- 变异 M3：truncated 判红改成不告警 ⇒ T4 判红 ---")
    _IN_MUT[0] = True
    try:
        mut = doc_js.replace("} else if(o.truncated) {", "} else if(false) {", 1)
        ok = mut != doc_js and (not t_ocr(mut))
    except Exception as e:
        _rec("M3 锚点命中", False, str(e)[:120])
        ok = False
    _rec("M3 截断不告警 ⇒ T4 判红", ok)
    _IN_MUT[0] = False

    print("\n--- 变异 M4：quality 当数字用（口径写反）⇒ T5 判红 ---")
    _IN_MUT[0] = True
    try:
        mut = doc_js.replace("typeof o.quality === 'object'", "typeof o.quality === 'number'", 1)
        ok = mut != doc_js and (not t_units(mut))
    except Exception as e:
        _rec("M4 锚点命中", False, str(e)[:120])
        ok = False
    _rec("M4 quality 口径写反 ⇒ T5 判红", ok)
    _IN_MUT[0] = False

    print("\n--- 变异 M5：旧文档改成显示 0 ms ⇒ T3 判红 ---")
    _IN_MUT[0] = True
    try:
        mut = doc_js.replace("? fmtMs(t.ms) : '<span style=\"color:var(--mut);\">—</span>'",
                             "? fmtMs(t.ms) : '0 ms'", 1)
        ok = mut != doc_js and (not t_stage_table(mut))
    except Exception as e:
        _rec("M5 锚点命中", False, str(e)[:120])
        ok = False
    _rec("M5 旧文档显示 0 ms ⇒ T3 判红", ok)
    _IN_MUT[0] = False

    print("\n--- 变异 M6：available=false 也渲染 ⇒ T4 判红 ---")
    _IN_MUT[0] = True
    try:
        mut = doc_js.replace("if(!o || o.available === false) return '';",
                             "if(!o) return '';", 1)
        ok = mut != doc_js and (not t_ocr(mut))
    except Exception as e:
        _rec("M6 锚点命中", False, str(e)[:120])
        ok = False
    _rec("M6 available=false 也渲染 ⇒ T4 判红", ok)
    _IN_MUT[0] = False


def main():
    if not os.path.exists(DOCS_JS):
        print("找不到 %s" % DOCS_JS)
        return 1
    doc_js = _src(DOCS_JS)
    ok = True
    ok &= t_extract(doc_js)
    if not _results:
        print("抽取器失效，后续断言无意义")
        return 1
    ok &= t_fmtms(doc_js)
    ok &= t_stage_table(doc_js)
    ok &= t_ocr(doc_js)
    ok &= t_units(doc_js)
    ok &= t_backend_contract()

    print("\n" + "=" * 68)
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("常态断言：%d 条，%d 通过 / %d 失败" % (len(_results), len(_results) - n_fail, n_fail))
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    if n_fail:
        print("结论：门禁未通过")
        return 1

    # ── 变异自证 ────────────────────────────────────────────────────────
    # ⚠️ 记账纪律（本次又踩到同型）：变异期**子测试的 _rec 也会进 _MUT_ROWS**，
    #   若直接按 `_MUT_ROWS` 里 FAIL/PASS 计数，就会把"子断言全部照过"
    #   （=变异没生效）算成"没判红"或反之，结论随机。
    #   正解：每组变异只认它**自己那一条** verdict（M1..M6），
    #   子断言进 _MUT_ROWS 仅供人工核对，不参与判定。
    print("\n--- 变异自证 ---")
    _MUT_ROWS.clear()
    mutations(doc_js)
    verdicts = [r for r in _MUT_ROWS if re.match(r"^M\d+\b", r[1])]
    # verdict 的 PASS 语义 = "该变异**成功复现**（被测判据确实转红）"
    n_red = sum(1 for r in verdicts if r[0] == PASS)
    # 反向：判红记账用专门的 channel
    print("变异组：%d，判红成功：%d" % (len(verdicts), n_red))
    # 变异期子断言（人工核对用）
    subs = [r for r in _MUT_ROWS if not re.match(r"^M\d+\b", r[1])]
    n_sub_red = sum(1 for r in subs if r[0] == FAIL)
    print("（变异期子断言 %d 条，其中 %d 条转红 —— 红灯应来自被测判据）"
          % (len(subs), n_sub_red))
    for st, name, detail in subs:
        if st == FAIL:
            print("  [%s] %s" % (st, name))
    print("-" * 68)
    for st, name, detail in verdicts:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    print("=" * 68)
    if n_red != len(verdicts) or not verdicts:
        print("结论：门禁未通过（%d/%d 变异未判红）" % (len(verdicts) - n_red, len(verdicts)))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组全部按预期判红）"
          % (len(_results), len(verdicts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
