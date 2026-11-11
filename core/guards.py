# -*- coding: utf-8 -*-
"""单调否决守卫合并（P0-3）。

契约（对标 DSH ``dsh-tools/lib/index.js:2807-2830`` 的
"no guard can force-allow a call another guard denied"）：

* 每个守卫是**同步纯函数**：接收一个只读 context，返回 ``None`` 表示通过，
  返回非空字符串表示否决（字符串即理由）。
* ``combine_guards`` 收集**所有**守卫的否决理由；只要有一条即整体否决。
* 模块**不提供** ``force_allow`` / ``allow`` / ``override`` 之类接口 ——
  后置守卫在结构上无法推翻前置守卫的否决。
* 判定（本模块）与执行（调用方）彻底分离：本模块零 IO、零 LLM 调用、
  零数据库访问，shadow 模式下可安全用于「只判定不执行」。

为什么单独成模块：自研工程里 ``_harness_gate`` 的旧路径把「观察」也走
``_exec_tool_call`` 真执行工具，且自然语言 verify 被解析成工具后带错参真调。
把「否决」收敛成不可覆盖的纯函数，才能让 shadow 的零执行成为可证明的性质，
而不是靠调用方自觉。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

Guard = Callable[[dict], "str | None"]


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reasons: tuple = ()

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def as_dict(self) -> dict:
        return {"allowed": bool(self.allowed), "reasons": list(self.reasons)}


def combine_guards(guards: Sequence[Guard], context: dict) -> GuardVerdict:
    """依次求值所有守卫，返回合并后的否决判定。

    注意：即使第一个守卫已否决，仍会继续求值剩余守卫（收集全部理由），
    但**结果不可被任何守卫改写为允许**。这是「单调」的含义。
    """
    reasons = []
    for guard in guards or ():
        try:
            reason = guard(context or {})
        except Exception as exc:  # 守卫自身异常按否决处理：fail-closed，绝不静默放行
            reason = "守卫异常: %s: %s" % (type(exc).__name__, str(exc)[:160])
        if reason:
            reasons.append(str(reason))
    return GuardVerdict(allowed=not reasons, reasons=tuple(reasons))


def require_deterministic(guard: Guard, context: dict, repeats: int = 3) -> bool:
    """纯函数的行为判据：同一 context 重复求值必须得到完全相同的结果。

    用于契约测试，避免「守卫里偷偷读了数据库/时间/随机数」把判定变成非确定性。
    """
    out = [guard(dict(context or {})) for _ in range(max(1, int(repeats)))]
    return all(x == out[0] for x in out)


__all__ = ["Guard", "GuardVerdict", "combine_guards", "require_deterministic"]
