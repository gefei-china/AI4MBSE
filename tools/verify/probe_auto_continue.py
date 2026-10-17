# -*- coding: utf-8 -*-
"""自主续跑（auto-continue / harness 持续工作）验证（2026-10-17）。

背景
----
米爸反馈：「整个链路有优化，但是还是存在可持续工作、输出的能力，每执行一轮都会结束，
让我继续下达指令才会进行下一步。作为一款可以长时间工作的 harness 平台，这一点必须支持。」

标杆调研：Claude Code /goal evaluator（模型想停 ≠ 该停，评估器显式判据放行/打回）；
Devin loop-of-death；AutoGPT boundedness 铁律「run until done → run within limits」。

本批改动
--------
· agent/auto_continue.py：纯函数模块（build_eval_messages/parse_verdict/decide/
  is_spinning/continuation_prompt/tools_brief）——四道闸 fail-safe 向「停」。
· agent/pipeline_parts/autopilot.py：AutoPilotMixin（_auto_continue_cfg + 
  _evaluate_completion，唯一 IO 层，绝不抛异常）。
· stream.py / execute.py（两处同构）：外层 while 包住 ReAct+finalize；每轮收尾跑
  完成度评估；未完成且四道闸全过 ⇒ 注入 continuation_prompt 续跑；clarify 优先不自动续。
· common.py：AUTOPILOT_RULES_TEXT 纯追加进 tool_rules（prompt 层「禁止中途停下问继续」）。
· core/config.py：agent.auto_continue_enabled / auto_continue_max / auto_continue_deadline_s。
· 前端 11-pipeline.js：「任务完成度评估 ⚖️」「自主续跑 🔄」环节块。

判据
----
A 纯函数：判据解析（含围栏/残缺 fail-safe）、四道闸逐条、空转检测、续跑指令、工具简报
B IO 层：cfg 读取 fail-safe；_evaluate_completion 正常/异常路径（monkeypatch llm_client）
C 接线：stream/execute 结构标记；prompt 纯追加；前端环节块
D 变异自证：M1 decide 翻转为「恒续跑」⇒ 四道闸用例全红；M2 stream 评估门删除 ⇒ 接线红
"""
import os
import sys
import io as _io
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PASSED, FAILED = [], []


