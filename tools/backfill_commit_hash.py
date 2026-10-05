# -*- coding: utf-8 -*-
"""回填 knowledge_commits.content_hash（2026-10-05）。

为什么需要：审计发现生产库有 6 行（id 607~612，kind='manual'，2026-09-23 13:16~13:20）
的 content_hash 为空 ⇒ `CommitRepo.verify_commits()` 的 `ok` 恒为 False
⇒ 所有「篡改是否被检出」的判据（它们比较 ok 的翻转）全部失效 —— 一个根因串起 7 条红灯。

定性：**数据缺陷，不是代码缺陷**。写入路径 `CommitRepo` 自身是写 content_hash 的
（repositories/commit_repo.py:55-58），这 6 行是**绕过代码直接 SQL INSERT** 产生的
（很当时的验收/测试数据）⇒ 属于「本连接最近一次 INSERT 之外」的历史脏数据。

修法：用**产品自身的** `content_hash_of` 重算（不另写一份算法，避免"用我另写的逻辑
验收源码"），逐行 UPDATE，只改 content_hash 一列。
自证：回填后 `verify_commits()` 必须 ok=True / missing=0 / mismatched=0。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from repositories.commit_repo import CommitRepo          # noqa: E402

DB = os.path.join(ROOT, "mbse.db")
DRY = "--apply" not in sys.argv

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
before = con.execute(
    "SELECT id, branch, parent_id, kind, changes, snapshot FROM knowledge_commits "
    "WHERE content_hash IS NULL OR trim(content_hash)='' ORDER BY id").fetchall()
print("待回填 %d 行：%s" % (len(before), [r["id"] for r in before]))

if DRY:
    print("\n[dry-run] 加 --apply 才真正写库。逐行将计算：")
    for r in before:
        h = CommitRepo.content_hash_of(r["branch"], r["parent_id"], r["kind"],
                                       r["changes"], r["snapshot"])
        print("  id=%s  %s -> %s..." % (r["id"], r["kind"], h[:16]))
else:
    for r in before:
        h = CommitRepo.content_hash_of(r["branch"], r["parent_id"], r["kind"],
                                       r["changes"], r["snapshot"])
        con.execute("UPDATE knowledge_commits SET content_hash=? WHERE id=?", (h, r["id"]))
    con.commit()
    print("已回填 %d 行" % len(before))

res = CommitRepo(con).verify_commits()
print("\n[自证] verify_commits: total=%s checked=%s missing=%s mismatched=%s ok=%s"
      % (res["total"], res["checked"], res["missing"], len(res["mismatched"]), res["ok"]))
if res["mismatched"]:
    for m in res["mismatched"][:5]:
        print("   mismatched:", m)
con.close()

if not DRY and not (res["missing"] == 0 and not res["mismatched"] and res["ok"]):
    print("!! 自证未通过")
    sys.exit(1)
print("[done]")
