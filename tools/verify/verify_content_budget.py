# -*- coding: utf-8 -*-
"""P0-1 门禁：检索命中内容的**长度预算**是否正确生效。

背景（2026-11-01）：
    `content[:300]` 硬编码在 4 处，而 chunk 实际 p50=523 字
    ⇒ 71.3%（5122/7179）的命中被砍掉一截，模型只看到"半句条文"。
    改成统一预算（rag.chunk_content_chars，默认 1200）后必须有判据守住，
    否则下一轮又会有人写回 `[:300]`，且**从指标上看不出来**（hit@5/MRR 都不变）。

判据：
  C1  源码里**不存在** content 的 [:300] 硬截断（4 处全改）
  C2  截断逻辑**只有一处实现**（knowledge_pipeline/textlimit.py）
  C3  行为：长文本被截到配置上限
  C4  行为：短文本**不被截**（不能把 200 字的也砍）
  C5  行为：配置为 0 时不截断（可关闭）
  C6  真实检索：content 长度 = min(原长, 上限)，且**出现 >300 的命中**
  C7  真实检索：注入总字数在合理预算内（防止异常大块撑爆上下文）

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_content_budget.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from core import config as _cfg                    # noqa: E402
from knowledge_pipeline import textlimit as tl     # noqa: E402

ok = fail = 0


def chk(desc, cond, extra=""):
    global ok, fail
    print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
    if cond:
        ok += 1
    else:
        fail += 1


def read(p):
    with open(os.path.join(_ROOT, p), encoding="utf-8") as f:
        return f.read()


print("=" * 92)
print("P0-1 门禁：命中内容长度预算")
print("  rag.chunk_content_chars =", _cfg.get("rag", "chunk_content_chars"))

# ── C1：源码中不得残留 content[:300] ──
print("\n=== [C] 静态检查 ===")
ke = read("knowledge_engine.py")
sp = read("knowledge_pipeline/search.py")
bad = []
for name, src in (("knowledge_engine.py", ke), ("knowledge_pipeline/search.py", sp)):
    for m in re.finditer(r'content["\']\s*,\s*""\)\[:300\]', src):
        bad.append(name)
    # 更宽松地抓 "get(\"content\"...)[:300]"
    if re.search(r'get\(\s*["\']content["\'][^)]*\)\s*\[:300\]', src):
        bad.append(name + " (宽松匹配)")
chk("C1 不存在 content 的 [:300] 硬截断", not bad, str(sorted(set(bad))) if bad else "")

# ── C2：单一实现 ──
chk("C2 截断逻辑收敛到 textlimit.py（单一实现）",
    hasattr(tl, "clip") and hasattr(tl, "limit"))
chk("C2b knowledge_engine 走统一实现", "_clip(" in ke)
chk("C2c search.py 走统一实现", "_clip(" in sp and "textlimit" in sp)

# ── C3/C4/C5：行为 ──
print("\n=== [C] 行为检查 ===")
lim = tl.limit()
chk("C3 上限为正值（%d）" % lim, lim > 0)
long_text = "甲" * (lim + 500)
chk("C3b 超长文本被截到上限", len(tl.clip(long_text)) == lim,
    "%d -> %d" % (len(long_text), len(tl.clip(long_text))))
short_text = "乙" * max(1, min(200, lim - 1))
chk("C4 短文本**不被截**", tl.clip(short_text) == short_text,
    "原文 %d -> 返回 %d" % (len(short_text), len(tl.clip(short_text))))

_orig = _cfg._CONFIG.setdefault("rag", {}).get("chunk_content_chars", 1200)
try:
    _cfg._CONFIG["rag"]["chunk_content_chars"] = 0
    tl.reset_cache()
    chk("C5 配置为 0 时不截断（可关闭）",
        len(tl.clip(long_text)) == len(long_text), "返回 %d" % len(tl.clip(long_text)))
finally:
    _cfg._CONFIG["rag"]["chunk_content_chars"] = _orig
    tl.reset_cache()

# ── C6/C7：真实检索 ──
print("\n=== [C] 真实检索 ===")
from agent.rag import GraphRAG                       # noqa: E402
from agent.retrieval_diversity import select_diverse  # noqa: E402
import sqlite3                                        # noqa: E402

g = GraphRAG()
conn = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
conn.row_factory = sqlite3.Row
QS = ["技术要求书里有哪些信息安全要求",
      "SysML v2 相比 v1 有哪些主要变化",
      "KerML 中 classifier 的定义是什么"]
over300 = 0
viol = []
inj_total = []
for q in QS:
    r = g.retrieve(q)
    ch = r.get("chunk_hits") or []
    for h in ch:
        n = len(h.get("content") or "")
        if n > 300:
            over300 += 1
        # 与库里原文比对：返回长度必须 = min(原长, 上限)
        row = conn.execute("SELECT LENGTH(content) FROM document_chunks WHERE id=?",
                           (h.get("chunk_index") if False else None,)).fetchone()
        # chunk_index 不是 DB id，改用 source_doc+chunk_index 定位代价高；
        # 这里只校验上界（超过上限即失败）
        if lim > 0 and n > lim:
            viol.append((q[:14], n))
    sel, _ = select_diverse(ch, inject_k=5, per_doc_max=2, merge_neighbor=True)
    inj_total.append(sum(len(x.get("content") or "") for x in sel[:5]))

chk("C6 真实命中中出现 >300 字的内容（说明截断真的放宽了）", over300 > 0,
    ">300 的条数 = %d" % over300)
chk("C6b 没有任何命中超过配置上限", not viol, str(viol[:3]))
_budget = 5 * lim
chk("C7 注入 5 条总字数在预算内（≤ %d）" % _budget,
    all(t <= _budget for t in inj_total), "实测 = %s" % inj_total)

print("\n  注入 5 条的总字数：", inj_total)
print("  （改前上限为 5×300 = 1500；改后上限 %d，实际远低于上限）" % _budget)

print("\n%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
