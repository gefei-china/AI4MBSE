# -*- coding: utf-8 -*-
"""门禁：定位类工具试探次数 + 校验停滞短路（TOOL_PROBE_BUDGET_V1）。

真机 conv 999999952：t4 用不同关键词调 17 次 query_projects + 11 次 query_data_tree
（旧 probe_dup 只挡语义等价 ⇒ 一次没挡）；t3 连续 8 次 validate 错误签名不变。
本门锁定两条新增护栏与其接线，含变异自证。
"""
# CI-OPTIONAL: 工具试探预算：纯函数+静态源码门禁，含源码变异自证，暂不进 CI

import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PASS = FAIL = 0
FAILS = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL  %s  << %s" % (name, detail))


def main():
    print("门禁：工具试探预算与校验停滞短路（TOOL_PROBE_BUDGET_V1）")
    from agent.loop_guard import LoopGuard, PROBE_TOOL_LIMITS

    print("\n=== P 纯函数：定位类工具次数上限 ===")
    lg = LoopGuard()
    hit_at = None
    for i in range(8):
        n = lg.probe_cap("query_projects")
        if n and hit_at is None:
            hit_at = i + 1
        lg.mark_done("query_projects", {"keyword": "kw%d" % i}, True)
    check("P1 第 6 次撞上限（上限 5，17 次调用被压到 5）",
          hit_at == 6 and PROBE_TOOL_LIMITS["query_projects"] == 5, "hit_at=%s" % hit_at)
    pl = lg.probe_cap_payload("query_projects", hit_at or 5)
    check("P2 短路回执带已有结果 + 明确'不要再换词'",
          "检索次数上限" in pl["result"] and "不要继续换词重试" in pl["result"])
    lg2 = LoopGuard()
    for i in range(12):
        lg2.mark_done("todo_write", {"id": i}, True)
    check("P3 非定位类工具不受限（零回归）", lg2.probe_cap("todo_write") == 0)

    print("\n=== P 纯函数：校验停滞 ===")
    v = LoopGuard()
    r1 = "词法 0 / 语法 0 / 语义 120，判据 report"
    flags = []
    for _ in range(4):
        flags.append(v.validation_stalled("sysml_v2_validate", {"result": r1}))
    check("P4 同签名第 3 次起短路（8 次重跑被压到 2）",
          flags == [False, False, True, True], str(flags))
    check("P5 错误数下降则放行（修复有效时不短路）",
          LoopGuard().validation_stalled("sysml_v2_validate",
                                         {"result": "语义 40，判据 report"}) is False)
    check("P6 非校验工具不受影响",
          LoopGuard().validation_stalled("query_projects", {"result": r1}) is False)

    print("\n=== S 源码契约 ===")
    s = read_stream_source()
    check("S1 主 ReAct 循环同时接了两条守卫",
          "_lg.probe_cap(tname)" in s and "_lg.validation_stalled(tname, result)" in s)
    check("S2 短路计数可观测（写进 usage）",
          '"probe_caps": _orch_probe_caps' in s and '"validation_stalls": _orch_val_stalls' in s)

    print("\n=== M 变异自证：放开 query_projects 上限，P1 必须翻红 ===")
    fp = os.path.join(ROOT, "agent/loop_guard.py")
    orig = open(fp, "rb").read()
    mutated = orig.decode("utf-8").replace(
        '"query_projects": 5,', '"query_projects": 99,').encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False)
    else:
        try:
            open(fp, "wb").write(mutated)
            import importlib
            import agent.loop_guard as _lgm
            importlib.reload(_lgm)
            a = _lgm.LoopGuard()
            for i in range(8):
                a.mark_done("query_projects", {"keyword": str(i)}, True)
            check("M1 上限放开后 8 次调用不再撞线（断言非空转）",
                  a.probe_cap("query_projects") == 0)
        finally:
            open(fp, "wb").write(orig)
            import importlib
            import agent.loop_guard as _lgm
            importlib.reload(_lgm)
            check("M2 还原后第 6 次撞线",
                  _lgm.PROBE_TOOL_LIMITS["query_projects"] == 5)

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
