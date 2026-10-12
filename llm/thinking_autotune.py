"""thinking_autotune — 思考分流的**数据自校正**（可维护性第3 层，2026-10-10）。

## 要解决的问题

前两层治理都只认"名字"或"形态"，覆盖不全：

| 层 | 依据 | 覆盖 | 局限 |
|---|---|---|---|
| 1 名单 | `thinking_policy.off` | 人配例外 | 靠人记得加 |
| 2 形态规则 | 带 tools / 上限小 / 探针语义 | 探针类新 intent | **只认探针语义** |
| **3 本模块** | ★**真库实测数据** | ★**任意** intent | 本模块 |

具体缺口：形态规则不认 `快速摘要`、`impact_extract` 这类名字。
若它们思考占 80% 而只产几十 token，规则不命中⇒ 思考继续失控 ⇒ **仍要人配**。

⇒ 本模块按「该 intent 的真库历史表现」判定，**不需要知道它的名字语义**。

## 判据（有数据支撑，不是拍脑袋）

对每个 intent 聚合 `llm_usage_stats`：
  · `ratio = Σ reasoning_tokens / Σ completion_tokens`（思考占比）
  · `avg_completion = AVG(completion_tokens)`（平均产出）

判定「建议关思考」需**同时**满足：
  1. `ratio ≥ min_ratio`（默认 **0.5**）—— 思考远大于产出
  2. `avg_completion ≤ max_output`（默认 **1200**）—— 产出确实很小

实测分界极其清晰（2026-10-10 真库，n=6890）：
  · 廉价任务思考占 **58%~98%**：`probe` 97.6% / `crypto_probe` 79.1% / `routing` 81.6%
  · 重产码任务思考占 **0%~2.5%**：`plan_refine` 0% / `plan_summary` 1.4%
⇒ **同一套 provider 覆盖两端，必然一边浪费、一边不足** —— 只有按实测数据分流才能解决。

## 三条安全阀（防误伤，缺一不可）

① **样本量下限**（`min_samples`，默认 5）：调用太少**不判**。
   理由：2 次调用的均值没有代表性，误判代价（关掉重产码的思考）远大于收益。
② **只建议、不自动改**：本模块返回**建议清单**，
   写不写进 `thinking_policy` 由**人**决定。
   理由：自动改配置 = 让一次统计口径错误直接变成线上行为变更，风险不可控。
③ **只读真库**：本模块**只 SELECT**，不写任何表。

## 用法

    from thinking_autotune import suggest_thinking_off
    for s in suggest_thinking_off():
        print(s["intent"], s["ratio"], s["n"])

配套门禁 `tools/verify/verify_thinking_autotune.py`。
"""
from __future__ import annotations

import os
import sys

# 允许 `python llm/thinking_autotune.py` 直接跑（自检）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_MIN_RATIO = 0.5      # 思考占比阈值
DEFAULT_MAX_OUTPUT = 1200    # 平均产出上限（token）
DEFAULT_MIN_SAMPLES = 5      # 样本量下限


def _db_path() -> str:
    """定位真库（口径单一真源 = `core.config`）。"""
    try:
        from core.config import DATABASE_PATH
        return DATABASE_PATH
    except Exception:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "mbse.db")


def suggest_thinking_off(*, min_ratio: float | None = None,
                         max_output: int | None = None,
                         min_samples: int | None = None,
                         db_path: str | None = None) -> list[dict]:
    """扫描真库，产出「建议关思考」的 intent 清单。**只读，不改任何配置。**

    返回：每项 `{"intent", "n", "ratio", "avg_completion", "samples_ok"}`，
          按 `ratio` 降序（最该关的排最前）。

    ⚠️ **为什么参数可覆盖**：门禁要做**变异自证**（把 `min_ratio` 调到 0.0
    看会不会误伤重产码）—— 若阈值写死在函数体里，变异就得改生产代码。
    ⇒ 参数化让"验证阈值有效性"不必碰生产代码。
    """
    try:
        from core import config as cfg
    except Exception:
        cfg = None

    def _get(key, default):
        if cfg is None:
            return default
        try:
            v = cfg.get("llm", key, default)
        except Exception:
            return default
        return default if v is None else v

    if min_ratio is None:
        min_ratio = float(_get("autotune_min_ratio", DEFAULT_MIN_RATIO))
    if max_output is None:
        max_output = int(_get("autotune_max_output", DEFAULT_MAX_OUTPUT))
    if min_samples is None:
        min_samples = int(_get("autotune_min_samples", DEFAULT_MIN_SAMPLES))

    path = db_path or _db_path()
    if not os.path.exists(path):
        return []

    import sqlite3
    try:
        # ★ **只读**打开（`mode=ro`）：本模块绝不写库。
        #   `COALESCE(call_failed,0)=0` —— 失败调用没有有效 usage，混进来会拉低占比。
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
        rows = conn.execute(
            "SELECT intent, COUNT(*) n, "
            "       SUM(COALESCE(reasoning_tokens,0)) rt, "
            "       SUM(COALESCE(completion_tokens,0)) ct, "
            "       AVG(COALESCE(completion_tokens,0)) av "
            "FROM llm_usage_stats "
            "WHERE COALESCE(call_failed,0)=0 AND intent<>'' "
            "GROUP BY intent"
        ).fetchall()
        conn.close()
    except Exception:
        return []

    out: list[dict] = []
    for intent, n, rt, ct, av in rows:
        n = int(n or 0)
        ct = int(ct or 0)
        if n < min_samples or ct <= 0:
            # ★ 安全阀①：样本量不足 / 无有效 usage ⇒ **不判**（不下结论）
            continue
        ratio = (int(rt or 0)) / ct
        if ratio >= min_ratio and float(av or 0) <= max_output:
            out.append({"intent": intent, "n": n, "ratio": ratio,
                        "avg_completion": float(av or 0)})
    out.sort(key=lambda d: -d["ratio"])
    return out


def main() -> int:
    sug = suggest_thinking_off()
    print(f"建议关思考的 intent：{len(sug)} 个")
    print(f"{'intent':30s} {'n':>6s} {'思考占比':>9s} {'均产tok':>9s}")
    for s in sug:
        print(f"{s['intent'][:29]:30s} {s['n']:6d} {s['ratio'] * 100:8.1f}% "
              f"{s['avg_completion']:9.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
