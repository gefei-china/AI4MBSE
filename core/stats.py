# -*- coding: utf-8 -*-
"""统计口径单一真源：Wilson 置信区间 + 配对回归判据（P0-3，2026-10-30）。

## 为什么单独一个模块
同一套公式若分别抄进"评测跑批器"和"门禁"，两份必然漂移 ——
而漂移的后果是**门禁算出的区间与报告里写的不是同一个数**，
等于让评审看一个数、让门禁拦另一个数（本仓"同语义多处副本"已多次出事）。
故两端都从这里取。

## 为什么用 Wilson 而不是正态近似
小样本 + 比例接近 1 时，Wald 区间会越界（>1）且严重偏窄，
恰好是本工程的形态（n≈48、accuracy≈0.9）。Wilson 是比例的推荐口径。

## 配对判据为什么比"两个比例比较"更该用
"改完有没有变好"是**同一批样本**上的前后对比，不是两个独立样本。
同一批样本上，多数样本对两次结果相同，独立样本公式会**低估**检出能力。
`mcnemar(b, c)` 只吃"基线对/现在错"与"基线错/现在对"这两个数，
是配对比例的标准检验，判定"变化是否系统性"。
"""
import math

__all__ = ["wilson_ci", "interval_width_pts", "n_for_margin", "pass_k", "mcnemar"]


def wilson_ci(k: int, n: int, z: float = 1.96):
    """比例 k/n 的 Wilson 得分区间。返回 (lo, hi)，n=0 时返回 (0.0, 1.0)。"""
    if n <= 0:
        return (0.0, 1.0)
    p = float(k) / float(n)
    d = 1.0 + z * z / n
    c = (p + z * z / (2.0 * n)) / d
    s = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / d
    return (max(0.0, c - s), min(1.0, c + s))


def interval_width_pts(k: int, n: int, z: float = 1.96) -> float:
    """区间宽度（百分点）。用于"这个 n 够不够"的直观判断。"""
    lo, hi = wilson_ci(k, n, z)
    return (hi - lo) * 100.0


def n_for_margin(p1: float, p2: float, power: float = 0.8, alpha: float = 0.05) -> int:
    """检出 p1 → p2 差异所需的**独立**样本量（两比例检验，80% 功效）。

    ⚠️ 这是**上界**：配对设计所需的 n 更小（同一批样本前后对比）。
    用它回答"要凑多少条才能看清 5 个点的回归"，属保守答案。
    """
    z_a = 1.959963985 if abs(alpha - 0.05) < 1e-9 else 1.96
    z_b = 0.841621234 if abs(power - 0.8) < 1e-9 else 0.8416
    pbar = (p1 + p2) / 2.0
    num = (z_a * math.sqrt(2 * pbar * (1 - pbar))
           + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    den = (p1 - p2) ** 2
    return int(math.ceil(num / den)) if den else 0


def pass_k(p: float, k: int) -> float:
    """同一任务连跑 k 次**全部**成功的概率（τ-bench 的 pass^k 口径）。

    用途：暴露"单次成功率 95% 但连跑 8 次只有 66%"这类用户可感知的不稳定 ——
    单次评测在结构上看不见它。
    """
    return float(p) ** int(k)


def mcnemar(b: int, c: int) -> dict:
    """配对检验（McNemar，连续校正）。b=基线对/现在错，c=基线错/现在对。

    :returns: {"n_disagree", "chi2", "p_approx", "direction", "improved"}
    `direction` 说明变化方向；`p_approx` 用 1 自由度卡方的尾部近似
    （精确二项在 b+c 大时与它一致，b+c 小时以 p_approx 为准更保守）。
    """
    n = int(b) + int(c)
    if n == 0:
        return {"n_disagree": 0, "chi2": 0.0, "p_approx": 1.0,
                "direction": "same", "improved": None}
    chi2 = (abs(int(b) - int(c)) - 1) ** 2 / float(n)
    # 1 自由度卡方尾：P(X > x) = erfc(sqrt(x/2))
    p = math.erfc(math.sqrt(max(0.0, chi2) / 2.0))
    direction = "better" if int(c) > int(b) else ("worse" if int(b) > int(c) else "same")
    return {"n_disagree": n, "chi2": round(chi2, 4), "p_approx": round(p, 5),
            "direction": direction,
            "improved": (direction == "better") if direction != "same" else None}
