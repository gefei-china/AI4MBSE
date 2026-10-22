#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RAG 检索评测进 CI —— 补上断链①（2026-10-22）。

## 为什么要有这道门禁

评估发现：**RAG 检索评测完全不在 CI 里**。`ci.yml` 对 `run_eval` /
`eval_retrieval` / `build_evalset` 的引用是零命中——检索质量的所有分数
（recall@5=0.90、MRR=0.867、nDCG=0.732）本质上是"有人手动跑了一次"的
产物：既无 PR 阻断能力，也无定时回归。

对照之下，意图路由有两道门禁在CI 里。**检索质量的度量比决策质量更完备
（双层漏斗分解 + 负对照 + 指纹漂移拒绝），却完全没被使用** —— 这是最
不该发生的一种浪费。

## 为什么是"消费报告"而不是"CI 里重跑检索"——以及一个硬约束

**硬约束（决定了架构，先说这个）**：`mbse.db`（约 378MB）**不入 git**
（见 .gitignore的 `*.db` 规则），CI 跑在 checkout 出的干净树上。
⇒ **PR CI 上根本没有语料库，检索评测无法运行**（跑出来 recall 全 0，
那是"没数据"不是"质量差"）。任何"在 PR 流水线里直接跑 run_eval"的方案
都会产出一个必然红的门禁，然后被人加进豁免名单 —— 那比没有门禁更糟。

因此分工是：
- **本门禁（verify_rag_eval_gate.py）**：只做**指标判定**，消费已有报告。
  不重跑检索 ⇒ PR CI 上零成本、无外部依赖。
- **nightly 作业**：跑 `build_evalset` → `run_eval` → 产出报告 → 跑本门禁。
  这同时补上了评估指出的第二条断链（nightly 从未落地 ⇒ 模型语义层漂移零监控）。

报告缺失时本门禁**判红并打印生成命令**（除非显式 `--allow-missing`）：
静默绿等于形同虚设。

## 取舍要说清楚

它防的是"**指标退化**"，不防"检索链路被改坏"——后者由检索链路自身的门禁负责。
换来的是一个几乎零成本、且不可被忽略的信号。若强行在 PR 内重跑，需要引入
embedding 服务依赖 + 分钟级耗时 + 网络抖动导致的红绿假阳，得不偿失。

## 判据（四项指标 + 三项判据自证）

指标下限读 `core.config.eval_gates`（单一真源，2026-10-22 起）：
- doc 域 `recall@5 ≥ retrieval_min_recall_at_5`（实测 0.90，阈值 0.85）
- graph 域 `route_acc ≥ retrieval_min_route_acc`（实测 1.0，阈值 0.95）
- neg 域 `route_ok ≥ retrieval_min_route_ok`（实测 0.875，阈值 0.75）
- `rerank_drop_rate ≤ retrieval_max_rerank_drop`（实测 0.0，阈值 0.15）

三项判据自证（防"指标算错却全绿"）：
- **D1 负对照必须非零**：负对照是把 gold 标签轮转后重算，真分数与轮转分数
  都高 ⇒ 指标没在测对齐关系 ⇒ **假绿**。实测真实 recall@10=0.9、轮转 0.0。
  ⚠ 判据是"拉开量级"不是"恒等于 0"（2026-10-22 15:21 修正）：轮转后
  recall@10 有 ~2% 机遇撞中（实测 0.02），恒零判据会随机红；现判
  "轮转后全部 ≤ retrieval_control_max_recall（0.05）"，与真实 0.90
  拉开 18 倍即健康，真假绿态（轮转也 ~0.9）仍判红。
- **D2 样本量下限**：n 过小则比例类指标分辨率不足（neg 域 8 条时 FPR
  分辨率仅 1/8）。低于下限报"样本不足"而非"通过"。
- **D3 无语料漂移**：报告带 `fingerprint_drift` 非空 ⇒ 该分数不可作依据
  （实测唯一一份正式报告就带 drift 标记）。

## 变异自证（M1~M4）

- M1 把某指标改成塌陷值 ⇒ 必须判红
- M2 把负对照改成非零（假绿态）⇒ 必须判红
- M3 把样本量压到下限以下 ⇒ 必须判"样本不足"
- M4 把 drift 填上 ⇒ 必须判红

用法：
  .venv/Scripts/python.exe tools/verify/verify_rag_eval_gate.py
  .venv/Scripts/python.exe tools/verify/verify_rag_eval_gate.py --report tmp/p21/eval_report_x.json
  .venv/Scripts/python.exe tools/verify/verify_rag_eval_gate.py --allow-missing   # 报告不存在时判 SKIP 而非红
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_REPORT = os.path.join(ROOT, "tmp", "p21", "eval_report_ci.json")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def _rec(name, ok, detail="", status=None):
    st = status or (PASS if ok else FAIL)
    row = (st, name, "" if st != FAIL else str(detail)[:300])
    if _IN_MUT[0]:
        _MUT_ROWS.append(row)      # 变异体内只留记录，不打印（外层只报 M1~M4 结论）
    else:
        _results.append(row)
        print("  [%s] %s" % (row[0], row[1]) + ("  " + row[2] if row[2] else ""))
    return st == PASS


