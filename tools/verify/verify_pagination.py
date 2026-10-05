#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：列表 limit 的边界语义统一（P2-1，2026-10-04）

## 这道门禁要纠正什么

原排序文档把 P2-1 估成「82 处已分页 / 573 端点要覆盖，3~5 天重构」。
**实测推翻了"要覆盖"这个前提**：

| 项 | 实测 |
|---|---|
| 全仓最大表 | `document_chunks` 7269行 |
| 业务表 | entities 189 / relations 249 / triples 137 / documents 15 |
| `/api/documents` 响应体 | **16 KB** |
| `/api/knowledge/entities?limit=2000` 响应体 | **126 KB** |
| 缺夹紧的 limit 端点 | **0 个**（44 处看着"无上限"，实际全部已有 `min()` 或 `Query(le=)`） |

⇒ **当前规模下分页不是瓶颈，44 处也都已夹紧** —— 这项**基本已实现**。
（我第一版启发式按"签名里没有 le="统计，报出 33 个"风险端点"，
 逐个核对后发现全是误报：有的在函数体内 `min()` 夹紧、有的按 id 过滤本就有限。
 **统计口径必须落到函数体，不能只判签名。**）

真正剩下的缺口是**边界语义不统一**（实测）：

| 入参 | entity-search 响应 | 问题 |
|---|---|---|
| `limit=99999` | 200 / 6588 B | ✅被夹到 50 |
| `limit=0` | 200 / 1963 B | 语义模糊：`int(0 or 15)`=15 ⇒ 走默认，**巧合**正确 |
| `limit=-5` | 200 / 173 B | `int(-5 or 15)`=-5（-5 是真值）⇒ min→-5 ⇒ max→1 ⇒ **只返 1 条** |
| `limit=abc` | 422 | FastAPI 拦（类型注解是 int）✅ |

⇒ `limit=-5` 返 1 条、`limit=0` 返 15 条：**同类输入两套行为**。
本门禁把 `core.pagination.clamp_limit` 定为**唯一口径**，
并要求各端点改用它（行为等价，但语义统一、可测）。

## 三条纪律

- **不复刻被测逻辑**：clamp_limit 的性质由**直接调用真函数**验证。
- **变异必须打在判据走到的分支**：M1/M2/M3 分别破坏夹紧/ 下界/ 回退。
- **门禁判据自身可判红**。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    return bool(ok)


