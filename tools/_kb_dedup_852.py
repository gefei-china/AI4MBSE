# -*- coding: utf-8 -*-
"""软删除重复文档 doc 852（与 doc 800 逐字节相同，仅 1 次引用 vs 800 的 23 次）。

为什么不用 `tools/_dedupe_documents.py --apply`：
  它的 `same_content` 保留策略是「保留**最新**一份」⇒ 会删 800、保 852。
  但 800 被历史 citations 引用 **23 次**（消息 4762/5685/5725/5727/5729/5733/5889/5928…），
  852 只被引用 **1 次**（msg 6674）。删 800 会让那 23 条历史引用的来源链接指向一个
  被删文档 ⇒ **修一个显示问题，制造 23 个断链**。
  ⇒ 正确判据不是「最新」，而是「**被引用最多**；并列取最早（原始件）」。
  该缺陷已在 docs 留痕，避免下次再被 --apply 误伤（见 §16.4）。

为什么软删（deprecated）而不是真删：
  · 检索侧 `agent/rag.py:399/1275` 与图谱侧 `graph_provenance.py:120` 都过滤
    `lifecycle_status='deprecated'` ⇒ 软删后**检索完全不可见**，效果等同真删；
  · 但历史 citations 仍能打开（document 行还在）⇒ 不制造断链；
  · 单步可回滚（transition_lifecycle 支持 deprecated → committed 回退）。
  doc 848 就是按这个范式处理的（chunks 全部 deprecated）。

纪律：默认 dry-run；写库前 sqlite3.backup()；走仓库既有 MetaRepo.transition_lifecycle
（不手写 UPDATE，避免绕过状态机与审计日志）；留痕 + 回滚 SQL。
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
OUT = os.path.join(ROOT, "docs", "评测", "kb-dedup-852-20261116.json")
BAKDIR = os.path.join(ROOT, "backups")
DROP_ID, KEEP_ID = 852, 800
OPERATOR = "kb-dedup-20261116"

PASS, FAIL = [], []


def rec(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print("  %s %-4s %s" % ("PASS" if cond else "FAIL", tag, detail))


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def cite_counts():
    """每个 document_id 被历史 citations 引用的次数（只读）。"""
    c = ro()
    out = {}
    for r in c.execute("SELECT card_data FROM messages "
                       "WHERE card_data LIKE '%\"citations\"%'"):
        try:
            cd = json.loads(r["card_data"] or "{}")
        except Exception:
            continue
        for x in (cd.get("citations") or []):
            d = x.get("document_id")
            if d:
                out[int(d)] = out.get(int(d), 0) + 1
    c.close()
    return out


def backup():
    os.makedirs(BAKDIR, exist_ok=True)
    dst = os.path.join(BAKDIR, "mbse.db.bak-before-kb-dedup-%s.db"
                       % datetime.now().strftime("%Y%m%d_%H%M%S"))
    src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    a, b = ro(), sqlite3.connect("file:%s?mode=ro" % dst.replace("\\", "/"), uri=True)
    na = a.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    nb = b.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    rows = {t: (a.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0],
                b.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0])
            for t in ("documents", "document_chunks", "messages")}
    a.close(), b.close()
    assert na == nb and all(x == y for x, y in rows.values())
    print("  备份 %s (%d B)  sqlite_master %d/%d  %s"
          % (os.path.basename(dst), os.path.getsize(dst), na, nb,
             {k: v[0] for k, v in rows.items()}))
    return dst


def judge(conn, drop_id, keep_id):
    """判据单一真源（dry-run / apply / 门禁共用）。返回违规列表。"""
    bad = []
    d = conn.execute("SELECT id,lifecycle_status,content_sha256 FROM documents WHERE id=?",
                     (drop_id,)).fetchone()
    k = conn.execute("SELECT id,lifecycle_status,content_sha256 FROM documents WHERE id=?",
                     (keep_id,)).fetchone()
    if not d or not k:
        return ["文档缺失：drop=%s keep=%s" % (bool(d), bool(k))]
    if (d["lifecycle_status"] or "") != "deprecated":
        bad.append("待删 doc %s 未被标记 deprecated（实为 %s）" % (drop_id, d["lifecycle_status"]))
    if (k["lifecycle_status"] or "") != "stored":
        bad.append("保留 doc %s 不应被动过（实为 %s）" % (keep_id, k["lifecycle_status"]))
    n_stored = conn.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE document_id=? "
        "AND COALESCE(lifecycle_status,'stored')='stored'", (drop_id,)).fetchone()[0]
    if n_stored:
        bad.append("待删 doc %s 仍有 %d 个 chunk 是 stored（检索仍会命中）"
                   % (drop_id, n_stored))
    n_keep = conn.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE document_id=? "
        "AND COALESCE(lifecycle_status,'stored')='stored'", (keep_id,)).fetchone()[0]
    if n_keep == 0:
        bad.append("保留 doc %s 的 chunk 全没了（误删）" % keep_id)
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    print("=" * 80)
    print("软删除重复文档 doc %s（保留 doc %s）[%s]"
          % (DROP_ID, KEEP_ID, "VERIFY" if args.verify else ("APPLY" if args.apply else "DRY-RUN")))
    print("=" * 80)

    if args.verify:
        c = ro()
        bad = judge(c, DROP_ID, KEEP_ID)
        rec("V1", not bad, "最终态: %s" % (bad or "852=deprecated 且 0 个 stored chunk；800=stored 且 chunk 齐全"))
        # 检索侧必须看不见
        n_hit = c.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id=? "
            "AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('deprecated'))",
            (DROP_ID,)).fetchone()[0]
        rec("V2", n_hit == 0, "检索侧可见 chunk = %d（应 0）" % n_hit)
        c.close()
        print("-" * 80)
        print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
        return 0 if not FAIL else 1

    c = ro()
    print("[S] 前置校验")
    cnt = cite_counts()
    d = c.execute("SELECT * FROM documents WHERE id=?", (DROP_ID,)).fetchone()
    k = c.execute("SELECT * FROM documents WHERE id=?", (KEEP_ID,)).fetchone()
    rec("S1", bool(d) and bool(k), "两文档都在")
    if not (d and k):
        print("APPLY 中止：文档缺失")
        return 1
    rec("S2", d["content_sha256"] == k["content_sha256"] and d["content_sha256"],
        "内容 sha 相同 = %s" % (d["content_sha256"] or "")[:16])
    rec("S3", cite_counts().get(KEEP_ID, 0) > cite_counts().get(DROP_ID, 0),
        "保留项引用更多（keep=%d > drop=%d）—— 这是选 800 而非 852 的唯一理由"
        % (cnt.get(KEEP_ID, 0), cnt.get(DROP_ID, 0)))
    rec("S4", (d["lifecycle_status"] or "") == "stored",
        "待删项目前状态 = %s" % d["lifecycle_status"])
    n_ch = c.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?",
                     (DROP_ID,)).fetchone()[0]
    rec("S5", n_ch > 0, "待删 doc 有 %d 个 chunk 将被同步 deprecate" % n_ch)

    # 变异自证（内存库，零 IO）
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, lifecycle_status TEXT, content_sha256 TEXT);
        CREATE TABLE document_chunks (document_id INTEGER, lifecycle_status TEXT);
    """)
    mem.execute("INSERT INTO documents VALUES (?,?,?)", (DROP_ID, "deprecated", "S"))
    mem.execute("INSERT INTO documents VALUES (?,?,?)", (KEEP_ID, "stored", "S"))
    for _ in range(3):
        mem.execute("INSERT INTO document_chunks VALUES (?,?)", (DROP_ID, "deprecated"))
        mem.execute("INSERT INTO document_chunks VALUES (?,?)", (KEEP_ID, "stored"))
    rec("M1", not judge(mem, DROP_ID, KEEP_ID), "变异① 正确清理后判据判绿（防恒红）")
    mem.execute("UPDATE document_chunks SET lifecycle_status='stored' WHERE document_id=?",
                (DROP_ID,))
    rec("M2", bool(judge(mem, DROP_ID, KEEP_ID)),
        "变异② 只改 documents 不同步 chunks ⇒ 必须判红（检索仍会命中）")
    mem.execute("UPDATE document_chunks SET lifecycle_status='deprecated' WHERE document_id=?",
                (DROP_ID,))
    mem.execute("UPDATE document_chunks SET lifecycle_status='deprecated' WHERE document_id=?",
                (KEEP_ID,))
    rec("M3", bool(judge(mem, DROP_ID, KEEP_ID)), "变异③ 误删保留项 chunk ⇒ 必须判红")
    mem.close()
    c.close()

    if not args.apply:
        print()
        print("-" * 80)
        print("DRY-RUN PASS %d / FAIL %d（零写入）" % (len(PASS), len(FAIL)))
        return 0 if not FAIL else 1

    if FAIL:
        print("\n!! 前置/变异有 FAIL，拒绝写库")
        return 1

    print()
    print("[W] 备份 + 走 MetaRepo 状态机")
    bak = backup()
    from repositories.meta_repo import MetaRepo
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        repo = MetaRepo(conn)
        res = repo.transition_lifecycle(
            DROP_ID, "stored", "deprecated", OPERATOR,
            reason="与 doc %d 内容逐字节相同（sha=%s）；保留引用更多的 doc %d"
                   % (KEEP_ID, (d["content_sha256"] or "")[:16], KEEP_ID),
            extra={"duplicate_of": KEEP_ID, "sha256": d["content_sha256"],
                   "kept_cites": cnt.get(KEEP_ID, 0), "dropped_cites": cnt.get(DROP_ID, 0)},
            chunk_sync=True)
        conn.commit()
        rec("W1", bool(res.get("ok")), "状态机转移 ok=%s %s" % (res.get("ok"),
            res.get("reason") or ""))
        bad = judge(conn, DROP_ID, KEEP_ID)
        rec("W2", not bad, "写后终态: %s" % (bad or "全部符合"))
        n_log = conn.execute("SELECT COUNT(*) FROM document_lifecycle_log "
                             "WHERE document_id=? AND operator=?", (DROP_ID, OPERATOR)).fetchone()[0]
        rec("W3", n_log >= 1, "审计日志 %d 行（状态机自动写）" % n_log)
        rec("W4", conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 14,
            "documents 行数未变（软删不删行）")
        n_keep = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id=? "
            "AND COALESCE(lifecycle_status,'stored')='stored'", (KEEP_ID,)).fetchone()[0]
        rec("W5", n_keep == 270, "保留 doc %d 的 270 个 chunk 全部完好（实得 %d）"
            % (KEEP_ID, n_keep))
    finally:
        conn.close()

    payload = {
        "schema": "kb/dedup-softdelete@1",
        "applied_at_utc": utc(),
        "backup": os.path.relpath(bak, ROOT).replace("\\", "/"),
        "dropped": {"id": DROP_ID, "filename": d["filename"],
                    "lifecycle_status": "stored -> deprecated",
                    "chunks": d["chunk_count"], "cites": cnt.get(DROP_ID, 0)},
        "kept": {"id": KEEP_ID, "filename": k["filename"],
                 "lifecycle_status": k["lifecycle_status"],
                 "chunks": k["chunk_count"], "cites": cnt.get(KEEP_ID, 0)},
        "sha256": d["content_sha256"],
        "why_keep_800_not_852": (
            "_dedupe_documents.py 的 same_content 策略是「保留最新」，会删 800 保 852；"
            "但 800 被历史 citations 引用 23 次、852 仅 1 次 ⇒ 删 800 会制造 23 条断链。"
            "正确判据是「被引用最多，并列取最早」"),
        "why_soft_delete": (
            "检索侧 rag.py:399/1275 与图谱侧 graph_provenance.py:120 均过滤 deprecated "
            "⇒ 软删后检索不可见（效果等同真删），但历史引用仍可打开且单步可回滚"),
        "rollback_sql": [
            "UPDATE documents SET lifecycle_status='stored', deprecated_at='', "
            "deprecated_by='', deprecate_reason='', lifecycle_version=lifecycle_version+1 "
            "WHERE id=%d;" % DROP_ID,
            "UPDATE document_chunks SET lifecycle_status='stored' WHERE document_id=%d;" % DROP_ID,
            "DELETE FROM document_lifecycle_log WHERE document_id=%d AND operator='%s';"
            % (DROP_ID, OPERATOR),
        ],
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
