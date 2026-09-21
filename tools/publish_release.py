"""dev → release 发布：修正 dev/main 文档元数据 + 快照复制到 release。

⚠️ **历史脚本，现已失效（保留仅作溯源）**：
1. 它操作的 `branch='dev/main'` 已被 `database/migrations/rebuild.py` 改名为 `dev`，
   故 `WHERE branch='dev/main'` 选不中任何行；
2. 文档已全局化——`ingest_document` 默认写 `branch='global'`，迁移把存量统一为 'global'，
   且明确声明「**发布机制不再复制文档快照**（实体/关系仍按分支）」。
   → 第 2 步 `snapshot_documents` 恒为 0（见 `branch_repo.snapshot_documents` 的废弃说明）。
即：**文档无需也没法按分支发布**；发布的对象是实体/关系（走 MR + 分支合并）。

用法（在 mbse_system 目录下）：
    python tools/publish_release.py --dry-run   # 预览（默认）
    python tools/publish_release.py             # 修正元数据并发布

行为：
1. fix_metadata：重新提取 dev/main 文档 title（清除污染值），补齐 author/version/tags/extra
2. snapshot_documents('dev/main', 'release')：复制 documents(含 domain)/doc_metadata/chunks
   （release 旧快照同文件名先删后插；chunks 带 hyde/domain 全字段）
3. 验证：release 文档数 / metadata 完整性 / embed_version / 检索 smoke
"""
import argparse
import json
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.config import DB_PATH

# domain → 建议 tags
DOMAIN_TAGS = {
    "sysml_norm": ["SysML", "规范"],
    "thermal_mgmt": ["热管理"],
    "satellite_comms": ["卫星通信"],
}
DEFAULT_AUTHOR = "王工"

_code_re = re.compile(r"^(package\s+.*\{|part\s+def\s+.*\{|requirement\s+.*\{)")


def _clean_title(raw: str, filename: str) -> str:
    """清洗提取的 title：去 markdown 标题前缀、代码痕迹；空/过长回退文件名。"""
    if not raw:
        return os.path.splitext(filename)[0]
    t = re.sub(r"^#+\s*", "", raw).strip()
    t = _code_re.sub("", t).strip(" '{}").strip()
    if not t or len(t) > 60:
        t = os.path.splitext(filename)[0]
    return t[:80]


def fix_metadata(conn) -> int:
    """修正 dev/main 文档的 doc_metadata（title 重新提取 + 补齐字段）。返回更新数。"""
    from knowledge_pipeline import extract_text, extract_doc_title
    from repositories.base import BaseRepo

    n = 0
    for r in conn.execute("SELECT id, filename, domain FROM documents WHERE branch='dev/main'").fetchall():
        # 源文件
        safe = re.sub(r"[^\w.\-\u4e00-\u9fa5]", "_", r["filename"]) or "doc"
        path = os.path.join(BASE, "data", "uploads", f"{r['id']}_{safe}")
        title = ""
        if os.path.exists(path):
            try:
                t = extract_text(r["filename"], open(path, "rb").read())
                title = _clean_title(extract_doc_title(r["filename"], t), r["filename"])
            except Exception:
                pass
        if not title:
            title = os.path.splitext(r["filename"])[0]

        md = conn.execute("SELECT * FROM doc_metadata WHERE document_id=?", (r["id"],)).fetchone()
        old_extra = {}
        if md:
            try:
                old_extra = json.loads(md["extra"] or "{}")
            except Exception:
                old_extra = {}
        old_extra["domain"] = r["domain"] or "unknown"
        old_extra["domain_label"] = DOMAIN_TAGS.get(r["domain"] or "", [r["domain"]])[0] if r["domain"] else ""

        tags = DOMAIN_TAGS.get(r["domain"] or "", [])
        conn.execute(
            "INSERT INTO doc_metadata (document_id, title, author, version, tags, source, extra) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(document_id) DO UPDATE SET title=excluded.title, author=excluded.author, "
            "version=excluded.version, tags=excluded.tags, extra=excluded.extra",
            (r["id"], title, DEFAULT_AUTHOR, "v1.0", json.dumps(tags, ensure_ascii=False),
             "upload", json.dumps(old_extra, ensure_ascii=False)))
        n += 1
    conn.commit()
    return n


def publish(conn) -> dict:
    """发布（建模数据）。文档全局化：文件管理为全局资产（branch='global'），
    不再做 dev/main → release 文档快照复制（发布仅针对实体/关系等建模数据）。"""
    return {"moved": 0,
            "release_docs": conn.execute(
                "SELECT COUNT(*) FROM documents WHERE branch='global'").fetchone()[0]}


def verify(conn) -> None:
    """发布后验证：metadata 完整 / embed_version / 检索 smoke。"""
    rows = conn.execute("SELECT * FROM documents WHERE branch='release' ORDER BY id").fetchall()
    print(f"release 文档数: {len(rows)}")
    bad = 0
    for r in rows:
        md = conn.execute("SELECT * FROM doc_metadata WHERE document_id=?", (r["id"],)).fetchone()
        m = dict(md) if md else {}
        title_ok = bool((m.get("title") or "").strip())
        ver_ok = bool((m.get("version") or "").strip())
        if not title_ok or not ver_ok:
            bad += 1
        ev = conn.execute(
            "SELECT DISTINCT embed_version FROM document_chunks WHERE document_id=?",
            (r["id"],)).fetchall()
        evs = [x[0] for x in ev]
        print(f"  {r['id']:>3} | {r['filename'][:30]:<32} | domain={r['domain'] or '-':<12} "
              f"| title={'Y' if title_ok else 'N'} ver={'Y' if ver_ok else 'N'} | chunks={r['chunk_count']} embed={','.join(evs)}")
    print(f"metadata 缺失: {bad}/{len(rows)}")
    # 检索 smoke（release 分支限定）
    try:
        from knowledge_engine import hybrid_search
        hy = hybrid_search(conn, "热管理 温度 控制", top_k=3, branches=["release"])
        print(f"release 检索: hits={len(hy['hits'])} vec={hy['vec_count']} bm25={hy['bm25_count']}")
    except Exception as e:
        print("release 检索 smoke 失败:", e)


def main():
    ap = argparse.ArgumentParser(description="dev → release 发布")
    ap.add_argument("--dry-run", action="store_true", help="只预览不改库（默认）")
    ap.add_argument("--skip-fix", action="store_true", help="跳过 dev 元数据修正")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    dev = conn.execute("SELECT id, filename, domain FROM documents WHERE branch='dev/main'").fetchall()
    rel = conn.execute("SELECT id, filename FROM documents WHERE branch='release'").fetchall()
    print(f"dev/main 文档 {len(dev)} 个 → release 现有快照 {len(rel)} 个")
    if args.dry_run:
        print("将执行：")
        print("  1. 修正 dev/main 文档 title/author/tags（重新提取标题）")
        print("  2. snapshot_documents('dev/main','release') 覆盖发布（含 domain/domain_confidence/hyde 字段）")
        print("确认后运行：python tools/publish_release.py")
        conn.close()
        return

    if not args.skip_fix:
        n = fix_metadata(conn)
        print(f"[1/2] dev 元数据修正 {n} 个文档")
    else:
        print("[1/2] 跳过 dev 元数据修正")

    p = publish(conn)
    print(f"[2/2] 发布完成：复制 {p['moved']} 个文档，release 现有 {p['release_docs']} 个")

    verify(conn)
    conn.close()


if __name__ == "__main__":
    main()
