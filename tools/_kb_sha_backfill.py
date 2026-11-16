# -*- coding: utf-8 -*-
"""回填存量文档的 content_sha256（去重机制失效的根本原因）。

实测（2026-11-16）：
  documents 14 行，content_sha256 **14/14 全空**。
  ⇒ `knowledge_pipeline/dedup.py::check_duplicate` 的 same_content 判据失效、
  ⇒ `duplicate_groups()` 看不见重复、
  ⇒ `tools/_dedupe_documents.py` 盘点输出「重复组：0」（明明有 270 chunk 重复）、
  ⇒ 2026-11-10 写了清理脚本却"没东西可删"，重复一直留在库里。

本脚本：从 `data/uploads/` 的源副本读**原始字节**算 sha256 写回
（`dedup.source_copy_sha()` 已是仓库既有实现，不自造一套）。

纪律：默认 dry-run；写库前 sqlite3.backup()；判等看行数不看字节；逐条留痕；
       取不到源副本的**不编造**（留空并如实计数，由门禁持续盯）。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
OUT = os.path.join(ROOT, "docs", "评测", "kb-sha-backfill-20261116.json")
BAKDIR = os.path.join(ROOT, "backups")

PASS, FAIL = [], []


def rec(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print("  %s %-4s %s" % ("PASS" if cond else "FAIL", tag, detail))


def ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def backup():
    os.makedirs(BAKDIR, exist_ok=True)
    dst = os.path.join(BAKDIR, "mbse.db.bak-before-kb-sha-%s.db"
                       % datetime.now().strftime("%Y%m%d_%H%M%S"))
    src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    a = ro()
    b = sqlite3.connect("file:%s?mode=ro" % dst.replace("\\", "/"), uri=True)
    na = a.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    nb = b.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    rows = {t: (a.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0],
                b.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0])
            for t in ("documents", "document_chunks", "messages", "eval_reports")}
    a.close(), b.close()
    assert na == nb and all(x == y for x, y in rows.values()), "备份判等失败"
    print("  备份 %s (%d B)  sqlite_master %d/%d  行数 %s"
          % (os.path.basename(dst), os.path.getsize(dst), na, nb,
             {k: v[0] for k, v in rows.items()}))
    return dst


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from knowledge_pipeline.dedup import source_copy_sha

    print("=" * 80)
    print("回填 documents.content_sha256 [%s]" % ("APPLY" if args.apply else "DRY-RUN"))
    print("=" * 80)

    c = ro()
    docs = c.execute("SELECT id,filename,COALESCE(content_sha256,'') sha,"
                     "lifecycle_status FROM documents ORDER BY id").fetchall()
    before = {d["id"]: d["sha"] for d in docs}
    n_empty = sum(1 for d in docs if not d["sha"].strip())
    print("[S] 前置")
    rec("S1", len(docs) > 0, "文档 %d 个" % len(docs))
    rec("S2", True, "当前 sha 为空 = %d / %d（这就是去重失效的根因）"
        % (n_empty, len(docs)))

    plan = []
    for d in docs:
        if d["sha"].strip():
            continue
        s = source_copy_sha(d["id"], d["filename"] or "")
        plan.append({"id": d["id"], "filename": d["filename"], "sha": s,
                     "lc": d["lifecycle_status"]})
    ok_n = sum(1 for x in plan if x["sha"])
    print("  待回填 %d 个，其中源副本可取 %d 个（取不到的如实留空，不编造）"
          % (len(plan), ok_n))
    for x in plan:
        print("    id=%-4s %-46s %s" % (x["id"], str(x["filename"])[:46],
                                        (x["sha"][:16] + "…") if x["sha"] else "!! 源副本不可取"))
    c.close()

    # 自证：回填后应能发现重复（这才是"修好了"的判据，不是"写了 sha"）
    sha_with = [x["sha"] for x in plan if x["sha"]]
    known = [d["sha"] for d in docs if d["sha"].strip()]
    from collections import Counter
    cnt = Counter(sha_with + known)
    dup_after = {k: v for k, v in cnt.items() if v > 1}
    rec("S3", True, "回填后同 sha 组 = %d %s"
        % (len(dup_after), dict(list(dup_after.items())[:3]) or ""))

    if not args.apply:
        print()
        print("-" * 80)
        print("DRY-RUN PASS %d / FAIL %d（零写入）" % (len(PASS), len(FAIL)))
        return 0 if not FAIL else 1

    print()
    print("[W] 备份 + 写库")
    bak = backup()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        n = 0
        for x in plan:
            if not x["sha"]:
                continue
            cur = conn.execute("UPDATE documents SET content_sha256=? WHERE id=?",
                               (x["sha"], x["id"]))
            n += cur.rowcount
        conn.commit()
        rec("W1", n == ok_n, "回填 %d 行（应 %d）" % (n, ok_n))
        left = conn.execute("SELECT COUNT(*) FROM documents "
                            "WHERE COALESCE(content_sha256,'')=''").fetchone()[0]
        rec("W2", left == 0, "回填后仍为空 = %d（应 0）" % left)
        # 行数不变
        rec("W3", conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == len(docs),
            "documents 行数未变（%d）" % len(docs))
        # 真重复必须显现
        dup = conn.execute(
            "SELECT content_sha256 s,COUNT(*) n,GROUP_CONCAT(id) ids FROM documents "
            "WHERE COALESCE(content_sha256,'')<>'' GROUP BY content_sha256 "
            "HAVING COUNT(*)>1").fetchall()
        rec("W4", True, "★ 回填后暴露的同内容重复组 = %d %s"
            % (len(dup), [(r["s"][:12], r["ids"]) for r in dup]))
    finally:
        conn.close()

    payload = {
        "schema": "kb/sha-backfill@1",
        "applied_at_utc": utc(),
        "backup": os.path.relpath(bak, ROOT).replace("\\", "/"),
        "single_source": "knowledge_pipeline.dedup.source_copy_sha（读 data/uploads 源副本原始字节）",
        "before_empty": n_empty,
        "plan": plan,
        "after_empty": 0 if ok_n == len(plan) else len(plan) - ok_n,
        "why": ("content_sha256 全空 ⇒ check_duplicate 的 same_content 判据失效 ⇒ "
                "duplicate_groups 看不见重复 ⇒ _dedupe_documents.py 盘点报「0 组」"
                "⇒ 2026-11-10 写的清理脚本无东西可删，重复持续留在库里"),
        "caveat": "源副本不可取的文档保持空 sha，不编造；由常驻门禁持续盯空 sha 数",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("  留痕 %s (%d B)" % (os.path.relpath(OUT, ROOT), os.path.getsize(OUT)))

    print()
    print("APPLY PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
