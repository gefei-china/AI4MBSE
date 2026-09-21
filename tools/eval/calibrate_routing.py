# -*- coding: utf-8 -*-
"""路由阈值 + 三因子权重标定（调研文档 §六 建议 #9，2026-09-21）。

## 为什么要单独标定
`rag.route_threshold`(0.75) 是**唯一决定「是否短路跳过向量检索」的闸门**：
    conf >= thr → route=graph，**向量检索与 LLM 重排全部跳过**（延迟差 ~600×）
    conf <  thr → 还会跑向量检索（route=mixed/vector）
而 `conf` 由三因子加权（w_coverage/w_relations/w_typing，默认 .5/.3/.2）产生。
两者都是拍的默认值，从未用标注样本验过。

## 标签怎么来（**弱标注，用它下结论前必读**）
· 正例 = `graph_cases` 18 条：由「release 分支真实关系的两端实体」派生 →
  期望能拿到高置信图谱命中（该短路）。
· 负例 = `neg_cases` 8 条：域外闲聊 / 域内不存在 / 术语邻域不相关 → **不得**被高置信图谱命中
  （否则就是缺陷 #2：无答案问题拿到 conf≈0.9，用无关子图硬答）。
· ⚠️ 判据是「**该不该短路**」，不等于 `expect_route_in`：后者两边都允许 'mixed'，
  无法区分「短路」与「补一脚向量」。故正例记 want_graph=1、负例 want_graph=0。
· ⚠️ 「名字能匹配」≠「用户意图就该走图谱」→ 用于改**生产默认值**前必须人工复核标签。

## 做法（离线重算，不重复跑检索）
跑一轮 retrieve 记录 (n_ent, n_rel, n_typed, conf)，随后**任意**权重/阈值都在内存里重算
（`_graph_confidence` 三因子可从 entities/relations 精确复原——脚本内先自证这一点）。

运行：.venv/Scripts/python.exe -X utf8 tools/eval/calibrate_routing.py
"""
import io
import itertools
import json
import os
import statistics
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
sys.path.insert(0, str(REPO))

try:
    import httpx  # noqa: F401
except Exception as e:
    print("⛔ 缺 httpx（%s）→ 链路会静默降级，标定无意义。请用项目 venv 运行。" % e)
    sys.exit(2)

from agent.rag import GraphRAG                       # noqa: E402
from core import config as _cfg                      # noqa: E402
from knowledge_engine import QueryRouter             # noqa: E402

_cfg.reload()
_orig_record = QueryRouter.record
QueryRouter.record = lambda *a, **k: None            # 纪律：标定不污染线上路由日志

EVALSET = REPO / "tmp" / "p21" / "evalset.json"
OUT = REPO / "tmp" / "p21" / "routing_calibration.json"

W0 = (float(_cfg.get("rag", "w_coverage", 0.5)),
      float(_cfg.get("rag", "w_relations", 0.3)),
      float(_cfg.get("rag", "w_typing", 0.2)))


def conf_of(n_ent, n_rel, n_typed, w):
    """与 `GraphRAG._graph_confidence` 同构的离线重算。"""
    if n_ent <= 0:
        return 0.0
    hit = min(n_ent / 5.0, 1.0)
    rels = min(n_rel / 3.0, 1.0)
    typed = n_typed / n_ent
    return min(w[0] * hit + w[1] * rels + w[2] * typed, 1.0)


def typed_count(ents):
    return sum(1 for e in ents
               if str(e.get("entity_type") or "").strip()
               and str(e.get("entity_type")) != str(e.get("name")))


