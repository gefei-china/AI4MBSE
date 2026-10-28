# -*- coding: utf-8 -*-
"""门禁：P1-1 预算感知 + P1-2 needs_user 二分。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.5 / §4.6 / §6 第 6 步。

P1-1 要解决的实测问题：89 次评估里 **11 次（12%）** 是 token 耗尽**硬停** ⇒
  用户感知"突然断了"。目标：70% 处主动降级收尾（写待续清单 + 告知还差什么）。

P1-2 要解决的实测问题：6/89 次落在 needs_user ⇒ 全转澄清卡 ⇒ 用户感知"停"。
  其中一部分有合理默认值（如"两个口径"任选都能推进）⇒ 应按默认推进 + 标注假设。

用法：
  python tools/verify/verify_budget_needsuser_p1.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))


def main():
    print("=" * 74)
    print("门禁：P1-1 预算感知 + P1-2 needs_user 二分")
    print("=" * 74)

    from agent import auto_continue as ac

    verdict_todo = {"done": False, "next_step": "执行校验", "remaining": ["执行校验"]}

    # ══════════════ A. P1-1 预算降级 ══════════════
    # A1 未达阈值 ⇒ 正常续跑
    c, why = ac.decide(verdict_todo, used=0, max_used=3, seen_steps=[],
                       budget_left=True, budget_ratio=0.5, degrade_ratio=0.7)
    check("A", "预算 50% < 阈值 70% ⇒ 正常续跑", c is True, why)

    # A2 ★ 达阈值但**预算未耗尽** ⇒ 收紧续跑
    #   ⚠️ 2026-10-28 判据**反转**（原判据是"达 70% ⇒ 停止续跑"）：
    #      真机 conv 9730 证明旧语义有害 —— 预算用到 86%（阈值 70%）就停，
    #      而当时还剩 14% ≈ 2~3 轮，任务差一步（跑校验器）没做完，
    #      用户原话「还是没有持续走下去」。停 vs 续的分界应是"还有没有额度"，
    #      不是"用掉多少比例" ⇒ 改为收紧续跑，只有硬停线/真耗尽才停。
    #      按纪律：判据**反转而非删除**（删掉即该维度不再受保护）。
    c, why = ac.decide(verdict_todo, used=0, max_used=3, seen_steps=[],
                       budget_left=True, budget_ratio=0.7, degrade_ratio=0.7)
    check("A", "★预算达 70%（未耗尽）⇒ 收紧续跑", c is True, why)
    check("A", "★reason 含「收紧续跑」", "收紧续跑" in why, why)
    check("A", "★reason 含实际占比", "70%" in why, why)

    # A3 ★ 到硬停线 ⇒ 停 + 降级收尾（这才是"停"的位置）
    c, why = ac.decide(verdict_todo, used=0, max_used=3, seen_steps=[],
                       budget_left=True, budget_ratio=0.96, degrade_ratio=0.7,
                       hard_stop_ratio=0.95)
    check("A", "★预算到硬停线 96% ⇒ 停止续跑", c is False, why)
    check("A", "★硬停时 reason 含「降级收尾」", "降级收尾" in why, why)

    # A4 ★ 优先级：任务已完成时先说完成，不提预算
    c, why = ac.decide({"done": True}, used=0, max_used=3, seen_steps=[],
                       budget_left=True, budget_ratio=0.99, degrade_ratio=0.7)
    check("A", "★任务已完成 ⇒ 不因预算改变结论（先说完成）",
          c is False and "已完成" in why, why)

    # A5 默认参数兼容（不传 budget_ratio 时行为不变）
    c, why = ac.decide(verdict_todo, used=0, max_used=3, seen_steps=[],
                       budget_left=True)
    check("A", "★不传 budget_ratio ⇒ 行为不变（逐字节兼容）", c is True, why)

    # A6 degrade_ratio=0 ⇒ 关闭**收紧**（但仍受硬停线约束）
    #   ★ 2026-10-28 语义更新：degrade_ratio 现在只控制"收紧"，不控制"停"。
    #     关闭收紧 ⇒ 0.99 时仍应普通续跑（因为硬停线 0.95 也必须显式传入才生效？
    #     不 —— 硬停线默认 0.95，0.99 已越线）。故此处显式把硬停线抬到 1.01 以隔离变量，
    #     证明"关掉收紧 = 不再提前收工"这一条独立成立。
    c, why = ac.decide(verdict_todo, used=0, max_used=3, seen_steps=[],
                       budget_left=True, budget_ratio=0.99, degrade_ratio=0.0,
                       hard_stop_ratio=1.01)
    check("A", "degrade_ratio=0 ⇒ 关闭收紧（可配置关闭）", c is True, why)
    c, why = ac.decide(verdict_todo, used=0, max_used=3, seen_steps=[],
                       budget_left=True, budget_ratio=0.99, degrade_ratio=0.0)
    check("A", "★关掉收紧也挡不住硬停线（安全网仍在）", c is False, why)

    # A7 待续清单内容
    nt = ac.build_degrade_notice(["校验包 P-001", "导出报告"], 0.72, 0.7)
    check("A", "★待续清单列出未完成项",
          "校验包 P-001" in nt and "导出报告" in nt, nt[:200])
    check("A", "★待续清单说明原因（预算）", "预算" in nt, nt[:200])
    check("A", "★待续清单说明怎么继续", "继续" in nt, nt[:200])
    check("A", "★待续清单明确「尚未全部完成」", "尚未全部完成" in nt, nt[:200])

    # A8 LoopGuard.budget_ratio 真实计算
    from agent.loop_guard import LoopGuard
    lg = LoopGuard(token_budget=1000)
    lg.add_tokens(700)
    check("A", "★LoopGuard.budget_ratio 计算正确（700/1000=0.7）",
          abs(lg.budget_ratio() - 0.7) < 1e-6, str(lg.budget_ratio()))
    lg2 = LoopGuard(token_budget=None)
    check("A", "无预算 ⇒ ratio=0.0（不崩）", lg2.budget_ratio() == 0.0,
          str(lg2.budget_ratio()))
    lg3 = LoopGuard(token_budget=1000)
    lg3.add_tokens(5000)
    check("A", "超额 ⇒ ratio>1（不截断，如实反映）", lg3.budget_ratio() > 1.0,
          str(lg3.budget_ratio()))

    # ══════════════ B. P1-2 needs_user 二分 ══════════════
    # B1 user_only 仍转卡
    v = {"done": False, "needs_user": True, "needs_kind": "user_only",
         "question": "用哪个工况口径？", "options": ["A", "B"]}
    cq = ac.clarify_from_verdict(v)
    check("B", "★user_only ⇒ 仍转澄清卡", cq is not None and cq.get("question"),
          str(cq)[:150])

    # B2 ★ has_default 不转卡
    v2 = {"done": False, "needs_user": True, "needs_kind": "has_default",
          "question": "按哪个口径做？", "default": "按最常见的 X 口径",
          "options": ["A", "B"]}
    cq2 = ac.clarify_from_verdict(v2)
    check("B", "★has_default ⇒ 不转澄清卡", cq2 is None, str(cq2)[:150])

    # B3 ★ has_default 转成续跑 verdict
    dp = ac.default_progress_verdict(v2)
    check("B", "★has_default ⇒ 转为续跑 verdict", dp is not None and not dp.get("done"),
          str(dp)[:150])
    if dp:
        check("B", "★续跑 next_step 写明默认值",
              "按最常见的 X 口径" in dp["next_step"], dp["next_step"][:150])
        check("B", "★要求答复标注假设（如需更改请告知）",
              "如需更改请告知" in dp["next_step"], dp["next_step"][:200])

    # B4 ★ 缺 needs_kind 时保守转卡（不为了少弹卡而猜）
    v3 = {"done": False, "needs_user": True, "question": "选哪个？",
          "options": ["A", "B"]}
    check("B", "★缺 needs_kind ⇒ 保守按 user_only（仍转卡）",
          ac.clarify_from_verdict(v3) is not None)
    check("B", "★缺 needs_kind ⇒ 不按默认推进",
          ac.default_progress_verdict(v3) is None)

    # B5 has_default 但无 default ⇒ 退回 user_only（无法推进）
    v4 = {"done": False, "needs_user": True, "needs_kind": "has_default",
          "question": "选哪个？", "options": ["A", "B"]}
    parsed = ac.parse_verdict(
        '{"done": false, "needs_user": true, "needs_kind": "has_default", '
        '"question": "选哪个？", "options": ["A","B"]}')
    check("B", "★has_default 缺 default ⇒ parse 退回 user_only",
          parsed.get("needs_kind") == "user_only", str(parsed)[:150])
    check("B", "★并因此仍转卡（不会静默结束）",
          ac.clarify_from_verdict(parsed) is not None)

    # B6 parse_verdict 保留 needs_kind / default
    parsed2 = ac.parse_verdict(
        '{"done": false, "needs_user": true, "needs_kind": "has_default", '
        '"question": "口径？", "default": "按 X", "options": ["A","B"]}')
    check("B", "parse_verdict 保留 needs_kind",
          parsed2.get("needs_kind") == "has_default", str(parsed2)[:150])
    check("B", "parse_verdict 保留 default", parsed2.get("default") == "按 X",
          str(parsed2)[:150])

    # B7 非 needs_user 的 verdict 不受影响
    cq3 = ac.clarify_from_verdict({"done": False, "next_step": "x"})
    check("B", "非 needs_user 不被误转卡", cq3 is None)
    check("B", "非 needs_user 不被误转续跑",
          ac.default_progress_verdict({"done": False, "next_step": "x"}) is None)

    # ══════════════ C. 接线 ══════════════
    sp = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
              encoding="utf-8").read()
    ex = open(os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py"),
              encoding="utf-8").read()
    cfg = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()

    check("C", "stream.py 调 default_progress_verdict",
          "_ac.default_progress_verdict(" in sp)
    # ⚠️ 判据必须找**代码调用**，不能找裸名 ——
    #   注释里也会提到函数名（本轮代码注释就写了），find 裸名会命中注释 ⇒ 假绿。
    i_dp = sp.find("_ac.default_progress_verdict(")
    i_cq = sp.find("_ac.clarify_from_verdict(")
    check("C", "★has_default 转换在 clarify_from_verdict 之前",
          i_dp >= 0 and i_cq >= 0 and i_dp < i_cq,
          f"dp@{i_dp} cq@{i_cq}")
    check("C", "stream.py 传 budget_ratio", "budget_ratio=" in sp)
    check("C", "execute.py 传 budget_ratio", "budget_ratio=" in ex)
    check("C", "stream.py 降级时写待续清单", "_ac.build_degrade_notice(" in sp)
    check("C", "execute.py 降级时写待续清单", "_ac.build_degrade_notice(" in ex)
    check("C", "budget_degrade_ratio 配置项存在", '"budget_degrade_ratio"' in cfg)
    # ⚠️ 默认值有两处，两处都要验：
    #   ① config.py 的 desc 声明（给人看的"默认 0.7"）
    #   ② 真正生效的读取默认值 `_cfg_dr.get(..., 0.7)`（代码行为）
    check("C", "★config.py 声明默认 0.7",
          "budget_degrade_ratio" in cfg and "默认 0.7" in cfg)
    check("C", "★代码读取默认值 = 0.7（真正生效的那一处）",
          '"budget_degrade_ratio", 0.7)' in sp
          and '"budget_degrade_ratio", 0.7)' in ex,
          "stream=%s execute=%s"
          % ('"budget_degrade_ratio", 0.7)' in sp,
             '"budget_degrade_ratio", 0.7)' in ex))

    # ══════════════ D. 变异自证 ══════════════
    # D1 去掉预算收紧/硬停 ⇒ A2/B3 判据翻红
    #   ★ 2026-10-28：判据随语义一起更新。现在要证的是**两条**：
    #     ① 0.75（未到硬停线）⇒ 收紧续跑；
    #     ② 0.96（到硬停线）⇒ 降级收尾。
    #   一个"完全不看预算"的变异实现应该两条都做不到。
    def c_degrade(dec) -> bool:
        c2, w2 = dec(verdict_todo, used=0, max_used=3, seen_steps=[],
                     budget_left=True, budget_ratio=0.75,
                     degrade_ratio=0.7, hard_stop_ratio=0.95)
        c3, w3 = dec(verdict_todo, used=0, max_used=3, seen_steps=[],
                     budget_left=True, budget_ratio=0.96,
                     degrade_ratio=0.7, hard_stop_ratio=0.95)
        return (c2 is True and "收紧续跑" in w2) and (c3 is False and "降级收尾" in w3)

    def bad_decide(verdict, *, used, max_used, seen_steps,
                   deadline_left_s=None, budget_left=True,
                   budget_ratio=0.0, degrade_ratio=0.7, hard_stop_ratio=0.95):
        if not verdict or verdict.get("done"):
            return False, "done"
        if used >= max_used:
            return False, "上限"
        if not budget_left:
            return False, "耗尽"
        return True, "继续"

    check("D", "★变异自证：去掉预算收紧与硬停 ⇒ 判据翻红",
          c_degrade(ac.decide) and not c_degrade(bad_decide),
          f"真实现={c_degrade(ac.decide)} 变异={c_degrade(bad_decide)}")

    # D2 has_default 也转卡 ⇒ B2 判据翻红
    def c_hasdef(clarify) -> bool:
        return clarify(v2) is None

    def bad_clarify(verdict, qid="x"):
        if not isinstance(verdict, dict) or not verdict.get("needs_user"):
            return None
        return {"id": qid, "question": "变异：一律转卡", "options": []}

    check("D", "★变异自证：has_default 也转卡 ⇒ B2 判据翻红",
          c_hasdef(ac.clarify_from_verdict) and not c_hasdef(bad_clarify),
          f"真实现={c_hasdef(ac.clarify_from_verdict)} 变异={c_hasdef(bad_clarify)}")

    # D3 待续清单不说明怎么继续 ⇒ A7 判据翻红
    def c_notice(build) -> bool:
        n = build(["x"], 0.72, 0.7)
        return ("x" in n) and ("继续" in n) and ("预算" in n)

    def bad_notice(remaining, ratio, threshold=0.7):
        return "已停止。"

    check("D", "★变异自证：待续清单不说明 ⇒ A7 判据翻红",
          c_notice(ac.build_degrade_notice) and not c_notice(bad_notice),
          f"真实现={c_notice(ac.build_degrade_notice)} 变异={c_notice(bad_notice)}")

    # ── 输出 ──
    groups = {}
    for g, n, ok, d in RESULTS:
        groups.setdefault(g, []).append((n, ok, d))
    total = failed = 0
    for g in sorted(groups):
        print(f"\n【{g} 组】")
        for n, ok, d in groups[g]:
            total += 1
            if not ok:
                failed += 1
            line = f"  [{'PASS' if ok else 'FAIL'}] {n}"
            if d and not ok:
                line += f"   ← {d}"
            print(line)
    print("\n" + "-" * 74)
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 门禁未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
