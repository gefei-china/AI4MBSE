# -*- coding: utf-8 -*-
"""P1-4 embedding 全量重嵌：chunks 6459 行用当前模型（qwen3.7-text-embedding）重算向量。

背景：embed_version 只标记引擎（openai-compat）不标记具体模型——存量向量是旧模型算的，
查询侧切新模型后版本标记同名不同源 → 余弦相似度错乱 → dense 检索 mrr 0.453→0.022。
本脚本分批重嵌（幂等覆盖），每 300 条 commit 一次；hyde_embedding 的假设文本未存列、
无法重嵌 → 置空（检索侧 v2 自动回退 embedding 列主路）。
可中断可重跑：按 id 升序分页，已重嵌行会被同模型再次覆盖（无害）。
"""
import json
import sys
import time

sys.path.insert(0, r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")
import os
os.chdir(r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")

from database import db_conn, get_db
from knowledge_pipeline import Embedder

BATCH = 64          # 每批条数（API 侧内部再按 api_batch_max=10 切）
COMMIT_EVERY = 5    # 每 5 批 commit 一次
T0 = time.time()

conn = db_conn().__enter__() if False else None  # 占位，下面用显式连接
import sqlite3
con = sqlite3.connect(r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/mbse.db")
total = con.execute("SELECT COUNT(*) FROM document_chunks WHERE content != ''").fetchone()[0]
print(f"待重嵌 {total} 行", flush=True)

emb = Embedder(get_db())  # 必须传 conn：probe llm_providers 取 embedding provider（None 则 _api=None 恒降级）
last_id = 0
done = 0
batch_no = 0
while True:
    rows = con.execute(
        "SELECT id, content FROM document_chunks WHERE content != '' AND id > ? ORDER BY id LIMIT ?",
        (last_id, BATCH)).fetchall()
    if not rows:
        break
    texts = [r[1] for r in rows]
    try:
        vecs, version = emb.embed_with_version(texts, batch_size=0)
    except Exception as e:
        print(f"[中断] 批 {batch_no}（id>{last_id}）embedding 失败：{e}", flush=True)
        sys.exit(1)
    if version != "openai-compat":
        print(f"[中断] 批 {batch_no} 降级为 {version}——API 异常，停止以免写坏向量", flush=True)
        sys.exit(1)
    for (cid, _), v in zip(rows, vecs):
        con.execute("UPDATE document_chunks SET embedding=?, embed_version=? WHERE id=?",
                    (json.dumps(v), version, cid))
        last_id = max(last_id, cid)
    done += len(rows)
    batch_no += 1
    if batch_no % COMMIT_EVERY == 0:
        con.commit()
        rate = done / max(time.time() - T0, 1)
        eta = (total - done) / max(rate, 0.1)
        print(f"进度 {done}/{total}（{done*100//total}%）| {rate:.0f} 行/s | ETA {eta:.0f}s", flush=True)

# hyde_embedding 置空：假设文本未存列无法重嵌，旧模型向量会错配——置空走主路
con.execute("UPDATE document_chunks SET hyde_embedding=NULL WHERE hyde_embedding IS NOT NULL")
con.commit()
con.close()
print(f"✅ 重嵌完成：{done} 行，耗时 {time.time()-T0:.0f}s；hyde_embedding 已置空（检索走主路）", flush=True)
