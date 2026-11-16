"""P0-1：端到端 Golden Set v0 的评分器与落库口。

⚠️ 两种模式共用**同一套判据**（这是「架构变更前后的 A/B 能对比」的前提）：
  · offline_replay（默认）：对数据集里每个 source_conv_id 的**历史已落库交付物**评分。
    不重新调用 LLM，不产生新会话，零成本、可复现。它证明的是「判据可计算 +
    历史基线是什么」，**不是**「当前架构现在跑得怎么样」。
  · live：run_arch_ab.py 新建真实会话后，把新 conv_id 传进 score_case()，
    判据不变、只是数据源换成这次真实运行的产物。

⚠️ 判据射程（必须如实声明，不得越界解读）：
  只覆盖「自然语言输入 → 最终交付物」的**存在性 / 结构 / 校验结论 / 编排完成率**，
  不覆盖内容语义质量、不覆盖事实正确性。内容质量需人工标注或 LLM judge（本批未做）。

落库：--save 时写 eval_reports(is_golden=1, eval_kind='golden_e2e', golden_set_id=1)
      + eval_metrics 每例一行。detail 存全量报告（含 limitations）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/eval/golden_e2e.py --save
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, ROOT)
DEFAULT_SET = os.path.join(ROOT, "docs", "评测", "datasets", "golden_e2e_v0.json")
DB = os.path.join(ROOT, "mbse.db")

EVAL_KIND = "golden_e2e"
GOLDEN_SET_ID = 1  # v0 = 1；后续 v1 = 2，别复用

LIMITATIONS = [
    "offline_replay 不重新调用 LLM：结果是历史交付物的回放基线，不代表当前架构的实时表现",
    "判据只覆盖交付物存在性/结构/校验结论/编排完成率，不覆盖内容语义质量与事实正确性",
    "用例从真库派生（固定 conv_id），语料演进时 dataset_id 会变化，须重建基线后再比",
    "逐任务「原本成功→失败」的回归清单由 run_arch_ab.py compare 产出，本评分器不判",
]


def load_dataset(path=DEFAULT_SET):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if d.get("schema") != "p0/golden-e2e@1":
        raise ValueError("不支持的数据集 schema: %s" % d.get("schema"))
    return d


def _ro_conn():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def _verdicts(c, cid):
    out = []
    for r in c.execute("SELECT element_summary FROM sysml_versions WHERE conversation_id=?", (cid,)):
        try:
            v = (json.loads(r[0] or "{}").get("check") or {}).get("verdict")
            if v:
                out.append(str(v))
        except Exception:
            pass
    return sorted(set(out))


# ── 判据单一真源：type → (bool, evidence) ──────────────────────────────────
def _artifact_kind(c, cid, chk):
    kind = chk.get("kind")
    n = c.execute("SELECT COUNT(*) FROM artifacts WHERE conversation_id=? AND kind=?",
                  (cid, kind)).fetchone()[0]
    return n > 0, "artifacts(kind=%s)=%d" % (kind, n)


def _sysml_code(c, cid, chk):
    n = c.execute("SELECT COUNT(*) FROM sysml_versions WHERE conversation_id=? "
                  "AND LENGTH(COALESCE(code_text,''))>0", (cid,)).fetchone()[0]
    m = c.execute("SELECT COUNT(*) FROM artifacts WHERE conversation_id=? AND kind='code'",
                  (cid,)).fetchone()[0]
    return (n + m) > 0, "sysml_versions.code_text=%d / artifacts(code)=%d" % (n, m)


def _check_verdict(c, cid, chk):
    vs = _verdicts(c, cid)
    allowed = set(chk.get("values") or [])
    hit = [v for v in vs if v in allowed]
    return bool(hit), "verdicts=%s allowed=%s hit=%s" % (vs, sorted(allowed), hit)


def _assistant_answer(c, cid, chk):
    n = c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant' "
                  "AND LENGTH(COALESCE(content,''))>0", (cid,)).fetchone()[0]
    return n > 0, "assistant_nonempty=%d" % n


def _orch_done_rate(c, cid, chk):
    total = c.execute("SELECT COUNT(*) FROM agent_tasks WHERE conversation_id=?", (cid,)).fetchone()[0]
    done = c.execute("SELECT COUNT(*) FROM agent_tasks WHERE conversation_id=? AND status='done'",
                     (cid,)).fetchone()[0]
    rate = (done / total) if total else 0.0
    return rate >= float(chk.get("min") or 0), "done=%d/%d rate=%.3f min=%.2f" % (
        done, total, rate, float(chk.get("min") or 0))


CHECKERS = {
    "artifact_kind_present": _artifact_kind,
    "sysml_code_nonempty": _sysml_code,
    "check_verdict_in": _check_verdict,
    "assistant_answer_nonempty": _assistant_answer,
    "orchestration_done_rate_min": _orch_done_rate,
}


def score_case(c, case, conv_id=None):
    """对一例评分。conv_id 覆盖 source_conv_id 即切换到 live 数据源。"""
    cid = int(conv_id if conv_id is not None else case["conv_id"])
    results = []
    for chk in case["checks"]:
        fn = CHECKERS.get(chk.get("type"))
        if fn is None:
            results.append({"type": chk.get("type"), "passed": False,
                            "evidence": "未知判据类型（CHECKERS 未登记）"})
            continue
        ok, ev = fn(c, cid, chk)
        results.append({"type": chk["type"], "passed": bool(ok), "evidence": ev})
    passed = all(x["passed"] for x in results) and bool(results)
    return {
        "case_id": case["id"], "chain": case["chain"], "intent": case["intent"],
        "conv_id": cid, "source_conv_id": case["conv_id"],
        "passed": passed, "score": sum(1 for x in results if x["passed"]) / max(len(results), 1),
        "checks": results,
    }


def score_dataset(c, dataset, mode="offline_replay", conv_id_map=None):
    conv_id_map = conv_id_map or {}
    rows = [score_case(c, cs, conv_id_map.get(cs["id"])) for cs in dataset["cases"]]
    n_pass = sum(1 for r in rows if r["passed"])
    return {
        "schema": "p0/golden-e2e-report@1", "eval_kind": EVAL_KIND,
        "mode": mode, "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["version"], "case_count": len(rows),
        "passed_cases": n_pass, "pass_rate": round(n_pass / max(len(rows), 1), 4),
        "mean_score": round(sum(r["score"] for r in rows) / max(len(rows), 1), 4),
        "by_chain": {ch: {"n": sum(1 for r in rows if r["chain"] == ch),
                          "passed": sum(1 for r in rows if r["chain"] == ch and r["passed"])}
                     for ch in sorted({r["chain"] for r in rows})},
        "rows": rows, "limitations": LIMITATIONS,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_report(conn, report):
    """落 eval_reports(is_golden=1) + eval_metrics 每例一行。"""
    cur = conn.execute(
        "INSERT INTO eval_reports (doc_id, doc_name, detail, created_by, model_version, "
        "is_golden, golden_set_id, eval_kind) VALUES (0,?,?,?,?,1,?,?)",
        ("端到端 Golden Set %s / %s / %d 例 / 通过率 %.2f" %
         (report["dataset_version"], report["mode"], report["case_count"], report["pass_rate"]),
         json.dumps(report, ensure_ascii=False), "tools/eval/golden_e2e.py",
         "golden-e2e dataset_id=%s" % report["dataset_id"],
         GOLDEN_SET_ID, EVAL_KIND))
    conn.commit()
    rid = int(cur.lastrowid)
    for r in report["rows"]:
        conn.execute(
            "INSERT INTO eval_metrics (report_id, eval_kind, metric_key, value, direction, "
            "gate_key, threshold, status) VALUES (?,?,?,?,?,?,?,?)",
            (rid, EVAL_KIND, "case.%s.score" % r["case_id"], round(r["score"], 4),
             "high", "golden_e2e_case", 1.0, "ok" if r["passed"] else "fail"))
    conn.commit()
    return rid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=DEFAULT_SET)
    ap.add_argument("--out", default="")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--mode", default="offline_replay")
    ap.add_argument("--conv-map", default="",
                    help="live 必填：case_id → 新建会话 id 的 JSON 文件")
    args = ap.parse_args()
    ds = load_dataset(args.set)
    conv_map = {}
    if args.conv_map:
        with open(args.conv_map, encoding="utf-8") as f:
            conv_map = {str(k): int(v) for k, v in json.load(f).items()}
    if args.mode == "live":
        # ★ P1 前置防伪造：没有 conv_map 时 score_dataset 会回退 source_conv_id
        # （历史会话）⇒ 落一条「标着 live、实为离线回放」的 eval_reports，
        # 会让 P1 前置被虚假满足。一律拒绝，绝不静默降级。
        if not conv_map:
            print("!! --mode live 必须给 --conv-map <case_id→新conv_id.json>；"
                  "否则评的是历史会话，会伪造 live 基线")
            return 2
        missing = [cs["id"] for cs in ds["cases"] if cs["id"] not in conv_map]
        if missing:
            print("!! --conv-map 缺用例：%s" % ",".join(missing))
            return 2
        if any(int(conv_map[cs["id"]]) == int(cs["conv_id"]) for cs in ds["cases"]):
            print("!! --conv-map 的 conv_id 与 source_conv_id 相同，不是真实新建会话")
            return 2
    c = _ro_conn()
    try:
        report = score_dataset(c, ds, mode=args.mode, conv_id_map=conv_map)
    finally:
        c.close()
    for r in report["rows"]:
        print("%s %-20s passed=%-5s score=%.2f  %s" %
              (r["case_id"], r["chain"], r["passed"], r["score"],
               " | ".join(x["evidence"] for x in r["checks"] if not x["passed"]) or "-"))
    print("TOTAL %d/%d pass_rate=%.4f mean_score=%.4f dataset_id=%s" %
          (report["passed_cases"], report["case_count"], report["pass_rate"],
           report["mean_score"], report["dataset_id"]))
    print("LIMITATIONS:", "; ".join(report["limitations"][:2]))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("WROTE", args.out)
    if args.save:
        conn = sqlite3.connect(DB)
        try:
            rid = save_report(conn, report)
            print("SAVED eval_reports.id=%d (is_golden=1, eval_kind=%s)" % (rid, EVAL_KIND))
        finally:
            conn.close()
    return 0 if report["passed_cases"] == report["case_count"] else 1


if __name__ == "__main__":
    sys.exit(main())
