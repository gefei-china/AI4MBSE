# -*- coding: utf-8 -*-
"""一次性结构改造：stream.py 包一层「自主续跑」外层循环（2026-10-17 米爸 harness 需求）。

改造点（全部带边界断言，漂移即报错）：
  A. try 内早处初始化 _full_content=""（异常/断开路径引用，防 NameError）
  B. tools_def 构建后：自主续跑就绪（配置/计数/墙钟/_tools_def_full 快照）
  C. _lg 创建后：外层 while True（12 层）+ 每轮状态复位（16 层）
  D. 2426-2905 原样右缩进 4 空格（成为外层循环体，16 层起）
  E. 外层循环体尾：完成度评估 + 四道闸决策 + continue/break（**16 层起——在 while 体内**）
  F. 外层循环后：全量产出覆写 llm_content（12 层——循环外）

⚠️ 缩进纪律（上一版事故教训）：外层 while 在 12 层 ⇒ 它的体（含 EVAL_BLOCK 与
   break）必须在 16 层；只有循环**之后**的 TAIL 才回到 12 层。EVAL_BLOCK 写成
   12 层会落到循环外 ⇒ 'continue' not properly in loop。
"""
import io

PATH = (r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
        r"\agent\pipeline_parts\stream.py")

with io.open(PATH, "r", encoding="utf-8", newline="") as f:
    raw = f.read()
NL = "\r\n" if "\r\n" in raw else "\n"
lines = raw.split(NL)


def L(n):                      # 1-based 取行
    return lines[n - 1]


# ── 边界断言（防脚本与文件漂移）──────────────────────────────────────
assert L(1781).strip() == 'llm_content = ""', repr(L(1781))
assert L(2399).strip() == "tools_def = self._build_tools_def(intent, user_input, user)", repr(L(2399))
assert L(2400).strip() == "tool_injected = False", repr(L(2400))
assert L(2425).strip().endswith("max_failed_rounds=max_failed_rounds)"), repr(L(2425))
assert L(2426).strip().startswith("# ★ 2026-11-11"), repr(L(2426))
assert L(2905).strip() == 'yield {"type": "token", "delta": _chunk}', repr(L(2905))
assert L(2906).strip().startswith("# SysML v2 视图联动"), repr(L(2906))

# ── B. 自主续跑就绪（tools_def 构建后、tool_injected 前）────────────
PREAMBLE = [
    '            # ★ 2026-10-17（米爸「harness 持续工作」）：自主续跑（auto-continue）就绪。',
    '            #   为什么需要外层循环：模型「想停」和「该停」是两件事——没有完成度评估器时，',
    '            #   多步任务模型常在第一步后就收尾等用户吩咐（米爸原话：「每执行一轮都会结束，',
    '            #   让我继续下达指令才会进行下一步」）。方案对齐 Claude Code /goal：每轮工作结束后',
    '            #   由**完成度评估器**判定放行还是打回；四道闸（次数/墙钟/token/空转）全部',
    '            #   fail-safe 向「停」（AutoGPT「无完成任务生成/无上限成本」教训的沉淀）。',
    '            #   判定纯函数在 agent/auto_continue.py（decide/is_spinning/parse_verdict，可单测），',
    '            #   IO（config 读取 + LLM 评估调用）在 AutoPilotMixin（pipeline_parts/autopilot.py）。',
    '            from agent import auto_continue as _ac',
    '            _auto_continues = 0',
    '            _ac_cfg = self._auto_continue_cfg()',
    '            _ac_seen_steps = []',
    '            _ac_deadline = ((time.time() + _ac_cfg["deadline_s"])',
    '                            if (_ac_cfg["enabled"] and _ac_cfg["deadline_s"] > 0) else None)',
    '            _tools_def_full = tools_def   # 每轮续跑恢复完整工具集（offload 注入/预算闸会改它）',
]

