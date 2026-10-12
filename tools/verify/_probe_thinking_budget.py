"""子进程探针：实测 `thinking_budget` 是否真的把 `max_tokens` 压下来。

**为什么独立进程**（同 `verify_total_timeout_budget` 的教训）：
判据主进程开头已 import 过 `llm`，改文件/清缓存/reload 都救不回来。

做法：用 **假的 httpx**，让provider 层把 payload 抓下来，
**不烧任何额度、也不依赖网络**。
输出：末行 `PROBE_RESULT {"capped": n, "uncapped": m}`。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.config as cfgmod          # noqa: E402
from llm.providers.openai_compat import OpenAICompatProvider   # noqa: E402

CFG = {
    "id": 9001, "name": "probe", "model_name": "probe-model",
    "base_url": "https://example.invalid", "api_key": "k",
    "max_tokens": 8000, "context_window": 128000, "temperature": 0.3,
    "model_params": "{}", "status": "active",
}


def _capture(think_budget, reserve, mt=8000):
    """跑一次 chat，把实际发出去的 max_tokens 抓出来。"""
    got = {}
    import llm.providers.openai_compat as oc
    real_post = None
    import httpx

    def fake_post(url, json=None, headers=None, timeout=None):
        got["max_tokens"] = (json or {}).get("max_tokens")
        got["timeout"] = timeout
        raise RuntimeError("STOP_AFTER_CAPTURE")   # 抓完就抛，避免真发请求

    httpx.post = fake_post
    real_get = cfgmod.get

    def fg(group, key, default=None):
        if group == "llm" and key == "thinking_budget":
            return think_budget
        if group == "llm" and key == "thinking_reserve_tokens":
            return reserve
        return real_get(group, key, default)

    cfgmod.get = fg
    try:
        p = OpenAICompatProvider(dict(CFG))
        p.chat([{"role": "user", "content": "x"}], max_tokens=mt)
    except Exception:
        pass
    finally:
        cfgmod.get = real_get
        httpx.post = real_post if real_post else httpx.post
    return got.get("max_tokens")


if __name__ == "__main__":
    result = {
        # 开启预算：思考 2000 + 正文保底 4000 ⇒ 上限 6000 < 8000 ⇒ 应被压到 6000
        "capped": _capture(2000, 4000, mt=8000),
        # 不设预算：原样 8000（零行为漂移）
        "uncapped": _capture(0, 0, mt=8000),
    }
    print("PROBE_RESULT " + json.dumps(result))
