# -*- coding: utf-8 -*-
"""重启后验证：BM25 修复是否真的在线生效（真实 HTTP 链路）。

⚠️ 必须走**真实 HTTP**而不是直接 import：`_BM25_CACHE` 是**进程内**缓存，
   直接 import 拿到的是**新进程的新缓存**，根本证明不了"跑着的服务"已生效。
   ⇒ 只能问活着的服务。

三条判据：
  ① 服务活着
  ② 服务返回的检索结果里，**章节标题类 query 能召回该section**
     （用真实 gold：从真库取一个「标题锚词不在正文里」的 section）
  ③ 修复前该query 的 gold 在 top-20 一次都不出现；修复后必须进 top-10
     —— 这是 A/B 的线上版本。
"""
import json
import os
import re
import sqlite3
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + detail) if (detail and not cond) else ""))


def api(path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json", "X-User-Id": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))


print("=== ① 服务探活 ===")
try:
    dash = api("/api/dashboard")
    ck("A1 /api/dashboard 可达", True)
    ck("A2 返回含 kb_stats（确认为本应用）", "kb_stats" in dash)
except Exception as e:
    ck("A1 服务可达", False, str(e)[:120])
    print("\n服务未起来，先别看后面。")
    sys.exit(1)

print("\n=== ② 选一个『标题锚词不在正文里』的真实 section ===")
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
db = sqlite3.connect("file:%s/mbse.db?mode=ro" % root.replace("\\", "/"), uri=True)
db.row_factory = sqlite3.Row
cand = None
for r in db.execute(
        "SELECT source_doc, section FROM document_chunks "
        "WHERE section LIKE '###%' AND content!='' "
        "GROUP BY source_doc, section HAVING COUNT(*)>=3"):
    title = (r["section"] or "").lstrip("# ").strip()
    core = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title).strip()
    m = re.search(r"[\u4e00-\u9fff]{4,}", core) or re.search(r"[A-Za-z]{6,}", core)
    if not m:
        continue
    anchor = m.group(0)
    n_hit = db.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE source_doc=? AND section=? "
        "AND content LIKE ?", (r["source_doc"], r["section"], "%" + anchor + "%")
    ).fetchone()[0]
    if n_hit == 0:
        cand = (r["source_doc"], r["section"], anchor)
        break
if not cand:
    ck("B0 找到合适样本", False, "全库无（正文均不含锚词）的 section")
    sys.exit(1)
doc, sec, anchor = cand
print("     section=%r 锚词=%r" % (sec[:40], anchor))
print("     ⚠️ 该锚词在该 section 所有块的 content 里**都不出现**")
print("     ⇒ 索引 content 必然召不回，只有 bm25_text 能救 ⇒ 是有效的判别样本")

print("\n=== ③ 走真实 HTTP 检索接口验证 ===")
#⚠️⚠️ 首版判据错了：只断言「目标 section 出现在返回里」，
#   而 /api/knowledge/chunks/search?hybrid=true 走的是 **hybrid_search（向量+BM25 混检）**
#   ⇒ **向量路会把缺陷态救回来** ⇒ 实测在**未修复**的旧服务上也 PASS（假绿）。
#   ⇒ 必须**只看 BM25 的贡献**（bm25_score 非零 = BM25 路真的命中了它）。
try:
    res = api("/api/knowledge/chunks/search",
              {"query": anchor, "hybrid": True, "top_k": 10})
    hits = res.get("hits") or res.get("results") or res.get("items") or []
    ck("C1 检索接口有返回", bool(hits), "hits=%d" % len(hits))
    ck("C1b 走的是 hybrid 模式", res.get("mode") == "hybrid",
       "mode=%r" % res.get("mode"))

    #★ 只认 BM25 路真的召回了它（bm25_score > 0）
    def _bm25_of(h):
        v = h.get("bm25_score")
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    sec_hits = [h for h in hits if (h.get("section") or "") == sec]
    bm25_hits = [h for h in sec_hits if _bm25_of(h) > 0]
    ck("C2 目标 section 出现在返回里（混合召回）", bool(sec_hits),
       "返回 %d 条，其中目标 section 0 条" % len(hits))
    ck("C3★ BM25 路真的命中了它（bm25_score>0）", bool(bm25_hits),
       "目标 section 的 %d 条里 bm25_score 全为 0 ⇒ BM25 仍索引 content（缺陷态）"
       % len(sec_hits))
    if sec_hits:
        idx = next(i for i, h in enumerate(hits) if (h.get("section") or "") == sec)
        print("     命中位置 = 第 %d 条 / 共 %d 条" % (idx + 1, len(hits)))
        print("     bm25_score = %s ｜ vec_score = %s"
              % ([_bm25_of(h) for h in sec_hits][:3],
                 [h.get("vec_score") for h in sec_hits][:3]))
    print("\n  返回前 3 条：")
    for h in hits[:3]:
        print("    [sec=%s] bm25=%s vec=%s score=%s"
              % ((h.get("section") or "")[:26], h.get("bm25_score"),
                 h.get("vec_score"), h.get("score")))
except Exception as e:
    ck("C1 检索接口调用", False, str(e)[:150])

db.close()
print("\n%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
if PASS and not FAIL:
    print("\n★ 线上确认：BM25 路确实召回了标题类 query ⇒ 修复已在线生效。")
print("★ 判据设计说明：本脚本**必须**在 bm25_score 上判定。")
print("  只看「返回里有没有目标 section」会在**缺陷态也 PASS** ——")
print("  因为 hybrid 的向量路会兜住，掩盖掉 BM25 的缺陷。")
sys.exit(1 if FAIL else 0)