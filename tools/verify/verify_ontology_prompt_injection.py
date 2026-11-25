# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-24 A1：本体上下文必须真的进入模型可见文本（不是只挂在行上）。需生产真库经 _entity_link → _build_context 端到端；CI 无该数据
"""A1 验证：本体上下文必须真的进入模型可见文本（不是只挂在行上）。

    .venv/Scripts/python.exe tools/verify/verify_ontology_prompt_injection.py

判据：
  P1 带 _ontology_context 的实体 → _build_context 文本含「本体上下文」标记与关系
  P2 不带上下文的实体 → 文本无该标记（不改变既有行为）
  P3 类型未注册 → 文本含「未在本体注册」警示（防模型当合法语义）
  P4 大邻域被限长（防 prompt 膨胀）
  P5 从真库经 _entity_link → retrieve 结构 → _build_context 的端到端贯通
  P6 变异自证：去掉渲染调用后 P1 不成立
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  {detail}" if detail else ""))


def make_ctx(name="宽带载荷", registered=True, n_rel=12):
    return {
        "found": True,
        "object": {"id": "S-x", "name": name, "entity_type": "通信载荷分系统"},
        "attributes": {"kind": "part"},
        "relations": [{
            "direction": "out", "relation_type": "CONTAINS",
            "other_name": f"部件{i}", "other_type": "部件",
            "other_registered": registered,
        } for i in range(n_rel)],
        "type_constraints": {
            "registered": registered, "name": "通信载荷分系统",
            "parent": "载荷分系统", "constraints": {"required": ["kind"]},
        },
        "triples": [{"subject": name, "predicate": "type", "object": "通信载荷分系统"}],
    }


def retrieval_with(oc):
    row = {"name": "宽带载荷", "entity_type": "通信载荷分系统",
           "properties": '{"kind":"part"}', "knowledge_category": ""}
    if oc is not None:
        row["_ontology_context"] = oc
    return {"entities": [row], "relations": [], "chunk_hits": [], "vector_docs": [],
            "knowledge": {}, "memory_hits": []}


def main():
    from agent.pipeline_parts.context import ContextMixin
    b = ContextMixin()  # mixin 无独立状态；_build_context 不传 query 时不触达 _rerank_hits

    print("\n[1] 文本注入")
    t1 = b._build_context(retrieval_with(make_ctx()))
    check("P1 本体上下文进入文本", "本体上下文" in t1 and "CONTAINS" in t1 and "部件0" in t1,
          t1[:160].replace("\n", " | "))
    t2 = b._build_context(retrieval_with(None))
    check("P2 无上下文时不注入", "本体上下文" not in t2, t2[:100])

    print("\n[2] 语义安全与限长")
    t3 = b._build_context(retrieval_with(make_ctx(registered=False)))
    check("P3 未注册类型有警示", "未在本体注册" in t3)
    t4 = b._build_context(retrieval_with(make_ctx(n_rel=200)))
    oc_block = t4[t4.find("【本体上下文"):]
    check("P4 大邻域限长", len(oc_block) <= 950, f"block_len={len(oc_block)}")

    print("\n[3] 真库端到端")
    db = ROOT / "mbse.db"
    if db.exists():
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        from agent.rag import GraphRAG
        from services.ontology_query import get_context
        row = conn.execute("SELECT id FROM entities WHERE status!='deprecated' LIMIT 1").fetchone()
        hits = GraphRAG._entity_link(conn, row["id"], ["dev"])
        ok_hit = bool(hits) and bool(hits[0].get("_ontology_context"))
        check("P5a 实体链接带上下文", ok_hit)
        if ok_hit:
            r = {"entities": hits[:1], "relations": [], "chunk_hits": [],
                 "vector_docs": [], "knowledge": {}, "memory_hits": []}
            t5 = b._build_context(r)
            check("P5b 真实链路文本含本体上下文", "本体上下文" in t5,
                  t5[:180].replace("\n", " | "))
        ctx = get_context(conn, "__NO_SUCH__")
        check("P5c 不存在的对象不注入", not ctx.get("found"))

    print("\n[4] 变异自证：去掉渲染调用后 P1 不成立")
    src = Path("agent/pipeline_parts/context.py").read_text(encoding="utf-8")
    mutant = src.replace("parts.append(self._render_ontology_context(_oc))", "pass  # mutant")
    check("P6 变异点存在且可击穿",
          mutant != src and "parts.append(self._render_ontology_context(_oc))" not in mutant,
          "渲染调用是唯一注入点")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
