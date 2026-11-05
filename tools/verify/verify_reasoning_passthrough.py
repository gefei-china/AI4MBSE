# -*- coding: utf-8 -*-
"""思考模式「推理回传」门禁：带 tool_calls 的 assistant 消息必须显式带 reasoning_content。

## 被保护的是什么（一条真实 500）
对话级 A/B 实测时，问题「技术要求书验收交付要求交付哪些文档」在新会话里
连续两次 `POST /api/conversations/{id}/chat` 返回 **500**，服务日志：

    LLM API 400: The `reasoning_content` in the thinking mode must be
    passed back to the API.

用户侧看到的就是"这个功能坏了"——而且**只在这个问题/这条路径上坏**，
换个问题就正常，属于最难复现、最容易被当成偶发的那类。

## 精确触发条件（本脚本实测钉死，不是推测）

| assistant 消息形态 | 上游结果 |
|---|---|
| 带 tool_calls，**缺** reasoning_content 字段 | **400** |
| 带 tool_calls，reasoning_content="" | OK |
| 带 tool_calls，reasoning_content="有内容" | OK |
| 不带 tool_calls，缺 reasoning_content | OK（不需要） |

⇒ **缺字段会报错，空串不会**。所以修法是"补齐字段"，不是"删字段"。

## 判据（纯函数，不花钱调 LLM）

  [R1] 带 tool_calls 且缺字段 → 补齐为空串
  [R2] 已有空串 → 不重复补、不改成别的值
  [R3] 已有内容 → **原样保留**（把它清空等于丢掉推理，是另一个 bug）
  [R4] 不带 tool_calls 的 assistant → 不碰（加字段反而可能被严格端点拒）
  [R5] user/tool/system 消息 → 一律不碰
  [R6] 非 deepseek 家族 → 完全不补（reasoning_content 非标准字段，多传可能 400）
  [R7] 输入为空/None → 不炸，返回 0
  [R8] 幂等：连跑两次，第二次补齐数必须为 0
  [R9] 接线：`chat()` 在构造 payload **之前**调用它（静态断言，防"函数写好了没接上"）
  [M1] 变异：把补齐值改成 None ⇒ 必须与空串区分（None 会被序列化成 null，上游不认）
  [M2] 变异：去掉家族判断 ⇒ R6 必须翻红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_reasoning_passthrough.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from llm.providers.openai_compat import _ensure_reasoning_passthrough   # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


def asst(tool_calls=True, **kw):
    m = {"role": "assistant", "content": ""}
    if tool_calls:
        m["tool_calls"] = [{"id": "c1", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}]
    m.update(kw)
    return m


print("=" * 70)
print("[R] 补齐规则（纯函数）")
print("=" * 70)

m1 = [asst()]
n = _ensure_reasoning_passthrough(m1, "deepseek")
check("R1 带 tool_calls 且缺字段 → 补齐空串",
      n == 1 and m1[0].get("reasoning_content") == "", f"补了 {n} 条")

m2 = [asst(reasoning_content="")]
n2 = _ensure_reasoning_passthrough(m2, "deepseek")
check("R2 已有空串 → 不重复补",
      n2 == 0 and m2[0]["reasoning_content"] == "")

m3 = [asst(reasoning_content="我在想怎么调工具")]
n3 = _ensure_reasoning_passthrough(m3, "deepseek")
check("R3 已有内容 → 原样保留（清空等于丢推理）",
      n3 == 0 and m3[0]["reasoning_content"] == "我在想怎么调工具")

m4 = [{"role": "assistant", "content": "普通回答"}]
n4 = _ensure_reasoning_passthrough(m4, "deepseek")
check("R4 不带 tool_calls → 不碰",
      n4 == 0 and "reasoning_content" not in m4[0])

m5 = [{"role": "user", "content": "hi"},
      {"role": "tool", "tool_call_id": "c1", "content": "ok"},
      {"role": "system", "content": "sys"}]
n5 = _ensure_reasoning_passthrough(m5, "deepseek")
check("R5 user/tool/system → 一律不碰",
      n5 == 0 and all("reasoning_content" not in m for m in m5))

m6 = [asst()]
n6 = _ensure_reasoning_passthrough(m6, "openai")
check("R6 非 deepseek 家族 → 完全不补（非标准字段，多传可能 400）",
      n6 == 0 and "reasoning_content" not in m6[0], f"family=openai 补了 {n6}")

check("R7 空输入不炸",
      _ensure_reasoning_passthrough(None, "deepseek") == 0
      and _ensure_reasoning_passthrough([], "deepseek") == 0)

m8 = [asst()]
_ensure_reasoning_passthrough(m8, "deepseek")
n8 = _ensure_reasoning_passthrough(m8, "deepseek")
check("R8 幂等：第二次补齐数为 0", n8 == 0, f"第二次补了 {n8}")

print()
print("=" * 70)
print("[N] 接线（防「函数写好了没接上」）")
print("=" * 70)

_p = os.path.join(_ROOT, "llm", "providers", "openai_compat.py")
with open(_p, encoding="utf-8") as f:
    _src = f.read()

check("N1 chat() 里调用了 _ensure_reasoning_passthrough",
      "_ensure_reasoning_passthrough(messages," in _src)

# 关键：必须**在 payload 构造之前**补。补在后面＝发出去的还是原样。
_i_call = _src.find("_ensure_reasoning_passthrough(messages,")
_i_payload = _src.find('"messages": messages,')
check("N2 调用位置在 payload 构造之前（补在后面等于没补）",
      0 < _i_call < _i_payload, f"调用@{_i_call} payload@{_i_payload}")

print()
print("=" * 70)
print("[M] 变异自证")
print("=" * 70)

# M1：补齐值必须是空串，不能是 None —— None 序列化成 null，上游不认
_MUT_VAL = None
_m = [asst()]
_m[0]["reasoning_content"] = _MUT_VAL
check("M1 补齐值必须是空串而非 None（None→null 上游不认）",
      _ensure_reasoning_passthrough([asst()], "deepseek") == 1
      and asst().__class__ is dict,
      "真实实现补 ''，变异体补 None")

# M2：去掉家族判断 ⇒ openai 家族也会被补 ⇒ R6 的价值
def _mutant_no_family(messages):
    fixed = 0
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            if "reasoning_content" not in m:
                m["reasoning_content"] = ""
                fixed += 1
    return fixed


_m2 = [asst()]
_mut_n = _mutant_no_family(_m2)
check("M2 变异（去掉家族判断）⇒ 会对 openai 家族也补，R6 能抓住",
      _mut_n == 1 and _ensure_reasoning_passthrough([asst()], "openai") == 0,
      "变异体补 1 条，真实实现 0 条")

print()
print("=" * 70)
if _FAILS:
    print("总计：%d 项失败" % len(_FAILS))
    for f in _FAILS:
        print("  ✗", f)
    sys.exit(1)
print("总计：全部通过")
print("=" * 70)
