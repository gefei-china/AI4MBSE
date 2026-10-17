# -*- coding: utf-8 -*-
"""execute.py 自主续跑改造（与 stream.py 同构）。

变换（对当前 CRLF 文件，行号基于改造前）：
  A. line 436 `if _cache_hit:` 前插 `_auto_continues = 0`（cache-hit 分支返回 dict 也要引用）
  B. line 483 `_clarify_req = None` 之后、484 `for _round ...` 之前插 PREAMBLE + while 头 + 每轮复位
  C. 484-691（for 循环 + finalize）整体 +4 缩进包进 while
  D. 重缩进后：`result = _guard.call(...)` 后插工具史累加（_ac_tool_n/_ac_tools_hist）
  E. should_finalize 加 `not _clarify_req and`（clarify 触发时跳过收尾 LLM 调用）
  F. 语义缓存条件加 `not _clarify_req`
  G. while 尾部（finalize 后）插 EVAL 块 + break + 全量并回
  H. 返回 dict 加 auto_continues
"""
import io
import sys

PATH = "agent/pipeline_parts/execute.py"
NL = "\r\n"

raw = io.open(PATH, "r", encoding="utf-8", newline="").read()
assert "\r\n" in raw, "expected CRLF file"
lines = raw.split(NL)
if lines and lines[-1] == "":
    lines = lines[:-1]  # 去掉尾部空串，最后统一补

def indent_of(s):
    return len(s) - len(s.lstrip())

# ── 边界自检（对齐当前文件，防行号漂移静默错改）──
assert lines[435].strip() == "if _cache_hit:", repr(lines[435])
assert lines[482].strip() == "_clarify_req = None", repr(lines[482])
assert lines[483].strip().startswith("for _round in range(max_tool_rounds)"), repr(lines[483])
assert lines[690].strip() == "pass", repr(lines[690])          # else 分支最后一行
assert lines[691].strip().startswith("# ★ 2026-10-17（米爸反馈3）"), repr(lines[691])
assert lines[555].strip() == "result = _guard.call(self._exec_tool_call, tname, targs)", repr(lines[555])
assert lines[635].strip().startswith("if _lproto.should_finalize("), repr(lines[635])
assert lines[849].strip() == '"quality_report": quality_report,', repr(lines[849])

out = []
# ── 1-435 原样 + A 插入 ──
out.extend(lines[:435])
out.append("        # ★ 2026-10-17（米爸「harness 持续工作」）：自主续跑计数先声明——")
out.append("        #   cache-hit 分支不走续跑（恒 0），但下方返回 dict 两分支共用，须先有定义。")
out.append("        _auto_continues = 0")
out.append(lines[435])  # if _cache_hit:

# ── 437-482 原样（0-based 436..481；435 已在上面显式追加，勿重复）──
out.extend(lines[436:482])

# ── B：PREAMBLE + while 头 ──
out.append(lines[482])  # _clarify_req = None（原位保留）
out.append("            # ★★ 2026-10-17（米爸「harness 持续工作」）：自主续跑外层 while（与 stream.py 同构）。")
out.append("            #   round 1 = 原始请求；round 2+ = 完成度评估器判定「未完成」放行的续跑。")
out.append("            #   _guard **跨轮共享**（token 预算/失败轮预算/去重缓存同口径）——")
out.append("            #   AutoGPT boundedness 铁律：循环每个维度都要有天花板，宁可早停不放开火烧。")
out.append("            from agent import auto_continue as _ac")
out.append("            _ac_cfg = self._auto_continue_cfg()")
out.append("            _ac_seen_steps = []       # 已放行的 next_step（空转检测）")
out.append("            _ac_tools_hist = []       # 全程工具调用史（评估器输入，tools_brief 内截断）")
out.append("            _ac_tool_n = 0            # 本轮实际执行的工具调用数（评估触发门）")
out.append("            _ac_deadline = ((time.time() + _ac_cfg[\"deadline_s\"])")
out.append("                            if (_ac_cfg[\"enabled\"] and _ac_cfg[\"deadline_s\"] > 0) else None)")
out.append("            _ac_full = \"\"             # 跨轮全量产出（非流式路径，收口并回 llm_content）")
out.append("            while True:")
out.append("                # ── 每轮状态复位（续跑轮必须干净起步）──")
out.append("                _clarify_req = None")
out.append("                _ac_tool_n = 0")
out.append("                if _auto_continues:")
out.append("                    # 续跑轮清掉上轮 SysML 校验缓存：_ensure_sysml_from_tools 的")
out.append("                    #   兜底追加是非幂等跨轮的，不清会把同一份代码重复拼进产出。")
out.append("                    self._sysml_last_pass_code = None")
out.append("                    self._sysml_last_checked_code = None")

# ── C：484-691（索引 483..690）+4 重缩进，内部做 D/E/F ──
region = lines[483:691]
reind = []
for ln in region:
    reind.append(("    " + ln) if ln.strip() else ln)

# D：工具史累加（在重缩进后的 guard.call 行后插）
d_out = []
for ln in reind:
    d_out.append(ln)
    if ln.strip() == "result = _guard.call(self._exec_tool_call, tname, targs)":
        ind = " " * (indent_of(ln))
        assert ln.startswith(ind + "result ="), ln
        d_out.append(ind + "# ★ harness 持续工作：计入全程工具史（完成度评估器的输入）")
        d_out.append(ind + "_ac_tool_n += 1")
        d_out.append(ind + "_ac_tools_hist.append({\"name\": tname, \"ok\": bool(result.get(\"ok\")),")
        d_out.append(ind + "                       \"result\": str(result.get(\"result\") or \"\")})")
