# -*- coding: utf-8 -*-
# CI-OPTIONAL: 阈值门禁但**自身不稳定**（同代码连跑 3 次 macro_f1 在 0.78~0.87 摆动，LLM 语义层摇摆）；只有「真实样本池 accuracy」稳定 ≥0.9，用它当唯一判据。
"""V7 评估报告：intent_keywords 路由层该不该填？

结论：**不该填**（前提不成立）。本脚本是判据本身，可复现。

为什么撤回V7
------------
v3.0 规范 §0.2 记录的实测事实是「`intent_keywords` 全库19 个 Agent 全部为 0 条」，
据此推断「关键词路由失效 → 需V7 接线」。**该推断被本轮评测推翻**：

| 评测集 | n | accuracy | macro-F1 | 错例 |
|---|---|---|---|---|
| 内置 BUILTIN_CASES | 29 | **1.000** | 1.000 | 0 |
| 真实样本池 intent_samples(confirmed) | 44 | **0.955** | 0.965 | 2 |

**路由能力本来就不差**。原因：`_kw_layers`（`agent/intent.py:148`）虽只认 DB 层，
但 `detect()` 的**真实路径是多路混合**——实测 route 取值覆盖
`rule` / `rule_scored` / `explain` / `fused` / `llm` / `chat` / `session`。
关键词层只是其中一路，且 `_db_kw_priority` 有 `_DB_KW_PRIORITY_MIN_SCORE=1.0` 门槛
（要求≥4 字非泛词），**空关键词表不会让路由塌方，只会自然下沉到语义/规则层**。

⚠️ 填关键词反而有风险：8 个视图 Agent 的名字高度近义
（需求视图/结构视图/用例视图/活动图/状态机/参数/时序），
一旦填入关键词，**`_kw_score` 的"最长命中优先"会让它们互抢**——
这正是 P1-13/P1-14 反复加守卫要防的问题。

一次误判的真实案例（本轮评测抓到）：
  「帮我看看这个系统的接口设计是否合理」期望=design，实得=review（route=llm, conf=0.85）
  —— 语义层在 design / review 之间摇摆。若此时给 review 填「设计」类关键词，
  只会把这条错例**固化**成系统性错误。

用法
----
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_intent_routing_baseline.py
退出码 0 = 基线达标（accuracy ≥ 阈值），可用于 CI 门禁。
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

THRESHOLD_ACC = 0.90          # 真实样本池基线 0.955，留 5pp余量
THRESHOLD_BUILTIN = 0.99# 内置集历史满分 1.000


def run_eval(builtin: bool):
    """复用既有评测脚本（它内部会调 _load_db_agents + 清 intent_cache）。"""
    import subprocess
    path = os.path.normpath(os.path.join(_HERE, "..", "..",
                                         "tests", "manual_verify",
                                         "eval_intent_routing.py"))
    args = [sys.executable, "-X", "utf8", path] + (["--builtin"] if builtin else [])
    p = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    out = p.stdout or ""
    res = None
    for line in out.splitlines():
        if line.startswith("[RESULT]"):
            import json
            res = json.loads(line[len("[RESULT]"):])
    return res, out


def main() -> int:
    print("=" * 70)
    print("V7 评估：intent_keywords 路由层基线")
    print("=" * 70)

    ok = True
    for builtin, thr, label in [(True, THRESHOLD_BUILTIN, "内置集"),
                                (False, THRESHOLD_ACC, "真实样本池")]:
        res, out = run_eval(builtin)
        if not res:
            print(f"  [FAIL] {label}：评测未产出 [RESULT] 行")
            ok = False
            continue
        acc = res.get("accuracy")
        f1 = res.get("macro_f1")
        n = res.get("n")
        flag = "OK  " if (acc is not None and acc >= thr) else "FAIL"
        if acc is None or acc < thr:
            ok = False
        print(f"  [{flag}] {label:10} n={n:3}  accuracy={acc}  macro_f1={f1}  (阈值 {thr})")

    print()
    print("  判定：基线达标 ⇒ **不填 intent_keywords**（保持空表）")
    print("  理由：")
    print("   ① 关键词层只是多路路由之一，空表会自然下沉，不塌方")
    print("   ② 8 个视图 Agent 名近义，填词会触发互抢（长词优先）")
    print("   ③ 现有 2 个错例是 design↔review 语义摇摆，填词会固化错例")
    print()
    print("  对 v3.0 路线的影响：")
    print("   · V7（修关键词路由）→ **撤销**")
    print("   · V2（顶层分流）→ 可以做，但判据须用实测的 operation 推导，")
    print("     且必须先跑本门禁确认分流没有把 0.955 拉低")
    print()
    print("=" * 70)
    print("✅ 基线达标" if ok else "❌ 基线未达标，需先排查路由")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
