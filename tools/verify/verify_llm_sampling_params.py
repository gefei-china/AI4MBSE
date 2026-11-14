# -*- coding: utf-8 -*-
"""验证 2026-11-13 采样参数改动（出站 payload 实测，**不采信代码自述**）。

覆盖两件事：
① Agent 级 `model_params.temperature` 从「死配置」变为真生效，
   并保持优先级链：本轮覆盖 > 调用点显式 > Agent 级 > provider 级 > provider 列 > 默认
② Frequency / Presence Penalty 链路打通（model_params → payload），
   并按厂商家族分流（DeepSeek 只发 frequency；Anthropic 两个都不发）。

判据口径：monkeypatch `httpx.post` 抓**真实出站 payload**（字节级证据）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402

CAPTURED = []


class _FakeResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _fake_post(url, json=None, headers=None, timeout=None, **kw):
    CAPTURED.append(json)
    return _FakeResp()


httpx.post = _fake_post

from llm.providers.openai_compat import OpenAICompatProvider  # noqa: E402

FAILS = []


def chk(cond, name, got=None):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("  [%s] %s%s" % (tag, name, ("  -> got=%r" % (got,)) if got is not None else ""))


def base_cfg(**over):
    cfg = {
        "id": 900, "name": "verify-provider", "provider_type": "openai",
        "base_url": "http://llm-provider.invalid/v1", "api_key": "sk-test",
        "model_name": "deepseek-v4-flash", "model_type": "chat",
        "max_tokens": 4096, "context_window": 32768, "temperature": 0.3,
        "model_params": "{}",
    }
    cfg.update(over)
    return cfg


def run(cfg, **kw):
    CAPTURED.clear()
    p = OpenAICompatProvider(cfg)
    try:
        p.chat([{"role": "user", "content": "ping"}], **kw)
    except Exception as e:  # noqa: BLE001
        print("     (chat raised: %s)" % e)
    return CAPTURED[-1] if CAPTURED else {}


def mp(**kv):
    import json as _j
    return _j.dumps(kv, ensure_ascii=False)


print("=" * 72)
print("A. 零行为漂移：未配置 penalty => 两个字段都不下发")
p = run(base_cfg())
chk(p.get("temperature") == 0.3, "A1 temperature 仍取 provider 列 0.3", p.get("temperature"))
chk("frequency_penalty" not in p, "A2 payload 无 frequency_penalty 键", sorted(p.keys()))
chk("presence_penalty" not in p, "A3 payload 无 presence_penalty 键", sorted(p.keys()))

print("B. provider 级 model_params.temperature 生效（此前是死配置）")
p = run(base_cfg(model_params=mp(temperature=0.15)))
chk(p.get("temperature") == 0.15, "B1 provider 级 temperature=0.15 落到 payload", p.get("temperature"))

print("C. Agent 级覆盖 provider 级（同名键 Agent 优先）")
p = run(base_cfg(model_params=mp(temperature=0.15)), _model_params={"temperature": 0.05})
chk(p.get("temperature") == 0.05, "C1 Agent 级 0.05 压过 provider 级 0.15", p.get("temperature"))

print("D. 本轮覆盖压过 Agent/provider 级")
from core import runtime_state as _rs  # noqa: E402
_rs.set_llm_overrides({"temperature": 0.02})
p = run(base_cfg(model_params=mp(temperature=0.15)), _model_params={"temperature": 0.05})
chk(p.get("temperature") == 0.02, "D1 本轮 0.02 最高优先", p.get("temperature"))
_rs.set_llm_overrides(None)

print("E. 调用点显式传参压过 mp（调用点 > Agent > provider）")
p = run(base_cfg(model_params=mp(temperature=0.15)), temperature=0.9,
        _model_params={"temperature": 0.05})
chk(p.get("temperature") == 0.9, "E1 调用点 0.9 最高优先", p.get("temperature"))

print("F. temperature=0 是合法值（贪心解码），不得被吞成默认")
p = run(base_cfg(model_params=mp(temperature=0)))
chk(p.get("temperature") == 0, "F1 temperature=0 落到 payload", p.get("temperature"))

print("G. Frequency Penalty 下发；DeepSeek 自动跳过 presence")
p = run(base_cfg(model_params=mp(frequency_penalty=0.5, presence_penalty=0.3)))
chk(p.get("frequency_penalty") == 0.5, "G1 frequency_penalty=0.5 已下发", p.get("frequency_penalty"))
chk("presence_penalty" not in p, "G2 DeepSeek 家族不发 presence_penalty", sorted(p.keys()))

print("H. penalty_support=all => 两个都发（逃生阀）")
p = run(base_cfg(model_params=mp(frequency_penalty=0.5, presence_penalty=0.3,
                                 penalty_support="all")))
chk(p.get("frequency_penalty") == 0.5, "H1 frequency 下发", p.get("frequency_penalty"))
chk(p.get("presence_penalty") == 0.3, "H2 presence 下发", p.get("presence_penalty"))

print("I. penalty_support=none => 两个都不发")
p = run(base_cfg(model_params=mp(frequency_penalty=0.5, presence_penalty=0.3,
                                 penalty_support="none")))
chk("frequency_penalty" not in p and "presence_penalty" not in p,
    "I1 两个都不下发", sorted(p.keys()))

print("J. Qwen 家族 => 两个都发")
p = run(base_cfg(model_name="qwen3.8-max",
                 model_params=mp(frequency_penalty=0.2, presence_penalty=0.1)))
chk(p.get("frequency_penalty") == 0.2 and p.get("presence_penalty") == 0.1,
    "J1 Qwen 两个都下发", (p.get("frequency_penalty"), p.get("presence_penalty")))

print("K. Anthropic 家族 => 两个都不发")
p = run(base_cfg(model_name="claude-3-5-sonnet",
                 model_params=mp(frequency_penalty=0.2, presence_penalty=0.1)))
chk("frequency_penalty" not in p and "presence_penalty" not in p,
    "K1 Anthropic 两个都不下发", sorted(p.keys()))

print("L. 越界值不下发（|x|>2）")
p = run(base_cfg(model_params=mp(frequency_penalty=5, presence_penalty=-9)))
chk("frequency_penalty" not in p and "presence_penalty" not in p,
    "L1 越界值被丢弃", sorted(p.keys()))

print("M. 空串/None 不下发（前端留空的真实形态）")
p = run(base_cfg(model_params=mp(frequency_penalty="", presence_penalty=None)))
chk("frequency_penalty" not in p and "presence_penalty" not in p,
    "M1 空值不下发", sorted(p.keys()))

print("=" * 72)
if FAILS:
    print("结论：FAIL %d 项 -> %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("结论：全部 PASS")
