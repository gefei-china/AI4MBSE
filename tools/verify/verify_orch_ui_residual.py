#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排 UI 残影 / 口径回归门禁（conv 990461 实证，静态接线 + Node 真实行为）。

## 事故（同一轮对话一次暴露四类缺陷）
1. 顶部「✓ 已完成」与思考块「思考中…」同屏，且终止按钮已隐藏 ⇒ 状态撕裂
   （`finishStream` 只置 `_procS.done`，不收敛 timeline；思考块收口只认「生成与校验」）
2. 标题后出现裸 `<div style="display:flex;`（`_procSumText` 把可信 HTML 当纯文本）
3. `plan.length - doneN` 把 partial / skipped 全算「失败」⇒ 1 个真失败报成 6 失败
4. T1 补跑重复 append ⇒ `card_data.plan` 里 t1 出现两次（5 个任务显示 6 条）

## 判据工程纪律
本仓已多次栽在「静态字串判据空转」上，故每条静态断言都配一条**真实行为断言**：
用 Node 直接加载 `11-pipeline.js` / `06-cards.js` 的**真实函数源码**执行，
并给 `_procSumText` 配一条**变异自证**（旧实现必须泄出 `<`，证明判据能判红）。
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def chk(label, ok, detail=""):
    (PASS if ok else FAIL).append((label, detail))
    print(("PASS" if ok else "FAIL"), label, detail)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


JS = r'''
const fs = require("fs");
const path = require("path");
const ROOT = __ROOT__;
const p11 = fs.readFileSync(path.join(ROOT, "static/js/mods/11-pipeline.js"), "utf8");
const p06 = fs.readFileSync(path.join(ROOT, "static/js/mods/06-cards.js"), "utf8");
const out = [];
const ck = (label, ok, detail) => out.push({label, ok: !!ok, detail: String(detail == null ? "" : detail)});
function slice(src, start, end){
  const i = src.indexOf(start), j = src.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error("抽取失败: " + start + " / " + end);
  return src.slice(i, j);
}

// ── B1 摘要函数：真实源码 + 真实 HTML 片段 ─────────────────────────────
const sumSrc = slice(p11, "function _procSumText(t){", "\n// Task 14");
const sumFn = new Function(sumSrc + "\nreturn _procSumText;")();
const html = '<div class="proc-subtask" style="border:1px solid var(--line);border-radius:8px;' +
             'padding:5px 8px;margin:3px 0;">解析并校验【发动机功能架构基线】SysML v2 代码</div>';
const s1 = sumFn(html);
ck("B1 摘要输出不含裸 HTML 标签", !s1.includes("<") && !s1.includes(">"), s1.slice(0, 70));
// 变异自证：旧实现（只剥 markdown）必须泄出标签，否则本判据是空转的
const oldSum = function(t){
  let s = String(t || "").replace(/\*\*(.+?)\*\*/g, "$1").trim();
  if (!s) return "";
  const m = s.match(/[。！？；!?;]/);
  if (m) s = s.slice(0, m.index + 1);
  return s.length > 52 ? s.slice(0, 52) + "…" : s;
};
ck("B1b 判据自证:旧实现确实会泄出 '<'", oldSum(html).includes("<"), oldSum(html).slice(0, 50));

// ── B2 编排 chip：partial 不得计入失败 ───────────────────────────────
const stubs = {
  esc: s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  subtaskCardHtml: p => '<div class="st-row">' + (p.key || "") + '</div>',
  document: { getElementById: () => null },
  window: {},
};
const api = new Function("esc", "subtaskCardHtml", "document", "window", "console",
  p06 + "\nreturn {renderOrchCard};")(
  stubs.esc, stubs.subtaskCardHtml, stubs.document, stubs.window, console);
const cd = { plan: [{key:"t1",status:"done"}, {key:"t2",status:"partial"},
                    {key:"t3",status:"partial"}],
             orchestrated: true, orchestrated_status: "partial" };
const h = api.renderOrchCard(cd);
ck("B2 partial 不计入失败数", !h.includes("2 失败"), h.slice(0, 160));
ck("B2b 未完成数单独可见", h.includes("2 未完成"), "");
ck("B2c 真实 failed 仍被计数", (() => {
  const h2 = api.renderOrchCard({plan:[{key:"a",status:"failed"},{key:"b",status:"partial"}],
                                 orchestrated:true});
  return h2.includes("1 失败") && h2.includes("1 未完成");
})(), "");

// ── B3 终态收敛：真实函数源码 + 桩状态 ───────────────────────────────
const finSrc = slice(p11, "function procFinalizePending(){", "\n// P0-1：编排 DAG");
const procS = {
  timeline: [
    {id:"thinking", type:"thinking", status:"run", statusText:"思考中…", raw:"先分析需求，再建模。"},
    {id:"stage_x", type:"stage", status:"run", statusText:"执行中…"},
    {id:"tool_x", type:"tool", status:"failed", statusText:"✗ 失败"},
  ],
  thinkingOrder: [0],
  dag: {order:["t1"], nodes:{t1:{status:"run"}}},
};
let rendered = 0;
const fn = new Function("_procS", "_procSumText", "subtaskCardHtml", "_subChildrenHtml",
                        "procRender", finSrc + "\nreturn procFinalizePending();");
const flipped = fn(procS, sumFn, () => '<div></div>', () => "", () => { rendered++; });
ck("B3 两个 run 态环节都被收敛", flipped === 2, "flipped=" + flipped);
ck("B3b thinking 不再思考中", procS.timeline[0].status === "done"
   && !procS.timeline[0].statusText.includes("思考中"), procS.timeline[0].statusText);
ck("B3c 其它 run 态变中性终态（不谎报完成）", procS.timeline[1].status === "stopped"
   && procS.timeline[1].statusText === "◦ 已结束", procS.timeline[1].statusText);
ck("B3d 既有 failed 不被误改", procS.timeline[2].status === "failed");
ck("B3e DAG run 节点归 partial", procS.dag.nodes.t1.status === "partial");
ck("B3f 触发重绘（DOM 快照前状态已刷新）", rendered >= 1, "rendered=" + rendered);

console.log(JSON.stringify(out));
'''


