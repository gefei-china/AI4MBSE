"""探针：走**真实 `LLMClient` 链路**，验证 `_intent` 真能到达 provider 并改变 payload。

## 为什么不能只靠 verify_chat_task_intent.py（AST 静态扫）

AST 只能证明「源码写了 `_intent=`」。中间还有三道可能断链：
  ① `LLMClient.chat(**kwargs)` 是否把 `_intent` 透传到 provider；
  ② provider 里 `kwargs.get("_intent")` 是否取到**同一个键**
     （★ kwargs 里还有 `_task_desc` 等同族键，取错键就全盘失效
      —— 这正是「写好了 ≠ 接上了」的典型形态）；
  ③ 三态决策是否真按 intent 走。

本探针**走真实 LLMClient**（不是直接 new provider），
只把 `httpx.post` 换成假实现（**不烧额度、不发网络**），打印实际发出的 payload。

## 与旧探针（_probe_thinking_policy.py）的区别 ★

旧探针 `new OpenAICompatProvider(...)` **直连 provider**，绕过 LLMClient。
⇒ 它验不到 ① 和 ② 这两道断链。本探针专测这两道。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/_probe_chat_task_payload.py
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 分流表：chat_probe 关、chat_answer 不在列表（应保持原行为=不发字段）
_POLICY = {"off": ["chat_probe"], "on": []}

CAPTURED: list[dict] = []


def main() -> int:
    import httpx
    import core.config as cfgmod

    real_post = httpx.post
    real_get = cfgmod.get

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        CAPTURED.append(json or {})
        raise RuntimeError("STOP_AFTER_CAPTURE")

    def fg(group, key, default=None):
        if group == "llm" and key == "thinking_policy":
            return json.dumps(_POLICY, ensure_ascii=False)
        return real_get(group, key, default)

    httpx.post = fake_post
    cfgmod.get = fg
    try:
        from llm import llm_client
        out = {}
        for intent in ("chat_probe", "chat_answer", "chat_repair", "__NONE__"):
            CAPTURED.clear()
            kwargs = {} if intent == "__NONE__" else {"_intent": intent}
            try:
                # provider_id=104 = qwen3.8-max（真库里存在）；stop 前就抛，不发真请求
                llm_client.chat([{"role": "user", "content": "探测"}],
                                provider_id=104, **kwargs)
            except Exception:
                pass  # 假 post 抛出的 STOP_AFTER_CAPTURE 属预期
            if not CAPTURED:
                out[intent] = "__NO_CALL__"
            else:
                out[intent] = CAPTURED[-1].get("thinking", "__ABSENT__")
        print("PROBE_RESULT " + json.dumps(out, ensure_ascii=False))
        return 0
    finally:
        httpx.post = real_post
        cfgmod.get = real_get


if __name__ == "__main__":
    sys.exit(main())
