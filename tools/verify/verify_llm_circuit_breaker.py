# -*- coding: utf-8 -*-
"""P1-4 熔断器门禁（2026-10-06）—— 三态语义 + 接线不变式 + 变异自证。

**这个门禁要防的具体事故**（依据 docs/Agent生产化Harness对照核查-20261006.md §4）：
没有熔断时，上游持续 429/503 ⇒ 每个请求都要付满 3 次退避等待才失败，
**延迟被放大、配额被白耗**，而失败原因始终不透明。

运行：`python tools/verify/verify_llm_circuit_breaker.py`
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from llm.circuit_breaker import CircuitBreaker  # noqa: E402

PASS, FAIL = [], []


def ck(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


print("== A. 三态基础语义 ==")
br = CircuitBreaker("t", fail_threshold=3, reset_sec=0.4)
ck(br.allow("p1")[0] is True, "CLOSED 态放行")
br.record_failure("p1", "e1")
br.record_failure("p1", "e2")
ck(br.allow("p1")[0] is True, "连续失败 2 次（未达阈值 3）仍放行")
br.record_failure("p1", "e3")
ok, why = br.allow("p1")
ck(ok is False, "连续失败达阈值 → OPEN，拒绝放行")
ck("熔断" in why, f"拒绝原因含'熔断'（可观测）：{why[:40]}")
ck(br.status("p1")["p1"]["phase"] == "open", "status 报告 phase=open")

print("\n== B. 成功即清零（不累积到阈值） ==")
br2 = CircuitBreaker("t", fail_threshold=3, reset_sec=0.4)
br2.record_failure("x", "e")
br2.record_success("x")                # 成功打断连续
br2.record_failure("x", "e")
br2.record_failure("x", "e")
ck(br2.allow("x")[0] is True, "中间成功会清零计数（连续失败才熔断，不是累计失败）")

print("\n== C. HALF_OPEN 只放一个探针（防第二次雪崩） ==")
# ⚠️ 用**假时钟**而非 time.sleep：生产 reset_sec 下界是 1 秒，
#真等 1 秒既慢又让门禁不稳定；且熔断器已支持注入 _now（见 circuit_breaker.py）。
class FakeClock(CircuitBreaker):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._now = 1000.0
    def _t(self):
        return self._now
    def advance(self, sec):
        self._now += sec

br3 = FakeClock("t", fail_threshold=2, reset_sec=10, min_reset_sec=0.001)
br3.record_failure("h", "e")
br3.record_failure("h", "e")
ck(br3.allow("h")[0] is False, "封锁期内拒绝")
br3.advance(11)                        # 封锁期到
ok1, why1 = br3.allow("h")
ok2, why2 = br3.allow("h")
ck(ok1 is True and "half_open" in why1, "封锁期后放一个探针（HALF_OPEN）")
ck(ok2 is False, "探针在飞时**拒绝后续并发**（否则是第二次雪崩，不是试探）")
ck("半开" in why2 or "探针" in why2, f"并发拒绝原因可读：{why2[:40]}")

print("\n== D. HALF_OPEN 成功→关闭；失败→重新封锁 ==")
br3.record_success("h")
ck(br3.status("h")["h"]["phase"] == "closed", "探针成功 → 回到 CLOSED")
ck(br3.allow("h")[0] is True, "CLOSED 后正常放行")

br4 = FakeClock("t", fail_threshold=2, reset_sec=10, min_reset_sec=0.001)
br4.record_failure("k", "e")
br4.record_failure("k", "e")
br4.advance(11)
br4.allow("k")# 探针放行
br4.record_failure("k", "probe failed")   # 探针失败
ck(br4.allow("k")[0] is False, "探针失败 → 立即重新熔断（不是慢慢恢复）")
ck(br4.status("k")["k"]["phase"] == "open", "重新计时，仍是 open")

print("\n== E. 按 provider 维度隔离（一个挂不影响另一个） ==")
br5 = FakeClock("t", fail_threshold=2, reset_sec=100, min_reset_sec=0.001)
br5.record_failure("bad", "e")
br5.record_failure("bad", "e")
ck(br5.allow("bad")[0] is False, "bad provider 已熔断")
ck(br5.allow("good")[0] is True, "good provider 不受影响（独立计数）")
# status 只报告**有状态的** provider：从未失败过的 good 不应出现在快照里
ck("bad" in br5.status(), "status 含已熔断的 bad")
ck("good" not in br5.status(), "status 不含从未失败的 good（不虚报状态）")

print("\n== F. reset 与快照 ==")
br5.reset()
ck(br5.allow("bad")[0] is True, "reset() 人工复位后放行")
br6 = FakeClock("t", fail_threshold=1, reset_sec=100, min_reset_sec=0.001)
br6.record_failure("z", "boom 500")
st = br6.status("z")["z"]
ck(st["last_reason"] == "boom 500", "熔断原因被保留（供 UI/告警显示'为什么'）")
ck(st["remaining_sec"] > 0, f"剩余封锁秒数可观测：{st['remaining_sec']}")

print("\n== G. 接线不变式（防「代码在、没接线」） ==")
_llm = open(os.path.join(ROOT, "llm", "__init__.py"), encoding="utf-8").read()
ck("circuit_breaker" in _llm, "llm/__init__.py 已引用 circuit_breaker")
ck("circuit_breaker_enabled" in _llm, "熔断总开关已读取（可灰度关闭）")
ck("_br.allow(" in _llm, "调用前问闸allow()")
ck("_br.record_failure(" in _llm, "失败计入 record_failure()")
ck("_br.record_success(" in _llm, "成功计入 record_success()")
ck("_chat_fallback" in _llm, "主路熔断时走回退路径（不直接 raise，保住退路）")
ck("class _CircuitOpen" in _llm, "熔断用专属异常区分于'下游报错'")
# 熔断必须包在重试**之外层**：若在 _chat_with_retry 内部，
# 一次调用的 3 次重试会被计成 3 次 ⇒ 阈值形同放大 3 倍。
_i_br = _llm.find("_br.record_failure(")
_i_retry_call = _llm.find("_chat_with_retry(")
ck(_i_retry_call < _i_br,
   "熔断计数在 _chat_with_retry **之后**（否则一次调用计 3 次，阈值放大 3 倍）")

_cfg = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
# ⚠️ 必须按**定义位置**切两段，而不是 `split("CONFIG_SCHEMA")`：
#    该名字在 config.py 里先出现在 135 行的**注释**里（"只命中本文件定义 + CONFIG_SCHEMA"），
#    split() 会从注释处切开 → 拿到的是注释到真定义之间的片段，判断必然出错。
_i_schema = _cfg.index("\nCONFIG_SCHEMA = {")
_cfg_defaults, _cfg_schema = _cfg[:_i_schema], _cfg[_i_schema:]
for k in ("circuit_breaker_enabled", "circuit_breaker_fail_threshold", "circuit_breaker_reset_sec"):
    ck(f'"{k}"' in _cfg_defaults, f"配置项 {k} 已在 DEFAULT_CONFIG 定义")
    ck(f'"{k}"' in _cfg_schema, f"配置项 {k} 已注册进 CONFIG_SCHEMA（可在配置面板调）")

print("\n== H. 变异自证 ==")
# H1: 把阈值判定改成永假 ⇒ 应永远不熔断（门禁的 A/B/C 组会判红）
src = open(os.path.join(ROOT, "llm", "circuit_breaker.py"), encoding="utf-8").read()
old = "            if st.fail_count >= self.fail_threshold:"
if old in src:
    ns = {"__name__": "m"}
    exec(compile(src.replace(old, "            if False:", 1), "cb.py", "exec"), ns)
    m = ns["CircuitBreaker"]("m", fail_threshold=2, reset_sec=9)
    for _ in range(5):
        m.record_failure("z", "e")
    ck(m.allow("z")[0] is True, "H1变异体永不熔断（确认变异生效，真实实现会熔断）")
    ck(br.allow("p1")[0] is False, "H1:真实实现已熔断（对照点）")
else:
    ck(False, "H1 变异锚点未命中（record_failure 源码形态变了）")

# H2: HALF_OPEN 不占位（去掉 probe_inflight 检查）⇒ 并发全部放行
old2 = "                if not st.probe_inflight:"
if old2 in src:
    ns2 = {"__name__": "m2"}
    exec(compile(src.replace(old2, "                if True:", 1), "cb2.py", "exec"), ns2)
    Mutant = ns2["CircuitBreaker"]

    class MClock(Mutant):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._now = 3000.0
        def _t(self):
            return self._now

    m2 = MClock("m2", fail_threshold=1, reset_sec=10, min_reset_sec=0.001)
    m2.record_failure("z", "e")        # 达阈值 → OPEN（opened_at=3000）
    m2._now += 11                       # 封锁期到 → HALF_OPEN
    a = m2.allow("z")[0]                # 变异体：第一个探针
    b = m2.allow("z")[0]                # 变异体：**第二个也放行**（雪崩）
    ck(a is True, "H2:变异体第一个探针放行（确认已进入 HALF_OPEN）")
    ck(b is True, "H2:变异体在 HALF_OPEN放行全部并发（确认变异生效 = 雪崩风险）")
    # 对照必须用**全新实例**：br3 在 D 组已 record_success 关闭（phase=closed），
    # 拿它当对照会得到"放行"的假结论 —— 对照组必须与被测组处于**同一状态**。
    ref = FakeClock("ref", fail_threshold=1, reset_sec=10, min_reset_sec=0.001)
    ref.record_failure("z", "e")
    ref.advance(11)
    ck(ref.allow("z")[0] is True, "对照:真实实现第一个探针放行")
    ck(ref.allow("z")[0] is False,
       "H2 对照:真实实现拦住第二个并发（probe_inflight 占位生效）")
else:
    ck(False, "H2 变异锚点未命中")

print("\n" + "=" * 60)
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("  x " + f)
    sys.exit(1)
print("ALL GREEN")