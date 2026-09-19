# -*- coding: utf-8 -*-
"""SysML 视图投影「结构证据兜底」不变式验证（零 LLM、零库写入，可反复跑）。

**为什么需要它**（2026-09-19，会话 369 实测）：
    用户输入「先分析电动汽车热管理系统需求，再生成 SysML V2 模型代码并进行校验」——
    一次典型的**架构建模**任务，交付代码里 20 个 `part def` / 3 个 `interface def` /
    27 个 `connect`，但前端只投影出 `REQ` / `TRACE` 两个视图，**一个结构视图都没出**。
    根因：`CardMixin._infer_view_types` 是**纯关键词**匹配，该输入只命中「需求」→
    结构类关键词（结构/组成/架构/部件/模块/子系统）**一个都没命中**；
    而「识别不到视角 → 兜底 BDD/IBD」这条兜底只在**完全没命中**时才触发 →
    **部分命中（REQ）就把结构视图的兜底一起关掉了**。
    关键词漏判不该让结构视图消失 —— 投影应服从**模型里实际存在的东西**。

本脚本锁定的不变式：
    I1 design 意图下，**代码里确有结构元素**时，视图集合必须含结构类视图（BDD/IBD）
    I2 反向：代码里**没有**结构元素时，**不得**凭空补结构视图（防退化成无条件兜底）
    I3 用户**显式指定**视图时，代码兜底不得覆盖用户诉求
    I4 既有回归不变（impact → None；无代码 → None）

**变异自证（强制）**：每组不变式同时对「旧写法 / 错误写法」跑一遍，**必须失败**。
    按工程纪律（skill §6.2）：断言"跑通了"证明不了它有效，能抓住旧写法才算数。
    本脚本内置 4 组变异，任一变异未被抓住 → 判为 VACUOUS（空转）并 rc=1。

用法（**裸跑自身即完整口径，无需额外参数**）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_view_projection.py
退出码：全绿 0 / 有失败或空转 1。
"""
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

CARDS = os.path.join(ROOT, "agent", "pipeline_parts", "cards.py")
E2E = os.path.join(ROOT, "tools", "verify", "verify_orchestration_e2e.py")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(status, tag, detail=""):
    _results.append((status, tag, detail))
    print("  %-7s %s%s" % (status, tag, ("  | " + detail) if detail else ""))


