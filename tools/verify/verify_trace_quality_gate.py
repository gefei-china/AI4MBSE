#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Trace 一致性评测门禁（2026-10-22）—— 补上断链②的 CI 消费侧。

## 为什么要有它

`tools/eval/eval_trace_consistency.py` 把"生产人工裁决 ↔ 当前路由"
变成了一致率指标，但**指标不进门禁就等于没接**（本工程评测体系的
完成态判据：有东西在拦着退化，不是有脚本能跑）。本门禁与检索/
生成质量门禁同构：只消费报告做判定，**不重跑评测**（评测要建生产
路由器、读生产库，CI 干净树上没有）。

## 判据（按"防塌陷/防假绿"分级）

| 编号 | 判据 | 阈值键 | 方向 |
|---|---|---|---|
| T0 | 黄金样本自证必须通过（指标算错 ⇒ 数字不可信） | — | 硬性 |
| T1 | gold 失效占比 ≤ 上限（样本池被删/被改 ⇒ 不出分） | trace_max_stale_ratio | 硬性 |
| T2 | positive 样本量 ≥ 下限（0=没测到判红；不足=SKIP） | trace_min_positive | 硬性/SKIP |
| T3 | 裁决一致率 ≥ 下限 | trace_min_agreement | 硬性 |
| T4 | 重错率 ≤ 上限（无路由纠错类负样本 ⇒ SKIP，不判 0） | trace_max_repeat_mistake | 条件 |
| T5 | 负对照必须可判别（差距 ≥ 下限；被关 ⇒ 判红） | trace_control_min_gap | 硬性 |
| T6 | 路由异常 = 0（半途失败的跑批给出的是有偏指标） | — | 硬性 |

T4/T5 的条件判定是本门禁最重要的设计，理由与生成质量门禁的 G6
完全同构：**在不具判别力的样本上设阈值，要么恒绿要么恒红**。
当前生产 13 条驳回全是"非用户原话不应入集"的池有效性裁决
（路由纠错类 n=0）⇒ 重错率没有分母，判 SKIP 并说明原因；
等出现真负样本，同一份代码自动升级为硬门禁。

## 变异自证（M1~M6）

每项判据都必须能被对应变异打红/打醒 —— 证明断言真会拦，而不是
摆设。SKIP 类变异（样本量不足）证明的是"不误判通过"。

用法：
  .venv/Scripts/python.exe tools/verify/verify_trace_quality_gate.py
  .venv/Scripts/python.exe tools/verify/verify_trace_quality_gate.py --report tmp/p21/x.json
  .venv/Scripts/python.exe tools/verify/verify_trace_quality_gate.py --allow-missing
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_REPORT = os.path.join(ROOT, "tmp", "p21", "eval_trace_consistency.json")

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
        _MUT_ROWS.append(row)
    else:
        _results.append(row)
        print("  [%s] %s" % (row[0], row[1]) + ("  " + row[2] if row[2] else ""))
    return st == PASS


def _gate(key, fallback):
    """惰性读 core.config.eval_gates（顶层 import 会绑死 DB 路径，见 P0-1 教训）。"""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from core.config import get as _g
        v = _g("eval_gates", key, None)
        return fallback if v is None else v
    except Exception:
        return fallback


