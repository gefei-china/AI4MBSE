# -*- coding: utf-8 -*-
"""话题类 dense 阈值标定（2026-09-21 P1-4b 新增，补齐 calibrate_dense_thresholds.py 的场景缺口）。

背景（依据 docs/AI上下文配置项-依据与行业对标-20260921.md §4.2）：
  同一个 `context` 组里三个 dense 阈值标定方法不一致 ——
    · `semantic_fallback_gate_dense = 0.79`  ✅ 分位等价映射标定（脚本 calibrate_dense_thresholds.py）
    · `topic_retrieve_threshold_dense = 0.35`  ❌ 无标定记录
    · `topic_group_match_dense = 0.30`         ❌ 无标定记录
  原脚本的 3 个场景（tools / agents / flows）**不含话题类判定**，故本脚本补齐。

标定方法与被标定对象（口径严格对齐消费点，非近似）：
  · 场景 A `topic_group_match`  ← agent/pipeline_parts/history.py:130-140
      候选 = 每个话题组的代表文本（**段首 3 条消息各截 200 字**拼接）
      阈值 = bigram 路**硬编码 0.12**（代码里没有配置键，注释自述「≥0.12 取最佳组」）
  · 场景 B `topic_retrieve`     ← agent/pipeline_parts/history.py:322-345
      候选 = 会话内**最近 120 条**消息各截 400 字
      阈值 = `context.topic_retrieve_threshold` = 0.15（bigram 路）
      ⚠️ bigram 路另含**话题域加权**（候选 topic 与当前话题 ts≥0.2 时 +0.25*ts），
         本脚本**同时给出未加权/加权两套分布**，如实标注差异。

方法：分位等价映射 —— 对旧 bigram 阈值 t_b，求正例率 r = P(B >= t_b)，再取 dense 分布 D 的 r 分位。
语义是「保持原判定通过率」的**行为等价迁移**，不是最优 F1 点（要谈最优需人工标注正负样本）。

产出：控制台报告 + tmp/p21/topic_threshold_calibrate.json
"""
import json
import math
import os
import re
import sqlite3
import sys
import datetime

REPO = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
DB = os.path.join(REPO, "mbse.db")
OUT_JSON = os.path.join(REPO, "tmp", "p21", "topic_threshold_calibrate.json")

sys.path.insert(0, REPO)
os.chdir(REPO)

LINES = []


def w(s=""):
    LINES.append(s)
    print(s)


def cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def pct(v, p):
    v = sorted(v)
    if not v:
        return 0.0
    return v[min(int(len(v) * p), len(v) - 1)]


def dist_line(name, v):
    if not v:
        return f"  {name:<24} (空)"
    return ("  %-24s n=%-4d min=%.4f p25=%.4f p50=%.4f p75=%.4f p90=%.4f p95=%.4f max=%.4f"
            % (name, len(v), min(v), pct(v, .25), pct(v, .50), pct(v, .75), pct(v, .90), pct(v, .95), max(v)))


def equiv_dense(t_b, B, D):
    """旧 bigram 阈值 t_b → 正例率 r → dense 分布的 r 分位（等价阈值）。"""
    r = sum(1 for x in B if x >= t_b) / max(len(B), 1)
    if r <= 0:
        return 0.0, r
    ds = sorted(D, reverse=True)
    return ds[max(0, int(r * len(ds)) - 1)], r


