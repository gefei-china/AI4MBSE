# -*- coding: utf-8 -*-
"""P1-29 自检：显式指定技能（`skill_name`）的注入在**两条路径上一致**。

## 要防的三件事（均已在 §30.6 / §31 登记，本脚本把"是否存在"变成"可重复断言"）

1. **`_skill_forced` 只在 `execute.py` 设置** ⇒ 对话主路径（`stream.py`）上该机制**未接线**，
   显式指定技能的 `allowed_tools` 授权可能不生效（`skills.py` 会按"未强制"重置并只并集
   *自动命中* 技能的白名单）。
2. **`stream.py` 的技能 SQL 只查 3 列**（`name/content/description`）⇒ references/scripts
   拿不到，渐进披露在对话路径上**无数据可用**。
3. **`_skill_allowed_tools` 无每轮起点清零** ⇒ 实例被复用时（`routers/conversations.py:14`
   用的是**全局单例** `from agent import agent`）会**跨轮/跨路径残留**。

## 判据分两层

- **行为级**（A/B 组）：直接调共用函数，用**真实库里的技能**（不构造假数据）。
- **源码级**（C 组）：断言两条路径都走同一入口、且不再各自读库 —— 防"函数抽了但不接线"。

## 污染

只读库；不调 LLM；不写任何表。
"""
import io
import os
import re
import sqlite3
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── 待实现的目标能力（缺失即判红：本脚本测的是"目标状态"）──
try:
    from agent.pipeline_parts.common import (          # noqa: E402
        load_forced_skill, reset_skill_state,
    )
    _READY = True
    _IMPORT_ERR = ""
except ImportError as e:
    _READY = False
    _IMPORT_ERR = "%s: %s" % (type(e).__name__, e)
    load_forced_skill = reset_skill_state = None

print("== 0 前置：目标能力是否已落地 ==")
check("0.1 common 暴露 reset_skill_state / load_forced_skill", _READY, _IMPORT_ERR)

_c = sqlite3.connect("mbse.db")
_c.row_factory = sqlite3.Row
_WL_SKILL = _c.execute("SELECT name FROM skills WHERE COALESCE(allowed_tools,'') "
                       "NOT IN ('','[]','null') AND status='published' LIMIT 1").fetchone()
_NO_WL_SKILL = _c.execute("SELECT name FROM skills WHERE COALESCE(allowed_tools,'') "
                          "IN ('','[]','null') AND status='published' LIMIT 1").fetchone()
check("0.2 库里存在「声明了白名单」的已发布技能（真实数据，不构造）", bool(_WL_SKILL),
      _WL_SKILL["name"] if _WL_SKILL else "无")
check("0.3 库里存在「未声明白名单」的已发布技能", bool(_NO_WL_SKILL),
      _NO_WL_SKILL["name"] if _NO_WL_SKILL else "无")

print()
print("== A 状态起点：每轮清零（防跨轮/跨路径残留）==")
if _READY:
    class _P:
        pass
    p = _P()
    p._skill_allowed_tools = {"residual_from_last_turn"}     # 模拟上一轮残留
    reset_skill_state(p, "some_skill")
    check("A1 带 skill_name → _skill_forced=True 且白名单清零",
          p._skill_forced is True and p._skill_allowed_tools is None,
          (p._skill_forced, p._skill_allowed_tools))
    p._skill_allowed_tools = {"residual_again"}
    reset_skill_state(p, "")
    check("A2 不带 skill_name → _skill_forced=False 且白名单清零（**残留必须被清**）",
          p._skill_forced is False and p._skill_allowed_tools is None,
          (p._skill_forced, p._skill_allowed_tools))
    check("A3 reset 幂等（重复调用结果一致）",
          (lambda: (reset_skill_state(p, ""), p._skill_allowed_tools)[1])() is None)
else:
    for n in ("A1 带 skill_name → _skill_forced=True 且白名单清零",
              "A2 不带 skill_name → _skill_forced=False 且白名单清零（残留必须被清）",
              "A3 reset 幂等"):
        check(n, False, "目标能力未落地")

print()
print("== B 显式技能注入（共用函数 + 真实库数据）==")
if _READY and _WL_SKILL:
    _name = _WL_SKILL["name"]
    _row = _c.execute("SELECT * FROM skills WHERE name=?", (_name,)).fetchone()
    import json as _json

    def _j(v):
        try:
            return _json.loads(v) if isinstance(v, str) else (v or [])
        except Exception:
            return []
    _exp_wl = {str(t) for t in _j(_row["allowed_tools"]) if str(t)}
    _blk, _at = load_forced_skill(_name, content_cap=4000, include_resources=True)
    check("B1 白名单 == 库里该技能声明的 allowed_tools", _at == _exp_wl,
          "got=%s exp=%s" % (sorted(_at), sorted(_exp_wl)))
    check("B2 块头为该技能（【指定技能：…】）", _blk.startswith("【指定技能：%s" % _name), _blk[:40])
    check("B3 正文被截断到 content_cap（4000）",
          _row["content"][:4000] in _blk or len(_row["content"] or "") <= 4000)
    _has_res = bool(_j(_row["references"])) or bool(_j(_row["scripts"]))
    check("B4 披露资源清单（该技能确有 references/scripts 时必须出现在块里）",
          (not _has_res) or ("📄 参考文档" in _blk or "⚙ 脚本" in _blk),
          "has_res=%s" % _has_res)
    check("B5 白名单行出现在块里（🔒 工具白名单）", "🔒 工具白名单" in _blk)
    _blk2, _at2 = load_forced_skill(_name, content_cap=4000, include_resources=False)
    check("B6 include_resources=False 时不披露资源、也不产出白名单",
          "📄 参考文档" not in _blk2 and "⚙ 脚本" not in _blk2 and not _at2)
