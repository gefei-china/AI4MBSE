# -*- coding: utf-8 -*-
"""只重算**降级向量**的 chunk（`embed_version='bigram-tf'`），不碰已正常的真向量。

## 为什么不直接用 `tools/reembed_chunks.py`

那个脚本是**全量重嵌**（`WHERE content != ''`，6459 条），设计目标是"换模型后统一重算"。
本次场景不同：库里只有 **720 条**是降级态向量，其余 **6459 条已经是真向量**
（`embed_version='openai-compat'`）。全量重嵌会：

  1. 白花 10 倍 API 调用（6459 条 × 平均 400 字）；
  2. **无谓改动已正确的数据** —— 真向量重算一遍，分数分布会轻微漂移，
     让"重算降级块"这个动作的效果**无法归因**（改了 6459 条，怎么知道提升来自哪 720 条）。

## 为什么要单独做这件事（不是洁癖）

检索层**只与同 `embed_version` 的 chunk 比较**（`knowledge_pipeline/search.py:125`
"防 bigram 与真向量维度混合"）。所以这 720 条：

  · 向量维度/口径与其他 6459 条不同源 ⇒ 在跨文档比较时**系统性偏低**
  · 实测受影响文档：ISO 29148（610 条**全部**降级）、技术要求书两份副本（各 55 条**全部**降级）
  · 后果：这三份文档在语义检索里基本是**哑的** —— 用户问 ISO 29148 的条款，
    召回靠 BM25 字面碰运气（实测 `4.2.2 R11 – SEPARATE CLAUSES` 在 `query_trace` 里是零命中）

## 安全约束

- **默认 dry-run**：只统计与试算，不写库；`--apply` 才真正写。
- **逐批校验版本**：`embed_with_version` 返回 `"bigram-tf"` 说明 API 又挂了
  ⇒ **立即停止且不写**（写坏向量比不写更糟：降级向量会永久留在库里且事后无法区分）。
- 每批 commit，可中断可重跑（幂等）。
- 只 UPDATE `embedding` 与 `embed_version` 两列，**不动 content / hyde_embedding**。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/reembed_degraded.py            # dry-run
  ./.venv/Scripts/python.exe -X utf8 tools/reembed_degraded.py --apply    # 真写
"""
import os
import sys
import json
import time
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import sqlite3

