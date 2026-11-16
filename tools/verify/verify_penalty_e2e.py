# -*- coding: utf-8 -*-
# CI-OPTIONAL: 依赖**外部预置状态**（DB 里 provider 的 model_params 已配好 penalty）且需真实上游；
#               干净库/无外网时跑不了，故不进 CI。配套脚本：verify_penalty_upstream.py。
"""端到端真机验证：DB 里的 penalty 是否真的进到出站 payload 并被上游接受。

链路：DB llm_providers.model_params → resolve_provider_cfg → openai_compat 组装 payload
      → httpx.post → 上游 HTTP 状态

⚠️ 判据纪律（2026-11-13 实测踩坑）：
   **不要用「PATCH 之前就打开」的 sqlite 连接回读** —— 那是旧读快照，
   会得出「没落库」的假结论（本次已踩过一次）。回读必须用**新连接**。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402

from llm import resolve_provider_cfg  # noqa: E402

PID = 71
_orig_post = httpx.post
SEEN = []


def _spy(url, json=None, headers=None, timeout=None, **kw):
    SEEN.append(json)
    return _orig_post(url, json=json, headers=headers, timeout=timeout, **kw)


httpx.post = _spy

cfg = resolve_provider_cfg(PID)
print("DB 里的 model_params = %s" % cfg.get("model_params"))
print("provider family 判定 base_url=%s model=%s" % (cfg.get("base_url"), cfg.get("model_name")))

from llm.providers.openai_compat import OpenAICompatProvider  # noqa: E402

SEEN.clear()
p = OpenAICompatProvider(cfg)
t0 = time.time()
try:
    resp = p.chat([{"role": "user", "content": "用一句话说明 SysML v2 需求视图。"}],
                  _intent="verify_penalty_e2e")
    dt = time.time() - t0
    txt = (resp["choices"][0]["message"].get("content") or "")
    print("上游响应 OK，耗时 %.1fs，输出片段：%s" % (dt, txt[:70].replace("\n", " ")))
    ok = True
except Exception as e:  # noqa: BLE001
    print("上游调用失败：%s: %s" % (type(e).__name__, e))
    ok = False

payload = SEEN[-1] if SEEN else {}
print("-" * 72)
print("实际出站 payload 的采样字段：")
for k in ("model", "temperature", "max_tokens", "frequency_penalty", "presence_penalty", "top_p"):
    print("   %-20s = %r" % (k, payload.get(k, "<未下发>")))
print("-" * 72)

checks = [
    ("上游调用成功", ok),
    ("payload 含 frequency_penalty", payload.get("frequency_penalty") == 0.5),
    ("payload 含 presence_penalty（penalty_support=all ⇒ 强制发）",
     payload.get("presence_penalty") == 0.3),
]
fails = []
for name, cond in checks:
    print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
    if not cond:
        fails.append(name)
print("结论：%s" % ("全部 PASS" if not fails else "FAIL %s" % fails))
sys.exit(1 if fails else 0)