def main():
    ev = json.loads(EVALSET.read_text(encoding="utf-8"))
    cases = ([{**c, "want_graph": 1} for c in ev["graph_cases"]]
             + [{**c, "want_graph": 0} for c in ev["neg_cases"]])

    print("=" * 78)
    print("路由阈值 + 三因子权重标定")
    print("=" * 78)
    print("标签来源  : graph_cases %d（正=应短路） / neg_cases %d（负=不得短路）"
          % (len(ev["graph_cases"]), len(ev["neg_cases"])))
    print("当前生效  : route_threshold=%s  w=(%.2f, %.2f, %.2f)"
          % (_cfg.get("rag", "route_threshold", 0.75), *W0))
    print("\n采样中（每条跑一次 retrieve，记录图谱侧因子）…")

    rag = GraphRAG()
    obs = []
    t0 = time.time()
    for i, c in enumerate(cases, 1):
        st = time.time()
        try:
            with redirect_stdout(io.StringIO()):
                r = rag.retrieve(c["query"], "dev")
        except Exception as e:
            obs.append({**c, "error": str(e)})
            print("  [%2d/%d] %-42s 失败: %s" % (i, len(cases), c["query"][:42], e))
            continue
        ents = r.get("entities") or []
        rels = r.get("relations") or []
        o = {
            "id": c["id"], "kind": c["kind"], "want_graph": c["want_graph"],
            "query": c["query"], "route": r.get("route"), "reason": r.get("route_reason"),
            "conf_reported": float(r.get("confidence") or 0.0),
            "n_ent": len(ents), "n_rel": len(rels), "n_typed": typed_count(ents),
            "latency_ms": int(r.get("latency_ms") or (time.time() - st) * 1000),
        }
        obs.append(o)
        print("  [%2d/%d] %-40s route=%-7s conf=%.3f n_ent=%d n_rel=%d"
              % (i, len(cases), c["query"][:40], o["route"], o["conf_reported"],
                 o["n_ent"], o["n_rel"]))
    print("采样耗时 %.1fs" % (time.time() - t0))

    ok = [o for o in obs if "error" not in o]

    # ── 自证：离线重算必须与线上 reported 一致，否则后续所有扫描都是错的 ──
    bad = [o for o in ok
           if abs(conf_of(o["n_ent"], o["n_rel"], o["n_typed"], W0) - o["conf_reported"]) > 1e-6]
    print("\n[自证] 用当前权重离线重算 conf，与线上 reported 一致的样本 %d/%d"
          % (len(ok) - len(bad), len(ok)))
    if bad:
        print("      ⛔ 不一致样本（离线重算与线上不同构，扫描结果不可用）：")
        for o in bad[:5]:
            print("        %s n_ent=%d n_rel=%d n_typed=%d 重算=%.6f reported=%.6f"
                  % (o["id"], o["n_ent"], o["n_rel"], o["n_typed"],
                     conf_of(o["n_ent"], o["n_rel"], o["n_typed"], W0), o["conf_reported"]))
        sys.exit(1)

    pos = [o for o in ok if o["want_graph"] == 1]
    neg = [o for o in ok if o["want_graph"] == 0]

    def score(w, thr):
        tp = sum(1 for o in pos if o["n_ent"] > 0 and conf_of(o["n_ent"], o["n_rel"], o["n_typed"], w) >= thr)
        fp = sum(1 for o in neg if o["n_ent"] > 0 and conf_of(o["n_ent"], o["n_rel"], o["n_typed"], w) >= thr)
        tpr = tp / len(pos) if pos else 0.0
        fpr = fp / len(neg) if neg else 0.0
        return tpr, fpr, (tpr + (1 - fpr)) / 2.0, tp, fp

    print("\n── 1) 只扫阈值（权重保持现网 %.2f/%.2f/%.2f）──" % W0)
    print("  阈值   TPR(正例短路)  FPR(负例误短路)  平衡准确率   误短路样例")
    grid_t = [round(0.30 + 0.05 * i, 2) for i in range(14)]
    rows_t = []
    for thr in grid_t:
        tpr, fpr, bal, tp, fp = score(W0, thr)
        rows_t.append((thr, tpr, fpr, bal, tp, fp))
        flag = " ← 当前" if abs(thr - float(_cfg.get("rag", "route_threshold", 0.75))) < 1e-9 else ""
        bad_ids = ",".join(o["id"] for o in neg
                           if o["n_ent"] > 0 and conf_of(o["n_ent"], o["n_rel"], o["n_typed"], W0) >= thr)[:28]
        print("  %.2f      %.3f            %.3f          %.3f      %s%s"
              % (thr, tpr, fpr, bal, bad_ids, flag))

    cur_thr = float(_cfg.get("rag", "route_threshold", 0.75))
    cur = score(W0, cur_thr)
    best_t = max(rows_t, key=lambda x: (x[3], -x[5]))
    print("\n  当前阈值 %.2f → 平衡准确率 %.3f（正例短路 %d/%d，负例误短路 %d/%d）"
          % (cur_thr, cur[2], cur[3], len(pos), cur[4], len(neg)))
    print("  单扫阈值最优 %.2f → 平衡准确率 %.3f（正例短路 %d/%d，负例误短路 %d/%d）"
          % (best_t[0], best_t[3], best_t[4], len(pos), best_t[5], len(neg)))

    print("\n── 2) 同时扫权重（步长 0.1，和为 1.0）× 阈值 ──")
    combos = [(round(a / 10, 1), round(b / 10, 1), round((10 - a - b) / 10, 1))
              for a in range(11) for b in range(11 - a)]
    best = None
    for w in combos:
        for thr in grid_t:
            tpr, fpr, bal, tp, fp = score(w, thr)
            key = (bal, -fpr, -abs(w[0] - W0[0]) - abs(w[1] - W0[1]) - abs(w[2] - W0[2]))
            if best is None or key > best[0]:
                best = (key, w, thr, tpr, fpr, bal, tp, fp)
    _, bw, bthr, btpr, bfpr, bbal, btp, bfp = best
    print("  最优组合：w=(coverage %.1f, relations %.1f, typing %.1f)  threshold=%.2f"
          % (*bw, bthr))
    print("            平衡准确率 %.3f（正例短路 %d/%d，负例误短路 %d/%d）"
          % (bbal, btp, len(pos), bfp, len(neg)))
    print("  对比现网：w=(%.2f, %.2f, %.2f) threshold=%.2f → 平衡准确率 %.3f"
          % (*W0, cur_thr, cur[2]))

    # ── 3) 负例高置信明细：这些是「该不该错」的核心证据 ──
    print("\n── 3) 负例明细（conf 越高越危险：会用无关子图硬答）──")
    for o in sorted(neg, key=lambda x: -conf_of(x["n_ent"], x["n_rel"], x["n_typed"], W0)):
        c0 = conf_of(o["n_ent"], o["n_rel"], o["n_typed"], W0)
        print("  %-8s conf=%.3f  n_ent=%-2d n_rel=%-3d  %-34s %s"
              % (o["id"], c0, o["n_ent"], o["n_rel"], o["query"][:34],
                 "⚠ 会短路" if c0 >= cur_thr else ""))

    print("\n── 4) 正例明细（conf 越低越危险：该短路却没短路，白付向量+重排延迟）──")
    for o in sorted(pos, key=lambda x: conf_of(x["n_ent"], x["n_rel"], x["n_typed"], W0)):
        c0 = conf_of(o["n_ent"], o["n_rel"], o["n_typed"], W0)
        print("  %-8s conf=%.3f  n_ent=%-2d n_rel=%-3d  %-34s %s"
              % (o["id"], c0, o["n_ent"], o["n_rel"], o["query"][:34],
                 "" if c0 >= cur_thr else "⚠ 未短路"))

    # ── 5) 阶梯：阈值改变的代价（TTFT 视角）──
    lat_g = statistics.median([o["latency_ms"] for o in pos if o["n_ent"] > 0] or [0])
    lat_n = statistics.median([o["latency_ms"] for o in neg] or [0])
    print("\n── 5) 延迟参照 ──")
    print("  图谱短路路径中位延迟 %.0f ms（样本 %d）" % (lat_g, len(pos)))
    print("  走到向量路径中位延迟 %.0f ms（样本 %d，负例必然走向量）" % (lat_n, len(neg)))
    print("  → 阈值降低会使更多查询短路（省延迟），但误短路 = 用无关子图硬答。")

    OUT.write_text(json.dumps({
        "schema": "p1-6/routing-calibration@1",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "current": {"w": W0, "threshold": cur_thr, "balanced_acc": round(cur[2], 4),
                    "tpr": round(cur[0], 4), "fpr": round(cur[1], 4)},
        "best_threshold_only": {"threshold": best_t[0], "balanced_acc": round(best_t[3], 4),
                                "tpr": round(best_t[1], 4), "fpr": round(best_t[2], 4)},
        "best_joint": {"w": bw, "threshold": bthr, "balanced_acc": round(bbal, 4),
                       "tpr": round(btpr, 4), "fpr": round(bfpr, 4)},
        "threshold_sweep": [{"threshold": t, "tpr": round(p, 4), "fpr": round(f, 4),
                             "balanced_acc": round(b, 4)} for t, p, f, b, _, _ in rows_t],
        "observations": obs,
        "caveat": "正例/负例均为弱标注（名字可链接 ≠ 意图该走图谱）；共 %d 正 / %d 负，"
                  "负例仅 %d 条，FPR 分辨率 1/%d ≈ %.3f。" % (len(pos), len(neg), len(neg), len(neg), 1.0 / max(len(neg), 1)),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n→ %s" % OUT)
    QueryRouter.record = _orig_record


if __name__ == "__main__":
    main()
