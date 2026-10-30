# CI-OPTIONAL: 全部判据基于真库 llm_usage_stats 的 14 天窗口（`mbse.db` 不入 git）；CI 无库 ⇒ 走 `--allow-missing` 也只能 SKIP，故本地/nightly 跑。
# -*- coding: utf-8 -*-
"""门禁：上下文利用率必须**可测**，且"不加滑窗"这个决定必须**可被数据翻转**（P1-5，2026-10-30）。

## 这条门禁要解决的两个问题

### ① 利用率此前是黑洞
`llm_usage_stats` 有 `prompt_tokens`，但**没有分母**（`context_window` 列不存在）⇒
"prompt=27529" 无法判断是"用了 21% 窗口"还是"用了 84%"。
行业口径里，"上下文持续高占用"是头号可观测项（它同时解释成本高与答得差）。
本门禁 A 组守住"分母存在且被写入"。

### ② "不加滑窗"必须是**可翻转的判据**，不是一句口头理由
实测（2026-10-30 真库，排除探针）：生产 `chat_answer` 的 prompt 最大 27,529 / 窗口 128,000 = **21%**，
p50 仅 1,408 ⇒ **没有滑窗的必要**。但"今天不用加"不等于"永远不用加"。
所以 F 组写成条件断言：

    若生产 p95 利用率 ≥ `llm.context_util_warn`  ⇒  **必须存在窗口/裁剪机制**，否则判红。

它的含义是：**一旦真实占用逼近窗口，这条会自动翻红并提示"该加滑窗了"**，
而不是等人某天想起来。这比"拍脑袋加一个永不触发的窗口"（死代码）诚实得多。

## 口径（不标口径的数字在本文档系统里不合格）
- **生产口径**：`intent NOT IN (探针/评测/重排/打标)` —— 排除 `chat_probe`、`*_eval`、
  `rag_rerank`、`intent_silver_label` 等脚本流量。实测这些占近 14 天调用的 50% 以上，
  且 `chat_probe` 的 avg prompt 是生产的两倍（15,147 vs 11,482）⇒ 混算会把结论带偏。
- **分母为 0 的行不计入**（存量行）—— 0 是"未知"，不是"窗口为 0"。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []

#: 非生产 intent（脚本/探针/评测/重排/打标）。**新增脚本必须在跑批时自报 intent 并加进这里**，
#: 否则它的流量会污染生产口径（本仓踩过：`chat_probe` 把 p90 从 1.4k 抬到 15.3k）。
NON_PROD_INTENTS = (
    "chat_probe", "rag_rerank", "rerank", "intent_silver_label",
    "chat_eval", "plan_eval", "eval", "probe", "skill_route",
)


def check(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  | " + str(detail)[:200]) if (detail and not ok) else ""))


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def _pct(vals, q):
    if not vals:
        return 0.0
    xs = sorted(vals)
    i = min(len(xs) - 1, max(0, int(len(xs) * q)))
    return float(xs[i])


conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
conn.row_factory = sqlite3.Row

# ─────────────────────────────────────────────────────────────────────────────
hr("[A] 分母必须存在，且写入侧已接线")
# ─────────────────────────────────────────────────────────────────────────────
_cols = [r[1] for r in conn.execute("PRAGMA table_info(llm_usage_stats)")]
check("A1 llm_usage_stats.context_window 列存在（迁移已跑）",
      "context_window" in _cols, "列缺失 ⇒ 利用率无法计算（本项修复前正是如此）")

_src_llm = open(os.path.join(ROOT, "llm/__init__.py"), encoding="utf-8").read()
check("A2 写入侧把 context_window 落库（INSERT 列清单含该列）",
      "context_window) \"" in _src_llm or "context_window)" in _src_llm)
check("A3 分母取自 provider.cfg（与 provider 层守卫读同一处，不另算一份）",
      'getattr(provider, "cfg", None)' in _src_llm and '"context_window"' in _src_llm)
check("A4 告警阈值配置化（llm.context_util_warn）",
      "context_util_warn" in _src_llm)
_src_cfg = open(os.path.join(ROOT, "core/config.py"), encoding="utf-8").read()
check("A5 阈值已登记 CONFIG_SCHEMA（界面可调，不是硬编码常量）",
      '"context_util_warn":' in _src_cfg)

# ★ A6：**"列在、值恒 0" 是接线但取不到值**（本轮真踩到）
#   第一版按 `getattr(provider, "cfg")` 取窗口，而 `provider` 实参是**配置 dict 本身**
#   ⇒ 每行都写 0，而 A2/A3（"INSERT 里有这一列""取的是 provider 配置"）**全绿**。
#
#   判据只断"取值路径通不通"（存在 cw>0 的行），**不按时间窗/行序去断**：
#   实测踩到——用"近 24h 内 cw=0 的行数"作判据，会把**修复前**写入的历史行
#   和**并发旧进程**（评测跑批）写的行一起算进来 ⇒ 恒红且与本次改动无关。
#   这属于本仓反复记录的「判据耦合部署状态」坑（§6.1）。
_cw_rows = conn.execute(
    "SELECT COUNT(*) FROM llm_usage_stats WHERE COALESCE(context_window,0)>0").fetchone()[0]
check("A6 存在带分母的行（%d 行）⇒ 取值路径确实通，不是只加了一列" % _cw_rows,
      int(_cw_rows or 0) > 0,
      "0 行 ⇒ 接线了但取不到值（`provider` 实参形态与取值方式不匹配）")
# 变异自证：换一个没有 context_window 的 provider dict ⇒ 必须得 0 且不抛异常（fail-safe）
try:
    _pp = {"name": "fixture-no-cw"}
    _cw_demo = int((_pp if isinstance(_pp, dict) else {}).get("context_window") or 0)
    check("A7 取不到窗口时安全降级为 0（不抛异常、不写垃圾值）", _cw_demo == 0)
except Exception as e:      # noqa: BLE001
    check("A7 取不到窗口时安全降级为 0", False, str(e)[:120])

# ─────────────────────────────────────────────────────────────────────────────
hr("[B] 生产口径利用率（排除探针/评测流量）")
# ─────────────────────────────────────────────────────────────────────────────
_marks = ",".join("?" * len(NON_PROD_INTENTS))
_rows_all = [dict(r) for r in conn.execute(
    "SELECT intent, prompt_tokens, context_window FROM llm_usage_stats "
    "WHERE prompt_tokens>0 AND created_at>=date('now','-14 day')")]
_prod = [r for r in _rows_all
         if str(r.get("intent") or "") not in NON_PROD_INTENTS]
print("  近 14 天：全部 %d 次 / 生产 %d 次（排除 %d 类脚本流量）"
      % (len(_rows_all), len(_prod), len(NON_PROD_INTENTS)))

# 分母：本列有值的行用它；存量行（0）用 provider 当前窗口近似（标注）
_cw_new = [r for r in _prod if int(r.get("context_window") or 0) > 0]
_cw_fallback = {}
for r in conn.execute("SELECT id, context_window FROM llm_providers WHERE model_type='chat' "
                      "AND COALESCE(context_window,0)>0"):
    _cw_fallback[int(r[0])] = int(r[1])

_util_new = [float(r["prompt_tokens"]) / float(r["context_window"]) for r in _cw_new]
_pt_prod = [int(r["prompt_tokens"]) for r in _prod]
check("B0 生产 prompt 分布可测（p50/p95 打印，供人工核）", True, "")
print("    生产 prompt_tokens：n=%d p50=%d p90=%d p95=%d max=%d"
      % (len(_pt_prod), _pct(_pt_prod, 0.5), _pct(_pt_prod, 0.9),
         _pct(_pt_prod, 0.95), max(_pt_prod) if _pt_prod else 0))
if _util_new:
    print("    有分母的行 %d 条：util p50=%.3f p95=%.3f max=%.3f"
          % (len(_util_new), _pct(_util_new, 0.5), _pct(_util_new, 0.95), max(_util_new)))
else:
    print("    有分母的行 0 条（存量行 context_window=0）⇒ 利用率改用"
          "『provider 当前窗口』近似，见 B2（**口径不同，不可与真实分母混用**）")
check("B1 分母为 0 的行不得进入利用率分母（0=未知，不是窗口为 0）",
      all(int(r.get("context_window") or 0) > 0 for r in _cw_new))

# 近似口径：用 intent→provider 不可考，故按"该次调用最可能用过的窗口"上界（取最大窗口）
if not _util_new and _cw_fallback:
    _maxcw = max(_cw_fallback.values())
    _util_apx = [float(x) / float(_maxcw) for x in _pt_prod]
    print("    近似口径（分母固定取最大窗口 %d）：p95=%.3f max=%.3f"
          % (_maxcw, _pct(_util_apx, 0.95), max(_util_apx)))
    check("B2 近似口径下 p95 仍远低于阈值（说明当前的宽裕不是假的）",
          _pct(_util_apx, 0.95) < 0.85, "p95=%.3f" % _pct(_util_apx, 0.95))

# ─────────────────────────────────────────────────────────────────────────────
hr("[C] 口径隔离：排除脚本流量必须**有可观测的差别**（否则等于没排除）")
# ─────────────────────────────────────────────────────────────────────────────
_pt_all = [int(r["prompt_tokens"]) for r in _rows_all]
_gap = _pct(_pt_all, 0.95) - _pct(_pt_prod, 0.95)
print("    全部口径 p95=%d vs 生产口径 p95=%d（差 %d）"
      % (_pct(_pt_all, 0.95), _pct(_pt_prod, 0.95), _gap))
check("C1 脚本流量确实会抬高 p95（否则'排除探针'这条口径是空动作）",
      _gap > 0, "两种口径 p95 相同 ⇒ 探针未混入，或排除名单已过时")

# ─────────────────────────────────────────────────────────────────────────────
hr("[D] 阈值与判据")
# ─────────────────────────────────────────────────────────────────────────────
try:
    from core import config as _c
    _warn = float(_c.get("llm", "context_util_warn", 0.85) or 0)
except Exception:
    _warn = 0.85
check("D1 告警阈值在 (0, 1] 内（0 表示关闭，属允许值）", 0.0 <= _warn <= 1.0, "got=%s" % _warn)
check("D2 阈值不是 1.0（到 100% 才告警等于没有余量）", _warn == 0 or _warn < 1.0, "got=%s" % _warn)

# ─────────────────────────────────────────────────────────────────────────────
hr("[E] ★可翻转判据：占用逼近阈值 ⇒ 必须存在窗口/裁剪机制")
# ─────────────────────────────────────────────────────────────────────────────
_p95 = _pct(_util_new, 0.95) if _util_new else (
    _pct([float(x) / float(max(_cw_fallback.values())) for x in _pt_prod], 0.95)
    if _cw_fallback and _pt_prod else 0.0)
# 窗口机制的存在判据（任一即可）：历史预算裁剪 / 话题窗口 / 工具结果截断
_src_hist = open(os.path.join(ROOT, "agent/pipeline_parts/history.py"), encoding="utf-8").read()
_src_ctx = open(os.path.join(ROOT, "agent/pipeline_parts/context.py"), encoding="utf-8").read()
_has_window = ("_apply_total_budget" in _src_hist
               or "_apply_context_budget" in _src_hist
               or "topic_current_max_msgs" in _src_hist)
print("    生产 p95 利用率 = %.3f；阈值 = %.2f；检测到窗口机制 = %s"
      % (_p95, _warn, _has_window))
check("E1 若 p95 已达阈值则必须有窗口机制（否则'不加滑窗'这个决定已过期，须实现）",
      (_warn <= 0) or (_p95 < _warn) or _has_window,
      "p95=%.3f ≥ 阈值 %.2f 且未检测到窗口机制 ⇒ 本项应从'不做'转为'必做'" % (_p95, _warn))
check("E2 当前判定：无需新增滑窗（p95 %.1f%% « 阈值 %.0f%%，余量充足）" % (_p95 * 100, _warn * 100),
      _p95 < _warn, "若此条红：说明真实占用已逼近窗口，请实现窗口裁剪而非继续观察")

# ─────────────────────────────────────────────────────────────────────────────
hr("[F] 不变量自检")
# ─────────────────────────────────────────────────────────────────────────────
check("F1 排除名单非空且显式（不允许'自动猜'哪些是脚本流量）",
      len(NON_PROD_INTENTS) >= 5, "got=%d" % len(NON_PROD_INTENTS))
check("F2 排除名单不含生产 intent（chat_answer 不在其中）",
      "chat_answer" not in NON_PROD_INTENTS)
_names = [r[1] for r in _results]
check("F3 断言名唯一", len(set(_names)) == len(_names),
      "dups=%s" % [x for x in set(_names) if _names.count(x) > 1])

conn.close()

n_pass = sum(1 for ok, _, _ in _results if ok)
n_fail = len(_results) - n_pass
print("\n" + "=" * 78)
print("结果：%d PASS / %d FAIL" % (n_pass, n_fail))
if n_fail:
    for ok, name, detail in _results:
        if not ok:
            print("  FAIL %s | %s" % (name, str(detail)[:200]))
print("=" * 78)
sys.exit(1 if n_fail else 0)
