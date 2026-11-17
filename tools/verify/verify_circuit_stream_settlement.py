# -*- coding: utf-8 -*-
"""流式请求必须结算熔断探针位门禁（2026-11-17 生产事故驱动）。

## 事故

`llm/__init__.py` 原实现里：
  · `allow()` 对**流式请求照样占用 HALF_OPEN 探针位**（`probe_inflight=True`）；
  · 但 `record_success` / `record_failure` 都挂着 `not stream` ⇒ 流式请求
    **拿了探针位永不归还**。
后果：provider 71 一次熔断后，16:45~16:52 连续 7 分钟**所有**请求（含非流式）
都得到「熔断半开试探中（已有探针在飞）」，36 个 live eval 用例 12 秒全灭。
`probe_inflight` 只在 `record_success`/`record_failure` 里清，无其它路径 ⇒
**进程不重启就永久卡死**。

## 判据

  A. 代码级：成功/失败两条结算路径都不得再用 `not stream` 把流式排除
  B. 行为级：真实 CircuitBreaker —— 占探针 → 第二发被拒 → 结算 → 恢复放行
     （证明"探针位可归还"这件事本身是通的，A 组保证流式路径真的调了结算）

用法：``.venv/Scripts/python.exe tools/verify/verify_circuit_stream_settlement.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import io
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from llm.circuit_breaker import CircuitBreaker  # noqa: E402

FAILS: list = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


def case_code() -> None:
    print("\nA. 代码级（流式必须结算探针位）")
    src = io.open(os.path.join(REPO, "llm", "__init__.py"), encoding="utf-8").read()
    check("A1 成功路径无条件结算（不再排除 stream）",
          "if _br:\n                _br.record_success(_pkey)" in src,
          "成功路径仍带 not stream")
    check("A2 失败路径无条件结算（不再排除 stream）",
          "if _br and self._retryable(e):\n                try:\n"
          "                    _br.record_failure(_pkey" in src,
          "失败路径仍带 not stream")
    check("A3 源码里不再残留 `not stream` 的结算条件",
          "and not stream and self._retryable(e)" not in src
          and "if _br and not stream:" not in src, "残留旧条件")
    _i_allow = src.find("_br.allow(_pkey)")
    _i_success = src.find("_br.record_success(_pkey)")
    check("A4 结算点在 allow 之后（同一条调用链）",
          0 < _i_allow < _i_success, "allow=%s success=%s" % (_i_allow, _i_success))


def case_breaker() -> None:
    print("\nB. 行为级（探针位可占、可拒、可归还）")
    # 可控时钟：不靠 sleep（判据是状态机，不是耗时；sleep 还会引入抖动）
    clock = [1000.0]
    br = CircuitBreaker("test_stream_settle", fail_threshold=1, reset_sec=10.0)
    br._t = lambda: clock[0]
    br.record_failure("p", "fixture trip")
    st = br.status("p")
    check("B1 失败达阈值 ⇒ 熔断打开",
          st.get("p", {}).get("phase") == "open", str(st))

    clock[0] += 11.0                      # 封锁期过
    ok1, why1 = br.allow("p")
    check("B2 封锁期过 ⇒ 第一个探针放行", ok1 is True and why1 == "half_open_probe",
          "%r/%r" % (ok1, why1))
    ok2, why2 = br.allow("p")
    check("B3 ★ 探针在飞 ⇒ 第二发被拒（防雪崩，语义不能丢）",
          ok2 is False and "探针在飞" in str(why2), "%r/%r" % (ok2, why2))

    # 关键：结算（成功路径）—— A 组保证流式请求确实会走到这里
    br.record_success("p")
    ok3, why3 = br.allow("p")
    check("B4 ★ 结算后探针位归还、熔断关闭（不再永久卡死）",
          ok3 is True and why3 == "", "%r/%r" % (ok3, why3))
    st2 = br.status("p")
    check("B5 关闭后 fail_count 清零",
          int(st2.get("p", {}).get("fail_count") or 0) == 0, str(st2))

    # 失败结算路径也能归还
    br.record_failure("p", "trip again")
    clock[0] += 11.0
    ok4a, _ = br.allow("p")                # 占探针
    ok4, why4 = br.allow("p")              # 第二发被拒
    check("B6 探针失败前仍互斥（占位生效）", ok4a is True and ok4 is False,
          "%r/%r" % (ok4a, ok4))
    br.record_failure("p", "probe failed")
    ok5, why5 = br.allow("p")
    check("B7 探针失败后重新计时封锁（不无限重试）",
          ok5 is False and "熔断中" in str(why5), "%r/%r" % (ok5, why5))


def main() -> int:
    print("流式请求熔断探针结算门禁")
    print("=" * 68)
    case_code()
    case_breaker()
    print("\n" + "=" * 68)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
