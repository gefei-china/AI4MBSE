"""子进程探针：实测 **thinking 三态分流**是否按预期下发。

三态（关键，缺一不可）：
  ①命中 off  ⇒ 显发 `{"type":"disabled"}`
     **必须显发** —— 实测原实现「关闭时什么都不发」，
     模型端就按**默认（开启）**思考 ⇒ 我方"关闭思考"从来没生效过。
  ② 命中 on  ⇒ 显发 `{"type":"enabled"}`
  ③ **未命中 ⇒ 完全不发该字段**（保持改动前行为）
     ★ 这是防"全链路被一起关掉"的护栏：`chat()` 默认 `thinking=False`
     且实测**全仓库无一处**传 True ⇒ 若把"关闭"实现成"总是发 disabled"，
     重产码任务（视图生成，正文 1.5 万 token）会被**一起关掉思考**。

用假 httpx 抓 payload，**不烧额度**。
输出：末行 `PROBE_RESULT {...}`
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import httpx# noqa: E402

import core.config as cfgmod          # noqa: E402
from llm.providers.openai_compat import OpenAICompatProvider   # noqa: E402

CFG = {
    "id": 9002, "name": "probe", "model_name": "probe-model",
    "base_url": "https://example.invalid", "api_key": "k",
    "max_tokens": 8000, "context_window": 128000, "temperature": 0.3,
    "model_params": "{}", "status": "active",
}
_POLICY = {"off": ["routing", "probe"], "on": ["plan_refine"]}


def _payload_of(intent, policy=None, thinking_arg=False):
    """跑一次 chat，返回实际发出去的 payload（抓完就抛，不发真请求）。"""
    got = {}
    real_post = httpx.post
    real_get = cfgmod.get

    def fake_post(url, json=None, headers=None, timeout=None):
        got.update(json or {})
        raise RuntimeError("STOP_AFTER_CAPTURE")

    def fg(group, key, default=None):
        if group == "llm" and key == "thinking_policy":
            return json.dumps(policy if policy is not None else _POLICY,
                              ensure_ascii=False)
        return real_get(group, key, default)

    httpx.post = fake_post
    cfgmod.get = fg
    try:
        p = OpenAICompatProvider(dict(CFG))
        try:
            if intent is None:
                p.chat([{"role": "user", "content": "x"}],
                       thinking=thinking_arg)
            else:
                p.chat([{"role": "user", "content": "x"}],
                       thinking=thinking_arg, _intent=intent)
        except Exception:                      # noqa: BLE001
            pass
    finally:
        cfgmod.post = real_post if hasattr(cfgmod, "post") else real_post
        httpx.post = real_post
        cfgmod.get = real_get
    return got.get("thinking", "__ABSENT__")


if __name__ == "__main__":
    result = {
        # ① 命中 off ⇒ 必须显式 disabled（不发 = 模型端仍按默认开启 = 假关闭）
        "off": _payload_of("routing"),
        "off2": _payload_of("probe"),
        # ② 命中 on⇒ 显式 enabled（即便调用方thinking=False）
        "on": _payload_of("plan_refine"),
        # ③ 未命中 ⇒ **必须不发**（保持改动前行为）
        "miss": _payload_of("view_expansion"),
        # ④ 没传 intent ⇒ 也不发
        "no_intent": _payload_of(None),
    }
    print("PROBE_RESULT " + json.dumps(result, ensure_ascii=False))
