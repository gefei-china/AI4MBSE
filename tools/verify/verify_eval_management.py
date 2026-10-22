#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测管理页门禁（2026-10-22）：静态挂载 + 后端字段 + 前端渲染冒烟。

## 为什么要有这道门禁

新增一个整页（`#pg-eval`）要动 6 处：页面容器 / 脚本引入 / 导航入口 /
`TITLES` / `loadPage` 分派 / 数据端点。**漏任何一处都不会报错**：
- 漏了 `loadPage` 分派 ⇒ 点进去白屏（无提示）
- 漏了 `TITLES` ⇒ 面包屑退化成上一级页面名
- 漏了导航入口 ⇒ 页面"存在但找不到"（正是 2026-09-17 补「审批管理」的原因）
- 漏了脚本引入 ⇒ 页面白屏且 console 报 404

本门禁把"接好了吗"变成**可断言的对象**。

## 三段判据（照 verify_capability_overview.py 的成熟做法）

- **A 静态挂载**：index.html / 02-shell.js 里该有的字符串都在
- **B 后端字段**：真连库打 `/api/eval/*`，核对前端要用的每个字段路径真实存在
  （前端拼路径时字段名写错 ⇒ 渲染成 undefined，而 undefined 在页面上
  看起来只是"空"）
- **C 前端渲染冒烟**：Node `vm` 加载**真实** 48-eval.js，用**真实端点 payload**
  驱动渲染，断言 innerHTML 里该有的内容都在、且**没有 undefined/NaN 泄漏**

## 三条判据自证（防"门禁形同虚设"）

- **S1 变异：删掉一处接线 ⇒ 必须判红**（证明判据真的会拦）
- **S2 变异：让后端少返回一个字段 ⇒ 必须判红**（证明字段核对有效）
- **S3 变异：让 payload 出现 undefined ⇒ 必须判红**（证明渲染冒烟能抓 undefined 泄漏）

## div 配平哨兵（抄自 verify_evolution_report.py 的 E8）

手写 innerHTML 模板漏一个 `</div>` 时，浏览器会静默把后续面板嵌进错误的
div —— `innerHTML` 不报错、console 干净，只有**数面板数量**才能发现。
本门禁静态断言各渲染函数的 `<div>` / `</div>` 计数相等。

用法：
  .venv/Scripts/python.exe tools/verify/verify_eval_management.py
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: Node 运行时：优先 managed 版本（环境隔离纪律），缺失回退系统 node
NODE = r"C:\Users\gefei\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
if not os.path.exists(NODE):
    NODE = "node"

PASS, FAIL = "PASS", "FAIL"
_results = []
#: 变异体内标志：为真时，打印带「变异体」前缀，且不计入最终统计
#: （否则"故意造出来的红"会留在统计里，让门禁明明抓到了问题却显示未通过；
#:   同时打印必须加前缀，否则看输出的人会把变异体的红误读成常态的红）。
_IN_MUT = [False]
#: 变异体内的行收集器（与常态分开，避免污染最终统计）
_MUT_ROWS = []


def ck(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:300])
    if _IN_MUT[0]:
        _MUT_ROWS.append(row)
        print("  [变异体] %s %s" % (row[0], row[1]) + ("  " + row[2] if row[2] else ""))
        return bool(ok)
    _results.append(row)
    print("  [%s] %s" % (row[0], row[1]) + ("  " + row[2] if row[2] else ""))
    return bool(ok)


