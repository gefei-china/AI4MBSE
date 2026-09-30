# -*- coding: utf-8 -*-
"""P0-1 语义 ↔ LLM 互证（融合矩阵补齐）—— 常驻断言 + 变异自证。

## 修的是什么
唯一的 confirmed 错例「帮我看看这个系统的接口设计是否合理」：
语义层 design(~0.55) > review(0.48)，LLM 判 review(0.85) → 采纳 review 且
`needs_clarification=False` —— **相反证据被静默丢弃**。根因不是 LLM 能力，是融合矩阵
只做了「关键词 ↔ 语义」一格，「语义 ↔ LLM」那一格缺失（断链 1）。

## 定稿设计（走过一版弯路，见下）
本改动**只做"互斥 → 转澄清"，刻意不做"采信语义"的翻盘**。
走过的弯路（记录以免重蹈）：初版加了 `llm_takeover`（语义够确信就翻盘），门槛用
"意图级 lead ≥ 1.15"。实测该例 lead 在 **1.145~1.158** 间随 embedding 抖动、恰好跨过门槛
两侧 → 判定不稳定；而把门槛调到"刚好命中该例"就是用测试集调参（方案 §4 明确反对）。
更根本地：语义 0.55 推翻 LLM 0.85 属**弱证据推翻强结论**，依据不足。
故定稿判据 = "语义意图级 top1 与 LLM 互斥 + 语义分数达最低识别阈（0.49）" → 转澄清。
是否允许翻盘，待 P1-3 标定 α/δ 后再定。

## 两个必须一起修的机制问题（方案的原始改法低估了这两点，否则改了也不生效）
1. **走到 LLM 段时语义结果必已被丢弃** —— `detect()` 里 `sem = detect_semantic(text); if sem: return`
   已把"语义有结论"的分支全部拦走，故 LLM 段永远看不到语义意见；语义"弃权"
   （达阈但未过采纳门槛）更从未被传递。→ 须新开一条 `_last_sem_alt` 旁路通道。
2. **语义 top2 常是同一意图的另一条 utterance** —— `_SEMANTIC_UTTERANCES` 让 design 在索引里
   出现多次，原始 top1/top2 是 design 0.5541 / design 0.4993（比值 0.90 → 判"未领先"）。
   → 须**按意图名聚合**取 max 后再比领先，否则判据恒失真。

## 断言（A1~A8）与变异自证（M1~M3，只认"新增 FAIL"）
- M1 删互证段（注回旧写法）      → A1 必须新增 FAIL
- M2 识别阈抬到不可达（99）      → A1 必须新增 FAIL（退回 route='llm'）
- M3 破坏意图级聚合（key 换 id） → A2 必须新增 FAIL（runner 退化成 design 自己）
对照项 A6（高置信语义路径）在三个变异下都必须仍 PASS —— 证明变异是"定点"的。

用法：.venv/Scripts/python.exe -X utf8 tools/verify/verify_intent_llm_fusion.py
"""
import os
import sys

ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.pipeline import AgentPipeline   # noqa: E402
from database import get_db                # noqa: E402

OK, FAIL = [], []