else:
    for n in ("B1 白名单 == 库里声明", "B2 块头为该技能", "B3 正文截断到 4000",
              "B4 披露资源清单", "B5 白名单行出现在块里", "B6 include_resources=False 时不披露"):
        check(n, False, "目标能力未落地或无可用技能")

if _READY and _NO_WL_SKILL:
    _b, _a = load_forced_skill(_NO_WL_SKILL["name"], content_cap=4000, include_resources=True)
    check("B7 未声明白名单的技能 → 空集（调用方据此保持 None=不限制）", not _a, _a)
else:
    check("B7 未声明白名单的技能 → 空集", False, "无可用技能")

if _READY:
    _b3, _a3 = load_forced_skill("__不存在的技能__", content_cap=4000, include_resources=True)
    check("B8 技能不存在 → 空块 + 空集，且**不抛**", _b3 == "" and not _a3, (_b3[:20], _a3))
else:
    check("B8 技能不存在 → 空块 + 空集，且不抛", False, "目标能力未落地")
_c.close()

print()
print("== C 源码级：两条路径走同一入口、不再各自读库 ==")
_src = {}
for n, p in (("execute.py", "agent/pipeline_parts/execute.py"),
             ("stream.py", "agent/pipeline_parts/stream.py")):
    _src[n] = io.open(p, encoding="utf-8").read()
for n in ("execute.py", "stream.py"):
    check("C1 %s 调用 reset_skill_state（统一状态起点）" % n, "reset_skill_state(" in _src[n])
    check("C2 %s 调用 load_forced_skill（统一读取入口）" % n, "load_forced_skill(" in _src[n])
check("C3 stream.py 不再内联技能 SQL（数据源已统一）",
      "SELECT name, content, description FROM skills" not in _src["stream.py"])
check("C4 stream.py 不再直接拼 【指定技能 文案（已收敛到 build_skill_block）",
      '【指定技能：{row[' not in _src["stream.py"])
# P1-29 发现的**第四处**路径差异（正文为空的技能是否注入）：两类行为都合理，故按 P1-28 的纪律
# **显式参数化**而不是抹平。这条判据防"顺手统一"把某一侧行为改掉。
check("C5 「空正文技能」的行为差异已显式参数化（execute=False / stream=True）",
      "require_content=False" in _src["execute.py"] and "require_content=True" in _src["stream.py"])

print()
print("== M 变异自证（改源码文本再 exec，判据必须被打破）==")


def _variant(replacements):
    path = os.path.join(_ROOT, "agent/pipeline_parts/common.py")
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    for a, b in replacements:
        if a not in src:
            check("变异锚点命中: %r" % a[:40], False, "锚点未命中 → 变异失效")
            return None
        src = src.replace(a, b, 1)
    mod = types.ModuleType("common_mut")
    exec(compile(src, path + "<mut>", "exec"), mod.__dict__)     # noqa: S102
    return mod


if _READY:
    _M1 = _variant([("pipe._skill_allowed_tools = None", "pass")])
    if _M1:
        _p = types.SimpleNamespace(_skill_allowed_tools={"residual"})
        try:
            _M1.reset_skill_state(_p, "")
            _ok = (_p._skill_allowed_tools is None)
        except Exception:
            _ok = False
        check("M1 去掉白名单清零 → A2 的残留判据被打破", not _ok,
              getattr(_p, "_skill_allowed_tools", "?"))
    # M2 直接模拟"这个缺陷当初的样子"：`_skill_forced` 不被设置（未接线）⇒ A1 必须翻。
    _M2 = _variant([("pipe._skill_forced = bool(skill_name)", "pipe._skill_forced = False")])
    if _M2:
        _p2 = types.SimpleNamespace(_skill_allowed_tools=None)
        _M2.reset_skill_state(_p2, "some_skill")
        check("M2 `_skill_forced` 恒 False（= 缺陷① 未接线的形态）→ A1 判据被打破",
              _p2._skill_forced is not True, _p2._skill_forced)
else:
    check("M1/M2 跳过（目标能力未落地）", True)

print()
print("PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
for f in _FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if _FAIL else 0)