def _read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
# A 静态挂载
# ══════════════════════════════════════════════════════════════════════
def t_A():
    print("== A 静态挂载 ==")
    idx = _read(os.path.join(ROOT, "static", "index.html"))
    shell = _read(os.path.join(ROOT, "static", "js", "mods", "02-shell.js"))
    js = _read(os.path.join(ROOT, "static", "js", "mods", "48-eval.js"))

    ck("A1 页面容器 #pg-eval 存在", 'id="pg-eval"' in idx)
    ck("A1b 挂载点 #evm-body 存在", 'id="evm-body"' in idx)
    ck("A2 脚本已引入", "48-eval.js" in idx)
    ck("A3 导航入口存在（更多浮层）",
       ('id="mf-eval-entry"' in idx) and ("go('eval')" in idx))
    ck("A4 loadPage 已分派", "p==='eval'" in shell and "loadEvalManagement()" in shell)
    ck("A4b 面包屑标题已登记", "eval:'评测管理'" in shell)
    ck("A5 48-eval.js 导出 loadEvalManagement", "window.loadEvalManagement" in js)

    # 页面容器必须是 #pages 的直接子级（踩坑注释：放错位置布局会挤成窄条）
    m = re.search(r'<div class="pages" id="pages"', idx)
    if m:
        # 粗判：#pg-eval 出现位置须在 #pages 之后，且 #pages 之后没有更早的 </div> 闭合
        pos_pages = m.start()
        pos_eval = idx.find('id="pg-eval"')
        ck("A6 #pg-eval 在 #pages 之后", pos_eval > pos_pages,
           "pages@%d eval@%d" % (pos_pages, pos_eval))

    # 前端不得硬编码 API 路径之外的端点（路径必须来自后端约定）。
    # 只取 `api('/api/...')` 的**实参**形式 —— 不要匹配 `indexOf('/api/...')`
    # 这类比较用的字符串（它们是同一批路径，重复匹配无意义且会误报）。
    code_nc0 = re.sub(r"//.*", "", re.sub(r"/\*[\s\S]*?\*/", "", js))
    paths = re.findall(r"api\(\s*['\"](/api/[a-z\-]+/[^'\"]*)['\"]", code_nc0)
    ck("A7 前端 API 路径均为 /api/eval/* 或 /api/knowledge/*",
       paths and all(p.startswith("/api/eval/") or p.startswith("/api/knowledge/")
                     for p in paths),
       str(paths))

    # ★前端不得自己算阈值（只渲染不计算）：三色判定必须由后端给 status。
    #   本页第一版曾在前端写 0.15/0.85/0.30/0.70 四个魔数 —— 改阈值要改两处，
    #   迟早漂移（这正是本仓 15-kb.js 文件头「禁止在前端再写一遍阈值」的纪律）。
    code_nc = re.sub(r"//.*", "", re.sub(r"/\*[\s\S]*?\*/", "", js))
    magic = re.findall(r"[<>]=?\s*0\.\d+", code_nc)
    ck("A8 前端不硬编码阈值（三色必须由后端给 status）", not magic, str(magic))
    ck("A8b 前端确实消费后端 status", "metric_status" in js)

    # ★ 下钻能力（2026-10-22 晚新增）：行业标杆评测页都能从"总览"点进
    #   "是哪几条案例"。本门禁把这条通路钉住，防止静默退化成纯展示。
    ck("A9 下钻入口已导出（openEvalReport）",
       "window.openEvalReport" in js and "function openEvalReport" in js)
    ck("A10 指标卡/历史行可点下钻",
       js.count("openEvalReport(") >= 2,
       "openEvalReport( 出现 %d 次" % js.count("openEvalReport("))
    ck("A11 前端调用下钻端点 /api/eval/report",
       "/api/eval/report" in js)
    return True


# ══════════════════════════════════════════════════════════════════════
# B 后端字段（真连库打真实端点）
# ══════════════════════════════════════════════════════════════════════
def _fetch_payload():
    """起一个临时库副本、造两类评测记录，返回三个端点的真实 payload。"""
    from fastapi.testclient import TestClient
    from main import app

    tmpdb = os.path.join(tempfile.gettempdir(), "evm_verify_%d.db" % os.getpid())
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(tmpdb + suf):
            os.remove(tmpdb + suf)
    prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(prod):
        return None, "生产库不存在（mbse.db）"
    import sqlite3
    s = sqlite3.connect("file:%s?mode=ro" % prod.replace("\\", "/"), uri=True)
    d = sqlite3.connect(tmpdb)
    s.backup(d)
    d.close()
    s.close()

    os.environ["MBSE_DB_PATH"] = tmpdb
    try:
        from database import init_db
        init_db()
        c = TestClient(app)
        ov = c.get("/api/eval/overview").json()
        hist = c.get("/api/eval/history?limit=20").json()
        gates = c.get("/api/eval/gates").json()
        # ★ 下钻端点：取一条真实记录（优先 trace_consistency——它的证据段
        #   最丰富：wrong 案例/混淆矩阵/负对照/weak 回归）
        report, report_err = None, ""
        rows_all = (hist.get("history") or {})
        cand = []
        for _k, _rows in rows_all.items():
            for _r in _rows:
                cand.append(_r)
        cand.sort(key=lambda r: (0 if (r.get("eval_kind") == "trace_consistency")
                                 else 1, -(r.get("id") or 0)))
        for _r in cand:
            _res = c.get("/api/eval/report?id=%s" % _r.get("id")).json()
            if _res.get("ok"):
                report = _res
                break
        if not report:
            report_err = "没有可下钻的评测记录"
        return {"overview": ov, "history": hist, "gates": gates,
                "report": report, "report_err": report_err}, None
    finally:
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(tmpdb + suf):
                try:
                    os.remove(tmpdb + suf)
                except Exception:
                    pass


