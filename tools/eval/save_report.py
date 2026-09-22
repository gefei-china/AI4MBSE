"""把 run_eval.py 产出的评测报告 JSON 落库到 eval_reports 表（7.1c~e 取证闭环）。

用法：python tools/eval/save_report.py [报告.json] [--tag 标签]
不传路径时默认取最新的 tmp/p21/eval_full.json。

映射口径（eval_reports 表原为抽取评测设计，检索评测复用其列 + detail 存全量 JSON）：
- entity_recall        ← graph 域 entity_recall（结构化实体召回，对标 7.1 结构化≥95%）
- relation_recall      ← graph 域 relation_hit
- doc_name             ← 评测标签 + 三域例数摘要
- detail               ← 报告全量 JSON（doc/graph/neg/负对照/指纹）
"""
import json
import sqlite3
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))  # tools/eval → 工程根
from database.connection import get_db  # noqa: E402


def save(report_path: str, tag: str = "") -> int:
    report = json.load(open(report_path, encoding="utf-8"))
    summary = report.get("summary", {})
    doc, graph, neg = summary.get("doc", {}), summary.get("graph", {}), summary.get("neg", {})
    n_total = (doc.get("n") or 0) + (graph.get("n") or 0) + (neg.get("n") or 0)
    doc_name = (f"RAG检索评测 {report.get('tag') or tag} "
                f"(doc {doc.get('n', 0)} / graph {graph.get('n', 0)} / neg {neg.get('n', 0)}"
                f"{'+负对照' if summary.get('doc_control') else ''}，共 {n_total} 例)")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO eval_reports (doc_id, doc_name, entity_recall, relation_recall, detail, "
            "created_by, model_version, is_golden) VALUES (0,?,?,?,?,?,?,0)",
            (doc_name, graph.get("entity_recall"), graph.get("relation_hit"),
             json.dumps(report, ensure_ascii=False), "tools/eval/run_eval",
             f"rag-eval {report.get('tag') or tag}"))
        conn.commit()
        rid = cur.lastrowid
        print(f"eval_reports 落库成功: id={rid}")
        print(f"  {doc_name}")
        print(f"  doc recall@5={doc.get('recall@5')} | graph entity_recall={graph.get('entity_recall')} "
              f"route_acc={graph.get('route_acc')} | neg route_ok={neg.get('route_ok')}")
        return rid
    finally:
        conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    path = args[0] if args and not args[0].startswith("--") else "tmp/p21/eval_full.json"
    tag = ""
    if "--tag" in args:
        tag = args[args.index("--tag") + 1]
    save(path, tag)
