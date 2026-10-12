"""verify_thinking_budget — 判据：**reasoning 思考预算可控**，不靠加超时掩盖。

## 根因（2026-10-09 实测诊断，v4conv 9244 的 545s 超时）
逐层排查结论（**推翻了前两个假设**）：

| 假设 | 检验方法 | 结果 |
|---|---|---|
|① 链路卡死 / provider 侧故障 | 慢调用是否时间聚集 | ❌ 12 次散布在 4 天 |
| ② `max_tokens == context_window` 越界（3 个 provider） | 按 provider 分组看慢调用率 | ❌ **越界组慢率 0%**，不越界的 103/104 反而 13%/6% |
| ③ **reasoning 思考烧掉输出额度** | 比对 reasoning 占 completion 的比例 | ✅ **成立** |

**铁证**：

| 时期 | 思考占输出 | 给用户的 |
|---|---|---|
| 10-05 之前（非 reasoning 模型时代） | **4%** | 96% |
| 今天（reasoning 模型） | **63%** | **37%** |
| 今天慢调用（>120s） | **72%** | 28% |

极端案例：一次 143 秒的调用，`completion=6425` / `reasoning=6322`
⇒ **98% 的额度用来思考，只给用户 103 个 token**。

## 为什么要单独判这件事
`thinking` 与正文**共享 `max_tokens`** ⇒ 思考失控会：
① 吃掉给用户的输出额度（今天最大`completion=21721`，其中 20547 是思考）
② 按~20–80 ms/token 线性拉长耗时 ⇒ **撞上超时 ⇒ 重试 ⇒ 更慢**（乘法放大）

⇒ **只加超时限制是治标**：限得再高也只是让用户多等。

## 判据
① `thinking_mode` 可配且**能被显式关闭**（不只"模型默认"）
② ★ **给正文留出保底额度**：思考不能用光 `max_tokens`
③ 两项都进 `CONFIG_SCHEMA`（界面可运营）
④ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_thinking_budget.py
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)          # ★ 漏了这行 ⇒ ModuleNotFoundError: core

PROVIDER = os.path.join(_ROOT, "llm", "providers", "openai_compat.py")
CONFIG = os.path.join(_ROOT, "core", "config.py")


def provider_checks() -> list[tuple[str, bool]]:
    with open(PROVIDER, encoding="utf-8") as f:
        src = f.read()
    out = []
    # ⚠️ 变量名 2026-10-09 从 `thinking_mode` 改为 `_tmode`
    #   （引入 per-intent policy 后避免与 `_decision` 混淆）⇒ 判据要跟住。
    out.append(("provider 能识别「关闭」（变量 _tmode）",
                '_tmode == "关闭"' in src))
    out.append(("provider 能识别「开启」（变量 _tmode）",
                '_tmode == "开启"' in src))
    out.append(("`_tmode` 仍从 model_params 读（未脱离配置）",
                'mp.get("thinking_mode")' in src))
    # ★ 核心：思考预算上限
    out.append(("支持给思考设**独立上限**（thinking_budget）",
                "thinking_budget" in src))
    out.append(("★ 给正文留**保底额度**（max_tokens 不能被思考吃光）",
                "reserve" in src.lower() or "_reserve" in src))
    return out


def config_checks() -> list[tuple[str, bool]]:
    from core.config import CONFIG_SCHEMA
    llm = CONFIG_SCHEMA.get("llm", {})
    out = [
        ("`llm.thinking_budget` 已登记 schema",
         "thinking_budget" in llm),
        ("`llm.thinking_reserve_tokens` 已登记 schema",
         "thinking_reserve_tokens" in llm),
    ]
    # 默认值：0=不启用，零行为漂移
    try:
        from core import config
        out.append(("`thinking_budget` 默认 0（不启用，零漂移）",
                    config.get("llm", "thinking_budget", 0) == 0))
        out.append(("`thinking_reserve_tokens` 默认 0（不启用，零漂移）",
                    config.get("llm", "thinking_reserve_tokens", 0) == 0))
    except Exception as exc:                # noqa: BLE001
        out.append((f"config 可读（{type(exc).__name__}）", False))
    return out


def behaviour_probe() -> list[tuple[str, bool]]:
    """★ 实测：思考预算真的会把 `max_tokens` 压下来吗（用假 provider，不烧额度）。"""
    import subprocess
    out = []
    probe = os.path.join(_HERE, "_probe_thinking_budget.py")
    out.append(("探针文件存在", os.path.exists(probe)))
    if not os.path.exists(probe):
        return out
    r = subprocess.run([sys.executable, "-X", "utf8", probe],
                       capture_output=True, text=True, timeout=120)
    line = [ln for ln in (r.stdout or "").splitlines() if "PROBE_RESULT" in ln]
    if not line:
        return out + [(f"探针可解析（rc={r.returncode}; "
                       f"stderr={(r.stderr or '')[-200:]!r}）", False)]
    import json
    d = json.loads(line[-1].split("PROBE_RESULT", 1)[1])
    out.append((f"★ 思考预算=2000 + 正文保底=4000 + max=8000"
                f" ⇒ 实收 max_tokens = **{d['capped']}**（期望 ≤ 6000）",
                d["capped"] <= 6000))
    out.append((f"★ 不设预算时 max_tokens 原样 = {d['uncapped']}（期望 8000，零漂移）",
                d["uncapped"] == 8000))
    return out


def main() -> int:
    print("=" * 74)
    print("判据 · reasoning 思考预算（根治超时，而非加限制）")
    print("=" * 74)
    ok = True
    for title, checks in [("① provider 层支持", provider_checks()),
                          ("② 配置与 schema", config_checks()),
                          ("③ 实测：预算真的压 max_tokens", behaviour_probe())]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if ok:
        print("\n[变异自证] 去掉思考预算对 max_tokens 的压算，判据应变红")
        with open(PROVIDER, encoding="utf-8") as f:
            src = f.read()
        anchor = "_think_cap"
        assert anchor in src, "锚点未找到"
        mutated = src.replace(anchor, "0", 1)
        with open(PROVIDER, "w", encoding="utf-8") as f:
            f.write(mutated)
        try:
            import glob
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "providers",
                                               "__pycache__", "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
            red = not all(p for _, p in behaviour_probe())
            print(f"[变异自证] {'已变红（真判据）' if red else '**仍是绿 ⇒ 空转断言**'}")
            ok = ok and red
        finally:
            with open(PROVIDER, "w", encoding="utf-8") as f:
                f.write(src)
            import glob
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "providers",
                                               "__pycache__", "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
        with open(PROVIDER, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")
        print("=" * 74)
        print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())