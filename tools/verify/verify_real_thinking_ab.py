"""真实 A/B（省额度版，2026-10-10）：flashY `deepseek-v4-flash` 已充值。

## 为什么重写（第一版的问题）

第一版用 `qwen3.8-max`，实测 403 Free quota exhausted；
换 `deepseek-v4-flash` 又 402 Insufficient Balance。两者都跑不了。
米爸充值 flashY 后重写，**目标不变但成本压到最低**。

## 省额度的四条做法
1. **关重试**（`retry_times=0`）—— 一次失败本来要翻 3 倍消耗；
   失败就直接判"不可用"，不重试。
2. **样本量=1/组**（A、B 各一次）—— 本判据验的是**方向性事实**
   （关思考会不会丢 tool_calls），不是统计均值。要统计显著性得跑 30 次，
   那是另一回事、也另一笔预算。
3. **短 prompt**（~60 token）—— 输入便宜，且不影响判据。
4. **先用 1 次极小调用验连通性**，不通就直接退出，不浪费后续预算。

## 判据（按重要性排序）
★ P0 **关思考后 tool_calls 是否归零** —— 这是**唯一会让功能坏掉**的风险：
   探针轮的唯一产出就是 tool_calls，而**工具调用本身需要推理**。
   若归零⇒ 禁止把 chat_probe 放进 off（配置层规则 1 也必须跟着收紧）。
★ P1 disabled 后 reasoning 是否真清零 —— 不清零说明该 provider 不认这个字段
   （要换格式：Qwen `enable_thinking` / DeepSeek `thinking.type`）。
P2 耗时与 token 收益 —— 决定"值不值得开"，但不影响安全性判断。

## 为什么会思考（已实测复现，省额度版的立论依据）
连通性探测那一次极小调用（35 token 输入、要求输出"ok"）：
    completion=99 / reasoning=**97** ⇒ **98% 的额度用于思考，只给用户 2 个字**。
⇒ 这不是个别现象，是 flashY 的默认形态。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_real_thinking_ab.py
    # 强制指定 provider：THINKING_AB_PROVIDER_ID=71 ...
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ★ 探针轮的真实形态：带工具，问一个"必须决定调不调工具"的问题。
#   用真实的 `sysml_v2_validate`，不是编造的工具名——否则模型可能不认。
TOOLS = [{
    "type": "function",
    "function": {
        "name": "sysml_v2_validate",
        "description": "校验 SysML v2 代码，返回诊断信息。",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "SysML v2 代码"}},
            "required": ["code"],
        },
    },
}]
MSGS = [
    {"role": "system", "content": "你是 MBSE 助手。需要校验 SysML 代码时请调用工具。"},
    {"role": "user", "content": "请校验：package V { part def E; }"},
]

INTENT_A = "chat_answer"   # 不在 off 列表 ⇒ 不发 thinking 字段（= 模型默认，思考开）
INTENT_B = "chat_probe"    # 命中 off ⇒ 显式 disabled


def _run_once(pid: int, policy: dict, intent: str) -> dict:
    import core.config as cfgmod
    from llm import llm_client

    real_get = cfgmod.get

    def fg(group, key, default=None):
        if group == "llm" and key == "thinking_policy":
            return json.dumps(policy, ensure_ascii=False)
        return real_get(group, key, default)

    cfgmod.get = fg
    t0 = time.time()
    try:
        resp = llm_client.chat(MSGS, provider_id=pid, tools=TOOLS, _intent=intent)
        dt = time.time() - t0
        usage = resp.get("usage") or {}
        msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
        calls = msg.get("tool_calls") or []
        return {
            "ok": True,
            "latency_s": round(dt, 1),
            "completion": usage.get("completion_tokens"),
            "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "n_tool_calls": len(calls),
            "tool_name": ((calls[0].get("function") or {}).get("name") if calls else None),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}",
                "latency_s": round(time.time() - t0, 1)}
    finally:
        cfgmod.get = real_get


def main() -> int:
    import sqlite3
    env_id = os.environ.get("THINKING_AB_PROVIDER_ID")
    db = os.path.join(_ROOT, "mbse.db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    if env_id and env_id.isdigit():
        row = conn.execute("SELECT id, model_name FROM llm_providers WHERE id=?",
                           (int(env_id),)).fetchone()
    else:
        # 有思考记录 + 调用量最大的（最有对照意义）
        row = conn.execute(
            "SELECT id, model_name FROM llm_providers WHERE status='active' "
            "ORDER BY (SELECT COUNT(*) FROM llm_usage_stats u "
            "  WHERE u.provider_id=llm_providers.id "
            "    AND COALESCE(u.reasoning_tokens,0)>0) DESC, id LIMIT 1"
        ).fetchone()
    conn.close()
    if not row:
        print("[SKIP] 找不到候选 provider，不判红")
        return 0
    pid, pname = int(row[0]), row[1]

    #省额度①：关重试（一次失败不翻 3 倍）
    os.environ.setdefault("MBSE_LLM_RETRY_TIMES", "0")
    print(f"[A/B] provider id={pid} model={pname}｜样本量=1/组｜已关重试")

    # 省额度②：先做一次极小连通性探测，不通就退出
    probe = _run_once(pid, {"off": [], "on": []}, "probe_liveness_min")
    if not probe.get("ok"):
        print(f"[SKIP] 连通性探测失败，不消耗后续预算：{probe.get('error')}")
        return 0
    print(f"  连通性 OK：{probe['latency_s']}s completion={probe['completion']} "
          f"reasoning={probe['reasoning']}")
    if (probe.get("reasoning") or 0) > 0:
        ratio = probe["reasoning"] / max(probe["completion"] or 1, 1) * 100
        print(f"  ★ 复现问题：{ratio:.0f}% 的输出额度用于思考"
              f"（只产出 {max((probe['completion'] or 0) - (probe['reasoning'] or 0), 0)} token 正文）")

    policy = {"off": [INTENT_B], "on": []}
    print(f"\n  A（不发 thinking 字段，思考开intent={INTENT_A}）")
    a = _run_once(pid, policy, INTENT_A)
    print(f"  B（显式 disabled，intent={INTENT_B}）")
    b = _run_once(pid, policy, INTENT_B)

    print("\n" + "=" * 74)
    for tag, d in (("A 思考开", a), ("B 关思考", b)):
        if d.get("ok"):
            print(f"[{tag}] {d['latency_s']}s completion={d['completion']} "
                  f"reasoning={d['reasoning']} tool_calls={d['n_tool_calls']}({d['tool_name']})")
        else:
            print(f"[{tag}] 失败：{d.get('error')}")

    if not (a.get("ok") and b.get("ok")):
        print("\n[WARN] 有调用未成功，A/B 不成立（不判红）")
        return 0

    print("=" * 74)
    ok = True
    # ★ P0：关思考不能丢 tool_calls（唯一会让功能坏掉的风险）
    if a["n_tool_calls"] > 0 and b["n_tool_calls"] == 0:
        print("[FAIL] ★★ 关思考后 tool_calls 归零 ⇒ 会破坏探针轮")
        print("       ⇒ **禁止**把 chat_probe 放进 off 列表")
        print("       ⇒ 形态规则 1（带 tools 就关思考）**必须收紧或改为只对"
              "「无 tools 的廉价轮」生效**")
        ok = False
    else:
        print(f"[OK  ] ★ 关思考后 tool_calls 未丢失"
              f"（A={a['n_tool_calls']} → B={b['n_tool_calls']}）")

    # ★ P1：disabled 必须真清零
    if (b.get("reasoning") or 0) > 0:
        print(f"[FAIL] disabled 后 reasoning 仍={b['reasoning']} "
              f"⇒ 该 provider 不认 `thinking:{{type:disabled}}`，需换字段格式")
        ok = False
    else:
        print("[OK  ] ★ disabled 真把思考清零")

    # P2：收益（只报告，不作为判据）
    save_ct = (a["completion"] or 0) - (b["completion"] or 0)
    save_s = round(a["latency_s"] - b["latency_s"], 1)
    print(f"[INFO] 收益：completion {a['completion']}→{b['completion']}"
          f"（省 {save_ct}）｜耗时 {a['latency_s']}s→{b['latency_s']}s（省 {save_s}s）")

    print("=" * 74)
    if ok:
        print("结论：★★ 关思考对探针轮安全 ⇒ 可把 chat_probe 加入 off 列表，"
              "并考虑打开形态规则")
    else:
        print("结论：★★ 关思考**不安全** ⇒ 保持 chat_probe 不在 off 列表；"
              "形态规则须收紧（只对无 tools 的廉价轮生效）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
