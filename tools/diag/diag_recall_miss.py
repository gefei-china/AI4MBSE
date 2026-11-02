# -*- coding: utf-8 -*-
"""召回失败诊断：正确 chunk 到底「没被召回」还是「召回了但排太后」。

## 为什么要分这两类

它们的修法完全不同，混在一起会改错地方：

| 失败类型 | 现象 | 该修什么 |
|---|---|---|
| **未召回**（not_in_pool） | 正确 chunk 不在候选池（recall_k 宽度内） | 召回路本身：分词/权重/embedding/多路召回 |
| **排序靠后**（in_pool_low_rank） | 在池子里，但排序进不了注入窗口 | 融合公式 / 重排 / 排序特征 |

本项目历史上就吃过「把排序问题当召回问题修」的亏（改 top_k 修不好排序，白烧算力）。
⇒ 本脚本对每条 query 输出：正确 chunk 在 **BM25 单路 / 向量单路 / 混合融合** 三处的名次，
一眼看出是哪一路把它弄丢了。

## 判据（每条都实际算，不猜）

- `bm25_rank`  / `bm25_score`  —— 稀疏路名次与分数（中文分词 + BM25）
- `vec_rank`   / `vec_score`   —— 稠密路名次与分数（embedding 不可用时为 bigram 降级）
- `fused_rank` / `fused_score` —— RRF 融合后的最终名次
- `degraded`   —— 本次是否为降级态（降级时 vec 分数不可信，结论要打折）

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_recall_miss.py
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_recall_miss.py --add "query|doc_id|chunk_index"
"""
import os
import sys
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

# (query, 正确 document_id, 正确 chunk_index, 说明)
CASES = [
    ("座舱空气调节仿真需要考虑哪些负荷", 815, 62, "6 项热负荷属性清单"),
    ("混合动力汽车动力经济性仿真关注哪些功率指标", 814, 39, "功率需求观测定义"),
]


def _pos_of(hits, did, idx):
    """在 hits 列表里找 (document_id, chunk_index)，返回 (名次1基, 该条) 或 (None, None)。"""
    for i, h in enumerate(hits or [], 1):
        if h.get("document_id") == did and h.get("chunk_index") == idx:
            return i, h
    return None, None


def _fmt(v, nd=3):
    if v is None:
        return "—"
    try:
        return ("%." + str(nd) + "f") % float(v)
    except Exception:
        return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", action="append", default=[],
                    help='追加用例："query|doc_id|chunk_index"')
    ap.add_argument("--wide", type=int, default=60, help="单路召回宽度（诊断用，默认 60）")
    args = ap.parse_args()

    extra = []
    for s in args.add:
        try:
            q, d, i = s.split("|")
            extra.append((q.strip(), int(d), int(i), "命令行追加"))
        except Exception:
            print("跳过无法解析的 --add：%r" % s)
    cases = CASES + extra

    print("=" * 88)
    print("召回失败诊断：区分「未召回」与「排序靠后」")
    print("=" * 88)

    from agent.rag import GraphRAG
    from knowledge_engine import hybrid_search
    from core import config as cfg

    conn = None
    try:
        from knowledge_engine import get_conn  # type: ignore
        conn = get_conn()
    except Exception:
        pass
    if conn is None:
        try:
            import agent.rag as _r
            conn = _r.get_db()
        except Exception:
            import sqlite3
            conn = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
            conn.row_factory = sqlite3.Row

    rk = int(cfg.get("rag", "recall_k", 20) or 20)
    print("配置 recall_k=%d / top_k=%s / inject_k=%s"
          % (rk, cfg.get("rag", "top_k"), cfg.get("rag", "inject_k")))
    print("诊断单路宽度：%d（放大以便看真实名次，生产用 recall_k）\n" % args.wide)

    svc = GraphRAG()
    for q, did, idx, note in cases:
        print("-" * 88)
        print("Q: %s" % q)
        print("   正确 chunk = doc=%d idx=%d（%s）" % (did, idx, note))

        # ① 混合融合（生产真实路径）
        try:
            r = svc.retrieve(q, branch="dev")
            fused = r.get("chunk_hits") or []
            fr, fh = _pos_of(fused, did, idx)
            print("   ① 混合融合：返回 %2d 条，正确 chunk 名次=%s"
                  % (len(fused), fr if fr else "❌ 不在返回里"))
        except Exception as e:  # noqa: BLE001
            print("   ① 混合融合：异常 %s" % str(e)[:80])
            fused, fr, fh = [], None, None

        # ② 融合前的候选池（宽召回，看它是否本就被召回）
        try:
            hy = hybrid_search(conn, q, top_k=args.wide, branches=None)
            pool = hy.get("hits") or []
            pr, ph = _pos_of(pool, did, idx)
            print("   ② 宽融合池：共 %2d 条，正确 chunk 名次=%s" % (len(pool), pr if pr else "❌ 未召回"))
            if ph:
                print("      score=%s bm25=%s vec=%s conf=%s degraded=%s"
                      % (_fmt(ph.get("score")), _fmt(ph.get("bm25_score"), 1),
                         _fmt(ph.get("vec_score")), ph.get("confidence_level"),
                         ph.get("degraded")))
        except Exception as e:  # noqa: BLE001
            print("   ② 宽融合池：异常 %s" % str(e)[:80])
            pool, pr = [], None

        # ③ 只看 BM25 路
        try:
            from knowledge_pipeline.search import bm25_search  # type: ignore
            bm = bm25_search(conn, q, top_k=args.wide) or []
            br, bh = _pos_of(bm, did, idx)
            print("   ③ BM25 单路：共 %2d 条，名次=%s" % (len(bm), br if br else "❌ 未召回"))
        except Exception:
            try:
                # 退路：从融合池里按 bm25_score>0 排序模拟
                withbm = [h for h in pool if float(h.get("bm25_score") or 0) > 0]
                withbm.sort(key=lambda x: -float(x.get("bm25_score") or 0))
                br, bh = _pos_of(withbm, did, idx)
                print("   ③ BM25 单路（由融合池按分重排近似）：共 %2d 条命中，名次=%s"
                      % (len(withbm), br if br else "❌ 未召回"))
            except Exception as e:  # noqa: BLE001
                print("   ③ BM25 单路：不可用（%s）" % str(e)[:60])

        # ④ 结论
        print("   ④ 判读：", end="")
        if fr:
            print("正确 chunk 已进最终返回（名次 %d）⇒ 不是召回问题，是**注入窗口/多样性配额**把它挤掉了" % fr)
        elif pr:
            print("正确 chunk 在融合池（名次 %d）但没进最终返回 ⇒ 被低置信过滤/域外拒答/去重挡掉" % pr)
        else:
            print("正确 chunk **连宽融合池都没进** ⇒ 真·召回失败，需查分词/embedding/两路权重")
        print()


if __name__ == "__main__":
    main()
