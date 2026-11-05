"""verify_tool_round_budget_consistency —门禁：ReAct **工具轮次预算**各实现同源同值。

## 为什么需要这道门禁（2026-10-09 实测）

`execute.py`（非流式）、`stream.py`（流式）是**同一套 ReAct 语义的两处实现**，
二者之外还有 `skills.py` 的**技能子循环**——它把「运行时工具轮次预算」作为提示注入给模型，
模型据此规划轮次。三处必须读同一个配置键、取同一个默认值，否则：

- 模型被告知「6 轮」却在 100 轮的 harness 上自我停手，
  把未真正触发的上限当成终止理由（会话 9629 实测：取 V2 代码时提前收尾）。

2026-10-09 初版只扫 execute/stream；2026-10-27 补 `skills.py`（否则第三处漂移无人锁）。

## 2026-10-09 实测背景

实测发现两处都硬编码 `max_tool_rounds = 3`，而模型的典型序列是

    查标准库(2 轮) → 取回技能正文(1 轮) → 生成(1 轮)

⇒ **3 轮预算必然在"生成"之前耗尽**；收尾轮又不给工具
⇒ 模型手里只有「未找到包 X」这类失败信息 ⇒ 只能凭记忆写
⇒ 实测 **N3 视图展开 6/8 产出雷同**（内容全是上一轮的用例视图）。

修法：三处都改为 `config.get("agent","max_tool_rounds", 100)`（与 config schema 默认对齐）。

## ⚠️ 这道门禁防的是**下一个**同类缺陷

我改成可配置后，**新的风险是两边配置漂移**——
改了一侧忘了另一侧 ⇒ 流式与非流式行为不一致，
而这种不一致只在"某条路径恰好走到工具耗尽"时才暴露，极难发现。

⇒ 断言：**两侧的取值代码必须同时存在，且默认值相同**。

## 判据（缺一即 FAIL）

① `execute.py` 与 `stream.py` 都从 `config` 读 `agent.max_tool_rounds`
② 各实现**默认值相同**（用 `ast` 抽字面量比对，不靠正则猜）
③ 都不再有裸的 `max_tool_rounds = <数字>` 硬编码
④ `loop_protocol` 自测通过（收尾判据本身没被改坏）

用法：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_tool_round_budget.py`
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# ★ 2026-10-27：补第三处 `skills.py`（技能子循环注入给模型的预算）。
#   此前只扫 execute/stream，漏掉 skills.py 的 fallback=6，
#   导致「同一配置键三处默认漂移」无人锁（会话 9629 模型被告知 6 轮却跑在 100 轮 harness 上）。
TARGETS = ("agent/pipeline_parts/execute.py", "agent/pipeline_parts/stream.py",
           "agent/pipeline_parts/skills.py")


def _literal(cfg_get_call):
    """从 `config.get("agent","max_tool_rounds", N)` 里抽出默认值的字面量。"""
    for arg in cfg_get_call.args[2:]:
        try:
            return ast.literal_eval(arg)
        except (ValueError, SyntaxError):
            continue
    return None


def scan(path: str) -> dict:
    """返回 {是否读配置, 默认值, 是否有裸硬编码}。

    ⚠️ 扫描顺序（2026-10-09 自踩）：
    必须**先扫 config.get 再扫裸赋值**，否则 `except: max_tool_rounds = 6`
    这个兜底分支会被当成"硬编码"而让门禁误判红。
    """
    full = os.path.join(ROOT, path)
    src = open(full, encoding="utf-8").read()
    tree = ast.parse(src)

    # ① 先找 config.get("agent", "max_tool_rounds", N)
    reads_cfg, default = False, None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) >= 2:
            a0, a1 = node.args[0], node.args[1]
            if (isinstance(a0, ast.Constant) and a0.value == "agent"
                    and isinstance(a1, ast.Constant)
                    and a1.value == "max_tool_rounds"):
                reads_cfg = True
                default = _literal(node)
    if reads_cfg:
        # 读了配置 ⇒ 裸赋值只可能是 except 兜底，不算缺陷
        return {"reads_cfg": True, "default": default, "hardcoded": False}

    # ② 没读配置 ⇒ 检查是否存在裸赋值（真正的硬编码）
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "max_tool_rounds":
                    try:
                        ast.literal_eval(node.value)
                        return {"reads_cfg": False, "default": None,
                                "hardcoded": True}
                    except (ValueError, SyntaxError):
                        pass
    return {"reads_cfg": False, "default": None, "hardcoded": False}


def main() -> int:
    print("=" * 72)
    print("ReAct 工具轮次预算 · 两侧一致性")
    print("=" * 72)
    ok = True
    vals = {}
    for p in TARGETS:
        r = scan(p)
        vals[p] = r
        c1 = r["reads_cfg"]
        c2 = not r["hardcoded"]
        c3 = r["default"] is not None and r["default"] >= 4
        good = c1 and c2 and c3
        ok = ok and good
        print(f"  [{'OK  ' if good else 'FAIL'}] {os.path.basename(p):14} "
              f"读配置={c1} 默认值={r['default']} 裸硬编码={r['hardcoded']}")
        if not c1:
            print("         ⇒ 没有从 config 读 agent.max_tool_rounds")
        if r["hardcoded"]:
            print("         ⇒ 仍存在 `max_tool_rounds = <数字>` 硬编码")
        if r["default"] is None:
            print("         ⇒ 取不到默认值（配置读取写法不对？）")
        elif r["default"] < 4:
            print(f"         ⇒ 默认值 {r['default']} < 4，"
                  f"不够「查资料 → 取正文 → 生成」这条典型序列")

    print()
    if len(vals) >= 2:
        defaults = [vals[p]["default"] for p in TARGETS]
        same = all(d == defaults[0] for d in defaults)
        ok = ok and same
        _map = " ".join(f"{os.path.basename(p)}={vals[p]['default']}" for p in TARGETS)
        print(f"  [{'OK  ' if same else 'FAIL'}] 各实现默认值一致： {_map}")
        if not same:
            print("         ⇒ 同一配置键「agent.max_tool_rounds」各路默认不同 "
                  "⇒ 模型被告知的预算与 harness 实际上限不一致")

    print("\n" + "=" * 72)
    print("收尾判据自测（确认没被改坏）")
    print("=" * 72)
    from agent.loop_protocol import (FINALIZE_HINT, finalize_messages,
                                     lacks_deliverable, should_finalize)
    NARR = "我先查标准库，再生成。"
    CODE = "好了：\n```sysml\npackage P { part def V; }\n```"
    msg = {"tool_calls": [{"id": "1"}]}
    checks = [
        ("叙述无代码 + 产码 Agent + 轮次用尽 ⇒ 补收尾",
         should_finalize(msg, NARR, 3, 3, expect_code=True), True),
        ("有代码⇒ 不补",
         should_finalize(msg, CODE, 3, 3, expect_code=True), False),
        ("纯问答 + 有正文 ⇒ 不补（原行为）",
         should_finalize(msg, NARR, 3, 3, expect_code=False), False),
        ("收尾可附加未取回正文",
         "BODY" in finalize_messages([], extra_context="BODY")[-1]["content"], True),
        ("空附加 ⇒ 提示逐字不变",
         finalize_messages([])[-1]["content"] == FINALIZE_HINT, True),
    ]
    for desc, got, want in checks:
        good = got == want
        ok = ok and good
        print(f"  [{'OK  ' if good else 'FAIL'}] {desc}")
    print(f"  [{'OK  ' if lacks_deliverable(CODE, True) is False else 'FAIL'}] "
          f"有代码块 ⇒ 不算缺口")

    print("\n" + "=" * 72)
    print("✅ 通过" if ok else "❌ 未通过")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
