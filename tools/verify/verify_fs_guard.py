# -*- coding: utf-8 -*-
"""core.fs_guard.bounded_unlink 的常驻自检：临时文件删除的看门狗语义 + 变异自证。

背景（2026-10-01 实测）：WorkBuddy 沙箱 tsbx.dll 挂钩 DeleteFileW → 回收站语义，在
COM 临界区死锁，把编排流 AST 解析的临时文件删除卡了 40+ 分钟（整条链路不收敛）。
本脚本证明：bounded_unlink 在「删除挂起 / 删除报错」时**既不抛也不阻塞**，且变异测试
证明这些断言不是空转（把看门狗退化成无超时 join 会被抓住）。

判据纪律（防空转）：
  - 慢删除用例用「真实 time.sleep 的假删除函数」而非 mock，且断言**耗时上限**（不等满）。
  - 变异锚点必须是行为开关（`t.join(timeout)` → `t.join()`），且变异后**目标断言真的翻转**。
"""
import inspect
import os
import sys
import tempfile
import textwrap
import time
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core.fs_guard import bounded_unlink, DEFAULT_WATCHDOG_TIMEOUT

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _mk():
    fd, p = tempfile.mkstemp(suffix=".fsg")
    os.close(fd)
    return p


# ── [F] 看门狗语义 ─────────────────────────────────────────────
print("[F] bounded_unlink 看门狗语义")

_p = _mk()
check("F1 正常删除返回 True 且文件消失", bounded_unlink(_p) is True and not os.path.exists(_p))

_p2 = _mk()
check("F2 不存在路径返回 True（幂等，不抛）", bounded_unlink(_p2) is True
      and bounded_unlink(_p2) is True)


def _slow(p):          # 假删除：真实 sleep 模拟「tsbx 死锁 / 慢删除」
    time.sleep(0.4)
    os.remove(p)


_p3 = _mk()
_t0 = time.time()
_r3 = bounded_unlink(_p3, timeout=0.1, deleter=_slow)
_dt3 = time.time() - _t0
check("F3 慢删除 → 看门狗超时返回 False，且不阻塞（不等满 0.4s）",
      _r3 is False and _dt3 < 0.35, "返回=%s 耗时=%.2fs" % (_r3, _dt3))
# daemon 线程 0.4s 后把文件删掉；给 1s 兜底确认不残留影响（不强依赖时序，仅顺带清理）
time.sleep(0.6)


def _boom(p):
    raise PermissionError("denied")


_p4 = _mk()
check("F4 删除抛异常 → 降级返回 False（不抛、不升级为管线失败）",
      bounded_unlink(_p4, deleter=_boom) is False)
# 该文件仍存在（未删成），手动清理
try:
    os.remove(_p4)
except Exception:
    pass


# ── [M] 变异自证 ───────────────────────────────────────────────
print("[M] 变异自证（把看门狗退化成无超时 join → 必须被抓住）")


def _twin(mutate, label):
    """exec 孪生体：bounded_unlink 源码注入变异后，连同 os/threading 一起执行。
    与 verify_multiturn_context 的 _at_twin 同族 —— 必须把被测函数整个 exec 进命名空间，
    否则被调函数仍是原模块对象（__globals__ 指向真模块）→ 调未变异版本 → 假绿。
    """
    src = textwrap.dedent(inspect.getsource(bounded_unlink)).replace("\r\n", "\n")
    mut = mutate(src)
    check("M%s 变异锚点命中" % label, mut != src, "无变化=锚点未命中")
    ns = {"os": os, "threading": threading, "DEFAULT_WATCHDOG_TIMEOUT": DEFAULT_WATCHDOG_TIMEOUT}
    exec(compile(mut, "<fs_guard_twin>", "exec"), ns)
    return ns


# 变异：去掉看门狗超时（t.join(timeout) → t.join()）——慢删除场景将同步等到完成 → 返回 True
_ns1 = _twin(lambda s: s.replace("t.join(timeout)", "t.join()"), "1")
_p5 = _mk()
_t0 = time.time()
_r5 = _ns1["bounded_unlink"](_p5, timeout=0.1, deleter=_slow)
_dt5 = time.time() - _t0
# 无超时 join → 等满 0.4s 且删除成功 → 返回 True；而正确行为应是 False。此断言必须 FAIL。
check("M1 无超时 join → F3 的「超时返回 False」语义被破坏（被抓住）",
      not (_r5 is False and _dt5 < 0.35),
      "变异后返回=%s 耗时=%.2fs（True/≥0.35 即正确暴露变异）" % (_r5, _dt5))

# 变异：把「返回 False 泄漏」改成「返回 True 谎报成功」——删除挂起时谎称已删，必须被抓住
_ns2 = _twin(lambda s: s.replace("        return False\n", "        return True\n", 1), "2")
_p6 = _mk()
_r6 = _ns2["bounded_unlink"](_p6, timeout=0.1, deleter=_slow)
check("M2 超时谎报成功（False→True）→ 慢删除场景应返回 False 却得到 True（被抓住）",
      _r6 is not False, "变异后返回=%s（True 即正确暴露变异）" % (_r6,))
time.sleep(0.6)

# ── 汇总 ───────────────────────────────────────────────────────
print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
