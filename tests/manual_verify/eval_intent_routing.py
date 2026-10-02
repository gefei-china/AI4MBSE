# -*- coding: utf-8 -*-
"""意图识别评测集（混淆矩阵 + 分类指标）——2026-09-25 建，服务路由层重构。

对标依据（调研见对话记录）：语义路由主流做法要求"用真实 utterance 集标定阈值 + 维护混淆矩阵"
（Aurelio semantic-router 的 evaluate()、Azure CLU 指标口径、vLLM Signal–Decision 的组合决策）。
本脚本用**真实 AgentPipeline 的 router** 跑一张小的生产分布集，报 per-class P/R/F1 + 混淆矩阵，
并支持 A/B：`--scored 0` 跑旧行为（关键词层"首个命中即 return"）作基线。

用法：
  .venv/Scripts/python.exe -X utf8 tests/manual_verify/eval_intent_routing.py            # 当前实现
  .venv/Scripts/python.exe -X utf8 tests/manual_verify/eval_intent_routing.py --scored 0 # 旧行为基线
"""
import os
import sys
from collections import Counter, defaultdict

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.pipeline import AgentPipeline  # noqa: E402
from core import config as _cfg  # noqa: E402
from database import get_db  # noqa: E402
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from intent_cases import BUILTIN_CASES  # noqa: E402  内置 29 例（2026-09-26 抽出：与样本池导入同源）

if "--scored" in sys.argv:
    v = sys.argv[sys.argv.index("--scored") + 1] == "1"
    os.environ["MBSE_FORCE_SCORED"] = "1" if v else "0"
    try:
        _cfg.DEFAULT_CONFIG.setdefault("intent", {})["keyword_scored"] = v
    except Exception:
        pass
# --generic 0：关掉"泛词参与打分"（用于**用数据回答**"词表里的泛词是否可移除"）
if "--generic" in sys.argv:
    g = sys.argv[sys.argv.index("--generic") + 1] == "1"
    try:
        _cfg.DEFAULT_CONFIG.setdefault("intent", {})["keyword_generic"] = g
    except Exception:
        pass

# ── 样本来源（2026-09-26）：优先用**样本池**（intent_samples 表，status='confirmed'）──
#  为什么改：原先 29 例硬编码在本文件里，线上真实说法进不来，且 F1 分辨率恒为 1/29。
#  现在评测集可在 设置 → 意图样本 里维护；本脚本自动改读样本池，`--builtin` 可强制回退内置集
#  （用于"库不可用/只想跑基线"的场景）。**必须打印用了哪套样本** —— 否则两次跑分不同却不知为何。
CASES = list(BUILTIN_CASES)
SOURCE = "builtin(29)"
if "--builtin" not in sys.argv:
    try:
        _c = get_db()
        try:
            _rows = _c.execute("SELECT text, intent FROM intent_samples "
                               "WHERE status='confirmed' AND intent<>'' ORDER BY id").fetchall()
        finally:
            _c.close()
        if len(_rows) >= 10:      # 太少（如只确认了 2 条）不足以支撑指标，回落内置集
            CASES = [(r["text"], r["intent"], "") for r in _rows]
            SOURCE = "pool(%d 条已确认样本)" % len(_rows)
    except Exception as _e:
        SOURCE = "builtin(29)（样本池不可用：%s）" % str(_e)[:40]
print("样本来源: " + SOURCE)

pipe = AgentPipeline()
# 2026-09-25 修正评测保真度：**必须**调 `_load_db_agents()`。
# 背景：`AgentPipeline.__init__` 只建 router，DB Agent 关键词与**语义索引**都在真实请求路径
# （stream.py `self._load_db_agents(user)`）里注入。本脚本此前没调它 → 语义索引恒为空、
# 路由池只有内置词表 → 测的是"退化的关键词层"，**语义层从未被这个评测覆盖**
# （实测：不调时「对这个系统做总体设计」等句子的语义分根本取不到）。
pipe._load_db_agents()
rt = pipe.router

rows, y_true, y_pred = [], [], []
for text, want, tag in CASES:
    c = get_db()
    try:
        # ⚠️ 每次评测前清空意图缓存：否则第 2 次跑全部命中 route=cache，
        #    测的是"历史缓存结果"而不是"当前实现"——A/B 会得到完全相同的假结论（2026-09-25 实测踩到）。
        #    缓存可重建（_cache_get/_cache_set），清它安全。
        try:
            c.execute("DELETE FROM intent_cache")
            c.commit()
        except Exception:
            pass
        got = rt.detect(text, conn=c)
        meta = rt.get_last_meta()
    finally:
        c.close()
    y_true.append(want)
    y_pred.append(got)
    rows.append((text, want, got, meta["route"], meta["confidence"], tag))

labels = sorted(set(y_true) | set(y_pred))
cm = defaultdict(Counter)
for a, b in zip(y_true, y_pred):
    cm[a][b] += 1

acc = sum(1 for a, b in zip(y_true, y_pred) if a == b) / len(y_true)
print("== 逐例结果 ==")
bad = 0
for text, want, got, route, conf, tag in rows:
    ok = want == got
    bad += (not ok)
    mark = "  " if ok else "❌"
    print(f"{mark} 「{text}」 期望={want:20s} 实得={got:20s} route={route:12s} conf={conf} {('['+tag+']') if tag else ''}")

print("\n== 混淆矩阵（行=期望，列=实得，仅列非零项）==")
for a in labels:
    cells = ", ".join(f"{b}:{n}" for b, n in sorted(cm[a].items()) if n)
    print(f"  {a:22s} {cells}")

print("\n== 分类指标 ==")
print(f"  accuracy = {acc:.3f}  错例 {bad}/{len(rows)}")
f1s = []
for lb in labels:
    tp = cm[lb][lb]
    fp = sum(cm[a][lb] for a in labels if a != lb)
    fn = sum(cm[lb][b] for b in labels if b != lb)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    f1s.append(f1)
    print(f"  {lb:22s} P={prec:.2f} R={rec:.2f} F1={f1:.2f} (n={tp+fn})")
print(f"  macro-F1 = {sum(f1s)/len(f1s):.3f}")

_n_trap = sum(1 for *_, tg in rows if tg == "泛词陷阱")
# ⚠️ 2026-10-02（评估时发现）：无样本时必须报 **null**，不能报 0.0 —— 否则 RESULT 里
# `generic_trap_acc: 0.0000` 会被读成"泛词陷阱子集全部判错"（实际是"本次没有该类样本"）。
# 本仓 P0-a 的纪律：**数据不足报 unknown/None，不许报 0**（假绿/假红同样有害）。
_trap_acc = (sum(1 for t, w, g, r, cf, tg in rows if tg == "泛词陷阱" and w == g) / _n_trap) if _n_trap else None
if _n_trap:
    print(f"  泛词陷阱子集准确率 = {_trap_acc:.3f}（这是本次报障的专项类别）")
else:
    print("  泛词陷阱子集：本次 n=0（样本池来源的用例标签在库里，该子集无样本）→ 指标记 null，不参与解读")
_trap_json = "null" if _trap_acc is None else "%.4f" % _trap_acc
print("\n[RESULT]" + f'{{"accuracy": {acc:.4f}, "macro_f1": {sum(f1s)/len(f1s):.4f}, '
                    f'"generic_trap_acc": {_trap_json}, "generic_trap_n": {_n_trap}, "n": {len(rows)}}}')
sys.exit(0)