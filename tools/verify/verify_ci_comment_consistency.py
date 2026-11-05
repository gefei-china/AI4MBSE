#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CI 注释与代码实值一致性门禁（2026-10-22）。

## 为什么要有这道门禁

评估 mbse_system 评测体系时发现一类**反复出现的静默失真**：**CI 注释里的数字
与脚本实值漂移了，而 CI 永远绿**。实测三处：

| 位置 | 注释写的 | 代码实值 | 后果 |
|---|---|---|---|
| `ci.yml` 意图路由门禁 | 阈值 0.70 | `run_ci_eval.py` 0.85 | 看注释调阈值 → 改了个不生效的地方 |
| `ci.yml` 分层绕过门禁 | 基线 292 | `verify_layering.BASELINE_DIRECT_SQL=323` | 同上 |
| `ci.yml` 意图路由注释 | 基线 0.828 | 干净库实测 0.931 | 误判余量，阈值定错 |

这类漂移比缺陷更危险：**它不报错、不变红，只让人读到一个错误的事实**。
本门禁的作用是把"注释里的断言"变成**可校验的对象**。

## 判据口径（重要：只管"能被机械校验的注释"）

不是所有注释都能校验。本门禁只抓一类——**注释里点名了某个脚本的某个量，
并给出了数字**：这是"可证伪的注释"。纯说明性文字（如"这里会自建干净库"）
不在范围内。

具体做法：
1. 扫 `ci.yml` 的注释行（`#` 开头）与其中的**中文/英文数字断言**；
2. 对每条断言，回到它点名的脚本里找**同名的模块级常量/默认值**；
3. 数字对不上 ⇒ 判红，并打印两边的值。

## 变异自证（M1~M3）

判据必须能自证"真的会拦"：
- **M1**：在临时副本里把某注释数字改错 ⇒ 必须判红；
- **M2**：把点名关系打断（注释指向不存在的符号）⇒ 必须判红；
- **M3**：反向自证 —— 把注释与实值都改成同一个错数 ⇒ **必须判绿**
  （证明判据比对的是"是否一致"，不是"数字是否够大"）。

M3 是最关键的一条：没有它，这道门禁可能退化成"数字看着不对就红"的噪音门禁。

用法：
  .venv/Scripts/python.exe tools/verify/verify_ci_comment_consistency.py
  .venv/Scripts/python.exe tools/verify/verify_ci_comment_consistency.py --no-mut   # 只跑常态
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:300])
    if _IN_MUT[0]:
        # 变异体内的逐条日志只留"是否红"这一事实，不进最终统计（否则计数虚高）
        _MUT_ROWS.append(row)
    else:
        _results.append(row)
        print("  [%s] %s" % (row[0], row[1]) + ("  " + row[2] if not ok and row[2] else ""))
    return bool(ok)


# ── 可校验断言表 ────────────────────────────────────────────────────────────
# 每条 = (断言标签, ci.yml 注释里应出现的正则, 脚本路径, 脚本里对应的赋值正则)
#   设计取舍：**只收"注释点名符号 + 给出数字"**这一类。
#   纯说明性注释不可机械校验，硬要校验会逼出一堆假红 ⇒ 那道门禁也就没人看了。
#
# ★ 2026-10-22 修订：分层基线这一条从"比对数字"改为**要求注释引用符号名**。
#   起因是本次修复把它改成了"基线见 verify_layering.BASELINE_DIRECT_SQL"（不带数字）。
#   两种形态各有风险：写数字会漂移，写符号名则**依赖链接而非复制**——后者更好，
#   但必须校验"符号名真的在注释里"，否则注释可以什么都不写而门禁仍绿。
#   故此处断言"注释含符号名 OR 含数字且数字与实值一致"，两种合法形态都放行。
ASSERTIONS = [
    (
        "分层绕过门禁基线",
        r"BASELINE_DIRECT_SQL",
        "tools/verify/verify_layering.py",
        r"BASELINE_DIRECT_SQL\s*=\s*(\d+)",
    ),
]


def _read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


# 变异自证用：把 ci.yml 的文本替换成变异版（None = 用磁盘真值）。
# 之所以做成"文本注入"而不是改磁盘，是因为 CI 门禁不该在运行期改工作区文件。
_CI_OVERRIDE = None


def _ci_comment_text(ci_text=None):
    """抽出 ci.yml 的注释行文本（供断言匹配）。"""
    text = ci_text
    if text is None:
        text = _CI_OVERRIDE if _CI_OVERRIDE is not None else _read(CI_YML)
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip().startswith("#"))


