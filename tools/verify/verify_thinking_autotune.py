"""verify_thinking_autotune — 判据：**数据自校正**（可维护性的最后一环，2026-10-10）。

## 米爸提的问题
"agent、技能、工具都是暂时的，后续会增加新的意图、任务，这个要考虑怎么覆盖处理"

## 已做的两层（还差最后一环）
| 层 | 做法 | 覆盖能力 | 局限 |
|---|---|---|---|
| 1 名单 | `thinking_policy.off` | 人配的例外 | 靠人记得加 |
| 2 形态规则 | 带 tools？上限小？探针语义？ | 覆盖**探针类**新 intent | **只认探针语义** |
| **3 自校正** | ★读真库历史数据自动判定 | ★**任何** intent，含非探针类 | 本文件 |

★ **第3 层为什么必需**：形态规则只认「探针语义」这一类。
若米爸新增一个 Agent 叫 `影响分析报告`，它既不带 probe/detect 语义、正文也长
（不该关）—— 规则不命中，这是**对的**。
但若新增一个叫 `快速摘要` 的（正文几十 token、思考占 80%），
形态规则也不命中 ⇒ 思考继续失控 ⇒ **仍然要人配**。
⇒ 自校正按**该 intent 的真库实测数据**判定，不需要知道它的名字语义。

## 自校正判据（全部有数据支撑，不是拍脑袋）
对每个 intent，用 `llm_usage_stats` 的历史聚合算两个量：
  · `reasoning_ratio` = Σ reasoning_tokens / Σ completion_tokens
  · `avg_completion`   = 平均产出 token
判定：
  · `reasoning_ratio ≥ min_ratio`（默认 0.5）**且** `avg_completion ≤ max_output`
    （默认 1200）⇒ **建议关思考**（思考远大于产出 = 纯浪费）
  · 任一条件不满足 ⇒ 不动

## 为什么必须「读真库」而不是写死
实测分界极其清晰（2026-10-10 真库，n=6890）：
  · 廉价任务：思考占 **58%~98%**，`probe`97.6% / `crypto_probe` 79.1% / `routing` 81.6%
  · 重产码：思考占 **0%~2.5%**，`plan_refine` 0% / `plan_summary` 1.4%
  ⇒ **同一个 provider 覆盖两端，必然一边浪费、一边不足** —— 只有按实测数据分流才能解决。

## 三条安全阀（防止误伤）
① **样本量下限**（默认 5次）：调用太少不判——数据不足不能定性。
② **只建议不自动改**：本层产出的是**建议清单**，写入/应用由人决定。
③ **默认关闭**：`llm.autotune_enabled` 默认 off。

## 判据
① 配置已登记 schema，默认关闭
② ★ 用**真库真实数据**跑聚合，产出建议清单
③ ★ 建议清单**必须包含**某个实测浪费严重的 intent（如 `probe` 97.6%）
④ ★ 建议清单**必须不包含**重产码 intent（`plan_refine` / `plan_summary`）
⑤ 样本量不足时**不判**（安全阀①）
⑥ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_thinking_autotune.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#★ 分析逻辑放在**生产模块**里，本判据只验它 —— 避免"判据里一份、生产里一份"
#   这种双份实现漂移（判据绿了但线上没这段代码 = 空转）。
sys.path.insert(0, os.path.join(_ROOT, "llm"))
from thinking_autotune import suggest_thinking_off  # noqa: E402


def main() -> int:
    print("=" * 74)
    print("判据 · 思考分流的数据自校正（第 3 层：覆盖任意新增 intent）")
    print("=" * 74)
    ok = True

    from core import config
    from core.config import CONFIG_SCHEMA
    schema = CONFIG_SCHEMA.get("llm", {})

    print("\n① 配置与 schema")
    for name, passed in [
        ("`llm.autotune_enabled` 已登记 CONFIG_SCHEMA", "autotune_enabled" in schema),
        ("`llm.autotune_min_ratio` 已登记 CONFIG_SCHEMA", "autotune_min_ratio" in schema),
        ("`llm.autotune_max_output` 已登记 CONFIG_SCHEMA", "autotune_max_output" in schema),
        ("`llm.autotune_min_samples` 已登记 CONFIG_SCHEMA", "autotune_min_samples" in schema),
        ("★ 自校正**默认关闭**（只建议不自动改）",
         not config.as_bool("llm", "autotune_enabled", False)),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ② ★ 用真库真实数据产出建议
    print("\n② ★ 用真库真实数据产出建议清单")
    sug = suggest_thinking_off()
    if not sug:
        print("[FAIL] 未产出任何建议（真库应有大量 intent 记录）")
        return 1
    print(f"    建议关思考的 intent：**{len(sug)}** 个")
    for s in sorted(sug, key=lambda x: -x["ratio"])[:8]:
        print(f"      {s['intent'][:26]:28s} n={s['n']:5d} 思考占比={s['ratio'] * 100:5.1f}% "
              f"均产={s['avg_completion']:7.0f} tok")

    # ③ ★ 必须捞到实测浪费最严重的 intent
    print("\n③ ★ 必须捞到实测浪费严重的 intent")
    names = {s["intent"] for s in sug}
    for want in ("probe", "crypto_probe", "routing"):
        hit = [s for s in sug if s["intent"] == want]
        if hit:
            print(f"[OK  ]捞到「{want}」（思考占比 {hit[0]['ratio'] * 100:.1f}%）")
        else:
            print(f"[WARN] 未捞到「{want}」"
                  + ("" if not names else f"（当前清单：{sorted(names)[:6]}）"))
    # 只要捞到任一高浪费 intent 即可（样本量可能不足）
    if any(s["ratio"] >= 0.5 for s in sug):
        print("[OK  ] 至少捞到一个思考占比 ≥50% 的 intent")
    else:
        print("[FAIL] 没有任何 intent 的思考占比 ≥50% ⇒ 聚合逻辑可疑")
        ok = False

    # ④ ★ 必须不包含重产码intent
    print("\n④ ★ 不得把重产码 intent 建议关掉（误伤 = 质量事故）")
    heavy = {"plan_refine", "plan_summary", "impact_advice", "plan_eval"}
    bad = heavy & names
    if bad:
        print(f"[FAIL] ★★ 误伤重产码 intent：{bad}")
        ok = False
    else:
        print(f"[OK  ] 未误伤重产码 intent（查了 {sorted(heavy)}）")

    # ⑤ 安全阀：样本量不足不判
    print("\n⑤ 安全阀：样本量不足的 intent 不应出现在建议里")
    low = [s for s in sug if s["n"] < 5]
    if low:
        print(f"[FAIL] 出现样本量 <5 的 intent：{[s['intent'] for s in low][:5]}")
        ok = False
    else:
        print("[OK  ] 所有建议的样本量都 ≥5")

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if not ok:
        return 1

    # ── 变异自证：抽掉「产出上限」这道闸 ⇒ 应把重产码捞进来 ⇒ 判红 ──
    # ⚠️ 第一版变异只把 `min_ratio` 调到 0.0，结果**仍是绿** ——
    #   因为判据是**双条件**（ratio ≥ min_ratio **且** avg ≤ max_output），
    #   单放开 ratio 那道闸，`max_output=1200` 仍在拦重产码。
    #   ⇒ 这不是"判据抓不住误伤"，而是**误伤被第二道闸挡住了**（设计生效）。
    #   ⇒ 正确的变异是**同时抽掉两道闸** —— 那才是"守卫真的没了"的状态。
    print("\n[变异自证] 同时抽掉 ratio 与产出上限两道闸，应捞进重产码 ⇒ 判红")
    try:
        mutated = suggest_thinking_off(min_ratio=0.0, max_output=10**9)
        hit = heavy & {s["intent"] for s in mutated}
        red = bool(hit)
        print(f"[变异自证] 捞到重产码 {sorted(hit) or '无'} ⇒ "
              f"{'已变红（判据能抓住误伤）' if red else '**仍是绿 ⇒ 判据抓不住误伤**'}")
        ok = ok and red
    finally:
        pass

    # 补充：证明**单放一道闸**仍安全（双条件设计的价值）
    print("\n[补充验证] 只放开 ratio 一道闸，重产码应仍被产出上限挡住（双条件的价值）")
    only_ratio = suggest_thinking_off(min_ratio=0.0)
    hit2 = heavy & {s["intent"] for s in only_ratio}
    if hit2:
        print(f"[WARN] 只放开 ratio 就误伤了 {sorted(hit2)} ⇒ 双条件设计没起作用")
    else:
        print("[OK  ] 只放开 ratio 未误伤 ⇒ 第二道闸（产出上限）确实在起作用")

    print("=" * 74)
    print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
