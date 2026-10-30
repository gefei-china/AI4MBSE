#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成质量评测门禁（2026-10-22）—— 补上断链③的 CI 消费侧。

## 为什么要有它

`tools/eval/eval_generation_quality.py` 把生成质量变成了指标，但指标不进
门禁就等于没接（评估报告的判据：**评测的完成态是"有东西在拦着退化"**，
不是"有脚本能跑"）。本门禁消费评测报告做判定，与检索门禁同构：

- 本门禁只做**指标判定**，不重跑评测（评测要读生产库，CI 干净树上没有）；
- 报告缺失 ⇒ 除非 `--allow-missing`（PR 上用），否则判红：静默绿等于形同虚设。

## 判据（按"防塌陷"分级，不是"越高越好"军备竞赛）

| 编号 | 判据 | 阈值键 | 方向 |
|---|---|---|---|
| G0 | 黄金样本自证必须通过（指标算错 ⇒ 数字不可信） | — | 硬性 |
| G1 | citation_valid_rate ≥ 阈值 | generation_min_citation_valid | 硬性 |
| G2 | citation_usage_rate ≥ 阈值 | generation_min_citation_usage | 硬性 |
| G3 | no_citation_factual_rate ≤ 阈值 | generation_max_no_citation_factual | 硬性 |
| G4 | 样本量 ≥ 下限（不足 ⇒ SKIP 不判通过） | generation_min_answers | 硬性 |
| G5 | schema 探针：无正文引用块 = 0（否则接地抽取静默失效=假绿路径） | — | 硬性 |
| G6 | 接地率：**仅当**实体量 ≥ 下限**且**负对照可判别才判定 | generation_min_grounded_rate | 条件 |
| G7 | 忠实率：judge 自证须过 + 样本足量才判定（LLM 语义层，2026-10-22 晚新增） | generation_min_faithful | 条件 |

G6/G7 的条件判定是本门禁最重要的设计：当前生产样本只有 2 个可核查实体、
池化负对照 0.000 拉不开差距 ⇒ 接地率**不具判别力**，此时判 SKIP 并说明
原因，绝不"设个宽松阈值混过去"（那正是评估报告点名的"假绿比没门禁更糟"）。
等 claim 密度上来（≥30 实体且对照可判别），同一份代码自动升级为硬门禁。
G7 同构：judge 合成对照（逐字引用⇒faithful / 追加无据声称⇒unfaithful）
不过 ⇒ judge 本身坏了，**判红**；LLM 服务不可用导致样本不足 ⇒ SKIP
（环境问题不是质量差）；判不了的（调用/解析失败）不计入分母。

## 变异自证（M1~M5）

- M1 valid 率打到 0.5 ⇒ 红
- M2 usage 打到 0.05 ⇒ 红
- M3 无据声称密度打到 0.9 ⇒ 红
- M4 自证标记改为 False ⇒ 红（"指标算错却全绿"路径）
- M5 样本量压到 5 ⇒ SKIP（而非误判通过）