def chk(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -- " + str(detail)) if detail else ""))


def section(t):
    print("\n" + "=" * 4 + " " + t + " " + "=" * 4)


from agent import auto_continue as _ac            # noqa: E402

# ── A. 纯函数 ──────────────────────────────────────────────
section("A. 纯函数（agent/auto_continue.py）")

_v = _ac.parse_verdict('{"done": true}')
chk("A1 parse_verdict done=true", _v == {"done": True}, _v)

_v = _ac.parse_verdict('{"done": false, "next_step": "生成 N3 视图", "remaining": ["a","b"]}')
chk("A2 parse_verdict done=false 完整", bool(_v) and _v.get("done") is False
    and _v.get("next_step") == "生成 N3 视图" and _v.get("remaining") == ["a", "b"], _v)

chk("A3 done=false 缺 next_step ⇒ None（fail-safe）",
    _ac.parse_verdict('{"done": false}') is None)

chk("A4 ```json 围栏容忍",
    _ac.parse_verdict('```json\n{"done": false, "next_step": "x"}\n```') == {"done": False, "next_step": "x", "remaining": []})

chk("A5 垃圾/空输入 ⇒ None（done 真值宽松→停，方向安全）",
    _ac.parse_verdict("") is None and _ac.parse_verdict("不是 JSON") is None
    and _ac.parse_verdict("{}") is None and _ac.parse_verdict("[1]") is None)

_c, _w = _ac.decide({"done": True}, used=0, max_used=3, seen_steps=[])
chk("A6 done ⇒ 停（任务已完成）", _c is False and "完成" in _w, (_c, _w))

_c, _w = _ac.decide(None, used=0, max_used=3, seen_steps=[])
chk("A7 verdict=None ⇒ 停（fail-safe）", _c is False, (_c, _w))

_v = {"done": False, "next_step": "生成依赖视图", "remaining": ["x"]}
_c, _w = _ac.decide(_v, used=1, max_used=3, seen_steps=[])
chk("A8 未完成+四闸全过 ⇒ 续跑", _c is True and "自动继续" in _w, (_c, _w))

_c, _w = _ac.decide(_v, used=3, max_used=3, seen_steps=[])
chk("A9 次数闸：used>=max ⇒ 停", _c is False and "上限" in _w, (_c, _w))

_c, _w = _ac.decide(_v, used=0, max_used=3, seen_steps=[], deadline_left_s=0)
chk("A10 墙钟闸：超时 ⇒ 停", _c is False and "墙钟" in _w, (_c, _w))

_c, _w = _ac.decide(_v, used=0, max_used=3, seen_steps=[], budget_left=False)
chk("A11 token 预算闸 ⇒ 停", _c is False and "预算" in _w, (_c, _w))

_c, _w = _ac.decide(_v, used=0, max_used=3, seen_steps=["生成依赖视图。"])
chk("A12 空转闸：与历史步骤相同 ⇒ 停", _c is False and "空转" in _w, (_c, _w))

chk("A13 normalize_step 标点/大小写不敏感",
    _ac.normalize_step("生成依赖视图。") == _ac.normalize_step("生成依赖 视图") != "")

_p = _ac.continuation_prompt("建 N3 视图", _v, 2)
chk("A14 continuation_prompt 含原始请求/下一步/轮次",
    all(k in _p for k in ("N3 视图", "生成依赖视图", "第 2 次", "ask_user")), _p[:60])

_tb = _ac.tools_brief([{"name": "t%d" % i, "ok": True, "result": "r" * 300} for i in range(20)])
chk("A15 tools_brief 截 12 条 + 单条截断",
    _tb.count("- t") == 12 and all(len(l) < 200 for l in _tb.splitlines()), _tb[:60])

# ── B. IO 层（AutoPilotMixin）────────────────────────────
section("B. IO 层（_auto_continue_cfg / _evaluate_completion）")

from agent.pipeline_parts.autopilot import AutoPilotMixin   # noqa: E402
from core import config as _cfgmod                          # noqa: E402


class _Pipe(AutoPilotMixin):
    pass


_p = _Pipe()
_orig_get, _orig_as_bool = _cfgmod.get, _cfgmod.as_bool
try:
    _cfgmod.get = lambda *a, **k: {"agent.auto_continue_max": 5}.get(
        "%s.%s" % (a[0], a[1]) if len(a) > 1 else "", 3) if a and a[0] == "agent" else _orig_get(*a, **k) if len(a) > 2 else 3
    _cfgmod.get = lambda sec, key, dft=None: 5 if (sec, key) == ("agent", "auto_continue_max") else (
        120 if (sec, key) == ("agent", "auto_continue_deadline_s") else dft)
    _cfgmod.as_bool = lambda sec, key, dft=False: True if (sec, key) == ("agent", "auto_continue_enabled") else dft
    _cfgv = _p._auto_continue_cfg()
    chk("B1 cfg 读取（enabled/max/deadline）",
        _cfgv == {"enabled": True, "max": 5, "deadline_s": 120.0}, _cfgv)
finally:
    _cfgmod.get, _cfgmod.as_bool = _orig_get, _orig_as_bool

# _evaluate_completion：monkeypatch llm_client.chat 返回合法 JSON
import llm as _llmmod   # noqa: E402


class _G:
    def __init__(self):
        self.calls = 0

    def add_tokens(self, n):
        self.calls += 1


def _fake_chat(msgs, **kw):
    assert kw.get("_intent") == "chat_eval", "评估调用必须带 chat_eval intent"
    return {"choices": [{"message": {"content": '{"done": false, "next_step": "补齐依赖清单"}'}}]}


_orig_chat = _llmmod.llm_client.chat
try:
    _llmmod.llm_client.chat = _fake_chat
    _g = _G()
    _v = _p._evaluate_completion(user_input="建视图", exec_tools=[{"name": "t", "ok": True, "result": "r"}],
                                 answer="第一步完成", provider_id="x", guard=_g)
    chk("B2 _evaluate_completion 正常路径（intent=chat_eval + 回填 guard）",
        _v == {"done": False, "next_step": "补齐依赖清单", "remaining": []} and _g.calls == 1, _v)

    def _boom(msgs, **kw):
        raise RuntimeError("429")

    _llmmod.llm_client.chat = _boom
    chk("B3 评估调用抛异常 ⇒ None（绝不带崩主流程）",
        _p._evaluate_completion(user_input="x", exec_tools=[], answer="y", provider_id="x") is None)
finally:
    _llmmod.llm_client.chat = _orig_chat

# ── C. 接线（stream / execute / common / 前端）──────────────
section("C. 接线结构标记")


def _read(p):
    return _io.open(ROOT / p, encoding="utf-8").read()


_s = _read("agent/pipeline_parts/stream.py")
_e = _read("agent/pipeline_parts/execute.py")
_m = _read("agent/pipeline_parts/common.py")
_f = _read("static/js/mods/11-pipeline.js")

chk("C1 stream：外层 while + 评估门 + continue/break + 全量并回 + done 计数",
    all(k in _s for k in (
        "while True:",
        'not dry_run and exec_tools',
        "continue   # ← 外层循环顶",
        "break               # ← 外层循环出口",
        '"auto_continues": _auto_continues,',
        "if _full_content:\n                llm_content = _full_content")),
    )

chk("C2 stream：autopilot reasoning 走 progress 通道（B2-10 约定）",
    '"kind": "progress"' in _s and '"phase": "autopilot"' in _s)

chk("C3 execute：外层 while + 评估门（工具计数/clarify 优先）+ clarify 跳过收尾 + 返回计数",
    all(k in _e for k in (
        "while True:",
        "not dry_run and _ac_tool_n > 0",
        "not _clarify_req and _auto_continues < _ac_cfg",
        "if not _clarify_req and _lproto.should_finalize(",
        '"auto_continues": _auto_continues,',
        "if _ac_full:")),
    )

chk("C4 prompt：AUTOPILOT_RULES_TEXT 纯追加进 tool_rules（其后可续追加其他规则段）",
    'TOOL_RULES_TEXT + ASK_USER_RULES_TEXT' in _m and 'AUTOPILOT_RULES_TEXT' in _m
    and "禁止用『需要我继续吗" in _m)

chk("C5 前端：两个续跑环节块 + 图标",
    all(k in _f for k in ("任务完成度评估", "自主续跑", "'任务完成度评估':'⚖️'", "'自主续跑':'🔄'")))

# ── D. 变异自证 ────────────────────────────────────────────
section("D. 变异自证（判据必须能红）")

import time as _time   # noqa: E402
_MOD = ROOT / "agent" / "auto_continue.py"
_ORIG = _MOD.read_text(encoding="utf-8")


def _mutate(tag, old, new):
    assert old in _ORIG, "mutation anchor missing: %r" % old[:40]
    _MOD.write_text(_ORIG.replace(old, new, 1), encoding="utf-8")
    import importlib
    importlib.reload(_ac)
    print("  [MUTANT] %s" % tag)


try:
    # M1：decide 翻转为「恒续跑」（fail-safe 方向反转 ⇒ 四道闸全失效）
    _mutate("M1 decide 恒续跑",
            "    if not verdict:\n        return False, \"完成度评估失败或无结论，按不续跑处理\"",
            "    if True:\n        return True, \"MUTANT\"")
    _c1, _ = _ac.decide({"done": True}, used=0, max_used=3, seen_steps=[])
    _c2, _ = _ac.decide(None, used=3, max_used=3, seen_steps=[], budget_left=False)
    chk("D-M1 decide 恒续跑 ⇒ A6/A7/A9/A11 全红", _c1 is True and _c2 is True)

    # M2：stream 评估门拆掉（永不评估 ⇒ 永不续跑）
    _MOD.write_text(_ORIG, encoding="utf-8")
    importlib.reload(_ac)
    _c3, _ = _ac.decide({"done": False, "next_step": "x"}, used=0, max_used=3, seen_steps=[])
    chk("D-restore decide 复原 ⇒ 恢复续跑判据", _c3 is True)
finally:
    _MOD.write_text(_ORIG, encoding="utf-8")
    import importlib
    importlib.reload(_ac)

_s_path = ROOT / "agent" / "pipeline_parts" / "stream.py"
_S_ORIG = _s_path.read_text(encoding="utf-8")
try:
    _GATE = 'if (_ac_cfg["enabled"] and not dry_run and exec_tools'
    assert _GATE in _S_ORIG
    _s_path.write_text(_S_ORIG.replace(_GATE, "if (False", 1), encoding="utf-8")
    _s2 = _s_path.read_text(encoding="utf-8")
    chk("D-M2 stream 评估门删除 ⇒ C1 接线判据红", 'not dry_run and exec_tools' not in _s2)
finally:
    _s_path.write_text(_S_ORIG, encoding="utf-8")

import py_compile   # noqa: E402
py_compile.compile(str(ROOT / "agent" / "auto_continue.py"), doraise=True)
py_compile.compile(str(ROOT / "agent" / "pipeline_parts" / "stream.py"), doraise=True)
chk("D-restore 变异后已复原且可编译", True)

# ── 汇总 ──────────────────────────────────────────────────
print("\n" + "=" * 60)
print("PASSED=%d FAILED=%d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("FAILED items:")
    for f in FAILED:
        print("  - " + f)
sys.exit(1 if FAILED else 0)
