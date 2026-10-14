"""重建存量分块的 `hyde_questions`（L1 落地配套）。

══════════════════════════════════════════════════════════════════════
为什么需要它
══════════════════════════════════════════════════════════════════════
`knowledge_pipeline/search.py::reindex_document` 只**重算向量**，
它`json.loads(c["hyde_questions"])` 读旧值 ⇒ **不会重新生成问句**。
而问句是本轮修复的标的（真库实测）：
  · 16436 条问题，**重复率 60.4%**（唯一仅 6516）
  · **1261 种带原始 `##`/`#` 标记**（如 `什么是## 附录 C：标准库…？`）
  · `什么是1.2.1.2.3 子系统架构设计？` ×169（编号未剥）
⇒ 只开 `hyde_heading_enabled` 对**存量**无效，**必须重建**。

══════════════════════════════════════════════════════════════════════
分两阶段（成本差别巨大，实测）
══════════════════════════════════════════════════════════════════════
实测规模：分块 6727条，**带 hyde_embedding 的只有 270 条**
（embed 成本闸`ingest.hyde_embed_enabled` 曾默认关闭）。
  · 阶段 1 `--stage questions`：只重写文本，**零 embedding 调用**
    ⇒ 立刻让 BM25 检索侧受益（bm25_text 不变，但问题对问题匹配受益）
  · 阶段 2 `--stage vectors`：重算 `hyde_embedding`，**仅对已有向量的 270 条**
    ⇒ 810 次 embedding 调用，可控
默认只跑阶段 1；阶段 2 需显式指定（**会产生真实 embedding 费用**）。

⚠️ **不改 embedding / embed_version**：主向量不动
   ⇒ 阶段 1 完全零风险（不触发向量失效、不影响 L2 二进制缓存）。
"""
import argparse
import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
#: 默认真库。⚠️ **不要在 A/B 实验里直接用它** —— 改它会写生产数据。
#:   做实验请传 `--db <副本路径>`（用 sqlite3.Connection.backup() 复制真库）。
DB = os.path.join(_ROOT, "mbse.db")


def _clean_marker_noise(questions: list) -> tuple:
    """把已生成问句里的裸 `##` / `#` / 反引号去掉（存量数据的最小修复）。

    ⚠️ 与 `ingest._clean_section` 的分工：
       - `_clean_section` 是**输入端清洗**（防产生脏数据）
       - 这个是**存量修复**（脏数据已落库，需等重建才干净）⇒ 两者都要。
    """
    import re
    out, changed = [], 0
    for q in questions or []:
        s = str(q)
        # 只删 markdown 标记字符本身，保留文字与标点
        new = re.sub(r"[#`]+", "", s).strip()
        if new != s:
            changed += 1
        out.append(new)
    return out, changed


def rebuild_questions(dry_run: bool = False, db: str = None) -> dict:
    """阶段 1：只重写 hyde_questions（零 embedding 调用）。"""
    # ⚠️ 必须用**包内导入**（`from knowledge_pipeline.ingest import ...`）：
    #    `ingest.py` 内部有 `from .extract import ...` 相对导入，
    #    直接 `sys.path.insert(.../knowledge_pipeline); from ingest import` 会
    #    报 "attempted relative import with no known parent package"。
    from knowledge_pipeline.ingest import _clean_section, _generate_hyde_questions
    from knowledge_pipeline.chunking_deepdoc import enhanced_hyde_questions

    conn = sqlite3.connect(db or DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, content, section, hyde_questions FROM document_chunks "
        "WHERE content IS NOT NULL AND content != ''").fetchall()
    stats = {"total": len(rows), "regen": 0, "was_dirty": 0,
             "identical": 0, "would_change": 0}
    updates = []
    for r in rows:
        old = []
        try:
            old = json.loads(r["hyde_questions"] or "[]")
        except Exception:
            old = []
        # ① 优先用增强版（L1 逻辑，不看开关——本次是显式重建）
        new = enhanced_hyde_questions(r["content"] or "", r["section"] or "")
        if not new:
            # ② 无标题路径 ⇒ 用模板版 + **清洗后的 section**
            new = _generate_hyde_questions(r["content"] or "",
                                           _clean_section(r["section"] or ""))
        # ③ 兜底：若模板版仍带裸标记，做最小修复
        new, fixed = _clean_marker_noise(new)
        # ★ 统计口径：`was_dirty` 数的是**旧值**里的裸标记（真正被修掉的存量脏数据），
        #   不是"修复动作生效次数"——后者恒为 0（因为 ② 已用 _clean_section 消除了），
        #   统计它会让人误以为"什么都没修"。
        old_dirty = any("#" in str(x) or "`" in str(x) for x in (old or []))
        if old_dirty:
            stats["was_dirty"] += 1
        if not new:
            stats["identical"] += 1
            continue
        if new != old:
            stats["would_change"] += 1
            updates.append((json.dumps(new, ensure_ascii=False), r["id"]))
            if new and old and new[0] != old[0]:
                stats["regen"] += 1
    if not dry_run and updates:
        conn.executemany(
            "UPDATE document_chunks SET hyde_questions=? WHERE id=?", updates)
        conn.commit()
    conn.close()
    stats["applied"] = 0 if dry_run else len(updates)
    return stats


