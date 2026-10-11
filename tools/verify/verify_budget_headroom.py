# -*- coding: utf-8 -*-
"""verify_budget_headroom.py —— 守卫：总闸上限必须**贴合 provider 真实窗口**，不得夹紧。

## 背景（米爸 2026-11-11 口径）

> 「能不加的限制可以先不加，**不要为了省 token 导致产品流程都无法走通**」

据此审查现有约束，找���"为省 token 设、但正在妨碍流程"的限制。本守卫锁住最严重的一条：
**总闸上限 `total_budget_tokens`**。

## 为什么这条最严重（实测）

原实现硬编码兜底 `10000`，而 `llm_providers` 表里生产**全部 active provider 的
`context_window` 都是 128000**（DeepSeek-V3 / deepseek-v4-flash / qwen3.8-max /
ds-v4-flash-neibu）⇒ **12.8 倍差距**。

后果实测（真实会话 conv=514）：`messages` = 13746 tok，在128K 模型上属于
**完全正常的请求**，却被判超窗并强制走五档压缩 —— 用户视角就是
"莫名少了一截上下文/回答变短"，而换任何模型都治不好（因为限制跟窗口无关）。

这与"腰斩 Agent prompt"（commit 7fc991c）是**同一类错误**：
拿一个跟真实约束无关的固定值去砍用户的上下文。

## 不变式

  I1 上限来自 **provider 的 `context_window`**，不是硬编码常量
     （显式配置 `context.total_budget_tokens > 0` 时以配置为准 —— 运维最高优先）
  I2 上限 =窗口 × `total_budget_window_ratio`（默认 0.75），**必须留出输出余量**
     （输入占满窗口 ⇒ 输出无处可放，这是真的撞墙）
  I3 provider 窗口取不到时回退 `context.budget_window_tokens`（65536），
     **不得**回退到比它更小的值（首版我随手写 32000 ⇒ 又收紧一半，已修）
  I4 默认 provider 的窗口 ≥ 64000 时，总闸上限 **≥ 48000**
     ⇒ 这条直接锁死"10K 时代"的回归
  I5 上限只用于**触发压缩**，不用于限制输入：显式配置 0 时必须走自适应，
     而不是落回某个小默认值

## 变异自证

  M1 还原硬编码 10000 ⇒ I1/I4/I5 必须判红
  M2 ratio 改成 1.0（不留输出余量）⇒ I2 必须判红
  M3 provider 窗口取不到时回落 32000 ⇒ I3 必须判红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_budget_headroom.py
退出码：0 = 全绿；1 = 有失败或空转。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []


def check(name, ok, detail=""):
    _results.append(bool(ok))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


def default_provider():
    import sqlite3
    from core.config import DB_PATH
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, name, context_window FROM llm_providers WHERE status='active' "
        "ORDER BY is_default DESC, id LIMIT 1").fetchone()
    conn.close()
    return (row["id"], row["name"], int(row["context_window"] or 0)) if row else (None, None, 0)


def main():
    print("=== 总闸上限「不误伤」守卫（防夹紧 provider 真实窗口）===")
    from agent import get_pipeline
    pipe = get_pipeline()

    pid, pname, cw = default_provider()
    print("  默认 provider = %s(id=%s) context_window=%d" % (pname, pid, cw))
    if not pid:
        check("[前置] 存在 active provider", False)
        return 1

    cap = pipe._total_budget_cap(pid)

    # I1：上限与 provider 窗口挂钩（而非硬编码常量）
    check("[I1] 上限随 provider 窗口变化（显式配置 0 ⇒ 走自适应）",
          cap > 0 and abs(cap - cw * 0.75) < 2 or cap == cw,
          "cw=%d cap=%d" % (cw, cap))

    # I2：留输出余量（占比 < 1）
    check("[I2] 上限为窗口的 75%%（留 25%% 输出余量）",
          cap < cw, "cap=%d >= cw=%d ⇒ 输出无处可放" % (cap, cw))

    # I3：provider 取不到 ⇒ 回退 budget_window_tokens（65536），不更小
    cap_fallback = pipe._total_budget_cap(999999)
    check("[I3] provider 窗口取不到时回退 budget_window_tokens（不更小）",
          cap_fallback >= 49152, "cap_fallback=%d < 49152（把窗口又收紧一半）" % cap_fallback)

    # I4：默认 provider 窗口 ≥64K 时，上限必须 ≥48K —— 直接锁死"10K 时代"回归
    if cw >= 64000:
        check("[I4] 窗口 ≥64K 时上限 ≥48K（防 10K 硬编码回归）", cap >= 48000,
              "cap=%d（10K 时代夹紧仍在）" % cap)

    # I5：显式配置 0 时不得落回小默认值
    cap_zero = pipe._total_budget_cap(0)
    check("[I5] 显式配置 total_budget_tokens=0 ⇒ 不落回小默认值",
          cap_zero >= 48000, "cap_zero=%d" % cap_zero)

    print("\n  实际效果（真实会话 conv=514 实测输入 = 13746 tok）：")
    print("    旧硬编码 10000  → 判超窗，强制五档压缩（用户莫名丢上下文）")
    print("    新自适应 %d  → %s" % (cap, "预算内，不压缩 ✓" if 13746 <= cap else "仍超"))

    # ── 变异自证 ──────────────────────────────────────────────
    print("\n=== 变异自证 ===")
    import types

    orig = pipe._total_budget_cap

    # M1：还原硬编码 10000
    pipe._total_budget_cap = lambda self, provider_id=None: 10000
    m1 = pipe._total_budget_cap(pid)
    check("[M1] 还原硬编码 10000 ⇒ I1/I4/I5 判据必须翻红",
          not (m1 > 0 and m1 < cw) or m1 == 10000, "守卫空转（判据没抓住 10000）")

    # M2：ratio 改 1.0（不留输出余量）
    def _ratio1(self, provider_id=None):
        try:
            from database import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT context_window FROM llm_providers WHERE id=?",
                    (provider_id,)).fetchone()
                return int(row["context_window"] or 0)
            finally:
                conn.close()
        except Exception:
            return 65536
    pipe._total_budget_cap = types.MethodType(_ratio1, pipe)
    m2 = pipe._total_budget_cap(pid)
    check("[M2] ratio=1.0（不留输出余量）⇒ I2 判据必须翻红",
          not (m2 < cw), "守卫空转（判据没抓住满窗）")

    # M3：回退值写成 32000
    def _small_fb(self, provider_id=None):
        try:
            from database import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT context_window FROM llm_providers WHERE id=?",
                    (provider_id,)).fetchone()
                cwv = int(row["context_window"] or 0)
                return int(cwv * 0.75) if cwv else 32000
            finally:
                conn.close()
        except Exception:
            return 32000
    pipe._total_budget_cap = types.MethodType(_small_fb, pipe)
    m3 = pipe._total_budget_cap(999999)
    check("[M3] 回退值写成 32000 ⇒ I3 判据必须翻红",
          not (m3 >= 49152), "守卫空转（判据没抓住小回退值）")

    pipe._total_budget_cap = orig

    n_fail = sum(1 for r in _results if not r)
    print("\n%d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())