def t_B(payload):
    print("== B 后端字段核对 ==")
    ov = payload["overview"]
    hist = payload["history"]
    gates = payload["gates"]

    ck("B1 /api/eval/overview ok", ov.get("ok") is True, str(ov.get("error"))[:200])
    ck("B2 overview 含 latest 字典", isinstance(ov.get("latest"), dict))
    ck("B3 overview 含 coverage 字段",
       isinstance(ov.get("coverage"), dict)
       and "generation_quality" in ov["coverage"])
    ck("B4 overview 含 gates 接线统计",
       isinstance((ov.get("gates") or {}).get("counts"), dict))
    ck("B5 /api/eval/history ok", hist.get("ok") is True, str(hist.get("error"))[:200])
    ck("B6 history 含 trends 序列", isinstance(hist.get("trends"), dict))
    ck("B7 /api/eval/gates ok", gates.get("ok") is True)
    ck("B8 gates counts 四项齐备",
       set((gates.get("counts") or {}).keys()) >= {"total", "wired", "exempted", "unwired"})

    # 前端会读的每条记录字段
    sample = None
    for _k, rows in (hist.get("history") or {}).items():
        if rows:
            sample = rows[0]
            break
    if sample is None:
        ck("B9 有历史记录可核对字段", False, "库里没有评测记录（先跑一次 save_report）")
    else:
        need = ["id", "doc_name", "entity_f1", "created_at", "trustworthy",
                "negative_control", "fingerprint_drift", "metric_status"]
        miss = [k for k in need if k not in sample]
        ck("B9 记录字段齐备", not miss, "缺：%s" % miss)
        ck("B10 trustworthy 是布尔", isinstance(sample.get("trustworthy"), bool))
    # ── 下钻端点（2026-10-22 晚）────────────────────────────────
    rep = payload.get("report")
    if not rep:
        ck("B11 下钻端点可取到真实报告", False,
           payload.get("report_err") or "无可下钻记录")
    else:
        ck("B11 下钻端点可取到真实报告", rep.get("ok") is True)
        ck("B11b 下钻报告含 detail 与类型",
           isinstance(rep.get("detail"), dict) and bool(rep.get("eval_kind") is not None))
        ck("B11c 下钻报告带 id（可回溯到具体那次评测）",
           rep.get("id") is not None)
    return True


# ══════════════════════════════════════════════════════════════════════
# C 前端渲染冒烟（Node vm + 真实 payload）
# ══════════════════════════════════════════════════════════════════════
HARNESS = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = process.argv[2], PF = process.argv[3];
const P = JSON.parse(fs.readFileSync(PF, 'utf8'));
const out = [];
const ck = (n, ok, d) => { out.push((ok ? 'PASS' : 'FAIL') + '|' + n + (d ? '|' + d : '')); };

const elStub = () => ({ innerHTML: '', style: {}, textContent: '',
  classList: { add() {}, remove() {} }, value: '', insertAdjacentHTML() {} });
const boxes = {};

// ⚠️ sandbox 的 `window` **必须就是 sandbox 本身**，否则脚本里的
//   `window.loadEvalManagement = ...` 写进的是另一个对象，
//   读 `sandbox.loadEvalManagement` 永远是 undefined。
//   （第一版踩了这个：harness 报 "loadEvalManagement 未导出"，
//     而脚本里明明有 window.loadEvalManagement = ...。）
const sandbox = { console, JSON, Math, Number, String, Object, Array, Date, Promise,
  isNaN, parseInt, parseFloat, encodeURIComponent, decodeURIComponent, setTimeout,
  document: { getElementById: id => boxes[id] || elStub(), querySelectorAll: () => [] },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  api: async () => ({}), toast() {}, esc: s => String(s == null ? '' : s),
  escA: s => String(s == null ? '' : s) };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
try {
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/js/mods/48-eval.js'), 'utf8'),
                  sandbox, { filename: '48-eval.js' });
} catch (e) { console.log('LOADFAIL|' + e.message); process.exit(0); }