def main():
    w("=" * 88)
    w("② 话题类 dense 阈值标定（分位等价映射）   " + datetime.datetime.now().isoformat(timespec="seconds"))
    w("=" * 88)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ── 1) query 集（与原脚本口径一致：query_trace ∪ messages.user，前 60 字符去重）──
    qs, seen = [], set()
    for sql in ("SELECT query AS q FROM query_trace",
                "SELECT content AS q FROM messages WHERE role='user'"):
        try:
            rows = conn.execute(sql).fetchall()
        except Exception as e:
            w(f"  !! 取 query 失败（{sql}）：{e}")
            continue
        for r in rows:
            q = (r["q"] or "").strip()
            if len(q) < 4:
                continue
            k = re.sub(r"\s+", "", q)[:60]
            if k in seen:
                continue
            seen.add(k)
            qs.append(q)
    w("\n[1] query 集：%d 条（query_trace ∪ messages.user，按前 60 字符去重）" % len(qs))
    if not qs:
        w("  !! query 集为空，无法标定"); return 1
    w("    样本：" + " | ".join(x.replace("\n", " ")[:20] for x in qs[:6]) + " ...")

    # ── 2) 构造两个场景的候选集（严格对齐消费点口径）──
    # 2a 话题组：按 conversation 取最近 N 个会话，组内按 topic 连续分段
    conv_rows = conn.execute(
        "SELECT DISTINCT conversation_id FROM messages WHERE role='user' "
        "ORDER BY conversation_id DESC LIMIT 6").fetchall()
    conv_ids = [r["conversation_id"] for r in conv_rows]
    groups = []           # [{conv, topic, msgs:[...]}]
    for cid in conv_ids:
        msgs = conn.execute(
            "SELECT content, topic, role FROM messages WHERE conversation_id=? AND content!='' "
            "ORDER BY id", (cid,)).fetchall()
        cur = None
        for m in msgs:
            t = m["topic"]
            if cur and cur["topic"] == t:
                cur["msgs"].append(m)
            else:
                cur = {"conv": cid, "topic": t, "msgs": [m]}
                groups.append(cur)
    # 代表文本 = 段首 3 条消息各截 200 字拼接（与 history.py:137 reprs 完全一致）
    reprs_A = [" ".join((m["content"] or "")[:200] for m in g["msgs"][:3]) for g in groups]
    reprs_A = [t for t in reprs_A if t.strip()]

    # 2b 语义拉回：每会话最近 120 条消息各截 400 字（与 history.py:334 cands 一致）
    cands_B = []
    for cid in conv_ids[:2]:                       # 成本护栏：只取最近 2 个会话
        msgs = conn.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND content!='' "
            "ORDER BY id DESC LIMIT 120", (cid,)).fetchall()
        cands_B += [(m["content"] or "")[:400] for m in msgs if (m["content"] or "")[:400]]
    cands_B = list(dict.fromkeys(cands_B))         # 去重控成本

    w("\n[2] 候选集（严格对齐消费点口径）")
    w("    场景 A 话题组匹配: %d 个话题组代表（段首3条×200字拼接），来自 %d 个会话"
      % (len(reprs_A), len(conv_ids)))
    w("    场景 B 语义拉回  : %d 条历史消息（会话内最近120条×400字，去重）" % len(cands_B))

    if not reprs_A or not cands_B:
        w("  !! 候选集为空（DB 里可能无历史会话消息），无法标定"); return 1

    # ── 3) 向量化（dense 真 embedding；bigram 本地零成本）──
    from knowledge_pipeline import Embedder
    from knowledge_engine import VectorEngine
    ed = Embedder(conn)
    ve = VectorEngine()

    def score_pair(cand_texts, tag):
        """返回 (B_bigram_top1, D_dense_top1)，长度 = len(qs)。"""
        texts = cand_texts + qs
        vecs, version = ed.embed_with_version(texts, batch_size=0)
        if version == "bigram-tf" or not vecs or len(vecs) != len(texts):
            w("  !! 真 embedding 不可用（version=%s）→ 无法标定 dense" % version)
            return None, None, None
        cv = vecs[:len(cand_texts)]
        qv = vecs[len(cand_texts):]
        cb = [ve._vector(t) for t in cand_texts]
        B, D = [], []
        for q, qq in zip(qs, qv):
            bs = [ve._cosine(ve._vector(q), c) for c in cb]
            bs = [x for x in bs if x > 0]
            B.append(max(bs) if bs else 0.0)
            D.append(max(cos(qq, c) for c in cv))
        w("  embedding version=%s dim=%d" % (version, len(vecs[0])))
        return B, D, version

    result = {}

    # ── 场景 A：话题组匹配（bigram 路硬编码 0.12）──
    w("\n" + "-" * 88)
    w("■ 场景 A  topic_group_match  ← history.py:130-140")
    w("  候选口径：话题组代表 = 段首 3 条消息各截 200 字拼接")
    w("  被标定：context.topic_group_match_dense（当前 0.30）；bigram 路为**硬编码 0.12**")
    B, D, ver = score_pair(reprs_A, "A")
    if B:
        w("  " + dist_line("bigram top1", B))
        w("  " + dist_line("dense  top1", D))
        w("")
        w("  %-16s %-10s %-14s %s" % ("bigram 阈值", "正例率", "等价 dense", "说明"))
        mapping = []
        for t_b in (0.12,):
            t_d, r = equiv_dense(t_b, B, D)
            w("  %-16.4f %-10.4f %-14.4f %s" % (t_b, r, t_d, "硬编码阈值 → dense 等价"))
            mapping.append({"bigram": t_b, "pos_rate": round(r, 4), "dense": round(t_d, 4)})
        w("  ---- dense top1 分位：p25=%.4f p50=%.4f p75=%.4f p90=%.4f p95=%.4f p99=%.4f"
          % (pct(D, .25), pct(D, .50), pct(D, .75), pct(D, .90), pct(D, .95), pct(D, .99)))
        w("  当前值 0.30 的通过率 = %.1f%%（dense 分布中 ≥0.30 的 query 占比）"
          % (sum(1 for x in D if x >= 0.30) / max(len(D), 1) * 100))
        result["topic_group_match"] = {
            "candidates": len(reprs_A), "queries": len(qs), "embed_version": ver,
            "mapping": mapping,
            "dense_pct": {"p25": pct(D, .25), "p50": pct(D, .50), "p75": pct(D, .75),
                          "p90": pct(D, .90), "p95": pct(D, .95), "p99": pct(D, .99)},
            "cur_0.30_pass_rate": round(sum(1 for x in D if x >= 0.30) / max(len(D), 1), 4),
        }

    # ── 场景 B：语义拉回（bigram 路 0.15，含话题域加权）──
    w("\n" + "-" * 88)
    w("■ 场景 B  topic_retrieve  ← history.py:322-345")
    w("  候选口径：会话内最近 120 条消息各截 400 字")
    w("  被标定：context.topic_retrieve_threshold_dense（当前 0.35）；bigram 路 0.15")
    B2, D2, ver2 = score_pair(cands_B, "B")
    if B2:
        w("  " + dist_line("bigram top1(未加权)", B2))
        w("  " + dist_line("dense  top1", D2))
        w("")
        w("  %-16s %-10s %-14s %s" % ("bigram 阈值", "正例率", "等价 dense", "说明"))
        mapping = []
        for t_b in (0.15,):
            t_d, r = equiv_dense(t_b, B2, D2)
            w("  %-16.4f %-10.4f %-14.4f %s" % (t_b, r, t_d, "topic_retrieve_threshold → dense 等价"))
            mapping.append({"bigram": t_b, "pos_rate": round(r, 4), "dense": round(t_d, 4)})
        w("  ---- dense top1 分位：p25=%.4f p50=%.4f p75=%.4f p90=%.4f p95=%.4f p99=%.4f"
          % (pct(D2, .25), pct(D2, .50), pct(D2, .75), pct(D2, .90), pct(D2, .95), pct(D2, .99)))
        w("  当前值 0.35 的通过率 = %.1f%%"
          % (sum(1 for x in D2 if x >= 0.35) / max(len(D2), 1) * 100))
        w("  ⚠️ 口径说明：bigram 路线上还叠**话题域加权**（候选 topic 与当前话题 ts≥0.2 时 +0.25*ts）。")
        w("     上表 bigram 分**未复现该加权** → 真实 bigram 分更高，等价 dense 阈值也会更高。")
        w("     故表中 dense 等价值是**下界**（保守方向：宁宽勿严）。")
        result["topic_retrieve"] = {
            "candidates": len(cands_B), "queries": len(qs), "embed_version": ver2,
            "mapping": mapping,
            "dense_pct": {"p25": pct(D2, .25), "p50": pct(D2, .50), "p75": pct(D2, .75),
                          "p90": pct(D2, .90), "p95": pct(D2, .95), "p99": pct(D2, .99)},
            "cur_0.35_pass_rate": round(sum(1 for x in D2 if x >= 0.35) / max(len(D2), 1), 4),
            "caveat": "bigram 路话题域加权未复现，等价 dense 阈值为下界",
        }

    w("\n" + "=" * 88)
    w("结论口径：等价 dense 阈值 = 「保持原判定通过率」的迁移点，不是最优 F1 点。")
    w("语料边界：query 来自本机历史（%d 条，非人工标注集）；候选来自 DB 现存会话。" % len(qs))
    w("=" * 88)

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("JSON 已写出：%s" % OUT_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