# ── 输入保真 ────────────────────────────────────────────────────────────────
def _e2e_message():
    """从 E2E 脚本取 DEFAULT_MESSAGE，保证本脚本用的是**同一条**输入。

    防的是「E2E 改了输入、本脚本没跟」这种静默漂移 —— 那会让本脚本验证的
    已经是另一条输入，绿也说明不了线上那条。
    """
    try:
        src = open(E2E, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(r'^DEFAULT_MESSAGE\s*=\s*"([^"]*)"', src, re.M)
    return m.group(1) if m else None


# ── 加载（可变异）的 cards 模块 ─────────────────────────────────────────────
SRC = open(CARDS, encoding="utf-8").read().replace("\r\n", "\n")
_load_seq = [0]


def _load(src, tag="cur"):
    """把（可能已变异的）cards.py 源码载入独立模块并返回 CardMixin 实例。

    `__package__` 必须指向真实包，否则模块顶部的 `from .common import *` 无法解析；
    另跳过 `_check_generated_sysml`（它要起 checker.jar，与本脚本验证的投影逻辑无关，
    校验挂载由 verify_sysml_check_gate.py 专测）。
    """
    _load_seq[0] += 1
    name = "cards_variant_%s_%d" % (tag, _load_seq[0])
    mod = types.ModuleType(name)
    mod.__package__ = "agent.pipeline_parts"
    mod.__file__ = CARDS
    sys.modules[name] = mod
    exec(compile(src, CARDS, "exec"), mod.__dict__)
    cls = mod.CardMixin
    cls._check_generated_sysml = staticmethod(lambda code: None)
    return cls()


def _mutate(src, old, new, tag):
    """源码级变异；锚点未命中即报错（防「变异静默没生效」导致自证空转）。"""
    if old not in src:
        raise RuntimeError("变异锚点未命中：%s" % tag)
    out = src.replace(old, new, 1)
    if out == src:
        raise RuntimeError("变异未生效：%s" % tag)
    return out


# ── 测试素材 ────────────────────────────────────────────────────────────────
STRUCT = """```sysml
package EV_TMS_Architecture {
    part def 电池冷却回路;
    part def 电机冷却回路;
    interface def 冷却液接口;
    port def 冷却液端口;
    connect 电池冷却回路 to 电机冷却回路;
}
```"""

ONLY_REQ = """```sysml
package Req {
    requirement def REQ1;
    requirement def REQ2;
}
```"""


def _views(inst, content, intent, ui):
    r = inst._gen_sysml_views(content, intent, ui)
    if r is None:
        return None
    return set((r.get("views") or {}).keys())


def _cases(msg):
    """(标签, LLM 内容, intent, user_input, 期望视图集合|None)"""
    return [
        ("c1 建模任务+结构代码 → 必出结构视图", STRUCT, "design", msg, {"REQ", "TRACE", "BDD", "IBD"}),
        ("c2 建模任务+仅需求代码 → 不补结构视图", ONLY_REQ, "design", msg, {"REQ", "TRACE"}),
        ("c3 显式指定视图 → 代码兜底不覆盖", STRUCT, "design", "只看需求图", {"REQ"}),
        ("c4 回归：impact → None", STRUCT, "impact", "影响分析", None),
        ("c5 回归：无代码 → None", "这是一段普通回答。", "design", "", None),
    ]


# 变异锚点（源码级，LF 归一化后）
_OLD_ELIF = ('elif (not ({"BDD", "PKG", "IBD"} & set(view_types))\n'
             '                      and self._code_has_structure(code)):')
_STRUCT_RE = ('        return bool(re.search(\n'
              '            r"\\b(?:part|interface|port|item|occurrence|attribute)\\s+def\\b"\n'
              '            r"|\\bconnect\\b|\\ballocate\\b|\\bbind\\b", str(code), re.I))')


def _variants():
    return [
        ("M1 还原旧写法：删掉「部分命中」的结构兜底",
         lambda s: _mutate(s, _OLD_ELIF, "elif False:", "M1"),
         "c1 建模任务+结构代码 → 必出结构视图"),
        ("M2 去掉代码证据条件（退化成无条件兜底）",
         lambda s: _mutate(s, _OLD_ELIF,
                           'elif (not ({"BDD", "PKG", "IBD"} & set(view_types))):', "M2"),
         "c2 建模任务+仅需求代码 → 不补结构视图"),
        ("M3 _code_has_structure 恒真（证据判据失效）",
         lambda s: _mutate(s, _STRUCT_RE, "        return True", "M3"),
         "c2 建模任务+仅需求代码 → 不补结构视图"),
        ("M4 去掉「显式指定优先」（用户诉求被覆盖）",
         lambda s: _mutate(s,
                           "            if user_views:\n                view_types = user_views",
                           "            if False:\n                view_types = user_views", "M4"),
         "c3 显式指定视图 → 代码兜底不覆盖"),
    ]


def _finish():
    n_fail = sum(1 for s, _, _ in _results if s in (FAIL, VACUOUS))
    n_pass = sum(1 for s, _, _ in _results if s == PASS)
    print("\n" + "=" * 90)
    print("口径：裸跑 tools/verify/verify_view_projection.py（无参数；零 LLM、零库写入）")
    print("结果：%d PASS / %d FAIL/VACUOUS  共 %d 项" % (n_pass, n_fail, len(_results)))
    print("=" * 90)
    return 1 if n_fail else 0


def main():
    print("=" * 90)
    print("SysML 视图投影「结构证据兜底」不变式验证")
    print("  repo=%s" % ROOT)
    print("=" * 90)

    msg = _e2e_message()
    print("\n[0] 输入保真：本脚本输入与 E2E 脚本同源")
    if msg:
        _rec(PASS, "取到 verify_orchestration_e2e.py::DEFAULT_MESSAGE", repr(msg))
    else:
        _rec(FAIL, "取到 DEFAULT_MESSAGE", "取不到 → 输入可能已漂移，验证无意义")
        return _finish()

    cases = _cases(msg)

    print("\n[1] 当前实现的行为（正例，全部必须通过）")
    cur = _load(SRC, "cur")
    if not hasattr(cur, "_code_has_structure"):
        _rec(FAIL, "_code_has_structure 存在", "方法缺失")
    n_bad = 0
    for tag, content, intent, ui, want in cases:
        got = _views(cur, content, intent, ui)
        ok = (got == want)
        n_bad += (0 if ok else 1)
        _rec(PASS if ok else FAIL, tag,
             "views=%s%s" % (sorted(got) if got else got,
                             "" if ok else "  期望=%s" % (sorted(want) if want else want)))

    print("\n[2] 变异自证（旧写法/错误写法必须被抓住，否则本组断言=空转）")
    by_tag = {c[0]: c for c in cases}
    for vtag, mut, catch in _variants():
        try:
            vsrc = mut(SRC)
        except Exception as e:                                  # noqa: BLE001
            _rec(FAIL, vtag, "变异失败：%s" % e)
            continue
        vinst = _load(vsrc, "mut")
        c = by_tag[catch]
        got = _views(vinst, c[1], c[2], c[3])
        if got == c[4]:
            _rec(VACUOUS, vtag, "未被抓住：%s 变异后仍返回 %s（与期望一致）"
                 % (catch, sorted(got) if got else got))
        else:
            _rec(PASS, vtag, "已抓住 %s（变异后 %s ≠ 期望 %s）"
                 % (catch, sorted(got) if got else got,
                    sorted(c[4]) if c[4] else c[4]))

    return _finish()


if __name__ == "__main__":
    sys.exit(main())
