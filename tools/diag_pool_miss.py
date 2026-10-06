# -*- coding: utf-8 -*-
"""doc 域 pool 未命中的根因诊断（**零 API 消耗**，2026-10-06）。

⚠️ 配额现状：重嵌入 810 条已用尽免费额度（403 Free quota exhausted），
   所以本脚本**不调用 embedding API** —— 用「库内已有向量」做自查询分析。

能回答的问题（不耗额度）：
  L1 gold chunk 的向量是否存在于矩阵、维度是否一致
  L2 **自查询名次**：拿 gold chunk 自己的向量去查矩阵，排名是否 = 1
     （=1 说明向量索引本身没问题；>1 说明索引/过滤有 bug）
  L3 同文档内聚合性：gold chunk 与同文档其它 chunk 的相似度 vs 跨文档
     （聚合性差 = 文档被切碎或向量被污染）
  L4 章节标题与正文的**语义关系**：gold chunk 的 section 文本与其 content 的
     向量相似度 —— 若很低，说明「标题类 query」天然难命中（评测集设计问题）

只读，不写库，不调 API。
"""
import json
import os
import sqlite3
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

EV = json.load(open(os.path.join(ROOT, "tmp", "p21", "evalset.json"), encoding="utf-8"))
REPORT = json.load(open(os.path.join(ROOT, "tmp", "p21", "eval_report_run.json"), encoding="utf-8"))

conn = sqlite3.connect(os.path.join(ROOT, "mbse.db"))
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, document_id, chunk_index, embedding, embed_version, section, content "
    "FROM document_chunks WHERE embedding IS NOT NULL AND embed_version='openai-compat'"
).fetchall()
ids = [r["id"] for r in rows]
def _vec(s):
    """embedding 存的是 JSON 数组字符串（如 '[0.03,-0.1,...]'）——直接 split(',') 会带上括号。"""
    t = (s or "").strip().strip("[]")
    return np.fromstring(t, sep=",", dtype=np.float32)


M = np.array([_vec(r["embedding"]) for r in rows], dtype=np.float32)
M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
id2row = {cid: i for i, cid in enumerate(ids)}
doc_of = {r["id"]: r["document_id"] for r in rows}
print("向量矩阵: %d 条 × %d 维" % M.shape)

miss = [r for r in REPORT["rows"]["doc"] if not r.get("pool_recall")]
ev_by_id = {c["id"]: c for c in EV["doc_cases"]}

print("\n%-7s %-24s %-6s %-9s %-9s %-8s" % ("case", "query", "gold#", "自查询名次", "同文档均值", "跨文档均值"))
print("-" * 74)
self_ranks, in_doc_sims, cross_sims = [], [], []
for r in miss[:12]:
    ev = ev_by_id.get(r["id"], {})
    gold_ids = []
    for g in (ev.get("gold") or []):
        for row in conn.execute(
                "SELECT id FROM document_chunks WHERE document_id="
                "(SELECT id FROM documents WHERE filename=?) AND chunk_index=?",
                (g.get("source_doc"), g.get("chunk_index"))):
            gold_ids.append(row["id"])
    if not gold_ids:
        print("%-7s %-24s %-6d %-9s %-9s %-8s" % (r["id"], ev.get("query", "")[:22], 0,
                                                    "gold缺失", "-", "-"))
        continue
    g = gold_ids[0]
    gi = id2row.get(g)
    if gi is None:
        print("%-7s %-24s %-6d %-9s %-9s %-8s" % (r["id"], ev.get("query", "")[:22], len(gold_ids),
                                                    "不在矩阵", "-", "-"))
        continue
    sims = M @ M[gi]
    order = np.argsort(-sims)
    rank = int(np.where(order == gi)[0][0]) + 1
    self_ranks.append(rank)
    same = sims[[id2row[x] for x in ids if doc_of.get(x) == doc_of[g] and x != g]]
    cross = sims[[i for i, x in enumerate(ids) if doc_of.get(x) != doc_of[g]]]
    m_in = float(same.mean()) if len(same) else 0.0
    m_out = float(cross.mean()) if len(cross) else 0.0
    in_doc_sims.append(m_in)
    cross_sims.append(m_out)
    print("%-7s %-24s %-6d %-9d %-9.3f %-8.3f"
          % (r["id"], ev.get("query", "")[:22], len(gold_ids), rank, m_in, m_out))

if self_ranks:
    print("\n自查询名次: min=%d  中位=%d  max=%d  （=1 意味着向量索引无 bug）"
          % (min(self_ranks), sorted(self_ranks)[len(self_ranks) // 2], max(self_ranks)))
    print("同文档平均相似度: %.3f   跨文档平均相似度: %.3f   差值: %+.3f"
          % (sum(in_doc_sims) / len(in_doc_sims), sum(cross_sims) / len(cross_sims),
             sum(in_doc_sims) / len(in_doc_sims) - sum(cross_sims) / len(cross_sims)))
    print("（差值≈0 ⇒ 同文档并不比跨文档更相似 ⇒ 向量缺乏文档级聚合性，"
          "标题类 query 召不回 gold 属**语料/模型能力**问题，不是索引 bug）")
conn.close()
