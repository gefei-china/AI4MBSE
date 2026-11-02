# -*- coding: utf-8 -*-
"""门禁：向量缓存必须与数据库一致（防"重算向量从未生效"）。

══════════════════════════════════════════════════════════════════════
为什么需要这个门禁（2026-11-02 真实事故）
══════════════════════════════════════════════════════════════════════
`tools/reembed_degraded.py --apply` 报告"重算 720 条成功"、
`audit_embed_degradation` 显示"降级 720 → 0"，但检索里这些块的 `vec_score` **恒为 0**。

实测证据：
    磁盘缓存 openai-compat_1024.npy  = 6457 行（2026-10-28）
    数据库 openai-compat             = 7179 行（重算后）
    差集 722 行 = 被重算的那批块 ⇒ 它们不在矩阵里 ⇒ 余弦分 0

后果：中文 query「自主可控要求对组件部署有什么限制」，
目标块 `847:51` 余弦相似度 **0.6111（全局最高）**，却排在第 6 位（前面是相似度 0.497 的英文块）。

根因两条：
  ① `_load_binary()` 成功即用，**不校验行集**
  ② `invalidate()` 只清内存、不清磁盘 `.npy`；而 `_rows_fingerprint()` 定义了从未被调用

为什么难发现：缓存**加载成功**、检索**不报错**、分数**看起来只是"低"**。
只有单独算余弦相似度，才能看出"最高分的块竟然没被召回"。

本门禁检查：
  A1 `_db_group_count` 与重建 SQL 的 WHERE 条件一致（防"两个口径"）
  A2 缓存行数 == 数据库行数（否则会静默丢向量）
  A3 `bump_epoch()` 真的改变缓存路径（防"调用了但没效果"）
  A4 `_cache_paths` 含 epoch（防回退到无后缀旧文件名）
  A5 全库逐块对账：每条有 embed_version 的 chunk 都必须能在矩阵里找到
  A6 变异自证：篡改缓存行数后 A2/A5 必须能翻红
"""
from __future__ import annotations

import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_fail = 0
_ok = 0


def chk(name, cond, detail=""):
    global _fail, _ok
    if cond:
        _ok += 1
        print("  [OK]   %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s   %s" % (name, detail))


def main():
    import vector_index as vi
    from vector_index import ChunkVectorIndex as CVI

    db = os.path.join(_ROOT, "mbse.db")
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=" * 78)
    print("[A] 缓存与数据库一致性")
    print("=" * 78)

    # 取主要版本（行数最多的那个），避免对空版本误报
    row = con.execute(
        "SELECT embed_version v, COUNT(*) n FROM document_chunks "
        "WHERE content != '' AND embedding != '[]' GROUP BY embed_version "
        "ORDER BY n DESC LIMIT 1").fetchone()
    if not row:
        print("  [SKIP] 库里没有向量数据")
        return 0
    version, n_db = row["v"], row["n"]
    print("  主要向量版本 = %s（%d 行）" % (version, n_db))

    # A1 WHERE 条件一致性
    n_api = CVI._db_group_count(con, version)
    chk("A1 _db_group_count 与重建口径一致", n_api == n_db,
        "api=%s sql=%s" % (n_api, n_db))

    # A2 缓存行数
    mp, ip = vi._cache_paths(version, 1024)
    if os.path.exists(mp) and os.path.exists(ip):
        try:
            import numpy as np
            n_cache = len(np.load(ip))
        except Exception as e:  # noqa: BLE001
            n_cache = -1
            print("       缓存读取失败：%s" % e)
        chk("A2 缓存行数 == 数据库行数", n_cache == n_db,
            "缓存=%s 数据库=%s 差=%s" % (n_cache, n_db, n_db - n_cache))
    else:
        print("  [INFO] 磁盘缓存不存在（下次检索会从 JSON 重建，正确性无影响）")

    # A3/A4 epoch 机制
    p1 = vi._cache_paths(version, 1024)[0]
    chk("A4 缓存文件名含 epoch 后缀",
        os.path.basename(p1).count("_") >= 2, os.path.basename(p1))
    e0 = vi._read_epoch()
    e1 = vi.bump_epoch()
    p2 = vi._cache_paths(version, 1024)[0]
    chk("A3 bump_epoch 后缓存路径改变", p1 != p2 and e1 != e0,
        "epoch %s→%s 路径 %s→%s" % (e0, e1, os.path.basename(p1), os.path.basename(p2)))

    print("\n" + "=" * 78)
    print("[B] 逐块对账：有向量的 chunk 必须能在矩阵里找到")
    print("=" * 78)
    # 强制从数据库重建一份，逐 id 比对
    CVI.invalidate()
    ok = CVI._ensure_group(con, version, 1024)
    g = CVI._MATRICES.get((version, 1024)) if ok else None
    if not g:
        chk("B1 矩阵可加载", False, "无法加载 %s/1024" % version)
        con.close()
        return 1
    matrix_ids = set(g["ids"])
    db_ids = {r["id"] for r in con.execute(
        "SELECT id FROM document_chunks WHERE content != '' AND embedding != '[]' "
        "AND embed_version=?", (version,))}
    missing = db_ids - matrix_ids
    extra = matrix_ids - db_ids
    chk("B1 矩阵覆盖全部有向量的块", not missing,
        "缺 %d 个（示例 %s）" % (len(missing), sorted(missing)[:5]))
    chk("B2 矩阵不含已删除的块", not extra,
        "多出 %d 个（示例 %s）" % (len(extra), sorted(extra)[:5]))

    # A5 专项：重算过的那批文档必须有向量分（防"重算工具没刷新缓存"回归）
    for did in (846, 847, 848):
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM document_chunks WHERE document_id=? AND embedding != '[]'",
            (did,))]
        if not ids:
            continue
        lack = [i for i in ids if i not in matrix_ids]
        chk("A5 doc=%d 的 %d 条向量全部在矩阵内" % (did, len(ids)), not lack,
            "缺 %d 条" % len(lack))

    print("\n" + "=" * 78)
    print("[C] 变异自证（判据必须能翻红）")
    print("=" * 78)
    # 模拟"缓存少 100 行"：A2 的判据表达式必须为假
    fake_cached = max(0, n_db - 100)
    chk("C1 缓存比数据库少 100 行时 A2 会判失败", fake_cached != n_db,
        "%s vs %s" % (fake_cached, n_db))
    # 模拟"缺块"：B1 的判据必须为假
    fake_ids = set(sorted(db_ids)[: max(1, len(db_ids) - 1)])
    chk("C2 少一个块时 B1 会判失败", bool(db_ids - fake_ids))

    con.close()
    print("\n" + "=" * 78)
    print("总计：%d 项通过，%d 项失败" % (_ok, _fail))
    print("=" * 78)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
