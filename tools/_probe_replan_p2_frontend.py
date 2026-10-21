# -*- coding: utf-8 -*-
"""P2 replanning 前端渲染验证探针（2026-10-21）。

**为什么需要**：P2 新增 `plan_revised` SSE 事件与前端「计划修订卡」
    （static/js/mods/11-pipeline.js::procAddPlanRevised）。门禁钉的是"接线存在"，
    但**渲染出来的 HTML 对不对**（徽章/原因/三类 key 清单/默认折叠）只有真跑
    函数体能证明 —— 与 P1 的 verify_doc_trace_ui.py 同款手法：抽真函数体在 Node 里执行。

**为什么必须用"抽真函数体"而不是重写一份**：重写的那份会与实现漂移，验证就失去意义。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools\\_probe_replan_p2_frontend.py
退出码：全绿 0 / 有失败 1。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "static", "js", "mods", "11-pipeline.js")

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


# ── 1. 语法检查（node --check 真解析）────────────────────────────────────
print("=== 1. JS 语法 ===")
r = subprocess.run(["node", "--check", JS], capture_output=True, text=True)
ok = _rec("F1 node --check 通过（11-pipeline.js 语法合法）", r.returncode == 0,
          r.stderr[:200])
if not ok:
    print("结果：%d PASS / %d FAIL" % (0, len(_results)))
    sys.exit(1)

# ── 2. 抽真函数体在 Node 执行 ────────────────────────────────────────────
print("\n=== 2. 计划修订卡渲染（抽真函数体）===")
JS_HARNESS = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
const m=src.match(/function procAddPlanRevised\(ev\)\{[\s\S]*?\n\}/);
if(!m){ console.log(JSON.stringify({error:'extract_failed'})); process.exit(1); }
const ups=[];
global.esc=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.procUpsert=(id,patch)=>ups.push({id,patch});
eval(m[0].replace('function procAddPlanRevised','globalThis.procAddPlanRevised = function'));
// 轻量级：同 key 重派
globalThis.procAddPlanRevised({run_id:538,level:'T2-light',action:'replan',
  reason:'T2-light 差量重画 1 个任务（超时）',updates:['t2'],new_tasks:[],dropped:['t5'],retry_keys:['t2']});
// 重量级：新 key + 移除旧 key
globalThis.procAddPlanRevised({run_id:538,level:'T2-heavy',action:'replan',
  reason:'换 Agent 重画（换实现路径）',updates:[],new_tasks:['t2_r1'],dropped:['t2'],retry_keys:['t2_r1']});
// 缺字段兜底（老服务端可能只发 level/reason）
globalThis.procAddPlanRevised({run_id:538,level:'T1.5',reason:'叶子任务主动跳过'});
const d1=ups[0].patch.detail, d2=ups[1].patch.detail, d3=ups[2].patch.detail;
const out={
  count: ups.length,
  title1: ups[0].patch.title,
  title2: ups[1].patch.title,
  title3: ups[2].patch.title,
  hasReason1: d1.includes('差量重画 1 个任务'),
  hasUpdates1: d1.includes('>t2<'),
  hasDrops1: d1.includes('>t5<'),
  hasAdds2: d2.includes('>t2_r1<'),
  collapsed1: ups[0].patch.collapsed===true,
  collapsed3: ups[2].patch.collapsed===true,
  noCrash3: d3.length>0,
  idUnique: (ups[0].id!==ups[1].id)
};
console.log(JSON.stringify(out));
"""
js_path = os.path.join(ROOT, "tools", "_p2_frontend_harness.js")
with open(js_path, "w", encoding="utf-8") as f:
    f.write(JS_HARNESS)
try:
    r2 = subprocess.run(["node", js_path, JS], capture_output=True, text=True)
finally:
    if os.path.exists(js_path):
        os.remove(js_path)

if r2.returncode != 0:
    _rec("F2 抽真函数体执行成功", False, r2.stderr[:300])
else:
    import json as _j
    try:
        d = _j.loads(r2.stdout.strip().splitlines()[-1])
    except Exception:
        d = {"error": "parse_failed", "raw": r2.stdout[:200]}
    _rec("F2 抽真函数体执行成功（procAddPlanRevised 可独立渲染）",
         "error" not in d, str(d)[:200])
    _rec("F3 三次修订各生成一张卡（含缺字段兜底那次）",
         d.get("count") == 3, str(d.get("count")))
    _rec("F4 徽章文案按级别区分（T2-light 局部重派 / T2-heavy 局部重画 / T1.5 跳过）",
         "局部重派" in str(d.get("title1")) and "局部重画" in str(d.get("title2"))
         and "跳过" in str(d.get("title3")),
         "%s / %s / %s" % (d.get("title1"), d.get("title2"), d.get("title3")))
    _rec("F5 原因全文进卡（不截断，不藏 tooltip）", d.get("hasReason1") is True)
    _rec("F6 重派 key 清单渲染", d.get("hasUpdates1") is True)
    _rec("F7 移除 key 清单渲染", d.get("hasDrops1") is True)
    _rec("F8 新增 key 清单渲染（重量级新 key）", d.get("hasAdds2") is True)
    _rec("F9 默认折叠（低频事件不占屏）", d.get("collapsed1") is True
         and d.get("collapsed3") is True)
    _rec("F10 缺字段不崩（老服务端兼容）", d.get("noCrash3") is True)
    _rec("F11 不同修订用不同卡 id（不会互相覆盖）", d.get("idUnique") is True)

# ── 3. 事件接线（plan_revised 分支存在且调用渲染函数）────────────────────
print("\n=== 3. 事件接线 ===")
with open(JS, encoding="utf-8") as f:
    src = f.read()
ok = _rec("F12 SSE 分支识别 plan_revised 并调用渲染函数",
          "evType === 'plan_revised'" in src and "procAddPlanRevised(ev)" in src)

fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
for _, n, d in fails:
    print("  FAIL  %s  %s" % (n, d))
sys.exit(0 if not fails else 1)