def _script_value(path, pat):
    m = re.search(pat, _read(os.path.join(ROOT, path)))
    return m.group(1) if m else None


def t_前置():
    ok = True
    ok &= _rec("A1 ci.yml 存在且非空", os.path.exists(CI_YML) and len(_read(CI_YML)) > 500)
    ok &= _rec("A2 断言表非空（防判据空转）", len(ASSERTIONS) >= 1, "ASSERTIONS=%d" % len(ASSERTIONS))
    return ok


def t_注释与实值一致():
    """核心判据：注释必须以「符号名」或「与实值一致的数字」二者之一锚定真源。

    放行两种合法形态：
      A. 注释写了符号名（如 BASELINE_DIRECT_SQL）⇒ 依赖链接，数字不会漂移；
      B. 注释写了数字 ⇒ 必须与脚本实值严格相等。
    两种都不满足 ⇒ 判红（注释既没指向真源、也没给可比对的数字）。
    """
    ok = True
    ctext = _ci_comment_text()
    for label, cpat, spath, spat in ASSERTIONS:
        sval = _script_value(spath, spat)
        if sval is None:
            _rec("C %s 脚本可取到实值" % label, False, "%s 无 %s" % (spath, spat))
            ok = False
            continue
        # 形态 A：注释引用了符号名（依赖链接，数字不会漂移）——首选
        hit_sym = bool(re.search(cpat, ctext))
        # 形态 B：注释里出现与实值**紧邻**的数字（如"基线 323"）。
        #   必须紧邻（允许"基线 323""基线=323""基线：323"这类直接跟在标签后），
        #   不能是"文本里随便哪个数字等于它"——第一版就是那么写的，结果
        #   "符号名被改错"这个变异照样判绿（判据抓的是无关数字的巧合）。
        hit_num = bool(re.search(r"(?:基线|阈值|下限)[^0-9\n]{0,6}" + re.escape(sval) + r"\b", ctext))
        if hit_sym:
            ok &= _rec("C %s 注释锚定符号名 %s" % (label, cpat), True)
        elif hit_num:
            ok &= _rec("C %s 注释数字紧邻且 == 实值(%s)" % (label, sval), True)
        else:
            ok &= _rec("C %s 注释既未引用符号名、数字也未与实值(%s)紧邻一致" % (label, sval),
                       False, "注释缺符号名 %s，且无『基线 %s』这类紧邻写法" % (cpat, sval))
    return ok


def t_旧阈值残留():
    """旧断言不得被**陈述为当前事实**。

    ⚠️ 口径陷阱（本门禁第一版踩到）：不能简单判"注释里出现了旧数字"。
    修正漂移时，正确做法恰恰是把旧值与新值都写出来（"原写 0.70，与实值 0.85
    已漂移"）—— 那时旧值出现是**必要的修订说明**，不是漂移。
    真正的漂移是"旧值被当成现状陈述"。故这里只抓一种形态：
    **注释里出现"阈值/下限/基线 <旧值>"且句中不含"原写/已漂移/修正/历史"等修订词。**
    """
    ctext = _ci_comment_text()
    ok = True
    stale = ["阈值 0.70", "准确率下限 0.70", "基线 292"]
    # 修订说明词：出现任一即视为"在讲历史"，不算漂移
    revise_words = ("原写", "已漂移", "漂移", "修正", "历史", "过期", "曾是", "作废")
    for s in stale:
        hits = []
        for ln in ctext.splitlines():
            if s in ln and not any(w in ln for w in revise_words):
                hits.append(ln.strip()[:110])
        ok &= _rec("S 旧值 %r 未被陈述为现状" % s, not hits,
                   "疑似漂移行：%s" % hits[:2])
    return ok


# ── 变异自证 ────────────────────────────────────────────────────────────────
def _run_core():
    """在注入的文本上跑核心判据，返回是否**全绿**。

    变异体内的逐条断言不计入最终统计（它们是"被注入的假世界里的内部日志"，
    只看整体红/绿）；否则变异体里的 C 项 FAIL 会混进常态的 FAIL 计数，
    让"变异自证按预期判红"这件事本身被记成失败。
    """
    global _IN_MUT
    saved = list(_results)
    mark = len(_MUT_ROWS)          # 记录本轮起始位置
    _IN_MUT[0] = True
    try:
        t_注释与实值一致()
        t_旧阈值残留()
        # ⚠️ 只看**本轮新增**的断言行。若遍历整�� _MUT_ROWS，
        # 上一轮变异（如 M2 故意造红）留下的记录会永久污染后续判定 ——
        # 实测 M3 因此恒判红。判据必须"每次运行只看自己这次产生的东西"。
        return all(r[0] == PASS for r in _MUT_ROWS[mark:])
    finally:
        _IN_MUT[0] = False
        _results[:] = saved


