# CI-OPTIONAL: [backlog] 从未接线，待逐个定性（接入 pr 需先过「全新克隆+空库能绿」）；未接线（2026-11-04 盘点未覆盖，待逐个定性） 
# -*- coding: utf-8 -*-
# CI-OPTIONAL: 阈值门禁且**评测本身非确定**（2026-11-09 实测：同代码连跑 3 次，
# 真实样本池 accuracy 在 0.8542~0.8958 摆动，macro_f1 亦摆动；LLM 语义层摇摆）。
# ⇒ 唯一判据 = 重复 3 次取**最差**的 accuracy ≥ 0.90；阈值不因摆动而调整，
#   内置集与 macro_f1 只作观测。取最差是更严格，不是放宽。
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

⚠️ 上表是 **2026-11-04 当时的历史快照**（n=44）。样本池会随人工确认增长，
   当前 n 由 `eval_intent_routing.py` 每次实跑给出，**不得把历史的 0.955 当成当前基线**。
   本门禁自 2026-11-09 起只以「当次真实样本池 accuracy ≥ 0.90」为唯一判据，
   内置集与 macro_f1 一律降为观测值（实现与本文件第 3 行的设计声明对齐）。

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

THRESHOLD_ACC = 0.90          # 唯一门禁判据；阈值不因样本池变化或评测摆动而调整
THRESHOLD_BUILTIN = 0.99      # 仅作观测参考，不参与 exit（见文件头部设计声明）
# 真实样本池重复次数；取最差值判定，对 LLM 语义层摆动鲁棒。
# 可由环境变量调整（例如空库/慢环境降到 1），判据与阈值本身不受影响。
REPEAT_GATE = max(1, int(os.environ.get("ROUTING_BASELINE_REPEAT", "3")))


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

    print("  判据：真实样本池重复 %d 次，取**最差** accuracy ≥ %.2f 决定 exit；"
          % (REPEAT_GATE, THRESHOLD_ACC))
    print("        内置集与 macro_f1 只输出观测值，不参与判定（阈值未下调）")

    # 内置集：一次观测（不参与 exit）
    res_b, _ = run_eval(True)
    if res_b:
        print("  [观测] 内置集        n=%3s  accuracy=%s  macro_f1=%s  (参考阈值 %s，不参与 exit)"
              % (res_b.get("n"), res_b.get("accuracy"), res_b.get("macro_f1"),
                 THRESHOLD_BUILTIN))
    else:
        print("  [观测] 内置集：评测未产出 [RESULT] 行")

    # 真实样本池：重复取最差（同代码同一样本池仍会摆动，取平均或取最好都会自欺）
    runs = []
    gate_n = None
    for i in range(1, REPEAT_GATE + 1):
        res, _ = run_eval(False)
        if not res:
            print("  [判据] 第 %d/%d 次：未产出 [RESULT] 行（按不达标计）" % (i, REPEAT_GATE))
            runs.append(None)
            continue
        acc = res.get("accuracy")
        runs.append(acc)
        gate_n = res.get("n")
        print("  [判据] 第 %d/%d 次  n=%3s  accuracy=%s  macro_f1=%s"
              % (i, REPEAT_GATE, gate_n, acc, res.get("macro_f1")))
    valid = [a for a in runs if a is not None]
    worst = min(valid) if len(valid) == REPEAT_GATE else None
    ok = worst is not None and worst >= THRESHOLD_ACC
    print()
    print("  样本池快照：n=%s  重复 %d 次 accuracy=%s  取最差 %s 与阈值 %.2f 比较"
          % (gate_n, REPEAT_GATE, [round(a, 4) if a is not None else None for a in runs],
             worst, THRESHOLD_ACC))
    print("            （历史快照 n=44/0.955 仅作对照，不参与判定）")
    if ok:
        print("  判定：基线达标 ⇒ 不填 intent_keywords（保持空表）")
    else:
        print("  判定：基线未达标 ⇒ 本门禁报红；不得通过调低阈值或修改样本池来消除")
        print("        需回到 eval_intent_routing.py 排查当次样本与路由，结论以本次实测为准")
    print("  理由：")
    print("   ① 关键词层只是多路路由之一，空表会自然下沉，不塌方")
    print("   ② 8 个视图 Agent 名近义，填词会触发互抢（长词优先）")
    print("   ③ 错例集中在 design↔review 语义摇摆，填词会固化错例")
    print()
    print("  对 v3.0 路线的影响：")
    print("   · V7（修关键词路由）→ 撤销")
    print("   · V2（顶层分流）→ 可做，但须先跑本门禁，")
    print("     确认分流后当次 accuracy 未低于本次实测值")
    print()
    print("=" * 70)
    print("✅ 基线达标" if ok else "❌ 基线未达标，需先排查路由")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
