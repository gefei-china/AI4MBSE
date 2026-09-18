# -*- coding: utf-8 -*-
"""SHACL 违规诊断（只读，不修改任何数据）。

产出：
1. 全量校验违规分布（按 path × severity 聚合）
2. 每类违规的根因定位：关系约束 src/tgt vs 实际数据的 (源类型→目标类型) 分布
3. 建议：自动修复 / 需人工确认 分类

用法： .venv/Scripts/python.exe diag_violations.py [--json out.json]
"""
import os
import sys
import json
import argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_db  # noqa: E402
from core import ns  # noqa: E402
from ingest_gate import check_triples  # noqa: E402


def unlocal_pred(path: str):
    """rel/prop IRI → 局部名。"""
    for prefix in (ns.NS_PRED_REL, ns.NS_PRED_PROP, ns.NS_ONTOLOGY):
        if path.startswith(prefix):
            return ns.unlocal(path[len(prefix):])
    return path


def ent_label(iri: str, conn):
    """实例 IRI → (id, name)。"""
    if not iri.startswith(ns.NS_ENT):
        return ("", iri)
    loc = ns.unlocal(iri[len(ns.NS_ENT):])
    row = conn.execute("SELECT name, entity_type FROM entities WHERE id=?", (loc,)).fetchone()
    if row:
        return (loc, row["name"], row["entity_type"])
    return (loc, "", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    conn = get_db()
    print("== 全量 SHACL 校验（record=False，只读） ==")
    res = check_triples(conn, record=False)
    if res.get("skipped"):
        print("skipped:", res.get("reason"))
        return
    viols = res["violations"]
    print(f"conforms={res['conforms']}  violations={len(viols)}  triples={res['total_checked']}")

    # ── 1. 按 (path, message前缀) 聚合 ──
    by_path = Counter()
    by_sev = Counter()
    msg_by_path = defaultdict(Counter)
    for v in viols:
        p = unlocal_pred(v.get("path") or "(class-level)")
        by_path[p] += 1
        by_sev[v.get("severity")] += 1
        # 消息归一：去掉具体 IRI，留模式
        m = v.get("message", "")
        msg_by_path[p][m[:120]] += 1

    print("\n== 按严重级别 ==")
    for s, n in by_sev.most_common():
        print(f"  {s}: {n}")

    print("\n== 按 path 聚合（Top 30） ==")
    for p, n in by_path.most_common(30):
        top_msg = msg_by_path[p].most_common(1)[0][0][:80]
        print(f"  {n:5d}  {p}   | {top_msg}")

    # ── 2. 根因定位：对每条违规 path，统计实际 (源类型 → 目标实际类型) ──
    # 重建数据图与本体约束对照
    from rdflib import Graph, URIRef
    print("\n== 关系约束 vs 实际数据分布 ==")
    rels = {}
    for t in conn.execute("SELECT name, constraints FROM ontology_types "
                          "WHERE type_kind='relation'").fetchall():
        try:
            rels[t["name"]] = json.loads(t["constraints"] or "{}").get("allowed_values", {})
        except Exception:
            rels[t["name"]] = {}
    # 实体 id → 类型
    id2type = {r["id"]: r["entity_type"] for r in
               conn.execute("SELECT id, entity_type FROM entities WHERE status!='deprecated'")}
    # 实际关系分布：(rel, src_type, tgt_type) → n
    actual = Counter()
    for r in conn.execute("SELECT source_id, target_id, relation_type FROM relations "
                          "WHERE status!='deprecated'").fetchall():
        st = id2type.get(r["source_id"], "?")
        tt = id2type.get(r["target_id"], "?")
        actual[(r["relation_type"], st, tt)] += 1

    report = {
        "conforms": res["conforms"],
        "total_violations": len(viols),
        "by_severity": dict(by_sev),
        "by_path": {p: {"n": n, "top_messages": dict(msg_by_path[p].most_common(3))}
                    for p, n in by_path.most_common()},
        "relation_constraints": rels,
        "actual_relation_distribution": [
            {"rel": k[0], "src": k[1], "tgt": k[2], "n": v}
            for k, v in actual.most_common()],
    }

    # 对照表：仅打印有违规的 path
    viol_paths = {unlocal_pred(v.get("path") or "") for v in viols if v.get("path")}
    for rp in sorted(viol_paths):
        if not rp or rp == "(class-level)":
            continue
        allowed = rels.get(rp, {})
        print(f"\n  关系 [{rp}]  约束 src={allowed.get('src')} tgt={allowed.get('tgt')}")
        sub = Counter()
        for (rel, st, tt), n in actual.items():
            if rel == rp:
                sub[(st, tt)] += n
        for (st, tt), n in sub.most_common(15):
            ok = (not allowed.get("src") or st in allowed["src"]) and \
                 (not allowed.get("tgt") or tt in allowed["tgt"])
            # 注意：此标记是"白名单字面匹配"参考——SHACL sh:class 含子类闭包
            # （子类实例也算命中），且形状只 target src 类型。字面 BAD 未必真违规，
            # 以 SHACL 全量校验结果为准（本脚本第一段输出）。
            mark = "ok* " if ok else "out*"
            print(f"    [{mark}] {st} → {tt}: {n}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"\n报告已写: {args.json}")
    conn.close()


if __name__ == "__main__":
    main()
