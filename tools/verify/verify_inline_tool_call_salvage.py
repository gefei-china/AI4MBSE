"""verify_inline_tool_call_salvage —门禁：行内工具调用兜底解析有效。

## 为什么需要（2026-10-09 实测）

部分模型（实测 `deepseek-v4-flash`）**不总是**把工具调用放进标准 `tool_calls` 字段，
而是把标记语言**直接写进 content 文本**：

```
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="sysml_v2_validate">
<｜｜DSML｜｜ parameter name="code" string="true">package P { ... }<｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ endinvoke>
<｜｜DSML｜｜ end_calls>
```

⇒ 上游 `msg.get("tool_calls")` 拿不到 ⇒ 判定"无工具调用" ⇒ 循环直接结束
⇒ 模型想调的工具没调、代码没产出，**还会把上一轮的代码贴出来**
（实测 N3 视图展开 `state`/`parameter` 产出雷同 3606 字符即此因）。

⚠️ 这是 **provider 层的模型兼容性缺口**，与哪个 Agent 无关
⇒ 修在 `llm/providers/openai_compat.py`，对所有 OpenAI 兼容模型生效。

## 判据（缺一即 FAIL）

① 用**真实格式**（从库里捞的 deepseek 原文 + 补齐结束标记）能解析出
   `tool_calls`，且**参数完整**（`instruction` + `code` 都在）
② 标记语言从 `content` 里**完全清除**
③ **绝不覆盖标准路径**：已有 `tool_calls` 时返回 None（不动）
④ 无标记的普通响应返回 None（**零回归面**）
⑤ 剥离后的正文仍保留模型的**原始叙述**（不能整段丢）

用法：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_inline_tool_call_salvage.py`
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from llm.providers.openai_compat import _salvage_inline_tool_calls  # noqa: E402

#: ★ 从生产库捞的**真实** deepseek-v4-flash 输出前缀（含真实的中文叙述）
#:   不手写样例——手写的格式很容易与真实不符，测了等于没测。
_REAL_CONV = 9169


def real_sample() -> str:
    db = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(db):
        return ""
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute(
            "SELECT content FROM messages WHERE conversation_id=? "
            "AND role='assistant' ORDER BY id DESC LIMIT 1",
            (_REAL_CONV,)).fetchone()
        return (r["content"] if r else "") or ""
    finally:
        c.close()


def build_full(real: str) -> str:
    """把库里那段（缺结束标记的）补成完整格式。"""
    head = real[:real.find("<｜｜DSML｜｜ calls>")] if "<｜｜DSML｜｜ calls>" in real \
        else real
    return (
        head
        + '<｜｜DSML｜｜ calls>\n'
        + '<｜｜DSML｜｜ invoke name="sysml_v2_validate">\n'
        + '<｜｜DSML｜｜ parameter name="instruction" string="true">'
          '参数视图：约束定义 + 属性绑定'
          '<｜｜DSML｜｜ parameter>\n'
        + '<｜｜DSML｜｜ parameter name="code" string="true">'
          'package P { part def V; }'
          '<｜｜DSML｜｜ parameter>\n'
        + '<｜｜DSML｜｜ endinvoke>\n'
        + '<｜｜DSML｜｜ end_calls>'
    )


def main() -> int:
    ok = True

    def chk(desc, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'OK  ' if cond else 'FAIL'}] {desc}")

    real = real_sample()
    print("=" * 72)
    print("行内工具调用兜底解析")
    print("=" * 72)
    print(f"  真实样本来源：会话 {_REAL_CONV}"
          f"（{len(real)} 字符，DSML 标记 {'有' if 'DSML' in real else '无'}）")

    print("\n① 真实格式能被解析")
    full = build_full(real)
    out = _salvage_inline_tool_calls(
        {"choices": [{"message": {"role": "assistant", "content": full}}]})
    chk("解析出 tool_calls", out is not None)
    if out is None:
        return 1
    calls = out["choices"][0]["message"].get("tool_calls") or []
    chk(f"调用数=1（实得 {len(calls)}）", len(calls) == 1)
    if calls:
        chk(f"工具名= sysml_v2_validate（实得 {calls[0]['function']['name']}）",
            calls[0]["function"]["name"] == "sysml_v2_validate")
        args = json.loads(calls[0]["function"]["arguments"])
        chk(f"参数含 instruction 与 code（实得 {list(args)}）",
            "instruction" in args and "code" in args)
        chk("code 值精确", args.get("code") == "package P { part def V; }")

    print("\n② 标记语言被完全清除")
    body = out["choices"][0]["message"]["content"]
    chk("content 不含 DSML", "DSML" not in body)
    chk(f"content 不含 invoke=（实得 {len(body)} 字符）", "invoke" not in body)

    print("\n③ 绝不覆盖标准路径")
    with_tc = {"choices": [{"message": {
        "content": "x",
        "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "already", "arguments": "{}"}}]}}]}
    chk("已有标准 tool_calls ⇒ 返回 None（不碰）",
        _salvage_inline_tool_calls(with_tc) is None)

    print("\n④ 零回归面")
    plain = {"choices": [{"message": {"content": "这是一段普通回答，没有工具。"}}]}
    chk("无标记 ⇒ 返回 None", _salvage_inline_tool_calls(plain) is None)
    chk("空 choices ⇒ 返回 None",
        _salvage_inline_tool_calls({"choices": []}) is None)
    chk("畸形输入不抛异常",
        _salvage_inline_tool_calls({"choices": [{"message": None}]}) is None)

    print("\n⑤ 剥离后保留原始叙述")
    chk("body 非空", bool(body.strip()))
    chk("body 含模型的原始叙述（'已核实' / '现在产出'）",
        ("已核实" in body) or ("现在产出" in body) or (not real.strip()))

    print("\n" + "=" * 72)
    print("✅ 通过：行内工具调用能被还原，标准路径不受影响" if ok
          else "❌ 未通过")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
