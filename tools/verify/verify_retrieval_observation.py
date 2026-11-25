"""S4 门禁：检索观测口径必须真实，且零命中要分得清成因（2026-10-29）。

## 要防的三件事

1. **观测口径造假象**：`query_trace.route` 曾被**硬编码为 `"vector"`** 且只在向量兜底
   分支内写 ⇒ 表内 3,484 条**全是 vector**，图/混合路由零 trace。
   危害不是"少几行"，而是**据错数据得出相反结论**：我曾判"图路由零命中"，
   查 `query_routing_stats` 才发现图路由实际占 264/1867（14%）。
2. **把"检索被用错地方"算成"召回质量差"**：59 条零命中里，
   指令性文本 44% + 业务数据查询 11% ⇒ **过半根本不是召回问题**。
   混在一起统计会把人引向"去调向量阈值"这条错路。
3. **细分时误伤真实知识提问**：把"什么是需求定义"判成"不该检索"会导致该召回的不召回。
   门禁必须有**保护性负例**（真实知识提问必须仍算 corpus_gap）。

## 判据写法
- 分类器用**真实库里的 59 条零命中样本**回放（不手写样本）。
- 路由正确性做**源码级**断言（`trace_query` 只在 decide 之后、route 传实参）。
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


from agent.retrieval_reason import (           # noqa: E402
    classify_no_hit, refine_reason, is_real_recall_problem,
)

print("== A 保护性负例：真实知识提问不得被判成「不该检索」==")
# 这些必须仍然算 corpus_gap（否则会把该召回的问题当成"不该检索"而放过）
for q in ("什么是需求定义",
          "如何生成状态机视图",
          "SysML v2 与 SysML v1 有何区别",
          "块定义图与内部块图的区别是什么",
          "接口需求的管理流程是什么"):
    got = classify_no_hit(q)
    check("A 知识提问仍算召回问题: %s" % q[:22], got == "no_hit_corpus_gap", got)

print()
print("== B 指令性文本必须识别为 directive ==")
for q in ("请生成 sequence 视图并调用校验",
          "继续生成整车的内部块图_IBD视图",
          "我的目标是生成状态机视图",
          "【澄清补充】用户已补充建模信息（请据此继续）"):
    got = classify_no_hit(q)
    check("B 指令识别: %s" % q[:22], got == "no_hit_directive", got)

print()
print("== C 业务数据查询必须识别为 biz_query ==")
for q in ("智源工程列表 工程 projectId",
          "工程列表 项目 project",
          "p1a0d2e694af 工程 包 模型树 层级"):
    got = classify_no_hit(q)
    check("C 业务查询识别: %s" % q[:22], got == "no_hit_biz_query", got)

print()
print("== D 过短噪声单独归类（不该污染召回率）==")
for q in ("q0", "q1", "工程", "", None):
    got = classify_no_hit(q)
    check("D 噪声识别: %r" % (q,), got == "no_hit_tiny", got)

print()
print("== E 关键：去掉裸「定义」的误伤（实测踩过）==")
# `块定义图_BDD` 的「定义」曾把指令误判成知识提问
q = "【澄清补充】问题「块定义图_BDD 视图希望以哪种形式交付？」→ 回答：Mermaid 代码"
check("E 「块定义图」不再被当成知识词", classify_no_hit(q) == "no_hit_directive",
      classify_no_hit(q))

print()
print("== F refine_reason 零侵入：只细分 no_hit_fallback ==")
check("F1 no_hit_fallback+vector → 细分",
      refine_reason("智源工程列表 工程 projectId", "vector", "no_hit_fallback")
      == "no_hit_biz_query")
for r in ("graph_no_hit", "graph_confidence_low_vector_supplement",
          "graph_hit_insufficient_confidence", "no_hit_directive"):
    check("F 其它 reason 原样返回: %s" % r, refine_reason("随便", "vector", r) == r)
check("F route 非 vector 时不细分",
      refine_reason("智源工程列表", "graph", "no_hit_fallback") == "no_hit_fallback")

print()
print("== G 「真实召回问题」的定义必须显式且唯一 ==")
check("G1 corpus_gap 才算真召回问题", is_real_recall_problem("no_hit_corpus_gap"))
for r in ("no_hit_directive", "no_hit_biz_query", "no_hit_tiny", "graph_no_hit", ""):
    check("G 不算真召回问题: %s" % (r or "(空)"), not is_real_recall_problem(r))

print()
print("== H 真实样本回放：错配占比必须显著（证明统计口径确有失真）==")
_db = (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db"))
if not os.path.isfile(_db):
    check("H 库存在", False, _db)
else:
    con = sqlite3.connect(f"file:{_db}?mode=ro", uri=True)
    rows = [r[0] for r in con.execute(
        "SELECT query FROM query_routing_stats WHERE reason='no_hit_fallback'")]
    con.close()
    if len(rows) < 10:
        print("  [SKIP] H 组：库内 no_hit_fallback 样本不足 10 条（样本缺失，非缺陷）")
    else:
        from collections import Counter
        c = Counter(classify_no_hit(q) for q in rows)
        n = len(rows)
        mismatch = c["no_hit_directive"] + c["no_hit_biz_query"]
        real = c["no_hit_corpus_gap"]
        print("  回放 %d 条：directive=%d biz=%d tiny=%d corpus_gap=%d"
              % (n, c["no_hit_directive"], c["no_hit_biz_query"], c["no_hit_tiny"], real))
        check("H1 错配（指令+业务）占比 > 0", mismatch > 0, "%d/%d" % (mismatch, n))
        check("H2 存在真实的召回缺口样本", real > 0, real)
        check("H3 分类覆盖全部样本（无 None/未知）", sum(c.values()) == n)

print()
print("== I 源码级：trace 必须在路由决策之后、用真实 route ==")
src = open(os.path.join(_ROOT, "agent", "rag.py"), encoding="utf-8").read()
_code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

# ★ 2026-11-25 修（实测假红）：I1/I2/I6 原来锚在字面量
#   `from glossary import trace_query` 上，而模块已搬进 core.domain、
#   import 变成 `from core.domain.glossary import trace_query as _tq`
#   ⇒ i_trace=-1，I1/I2/I6 三条连坐恒红。这与 memory_dream M1 是同一类缺陷：
#   **判据写死了 import 的写法**。正解：按 AST 找**调用点**（认别名），只认语义不认写法。
import ast as _ast

_alias = set()
for _n in _ast.walk(_ast.parse(src)):
    if isinstance(_n, _ast.ImportFrom) and (_n.module or "").endswith("glossary"):
        for _a in _n.names:
            if _a.name == "trace_query":
                _alias.add(_a.asname or _a.name)
_trace_calls, _decide_calls = [], []
for _n in _ast.walk(_ast.parse(src)):
    if isinstance(_n, _ast.Call):
        _f = _n.func
        if isinstance(_f, _ast.Name) and _f.id in _alias:
            _trace_calls.append(_n.lineno)
        if (isinstance(_f, _ast.Attribute) and _f.attr == "decide"
                and isinstance(_f.value, _ast.Attribute) and _f.value.attr == "router"):
            _decide_calls.append(_n.lineno)

i_decide = min(_decide_calls) if _decide_calls else -1
i_trace = min(_trace_calls) if _trace_calls else -1
check("I1 两者都存在", i_decide > 0 and i_trace > 0, ("decide@%s" % i_decide, "trace@%s" % i_trace))
check("I2 trace 在 decide 之后（行号更大）", i_trace > i_decide,
      "decide@%d trace@%d" % (i_decide, i_trace))
# 硬编码 route 必须消失（只允许出现在注释里——已剥离注释）
check("I3 可执行代码里无硬编码 route=vector",
      '"route": "vector"' not in _code)
check("I4 trace 传的是 route 变量",
      re.search(r'"route":\s*route', _code) is not None)
check("I5 trace 附带了路由理由（可按列聚合）", '"reason": reason' in _code)
# I6：全文件只有**一处** trace 调用（不再散落在分支内），且它在 decide 之后
check("I6 三种路由都写（只有一处调用，且在决策之后）",
      len(_trace_calls) == 1 and i_trace > i_decide,
      "调用点=%s（decide@%d）" % (_trace_calls, i_decide))

print()
print("[RESULT] PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
if _FAIL:
    print("[FAILED]", _FAIL)
    sys.exit(1)
print("[ALL GREEN]")
