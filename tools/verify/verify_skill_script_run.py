# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""门禁：技能自带脚本**真的可执行**（P1a，2026-11-07）。

## 这一轮修的到底是什么

7 个已发布技能声明了 `scripts/main.py` / `dump_tools.py` / `lookup.py`，文件都在，
但 `skills.py:355` 只往上下文注入一行「⚙ 脚本（需要时执行）：scripts/main.py」——
**没有任何执行入口**。⇒ 「技能自己带逻辑」此前是假的：技能=一段 Markdown，
脚本=磁盘上的死文件。

## 判据（六段，覆盖"能跑"与"不会变成后门"）

| # | 断言 | 防的失效 |
|---|---|---|
| A1 | `skill_script_run` 在三处登记齐（BUILTIN_DESC / workflows.BUILTINS / 两条分发） | 历史上 save_artifact 漏 BUILTIN_DESC ⇒ 绑了也不注入 |
| A2 | tools 表有行且 `side_effect='write'` | 标 read ⇒ 绕过写前确认闸（脚本能写文件/发 HTTP） |
| A3 | **不全量绑定**（agent_tools 里 0 条） | 全量绑 = 给每个 Agent 发一把"跑任意受信脚本"的钥匙 |
| B1 | 真实执行：`lookup.py --list` 输出 71 个工具且 ok=True | 只做了接线、没真跑 |
| B2 | **SystemExit(0) 不得判失败**（脚本用 `sys.exit(main())`，正常返回 0） | "把成功判成失败" —— 实测踩过 |
| B3 | 带参数 `run(**kwargs)` 模式可用 | 只支持无参脚本 |
| C1 | 路径穿越被拒（`../../config.py`） | 技能名成了读任意文件的通行证 |
| C2 | 未在 manifest 声明的脚本被拒 | 同上 |
| C3 | 非 .py 被拒 | 跑 bat/exe |
| C4 | 缺参时给出**可操作**提示（不是 `SystemExit: 2`） | 模型看到 SystemExit 不知道下一步做什么 |
| D1 | 技能命中且声明了 scripts ⇒ `skill_script_run` 进候选（任务↔工具不放行等于没接） | 注册了但模型看不见（本仓最常见的假绿） |
| D2 | 技能没有 scripts ⇒ 不授权（不无条件放行） | 一刀切放行，等于绕过写意图闸 |
| E1 | 编排侧 ToolExecutor 也能执行 | 两套独立分发只改一处 |

D1 是本文件最重要的断言：**A1-A3 全绿也可能模型根本调不到**。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_skill_script_run.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_res = []