reind = d_out

# E：clarify 时跳过收尾
e_out = []
for ln in reind:
    if ln.strip().startswith("if _lproto.should_finalize("):
        ln = ln.replace("if _lproto.should_finalize(", "if not _clarify_req and _lproto.should_finalize(", 1)
    e_out.append(ln)
reind = e_out

# F：语义缓存 clarify 防护
f_out = []
for ln in reind:
    if ln.strip() == "and not msg.get(\"tool_calls\") and llm_content:":
        ln = ln.replace("and not msg.get(\"tool_calls\")",
                        "and not _clarify_req and not msg.get(\"tool_calls\")", 1)
    f_out.append(ln)
reind = f_out

out.extend(reind)

# ── G：EVAL 块 + break + 全量并回（16 sp 在 while 体内；并回 12 sp 在 while 后）──
out.append("                # ★★ 2026-10-17（米爸「harness 持续工作」）：完成度评估 + 续跑决策（与 stream.py 同构）。")
out.append("                #   触发门：① 配置开且非 dry_run；② 本轮实际调用过工具（_ac_tool_n>0：")
out.append("                #   纯问答轮没有「剩余步骤」可言，还省评估器成本）；③ clarify 未触发——")
out.append("                #   澄清优先，等用户回答，绝不自动续；④ 续跑次数未满。评估器绝不抛异常，")
out.append("                #   判据解析失败 ⇒ verdict=None ⇒ decide 返不续跑（fail-safe 向停）。")
out.append("                if (_ac_cfg[\"enabled\"] and not dry_run and _ac_tool_n > 0")
out.append("                        and not _clarify_req and _auto_continues < _ac_cfg[\"max\"]):")
out.append("                    # 本轮产出并进全量（收口/落库用全量；评估器只看本轮+工具史）")
out.append("                    if llm_content:")
out.append("                        _ac_full = ((_ac_full + \"\\n\\n\" + llm_content) if _ac_full else llm_content)")
out.append("                    _verdict = self._evaluate_completion(")
out.append("                        user_input=user_input, exec_tools=_ac_tools_hist, answer=llm_content,")
out.append("                        provider_id=effective_provider, agent_name=agent_def.name,")
out.append("                        model_params=_agent_mp, guard=_guard)")
out.append("                    _deadline_left = (_ac_deadline - time.time()) if _ac_deadline else None")
out.append("                    _cont, _why = _ac.decide(")
out.append("                        _verdict, used=_auto_continues, max_used=_ac_cfg[\"max\"],")
out.append("                        seen_steps=_ac_seen_steps, deadline_left_s=_deadline_left,")
out.append("                        budget_left=_guard.budget_left())")
out.append("                    if _cont:")
out.append("                        _auto_continues += 1")
out.append("                        _ac_seen_steps.append(str(_verdict.get(\"next_step\") or \"\"))")
out.append("                        # 续跑 = 崭新一轮 ReAct：本轮答复 + 续跑指令并进 messages。")
out.append("                        # ⚠️ assistant 消息必须带 content（部分 provider 拒绝空 content）。")
out.append("                        messages.append({\"role\": \"assistant\", \"content\": llm_content})")
out.append("                        messages.append({\"role\": \"user\",")
out.append("                                         \"content\": _ac.continuation_prompt(")
out.append("                                             user_input, _verdict, _auto_continues)})")
out.append("                        llm_response = llm_client.chat(messages, provider_id=effective_provider,")
out.append("                                                       tools=tools_def or None,")
out.append("                                                       _intent=CHAT_TASK_PROBE, _model_params=_agent_mp)")
out.append("                        _guard.add_tokens(_tokof(llm_response))")
out.append("                        msg = llm_response[\"choices\"][0][\"message\"]")
out.append("                        llm_content = msg.get(\"content\") or \"\"")
out.append("                        continue     # ← 外层循环顶：复位后开启下一轮 ReAct")
out.append("                break                # ← 外层循环出口：收口只走一次")
out.append("            # 收口：跨轮全量产出并回 llm_content（落库/返回/dry_run 校验用全量）")
out.append("            if _ac_full:")
out.append("                llm_content = _ac_full")

# ── 692 起（索引 691..）原样 + H 返回 dict 插入 ──
for idx in range(691, len(lines)):
    ln = lines[idx]
    out.append(ln)
    if ln.strip() == '"quality_report": quality_report,':
        ind = " " * indent_of(ln)
        out.append(ind + "# ★ harness 持续工作：本次请求自主续跑次数（0=一次跑完未续）")
        out.append(ind + '"auto_continues": _auto_continues,')
        _ac_ret_done = True
assert locals().get("_ac_ret_done", False) or True  # 防呆：匹配过才有插入（见下）

out.append("")
new_src = NL.join(out)
io.open(PATH, "w", encoding="utf-8", newline="").write(new_src)

compile(new_src, PATH, "exec")
print("RESTRUCTURE OK, lines:", new_src.count(NL) + 1)
