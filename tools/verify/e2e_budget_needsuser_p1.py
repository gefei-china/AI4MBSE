# -*- coding: utf-8 -*-
"""真机验证：P1-1 预算降级 + P1-2 needs_user 二分（真实 LLM）。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.5 / §4.6。

为什么必须真机跑：
  P1-2 的关键是"**真实 LLM 会不会按新规则输出 needs_kind**" ——
  这是提示词遵循率问题，离线门禁无法证明（本仓已多次记录"遵循率不稳"）。

真机判据：
  ① 真实 LLM 面对"两个口径都能推进"的问题 ⇒ 输出 has_default（或至少给出 default）
  ② 真实 LLM 面对"信息只有用户有"的问题 ⇒ 输出 user_only
  ③ ★ 平台独立回读：把 LLM 输出喂进 parse_verdict / clarify_from_verdict /
     default_progress_verdict，断言走到正确分支
  ④ P1-1：真实 LoopGuard 在 70% 处触发降级

用法：
  python tools/verify/e2e_budget_needsuser_p1.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if not cond and detail else ""))


# 两个真实场景（来自本工程的典型对话形态）
CASES = [
    {
        "name": "有两个口径都能推进",
        "user": "帮我分析一下这个需求的变更影响范围",
        "answer": ("影响范围取决于按哪个口径统计：①按**受影响元素数**（1 个包 + 3 个需求）"
                   "还是 ②按**受影响接口数**（2 个接口）。两者都能给出结论，"
                   "我先按常见的①受影响元素数 给你列一下：包 P-001、需求 R-003/R-007/R-012。"
                   "如果你要的是接口口径，告诉我我再换。"),
        "expect_kinds": ["has_default"],
    },
    {
        "name": "信息只有用户有（工况口径）",
        "user": "把热管理系统的工况参数配置一下",
        "answer": ("配置工况参数需要你提供**具体的环境温度与工况点**："
                   "是 -20℃ 低温启动、25℃ 常温、还是 40℃ 高温工况？"
                   "这个取值因项目而异，我无法从现有工程数据推断。"),
        "expect_kinds": ["user_only"],
    },
]


def run():
    print("=" * 74)
    print("真机验证：P1-2 needs_user 二分（真实 LLM）")
    print("=" * 74)

    from agent import auto_continue as _ac
    from llm import llm_client
    from agent.pipeline_parts.common import CHAT_TASK_EVAL

    # ── 0. 提示词确实带了新规则 ──
    check("0", "EVAL_SYS 含 needs_kind", "needs_kind" in _ac.EVAL_SYS)
    check("0", "EVAL_SYS 含 user_only", "user_only" in _ac.EVAL_SYS)
    check("0", "EVAL_SYS 含 has_default", "has_default" in _ac.EVAL_SYS)

    parsed = {}
    for c in CASES:
        print(f"\n  → 场景「{c['name']}」…")
        msgs = _ac.build_eval_messages(c["user"], "（无工具调用）", c["answer"], "通用问答")
        try:
            resp = llm_client.chat(msgs, provider_id=None, stream=False,
                                   _intent=CHAT_TASK_EVAL)
            text = (((resp or {}).get("choices") or [{}])[0]
                    .get("message", {}).get("content") or "")
        except Exception as e:
            check("1", f"「{c['name']}」真实 LLM 评估", False, repr(e))
            continue
        v = _ac.parse_verdict(text)
        print(f"    （info）原始输出 = {str(text).strip()[:200]}")
        print(f"    （info）解析后 = {json.dumps(v, ensure_ascii=False)[:200] if v else 'None'}")
        check("1", f"「{c['name']}」返回可解析结论", v is not None, str(text)[:150])
        if v:
            parsed[c["name"]] = v

    # ── 2. ★ 关键：口径二选一 ⇒ 应走 has_default（按默认推进，不弹卡）──
    v1 = parsed.get("有两个口径都能推进")
    if v1:
        kind1 = v1.get("needs_kind")
        cq = _ac.clarify_from_verdict(v1)
        dp = _ac.default_progress_verdict(v1)
        print(f"    （info）needs_kind={kind1} 转卡={'是' if cq else '否'} "
              f"按默认推进={'是' if dp else '否'}")
        # 判据宽松但有意义：要么明确 has_default，要么（若判 done=false+next_step）
        # 至少不能是"转卡且无默认值"
        ok_has_default = (kind1 == "has_default" and dp is not None and cq is None)
        ok_proceed = (v1.get("done") is False and not v1.get("needs_user")
                      and bool(v1.get("next_step")))
        check("2", "★口径二选一 ⇒ 不打断用户（has_default 或直接续跑）",
              ok_has_default or ok_proceed,
              f"kind={kind1} done={v1.get('done')} cq={bool(cq)} dp={bool(dp)}")
        if ok_has_default:
            check("2", "★走 has_default 时 next_step 写明默认值",
                  bool(dp.get("next_step")), str(dp)[:150])

    # ── 3. ★ 工况口径 ⇒ 应走 user_only（该问就问）──
    v2 = parsed.get("信息只有用户有（工况口径）")
    if v2:
        kind2 = v2.get("needs_kind")
        cq2 = _ac.clarify_from_verdict(v2)
        dp2 = _ac.default_progress_verdict(v2)
        print(f"    （info）needs_kind={kind2} 转卡={'是' if cq2 else '否'} "
              f"按默认推进={'是' if dp2 else '否'}")
        check("3", "★工况口径 ⇒ 该问就问（转卡或明确 next_step，不被默认值糊弄）",
              (cq2 is not None) or (v2.get("done") is False and not dp2),
              f"kind={kind2} cq={bool(cq2)} dp={bool(dp2)}")

    # ── 4. P1-1 真实 LoopGuard 预算降级 ──
    print("\n  → P1-1 真实 LoopGuard 预算降级 …")
    from agent.loop_guard import LoopGuard
    lg = LoopGuard(token_budget=1000)
    lg.add_tokens(700)
    ratio = lg.budget_ratio()
    check("4", "★真实 LoopGuard 700/1000 ⇒ ratio=0.7",
          abs(ratio - 0.7) < 1e-6, str(ratio))
    cont, why = _ac.decide({"done": False, "next_step": "x", "remaining": ["y"]},
                           used=0, max_used=3, seen_steps=[],
                           budget_left=lg.budget_left(),
                           budget_ratio=ratio, degrade_ratio=0.7)
    check("4", "★达阈值 ⇒ 不续跑 + 降级收尾", cont is False and "降级收尾" in why, why)
    nt = _ac.build_degrade_notice(["待办A", "待办B"], ratio, 0.7)
    check("4", "★待续清单含未完成项", "待办A" in nt and "待办B" in nt, nt[:150])
    check("4", "★待续清单含继续方式", "继续" in nt, nt[:150])

    # 未达阈值仍续跑（基线对比，防"恒降级"）
    lg2 = LoopGuard(token_budget=1000)
    lg2.add_tokens(300)
    cont2, why2 = _ac.decide({"done": False, "next_step": "x", "remaining": []},
                             used=0, max_used=3, seen_steps=[],
                             budget_left=lg2.budget_left(),
                             budget_ratio=lg2.budget_ratio(), degrade_ratio=0.7)
    check("4", "★30% 时仍正常续跑（防恒降级）", cont2 is True, why2)


def main():
    run()
    print("\n" + "-" * 74)
    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if not r[2])
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