用法：
  .venv/Scripts/python.exe tools/verify/verify_generation_quality_gate.py
  .venv/Scripts/python.exe tools/verify/verify_generation_quality_gate.py --report tmp/p21/x.json
  .venv/Scripts/python.exe tools/verify/verify_generation_quality_gate.py --allow-missing
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_REPORT = os.path.join(ROOT, "tmp", "p21", "eval_generation_quality.json")

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
    """把一份生成质量报告变成断言。返回本调用是否无 FAIL（SKIP 不算失败）。

    ⚠ 为什么 SKIP 不算失败：G6 接地率判定的 SKIP 是**当前生产常态**
    （可核查实体 2 < 30，负对照拉不开）。若 SKIP 吞掉 ok，门禁今天恒红
    ⇒ 下一步就是被人加豁免 ⇒ 整道门禁死亡，比没有门禁更糟（工程自己的
    纪律原话）。故：FAIL 才红，SKIP 在结论里单独报"条件未满足未判定"。
    """
    mark = len(_results)
    thr = thr or {}
    t_valid = thr.get("valid", _gate("generation_min_citation_valid", 1.0))
    t_usage = thr.get("usage", _gate("generation_min_citation_usage", 0.25))
    t_nocit = thr.get("nocit", _gate("generation_max_no_citation_factual", 0.30))
    t_min_n = thr.get("min_n", _gate("generation_min_answers", 20))
    t_min_ent = thr.get("min_ent", _gate("generation_min_entities", 30))
    t_ground = thr.get("ground", _gate("generation_min_grounded_rate", 0.70))
    t_min_judged = thr.get("min_judged", _gate("generation_min_judged", 10))
    t_faith = thr.get("faith", _gate("generation_min_faithful", 0.40))

    ok = True
    s = report.get("summary") or {}

    # G0：黄金样本自证（先判：自证不过 ⇒ 后面所有数字都不可信）
    fx = report.get("fixture_self_check") or {}
    ok &= _rec("G0 黄金样本自证通过（指标计算正确性）", bool(fx.get("ok")),
               "fixture_self_check.ok=%s —— 指标可能算错，先修评测器" % fx.get("ok"))

    # G4：样本量下限（0 条 = 取材坏了，是硬失败；不足但非 0 = 分辨率不足，SKIP）
    n = s.get("n_answers") or 0
    if n == 0:
        ok &= _rec("G4 样本非空（0 条 = 没测到，不是质量差）", False,
                   "无可评测答案：Mock/附件过滤后为空，或库里没有 text 助手消息",
                   status=FAIL)
    elif n < t_min_n:
        ok &= _rec("G4 样本量 ≥%d" % t_min_n, False,
                   "n=%s，样本池疑似被删/库被换" % n, status=SKIP)
    else:
        _rec("G4 样本量 %d ≥%d" % (n, t_min_n), True)

    # G1：引用编号有效率（编号错位 = 前端可点条目与正文引用脱钩，硬伤）
    v = s.get("citation_valid_rate")
    if v is None:
        ok &= _rec("G1 引用编号有效率（无 [n] 标记，无法核验编号）", False,
                   "样本中没有任何 [n] 引用标记", status=SKIP)
    else:
        ok &= _rec("G1 引用编号有效率 %.3f ≥ %.2f" % (v, t_valid), v >= t_valid,
                   "正文 [n] 超出了 citations 范围 —— 编号体系错位")

    # G2：引用使用率（带引用的答案里真用 [n] 的比例；软约束塌陷即判红）
    u = s.get("citation_usage_rate")
    if u is None:
        ok &= _rec("G2 引用使用率（无带引用的答案，无法判定）", False,
                   "n_with_citations=0", status=SKIP)
    else:
        ok &= _rec("G2 引用使用率 %.3f ≥ %.2f" % (u, t_usage), u >= t_usage,
                   "注入了引用却不在正文标注 —— 引用体系退化为装饰")

    # G3：无据声称密度上限
    nc = s.get("no_citation_factual_rate")
    if nc is None:
        ok &= _rec("G3 无据声称密度（无无引用答案，分母为 0）", False,
                   "n_without_citations=0", status=SKIP)
    else:
        ok &= _rec("G3 无据声称密度 %.3f ≤ %.2f" % (nc, t_nocit), nc <= t_nocit,
                   "含可核查实体却不附引用的答案占比过高")

    # G5：schema 探针（引用块无正文 ⇒ 接地抽取静默失效 ⇒ 假绿路径）
    swc = s.get("citations_without_content")
    if swc is None:
        ok &= _rec("G5 schema 探针已上报（报告缺 citations_without_content）",
                   False, "报告字段缺失，评测器版本过旧", status=SKIP)
    else:
        ok &= _rec("G5 schema 探针：无正文引用块 = 0", swc == 0,
                   "%d 个引用块没有 content —— 接地率会假性冲高" % swc)

    # G6：接地率 —— 只在"样本具判别力"时判定（见文件头设计说明）
    n_ent = s.get("n_entities") or 0
    ctrl = s.get("control") or {}
    g = s.get("grounded_entity_rate")
    if n_ent < t_min_ent:
        ok &= _rec("G6 接地率判定（可核查实体 %d < %d）" % (n_ent, t_min_ent),
                   False, "claim 密度不足，接地率不具判别力；升级门槛见 config",
                   status=SKIP)
    elif not ctrl.get("discriminates"):
        ok &= _rec("G6 接地率判定（负对照拉不开差距）", False,
                   "真实 %.3f vs 池化 %.3f —— 指标未测'答案↔自己证据'的对齐"
                   % (g if g is not None else -1,
                      ctrl.get("pooled_grounded_rate")), status=SKIP)
    elif g is None:
        ok &= _rec("G6 接地率判定（指标缺失）", False, "grounded_entity_rate=None",
                   status=SKIP)
    else:
        ok &= _rec("G6 实体接地率 %.3f ≥ %.2f" % (g, t_ground), g >= t_ground,
                   "答案实体在引用证据中的比例过低")

    # G7：LLM 忠实率（语义层；三态记账 + judge 自证，见文件头设计说明）
    j = report.get("llm_judge") or {}
    if not j.get("enabled"):
        ok &= _rec("G7 忠实率判定（未启用 LLM 评判）", False,
                   "报告无 llm_judge 段或 enabled=false —— 本轮未跑语义层"
                   "（--llm-judge 才启用）", status=SKIP)
    else:
        n_judged = j.get("n_judged") or 0
        ctl = j.get("control") or {}
        # G7a：judge 自证（合成对照）—— 不过 ⇒ judge 坏了，分数不可信，判红
        if ctl.get("ran") and not ctl.get("ok"):
            ok &= _rec("G7a judge 自证通过（合成对照）", False,
                       "%s —— prompt/解析/模型可疑，先修评测器再信分数"
                       % (ctl.get("note") or "对照失败"), status=FAIL)
        elif not ctl.get("ran"):
            ok &= _rec("G7a judge 自证已跑（无带正文引用的答案可构造对照）",
                       False, ctl.get("note") or "对照未建", status=SKIP)
        else:
            _rec("G7a judge 自证通过（pos=faithful / neg=unfaithful）", True)
        # G7b：样本足量（LLM 服务不可用 ⇒ 没测到，不是质量差）
        if n_judged < t_min_judged:
            ok &= _rec("G7b 评判成功条数 ≥%d" % t_min_judged, False,
                       "n_judged=%s（判不了 %s 条：LLM 服务不可用或响应不可"
                       "解析——环境问题，不判通过也不判红）"
                       % (n_judged, j.get("n_failed")), status=SKIP)
        else:
            _rec("G7b 评判成功条数 %d ≥%d" % (n_judged, t_min_judged), True)
        # G7c：忠实率下限（仅前两关都过才判）
        fr = j.get("faithful_rate")
        if fr is None:
            ok &= _rec("G7c 忠实率（无成功评判，无法判定）", False,
                       "faithful_rate=None", status=SKIP)
        elif n_judged >= t_min_judged and ctl.get("ok"):
            ok &= _rec("G7c LLM 忠实率 %.3f ≥ %.2f" % (fr, t_faith),
                       fr >= t_faith,
                       "答案存在引用证据不支撑的具体声称（明细见报告 "
                       "llm_judge.unfaithful_examples）")
    # 本调用的结论 = 新增行里没有 FAIL（SKIP 不是失败，理由见函数 docstring）
    return not any(r[0] == FAIL for r in _results[mark:])


