# -*- coding: utf-8 -*-
"""门禁：跑一遍 pytest 全量套件（让 `gate_runner --tier pr` 真正等价于 CI）。

── 为什么要有这个门禁（2026-11-05 实测事故）──────────────────────────────
我改完产品代码后跑了 `gate_runner --tier pr`（160/160 全绿）就推送，
CI 却在**第一步 pytest** 变红：`test_probe_filter_consumers.py` 用精确 SQL
字符串当锚点，我改了那段 SQL（补 `,'localtime'`）⇒ 锚点失效。

教训不是"记得跑 pytest"，而是：**"本地预演 = CI" 这个承诺本身没有机制保障**。
`gate_runner` 只跑 manifest 里注册的 `tools/verify/*.py`，而 ci.yml 里
pytest / init_db 这些**非门禁步骤**从来没进过清单 ⇒ 本地永远不知道 CI 第一步会红。
这和「清单 vs ci.yml 是两套列表」是同一个病，只是这次漏的是 pytest。

└ 本门禁把 pytest 收进清单，于是：
    · `gate_runner --tier pr` 跑它 ⇒ 本地预演覆盖 pytest
    · ci.yml 用同一行跑它 ⇒ 只有一处真源
    · 元门禁 G6a/G11 照常对账

⏱ 耗时：约 40s（183 项）。所以 timeout 给 900。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    print("=" * 72)
    print("门禁：pytest 全量套件（CI 同口径：-q -p no:warnings）")
    print("=" * 72)
    try:
        import pytest  # noqa: F401
    except ImportError:
        # 无 pytest 的环境（如最小化容器）不应因此判红 —— 但要显式说明，
        # 不能静默通过（静默通过 = "看起来跑了其实没跑"）。
        print("  SKIP：当前解释器没有 pytest（%s）" % sys.executable)
        print("  ⇒ 本环境无法验证单测；请在装了 requirements.txt 的环境跑")
        return 0
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:warnings"],
                       cwd=ROOT, capture_output=True, text=True)
    tail = (r.stdout or "")[-4000:]
    print(tail)
    if r.returncode != 0:
        err = (r.stderr or "")[-1500:]
        if err.strip():
            print("--- stderr ---")
            print(err)
        print("\n结论：pytest 未通过（rc=%d）" % r.returncode)
        return 1
    print("\n结论：pytest 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