def main():
    p11 = read("static/js/mods/11-pipeline.js")
    p13 = read("static/js/mods/13-reports.js")
    p06 = read("static/js/mods/06-cards.js")
    pstream = read("agent/pipeline_parts/stream.py")

    chk("S1 finishStream 两条快照路径都先收敛", p13.count("procFinalizePending()") >= 2,
        "count=%d" % p13.count("procFinalizePending()"))
    chk("S2 procFinalizePending 已定义", "function procFinalizePending()" in p11)
    chk("S3 思考块收口认「生成汇总」", "name === '生成汇总'" in p11)
    chk("S4 失败计数只数 failed", "plan.filter(p=>p.status==='failed')" in p06)
    chk("S5 摘要函数剥 HTML 标签", "<[^>]*>" in p11 and "&lt;" in p11)
    chk("S6 plan/subtasks 按 key 覆盖（T1 补跑不重复）",
        'orch_tasks[:] = [x for x in orch_tasks if x.get("key") != tkey]' in pstream
        and 'subtasks[:] = [x for x in subtasks if x.get("key") != tkey]' in pstream)

    js = JS.replace("__ROOT__", json.dumps(ROOT.replace("\\", "/")))
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        r = subprocess.run(["node", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=ROOT, timeout=90)
        chk("N0 Node 行为验证可执行", r.returncode == 0,
            (r.stderr or "").strip()[:200])
        if r.returncode == 0:
            try:
                for row in json.loads((r.stdout or "").strip().splitlines()[-1]):
                    chk("N " + row["label"], row["ok"], row["detail"])
            except Exception as e:
                chk("N0 行为结果可解析", False, "%s: %s" % (type(e).__name__, e))
    except FileNotFoundError:
        chk("N0 Node 行为验证可执行", False, "node 不存在 ⇒ 行为判据无法执行（不判 SKIP）")
    except subprocess.TimeoutExpired:
        chk("N0 Node 行为验证可执行", False, "90s 超时")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)

    print("\n==== %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
    for label, detail in FAIL:
        print("  FAIL:", label, detail)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
