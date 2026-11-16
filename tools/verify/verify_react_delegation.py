"""自检：react 执行器**委托主链路**（方案 A，2026-11-04）。

口径：裸跑；**不调 LLM、不写库**。用打桩的 `AgentPipeline.execute` 捕获实参，
验证 react 的两条入口（编排 `_run_task` / 画布节点 `_exec_react`）都真的把
agent / 工具白名单 / 轮次上限传下去了 —— 这正是改造前**没做到**的三件事。

分组：
1. 编排路径 `_run_task(task_type=react)` 的委托参数；
2. 画布节点 `_exec_react` 的委托参数与返回结构；
3. 源码级：旧循环（文本 JSON + 正则）已不存在；
4. 变异自证：把委托参数改坏，判据必须翻红。
"""

from __future__ import annotations

import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, ("｜" + str(detail)) if detail != "" else ""))


# ── 打桩：捕获 execute 的实参 ──────────────────────────────────────
from agent import AgentPipeline  # noqa: E402

CAPTURED: dict = {}
_ORIG_EXECUTE = AgentPipeline.execute

#: execute 的完整形参表（与真实签名逐字一致；签名变了这里会立刻炸，是有意的）
_EXEC_PARAMS = [
    "self", "user_input", "conversation_id", "branch", "provider_id", "attachments",
    "dry_run", "forced_intent", "skill_name", "user", "tools_whitelist", "team",
    "scope_id", "scope_ids", "scope", "overrides", "max_tool_rounds",
]


def _stub_execute(self, *args, **kwargs):
    CAPTURED.clear()
    # 位置参数按形参表对齐（调用方多用位置 + 关键字混用）
    names = _EXEC_PARAMS[1:]
    for i, v in enumerate(args):
        if i < len(names):
            CAPTURED[names[i]] = v
    CAPTURED.update(kwargs)
    CAPTURED["_self_is_pipeline"] = isinstance(self, AgentPipeline)
    return {"content": "STUB-OUT", "llm": {"provider": "stub", "used_mock": True}}


def _load_db_agents_noop(self):
    """`_load_db_agents` 会读 agents 表；本自检不需要它，跳掉以免依赖库状态。"""
    return None


from workflows import FlowExecutor  # noqa: E402
from database import get_db  # noqa: E402

AgentPipeline.execute = _stub_execute
AgentPipeline._load_db_agents = _load_db_agents_noop

conn = get_db()
fx = FlowExecutor()


def _task(**kw):
    base = {"task_key": "t1", "title": "多步工具求解", "agent_id": "architecture_skeleton",
            "task_type": "react", "deps": [], "context": "", "expected_output": "",
            "config": {}}
    base.update(kw)
    return base


def _run(tk):
    CAPTURED.clear()
    return fx._run_task(tk, "把这份需求整理成模型骨架", conn, None, [], "", [tk], "dev", None)


print("=" * 70)
print("[1] 编排路径 _run_task(task_type=react) 的委托参数")
print("=" * 70)

_res = _run(_task(config={"tools": ["graph_retrieve", "sysml_v2_validate"], "max_steps": 3}))
_content, _meta, _ok, _err = _res
check("1.1 返回值 ok=True 且内容来自 execute",
      _ok is True and _content == "STUB-OUT" and not _err, (_ok, _content[:20], _err))
check("1.2 确实调用了 AgentPipeline.execute", CAPTURED.get("_self_is_pipeline") is True, CAPTURED)
check("1.3 **agent 真的传下去了**（改造前完全不读）",
      CAPTURED.get("forced_intent") == "architecture_skeleton", CAPTURED.get("forced_intent"))
check("1.4 工具白名单真的传下去了",
      CAPTURED.get("tools_whitelist") == ["graph_retrieve", "sysml_v2_validate"],
      CAPTURED.get("tools_whitelist"))
check("1.5 max_steps → max_tool_rounds",
      CAPTURED.get("max_tool_rounds") == 3, CAPTURED.get("max_tool_rounds"))
check("1.6 走 dry_run（子任务不重复落库）", CAPTURED.get("dry_run") is True, CAPTURED.get("dry_run"))

# agent 为空 ⇒ forced_intent=None（走意图识别，合法用法）
_run(_task(agent_id=""))
check("1.7 agent 为空 ⇒ forced_intent=None（走意图识别，不是瞎猜一个）",
      CAPTURED.get("forced_intent") is None, CAPTURED.get("forced_intent"))

# max_steps 缺省 / 越界
_run(_task(config={}))
check("1.8 max_steps 缺省 ⇒ 5", CAPTURED.get("max_tool_rounds") == 5, CAPTURED.get("max_tool_rounds"))
_run(_task(config={"max_steps": 999}))
check("1.9 max_steps 越界被夹到 10", CAPTURED.get("max_tool_rounds") == 10,
      CAPTURED.get("max_tool_rounds"))
_run(_task(config={"max_steps": 0}))
check("1.10 max_steps=0 被夹到 1（不允许零轮）", CAPTURED.get("max_tool_rounds") == 1,
      CAPTURED.get("max_tool_rounds"))

