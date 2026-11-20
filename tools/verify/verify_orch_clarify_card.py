# -*- coding: utf-8 -*-
"""门禁：编排子任务的「等用户」信号必须冒泡成卡片，且不得被重试吃掉。

真机取证（conv 999999952 / run 7，2026-11-20）：
  t5 在 01:57:19 调 ask_user，回执明写"流程暂停，等待用户回答后继续"，
  但 0.4s 后模型继续吐正文；全场 0 张 clarify_ask 卡、pending_clarify 为空；
  agent_tasks 显示 t5 retry_count=2 —— 首次中断被重试覆盖，最终变成"请你确认"散文。

三层判据：
  C 分类：pending_* / 等待用户 等文案 → needs_user，且明确不可重试
  S 源码：子任务事件 switch 捕获 clarify_ask；worker 不重试 needs_user；
          编排层在汇总前只发一张卡并 return
  M 变异：把 needs_user 从 NON_RETRYABLE 去掉 → C 判据必须翻红
"""
# CI-OPTIONAL: 编排澄清卡冒泡：纯函数+静态源码门禁，改动热路径时需人工确认接线，暂不进 CI

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
    print("门禁：编排澄清卡冒泡（ORCH_CLARIFY_CARD_V1）")
    from services import failure_class as fc

    print("\n=== C 失败分类 ===")
    for text in ("pending_clarify: 等待用户澄清回答，本子任务未产生交付物",
                 "pending_confirm: 等待人工确认（确认单 #12）",
                 "已向用户发起确认（写不写入？，4 个选项）——流程暂停，等待用户回答后继续"):
        got = fc.classify(text, ok=False)
        check("C 等用户输入 → needs_user：%s" % text[:28], got == "needs_user", "got=%s" % got)
    check("C needs_user 不可重试",
          fc.is_retryable_class("needs_user") is False,
          "NON_RETRYABLE=%s" % sorted(fc.NON_RETRYABLE))
    check("C 普通 timeout 仍可重试（不过度收紧）",
          fc.is_retryable_class(fc.classify("timeout（240s 无产出）", ok=False)) is True)

    print("\n=== S 源码契约 ===")
    p = os.path.join(ROOT, "agent/pipeline_parts/stream.py")
    src = open(p, encoding="utf-8").read()
    check("S1 子任务事件 switch 捕获 clarify_ask（此前无分支被静默丢弃）",
          'elif _et in ("clarify_ask", "hil_confirm"):' in src)
    check("S2 用 run 级 _orch_pending 汇总，而不是每任务直接发卡",
          "_orch_pending = {}" in src and "_orch_pending_lock" in src
          and "_orch_pending[tkey]" in src)
    check("S3 编排层发唯一一张卡并 return（早于汇总）",
          'yield {"type": "clarify_ask", "questions": _pqs' in src
          and "if _orch_pending:" in src)
    check("S4 卡片落 parent 会话 pending_clarify", "self._persist_clarify(" in src)
    check("S5 done 携带 clarify_asked（前端据此建卡）",
          '"ok": True, "clarify_asked": True, "intent": intent' in src)
    check("S6 worker 重试判据读分类器（needs_user 才能被挡住）",
          "_fc.classify(last_res[5], ok=bool(last_res[2]))" in src
          and "_fc.is_retryable_class(_ecls)" in src)

    print("\n=== M 变异自证：放开 needs_user 重试，C 判据必须翻红 ===")
    fp = os.path.join(ROOT, "services/failure_class.py")
    orig = open(fp, "rb").read()
    mutated = orig.decode("utf-8").replace(
        '"business", "needs_user"})', '"business"})').encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False, "未找到 NON_RETRYABLE 锚点")
    else:
        try:
            open(fp, "wb").write(mutated)
            import importlib
            importlib.reload(fc)
            check("M1 去掉 needs_user 后变可重试 ⇒ 断言不是空转",
                  fc.is_retryable_class("needs_user") is True)
        finally:
            open(fp, "wb").write(orig)
            import importlib
            importlib.reload(fc)
            check("M2 还原后重新不可重试",
                  fc.is_retryable_class("needs_user") is False)

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
