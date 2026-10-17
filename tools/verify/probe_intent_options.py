# -*- coding: utf-8 -*-
"""意图确认卡候选动态化探针（2026-10-17，米爸反馈"每次都是固定 4 项清单"）。

背景
----
意图确认卡（阶段 1.4）的候选选项此前 = 「当前猜测」+「语义 top1」+ `_db_intents`
**字典序前 4 项** —— 换任何输入都是同一张清单（米爸截图：设计/建模、
architecture_skeleton、change_safety_gate、变更影响分析）。

本批改动
-------
· intent.py：detect_semantic 的意图级聚合（`_agg`）除 top1（`_last_sem_alt`）外，
  把**全量排名**旁路存入 `_last_sem_ranking`（纯观测，不参与路由判定）。
· session.py `_intent_confirm_questions`：候选主序改为该排名（按本次输入相关度），
  数量取 `intent.confirm_options_max`（默认 10）；排名为空才退回字典序。
· session.py `_clarify_candidates`（改选重发细条）：同口径动态排序。
· `_INTENT_LABEL` 补 8 个细粒度 Agent 的中文名（进选项后不再显示裸英文 key）。

判据（D1~D5）
------------
D1 选项数 = min(confirm_options_max, 候选意图总数)（当前库 = 10，旧行为恒 4）
D2 选项无重复
D3 **动态性**：至少两组不同输入的候选清单不完全相同（旧行为下恒相同）
D4 当前猜测在 names 时置顶且带（我的猜测）标注
D5 细条候选（_clarify_candidates）同样随输入变化
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import AgentPipeline

# 覆盖不同领域的输入（关键词层全 active=0，走语义层排名）
INPUTS = [
    "帮我弄一下这个",          # 无信号求助（1.4 触发形态）
    "导出最近的审计日志",       # system_mgmt 向
    "这个需求条目质量怎么样",   # requirement_quality 向
    "星链链路预算超了",         # design 向
    "删除这个部件会影响什么",   # impact / change_safety_gate 向
    "把模型发布到库里",         # model_release 向
]

p = AgentPipeline()
p._load_db_agents("1")

_fails: list[str] = []
_passes = 0


def chk(name, ok, detail=""):
    global _passes
    if ok:
        _passes += 1
        print(f"  [PASS] {name}" + (f"  — {detail}" if detail else ""))
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  — {detail}")


def section(t):
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def main() -> int:
    section("候选清单实测（每个输入跑 detect → 构造 1.4 卡）")
    cards = {}
    for inp in INPUTS:
        det = p.router.detect(inp)
        meta = p.router.get_last_meta()
        qs = p._intent_confirm_questions(inp, det, meta.get("sem_alt"))
        opts = (qs[0].get("options") if qs else []) or []
        cards[inp] = opts
        ranking = ", ".join(
            f"{r['intent']}:{r['score']:.3f}"
            for r in (getattr(p.router, "_last_sem_ranking", None) or [])[:5]) or "（空）"
        print(f"\n  输入[{inp}]  detected={det}  sem_alt={(meta.get('sem_alt') or {}).get('intent')}")
        print(f"    语义排名 top5: {ranking}")
        print(f"    候选 {len(opts)} 项:")
        for i, o in enumerate(opts, 1):
            print(f"      {i:2d}. {o[:58]}")

    names = [k for k in (p.router._db_intents or {}).keys() if k != "chat"]
    expect_n = min(10, len(names))

    section("判据")
    for inp, opts in cards.items():
        chk(f"D1 [{inp[:12]}] 选项数 = {expect_n}", len(opts) == expect_n,
            f"got={len(opts)}")
        chk(f"D2 [{inp[:12]}] 选项无重复", len(set(opts)) == len(opts))

    sigs = {inp: tuple(o.split("：")[0] for o in opts) for inp, opts in cards.items()}
    uniq = len(set(sigs.values()))
    chk("D3 动态性：不同输入的候选清单不完全相同（旧行为下 6/6 相同）",
        uniq >= 2, f"{len(sigs)} 个输入 → {uniq} 种不同清单")

    # D4：构造一个 current_intent 命中 names 的场景（直接调函数，不依赖 detect 结果）
    if names:
        _cur = names[0]
        qs = p._intent_confirm_questions("测试置顶", _cur, None)
        opts = qs[0].get("options") or []
        chk("D4 当前猜测置顶且带（我的猜测）",
            bool(opts) and opts[0].startswith(p._INTENT_LABEL.get(_cur, _cur))
            and "（我的猜测）" in opts[0],
            f"first={opts[0][:40] if opts else '无'}")

    # D5：细条候选随输入变化
    cands = set()
    for inp in INPUTS[:3]:
        p.router.detect(inp)
        cands.add(tuple(p._clarify_candidates("chat")))
    chk("D5 细条候选（_clarify_candidates）随输入变化",
        len(cands) >= 2, f"{len(cands)} 种不同候选序列")

    section("结果")
    print(f"  通过 {_passes} 项，失败 {len(_fails)} 项")
    for f in _fails:
        print(f"    · {f}")
    if not _fails:
        print("  全部通过 ✓  意图确认卡候选已按本次输入动态排序")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
