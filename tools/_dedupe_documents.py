# -*- coding: utf-8 -*-
"""库内重复文档盘点与清理（2026-11-10）—— **默认只读，--apply 才动手**。

## 背景（真库实测，不是推测）
`documents` 里有两组重复（各 2 份，源副本 sha256 **完全相同**）：
  · doc 847 / 848《Print_面向网络总体的AI赋能MBSE…技术要求.pdf》各 55 块（110 块冗余）
  · doc 851 / 852《1_800_SysML-v2-AI建模知识文档.md》各 270 块（540 块冗余）
合计 **650 个冗余 chunk** —— 检索里同一段话占两个槽位，且 `verify_vec_cache` 与库
差的正是这 540（向量缓存没跟上）。

## 两类分组的处置**刻意不同**（别一刀切）
| 组 | 判据 | 默认处置 |
|---|---|---|
| `same_content` | `content_sha256` 相同（字节级）| 保留**最新**一份，其余可删 |
| `same_name` | 仅文件名相同 | **只报告**（可能是版本迭代，必须人工判断）|

## 为什么默认不删
删文档是**用户资产级操作**：`MetaRepo.delete_document` 会连带删 chunks / metadata /
复核队列 / 源副本。误删的代价远高于多占 540 块。故本工具的默认行为是"打印清单 + 给出
它认为该删的 id"，删不删由人决定；真要删，显式 `--apply`，且**每批 ≤10 个**、
逐个复核（对齐本仓「破坏性操作要小批量 + 可回滚」的纪律）。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/_dedupe_documents.py                 # 只读盘点
    ./.venv/Scripts/python.exe -X utf8 tools/_dedupe_documents.py --apply --kind same_content
    # 需要把"同名不同内容"也删掉时：先只读看一眼，再 --apply --kind same_name --keep newest
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")


def _copy_sha(doc_id, filename):
    """复用 `knowledge_pipeline.dedup.source_copy_sha`（单一真源，别在这里再写一份）。"""
    from knowledge_pipeline.dedup import source_copy_sha
    return source_copy_sha(doc_id, filename)


def _plan(groups, kinds):
    """算出"该删哪些 id"：每组保留最新一份，其余待删。"""
    plan = []
    for g in groups:
        if g["kind"] not in kinds:
            continue
        docs = sorted(g["docs"], key=lambda d: int(d["doc_id"]))
        if len(docs) < 2:
            continue
        keep, drop = docs[-1], docs[:-1]
        plan.append({"kind": g["kind"], "key": g["key"], "keep": keep, "drop": drop,
                     "reclaim_chunks": sum(int(d.get("chunk_count") or 0) for d in drop)})
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只读）")
    ap.add_argument("--kind", default="same_content",
                    help="要处理的组类型：same_content（默认）| same_name | both")
    ap.add_argument("--max", type=int, default=10, help="单批最多删几个（默认 10）")
    ap.add_argument("--force", action="store_true",
                    help="允许删除「同名但未能证明字节相同」的组（有删掉不同版本的风险）")
    a = ap.parse_args()

    kinds = {"same_content", "same_name"} if a.kind == "both" else {a.kind}
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    from knowledge_pipeline.dedup import duplicate_groups
    groups = duplicate_groups(c)
    c.close()

    print("库：%s" % DB)
    print("重复组：%d（%s）" % (len(groups), "、".join(sorted(kinds))))
    for g in groups:
        print("\n[%s] %s　%d 份 / %d 块 / %.0f KB"
              % (g["kind"], g["key"], g["count"], g["chunks"], g["bytes"] / 1024.0))
        for d in g["docs"]:
            print("   #%-6s %8d B  %4d 块  %s  %s"
                  % (d["doc_id"], d["file_size"], d["chunk_count"],
                     d["created_at"], d["filename"][:56]))
        if g["kind"] == "same_name":
            # 存量 sha 为空 ⇒ 用源副本现场哈希，给出**可判读的结论**而不是"疑似重复"
            shas = {int(d["doc_id"]): _copy_sha(d["doc_id"], d["filename"]) for d in g["docs"]}
            same = len({v for v in shas.values() if v}) == 1 and all(shas.values())
            print("   → 源副本哈希：%s ⇒ %s"
                  % ({k: (v[:12] + "…" if v else "缺失") for k, v in shas.items()},
                     "**字节相同**（误传，可直接去重）" if same
                     else "**内容不同**（版本迭代，需人工判断，不要直接删）"))
            if same:
                g["_sha_verified"] = True

    plan = _plan(groups, kinds)
    total = sum(p["reclaim_chunks"] for p in plan)
    print("\n计划：保留每组最新一份，待删 %d 份，可回收 %d 个 chunk"
          % (sum(len(p["drop"]) for p in plan), total))
    for p in plan:
        print("  · [%s] 保留 #%s ← 删除 %s"
              % (p["kind"], p["keep"]["doc_id"],
                 ", ".join("#%s" % d["doc_id"] for d in p["drop"])))

    if not a.apply:
        print("\n（只读模式，未删除任何东西。要真删加 --apply，建议先备份 mbse.db）")
        return 0

    unverified = [p for p in plan if p["kind"] == "same_name"
                  and not any(g.get("_sha_verified") for g in groups if g["key"] == p["key"])]
    if unverified and not a.force:
        print("\n⛔ 拒绝删除：以下同名组的源副本**无法证明字节相同**（可能是版本迭代）：")
        for p in unverified:
            print("   · %s" % p["key"][:60])
        print("   请先人工核对；确认要删再加 --force")
        return 2
    todo = [d for p in plan for d in p["drop"]][:max(1, int(a.max))]
    if not todo:
        print("\n没有需要删除的文档")
        return 0
    from database import db_conn
    from repositories.meta_repo import MetaRepo
    removed = []
    with db_conn() as conn:
        repo = MetaRepo(conn)
        for d in todo:
            did = int(d["doc_id"])
            try:
                repo.delete_document(did)     # 连带 chunks / metadata / 复核队列 / 源副本
                removed.append(did)
                print("  已删除 #%d（%d 块）" % (did, int(d.get("chunk_count") or 0)))
            except Exception as e:
                print("  ✗ 删除 #%d 失败：%r" % (did, e))
                break                            # 小批量纪律：一失败就停，让人看
    print("\n完成：删除 %d 份 → %s" % (len(removed), removed))
    print("⚠️ 之后应复核：documents / document_chunks 计数，以及 verify_vec_cache 的差值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