def rebuild_vectors(limit: int = 0, dry_run: bool = False, db: str = None) -> dict:
    """阶段 2：重算已有 hyde_embedding 的行（**会产生真实 embedding 费用**）。"""
    conn = sqlite3.connect(db or DB)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, hyde_questions FROM document_chunks "
           "WHERE hyde_embedding IS NOT NULL AND hyde_embedding != '' "
           "AND hyde_embedding != '[]'")
    args = ()
    if limit:
        sql += " LIMIT ?"
        args = (limit,)
    rows = conn.execute(sql, args).fetchall()
    texts = []
    for r in rows:
        try:
            qs = json.loads(r["hyde_questions"] or "[]")
        except Exception:
            qs = []
        texts.append("；".join(qs) if qs else "")
    conn.close()
    need = [(r["id"], t) for r, t in zip(rows, texts) if t.strip()]
    out = {"candidates": len(rows), "with_text": len(need), "applied": 0,
           "embed_version": None}
    if dry_run or not need:
        out["dry_run"] = dry_run
        return out
    from knowledge_pipeline.embedder import Embedder
    conn = sqlite3.connect(db or DB)
    conn.row_factory = sqlite3.Row
    try:
        emb = Embedder(conn)
        vecs, ver = emb.embed_with_version([t for _, t in need], batch_size=8)
        from vector_index import normalize_vector
        ups = [(json.dumps(normalize_vector(v), ensure_ascii=False), cid)
               for (cid, _), v in zip(need, vecs) if v]
        conn.executemany(
            "UPDATE document_chunks SET hyde_embedding=? WHERE id=?", ups)
        conn.commit()
        out["applied"] = len(ups)
        out["embed_version"] = ver
    finally:
        conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["questions", "vectors"], default="questions")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default="", help="目标库路径（默认真库mbse.db）。"
                        "★ A/B 实验必须指向 sqlite3.backup() 出来的副本，"
                        "否则会改生产数据")
    ap.add_argument("--yes-write-prod", action="store_true",
                    help="明确允许写真库（未给此参数且目标==真库则拒跑）")
    a = ap.parse_args()
    db = a.db or None
    # ── 防误伤：目标解析后等于真库、又没有显式确认 ⇒ 拒跑 ──────────────
    #   理由：这个脚本是**批量写**hyde_questions（6727 行），误跑一次就污染生产。
    #   「问一句再确认」在这里比「多一个参数」更可靠。
    target = os.path.abspath(db or DB)
    prod = os.path.abspath(DB)
    if target == prod and not a.dry_run and not a.yes_write_prod:
        print("⛔ 目标是**真库**且未加 --dry-run。")
        print("   这是批量写 %d 行的操作，A/B 实验请用副本："
              % sqlite3.connect(prod).execute(
                  "SELECT COUNT(*) FROM document_chunks").fetchone()[0])
        print("   sqlite3 连接真库 → .backup('tmp/xxx.db') → --db tmp/xxx.db")
        print("   确需写真库请显式加 --yes-write-prod（会留痕在终端输出）")
        return 2
    if db:
        print(f"目标库: {target}"
              + ("（副本）" if target != prod else "（★真库）"))
    print("=" * 70)
    print(f"hyde 存量重建 · stage={a.stage}"
          f"{'（dry-run，不写库）' if a.dry_run else ''}")
    print("=" * 70)
    if a.stage == "questions":
        s = rebuild_questions(dry_run=a.dry_run, db=db)
        print(f"分块总数 {s['total']}")
        print(f"  ★ 旧值含裸标记（脏）{s['was_dirty']}")
        print(f"  首问变化（真重建）  {s['regen']}")
        print(f"  待更新行            {s['would_change']}"
              f"　已写入 {s['applied'] if not a.dry_run else 0}")
        if a.dry_run and s["would_change"]:
            print("  ⇒ 去掉 --dry-run 即真正执行（★零 embedding 调用）")
    else:
        print("⚠️ 本阶段会产生**真实 embedding 调用费用**")
        s = rebuild_vectors(limit=a.limit, dry_run=a.dry_run, db=db)
        for k, v in s.items():
            print(f"  {k} = {v}")
        if s["with_text"] and not a.dry_run:
            print("  ⇒ 已重算 hyde_embedding")
    return 0


if __name__ == "__main__":
    sys.exit(main())