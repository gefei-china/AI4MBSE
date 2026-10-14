# -*- coding: utf-8 -*-
"""门禁：BM25 索引必须包含 bm25_text（不能退回content）。

⚠️ 背景（2026-10-14 实测缺陷，一行字段遗漏）：
   `_CHUNK_COLS` 漏了 bm25_text ⇒ `BM25Engine.extend()` 的
   `c.get("bm25_text") or c.get("content")` 永远拿到 None
   ⇒ BM25 索引的是 content（**不含所属章节标题**），而不是 bm25_text。
   实测 24 例 doc 域：
     索引 content   → BM25-only R@1 = 0%（'4.1 说明性对象配置' top-20 一次不出现）
     索引 bm25_text → BM25-only R@1 = 95.8%
   ⇒ 回归会让召回**悄悄退回原样**且不报错。

本门禁三条判据：
  A. `_CHUNK_COLS` 含 bm25_text（静态）
  B. 真实库走 `_bm25_cache_get()` 拿到的行**真的带 bm25_text**（行为）
  C. 用 section 标题作 query 能召回该 section（端到端语义）
     —— 只查"字段存在"不够：字段存在但引擎不读它，同样是缺陷。
"""
import importlib.util
import os
import sqlite3
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + detail) if (detail and not cond) else ""))


def main():
    import knowledge_engine as ke

    print("=== A. 静态：字段清单 ===")
    ck("A1 _CHUNK_COLS 含 bm25_text", "bm25_text" in ke._CHUNK_COLS,
       ke._CHUNK_COLS)
    ck("A2 仍保留 content（兜底路径不可删）", "content" in ke._CHUNK_COLS)

    print("\n=== B. 行为：真实索引真的带该字段 ===")
    db_path = os.path.join(_ROOT, "mbse.db")
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    ke._BM25_CACHE.clear()
    eng, rows = ke._bm25_cache_get(conn, None, None, None, False)
    ck("B1 索引行里有 bm25_text 键", rows and "bm25_text" in rows[0],
       str(sorted(rows[0].keys())) if rows else "no rows")
    ck("B2 bm25_text 有非空值（不是全 NULL）",
       rows and sum(1 for r in rows if (r.get("bm25_text") or "").strip()) > len(rows) * 0.5,
       "非空 %d / %d" % (sum(1 for r in rows if (r.get('bm25_text') or '').strip()), len(rows)))

    print("\n=== C. 端到端：标题类 query 能召回所属 section ===")
    # ⚠️ 必须挑一个**标题词不出现在自身正文里**的 section。
    #   否则即使退回「索引 content」，正文里恰好含该词照样能命中 ⇒ 变异判据抓不到（实测踩过：
    #   '场景知识覆盖检查' 那条D3 FAIL 就是这个原因—— 它在 content 里也出现）。
    #   判据：section 标题的核心词，在该 section **所有块的 content 里都不出现**，
    #   但在 bm25_text 里出现（因为 bm25_text = section + content）。
    import re
    cand = None
    for r in conn.execute(
            "SELECT source_doc, section FROM document_chunks "
            "WHERE section LIKE '###%' AND content!='' AND bm25_text!='' "
            "GROUP BY source_doc, section HAVING COUNT(*)>=3"):
        title = (r["section"] or "").lstrip("# ").strip()
        core = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title).strip()
        if len(core) < 4:
            continue
        # 取标题里较长的那个连续中文片段做锚（≥3字）
        m = re.search(r"[\u4e00-\u9fff]{3,}", core)
        if not m:
            continue
        anchor = m.group(0)
        n_content = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE source_doc=? AND section=? "
            "AND content LIKE ?", (r["source_doc"], r["section"], "%" + anchor + "%")
        ).fetchone()[0]
        n_total = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE source_doc=? AND section=?",
            (r["source_doc"], r["section"])).fetchone()[0]
        if n_content == 0 and n_total >= 3:      # ★ 正文里一次都不出现
            cand = (r["source_doc"], r["section"], anchor, n_total)
            break
    if not cand:
        ck("C0 找到『标题词不在正文里』的样本 section", False,
           "全库无此样本 ⇒ C 组无法自证（这条本身也要知道）")
        return 1
    doc, sec, core, n_gold = cand
    print("     样本：section=%r 锚词=%r（该 section %d 块，正文均不含锚词）"
          % (sec[:34], anchor, n_gold))
    gids = {r["id"] for r in conn.execute(
        "SELECT id FROM document_chunks WHERE source_doc=? AND section=?", (doc, sec))}
    hits = eng.search(anchor, top_k=10)
    pos = [i + 1 for i, h in enumerate(hits) if h["chunk_id"] in gids]
    ck("C1 用标题锚词检索能召回本 section 的块（top-10）", bool(pos),
       "query=%r 无任何 gold 进入 top-10" % anchor)
    ck("C2 命中位置靠前（top-3 内）", bool(pos) and pos[0] <= 3, "pos=%s" % pos)
    print("     query=%r -> 位置 %s" % (anchor, pos))
    conn.close()

    print("\n=== D. 变异自证（判据必须有牙齿）===")
    # D1：把 bm25_text 从字段清单拿掉 ⇒ A1/B1/C1 必须 FAIL
    orig = ke._CHUNK_COLS
    importlib.reload  # noqa
    try:
        # 直接改模块变量并清缓存，模拟"字段被漏掉"的缺陷态
        ke._CHUNK_COLS = orig.replace("bm25_text, ", "").replace(", bm25_text", "")
        ke._BM25_CACHE.clear()
        conn2 = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        conn2.row_factory = sqlite3.Row
        eng2, rows2 = ke._bm25_cache_get(conn2, None, None, None, False)
        a1_fail = "bm25_text" not in ke._CHUNK_COLS
        b1_fail = not (rows2 and "bm25_text" in rows2[0])
        hits2 = eng2.search(core, top_k=10)
        c1_fail = not [i for i, h in enumerate(hits2) if h["chunk_id"] in gids]
        conn2.close()
        ck("D1 变异(漏掉 bm25_text) ⇒ A1 判据会 FAIL", a1_fail)
        ck("D2 变异 ⇒ B1 判据会 FAIL", b1_fail)
        ck("D3 变异 ⇒ C1/C2 判据会 FAIL（召回退回原样）", c1_fail)
    finally:
        ke._CHUNK_COLS = orig
        ke._BM25_CACHE.clear()

    # 复跑基线，确认还原成功
    ke._BM25_CACHE.clear()
    conn3 = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    conn3.row_factory = sqlite3.Row        #★ 必须设，否则 dict(r) 抛 TypeError
    eng3, rows3 = ke._bm25_cache_get(conn3, None, None, None, False)
    ck("D4 还原后基线复跑通过（字段回来了）",
       bool(rows3) and "bm25_text" in rows3[0] and
       bool([i for i, h in enumerate(eng3.search(core, top_k=10))
             if h["chunk_id"] in gids]))
    conn3.close()

    print("\n%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main() or 0)