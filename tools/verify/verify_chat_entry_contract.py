# -*- coding: utf-8 -*-
"""门禁：对话入口（附件 / 引用 / 发送）的**契约**（2026-10-30）。

## 三个已发生的真实故障，全部在"入口"这一段，且全部**静默**
| 现象（用户原话） | 真因 |
|---|---|
| 「从产物列表加入会话后发送异常，附件不见了，用户输入还在输入框，会话没发起」 | `sendChat()` 里 `const msg` 又被 `msg = _refs.map(...)` 赋值 ⇒ `TypeError: Assignment to constant variable`。抛点在 `chatRefs.splice()` **之后**、`input.value=''` **之前** ⇒ 三个症状同时出现 |
| 「选『当前会话的文件』列表为空，但产物里明明有代码文件」 | 该清单只扫 `messages[].attachments`（上传附件），**没扫 `artifacts`**（AI 产物是另一个数据源）|
| 「产物里有的文件无法查看」 | 清单只有「添加」一个动作，没有「预览」——只读诉求被迫绑定写入动作 |

## 为什么必须做成门禁（而不是"修一次就行"）
三处失效的共同特征是**错了也不报错**：
  · 前两个是"代码写了但语义错"，只有执行到那一行才炸；
  · 第二个是"少查一个数据源"，页面表现是"空列表"，看上去像"本来就没有"。
`node --check` 抓不到（赋值 const 是运行时错误，实测 exit 0）。
所以把**契约**本身写成断言，并各自配一条变异自证。

## 判据
| # | 断言 |
|---|---|
| C1 | `sendChat` 内 `msg` 用 `let` 声明（不得 `const`），且存在"引用前置"赋值 |
| C2 | `sendChat` 不得有裸 `if(!msg) return`（只有附件/引用时不许静默不发送）|
| C3 | `loadAttachProjectFiles` **同时**消费 messages 与 artifacts 两个数据源 |
| C4 | `renderAttachProjectList` 的点击索引回到**全量**数组（`indexOf`），不用过滤后下标 |
| C5 | `attachConvFile` 对产物走正文引用通道（`artAddToChat`），不得塞进附件栏 |
| C6 | 变异自证：①`let msg`→`const msg` ②去掉 `indexOf` ③去掉 artifacts 拉取 ⇒ 各自判红 |

⚠️ C5 的依据（可复核）：后端 `_load_attachment_text` 只认两种附件来源 ——
   `doc_id`（文档库 chunk）或 `url` 以 `/static/` 开头；而 `artifacts.file_url` 实测为空串、
   也没有 doc_id ⇒ 产物进附件栏必然被记为 skipped（模型说"没收到附件"）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_chat_entry_contract.py
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

PASS, FAIL = "PASS", "FAIL"
_results = []

_SEND = os.path.join(_ROOT, "static", "js", "mods", "12-chatsend.js")
_INPUT = os.path.join(_ROOT, "static", "js", "mods", "10-chatinput.js")


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def body_of(src, fn):
    """切出函数体：从 `function NAME(` 到**下一行列首的 `}`**。

    为什么用"列首 }"而不是括号配平：本仓 JS 全是列首缩进风格，列首 `}` 就是
    函数结束，比手写配平可靠得多 —— 后者在 `function f({a, b})` 这种**解构参数**
    上会把参数的 `}` 当成函数体结束（本轮试写通用扫描器时就栽在这里，
    两条误报全部来自解构/CSS 模板串）。
    """
    m = re.search(r"^(?:async\s+)?function\s+%s\s*\(" % re.escape(fn), src, re.M)
    if not m:
        return None
    lines = src[m.start():].split("\n")
    for i, ln in enumerate(lines[1:], 1):
        if ln == "}":
            return "\n".join(lines[:i + 1])
    return None


def check_send_contract(src_send, mutate=None):
    """C1/C2。mutate: None | 'const' | 'no_cargo'（用于变异自证）。"""
    b = body_of(src_send, "sendChat")
    if b is None:
        return False, "找不到 sendChat（切分锚点失效）", ""
    m_src = b
    if mutate == "const":
        m_src = b.replace("let msg = input.value.trim();", "const msg = input.value.trim();", 1)
    if mutate == "no_cargo":
        m_src = re.sub(r"if\(!msg && !_hasCargo\) return;", "if(!msg) return;", m_src, 1)
    ok1 = ("let msg = input.value.trim();" in m_src
           and "const msg = input.value.trim();" not in m_src
           and re.search(r"msg\s*=\s*_refs\.map\(", m_src) is not None)
    ok2 = ("if(!msg) return;" not in m_src) and ("_hasCargo" in m_src)
    return ok1, ok2, b


def check_sources(src_input, mutate=None):
    """C3/C4/C5。mutate: None | 'no_artifacts' | 'no_indexof'"""
    b1 = body_of(src_input, "loadAttachProjectFiles") or ""
    b2 = body_of(src_input, "renderAttachProjectList") or ""
    b3 = body_of(src_input, "attachConvFile") or ""
    if mutate == "no_artifacts":
        b1 = re.sub(r"const arts = await api\(`/api/artifacts\?conversation_id=\$\{currentConvId\}`\);",
                    "const arts = [];", b1, 1)
    if mutate == "no_indexof":
        b2 = b2.replace("const gi = _attachProjectDocs.indexOf(d);", "const gi = i;", 1)
    ok3 = ("/messages?limit=" in b1) and ("/api/artifacts?conversation_id=" in b1)
    ok4 = ("_attachProjectDocs.indexOf(d)" in b2) and ("attachConvFile(${gi})" in b2)
    ok5 = ("artAddToChat" in b3) and ("kind === 'artifact'" in b3)
    return ok3, ok4, ok5


def main():
    print("=" * 78)
    print("对话入口契约（附件 / 引用 / 发送）")
    print("=" * 78)
    for p in (_SEND, _INPUT):
        if not os.path.exists(p):
            _rec("前置：%s 存在" % os.path.basename(p), False, "文件缺失")
            print("\n结论：FAIL（前置缺失）")
            return 1
    src_send, src_input = _read(_SEND), _read(_INPUT)

    print("\n=== C1-C2 sendChat：msg 可变 + 有材料时不静默 ===")
    ok1, ok2, body = check_send_contract(src_send)
    _rec("C1 sendChat 的 msg 用 let 声明，且有「引用前置」赋值", ok1,
         "" if ok1 else "若为 const ⇒ TypeError: Assignment to constant variable"
                        "（抛点在 splice 之后 / 清空输入之前 ⇒ 附件消失+输入残留+会话未发起）")
    _rec("C2 sendChat 无裸 if(!msg) return（只有附件时也要能发）", ok2,
         "" if ok2 else "裸 return 会让「只加附件不打字」时点发送毫无反应")

    print("\n=== C3-C5 会话文件清单：两个数据源 + 正确索引 + 产物走引用 ===")
    ok3, ok4, ok5 = check_sources(src_input)
    _rec("C3 清单同时消费 messages 与 artifacts（产物不再缺席）", ok3,
         "" if ok3 else "只扫 messages.attachments ⇒ 产物里的代码文件永远看不到")
    _rec("C4 点击索引回到全量数组（搜索过滤后不取错项）", ok4,
         "" if ok4 else "用过滤后下标 ⇒ 搜索后点第一条却加了另一条")
    _rec("C5 产物走正文引用通道（artAddToChat），不进附件栏", ok5,
         "" if ok5 else "产物无 doc_id/file_url ⇒ 进附件栏会被后端判「不支持的地址」")

    print("\n=== C6 变异自证（三条各自必须判红）===")
    m1a, m1b, _ = check_send_contract(src_send, mutate="const")
    _rec("M1 let→const ⇒ C1 判红", not m1a,
         "" if not m1a else "判据没读声明关键字 ⇒ C1 是空转")
    m2a, m2b, _ = check_send_contract(src_send, mutate="no_cargo")
    _rec("M2 复原裸 if(!msg) return ⇒ C2 判红", not m2b,
         "" if not m2b else "C2 空转")
    n3, n4, n5 = check_sources(src_input, mutate="no_artifacts")
    _rec("M3 去掉 artifacts 拉取 ⇒ C3 判红", not n3,
         "" if not n3 else "C3 空转")
    n3b, n4b, n5b = check_sources(src_input, mutate="no_indexof")
    _rec("M4 索引退回过滤后下标 ⇒ C4 判红", not n4b,
         "" if not n4b else "C4 空转")

    print("\n" + "=" * 78)
    fails = [r for r in _results if r[0] == FAIL]
    print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
