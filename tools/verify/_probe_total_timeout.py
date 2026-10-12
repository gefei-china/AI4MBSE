"""子进程探针：实测 `_chat_with_retry` 的**总时间预算**闸是否真能截断重试。

**为什么必须独立进程**（2026-10-09 实测踩坑记录）：
判据主进程开头已 `import llm`，此后无论改源文件、清 `__pycache__`、还是
`importlib.reload`，`_chat_with_retry` 的绑定都不可靠 ——
实测出现过「尝试次数恒 0」（以为预算把重试全掐了，实际是根本没调到底）
与「变异后预算闸仍生效 ⇒ 判据永远绿」两个假象。
⇒ 唯一可信的做法是**新建解释器**跑本文件。

用法（一般由 verify_total_timeout_budget.py 调用，也可单独跑）：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/_probe_total_timeout.py
输出：末行 `PROBE_RESULT {"with": n, "without": m}`
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.config as cfgmod          # noqa: E402
from llm import LLMClient              # noqa: E402


def run_with(per_try, budget):
    """用给定的「单次超时 / 总预算」跑一次「每次必超时」的重试循环，返回尝试次数。"""
    real = cfgmod.get

    def fg(group, key, default=None):
        if group == "llm" and key == "http_timeout_s":
            return per_try
        if group == "llm" and key == "total_timeout_budget":
            return budget
        return real(group, key, default)

    cfgmod.get = fg
    cnt = {"n": 0}

    class Slow:
        """每次调用必超时 —— 用来把重试路径顶出来。

        注意：`_chat_with_retry` 调的是 `impl.chat(...)`，
        所以这里必须传**对象**而不是函数（首版传函数 ⇒ AttributeError，计数恒 0）。
        """

        def chat(self, *a, **k):
            cnt["n"] += 1
            time.sleep(0.02)
            raise TimeoutError("The read operation timed out")

    try:
        LLMClient()._chat_with_retry(
            Slow(), [{"role": "user", "content": "x"}], {"model": "m"},
            False, None, False, "fake", "fake")
    except Exception:                                   # noqa: BLE001
        pass
    finally:
        cfgmod.get = real
    return cnt["n"]


if __name__ == "__main__":
    result = {"with": run_with(0.01, 0.05),      # 总预算很小 ⇒ 应截断
              "without": run_with(0.01, 0)}      # 无预算 ⇒ 跑满 times+1
    print("PROBE_RESULT " + json.dumps(result))