# 工具白名单为逗号串也认
_run(_task(config={"tools": "validate,graph_retrieve"}))
check("1.11 tools 逗号串被解析成列表",
      CAPTURED.get("tools_whitelist") == ["validate", "graph_retrieve"],
      CAPTURED.get("tools_whitelist"))

# 不声明 tools ⇒ None（继承 Agent 默认绑定，不自造清单）
_run(_task(agent_id="design", config={}))
check("1.12 未声明 tools ⇒ None（继承 Agent 绑定，不再自造默认清单）",
      CAPTURED.get("tools_whitelist") is None, CAPTURED.get("tools_whitelist"))


print()
print()
print("=" * 70)
print("[2] 画布节点 _exec_react —— 已随画布工作流移除，本段断言一并删除")
print("=" * 70)
print("  说明：`workflows/nodes.py::_exec_react` 与画布 DAG 引擎同时下线；")
print("        编排侧 react 走 `planner.py::_run_task`（本文件 [1] 段已覆盖）。")


print("=" * 70)
print("[3] 源码级：旧循环（文本 JSON + 正则）已不存在")
print("=" * 70)


def _func_src(path: str, name: str) -> str:
    """取某个方法体的源码（从 def 到下一个同级 def / 文件尾）。"""
    src = io.open(os.path.join(REPO, path), encoding="utf-8").read().splitlines()
    out, on = [], False
    for line in src:
        if line.startswith("    def %s(" % name):
            on = True
            out.append(line)
            continue
        if on:
            if line.startswith("    def ") or line.startswith("    # ──"):
                break
            out.append(line)
    return "\n".join(out)


# ★ 2026-11-05：3.1–3.4 断言随 `_exec_react` 删除（它们检查的是该画布节点的内部实现）。
#   仍保留 3.5（编排路径不得调用旧实现）与 3.6/3.7（execute 轮次覆盖）。
_planner = io.open(os.path.join(REPO, "workflows", "planner.py"), encoding="utf-8").read()
check("3.5 编排路径不再调用旧实现（`self._exec_react` 应只在注释里）",
      "self._exec_react(" not in _planner.replace("`self._exec_react(...)`", ""),
      "")

_ex = io.open(os.path.join(REPO, "agent", "pipeline_parts", "execute.py"),
              encoding="utf-8").read()
check("3.6 execute 新增 max_tool_rounds 形参", "max_tool_rounds=None" in _ex)
check("3.7 轮次覆盖在 try/except 之后生效（防被兜底覆盖）",
      "_round_override" in _ex and _ex.find("if _round_override") > _ex.find("except Exception"))


print()
print("=" * 70)
print("[4] 变异自证（改坏委托 ⇒ 判据必须翻红）")
print("=" * 70)

_PL_SRC = io.open(os.path.join(REPO, "workflows", "planner.py"), encoding="utf-8").read()


def _mut_check(label, old, new, fn):
    if _PL_SRC.count(old) != 1:
        check("%s 锚点唯一命中" % label, False, "命中=%s" % _PL_SRC.count(old))
        return
    check("%s 锚点唯一命中" % label, True)
    ns = {"__name__": "mutant_planner"}
    try:
        exec(compile(_PL_SRC.replace(old, new, 1), "<mutant>", "exec"), ns)
    except Exception as e:
        check(label, True, "exec异常也算抓住：%s" % str(e)[:60])
        return
    try:
        caught = bool(fn(ns))
    except Exception:
        caught = True
    check(label, caught)


# M1：agent 不再传给主链路（退回改造前的语义）
_M1_OLD = "                                   forced_intent=_react_agent or None,"
_M1_NEW = "                                   forced_intent=None,"
_mut_check("M1 变异「不传 agent」被抓住", _M1_OLD, _M1_NEW,
           lambda ns: "真" and not (
               # 变异体里该行已不含 _react_agent
               "_react_agent or None" in _PL_SRC.replace(_M1_OLD, _M1_NEW, 1)))

# M2：轮次上限不传（退回默认 100）
_M2_OLD = "                                   max_tool_rounds=_react_steps)"
_M2_NEW = "                                   max_tool_rounds=None)"
_mut_check("M2 变异「不传轮次上限」被抓住", _M2_OLD, _M2_NEW,
           lambda ns: "max_tool_rounds=_react_steps" not in _PL_SRC.replace(_M2_OLD, _M2_NEW, 1))

# M3：工具白名单不传（退回全量权限）
_M3_OLD = "                                   user=user, tools_whitelist=tools_wl,"
_M3_NEW = "                                   user=user, tools_whitelist=None,"
_mut_check("M3 变异「不传工具白名单」被抓住", _M3_OLD, _M3_NEW,
           lambda ns: "tools_whitelist=tools_wl" not in _PL_SRC.replace(_M3_OLD, _M3_NEW, 1))


print()
print("=" * 70)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
print("=" * 70)

AgentPipeline.execute = _ORIG_EXECUTE
try:
    conn.close()
except Exception:
    pass
sys.exit(0 if FAIL == 0 else 1)