DEGRADED_MARKS = ("bigram-tf", "bigram")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    db = os.path.join(_ROOT, "mbse.db")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    print("=" * 80)
    print("降级向量定向重算%s" % ("（APPLY 真实写入）" if args.apply else "（DRY-RUN 不写库）"))
    print("=" * 80)

    # ① 现状
    rows = con.execute(
        "SELECT embed_version, COUNT(*) c FROM document_chunks GROUP BY embed_version"
    ).fetchall()
    print("\n[1] 当前向量版本分布")
    for r in rows:
        print("   %-16s %d" % (r["embed_version"], r["c"]))

    marks = ",".join("?" * len(DEGRADED_MARKS))
    targets = con.execute(
        "SELECT id, document_id, chunk_index, LENGTH(content) lc FROM document_chunks "
        f"WHERE embed_version IN ({marks}) AND content != '' ORDER BY id",
        DEGRADED_MARKS).fetchall()
    print("\n[2] 待重算 %d 条" % len(targets))
    if not targets:
        print("   无需重算，退出")
        return 0
    bydoc = {}
    for t in targets:
        bydoc.setdefault(t["document_id"], []).append(t)
    for did, lst in sorted(bydoc.items()):
        name = con.execute("SELECT filename FROM documents WHERE id=?", (did,)).fetchone()
        print("   doc=%-5s %-56s %d 条"
              % (did, ((name["filename"] if name else "") or "")[:54], len(lst)))

    if not args.apply:
        # dry-run 也要验证 API 真能用，否则"看起来能跑"没意义
        from database import get_db
        from knowledge_pipeline import Embedder
        emb = Embedder(get_db())
        sample_ids = [t["id"] for t in targets[:3]]
        ph = ",".join("?" * len(sample_ids))
        sample_texts = [r["content"] for r in con.execute(
            f"SELECT content FROM document_chunks WHERE id IN ({ph})", sample_ids)]
        try:
            vecs, version = emb.embed_with_version(sample_texts, batch_size=0)
        except Exception as e:  # noqa: BLE001
            print("\n[3] API 自检失败：%s" % str(e)[:120])
            return 1
        print("\n[3] API 自检：version=%s，试算 %d 条 → 向量维度 %s"
              % (version, len(vecs), len(vecs[0]) if vecs else "?"))
        if version in DEGRADED_MARKS:
            print("   ⚠️ 仍返回降级版本 ⇒ 真跑会写坏向量，已阻止")
            return 1
        print("\n   DRY-RUN 结束：加 --apply 实际写入")
        return 0

    # ② 真跑
    from database import get_db
    from knowledge_pipeline import Embedder
    emb = Embedder(get_db())

    t0 = time.time()
    done = 0
    failed = 0
    for i in range(0, len(targets), args.batch):
        batch = targets[i:i + args.batch]
        ids = [b["id"] for b in batch]
        ph = ",".join("?" * len(ids))
        texts = [r["content"] for r in con.execute(
            f"SELECT content FROM document_chunks WHERE id IN ({ph})", ids)]
        try:
            vecs, version = emb.embed_with_version(texts, batch_size=0)
        except Exception as e:  # noqa: BLE001
            print("   [中断] 批 %d embedding 异常：%s" % (i // args.batch + 1, str(e)[:100]))
            failed += len(batch)
            break
        if version in DEGRADED_MARKS:
            print("   [中断] 批 %d 返回降级版本 %r —— API 异常，停止以免写坏向量"
                  % (i // args.batch + 1, version))
            failed += len(batch)
            break
        if len(vecs) != len(texts):
            print("   [中断] 批 %d 向量数 %d != 文本数 %d" % (i // args.batch + 1, len(vecs), len(texts)))
            failed += len(batch)
            break
        for cid, v in zip(ids, vecs):
            con.execute("UPDATE document_chunks SET embedding=?, embed_version=? WHERE id=?",
                        (json.dumps(v), version, cid))
        con.commit()
        done += len(batch)
        print("   进度 %d/%d（%d%%）| %.1fs"
              % (done, len(targets), done * 100 // len(targets), time.time() - t0), flush=True)

    con.close()

    # ★★ 2026-11-02：**必须使向量缓存失效**，否则本轮重算等于没做。
    #   实测事故：本脚本报"成功 720 条"，但磁盘 .npy 缓存仍是旧的 6457 行
    #   （数据库 7179 行，差 722 = 被重算的块）⇒ 这些块的余弦分恒为 0，
    #   检索把相似度最高的块排在第 6 位。当时误判"重算不是 hit@5 的主因"。
    #   为什么用 bump_epoch 而不是 drop_disk_cache：本环境 os.remove 被 safe-delete
    #   拦截，drop_disk_cache() 会静默返回 0（"删不掉就算了"）⇒ 依赖删除不可靠。
    bumped = ""
    try:
        from vector_index import bump_epoch
        bumped = bump_epoch()
    except Exception as e:  # noqa: BLE001
        print("   ⚠️ 缓存纪元刷新失败（检索可能仍用旧向量）：%s" % e)
    # 自检：缓存行数必须与数据库一致，否则"重算"是假的
    try:
        import sqlite3
        from vector_index import ChunkVectorIndex, _cache_paths
        import numpy as _np
        c2 = sqlite3.connect(db)
        n_db = c2.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE content != '' "
            "AND embedding != '[]' AND embed_version='openai-compat'").fetchone()[0]
        c2.close()
        mp = _cache_paths("openai-compat", 1024)[1]
        if os.path.exists(mp):
            print("   缓存已刷新：epoch=%s，缓存 %d 行 / 数据库 %d 行 %s"
                  % (bumped, len(_np.load(mp)), n_db,
                     "✅" if len(_np.load(mp)) == n_db else "❌ 不一致"))
        else:
            print("   缓存已标脏（epoch=%s），下次检索将从数据库重建（约 2.4s）" % bumped)
    except Exception:
        pass

    print("\n[4] 完成：重算 %d 条，失败 %d 条，耗时 %.1fs"
          % (done, failed, time.time() - t0))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
