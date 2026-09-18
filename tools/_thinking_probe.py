# -*- coding: utf-8 -*-
"""诊断：deepseek 开/关 thinking 是否返回 reasoning_content"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx, json
from llm import llm_client

p = llm_client.get_provider(71)
url = p["base_url"].rstrip("/") + "/chat/completions"
headers = {"Authorization": "Bearer " + p["api_key"], "Content-Type": "application/json"}
for label, extra in (("不开思考", {}), ("开思考", {"thinking": {"type": "enabled"}})):
    body = {"model": p["model_name"],
            "messages": [{"role": "user", "content": "用一句话说明什么是BDD"}],
            "max_tokens": 800, "stream": True, **extra}
    rc, cc = 0, 0
    with httpx.stream("POST", url, json=body, headers=headers, timeout=90) as r:
        for line in r.iter_lines():
            if line.startswith("data: ") and "[DONE]" not in line:
                try:
                    d = json.loads(line[6:])
                except Exception:
                    continue
                delta = (d.get("choices") or [{}])[0].get("delta", {}) or {}
                if delta.get("reasoning_content"):
                    rc += len(delta["reasoning_content"])
                if delta.get("content"):
                    cc += len(delta["content"])
    print(f"{label} | reasoning_content 字符: {rc} | content 字符: {cc}")