def run_mutations(base_report):
    """M1~M5：每项判据都必须能被对应变异打红/打醒。"""
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

    ok &= _expect("1 编号有效率塌陷", lambda r: r["summary"].update(
        {"citation_valid_rate": 0.5}), FAIL)
    ok &= _expect("2 引用使用率塌陷", lambda r: r["summary"].update(
        {"citation_usage_rate": 0.05}), FAIL)
    ok &= _expect("3 无据声称密度爆表", lambda r: r["summary"].update(
        {"no_citation_factual_rate": 0.9}), FAIL)
    ok &= _expect("4 自证被关（假绿路径）", lambda r: r.update(
        {"fixture_self_check": {"ok": False, "cases": []}}), FAIL)
    ok &= _expect("5 样本量不足 ⇒ SKIP", lambda r: r["summary"].update(
        {"n_answers": 5}), SKIP)
    ok &= _expect("6 零样本 ⇒ 判红（没测到≠质量差）", lambda r: r["summary"].update(
        {"n_answers": 0}), FAIL)
    # ── M7~M10：LLM 语义层（G7）判据自证 ────────────────────────
    ok &= _expect("7 忠实率塌陷", lambda r: r["llm_judge"].update(
        {"faithful_rate": 0.1}), FAIL)
    ok &= _expect("8 评判样本不足 ⇒ SKIP（服务不可用不是质量差）",
                  lambda r: r["llm_judge"].update({"n_judged": 3}), SKIP)
    ok &= _expect("9 judge 自证失败 ⇒ 判红（judge 坏了分数不可信）",
                  lambda r: r["llm_judge"]["control"].update(
                      {"ok": False, "neg_ok": False, "note": "变异"}), FAIL)
    ok &= _expect("10 语义层未启用 ⇒ SKIP", lambda r: r.update(
        {"llm_judge": {"enabled": False}}), SKIP)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help="eval_generation_quality.py 产出的报告 JSON")
    ap.add_argument("--allow-missing", action="store_true",
                    help="报告不存在时判 SKIP 而非红（PR 上本地尚未跑过时用）")
    ap.add_argument("--no-mut", action="store_true", help="跳过变异自证")
    args = ap.parse_args()

    print("=" * 74)
    print("生成质量评测门禁（消费报告，不重跑评测）")
    print("=" * 74)
    print("报告: %s" % os.path.relpath(args.report, ROOT).replace("\\", "/"))

    if not os.path.exists(args.report):
        print("\n⚠ 未找到生成质量评测报告 —— 本门禁无法判定。")
        print("  生成方式（两条命令，只读生产库）：")
        print("    .venv/Scripts/python.exe -X utf8 tools/eval/eval_generation_quality.py")
        print("    # --save 可同时落库 eval_reports（eval_kind=generation_quality）")
        print("  ⚠ 这正是评估指出的断链③：生成质量此前无指标、更无门禁。")
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