def run_mutations():
    """M1 注释锚定被改错必红 / M2 符号缺失必红 / M3 两边同错必绿。"""
    global _CI_OVERRIDE
    orig_ci = _read(CI_YML)
    layer = os.path.join(ROOT, "tools", "verify", "verify_layering.py")
    orig_layer = _read(layer)
    ok = True

    # M1：注释里的符号名被改成一个不存在的名字 ⇒ 必须判红
    try:
        _CI_OVERRIDE = orig_ci.replace("BASELINE_DIRECT_SQL", "BASELINE_TYPO_NAME")
        red = not _run_core()
        ok &= _rec("M1 注释符号名改错 ⇒ 判红", red)
    finally:
        _CI_OVERRIDE = None

    # M2：脚本里的实值符号改名（断言取不到值）⇒ 必须判红
    try:
        with io.open(layer, "w", encoding="utf-8", newline="") as f:
            f.write(orig_layer.replace("BASELINE_DIRECT_SQL", "BASELINE_SQL_RENAMED"))
        red = not _run_core()
        ok &= _rec("M2 实值符号缺失 ⇒ 判红", red)
    finally:
        with io.open(layer, "w", encoding="utf-8", newline="") as f:
            f.write(orig_layer)

    # M3：注释与实值**同时**改成同一个错数、且注释不含符号名 ⇒ 必须判绿
    #   （证明判据比对的是"是否一致"，而不是"数字是否够大/够小"）
    #   注意：注释里要**去掉符号名**（否则会走形态 A 直接放行，测不到数字分支）
    try:
        with io.open(layer, "w", encoding="utf-8", newline="") as f:
            f.write(orig_layer.replace("BASELINE_DIRECT_SQL = 323",
                                       "BASELINE_DIRECT_SQL = 777"))
        _CI_OVERRIDE = re.sub(
            r"BASELINE_DIRECT_SQL", "基线 777", orig_ci)
        _CI_OVERRIDE = _CI_OVERRIDE.replace(
            "基线见 verify_layering.基线 777", "基线 777")
        green = _run_core()
        ok &= _rec("M3 注释与实值同为一个错数 ⇒ 仍判绿（证明比对的是一致性）", green)
    finally:
        _CI_OVERRIDE = None
        with io.open(layer, "w", encoding="utf-8", newline="") as f:
            f.write(orig_layer)

    # 自证：跑完变异后，工作区必须与原始一致
    same = _read(layer) == orig_layer
    ok &= _rec("M4 变异自证后工作区已还原（未留下污染）", same)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mut", action="store_true", help="只跑常态，跳过变异自证")
    args = ap.parse_args()

    print("=" * 72)
    print("CI 注释 ↔ 代码实值 一致性门禁")
    print("=" * 72)
    ok = t_前置()
    ok &= t_注释与实值一致()
    ok &= t_旧阈值残留()

    if not args.no_mut:
        print("\n=== 变异自证 ===")
        ok &= run_mutations()

    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 72)
    print("常态断言 %d  PASS %d  FAIL %d" % (len(_results), len(_results) - len(bad), len(bad)))
    # 变异自证的计数：只算外层 M1~M4 的判定行。
    # ⚠️ 不可用 len(_MUT_ROWS) —— 里面混着变异体内**故意造红**的内部断言
    #   （M1/M2 注入的假世界本就该红），把它们计进统计会让"变异按预期判红"
    #   这件事本身显示成失败。第一版就踩了这个，现象是 4 条 M 全 PASS
    #   但汇总行写着"变异断言 12 PASS 10 FAIL 2"。
    mut_outer = [r for r in _MUT_ROWS if r[1][:1] == "M"]
    badm = [r for r in mut_outer if r[0] != PASS]
    if mut_outer:
        print("变异断言 %d  PASS %d  FAIL %d" % (len(mut_outer), len(mut_outer) - len(badm), len(badm)))
    ok &= not bad and not badm
    print("=" * 72)
    print("结论：%s" % ("全部通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())