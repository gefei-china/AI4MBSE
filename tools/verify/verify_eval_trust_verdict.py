# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-21 补登记（元门禁 G5a/G5b 一直在报「未分类」的存量欠账，非本轮改动引入）。脚本自带声明「CI-OPTIONAL」，故按 local 登记而不擅改其 CI 地位。实测零依赖、1.4s、21/21 全绿，后续可晋 pr。
"""门禁：评测可信度按类型判据（TRUST_VERDICT_BY_KIND_V1）—— CI-OPTIONAL。

纯函数 + 真接口 + 变异自证。防止"用 RAG 负对照判据冤枉 golden_e2e /
generation_quality"再次回归（2026-11-20 米爸反馈）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS ", name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL ", name, extra)


def main():
    from repositories.eval_report_repo import trust_verdict as tv

    print("\n=== P 纯函数：按类型判据 ===")
    # RAG / Trace：顶层 negative_control.recall + 无漂移
    r = tv("rag_retrieval", {"negative_control": {"recall": {"recall@1": 0.1}}}, [])
    check("P1 RAG 有负对照无漂移 → 可信/适用", r == (True, True, "有负对照且无漂移", {"recall@1": 0.1}), str(r))
    r = tv("trace_consistency", {"negative_control": {"recall": 0.2}}, ["drift"])
    check("P2 Trace 有漂移 → 不可信", r[0] is False and "漂移" in r[2], str(r))
    r = tv("rag_retrieval", {}, [])
    check("P3 RAG 无负对照 → 不可信且理由准确", r[0] is False and "无负对照" in r[2], str(r))

    # 生成质量：summary.control.discriminates
    gen_good = {"summary": {"control": {"discriminates": True, "pooled_grounded_rate": 0.01}}}
    r = tv("generation_quality", gen_good, [])
    check("P4 生成质量负对照有判别力 → 可信", r[0] is True and r[1] is True, str(r))
    gen_bad = {"summary": {"control": {"discriminates": False, "pooled_grounded_rate": 0.003}}}
    r = tv("generation_quality", gen_bad, [])
    check("P5 生成质量无判别力 → 不可信（理由指明 discriminates）",
          r[0] is False and "判别力" in r[2], str(r))
    # 回归核心：顶层没有 negative_control 但生成质量有 summary.control，不该判"无负对照"
    check("P6 生成质量不再误报'无负对照'", "无负对照" not in r[2], str(r))

    # golden_e2e / 空类型：判据不适用（旧实现会判不可信）
    for kind in ("golden_e2e", "", "memory_quality", "doc_extract", "intent_routing"):
        r = tv(kind, {}, [])
        check("P7 %s → 不适用（非不可信）" % (kind or "空类型"),
              r[1] is False and "不适用" in r[2], str(r))

    print("\n=== C 引用连续性参考线（修复②）===")
    from repositories.eval_report_repo import metric_status_for as msf
    check("C1 contiguity 0.542 < 0.90 → warn（断号显黄而非灰色不适用）",
          msf("generation_quality", "gen_citation_contiguity_rate", 0.542) == "warn")
    check("C2 contiguity 0.95 ≥ 0.90 → ok",
          msf("generation_quality", "gen_citation_contiguity_rate", 0.95) == "ok")
    check("C3 contiguity 无值 → 空（不适用，不假绿）",
          msf("generation_quality", "gen_citation_contiguity_rate", None) == "")

    print("\n=== S 源码接线 ===")
    src = open(os.path.join(ROOT, "repositories/eval_report_repo.py"), encoding="utf-8").read()
    check("S1 fetch_history 调用纯函数", "trust_verdict(k, obj, drift)" in src)
    check("S2 前端三态化 trust_applicable",
          "trust_applicable" in open(os.path.join(ROOT, "static/js/mods/48-eval.js"), encoding="utf-8").read())
    check("S3 O3 快照空 status 用参考线重算",
          "metric_status_for(k, mk, item.get(mk), thr)" in src)
    check("S4 前端作业动态化（无硬编码 _EVAL_JOBS 常量）",
          "const _EVAL_JOBS = [" not in open(os.path.join(ROOT, "static/js/mods/48-eval.js"), encoding="utf-8").read())
    check("S5 golden_e2e 有中文标签", '"golden_e2e": "端到端黄金集评测"' in src)

    print("\n=== M 变异自证：把 golden_e2e 排除条件删掉，P7 必须翻红 ===")
    fp = os.path.join(ROOT, "repositories/eval_report_repo.py")
    orig = open(fp, "rb").read()
    mutated = orig.decode("utf-8").replace("\r\n", "\n").replace(
        'if kind in ("golden_e2e", "", "memory_quality", "doc_extract", "intent_routing"):',
        'if kind in ("__NEVER__",):').encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False)
    else:
        try:
            open(fp, "wb").write(mutated)
            import importlib
            import repositories.eval_report_repo as _m
            importlib.reload(_m)
            mv = _m.trust_verdict("golden_e2e", {}, [])
            check("M1 删掉排除后 golden_e2e 变成'不可信'（P7 翻红）",
                  mv[1] is True and mv[0] is False, str(mv))
        finally:
            open(fp, "wb").write(orig)
            import importlib
            import repositories.eval_report_repo as _m
            importlib.reload(_m)
            rv = _m.trust_verdict("golden_e2e", {}, [])
            check("M2 还原后恢复'不适用'", rv[1] is False and "不适用" in rv[2], str(rv))

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