def _gate(key, fallback):
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from core.config import get as _g
        v = _g("eval_gates", key, None)
        return fallback if v is None else v
    except Exception:
        return fallback


# 样本量下限：doc/graph 各 ≥5，neg ≥5（neg 域实际 8 条；低于 5 时
# FPR 分辨率 <0.2，报 SKIP 而非"通过"）
MIN_N_DOC = 5
MIN_N_GRAPH = 5
MIN_N_NEG = 5


def judge(report, thr=None):
    """把一份 run_eval 报告变成断言。返回是否全绿。"""
    thr = thr or {}
    t_rec5 = thr.get("rec5", _gate("retrieval_min_recall_at_5", 0.85))
    t_racc = thr.get("racc", _gate("retrieval_min_route_acc", 0.95))
    t_rok = thr.get("rok", _gate("retrieval_min_route_ok", 0.75))
    t_drop = thr.get("drop", _gate("retrieval_max_rerank_drop", 0.15))
    need_ctrl = bool(_gate("require_negative_control", 1))

    ok = True
    # D3 语料漂移（先判：漂移下所有分数都不可作依据）
    drift = report.get("fingerprint_drift") or []
    ok &= _rec("D3 无语料漂移（漂移下分数不作依据）", not drift,
               "drift=%s（评测集与当前语料不一致，须重建评测集后重跑）" % (drift[:2],))

    s = report.get("summary") or {}
    doc = s.get("doc") or {}
    graph = s.get("graph") or {}
    neg = s.get("neg") or {}

    # D2 样本量下限
    for label, blk, lim in (("doc", doc, MIN_N_DOC), ("graph", graph, MIN_N_GRAPH),
                            ("neg", neg, MIN_N_NEG)):
        n = blk.get("n", 0)
        if n < lim:
            ok &= _rec("D2 %s 域样本量 ≥%d" % (label, lim), False,
                       "n=%s，样本不足时比例类指标无判别力" % n, status=SKIP)
        else:
            _rec("D2 %s 域样本量 %d ≥%d" % (label, n, lim), True)

    # 四项指标下限
    r5 = doc.get("recall@5")
    if r5 is not None:
        ok &= _rec("M1a doc recall@5 %.3f ≥ %.2f" % (r5, t_rec5), r5 >= t_rec5,
                   "低于下限（实测基线 0.90）")
    racc = graph.get("route_acc")
    if racc is not None:
        ok &= _rec("M1b graph route_acc %.3f ≥ %.2f" % (racc, t_racc), racc >= t_racc,
                   "该走图谱时没走对")
    rok = neg.get("route_ok")
    if rok is not None:
        ok &= _rec("M1c neg route_ok %.3f ≥ %.2f" % (rok, t_rok), rok >= t_rok,
                   "无依据时不该硬答（域外/不存在问题）")
    drop = doc.get("rerank_drop_rate")
    if drop is not None:
        ok &= _rec("M1d rerank_drop_rate %.3f ≤ %.2f" % (drop, t_drop), drop <= t_drop,
                   "重排把池里召回的正确答案挤下去了")

    # D1 负对照必须"塌到 0"（真分数高、轮转分数也高 ⇒ 假绿）
    if need_ctrl:
        ctrl = report.get("negative_control") or {}
        crec = ctrl.get("recall") or {}
        # 取轮转后的 recall@k；接近 0 才是健康的
        vals = [v for v in crec.values() if isinstance(v, (int, float))]
        if not vals:
            ok &= _rec("D1 负对照已跑（缺对照无法排除假绿）", False,
                       "报告无 negative_control；建议加跑 --control", status=SKIP)
        else:
            # ★ 2026-10-22 15:21 实测修正：原判据是 max(vals) == 0.0（恒零），
            #   但轮转是确定性重排、50 例里 recall@10 有 ~2% 概率撞上一个
            #   机遇命中（本次 0.02）⇒ 门禁随机红。判据的**意图**是
            #   "真分数与轮转分数拉开量级"（文件头原话），不是"恒等于 0"：
            #   实现严于意图 = 拿机遇噪声当假绿 = 每 ~7 周红一次的红灯。
            #   红灯狼来了的下场是被人加豁免（本工程自己的纪律），故改为
            #   量级判定：轮转后全部 ≤ retrieval_control_max_recall（0.05）
            #   ⇒ 与真实 0.90 拉开 18 倍；真假绿态（轮转也 ~0.9）仍判红。
            t_ctrl = thr.get("ctrl_max", _gate("retrieval_control_max_recall", 0.05))
            ok &= _rec("D1 负对照塌到 ≤%.2f（真分数与轮转分数须拉开量级）" % t_ctrl,
                       max(vals) <= t_ctrl,
                       "轮转后 recall=%s，与真实分数相近 ⇒ 假绿" % crec)
    return ok


