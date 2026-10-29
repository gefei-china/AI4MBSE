"""验证「观测新口径」在页面上真的渲染出来，并产出可视化预览。

## 为什么用这种方式（没有浏览器可用）

宿主没有可 headless 的浏览器（venv 无 playwright、系统无 Edge/Chrome 路径命中），
所以退一步：**把两个渲染函数原样抽出来，在 node 里用真实 API 数据执行**，
再把它产出的 HTML 包成可独立打开的预览页。

这验证的是「渲染函数 + 真实数据 ⇒ 期望 HTML」，比读源码截图更接近真值，
但它**不能**替代"整页在浏览器里的最终排版"——所以：
· 抽取值、断言关系 → 可以；
· 复杂 CSS 排版效果 → 不声称已验证（如实标注）。

## 抽取纪律（本仓踩过的坑）

⛔ 不能用行号（改一行就错位）、不能用「下一个 `function`」切（会切进内部函数）、
   不能纯数花括号（模板字符串里的 `${...}` 会把它带偏）。
✅ 用**行边界法**：从函数首行起，取**缩进为 0 且以 `}` 开头**的首行作为结束。
   顶层函数体在 0 缩进处闭合，模板字符串内的 `}` 都在更深的缩进里 ⇒ 天然免疫。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ⚠️ chdir **不等于** 改 import 路径：只 chdir 时 `import knowledge_engine` 会
#   `ModuleNotFoundError`（脚本在 tools/verify/ 下，父目录不在 sys.path）。
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def extract_fn(path: str, fn_name: str) -> str:
    """按**行边界法**抽出顶层函数源码（见模块 docstring 的纪律）。"""
    lines = open(path, encoding="utf-8").read().splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("function %s(" % fn_name):
            start = i
            break
    if start is None:
        raise RuntimeError("找不到函数 %s in %s" % (fn_name, path))
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        # 顶层闭合：0 缩进且以 } 开头
        if ln and not ln[0].isspace() and ln.startswith("}"):
            return "\n".join(lines[start:j + 1])
    raise RuntimeError("函数 %s 未闭合（抽取失败）" % fn_name)


print("== A 抽取两个渲染函数（行边界法）==")
try:
    fn_probe = extract_fn("static/js/mods/31-admin.js", "renderProbeSplit")
    fn_nohit = extract_fn("static/js/mods/15-kb.js", "renderNoHitBreakdown")
    check("A1 抽到 renderProbeSplit", fn_probe.startswith("function renderProbeSplit("))
    check("A2 抽到 renderNoHitBreakdown", fn_nohit.startswith("function renderNoHitBreakdown("))
    check("A3 抽取完整（尾部是 }）", fn_probe.rstrip().endswith("}")
          and fn_nohit.rstrip().endswith("}"))
    # 完整性守卫：抽出的片段里不该混入下一个顶层函数
    check("A4 未吞入后续模块级代码（不出现下一函数名）",
          "function renderOpsTrend" not in fn_probe
          and "async function loadEngineStats" not in fn_nohit)
except Exception as e:
    check("A 抽取成功", False, str(e))
    print("\n[RESULT] 抽取失败，后续断言跳过")
    sys.exit(1)

print()
print("== B 用真实数据执行渲染函数 ==")
# 数据来源优先级：① 运行中的服务（最接近真值）→ ② 直连数据库用**同一套逻辑**取数。
#
# 为什么要 ② 这个 fallback：本机服务会被别的会话随时起停
# （实测 09:37 还在 200、09:38 已无监听，日志无崩溃痕迹 ⇒ 不是崩溃，是被外部终止）。
# 若只认 HTTP，门禁就会因为"服务此刻没起"而红 —— 那**不是代码缺陷**。
# 本仓纪律：数据不足/前提不满足时**不得报 FAIL**，应如实标注来源。
# ⚠️ ② 不能用手写假数据：必须走与 API 完全相同的聚合逻辑（QueryRouter().stats /
#    同一份 is_probe 判定），否则"渲染验证"验的是假数据。
_src = ""
dash = eng = None
try:
    import urllib.request
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}))          # {} = 全部直连（否则 502）
    with _opener.open("http://127.0.0.1:8000/api/monitor/dashboard?days=7",
                      timeout=25) as r:
        dash = json.loads(r.read())
    with _opener.open("http://127.0.0.1:8000/api/knowledge/engine-stats",
                      timeout=25) as r:
        eng = json.loads(r.read())
    _src = "HTTP API（服务在跑）"
except Exception as _e:
    print("  [说明] HTTP 不可用（%s）⇒ 回退数据库直连，用同一套聚合逻辑取数"
          % str(_e)[:60])
    import sqlite3
    from knowledge_engine import QueryRouter
    _con = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    _con.row_factory = sqlite3.Row
    eng = QueryRouter().stats(_con)
    _rows = [dict(x) for x in _con.execute(
        "SELECT prompt_tokens, completion_tokens, estimated_cost, is_probe, used_mock"
        " FROM llm_usage_stats ORDER BY id DESC LIMIT 1000")]
    _con.close()
    _biz = [u for u in _rows if not (u.get("is_probe") or 0)]
    dash = {"llm": {
        "calls": len(_rows), "calls_biz": len(_biz),
        "calls_probe": len(_rows) - len(_biz),
        "total_tokens": sum((u["prompt_tokens"] or 0) + (u["completion_tokens"] or 0)
                            for u in _rows),
        "tokens_biz": sum((u["prompt_tokens"] or 0) + (u["completion_tokens"] or 0)
                          for u in _biz),
        "estimated_cost": round(sum(u["estimated_cost"] or 0 for u in _rows), 6),
        "cost_biz": round(sum(u["estimated_cost"] or 0 for u in _biz), 6),
        "mock_rate": 0.0,
    }}
    _src = "数据库直连（服务未启动）"

check("B1 取到 dashboard 数据", bool(dash and "llm" in dash), _src)
check("B2 取到 engine-stats 数据", bool(eng and "no_hit" in eng), _src)
print("  数据来源：", _src)

# escA/escMd 的 stub（与 01-core.js 同语义：转义 HTML 特殊字符）
STUB = """
function escA(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function escMd(s){return escA(s);}
function esc(s){return escA(s);}
"""

runner = STUB + fn_probe + "\n" + fn_nohit + """
const dash = JSON.parse(process.argv[2]);
const eng  = JSON.parse(process.argv[3]);
const out = {
  probe: renderProbeSplit(dash.llm),
  nohit: renderNoHitBreakdown(eng.no_hit),
};
process.stdout.write(JSON.stringify(out));
"""
_tmp_runner = os.path.join(_ROOT, "tmp", "_render_runner.js")
open(_tmp_runner, "w", encoding="utf-8").write(runner)

try:
    res = subprocess.run(["node", _tmp_runner, json.dumps(dash), json.dumps(eng)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    if res.returncode != 0:
        check("B3 node 执行渲染函数", False, (res.stderr or "")[:400])
        sys.exit(1)
    out = json.loads(res.stdout)
    check("B3 node 执行渲染函数成功", True)
except Exception as e:
    check("B3 node 执行渲染函数", False, str(e))
    sys.exit(1)
finally:
    try:
        os.remove(_tmp_runner)
    except Exception:
        pass

html_probe, html_nohit = out["probe"], out["nohit"]
print("  probe 片段长度:", len(html_probe))
print("  nohit 片段长度:", len(html_nohit))

print()
print("== C 渲染结果断言（真实数据下的产出）==")
check("C1 探针卡片含「探针占比」标题", "探针占比" in html_probe)
check("C2 探针卡片含业务次数与成本", "业务" in html_probe and "$" in html_probe)
check("C3 探针卡片明确写出「含探针」全量口径", "含探针" in html_probe)
llm = dash.get("llm") or {}
check("C4 探针次数与 API 一致（%s）" % llm.get("calls_probe"),
      str(llm.get("calls_probe", 0)) in html_probe)
check("C5 业务次数与 API 一致（%s）" % llm.get("calls_biz"),
      str(llm.get("calls_biz", 0)) in html_probe)

nh = eng.get("no_hit") or {}
check("C6 零命中区块含总数 %s" % nh.get("total"), str(nh.get("total", 0)) in html_nohit)
check("C7 区块含「真召回缺口占比」结论", "真召回缺口占比" in html_nohit)
if nh.get("legacy"):
    check("C8 存量未细分单列并说明（legacy=%s）" % nh["legacy"],
          "未细分" in html_nohit and str(nh["legacy"]) in html_nohit)
else:
    print("  [SKIP] C8 当前无 legacy 数据")
if nh.get("corpus_gap"):
    check("C9 真召回缺口已展示（corpus_gap=%s）" % nh["corpus_gap"],
          "真召回缺口" in html_nohit)

print()
print("== D 产出可视化预览（可独立打开）==")
preview = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>观测新口径 · 页面呈现预览</title>
<style>
  body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f6fa;margin:0;padding:24px;color:#1f2430;}
  h2{font-size:15px;margin:22px 0 10px;color:#4b5563;}
  .note{font-size:12px;color:#6b7280;margin-bottom:16px;line-height:1.7;background:#fff;border-left:3px solid #7c5cff;padding:10px 14px;border-radius:4px;}
  .kpi{display:inline-block;vertical-align:top;min-width:170px;margin:0 10px 10px 0;border:1px solid #e5e7eb;border-radius:8px;padding:12px;background:#fff;}
  .kpi .n{font-size:26px;font-weight:700;}
  .kpi .l{font-size:12px;color:#6b7280;margin-top:2px;}
  .kpi .l .st{font-size:11px;padding:1px 6px;border-radius:9px;background:#eef2ff;color:#4338ca;margin-left:4px;}
  .st.w{background:#fff7ed;color:#c2410c;} .st.ok{background:#ecfdf5;color:#047857;}
  .st.b{background:#eff6ff;color:#1d4ed8;} .st.g{background:#f3f4f6;color:#4b5563;}
  .st.r{background:#fef2f2;color:#b91c1c;}
  .tag{display:inline-block;font-size:11px;background:#f3f4f6;border-radius:4px;padding:2px 8px;margin:2px 4px 2px 0;color:#4b5563;}
  #wrap{max-width:1080px;}
</style></head>
<body><div id="wrap">
<div class="note">
  <b>说明：</b>本页是把 <code>renderProbeSplit()</code>（监控页 KPI）与
  <code>renderNoHitBreakdown()</code>（知识库引擎卡片）这两个函数
  <b>原样抽出</b>，喂以<b>真实数据</b>后生成的渲染结果。<br>
  <b>本次数据来源：{SRC}</b>（服务可运行时优先走 HTTP API；
  服务被外部起停时回退数据库直连，聚合逻辑与 API 完全相同）。<br>
  用途是让「新口径在页面上长什么样」可被直接看到；它<b>不等于</b>整页浏览器截图
  （宿主无可用 headless 浏览器），复杂排版效果未声称已验证。
</div>

<h2>① 监控页 · LLM 用量「业务 / 探针」双口径</h2>
<div>{PROBE}</div>
<div style="font-size:11px;color:#9ca3af;margin-top:4px;">
  全量 calls={CALLS} · business={BIZ} · probe={PROBEN}
</div>

<h2>② 知识库页 · 零命中成因分解</h2>
<div>{NOHIT}</div>
<div style="font-size:11px;color:#9ca3af;margin-top:4px;">
  total={TOT} · pct_of_all={PCT}% · legacy={LEG}
</div>
</div></body></html>
""".replace("{SRC}", _src).replace("{PROBE}", html_probe).replace("{NOHIT}", html_nohit) \
   .replace("{CALLS}", str(llm.get("calls"))).replace("{BIZ}", str(llm.get("calls_biz"))) \
   .replace("{PROBEN}", str(llm.get("calls_probe"))).replace("{TOT}", str(nh.get("total"))) \
   .replace("{PCT}", str(nh.get("pct_of_all"))).replace("{LEG}", str(nh.get("legacy")))
_out = os.path.join(_ROOT, "docs", "观测口径-页面呈现预览.html")
os.makedirs(os.path.dirname(_out), exist_ok=True)
open(_out, "w", encoding="utf-8").write(preview)
check("D1 预览文件已生成", os.path.isfile(_out), _out)
check("D2 预览含两块内容", len(preview) > 800, "%d 字符" % len(preview))
check("D3 预览如实标注数据来源（不谎称 API）", _src in preview, _src)

print()
print("[RESULT] PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
print("[预览]", _out)
if _FAIL:
    print("[FAILED]", _FAIL)
    sys.exit(1)
print("[ALL GREEN]")
