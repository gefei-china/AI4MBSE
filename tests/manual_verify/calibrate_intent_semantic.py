# -*- coding: utf-8 -*-
"""意图路由：**语义层弱信号**标定与验收（2026-09-25，「让语义层接住弱信号句」）。

## 要解决什么
关键词层收紧为"泛词不单独构成信号"（agent/intent.py 的 `_GENERIC_KW` + `_kw_score`）之后，
单泛词句（如「对这个系统做总体设计」只命中泛词"设计"）在关键词层**判无信号** → 下沉语义层。
但语义层也**给不出结论** → 一路掉到 LLM 兜底：结果虽对，却白付一次调用（用户说的
"语义层还没做到能接住弱信号句"）。

## 根因（本脚本实测，两条，缺一不可）
① **答词表不一致**（主因，比阈值致命）：语义索引装的全是 Agent 名+描述（20 条），其中大半是
   「结构视图生成」「参数视图生成」这类**细粒度子 Agent**；于是语义层 top1 常是**子 Agent 名**
   （实测「帮我生成这个系统的SysML v2模型代码」→「结构视图生成」0.7255，期望 design）
   —— 阈值再松也只会路由到错的 Agent 上。修法：`_SEMANTIC_UTTERANCES` 补粗粒度示例 utterance。
② **双阈值把弱信号挡在门外**：`intent_threshold_dense`(0.49) 是"够不够低"，而
   `intent_sem_low_dense`(0.69) 是"低置信一律不硬检索"的下限；top1 ∈ [0.49,0.69) 被
   `if top_score < sem_low: return ""` 直接拦掉，**真正的门槛是 0.69**。修法：标定（本脚本）。

## 方法（四段）
0) 前置自证：配置 patch 通道可用 / 走的是 dense 路 / 索引规模
1) **真实 detect() 剖面**：对评测集逐例跑真链路（禁缓存 / LLM 兜底置空但**记录是否被调用**），
   对比「补 utterance 前 / 后」到底少了几句掉 LLM 兜底
   ⚠️ 只按 `_kw_score==0` 挑"弱信号句"是**错的**（那样会漏掉规则/正则等**更前置**的层已经
      兜住的句子，把"其实没走到语义层"的句子算成弱信号）—— 必须按真实剖面里
      "`detect_semantic` 被调用且返回空"来判定。
2) 对"走到语义层却没接住"的句子，取 top1/top2 明细
3) 网格扫描 (th, sem_high, 弱档领先倍率, 强档领先倍率)：多接 × 零接错 × 零冲突
4) 最优组合真实链路自证：需 LLM 兜底的句子数、准确率、路由

用法：
  .venv/Scripts/python.exe -X utf8 tests/manual_verify/calibrate_intent_semantic.py
每次运行都**真实**跑链路（不采样缓存）—— 阈值标定的结论要能随时复现，缓存会让第二次跑出假结论。
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.intent import IntentRouter  # noqa: E402
from agent.pipeline import AgentPipeline  # noqa: E402
from core import config as _cfg  # noqa: E402
from database import get_db  # noqa: E402
from semantic import SemanticSearch  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from intent_cases import BUILTIN_CASES  # noqa: E402  内置 29 例（与样本池导入同源）

MEASURE_CACHE = os.path.join(ROOT, "tmp", "v2docs", "intent_semantic_measure.json")

# ── 评测集：与 eval_intent_routing.py 同源（29 例 / 9 类意图）──
#  2026-09-26：改为**优先读样本池**（intent_samples 表，status='confirmed'）——
#  这样"重标定"用的是**当前**评测集，人在设置页确认多少条，标定的样本量就跟着涨多少。
#  这正是本轮"扩集"的意义：29 例撑不起阈值窗口（th=0.64 的可用区间是被其中两句夹出来的）。
CASES = list((t, i) for t, i, _ in BUILTIN_CASES)
_SRC = "builtin(29)"
try:
    _c = get_db()
    try:
        _rows = _c.execute("SELECT text, intent FROM intent_samples "
                           "WHERE status='confirmed' AND intent<>'' ORDER BY id").fetchall()
    finally:
        _c.close()
    if len(_rows) >= 10:
        CASES = [(r["text"], r["intent"]) for r in _rows]
        _SRC = "pool(%d 条已确认样本)" % len(_rows)
except Exception as _e:
    _SRC = "builtin(29)（样本池不可用：%s）" % str(_e)[:40]
print("样本来源: " + _SRC)

KEYS = ("intent_threshold_dense", "intent_sem_low_dense", "intent_sem_mid_dense",
        "intent_sem_high_dense", "intent_lead_weak", "intent_lead_strong")


def cfg_now():
    return {k: float(_cfg.get("embedding", k, -1)) for k in KEYS}


def emb_patch(**over):
    """patch embedding 组阈值并 reload，**自证**生效（否则后面所有扫描都是假绿）。"""
    d = _cfg.DEFAULT_CONFIG.setdefault("embedding", {})
    d.update(over)
    _cfg.reload()
    return [k for k, v in over.items() if abs(float(_cfg.get("embedding", k, -1)) - float(v)) > 1e-9]


def gate(top1, top2, name, want, th, hi, lw, ls):
    """`detect_semantic` 分档判定的**同构重算**（离线扫描用）。

    - 与线上一致：低于 th 不接；弱档（th ≤ top1 < hi）要求 glossary 命中（离线按**无**处理，
      即最保守）或 `top1 ≥ lw × top2`；强档要求 `top1 ≥ ls × top2`；top1 为 chat 不接。
    """
    if not name or name == "chat":
        return False
    if top1 < th:
        return False
    if top1 < hi:
        return bool(top2) and top1 >= top2 * lw
    return (not top2) or top1 >= top2 * ls


def profile(rt):
    """真实链路剖面（禁缓存 / LLM 兜底置空但记录调用）。返回每例 dict + 汇总。"""
    _og, _os, _ol = IntentRouter._cache_get, IntentRouter._cache_set, IntentRouter.detect_llm
    IntentRouter._cache_get = lambda self, *a, **k: None
    IntentRouter._cache_set = lambda self, *a, **k: None
    orig_sem, orig_llm = rt.detect_semantic, rt.detect_llm
    rec = {}
    rows = []
    try:
        def llm_wrap(text, *a, **k):
            rec["llm"] = True
            return ("", 0.0)          # 置空：让结果与"LLM 不可用"对齐，跑批可比

        rt.detect_llm = llm_wrap
        IntentRouter.detect_llm = llm_wrap
        for text, want in CASES:
            rec.clear()
            rec.update({"llm": False, "sem": False, "sem_result": "", "sem_score": 0.0})

            def sem_wrap(t, *a, **k):
                r = orig_sem(t, *a, **k)
                rec["sem"] = True
                rec["sem_result"] = r
                rec["sem_score"] = float(getattr(rt, "_last_sem_score", 0.0) or 0.0)
                return r

            rt.detect_semantic = sem_wrap
            got = rt.detect(text)
            meta = rt.get_last_meta()
            rows.append({"text": text, "want": want, "got": got, "route": meta["route"],
                         "conf": meta["confidence"], "llm": rec["llm"], "sem": rec["sem"],
                         "sem_result": rec["sem_result"], "sem_score": round(rec["sem_score"], 4),
                         "ok": got == want})
    finally:
        rt.detect_semantic, rt.detect_llm = orig_sem, orig_llm
        IntentRouter._cache_get, IntentRouter._cache_set, IntentRouter.detect_llm = _og, _os, _ol
    return rows


def summarize(tag, rows):
    acc = sum(r["ok"] for r in rows)
    need_llm = [r for r in rows if r["llm"]]
    wrong = [r for r in rows if not r["ok"]]
    print("  %-22s 准确率 %d/%d   需 LLM 兜底 %d 句   错例 %d 句"
          % (tag, acc, len(rows), len(need_llm), len(wrong)))
    return {"acc": acc, "need_llm": need_llm, "wrong": wrong}


# ── 标定的**固定参照系**：本轮改动之前的 dense 参数值 ──────────────────────────────
#  为什么写死而不用"当前配置"：本脚本是**证据/复现**用（要能随时重跑同一结论）。若把"当前配置"
#  当基线，标定落地后重跑就会把**已改好的值**当成待标定的坏值 → 得出"标定后 27/29"的
#  反向结论（实测踩到）。故基线固定为改动前的值，每次运行都从它出发、得出同一推荐值。
_BEFORE = {"intent_threshold_dense": 0.49, "intent_sem_low_dense": 0.69,
           "intent_sem_mid_dense": 0.69, "intent_sem_high_dense": 0.76,
           "intent_lead_weak": 1.15, "intent_lead_strong": 1.50}


def stuck_detail(rt, rows) -> list:
    """取"到达语义层但没接住"的句子，补上 top1/top2 明细（供扫描用）。"""
    out = []
    for r in rows:
        if not r["sem"] or r["sem_result"]:
            continue
        with redirect_stdout(io.StringIO()):
            scored = SemanticSearch().rank(r["text"], rt._semantic_index, top_k=2,
                                           threshold=0, key="text")
        t1 = scored[0][0] if scored else 0.0
        n1 = scored[0][1].get("name") if scored else ""
        t2 = scored[1][0] if len(scored) >= 2 else 0.0
        out.append({**r, "top1": n1, "top1_score": round(t1, 4), "top2_score": round(t2, 4)})
    return out


def grid_scan(detail: list, fused: list) -> tuple:
    """在 (th, hi, lead_w, lead_s) 网格上求"零接错 × 零改坏 → 接住最多 → 门槛最高 → 守卫最紧"。

    为什么"门槛最高"排在"守卫最紧"前面：绝对相似度门槛比比值门槛更可解释、更抗量纲漂移；
    宁可"门槛高 + 守卫松"，也不要"门槛低 + 守卫紧" —— 后者把判定押在一个在 dense 下
    几乎不可达的比值上（这正是修前的病根：0.49 的低门槛 + 1.5x 的严守卫）。
    """
    grid = []
    for th in [round(0.30 + 0.02 * i, 2) for i in range(24)]:            # 0.30~0.76
        for hi in (0.70, 0.76, 0.80, 0.85):
            for lw in (1.15, 1.05, 1.00):
                for ls in (1.50, 1.10, 1.05, 1.00):
                    got = [d for d in detail
                           if gate(d["top1_score"], d["top2_score"], d["top1"], d["want"],
                                   th, hi, lw, ls)]
                    okd = [d for d in got if d["top1"] == d["want"]]
                    badd = [d for d in got if d["top1"] != d["want"]]
                    # 已接住的句子是否被"改坏"（语义结果变了）
                    chg = [f for f in fused
                           if gate(f["sem_score"], 0.0, f["sem_result"], f["want"], th, hi, lw, ls)
                           and f["sem_result"] != f["want"]]
                    grid.append({"th": th, "hi": hi, "lw": lw, "ls": ls, "n_get": len(got),
                                 "n_ok": len(okd), "n_bad": len(badd),
                                 "bad": [d["text"] for d in badd], "n_chg": len(chg)})
    best = None
    for g in grid:
        if g["n_bad"] or g["n_chg"]:
            continue
        key = (g["n_ok"], g["th"], g["lw"], g["ls"], -abs(g["hi"] - 0.76))
        if best is None or key > best[0]:
            best = (key, g)
    return (best[1] if best else None), grid


def main():
    print("=" * 84)
    print("意图路由 · 语义层弱信号 标定与验收")
    print("=" * 84)

    # ── 0) 前置自证 ──
    print("\n── 0) 前置自证 ──")
    cur = cfg_now()                       # 现网配置（结束时要还原到它，脚本不留副作用）
    bad = emb_patch(**cur)
    print("  配置文件       : %s（存在=%s）" % (_cfg.CONFIG_PATH, os.path.exists(_cfg.CONFIG_PATH)))
    print("  patch 通道自证 : %s" % ("✅ 生效" if not bad else "⛔ 无效 %s" % bad))
    if bad:
        print("  → 配置被文件/环境覆盖，patch DEFAULT_CONFIG 不生效，标定无意义，退出。")
        sys.exit(2)
    print("  现网参数       : th=%.2f low=%.2f mid=%.2f high=%.2f lead_w=%.2f lead_s=%.2f"
          % tuple(cur[k] for k in KEYS))
    print("  标定基线(固定) : th=%.2f low=%.2f mid=%.2f high=%.2f lead_w=%.2f lead_s=%.2f"
          % tuple(_BEFORE[k] for k in KEYS))
    emb_patch(**_BEFORE)                  # 从基线出发测量

    pipe = AgentPipeline()
    # ⚠️ 必须调 `_load_db_agents()`：`AgentPipeline.__init__` 只建 router，语义索引与 DB Agent
    #    关键词都在真实请求路径（stream.py `self._load_db_agents(user)`）里注入。不调它 →
    #    索引恒为空 → 测的是"内置词表 + 规则"的退化路由（eval_intent_routing.py 早期就如此）。
    pipe._load_db_agents()
    rt = pipe.router
    n_agent = len([i for i in rt._semantic_index if i.get("kind") != "intent_utterance"])
    n_utt = len(rt._semantic_index) - n_agent
    print("  语义索引       : 共 %d 条 = Agent 条目 %d + 意图示例 utterance %d"
          % (len(rt._semantic_index), n_agent, n_utt))

    # ── 1) 补 utterance 前后对照（真实 detect() 剖面）──
    print("\n── 1) 补示例 utterance 的前后对照（真实 detect() 剖面；禁缓存 / LLM 置空）──")
    keep = IntentRouter._SEMANTIC_UTTERANCES
    try:
        IntentRouter._SEMANTIC_UTTERANCES = {}
        pipe._load_db_agents()
        before = profile(rt)
        detail0 = stuck_detail(rt, before)
    finally:
        IntentRouter._SEMANTIC_UTTERANCES = keep
    pipe._load_db_agents()
    after = profile(rt)
    b = summarize("A0 索引仅 Agent 条目", before)
    a = summarize("A1 补意图示例 utterance", after)
    print("  改善：需 LLM 兜底的句子 %d → %d（少 %d）；准确率 %d → %d"
          % (len(b["need_llm"]), len(a["need_llm"]),
             len(b["need_llm"]) - len(a["need_llm"]), b["acc"], a["acc"]))
    changed = [(x["text"], x["sem_score"], y["sem_score"], y["got"], y["route"])
               for x, y in zip(before, after) if x["route"] != y["route"] or x["got"] != y["got"]]
    for t, s0, s1, got, route in changed:
        print("    「%s」sem_score %.4f → %.4f  ⇒ %s / %s" % (t, s0, s1, got, route))

    # ── 2) 走到语义层却"没接住"的句子（这才是真正的弱信号句）──
    detail = stuck_detail(rt, after)
    print("\n── 2) 走到语义层、但语义层没接住的句子：%d 句（真实弱信号句）" % len(detail))
    if not detail:
        print("   （无 —— 语义层已接住全部到达它的句子）")
    for d in detail:
        print("    top1=%-18s %.4f (2nd %.4f)  期望=%-20s route=%-8s 「%s」"
              % (d["top1"], d["top1_score"], d["top2_score"], d["want"], d["route"], d["text"]))
    if detail and SemanticSearch().last_backend != "dense":
        print("  ⚠️ 语义后端非 dense → dense 阈值扫描无效，请先修 embedding provider。")

    # ── 3) 网格扫描 ──
    fused = [r for r in after if r["sem"] and r["sem_result"]]  # 已接住的（阈值放松不应改坏）
    print("\n── 3) 参数网格扫描（对'没接住'的句子求：多接 × 零接错 × 零冲突）──")
    best0, grid0 = grid_scan(detail0, [r for r in before if r["sem"] and r["sem_result"]])
    best, grid = grid_scan(detail, fused)
    g0 = max(grid0, key=lambda x: (x["n_ok"], x["th"])) if grid0 else None
    print("  【对照】索引只有 Agent 条目：网格内**最多正确接住 %d 句**（该格共接住 %d 句、接错 %d 句）"
          % (g0["n_ok"] if g0 else 0, g0["n_get"] if g0 else 0, g0["n_bad"] if g0 else 0))
    print("         → 这是「补示例 utterance」必要性的证据：不补时句子的 top1 会落到"
          "「结构视图生成」这类**子 Agent 名**上，调阈值必须**同时接错**（本格 2 句）——"
          "不存在「零接错且接住 ≥1」的格子，故下面表格只列补过 utterance 的结果")
    if best:
        print("  【正式】补示例 utterance 后：最优格正确接住 %d 句（共接住 %d、接错 %d）"
              % (best["n_ok"], best["n_get"], best["n_bad"]))
    show = [g for g in grid if g["n_get"] > 0]
    print("  可行组合 %d 个（列出接住数 ≥1 且零接错的）：" % len(show))
    print("  th    hi    lead_w lead_s  接住  正确  接错")
    for g in sorted([g for g in show if g["n_bad"] == 0], key=lambda x: (-x["n_ok"], -x["th"], -x["ls"]))[:12]:
        print("  %.2f  %.2f  %.2f   %.2f     %3d   %3d   %3d"
              % (g["th"], g["hi"], g["lw"], g["ls"], g["n_get"], g["n_ok"], g["n_bad"]))
    if not [g for g in show if g["n_bad"] == 0]:
        print("  （无「零接错」组合：语义索引对这些句子的 top1 本身就是错的 → 光调阈值没用，"
              "应先补/改示例 utterance 或修子 Agent 描述，见第 1、2 段输出）")
    print("\n  推荐组合：%s" % (
        "th=%.2f hi=%.2f lead_w=%.2f lead_s=%.2f → 多接住 %d 句（全部正确）、已接住的 0 句被改坏"
        % (best["th"], best["hi"], best["lw"], best["ls"], best["n_ok"])
        if best else "不存在「零接错」组合 → 阈值不动，仅保留第 1 段的 utterance 改善"))

    # ── 4) 最优组合真实链路自证 ──
    if best:
        print("\n── 4) 真实 detect() 自证（禁缓存 / LLM 置空但计数）──")
        emb_patch(intent_threshold_dense=best["th"], intent_sem_low_dense=best["th"],
                  intent_sem_mid_dense=best["th"], intent_sem_high_dense=best["hi"],
                  intent_lead_weak=best["lw"], intent_lead_strong=best["ls"])
        try:
            final = profile(rt)
        finally:
            emb_patch(**cur)   # 还原现场：脚本不留副作用
        f = summarize("标定后", final)
        print("  仍需 LLM 兜底：%s" % ("；".join(r["text"] for r in f["need_llm"]) or "无"))
        print("  错例         ：%s" % ("；".join("%s→%s" % (r["text"], r["got"]) for r in f["wrong"]) or "无"))
        print("  对比标定前   ：需 LLM %d → %d；准确率 %d → %d"
              % (len(a["need_llm"]), len(f["need_llm"]), a["acc"], f["acc"]))
        from collections import Counter
        print("  路由分布     ：%s" % dict(Counter(r["route"] for r in final)))
        print("  逐例（标定后）:")
        for r in final:
            print("    %s route=%-14s conf=%.2f sem=%-18s %-20s 「%s」"
                  % ("  " if r["ok"] else "❌", r["route"], r["conf"], r["sem_result"] or "-",
                     r["got"], r["text"]))

    json.dump({"current": cur, "best": best, "n_index": len(rt._semantic_index),
               "before_need_llm": [r["text"] for r in b["need_llm"]],
               "after_need_llm": [r["text"] for r in a["need_llm"]]},
              open(MEASURE_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n[RESULT]" + json.dumps({"current": cur, "best": best}, ensure_ascii=False))


if __name__ == "__main__":
    main()