def run_mutations(base_report):
    """M1 指标塌陷判红 / M2 负对照非零判红 / M3 样本不足判 SKIP / M4 drift 判红。

    ⚠️ 每次 judge() 前必须清空 _MUT_ROWS —— 否则上一轮变异留下的断言行会
    污染下一轮的判定与统计（"只看本轮新增"这件事在本脚本第一版就漏了）。
    """
    ok = True
    import copy

    def _judge_iso(r):
        """隔离跑一次 judge：只把本轮新增的行算作结果。

        ⚠️ 必须同时置 _IN_MUT —— 否则 _rec 会把行写进 _results（常态列表）
        而非 _MUT_ROWS，导致 _MUT_ROWS 为空、变异判定永远拿不到证据。
        第一版漏了这行，现象是"变异明明触发了，判定却恒为 FAIL"。
        """
        del _MUT_ROWS[:]
        saved = _IN_MUT[0]
        _IN_MUT[0] = True
        try:
            judge(r)
            return list(_MUT_ROWS)
        finally:
            _IN_MUT[0] = saved

    # M1：把 recall@5 打到塌陷值
    r = copy.deepcopy(base_report)
    r.setdefault("summary", {}).setdefault("doc", {})["recall@5"] = 0.10
    rows = _judge_iso(r)
    red = any(st == FAIL for st, _n, _d in rows)
    ok &= _rec("M1 指标塌陷 ⇒ 判红", red, status=None)

    # M2：负对照非零（假绿态）
    r = copy.deepcopy(base_report)
    r["negative_control"] = {"recall": {"recall@1": 0.9, "recall@5": 0.9,
                                        "recall@10": 0.9}, "mrr@10": 0.9}
    rows = _judge_iso(r)
    red = any(st == FAIL for st, _n, _d in rows)
    ok &= _rec("M2 负对照非零（假绿态）⇒ 判红", red)

    # M3：样本量压到下限以下 ⇒ 判 SKIP
    r = copy.deepcopy(base_report)
    r.setdefault("summary", {}).setdefault("neg", {})["n"] = 2
    rows = _judge_iso(r)
    got_skip = any(st == SKIP for st, _n, _d in rows)
    ok &= _rec("M3 样本量不足 ⇒ 判 SKIP（而非误判为通过）", got_skip)

    # M4：填上语料漂移 ⇒ 判红
    r = copy.deepcopy(base_report)
    r["fingerprint_drift"] = ["release 实体名集合 abc → def（图谱已被改写）"]
    rows = _judge_iso(r)
    red = any(st == FAIL for st, _n, _d in rows)
    ok &= _rec("M4 语料漂移 ⇒ 判红", red)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help="run_eval 产出的报告 JSON 路径")
    ap.add_argument("--allow-missing", action="store_true",
                    help="报告不存在时判 SKIP 而非 FAIL（本地尚未跑过评测时用）")
    ap.add_argument("--no-mut", action="store_true", help="跳过变异自证")
    args = ap.parse_args()

    print("=" * 74)
    print("RAG 检索评测 CI 门禁（消费 run_eval 报告，不重跑检索）")
    print("=" * 74)
    print("报告: %s" % os.path.relpath(args.report, ROOT).replace("\\", "/"))

    if not os.path.exists(args.report):
        print("\n⚠ 未找到评测报告 —— 本门禁无法判定。")
        print("  生成方式（两条命令，需真实 embedding 服务）：")
        print('    .venv/Scripts/python.exe -X utf8 tools/eval/build_evalset.py')
        print('    .venv/Scripts/python.exe -X utf8 tools/eval/run_eval.py '
              '--tag ci --out tmp/p21/eval_report_ci.json --control')
        print("  ⚠ 这正是评估 P1-1 指出的断链：检索评测不在 CI ⇒ CI 对检索质量无感知。")
        return 0 if args.allow_missing else 1

    with io.open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    ok = judge(report)

    if not args.no_mut:
        print("\n=== 变异自证 ===")
        ok &= run_mutations(report)

    bad = [r for r in _results if r[0] == FAIL]
    skips = [r for r in _results if r[0] == SKIP]
    print("\n" + "=" * 74)
    print("常态断言 %d  PASS %d  FAIL %d  SKIP %d"
          % (len(_results), len(_results) - len(bad) - len(skips), len(bad), len(skips)))
    if _MUT_ROWS:
        mo = [r for r in _MUT_ROWS if r[1][:1] == "M"]
        bm = [r for r in mo if r[0] != PASS]
        print("变异断言 %d  PASS %d  FAIL %d" % (len(mo), len(mo) - len(bm), len(bm)))
    print("=" * 74)
    print("结论：%s" % ("全部通过" if ok and not bad else "未通过"))
    return 0 if (ok and not bad) else 1


if __name__ == "__main__":
    sys.exit(main())