# ── C. 外层循环头 + 每轮状态复位（while 在 12 层，体在 16 层）───────
OUTER_HEAD = [
    '            # ★ 2026-10-17（米爸「harness 持续工作」）：自主续跑外层循环。',
    '            #   round 1 = 原始请求；round 2+ = 评估器判定「未完成」放行的续跑。',
    '            #   _lg（LoopGuard）**故意在循环外创建**：token 预算跨轮共享（config 既定语义），',
    '            #   失败轮预算与去重缓存同样跨轮保持——宁可早停，不放开火烧（boundedness 铁律）。',
    '            while True:',
    '                # ── 每轮状态复位（续跑轮必须干净起步，否则带上轮半成品）──',
    '                tool_injected = False',
    '                llm_content = ""',
    '                _ctx_skill = None           # None = 未命中技能（工具直接挂 agent 下）',
    '                _clarify_req = None',
    '                _rnd = -1',
    '                _prev_tool_idx = []',
    '                _fu_budget = 3              # 续跑轮同样限 3 次补充（与首轮同口径）',
    '                _fu_used = 0',
    '                tools_def = _tools_def_full  # 恢复完整工具集（offload 注入/预算闸会改它）',
    '                if _auto_continues:',
    '                    # 续跑轮清掉上轮 SysML 校验缓存：否则本轮 prose 答复会把**同一份代码**',
    '                    #   再次拼进全量产出（_ensure_sysml_from_tools 的兜底追加是非幂等跨轮的）。',
    '                    self._sysml_last_pass_code = None',
    '                    self._sysml_last_checked_code = None',
]

# ── E. 完成度评估 + 续跑决策（**16 层起——外层 while 体内**）─────────
EVAL_BLOCK = [
    '                # ★ 2026-10-17（米爸「harness 持续工作」）：完成度评估 + 续跑决策。',
    '                #   触发门三重（全满足才评估）：① 配置开且非 dry_run；② 本轮**实际调用过工具**',
    '                #   （exec_tools 非空——纯问答轮没有「剩余步骤」可言，且省评估器成本）；',
    '                #   ③ 续跑次数未满。评估器本身绝不抛异常（_evaluate_completion 内部吞），',
    '                #   判据解析失败 ⇒ verdict=None ⇒ decide 返不续跑（fail-safe 向停）。',
    '                if (_ac_cfg["enabled"] and not dry_run and exec_tools',
    '                        and _auto_continues < _ac_cfg["max"]):',
    '                    # 本轮产出并进全量（落库/done/前端历史用全量；评估器只看本轮+工具史）',
    '                    if llm_content:',
    '                        _full_content = ((_full_content + "\\n\\n" + llm_content)',
    '                                         if _full_content else llm_content)',
    '                    _verdict = self._evaluate_completion(',
    '                        user_input=user_input, exec_tools=exec_tools, answer=llm_content,',
    '                        provider_id=effective_provider, agent_name=agent_def.name,',
    '                        model_params=_agent_mp, guard=_lg)',
    '                    _deadline_left = (_ac_deadline - time.time()) if _ac_deadline else None',
    '                    _cont, _why = _ac.decide(',
    '                        _verdict, used=_auto_continues, max_used=_ac_cfg["max"],',
    '                        seen_steps=_ac_seen_steps, deadline_left_s=_deadline_left,',
    '                        budget_left=_lg.budget_left())',
    '                    # 评估发生过就透传（无论结论）——用户看得到「为什么停 / 为什么继续」',
    '                    try:',
    '                        yield {"type": "stage", "name": _ac.AUTO_EVAL_STAGE,',
    '                               "status": "done", "detail": _why}',
    '                    except Exception:',
    '                        pass',
    '                    if _cont:',
    '                        _auto_continues += 1',
    '                        _ac_seen_steps.append(str(_verdict.get("next_step") or ""))',
    '                        # 续跑 = 崭新的一轮 ReAct：本轮答复 + 续跑指令并进 messages。',
    '                        # ⚠️ assistant 消息必须带 content（部分 provider 拒绝空 content）。',
    '                        messages.append({"role": "assistant", "content": llm_content})',
    '                        messages.append({"role": "user",',
    '                                         "content": _ac.continuation_prompt(',
    '                                             user_input, _verdict, _auto_continues)})',
    '                        _ac_delta = f"（harness 持续工作）{_why}"',
    '                        exec_reasoning.append(_ac_delta)',
    '                        yield {"type": "reasoning", "delta": _ac_delta,',
    '                               "round": _auto_continues, "phase": "autopilot"}',
    '                        try:',
    '                            yield {"type": "stage", "name": _ac.AUTO_CONTINUE_STAGE,',
    '                                   "status": "done",',
    '                                   "detail": f"第 {_auto_continues}/{_ac_cfg[\'max\']} 次："',
    '                                             f"{str(_verdict.get(\'next_step\') or \'\')[:80]}"}',
    '                        except Exception:',
    '                            pass',
    '                        continue   # ← 外层循环顶：状态复位后开启下一轮 ReAct',
    '                break               # ← 外层循环出口：收口（落库/done 只走一次）',
]