def judge(report, thr=None):
    """把一份 trace 一致性报告变成断言。返回本调用是否无 FAIL（SKIP 不算失败）。"""
    mark = len(_results)
    thr = thr or {}
    t_agree = thr.get("agree", _gate("trace_min_agreement", 0.75))
    t_rep = thr.get("rep", _gate("trace_max_repeat_mistake", 0.50))
    t_min_n = thr.get("min_n", _gate("trace_min_positive", 20))
    t_gap = thr.get("gap", _gate("trace_control_min_gap", 0.30))
    t_stale = thr.get("stale", _gate("trace_max_stale_ratio", 0.20))

    ok = True

    # 报告身份（防拿错报告喂门禁 —— 两类报告的字段名不同，喂错会
    # 全部 SKIP 成"假通过"，这正是要防的）
    if report.get("kind") != "trace_consistency":
        ok &= _rec("T0a 报告类型正确（kind=trace_consistency）", False,
                   "kind=%r —— 这是一份别的评测报告，本门禁拒判" % report.get("kind"),
                   status=FAIL)
        return not any(r[0] == FAIL for r in _results[mark:])

    # T0b：黄金样本自证（先判：自证不过 ⇒ 后面所有数字都不可信）
    fx = report.get("fixture_self_check") or {}
    ok &= _rec("T0b 黄金样本自证通过（指标计算正确性）", bool(fx.get("ok")),
               "fixture_self_check.ok=%s —— 指标可能算错，先修评测器" % fx.get("ok"))

    pop = report.get("population") or {}
    pos = report.get("positive") or {}
    neg = report.get("negative") or {}
    ctl = report.get("negative_control") or {}

    # T1：gold 失效占比（样本池被删/被改 ⇒ 分数不是"变差"是"没测到"）
    stale_ratio = pop.get("gold_stale_ratio")
    if stale_ratio is None:
        ok &= _rec("T1 gold 失效占比已上报（报告缺 gold_stale_ratio）", False,
                   "评测器版本过旧", status=SKIP)
    else:
        ok &= _rec("T1 gold 失效占比 %.3f ≤ %.2f" % (stale_ratio, t_stale),
                   stale_ratio <= t_stale,
                   "strong 样本大面积失效 —— 先查 intent_samples 是否被清/被改")

    # T2：positive 样本量（0 = 没测到判红；不足但非 0 = 分辨率不足，SKIP）
    n_pos = pos.get("n") or 0
    if n_pos == 0:
        ok &= _rec("T2 positive 样本非空（0 条 = 没测到，不是质量差）", False,
                   "无有效 positive 样本：gold 全失效或 trace 集为空", status=FAIL)
    elif n_pos < t_min_n:
        ok &= _rec("T2 positive 样本量 ≥%d" % t_min_n, False,
                   "n=%s，样本池疑似被删/库被换" % n_pos, status=SKIP)
    else:
        _rec("T2 positive 样本量 %d ≥%d" % (n_pos, t_min_n), True)

    # T3：裁决一致率（主指标）
    agree = pos.get("agreement")
    if agree is None:
        ok &= _rec("T3 裁决一致率（无有效 positive 样本，无法判定）", False,
                   "positive.agreement=None", status=SKIP)
    else:
        ok &= _rec("T3 裁决一致率 %.3f ≥ %.2f" % (agree, t_agree), agree >= t_agree,
                   "当前路由与生产人工裁决的一致率塌陷 —— 按 §4.2 定位表查"
                   "（规则层/语义层哪一层开始丢分）")

    # T4：重错率（条件判定：无路由纠错类负样本 ⇒ SKIP，绝不判 0）
    rep = neg.get("repeat_mistake_rate")
    n_neg = neg.get("n") or 0
    if rep is None:
        ok &= _rec("T4 重错率判定（路由纠错类负样本 n=%d）" % n_neg, False,
                   "无分母不判 0：当前生产没有'这文本应走 X 不是 Y'的纠错类驳回"
                   "（池有效性驳回不计入——那不是路由错误）", status=SKIP)
    else:
        ok &= _rec("T4 重错率 %.3f ≤ %.2f" % (rep, t_rep), rep <= t_rep,
                   "人工纠正过的路由错误又犯了 —— 修好了它才算进步")

    # T5：负对照（被关 ⇒ 假绿路径，判红；不具判别力 ⇒ SKIP）
    if not ctl.get("enabled"):
        ok &= _rec("T5 负对照已启用（--no-control 是假绿路径）", False,
                   "negative_control.enabled=false —— 没有对照就无法排除"
                   "'指标在测某个恒定东西'", status=FAIL)
    elif not ctl.get("discriminates"):
        gap = ctl.get("gap")
        ok &= _rec("T5 负对照可判别（gold 意图 %s 类，差距 %s）"
                   % (ctl.get("distinct_gold"), gap), False,
                   ctl.get("note") or "洗牌后一致率与真实拉不开差距 ⇒ 指标未测对齐关系",
                   status=SKIP)
    else:
        gap = ctl.get("gap") or 0.0
        ok &= _rec("T5 负对照可判别（真实−洗牌 差距 %.3f ≥ %.2f）" % (gap, t_gap),
                   gap >= t_gap,
                   "差距不足 —— 指标可能没在测'路由↔人工裁决'的对齐关系")

    # T6：路由异常（半途失败的跑批给出的是有偏指标）
    errs = pop.get("route_errors") or 0
    ok &= _rec("T6 路由异常 = 0", errs == 0,
               "%d 条样本路由抛异常（route='error'）—— 跑批不完整，指标有偏" % errs)

    # 本调用的结论 = 新增行里没有 FAIL（SKIP 不是失败，理由同生成质量门禁）
    return not any(r[0] == FAIL for r in _results[mark:])


