# -*- coding: utf-8 -*-
"""行内工具调用（DSML）还原与剥离门禁。

## 被保护的两个真实缺陷（都来自真实输出，不是构造的）

会话 9911（2026-11-01 对话级 A/B 实测产出）模型输出：

    <｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="file_list"> <｜｜DSML｜｜ parameter
    name="path" string="true">C:\\...\\mbse_system</｜｜DSML｜｜ parameter> ...

旧实现的两个后果：

1. **参数全丢**：`arguments` 退化成 `{}` ⇒ 工具用空参数调用 ⇒ 必然查不到东西。
   表现为"模型说要查文件、结果什么也没查到"，且**看不出是参数丢了**。
2. **标记漏进正文**：剥除正则匹配不到 ⇒ **用户直接看到 `<｜｜DSML｜｜ ...>` 这串乱码**。
   实测全库有 6 条消息含 DSML，其中 2 条是这种残留（msg 3056 / msg 5513）。

## 判据

  [T1] 工具名正确提取
  [T2] **参数完整提取**（旧实现的核心缺陷：`arguments == "{}"`）
  [T3] 标记从 content 中剥净（不含 DSML 字样）
  [T4] 工具调用**前后的人类正文保留**（不能因为剥标记把正常回答删掉）
  [T5] `reasoning_content` 原样保留（salvage 不得丢推理字段）
  [T6] 多个 invoke 各自拿到自己的参数（不串味）
  [T7] 自闭合/孤立标签也能清理
  [T8] 非 DSML 内容（普通回答）**不做任何改动**（零回归面）
  [M1] 变异：把参数正则退回"精确匹配结束标签" ⇒ T2 必须翻红
  [M2] 变异：去掉孤立标签清理 ⇒ T7 必须翻红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_inline_tool_calls.py
"""
import os
import re
import sys
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from llm.providers.openai_compat import (   # noqa: E402
    _salvage_inline_tool_calls, _strip_inline_markup)

V = "\uff5c"     # 全角竖线 U+FF5C（DeepSeek DSML 实际使用）
T = V + V        # 标记用的双竖线

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


def payload(content):
    return {"choices": [{"message": {"role": "assistant", "content": content,
                                     "reasoning_content": "推理中"}}]}


S1 = (f"<{T}DSML{T} calls> <{T}DSML{T} invoke name=\"file_list\"> "
      f"<{T}DSML{T} parameter name=\"path\" string=\"true\">C:\\Users\\gefei\\WorkBuddy\\x"
      f"</{T}DSML{T} parameter> </{T}DSML{T} invoke> </{T}DSML{T} calls>")

print("=" * 70)
print("[T] 还原与剥离（真实样本）")
print("=" * 70)

out = _salvage_inline_tool_calls(payload(S1))
check("T1 样本可被识别并还原", out is not None)
if out is None:
    print("\n还原失败，后续判据无法继续")
    sys.exit(1)
msg = out["choices"][0]["message"]
tc = msg.get("tool_calls") or []
check("T1b 工具名正确", bool(tc) and tc[0]["function"]["name"] == "file_list",
      f"got={tc[0]['function']['name'] if tc else None}")

args = {}
try:
    args = json.loads(tc[0]["function"]["arguments"]) if tc else {}
except Exception:
    args = {}
check("T2 ★ 参数完整提取（旧实现退化成 {}）",
      bool(args.get("path")) and "WorkBuddy" in str(args.get("path")),
      f"arguments={tc[0]['function']['arguments'][:60] if tc else None}")

check("T3 标记从正文剥净", "DSML" not in (msg.get("content") or ""),
      f"content={msg.get('content')!r}")

check("T5 reasoning_content 保留", msg.get("reasoning_content") == "推理中")

# T4：前后正文必须保留
S4 = (f"前置说明文字。<{T}DSML{T} invoke name=\"f\"> <{T}DSML{T} parameter name=\"k\">v"
      f"</{T}DSML{T} parameter> </{T}DSML{T} invoke> 后置说明文字。")
o4 = _salvage_inline_tool_calls(payload(S4))
c4 = (o4["choices"][0]["message"].get("content") or "") if o4 else ""
check("T4 工具调用前后的人类正文保留",
      "前置说明文字" in c4 and "后置说明文字" in c4, f"content={c4!r}")

# T6：两个 invoke 各拿自己的参数
S6 = (f"<{T}DSML{T} invoke name=\"tool_a\"> <{T}DSML{T} parameter name=\"x\">AAA"
      f"</{T}DSML{T} parameter> </{T}DSML{T} invoke> "
      f"<{T}DSML{T} invoke name=\"tool_b\"> <{T}DSML{T} parameter name=\"y\">BBB"
      f"</{T}DSML{T} parameter> </{T}DSML{T} invoke>")
o6 = _salvage_inline_tool_calls(payload(S6))
tcs = (o6["choices"][0]["message"].get("tool_calls") or []) if o6 else []
names = [t["function"]["name"] for t in tcs]
check("T6 多个 invoke 各自成调用", names == ["tool_a", "tool_b"], f"got={names}")
if len(tcs) == 2:
    a0 = json.loads(tcs[0]["function"]["arguments"])
    a1 = json.loads(tcs[1]["function"]["arguments"])
    check("T6b 参数不串味", a0.get("x") == "AAA" and a1.get("y") == "BBB",
          f"{a0} / {a1}")

# T7：孤立标签
check("T7 孤立闭合标签也能清理",
      "DSML" not in _strip_inline_markup(f"正文{f'</{T}DSML{T} invoke>'}尾部"),
      f"got={_strip_inline_markup(f'正文</{T}DSML{T} invoke>尾部')!r}")

# T8：普通回答零改动
plain = "这是一段正常回答，包含 <b>HTML</b> 与 a<b 形式的比较。"
check("T8 普通内容不被改动（零回归面）",
      _strip_inline_markup(plain) == plain, f"got={_strip_inline_markup(plain)!r}")
check("T8b 无标记时 salvage 返回 None（调用方保留原响应）",
      _salvage_inline_tool_calls(payload(plain)) is None)

print()
print("=" * 70)
print("[M] 变异自证")
print("=" * 70)

# M1：旧正则（精确匹配结束标签）在本样本上必然丢参数
_OLD_RE = re.compile(
    rf"parameter\s+name\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)"
    rf"<[|｜]{{2}}\s*DSML\s*[|｜]{{2}}\s*parameter\s*>",
    re.I | re.S)
_old_params = _OLD_RE.findall(S1)
check("M1 变异（旧正则精确匹配结束标签）在本样本上丢参数 ⇒ T2 的价值",
      _old_params == [] and bool(args.get("path")),
      f"旧正则 {len(_old_params)} 个参数，新实现 {len(args)} 个")

# M2：只删成对标签、不删孤立标签 ⇒ 残留
_only_pair = re.sub(
    rf"<\s*[|｜]{{1,3}}\s*DSML\s*[|｜]{{1,3}}[^>]*>[\s\S]*?"
    rf"</\s*[|｜]{{1,3}}\s*DSML\s*[|｜]{{1,3}}[^>]*>", "", S1, flags=re.I)
check("M2 变异（只删成对标签）会残留孤立标签 ⇒ T7 的价值",
      "DSML" in _only_pair and "DSML" not in _strip_inline_markup(S1),
      f"变异体残留={_only_pair.strip()[:40]!r}")

print()
print("=" * 70)
if _FAILS:
    print("总计：%d 项失败" % len(_FAILS))
    for f in _FAILS:
        print("  ✗", f)
    sys.exit(1)
print("总计：全部通过")
print("=" * 70)
