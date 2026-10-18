"""verify_stream_repair_loop — 判据：**stream.py（前端真实路径）也能"报了错就修"**。

## 缺陷（2026-10-09 实测八视图后确认）
`verify_p0_repair_round_tools.py` 只覆盖了**非流式** `execute.py`。
`stream.py`（前端 `/chat/stream` 的真实入口）的收尾是
**单次 `chat(..., stream=True)` 且不传 tools** ⇒
v6 报出 78 个语法错后，收尾只写了 28 token ⇒ **「报了错没人修」在前端路径同样成立**。

## 为什么不能直接给那个 stream 调用塞 tools
`stream=True` 返回**生成器**，**没有配套的工具执行循环**
⇒ provider 返回 `tool_calls` 而**无人消费** ⇒ 停摆（更糟：前端看到空白）。

## 已实施的正解
`stream.py` 的收尾改成：
1. `repair_tools_needed(messages)` 命中（仅 `判据：block`）
2. ⇒ **先用非流式探针**把工具跑掉（SSE `tool run/done` 事件照常 yield）
3. 上限 2 轮，且每轮 `charge_failed_round`（失败轮才扣预算）
4. ⇒ 再用**更新后的 messages** 做流式收尾
5. 任何异常都吞掉并降级（不得让整个流式请求失败）

## 判据
① 结构：`stream.py` 收尾里确有非流式探针 + 工具执行 + `_repair_msgs` 回填
② **不能**给流式收尾直接塞 tools（防回退到"停摆"实现）
③ 异常安全：修复循环被`try` 包住且有降级分支
④ 复用同一判据`repair_tools_needed`（与 execute.py 同源，不各写一套）
⑤ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_stream_repair_loop.py
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")


def structure_checks() -> list[tuple[str, bool]]:
    with open(STREAM, encoding="utf-8") as f:
        src = f.read()
    out = []

    # ⚠️ 判据必须**定位到收尾那一段**，不能全文件 `in`：
    #   首版用 `'stream=False' in src` ⇒ 首轮绿、变异后仍绿（因为文件别处
    #   也有 stream=False）⇒ **空转断言**，被变异自证当场抓出。
    #   正解：截取「收尾整段」，只在段内判。
    #⚠️⚠️ 第二版又踩一次：用 `rfind` 取到的是**最后**一个 `finalize_messages`
    #   （我新加的那个 `_fin_msgs = finalize_messages(_repair_msgs, …)`）
    #   ⇒ 截出 941 字符，**修复循环整段都在它之前** ⇒ 判据全部判红。
    #   ⇒ 必须用锚点**之前**的最后一个出现，即以 `finalize_messages(` 首次
    #   出现后的**注释块起点**为准。稳妥做法：锚定修复循环的特征串
    #   `repair_tools_needed`，从它往前回溯到 `_fin_msgs = _lproto.finalize_messages`。
    i = src.find("_lproto.repair_tools_needed(messages)")
    if i < 0:
        return [("★ 能在 stream.py 定位到修复循环（repair_tools_needed）", False)]
    # 往回扩到 finalize_messages 赋值，往后扩到 `if not tool_injected`
    start = src.rfind("_fin_msgs = _lproto.finalize_messages", 0, i)
    if start < 0:
        start = max(0, i - 1500)
    j = src.find("if not tool_injected", i)
    seg = src[start: j if j > 0 else i + 4000]

    out.append((f"能定位到收尾段（{len(seg)} 字符）", len(seg) > 1200))
    # ① 非流式探针：**必须**在收尾段内，且**必须**紧邻修复工具的调用
    has_probe = "stream=False" in seg
    out.append((f"收尾段内有**非流式**探针（stream=False）—— 段长 {len(seg)}",
                has_probe))
    out.append(("收尾段内探针与修复工具绑定（`tools=_repair_tools`）",
                "tools=_repair_tools" in seg))
    out.append(("收尾段里有工具执行（`_exec_tool_call`）",
                "_exec_tool_call" in seg))
    out.append(("收尾段里有 `_repair_msgs` 回填",
                "_repair_msgs" in seg))
    out.append(("修复循环有轮次上限（防放开成新 ReAct 循环）",
                "for _ri in range(2)" in seg))
    # ② 复execute.py 的同一判据（不各写一套）
    out.append(("复用 `_lproto.repair_tools_needed`（与 execute.py 同源）",
                "repair_tools_needed" in seg))
    # ③ 异常安全
    out.append(("修复循环被 try 包住并降级",
                "收尾修复轮跳过" in seg))
    # ④ 失败轮记账
    out.append(("修复轮也走 `charge_failed_round`（失败轮才扣预算）",
                "charge_failed_round" in seg))
    # ⑤ ★防回退：**流式**收尾仍不许传 tools
    #    2026-10-18 锚点演进（#10 拆型批次漂移）：收尾调用形态已变为
    #    `chat(_fin_msgs, ..., stream=True,\n  _intent=CHAT_TASK_ANSWER, _model_params=...)`
    #    —— 原锚串结尾的右括号 `)` 永远匹配不上（假红：seg2 取空）。
    #    改为不带右括号的锚；窗口放到 200 字符覆盖整个调用语句
    #    （tools= 若被加回来，只会落在这个窗口内）。
    stream_call = "chat(_fin_msgs, provider_id=effective_provider, stream=True"
    i2 = seg.find(stream_call)
    seg2 = seg[i2:i2 + 200] if i2 >= 0 else ""
    out.append((f"★ 流式收尾**不传 tools**（实测：...{seg2[:66]}…）",
                i2 >= 0 and "tools=" not in seg2))
    return out


def scope_check() -> list[tuple[str, bool]]:
    """作用域检查：修复循环里的变量不能「读了却从未赋值」。"""
    with open(STREAM, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    watched = {"_repair_msgs", "_repair_tools", "_got_ok", "_pm", "_pcalls",
               "_tc", "_fn", "_tn", "_ta", "_res"}
    out = []
    bad_all = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef):
            a = {x.id for x in ast.walk(n)
                 if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
            u = {x.id for x in ast.walk(n)
                 if isinstance(x, ast.Name) and not isinstance(x.ctx, ast.Store)}
            bad_all |= {x for x in u if x in watched and x not in a}
    out.append((f"修复循环变量无「读了未赋值」（实测 {sorted(bad_all) or '无'}）",
                not bad_all))
    return out


def compile_check() -> list[tuple[str, bool]]:
    with open(STREAM, encoding="utf-8") as f:
        src = f.read()
    try:
        compile(src, STREAM, "exec")
        return [("stream.py 语法可编译", True)]
    except SyntaxError as exc:
        return [(f"stream.py 语法可编译（SyntaxError: {exc}）", False)]


def main() -> int:
    print("=" * 72)
    print("判据 · stream.py（前端路径）收尾修复循环")
    print("=" * 72)
    ok = True
    for title, checks in [("① 结构与接线", structure_checks()),
                          ("② 变量作用域", scope_check()),
                          ("③ 语法", compile_check())]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    print("\n" + "=" * 72)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 72)
    if ok:
        print("\n[变异自证] 把收尾段内的非流式探针改成流式，判据应变红")
        with open(STREAM, encoding="utf-8") as f:
            src = f.read()
        # ★ 必须**在收尾段内**做替换：首版用 `src.replace("stream=False", ..., 1)`
        #   替换的是**文件里第一个** stream=False（在收尾段之外）
        #   ⇒ 收尾段没变 ⇒ 判据仍绿 ⇒ **空转断言**。
        #   ⇒ 先截段，再在段内替换，最后拼回去。
        i = src.find("_lproto.repair_tools_needed(messages)")
        start = src.rfind("_fin_msgs = _lproto.finalize_messages", 0, i)
        j = src.find("if not tool_injected", i)
        seg_old = src[start:j]
        assert "stream=False" in seg_old, "收尾段内没有 stream=False，变异锚点失效"
        seg_new = seg_old.replace("stream=False", "stream=True", 1)
        mutated = src[:start] + seg_new + src[j:]
        with open(STREAM, "w", encoding="utf-8") as f:
            f.write(mutated)
        try:
            red = not all(p for _, p in structure_checks())
            print(f"[变异自证] 结构判据 {'已变红（真判据）' if red else '**仍是绿 ⇒ 空转断言**'}")
            if not red:
                for nm, p in structure_checks():
                    if not p:
                        print(f"           ★ 变红：{nm}")
            ok = ok and red
        finally:
            with open(STREAM, "w", encoding="utf-8") as f:
                f.write(src)
        with open(STREAM, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")
        print("=" * 72)
        print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())