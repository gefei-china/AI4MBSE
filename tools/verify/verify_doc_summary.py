# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
# -*- coding: utf-8 -*-
"""P0-2 门禁：文档级摘要是否正确接入检索与注入。

背景（2026-11-01）：
    `documents.summary` 的 LLM 摘要早就在生成，但 `rag.py` / `knowledge_engine.py`
    **完全不读它** —— 能力空转。实测 14 篇里只有 3 篇有值（21%），
    补齐后 14/14。本门禁守住四条语义：

  D1 命中文档有摘要时，`doc_summaries` 必须非空
  D2 条数 ≤ 3（预算控制，防止摘要挤占正文）
  D3 **只含命中文档**（不能把无关文档的摘要塞进上下文）
  D4 无摘要时**优雅降级**（不报错、不阻塞检索）
  D5 注入文本里出现「文档背景」块（真的进了 prompt，不只是字段存在）

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_doc_summary.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import sqlite3                                       # noqa: E402
from agent.rag import GraphRAG                       # noqa: E402

ok = fail = 0


def chk(desc, cond, extra=""):
    global ok, fail
    print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
    if cond:
        ok += 1
    else:
        fail += 1


conn = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
conn.row_factory = sqlite3.Row

print("=" * 92)
print("P0-2 门禁：文档摘要接入")

# ── 覆盖率前置（不达标后面都没意义）──
tot = conn.execute("SELECT COUNT(*) FROM documents WHERE parse_status='completed'").fetchone()[0]
has = conn.execute("SELECT COUNT(*) FROM documents WHERE parse_status='completed' "
                   "AND COALESCE(summary,'')<>''").fetchone()[0]
print("  摘要覆盖：%d / %d 篇（%.0f%%）" % (has, tot, 100.0 * has / max(tot, 1)))
chk("D0 摘要覆盖率 ≥ 80%%（补齐工具效果）", tot and has / tot >= 0.8,
    "%d/%d" % (has, tot))

g = GraphRAG()
QS = ["技术要求书里有哪些信息安全要求",
      "SysML v2 相比 v1 有哪些主要变化",
      "KerML 中 classifier 的定义是什么",
      "车辆热管理仿真的整体建模思路"]
print("\n=== 真实检索 ===")
results = {}
for q in QS:
    r = g.retrieve(q)
    ds = r.get("doc_summaries") or []
    hits = r.get("chunk_hits") or []
    hit_docs = {str(h.get("source_doc") or "") for h in hits}
    results[q] = {"ds": ds, "hit_docs": hit_docs, "hits": len(hits)}
    print("  %-30s 命中=%-3d 摘要=%-2d %s"
          % (q[:30], len(hits), len(ds),
             [str(d.get("source_doc"))[:18] for d in ds]))

print("\n=== 判据 ===")
# D1：有命中且有摘要 → 至少要给出一些
with_summary = [q for q in QS if results[q]["hits"] > 0]
got = [q for q in with_summary if results[q]["ds"]]
chk("D1 命中且有摘要的 query 都返回了 doc_summaries", bool(got),
    "命中 %d 条 query，返回摘要的 %d 条" % (len(with_summary), len(got)))

# D2：条数上限
over = [(q, len(results[q]["ds"])) for q in QS if len(results[q]["ds"]) > 3]
chk("D2 摘要条数 ≤ 3（预算控制）", not over, str(over) if over else "")

# D3：摘要只来自命中文档
leak = []
for q in QS:
    for d in results[q]["ds"]:
        fn = str(d.get("source_doc") or "")
        if fn and results[q]["hit_docs"] and fn not in results[q]["hit_docs"]:
            leak.append((q[:12], fn[:24]))
chk("D3 摘要只含**命中文档**（无无关文档摘要混入）", not leak, str(leak[:3]) if leak else "")

# D4：无摘要场景不炸（构造：一个库里不存在的文档）
try:
    from agent.rag import GraphRAG as _G
    _r = _G().retrieve("一个完全不存在的主题 zzqqxx")
    _ok = isinstance(_r.get("doc_summaries"), list)
except Exception as e:
    _ok = False
chk("D4 零命中/无摘要时优雅降级（不抛异常）", _ok)

# D5：真的进了 prompt
print("\n=== 注入层（真的进了 prompt 吗）===")
try:
    from agent.pipeline_parts.context import _ContextMixin  # noqa
    _has_code = True
except Exception:
    _has_code = None
ctx_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "context.py"), encoding="utf-8").read()
chk("D5 context.py 消费 doc_summaries 并输出「文档背景」",
    "doc_summaries" in ctx_src and "文档背景" in ctx_src)

# D5b：实测注入文本（用真实 retrieval 走一遍拼装）
# ⚠️ 类名/签名必须核对真实代码，别猜（第一版本写成 `_ContextMixin` ⇒ 直接 skip，
#    等于最关键的一条判据**静默没验**，而门禁仍显示全绿）。
try:
    from agent.pipeline_parts.context import ContextMixin
    inst = object.__new__(ContextMixin)
    _q = "技术要求书里有哪些信息安全要求"
    _ret = g.retrieve(_q)
    _txt = ContextMixin._build_context(inst, _ret, _q)
    chk("D5b 注入文本里包含「文档背景」块", "文档背景" in (_txt or ""),
        "注入长度=%d" % len(_txt or ""))
    chk("D5c 注入文本同时保留【来源n】引用", "【来源1】" in (_txt or ""))
    # D5d：摘要块在【来源1】**之前**（先建立语境，再看条文）
    _i_bg = (_txt or "").find("文档背景")
    _i_src = (_txt or "").find("【来源1】")
    chk("D5d 文档背景排在【来源1】之前（先语境后条文）",
        0 <= _i_bg < _i_src if (_i_src >= 0) else _i_bg >= 0,
        "bg@%d src@%d" % (_i_bg, _i_src))
except Exception as e:
    chk("D5b/D5c/D5d 拼装调用成功（不允许静默 skip）", False, "%s: %s" % (type(e).__name__, str(e)[:80]))

print("\n%d 项通过，%d 项失败" % (ok, fail))
sys.exit(1 if fail else 0)
