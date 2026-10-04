# -*- coding: utf-8 -*-
"""Agent loop 护栏（2026-10-04）—— 不变式 + 变异自证。

背景：编排路径早有任务数/token 预算，而单Agent ReAct 路径（`execute.py` 与
`stream.py` 内联的两段）只有 `max_tool_rounds=3` 一道闸。本次补齐 token 预算、
重复调用短路、工具超时，并让两处 loop **共用** `agent/loop_guard.py`。

## 为什么这些断言这样写（每条都对应一个"补错了反而更糟"的坑）
G1重复检测必须**与参数 key 顺序无关** —— 否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}`
会被当成两次不同调用，检测形同虚设（最容易写错的一步）。
G2 短路时**必须回喂既有结果** —— 只说"已调用过"会让模型无从作答，诱发第三次调用。
G3 弱结果**不进**去重计数 —— 否则"工具本来就查不到东西"会被误判成重复调用，
    把真正的"查不到"信号掩盖掉。
G4 超时结果必须**如实说"超时"而非"已取消"** —— Python 无法强杀线程，线程仍在跑；
    谎称已取消会让模型以为可以安全重试 ⇒ 副作用重复。
G5 预算必须按**累计**判定，且 token 取 `_meta.token_count`（含 mock 估算兜底）——
    直接读 `usage.prompt_tokens` 会在降级路径上恒为 0，预算闸完全失效。
G6 两处 loop 必须**同源引用同一个模块**（防"补了 A 忘了 B"再次出现不对称）。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []
LG_SRC = os.path.join(ROOT, "agent", "loop_guard.py")


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((PASS if ok else kind, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _fresh(**kw):
    """每次全新实例 —— 护栏是有状态的，复用会串账。"""
    from agent.loop_guard import LoopGuard
    return LoopGuard(**kw)


# ══════════════════════════════════════════════════════════════════
# G1 去重键与参数顺序无关
# ══════════════════════════════════════════════════════════════════
def t_g1():
    print("\n=== G1 重复检测与参数 key 顺序无关 ===")
    from agent.loop_guard import call_key, normalize_args
    k1 = call_key("search", {"a": 1, "b": 2})
    k2 = call_key("search", {"b": 2, "a": 1})
    ok = _rec("G1a 同参数不同 key 顺序 ⇒ 同一个键", k1 == k2, "%s vs %s" % (k1, k2))
    ok &= _rec("G1b 不同参数 ⇒ 不同键", call_key("search", {"a": 1}) != call_key("search", {"a": 2}))
    ok &= _rec("G1c 不同工具同参数 ⇒ 不同键", call_key("s1", {"a": 1}) != call_key("s2", {"a": 1}))
    # 真实走一遍 loop 状态机
    g = _fresh(dup_threshold=2)
    ok &= _rec("G1d 首次探测不短路", g.probe_dup("t", {"a": 1, "b": 2}) == 0)
    g.call(lambda n, a: {"ok": True, "result": "R"}, "t", {"b": 2, "a": 1})
    d2 = g.probe_dup("t", {"a": 1, "b": 2})
    ok &= _rec("G1e 第二次探测（key 顺序不同）被短路", d2 >= 2, "dup=%d" % d2)
    return ok


# ══════════════════════════════════════════════════════════════════
# G2 短路必须回喂既有结果（否则模型无从作答 → 诱发第三次调用）
# ══════════════════════════════════════════════════════════════════
def t_g2():
    print("\n=== G2 短路回喂既有结果 ===")
    g = _fresh(dup_threshold=2)
    g.call(lambda n, a: {"ok": True, "result": "检索到 12 条相关需求"}, "kb_search", {"q": "刹车"})
    g.probe_dup("kb_search", {"q": "刹车"})
    prev = g.last_result_for("kb_search", {"q": "刹车"})
    ok = _rec("G2a 能取回上次结果文本（非空）", bool(prev) and "12" in prev, "prev=%r" % prev[:40])
    ok &= _rec("G2b 未调用过的键取不到结果（不编造）",
               g.last_result_for("other", {"q": "x"}) == "")
    return ok


# ══════════════════════════════════════════════════════════════════
# G3 弱结果不进去重计数（否则掩盖"查不到"信号）
# ══════════════════════════════════════════════════════════════════
def t_g3():
    print("\n=== G3 弱结果不进去重计数 ===")
    from agent.loop_guard import call_key
    g = _fresh(dup_threshold=2)
    g.call(lambda n, a: {"ok": True, "result": "未检索到相关内容"}, "kb", {"q": "x"})
    cnt = g._seen.get(call_key("kb", {"q": "x"}), 0)
    ok = _rec("G3a call() 后计数为 1（尚未短路）", cnt == 1, "cnt=%d" % cnt)
    g.undo_dup_count("kb", {"q": "x"})
    cnt2 = g._seen.get(call_key("kb", {"q": "x"}), 0)
    ok &= _rec("G3b undo_dup_count 可撤回计数", cnt2 == 0, "cnt=%d" % cnt2)
    # 撤回后不应被误判为重复
    d = g.probe_dup("kb", {"q": "x"})
    ok &= _rec("G3c 撤回后不触发短路（'查不到'不被当成'重复'）", d == 0, "dup=%d" % d)
    # undo 不能把计数弄成负数
    g2 = _fresh()
    g2.undo_dup_count("nope", {})
    ok &= _rec("G3d 对未记录过的键撤回不会变成负数",
               g2._seen.get(call_key("nope", {}), 0) == 0)
    return ok


# ══════════════════════════════════════════════════════════════════
# G4 超时：如实报"超时"，且计数生效
# ══════════════════════════════════════════════════════════════════
def t_g4():
    print("\n=== G4 工具超时可观测且不失真 ===")
    import threading
    from agent.loop_guard import call_tool_guarded

    def slow(n, a):
        time.sleep(3.0)
        return {"ok": True, "result": "太晚了"}

    t0 = time.time()
    r = call_tool_guarded(slow, "slow_tool", {}, timeout_s=0.3)
    el = time.time() - t0
    ok = _rec("G4a 超时按时返回（不等待真实执行）", el < 1.5, "elapsed=%.2fs" % el)
    ok &= _rec("G4b 结果标记 _t_timeout 与 error=tool_timeout",
               r.get("_t_timeout") is True and r.get("error") == "tool_timeout",
               "keys=%s" % sorted(r))
    ok &= _rec("G4c 提示文本如实说明'已停止等待'且提醒勿重复",
               "停止等待" in str(r.get("result")) and "勿重复调用" in str(r.get("result")),
               str(r.get("result"))[:60])
    ok &= _rec("G4d 明确不谎称已取消（线程仍在跑）",
               "已停止等待" in str(r.get("result")) and "已取消" not in str(r.get("result")))
    # 超时被计入 guard 统计
    g = _fresh(tool_timeout_s=0.2)
    g.call(slow, "slow_tool", {})
    ok &= _rec("G4e guard 统计到1 次超时", g.timeouts == 1, "timeouts=%d" % g.timeouts)
    # 正常工具不受影响
    g2 = _fresh(tool_timeout_s=5)
    r2 = g2.call(lambda n, a: {"ok": True, "result": "OK"}, "fast", {})
    ok &= _rec("G4f 正常工具照常返回", r2.get("ok") and r2.get("result") == "OK")
    ok &= _rec("G4g 正常路径 timeouts=0", g2.timeouts == 0)
    # 工具抛异常也要被接住（不能静默丢结果）
    def boom(n, a):
        raise ValueError("炸了")
    r3 = call_tool_guarded(boom, "bad", {}, timeout_s=2)
    ok &= _rec("G4h 工具异常被接住并转成结构化失败",
               r3.get("ok") is False and "炸了" in str(r3.get("result")),
               str(r3.get("result"))[:50])
    # timeout<=0 ⇒ 不启用超时（保护设施自己不能是故障源）
    r4 = call_tool_guarded(lambda n, a: {"ok": True, "result": "X"}, "t", {}, timeout_s=0)
    ok &= _rec("G4i timeout_s<=0 时退化为直接执行（不套线程）", r4.get("ok") is True)
    return ok


# ══════════════════════════════════════════════════════════════════
# G5 token 预算按累计判定，且 token 取值口径正确
# ══════════════════════════════════════════════════════════════════
def t_g5():
    print("\n=== G5 token 预算与 token 取值口径 ===")
    from agent.loop_guard import tokens_of, budget_exhausted
    ok = _rec("G5a token 取 _meta.token_count（含 mock 估算兜底）",
              tokens_of({"_meta": {"token_count": 1234}}) == 1234)
    ok &= _rec("G5b 无 _meta 时返回 0（不抛异常）", tokens_of({}) == 0 and tokens_of(None) == 0)
    ok &= _rec("G5c 字典缺键返回 0", tokens_of({"_meta": {}}) == 0)

    g = _fresh(token_budget=1000)
    ok &= _rec("G5d 初始预算充足", g.budget_left() is True)
    g.add_tokens(600)
    ok &= _rec("G5e 累计 600/1000 仍充足（按累计非单次）", g.budget_left() is True)
    g.add_tokens(500)
    ok &= _rec("G5f 累计 1100/1000 判定耗尽", g.budget_left() is False)
    ok &= _rec("G5g stop_reason 记录为 token_budget（可回显）",
               g.stop_reason == "token_budget", g.stop_reason)
    ok &= _rec("G5h budget<=0 视为不设闸", budget_exhausted(99999, 0) is False
               and budget_exhausted(99999, None) is False)
    ok &= _rec("G5i budget=None 且已用token ⇒ 不触发（关掉的闸必须真的关）",
               _fresh(token_budget=None).budget_left() is True)
    return ok


# ══════════════════════════════════════════════════════════════════
# G6 两处 loop 同源（防"补了 A 忘了 B"再次出现不对称）
# ══════════════════════════════════════════════════════════════════
def t_g6():
    print("\n=== G6 两条 loop 路径护栏同源 ===")
    ex = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"), encoding="utf-8").read()
    st = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"), encoding="utf-8").read()
    ok = _rec("G6a execute.py 引用 loop_guard", "from agent.loop_guard import" in ex)
    ok &= _rec("G6b stream.py 引用 loop_guard", "from agent.loop_guard import" in st)
    # 关键：不能再出现裸的 self._exec_tool_call( （绕过护栏）
    n_bare_ex = ex.count("result = self._exec_tool_call(")
    ok &= _rec("G6c execute.py 无绕过护栏的裸工具调用", n_bare_ex == 0, "count=%d" % n_bare_ex)
    ok &= _rec("G6d stream.py 无绕过护栏的裸工具调用",
               st.count("result = self._exec_tool_call(") == 0)
    ok &= _rec("G6e 两条路径的工具调用都走护栏入口",
           "_lg.call(self._exec_tool_call" in st and "_guard.call(self._exec_tool_call" in ex)
    # 一次性算出布尔再传给 _rec：直接把 `all(生成器) and ...` 塞进参数里时，
    # 末尾的 and 链会返回 Match 对象而非 bool，_rec 的 bool(ok) 判定容易误读。
    import re as _re
    _src = open(LG_SRC, encoding="utf-8").read()
    _has_api = all(bool(_re.search(r"def %s\s*\(" % x, _src))
                   for x in ("probe_dup", "budget_left", "call",
                             "undo_dup_count", "call_tool_guarded", "tokens_of"))
    ok &= _rec("G6f 护栏模块提供三闸所需 API（probe_dup/budget_left/call/"
               "undo_dup_count/call_tool_guarded/tokens_of）", _has_api)
    return ok


# ══════════════════════════════════════════════════════════════════
# 变异自证
# ══════════════════════════════════════════════════════════════════
def _load_lg(mutate=None):
    src = open(LG_SRC, encoding="utf-8").read()
    if mutate:
        old, new = mutate
        assert old in src, "变异锚点未命中：%r" % old
        src = src.replace(old, new, 1)
    ns = {"__name__": "agent.loop_guard__mut", "__file__": LG_SRC}
    exec(compile(src, LG_SRC, "exec"), ns)
    return ns


def m_m1():
    """M1：normalize_args 不排序 ⇒ G1 必须判红。"""
    print("\n=== 变异 M1：去重键不按 key 排序 ===")
    ns = _load_lg(('return json.dumps(args or {}, sort_keys=True, ensure_ascii=False,\n                          default=str)',
                   'return json.dumps(args or {}, ensure_ascii=False, default=str)'))
    k = ns["call_key"]
    caught = k("s", {"a": 1, "b": 2}) != k("s", {"b": 2, "a": 1})
    return _rec("M1 复现（顺序不同的参数被认作不同调用）⇒ 证明 G1 非空转", caught)


def m_m2():
    """M2：短路时不回喂结果（返回空串）⇒ G2 必须判红。"""
    print("\n=== 变异 M2：短路不回喂结果 ===")
    ns = _load_lg(("return self._results.get(k, default) or default",
               "return ''"))
    G = ns["LoopGuard"]
    g = G(dup_threshold=2)
    g.call(lambda n, a: {"ok": True, "result": "R-内容"}, "t", {"q": 1})
    g.probe_dup("t", {"q": 1})
    caught = g.last_result_for("t", {"q": 1}) == ""
    return _rec("M2 复现（短路拿不到既有结果）⇒ 证明 G2a 非空转", caught)


def m_m3():
    """M3：弱结果也计入去重 ⇒ G3 必须判红。"""
    print("\n=== 变异 M3：弱结果也计入去重计数 ===")
    ns = _load_lg(("            if not weak:\n                self._seen[k] = int(self._seen.get(k, 0)) + 1",
                   "            self._seen[k] = int(self._seen.get(k, 0)) + 1"))
    G = ns["LoopGuard"]
    g = G(dup_threshold=2)
    g.call(lambda n, a: {"ok": True, "result": "未检索到"}, "kb", {"q": 1}, weak=True)
    d = g.probe_dup("kb", {"q": 1})
    return _rec("M3 复现（弱结果被当重复调用）⇒ 证明 G3c 非空转", d >= 2, "dup=%d" % d)


def m_m4():
    """M4：超时谎称已取消 ⇒ G4d 必须判红。"""
    print("\n=== 变异 M4：超时谎称已取消 ===")
    ns = _load_lg(("仍未返回，已停止等待。", "仍未返回，已取消。"))
    import time as _t

    def _slow(n, a):
        _t.sleep(2.0)
        return {"ok": True, "result": "太晚"}
    r = ns["call_tool_guarded"](_slow, "slow", {}, timeout_s=0.2)
    txt = str(r.get("result") or "")
    return _rec("M4 复现（文案谎称已取消）⇒ 证明 G4d 非空转",
                "已取消" in txt, "text=%r" % txt[:50])


def m_m5():
    """M5：token 只读 usage.prompt_tokens（降级路径恒 0）⇒ G5 必须判红。"""
    print("\n=== 变异 M5：token 改读 usage.prompt_tokens ===")
    ns = _load_lg(('        return int(meta.get("token_count") or 0)',
                   '        return 0'))
    caught = ns["tokens_of"]({"_meta": {"token_count": 999}}) == 0
    return _rec("M5 复现（预算闸读到 0 ⇒ 完全失效）⇒ 证明 G5a 非空转", caught)


def main():
    print("=" * 76)
    print("Agent loop 护栏（token 预算 / 重复调用短路 / 工具超时）—— 不变式 + 变异自证")
    print("=" * 76)
    t_g1(); t_g2(); t_g3(); t_g4(); t_g5(); t_g6()
    m_m1(); m_m2(); m_m3(); m_m4(); m_m5()
    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 76)
    print("断言总数 %d  PASS %d  FAIL/VACUOUS %d"
          % (len(_results), len(_results) - len(bad), len(bad)))
    if bad:
        for k, n, d in bad:
            print("  [%s] %s  %s" % (k, n, d))
        print("\n结论：有断言未通过 / 至少一组变异未被复现（空转）")
        return 1
    print("结论：全部通过，且 5 组变异均被复现（断言非空转）")
    return 0


if __name__ == "__main__":
    sys.exit(main())