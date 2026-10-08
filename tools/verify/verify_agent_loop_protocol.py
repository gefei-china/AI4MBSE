# -*- coding: utf-8 -*-
"""Agent Loop 协议门禁（2026-10-07）—— 并行 tool_calls 配对 + 截断参数拒绝。

覆盖 `docs/AgentLoop优化方案-对标行业标杆-20261007.md` §1（并行配对）/ §2（截断拒绝）。

**为什么必须有这个门禁**：这两个缺陷都**不会报错、不会崩**，只是
① 配对缺失 ⇒ 下一轮 HTTP 400（模型请求被协议层拒绝）；
② 截断丢参 ⇒ 工具以空参数执行（可能误用默认行为）。
两者都是「跑得看起来正常、实际结果是错的」，靠读代码很难发现 ⇒ 必须有断言锁住。

运行：`python tools/verify/verify_agent_loop_protocol.py`
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.loop_protocol import (MAX_TOOLS_PER_ROUND,  # noqa: E402
                                is_truncated_args, skipped_results,
                                split_calls, truncated_result)

PASS, FAIL = [], []


def ck(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


def _calls(n, arg='{"q":1}'):
    """造 n 笔tool_call（形态同 OpenAI：`function.arguments` 是 **JSON 字符串**）。"""
    return [{"id": "callu_%02d" % i,
             "function": {"name": "tool_%d" % i, "arguments": arg}}
            for i in range(n)]


print("== A. 协议不变量：每个 tool_call 都有配对的 tool_result ==")
for n in (1, 3, 4, 6, 10):
    calls = _calls(n)
    ex, sk = split_calls(calls)
    res = [{"tool_call_id": c["id"]} for c in ex] + skipped_results(sk)
    got = [r["tool_call_id"] for r in res]
    want = [c["id"] for c in calls]
    missing = set(want) - set(got)
    ck(not missing,
       "A%d %d 笔调用 ⇒ 配对完整（缺失 %d）" % (n, n, len(missing)))

print("\n== B. 上限仍是护栏（不能因为补结果就放开执行数） ==")
calls = _calls(10)
ex, sk = split_calls(calls)
ck(len(ex) == MAX_TOOLS_PER_ROUND, "B1 执行数仍受限（%d）" % MAX_TOOLS_PER_ROUND)
ck(len(sk) == 10 - MAX_TOOLS_PER_ROUND, "B2 未执行数正确")
ck(all(r["content"].find("请勿重试") >= 0 for r in skipped_results(sk)),
   "B3 ★补的提示语含「请勿重试」——否则模型下一轮会重发，变成真死循环")
_b = json.loads(skipped_results(sk)[0]["content"])
ck(_b.get("skipped") is True and _b.get("skip_reason") == "round_tool_limit",
   "B4 带 skipped/skip_reason 标记（前端要显示「未执行」而非「失败」）")
# ⚠️ 原因码在 `skip_reason` **字段**里，不在 result 文案里（第一版断言查错位置 → 假 FAIL）
ck(str(MAX_TOOLS_PER_ROUND) in _b["result"] and "上限" in _b["result"],
   "B5 提示语点明上限值与原因（上限值在文案，原因码在 skip_reason 字段）")

print("\n== C. 截断判别（三分法，最容易误杀的一类） ==")
ck(is_truncated_args({"arguments": '{"path":"/x'}) is True, "C1 非法JSON ⇒ 截断")
ck(is_truncated_args({"arguments": ""}) is True, "C2 空串 ⇒ 截断")
ck(is_truncated_args({"arguments": "   "}) is True, "C3 空白 ⇒ 截断")
ck(is_truncated_args({}) is True, "C4 无 arguments 字段 ⇒ 截断")
ck(is_truncated_args({"arguments": '{"path":"/x.md"}'}) is False, "C5 完整 JSON ⇒ 不截断")
ck(is_truncated_args({"arguments": "{}"}) is False,
   "C6 ★★ 合法空参数**不算截断**（模型确实可能发无参调用）—— 误判会误杀正常请求")
ck(is_truncated_args({"arguments": '{"a":1}'}) is False, "C7 合法单字段 ⇒ 不截断")
ck(is_truncated_args({"arguments": {"path": "/x"}}) is False,
   "C8 dict 形态（已结构化）⇒ 不截断")
_tr = truncated_result("graph_retrieve")
ck(_tr["ok"] is False and _tr.get("error") == "args_truncated", "C9 截断结果标记 ok=False")
ck("截断" in _tr["result"] and "重新发起" in _tr["result"],
   "C10 截断文案说明原因且要求重发（模型据此自我修复）")
ck("拒绝执行" in _tr["result"], "C11 截断文案明确「拒绝执行」而非静默")

print("\n== D. 两处接线同源（防「补一边忘一边」） ==")
_sp = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
_ex = os.path.join(ROOT, "agent", "pipeline_parts", "execute.py")
_s = open(_sp, encoding="utf-8").read()
_e = open(_ex, encoding="utf-8").read()
ck("from agent import loop_protocol as _lproto" in _s, "D1 stream.py 导入 loop_protocol")
ck("from agent import loop_protocol as _lproto" in _e, "D2 execute.py 导入 loop_protocol")
ck("split_calls" in _s and "skipped_results" in _s, "D3 stream.py 用到 split+补结果")
ck("split_calls" in _e and "skipped_results" in _e, "D4 execute.py 用到 split+补结果")
ck("is_truncated_args" in _s and "is_truncated_args" in _e, "D5 两处都接了截断判别")
# ★ 顺序：补结果必须在 `if not tool_results:` 之前 —— 全被跳过时也要有结果
# ⚠️ 用**代码行**比较而非 find()：注释里也含这些字符串（第一版 find 命中了注释 → 假 FAIL）。
_s_lines = _s.splitlines()
_e_lines = _e.splitlines()


def _line_of(lines, needle):
    """返回**代码行**（跳过注释行）里首次出现 needle 的下标，找不到返回 -1。"""
    for i, ln in enumerate(lines):
        if needle in ln and not ln.strip().startswith("#"):
            return i
    return -1


_i_s = _line_of(_s_lines, "skipped_results(_skip_calls)")
_i_guard = _line_of(_s_lines, "if results:")
ck(0 <= _i_s < _i_guard,
   "D6 ★stream 补结果在 `if results:` 之前（全跳过时也不悬空）")
_i_e = _line_of(_e_lines, "skipped_results(_skip_calls)")
_i_eg = _line_of(_e_lines, "if not tool_results:")
ck(0 <= _i_e < _i_eg,
   "D7 ★execute 补结果在 `if not tool_results:` 之前")
# ★ 执行循环本身不得再用 [:3] 切片（根因）。
#   ⚠️ `plan = ... for tc in ptool_calls[:3]`（**仅拼提示文案**）保留是对的，
#   它不参与执行；第一版断言用「全文不含」把这条也判红了（假 FAIL）。
ck(_line_of(_s_lines, "for _ti, tc in enumerate(_exec_calls):") >= 0
   and _line_of(_s_lines, "for _ti, tc in enumerate(ptool_calls[:3]):") < 0,
   "D8 ★stream 执行循环用 _exec_calls（根因已除）")
ck(_line_of(_e_lines, "for tc in _exec_calls:") >= 0
   and _line_of(_e_lines, "for tc in tool_calls[:3]") < 0,
   "D9 ★execute 执行循环用 _exec_calls（根因已除）")
# 收敛判定分母必须是执行数
ck("weak_count == len(_exec_calls)" in _s, "D10 ★stream 弱结果收敛分母用执行数")
ck("_weak_n == len(_exec_calls)" in _e, "D11 ★execute 弱结果收敛分母用执行数")
# 截断分支必须 continue（不执行工具）
ck(skipped_results and "continue" in _s, "D12 截断/跳过分支不执行工具（continue）")

print("\n== E. 变异自证（证明门禁会判红） ==")
_lp = os.path.join(ROOT, "agent", "loop_protocol.py")
_src = open(_lp, encoding="utf-8").read()


def _mut(src, old, new):
    assert old in src, "变异锚点未命中：%r" % old[:50]
    ns = {"__name__": "mutant"}
    exec(compile(src.replace(old, new, 1), "lp.py", "exec"), ns)
    return ns


# M1: 上限放大到 100（模拟"直接删掉切片"）⇒ A 组/B 组应判红
try:
    m1 = _mut(_src, "MAX_TOOLS_PER_ROUND = 3", "MAX_TOOLS_PER_ROUND = 100")
    e1, s1 = m1["split_calls"](_calls(6))
    ck(len(e1) == 6 and not s1,
       "M1 变异体上限=100 ⇒ 6 笔全执行（确认变异生效：失控形态）")
    ck(len(split_calls(_calls(6))[0]) == 3, "M1 对照:真实实现仍限 3 笔")
except AssertionError as e:
    ck(False, str(e))

# M2: 补结果时丢掉 tool_call_id（协议配对失效）⇒ A 组应判红
try:
    m2 = _mut(_src, '"tool_call_id": (tc or {}).get("id", ""),', '"tool_call_id": "",')
    r2 = m2["skipped_results"](_calls(6)[3:])
    ck(all(r["tool_call_id"] == "" for r in r2),
       "M2 变异体补结果丢 tool_call_id（确认变异生效：配对失效）")
    ck(all(r["tool_call_id"] for r in skipped_results(_calls(6)[3:])),
       "M2 对照:真实实现逐字回显 id")
except AssertionError as e:
    ck(False, str(e))

# M3: 把"合法空参数"也判成截断（过度拒绝）⇒ C6 应判红
try:
    m3 = _mut(_src, "        json.loads(raw)\n        return False",
              "        json.loads(raw)\n        return True")
    ck(m3["is_truncated_args"]({"arguments": "{}"}) is True,
       "M3 变异体把合法空参数判成截断（确认变异生效：会误杀正常请求）")
    ck(is_truncated_args({"arguments": "{}"}) is False,
       "M3 对照:真实实现不误杀无参调用")
except AssertionError as e:
    ck(False, str(e))

# M4: skipped 不带提示语 ⇒ B3 应判红
try:
    m4 = _mut(_src, '**请勿重试同一个调用**', '（无提示）')
    r4 = m4["skipped_results"](_calls(6)[3:])
    ck("请勿重试" not in r4[0]["content"], "M4 变异体无「请勿重试」提示（确认变异生效）")
    ck("请勿重试" in skipped_results(_calls(6)[3:])[0]["content"],
       "M4 对照:真实实现带防重试提示")
except AssertionError as e:
    ck(False, str(e))


# ─────────────────────────────────────────────────────────────────────────────
# F组：★ 端到端跑**真实 ReAct 循环**（进程内注入桩，驱动真实 execute_stream/execute）
#
# ★ 为什么 A~E 组还不够：A~E 全是「单元 + 源码文本」断言，而这两个缺陷的本质是
#   「跑得看起来正常、实际结果是错的」。曾出现过：门禁 41/41 全绿，
#   但真实链路的 skipped 分支**一次都没触发过**（tool_call_logs 里 skipped 行数 = 0）
#   ⇒ 绿不等于分支被走到过。
#   本组用桩替换 llm.llm_client，**驱动真实 pipeline 生成器**，
#   断言真实 SSE 事件流里确实出现「执行 3 + skipped N」与「截断被拒」。
#   走 tmp 库副本，不碰生产数据。
# ─────────────────────────────────────────────────────────────────────────────
print("\n== F. ★端到端：驱动真实 ReAct 循环 ==")
import shutil
import tempfile

_SRC_DB = os.path.join(ROOT, "mbse.db")
_TMP_DB = os.path.join(tempfile.gettempdir(), "_loop_e2e.db")
_have_db = os.path.exists(_SRC_DB)
ck(_have_db, "F0 生产库存在（端到端需真实会话/工具元数据）")

if _have_db:
    try:
        shutil.copyfile(_SRC_DB, _TMP_DB)
    except Exception as _ce:
        print("    (复制库失败：%s)" % _ce)
        _TMP_DB = _SRC_DB
    # ★★★切库必须改 **`database.connection.DB_PATH`**，不能只设环境变量。
    #   实测（2026-10-07）：`database/connection.py:8` 是 `from core.config import DB_PATH`
    #   —— **模块级 import**，路径在 import 时就固化了。
    #   本文件第 20 行 `from agent.loop_protocol import ...` 已触发 agent 包 ⇒
    #   agent/__init__.py 链式 import database ⇒ **DB_PATH 早已指向生产库**。
    #   此时再 `os.environ["MBSE_DB_PATH"]=...` **完全无效** ⇒
    #   F/G 组会把桩产出的 messages / tool_call_logs **写进生产库**。
    #   （实测已污染 126 条消息 + 20 条工具日志，2026-10-07 清理。）
    import database.connection as _dbc
    _old_db_env = os.environ.get("MBSE_DB_PATH")
    _old_db_path = _dbc.DB_PATH
    os.environ["MBSE_DB_PATH"] = _TMP_DB
    _dbc.DB_PATH = _TMP_DB
    # 前置断言：确认真的切走了（否则下面所有断言都可能"看着通过、实际写生产库"）
    ck(_dbc.DB_PATH == _TMP_DB,
       "F0b ★已切库到tmp 副本（改 connection.DB_PATH 而非仅环境变量）")
    try:
        import llm as _llm

        class _Stub:
            """按「是否传了 tools」判定探测轮；其余轮返回收尾正文。"""

            def __init__(self, calls):
                self._calls = calls
                self._sent = False
                self.seen = []

            def _audit(self, messages):
                """按 OpenAI/Anthropic 校验器口径检查配对。"""
                for i, m in enumerate(messages):
                    if not isinstance(m, dict):
                        continue
                    tcs = m.get("tool_calls") or []
                    if not tcs:
                        continue
                    want = {tc["id"] for tc in tcs}
                    got = set()
                    for j in range(i + 1, len(messages)):
                        nx = messages[j]
                        if not isinstance(nx, dict):
                            break
                        if nx.get("role") == "tool":
                            got.add(nx.get("tool_call_id"))
                        elif nx.get("role") in ("assistant", "user"):
                            break
                    self.seen.append((len(want), len(got), sorted(want - got)))

            def chat(self, messages, provider_id=None, stream=False, tools=None, **kw):
                self._audit(messages)
                if tools and not self._sent:
                    self._sent = True
                    return {"choices": [{"message": {"role": "assistant", "content": "",
                                                     "tool_calls": self._calls},
                                         "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                      "total_tokens": 150}}
                txt = "已处理。"
                if stream:
                    def _g(t=txt):
                        for ch in t:
                            yield {"choices": [{"delta": {"content": ch}}]}
                    return _g()
                return {"choices": [{"message": {"role": "assistant", "content": txt},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 10,
                                  "total_tokens": 60}}

        _USER = {"id": 1, "username": "verify", "display_name": "verify",
                 "permissions": {"agent_tool": ["read", "write"]}}
        # ★★ 打桩必须打**类方法**，不能替换 `llm.llm_client` 实例。
        #   原因（实测踩到）：`agent/pipeline_parts/common.py:20` 等 6 处都在
        #   **模块级** `from llm import llm_client` ⇒ 实例在 import 时已被绑进
        #   各模块全局。此后替换 `llm.llm_client` 对已导入模块**完全无效**。
        #   （本门禁第 20 行就 import 了 loop_protocol ⇒ 早已触发 agent 包导入。）
        #   替换类方法 `LLMClient.chat` ⇒ 所有绑定点自动生效。
        _REAL_CHAT = _llm.LLMClient.chat
        _CUR_STUB = {"obj": None}

        def _class_chat(self, messages, **kw):
            st = _CUR_STUB["obj"]
            if st is None:
                return _REAL_CHAT(self, messages, **kw)
            return st.chat(messages, **kw)

        _llm.LLMClient.chat = _class_chat

        def _use(stub):
            _CUR_STUB["obj"] = stub

        # ── F1/F2：6 笔并行 ⇒ 执行 3 + skipped 3，协议零悬空 ──
        _calls6 = [{"id": "e2e_%02d" % i, "type": "function",
                    "function": {"name": "graph_retrieve",
                                 "arguments": json.dumps({"query": "q%d" % i})}}
                   for i in range(6)]
        _stub = _Stub(_calls6)
        _use(_stub)
        _done, _run_n, _skip_n = [], 0, 0
        try:
            from agent.pipeline import AgentPipeline as _AP
            for _ev in _AP().execute_stream("查询需求包结构", 325, branch="dev", provider_id=1,
                                             user=_USER):
                if _ev.get("type") == "tool":
                    if _ev.get("status") == "run":
                        _run_n += 1
                    else:
                        _done.append(_ev)
                        if _ev.get("skipped"):
                            _skip_n += 1
        except Exception as _e:
            print("    (execute_stream 异常：%s: %s)" % (type(_e).__name__, str(_e)[:90]))
        ck(_run_n == MAX_TOOLS_PER_ROUND,
           "F1 ★真实循环只执行 %d 笔（上限未被放开）" % MAX_TOOLS_PER_ROUND)
        ck(_skip_n == 6 - MAX_TOOLS_PER_ROUND,
           "F2 ★真实循环为尾部 %d 笔补出 skipped 结果" % (6 - MAX_TOOLS_PER_ROUND))
        _miss = [m for _a, _b, m in _stub.seen if m]
        ck(bool(_stub.seen) and not _miss,
           "F3 ★真实 messages 序列协议零悬空（否则下轮 HTTP 400）")

        # ── F4/F5/F6：截断三分法在真实循环里 ──
        _calls3 = [{"id": "e2e_tr", "type": "function",
                    "function": {"name": "graph_retrieve", "arguments": '{"query":"需求包1'}},
                   {"id": "e2e_em", "type": "function",
                    "function": {"name": "conflict_check", "arguments": "{}"}},
                   {"id": "e2e_ok", "type": "function",
                    "function": {"name": "validate", "arguments": '{"target":"x"}'}}]
        _stub2 = _Stub(_calls3)
        _use(_stub2)
        _ev2 = []
        try:
            from agent.pipeline import AgentPipeline as _AP2
            for _e2 in _AP2().execute_stream("校验模型", 325, branch="dev", provider_id=1,
                                             user=_USER):
                if _e2.get("type") == "tool" and _e2.get("status") == "done":
                    _ev2.append(_e2)
        except Exception as _e:
            print("    (execute_stream 异常：%s: %s)" % (type(_e).__name__, str(_e)[:90]))
        ck(any(x.get("error") == "args_truncated" for x in _ev2),
           "F4 ★截断参数在真实循环里被拒（标记 args_truncated）")
        ck(any(x.get("name") == "conflict_check" and x.get("ok") for x in _ev2),
           "F5 ★合法空参数在真实循环里正常执行（未被误判为截断）")
        _m2 = [m for _a, _b, m in _stub2.seen if m]
        ck(not _m2, "F6 ★截断场景协议同样零悬空")

        # ── F7：execute.py 冷路径（非流式）同样配对 ──
        _calls5 = [{"id": "e2e_c_%02d" % i, "type": "function",
                    "function": {"name": "graph_retrieve",
                                 "arguments": json.dumps({"query": "c%d" % i})}}
                   for i in range(5)]
        _stub3 = _Stub(_calls5)
        _use(_stub3)
        try:
            from agent.pipeline import AgentPipeline as _AP3
            _AP3().execute("查询需求包结构", 325, branch="dev", provider_id=1, user=_USER)
        except Exception as _e:
            print("    (execute 异常：%s: %s)" % (type(_e).__name__, str(_e)[:90]))
        ck(_stub3.seen and not [m for _a, _b, m in _stub3.seen if m],
           "F7 ★execute.py 冷路径协议零悬空（两条路径同源）")
        _CUR_STUB["obj"] = None
        _llm.LLMClient.chat = _REAL_CHAT
    finally:
        # ⚠️ 这里**故意不**还原 DB_PATH —— G 组在 F 组之后还要跑端到端，
        #   提前还原会让 G 组写进生产库（同"桩提前还原"是同一类作用域错误）。
        #   统一在 G 组之后（`_restore_db()`）还原。
        if _old_db_env is None:
            os.environ.pop("MBSE_DB_PATH", None)
        else:
            os.environ["MBSE_DB_PATH"] = _old_db_env
        # ⚠️ 这里**故意不删副本库** —— G 组还要用它（G12/G14 在其后执行）。
        #   提前删 ⇒ G 组报 `no such table: messages`（实测踩到）。
        #   与"DB_PATH 不提前还原"同源：**资源的生命周期必须覆盖所有使用者**。


# ─────────────────────────────────────────────────────────────────────────────
# G 组：P1-① 轮次用尽收尾（执行计划 §3）
# ─────────────────────────────────────────────────────────────────────────────
print("\n== G. P1-① 轮次用尽收尾（不静默返回半成品） ==")
from agent.loop_protocol import (FINALIZE_HINT, finalize_messages,  # noqa: E402
                                should_finalize)

# G1/G2 判定条件
ck(should_finalize({}, "", 3, 3) is True, "G1 轮次用尽 + 空正文 ⇒ 需要收尾")
ck(should_finalize({}, "已有正文", 3, 3) is False,
   "G2 有正文**不**收尾（否则用户看到重复内容）")
ck(should_finalize({}, "", 2, 3) is False,
   "G3 中途 break（轮次未用尽）**不**收尾（多花一次调用）")
ck(should_finalize({}, "   ", 3, 3) is True, "G4 空白正文视为空（strip 语义）")

# G5 不改原messages（★主流程 messages 后续要写卡片/落库）
_m0 = [{"role": "user", "content": "q"}]
_mf = finalize_messages(_m0)
ck(len(_m0) == 1 and len(_mf) == 2, "G5finalize_messages 不改原列表（防污染主流程）")
ck(_mf[-1]["role"] == "user" and FINALIZE_HINT in _mf[-1]["content"],
   "G6 追加 user 收尾指令且内容可辨识")
ck(all(m.get("role") != "assistant" for m in _mf[-1:]),
   "G7 不追加 assistant 空消息（部分 provider 对tool_call 后的空 assistant 敏感）")

# G8 接线（源码级：两条路径都要有）
_e_s = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"), encoding="utf-8").read()
_e_e = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"), encoding="utf-8").read()
ck("finalize_messages(messages)" in _e_s, "G8 stream 收尾走 finalize_messages（同源）")
ck("should_finalize(" in _e_e and "finalize_messages(messages)" in _e_e,
   "G9 ★execute 冷路径接入收尾（此前**完全没有**，实测返回 content 长度 0）")
# ★ 收尾调用不得传 tools（否则模型又要一轮工具 ⇒ 死循环）
_i_fin_e = _e_e.find("finalize_messages(messages)")
ck("tools=None" in _e_e[_i_fin_e:_i_fin_e + 400],
   "G10 ★execute 收尾调用不带 tools（物理上无法再请求工具）")
_i_fin_s = _e_s.find("finalize_messages(messages)")
ck(_i_fin_s > 0 and "stream=True" in _e_s[_i_fin_s:_i_fin_s + 300],
   "G11 stream 收尾调用不传 tools（chat(..., stream=True) 无 tools 参数）")


# ── G12/G13：★端到端（复用 F 组已装配的类方法桩）──
# 场景：模型连续 3 轮都要工具、content 恒空 ⇒ 轮次用尽
# ★ F 组的 finally 已把 `_CUR_STUB["obj"]` 还原为 None（桩已撤），
#   所以这里**不能**用 "obj 非空" 当守卫—— 那是 F 组自己清掉的。
#   守卫只判「库在 + 类方法桩装配过」，由 _use() 自行重挂/还原。
if _have_db and "_use" in dir():
    # ★★ 必须**重新挂桩 + 重新切库**：
    #   ·桩：F 组的 finally 已把 `LLMClient.chat` 还原成真实方法，
    #   这里若不重挂，G 组的桩**一次都不会被调用** ⇒ G12/G14 看着"通过"
    #   （真实 LLM 走Mock 兜底也能给正文），而 G13/G15 永远False。
    #   ⇒ 门禁自己"看起来在测端到端"，实际测的是真实 LLM 兜底路径（假覆盖）。
    _llm.LLMClient.chat = _class_chat
    # ★★ 必须**重新切库**：F 组的 finally 已把 `DB_PATH` 还原成生产库，
    #   而 G 组还要跑 execute/execute_stream（会写 messages / tool_call_logs）
    #   ⇒ 不重切就会**再次污染生产库**（实测 4 条）。
    #   ★ 与"桩被提前还原"是同一类错误：**状态边界与代码块边界不对齐**。
    import database.connection as _dbc2
    _dbc2.DB_PATH = _TMP_DB
    # ★★ 夹具的 query **必须是强相关**（实测：`graph_retrieve` 对
    #   「工程包结构查询」返回"未检索到实质相关内容" ⇒ 命中**弱结果收敛**分支，
    #   循环第 1 轮就 break ⇒ **永远走不到轮次用尽** ⇒ G13 永远 False，
    #   而G12/G14 却"看着通过"（弱收敛里也有 tools=None 的收尾调用）。
    #   「SysML v2 需求追溯」实测命中 20 条向量 ⇒ 强相关 ⇒ 循环能走满 3 轮。
    #   ⇒ 端到端夹具必须先确认"目标分支真的可达"，否则测的是另一条分支。
    _G_Q = "SysML v2 需求追溯"

    def _cap_stub(tag):
        _c = [{"id": "%s_%02d" % (tag, i), "type": "function",
               "function": {"name": "graph_retrieve",
                            "arguments": json.dumps({"query": _G_Q})}}
              for i in range(3)]

        class _Cap:
            # ★ 用 `calls` 列表记**每一次**调用，判据取 any-wins。
            #   上一版用 `self.got_hint`（last-wins）⇒ 收尾轮之后还有调用
            #   （_ensure_sysml / 语义缓存 / card 构建）把标志覆盖成 False ⇒ 假FAIL。
            def __init__(self, calls):
                self._calls = calls
                self.trace = []
                self._n = 0

            def chat(self, messages, provider_id=None, stream=False, tools=None, **kw):
                last = str((messages[-1] or {}).get("content") or "") if messages else ""
                self.trace.append((bool(tools), stream, last))
                # ★★ **每次**带 tools 的探测都返回 tool_calls（不是只有第一次）——
                #   否则第 2 轮探测就返回 stop，循环在 `if not tool_calls: break`
                #   提前退出 ⇒ **永远走不到轮次用尽** ⇒ 收尾分支不可达（假FAIL）。
                #   id 必须每轮唯一（协议要求 tool_call_id 不重复）。
                if tools:
                    self._n += 1
                    _c = [{"id": "%s_r%d_%02d" % (tag, self._n, i), "type": "function",
                           "function": {"name": "graph_retrieve",
                                        "arguments": json.dumps({"query": _G_Q})}}
                          for i in range(3)]
                    return {"choices": [{"message": {"role": "assistant", "content": "",
                                                     "tool_calls": _c},
                                         "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                      "total_tokens": 150}}
                if stream:
                    def _g():
                        yield 'data: %s\n\n' % json.dumps(
                            {"choices": [{"delta": {"content": "已给出最终结论。"}}]},
                            ensure_ascii=False)
                        yield 'data: [DONE]\n\n'
                    return _g()
                return {"choices": [{"message": {"role": "assistant",
                                                 "content": "已给出最终结论。"},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 8,
                                  "total_tokens": 18}}

        return _Cap(_c)          # ★ 必须传 calls（我漏了参数，__init__ 形参没默认值）

    # G12：execute.py 冷路径（此前实测返回空 content）
    _cap1 = _cap_stub("gE")
    _use(_cap1)
    _r = None
    try:
        from agent.pipeline import AgentPipeline as _APG
        _r = _APG().execute("查询需求包结构", 325, branch="dev", provider_id=1, user=_USER)
    except Exception as _e:
        print("    (execute 异常：%s: %s)" % (type(_e).__name__, str(_e)[:80]))
    _txt = str((_r or {}).get("content") or "")
    ck(len(_txt.strip()) > 0, "G12 ★execute 轮次用尽后 content 非空（修复前长度 0）")
    ck(any((not t) and ("已达上限" in c) for t, _s, c in _cap1.trace),
       "G13 ★execute 收尾请求带「已达上限」指令（模型知道要总结）")

    # G14：stream.py 路径同样带提示且产出正文
    _cap2 = _cap_stub("gS")
    _use(_cap2)
    _tok = 0
    try:
        from agent.pipeline import AgentPipeline as _APG2
        for _ev in _APG2().execute_stream("查询需求包结构", 325, branch="dev",
                                           provider_id=1, user=_USER):
            if _ev.get("type") == "token":
                _tok += len(str(_ev.get("delta") or ""))
    except Exception as _e:
        print("    (execute_stream 异常：%s: %s)" % (type(_e).__name__, str(_e)[:80]))
    ck(_tok > 0, "G14 ★stream 轮次用尽后仍产出正文")
    ck(any((not t) and ("已达上限" in c) for t, _s, c in _cap2.trace),
       "G15 ★stream 收尾请求带「已达上限」指令")
    _use(None)

# ══ 全部端到端（F + G）跑完，最后统一还原 DB_PATH / 桩 / 副本库 ══
if _have_db and "_use" in dir():
    import database.connection as _dbc2
    _dbc2.DB_PATH = _old_db_path
    _llm.LLMClient.chat = _REAL_CHAT
    _use(None)
    try:
        if os.path.exists(_TMP_DB) and _TMP_DB != _SRC_DB:
            os.remove(_TMP_DB)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# H 组：P1-② follow-up 队列（只做阶段 1：轮次边界消费，不做 steering）
# ─────────────────────────────────────────────────────────────────────────────
print("\n== H. P1-② follow-up 队列与消费 ==")
from agent import followup as _fuv  # noqa: E402

_fuv.drop(999001)
ck(_fuv.push(999001, "只要顶层包") is True, "H1 push 成功")
ck(_fuv.pending_count(999001) == 1, "H2 pending_count 不消费")
ck(_fuv.pop(999001) == "只要顶层包", "H3 pop 取回原内容")
ck(_fuv.pop(999001) == "" and _fuv.pending_count(999001) == 0,
   "H4 ★取完即删（不会被重复消费）")
# 隔离
_fuv.push(999001, "A")
_fuv.push(999002, "B")
ck(_fuv.pop(999001) == "A" and _fuv.pop(999002) == "B", "H5 ★按会话隔离（不跨会话泄漏）")
# 有界
for i in range(_fuv.MAX_QUEUE_LEN + 3):
    _fuv.push(999003, "q%d" % i)
ck(_fuv.pending_count(999003) == _fuv.MAX_QUEUE_LEN, "H6 单会话条数有上限（防内存膨胀）")
ck("q%d" % (_fuv.MAX_QUEUE_LEN + 2) in _fuv.pop(999003), "H7 超限丢最旧、保留最新")
# 边界
ck(_fuv.push(999004, "   ") is False, "H8 空白文本拒收")
ck(_fuv.push(0, "x") is False, "H9 会话号 0 拒收（不建脏槽位）")
_fuv.push(999005, "x" * (_fuv.MAX_ITEM_LEN + 500))
ck(len(_fuv.pop(999005)) <= _fuv.MAX_ITEM_LEN + 8, "H10 超长截断而非丢弃")
_fuv.drop(999001), _fuv.drop(999002), _fuv.drop(999003), _fuv.drop(999005)

# ── 接线（源码级：两处消费点 + 收尾清理 + 归属校验）──
_h_s = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"), encoding="utf-8").read()
ck(_fuv.__name__ in _h_s or "from agent import followup as _fup" in _h_s,
   "H11 stream.py 导入 follow-up 队列")
ck(_h_s.count("_fup.pending_count(") >= 2,
   "H12 ★两个消费点：循环头 + 最终生成前（缺后者则输出中补充静默失效）")
ck("_fup.drop(conversation_id)" in _h_s,
   "H13 ★每轮收尾 drop（否则残留会漏给下一轮）")
ck("_fup.drop(conversation_id)" in _h_s[_h_s.find("except GeneratorExit"):],
   "H14 ★断连路径也 drop（否则下次接续会消费到上轮残留）")
_r = open(os.path.join(ROOT, "routers", "conversations.py"), encoding="utf-8").read()
ck("/api/conversations/{conv_id}/followup" in _r, "H15 投递端点已注册")
ck("SELECT user_id FROM conversations WHERE id=?" in _r,
   "H16 ★端点校验会话归属（agent 是模块级单例，队列按 conv 分槽）")
ck("无权向该会话投递指令" in _r, "H17 归属不符返回 403（越权注入防护）")
ck("_fu_budget = 3" in _h_s, "H18 有消费次数上限（防用户狂点导致无限循环）")


# ── H19-H23：前端接线（否则后端能力 =没人用的死代码）──
_j = open(os.path.join(ROOT, "static", "js", "mods", "12-chatsend.js"),
          encoding="utf-8").read()
ck("/followup" in _j, "H19 ★前端已调用 /followup 端点（无调用方 = 死代码）")
ck("_el.dataset.convId" in _j or "dataset.convId" in _j,
   "H20 ★用流式现场 convId 而非 currentConvId（切会话时会投错）")
ck("已作为补充指令提交" in _j, "H21 投递成功有用户可见反馈")
ck("补充指令提交失败" in _j,
   "H22 ★投递失败有 toast 且回退旧行为（不能静默吞掉用户输入）")
ck("commitPartialStream" in _j,
   "H23 回退路径仍固化上一轮已生成内容（不丢已有产出）")

print("\n" + "=" * 68)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("HAS FAILURE")
    for f in FAIL:
        print("  x " + f)
    sys.exit(1)
print("ALL GREEN")
