# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""门禁 · 「智源对接任务」为何总匹配 chat（2026-11-06，conv 990480/990500 实测）

用户原话：
  ①「我的对接智源的任务还是没完成，我的意图是什么，为什么总是匹配到 chat，
     我明明说了要获取智源的数据」
  ②「agent 绑定 zhiyuan_dev_mcp 字段显示 mcp 下线，你不是验证了是可用的嘛」

本门禁锁住这次定位出的**四个**成因与对应修复，全部是行为级断言（不是字符串好看）：

  A. 平台读取句必须判到 design（含中英混排空格：「V2 代码」）
     事故：`"v2代码" in t` 对「V2 代码」为假 ⇒ 落 rule/knowledge_qa@0.95
     ⇒ 定向给知识问答Agent（读不到工程）。实测 conv 990498。

  B. 团队路由把本轮定向给**兜底 `chat` 成员**时，不得更换执行主体
     事故：`direct · target='chat'` ⇒ intent=chat ⇒「通用问答Agent」(tools=0)
     ⇒ 用户看到"总匹配到 chat"。实测 conv 990500。

  C. 绑定的 MCP 离线时必须**系统明示**（含 401 归因），而不是让模型自称"没找到 MCP"
     事故：凭据过期后连续三轮答"从未成功调用过任何智源工具"。

  D. 保存 Agent 时，**用户没动过绑定就不许删**
     事故：Agent#735「智源对接agent」的 mcp+技能在 16:35:04 被一次"打开就保存"清空，
     而它正是会话 990480 的主 Agent ⇒ 之后无论意图多准都没有工具可用。

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_intent_no_demote.py
"""
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

OK, FAIL = [], []


def rec(name, cond, detail=""):
    (OK if cond else FAIL).append((name, detail))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))


def read(p):
    if p.replace('\\', '/').endswith('pipeline_parts/stream.py'):
        return read_stream_source()
    with io.open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


stream = read("agent/pipeline_parts/stream.py")
session = read("agent/pipeline_parts/session.py")
intent = read("agent/intent.py")
agents_js = read("static/js/mods/30-agents.js")

# ── A. 平台读取句（含空格）判到 design ──
rec("A1 intent.py 平台读取判定已去空白",
    "_t_ns = re.sub(r\"\\s+\", \"\", t)" in intent)
try:
    from agent.intent import IntentRouter
    r = IntentRouter()
    cases = [
        ("我准备通过 MCP 读取工程 AI验证1104 的 V2 代码", "design"),
        ("获取工程「AI验证1104」中 发动机功能架构基线 的V2代码数据", "design"),
        ("重新尝试读取工程的V2代码", "design"),
    ]
    for txt, want in cases:
        r.detect(txt)
        got = r.get_last_meta().get("intent")
        rec("A2 平台读取句→%s（%s…）" % (want, txt[:16]), got == want, got)
    # 反例：不能把"校验/介绍"也吸过来
    for txt, forbid in (("校验如下代码，是否符合V2代码规范", "design"),
                        ("介绍一下SysML v2的作用", "design")):
        r.detect(txt)
        got = r.get_last_meta().get("intent")
        rec("A3 反例不误判 design（%s…）" % txt[:12], got != forbid, got)
except Exception as e:  # pragma: no cover
    rec("A2/A3 意图实测可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ── B. chat 不降级（两个降级点） ──
rec("B1 定义 _has_ctx（会话是否已有上下文）", "_has_ctx = bool(_hist) or" in stream)
rec("B2 `_act == 'chat'` 分支有「不降级」保护",
    '_no_demote = bool(team_intent) and _has_ctx' in stream)
rec("B3 `direct · target='chat'` 分支有「不降级」保护",
    '== "chat"\n                      and _has_ctx and team_intent' in stream)
# team_routed_to="chat" 必须留在 else 内（缩进 24 空格），否则会把归因写反
m = re.search(r"\n( +)self\._team_routed_to = \"chat\"", stream)
rec("B4 self._team_routed_to='chat' 缩进在 else 分支内",
    bool(m) and len(m.group(1)) == 24, (len(m.group(1)) if m else None))

# ── C. 离线 MCP 必须明示 ──
rec("C1 存在 _bound_mcp_offline_note", "def _bound_mcp_offline_note" in session)
rec("C2 stream 在工具构建后调用并推送 progress",
    "_bound_mcp_offline_note(intent, team_intent)" in stream
    and "_off_note" in stream)
rec("C3 文案含 401 归因与修复指引",
    "401 鉴权失败" in session and "重新签发凭据" in session)
try:
    from agent.pipeline import AgentPipeline
    p = AgentPipeline()
    n_off = p._bound_mcp_offline_note("design", "智源对接agent")
    # ⚠️ 取一个**确实没有 MCP 绑定**的 Agent（system_mgmt 也绑了 zhiyuan-dev-mcp，
    #    用它当反例会假红 —— 第一版就踩了，记在这里）
    n_none = p._bound_mcp_offline_note("__no_such_agent__", None)
    # 依赖当前 MCP 状态：offline ⇒ 有提示；online ⇒ 无提示
    import sqlite3
    c = sqlite3.connect("file:" + os.path.join(ROOT, "mbse.db") + "?mode=ro", uri=True)
    st = (c.execute("SELECT status FROM mcp_servers WHERE id=161").fetchone() or ["?"])[0]
    c.close()
    if st == "online":
        rec("C4 在线时**不**误报离线", n_off == "", "status=online")
    else:
        rec("C4 离线时提示含服务名与 401", ("zhiyuan-dev-mcp" in n_off and "401" in n_off),
            n_off[:60])
    rec("C5 无绑定 Agent 不产生噪音提示", n_none == "", repr(n_none[:30]))
except Exception as e:  # pragma: no cover
    rec("C4/C5 离线提示实测可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ── D. 保存 Agent 不得凭未渲染状态删绑定 ──
rec("D1 有 _agentBindDirty 标志", "let _agentBindDirty = false;" in agents_js)
rec("D2 打开弹窗时重置为 false", "_agentBindDirty = false;   // ★" in agents_js)
rec("D3 勾选时置 true", "_agentBindDirty = true;" in agents_js)
rec("D4 syncAgentBindings 删除前有护栏",
    "if(!_agentBindDirty){".replace("(", "\\(").replace("{", "\\{") in agents_js
    or "if(!_agentBindDirty){" in agents_js)

print("\n%s  ALL_OK=%s  (%d passed / %d failed)"
      % ("=" * 60, not FAIL, len(OK), len(FAIL)))
if FAIL:
    for n, d in FAIL:
        print("  ✗", n, "|", d)
    sys.exit(1)