def _rec(name, ok, detail=""):
    _res.append(bool(ok))
    print("  [%s] %s%s" % (PASS if ok else FAIL, name,
                           ("  | " + str(detail)[:220]) if detail else ""))
    return bool(ok)


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def main() -> int:
    print("=" * 72)
    print("门禁 P1a：技能脚本执行通道（skill_script_run）")
    print("=" * 72)

    # ── A 登记完整性 ───────────────────────────────────────────────
    from agent.pipeline_parts.tools import BUILTIN_DESC
    from workflows.tools import ToolRegistry

    _rec("A1a BUILTIN_DESC 已登记（Agent 侧注入事实源）", "skill_script_run" in BUILTIN_DESC)
    _rec("A1b workflows.BUILTINS 已登记（编排侧）",
         "skill_script_run" in ToolRegistry.BUILTINS)
    _agent_py = _read("agent/pipeline_parts/tools.py")
    _orch_py = _read("workflows/tools.py")
    _rec("A1c Agent 侧有执行分发",
         bool(re.search(r"_SKILL_TOOL_NAMES", _agent_py)) and
         "exec_skill_tool" in _agent_py)
    _rec("A1d 编排侧有执行分发",
         bool(re.search(r"_SKILL_TOOL_NAMES", _orch_py)) and "exec_skill_tool" in _orch_py)
    _common_py = _read("agent/pipeline_parts/common.py")
    _rec("A1e common.py 导入并进 __all__（否则 import * 跳过）",
         "exec_skill_tool" in _common_py and "_SKILL_TOOL_NAMES" in _common_py
         and re.search(r"'exec_skill_tool'\s*,\s*'_SKILL_TOOL_NAMES'", _common_py) is not None)

    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(_ROOT, "mbse.db").replace("\\", "/"),
                           uri=True)
    row = conn.execute("SELECT side_effect, status, builtin FROM tools WHERE name=?",
                       ("skill_script_run",)).fetchone()
    _rec("A2 tools 表有行且 side_effect=write（脚本有文件/网络能力）",
         bool(row) and row[0] == "write", row)
    _nb = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_name=?",
                       ("skill_script_run",)).fetchone()[0]
    _rec("A3 不做全量绑定（靠技能白名单注入）", _nb == 0, "bindings=%d" % _nb)
    conn.close()

    # ── B 真实执行 ────────────────────────────────────────────────
    from skill_tools import exec_skill_tool

    r = exec_skill_tool("skill_script_run",
                        {"skill": "智源MCP工具手册", "script": "scripts/lookup.py",
                         "argv": ["--list"], "timeout": 60})
    _out = r.get("result") or ""
    _rec("B1 真实执行 lookup.py --list（ok=True 且输出工具数）",
         r.get("ok") and re.search(r"共\s*(\d+)\s*个工具", _out) is not None,
         _out[:110])

    # B2：脚本用 sys.exit(main())，main() 返回 0 ⇒ 抛 SystemExit(0)。
    #   绝不能因此判失败（实测踩过：明明成功了却报 ok=false）。
    _rec("B2 SystemExit(0) 视为成功（不是失败）", r.get("ok") is True,
         "ok=%s" % r.get("ok"))

    r2 = exec_skill_tool("skill_script_run",
                         {"skill": "文件操作", "script": "scripts/main.py",
                          "args": {"path": "tmp/__gate_probe.txt", "content": "hi",
                                   "mode": "write"}, "timeout": 30})
    _r2 = r2.get("result") or ""
    _rec("B3 run(**kwargs) 带参模式可用（写文件脚本返回回执）",
         ("__gate_probe" in _r2) or ("已write" in _r2) or ("已 write" in _r2),
         _r2[:150])
    try:                                     # 清掉探针产物
        os.remove(os.path.join(_ROOT, "tmp", "__gate_probe.txt"))
    except OSError:
        pass

    # ── C 安全边界 ────────────────────────────────────────────────
    _c1 = exec_skill_tool("skill_script_run",
                          {"skill": "智源平台管理", "script": "scripts/../../config.py"})
    _rec("C1 路径穿越被拒", not _c1.get("ok"), _c1.get("result", "")[:100])

    _c2 = exec_skill_tool("skill_script_run",
                          {"skill": "智源平台管理", "script": "scripts/other.py"})
    _rec("C2 未在 manifest 声明的脚本被拒", not _c2.get("ok"),
         _c2.get("result", "")[:100])

    _c3 = exec_skill_tool("skill_script_run",
                          {"skill": "智源平台管理", "script": "scripts/main.txt"})
    _rec("C3 非 .py 被拒", not _c3.get("ok"), _c3.get("result", "")[:100])

    _c4 = exec_skill_tool("skill_script_run",
                          {"skill": "智源MCP工具手册", "script": "scripts/lookup.py",
                           "timeout": 25})
    _r4 = _c4.get("result") or ""
    _rec("C4 缺参给出可操作提示（含 argv 用法，而非裸 SystemExit）",
         (not _c4.get("ok")) and "argv" in _r4 and "SystemExit: 2" not in _r4,
         _r4[:130])

    # ── D 授权链路（最容易断的一环）────────────────────────────────
    from agent.pipeline_parts.skills import SkillMixin
    from agent.registry import AgentRegistry

    # ⚠️ 第一版只继承 SkillMixin ⇒ `self.registry` 不存在 ⇒ `_build_skill_prompt`
    #   在 skills.py:185 抛 AttributeError，被 except 吞掉 ⇒ D1 红、**D2 假绿**
    #   （异常路径本来就不会授权）。必须给真 registry：判据要穿透真实代码路径。
    class _P(SkillMixin):
        def __init__(self):
            self.registry = AgentRegistry()

    def _mk():
        p = _P()
        p._skill_allowed_tools = None
        p._skill_prompt_cache = None
        p._skill_body_offloads = []
        p._last_skill_hits = []
        return p

    p = _mk()
    _err = ""
    try:
        p._build_skill_prompt("智源平台管理", "用智源平台管理技能自检一下连通性", None)
    except Exception as e:
        _err = "%s: %s" % (type(e).__name__, str(e)[:100])
    _grant = getattr(p, "_skill_allowed_tools", None) or set()
    _rec("D1 技能声明了 scripts ⇒ 自动授权 skill_script_run（进候选的前提）",
         "skill_script_run" in set(_grant), (_err or sorted(set(_grant))[:6]))

    # 候选里真的出现？（读 tools.py:582 那条补入路径的代码形态 + 直接跑一次候选构建）
    _tools_py = _read("agent/pipeline_parts/tools.py")
    _rec("D1b tools.py 有「skill_script_run 补入候选」分支",
         re.search(r'if\s+"skill_script_run"\s+in\s+_sk_wl', _tools_py) is not None)
    _skills_py = _read("agent/pipeline_parts/skills.py")
    _rec("D1c 注入文案写明工具名与参数（不是只给文件名）",
         "skill_script_run" in _skills_py and "看不到就假装" not in _skills_py
         and ("⚙ 脚本（用 `skill_script_run`" in _skills_py))

    # D2：没有 scripts 的技能不得授权
    p2 = _mk()
    _err2 = ""
    try:
        p2._build_skill_prompt("智能体平台运维", "运维巡检", None)
    except Exception as e:
        _err2 = "%s: %s" % (type(e).__name__, str(e)[:100])
    _g2 = getattr(p2, "_skill_allowed_tools", None) or set()
    # 判据要**同时**要求"没有异常"——否则异常路径会假绿（第一版就这么绿的）
    _rec("D2 未声明 scripts 的技能不授权（不无条件放行）",
         (not _err2) and ("skill_script_run" not in set(_g2)),
         _err2 or sorted(set(_g2))[:6])

    # ── E 编排侧 ──────────────────────────────────────────────────
    from workflows.tools import ToolExecutor as _TE
    try:
        ex = _TE()
        rr = ex.execute("skill_script_run",
                        {"skill": "智源MCP工具手册", "script": "scripts/lookup.py",
                         "argv": ["--list"]}, branch="dev")
        _rec("E1 编排侧 ToolExecutor 可执行", bool(rr.get("ok")),
             str(rr.get("result", ""))[:110])
    except Exception as e:
        _rec("E1 编排侧 ToolExecutor 可执行", False, "%s: %s" % (type(e).__name__, e))

    ok = all(_res)
    print("\n" + "=" * 72 + "  ALL_OK=%s  (%d passed / %d failed)"
          % (ok, sum(1 for x in _res if x), sum(1 for x in _res if not x)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
