# -*- coding: utf-8 -*-
"""知识库卫生常驻门禁 —— 守「去重机制不失效」这件事本身。

## 为什么需要它（本轮实证的完整因果链）

真库实测 2026-11-16：
  `documents.content_sha256` **14/14 全空**
  ⇒ `dedup.check_duplicate` 的 same_content 判据失效（只按文件名判）
  ⇒ 文档被改名后重传（`1_800_SysML-v2-AI建模知识文档.md`）时文件名也不匹配
  ⇒ 重复文档放行：doc 800 与 doc 852 **逐字节相同**（270 chunk × 2）
  ⇒ `dedup.duplicate_groups` 看不见它（依赖非空 sha）
  ⇒ `tools/_dedupe_documents.py` 盘点报「重复组：0」
  ⇒ 2026-11-10 写的清理脚本"没东西可删"，**重复在库里躺了 7 天**
  ⇒ 检索时同一段话占两个槽位（`per_doc_max` 按 document_id 分别算配额）

三条既有门禁都守不住这个：`verify_upload_dedup.py` 测的是**上传路径的判重逻辑**
（逻辑对，但它假设 sha 有值）；另有新鲜度门禁只看评测报告是否停更。
⇒ 缺的正是「**存量数据卫生**」这一层。

## 判据

  H1 stored 文档的 content_sha256 必须非空（空 ⇒ 内容判据永久失效）
  H2 stored 文档之间不得存在内容重复（按 sha；sha 缺失时按 chunk 内容签名兜底）
  H3 生命周期一致性：deprecated 文档不得残留 stored 的 chunk（否则检索仍会命中）
  H4 反向对照：stored 文档必须有 chunk（防空库/误删导致的"看起来干净"）
无真库时全部 SKIP 并写明；H2 的兜底签名在 chunk 数大时会慢，用 --fast 可跳过
（默认开，因为这是本门禁的核心价值）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_kb_hygiene.py
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_kb_hygiene.py --fast
"""
import argparse
import hashlib
import os
import sqlite3
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
LIVE = "COALESCE(lifecycle_status,'stored')!='deprecated'"

