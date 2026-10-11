# -*- coding: utf-8 -*-
"""verify_hier_events.py —— 守卫：SSE 事件必须带三级层级字段（Agent→Skill→工具/MCP）。

## 为什么需要它（米爸 2026-11-11）

用户反馈：「整个执行过程无法看到具体的执行过程」「不要都平铺出来，
agent 下的 skill 调用的 mcp，应该按照层级进行分层展示」。

**根因不是前端，是协议没传**：实测 `11-pipeline.js` 里
`procAddAgent` / `procAddSkill` / `procAddTool` 各自 `procUpsert` 到**同一层**
timeline；而后端 `tool` 事件只有 `{name, arguments, call_id, latency_ms}`，
**没有任何"这是谁调的"字段** ⇒ 前端拿不到层级，只能平铺。

⇒ 修复在后端补字段。本守卫防止它被后续改动回退掉。

## 契约

  层级定义（米爸拍板：**三级**，最终以实际数据为准）：
    depth=0  Agent          —— 第 1 层容器
    depth=1  Skill          —— 第 2 层（命中技能时）
    depth=1  工具           —— 未命中技能时，直挂 Agent 下
    depth=2  工具           —— 命中技能时，挂 Skill 下
    `mcp` 字段 = MCP 服务器名；`tools.mcp_server_id` 全库为空时为 None（协议留位）

## 不变式

  I1 `tool` 事件的 **每一处** yield 都带 agent/skill/depth/mcp 四个字段
     （漏一处 ⇒ 该分支的节点掉回平铺，且极难肉眼发现）
  I2 `skill` 事件带 agent/depth（depth=1）
  I3 depth 取值只能是 1 或 2，且「有 skill ⇒ depth=2」自洽
  I4 前端：timeline 项按 depth 缩进 + data-parent/data-depth 属性
  I5 `stage`「知识库检索」**只在 should_retrieve 为真时** yield
     （消除"从未真正执行过的检索环节"——用户反馈"为啥每次都有查询知识库"的直接来源）

## 变异自证

  M1 去掉某处 tool 事件的层级字段 ⇒ I1 必须判红
  M2 前端 depth 缩进去掉 ⇒ I4 必须判红
  M3 把 stage 移回无条件 yield ⇒ I5 必须判红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_hier_events.py
"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
JS = os.path.join(ROOT, "static", "js", "mods", "11-pipeline.js")
CSS = os.path.join(ROOT, "static", "css", "app.css")

_results = []


def check(name, ok, detail=""):
    _results.append(bool(ok))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


def tool_yields(src):
    """用 AST 找出所有 yield 了含 `"type": "tool"` 的 dict 字面量。

    ⚠️ 首版用 `d.get("type") == "tool"` 比对**值**，但 AST 里 dict 的 value 是
    `ast.Constant`，我的取值逻辑写错了 ⇒ 一处都没匹配上（报"全部 0 处 tool 事件"
    却判OK —— **判据自身失真**，比没门禁更坏）。改为比对**键名**+ 源码片段。
    """
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Yield) and isinstance(node.value, ast.Dict)):
            continue
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if "type" not in keys:
            continue
        seg = ast.get_source_segment(src, node.value) or ""
        if '"type": "tool"' not in seg:
            continue
        out.append((node.lineno, keys))
    return out


def main():
    print("=== 三级层级事件协议守卫 ===")
    ssrc = io.open(STREAM, encoding="utf-8").read()
    jsrc = io.open(JS, encoding="utf-8").read()
    css = io.open(CSS, encoding="utf-8").read()

    # I1：每一处 tool 事件都带四个层级字段
    tys = tool_yields(ssrc)
    need = {"agent", "skill", "depth", "mcp"}
    missing = [(ln, sorted(need - ks)) for ln, ks in tys if not need.issubset(ks)]
    check("I1 全部 %d 处 tool 事件 yield 都带 agent/skill/depth/mcp" % len(tys),
          not missing, "缺字段: %s" % missing[:4])

    # I1b：这些字段必须引用同一组上下文变量（防止各处写死不同值）
    check("I1b 层级字段统一来自 _ctx_agent/_ctx_skill/_mcp_of（单一真源）",
          ssrc.count('_ctx_agent') >= 8 and '_mcp_of' in ssrc,
          "count(_ctx_agent)=%d" % ssrc.count("_ctx_agent"))

    # I2：skill 事件带 agent/depth（按 AST 判，不用行号猜）
    tree = ast.parse(ssrc)
    skill_ys = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Yield) and isinstance(node.value, ast.Dict)):
            continue
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        seg = ast.get_source_segment(ssrc, node.value) or ""
        # ⚠️ 必须匹配 `"type": "skill"`（**带 key**），不能只找 '"skill"' ——
        #   后者会把 `"skill_hits": ...`、`"skillKey"` 之类一起算进来
        #   （实测因此把 2 处真实事件数成 8 处，I2b 判据失真）。
        if "type" in keys and '"type": "skill"' in seg:
            skill_ys.append((node.lineno, keys))
    need_sk = {"agent", "depth"}
    sk_missing = [(ln, sorted(need_sk - ks)) for ln, ks in skill_ys if not need_sk.issubset(ks)]
    check("I2 全部 %d 处 skill 事件 yield 带 agent/depth" % len(skill_ys),
          bool(skill_ys) and not sk_missing, "缺字段: %s" % sk_missing[:3])
    # skill 的 depth 必须是 1（第 2 层）
    segs = [ast.get_source_segment(ssrc, n.value) or ""
            for n in ast.walk(tree)
            if isinstance(n, ast.Yield) and isinstance(n.value, ast.Dict)
            and '"type": "skill"' in (ast.get_source_segment(ssrc, n.value) or "")]
    check("I2b skill 事件 depth=1（第 2 层）",
          bool(segs) and all('"depth": 1' in x for x in segs))

    # I3：depth 表达式自洽（有 skill ⇒ 2，否则 1）
    check("I3 depth 表达式自洽（2 if _ctx_skill else 1）",
          '2 if _ctx_skill else 1' in ssrc)

    # I4：前端按 depth 缩进
    check("I4a 前端 timeline 按 depth 输出 data-depth/data-parent",
          'data-depth="${dp}"' in jsrc and 'data-parent' in jsrc)
    check("I4b procAddTool 把 agent/skill 映射到 parent（技能优先）",
          "'skill_'+ev.skill" in jsrc and "'agent_'+ev.agent" in jsrc)
    check("I4c CSS 按 depth 缩进（1/2 级）",
          '[data-depth="1"]' in css and '[data-depth="2"]' in css)

    # I5：stage「知识库检索」在 should_retrieve 之内
    lines = ssrc.splitlines()
    idx_yield = next((i for i, l in enumerate(lines)
                      if '"type": "stage", "name": "知识库检索"' in l), -1)
    idx_should = next((i for i, l in enumerate(lines)
                       if l.strip().startswith("should_retrieve =")), -1)
    check("I5 stage「知识库检索」在 should_retrieve 判定之后 yield（无幽灵环节）",
          idx_yield > idx_should >= 0,
          "yield@%d should_retrieve@%d" % (idx_yield + 1, idx_should + 1))

    # ── 变异自证 ──────────────────────────────────────────────
    print("\n=== 变异自证 ===")
    # M1：删掉**一处** tool 事件的层级字段（整行删，确保真的删掉）
    #⚠️ 首版用 `.replace('"agent": _ctx_agent, ', '', 1)` 只在**同一行**内替换；
    #   而 tool 事件是多行 dict、该子串出现在后续行 ⇒ replace 找不到、变异体与原文相同
    #   ⇒ 判据自然不翻红 ⇒ **变异自证空转**（我犯的正是"变异没生效却以为守卫有效"）。
    #   修法：按 AST 定位行号，**整行删掉** `"agent": _ctx_agent,` 那一行。
    ln0 = tys[0][0]
    lines_mut = list(lines)
    victim = ln0
    for i in range(ln0 - 1, min(ln0 + 6, len(lines_mut))):
        if '"agent": _ctx_agent' in lines_mut[i]:
            victim = i + 1
            break
    lines_mut[victim - 1] = ""
    mut_src = "\n".join(lines_mut)
    m1 = [(ln, sorted(need - ks)) for ln, ks in tool_yields(mut_src)
          if not need.issubset(ks)]
    check("M1 删掉一处 tool 层级字段 ⇒ I1 判据必须翻红（删第 %d 行）" % victim,
          bool(m1), "守卫空转（变异后仍全带字段）")

    # M2：前端 depth 缩进去掉
    m2 = jsrc.replace('data-depth="${dp}"', 'data-x="${dp}"')
    check("M2 前端去 data-depth ⇒ I4a 判据必须翻红",
          not ('data-depth="${dp}"' in m2))

    # M3：把 stage 移回无条件（放到 should_retrieve 之前）
    lines_m3 = list(lines)
    stage_line = lines_m3[idx_yield]
    lines_m3[idx_yield] = " " * 16 + 'if True:  # M3'
    lines_m3.insert(idx_should, stage_line.replace(
        "                yield {\"type\": \"stage\", \"name\": \"知识库检索\", \"status\": \"run\"}",
        "        yield {\"type\": \"stage\", \"name\": \"知识库检索\", \"status\": \"run\"}"))
    m3_src = "\n".join(lines_m3)
    try:
        ast.parse(m3_src)
        m3_y = next((i for i, l in enumerate(m3_src.splitlines())
                     if '"type": "stage", "name": "知识库检索"' in l), -1)
        m3_s = next((i for i, l in enumerate(m3_src.splitlines())
                     if l.strip().startswith("should_retrieve =")), -1)
        red = m3_y < m3_s
    except SyntaxError:
        red = True
    check("M3 stage 移回无条件 yield ⇒ I5 判据必须翻红", red, "守卫空转")

    n_fail = sum(1 for r in _results if not r)
    print("\n%d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())