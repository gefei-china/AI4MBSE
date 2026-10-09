# -*- coding: utf-8 -*-
"""verify_request_state_reset —— 防「单例Pipeline 跨请求状态泄漏」的门禁。

## 为什么需要这道门禁（2026-10-09 实测，非推断）

对话入口是 `routers/conversations.py` 的 `from agent import agent`
⇒ **模块级单例 Pipeline，所有用户、所有请求共用一个实例**。
Pipeline 把"本次请求的中间产物"挂在 `self` 上 ⇒ 入口不清零就会跨请求残留。

实测危害（N3 视图展开 6/8 产出雷同的**根因**）：
  `tools.py` 在 `sysml_v2_*` 分派里写 `self._sysml_last_pass_code`，
  `cards.py` 的 `_ensure_sysml_from_tools` / `_gen_sysml_views`
  在**正文无代码时**拿它兜底交付 ⇒ **上一个请求的代码被当成本次产出**。
  现象正是此前反复记录到的"叙述是新的、代码是旧的"。

⚠️ 这类缺陷**不会被任何既有门禁抓到**：单次跑必绿（首个请求本来就干净），
   只有**同实例连跑**才暴露 ⇒ 必须有专门的门禁。

## 判据（三条，均为可复现断言）

A1 · **入口不变量**：`execute()` 与 `execute_stream()` 都调用了
    `reset_request_state(self)`，且调用位置**早于**本方法内所有 `self.X =` 赋值。
    （顺序错会抹掉本次请求刚设好的 `_mem_ctx` / `_tool_conv_ctx` —— 这是本轮真实踩过的坑）
A2 · **覆盖完整性**：把 `agent/pipeline_parts/*.py` 里所有 `self.X = ...` 赋值扫出来
    （X 以 `_` 开头 = 请求级瞬态），与 `reset_request_state` 里登记的字段求差集
    ⇒ 差集必须为空。**新增字段忘了登记会判红**，不靠人记得。
A3 · **无泄漏**（端到端）：单实例连跑两次请求，
    请求B 的交付内容里**不得**出现请求A 的代码。

## 为什么 A2 要扫源码而不是只看清单

MEMORY 里的教训：清单/登记制会过期。本门禁的差集检查让
"新增一个 `self._xxx` 字段却忘了清零"这件事**立刻判红**，而不是等线上出问题。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_request_state_reset.py
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PARTS_DIR = os.path.join(_ROOT, "agent", "pipeline_parts")

#: 本轮**故意**不做变异自证的字段：它们是"刻意不清零"的实例状态
#: （每次执行由 `_load_db_agents` / `load_from_db`整体重建，不存在跨请求脏值）。
#:若把它们改成"有值才设"，`reset_request_state` 反而会破坏正常运行。
ALLOWED_UNRESET = {
    "router", "rag", "conflict", "registry",       # __init__ 里建、整体复用
    "_retry_policy_cache",                          # 工具重试策略（按工具名缓存，跨请求安全）
    "_mem_deposit_count",                           # 记忆写入计数（单调递增，语义上就该全局）
    "_orch_acc", "_orch_token",                     # 编排累计（在 with/finally 内成对初始化）
    # ↓ 以下均**不是单例 Pipeline 的请求级状态**，实测确认（2026-10-09）：
    #   `_ns_ctx` / `_ns_kb_scope` / `_orch_subtask` / `_tool_run_ctx` / `_tool_task_key`
    #       —— 全是 stream.py 的 **worker 里 `sub._xxx = ...`**：
    #          `sub` 是编排子任务**新建的短生命周期子 Pipeline**（stream.py:396-403），
    #          每个子任务新建一个 ⇒ 不跨请求，也不跨子任务。
    #          ⚠️ 其中 `_orch_subtask` 虽在 finally 里复位为 False，但读取侧一律用
    #             `getattr(self, "_orch_subtask", False)` ⇒ 默认值就是安全值，
    #             复位与否行为一致，清它反而是噪声。
    "_ns_ctx", "_ns_kb_scope", "_orch_subtask", "_tool_run_ctx", "_tool_task_key",
    #   `_CONTINUATION_RE` —— history.py 里的**类级**正则缓存（`cls._CONTINUATION_RE = ...`），
    #   不是实例字段，且正则编译一次复用是**有意为之**（避免每请求重编译）。
    "_CONTINUATION_RE",
}

_fails: list[str] = []
_passes: list[str] = []


def _ok(msg: str) -> None:
    _passes.append(msg)
    print(f"[PASS] {msg}")


def _bad(msg: str) -> None:
    _fails.append(msg)
    print(f"[FAIL] {msg}")


# ─────────────────────────A2：扫出全部请求级瞬态赋值字段 ─────────────────────────
def _collect_assigned_fields() -> set[str]:
    """扫 pipeline_parts/*.py 的 AST，取所有 `<实例>.<字段> = ...` 的字段名。

    ⚠️ 2026-10-09 自测踩坑（判据自身口径缺陷，会假红）：
      首版只认 `self.X =`，而 `reset_skill_state` 里的写法是 `pipe.X =`
      （形参名叫 pipe）⇒ `_skill_forced` 被误报成"清单里已不再赋值"。
      ⇒ 这里改为**认任何实例变量名**（`self` / `pipe` 等），字段名本身才是判据对象。
    """
    found: set[str] = set()
    for fn in sorted(os.listdir(PARTS_DIR)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(PARTS_DIR, fn)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            # 只认赋值/增删的目标，且必须是 `<某个名字>.<字段>`，字段以 `_` 开头
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and isinstance(t.attr, str)
                        and t.attr.startswith("_")
                        and not t.attr.startswith("__")):
                    found.add(t.attr)
    return found


def _collect_reset_fields() -> set[str]:
    """取`reset_request_state` 函数体里登记的字段（pipe.<字段> = ...）。"""
    path = os.path.join(PARTS_DIR, "common.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "reset_request_state":
            out = set()
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Assign)
                        and isinstance(sub.targets[0], ast.Attribute)
                        and isinstance(sub.targets[0].value, ast.Name)
                        and sub.targets[0].value.id == "pipe"):
                    out.add(sub.targets[0].attr)
            return out
    return set()


# ───────────────────────── A1：入口不变量（含位置纪律） ─────────────────────────
def _check_entry_invariance() -> None:
    for fn, entry in (("execute.py", "execute"),
                      ("stream.py", "execute_stream")):
        path = os.path.join(PARTS_DIR, fn)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        fn_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == entry:
                fn_node = node
                break
        if fn_node is None:
            _bad(f"A1 {fn}: 找不到入口方法 {entry}")
            continue

        reset_line = None
        assign_lines: list[tuple[int, str]] = []
        for sub in ast.walk(fn_node):
            if isinstance(sub, ast.Call):
                f_ = sub.func
                if (isinstance(f_, ast.Name) and f_.id == "reset_request_state") or \
                   (isinstance(f_, ast.Attribute) and f_.attr == "reset_request_state"):
                    reset_line = sub.lineno
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if (isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"
                            and t.attr.startswith("_")):
                        assign_lines.append((sub.lineno, t.attr))

        if reset_line is None:
            _bad(f"A1 {fn}: {entry}() 未调用 reset_request_state —— "
                 f"单例下的跨请求泄漏会复现")
            continue
        _ok(f"A1 {fn}: {entry}() 调用了 reset_request_state（第 {reset_line} 行）")

        # 位置纪律：不能晚于任何 self._x 赋值
        if assign_lines:
            earliest_assign = min(assign_lines, key=lambda x: x[0])
            if reset_line > earliest_assign[0]:
                _bad(f"A1 {fn}: reset_request_state 在第 {reset_line} 行，"
                     f"晚于第 {earliest_assign[0]} 行的 `self.{earliest_assign[1]} = `"
                     f" ⇒ 会抹掉本次请求刚设好的上下文")
            else:
                _ok(f"A1 {fn}: 清零早于首个赋值"
                    f"（清零@{reset_line} < 赋值@{earliest_assign[0]} "
                    f"self.{earliest_assign[1]}）")


# ───────────────────────── A3：端到端无泄漏 ─────────────────────────
def _check_no_leak() -> None:
    from agent.pipeline import AgentPipeline
    from agent.pipeline_parts.common import reset_request_state

    code_a = "package 'ReqA' { part def '旧视图零件A'; }"
    pipe = AgentPipeline()
    try:
        reset_request_state(pipe)
        pipe._sysml_last_checked_code = code_a
        pipe._sysml_last_pass_code = code_a

        reset_request_state(pipe)          # ← 模拟第二个请求进来
        out_b = pipe._ensure_sysml_from_tools(
            "已生成状态机视图，包含 3 个状态与 4 条 transition。")

        if code_a in out_b:
            _bad("A3 单例连跑后，第二个请求交付了第一个请求的 SysML 代码")
        else:
            _ok("A3 单例连跑无泄漏：第二个请求未交付前一个请求的代码")

        # 反向：同一次请求内必须仍然可用（防止"清零过头"把功能改坏）
        reset_request_state(pipe)
        pipe._sysml_last_pass_code = code_a
        same_req = pipe._ensure_sysml_from_tools("本轮正文没有代码。")
        if code_a not in same_req:
            _bad("A3-2 清零过头：同一次请求内 tools→cards 的兜底取回失效了"
                 "（这是功能的必要路径，_sysml_last_pass_code 不能被永久禁用）")
        else:
            _ok("A3-2 同请求内兜底取回仍有效（修复未破坏原功能）")
    except Exception as exc:                # noqa: BLE001
        _bad(f"A3 端到端探针异常：{type(exc).__name__}: {exc}")


def main() -> int:
    print("=" * 72)
    print("verify_request_state_reset — 单例 Pipeline 跨请求状态泄漏门禁")
    print("=" * 72)

    assigned = _collect_assigned_fields()
    reset = _collect_reset_fields()

    print(f"\n扫到请求级瞬态字段 {len(assigned)} 个；"
          f"清零清单登记 {len(reset)} 个\n")

    # A2 差集
    missing = assigned - reset - ALLOWED_UNRESET
    if missing:
        _bad(f"A2 有 {len(missing)} 个 `self._xxx` 赋值字段未在reset_request_state "
             f"中登记（跨请求会残留）：{sorted(missing)}")
    else:
        _ok(f"A2 全部 {len(assigned)} 个 `self._xxx` 字段已登记清零"
            f"（豁免 {len(ALLOWED_UNRESET)} 个刻意保留项）")

    # 反向：登记了但源码里已不存在 ⇒ 清单过期，同样该报
    stale = reset - assigned
    if stale:
        _bad(f"A2 反向检查：清零清单里有 {len(stale)} 个字段源码中已不再赋值"
             f"（清单已过期）：{sorted(stale)}")
    else:
        _ok("A2 反向检查：清零清单无过期条目")

    # 关键字段必须在清单里（点名，防漏）
    for must in ("_sysml_last_checked_code", "_sysml_last_pass_code",
                 "_skill_body_offloads", "_tool_conv_ctx", "_tool_user"):
        if must in reset:
            _ok(f"A2 关键字段在清零清单中：{must}")
        else:
            _bad(f"A2 关键字段缺失：{must}（这是本轮实测泄漏的字段之一）")

    print()
    _check_entry_invariance()
    print()
    _check_no_leak()

    print("\n" + "=" * 72)
    print(f"结果：{len(_passes)} PASS / {len(_fails)} FAIL")
    if _fails:
        for m in _fails:
            print(f"  - {m}")
        print("FAIL")
        return 1
    print("✅ 门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
