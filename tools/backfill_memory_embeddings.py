# -*- coding: utf-8 -*-
"""补齐 `agent_memory` 的**缺失向量**（`embed_version=''` / `embedding` 空）。

## 为什么必须做（2026-11-16 实测，conv 999999065 会话引出）

诊断"高相关记忆进不了 top4"时逐维拆解排序分数，发现：

| 向量状态 | alive 条数 | 代表 | 语义分 s |
|---|---|---|---|
| **无向量** | **120 条（61%）** | #284（access=**849**）、#285（834） | ~0.24~0.27（bigram） |
| 有向量 openai-compat 1024 | 78 条 | #657、#504 | 0.63~0.71 |

`MemoryService.search_core` 的排序是
`final = s × decay × boost × relevance × credibility`，
其中 `s` **只在与查询向量同 `embed_version` 时才用真向量**，否则降级 bigram：

```python
if qvec and r.get("embed_version") == qver:   # ← 空版本永远不满足
    ...真向量余弦...
if s <= 0:
    s = bigram 余弦        # ← #284 走这里
```

⇒ **有向量的记忆语义分是没向量的 2.5 倍**，排序结果实际由"有没有向量"决定，
而不是"内容相关不相关"。

更糟的是它形成**恶性循环**：
  老记忆（10 月初沉淀、价值最高的那批）无向量 → bigram 分低 → 排不进 top4
  → 更少被访问 → 更容易被遗忘引擎淘汰（库里 97 条 forgotten 的成因之一）
  → 留下来的多是新记忆（有向量）

## 安全约束（与 tools/reembed_degraded.py 同款）
- **默认 dry-run**：只统计与试算，不写库；`--apply` 才写。
- **逐批校验版本**：`embed_with_version` 返回 `"bigram-tf"` 说明 API 挂了
  ⇒ **立即停止且不写**（写坏向量比不写更糟：降级向量会永久留在库里且事后无法区分）。
- 只 UPDATE `embedding` / `embed_version` 两列，**不动 content 及其他任何列**。
- 可回退：`--apply` 前把 (id, 原 embed_version, 原 embedding 长度) 写进备份 JSON。
- 幂等：只选 `embed_version` 为空的行，重跑不会重复处理已补好的。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/backfill_memory_embeddings.py            # dry-run
  ./.venv/Scripts/python.exe -X utf8 tools/backfill_memory_embeddings.py --apply    # 真写
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def select_targets(conn, limit=0, include_forgotten=False, force_reembed=False):
    """选出需要（重）算向量的记忆。

    ⚠️ 默认**只补 `embed_version` 为空的**：那些是"确定没有向量"的。
    默认**只补 alive**：忘记的记忆不再参与召回，补它纯浪费 API 且会掩盖
      "遗忘是否正常"这一事实。要补已遗忘的需显式 `--include-forgotten`。

    ★★ 2026-11-16 新增 `force_reembed`（重大发现，见模块 docstring）：
      只补空向量**不够**。实测 12 条样本里，2026-11-04 **之前**写入的向量与当前
      embedding 模型**近乎正交**（自洽余弦 -0.02 ~ 0.02），而 11-04 之后写入的是 1.0000。
      规律：中间换过 embedding 模型，旧向量没重算，但 `embed_version` 仍标
      `openai-compat` ⇒ 代码按版本号判断"同源"，不做任何校验 ⇒
      **旧记忆在向量检索里完全不可见**（余弦≈0 ⇒ 排序垫底 ⇒ 永不召回）。
      这是"排序公式怎么调都没用"的根因 ⇒ 必须全量重算，不只是补空。
    """
    sql = ("select id, content, embed_version, embedding from agent_memory "
           "where content is not null and trim(content)<>''")
    if not force_reembed:
        sql += " and (embed_version is null or embed_version='')"
    if not include_forgotten:
        sql += " and coalesce(forgotten,0)=0"
    sql += " order by id"
    if limit:
        sql += " limit %d" % int(limit)
    return conn.execute(sql).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "mbse.db"))
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--include-forgotten", action="store_true",
                    help="连已遗忘的记忆一起补（默认只补 alive）")
    ap.add_argument("--force-reembed", action="store_true",
                    help="**全量重算**（含已有 openai-compat 向量的）—— "
                         "用于修复'换过 embedding 模型但旧向量没重算'（见 select_targets）")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    import sqlite3
    from knowledge_pipeline import Embedder

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    targets = select_targets(conn, args.limit, args.include_forgotten,
                             args.force_reembed)
    print("=== 待补向量记忆：%d 条（%s%s）"
          % (len(targets), "含已遗忘" if args.include_forgotten else "仅 alive",
             "，全量重算" if args.force_reembed else ""))
    if not targets:
        print("无需处理。")
        return 0

    emb = Embedder(conn)
    done, failed, versions = 0, [], {}
    backup = []
    t0 = time.time()

    for i in range(0, len(targets), args.batch):
        chunk = targets[i:i + args.batch]
        texts = [str(r["content"])[:400] for r in chunk]
        try:
            vecs, version = emb.embed_with_version(texts, batch_size=0)
        except Exception as e:
            print("  [批 %d] 嵌入调用异常：%r —— 停止" % (i // args.batch + 1, e))
            break
        # ★ 关键安全闸：拿不到真向量就**立即停**，绝不把 bigram 结果写进库
        if not vecs or version in ("", "bigram-tf") or any(not v for v in vecs):
            print("  [批 %d] 版本=%r 或存在空向量 ⇒ **停止且不写**"
                  "（写坏向量比不写更糟）" % (i // args.batch + 1, version))
            break
        versions[version] = versions.get(version, 0) + len(vecs)

        for r, v in zip(chunk, vecs):
            if len(v) != len(vecs[0]):
                failed.append((r["id"], "维度不一致 %d vs %d" % (len(v), len(vecs[0]))))
                continue
            backup.append({"id": r["id"],
                           "old_embed_version": r["embed_version"],
                           "old_embedding_len": len(r["embedding"] or "")})
            if args.apply:
                conn.execute("UPDATE agent_memory SET embedding=?, embed_version=? WHERE id=?",
                             (json.dumps([round(x, 6) for x in v]), version, r["id"]))
            done += 1
        if args.apply:
            conn.commit()
        print("  [批 %d] 已处理 %d/%d（版本=%s）"
              % (i // args.batch + 1, done, len(targets), version), flush=True)

    dt = time.time() - t0
    print()
    print("=== 结果：%s %d 条，失败 %d 条，用时 %.1fs"
          % ("已写入" if args.apply else "dry-run 可处理", done, len(failed), dt))
    print("    向量版本分布:", versions)
    if failed:
        print("    失败明细:", failed[:8])

    if args.apply and backup:
        bpath = args.report or os.path.join(
            ROOT, "tmp", "_memory_embed_backup_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(os.path.dirname(bpath), exist_ok=True)
        with open(bpath, "w", encoding="utf-8") as f:
            json.dump({"applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "db": args.db, "count": len(backup), "rows": backup},
                      f, ensure_ascii=False, indent=1)
        print("    回退备份:", bpath)
        print("    回退方式: 把备份里各行 UPDATE agent_memory SET embedding=NULL,"
              " embed_version='' WHERE id=?")

    # 事后核对：库内空向量应清零（或只剩未处理的）
    left = conn.execute(
        "select count(*) from agent_memory where coalesce(forgotten,0)=0 "
        "and (embed_version is null or embed_version='')").fetchone()[0]
    print("    库内 alive 空向量剩余: %d" % left)
    conn.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
