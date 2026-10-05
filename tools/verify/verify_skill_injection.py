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

_DB = os.environ.get("MBSE_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")
_c = sqlite3.connect(_DB)
_c.row_factory = sqlite3.Row
# ⚠️ 2026-10-04：原为 `sqlite3.connect("mbse.db")`（**相对路径硬编码**）——
#   ⇒ CI 用 MBSE_DB_PATH 指向干净库时，本脚本**仍在读仓库根目录的生产库**，
#   于是「本地绿 / CI 红」：干净库里没有"声明了白名单的已发布技能"，
#   B1 组断言全部落空（实测 got=[] exp=[4个工具]）。
#   正解：读 MBSE_DB_PATH（与全仓其它门禁一致）。
#   教训与 MEMORY 里「测试切库必须改真正生效的那个源」同型 ——
#   **这里生效的源是 sqlite3.connect 的参数，不是 core.config.DB_PATH。**
_WL_SKILL = _c.execute("SELECT name FROM skills WHERE COALESCE(allowed_tools,'') "
                       "NOT IN ('','[]','null') AND status='published' LIMIT 1").fetchone()
_NO_WL_SKILL = _c.execute("SELECT name FROM skills WHERE COALESCE(allowed_tools,'') "
                          "IN ('','[]','null') AND status='published' LIMIT 1").fetchone()
# 干净库里没有「已发布且带白名单」的技能 ⇒ 这不是缺陷，是**样本缺失**。
# ⚠️ 纪律（MEMORY）：**数据不足报unknown，不许报 FAIL** ——
#   否则 CI 每次都会红，而红的原因不是代码坏了，是库还没被灌数据。
#   与「该门禁本地绿 / CI 红」的实测教训一致。
_SAMPLES = bool(_WL_SKILL) and bool(_NO_WL_SKILL)
print("[库] %s（带白名单技能样本=%s / 未带样本=%s）"
      % (os.path.basename(_DB), bool(_WL_SKILL), bool(_NO_WL_SKILL)))
# ⚠️ 纪律（MEMORY）：**数据不足报 unknown，不许报 FAIL**。
#   原实现在这里 `check("0.2 …", bool(_WL_SKILL))` —— 于是 CI 干净库
#   （init_db 只灌结构、不含"已发布且带白名单"的技能）**每次必红**，
#   而红的原因不是代码坏了，是库还没灌数据。
#   改成：`SKIP`（不计失败、不计入断言总数），并在输出里显式说明。
if _SAMPLES:
    check("0.2a 样本可用（带白名单 / 未带白名单的已发布技能各一）", True)
else:
    print("  [SKIP] 0.2a 样本不可用（本库无「已发布且带白名单」或「未带白名单」的技能）")
    print("         ⇒ B 组中依赖样本的断言不适用。这是**样本缺失**，不是缺陷。")
    print("         ⇒ 需要它们生效请在开发机灌数据后跑；CI 干净库下不阻塞。")
if not _WL_SKILL:
    print("  ⚠️ 本库无「带白名单的已发布技能」⇒ B1/B5 组不适用（样本缺失，非缺陷）")
if not _NO_WL_SKILL:
    print("  ⚠️ 本库无「未带白名单的已发布技能」⇒ B7 组不适用（样本缺失，非缺陷）")

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
    # ⚠️ 分两种「进不来」的原因，**不能一律判红**：
    #   ① `_READY=False` ⇒ **能力真的没落地**（import 失败）⇒ 必须判红
    #   ② `_READY=True` 但 `_WL_SKILL is None` ⇒ 只是**本库缺这类样本**
    #      （CI 干净库 init_db 只灌结构，不含「已发布且带白名单」的技能）
    #      ⇒ 判红等于「每次 CI 都红，而红的原因不是代码坏了」
    #   原实现两者都 `check(n, False)` ⇒ CI 必红（实测 got=[] exp=[6个工具]）。
    _B_NAMES = ("B1 白名单 == 库里声明", "B2 块头为该技能", "B3 正文截断到 4000",
                "B4 披露资源清单", "B5 白名单行出现在块里",
                "B6 include_resources=False 时不披露")
    if not _READY:
        for n in _B_NAMES:
            check(n, False, "目标能力未落地（import 失败）")
    else:
        for n in _B_NAMES:
            print("  [SKIP] %s（本库无「已发布且带白名单」的技能 ⇒ 样本缺失）" % n)

if _READY and _NO_WL_SKILL:
    _b, _a = load_forced_skill(_NO_WL_SKILL["name"], content_cap=4000, include_resources=True)
    check("B7 未声明白名单的技能 → 空集（调用方据此保持 None=不限制）", not _a, _a)
elif not _READY:
    check("B7 未声明白名单的技能 → 空集", False, "能力未落地")
else:
    print("  [SKIP] B7 未声明白名单的技能 → 空集（本库无「未带白名单」的已发布技能）")

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
