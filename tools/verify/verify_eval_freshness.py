"""CI-OPTIONAL: [local] P0-3 评测常态化「新鲜度」门禁。

要防的是 `eval_reports` 停更 23 天、`online_eval_queue` 46 条 pending 空转这类
「代码都在、链路没跑」的静默失效 —— 它们不会让任何单测变红，只会让看板说谎。

硬断言（不满足即 rc=1）：
  F1 eval_reports 最新一行距今 <= --max-age-days（默认 7）
  F2 eval_reports 里存在 is_golden=1 的端到端报告
  F3 online_eval_queue 的 pending 数 <= --max-pending（默认 0）
  F4 Golden Set 基线报告存在且 dataset_id 与当前数据集一致（防语料漂移后沿用旧基线）
软断言（只记录 warning，不阻断）：
  W1 docs/评测/nightly/latest.json 的新鲜度 —— 本机跑批器需人工触发，
     CI 干净库没有 mbse.db，不能拿它当 PR 硬门槛；陈旧时明确提示去跑
     `tools/eval/run_nightly_local.py`。

输出 docs/评测/eval_freshness_latest.json（checked-in，供 PR 侧消费），不写业务库、不调 LLM。
裸跑：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_eval_freshness.py
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "eval"))
import golden_e2e as G  # noqa: E402

DB = os.path.join(ROOT, "mbse.db")
NIGHTLY = os.path.join(ROOT, "docs", "评测", "nightly", "latest.json")
BASELINE = os.path.join(ROOT, "docs", "评测", "golden_e2e_v0_baseline.json")
OUT = os.path.join(ROOT, "docs", "评测", "eval_freshness_latest.json")

PASS, FAIL = 0, 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name, detail)
    else:
        FAIL += 1
        print("  FAIL", name, detail)


def _age_days(ts):
    if not ts:
        return 10 ** 6
    try:
        # eval_reports 是「YYYY-MM-DD HH:MM:SS」；nightly/latest.json 的
        # updated_at 是 ISO「YYYY-MM-DDTHH:MM:SS」—— 两种口径都要认，
        # 否则会报出 1000000 天这种假数字（数字口径纪律）。
        d = datetime.strptime(str(ts)[:19].replace("T", " "),
                              "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400.0
    except Exception:
        return 10 ** 6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=float, default=7.0)
    ap.add_argument("--max-pending", type=int, default=0)
    args = ap.parse_args()
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    try:
        latest = c.execute("SELECT MAX(created_at) FROM eval_reports").fetchone()[0]
        n_reports = c.execute("SELECT COUNT(*) FROM eval_reports").fetchone()[0]
        n_golden = c.execute("SELECT COUNT(*) FROM eval_reports WHERE is_golden=1").fetchone()[0]
        n_pending = c.execute(
            "SELECT COUNT(*) FROM online_eval_queue WHERE status='pending'").fetchone()[0]
        n_dismissed = c.execute(
            "SELECT COUNT(*) FROM online_eval_queue WHERE status='dismissed'").fetchone()[0]
        by_kind = {str(r[0]): int(r[1]) for r in c.execute(
            "SELECT eval_kind,COUNT(*) FROM eval_reports GROUP BY 1")}
    finally:
        c.close()
    age = _age_days(latest)
    print("[F] 硬断言")
    ck("F1 eval_reports 新鲜（<= %.0f 天）" % args.max_age_days, age <= args.max_age_days,
       "latest=%s age=%.2f d rows=%d" % (latest, age, n_reports))
    ck("F2 存在 is_golden=1 端到端报告", n_golden >= 1, "n=%d" % n_golden)
    ck("F3 online_eval_queue pending <= %d" % args.max_pending, n_pending <= args.max_pending,
       "pending=%d dismissed=%d" % (n_pending, n_dismissed))
    ds = G.load_dataset()
    base_ok = os.path.isfile(BASELINE)
    base = json.load(open(BASELINE, encoding="utf-8")) if base_ok else {}
    ck("F4 Golden Set 基线未漂移", base_ok and base.get("dataset_id") == ds["dataset_id"],
       "base=%s set=%s" % (base.get("dataset_id"), ds["dataset_id"]))

    print("[W] 软断言（只记录，不阻断）")
    warnings = []
    nightly_age = None
    if os.path.isfile(NIGHTLY):
        try:
            nj = json.load(open(NIGHTLY, encoding="utf-8"))
            nightly_age = _age_days(nj.get("generated_at") or nj.get("updated_at")
                                    or nj.get("when") or "")
        except Exception:
            nightly_age = None
        if nightly_age is None or nightly_age > args.max_age_days:
            warnings.append("nightly/latest.json 陈旧或未带时间戳（age=%s d）；"
                            "本机跑批器 tools/eval/run_nightly_local.py 需人工触发" % nightly_age)
    else:
        warnings.append("nightly/latest.json 不存在")
    for w in warnings:
        print("  WARN", w)

    out = {
        "schema": "p0/eval-freshness@1",
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {"max_age_days": args.max_age_days, "max_pending": args.max_pending},
        "eval_reports": {"rows": n_reports, "latest_created_at_utc": latest,
                         "age_days": round(age, 2), "is_golden_rows": n_golden,
                         "by_eval_kind": by_kind},
        "online_eval_queue": {"pending": n_pending, "dismissed": n_dismissed},
        "golden_set": {"dataset_id": ds["dataset_id"], "cases": ds["case_count"],
                       "baseline_dataset_id": base.get("dataset_id")},
        "nightly_latest_age_days": nightly_age,
        "warnings": warnings, "pass": PASS, "fail": FAIL,
        "note": "库内 created_at 为 UTC；本门禁只读业务库，不调用 LLM、不写业务库",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("RESULT: %d PASS / %d FAIL → %s" % (PASS, FAIL, OUT))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