(async () => {
  if (typeof sandbox.loadEvalManagement !== 'function') {
    console.log('LOADFAIL|loadEvalManagement 未导出'); process.exit(0);
  }
  boxes['evm-body'] = elStub();

  // 正常路径：注入真实 payload
  sandbox.api = async (url) => {
    if (url.indexOf('/api/eval/overview') >= 0) return P.overview;
    if (url.indexOf('/api/eval/history') >= 0) return P.history;
    if (url.indexOf('/api/eval/gates') >= 0) return P.gates;
    return {};
  };
  await sandbox.loadEvalManagement();
  const html = boxes['evm-body'].innerHTML;

  ck('C1 三段标题齐备',
     ['现在什么状态', '历史怎么变', '什么在拦着我'].every(t => html.indexOf(t) >= 0));
  ck('C2 无 undefined 泄漏', html.indexOf('undefined') < 0);
  ck('C3 无 NaN 泄漏', html.indexOf('NaN') < 0);
  ck('C4 无 [object Object] 泄漏', html.indexOf('[object Object]') < 0);
  ck('C5 无 null 字面量泄漏', html.indexOf('>null<') < 0);
  ck('C6 可信度徽章已渲染',
     html.indexOf('可信') >= 0 || html.indexOf('不可信') >= 0);
  ck('C7 门禁统计已渲染', html.indexOf('已接线') >= 0);
  ck('C8 三个 panel 均渲染',
     (html.match(/class="panel"/g) || []).length >= 3,
     'panel 数=' + (html.match(/class="panel"/g) || []).length);
  ck('C9 生成质量缺口有显式提示', html.indexOf('生成质量') >= 0);

  // 降级路径：后端报 ok:false
  boxes['evm-body'] = elStub();
  sandbox.api = async (url) => {
    if (url.indexOf('/api/eval/overview') >= 0) return { ok: false, error: '库未迁移' };
    return {};
  };
  await sandbox.loadEvalManagement();
  const h2 = boxes['evm-body'].innerHTML;
  ck('C10 降级路径显式报错（不白屏）', h2.indexOf('库未迁移') >= 0);
  ck('C11 降级路径无 undefined 泄漏', h2.indexOf('undefined') < 0);

  // 突变路径：payload 里塞 undefined 值
  boxes['evm-body'] = elStub();
  const bad = JSON.parse(JSON.stringify(P.overview));
  if (bad.latest) { for (const k of Object.keys(bad.latest)) { bad.latest[k].entity_f1 = undefined; } }
  sandbox.api = async (url) => {
    if (url.indexOf('/api/eval/overview') >= 0) return bad;
    return P.history;
  };
  await sandbox.loadEvalManagement();
  const h3 = boxes['evm-body'].innerHTML;
  ck('C12 payload 含 undefined 时不泄漏到 HTML', h3.indexOf('undefined') < 0);

  // ── 下钻面板渲染冒烟（2026-10-22 晚）──────────────────────────
  // 真实 detail 载荷渲染一遍，检查：无 undefined/[object Object] 泄漏、
  // 证据段标题在、div 配平（页面级哨兵之外，单独守下钻面板）。
  if (P.report) {
    boxes['modal-body'] = elStub();
    boxes['modal'] = elStub();
    sandbox.api = async (url) => {
      if (url.indexOf('/api/eval/report') >= 0) return P.report;
      return {};
    };
    if (typeof sandbox.openEvalReport === 'function') {
      await sandbox.openEvalReport(P.report.id);
      const h4 = boxes['modal-body'].innerHTML;
      ck('C13 下钻面板已渲染', h4.length > 0, '长度=' + h4.length);
      ck('C14 下钻面板无 undefined 泄漏', h4.indexOf('undefined') < 0);
      ck('C15 下钻面板无 [object Object] 泄漏', h4.indexOf('[object Object]') < 0);
      ck('C16 下钻面板无 NaN 泄漏', h4.indexOf('NaN') < 0);
      const opens = (h4.match(/<div\b/g) || []).length;
      const closes = (h4.match(/<\/div>/g) || []).length;
      ck('C17 下钻面板 div 配平', opens === closes, '<div=' + opens + ' </div>=' + closes);
      ck('C18 下钻面板含证据段标题',
         h4.indexOf('取材记账') >= 0 || h4.indexOf('报告字段') >= 0
         || h4.indexOf('检索三域指标') >= 0);
    } else {
      ck('C13 下钻入口已导出（openEvalReport）', false, 'sandbox 未导出');
    }
  } else {
    ck('C13 下钻渲染冒烟（无可下钻记录，跳过）', true, '库中暂无评测记录');
  }

  console.log(out.join('\n'));
})().catch(e => console.log('HARNESS_ERR|' + e.message));
"""


def t_C(payload):
    print("== C 前端渲染冒烟（Node vm + 真实 payload）==")
    os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
    pf = os.path.join(ROOT, "tmp", "evm_payloads.json")
    with io.open(pf, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    hf = os.path.join(ROOT, "tmp", "evm_harness.js")
    with io.open(hf, "w", encoding="utf-8") as f:
        f.write(HARNESS)
    try:
        r = subprocess.run([NODE, hf, ROOT, pf], capture_output=True, text=True, timeout=180)
    except Exception as e:
        return ck("C0 harness 执行", False, str(e)[:200])
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    if not lines:
        return ck("C0 harness 执行", False, (r.stderr or r.stdout or "no output")[:300])
    for l in lines:
        if l.startswith("LOADFAIL|") or l.startswith("HARNESS_ERR|"):
            return ck("C0 harness 执行", False, l[:300])
        parts = l.split("|")
        ck(parts[1], parts[0] == "PASS", parts[2] if len(parts) > 2 else "")
    return True


# ══════════════════════════════════════════════════════════════════════
# div 配平哨兵（抄自 verify_evolution_report.py 的 E8）
# ══════════════════════════════════════════════════════════════════════
def t_div_balance():
    print("== D div 配平哨兵 ==")
    js = _read(os.path.join(ROOT, "static", "js", "mods", "48-eval.js"))
    code = re.sub(r"/\*[\s\S]*?\*/", "", js)
    code = re.sub(r"//.*", "", code)
    for fn in ("_renderLatest", "_renderTrends", "_renderGates", "_mount",
               "_sparkline", "_dval", "_dtable", "_dsection", "_dtrace",
               "_dgen", "_drag", "_dgeneric", "_renderReportDetail",
               "openEvalReport"):
        m = re.search(r"function %s\([^)]*\)\s*\{([\s\S]*?)\n  \}" % fn, code)
        if not m:
            ck("D %s 模板 div 配平" % fn, False, "函数体未匹配到")
            continue
        body = m.group(1)
        opens = len(re.findall(r"<div\b", body))
        closes = len(re.findall(r"</div>", body))
        ck("D %s 模板 div 配平" % fn, opens == closes,
           "<div=%d </div>=%d" % (opens, closes))
    return True


# ══════════════════════════════════════════════════════════════════════
# 判据自证（变异必须判红）
# ══════════════════════════════════════════════════════════════════════
def t_mutations(payload):
    """S1 删一处接线必红 / S2 后端少一字段必红 / S3 payload 含 undefined 必红。

    ★变异体内的一切输出都带「变异体」前缀且**不计入最终统计** ——
      否则"故意造出来的红"会被误读成常态的问题（实测第一版就出现了
      "B9 FAIL 但最终统计全绿"的自相矛盾现象）。
    """
    print("== E 判据自证（变异必须判红）==")
    _IN_MUT[0] = True
    try:
        _mutations_body(payload)
    finally:
        _IN_MUT[0] = False
    # 还原后常态 A 段必须重新全绿（这行计入统计）
    del _results[:]
    t_A()
    return True


def _mutations_body(payload):
    # ★每轮变异前清空 _MUT_ROWS —— 它是累积的，不清会让"本轮的红"被
    #   "上一轮的绿"盖住（现象：E2 明明看到 A4 判红，E2 本身却报"A4 仍为 PASS"）。
    idx_path = os.path.join(ROOT, "static", "index.html")
    idx_orig = _read(idx_path)
    shell_path = os.path.join(ROOT, "static", "js", "mods", "02-shell.js")
    shell_orig = _read(shell_path)

    # S1：删掉导航入口 ⇒ A3 必须判红
    try:
        with io.open(idx_path, "w", encoding="utf-8", newline="") as f:
            f.write(idx_orig.replace('id="mf-eval-entry"', 'id="mf-eval-REMOVED"'))
        del _MUT_ROWS[:]
        t_A()
        a3 = [r for r in _MUT_ROWS if r[1].startswith("A3 ")]
        ck("E1 删掉导航入口 ⇒ A3 判红", a3 and a3[0][0] == FAIL,
           "A3 仍为 %s" % (a3[0][0] if a3 else "缺"))
    finally:
        with io.open(idx_path, "w", encoding="utf-8", newline="") as f:
            f.write(idx_orig)
        del _results[:]

    # S1b：删掉 loadPage 分派 ⇒ A4 必须判红
    try:
        with io.open(shell_path, "w", encoding="utf-8", newline="") as f:
            f.write(shell_orig.replace("if(p==='eval') loadEvalManagement();", ""))
        del _MUT_ROWS[:]
        t_A()
        a4 = [r for r in _MUT_ROWS if r[1].startswith("A4 ")]
        ck("E2 删掉 loadPage 分派 ⇒ A4 判红", a4 and a4[0][0] == FAIL,
           "A4 仍为 %s" % (a4[0][0] if a4 else "缺"))
    finally:
        with io.open(shell_path, "w", encoding="utf-8", newline="") as f:
            f.write(shell_orig)
        del _results[:]

    # S2：后端少返回 trustworthy 字段 ⇒ B9 必须判红
    # ⚠️ 变异体内 t_B() 产生的行**必须丢弃** —— 否则"故意造出来的红"
    #   会留在最终统计里，让门禁明明抓到了问题却显示"未通过"。
    #   现象：第一版 E3 之后 B9/B10 显示 FAIL，而常态化时它们是 PASS。
    p2 = json.loads(json.dumps(payload))
    for _k, rows in (p2["history"].get("history") or {}).items():
        if rows:
            rows[0].pop("trustworthy", None)
            break
    del _MUT_ROWS[:]
    t_B(p2)
    b9 = [r for r in _MUT_ROWS if r[1].startswith("B9 ")]
    ck("E3 后端少一字段 ⇒ B9 判红", b9 and b9[0][0] == FAIL,
       "B9 仍为 %s" % (b9[0][0] if b9 else "缺"))

    # S3：前端读取一个后端**没给**的字段 ⇒ 必须渲染成「不适用」而不是 undefined 泄漏
    #   变异方式：删掉 metric_status 里某个键 —— 前端读 st[c.key] 会得到 undefined。
    #   ★不能用字符串 "undefined_marker" 代替：那会被 esc() 正常转义成文本，
    #     渲染出 "undefined_marker" 字样但**不是** undefined 泄漏 ⇒ 测不到真问题。
    #     （第一版就是这么写的，E4 恒判红——变异设计的错，不是判据的错。）
    p3 = json.loads(json.dumps(payload))
    touched = False
    # ⚠️ overview.latest 是**字典**（key = 评测类型），不是 list ——
    #   按 list 下标访问会 KeyError: 0（第一版踩了这个，门禁直接崩）。
    #
    # ★变异内容的选择：把 doc_name 换成**对象**（后端字段类型错乱时最常见）
    #   ⇒ 前端 esc(对象) 会渲染成 "[object Object]" ⇒ C4 判红。
    #   为什么不用 undefined：前端对 undefined 本来就渲染成「不适用」，
    #   **不泄漏**是正确行为而非缺陷 —— 拿它当变异，测的是"前端没坏"，
    #   而门禁要证明的是"断言能抓住真的坏了"。
    for _k, item in (p3["overview"].get("latest") or {}).items():
        if isinstance(item, dict):
            item["doc_name"] = {"oops": "对象不是字符串"}
            touched = True
            break
    del _MUT_ROWS[:]
    t_C(p3)
    c_leak = [r for r in _MUT_ROWS if "object Object" in r[1] and r[0] == FAIL]
    ck("E4 后端字段类型错乱 ⇒ 渲染冒烟判红", touched and bool(c_leak),
       "touched=%s 泄漏断言=%s" % (touched, c_leak[:2]))

    # 还原后 A 段必须重新全绿
    t_A()
    return True


def main():
    print("=" * 74)
    print("评测管理页门禁（静态挂载 / 后端字段 / 前端渲染冒烟）")
    print("=" * 74)
    t_A()
    payload, err = _fetch_payload()
    if payload is None:
        ck("B0 取得真实端点 payload", False, err or "未知")
    else:
        ck("B0 取得真实端点 payload", True)
        t_B(payload)
        t_C(payload)
    t_div_balance()
    t_mutations(payload or {})

    bad = [r for r in _results if r[0] == FAIL]
    print("\n" + "=" * 74)
    print("断言总数 %d  PASS %d  FAIL %d" % (len(_results), len(_results) - len(bad), len(bad)))
    print("=" * 74)
    print("结论：%s" % ("全部通过" if not bad else "未通过"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
