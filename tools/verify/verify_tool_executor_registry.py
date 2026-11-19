# -*- coding: utf-8 -*-
"""「工具名 → 执行器」注册表门禁（2026-11-19）。

锁定的不变式：**「登记」与「接线」必须一致**。

四次同型事故（都以「模型看得见 → 调用 → 必败」收场）：
  1. `conflict_check` 假实现（恒返回「无新冲突」）⇒ 2026-10-13 连登记一起删；
  2. `code_exec` / `doc_parse` / `requirement_itemize` 实现好了但分发点没 import
     ⇒ 真机 46 次调用全返回「未知工具」；
  3. `save_artifact` 有实现但 BUILTIN_DESC 漏登记 ⇒ 绑定了也不注入；
  4. `entity_create` 至今无执行分支，却被 seeds 绑给 requirement_analysis / design
     ⇒ 这两个 Agent 每轮都拿到一个必败工具（本门禁把它钉成**显式例外**）。

判据（D 组）：
  D1 注册表非空且规模合理
  D2 BUILTIN_DESC - 注册表 == 显式例外集合（当前只有 entity_create）
  D3 源码里每个 `if name == "X"` 的 X 都在注册表里（防新增分支忘登记）
  D4 判据非恒真非恒假（一真一假两个样本）
  D5 注入过滤真的接线了（过滤在 append 之前 + 剔除留痕属性存在）

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_tool_executor_registry.py
退出码：全绿 0 / 有失败 1。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []


def ck(tag, ok, detail=""):
    _results.append((PASS if ok else FAIL, tag))
    print("  %-5s %s%s" % (PASS if ok else FAIL, tag, ("  | " + str(detail)) if detail else ""))


#: 已知无执行器、且**故意**留在 BUILTIN_DESC 里的名字（新增必须改本集合 + 写理由）。
KNOWN_SHELLS = {"entity_create"}


def main():
    print("=" * 84)
    print("工具执行器注册表门禁（2026-11-19）")
    print("  repo=%s" % ROOT)
    print("=" * 84)

    from agent.pipeline_parts.tools import (BUILTIN_DESC, EXECUTABLE_BUILTIN_NAMES,
                                            builtin_has_executor)
    src = open(os.path.join(ROOT, "agent", "pipeline_parts", "tools.py"),
               encoding="utf-8").read()

    print("\n[D] 注册表与注入过滤")
    ck("D1 注册表规模合理（>=25）", len(EXECUTABLE_BUILTIN_NAMES) >= 25,
       "%d 个" % len(EXECUTABLE_BUILTIN_NAMES))
    ck("D1b 注册表不含空串/None", "" not in EXECUTABLE_BUILTIN_NAMES,
       sorted(EXECUTABLE_BUILTIN_NAMES)[:3])

    diff = set(BUILTIN_DESC) - set(EXECUTABLE_BUILTIN_NAMES)
    ck("D2 BUILTIN_DESC - 注册表 == 显式例外", diff == KNOWN_SHELLS,
       "差集=%s（例外=%s）" % (sorted(diff), sorted(KNOWN_SHELLS)))

    singles = set(re.findall(r'if name == "([a-z0-9_]+)"', src))
    orphan = sorted(singles - set(EXECUTABLE_BUILTIN_NAMES))
    ck("D3 所有 `if name ==` 分支都在注册表里", not orphan, "缺登记=%s" % orphan)

    ck("D4 判据非恒假：file_read 有执行器", builtin_has_executor("file_read") is True)
    ck("D4b 判据非恒真：entity_create 无执行器", builtin_has_executor("entity_create") is False)

    i_filter = src.find("if not builtin_has_executor(name):")
    i_append = src.find('candidates.append((name, builtin_desc[name], "builtin", ""))')
    ck("D5 过滤接线（过滤在 append 之前）", 0 < i_filter < i_append,
       "filter@%d append@%d" % (i_filter, i_append))
    ck("D5b 剔除留痕属性 _dropped_no_executor 存在", "_dropped_no_executor" in src)
    ck("D5c 假名同样被判无执行器", builtin_has_executor("__shell_probe__") is False)

    # ── D6 功能自证：把一个"有登记、无执行器"的假工具绑给某个 Agent，看它进不进 payload ──
    # 为什么必须做功能级：D1–D5 全是"读源码"，源码里写了过滤但**分支没被走到**也全绿
    # （本仓既有教训：判据只复述实现）。这里真跑一次 `_build_tools_def`。
    try:
        from types import SimpleNamespace
        import agent.pipeline_parts.tools as T
        from agent import AgentPipeline
        _pipe = AgentPipeline()
        _pipe._load_db_agents()
        T.BUILTIN_DESC["__shell_probe__"] = "探针假工具（登记了但没有任何执行分支）"
        _orig = _pipe.registry.get
        _pipe.registry.get = lambda intent: SimpleNamespace(
            tools=["__shell_probe__"], system_prompt="", name=intent)
        try:
            _defs = _pipe._build_tools_def("__registry_probe__", "任意输入", None)
            _names = [d["function"]["name"] for d in _defs]
        finally:
            _pipe.registry.get = _orig
            T.BUILTIN_DESC.pop("__shell_probe__", None)
        ck("D6 无执行器的登记项不进 payload", "__shell_probe__" not in _names,
           "payload=%d 项" % len(_names))
        ck("D6b 剔除留痕（不静默）",
           "__shell_probe__" in (getattr(_pipe, "_dropped_no_executor", None) or []),
           getattr(_pipe, "_dropped_no_executor", None))
    except Exception as e:                                   # noqa: BLE001
        ck("D6 无执行器的登记项不进 payload", False, "异常：%s" % str(e)[:120])

    fails = [t for s, t in _results if s == FAIL]
    print("\n" + "-" * 84)
    print("合计 %d 项，失败 %d 项" % (len(_results), len(fails)))
    if fails:
        for t in fails:
            print("  · " + t)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
