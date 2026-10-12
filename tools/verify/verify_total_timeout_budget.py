"""verify_total_timeout_budget — 判据：一次调用（含重试）**有总时间预算**。

## 缺陷（2026-10-09 实测八视图）
`llm/providers/openai_compat.py` 的 `httpx.post(..., timeout=180)` 是**硬编码**，
而 `_chat_with_retry` 会重试 `llm.retry_times=2` 次 ⇒

    **单次调用最坏 = 180 × (2+1) + 退避 ≈ 541.5 秒**（实测 545s 吻合）

⇒ 用户等**9 分钟**换来一个异常（conv 9244 落库 0 行、什么都没拿到；
v1 重跑 conv 9289 又踩一次，`failure_kind='timeout'`）。

## 为什么不是"把 180 改小"
实测 6585 次成功调用里**只有 9 次（0.1%）**超过 180s，
而慢模型单次实测可达 **121s~380s**（deepseek-v4-pro / qwen3.8-max）
⇒ 改小单次超时**会误杀那些合法但慢的调用**。
真正的闸门是**总预算**：它掐的是"×3 之后的长尾"，不影响单次成功率。

## 判据
① `http_timeout_s` 可配（不再是硬编码），默认 180（零行为漂移）
② `total_timeout_budget` 存在且**默认 0=不启用**（不改变现有行为）
③ **总预算闸真的生效**：模拟"每次都超时"，验证重试次数被截断
④ 两项都登记进 `CONFIG_SCHEMA`（界面可调）
⑤ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_total_timeout_budget.py
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PROVIDER_SRC = os.path.join(_ROOT, "llm", "providers", "openai_compat.py")
LLM_INIT = os.path.join(_ROOT, "llm", "__init__.py")
CONFIG_SRC = os.path.join(_ROOT, "core", "config.py")


def config_checks() -> list[tuple[str, bool]]:
    from core import config
    from core.config import CONFIG_SCHEMA
    out = []
    with open(PROVIDER_SRC, encoding="utf-8") as f:
        prov = f.read()
    # ⚠️ 不能用 `"timeout=180" not in 源码` 判：我的**注释里**就写了
    #   「原 `timeout=180` …」来解释历史 ⇒ 首版因此误判红。
    #   正解：用 AST 找 `httpx.post(...)` 的**关键字实参**，看 timeout 是不是字面量。
    # ⚠️ 只判**chat 主路径**那一次 post；`test_connection` 里还有两处
    #   `httpx.post(..., timeout=15)`（探活用，15s 本来就是对的）⇒ 不能一并判红。
    import ast as _ast
    hard = []
    for _fn in [n for n in _ast.walk(_ast.parse(prov))
                if isinstance(n, _ast.FunctionDef)]:
        if _fn.name == "test_connection":       # 探活：timeout=15 是合理的
            continue
        for _n in _ast.walk(_fn):
            if (isinstance(_n, _ast.Call)
                    and isinstance(_n.func, _ast.Attribute)
                    and _n.func.attr == "post"
                    and "chat" in _ast.dump(_n)[:200]):
                for _kw in _n.keywords:
                    if (_kw.arg == "timeout"
                            and isinstance(_kw.value, _ast.Constant)):
                        hard.append((_fn.name, _kw.value.value))
    out.append((f"① chat 主路径的 timeout 不再是字面量（实测 {hard or '无'}；"
                f"test_connection 的 timeout=15 是探活，不计）", not hard))
    out.append(("① 改为读 `llm.http_timeout_s`",
                'get("llm", "http_timeout_s"' in prov))
    out.append(("② 存在 `total_timeout_budget` 且默认 0（不启用）",
                config.get("llm", "total_timeout_budget", 0) == 0))
    out.append(("② 单次超时默认仍是 180（零行为漂移）",
                config.get("llm", "http_timeout_s", 180) == 180))
    for key in ("http_timeout_s", "total_timeout_budget"):
        out.append((f"④ `{key}` 已登记进 CONFIG_SCHEMA",
                    key in CONFIG_SCHEMA.get("llm", {})))
    return out


def budget_gate_check() -> list[tuple[str, bool]]:
    """★ 核心：总预算闸真能截断重试吗？

    ⚠️ 必须**跑独立探针文件**（`tools/verify/_probe_total_timeout.py`）而不是内联：
       判据主进程开头已 import llm，改文件/清缓存/reload 都救不回来 ——
       实测踩过「尝试次数恒 0」（根本没调到底，却像"预算全掐了"）
       与「变异后仍绿 ⇒ 空转断言」两个假象。
    """
    import subprocess
    out = []
    probe = os.path.join(_HERE, "_probe_total_timeout.py")
    with open(LLM_INIT, encoding="utf-8") as f:
        src = f.read()
    out.append(("`_chat_with_retry` 里有总预算判断",
                "total_timeout_budget" in src and "已耗时" in src))
    out.append(("探针文件存在", os.path.exists(probe)))

    r = subprocess.run([sys.executable, "-X", "utf8", probe],
                       capture_output=True, text=True, timeout=120)
    line = [ln for ln in (r.stdout or "").splitlines()
            if "PROBE_RESULT" in ln]
    if not line:
        return out + [(f"探针可解析（rc={r.returncode}；"
                       f"stderr 末 200 字={(r.stderr or '')[-200:]!r}）", False)]
    data = json.loads(line[-1].split("PROBE_RESULT", 1)[1])
    n_with, n_no = data["with"], data["without"]
    out.append((f"★ 开启总预算 ⇒ 尝试 **{n_with}** 次 < 无预算时 **{n_no}** 次",
                n_with < n_no))
    out.append((f"★ 无总预算 ⇒ 跑满 times+1 = {n_no} 次（retry_times=2 ⇒ 3）",
                n_no == 3))
    return out


def main() -> int:
    print("=" * 72)
    print("判据 · LLM 调用的总时间预算（掐掉 541s 长尾）")
    print("=" * 72)
    ok = True
    for title, checks in [("① 配置化与 schema", config_checks()),
                          ("② 总预算闸实际生效", budget_gate_check())]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    print("\n" + "=" * 72)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 72)
    if ok:
        print("\n[变异自证] 把总预算判断删掉，判据应变红")
        with open(LLM_INIT, encoding="utf-8") as f:
            src = f.read()
        anchor = "if _budget and attempt > 0:"
        assert anchor in src, "锚点未找到"
        mutated = src.replace(anchor, "if False and attempt > 0:", 1)
        with open(LLM_INIT, "w", encoding="utf-8") as f:
            f.write(mutated)
        try:
            # ⚠️⚠️ 必须**清 `__pycache__`**：本判据进程已在开头import 过 `llm`，
            #   改文件后 Python 仍会按 mtime+size 判断缓存有效 ⇒ 跑的是**旧代码**
            #   ⇒ 变异后预算闸照样生效 ⇒ 判据仍绿 ⇒ **空转断言**。
            #   首版就是踩这个：报「仍是绿」，差点以为判据写错。
            import glob
            import importlib
            import py_compile
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "__pycache__",
                                               "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
            importlib.invalidate_caches()
            red = not all(p for _, p in budget_gate_check())
            print(f"[变异自证] {'已变红（真判据）' if red else '**仍是绿 ⇒ 空转断言**'}")
            if not red:
                for nm, p in budget_gate_check():
                    if not p:
                        print(f"           ★ 变红：{nm}")
            ok = ok and red
        finally:
            with open(LLM_INIT, "w", encoding="utf-8") as f:
                f.write(src)
            import glob
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "__pycache__",
                                               "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
            importlib.invalidate_caches()
        with open(LLM_INIT, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")
        print("=" * 72)
        print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())