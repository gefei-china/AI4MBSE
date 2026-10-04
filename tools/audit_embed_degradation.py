# -*- coding: utf-8 -*-
"""P0-2（2026-10-04）：embedding 降级审计 —— 找出被词面向量污染的 chunk/文档。

## 为什么需要这个脚本（一次真实事故）
embedding 供应商返回 `403 Free quota exhausted`，而当时的
`embed_with_version` 把异常**静默**吞掉并降级 bigram ⇒
入库报"成功"，界面无任何提示，而**810 个 chunk 已经用词面向量写进库里**，
与此前 6459 条真向量**混在同一张检索表**里。

关键危害不是"慢"，而是**不可区分**：事后没人能一眼看出哪些 chunk 是词面向量，
于是：
- 语义检索被词面匹配污染，且**没有任何指标异常**（检索照样返回结果）；
- 供应商恢复后，不知道该重算哪些。

本脚本产出三件事：
① 按文档列出污染情况（可直接决定"要不要整篇重算"）；
② 给出**重算命令**（不是自动执行 —— 重算要调 API，是运维决策）；
③ 混合向量比例（> 50% 时语义检索基本失效，应显式告知用户）。

## 用法
```bash
# 只报告（默认，零副作用）
.venv/Scripts/python.exe tools/audit_embed_degradation.py

# 导出受影响文档清单（供后续重算）
.venv/Scripts/python.exe tools/audit_embed_degradation.py --export docs/_reembed_targets.json

# 供应商恢复后重算（真正调 API，消耗额度）
.venv/Scripts/python.exe tools/audit_embed_degradation.py --reembed
```

⚠️ **不要**在本机配额耗尽时跑 `--reembed`：它会立刻失败并（因 P0-2 的 block 策略）
把文档置为 `parse_status=failed`。这是**期望行为** —— 宁可失败也不要再写一批降级向量。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print("  [%s] %s%s" % (rec_status(ok), name, ("  ← " + str(detail)) if detail else ""))
    return bool(ok)


def rec_status(ok):
    return PASS if ok else FAIL


def audit(conn):
    """返回 {total, degraded, ratio, docs:[{doc_id, filename, degraded, total}]}。"""
    rows = conn.execute(
        "SELECT c.document_id AS did, d.filename AS fn, "
        "SUM(CASE WHEN c.embed_version != 'openai-compat' THEN 1 ELSE 0 END) AS deg, "
        "COUNT(*) AS tot "
        "FROM document_chunks c LEFT JOIN documents d ON d.id = c.document_id "
        "GROUP BY c.document_id ORDER BY deg DESC, tot DESC").fetchall()
    docs, deg_total, tot_total = [], 0, 0
    for r in rows:
        deg = int(r["deg"] or 0)
        tot = int(r["tot"] or 0)
        deg_total += deg
        tot_total += tot
        if deg:
            docs.append({"doc_id": r["did"], "filename": r["fn"],
                         "degraded": deg, "total": tot,
                         "ratio": round(deg / tot, 4) if tot else 0.0})
    ratio = (deg_total / tot_total) if tot_total else 0.0
    return {"total": tot_total, "degraded": deg_total, "ratio": round(ratio, 4),
            "docs": docs}


def t_audit_shape(conn):
    """自检：审计函数本身不能算错（用构造数据，不是真实库）。"""
    print("\n=== 自检：审计口径（构造数据）===")
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, filename TEXT)")
    c.execute("CREATE TABLE document_chunks (document_id INT, embed_version TEXT)")
    c.execute("INSERT INTO documents VALUES (1,'a.md'),(2,'b.md')")
    # 文档1：3 真 + 1 降级= 4 块；文档2：2 真 = 2 块 ⇒ 合计 6 块、降级 1 块
    for v in ("openai-compat", "openai-compat", "openai-compat", "bigram-tf"):
        c.execute("INSERT INTO document_chunks VALUES (1,?)", (v,))
    for v in ("openai-compat", "openai-compat"):
        c.execute("INSERT INTO document_chunks VALUES (2,?)", (v,))
    a = audit(c)
    ok = _rec("自检1 total=6", a["total"] == 6, a["total"])
    ok &= _rec("自检2 degraded=1", a["degraded"] == 1, a["degraded"])
    ok &= _rec("自检3 ratio=1/6", abs(a["ratio"] - 1.0 / 6) < 1e-4, a["ratio"])
    ok &= _rec("自检4 只列出受影响文档（1 条）", len(a["docs"]) == 1, len(a["docs"]))
    ok &= _rec("自检5 命中的是文档1 且 degraded=1",
               bool(a["docs"]) and a["docs"][0]["doc_id"] == 1
               and a["docs"][0]["degraded"] == 1,
               a["docs"][0] if a["docs"] else None)
    # 全真向量 ⇒ 不应有任何 docs（避免"全部列出"的噪声）
    c.execute("UPDATE document_chunks SET embed_version='openai-compat'")
    a2 = audit(c)
    ok &= _rec("自检6 全真向量时 docs 为空（不刷屏）", len(a2["docs"]) == 0, len(a2["docs"]))
    # 全降级 ⇒ ratio=1.0
    c.execute("UPDATE document_chunks SET embed_version='bigram-tf'")
    a3 = audit(c)
    ok &= _rec("自检7 全降级时 ratio=1.0", a3["ratio"] == 1.0, a3["ratio"])
    c.close()
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default="", help="导出受影响文档清单到该JSON 路径")
    ap.add_argument("--reembed", action="store_true",
                    help="真调 API 重算（消耗额度；配额耗尽时会失败并把文档置 failed）")
    args = ap.parse_args()

    from core.config import DB_PATH
    import sqlite3
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    ok = t_audit_shape(con)

    print("\n=== 真实库审计 ===")
    a = audit(con)
    print("  chunks 总数=%s  降级=%s  比例=%.1f%%"
          % (a["total"], a["degraded"], a["ratio"] * 100))
    if a["docs"]:
        print("  受影响文档（降级块数 / 总块数）：")
        for d in a["docs"][:15]:
            print("    #%-5s %-44s %s/%s" % (d["doc_id"], str(d["filename"])[:44],
                                             d["degraded"], d["total"]))
        if len(a["docs"]) > 15:
            print("    ... 另有 %d 篇" % (len(a["docs"]) - 15))
    else:
        print("  ✅ 无降级 chunk")

    # 反证：provider 当前是否真的不可用（否则"降级"可能是别的原因）
    try:
        from knowledge_pipeline.embedder import Embedder
        ed = Embedder(con)
        print("\n  provider 可用=%s  熔断中=%s"
              % (bool(ed._api), Embedder.quota_blocked()))
        if Embedder.LAST_QUOTA_ERROR:
            print("  供应商原话：%s" % Embedder.LAST_QUOTA_ERROR[:160])
    except Exception as e:
        print("  provider 探测失败：%s" % str(e)[:100])

    if a["ratio"] > 0.5:
        print("\n  ⚠️ 降级比例 > 50%%：语义检索已主要依赖词面匹配，"
              "建议向用户显式提示（`/api/jobs/stats` 或文档列表页）。")

    if args.export and a["docs"]:
        path = args.export if os.path.isabs(args.export) else os.path.join(ROOT, args.export)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"audit": {k: v for k, v in a.items() if k != "docs"},
                       "targets": a["docs"]}, f, ensure_ascii=False, indent=1)
        print("\n  已导出 %d 篇到 %s" % (len(a["docs"]), path))

    if args.reembed and a["docs"]:
        print("\n=== 重算（真调 API）===")
        ok_re, fail_re = 0, 0
        for d in a["docs"]:
            con2 = sqlite3.connect(DB_PATH)
            con2.row_factory = sqlite3.Row
            try:
                from knowledge_pipeline.search import reindex_document
                res = reindex_document(con2, int(d["doc_id"]))
                if res.get("parse_status") == "completed":
                    ok_re += 1
                    print("    ✅ #%s %s → %s 块 / %s"
                          % (d["doc_id"], str(d["filename"])[:30],
                             res.get("chunk_count"), res.get("embed_version")))
                else:
                    fail_re += 1
                    print("    ❌ #%s 失败：%s" % (d["doc_id"], res.get("error", "")[:80]))
            except Exception as e:
                fail_re += 1
                print("    ❌ #%s 异常：%s" % (d["doc_id"], str(e)[:100]))
            finally:
                con2.close()
        print("  重算完成：成功 %d / 失败 %d" % (ok_re, fail_re))
        ok &= _rec("R1 重算全部成功", fail_re == 0, "失败 %d" % fail_re)
    con.close()

    n_pass = sum(1 for r in _results if r[0] == PASS)
    print("\n断言：%d/%d 通过" % (n_pass, len(_results)))
    return 0 if n_pass == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())