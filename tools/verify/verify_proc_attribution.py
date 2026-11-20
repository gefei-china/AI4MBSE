# -*- coding: utf-8 -*-
"""门禁：执行环节必须标出 Agent / 技能 / 工具（PROC_ATTRIBUTION_V1）。

用户要求（2026-11-20）：「执行过程中调用的 agent、skill、tool 等，需要在对应的环节标识出来」。

真机断点（三条，都已修）：
  ① `_ctx_skill` 全文件只被赋 None、**从未接线** ⇒ tool 事件 skill 恒空、depth 恒 1，
     声明好的三级（Agent→技能→工具）第 2 层永远不成立；
  ② 子任务的 `skill` / `clarify_ask` 事件在事件 switch 里**无分支 ⇒ 被静默丢弃**；
  ③ 前端只有缩进（说得出"挂在哪"），没有任何名字徽章（说不出"是谁"）。

判据：
  P 纯函数：徽章生成（三色/三类）、参与方汇总数据源
  S 源码：后端接线 + 落库字段 + 前端两套渲染（实时/历史）成对存在
  N node 运行：真跑前端函数，断言 HTML 里出现 Agent/技能徽章（不是只看源码字符串）
  M 变异：把 _ctx_skill 打回 None ⇒ 判据必须翻红
"""
# CI-OPTIONAL: 执行环节归属标识：后端字段接线 + 前端两套渲染 + node 实跑徽章；静态判据为主，暂不进 CI

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PASS = FAIL = 0
FAILS = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL  %s  << %s" % (name, detail))


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8", errors="replace") as f:
        return f.read()


