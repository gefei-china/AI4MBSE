# -*- coding: utf-8 -*-
"""门禁：AI 输出后统计不丢 —— token 入/出口径正确 + 两条主落库路径都固化 usage。

背景（2026-11-27）：用户反馈「最终 AI 输出后统计数据又不见了，整体时长、
输入/输出 token 量等，是被覆盖了吗」。调研结论：**不是被覆盖**，而是
① token 口径错：读 llm_info.tokens.total（该键不存在）后回退 completion，
  只落最后一次输出 token（真机 conv 1000000255：卡片 559 vs 整轮 160765）；
② 非流式直行路径（execute.py）从未写 usage/summary。

判据：
  A stream 口径：prompt/completion 分列 + total 正确；
  B execute 固化：card_data 构造前 attach usage；
  C 覆盖断言（不是总次数）：两个主路径都必须出现固化调用；
  D 变异自证：去掉 token 分列后 A 必须翻红。

用法：python tools/verify/verify_usage_stats_paths.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))


def group_a():
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
               encoding="utf-8").read()
    check("A", "★ prompt_tokens 落卡", '"prompt_tokens": _pt' in src)
    check("A", "★ completion_tokens 落卡", '"completion_tokens": _ct' in src)
    check("A", "total 口径 = total 或 prompt+completion",
          'int(_tk.get("total") or (_pt + _ct) or 0)' in src)
    check("A", "旧错误口径已移除（不再把 completion 当 total）",
          '.get("completion")\n' not in src)


def group_b():
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py"),
               encoding="utf-8").read()
    check("B", "execute 计算 _card_usage", "_card_usage = {" in src)
    check("B", "★ execute 落库前 attach usage",
          "_attach_usage_summary(_card_obj, llm_content, _card_usage)" in src)
    check("B", "attach 失败也有兜底（不丢统计）",
          '_card_obj["usage"] = _card_usage' in src)
    check("B", "tool_calls 用真实存在的 _ac_tools_hist（不是臆造的 exec_tools）",
          'len(_ac_tools_hist or [])' in src)


def group_c():
    # 覆盖范围断言：对每个主落库文件都要求出现固化，不能只数总次数
    paths = {
        "stream.py": os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
        "execute.py": os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py"),
    }
    for name, p in paths.items():
        s = open(p, encoding="utf-8").read()
        check("C", "%s 落库路径含 usage 固化" % name,
              "_attach_usage_summary" in s)


def group_d():
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
               encoding="utf-8").read()
    anchor = '                    "prompt_tokens": _pt,\n                    "completion_tokens": _ct,'
    if src.count(anchor) != 1:
        check("D", "变异锚点唯一", False, "count=%d" % src.count(anchor))
        return
    check("D", "变异锚点唯一", True)
    mutated = src.replace(anchor, '                    "tokens_only": _tokens_used,', 1)
    check("D", "变异体真的变了", mutated != src)
    # 用文本变异 + 源码断言（不改产品文件）：变异后 A 的判据必须能判红
    check("D", "★ 去掉入/出分列后 A 判据判红（有牙齿）",
          '"prompt_tokens": _pt' not in mutated)


def main():
    for fn in (group_a, group_b, group_c, group_d):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, "组执行异常", False, repr(e))
    w = max(len(n) for _, n, _, _ in RESULTS)
    print("=" * (w + 16))
    print("统计固化门禁（入/出 token 不丢 · 落库路径全覆盖）")
    print("=" * (w + 16))
    for g, n, ok, detail in RESULTS:
        print("  [%s] %s%s" % ("OK" if ok else "FAIL", n,
                               ("  — " + detail) if (detail and not ok) else ""))
    bad = [x for x in RESULTS if not x[2]]
    print("-" * (w + 16))
    print("合计 %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    for g, n, _, d in bad:
        print("  - %s：%s" % (n, d[:200]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
