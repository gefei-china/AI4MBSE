# -*- coding: utf-8 -*-
"""意图识别「DB 自定义 Agent 强特异关键词优先」的自检（P1-13，步骤 B 代码侧）。

背景：实测「生成需求视图/结构视图/参数视图」全被 detect 里的 builtin 建模强信号正则
`(生成|...).{0,24}(视图|...)` 劫持到 design，用户自建 Agent（需求视图生成等）即使配了
intent_keywords 也永远轮不到（db 关键词匹配在强信号正则之后）。

修法：在 builtin 强信号之前插入「db 层强特异命中（≥ _DB_KW_PRIORITY_MIN_SCORE = 1.0，
即至少一个 ≥4 字非泛词）」的优先返回。本脚本证明：
  1) 配强特异关键词 → 命中自建 Agent（新能力）；
  2) 不配关键词 → 行为不变（仍 design，向后兼容）；
  3) 不误伤 builtin 正例；
  4) 阈值守卫不是空转（monkeypatch 阈值=0.0 → 弱信号 2 字词也抢，负对照被抓住）。
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.intent import IntentRouter  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _load_agents():
    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT name, display_name, description, intent_keywords FROM agents "
        "WHERE status='active' ORDER BY name").fetchall()
    c.close()
    return [dict(r) for r in rows]


_ROWS = _load_agents()


def make_router(extra_kw=None):
    rt = IntentRouter()
    sem_idx = []
    for r in _ROWS:
        kws = json.loads(r["intent_keywords"] or "[]")
        if extra_kw and r["name"] in extra_kw:
            kws = list(extra_kw[r["name"]])
        rt.register_keywords(r["name"], kws)
        sem_idx.append({"name": r["name"],
                        "text": f"{r['display_name'] or r['name']} {r['description'] or ''} {' '.join(kws)}"})
    rt.set_semantic_index(sem_idx)
    return rt


def detect(rt, text):
    return rt.detect(text, conn=None)


# ── [F] 功能语义 ───────────────────────────────────────────────
print("[F] DB 强特异关键词优先")

_rt_a = make_router()                      # 现状：自建 Agent 关键词全空
check("F1 不配关键词 → 行为不变（仍 design，向后兼容）",
      detect(_rt_a, "生成需求视图") == "design",
      "intent=%s" % detect(_rt_a, "生成需求视图"))

_rt_b = make_router(extra_kw={"需求视图生成": ["需求视图", "生成需求视图"],
                              "结构视图生成": ["结构视图", "生成结构视图"]})
check("F2 配强特异关键词 → 命中自建 Agent（新能力）",
      detect(_rt_b, "生成需求视图") == "需求视图生成",
      "intent=%s" % detect(_rt_b, "生成需求视图"))
check("F2b 第二条自建 Agent 同样命中",
      detect(_rt_b, "生成结构视图") == "结构视图生成",
      "intent=%s" % detect(_rt_b, "生成结构视图"))
check("F3 不误伤 impact 正例（内置强特异词仍优先）",
      detect(_rt_b, "分析变更影响") == "impact",
      "intent=%s" % detect(_rt_b, "分析变更影响"))
check("F4 不误伤 design 正例（无 db 强特异命中 → 仍走建模强信号）",
      detect(_rt_b, "生成 SysML 模型代码") == "design",
      "intent=%s" % detect(_rt_b, "生成 SysML 模型代码"))
# 报告命令守卫：SP-R 报告路由（「输出…报告」）优先于 db 关键词 —— 否则「输出影响报告」
# 会被 db 层 impact 的「变更影响」抢走（评测集曾抓到 report_generation→impact 错例）。
# ⚠️ 用例须**不含建模词**（视图/建模/代码），否则先被 design 建模强信号（458 行）抢走，测不到 SP-R。
_rt_d = make_router()   # impact 的「变更影响」已在 db 层（builtin 也注册关键词）
check("F5 报告命令（输出…报告）优先于 db 关键词（SP-R 守卫）",
      detect(_rt_d, "输出变更影响报告") == "report_generation",
      "intent=%s" % detect(_rt_d, "输出变更影响报告"))


# ── [M] 阈值守卫负对照 ─────────────────────────────────────────
print("[M] 阈值守卫（≥1.0 不是空转）")

# 2 字特异词「参数」score=0.5 < 1.0 → 正常应不触发 db 优先；阈值降到 0 则被抢。
# ⚠️ 直接测纯函数 `_db_kw_priority`，不走 detect —— 意图缓存是类级共享且指纹不含阈值，
#   走 detect 会被上一次缓存毒化（M1b 首版就踩了）。
_rt_m = make_router(extra_kw={"参数视图生成": ["参数"]})
_t = "生成参数视图".lower()
_before = _rt_m._db_kw_priority(_t)
IntentRouter._DB_KW_PRIORITY_MIN_SCORE = 0.0
try:
    _after = _rt_m._db_kw_priority(_t)
finally:
    IntentRouter._DB_KW_PRIORITY_MIN_SCORE = 1.0
check("M1 阈值 1.0 时弱信号（2 字词 0.5 分）不触发 db 优先",
      _before is None, "before=%s" % _before)
check("M1b 阈值降到 0 → 弱信号也被抢（守卫失效被抓住）",
      _after == "参数视图生成", "after=%s" % _after)


# ── 汇总 ───────────────────────────────────────────────────────
print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