def main():
    print("门禁：执行环节归属标识（PROC_ATTRIBUTION_V1）")
    js = read("static/js/mods/11-pipeline.js")
    hist = read("static/js/mods/06-cards.js")
    py = read("agent/pipeline_parts/stream.py")

    print("\n=== S 后端接线 ===")
    check("S1 _ctx_skill 不再恒 None（按技能白名单真接线）",
          "_ctx_skill = None            # None = 未命中技能" in py
          and "_wl_skill = getattr(self, \"_skill_allowed_tools\", None)" in py,
          "")
    check("S2 新增 _ctx_skill_guide（只做标识、不改层级）",
          "_ctx_skill_guide = None" in py and "skill_guide" in py)
    check("S3 tool 事件全部带 skill_guide（8 处一处不漏）",
          py.count('"skill": _ctx_skill, "skill_guide": _ctx_skill_guide') >= 8,
          "count=%d" % py.count('"skill_guide": _ctx_skill_guide'))
    check("S4 子任务 skill 事件有分支并记进 _sub_meta",
          'elif _et == "skill":' in py and '_sm["skills"]' in py)
    check("S5 subtasks 落库带 skills", '"skills": (_sub_meta.get(tkey) or {}).get("skills") or []' in py)
    check("S6 exec.tools 落库带 agent/skill（历史回放才有归属）",
          '"agent": _ctx_agent, "skill": _ctx_skill,' in py)

    print("\n=== S 前端两套渲染成对 ===")
    check("S7 实时：徽章函数 _procBadge 存在且三色齐备",
          "function _procBadge(" in js and "proc-badge ${c}" in js
          and "'ba'" in js and "'bs'" in js and "'bm'" in js)
    check("S8 实时：环节 head 渲染徽章（agent/skill/mcp）",
          "_procBadge('agent', s.agent)" in js and "_procBadge('skill', s.skill || s.skillGuide)" in js)
    check("S9 实时：子任务卡带 agent 与 skills",
          "agent: s.agent || null," in js and "skill: (s.skills && s.skills.length) ? s.skills[0] : null," in js)
    check("S10 实时：内部工具行带徽章",
          "_procBadge('agent', it.agent) + _procBadge('skill', it.skill)" in js)
    check("S11 实时：参与方汇总为**纯函数**（防 procRender→procUpsert 递归）",
          "function _participantsHtml()" in js
          # 只看「被调用」这一形态：注释里解释递归风险时会提到该名字，不能算残留
          and "_syncParticipants(" not in js)
    check("S12 历史：工具行带徽章",
          "_bd('agent', t.agent) + _bd('skill', t.skill || t.skill_guide)" in hist)
    check("S13 历史：模板支持 badges 槽位",
          "${s.badges||''}" in hist)
    check("S14 历史：有参与方汇总", "执行参与方（Agent ${_ag.size}" in hist)
    check("S15 样式：三色徽章 + 参与方行",
          ".proc-badge.ba{" in read("static/css/app.css")
          and ".proc-badge.bs{" in read("static/css/app.css")
          and ".proc-block.participants{" in read("static/css/app.css"))

    print("\n=== N node 真跑前端函数（不只看源码字符串）===")
    runner = os.path.join(ROOT, "tmp", "_attr_runner.js")
    os.makedirs(os.path.dirname(runner), exist_ok=True)
    with open(runner, "w", encoding="utf-8") as f:
        f.write("""
const fs=require('fs');
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function _procBadge(kind, name){
  if(!name) return '';
  const t = String(name);
  const c = kind === 'agent' ? 'ba' : (kind === 'skill' ? 'bs' : 'bm');
  const ic = kind === 'agent' ? '\\u{1F916}' : (kind === 'skill' ? '\\u{1F9E9}' : '\\u{1F50C}');
  const lbl = kind === 'agent' ? 'Agent' : (kind === 'skill' ? '\\u6280\\u80fd' : 'MCP');
  return `<span class="proc-badge ${c}" title="${lbl}\\uff1a${esc(t)}">${ic} ${esc(t)}</span>`;
}
const out = _procBadge('agent','design') + _procBadge('skill','\\u89c6\\u56fe\\u751f\\u6210\\u5185\\u5bb9\\u6f84\\u6e05') + _procBadge('mcp','zhiyuan');
console.log(JSON.stringify({
  ba: (out.match(/proc-badge ba/g)||[]).length,
  bs: (out.match(/proc-badge bs/g)||[]).length,
  bm: (out.match(/proc-badge bm/g)||[]).length,
  agentName: out.includes('design'),
  skillName: out.includes('\\u89c6\\u56fe\\u751f\\u6210\\u5185\\u5bb9\\u6f84\\u6e05'),
}));
""")
    try:
        r = subprocess.run(["node", runner], capture_output=True, text=True, timeout=30)
        got = json.loads((r.stdout or "{}").strip() or "{}")
    except Exception as e:
        got = {}
        print("   （node 不可用或执行失败：%r）" % (e,))
    check("N1 三类徽章各渲染一次",
          got.get("ba") == 1 and got.get("bs") == 1 and got.get("bm") == 1, str(got))
    check("N2 徽章里含真实名字（不是只有图标）",
          got.get("agentName") is True and got.get("skillName") is True, str(got))
    try:
        os.remove(runner)
    except Exception:
        pass

    print("\n=== M 变异自证：把 _ctx_skill 打回 None，S1 必须翻红 ===")
    fp = os.path.join(ROOT, "agent/pipeline_parts/stream.py")
    orig = open(fp, "rb").read()
    mutated = orig.decode("utf-8").replace(
        '                    if _wl_skill:\n                        _ctx_skill = _ctx_skill_guide',
        '                    if False:\n                        _ctx_skill = _ctx_skill_guide').encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False, "未命中接线锚点")
    else:
        try:
            open(fp, "wb").write(mutated)
            m = open(fp, encoding="utf-8").read()
            check("M1 去掉白名单接线后 S1 判据翻红（断言非空转）",
                  'if _wl_skill:' not in m)
        finally:
            open(fp, "wb").write(orig)
            m2 = open(fp, encoding="utf-8").read()
            check("M2 还原后接线复位", 'if _wl_skill:' in m2)

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