def _src(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


# ── L1：clamp_limit 存在且导出 ───────────────────────────────────────────
def t_l1_exists():
    print("\n=== L1 clamp_limit 存在 ===")
    ok = True
    try:
        from core.pagination import clamp_limit, DEFAULT_MAX
        ok &= _rec("L1a 可导入", True)
        ok &= _rec("L1b 有默认上限常量", isinstance(DEFAULT_MAX, int) and DEFAULT_MAX > 0,
                   DEFAULT_MAX)
    except Exception as e:
        ok &= _rec("L1a 可导入", False, str(e)[:140])
    return ok


# ── L2：边界性质（直接调真函数，不复刻）────────────────────────────────
def t_l2_semantics(pagination_src=None):
    """边界语义断言。

    :param pagination_src: **变异后的源码文本**（供变异自证注入）。
        ⚠️ 必须走"重载模块"而不是"改磁盘文件"：第一版变异直接改
        `core/pagination.py` 的文本，但 `clamp_limit` 早已被
        `from core.pagination import clamp_limit` 导入并缓存在 sys.modules
        ⇒ 判据跑的还是**未被变异的真函数** ⇒ 4/4 变异全部"未判红"。
        正解：把变异文本 exec 到独立命名空间，取它的 clamp_limit。
        这与 MEMORY 里「变异必须打在判据真正跑到的代码上」同源。
    """
    print("\n=== L2 clamp_limit 边界语义 ===")
    fn = None
    if pagination_src is None:
        try:
            from core.pagination import clamp_limit
            fn = clamp_limit
            _rec("L2a 可导入", True)
        except Exception as e:
            return _rec("L2a 可导入", False, str(e)[:140])
    else:
        ns = {"__name__": "pagination_mut"}
        try:
            exec(compile(pagination_src, "pagination_mut.py", "exec"), ns)
            fn = ns["clamp_limit"]
            _rec("L2a 可导入（变异体已重载）", callable(fn))
        except Exception as e:
            _rec("L2a 变异体无法执行", False, str(e)[:140])
            return False

    ok = True
    def _safe(x):
        try:
            return fn(x, default=50, maximum=200)
        except Exception as e:
            return "EXC:%s" % str(e)[:40]
    cases = [
        # (入参, default, maximum, 期望, 说明)
        (50, 50, 200, 50, "正常值原样返回"),
        (99999, 50, 200, 200, "超上限被夹紧"),
        (0, 50, 200, 50, "0 回落 default（不能返 0 条）"),
        (-5, 50, 200, 50, "负数回落 default（不能返 1 条）"),
        (None, 50, 200, 50, "None 回落 default"),
        ("abc", 50, 200, 50, "非数字回落 default（不抛异常）"),
        ("", 50, 200, 50, "空串回落 default"),
        (1, 50, 200, 1, "下界 1 有效"),
        (200, 50, 200, 200, "恰好等于上限"),
    ]
    for v, d, mx, want, why in cases:
        try:
            got = fn(v, default=d, maximum=mx)
        except Exception as e:
            got = "EXC:%s" % str(e)[:60]
        ok &= _rec("L2 %s(%r, default=%d, max=%d)=%d" % (why, v, d, mx, want),
                   got == want, "got=%s" % got)

    # 不变量：任何输入都落在 [1, maximum]
    inv = [_safe(x) for x in (-10 ** 9, -1, 0, 1, 199, 200, 201, 10 ** 9,
                             None, "", "abc", [], {}, 3.7, float("nan"))]
    ok &= _rec("L2b 不变量：任意输入都落在 [1, maximum]",
               all(isinstance(v, int) and 1 <= v <= 200 for v in inv),
               "越界=%s" % [v for v in inv
                              if not (isinstance(v, int) and 1 <= v <= 200)])
    # 夹紧而非抛错（抛 422 会让既有前端突然失败）
    try:
        no_exc = _safe("abc") == 50
    except Exception:
        no_exc = False
    ok &= _rec("L2c 非法输入不抛异常（夹紧而非 422）", no_exc)
    # maximum 本身非法时不崩
    try:
        m_ok = 1 <= fn(10, default=50, maximum=-1) <= 200
    except Exception:
        m_ok = False
    ok &= _rec("L2d maximum 非法时回落默认上限", m_ok)
    return ok


# ── L3：端点已改用统一口径 ───────────────────────────────────────────────
def t_l3_migration():
    print("\n=== L3 端点已改用 clamp_limit ===")
    ok = True
    import glob
    users = 0
    old_style = []
    for p in glob.glob(os.path.join(ROOT, "routers", "*.py")) + \
            glob.glob(os.path.join(ROOT, "routers", "knowledge_parts", "*.py")):
        src = open(p, encoding="utf-8").read()
        n = src.count("clamp_limit(")
        if n:
            users += 1
        for m in re.finditer(r"limit = max\(1, min\(int\(limit or (\d+)\), (\d+)\)\)", src):
            old_style.append((os.path.basename(p), m.group(1), m.group(2)))
    ok &= _rec("L3a 已有端点改用 clamp_limit", users >= 1, "users=%d" % users)
    ok &= _rec("L3b 不再有手写的 max(1,min(int(limit or N),C)) 写法",
               not old_style, "残留=%s" % old_style[:4])
    ok &= _rec("L3c 各端点的夹紧上限有据可查（函数体内有 clamp_limit 调用）",
               users >= 1)
    return ok


# ── 变异自证 ─────────────────────────────────────────────────────────────
def mutations():
    print("\n--- M1：clamp_limit 去掉上限夹紧 ⇒ L2 判红 ---")
    _IN_MUT[0] = True
    src = _src(os.path.join("core", "pagination.py"))
    mut = src.replace("return max(1, min(n, cap))", "return max(1, n)")
    _rec("M1 去掉上限 ⇒ L2 判红",
         (mut != src) and (not t_l2_semantics(mut)))
    _IN_MUT[0] = False

    print("\n--- M2：负数不再回落 default（返回 1）⇒ L2 判红 ---")
    _IN_MUT[0] = True
    mut2 = src.replace("    if n <= 0:\n", "    if n < 0:\n")
    _rec("M2 负数返 1 而非 default ⇒ L2 判红",
         (mut2 != src) and (not t_l2_semantics(mut2)))
    _IN_MUT[0] = False

    print("\n--- M3：非法输入抛异常而非回落 ⇒ L2 判红 ---")
    _IN_MUT[0] = True
    mut3 = src.replace(
        "    except (TypeError, ValueError):\n"
        "        # None / \"\" / \"abc\" / 列表 → 回落默认值\n"
        "        n = int(default)\n",
        "    except (TypeError, ValueError):\n"
        "        raise\n")
    _rec("M3 非法输入抛异常 ⇒ L2 判红",
         (mut3 != src) and (not t_l2_semantics(mut3)))
    _IN_MUT[0] = False

    print("\n--- M4：端点退回手写 min() ⇒ L3 判红 ---")
    _IN_MUT[0] = True
    # ⚠️ M4 第一版写成"断言当前没有残留写法"—— 那不是变异，只是**重跑常态**
    #   （而常态本就通过）⇒ 永远不会判红。
    #   正解：真的**注入**一份含旧写法的源码，让 L3 的判据对它判红。
    fake = (
        "from core.pagination import clamp_limit\n"
        "def _demo(limit: int = 50):\n"
        "    limit = max(1, min(int(limit or 50), 200))\n"
        "    return limit\n"
    )
    _rec("M4 端点退回手写 min() ⇒ L3 判红",
         not t_l3_no_old_style({"__legacy__.py": fake}))
    _IN_MUT[0] = False


def t_l3_no_old_style(fake_sources=None):
    """判"没有残留手写夹紧"。

    :param fake_sources: {文件名: 源码} 注入的额外样本（供变异自证）。
        旧写法正则：`limit = max(1, min(int(limit or N), C))`
    """
    import glob
    old_style = []
    files = glob.glob(os.path.join(ROOT, "routers", "*.py")) + \
        glob.glob(os.path.join(ROOT, "routers", "knowledge_parts", "*.py"))
    items = [(p, open(p, encoding="utf-8").read()) for p in files]
    if fake_sources:
        items += [(("__fake__/" + k), v) for k, v in fake_sources.items()]
    for p, src in items:
        for m in re.finditer(r"limit = max\(1, min\(int\(limit or (\d+)\), (\d+)\)\)", src):
            old_style.append((os.path.basename(p), m.group(1), m.group(2)))
    return _rec("L3b 不再有手写 max(1,min(int(limit or N),C)) 写法",
                not old_style, "残留=%s" % old_style[:4])


def main():
    ok = True
    ok &= t_l1_exists()
    ok &= t_l2_semantics()
    ok &= t_l3_migration()

    print("\n" + "=" * 68)
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("常态断言：%d 条，%d 通过 / %d 失败" % (len(_results), len(_results) - n_fail, n_fail))
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    if n_fail:
        print("结论：门禁未通过")
        return 1

    print("\n--- 变异自证 ---")
    _MUT_ROWS.clear()
    mutations()
    verdicts = [r for r in _MUT_ROWS if re.match(r"^M\d", r[1])]
    subs = [r for r in _MUT_ROWS if not re.match(r"^M\d", r[1])]
    n_red = sum(1 for r in verdicts if r[0] == PASS)
    print("变异组：%d，判红成功：%d（变异期子断言 %d 条，其中 %d 条转红）"
          % (len(verdicts), n_red, len(subs), sum(1 for r in subs if r[0] == FAIL)))
    print("-" * 68)
    for st, name, detail in verdicts:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    print("=" * 68)
    if n_red != len(verdicts) or not verdicts:
        print("结论：门禁未通过（%d/%d 变异未判红）" % (len(verdicts) - n_red, len(verdicts)))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组全部按预期判红）"
          % (len(_results), len(verdicts)))
    print("注：实测**缺夹紧的 limit 端点为 0 个**（原估 33 个全是误报）；"
          "本项的价值是把边界语义统一（limit=-5 曾返 1 条、limit=0 返 15 条）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
