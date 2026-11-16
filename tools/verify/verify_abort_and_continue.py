# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""回归判据 · 中断原因判定 + 短续句不澄清（2026-11-06）

锁定两件事（都是米爸实测反馈）：
  A. 「本轮未产出正文」必须按**真实原因**分类，不能再统一写"连接中断或服务端异常"
     —— 事故 conv 990485：真因是被路由到「通用问答Agent」，用户却以为是服务重启。
  B. 短续句（含错一个字）必须**承接上文、不弹澄清**：用户原话
     「我只是写错了字，没事别问题点」。

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_abort_and_continue.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from routers.conversations import _classify_abort            # noqa: E402
from agent.intent import IntentRouter                        # noqa: E402

fails = []


def rec(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + ((" ⇒ " + str(extra)) if extra else ""))
    if not ok:
        fails.append(name)


# ── A. 中止原因分类：四类必须互相区分 ──────────────────────────────
c1, _ = _classify_abort({"agent": "通用问答Agent", "tools": []}, False, "", "stage")
rec("A1 路由到通用问答Agent ⇒ route_chat", c1 == "route_chat", c1)

c2, _ = _classify_abort({"agent": "工程建模方案设计Agent", "tools": [1]}, True, "", "stage")
rec("A2 客户端断开 ⇒ client_gone", c2 == "client_gone", c2)

c3, _ = _classify_abort({"agent": "x", "tools": []}, False, "exception:LLMUnavailableError", "stage")
rec("A3 服务端异常 ⇒ exception", c3 == "exception", c3)

c4, _ = _classify_abort({"agent": "x", "tools": [1, 2]}, False, "", "ping")
rec("A4 其余 ⇒ no_terminal_frame（不再谎称连接中断）", c4 == "no_terminal_frame", c4)

rec("A5 四种原因互不重复", len({c1, c2, c3, c4}) == 4, [c1, c2, c3, c4])

# 文案里不许再出现"连接中断或服务端异常"这种合并口径
src = open(os.path.join(ROOT, "routers", "conversations.py"), encoding="utf-8").read()
rec("A6 旧合并口径已移除",
    "连接中断或服务端异常；执行过程见下方" not in src)
rec("A7 中止原因已结构化落库", 'abort_reason' in src)

# ── B. 短续句：承继上文 + 不澄清（含错字） ─────────────────────────
r = IntentRouter()
CASES = [
    ("继续", "design", "continue", "design", False),
    ("接着往下", "design", "continue", "design", False),
    ("出P1样板", "design", "continue", "design", False),
    ("显出P1样板", "design", "continue", "design", False),   # 错一个字
    ("继续", None, None, None, False),                        # 无上文：不澄清即可
]
for text, prev, want_route, want_intent, want_clarify in CASES:
    r.detect(text, prev_intent=prev)
    m = r._last_meta
    ok_route = (want_route is None) or (m.get("route") == want_route)
    ok_intent = (want_intent is None) or (m.get("intent") == want_intent)
    ok_clar = bool(m.get("needs_clarification")) == want_clarify
    rec("B 续写「%s」(prev=%s) route/intent/clarify" % (text, prev),
        ok_route and ok_intent and ok_clar,
        "%s/%s/%s" % (m.get("route"), m.get("intent"), m.get("needs_clarification")))

# 反例：不能被续写规则劫持
for text, prev, want in [("谢谢", "design", "chat"), ("你好", None, "chat"),
                         ("校验如下代码，是否符合V2代码规范", None, "review")]:
    r.detect(text, prev_intent=prev)
    rec("B 反例「%s」不误判" % text, r._last_meta.get("intent") == want,
        r._last_meta.get("intent"))
    rec("B 反例「%s」不弹澄清" % text, not r._last_meta.get("needs_clarification"))

# ── C. 结构与开关存在（防止"改了但没接线"） ───────────────────────
sp = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"), encoding="utf-8").read()
rec("C1 续写不再降级到通用问答Agent", "continue_uses_main_agent" in sp)
rec("C2 继承登记已补回读分支", "self._team_main_agent = _bound" in sp)

js12 = open(os.path.join(ROOT, "static", "js", "mods", "12-chatsend.js"), encoding="utf-8").read()
rec("C3 前端开流前取 boot_id", "_getBootId()" in js12 and "/api/system/boot" in js12)
rec("C4 前端断流时比对 boot_id", "_annotateAbnormalEnd" in js12)
js11 = open(os.path.join(ROOT, "static", "js", "mods", "11-pipeline.js"), encoding="utf-8").read()
rec("C5 终态帧置位 _sseTerminal", js11.count("window._sseTerminal = Date.now()") >= 2)
mn = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
rec("C6 /api/system/boot 端点存在", '"/api/system/boot"' in mn)

print("\nALL_OK=%s" % (not fails))
if fails:
    print("FAILED:", fails)
sys.exit(0 if not fails else 1)