def chk(name, cond, ev=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  <- " + ev) if ev else ""))


Q_BAD = "帮我看看这个系统的接口设计是否合理"   # 语义 design 领先 review；LLM 稳定判 review(0.85)
Q_STRONG = "对这个系统做总体设计"              # 高置信语义（不经 LLM 段）
Q_BUDGET = "帮我看看这个项目的预算"            # LLM 低置信 + 语义不领先 → 不得硬选


def run(router, text):
    c = get_db()
    try:
        c.execute("DELETE FROM intent_cache")
        c.commit()
        got = router.detect(text, conn=c)
        return got, router.get_last_meta(), getattr(router, "_last_sem_alt", None)
    finally:
        c.close()


def run_until(router, text, want_route, tries=3):
    """LLM 链路有抖动，取首次达到期望 route 的一次；耗尽则返回最后一次（便于诊断）。"""
    last = None
    for _ in range(tries):
        last = run(router, text)
        if last[1]["route"] == want_route:
            return last
    return last


pipe = AgentPipeline()
pipe._load_db_agents()          # 必须：否则语义索引为空，本测试的判据面整体不存在
rt = pipe.router

print("── A. 判定面：互斥 → 不硬选、转澄清 ──")
got, meta, alt = run_until(rt, Q_BAD, "llm_conflict")
print(f"     {Q_BAD!r} -> got={got} route={meta['route']} conf={meta['confidence']:.4f} "
      f"clarify={meta['needs_clarification']}")
print(f"     alt={alt}")
chk("A1 语义与 LLM 互斥 → route=llm_conflict（不硬选）", meta["route"] == "llm_conflict",
    f"实得={meta['route']}")
chk("A1b 返回的仍是 LLM 侧标签（不翻盘）", got == "review", f"实得={got}")
chk("A1c 澄清标志置位（不再静默误路由）", meta["needs_clarification"] is True)
chk("A2 意图级聚合生效：runner 是竞争意图 review（非 design 自己）",
    bool(alt) and alt["runner"] == "review", f"runner={None if not alt else alt['runner']}")
chk("A2b 语义分数达最低识别阈 0.49", bool(alt) and alt["score"] >= 0.49,
    f"score={None if not alt else round(alt['score'], 4)}")
chk("A8 _last_sem_alt 结构完整（5 键）",
    bool(alt) and set(alt) == {"intent", "score", "runner", "runner_score", "lead"},
    f"keys={None if not alt else sorted(alt)}")

print("── B. 澄清判定面 _should_confirm_intent ──")
chk("A4 llm_conflict → 问", pipe._should_confirm_intent(Q_BAD, {"route": "llm_conflict", "confidence": 0.6}) is True)
chk("A5 高置信 llm（conf>=0.85）→ 不问", pipe._should_confirm_intent(Q_BAD, {"route": "llm", "confidence": 0.9}) is False)

print("── C. 不误伤（边界路径必须原样）──")
g_s, m_s, _ = run(rt, Q_STRONG)
print(f"     {Q_STRONG!r} -> got={g_s} route={m_s['route']} conf={m_s['confidence']:.4f}")
chk("A6 高置信语义路径不变（semantic / design）",
    g_s == "design" and m_s["route"] == "semantic", f"got={g_s} route={m_s['route']}")
g_b, m_b, alt_b = run(rt, Q_BUDGET)
print(f"     {Q_BUDGET!r} -> got={g_b} route={m_b['route']} alt_score="
      f"{None if not alt_b else round(alt_b['score'], 4)}")
chk("A7 低置信猜测仍不硬选（chat / llm_weak）",
    g_b == "chat" and m_b["route"] == "llm_weak", f"got={g_b} route={m_b['route']}")

# ───────────────────────── 变异自证 ─────────────────────────
print("── D. 变异自证（只认「新增 FAIL」）──")


def load_src():
    return open(os.path.join(ROOT, "agent", "intent.py"), encoding="utf-8").read().replace("\r\n", "\n")


def make_router(src):
    ns = {}
    exec(compile(src, "<mutant>", "exec"), ns)
    m = ns["IntentRouter"]()
    m._semantic_index = rt._semantic_index      # 复用同一份索引（含意图 utterance）
    m._db_intents = dict(rt._db_intents)
    return m


BASE = load_src()
SEC_DELETE = (
    '            _alt = getattr(self, "_last_sem_alt", None)\n'
    '            if (_alt and _alt["intent"] and _alt["intent"] != llm_intent\n'
    '                    and _alt["score"] >= float(self._cfg_get("llm_sem_conflict_min", 0.49))):\n'
    '                return self._done(llm_intent, "llm_conflict", min(llm_conf, 0.6), text, fp)\n'
)
MUTANTS = [
    ("M1 删互证段（注回旧写法）", BASE.replace(SEC_DELETE,
        '            return self._done(llm_intent, "llm", llm_conf, text, fp)\n', 1)),
    ("M2 识别阈抬到不可达 score>=99", BASE.replace(
        'and _alt["score"] >= float(self._cfg_get("llm_sem_conflict_min", 0.49))',
        'and _alt["score"] >= 99.0', 1)),
    ("M3 破坏意图级聚合（key 换 id）", BASE.replace("_agg[_nm] = _sc", "_agg[id(_it)] = _sc", 1)
        .replace("_agg.get(_nm, 0.0)", "_agg.get(id(_it), 0.0)", 1)),
]

base_ok = (meta["route"] == "llm_conflict") and (bool(alt) and alt["runner"] == "review")
chk("D0 基线 A1/A2 均 PASS（否则变异自证无意义）", base_ok)
for label, src in MUTANTS:
    assert src != BASE, f"{label}: 变异未生效（替换没命中）"
    mr = make_router(src)
    g, m, a = run(mr, Q_BAD)
    gs, ms, _ = run(mr, Q_STRONG)
    a1_fail = m["route"] != "llm_conflict"          # 判「澄清未发生」——与 LLM 抖动解耦
    a2_fail = not (bool(a) and a["runner"] == "review")
    ctl_ok = (gs == "design" and ms["route"] == "semantic")
    print(f"     {label}: got={g} route={m['route']} runner={None if not a else a['runner']} "
          f"| 对照 A6={'PASS' if ctl_ok else 'FAIL'}")
    chk(f"{label} → 断言新增 FAIL", a1_fail or a2_fail, f"A1_fail={a1_fail} A2_fail={a2_fail}")
    chk(f"{label} → 对照项 A6 未受影响", ctl_ok)

print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：\n  " + "\n  ".join(FAIL))
    sys.exit(1)