# ── F. 外层循环后：全量产出覆写（12 层——循环外）────────────────────
TAIL = [
    '            # 落库/done 用跨轮全量产出（续跑轮内容已在评估点并进 _full_content）',
    '            if _full_content:',
    '                llm_content = _full_content',
]

# ── 组装：单遍顺序输出，到锚点行时插入对应块 ─────────────────────────
out = []
handled_body = False
for i, ln in enumerate(lines, start=1):
    if 2426 <= i <= 2905:
        if not handled_body:
            # 2426..2905 缩进后整体接在 OUTER_HEAD 之后（OUTER_HEAD 在 2425 行尾已插入）
            for bl in lines[2425:2905]:
                out.append(("    " + bl) if bl.strip() else bl)
            out.extend(EVAL_BLOCK)
            out.extend(TAIL)
            handled_body = True
        continue                                    # 原始 2426..2905 已处理，跳过
    out.append(ln)
    if i == 1781:                                   # A. _full_content 早初始化
        out.append('            # ★ 2026-10-17（米爸「harness 持续工作」）：跨自主续跑轮的完整产出。'
                   '早初始化是为了异常/断开路径（下方 except）引用不炸。')
        out.append('            _full_content = ""')
    elif i == 2399:                                 # B. 自主续跑就绪
        out.extend(PREAMBLE)
    elif i == 2425:                                 # C. 外层循环头 + 复位
        out.extend(OUTER_HEAD)
assert handled_body, "body range not handled"

# ── 落盘前自校验：编译 + 环边界断言 ─────────────────────────────────
src = NL.join(out)
try:
    compile(src, PATH, "exec")
except SyntaxError as e:
    raise SystemExit(f"RESTRUCTURE PRODUCED INVALID PYTHON @ {e.lineno}: {e.msg}")
# 断言 EVAL_BLOCK 的 continue/break 在外层 while 体内（16 层）
_wi = next(k for k, l in enumerate(out) if l.strip() == "while True:" and l.startswith("            while"))
_ci = next(k for k, l in enumerate(out) if l.strip().startswith("continue") and "外层循环顶" in l)
_bi = next(k for k, l in enumerate(out) if l.strip().startswith("break") and "外层循环出口" in l)
assert _wi < _ci < _bi, (_wi, _ci, _bi)
for k in (_ci, _bi):
    ind = len(out[k]) - len(out[k].lstrip(" "))
    assert ind == 24 if k == _ci else ind == 16, (k, ind)

with io.open(PATH, "w", encoding="utf-8", newline="") as f:
    f.write(src)
print("OK: stream.py restructured, total lines:", len(out), "NL:", repr(NL))
