"""验证「思考强度」五档是否真的落到上游请求 payload（2026-10-31）。

为什么必须拦 payload 而不是读代码：
  `thinking` / `enable_thinking` / `reasoning_effort` 都**不是三个厂商通用的字段**，
  发错会 400、发漏会静默失效（"配了像没配"）。本地 mock 永远发现不了 ⇒
  只有把**真实发出去的 JSON** 截下来断言，才能证明链路通。

判据（矩阵）—— ★ 2026-10-31 依据实测修订，勿按"看起来更合理"改回去：
  ⚠️ deepseek 家族**忽略** budget_tokens（实测 provider 71：256/1024/4096/8192 → reasoning
     58/167/69/64，纯噪声；reasoning_effort 亦无效）。故 deepseek 的 低/中/高 **不发 budget**，
     改由提示词承担（高=放大，实测 3.2×；低=简短提示，实测约 -42%）。
     第一版门禁按"deepseek 会发 budget"写期望 ⇒ 实测后 3 项转红，
     那是**期望错了**，不是实现错了 —— 判据必须跟着实测反转（否则持续假红）。

  档位          deepseek/anthropic(不认预算时)   qwen(DashScope)          openai(o系/gpt-5)
  模型默认      不发任何思考字段                 不发                     不发
  关闭          thinking:{type:disabled}         enable_thinking:false    reasoning_effort:low
  低            thinking:{type:enabled}          enable_thinking+budget1024  reasoning_effort:low
  中            thinking:{type:enabled}          enable_thinking+budget4096  reasoning_effort:medium
  高            thinking:{type:enabled}          enable_thinking+budget8192  reasoning_effort:high
                + 深度提示词 + mt 扩到 9216

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_thinking_level.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import llm.providers.openai_compat as oc  # noqa: E402

_CAPTURED = []


class _FakeResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def _fake_post(url, json=None, headers=None, timeout=None, **kw):
    _CAPTURED.append(json or {})
    return _FakeResp()


def run_case(family_cfg, level, max_tokens=8192, tools=None):
    mp = {"thinking_mode": "模型默认"}
    if level is not None:
        mp["thinking_level"] = level
    cfg = dict(family_cfg)
    cfg["model_params"] = json.dumps(mp, ensure_ascii=False)
    _CAPTURED.clear()
    p = oc.OpenAICompatProvider(cfg)
    p.chat([{"role": "user", "content": "hi"}], max_tokens=max_tokens, tools=tools)
    return _CAPTURED[0]


def think_fields(payload):
    """抽出所有「思考相关」字段，便于断言不受其他键干扰。"""
    keys = ("thinking", "enable_thinking", "thinking_budget", "reasoning_effort")
    return {k: payload[k] for k in keys if k in payload}


DEEPSEEK = {"name": "ds", "base_url": "https://api.deepseek.com/v1", "model_name": "deepseek-chat",
            "api_key": "x", "provider_type": "openai", "context_window": 128000}
QWEN = {"name": "qw", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_name": "qwen3.8-max", "api_key": "x", "provider_type": "openai",
        "context_window": 128000}
OPENAI = {"name": "oa", "base_url": "https://api.openai.com/v1", "model_name": "o3-mini",
          "api_key": "x", "provider_type": "openai", "context_window": 128000}

CASES = [
    # (cfg,  档位,      期望 thinking 相关字段)
    (DEEPSEEK, "模型默认", {}),
    (DEEPSEEK, "关闭",    {"thinking": {"type": "disabled"}}),
    # ★ deepseek 不认 budget（实测），故 低/中/高 只发 enabled，预算靠提示词/不被支持
    (DEEPSEEK, "低",      {"thinking": {"type": "enabled"}}),
    (DEEPSEEK, "中",      {"thinking": {"type": "enabled"}}),
    (DEEPSEEK, "高",      {"thinking": {"type": "enabled"}}),
    (QWEN,     "模型默认", {}),
    (QWEN,     "关闭",    {"enable_thinking": False}),
    (QWEN,     "低",      {"enable_thinking": True, "thinking_budget": 1024}),
    (QWEN,     "高",      {"enable_thinking": True, "thinking_budget": 8192}),
    (OPENAI,   "模型默认", {}),
    (OPENAI,   "关闭",    {"reasoning_effort": "low"}),
    (OPENAI,   "低",      {"reasoning_effort": "low"}),
    (OPENAI,   "中",      {"reasoning_effort": "medium"}),
    (OPENAI,   "高",      {"reasoning_effort": "high"}),
]


def main():
    oc.httpx_post_orig = None
    import httpx
    httpx.post = _fake_post

    fails = []
    print("%-10s %-8s %-46s %s" % ("家族", "档位", "实际下发的思考字段", "结果"))
    print("-" * 96)
    for cfg, level, want in CASES:
        got = think_fields(run_case(cfg, level))
        ok = got == want
        if not ok:
            fails.append((cfg["name"], level, want, got))
        print("%-10s %-8s %-46s %s" % (cfg["name"], level, json.dumps(got, ensure_ascii=False), "OK" if ok else "FAIL"))
        if not ok:
            print("           期望: %s" % json.dumps(want, ensure_ascii=False))

    # 附加断言：预算不得吃光正文（低档 max_tokens=8192 时应保持 8192，不需扩容）
    p = run_case(DEEPSEEK, "低", max_tokens=8192)
    assert p["max_tokens"] >= 1024 + 1024, "低档把 max_tokens 压到正文无空间: %s" % p["max_tokens"]
    print("\n[ok] 低档 max_tokens=%s（≥ 预算+正文保底，正文未被吃光）" % p["max_tokens"])

    # 附加断言：极小 max_tokens + 高档 ⇒ 自动扩容到 预算+保底，避免正文只剩 0
    p2 = run_case(DEEPSEEK, "高", max_tokens=100)
    assert p2["max_tokens"] == 8192 + 1024, "高档未做正文保底扩容: %s" % p2["max_tokens"]
    print("[ok] 高档 max_tokens=100 → 自动扩容到 %s（预算 8192 + 正文保底 1024）" % p2["max_tokens"])

    # 附加断言（2026-10-31 新增，真实事故回归）：高档必须**带深度提示**且**留出正文额度**。
    #   事故现象：实测 reasoning=4095/4096 吃光 max_tokens ⇒ 正文 0 字（用户拿到空回答）。
    p4 = run_case(DEEPSEEK, "高", max_tokens=4096)
    _msgs = p4.get("messages") or []
    assert any("思考强度：高" in str(m.get("content") or "") for m in _msgs), \
        "高档未注入深度思考提示"
    assert p4["max_tokens"] >= 8192, "高档未给正文留额度: %s" % p4["max_tokens"]
    print("[ok] 高档注入深度提示，且 max_tokens=%s ≥ 8192（正文不会被思考吃光）" % p4["max_tokens"])

    # 附加断言：低档必须带简短提示（压不住也要如实下发）
    p5 = run_case(DEEPSEEK, "低", max_tokens=4096)
    _m5 = p5.get("messages") or []
    assert any("思考强度：低" in str(m.get("content") or "") for m in _m5), "低档未注入简短提示"
    print("[ok] 低档注入简短提示（deepseek 无预算能力时的唯一杠杆）")

    # ── 2026-11-01 新增：toggle（按开关）语义 ──────────────────────────────
    # 模型配置里选「按开关」时，会话只能传 开启/关闭。判据有三条：
    #   ① 只发 enabled/disabled 形态，**绝不带预算**（开关模式不控预算）
    #   ② **不加任何提示词**（用户选开关就是想要"和厂商控制台一致"，
    #      偷偷加"深度思考提示"等于擅自改了粒度）
    #   ③ 三家族各自映射到自己的字段
    for fam_cfg, fam_name in ((DEEPSEEK, "deepseek"), (QWEN, "qwen"), (OPENAI, "openai")):
        on = run_case(fam_cfg, "开启", max_tokens=4096)
        f_on = think_fields(on)
        has_budget = any("budget" in json.dumps(v) for v in f_on.values())
        no_hint = not any("思考强度" in str(m.get("content") or "")
                          for m in (on.get("messages") or []))
        assert not has_budget, "%s 开启 不该带预算: %s" % (fam_name, f_on)
        assert no_hint, "%s 开启 不该加提示词（开关模式不干预粒度）" % fam_name
        off = run_case(fam_cfg, "关闭", max_tokens=4096)
        assert think_fields(off), "%s 关闭 必须显式下发关闭字段" % fam_name
        print("[ok] %-8s toggle: 开启=%s（无预算/无提示） 关闭=%s"
              % (fam_name, json.dumps(f_on, ensure_ascii=False),
                 json.dumps(think_fields(off), ensure_ascii=False)))

    # 附加断言：模型默认必须**完全不下发**思考字段（零行为漂移的唯一判据）
    p3 = run_case(DEEPSEEK, None)
    assert think_fields(p3) == {}, "缺省档位却下发了思考字段: %s" % think_fields(p3)
    print("[ok] 缺省（无 thinking_level）⇒ 不下发任何思考字段（旧行为零漂移）")

    print("\n%d/%d 用例通过" % (len(CASES) - len(fails), len(CASES)))
    if fails:
        print("失败用例：")
        for f in fails:
            print("  ", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