def run_mutations(base_report):
    """M1~M6：每项判据都必须能被对应变异打红/打醒。"""
    ok = True

    def _judge_iso(r):
        del _MUT_ROWS[:]
        saved = _IN_MUT[0]
        _IN_MUT[0] = True
        try:
            judge(r)
            return list(_MUT_ROWS)
        finally:
            _IN_MUT[0] = saved

    def _expect(cid, mutate, want):
        r = copy.deepcopy(base_report)
        mutate(r)
        rows = _judge_iso(r)
        got = any(st == want for st, _n, _d in rows)
        label = {"FAIL": "判红", "SKIP": "判 SKIP"}.get(want, want)
        return _rec("M%s %s" % (cid, label), got,
                    "变异后未出现预期结果 %s" % want)

    ok &= _expect("1 一致率塌陷", lambda r: r["positive"].update(
        {"agreement": 0.50}), FAIL)
    ok &= _expect("2 重错率爆表", lambda r: r["negative"].update(
        {"repeat_mistake_rate": 0.9, "n": 10}), FAIL)
    ok &= _expect("3 自证被关（假绿路径）", lambda r: update_fixture(r), FAIL)
    ok &= _expect("4 负对照被关", lambda r: r["negative_control"].update(
        {"enabled": False}), FAIL)
    ok &= _expect("5 对照拉不开", lambda r: r["negative_control"].update(
        {"gap": 0.05, "discriminates": False, "note": "变异"}), SKIP)
    ok &= _expect("6 样本量不足 ⇒ SKIP", lambda r: r["positive"].update(
        {"n": 5}), SKIP)
    ok &= _expect("7 零样本 ⇒ 判红（没测到≠质量差）", lambda r: r["positive"].update(
        {"n": 0, "agreement": None}), FAIL)
    ok &= _expect("8 gold 大面积失效", lambda r: r["population"].update(
        {"gold_stale_ratio": 0.5}), FAIL)
    ok &= _expect("9 路由异常 ⇒ 判红", lambda r: r["population"].update(
        {"route_errors": 3}), FAIL)
    ok &= _expect("10 喂错报告 ⇒ 拒判", lambda r: r.update(
        {"kind": "generation_quality"}), FAIL)
    return ok


def update_fixture(r):
    r["fixture_self_check"] = {"ok": False, "cases": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help="eval_trace_consistency.py 产出的报告 JSON")
    ap.add_argument("--allow-missing", action="store_true",
                    help="报告不存在时判 SKIP 而非红（PR 上本地尚未跑过时用）")
    ap.add_argument("--no-mut", action="store_true", help="跳过变异自证")
    args = ap.parse_args()

    print("=" * 74)
    print("Trace 一致性评测门禁（消费报告，不重跑评测）")
    print("=" * 74)
    print("报告: %s" % os.path.relpath(args.report, ROOT).replace("\\", "/"))

    if not os.path.exists(args.report):
        print("\n⚠ 未找到 Trace 一致性评测报告 —— 本门禁无法判定。")
        print("  生成方式（生产库口径，确定性模式推荐进门禁的这一条）：")
        print("    .venv/Scripts/python.exe -X utf8 tools/eval/eval_trace_consistency.py --no-llm")
        print("    # --save 可同时落库 eval_reports（eval_kind=trace_consistency）")
        print("  ⚠ 这正是评估指出的断链②：trace_evalset 228 条生产裁决样本"
              "此前无消费方。")
        return 0 if args.allow_missing else 1

    with io.open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    mark = len(_results)
    ok = judge(report)
    normal_rows = _results[mark:]

    if not args.no_mut:
        print("\n=== 变异自证 ===")
        mark2 = len(_results)
        ok &= run_mutations(report)
        mut_rows = _results[mark2:]
    else:
        mut_rows = []

    def _tally(rows):
        return (len(rows),
                sum(1 for r in rows if r[0] == PASS),
                sum(1 for r in rows if r[0] == FAIL),
                sum(1 for r in rows if r[0] == SKIP))

    n_tot, n_pass, n_fail, n_skip = _tally(normal_rows)
    m_tot, m_pass, m_fail, m_skip = _tally(mut_rows)
    print("\n" + "=" * 74)
    print("常态断言 %d  PASS %d  FAIL %d  SKIP %d" % (n_tot, n_pass, n_fail, n_skip))
    if mut_rows:
        print("变异断言 %d  PASS %d  FAIL %d" % (m_tot, m_pass, m_fail))
    print("=" * 74)
    if n_fail or m_fail:
        print("结论：未通过（FAIL %d + 变异 FAIL %d）" % (n_fail, m_fail))
    elif n_skip:
        print("结论：通过（含 %d 项条件未满足未判定 —— SKIP ≠ 通过，"
              "也不等于失败；条件清单见上行）" % n_skip)
    else:
        print("结论：全部通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
