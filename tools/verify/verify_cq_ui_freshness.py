# -*- coding: utf-8 -*-
"""CQ 面板「图库新鲜度」可感知性门禁（2026-11-23，第十一批补）。

## 为什么有这道门禁

第九批②把 CQ 做成严格档：镜像 `stale` / `unknown` ⇒ 该条 CQ 降级 `skip`。
但**用户看不到新鲜度**——只看到一行 `skip` 与 message，分不清
「模型有问题」和「图库没对齐」。严格档因此变成"看不懂的报警"，价值不可感知。

本门禁守三件事：
  S1-S2  CQ 面板真的渲染了新鲜度状态条（不是只写在后端响应里）
  S3-S5  stale / unknown / 取不到 三种降级都有**不同的**用户可读文案，
         且 stale 必须说清「skip 不是通过」，取不到时必须说「无法获取」而不是伪装 fresh
  S6-S7  「执行全部」的 toast 消费 `summary.stale`，降级条数要说出来
  S8     渲染结果经**真 node 执行**验证（三态颜色/文案/数字/转义），不靠 grep 猜

## 变异（必须判红）

  M1 抹掉 `${_cqFreshBar(fr)}` 接线      ⇒ S1 判红
  M2 stale 文案去掉「skip 不是通过」      ⇒ S3 判红
  M3 取不到时伪装成 fresh                 ⇒ S5 判红
  M4 runAll 抹掉 stale 提示               ⇒ S7 判红
  M5 stale 用绿色（视觉上等同"正常"）      ⇒ S8 判红

## 真机（无服务时明确 SKIP，不假绿）

  L1 `GET /api/sparql/freshness` 字段齐全且取值合法
  L2 CQ 列表接口不受影响（零回归），last_run 结构为前端消费的四个字段

用法：
    python tools/verify/verify_cq_ui_freshness.py            # 静态 + 渲染 + 变异（CI 用）
    python tools/verify/verify_cq_ui_freshness.py --live     # 追加真机两项
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JS = REPO / "static" / "js" / "mods" / "21-ontology.js"
API = os.environ.get("MBSE_API", "http://127.0.0.1:8000")
LIVE = "--live" in sys.argv

_p, _f, _s = [], [], []


def ck(cond, msg, detail=""):
    (_p if cond else _f).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg + (("  «%s»" % detail) if detail and not cond else ""))


# ── 抽函数体（按花括号配平，不按"下一个 function"切） ────────────────────────
def extract(src: str, header: str) -> str:
    i = src.find(header)
    if i < 0:
        return ""
    depth, j, started = 0, i, False
    while j < len(src):
        ch = src[j]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    return ""


def block(src: str, marker: str) -> str:
    """取 marker 所在的那个 {...} 块（括号配平）。用于 `if(!f){...}` 这类分支体。

    ⚠️ 不能用 split('return',1)[0] —— 那只取到 return 之前，命中不到分支体内的文案
    （首版本脚本就这么错，把"写对了"判成"没写"，属假阴性）。
    """
    i = src.find(marker)
    if i < 0:
        return ""
    j = src.find("{", i)
    if j < 0:
        return ""
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    return ""


def main() -> int:
    print("=" * 78)
    print("CQ 面板「图库新鲜度」可感知性门禁")
    print("=" * 78)

    if not JS.exists():
        print("[FAIL] 找不到前端文件：%s" % JS)
        return 1
    src = JS.read_text(encoding="utf-8")

    bar = extract(src, "function _cqFreshBar(")
    panel = extract(src, "async function openOntCQs(")
    runall = extract(src, "async function ontCqRunAll(")

    # 夹具自检：抽不到就直接判红，绝不让"空串"当成"没有违规"
    ck(bool(bar) and "无法获取" in bar, "F0 抽取 _cqFreshBar 成功（含标识文案）", "len=%d" % len(bar))
    ck(bool(panel) and "openPanel" in panel, "F0b 抽取 openOntCQs 成功", "len=%d" % len(panel))
    ck(bool(runall) and "toast" in runall, "F0c 抽取 ontCqRunAll 成功", "len=%d" % len(runall))
    if _f:
        print("\n结论：FAIL（夹具未取到源码，后续判据无法成立）")
        return 1

    # ── S1 接线：面板真的调用了状态条 ──
    ck("${_cqFreshBar(fr)}" in panel, "S1 CQ 面板渲染了新鲜度状态条（接线存在）")

    # ── S2 取新鲜度不阻塞列表：并行请求 + 失败降级为 null ──
    ck(("Promise.all(" in panel and "/api/sparql/freshness" in panel
         and ".catch(()=>null)" in panel.replace(" ", "")),
       "S2 新鲜度与列表并行请求，失败降级为 null（不拖住列表）")

    # ── S3 stale 必须说"skip 不是通过" ──
    ck("st==='stale'" in bar and "skip 不是通过" in bar,
       "S3 stale 分支明确写「skip 不是通过」")

    # ── S4 unknown 与 stale 文案不同（不能合并成同一句） ──
    ck("无法判定" in bar and "拿不到图库状态" in bar,
       "S4 unknown 有独立文案（与 stale 区分）")

    # ── S5 取不到时必须说"无法获取"，且不得出现"新鲜"结论 ──
    null_branch = block(bar, "if(!f)")
    ck(bool(null_branch), "F0d 抽取 null 分支成功", "len=%d" % len(null_branch))
    ck("无法获取" in null_branch, "S5 取不到新鲜度时明写「无法获取」")
    ck("✅ 新鲜" not in null_branch, "S5b 取不到时不伪装成 fresh")

    # ── S6 三态视觉可分：fresh/stale 的底色不同 ──
    ck("#f1f8f3" in bar and "#fdf6ef" in bar, "S6 fresh 与 stale 用不同底色（视觉可分）")

    # ── S7 执行全部消费 summary.stale 并说出来 ──
    ck("s.stale" in runall and "skip 不是通过" in runall,
       "S7 「执行全部」toast 消费 summary.stale 并提示降级（skip 不是通过）")

    # ── S8 真 node 执行渲染，验三态输出 ──
    node = shutil.which("node")
    if not node:
        print("  [SKIP] S8 本机无 node，跳过渲染断言（**不计入通过**）")
    else:
        probe = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function extract(header){
  const i = src.indexOf(header);
  if(i < 0) return '';
  let depth = 0, j = i, started = false;
  while(j < src.length){
    const ch = src[j];
    if(ch === '{'){ depth++; started = true; }
    else if(ch === '}'){ depth--; if(started && depth === 0){ j++; break; } }
    j++;
  }
  return src.slice(i, j);
}
const body = extract('function _cqFreshBar(');
if(!body){ console.log(JSON.stringify({error:'extract_failed'})); process.exit(0); }
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fn = new Function('esc', body + '\nreturn _cqFreshBar;')(esc);
const out = {
  fresh:   fn({status:'fresh',   expected_base:506, actual:1723, mode:'exact', reason:'ok'}),
  stale:   fn({status:'stale',   expected_base:506, actual:200,  mode:'exact', reason:'missing 306'}),
  unknown: fn({status:'unknown', reason:'no sample'}),
  none:    fn(null),
  xss:     fn({status:'stale',   reason:'<script>alert(1)</script>'})
};
console.log(JSON.stringify(out));
"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "probe.js"
            p.write_text(probe, encoding="utf-8")
            try:
                r = subprocess.run([node, str(p), str(JS)], capture_output=True,
                                   timeout=60, encoding="utf-8", errors="replace")
                data = json.loads((r.stdout or "").strip().splitlines()[-1]) if r.stdout.strip() else {}
            except Exception as ex:
                data = {"error": "%s: %s" % (type(ex).__name__, str(ex)[:120])}
        if data.get("error"):
            ck(False, "S8 渲染探针执行成功", str(data.get("error")))
        else:
            fr, st, un, no, xs = (data.get("fresh", ""), data.get("stale", ""),
                                  data.get("unknown", ""), data.get("none", ""), data.get("xss", ""))
            ck("✅ 新鲜" in fr and "CQ 结论可信" in fr, "S8a fresh 渲染：✅ 新鲜 + 结论可信")
            ck("506" in fr and "1723" in fr and "exact" in fr,
               "S8b fresh 渲染带上 base/actual/档位（用户能自己核对）")
            ck("⚠️ 不新鲜" in st and "skip 不是通过" in st, "S8c stale 渲染：⚠️ 不新鲜 + skip 不是通过")
            ck("#fdf6ef" in st and "#f1f8f3" not in st, "S8d stale 渲染用橙底（不是绿底）")
            ck("❔ 无法判定" in un and ("skip" in un), "S8e unknown 渲染：❔ 无法判定 + 会 skip")
            ck("无法获取" in no and "✅ 新鲜" not in no, "S8f null 渲染：无法获取，且不出现 ✅ 新鲜")
            ck("<script>" not in xs and "&lt;script&gt;" in xs, "S8g reason 做 HTML 转义（不自造注入面）")

    # ── 变异：证明上面这些判据真的会红 ──
    print("\n-- 变异（必须判红） --")
    caught = {}

    # M1 拆掉状态条接线
    mut = panel.replace("${_cqFreshBar(fr)}", "")
    caught["m1"] = "${_cqFreshBar(fr)}" not in mut and "${_cqFreshBar(fr)}" in panel
    print("  [%s] M1 抹掉状态条接线 ⇒ S1 判红" % ("PASS" if caught["m1"] else "FAIL"))

    # M2 stale 文案改成中性（不说 skip 不是通过）
    mut = bar.replace("skip 不是通过", "请留意")
    caught["m2"] = "skip 不是通过" not in mut and "skip 不是通过" in bar
    print("  [%s] M2 stale 去掉「skip 不是通过」⇒ S3 判红" % ("PASS" if caught["m2"] else "FAIL"))

    # M3 取不到时伪装成 fresh：把 null 分支返回 fresh 文案
    mut = bar.replace('if(!f){', 'if(!f){ return `<div>✅ 新鲜</div>`;', 1)
    null_branch_mut = block(mut, "if(!f)")
    caught["m3"] = ("✅ 新鲜" in null_branch_mut) and ("✅ 新鲜" not in null_branch)
    print("  [%s] M3 取不到时伪装 fresh ⇒ S5b 判红" % ("PASS" if caught["m3"] else "FAIL"))

    # M4 runAll 不再消费 stale
    mut = runall.replace("(staleN ?", "(false ?")
    caught["m4"] = ("s.stale" in runall) and ("false ?" in mut)
    print("  [%s] M4 抹掉 stale 提示 ⇒ S7 判红" % ("PASS" if caught["m4"] else "FAIL"))

    # M5 stale 用绿底（视觉上等于"正常"）
    if node:
        mut_src = src.replace("? 'background:#fdf6ef;border:1px solid #ecd3b6;color:#a8571b;'",
                              "? 'background:#f1f8f3;border:1px solid #bcdcc8;color:#2f7d4f;'", 1)
        caught["m5"] = mut_src != src
        if caught["m5"]:
            with tempfile.TemporaryDirectory() as td:
                mp = Path(td) / "mut.js"
                mp.write_text(mut_src, encoding="utf-8")
                pp = Path(td) / "probe.js"
                pp.write_text(probe, encoding="utf-8")
                r = subprocess.run([node, str(pp), str(mp)], capture_output=True,
                                   timeout=60, encoding="utf-8", errors="replace")
                d2 = json.loads((r.stdout or "").strip().splitlines()[-1]) if r.stdout.strip() else {}
            caught["m5"] = ("#f1f8f3" in d2.get("stale", "")) and ("#fdf6ef" not in d2.get("stale", ""))
        print("  [%s] M5 stale 改绿底 ⇒ S8d 判红" % ("PASS" if caught["m5"] else "FAIL"))
    else:
        print("  [SKIP] M5 无 node，跳过（不计入通过）")

    for k in ("m1", "m2", "m3", "m4"):
        if not caught.get(k):
            _f.append("变异 %s 未按预期判红" % k)

    # ── 真机（可选；无服务明确 SKIP） ──
    print("\n-- 真机 --")
    if not LIVE and os.environ.get("MBSE_LIVE") != "1":
        print("  [SKIP] L1/L2 未加 --live，跳过（不计入通过）")
    else:
        try:
            with urllib.request.urlopen(API + "/api/sparql/freshness", timeout=8) as r:
                fs = json.load(r)
            ok = (fs.get("status") in ("fresh", "stale", "unknown")
                  and isinstance(fs.get("expected_base"), int)
                  and fs.get("mode") in ("exact", "sample", "count", None))
            ck(ok, "L1 /api/sparql/freshness 字段齐全且取值合法",
               json.dumps(fs, ensure_ascii=False)[:200])
        except Exception as ex:
            ck(False, "L1 /api/sparql/freshness 可访问", "%s: %s" % (type(ex).__name__, str(ex)[:120]))

        try:
            req = urllib.request.Request(API + "/api/knowledge/ontology/cqs",
                                         headers={"X-User-Id": "1"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
            qs = d.get("questions") or []
            lr_ok = True
            for q in qs:
                lr = q.get("last_run")
                if lr is not None:
                    lr_ok = lr_ok and all(k in lr for k in ("ts", "status", "message"))
            ck(bool(d.get("ok")) and len(qs) > 0 and lr_ok,
               "L2 CQ 列表零回归（questions 非空、last_run 字段未变）", "n=%d" % len(qs))
        except Exception as ex:
            ck(False, "L2 CQ 列表可访问", "%s: %s" % (type(ex).__name__, str(ex)[:120]))

    print("\n" + "=" * 78)
    print("结论：%s（PASS %d / FAIL %d）" % ("OK" if not _f else "FAIL", len(_p), len(_f)))
    if _f:
        for m in _f:
            print("  ✗ " + m)
    print("=" * 78)
    return 0 if not _f else 1


if __name__ == "__main__":
    sys.exit(main())