PASS, FAIL, SKIP = [], [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("　— " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


# ── 判据单一真源（门禁与变异共用）──────────────────────────────────────
def empty_sha_docs(conn):
    return [r[0] for r in conn.execute(
        "SELECT id FROM documents WHERE " + LIVE + " "
        "AND COALESCE(content_sha256,'')='' ORDER BY id")]


def dup_sha_groups(conn):
    return [dict(zip(("sha", "n", "ids"), (r[0], r[1], r[2])))
            for r in conn.execute(
                "SELECT content_sha256, COUNT(*), GROUP_CONCAT(id) FROM documents "
                "WHERE " + LIVE + " AND COALESCE(content_sha256,'')<>'' "
                "GROUP BY content_sha256 HAVING COUNT(*)>1")]


def dup_by_chunk_sig(conn):
    """sha 缺失时的兜底：按 chunk 内容签名找重复（只对 stored 文档）。"""
    out = []
    docs = conn.execute("SELECT id FROM documents WHERE " + LIVE + " ORDER BY id").fetchall()
    sig = defaultdict(list)
    for (did,) in docs:
        h = hashlib.sha1()
        n = 0
        for (c,) in conn.execute("SELECT content FROM document_chunks "
                                 "WHERE document_id=? ORDER BY chunk_index", (did,)):
            h.update(((c or "") + "\x00").encode("utf-8"))
            n += 1
        if n:
            sig[h.hexdigest()[:16]].append(did)
    for s, ids in sig.items():
        if len(ids) > 1:
            out.append({"sig": s, "ids": ids})
    return out


def dangling_stored_chunks(conn):
    """deprecated 文档残留 stored chunk（检索侧仍会命中）。"""
    return [dict(zip(("doc", "n"), (r[0], r[1]))) for r in conn.execute(
        "SELECT document_id, COUNT(*) FROM document_chunks dc "
        "WHERE COALESCE(dc.lifecycle_status,'stored')='stored' "
        "AND EXISTS (SELECT 1 FROM documents d WHERE d.id=dc.document_id "
        "AND COALESCE(d.lifecycle_status,'stored')='deprecated') "
        "GROUP BY document_id")]


def empty_live_docs(conn):
    return [r[0] for r in conn.execute(
        "SELECT d.id FROM documents d WHERE " + LIVE + " AND NOT EXISTS "
        "(SELECT 1 FROM document_chunks dc WHERE dc.document_id=d.id "
        "AND COALESCE(dc.lifecycle_status,'stored')='stored')")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过 H2 的 chunk 签名兜底")
    args = ap.parse_args()

    print("=" * 78)
    print("知识库卫生门禁（去重机制不得失效）")
    print("=" * 78)

    has_db = os.path.isfile(DB) and os.path.getsize(DB) > 0
    if not has_db:
        for n in ("H1", "H2", "H3", "H4"):
            skip(n, "无 mbse.db（干净 checkout）")
        return report()

    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        n_live = conn.execute("SELECT COUNT(*) FROM documents WHERE " + LIVE).fetchone()[0]
        print("  口径：非下架文档 %d 个" % n_live)

        e = empty_sha_docs(conn)
        chk("H1 非下架文档的 content_sha256 必须非空（空 ⇒ 内容判据永久失效）",
            not e, "空 sha 的文档 id=%s" % e)

        d = dup_sha_groups(conn)
        chk("H2a 非下架文档之间不得有相同 sha（真重复）", not d,
            "重复组=%s" % [(x["sha"][:12], x["ids"]) for x in d])

        if args.fast:
            skip("H2b chunk 内容签名兜底", "--fast 跳过")
        else:
            s = dup_by_chunk_sig(conn)
            chk("H2b 内容签名兜底也不得发现重复（防 sha 缺失时漏检）", not s,
                "重复=%s" % s)

        f = dangling_stored_chunks(conn)
        chk("H3 deprecated 文档不得残留 stored chunk（否则检索仍会命中）", not f,
            "残留=%s" % f)

        z = empty_live_docs(conn)
        chk("H4 非下架文档必须有 stored chunk（防空库/误删伪干净）", not z,
            "无 chunk 的文档 id=%s" % z)

        # ── 变异自证（内存库，零 IO）──────────────────────────────────
        print("\n  --- 变异自证 ---")
        mem = sqlite3.connect(":memory:")
        mem.executescript("""
            CREATE TABLE documents (id INTEGER PRIMARY KEY, content_sha256 TEXT,
                                    lifecycle_status TEXT);
            CREATE TABLE document_chunks (document_id INTEGER, chunk_index INTEGER,
                                          content TEXT, lifecycle_status TEXT);
        """)
        mem.executemany("INSERT INTO documents VALUES (?,?,?)",
                        [(1, "AAA", "stored"), (2, "BBB", "stored")])
        mem.executemany("INSERT INTO document_chunks VALUES (?,?,?,?)",
                        [(1, 0, "x", "stored"), (2, 0, "y", "stored")])
        mem.commit()
        chk("M1 干净库 ⇒ 四条判据全绿（防恒红）",
            not empty_sha_docs(mem) and not dup_sha_groups(mem)
            and not dangling_stored_chunks(mem) and not empty_live_docs(mem)
            and not dup_by_chunk_sig(mem))
        mem.execute("UPDATE documents SET content_sha256='' WHERE id=2")
        chk("M2 变异：sha 置空 ⇒ H1 判红（这正是本轮的真实缺陷）",
            bool(empty_sha_docs(mem)))
        mem.execute("UPDATE documents SET content_sha256='AAA' WHERE id=2")
        chk("M3 变异：两文档同 sha ⇒ H2a 判红", bool(dup_sha_groups(mem)))
        mem.execute("UPDATE documents SET content_sha256='' WHERE id=2")
        mem.execute("UPDATE document_chunks SET content='x' WHERE document_id=2")
        chk("M4 变异：sha 缺失但内容相同 ⇒ H2b 兜底必须抓到（证明兜底有牙齿）",
            bool(dup_by_chunk_sig(mem)))
        mem.execute("UPDATE document_chunks SET content='y' WHERE document_id=2")
        mem.execute("UPDATE documents SET content_sha256='BBB', "
                    "lifecycle_status='deprecated' WHERE id=2")
        chk("M5 变异：文档下架但 chunk 仍 stored ⇒ H3 判红",
            bool(dangling_stored_chunks(mem)))
        mem.execute("UPDATE document_chunks SET lifecycle_status='deprecated' "
                    "WHERE document_id=2")
        chk("M6 变异：下架文档的 chunk 已同步 ⇒ H3 转绿", not dangling_stored_chunks(mem))
        mem.execute("DELETE FROM document_chunks WHERE document_id=1")
        chk("M7 变异：在役文档无 chunk ⇒ H4 判红", bool(empty_live_docs(mem)))
        mem.close()
    finally:
        conn.close()
    return report()


def report():
    print("\n" + "=" * 78)
    print("PASS %d / FAIL %d / SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("红项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
