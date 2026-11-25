# -*- coding: utf-8 -*-
"""门禁：耗时/token/工具数随消息落库，刷新后仍可见（USAGE_STATS_PERSIST_V1）。

真机取证（conv 999999952 / msg 7135）：
  run_budget.used_tokens=22682 有值，但 card_data 无 summary、exec 无 elapsed_ms、
  exec.tools=[]（实际 198 次）⇒ 前端统计栏不显示；done 事件也没有 usage。
判据：
  P 纯函数：build_message_summary 优先读 usage，四项齐全
  S 静态：编排/主路径 card_data 装配时调 _attach_usage_summary；两条 done 均带 usage
  M 变异：删掉 cards.py 的 usage 优先级 ⇒ P 判据必须翻红
"""
# CI-OPTIONAL: 统计落库：纯函数+静态源码门禁，依赖 card/summary 当前结构，暂不进 CI

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


def stats_of(cd):
    from agent.pipeline_parts.cards import CardMixin
    return CardMixin.build_message_summary(cd, "正文")["stats"]


def main():
    print("门禁：统计落库与自动展示（USAGE_STATS_PERSIST_V1）")

    print("\n=== P 纯函数：速览统计 ===")
    cd = {"usage": {"tokens": 22682, "elapsed_ms": 1208000,
                    "tool_calls": 198, "subtasks": "4/5"},
          "run_budget": {"used_tokens": 999},
          "exec": {"tools": []}}
    s = stats_of(cd)
    check("P1 token 用 usage 而非旧 run_budget", s["tokens"] == 22682, str(s))
    check("P2 墙钟从 usage 取（旧 exec 无 elapsed_ms）", s["elapsed_ms"] == 1208000, str(s))
    check("P3 工具数不再因 exec.tools=[] 显示 0", s["tool_calls"] == 198, str(s))
    check("P4 子任务进度从 usage 取", s["subtasks"] == "4/5", str(s))
    s2 = stats_of({"run_budget": {"used_tokens": 77}, "exec": {"tools": [{"ok": True}]}})
    check("P5 旧卡片无 usage 时保持旧口径（零回归）",
          s2["tokens"] == 77 and s2["tool_calls"] == 1, str(s2))

    print("\n=== S 源码契约 ===")
    src = read_stream_source()
    check("S1 落库前统一挂 usage + summary 的助手存在",
          "def _attach_usage_summary(" in src and 'card_obj["summary"] = _sum' in src)
    check("S2 编排路径调用（两条 card_data 装配均覆盖）",
          src.count("_attach_usage_summary(card_obj") >= 2,
          "calls=%d" % src.count("_attach_usage_summary(card_obj"))
    check("S3 编排路径工具数来自实时事件计数（不是硬写 []）",
          "_orch_tool_counts" in src and '"tool_calls": int(sum(_orch_tool_counts.values()))' in src)
    check("S4 编排 done 带 usage", '"usage": _usage,' in src)
    check("S5 主路径 done 带 usage",
          '"elapsed_ms": int((_t_stats.time() - _stats_t0) * 1000)' in src)
    cards = open(os.path.join(ROOT, "agent/pipeline_parts/cards.py"), encoding="utf-8").read()
    check("S6 速览生成器优先读 usage", 'usage = cd.get("usage") or {}' in cards)

    print("\n=== M 变异自证：去掉 usage 优先级，P1/P3 必须翻红 ===")
    fp = os.path.join(ROOT, "agent/pipeline_parts/cards.py")
    orig = open(fp, "rb").read()
    mutated = orig.decode("utf-8").replace(
        'usage = cd.get("usage") or {}', 'usage = {}').encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False)
    else:
        try:
            open(fp, "wb").write(mutated)
            import importlib
            import agent.pipeline_parts.cards as _c
            importlib.reload(_c)
            from agent.pipeline_parts.cards import CardMixin as _CM
            bad = _CM.build_message_summary(cd, "正文")["stats"]
            check("M1 去掉 usage 后 token/工具数回落到旧值（断言非空转）",
                  bad["tokens"] == 999 and bad["tool_calls"] == 0, str(bad))
        finally:
            open(fp, "wb").write(orig)
            import importlib
            import agent.pipeline_parts.cards as _c
            importlib.reload(_c)
            check("M2 还原后统计正确", stats_of(cd)["tokens"] == 22682)

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
